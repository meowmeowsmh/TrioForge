# gmail.py — a TrioForge PLUGIN that gives the agent a Gmail connector.
#
# Primary flow is the normal one: click "Sign in with Google", pick an account, Allow.
# That needs a Google OAuth client (the "app"), which a hosted service like Claude owns
# centrally but a self-hosted app does not - so the INSTALL OWNER sets it once (see
# "One-time app setup" below) and after that everybody just clicks the button.
#
# An App Password over IMAP is kept as an automatic fallback for anyone who would
# rather not create a Google app at all.
#
# One-time app setup (the install owner, ~5 minutes, once):
#   1. console.cloud.google.com -> new project
#   2. APIs & Services -> Library -> enable "Gmail API"
#   3. APIs & Services -> OAuth consent screen -> External -> add yourself as a Test user
#   4. Credentials -> Create credentials -> OAuth client ID -> "Web application"
#   5. Authorized redirect URIs -> add exactly:
#          https://127.0.0.1:5003/api/connectors/gmail/oauth2callback
#   6. Paste the Client ID + Client Secret into 🔌 Connectors -> Save app
#   7. Click "Sign in with Google" -> pick account -> Allow. Done.

import base64
import email
import imaplib
import json
import os
import re
import time
from email.header import decode_header, make_header

import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CRED_PATH = os.path.join(REPO_ROOT, "json_configuration", "gmail_credentials.json")

IMAP_HOST = "imap.gmail.com"
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/"
SCOPES = "https://www.googleapis.com/auth/gmail.readonly"

# ── Bake the app in here to make the fields disappear for ever ────────────────
# Fill these two (or set GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET in the environment) and
# the Connectors panel NEVER asks anybody for credentials again - it shows only
# "Sign in with Google". This is how a published build ships the Claude experience.
DEFAULT_CLIENT_ID = ""
DEFAULT_CLIENT_SECRET = ""

MANIFEST = {
    "name": "gmail",
    "title": "Gmail",
    "version": "1.3.0",
    "description": "Sign in with Google so the agent can read your Gmail.",
    # Marks this as a CONNECTOR: a service the user signs into, which is a different
    # thing from a plugin that merely exposes tools. Connectors appear in the 🔌
    # panel, where the account and its sign-in state are the point.
    "connector": True,
    # One click in the 🔌 panel opens this to enable the Gmail API in Google Cloud.
    "enable_url": "https://console.cloud.google.com/apis/library/gmail.googleapis.com",
    # Rendered by the Connectors panel. This is the one-time APP setup, not a
    # per-user login - after it is saved, users only ever click Sign in with Google.
    "credentials": [
        {"key": "oauth_client_id", "label": "Google Client ID (one-time app setup)",
         "type": "text", "placeholder": "…apps.googleusercontent.com"},
        {"key": "oauth_client_secret", "label": "Google Client Secret", "type": "password",
         "placeholder": "GOCSPX-…"},
    ],
    # A built-in guide, shown in the panel itself: the setup must not depend on
    # somebody finding and following a README.
    "guide": [
        "1. console.cloud.google.com -> create a project, and stay in THAT project for "
        "every step below (the #1 cause of failures is doing one step in a different project).",
        "2. APIs & Services -> Library -> search 'Gmail API' -> ENABLE.",
        "3. Google Auth Platform -> Audience -> Test users -> add YOUR OWN Gmail address "
        "-> Save. (Skip this and Google answers with 'access_denied'.)",
        "4. Credentials -> + Create credentials -> OAuth client ID -> type: Web application.",
        "5. Authorized redirect URIs -> + ADD URI -> paste exactly "
        "https://127.0.0.1:5003/api/connectors/gmail/oauth2callback -> then click SAVE "
        "(the URI does nothing until you save).",
        "6. Copy the Client ID + Client secret, paste them here -> Save app.",
        "7. Click 'Sign in with Google' -> pick your account -> Continue. The tab then "
        "says 'Gmail connected'.",
    ],
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "gmail_list_inbox",
            "description": "List the user's recent Gmail emails. Use an EMPTY query ('') for the most recent messages, or 'is:unread' for unread ones. Only add a filter (from:, subject:, newer_than:) when the user explicitly asks — do NOT invent filters such as 'is:important'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Gmail search query, e.g. 'is:unread' or 'from:x@y.com'. Leave empty ('') for the most recent emails."},
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
            "description": "Read the body of one Gmail message by id (from gmail_list_inbox).",
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

