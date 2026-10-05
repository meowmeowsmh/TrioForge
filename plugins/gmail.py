# gmail.py — a TrioForge PLUGIN that gives the agent a Gmail connector.
#
# This is the difference from a plain route-plugin: besides the OAuth routes, it
# advertises TOOLS (gmail_list_inbox / gmail_read_message / gmail_status) and a
# dispatch() hook, so when you ask the agent "summarize my inbox" it actually calls
# these tools and reads your mail.
#
# One-time setup (only you can do this part):
#   1. console.cloud.google.com -> create a project -> enable the Gmail API
#   2. OAuth consent screen -> add yourself as a test user
#   3. Credentials -> OAuth client ID -> "Desktop app"
#   4. Add this redirect URI:  https://127.0.0.1:5003/api/connectors/gmail/oauth2callback
#   5. Put the client id/secret into json_configuration/gmail_credentials.json:
#          {"client_id": "…", "client_secret": "…"}
#   6. GET /api/connectors/gmail/auth  (or use the Connect button) -> click Allow.
#
# Read-only scope by default; add "https://www.googleapis.com/auth/gmail.modify" and
# "https://www.googleapis.com/auth/gmail.send" to "scopes" if you want the agent to
# draft/send too.

import base64
import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CRED_PATH = os.path.join(REPO_ROOT, "json_configuration", "gmail_credentials.json")
_TOKEN_PATH = os.path.join(REPO_ROOT, "json_configuration", "gmail_token.json")

MANIFEST = {
    "name": "gmail",
    "title": "Gmail",
    "version": "1.0.0",
    "description": "Let the agent read (and, with the write scope, act on) your Gmail.",
}

# These become agent tools. The agent is handed these definitions, so "do my gmail"
# becomes a real function call instead of a chatbot that pretends it can't.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "gmail_list_inbox",
            "description": "List recent emails in the connected Gmail account. Use query terms like 'is:unread', 'from:someone', 'newer_than:2d'.",
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
            "description": "Read the body of one Gmail message by id.",
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


def _credentials_config():
    cfg = {}
    if os.path.isfile(_CRED_PATH):
        try:
            with open(_CRED_PATH, encoding="utf-8") as fh:
                cfg = json.load(fh) or {}
        except Exception:
            cfg = {}
    client_id = cfg.get("client_id") or os.environ.get("GMAIL_CLIENT_ID")
    client_secret = cfg.get("client_secret") or os.environ.get("GMAIL_CLIENT_SECRET")
    scopes = cfg.get("scopes") or ["https://www.googleapis.com/auth/gmail.readonly"]
    return client_id, client_secret, scopes


