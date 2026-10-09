# calendar.py — a TrioForge PLUGIN that gives the agent a Google Calendar connector.
#
# Same OAuth pattern as gmail.py: click "Sign in with Google", pick an account, Allow.
# Gmail and Calendar are different Google services, so this connector is kept fully
# SEPARATE from Gmail — its own Google app (Client ID + Secret) and its own token.
#
# One-time app setup (~5 minutes, once):
#   1. console.cloud.google.com -> new project (or reuse your Gmail project)
#   2. APIs & Services -> Library -> enable "Google Calendar API"
#   3. OAuth consent screen -> add yourself as a Test user
#   4. Credentials -> Create credentials -> OAuth client ID -> "Web application"
#   5. Authorized redirect URIs -> add exactly:
#          https://127.0.0.1:5003/api/connectors/calendar/oauth2callback
#   6. Paste the Client ID + Secret into 🔌 Connectors -> Google Calendar -> Save app
#   7. Click "Sign in with Google". Done.

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CRED_PATH = os.path.join(REPO_ROOT, "json_configuration", "calendar_credentials.json")

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
CAL_API = "https://www.googleapis.com/calendar/v3/"
SCOPES = "https://www.googleapis.com/auth/calendar.readonly"

# ── Bake the app in here to make the fields disappear for ever ────────────────
DEFAULT_CLIENT_ID = ""
DEFAULT_CLIENT_SECRET = ""

