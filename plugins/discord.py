# discord.py — a TrioForge PLUGIN that puts Discord in the app: a Connectors card
# and two agent tools (discord_send, discord_status).
#
# ── Why this plugin does almost nothing itself ────────────────────────────────
# Plugins run INSIDE the app process with full permissions - the app's own docs say
# a crashing plugin "crashes the app". A chat bot is the one component you expect to
# break: a bad reply, a loop, a Discord rate limit, a token that expires. So the
# actual Discord work lives in a SEPARATE process (`bridge.py`) and this file is a
# thin shim around it:
#
#   * no network calls here at all - it only spawns a child and reads its output
#   * every call has a hard timeout, and the child is killed if it overruns
#   * a crash or a hang in the bridge comes back as an error string, nothing more
#
# The bot token is NEVER stored here and never read by this plugin. It lives in the
# bridge's own .env, so the app's configuration (and its backups) contain no Discord
# secret at all. The only thing saved in TrioForge is where the bridge lives.
#
# Setup lives in the 🔌 Connectors panel as a guide, and the long version is in
# D:\discord-bridge\README.md.

import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SETTINGS_PATH = os.path.join(REPO_ROOT, "json_configuration", "discord_bridge.json")

DEFAULT_BRIDGE_DIR = r"D:\discord-bridge"
CALL_TIMEOUT = 45          # seconds: the bridge itself answers in well under a second