def _client_config():
    cid, secret, _ = _credentials_config()
    if not cid or not secret:
        return None
    return {
        "installed": {
            "client_id": cid, "client_secret": secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def _stored_credentials():
    if not os.path.isfile(_TOKEN_PATH):
        return None
    try:
        with open(_TOKEN_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        from google.oauth2.credentials import Credentials
        return Credentials.from_authorized_user_info(data)
    except Exception:
        return None


def _store_credentials(creds):
    os.makedirs(os.path.dirname(_TOKEN_PATH), exist_ok=True)
    with open(_TOKEN_PATH, "w", encoding="utf-8") as fh:
        json.dump(json.loads(creds.to_json()), fh, indent=2)


def _service():
    creds = _stored_credentials()
    if creds is None or not creds.valid:
        if creds is not None and creds.refresh_token:
            try:
                import google.auth.transport.requests
                creds.refresh(google.auth.transport.requests.Request())
                _store_credentials(creds)
            except Exception:
                return None, "refresh failed"
        else:
            return None, "not connected"
    try:
        from googleapiclient.discovery import build
        return build("gmail", "v1", credentials=creds, cache_discovery=False), None
    except Exception as exc:
        return None, str(exc)


def _header(headers, name):
    for h in headers or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _list_inbox(query, max_results):
    svc, err = _service()
    if svc is None:
        return {"error": "Gmail is not connected ({}).".format(err or "no token")}
    try:
        max_results = min(max(int(max_results or 10), 1), 50)
    except (TypeError, ValueError):
        max_results = 10
    try:
        ids = svc.users().messages().list(userId="me", q=query or None,
                                          maxResults=max_results).execute()
        msgs = ids.get("messages", [])
    except Exception as exc:
        return {"error": str(exc)}
    out = []
    for m in msgs:
        try:
            meta = svc.users().messages().get(
                userId="me", id=m["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"]).execute()
        except Exception:
            continue
        headers = meta.get("payload", {}).get("headers", [])
        out.append({
            "id": m["id"],
            "from": _header(headers, "From"),
            "subject": _header(headers, "Subject"),
            "date": _header(headers, "Date"),
            "snippet": meta.get("snippet", ""),
            "unread": "UNREAD" in meta.get("labelIds", []),
        })
    return {"messages": out, "count": len(out)}


def _read_message(mid):
    svc, err = _service()
    if svc is None:
        return {"error": "Gmail is not connected."}
    try:
        full = svc.users().messages().get(userId="me", id=mid, format="full").execute()
    except Exception as exc:
        return {"error": str(exc)}
    payload = full.get("payload", {})
    headers = payload.get("headers", [])
    body = ""
    if payload.get("mimeType") == "text/plain":
        data = (payload.get("body") or {}).get("data", "")
        if data:
            try:
                body = base64.urlsafe_b64decode(data + "===").decode("utf-8", "replace")
            except Exception:
                body = ""
    return {"id": mid, "from": _header(headers, "From"),
            "subject": _header(headers, "Subject"), "date": _header(headers, "Date"),
            "body": body[:8000]}


def _status():
    cid, _, _ = _credentials_config()
    if not cid:
        return {"connected": False, "configured": False,
                "hint": "Add client_id/client_secret to json_configuration/gmail_credentials.json"}
    creds = _stored_credentials()
    if creds is None:
        return {"connected": False, "configured": True}
    account = None
    svc, _ = _service()
    if svc is not None:
        try:
            account = svc.users().getProfile(userId="me").execute().get("emailAddress")
        except Exception:
            account = None
    return {"connected": True, "configured": True, "account": account}


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
    """The HTTP side: OAuth connect/disconnect and a raw status/inbox endpoint."""
    from flask import jsonify, request

    def _redirect_uri():
        return "{}://{}/api/connectors/gmail/oauth2callback".format(
            request.scheme or "https", request.host)

    @app.route("/api/connectors/gmail/status")
    def _status_route():
        return jsonify(_status())

    @app.route("/api/connectors/gmail/auth")
    def _auth_route():
        cc = _client_config()
        if cc is None:
            return jsonify({"error": "Gmail not configured."}), 400
        _, _, scopes = _credentials_config()
        try:
            from google_auth_oauthlib.flow import Flow
            flow = Flow.from_client_config(cc, scopes=scopes)
            flow.redirect_uri = _redirect_uri()
            auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
            return jsonify({"auth_url": auth_url})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/connectors/gmail/oauth2callback")
    def _callback_route():
        code = request.args.get("code")
        if not code:
            return "<h3>missing code</h3>", 400
        cc = _client_config()
        if cc is None:
            return "<h3>Gmail not configured.</h3>", 400
        _, _, scopes = _credentials_config()
        try:
            from google_auth_oauthlib.flow import Flow
            flow = Flow.from_client_config(cc, scopes=scopes)
            flow.redirect_uri = _redirect_uri()
            flow.fetch_token(code=code)
            _store_credentials(flow.credentials)
            return ("<html><body style='background:#0b0d12;color:#e6edf3;font-family:Segoe UI;"
                    "text-align:center;padding-top:15vh'><h2>Gmail connected</h2>"
                    "<p>Close this tab and ask TrioForge about your inbox.</p></body></html>")
        except Exception as exc:
            return "<html><body><h3>Authorization failed</h3><pre>{}</pre></body></html>".format(exc), 400

    @app.route("/api/connectors/gmail/inbox")
    def _inbox_route():
        q = request.args.get("query", "")
        return jsonify(_list_inbox(q, request.args.get("max")))

    @app.route("/api/connectors/gmail/disconnect", methods=["POST"])
    def _disconnect_route():
        try:
            if os.path.isfile(_TOKEN_PATH):
                os.remove(_TOKEN_PATH)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify({"ok": True})
