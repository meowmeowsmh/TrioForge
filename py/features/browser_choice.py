# browser_choice.py – let the user choose which browser TrioForge opens in.
#
# The app used to call webbrowser.open(), which always used the system default.
# This adds a chooser (Chrome / Firefox / Safari / Edge / Brave / ...) plus an
# "open now" action, and the launcher honours the saved choice on startup.
#
#   GET  /api/browser/list    – detected browsers + which is selected
#   POST /api/browser/select  – save the choice  {"browser": "firefox"}
#   POST /api/browser/open    – open the app in the chosen (or given) browser now

import os
import json
import shutil
import logging
import subprocess
import sys

from flask import Blueprint, request, jsonify

from paths import root_path

logger = logging.getLogger(__name__)

browser_bp = Blueprint('browser_choice', __name__, url_prefix='/api/browser')

# id, display name, candidate executables. Safari is macOS-only (detected by path).
KNOWN = (
    ("chrome",   "Google Chrome",  ("google-chrome", "google-chrome-stable")),
    ("chromium", "Chromium",       ("chromium", "chromium-browser")),
    ("firefox", "Mozilla Firefox", ("firefox",)),
    ("edge",    "Microsoft Edge",  ("microsoft-edge", "microsoft-edge-stable")),
    ("brave",   "Brave",           ("brave-browser",)),
    ("vivaldi", "Vivaldi",         ("vivaldi",)),
    ("opera",   "Opera",           ("opera",)),
    ("safari",  "Safari",          ()),          # macOS only
)

DEFAULT_ID = "default"


def _pref_file():
    return root_path("json_configuration", "browser.json")


def read_preference():
    """The saved browser id, or 'default'."""
    try:
        with open(_pref_file(), encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("browser") or DEFAULT_ID
    except Exception:
        return DEFAULT_ID


def write_preference(browser_id):
    path = _pref_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"browser": browser_id or DEFAULT_ID}, fh, indent=2)


def _safari_path():
    if sys.platform != "darwin":
        return None
    app = "/Applications/Safari.app"
    return app if os.path.isdir(app) else None


def detect():
    """Every known browser with whether it exists on this machine."""
    found = []
    for bid, name, commands in KNOWN:
        path = ""
        for cmd in commands:
            located = shutil.which(cmd)
            if located:
                path = located
                break
        if not path and bid == "safari":
            path = _safari_path() or ""
        found.append({"id": bid, "name": name, "path": path,
                      "available": bool(path) or bid == DEFAULT_ID})
    return found


def open_url(url, browser=None):
    """Open `url` in the chosen browser. Returns the id actually used."""
    wanted = browser or read_preference()
    if wanted and wanted != DEFAULT_ID:
        for entry in detect():
            if entry["id"] != wanted or not entry["available"]:
                continue
            try:
                if wanted == "safari":
                    subprocess.Popen(["open", "-a", "Safari", url])
                else:
                    subprocess.Popen([entry["path"], url],
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
                return wanted
            except Exception as exc:
                logger.warning("could not launch %s: %s", wanted, exc)
    try:
        import webbrowser
        webbrowser.open(url)
        return DEFAULT_ID
    except Exception as exc:
        logger.warning("could not open the default browser: %s", exc)
        return None


@browser_bp.route('/list', methods=['GET'])
def list_browsers():
    return jsonify({"browsers": detect(), "selected": read_preference()})


@browser_bp.route('/select', methods=['POST'])
def select():
    data = request.get_json(silent=True) or {}
    wanted = (data.get('browser') or '').strip()
    valid = {b["id"] for b in detect()} | {DEFAULT_ID}
    if wanted not in valid:
        return jsonify({"error": "unknown browser: %s" % wanted}), 400
    write_preference(wanted)
    return jsonify({"ok": True, "selected": wanted})


@browser_bp.route('/open', methods=['POST'])
def open_now():
    """Open the app in the chosen browser (used by the 'Open now' button)."""
    data = request.get_json(silent=True) or {}
    host = request.host or "127.0.0.1:5003"
    scheme = request.scheme or "http"
    url = "%s://%s/" % (scheme, host)
    used = open_url(url, data.get('browser'))
    if not used:
        return jsonify({"error": "could not open any browser"}), 500
    return jsonify({"ok": True, "opened_in": used, "url": url})