_token_cache = {"access": None, "expires": 0.0}


# ── config ────────────────────────────────────────────────────────────────────
def _cfg():
    if os.path.isfile(_CRED_PATH):
        try:
            with open(_CRED_PATH, encoding="utf-8") as fh:
                return json.load(fh) or {}
        except Exception:
            return {}
    return {}


def _save_cfg(updates):
    cfg = _cfg()
    cfg.update(updates)
    os.makedirs(os.path.dirname(_CRED_PATH), exist_ok=True)
    with open(_CRED_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


def _oauth_client():
    cfg = _cfg()
    cid = ((cfg.get("oauth_client_id") or "").strip()
           or os.environ.get("GMAIL_CLIENT_ID", "").strip()
           or DEFAULT_CLIENT_ID.strip())
    secret = ((cfg.get("oauth_client_secret") or "").strip()
              or os.environ.get("GMAIL_CLIENT_SECRET", "").strip()
              or DEFAULT_CLIENT_SECRET.strip())
    return cid, secret


# ── OAuth (the normal "Sign in with Google") ──────────────────────────────────
def auth_url(redirect_uri):
    cid, _ = _oauth_client()
    if not cid:
        return None, "no Google app configured"
    params = {
        "client_id": cid, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": SCOPES, "access_type": "offline", "prompt": "consent",
        "include_granted_scopes": "true",
    }
    query = "&".join("{}={}".format(k, requests.utils.quote(str(v), safe=""))
                     for k, v in params.items())
    return GOOGLE_AUTH + "?" + query, None


def exchange_code(code, redirect_uri):
    cid, secret = _oauth_client()
    if not cid or not secret:
        return None, "no Google app configured"
    r = requests.post(GOOGLE_TOKEN, data={
        "code": code, "client_id": cid, "client_secret": secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code"}, timeout=30)
    if r.status_code != 200:
        return None, "token exchange failed: {}".format(r.text[:300])
    data = r.json()
    _save_cfg({"refresh_token": data.get("refresh_token", ""),
               "access_token": data.get("access_token", ""),
               "token_expires": time.time() + int(data.get("expires_in", 3600))})
    _token_cache["access"] = data.get("access_token")
    _token_cache["expires"] = time.time() + int(data.get("expires_in", 3600)) - 30
    return data, None


def _access_token():
    """A live access token, refreshing from the stored refresh_token as needed."""
    cid, secret = _oauth_client()
    cfg = _cfg()
    refresh = cfg.get("refresh_token")
    if not cid or not secret or not refresh:
        return None, "not signed in"
    now = time.time()
    if _token_cache["access"] and _token_cache["expires"] > now:
        return _token_cache["access"], None
    if cfg.get("access_token") and float(cfg.get("token_expires") or 0) > now:
        _token_cache["access"] = cfg["access_token"]
        _token_cache["expires"] = float(cfg["token_expires"])
        return cfg["access_token"], None
    r = requests.post(GOOGLE_TOKEN, data={
        "client_id": cid, "client_secret": secret, "refresh_token": refresh,
        "grant_type": "refresh_token"}, timeout=30)
    if r.status_code != 200:
        return None, "token refresh failed: {}".format(r.text[:200])
    data = r.json()
    _token_cache["access"] = data.get("access_token")
    _token_cache["expires"] = time.time() + int(data.get("expires_in", 3600)) - 30
    _save_cfg({"access_token": data.get("access_token", ""),
               "token_expires": time.time() + int(data.get("expires_in", 3600))})
    return data.get("access_token"), None


def _api(path, params=None):
    tok, err = _access_token()
    if tok is None:
        return None, err
    r = requests.get(GMAIL_API + path, headers={"Authorization": "Bearer " + tok},
                     params=params or {}, timeout=30)
    if r.status_code != 200:
        return None, "Gmail API {}: {}".format(r.status_code, r.text[:200])
    return r.json(), None


def _hdr(headers, name):
    for h in headers or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _list_oauth(query, max_results):
    params = {"maxResults": max_results}
    if query:
        params["q"] = query
    data, err = _api("users/me/messages", params)
    if err:
        return {"error": err}
    out = []
    for m in data.get("messages", []):
        # metadataHeaders must be REPEATED params (…&metadataHeaders=From&…). Passing
        # one comma-joined string made the API ignore it and return no headers at all,
        # so every message came back with an empty Subject/From.
        meta, err2 = _api("users/me/messages/" + m["id"],
                          {"format": "metadata",
                           "metadataHeaders": ["From", "Subject", "Date"]})
        if err2:
            continue
        payload = meta.get("payload", {})
        out.append({
            "id": m["id"],
            "from": _hdr(payload.get("headers"), "From"),
            "subject": _hdr(payload.get("headers"), "Subject"),
            "date": _hdr(payload.get("headers"), "Date"),
            "snippet": meta.get("snippet", ""),
            "unread": "UNREAD" in meta.get("labelIds", []),
        })
    return {"messages": out, "count": len(out), "method": "oauth"}


def _read_oauth(mid):
    data, err = _api("users/me/messages/" + str(mid), {"format": "full"})
    if err:
        return {"error": err}
    payload = data.get("payload", {})
    body = ""

    def walk(part):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"] + "===").decode("utf-8", "replace")
        for sub in part.get("parts", []) or []:
            got = walk(sub)
            if got:
                return got
        return ""

    body = walk(payload)
    return {"id": str(mid), "from": _hdr(payload.get("headers"), "From"),
            "subject": _hdr(payload.get("headers"), "Subject"),
            "date": _hdr(payload.get("headers"), "Date"),
            "body": body[:8000], "method": "oauth"}


# ── App-password fallback (IMAP) ──────────────────────────────────────────────
def _imap():
    cfg = _cfg()
    addr = (cfg.get("email") or "").strip()
    pw = (cfg.get("app_password") or "").strip().replace(" ", "")
    if not addr or not pw:
        return None, "no app password stored"
    try:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, 993)
        conn.login(addr, pw)
        return conn, None
    except imaplib.IMAP4.error as exc:
        text = exc.decode("utf-8", "replace") if isinstance(exc, bytes) else str(exc)
        if "AUTHENTICATIONFAILED" in text or "Invalid credentials" in text:
            return None, ("Gmail rejected the app password. Check that 2-Step Verification "
                          "is ON and that you pasted the 16-letter App Password, not your "
                          "normal Google password.")
        return None, text
    except Exception as exc:
        return None, str(exc)


def _decode(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _list_imap(query, max_results):
    conn, err = _imap()
    if conn is None:
        return {"error": "Gmail is not connected ({})".format(err)}
    try:
        conn.select("INBOX", readonly=True)
        if query:
            typ, data = conn.uid("search", None, 'X-GM-RAW', '"{}"'.format(query.replace('"', "")))
        else:
            typ, data = conn.uid("search", None, "ALL")
        uids = data[0].split() if typ == "OK" else []
        out = []
        for uid in reversed(uids[-max_results:]):
            typ, data = conn.uid("fetch", uid,
                                 "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)] FLAGS)")
            if typ != "OK":
                continue
            raw, flags = b"", ""
            for part in data:
                if isinstance(part, tuple):
                    raw += part[1] or b""
                elif isinstance(part, bytes):
                    flags += part.decode("utf-8", "replace")
            msg = email.message_from_bytes(raw)
            out.append({"id": uid.decode(), "from": _decode(msg.get("From")),
                        "subject": _decode(msg.get("Subject")),
                        "date": _decode(msg.get("Date")), "snippet": "",
                        "unread": "\\Seen" not in flags})
        return {"messages": out, "count": len(out), "method": "app_password"}
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _read_imap(mid):
    conn, err = _imap()
    if conn is None:
        return {"error": "Gmail is not connected ({})".format(err)}
    try:
        conn.select("INBOX", readonly=True)
        typ, data = conn.uid("fetch", str(mid).encode(), "(RFC822)")
        if typ != "OK":
            return {"error": "message not found"}
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
                "body": body[:8000], "method": "app_password"}
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        try:
            conn.logout()
        except Exception:
            pass


# ── tools ─────────────────────────────────────────────────────────────────────
def _list_inbox(query, max_results):
    try:
        max_results = min(max(int(max_results or 10), 1), 50)
    except (TypeError, ValueError):
        max_results = 10
    if _cfg().get("refresh_token"):
        return _list_oauth(query, max_results)
    return _list_imap(query, max_results)


def _read_message(mid):
    if _cfg().get("refresh_token"):
        return _read_oauth(mid)
    return _read_imap(mid)


def _status_base():
    cfg = _cfg()
    cid, secret = _oauth_client()
    if cfg.get("refresh_token") and cid:
        data, err = _api("users/me/profile")
        if err:
            return {"connected": False, "configured": True, "needs_client": False,
                    "method": "oauth", "error": err}
        return {"connected": True, "configured": True, "needs_client": False,
                "method": "oauth", "account": data.get("emailAddress")}
    if cfg.get("email") and cfg.get("app_password"):
        conn, err = _imap()
        if conn is None:
            return {"connected": False, "configured": True, "needs_client": not cid,
                    "method": "app_password", "account": cfg.get("email"), "error": err}
        try:
            conn.logout()
        except Exception:
            pass
        return {"connected": True, "configured": True, "needs_client": False,
                "method": "app_password", "account": cfg.get("email")}
    if cid and secret:
        return {"connected": False, "configured": True, "needs_client": False,
                "method": None, "account": None}
    return {"connected": False, "configured": False, "needs_client": True,
            "method": None, "account": None,
            "hint": "The install owner sets the Google app once (Client ID + Secret); "
                    "after that everyone just clicks Sign in with Google."}


def _status():
    """Status plus the two flags the connector card renders from.

    ``needs_setup`` -> show the declared credential fields.
    ``can_sign_in`` -> show a sign-in button.
    Stating both explicitly is what lets one card render Gmail (a sign-in) and
    Obsidian (just a folder path) without the panel special-casing either.
    """
    st = _status_base()
    st.setdefault("needs_setup", bool(st.get("needs_client")))
    st["can_sign_in"] = bool(st.get("configured")) and not bool(st.get("connected"))
    st["sign_in_label"] = "🔗 Sign in with Google"
    return st


def connect_info():
    """Terminal sign-in for the TUI: report status, or hand back the OAuth URL.

    The redirect lands on TrioForge's web server (port 5003), which writes the
    token to the shared credentials file; the TUI then polls _status() until it
    flips to connected. So the web app must be running to catch the redirect.
    """
    st = _status()
    if st.get("connected"):
        return {"connected": True, "account": st.get("account"), "method": st.get("method")}
    if st.get("needs_client") or not st.get("configured"):
        return {"error": "the Google app is not set up yet (Client ID + Secret). "
                         "Set it in the web UI (Connectors panel) or ship it in "
                         "DEFAULT_CLIENT_ID / GMAIL_CLIENT_ID."}
    url, err = auth_url("https://127.0.0.1:5003/api/connectors/gmail/oauth2callback")
    if url is None:
        return {"error": err or "could not build the sign-in URL"}
    return {"url": url,
            "note": "sign in, and TrioForge's web server (https://127.0.0.1:5003) "
                    "must be running to catch the redirect"}


def dispatch(tool_name, args):
    args = args or {}
    if tool_name == "gmail_list_inbox":
        return _list_inbox(args.get("query"), args.get("max"))
    if tool_name == "gmail_read_message":
        return _read_message(args.get("id"))
    if tool_name == "gmail_status":
        # A plain-language summary, NOT the raw card flags (needs_client /
        # can_sign_in / needs_setup). Those UI flags are what made a model read
        # "connected:true, can_sign_in:false" as "not signed in" and refuse.
        st = _status()
        if st.get("connected"):
            return {"connected": True, "account": st.get("account"),
                    "summary": "Gmail IS signed in as {}. You can list and read email "
                               "right now — call gmail_list_inbox.".format(st.get("account"))}
        return {"connected": False,
                "summary": "Gmail is not signed in yet. Ask the user to connect it "
                           "in the Connectors panel."}
    return {"error": "unknown tool " + str(tool_name)}


# ── HTTP ──────────────────────────────────────────────────────────────────────
def register(app):
    from flask import jsonify, request

    def _redirect_uri():
        return "{}://{}/api/connectors/gmail/oauth2callback".format(
            request.scheme or "https", request.host)

    @app.route("/api/connectors/gmail/status")
    def _status_route():
        return jsonify(_status())

    @app.route("/api/connectors/gmail/credentials", methods=["POST"])
    def _credentials_route():
        """Save the one-time Google app (Client ID/Secret) or an app-password pair."""
        data = request.get_json(silent=True) or {}
        if data.get("oauth_client_id"):
            cid = (data.get("oauth_client_id") or "").strip()
            secret = (data.get("oauth_client_secret") or "").strip()
            if not secret:
                return jsonify({"error": "Client Secret is required too."}), 400
            _save_cfg({"oauth_client_id": cid, "oauth_client_secret": secret})
            return jsonify({"ok": True})
        addr = (data.get("email") or "").strip()
        pw = (data.get("app_password") or "").strip().replace(" ", "")
        if not addr or not pw:
            return jsonify({"error": "Gmail address and app password are both required."}), 400
        if not re.fullmatch(r"[a-zA-Z]{16}", pw):
            return jsonify({"error": "That is not a Google App Password (16 letters). "
                                     "Your normal Google password cannot work here."}), 400
        _save_cfg({"email": addr, "app_password": pw.lower()})
        st = _status()
        if not st.get("connected"):
            return jsonify({"error": st.get("error") or "sign-in failed"}), 400
        return jsonify({"ok": True, "account": st.get("account")})

    @app.route("/api/connectors/gmail/auth")
    def _auth_route():
        url, err = auth_url(_redirect_uri())
        if url is None:
            return jsonify({"error": "Set the Google app first (Client ID + Secret).",
                            "needs_client": True}), 400
        return jsonify({"auth_url": url})

    @app.route("/api/connectors/gmail/oauth2callback")
    def _callback_route():
        oauth_error = request.args.get("error")
        if oauth_error:
            return ("<html><body style='background:#0b0d12;color:#f0883e;font-family:Segoe UI;"
                    "text-align:center;padding-top:12vh'><h2>Google refused the sign-in</h2>"
                    "<p style='font-family:monospace'>{}</p>"
                    "<p style='color:#8b949e'>If this says redirect_uri_mismatch, add exactly "
                    "{} to Authorized redirect URIs on the OAuth client.</p></body></html>"
                    ).format(oauth_error, _redirect_uri())
        code = request.args.get("code")
        if not code:
            return "<h3>missing code</h3><p>Start from /api/connectors/gmail/auth.</p>", 400
        data, err = exchange_code(code, _redirect_uri())
        if err:
            # A Google authorization code is single-use. When the callback is reached
            # twice (the browser retries, or the tab is reopened), the SECOND exchange
            # fails with invalid_grant even though the first one already stored a valid
            # token - so a scary "Sign-in failed" was being shown for a sign-in that
            # had actually worked. Report the real state instead.
            st = _status()
            if st.get("connected"):
                return ("<html><body style='background:#0b0d12;color:#e6edf3;"
                        "font-family:Segoe UI;text-align:center;padding-top:15vh'>"
                        "<h2>Gmail connected</h2><p>{}</p>"
                        "<p style='color:#8b949e'>You can close this tab.</p>"
                        "</body></html>").format(st.get("account") or "")
            if "invalid_grant" in err:
                return ("<html><body style='background:#0b0d12;color:#f0883e;"
                        "font-family:Segoe UI;text-align:center;padding-top:14vh'>"
                        "<h2>This sign-in link was already used</h2>"
                        "<p style='color:#8b949e'>Google codes work once. Start again from "
                        "🔌 Connectors → Sign in with Google.</p></body></html>"), 400
            return ("<html><body style='background:#0b0d12;color:#f0883e;font-family:Segoe UI;"
                    "text-align:center;padding-top:14vh'><h2>Sign-in failed</h2>"
                    "<pre style='color:#8b949e'>{}</pre></body></html>").format(err), 400
        st = _status()
        return ("<html><body style='background:#0b0d12;color:#e6edf3;font-family:Segoe UI;"
                "text-align:center;padding-top:15vh'><h2>Gmail connected</h2>"
                "<p>{}</p><p style='color:#8b949e'>You can close this tab.</p></body></html>"
                ).format(st.get("account") or "")

    @app.route("/api/connectors/gmail/inbox")
    def _inbox_route():
        return jsonify(_list_inbox(request.args.get("query", ""), request.args.get("max")))

    @app.route("/api/connectors/gmail/disconnect", methods=["POST"])
    def _disconnect_route():
        """Sign out, but KEEP the one-time Google app credentials.

        Disconnecting should not make the install owner re-enter the Client ID/Secret -
        it only drops this account's token (and any app password), so the panel goes
        straight back to a plain "Sign in with Google" button.
        """
        try:
            cfg = _cfg()
            for key in ("refresh_token", "access_token", "token_expires",
                        "email", "app_password"):
                cfg.pop(key, None)
            os.makedirs(os.path.dirname(_CRED_PATH), exist_ok=True)
            with open(_CRED_PATH, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2)
            _token_cache["access"] = None
            _token_cache["expires"] = 0.0
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify({"ok": True})