MANIFEST = {
    "name": "discord",
    "title": "Discord",
    "version": "1.0.0",
    "description": "Post to Discord from the agent, via an isolated bridge process.",
    # A CONNECTOR: it shows up in the 🔌 panel with its own card and guide.
    "connector": True,
    # Only the location is asked for. The token stays in the bridge's .env, so a
    # secret never enters TrioForge's config (or its backups).
    "credentials": [
        {"key": "bridge_dir", "label": "Bridge folder (holds bridge.py and .env)",
         "type": "text", "placeholder": DEFAULT_BRIDGE_DIR},
    ],
    "guide": [
        "1. Open the Discord Developer Portal -> New Application -> Bot tab -> Reset "
        "Token, and copy the token.",
        "2. Leave 'Privileged Gateway Intents' OFF. The bridge uses plain REST polling, "
        "so it needs none - and that is also why it can never get your account banned.",
        "3. OAuth2 -> URL Generator -> scope 'bot', permission 'Send Messages' only -> "
        "open the URL and add the bot to ONE private server with only you. Give it no "
        "other permissions.",
        "4. In the bridge folder: copy .env.example to .env, paste the token as "
        "DISCORD_TOKEN.",
        "5. Ask the bot to work out your DM channel: python bridge.py dm-channel "
        "<your_user_id> (Developer Mode on, right-click your name, Copy User ID).",
        "6. Put that channel id in DISCORD_ALLOWED_CHANNEL_IDS in .env. The bot can "
        "post nowhere else.",
        "7. Test it: python bridge.py send \"hello from TrioForge\".",
        "Optional: set ALLOW_INBOUND=1 and DISCORD_ALLOWED_USER_IDS=<your id> to also "
        "let you message the bot from your phone. Only listed user ids are obeyed.",
    ],
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "discord_send",
            "description": "Send a Discord message through the user's own bot. The bot "
                           "can only post to the channels the user allow-listed, so a "
                           "channel is optional - leave it out to use the configured "
                           "one. Use this only when the user actually asks to send "
                           "something to Discord.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "the message to post"},
                    "channel_id": {"type": "string",
                                   "description": "optional target channel id; must "
                                                  "already be allow-listed"},
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
            "description": "Check whether the Discord bridge is set up and its token works.",
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


def bridge_dir():
    """Where bridge.py lives: the saved setting, then the environment, then the default."""
    saved = (_settings().get("bridge_dir") or "").strip()
    return saved or (os.environ.get("DISCORD_BRIDGE_DIR") or "").strip() or DEFAULT_BRIDGE_DIR


def _bridge_path():
    return os.path.join(bridge_dir(), "bridge.py")


# ── the isolated call ─────────────────────────────────────────────────────────
def _python():
    """The interpreter to run the bridge with. Stdlib only, so the app's own works."""
    return sys.executable or "python"


def _run_bridge(args, timeout=CALL_TIMEOUT):
    """Run the bridge in its OWN process and return (ok, text).

    Everything risky happens out there: no Discord library is imported here, no
    socket is opened here, and a hang is killed instead of blocking the app. This
    function is the entire coupling between TrioForge and the bot.
    """
    script = _bridge_path()
    if not os.path.isfile(script):
        return False, (f"bridge.py not found in {bridge_dir()!r} - put the bridge "
                       "folder path in this card's settings.")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    kwargs = {}
    try:                                   # never flash a console window on Windows
        from proc_util import hidden_flags
        kwargs["creationflags"] = hidden_flags()
    except Exception:
        pass
    kwargs["stdout"] = subprocess.PIPE
    kwargs["stderr"] = subprocess.STDOUT
    kwargs["cwd"] = bridge_dir()
    try:
        proc = subprocess.run([_python(), script] + list(args), env=env,
                              timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        return False, (f"the bridge did not answer within {timeout}s and was stopped - "
                       "TrioForge is unaffected; check the bridge window/log")
    except Exception as exc:               # noqa: BLE001 - never let a plugin raise
        return False, f"could not start the bridge: {exc}"
    out = (proc.stdout or b"").decode("utf-8", "replace").strip()
    return proc.returncode == 0, out


def _last_line(text):
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


# ── status ────────────────────────────────────────────────────────────────────
def _status():
    """What the Connectors card renders. Never raises.

    The generic card reads `connected` / `configured` / `account` / `error` /
    `needs_setup` and shows the declared fields only while it is not configured -
    so all of them are always present, not just when something is wrong.
    """
    folder = bridge_dir()
    st = {
        "connector": "discord",
        "bridge_dir": folder,
        "installed": os.path.isfile(_bridge_path()),
        "env_file": os.path.isfile(os.path.join(folder, ".env")),
        # Stated on the card itself: the risky half runs in another process.
        "isolated_from_app": True,
        "configured": False,
        "connected": False,
        "account": None,
        "needs_setup": False,
    }
    if not st["installed"]:
        st.update({"needs_setup": True,
                   "error": f"bridge.py is not in {folder}. Set the folder above, or "
                            "copy the bridge there."})
        return st
    ok, out = _run_bridge(["whoami"], timeout=30)
    st["detail"] = _last_line(out)
    st["connected"] = bool(ok and "token OK" in out)
    st["configured"] = bool(st["env_file"])
    if st["connected"]:
        # "token OK - bot is NAME#0 (id 123)" -> NAME#0
        name = ""
        if "bot is" in st["detail"]:
            name = st["detail"].split("bot is", 1)[1].split("(", 1)[0].strip()
        st["account"] = ("discord bot " + name) if name else "discord bot"
        st["summary"] = ("Discord bridge is ready - " + st["detail"] +
                         ". The bot can only post to allow-listed channels.")
    elif not st["env_file"]:
        st["error"] = ("No .env in the bridge folder yet: copy .env.example to .env "
                       "and paste DISCORD_TOKEN.")
    else:
        st["error"] = "Bridge found but the token check failed: " + st["detail"]
    return st


# ── tools ─────────────────────────────────────────────────────────────────────
def _send(text, channel_id=None):
    text = (text or "").strip()
    if not text:
        return {"error": "nothing to send"}
    if channel_id:
        # The bridge refuses anything not allow-listed; say so up front so the model
        # does not read a refusal as a temporary glitch and retry.
        args = ["send", text] if str(channel_id) in _allowed_channels() else None
        if args is None:
            return {"error": f"channel {channel_id} is not in the bridge's "
                             "DISCORD_ALLOWED_CHANNEL_IDS allow-list"}
    else:
        args = ["send", text]
    ok, out = _run_bridge(args)
    if ok:
        return {"ok": True, "summary": "sent to Discord: " + _last_line(out)}
    return {"error": _last_line(out) or "the bridge did not confirm the send"}


def _allowed_channels():
    """Read the allow-list out of the bridge's .env (ids only - never the token)."""
    ids = []
    path = os.path.join(bridge_dir(), ".env")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("DISCORD_ALLOWED_CHANNEL_IDS"):
                    ids = [p.strip() for p in line.partition("=")[2].split(",") if p.strip()]
    except Exception:                      # noqa: BLE001 - a missing .env is reported
        pass                               # by _status(), not here
    return ids


def dispatch(tool_name, args):
    args = args or {}
    if tool_name == "discord_send":
        return _send(args.get("text"), args.get("channel_id"))
    if tool_name == "discord_status":
        st = _status()
        if st.get("connected"):
            return {"connected": True, "summary": st["summary"]}
        # Hand back the REASON, not just "not ready": the model repeats this to the
        # user, and "the token is not set" is actionable where "not ready" is not.
        return {"connected": False,
                "summary": st.get("error") or st.get("summary")
                           or "the Discord bridge is not ready"}
    return {"error": "unknown tool " + str(tool_name)}


def connect_info():
    """The TUI's view of this connector (there is no browser sign-in to offer)."""
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
        """The card's Save button POSTs the declared fields here.

        Only the folder is stored. There is no Discord secret to save: the token
        stays in the bridge's own .env, so it never lands in TrioForge's config or
        in a backup of it.
        """
        data = request.get_json(silent=True) or {}
        folder = (data.get("bridge_dir") or "").strip()
        if not folder:
            return jsonify({"error": "a bridge folder is required"}), 400
        if not os.path.isdir(folder):
            return jsonify({"error": f"{folder} is not a folder"}), 400
        _save_settings({"bridge_dir": folder})
        return jsonify({"ok": True, "status": _status()})

    # Kept as an alias so either name works from a script or the terminal client.
    @app.route("/api/connectors/discord/settings", methods=["POST"])
    def _discord_settings_route():
        return _discord_credentials_route()

    @app.route("/api/connectors/discord/test", methods=["POST"])
    def _discord_test_route():
        """Run the token check on demand - the button in the card."""
        return jsonify(_status())

    @app.route("/api/connectors/discord/disconnect", methods=["POST"])
    def _discord_disconnect_route():
        """Forget the bridge folder.

        There is no token inside TrioForge to revoke, so this clears the setting and
        says so plainly rather than pretending something was signed out.
        """
        try:
            if os.path.isfile(_SETTINGS_PATH):
                os.remove(_SETTINGS_PATH)
        except OSError as exc:
            return jsonify({"error": "could not clear the setting: {}".format(exc)}), 500
        st = _status()
        st["note"] = ("TrioForge forgot the bridge folder. The bot token lives in the "
                      "bridge's own .env - revoke it in the Discord Developer Portal "
                      "if you want it dead.")
        return jsonify({"ok": True, "status": st})
