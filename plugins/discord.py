# discord.py — a TrioForge PLUGIN that posts to Discord from the agent.
#
# ── Why a webhook instead of a bot / bridge ───────────────────────────────────
# The old design needed a Discord Developer-Portal app, a bot token in a separate
# bridge.py's .env, OAuth2 scopes, and a DM-channel lookup — a seven-step chore for
# "post this to Discord". A channel WEBHOOK is one URL: the user creates it in
# Discord (Server Settings > Integrations > Webhooks > New Webhook), pastes it
# here ONCE, and TrioForge posts straight to that channel forever after.
#
#   * auto-detects whether Discord is installed / running (no config needed)
#   * the webhook URL is the ONLY secret, stored in the gitignored settings file
#   * a send is one bounded HTTPS POST; failures come back as an error string
#
# The URL grants exactly one capability — write to that one channel — and can be
# deleted in Discord at any time to revoke it.

import json
import os
import sys

try:
    import requests
except ImportError:  # pragma: no cover - requests is a TrioForge dependency
    requests = None

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SETTINGS_PATH = os.path.join(REPO_ROOT, "json_configuration", "discord_bridge.json")

MANIFEST = {
    "name": "discord",
    "title": "Discord",
    "version": "2.0.0",
    "description": "Post to a Discord channel from the agent, via a one-paste webhook.",
    "connector": True,
    # Only the webhook URL is asked for. That is the entire secret.
    "credentials": [
        {"key": "webhook_url", "label": "Webhook URL",
         "type": "text",
         "placeholder": "https://discord.com/api/webhooks/…"},
    ],
    "guide": [
        "1. In Discord: Server Settings -> Integrations -> Webhooks -> New Webhook, "
        "pick a channel, Copy Webhook URL.",
        "2. Paste that URL here and Save. Done — no bot, no token, no .env.",
        "3. The webhook can only post to the one channel you created it in; delete "
        "the webhook in Discord to revoke it at any time.",
    ],
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "discord_send",
            "description": "Post a message to the user's Discord channel (via their configured "
                           "webhook). Use only when the user actually asks to send something to "
                           "Discord.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "the message to post"},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "discord_status",
            "description": "Check whether Discord is installed/running on this machine and "
                           "whether a webhook is connected.",
            "parameters": {"type": "object", "properties": {},
                           "additionalProperties": False},
        },
    },
]