MANIFEST = {
    "name": "calendar",
    "title": "Google Calendar",
    "version": "1.0.0",
    "description": "Sign in with Google so the agent can read your calendar.",
    "connector": True,
    "credentials": [
        {"key": "oauth_client_id", "label": "Google Client ID (one-time app setup)",
         "type": "text", "placeholder": "….apps.googleusercontent.com"},
        {"key": "oauth_client_secret", "label": "Google Client Secret", "type": "password",
         "placeholder": "GOCSPX-…"},
    ],
    "guide": [
        "1. console.cloud.google.com -> create a project (or reuse your Gmail project), "
        "and stay in THAT project for every step below.",
        "2. APIs & Services -> Library -> search 'Google Calendar API' -> ENABLE.",
        "3. Google Auth Platform -> Audience -> Test users -> add YOUR OWN Gmail address "
        "-> Save. (Skip this and Google answers with 'access_denied'.)",
        "4. Credentials -> + Create credentials -> OAuth client ID -> type: Web application.",
        "5. Authorized redirect URIs -> + ADD URI -> paste exactly "
        "https://127.0.0.1:5003/api/connectors/calendar/oauth2callback -> then click SAVE.",
        "6. Copy the Client ID + Client secret, paste them here -> Save app.",
        "7. Click 'Sign in with Google' -> pick your account -> Continue. The tab then "
        "says 'Calendar connected'.",
    ],
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "calendar_list_events",
            "description": "List the user's upcoming Google Calendar events. Leave query empty for everything in the next few days; use a word to search event titles/locations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "max": {"type": "integer", "description": "max events to return (default 10)"},
                    "days": {"type": "integer", "description": "how many days ahead to look (default 7)"},
                    "query": {"type": "string", "description": "optional search text (matches summary/location)"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_status",
            "description": "Check whether Google Calendar is connected and which account.",
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
    """Calendar's OWN Google app credentials.

    Kept deliberately separate from Gmail's: Gmail and Calendar are different
    services with their own consent and scope, so each connector carries its own
    Client ID + Secret and its own token.
    """
    cfg = _cfg()
    cid = ((cfg.get("oauth_client_id") or "").strip()
           or os.environ.get("CALENDAR_CLIENT_ID", "").strip()
           or DEFAULT_CLIENT_ID.strip())
    secret = ((cfg.get("oauth_client_secret") or "").strip()
              or os.environ.get("CALENDAR_CLIENT_SECRET", "").strip()
              or DEFAULT_CLIENT_SECRET.strip())
    return cid, secret


# ── OAuth ─────────────────────────────────────────────────────────────────────
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
    r = requests.get(CAL_API + path, headers={"Authorization": "Bearer " + tok},
                     params=params or {}, timeout=30)
    if r.status_code != 200:
        return None, "Calendar API {}: {}".format(r.status_code, r.text[:200])
    return r.json(), None


# ── tools ─────────────────────────────────────────────────────────────────────
def _rfc3339(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _event_dict(e):
    """One event -> a compact dict the model can read."""
    start = e.get("start") or {}
    end = e.get("end") or {}
    return {
        "summary": e.get("summary") or "(no title)",
        "start": start.get("dateTime") or start.get("date") or "",
        "end": end.get("dateTime") or end.get("date") or "",
        "location": e.get("location") or "",
        "htmlLink": e.get("htmlLink") or "",
    }


def _list_events(query, max_results, days):
    try:
        max_results = min(max(int(max_results or 10), 1), 50)
    except (TypeError, ValueError):
        max_results = 10
    try:
        days = min(max(int(days or 7), 1), 90)
    except (TypeError, ValueError):
        days = 7
    params = {
        "timeMin": _rfc3339(datetime.now(timezone.utc)),
        "timeMax": _rfc3339(datetime.now(timezone.utc) + timedelta(days=days)),
        "maxResults": max_results,
        "singleEvents": "true",
        "orderBy": "startTime",
    }
    if query:
        params["q"] = query
    data, err = _api("calendars/primary/events", params)
    if err:
        return {"error": err}
    return {"events": [_event_dict(e) for e in data.get("items", [])],
            "count": len(data.get("items", []))}


def _status():
    """Status plus the two flags the connector card renders from.

    Mirrors gmail.py: ``can_sign_in`` shows the Sign-in button and
    ``sign_in_label`` is its text — without them the card says "Not connected"
    but offers no way to actually connect.
    """
    cfg = _cfg()
    cid, secret = _oauth_client()
    st = {"connected": False, "configured": bool(cid and secret)}
    if cfg.get("refresh_token") and cid:
        data, err = _api("calendars/primary")
        if err:
            st["configured"] = True
            st["error"] = err
        else:
            st["connected"] = True
            st["method"] = "oauth"
            st["account"] = data.get("id")
    st["can_sign_in"] = bool(st.get("configured")) and not bool(st.get("connected"))
    st["sign_in_label"] = "🔗 Sign in with Google"
    return st


def connect_info():
    """Terminal sign-in for the TUI (same shape as the Gmail connector)."""
    st = _status()
    if st.get("connected"):
        return {"connected": True, "account": st.get("account"), "method": st.get("method")}
    if not st.get("configured"):
        return {"error": "the Google app is not set up yet (Client ID + Secret). "
                         "It is shared with Gmail, so if Gmail is set up this should "
                         "not happen."}
    url, err = auth_url("https://127.0.0.1:5003/api/connectors/calendar/oauth2callback")
    if url is None:
        return {"error": err or "could not build the sign-in URL"}
    return {"url": url,
            "note": "sign in, and TrioForge's web server (https://127.0.0.1:5003) "
                    "must be running to catch the redirect"}


def dispatch(tool_name, args):
    args = args or {}
    if tool_name == "calendar_list_events":
        return _list_events(args.get("query"), args.get("max"), args.get("days"))
    if tool_name == "calendar_status":
        st = _status()
        if st.get("connected"):
            return {"connected": True, "account": st.get("account"),
                    "summary": "Google Calendar IS signed in as {}. You can list events "
                               "right now — call calendar_list_events.".format(st.get("account"))}
        return {"connected": False,
                "summary": "Google Calendar is not signed in yet. Ask the user to connect "
                           "it in the Connectors panel."}
    return {"error": "unknown tool " + str(tool_name)}


# ── HTTP ──────────────────────────────────────────────────────────────────────
def register(app):
    from flask import jsonify, request

    def _redirect_uri():
        return "{}://{}/api/connectors/calendar/oauth2callback".format(
            request.scheme or "https", request.host)

    @app.route("/api/connectors/calendar/status")
    def _status_route():
        return jsonify(_status())

    @app.route("/api/connectors/calendar/credentials", methods=["POST"])
    def _credentials_route():
        data = request.get_json(silent=True) or {}
        cid = (data.get("oauth_client_id") or "").strip()
        secret = (data.get("oauth_client_secret") or "").strip()
        if not cid or not secret:
            return jsonify({"error": "Client ID and Secret are both required."}), 400
        _save_cfg({"oauth_client_id": cid, "oauth_client_secret": secret})
        return jsonify({"ok": True})

    @app.route("/api/connectors/calendar/auth")
    def _auth_route():
        url, err = auth_url(_redirect_uri())
        if url is None:
            return jsonify({"error": "Set the Google app first (Client ID + Secret).",
                            "needs_client": True}), 400
        return jsonify({"auth_url": url})

    @app.route("/api/connectors/calendar/oauth2callback")
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
            return "<h3>missing code</h3><p>Start from /api/connectors/calendar/auth.</p>", 400
        data, err = exchange_code(code, _redirect_uri())
        if err:
            st = _status()
            if st.get("connected"):
                return ("<html><body style='background:#0b0d12;color:#e6edf3;"
                        "font-family:Segoe UI;text-align:center;padding-top:15vh'>"
                        "<h2>Calendar connected</h2><p>{}</p>"
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
                "text-align:center;padding-top:15vh'><h2>Calendar connected</h2>"
                "<p>{}</p><p style='color:#8b949e'>You can close this tab.</p></body></html>"
                ).format(st.get("account") or "")

    @app.route("/api/connectors/calendar/events")
    def _events_route():
        return jsonify(_list_events(request.args.get("query", ""),
                                    request.args.get("max"),
                                    request.args.get("days")))

    @app.route("/api/connectors/calendar/disconnect", methods=["POST"])
    def _disconnect_route():
        try:
            cfg = _cfg()
            for key in ("refresh_token", "access_token", "token_expires"):
                cfg.pop(key, None)
            os.makedirs(os.path.dirname(_CRED_PATH), exist_ok=True)
            with open(_CRED_PATH, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2)
            _token_cache["access"] = None
            _token_cache["expires"] = 0.0
            return jsonify({"ok": True})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
