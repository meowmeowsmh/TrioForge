# gmail.py — a TrioForge PLUGIN that gives the agent a Gmail connector.
#
# No Google Cloud. No Client ID. No Client Secret. No OAuth redirect.
#
# A self-hosted app cannot be "Claude for Google Drive" (a centrally registered Google
# app) - so instead of making you create one, this talks to Gmail over IMAP with a
# Google **App Password**: one value you generate on your own account page and paste
# once. That is the whole setup.
#
#   1. myaccount.google.com -> Security -> 2-Step Verification (must be ON)
#   2. Security -> App passwords -> create one for "Mail"
#   3. Paste it into 🔌 Connectors (with your Gmail address) -> Connect
#
# Read-only by default. Nothing is sent unless you also enable the send scope below.

import email
import imaplib
import json
import os
import re
import smtplib
from email.header import decode_header, make_header
from email.mime.text import MIMEText

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CRED_PATH = os.path.join(REPO_ROOT, "json_configuration", "gmail_credentials.json")

IMAP_HOST = "imap.gmail.com"
SMTP_HOST = "smtp.gmail.com"

MANIFEST = {
    "name": "gmail",
    "title": "Gmail",
    "version": "1.1.0",
    "description": "Read your Gmail with an app password - no Google Cloud, no Client ID.",
    # The Connectors panel renders these inputs from this spec, so the UI stays generic.
    "credentials": [
        {"key": "email", "label": "Gmail address", "type": "text",
         "placeholder": "you@gmail.com"},
        {"key": "app_password", "label": "App password", "type": "password",
         "placeholder": "16 characters, no spaces",
         "hint": "Google Account -> Security -> 2-Step Verification -> App passwords "
                 "-> create one for Mail, then paste it here."},
    ],
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "gmail_list_inbox",
            "description": "List recent emails in the connected Gmail account. Query uses Gmail syntax, e.g. 'is:unread', 'from:someone@x.com', 'newer_than:2d'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Gmail search query, e.g. is:unread"},
                    "max": {"type": "integer", "description": "max messages to return (default 10)"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gmail_read_message",
            "description": "Read the body of one Gmail message by id (as returned by gmail_list_inbox).",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string", "description": "message id"}},
                "required": ["id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gmail_status",
            "description": "Check whether Gmail is connected and which account.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]


def _creds():
    """{"email": …, "app_password": …} from the credentials file or the environment."""
    cfg = {}
    if os.path.isfile(_CRED_PATH):
        try:
            with open(_CRED_PATH, encoding="utf-8") as fh:
                cfg = json.load(fh) or {}
        except Exception:
            cfg = {}
    email_addr = (cfg.get("email") or os.environ.get("GMAIL_ADDRESS") or "").strip()
    app_password = (cfg.get("app_password") or os.environ.get("GMAIL_APP_PASSWORD") or "").strip()
    return email_addr, app_password.replace(" ", "")


def _imap():
    """A logged-in IMAP connection, or (None, error)."""
    addr, pw = _creds()
    if not addr or not pw:
        return None, "not connected"
    try:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, 993)
        conn.login(addr, pw)
        return conn, None
    except imaplib.IMAP4.error as exc:
        return None, "sign-in failed: {}".format(exc)
    except Exception as exc:
        return None, str(exc)


def _decode(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _search(conn, query):
    """Gmail query syntax via X-GM-RAW, falling back to plain text search."""
    if not query:
        typ, data = conn.uid("search", None, "ALL")
        return data[0].split() if typ == "OK" else []
    try:
        typ, data = conn.uid("search", None, "X-GM-RAW", '"{}"'.format(query.replace('"', "")))
        if typ == "OK":
            return data[0].split()
    except Exception:
        pass
    # Fallback: translate the few common Gmail operators into IMAP ones.
    imap_query = query
    imap_query = re.sub(r"\bis:unread\b", "UNSEEN", imap_query, flags=re.I)
    imap_query = re.sub(r"\bis:read\b", "SEEN", imap_query, flags=re.I)
    imap_query = re.sub(r"\bfrom:(\S+)", r'FROM "\1"', imap_query, flags=re.I)
    imap_query = re.sub(r"\bsubject:(\S+)", r'SUBJECT "\1"', imap_query, flags=re.I)
    if not re.search(r"\b(ALL|UNSEEN|SEEN|FROM|SUBJECT|TO|SINCE|BEFORE)\b", imap_query, re.I):
        imap_query = 'TEXT "{}"'.format(query.replace('"', ""))
    try:
        typ, data = conn.uid("search", None, imap_query)
        return data[0].split() if typ == "OK" else []
    except Exception:
        return []


def _list_inbox(query, max_results):
    conn, err = _imap()
    if conn is None:
        return {"error": "Gmail is not connected ({}).".format(err)}
    try:
        try:
            max_results = min(max(int(max_results or 10), 1), 50)
        except (TypeError, ValueError):
            max_results = 10
        conn.select("INBOX", readonly=True)
        uids = _search(conn, query or "")
        uids = uids[-max_results:]
        out = []
        for uid in reversed(uids):                      # newest first
            typ, data = conn.uid(
                "fetch", uid,
                "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)] FLAGS)")
            if typ != "OK" or not data:
                continue
            raw = b""
            flags = ""
            for part in data:
                if isinstance(part, tuple):
                    raw += part[1] or b""
                elif isinstance(part, bytes):
                    flags += part.decode("utf-8", "replace")
            msg = email.message_from_bytes(raw)
            out.append({
                "id": uid.decode() if isinstance(uid, bytes) else str(uid),
                "from": _decode(msg.get("From")),
                "subject": _decode(msg.get("Subject")),
                "date": _decode(msg.get("Date")),
                "snippet": "",
                "unread": "\\Seen" not in flags,
            })
        return {"messages": out, "count": len(out)}
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _read_message(mid):
    conn, err = _imap()
    if conn is None:
        return {"error": "Gmail is not connected ({}).".format(err)}
    try:
        conn.select("INBOX", readonly=True)
        typ, data = conn.uid("fetch", str(mid).encode(), "(RFC822)")
        if typ != "OK" or not data:
            return {"error": "message {} not found".format(mid)}
        raw = b""
        for part in data:
            if isinstance(part, tuple):
                raw += part[1] or b""
        msg = email.message_from_bytes(raw)
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    try:
                        body = part.get_payload(decode=True).decode(
                            part.get_content_charset() or "utf-8", "replace")
                        break
                    except Exception:
                        continue
        else:
            try:
                body = msg.get_payload(decode=True).decode(
                    msg.get_content_charset() or "utf-8", "replace")
            except Exception:
                body = ""
        return {"id": str(mid), "from": _decode(msg.get("From")),
                "subject": _decode(msg.get("Subject")), "date": _decode(msg.get("Date")),
                "body": body[:8000]}
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _status():
    addr, pw = _creds()
    if not addr or not pw:
        return {"connected": False, "configured": False, "needs_credentials": True,
                "account": None,
                "hint": "Create a Google App Password (Security -> App passwords) and "
                        "paste it here with your Gmail address. No Client ID needed."}
    conn, err = _imap()
    if conn is None:
        return {"connected": False, "configured": True, "needs_credentials": False,
                "account": addr, "error": err}
    try:
        conn.logout()
    except Exception:
        pass
    return {"connected": True, "configured": True, "needs_credentials": False,
            "account": addr}


def dispatch(tool_name, args):
    """Run one of this plugin's tools (called by the agent loop)."""
    args = args or {}
    if tool_name == "gmail_list_inbox":
        return _list_inbox(args.get("query"), args.get("max"))
    if tool_name == "gmail_read_message":
        return _read_message(args.get("id"))
    if tool_name == "gmail_status":
        return _status()
    return {"error": "unknown tool " + str(tool_name)}


def register(app):
    """The HTTP side: save credentials, connect/disconnect, raw inbox endpoints."""
    from flask import jsonify, request

    @app.route("/api/connectors/gmail/status")
    def _status_route():
        return jsonify(_status())

    @app.route("/api/connectors/gmail/credentials", methods=["POST"])
    def _credentials_route():
        data = request.get_json(silent=True) or {}
        addr = (data.get("email") or "").strip()
        pw = (data.get("app_password") or "").strip().replace(" ", "")
        if not addr or not pw:
            return jsonify({"error": "Both your Gmail address and the app password "
                                     "are required."}), 400
        try:
            os.makedirs(os.path.dirname(_CRED_PATH), exist_ok=True)
            with open(_CRED_PATH, "w", encoding="utf-8") as fh:
                json.dump({"email": addr, "app_password": pw}, fh, indent=2)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        st = _status()
        if not st.get("connected"):
            return jsonify({"error": "Saved, but Gmail refused the sign-in: {}".format(
                st.get("error") or "check the app password")}), 400
        return jsonify({"ok": True, "account": st.get("account")})

    @app.route("/api/connectors/gmail/connect", methods=["POST"])
    def _connect_route():
        return jsonify(_status())

    @app.route("/api/connectors/gmail/auth")
    def _auth_route():
        # No OAuth in this connector - "connecting" is saving a working app password.
        st = _status()
        if st.get("connected"):
            return jsonify({"ok": True, "account": st.get("account")})
        return jsonify({"error": st.get("hint") or "Not connected.",
                        "needs_credentials": True}), 400

    @app.route("/api/connectors/gmail/inbox")
    def _inbox_route():
        return jsonify(_list_inbox(request.args.get("query", ""), request.args.get("max")))

    @app.route("/api/connectors/gmail/disconnect", methods=["POST"])
    def _disconnect_route():
        try:
            if os.path.isfile(_CRED_PATH):
                os.remove(_CRED_PATH)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify({"ok": True})