# ── settings ──────────────────────────────────────────────────────────────────
def _settings():
    try:
        with open(_SETTINGS_PATH, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def _save_settings(updates):
    data = _settings()
    data.update(updates)
    os.makedirs(os.path.dirname(_SETTINGS_PATH), exist_ok=True)
    with open(_SETTINGS_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def _webhook_url():
    return (_settings().get("webhook_url") or "").strip()


# ── detection ─────────────────────────────────────────────────────────────────
def _discord_running():
    """True when a Discord client process is running right now."""
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            name = (p.info.get("name") or "").lower()
            if "discord" in name:
                return True
    except Exception:  # noqa: BLE001 - detection is best-effort
        pass
    return False


def _discord_installed():
    """True when Discord appears to be installed in the user's profile."""
    local = os.environ.get("LOCALAPPDATA", "")
    if local and os.path.isdir(os.path.join(local, "Discord")):
        return True
    # Discord's stable build also leaves an Update.exe; check a couple of names.
    for sub in ("Discord", "discord"):
        if local and os.path.isdir(os.path.join(local, sub)):
            return True
    return False


# ── webhook ───────────────────────────────────────────────────────────────────
def _webhook_info():
    """GET the webhook's own metadata to validate the URL (no message is sent)."""
    if requests is None:
        return {"error": "requests is not installed"}
    url = _webhook_url()
    if not url:
        return {"error": "no webhook configured"}
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            d = r.json()
            return {"name": d.get("name"), "channel_id": d.get("channel_id"),
                    "guild_id": d.get("guild_id")}
        return {"error": "HTTP {}: {}".format(r.status_code, (r.text or "")[:160])}
    except Exception as exc:  # noqa: BLE001 - report anything as not-reachable
        return {"error": str(exc)}


# ── status ────────────────────────────────────────────────────────────────────
def _status():
    """What the Connectors card renders. Never raises."""
    url = _webhook_url()
    st = {
        "connector": "discord",
        "discord_installed": _discord_installed(),
        "discord_running": _discord_running(),
        "configured": bool(url),
        "connected": False,
        "account": None,
        "needs_setup": not bool(url),
    }
    if not url:
        st["error"] = ("No webhook yet. In Discord: Server Settings > Integrations > "
                       "Webhooks > New Webhook, then paste the URL here.")
        return st
    info = _webhook_info()
    if "channel_id" in info:
        st["connected"] = True
        name = (info.get("name") or "").strip()
        st["account"] = "Discord webhook" + (" — " + name if name else "")
        st["summary"] = ("Connected to Discord channel {}. Posts go to that one "
                         "channel only.".format(info.get("channel_id")))
    else:
        st["error"] = "Webhook failed: " + (info.get("error") or "unknown error")
    return st


# ── tools ─────────────────────────────────────────────────────────────────────
def _send(text, channel_id=None):
    text = (text or "").strip()
    if not text:
        return {"error": "nothing to send"}
    url = _webhook_url()
    if not url:
        return {"error": "No Discord webhook is configured — set one in the "
                         "Connectors panel first."}
    if requests is None:
        return {"error": "requests is not installed"}
    if len(text) > 2000:
        text = text[:1997] + "…"     # Discord's message cap
    try:
        r = requests.post(url, json={"content": text}, timeout=15)
    except Exception as exc:  # noqa: BLE001
        return {"error": "Could not reach Discord: {}".format(exc)}
    if r.status_code in (200, 204):
        return {"ok": True, "summary": "posted to Discord"}
    return {"error": "Discord rejected the post (HTTP {}): {}".format(
        r.status_code, (r.text or "")[:200])}


def dispatch(tool_name, args):
    args = args or {}
    if tool_name == "discord_send":
        return _send(args.get("text"), args.get("channel_id"))
    if tool_name == "discord_status":
        st = _status()
        if st.get("connected"):
            return {"connected": True, "summary": st["summary"]}
        return {"connected": False,
                "summary": st.get("error") or st.get("summary")
                           or "Discord is not connected"}
    return {"error": "unknown tool " + str(tool_name)}


def connect_info():
    """The TUI's view of this connector (no browser sign-in to offer)."""
    st = _status()
    if st.get("connected"):
        return {"connected": True, "account": st.get("account")}
    return {"error": st.get("error") or st.get("summary") or "not ready",
            "configured": bool(st.get("configured"))}


# ── HTTP ──────────────────────────────────────────────────────────────────────
def register(app):
    from flask import jsonify, request

    @app.route("/api/connectors/discord/status")
    def _discord_status_route():
        return jsonify(_status())

    @app.route("/api/connectors/discord/credentials", methods=["POST"])
    def _discord_credentials_route():
        """The card's Save button POSTs the webhook URL here."""
        data = request.get_json(silent=True) or {}
        url = (data.get("webhook_url") or "").strip()
        if not url:
            return jsonify({"error": "a webhook URL is required"}), 400
        if not (url.startswith("https://discord.com/api/webhooks/")
                or url.startswith("https://discordapp.com/api/webhooks/")):
            return jsonify({"error": "that does not look like a Discord webhook URL"}), 400
        _save_settings({"webhook_url": url})
        return jsonify({"ok": True, "status": _status()})

    # Kept as an alias so either name works from a script or the terminal client.
    @app.route("/api/connectors/discord/settings", methods=["POST"])
    def _discord_settings_route():
        return _discord_credentials_route()

    @app.route("/api/connectors/discord/test", methods=["POST"])
    def _discord_test_route():
        """Validate the webhook on demand - the button in the card."""
        return jsonify(_status())

    @app.route("/api/connectors/discord/disconnect", methods=["POST"])
    def _discord_disconnect_route():
        """Forget the webhook URL. Delete the webhook in Discord to truly revoke it."""
        try:
            if os.path.isfile(_SETTINGS_PATH):
                os.remove(_SETTINGS_PATH)
        except OSError as exc:
            return jsonify({"error": "could not clear the setting: {}".format(exc)}), 500
        st = _status()
        st["note"] = ("TrioForge forgot the webhook URL. Delete the webhook in Discord "
                      "if you want it fully revoked.")
        return jsonify({"ok": True, "status": st})
