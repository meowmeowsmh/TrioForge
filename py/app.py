# app.py – chat + notes + cork board + integrated weather toast (performance-optimized)
from flask import Flask, request, jsonify, Response, redirect, session
import secrets
from flask_compress import Compress
import requests
import hashlib
import base64
import os
import json as std_json
import sys
import shutil
from datetime import datetime
import uuid
import psutil
import subprocess
import re
import urllib.request
import platform
import time
from functools import lru_cache, wraps
from concurrent.futures import ThreadPoolExecutor
import threading
import sqlite3
import csv
import logging
from logging.handlers import RotatingFileHandler
from io import StringIO
from typing import Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Try orjson ──
try:
    import orjson
    def json_dumps(obj):
        return orjson.dumps(obj).decode('utf-8')
    def json_dumps_pretty(obj):
        return orjson.dumps(obj, option=orjson.OPT_INDENT_2).decode('utf-8')
    def json_loads(s):
        return orjson.loads(s)
    logger.info("Using orjson for faster JSON")
except ImportError:
    json_dumps = std_json.dumps
    def json_dumps_pretty(obj):
        return std_json.dumps(obj, ensure_ascii=False, indent=2)
    json_loads = std_json.loads
    logger.info("Using standard json (install orjson for better performance)")

from paths import root_path
import backup_store
import llamacpp_service
import voice_service

# ── Server log file (tailable from the in-app Logs viewer) ──
# Written in addition to stderr so the UI can show the server log live,
# without needing a terminal / VS Code open.
_SERVER_LOG_PATH = root_path("logs", "server.log")
os.makedirs(os.path.dirname(_SERVER_LOG_PATH), exist_ok=True)


class _RedactSecretsFilter(logging.Filter):
    """Mask API keys before anything is written to a log.

    The UI sends some keys as query parameters (e.g. `/deepseek/status?api_key=sk-…`),
    and werkzeug logs the full request line — so keys used to land in
    `logs/server.log` in plaintext. Logs get shared for debugging, so redact them.
    """

    _PAT = re.compile(
        r'((?:api[_-]?key|apikey|token|access[_-]?token|auth|password|secret)["\']?\s*[=:]\s*["\']?)'
        r'([A-Za-z0-9_\-\.]{8,})',
        re.IGNORECASE,
    )
    _BEARER = re.compile(r'(Bearer\s+)([A-Za-z0-9_\-\.]{8,})', re.IGNORECASE)

    @classmethod
    def _clean(cls, text):
        text = cls._PAT.sub(r'\1***REDACTED***', text)
        return cls._BEARER.sub(r'\1***REDACTED***', text)

    def filter(self, record):
        try:
            if isinstance(record.msg, str) and ('=' in record.msg or ':' in record.msg):
                record.msg = self._clean(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: (self._clean(v) if isinstance(v, str) else v)
                                   for k, v in record.args.items()}
                elif isinstance(record.args, tuple):
                    record.args = tuple(
                        self._clean(a) if isinstance(a, str) else a for a in record.args
                    )
        except Exception:
            pass  # never let redaction break logging
        return True


_redactor = _RedactSecretsFilter()
_server_log_handler = RotatingFileHandler(
    _SERVER_LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8"
)
_server_log_handler.addFilter(_redactor)
_server_log_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
))
logging.getLogger().addHandler(_server_log_handler)
# Also redact what werkzeug prints straight to stderr (the terminal), and any
# message logged through the werkzeug logger.
logging.getLogger("werkzeug").addFilter(_redactor)
logging.getLogger().addFilter(_redactor)

# ── Imports ──
from providers.llm_providers import (
    OllamaProvider,
    LlamaCppProvider,
    HuggingFaceProvider,
    GroqProvider,
    DeepSeekProvider,
    ClaudeProvider,
    OpenRouterProvider,
    GeminiProvider,
    model_supports_vision,
    VISION_MODELS,
    describe_or_extract_file,
    sanitize_api_key,
    ProviderError,
)
from features.notes import notes_bp, upsert_note
from features.cork_board import corkboard_bp, upsert_pin, add_link
from features.viewer import setup_viewer
import personas
import comfyui_service
import rag
import plugin_loader
import setup_check
import edits_store
import video_to_text

try:
    import pynvml
    pynvml.nvmlInit()
    NVML_AVAILABLE = True
except Exception:
    NVML_AVAILABLE = False

app = Flask(__name__, static_folder=root_path("static"))
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024  # 25 MB request body cap (uploads + chat JSON)


@app.before_request
def _allow_streamed_attachment_upload():
    """Lift the 25 MB body cap for the streaming upload route only.

    That route writes the body straight to disk in 1 MB chunks, so it needs no cap;
    everything else keeps it, because a JSON body is held in memory.
    """
    if request.path == '/api/attach/upload':
        try:
            # NOTE: setting this to None does NOT remove the limit - Flask then falls
            # back to the MAX_CONTENT_LENGTH config, i.e. the 25 MB cap being lifted.
            # A real ceiling has to be set instead.
            request.max_content_length = 16 * 1024 * 1024 * 1024
        except Exception:
            pass

Compress(app)
app.register_blueprint(notes_bp)
app.register_blueprint(corkboard_bp)

# ── Plugins (loaded best-effort at startup) ──
try:
    plugin_loader.load_all(app)
except Exception as _plugin_exc:
    logger.warning("Plugin loading failed: %s", _plugin_exc)

# ── Live-coding edits: load persisted edits so the panel survives restarts ──
try:
    edits_store.init()
except Exception as _edits_exc:
    logger.warning("edits_store init failed: %s", _edits_exc)

# ── Lightweight per-IP rate limiting ──
_rate_limit_lock = threading.Lock()
_rate_limit_buckets = {}

def rate_limited(max_per_minute=20):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            ip = request.headers.get('X-Forwarded-For', request.remote_addr) or 'unknown'
            now = time.time()
            with _rate_limit_lock:
                bucket = _rate_limit_buckets.setdefault(ip, [])
                bucket[:] = [t for t in bucket if now - t < 60]
                if len(bucket) >= max_per_minute:
                    return jsonify({
                        "error": f"Rate limit exceeded ({max_per_minute}/min). Please slow down."
                    }), 429
                bucket.append(now)
                # Bound memory: drop IPs whose 60s window is now empty.
                if len(_rate_limit_buckets) > 10_000:
                    for stale_ip in [k for k, v in _rate_limit_buckets.items() if not v]:
                        del _rate_limit_buckets[stale_ip]
            return fn(*args, **kwargs)
        return wrapped
    return decorator

@app.after_request
def _add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    # SAMEORIGIN, not DENY: the chat shell embeds /notes and /corkboard in a frame
    # so that switching tabs does not reload the document (which is what used to
    # drop fullscreen). Other sites still cannot frame the app, so the
    # clickjacking protection is intact.
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


# ── Host mode: a password gate for instances other people can reach ──────────
# TrioForge has no accounts by design, which is fine while it is only listening on
# your own machine. The moment it is on the LAN, behind a tunnel, or in a
# container that others can open, it needs a door. Set TRIOFORGE_PASSWORD (or run
# the launcher with --host, which generates one for you and prints the link) and
# every page and API call asks for it first.
HOST_PASSWORD = os.environ.get("TRIOFORGE_PASSWORD", "").strip()
HOST_SESSION_KEY = "trio_host_ok"

# Reachable without the password: the gate itself, the launcher's health check,
# the PWA plumbing, and static assets (needed to draw the login page).
_HOST_OPEN_PATHS = ("/login", "/logout", "/api/ping", "/manifest.webmanifest",
                    "/sw.js", "/favicon.ico")

# Routes that return data rather than a page: answer them with 401 + JSON instead
# of handing back the login HTML, so a client never tries to parse it as data.
_HOST_JSON_PREFIXES = ("/api/", "/conversations", "/messages", "/providers",
                       "/resources", "/deepseek", "/check_vision")


def _host_gate_on() -> bool:
    return bool(HOST_PASSWORD)


def _host_secret() -> str:
    """A stable signing key, kept beside the other local data."""
    path = root_path("json_configuration", ".host_secret")
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                value = fh.read().strip()
            if len(value) >= 32:
                return value
        value = secrets.token_hex(32)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(value)
        return value
    except Exception:
        return secrets.token_hex(32)


app.secret_key = _host_secret()


def _host_login_page(error: str = "", next_url: str = "/") -> str:
    """On-brand, dependency-free login page."""
    err = ('<p style="color:#f85149;margin:0 0 14px">{}</p>'.format(error)) if error else ""
    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TrioForge — sign in</title>
<link rel="icon" href="/static/logo/favicon.png">
<style>
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:#0b0d12;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
  .card{background:#12151d;border:1px solid rgba(255,255,255,.09);border-radius:14px;
        padding:30px 32px;width:min(380px,92vw)}
  img{height:30px;display:block;margin:0 auto 18px}
  h1{font-size:16px;font-weight:600;margin:0 0 6px;text-align:center}
  p.sub{color:#9aa4b2;font-size:13.5px;margin:0 0 18px;text-align:center}
  input{width:100%%;box-sizing:border-box;padding:12px 14px;border-radius:9px;border:1px solid #2b3240;
        background:#0a0c11;color:#e6edf3;font-size:15px;margin-bottom:12px}
  button{width:100%%;padding:12px;border:0;border-radius:9px;font-weight:650;font-size:15px;cursor:pointer;
         background:linear-gradient(135deg,#ff7a2f,#ff9a4d);color:#1a0e05}
  .hint{color:#6e7784;font-size:12px;margin-top:14px;text-align:center}
</style></head>
<body>
  <form class="card" method="post" action="/login">
    <img src="/static/logo/wordmark.png" alt="TrioForge">
    <h1>This TrioForge is hosted</h1>
    <p class="sub">Enter the password the host gave you.</p>
    %s
    <input type="password" name="password" placeholder="Password" autofocus autocomplete="current-password">
    <input type="hidden" name="next" value="%s">
    <button type="submit">Sign in</button>
    <p class="hint">TrioForge stays on this machine — signing in only unlocks the workspace.</p>
  </form>
</body></html>""" % (err, next_url)


@app.before_request
def _host_password_gate():
    """Ask for the password on every page and API call while host mode is on."""
    if not _host_gate_on():
        return None
    path = request.path or "/"
    if path.startswith("/static/") or path.startswith("/viewer/static/"):
        return None
    if path in _HOST_OPEN_PATHS:
        return None
    if session.get(HOST_SESSION_KEY) is True:
        return None
    if request.path.startswith("/api/") or request.method != "GET":
        return jsonify({"error": "password required", "login": "/login"}), 401
    if path.startswith(_HOST_JSON_PREFIXES):
        return jsonify({"error": "password required", "login": "/login"}), 401
    return Response(_host_login_page(next_url=path), mimetype="text/html"), 200


@app.route('/login', methods=['GET', 'POST'])
def host_login():
    """Password gate for hosted instances (only active with TRIOFORGE_PASSWORD)."""
    if not _host_gate_on():
        return redirect("/")
    nxt = (request.form.get("next") or request.args.get("next") or "/").strip()
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    if request.method == "POST":
        supplied = (request.form.get("password") or "").strip()
        if supplied and secrets.compare_digest(supplied, HOST_PASSWORD):
            session[HOST_SESSION_KEY] = True
            session.permanent = True
            return redirect(nxt)
        return Response(_host_login_page("Wrong password.", nxt), mimetype="text/html"), 401
    return Response(_host_login_page(next_url=nxt), mimetype="text/html")


@app.route('/logout')
def host_logout():
    session.pop(HOST_SESSION_KEY, None)
    return redirect("/login")


DEFAULT_MODEL = "vaultbox/qwen3.5-uncensored:9b"
OLLAMA_BASE_URL = os.environ.get('OLLAMA_BASE_URL', 'http://127.0.0.1:11434')
CONVERSATIONS_FILE = root_path("json_configuration", "conversations.json")
MODEL_CONFIG_FILE = root_path("json_configuration", "model_config.json")
ATTACHMENTS_DIR = root_path("json_configuration", "attachments")
SQLITE_DIR = root_path("sqlite_data")
SQLITE_DB_PATH = os.path.join(SQLITE_DIR, "conversations.db")
WORKSPACES_DIR = root_path("json_configuration", "workspaces")
CURRENT_WORKSPACE_FILE = os.path.join(WORKSPACES_DIR, "current.txt")
if not os.path.exists(WORKSPACES_DIR):
    os.makedirs(WORKSPACES_DIR, exist_ok=True)

try:
    from duckduckgo_search import DDGS
    SEARCH_AVAILABLE = True
except ImportError:
    SEARCH_AVAILABLE = False

os.makedirs(os.path.dirname(CONVERSATIONS_FILE), exist_ok=True)
os.makedirs(os.path.dirname(MODEL_CONFIG_FILE), exist_ok=True)
os.makedirs(ATTACHMENTS_DIR, exist_ok=True)
os.makedirs(SQLITE_DIR, exist_ok=True)

# ── Model folders ──
# Always exist so a fresh `git clone` + run gives users a place to drop (or the
# ⬇ button to download) GGUFs — no manual mkdir.
for _capability_dir in ("models", "video_model", "universal_models_to_text"):
    os.makedirs(root_path(_capability_dir), exist_ok=True)

# ── SQLite ──
_sqlite_conn = sqlite3.connect(SQLITE_DB_PATH, check_same_thread=False)
_sqlite_lock = threading.Lock()
# Set True when the FTS5 full-text index is available (falls back to LIKE otherwise).
_FTS_OK = False

def _init_sqlite():
    global _FTS_OK
    with _sqlite_lock:
        # WAL / synchronous are unreliable on WSL /mnt/<drive> mounts (they throw
        # "disk I/O error"); apply each defensively and fall back to defaults.
        try:
            _sqlite_conn.execute("PRAGMA journal_mode=WAL;")
        except sqlite3.OperationalError:
            logger.warning("WAL journal mode unavailable; using default journal mode.")
        try:
            _sqlite_conn.execute("PRAGMA synchronous=NORMAL;")
        except sqlite3.OperationalError:
            logger.warning("synchronous=NORMAL unavailable; using default.")
        _sqlite_conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role            TEXT NOT NULL,
                content         TEXT,
                attachments     TEXT,          -- JSON array: {"images":[...], "files":[...]}
                created_at      TEXT NOT NULL
            );
        """)
        _sqlite_conn.execute("""
            CREATE TABLE IF NOT EXISTS usage_stats (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                provider           TEXT NOT NULL,
                model              TEXT,
                session_id         TEXT,
                prompt_tokens      INTEGER DEFAULT 0,
                completion_tokens  INTEGER DEFAULT 0,
                cost               REAL DEFAULT 0,
                created_at         TEXT NOT NULL
            );
        """)
        cur = _sqlite_conn.cursor()
        cur.execute("PRAGMA table_info(messages)")
        columns = [col[1] for col in cur.fetchall()]
        if 'attachments' not in columns:
            _sqlite_conn.execute("ALTER TABLE messages ADD COLUMN attachments TEXT")
        _sqlite_conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);")
        _sqlite_conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);")

        # ── Full-text search (FTS5) over message content ──
        # External-content FTS table: space-efficient, and kept in sync by the
        # triggers below. If this SQLite build lacks FTS5 we degrade to LIKE.
        try:
            _sqlite_conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5("
                "content, conversation_id UNINDEXED, role UNINDEXED, "
                "content='messages', content_rowid='id');"
            )
            _sqlite_conn.execute(
                "CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN "
                "INSERT INTO messages_fts(rowid, content, conversation_id, role) "
                "VALUES (new.id, new.content, new.conversation_id, new.role); END;"
            )
            _sqlite_conn.execute(
                "CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN "
                "INSERT INTO messages_fts(messages_fts, rowid, content, conversation_id, role) "
                "VALUES ('delete', old.id, old.content, old.conversation_id, old.role); END;"
            )
            _sqlite_conn.execute(
                "CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE ON messages BEGIN "
                "INSERT INTO messages_fts(messages_fts, rowid, content, conversation_id, role) "
                "VALUES ('delete', old.id, old.content, old.conversation_id, old.role); "
                "INSERT INTO messages_fts(rowid, content, conversation_id, role) "
                "VALUES (new.id, new.content, new.conversation_id, new.role); END;"
            )
            # Is the INDEX actually populated? For an external-content FTS table
            # `SELECT count(*) FROM messages_fts` reads through to `messages`, so
            # it can't tell us. The shadow table `messages_fts_docsize` holds one
            # row per INDEXED document, so it's the honest signal. If the index is
            # empty while messages exist (fresh install, or an interrupted
            # rebuild), backfill it once.
            try:
                cur.execute("SELECT count(*) FROM messages_fts_docsize")
                indexed = cur.fetchone()[0]
            except sqlite3.OperationalError:
                indexed = 0
            cur.execute("SELECT count(*) FROM messages")
            n_msg = cur.fetchone()[0]
            if indexed == 0 and n_msg > 0:
                _sqlite_conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild');")
                logger.info("Built full-text search index over %d existing message(s).", n_msg)
            _FTS_OK = True
        except sqlite3.OperationalError as e:
            _FTS_OK = False
            logger.warning("FTS5 unavailable (%s) — message search falls back to LIKE.", e)

        _sqlite_conn.commit()
_init_sqlite()

# ── Migration: move existing JSON messages to SQLite ──
def _migrate_json_to_sqlite():
    """One‑time copy of messages from conversations.json into SQLite,
       then strips the 'messages' key from the JSON file."""
    if not os.path.exists(CONVERSATIONS_FILE):
        return
    try:
        with open(CONVERSATIONS_FILE, "r", encoding="utf-8") as f:
            full_data = json_loads(f.read())
    except Exception:
        return
    migrated_any = False
    for cid, conv in full_data.items():
        with _sqlite_lock:
            cur = _sqlite_conn.cursor()
            cur.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (cid,))
            if cur.fetchone()[0] > 0:
                continue
        msgs = conv.get("messages", [])
        if not msgs:
            continue
        for msg in msgs:
            role = msg.get("role")
            text = msg.get("text", "")
            images = msg.get("images", [])
            files = msg.get("files", [])
            stored_images = [{"name": im.get("name"), "file": im.get("file"), "mime": im.get("mime")} for im in images if im.get("file")]
            stored_files = [{"name": f.get("name"), "file": f.get("file"), "mime": f.get("mime")} for f in files if f.get("file")]
            attachments_json = std_json.dumps({"images": stored_images, "files": stored_files}) if (stored_images or stored_files) else None
            created_at = msg.get("ts", datetime.now().isoformat())
            with _sqlite_lock:
                _sqlite_conn.execute(
                    "INSERT INTO messages (conversation_id, role, content, attachments, created_at) VALUES (?, ?, ?, ?, ?)",
                    (cid, role, text, attachments_json, created_at)
                )
                _sqlite_conn.commit()
        migrated_any = True
    if migrated_any:
        for cid in full_data:
            if "messages" in full_data[cid]:
                del full_data[cid]["messages"]
        with open(CONVERSATIONS_FILE, "w", encoding="utf-8") as f:
            f.write(json_dumps_pretty(full_data))
        logger.info("Migration: messages moved to SQLite and stripped from JSON.")
    else:
        logger.info("Migration: no new messages to move.")

_migrate_json_to_sqlite()

# ── Create JSON files if missing ──
if not os.path.exists(CONVERSATIONS_FILE):
    with open(CONVERSATIONS_FILE, "w", encoding="utf-8") as f:
        std_json.dump({}, f, ensure_ascii=False, indent=2)
if not os.path.exists(MODEL_CONFIG_FILE):
    with open(MODEL_CONFIG_FILE, "w", encoding="utf-8") as f:
        std_json.dump({"model": DEFAULT_MODEL}, f, ensure_ascii=False, indent=2)

# ── SSL ──
def _mkcert_asset_name():
    """Return the mkcert release asset name for this OS/architecture."""
    system = platform.system()
    machine = (platform.machine() or "").lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    if system == "Windows":
        return "mkcert-v1.4.4-windows-amd64.exe"
    if system == "Darwin":
        return "mkcert-v1.4.4-darwin-" + arch
    return "mkcert-v1.4.4-linux-" + arch


def ensure_certificates():
    cert_dir = root_path('cert_store')
    cert_file = os.path.join(cert_dir, 'localhost+1.pem')
    key_file = os.path.join(cert_dir, 'localhost+1-key.pem')
    if os.path.exists(cert_file) and os.path.exists(key_file):
        return True
    logger.info("Certificates not found. Auto-generating...")
    os.makedirs(cert_dir, exist_ok=True)

    # 1) Reuse an existing mkcert on PATH (macOS brew / Linux apt / user-installed).
    mkcert = shutil.which("mkcert")
    if mkcert:
        logger.info("Using mkcert found on PATH: %s", mkcert)
    else:
        # 2) Download the platform-specific release binary (Windows/macOS/Linux).
        asset = _mkcert_asset_name()
        mkcert = os.path.join(cert_dir, "mkcert" + (".exe" if platform.system() == "Windows" else ""))
        if not os.path.exists(mkcert):
            url = "https://github.com/FiloSottile/mkcert/releases/latest/download/" + asset
            logger.info("Downloading mkcert (%s)...", asset)
            try:
                urllib.request.urlretrieve(url, mkcert)
                if platform.system() != "Windows":
                    os.chmod(mkcert, 0o755)
            except Exception as e:
                logger.error("Failed to download mkcert: %s", e)
                return False
    # Install the local CA (best-effort: on Linux it may need sudo; the cert is
    # still generated and just shows a browser warning if install fails).
    try:
        # CREATE_NO_WINDOW: mkcert is a console program, and the app itself runs
        # without a console now, so this would otherwise flash a terminal window.
        subprocess.run([mkcert, "-install"], check=True, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        logger.warning("mkcert -install failed (browser may warn): %s", e)
    try:
        subprocess.run(
            [mkcert, "-cert-file", cert_file, "-key-file", key_file,
             "localhost", "127.0.0.1"],
            check=True, capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return os.path.exists(cert_file) and os.path.exists(key_file)
    except Exception as e:
        logger.error("Certificate generation failed: %s", e)
        return False


def _mkcert_ca_trusted():
    """True if the mkcert local CA was installed (so browsers accept the cert).

    mkcert -install writes its root CA to ~/.local/share/mkcert (Linux/macOS) or
    %LOCALAPPDATA%\\mkcert (Windows) and registers it with the system/NSS trust
    store. If the CA is NOT present, the generated cert is untrusted and browsers
    show a scary "not secure" page — so we serve plain HTTP on localhost instead.
    """
    home_ca = os.path.join(os.path.expanduser("~"), ".local", "share", "mkcert", "rootCA.pem")
    if os.path.isfile(home_ca):
        return True
    local_ca = os.path.join(os.environ.get("LOCALAPPDATA", ""), "mkcert", "rootCA.pem")
    if local_ca and os.path.isfile(local_ca):
        return True
    return False

# ── Model persistence ──
def load_model_config():
    if os.path.exists(MODEL_CONFIG_FILE):
        try:
            with open(MODEL_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = std_json.load(f)
                return data.get("model", DEFAULT_MODEL)
        except Exception:
            pass
    return DEFAULT_MODEL
def save_model_config(model):
    with open(MODEL_CONFIG_FILE, "w", encoding="utf-8") as f:
        std_json.dump({"model": model}, f, ensure_ascii=False, indent=2)
current_model = load_model_config()

# ── Conversation storage (JSON metadata only) ──
_conversations_cache = {}
_cache_loaded = False
_cache_lock = threading.Lock()
_conversations_sorted = None
_conversations_dirty = True

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024  # 25 MB per attachment (defense in depth)

# Uploaded large attachments (see /api/attach/upload) live in the same store the
# app already uses for message attachments, so a message just records the file name.
UPLOADS_DIR = ATTACHMENTS_DIR
# Above this size an attachment is NOT re-read into base64 for the UI: a 563 MB
# meeting recording would become a ~750 MB string in the browser for no reason.
MAX_INLINE_RELOAD_BYTES = 24 * 1024 * 1024


def _attachment_path(entry):
    """Local path of an attachment uploaded by id, or None for inline base64.

    The chat JSON carries small attachments as base64, but its body is capped at
    25 MB - a meeting recording cannot fit through it. Those are uploaded first and
    referenced by the stored file name, which is what this resolves.

    Any extension is accepted, deliberately: the uploader takes whatever the user
    picked, and enumerating formats here only meant that an uploaded .wma/.aiff
    (or a large image) silently resolved to nothing. What matters for safety is that
    the name is one this app generated - a bare 32-hex-char uuid, never a path.
    """
    import re
    raw = str((entry or {}).get("id") or "")
    if not raw or raw != os.path.basename(raw):
        return None                      # rejects any path/traversal outright
    if not re.fullmatch(r"[0-9a-fA-F]{32}(\.[A-Za-z0-9]{1,12})?", raw):
        return None                      # only names this app generated
    path = os.path.join(UPLOADS_DIR, raw)
    return path if os.path.isfile(path) else None


def _with_source_path(entry):
    """A copy of an attachment entry carrying `path` when it was uploaded by id."""
    path = _attachment_path(entry)
    if not path:
        return entry
    merged = dict(entry or {})
    merged["path"] = path
    return merged


def _rehydrate_uploads(entries, max_bytes=None):
    """Fill in base64 for attachments that were uploaded by id.

    Images (and non-audio files) need their bytes inline: the vision and file-text
    paths read `b64`. A video/audio attachment is read from disk instead, so this
    only fills what is missing, and refuses to inline anything oversized.
    """
    limit = MAX_INLINE_RELOAD_BYTES if max_bytes is None else max_bytes
    out = []
    for entry in entries or []:
        item = dict(entry or {})
        if not item.get("b64"):
            path = _attachment_path(item)
            if path:
                try:
                    if os.path.getsize(path) <= limit:
                        with open(path, "rb") as fh:
                            item["b64"] = base64.b64encode(fh.read()).decode("ascii")
                    else:
                        logger.info("Attachment %s is too large to inline (%d bytes)",
                                    item.get("name"), os.path.getsize(path))
                except Exception as e:
                    logger.warning("Could not read uploaded attachment %s: %s",
                                   item.get("name"), e)
        out.append(item)
    return out


def _write_attachment(path, b64_data):
    try:
        raw = base64.b64decode(b64_data)
    except Exception:
        logger.warning("Failed to decode attachment data")
        return
    if len(raw) > MAX_ATTACHMENT_BYTES:
        logger.warning("Rejecting oversized attachment (%d bytes)", len(raw))
        return
    try:
        with open(path, "wb") as f:
            f.write(raw)
    except OSError as e:
        logger.warning("Failed to write attachment: %s", e)

def _save_attachment_to_disk_async(b64_data, hint_name=""):
    if not b64_data:
        return ""
    # Cheap pre-check before decoding (base64 expands ~4/3 over raw bytes).
    if len(b64_data) > MAX_ATTACHMENT_BYTES * 4 // 3 + 4:
        logger.warning("Rejecting oversized attachment (%d base64 chars)", len(b64_data))
        return ""
    ext = os.path.splitext(hint_name)[1] or ".bin"
    fname = f"{uuid.uuid4().hex}{ext}"
    path = os.path.join(ATTACHMENTS_DIR, fname)
    _executor.submit(_write_attachment, path, b64_data)
    return fname

def _load_attachment_from_disk(fname):
    if not fname:
        return ""
    path = os.path.join(ATTACHMENTS_DIR, fname)
    try:
        if os.path.getsize(path) > MAX_INLINE_RELOAD_BYTES:
            # Too big to inline back into the page (it would be a huge base64 string
            # in the browser). The file is still on disk and the name is still shown.
            logger.info("Not inlining %s (%d bytes) - keeping it as a file reference",
                        fname, os.path.getsize(path))
            return ""
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        logger.warning("Failed to read attachment %s: %s", fname, e)
        return ""

def _strip_blobs_for_disk(convs):
    lean = {}
    for cid, conv in convs.items():
        lean_conv = {k: v for k, v in conv.items() if k != "messages"}
        lean[cid] = lean_conv
    return lean

def _ensure_cache():
    global _conversations_cache, _cache_loaded, _conversations_sorted, _conversations_dirty
    if _cache_loaded:
        return
    with _cache_lock:
        if _cache_loaded:
            return
        if os.path.exists(CONVERSATIONS_FILE):
            try:
                with open(CONVERSATIONS_FILE, "r", encoding="utf-8") as f:
                    _conversations_cache = json_loads(f.read())
                for cid, conv in _conversations_cache.items():
                    if "order" not in conv:
                        conv["order"] = 0
                    if "created" not in conv:
                        conv["created"] = datetime.now().isoformat()
                _conversations_sorted = None
                _conversations_dirty = True
            except Exception as e:
                logger.warning("Error loading conversations: %s", e)
                _conversations_cache = {}
        _cache_loaded = True

def load_conversations() -> Dict[str, dict]:
    _ensure_cache()
    return _conversations_cache

def get_sorted_conversations() -> List[dict]:
    global _conversations_sorted, _conversations_dirty
    _ensure_cache()
    if _conversations_dirty or _conversations_sorted is None:
        convs = load_conversations()
        _conversations_sorted = sorted(convs.values(), key=lambda c: (c.get('order', 0), c.get('created', '')))
        _conversations_dirty = False
    return _conversations_sorted

_executor = ThreadPoolExecutor(max_workers=4)
_save_executor = ThreadPoolExecutor(max_workers=1)
_save_timer = None
_save_timer_lock = threading.Lock()
_SAVE_DEBOUNCE_SECONDS = 1.0

def save_conversations_async(convs: Dict[str, dict]) -> None:
    global _save_timer
    def _save():
        try:
            lean = _strip_blobs_for_disk(convs)
            temp_file = CONVERSATIONS_FILE + ".tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                f.write(json_dumps_pretty(lean))
            os.replace(temp_file, CONVERSATIONS_FILE)
        except Exception as e:
            logger.error("Failed to save conversations: %s", e)
    with _save_timer_lock:
        if _save_timer is not None:
            _save_timer.cancel()
        _save_timer = threading.Timer(_SAVE_DEBOUNCE_SECONDS, lambda: _save_executor.submit(_save))
        _save_timer.daemon = True
        _save_timer.start()

def create_conversation(title: Optional[str] = None) -> str:
    global _conversations_dirty
    _ensure_cache()
    cid = str(uuid.uuid4())
    orders = [c.get('order', 0) for c in _conversations_cache.values()]
    max_order = max(orders) if orders else 0
    new_order = max_order + 1
    with _cache_lock:
        _conversations_cache[cid] = {
            "id": cid,
            "title": title or "New Chat",
            "created": datetime.now().isoformat(),
            "order": new_order,
            "last_activity": datetime.now().isoformat()
        }
        _conversations_dirty = True
    save_conversations_async(_conversations_cache)
    return cid

def get_conversation(cid: str) -> Optional[dict]:
    _ensure_cache()
    return _conversations_cache.get(cid)

def get_messages(cid: str) -> List[dict]:
    with _sqlite_lock:
        cur = _sqlite_conn.cursor()
        cur.execute(
            "SELECT id, role, content, attachments, created_at FROM messages WHERE conversation_id = ? ORDER BY created_at",
            (cid,)
        )
        rows = cur.fetchall()
    msgs = []
    for mid, role, content, attachments_json, created_at in rows:
        # `id` lets the UI jump to a specific message from a search hit.
        msg = {"id": mid, "role": role, "text": content or "", "ts": created_at}
        if attachments_json:
            try:
                att = std_json.loads(attachments_json)
                for im in att.get("images", []):
                    if im.get("file"):
                        im["b64"] = _load_attachment_from_disk(im["file"])
                for f in att.get("files", []):
                    if f.get("file"):
                        f["b64"] = _load_attachment_from_disk(f["file"])
                msg["images"] = att.get("images", [])
                msg["files"] = att.get("files", [])
                if att.get("meta"):
                    msg["meta"] = att["meta"]
            except Exception:
                pass
        msgs.append(msg)
    return msgs

def add_message(cid: str, role: str, text: str, images: Optional[List[dict]] = None, files: Optional[List[dict]] = None, meta: Optional[dict] = None) -> bool:
    global _conversations_dirty
    if images is None:
        images = []
    if files is None:
        files = []

    stored_images = []
    for img in images:
        b64 = img.get("b64", "")
        fname = _save_attachment_to_disk_async(b64, img.get("name", "image.png"))
        stored_images.append({
            "name": img.get("name", "image"),
            "file": fname,
            "mime": img.get("mime", "image/png")
        })
    stored_files = []
    for f in files:
        b64 = f.get("b64", "")
        fname = _save_attachment_to_disk_async(b64, f.get("name", "file.bin"))
        stored_files.append({
            "name": f.get("name", "file"),
            "file": fname,
            "mime": f.get("mime", "application/octet-stream")
        })

    att_data = {}
    if stored_images or stored_files:
        att_data["images"] = stored_images
        att_data["files"] = stored_files
    if meta:
        att_data["meta"] = meta
    attachments_json = std_json.dumps(att_data) if att_data else None

    with _sqlite_lock:
        _sqlite_conn.execute(
            "INSERT INTO messages (conversation_id, role, content, attachments, created_at) VALUES (?, ?, ?, ?, ?)",
            (cid, role, text, attachments_json, datetime.now().isoformat())
        )
        _sqlite_conn.commit()

    _ensure_cache()
    with _cache_lock:
        if cid in _conversations_cache:
            conv = _conversations_cache[cid]
            if role == "user" and len(get_messages(cid)) == 1:
                # A readable first title (filler stripped) instead of a raw
                # character truncation. May be refined by _auto_title().
                conv["title"] = (_heuristic_title(text)
                                 or (text[:40] + ("..." if len(text) > 40 else "")))
            conv["last_activity"] = datetime.now().isoformat()
            _conversations_dirty = True
    save_conversations_async(_conversations_cache)
    return True

def delete_conversation(cid: str) -> bool:
    global _conversations_dirty
    _ensure_cache()
    if cid in _conversations_cache:
        meta = _conversations_cache[cid]
        # Archive the conversation + its messages before removing them, so the
        # user can restore it later from the backup database.
        try:
            with _sqlite_lock:
                cur = _sqlite_conn.cursor()
                cur.execute(
                    "SELECT role, content, attachments, created_at FROM messages WHERE conversation_id = ? ORDER BY created_at",
                    (cid,),
                )
                rows = cur.fetchall()
            backup_store.archive_conversation(
                {
                    "id": cid,
                    "title": meta.get("title", ""),
                    "created": meta.get("created"),
                    "order": meta.get("order", 0),
                    "last_activity": meta.get("last_activity"),
                },
                [{"role": r[0], "content": r[1], "attachments": r[2], "created_at": r[3]} for r in rows],
            )
        except Exception as e:
            logger.warning("Backup archive failed for conversation %s: %s", cid, e)
        with _cache_lock:
            del _conversations_cache[cid]
            _conversations_dirty = True
        # Remove the conversation's messages from SQLite too, so no orphans remain.
        with _sqlite_lock:
            _sqlite_conn.execute("DELETE FROM messages WHERE conversation_id = ?", (cid,))
            _sqlite_conn.commit()
        save_conversations_async(_conversations_cache)
        return True
    return False

def clear_conversation_messages(cid: str) -> bool:
    with _sqlite_lock:
        cur = _sqlite_conn.cursor()
        cur.execute(
            "SELECT role, content, attachments, created_at FROM messages WHERE conversation_id = ? ORDER BY created_at",
            (cid,),
        )
        for r in cur.fetchall():
            try:
                backup_store.archive_message(cid, {
                    "role": r[0], "content": r[1], "attachments": r[2], "created_at": r[3],
                })
            except Exception as e:
                logger.warning("Backup archive failed for message in %s: %s", cid, e)
        _sqlite_conn.execute("DELETE FROM messages WHERE conversation_id = ?", (cid,))
        _sqlite_conn.commit()
    return True

def describe_image_with_llava(image_b64):
    vision_model = "llava:7b"
    vision_prompt = "Describe this image in detail. Include objects, colors, layout, text, and any notable features."
    payload = {
        "model": vision_model,
        "prompt": vision_prompt,
        "images": [image_b64],
        "stream": False,
        "options": {"temperature": 0.3}
    }
    try:
        resp = requests.post(f"{OLLAMA_BASE_URL}/api/generate", json=payload, timeout=60)
        resp.raise_for_status()
        return resp.json().get("response", "")
    except Exception as e:
        logger.warning("llava fallback failed: %s", e)
        return ""

def trim_conversation_history(messages, max_messages=10, max_tokens=3000):
    if not messages:
        return messages
    system_msg = None
    if messages and messages[0]["role"] == "system":
        system_msg = messages.pop(0)
    if len(messages) > max_messages:
        messages = messages[-max_messages:]
    total_len = sum(len(m.get("content", "")) for m in messages)
    while messages and total_len > max_tokens * 4:
        if len(messages) > 1:
            removed = messages.pop(1)
            total_len -= len(removed.get("content", ""))
        else:
            break
    if system_msg:
        messages.insert(0, system_msg)
    return messages

def get_ollama_memory_settings():
    try:
        mem = psutil.virtual_memory()
        ram_free_gb = mem.available / (1024**3)
        low_ram = ram_free_gb < 2.0
        vram_available = False
        vram_free_gb = 0
        if NVML_AVAILABLE:
            try:
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                vram_free_gb = info.free / (1024**3)
                vram_available = True
            except Exception:
                pass
        if low_ram and vram_available and vram_free_gb > 2.0:
            return {"num_gpu": 99, "low_vram": True}
        elif low_ram and not vram_available:
            return {"num_gpu": 0, "low_vram": True}
        else:
            return {"num_gpu": 99 if vram_available else 0, "low_vram": False}
    except Exception:
        return {"num_gpu": 99, "low_vram": False}

def is_ollama_command(text):
    return text.strip().lower().startswith("ollama ")

def execute_ollama_command_sync(text):
    parts = text.strip().split()
    if len(parts) < 2:
        return "❌ Usage: ollama <pull|list|ps|rm|push|stop|show> ..."
    cmd = parts[1].lower()
    args = parts[2:]
    try:
        if cmd == 'list':
            r = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
            r.raise_for_status()
            models = r.json().get('models', [])
            return "📦 Installed models:\n" + "\n".join(m['name'] for m in models)
        elif cmd == 'ps':
            result = subprocess.run(['ollama', 'ps'], capture_output=True, text=True, timeout=5)
            return result.stdout or result.stderr
        elif cmd == 'show':
            if not args:
                return "❌ Usage: ollama show <model>"
            model = args[0]
            r = requests.post(f"{OLLAMA_BASE_URL}/api/show", json={"name": model}, timeout=10)
            r.raise_for_status()
            return json_dumps(r.json())
        elif cmd in ('rm', 'delete'):
            if not args:
                return "❌ Usage: ollama rm <model>"
            model = args[0]
            r = requests.delete(f"{OLLAMA_BASE_URL}/api/delete", json={"name": model}, timeout=10)
            r.raise_for_status()
            _models_cache_clear()
            return f"✅ Model '{model}' deleted."
        elif cmd == 'stop':
            if not args:
                return "❌ Usage: ollama stop <model>"
            model = args[0]
            subprocess.run(['ollama', 'stop', model], capture_output=True, text=True, timeout=10)
            return f"✅ Model '{model}' stopped (unloaded from memory)."
        elif cmd == 'pull':
            if not args:
                return "❌ Usage: ollama pull <model>"
            model = args[0]
            r = requests.post(f"{OLLAMA_BASE_URL}/api/pull", json={"name": model}, stream=True, timeout=600)
            r.raise_for_status()
            last_status = ""
            for line in r.iter_lines():
                if line:
                    chunk = json_loads(line)
                    if 'status' in chunk:
                        last_status = chunk['status']
                    if 'error' in chunk:
                        return f"❌ Error pulling '{model}': {chunk['error']}"
            _models_cache_clear()
            return f"✅ Model '{model}' pulled successfully.\nLast status: {last_status}"
        elif cmd == 'push':
            if not args:
                return "❌ Usage: ollama push <model> [--insecure]"
            model = args[0]
            insecure = "--insecure" in args
            payload = {"name": model, "insecure": insecure}
            headers = {}
            token = os.environ.get("OLLAMA_REGISTRY_TOKEN")
            if token:
                headers["Authorization"] = f"Bearer {token}"
            r = requests.post(f"{OLLAMA_BASE_URL}/api/push", json=payload, headers=headers, stream=True, timeout=600)
            r.raise_for_status()
            last_status = ""
            for line in r.iter_lines():
                if line:
                    chunk = json_loads(line)
                    if 'status' in chunk:
                        last_status = chunk['status']
                    if 'error' in chunk:
                        return f"❌ Error pushing '{model}': {chunk['error']}"
            return f"✅ Model '{model}' pushed successfully.\nLast status: {last_status}"
        else:
            return f"❌ Unknown command: {cmd}"
    except Exception as e:
        return f"❌ Command failed: {str(e)}"

def handle_ollama_command_stream(conv_id, user_message, images, files):
    parts = user_message.strip().split()
    if len(parts) < 2:
        yield f"data: {json_dumps({'token': '❌ Usage: ollama <pull|list|ps|rm|push|stop|show> ...'})}\n\n"
        yield f"data: {json_dumps({'done': True, 'full_response': 'Invalid command.'})}\n\n"
        return
    cmd = parts[1].lower()
    args = parts[2:]
    full_response = ""
    try:
        if cmd == 'pull':
            if not args:
                full_response = "❌ Usage: ollama pull <model>"
                yield f"data: {json_dumps({'token': full_response})}\n\n"
            else:
                model = args[0]
                r = requests.post(f"{OLLAMA_BASE_URL}/api/pull", json={"name": model}, stream=True, timeout=600)
                r.raise_for_status()
                for line in r.iter_lines():
                    if line:
                        chunk = json_loads(line)
                        status = chunk.get('status', '')
                        if status:
                            full_response += status + "\n"
                            yield f"data: {json_dumps({'token': status + chr(10)})}\n\n"
                        if 'error' in chunk:
                            err = '❌ ' + chunk['error']
                            full_response += err
                            yield f"data: {json_dumps({'token': err})}\n\n"
                final = f"\n✅ Model '{model}' pulled successfully."
                full_response += final
                yield f"data: {json_dumps({'token': final})}\n\n"
        elif cmd == 'push':
            if not args:
                full_response = "❌ Usage: ollama push <model> [--insecure]"
                yield f"data: {json_dumps({'token': full_response})}\n\n"
            else:
                model = args[0]
                insecure = "--insecure" in args
                payload = {"name": model, "insecure": insecure}
                headers = {}
                token = os.environ.get("OLLAMA_REGISTRY_TOKEN")
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                r = requests.post(f"{OLLAMA_BASE_URL}/api/push", json=payload, headers=headers, stream=True, timeout=600)
                r.raise_for_status()
                for line in r.iter_lines():
                    if line:
                        chunk = json_loads(line)
                        status = chunk.get('status', '')
                        if status:
                            full_response += status + "\n"
                            yield f"data: {json_dumps({'token': status + chr(10)})}\n\n"
                        if 'error' in chunk:
                            err = '❌ ' + chunk['error']
                            full_response += err
                            yield f"data: {json_dumps({'token': err})}\n\n"
                final = f"\n✅ Model '{model}' pushed successfully."
                full_response += final
                yield f"data: {json_dumps({'token': final})}\n\n"
        else:
            output = execute_ollama_command_sync(user_message)
            full_response = output
            for line in output.splitlines():
                yield f"data: {json_dumps({'token': line + chr(10)})}\n\n"
        yield f"data: {json_dumps({'done': True, 'full_response': full_response})}\n\n"
    except Exception as e:
        err = f"❌ Command failed: {e}"
        yield f"data: {json_dumps({'error': err})}\n\n"
    if conv_id:
        add_message(conv_id, "user", user_message, images, files)
        add_message(conv_id, "bot", full_response, [], [])

# ── HTML caching ──
_cached_html = None
_cached_html_key = None

def get_cached_html():
    """Return the index.html contents, re-reading when the FILE changes.

    The cache key is (mtime, size, model) — NOT just the model. Previously the
    model was the only key, so the server kept serving a stale HTML page forever:
    editing templates/index.html or `git pull`-ing an update appeared to "do
    nothing" until a full server restart. Now a changed file is picked up on the
    next page load.
    """
    global _cached_html, _cached_html_key
    try:
        st = os.stat(CHAT_HTML_PATH)
        key = (st.st_mtime_ns, st.st_size, current_model, _ui_settings_stamp())
    except Exception:
        key = (None, None, current_model, _ui_settings_stamp())
    if _cached_html is None or _cached_html_key != key:
        _cached_html = build_html(current_model)
        _cached_html_key = key
    return _cached_html

# ── Build HTML (served from templates/index.html) ──
CHAT_HTML_PATH = root_path("templates", "index.html")

UI_SETTINGS_PATH = root_path("json_configuration", "ui_settings.json")


def _load_ui_settings():
    """The user's UI settings as a dict (empty when none have been saved yet)."""
    try:
        with open(UI_SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json_loads(f.read())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_ui_settings(data):
    try:
        os.makedirs(os.path.dirname(UI_SETTINGS_PATH), exist_ok=True)
        with open(UI_SETTINGS_PATH, "w", encoding="utf-8") as f:
            f.write(json_dumps(data))
    except Exception:
        pass


def _ui_settings_stamp():
    """mtime of the settings file (0 when absent) - part of the HTML cache key."""
    try:
        return os.stat(UI_SETTINGS_PATH).st_mtime_ns
    except Exception:
        return 0


def build_html(model_name=None):
    with open(CHAT_HTML_PATH, "r", encoding="utf-8") as f:
        html = f.read()
    # Inject the user's UI settings (theme, provider, model, persona, last view…)
    # so a WebView2 profile reset - which wipes localStorage - cannot lose them. The
    # frontend overlays this onto localStorage before it reads anything, and mirrors
    # changes back here. "</" is escaped so a value can never break out of the tag.
    settings = _load_ui_settings()
    blob = json_dumps(settings).replace("</", "<\\/")
    inject = "<script>window.__ui_settings = {};</script>".format(blob)
    return html.replace("<head>", "<head>\n" + inject, 1)

# ── Routes ──
@app.route('/unload_model', methods=['POST'])
def unload_model():
    try:
        requests.post(
            f"{OLLAMA_BASE_URL}/api/generate",
            json={"model": current_model, "prompt": "", "keep_alive": 0},
            timeout=3
        )
    except Exception:
        pass
    return '', 204

providers = {
    "ollama": OllamaProvider(model=current_model, base_url=OLLAMA_BASE_URL),
    "llamacpp": LlamaCppProvider(),
    "huggingface": HuggingFaceProvider(),
    "groq": GroqProvider(),
    "deepseek": DeepSeekProvider(),
    "claude": ClaudeProvider(),
    "openrouter": OpenRouterProvider(),
    "gemini": GeminiProvider(),
}

API_PROVIDERS = {"groq", "huggingface", "deepseek", "claude", "openrouter", "gemini"}


def _bot_meta(provider_name, model, reasoning=None):
    """Tag a bot message so the UI can show whether it came from a local model
    (free) or an API-key model (paid), and optionally its chain-of-thought."""
    meta = {
        "kind": "api" if provider_name in API_PROVIDERS else "local",
        "provider": provider_name,
        "model": model or None,
    }
    if reasoning:
        meta["reasoning"] = reasoning
    return meta

# Estimated cost per 1,000,000 tokens (input, output) for paid providers.
# These are rough averages and are editable here if your plan differs.
PROVIDER_PRICING = {
    "ollama":      {"input": 0.0, "output": 0.0},
    "llamacpp":    {"input": 0.0, "output": 0.0},
    "huggingface": {"input": 0.0, "output": 0.0},
    "groq":        {"input": 0.05, "output": 0.08},
    "deepseek":    {"input": 0.27, "output": 1.10},
    "claude":      {"input": 3.00, "output": 15.00},
    "openrouter":  {"input": 0.25, "output": 1.00},
    "gemini":      {"input": 0.30, "output": 2.50},
}


def _estimate_tokens(text):
    """Very rough token estimate (~4 chars/token) for usage tracking."""
    if not text:
        return 0
    return max(1, int(len(text) / 4))


def record_usage(provider, model, session_id, prompt_tokens, completion_tokens):
    """Persist one generation's token/cost estimate to the usage table."""
    rates = PROVIDER_PRICING.get(provider, {"input": 0.0, "output": 0.0})
    cost = (prompt_tokens / 1_000_000) * rates["input"] + (completion_tokens / 1_000_000) * rates["output"]
    try:
        with _sqlite_lock:
            _sqlite_conn.execute(
                "INSERT INTO usage_stats (provider, model, session_id, prompt_tokens, completion_tokens, cost, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (provider, model, session_id, int(prompt_tokens), int(completion_tokens), round(cost, 8), datetime.now().isoformat())
            )
            _sqlite_conn.commit()
    except Exception as e:
        logger.warning("Failed to record usage: %s", e)

@app.route('/api/ping', methods=['GET'])
def api_ping():
    """Lightweight identity marker.

    The launcher calls this to tell TrioForge apart from some OTHER app that
    happens to be holding the port — so it only says "already running" when it
    really is TrioForge, and otherwise picks a free port instead.
    """
    return jsonify({"app": "trioforge", "ok": True})


_PWA_MANIFEST = {
    "name": "TrioForge — AI Workspace",
    "short_name": "TrioForge",
    "description": "Your own private AI workspace — chat, notes and corkboard with local + cloud models.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    # Installed, this runs the whole app WITHOUT browser chrome — so Chat → Notes
    # → Corkboard stay full-screen with no taps. One tap of ⛶ can't do that in a
    # normal tab: the Fullscreen API belongs to the current document, and switching
    # tabs is a real page load, which exits fullscreen. Chrome/Edge honour
    # display_override; iOS/Safari ignore it and fall back to `display`.
    "display_override": ["fullscreen", "standalone"],
    "orientation": "any",
    "background_color": "#0d1117",
    "theme_color": "#0d1117",
    "icons": [
        {"src": "/static/pwa/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/static/pwa/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/static/pwa/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}

# Minimal service worker. Its job is to make the app INSTALLABLE ("Add to Home
# Screen"); it deliberately does NOT cache "/" or the API — this app is local and
# dynamic, and caching the shell is exactly what caused the stale-UI bug.
_SERVICE_WORKER = """\
const CACHE = 'trioforge-static-v1';
self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  let url;
  try { url = new URL(req.url); } catch (e) { return; }
  if (url.origin !== self.location.origin) return;      // never touch CDNs / APIs
  // App shell + API: always network (no stale UI, no stale data).
  if (url.pathname === '/' || url.pathname.startsWith('/api/') ||
      url.pathname.startsWith('/conversations') || url.pathname.startsWith('/notes') ||
      url.pathname.startsWith('/corkboard') || url.pathname.startsWith('/messages')) return;
  // Static assets: cache-first, refreshed in the background.
  if (url.pathname.startsWith('/static/')) {
    event.respondWith((async () => {
      const cache = await caches.open(CACHE);
      const hit = await cache.match(req);
      if (hit) return hit;
      const res = await fetch(req);
      try { if (res && res.ok) cache.put(req, res.clone()); } catch (e) {}
      return res;
    })());
  }
});
"""


@app.route('/manifest.webmanifest', methods=['GET'])
def pwa_manifest():
    resp = Response(json_dumps_pretty(_PWA_MANIFEST), content_type='application/manifest+json')
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@app.route('/sw.js', methods=['GET'])
def pwa_service_worker():
    """Served from the ROOT (not /static/) so its scope covers the whole app."""
    resp = Response(_SERVICE_WORKER, content_type='application/javascript')
    resp.headers['Service-Worker-Allowed'] = '/'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@app.route('/')
def index():
    html = get_cached_html()
    etag = hashlib.md5(html.encode('utf-8')).hexdigest()
    if request.headers.get('If-None-Match') == etag:
        return '', 304
    resp = Response(html)
    resp.headers['ETag'] = etag
    resp.headers['Cache-Control'] = 'no-cache'
    return resp

@app.route('/resources', methods=['GET'])
def get_resources():
    try:
        ram = psutil.virtual_memory()
        ram_used_gb = (ram.total - ram.available) / (1024**3)
        vram_used_gb = None
        if NVML_AVAILABLE:
            try:
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                vram_used_gb = info.used / (1024**3)
            except Exception:
                pass
        if vram_used_gb is None:
            try:
                output = subprocess.check_output(
                    ['rocm-smi', '--showmeminfo', 'vram'],
                    text=True, timeout=5, stderr=subprocess.DEVNULL
                )
                match = re.search(r'Used\s+(\d+)\s+MB', output)
                if match:
                    vram_used_gb = float(match.group(1)) / 1024
            except Exception:
                pass
        if vram_used_gb is None and platform.system() == "Darwin":
            vram_used_gb = ram_used_gb
        return jsonify({
            'ram_used': ram_used_gb,
            'vram_used': vram_used_gb,
            'ram_total': ram.total / (1024**3)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Cached vision check ──
@lru_cache(maxsize=128)
def cached_vision_check(provider_name, model):
    if provider_name == 'ollama' and model:
        try:
            resp = requests.post(f"{OLLAMA_BASE_URL}/api/show", json={"name": model}, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                details = data.get("details", {})
                caps = details.get("capabilities", [])
                if "vision" in caps:
                    return True
                family = details.get("family", "").lower()
                vision_families = VISION_MODELS["ollama"]
                return any(kw in family for kw in vision_families)
        except Exception:
            pass
    return model_supports_vision(provider_name, model)

# Pre‑warm vision cache in background
def prewarm_vision_cache():
    cached_vision_check("ollama", current_model)
    try:
        resp = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=3)
        if resp.status_code == 200:
            models = [m['name'] for m in resp.json().get('models', [])]
            for m in models:
                cached_vision_check("ollama", m)
    except Exception:
        pass
threading.Thread(target=prewarm_vision_cache, daemon=True).start()

@app.route('/check_vision', methods=['POST'])
def check_vision():
    data = request.get_json()
    provider_name = data.get('provider', 'ollama')
    model = data.get('model', '')
    has_vision = cached_vision_check(provider_name, model)
    return jsonify({"vision": has_vision})

@app.route('/providers/models', methods=['POST'])
def get_provider_models():
    data = request.get_json()
    provider_name = data.get('provider', 'ollama')
    api_key = sanitize_api_key(data.get('api_key', None))
    models = _cached_models(provider_name, api_key or 'None')
    return jsonify({'models': models})


# Model lists CHANGE while the app runs: you pull a model in Ollama, download a
# GGUF into models/, or install llama.cpp. An @lru_cache here cached the answer
# for the life of the process, so the dropdown stayed empty until a restart -
# exactly what happens when Ollama is installed after TrioForge was started.
# A short TTL keeps it fast without ever going permanently stale.
_MODELS_TTL_SECONDS = 10
_models_cache: dict = {}
_models_cache_lock = threading.Lock()


def _models_cache_clear() -> None:
    """Forget cached model lists (call after pulling/downloading/removing a model)."""
    with _models_cache_lock:
        _models_cache.clear()


def _cached_models(provider_name, api_key):
    now = time.time()
    key = (provider_name, api_key)
    with _models_cache_lock:
        hit = _models_cache.get(key)
        if hit and (now - hit[0]) < _MODELS_TTL_SECONDS:
            return hit[1]

    models = []
    provider = providers.get(provider_name)
    if provider:
        try:
            models = provider.list_models(api_key=api_key if api_key != 'None' else None)
        except Exception:
            models = []

    with _models_cache_lock:
        _models_cache[key] = (now, models)
    return models

@app.route('/api/models/download', methods=['POST'])
def download_hf_model():
    """Download a GGUF model (and optional mmproj projector) from Hugging Face
    into the local models/ folder so llama.cpp can run it locally."""
    data = request.get_json(silent=True) or {}
    repo_id = (data.get('repo_id') or '').strip()
    filename = (data.get('filename') or '').strip()
    mmproj_filename = (data.get('mmproj_filename') or '').strip()

    if not repo_id:
        return jsonify({'error': 'HuggingFace repo id is required (e.g. bartowski/Qwen2.5-7B-Instruct-GGUF).'}), 400
    if not filename and not mmproj_filename:
        return jsonify({'error': 'Specify a GGUF filename, an mmproj filename, or both.'}), 400

    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        return jsonify({'error': 'huggingface_hub is not installed. Run: pip install huggingface_hub'}), 500

    models_dir = root_path("models")
    os.makedirs(models_dir, exist_ok=True)
    downloaded = []

    for fname in (filename, mmproj_filename):
        if not fname:
            continue
        try:
            local_path = hf_hub_download(
                repo_id=repo_id,
                filename=fname,
                local_dir=models_dir,
            )
            downloaded.append(os.path.basename(local_path))
        except Exception as e:
            return jsonify({'error': f'Failed to download {fname}: {e}'}), 500

    # Invalidate cached model lists so the new file shows up immediately.
    _models_cache_clear()
    llcpp = providers.get("llamacpp")
    if llcpp:
        llcpp.available_models = llcpp._discover_models()
    return jsonify({'ok': True, 'downloaded': downloaded})


@app.route('/set_model', methods=['POST'])
def set_model():
    global current_model, _cached_html
    data = request.get_json()
    model = data.get('model')
    if not model:
        return jsonify({'error': 'No model provided'}), 400
    try:
        resp = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=3)
        if resp.status_code == 200:
            models = [m['name'] for m in resp.json().get('models', [])]
            if model not in models:
                return jsonify({'error': f'Model "{model}" not found in Ollama. Please pull it first.'}), 400
    except Exception:
        pass
    # Unload the PREVIOUS Ollama model before switching, so RAM/VRAM doesn't
    # stack up when the user jumps between several models in one session.
    old_model = current_model
    if old_model and old_model != model:
        try:
            requests.post(
                f"{OLLAMA_BASE_URL}/api/generate",
                json={"model": old_model, "prompt": "", "keep_alive": 0},
                timeout=5,
            )
        except Exception:
            pass
    current_model = model
    save_model_config(model)
    providers["ollama"].model = model
    cached_vision_check.cache_clear()
    _models_cache_clear()
    _cached_html = None
    return jsonify({'ok': True, 'model': model})


# ── llama.cpp server lifecycle (auto-run when provider selected) ──
@app.route('/api/llamacpp/status', methods=['GET'])
def llamacpp_status():
    return jsonify(llamacpp_service.status())


@app.route('/api/llamacpp/start', methods=['POST'])
def llamacpp_start():
    data = request.get_json(silent=True) or {}
    model = data.get('model')
    return jsonify(llamacpp_service.start(model=model))


@app.route('/api/llamacpp/stop', methods=['POST'])
def llamacpp_stop():
    return jsonify(llamacpp_service.stop())


@app.route('/api/llamacpp/capabilities', methods=['GET'])
def llamacpp_capabilities():
    """Return each llama.cpp model with its input capabilities (folder-based)."""
    try:
        llcpp = providers.get("llamacpp")
        return jsonify(llcpp.list_models_with_caps())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Services control panel (turn a specific service on/off) ──
@app.route('/api/services', methods=['GET'])
def services_status():
    return jsonify({
        "llamacpp": llamacpp_service.status(),
        "voice": voice_service.status(),
    })


@app.route('/api/voice/install', methods=['POST'])
def voice_install():
    """Install the speech-to-speech package for voice-to-voice (required, bundled
    with llama.cpp). Explicitly user-triggered; may pull torch + TTS models (~GB)."""
    import subprocess as _sp
    import shutil as _sh
    if _sh.which("speech-to-speech"):
        return jsonify({"ok": True, "path": _sh.which("speech-to-speech"), "message": "already installed"})
    try:
        r = _sp.run([sys.executable, "-m", "pip", "install", "speech-to-speech"],
                    capture_output=True, text=True, timeout=1800)
        if r.returncode == 0:
            return jsonify({"ok": True, "message": "speech-to-speech installed",
                            "path": _sh.which("speech-to-speech") or ""})
        return jsonify({"ok": False, "error": (r.stderr or r.stdout or "")[-500:]}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/llamacpp/install', methods=['POST'])
def llamacpp_install():
    """Auto-download + install llama.cpp for the detected GPU backend.

    Lets macOS/Linux/Windows users avoid a manual llama.cpp install: the app
    detects the backend, downloads the right prebuilt build, and returns the
    llama-server path. This is explicitly user-triggered (never runs on startup).
    """
    import llama_installer
    try:
        result = llama_installer.install_llamacpp()
    except Exception as e:
        return jsonify({'ok': False, 'path': '', 'error': str(e)}), 500
    status_code = 200 if result.get("ok") else 400
    return jsonify(result), status_code


@app.route('/api/llamacpp/install_status', methods=['GET'])
def llamacpp_install_status():
    """Report whether a llama-server is already available (path or '') for the UI."""
    import llama_installer
    installed = llama_installer.find_installed()
    return jsonify({"installed": bool(installed), "path": installed or ""})


@app.route('/api/ui_settings', methods=['GET'])
def ui_settings_get():
    return jsonify(_load_ui_settings())


@app.route('/api/ui_settings', methods=['POST'])
def ui_settings_save():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "expected an object"}), 400
    _save_ui_settings(data)
    return jsonify({"ok": True})


@app.route('/api/attach/upload', methods=['POST'])
def attach_upload():
    """Stream a large attachment to disk and return the stored file name.

    The chat endpoint carries attachments as base64 inside its JSON and its body is
    capped at 25 MB, so a 563 MB meeting recording (~750 MB as base64) can never get
    through - Flask answered 413 and the audio path saw an empty payload. Big media
    is uploaded here instead: streamed straight to disk (no base64 inflation), and
    the chat message then references it by name.
    """
    hint = os.path.basename(request.args.get('name') or 'attachment')
    ext = os.path.splitext(hint)[1][:12]
    fname = "{}{}".format(uuid.uuid4().hex, ext)
    dest = os.path.join(UPLOADS_DIR, fname)
    total = 0
    try:
        os.makedirs(UPLOADS_DIR, exist_ok=True)
        with open(dest, 'wb') as fh:
            stream = request.stream
            while True:
                chunk = stream.read(1 << 20)      # 1 MB at a time
                if not chunk:
                    break
                fh.write(chunk)
                total += len(chunk)
    except Exception as e:
        try:
            os.remove(dest)
        except Exception:
            pass
        logger.warning("Attachment upload failed: %s", e)
        return jsonify({'ok': False, 'error': str(e)}), 500
    if total == 0:
        try:
            os.remove(dest)
        except Exception:
            pass
        return jsonify({'ok': False, 'error': 'the upload was empty'}), 400
    logger.info("Attachment uploaded: %s (%d bytes) as %s", hint, total, fname)
    return jsonify({'ok': True, 'id': fname, 'name': hint, 'size': total,
                    'mime': request.headers.get('Content-Type') or ''})


@app.route('/api/ffmpeg/status', methods=['GET'])
def ffmpeg_status():
    """Report which ffmpeg audio/video conversion will use, and how old it is.

    Age matters: an ancient build (a real user had a 2013 one shipped inside a
    Python package) silently fails on modern containers, and every audio/video
    feature then reported a misleading "is ffmpeg installed?" error.
    """
    import video_to_text
    path = video_to_text.find_ffmpeg()
    return jsonify({
        "installed": bool(path),
        "path": path or "",
        "version": video_to_text.ffmpeg_version(path) if path else "",
        "managed": bool(path and "tools" + os.sep + "ffmpeg" in path),
    })


@app.route('/api/ffmpeg/install', methods=['POST'])
def ffmpeg_install():
    """Download a current ffmpeg into tools/ffmpeg (explicit user action only)."""
    import ffmpeg_installer
    import video_to_text
    try:
        result = ffmpeg_installer.install_ffmpeg()
    except Exception as e:
        return jsonify({'ok': False, 'path': '', 'error': str(e)}), 500
    if result.get("ok"):
        # Drop the cached lookup so the very next conversion uses the new build.
        video_to_text._FFMPEG_CACHE.clear()
        result["version"] = video_to_text.ffmpeg_version(result.get("path", ""))
    return jsonify(result), (200 if result.get("ok") else 400)


@app.route('/api/services/voice/start', methods=['POST'])
def voice_start():
    return jsonify(voice_service.start())


@app.route('/api/services/voice/stop', methods=['POST'])
def voice_stop():
    return jsonify(voice_service.stop())


@app.route('/api/shutdown', methods=['POST'])
def shutdown_all():
    """Stop every TrioForge service and exit the server.

    The desktop window calls this when it is closed, so closing the app also frees
    everything it started - the llama.cpp server, the voice agent, and Ollama's
    loaded model - instead of leaving a Python process and gigabytes of model
    resident after the window is gone. Local-only on purpose: in host mode a remote
    visitor must not be able to kill the server.
    """
    if request.remote_addr not in ("127.0.0.1", "::1", "localhost"):
        return jsonify({"error": "shutdown is local-only"}), 403

    def _teardown():
        time.sleep(0.5)                     # let the HTTP reply flush first
        try:
            llamacpp_service.stop()
        except Exception:
            pass
        try:
            voice_service.stop()
        except Exception:
            pass
        try:
            requests.post(
                f"{OLLAMA_BASE_URL}/api/generate",
                json={"model": current_model, "prompt": "", "keep_alive": 0},
                timeout=3)
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=_teardown, daemon=True).start()
    return jsonify({"ok": True, "shutting_down": True})


@app.route('/deepseek/model_info', methods=['GET'])
def deepseek_model_info():
    model = request.args.get('model')
    if not model:
        return jsonify({"error": "No model specified"}), 400
    provider = providers.get('deepseek')
    if provider and hasattr(provider, 'get_model_info'):
        return jsonify(provider.get_model_info(model))
    return jsonify({"error": "DeepSeek provider not available"}), 404

@app.route('/deepseek/status', methods=['GET'])
def deepseek_status():
    provider = providers.get('deepseek')
    if not provider:
        return jsonify({"ok": False, "error": "Provider not initialized"}), 503
    key = sanitize_api_key(request.args.get('api_key', None))
    if key:
        provider._default_key = key
    return jsonify(provider.get_status())

@app.route('/api/personas', methods=['GET'])
def personas_endpoint():
    return jsonify(personas.list_personas())

@app.route('/api/stats', methods=['GET'])
def stats_endpoint():
    """Aggregate token + estimated-cost usage, grouped by provider and by session."""
    with _sqlite_lock:
        cur = _sqlite_conn.cursor()
        cur.execute("""
            SELECT provider,
                   COUNT(*) AS requests,
                   COALESCE(SUM(prompt_tokens), 0),
                   COALESCE(SUM(completion_tokens), 0),
                   COALESCE(SUM(cost), 0)
            FROM usage_stats
            GROUP BY provider
            ORDER BY SUM(cost) DESC
        """)
        by_provider = [
            {
                "provider": r[0],
                "requests": r[1],
                "prompt_tokens": r[2],
                "completion_tokens": r[3],
                "total_tokens": r[2] + r[3],
                "cost": round(r[4], 8),
            }
            for r in cur.fetchall()
        ]
        cur.execute("""
            SELECT session_id, provider, model,
                   COALESCE(SUM(prompt_tokens), 0),
                   COALESCE(SUM(completion_tokens), 0),
                   COALESCE(SUM(cost), 0),
                   MAX(created_at)
            FROM usage_stats
            GROUP BY session_id, provider, model
            ORDER BY MAX(created_at) DESC
            LIMIT 200
        """)
        by_session = [
            {
                "session_id": r[0],
                "provider": r[1],
                "model": r[2],
                "prompt_tokens": r[3],
                "completion_tokens": r[4],
                "total_tokens": r[3] + r[4],
                "cost": round(r[5], 8),
                "last_used": r[6],
            }
            for r in cur.fetchall()
        ]
    total_cost = round(sum(p["cost"] for p in by_provider), 8)
    total_tokens = sum(p["total_tokens"] for p in by_provider)
    return jsonify({
        "by_provider": by_provider,
        "by_session": by_session,
        "total_cost": total_cost,
        "total_tokens": total_tokens,
        "pricing": PROVIDER_PRICING,
    })

@app.route('/api/generate_image', methods=['POST'])
def generate_image():
    """Generate an image: local .gguf models go through ComfyUI, HF IDs through Diffusers."""
    data = request.get_json(silent=True) or {}
    prompt = (data.get('prompt') or '').strip()
    if not prompt:
        return jsonify({'error': 'Prompt is required'}), 400
    model = (data.get('model') or 'Tongyi-MAI/Z-Image-Turbo').strip()
    width = int(data.get('width') or 1024)
    height = int(data.get('height') or 1024)
    steps = int(data.get('steps') or 4)

    # Free VRAM first: unload the current Ollama model so the image model fits.
    try:
        requests.post(
            f"{OLLAMA_BASE_URL}/api/generate",
            json={"model": current_model, "prompt": "", "keep_alive": 0},
            timeout=3,
        )
    except Exception:
        pass

    out_dir = root_path("static", "uploads", "generated")
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, uuid.uuid4().hex)
    tmp = base + ".bin"
    backend = (data.get('backend') or 'gemini').strip().lower()
    api_key = sanitize_api_key(data.get('api_key', None))

    try:
        if backend == 'gemini':
            gp = providers.get('gemini')
            ext = gp.generate_image(
                prompt, tmp, model=data.get('image_model') or None, api_key=api_key,
                aspect_ratio=data.get('aspect_ratio') or "1:1",
            )
        elif backend == 'openrouter':
            gp = providers.get('openrouter')
            ext = gp.generate_image(
                prompt, tmp, model=data.get('image_model') or None, api_key=api_key,
                aspect_ratio=data.get('aspect_ratio') or "1:1",
                image_size=data.get('image_size') or "1K",
            )
        else:
            comfy_workflow = (data.get('workflow') or '').strip()
            if not comfy_workflow:
                if model.startswith("comfyui::"):
                    comfy_workflow = model[len("comfyui::"):]
                elif model:
                    comfy_workflow = model
            comfyui_service.generate_image(
                prompt, tmp, workflow=comfy_workflow or None, width=width,
                height=height, steps=steps, timeout=900,
            )
            ext = ".png"
        final = base + ext
        os.replace(tmp, final)
        out_name = os.path.basename(final)
    except Exception as e:
        return jsonify({'error': f'Image generation failed: {str(e)}'}), 500

    url = f'/static/uploads/generated/{out_name}'
    meta_kind = "api" if backend in ('gemini', 'openrouter') else "local"
    if backend == 'gemini':
        bot_label = "🖼️ Image generated via Gemini"
    elif backend == 'openrouter':
        bot_label = "🖼️ Image generated via OpenRouter"
    else:
        bot_label = "🖼️ Image generated via ComfyUI"

    cid = (data.get('conversation_id') or '').strip()
    if cid:
        try:
            # Persist the prompt + image into the conversation so the history
            # survives a reload and the delete buttons can remove it.
            add_message(cid, "user", prompt)
            img_b64 = ""
            try:
                with open(final, "rb") as f:
                    img_b64 = base64.b64encode(f.read()).decode("utf-8")
            except Exception:
                pass
            add_message(
                cid, "bot", bot_label,
                images=[{"name": out_name, "b64": img_b64, "mime": "image/png"}],
                meta={"kind": meta_kind},
            )
            return jsonify({'ok': True, 'url': url, 'conversation_id': cid})
        except Exception as e:
            return jsonify({'ok': True, 'url': url, 'conversation_id': cid,
                            'warning': f'Image generated, but history was not saved: {e}'})
    return jsonify({'ok': True, 'url': url})

@app.route('/api/image_models', methods=['GET'])
def image_models():
    """Image generation is handled entirely by ComfyUI; expose its workflows."""
    options = []
    try:
        for wf in comfyui_service.discover_workflows():
            name = wf["name"].lower()
            if wf.get("is_fhdr") or (
                wf.get("is_z_image") and "text to image" in name and "turbo" in name
            ):
                options.append({
                    "label": wf["name"],
                    "value": "comfyui::" + wf["id"],
                })
    except Exception:
        pass
    return jsonify(options)

@app.route('/api/comfyui/status', methods=['GET'])
def comfyui_status():
    """Live ComfyUI detection: running state, install path, workflows."""
    try:
        info = comfyui_service.detect_comfyui()
        if info.get("running"):
            info["install"] = comfyui_service.find_comfyui_install()
            info["workflows"] = comfyui_service.discover_workflows()
        return jsonify(info)
    except Exception as e:
        return jsonify({"running": False, "error": str(e)})

@app.route('/api/video_models', methods=['GET'])
def video_models():
    """ComfyUI video workflows for the video dropdown (live discovery)."""
    try:
        return jsonify([
            {"label": wf["name"], "value": "comfyui::video::" + wf["id"]}
            for wf in comfyui_service.discover_video_workflows()
        ])
    except Exception:
        return jsonify([])

@app.route('/api/openrouter/video_models', methods=['GET'])
def openrouter_video_models():
    """Live list of OpenRouter video-generation models."""
    try:
        key = sanitize_api_key(request.args.get('api_key', None))
        return jsonify(providers.get('openrouter').list_video_models(key or None))
    except Exception:
        return jsonify([])

@app.route('/api/openrouter/image_models', methods=['GET'])
def openrouter_image_models():
    """Live list of OpenRouter image-generation models."""
    try:
        key = sanitize_api_key(request.args.get('api_key', None))
        return jsonify(providers.get('openrouter').list_image_models(key or None))
    except Exception:
        return jsonify([])

@app.route('/api/generate_video', methods=['POST'])
def generate_video():
    """Generate a video via OpenRouter (cloud) or ComfyUI (local)."""
    data = request.get_json(silent=True) or {}
    prompt = (data.get('prompt') or '').strip()
    if not prompt:
        return jsonify({'error': 'Prompt is required'}), 400
    backend = (data.get('backend') or 'openrouter').strip().lower()
    api_key = sanitize_api_key(data.get('api_key', None))

    out_dir = root_path("static", "uploads", "generated_video")
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, uuid.uuid4().hex)
    tmp = base + ".bin"

    try:
        if backend == 'openrouter':
            gp = providers.get('openrouter')
            gp.generate_video(
                prompt, tmp, model=data.get('video_model') or None, api_key=api_key,
                duration=data.get('duration') or None,
                resolution=data.get('resolution') or None,
                aspect_ratio=data.get('aspect_ratio') or None,
                generate_audio=bool(data.get('generate_audio')),
            )
            ext = ".mp4"
        else:
            workflow = (data.get('workflow') or '').strip()
            if workflow.startswith("comfyui::video::"):
                workflow = workflow[len("comfyui::video::"):]
            elif workflow.startswith("comfyui::"):
                workflow = workflow[len("comfyui::"):]
            _, media_name = comfyui_service.generate_video(
                prompt, tmp, workflow=workflow or None,
                width=int(data.get('width') or 1280), height=int(data.get('height') or 720),
                length=int(data.get('length') or 81), steps=int(data.get('steps') or 20),
                timeout=1800,
            )
            ext = os.path.splitext(media_name)[1] or ".mp4"
        final = base + ext
        os.replace(tmp, final)
        url = f'/static/uploads/generated_video/{os.path.basename(final)}'

        # Persist the prompt + video into the conversation so it survives reload.
        cid = (data.get('conversation_id') or '').strip()
        if cid:
            try:
                add_message(cid, "user", prompt)
                meta_kind = "api" if backend == 'openrouter' else "local"
                bot_label = ("🎬 Video generated via OpenRouter" if backend == 'openrouter'
                             else "🎬 Video generated via ComfyUI")
                add_message(cid, "bot", bot_label, meta={"kind": meta_kind, "video": url})
                return jsonify({'ok': True, 'url': url, 'conversation_id': cid})
            except Exception as e:
                return jsonify({'ok': True, 'url': url, 'conversation_id': cid,
                                'warning': f'Video generated, but history not saved: {e}'})
        return jsonify({'ok': True, 'url': url})
    except Exception as e:
        return jsonify({'error': f'Video generation failed: {str(e)}'}), 500


@app.route('/api/audio_workflows', methods=['GET'])
def audio_workflows():
    """List auto-discovered ComfyUI text-to-audio workflows (Stable Audio, ACE-Step, etc.)."""
    options = []
    try:
        for wf in comfyui_service.discover_audio_workflows():
            options.append({
                "label": wf["name"],
                "value": "comfyui::audio::" + wf["id"],
                "kind": wf["kind"],
            })
    except Exception:
        pass
    return jsonify(options)


@app.route('/api/generate_audio', methods=['POST'])
def generate_audio():
    """Generate audio (text-to-audio / music / TTS) via ComfyUI."""
    data = request.get_json(silent=True) or {}
    prompt = (data.get('prompt') or '').strip()
    if not prompt:
        return jsonify({'error': 'Prompt is required'}), 400

    # Free VRAM first: unload the current Ollama model so the audio model fits.
    try:
        requests.post(
            f"{OLLAMA_BASE_URL}/api/generate",
            json={"model": current_model, "prompt": "", "keep_alive": 0},
            timeout=3,
        )
    except Exception:
        pass

    out_dir = root_path("static", "uploads", "generated_audio")
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, uuid.uuid4().hex)
    tmp = base + ".bin"

    try:
        workflow = (data.get('workflow') or '').strip()
        if workflow.startswith("comfyui::audio::"):
            workflow = workflow[len("comfyui::audio::"):]
        elif workflow.startswith("comfyui::"):
            workflow = workflow[len("comfyui::"):]
        _, media_name = comfyui_service.generate_audio(
            prompt, tmp, workflow=workflow or None,
            seed=data.get('seed') or None, timeout=1800,
        )
        ext = os.path.splitext(media_name)[1] or ".mp3"
        final = base + ext
        os.replace(tmp, final)
        url = f'/static/uploads/generated_audio/{os.path.basename(final)}'

        cid = (data.get('conversation_id') or '').strip()
        if cid:
            try:
                add_message(cid, "user", prompt)
                add_message(cid, "bot", "🎵 Audio generated via ComfyUI",
                            meta={"kind": "local", "audio": url})
                return jsonify({'ok': True, 'url': url, 'conversation_id': cid})
            except Exception as e:
                return jsonify({'ok': True, 'url': url, 'conversation_id': cid,
                                'warning': f'Audio generated, but history not saved: {e}'})
        return jsonify({'ok': True, 'url': url})
    except Exception as e:
        return jsonify({'error': f'Audio generation failed: {str(e)}'}), 500

@app.route('/conversations', methods=['GET'])
def list_conversations():
    sorted_list = get_sorted_conversations()
    result = [{
        "id": c["id"],
        "title": c.get("title", "Untitled"),
        "created": c.get("created", ""),
        "order": c.get("order", 0)
    } for c in sorted_list]
    return jsonify(result)

@app.route('/conversations', methods=['POST'])
def create_new_conversation():
    cid = create_conversation()
    return jsonify({"id": cid})

@app.route('/conversations/<cid>', methods=['DELETE'])
def delete_conversation_route(cid):
    ok = delete_conversation(cid)
    return jsonify({"ok": ok})

@app.route('/conversations/<cid>/messages', methods=['GET'])
def get_messages_route(cid):
    if cid not in load_conversations():
        return jsonify([])
    return jsonify(get_messages(cid))

@app.route('/clear_all', methods=['POST'])
def clear_all():
    data = request.get_json(silent=True) or {}
    cid = data.get('cid') or request.args.get('cid')
    if not cid:
        return jsonify({"ok": False, "message": "No conversation id (cid) provided"}), 400
    ok = clear_conversation_messages(cid)
    return jsonify({"ok": ok})

@app.route('/conversations/<cid>/messages/<int:idx>', methods=['PUT'])
def edit_message(cid, idx):
    data = request.get_json()
    new_text = data.get('text', '').strip()
    if not new_text:
        return jsonify({'error': 'Text cannot be empty'}), 400
    with _sqlite_lock:
        cur = _sqlite_conn.cursor()
        cur.execute("SELECT id FROM messages WHERE conversation_id = ? ORDER BY created_at", (cid,))
        rowids = [row[0] for row in cur.fetchall()]
        if idx >= len(rowids):
            return jsonify({'error': 'Index out of range'}), 400
        rowid = rowids[idx]
        cur.execute("UPDATE messages SET content = ? WHERE id = ?", (new_text, rowid))
        _sqlite_conn.commit()
    return jsonify({'ok': True})

@app.route('/conversations/<cid>/messages/<int:idx>', methods=['DELETE'])
def delete_message(cid, idx):
    with _sqlite_lock:
        cur = _sqlite_conn.cursor()
        cur.execute("SELECT id, role, content, attachments, created_at FROM messages WHERE conversation_id = ? ORDER BY created_at", (cid,))
        rows = cur.fetchall()
        if idx < 0 or idx >= len(rows):
            return jsonify({'error': 'Index out of range'}), 400
        rowid = rows[idx][0]
        # Archive the single message before deleting it.
        try:
            backup_store.archive_message(cid, {
                "role": rows[idx][1],
                "content": rows[idx][2],
                "attachments": rows[idx][3],
                "created_at": rows[idx][4],
            })
        except Exception as e:
            logger.warning("Backup archive failed for message %s: %s", rowid, e)
        cur.execute("DELETE FROM messages WHERE id = ?", (rowid,))
        _sqlite_conn.commit()
    return jsonify({'ok': True})

@app.route('/conversations/<cid>/rename', methods=['PUT'])
def rename_conversation(cid):
    global _conversations_dirty
    data = request.get_json()
    new_title = data.get('title', '').strip()
    if not new_title:
        return jsonify({'error': 'Title cannot be empty'}), 400
    _ensure_cache()
    if cid not in _conversations_cache:
        return jsonify({'error': 'Conversation not found'}), 404
    with _cache_lock:
        _conversations_cache[cid]['title'] = new_title
        _conversations_dirty = True
    save_conversations_async(_conversations_cache)
    return jsonify({'ok': True})

@app.route('/conversations/reorder', methods=['POST'])
def reorder_conversations():
    global _conversations_dirty
    data = request.get_json()
    order_map = data.get('order')
    if not order_map or not isinstance(order_map, dict):
        return jsonify({'error': 'Invalid order data'}), 400
    _ensure_cache()
    with _cache_lock:
        for cid, new_order in order_map.items():
            if cid in _conversations_cache:
                _conversations_cache[cid]['order'] = int(new_order)
        _conversations_dirty = True
    save_conversations_async(_conversations_cache)
    return jsonify({'ok': True})

_SNIP_OPEN = "\ue000"   # private-use markers: never occur in real text, survive HTML escaping
_SNIP_CLOSE = "\ue001"


def _fts_query(raw: str) -> str:
    """Turn free user text into a SAFE FTS5 MATCH expression.

    Every token is quoted (so FTS operators in the input can't raise a syntax
    error) and gets a trailing ``*`` for prefix matching — so "hel wor" already
    finds "hello world" as you type. Tokens are ANDed.
    """
    toks = re.findall(r"[0-9A-Za-z_\u00c0-\uffff]{1,}", raw or "")[:12]
    if not toks:
        return ""
    return " ".join('"%s"*' % t.replace('"', '""') for t in toks)


@app.route('/messages/search', methods=['GET'])
def search_messages():
    """Ranked full-text search across ALL messages, with highlighted snippets.

    Returns the matching message (+ its conversation) so the UI can jump straight
    to the hit. Uses FTS5 when available and falls back to a LIKE scan otherwise.
    """
    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify({"results": [], "fts": _FTS_OK, "query": q})
    try:
        limit = max(1, min(int(request.args.get('limit', 40)), 200))
    except (TypeError, ValueError):
        limit = 40

    convs = load_conversations()
    results = []

    if _FTS_OK:
        match = _fts_query(q)
        if match:
            try:
                with _sqlite_lock:
                    cur = _sqlite_conn.cursor()
                    cur.execute(
                        "SELECT m.id, m.conversation_id, m.role, m.created_at, "
                        "snippet(messages_fts, 0, ?, ?, '…', 14) AS snip, "
                        "bm25(messages_fts) AS rank "
                        "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
                        "WHERE messages_fts MATCH ? ORDER BY rank LIMIT ?",
                        (_SNIP_OPEN, _SNIP_CLOSE, match, limit),
                    )
                    rows = cur.fetchall()
                for mid, cid, role, created, snip, rank in rows:
                    c = convs.get(cid) or {}
                    results.append({
                        "id": mid,
                        "conversation_id": cid,
                        "conversation_title": c.get("title", "Untitled"),
                        "role": role,
                        "created_at": created,
                        "snippet": snip,
                        "rank": rank,
                    })
                return jsonify({"results": results, "fts": True, "query": q})
            except sqlite3.OperationalError as e:
                logger.warning("FTS query failed (%s) — falling back to LIKE.", e)

    # ── Fallback: plain LIKE scan (no ranking, simple snippet) ──
    with _sqlite_lock:
        cur = _sqlite_conn.cursor()
        cur.execute(
            "SELECT id, conversation_id, role, created_at, content FROM messages "
            "WHERE LOWER(content) LIKE ? ORDER BY created_at DESC LIMIT ?",
            ('%' + q.lower() + '%', limit),
        )
        rows = cur.fetchall()
    for mid, cid, role, created, content in rows:
        c = convs.get(cid) or {}
        text = content or ""
        i = text.lower().find(q.lower())
        start = max(0, i - 60) if i >= 0 else 0
        snip = ("…" if start else "") + text[start:start + 180]
        if i >= 0:
            rel = i - start
            snip = (snip[:rel] + _SNIP_OPEN + snip[rel:rel + len(q)] + _SNIP_CLOSE + snip[rel + len(q):])
        results.append({
            "id": mid,
            "conversation_id": cid,
            "conversation_title": c.get("title", "Untitled"),
            "role": role,
            "created_at": created,
            "snippet": snip,
            "rank": 0,
        })
    return jsonify({"results": results, "fts": False, "query": q})


# ── Titles ───────────────────────────────────────────────────────────────────
# Filler words stripped when turning a first message into a readable title.
_TITLE_FILLER = re.compile(
    r'^(?:hi|hey|hello|please|pls|can you|could you|would you|can u|help me|i need|i want|'
    r'i would like|write me|write a|make a|create a|give me|show me|tell me|explain|'
    r'what is|whats|what are|how do i|how to|how can i|why is|why does|is it|are there)\b[\s,:]*',
    re.IGNORECASE,
)


def _heuristic_title(text: str, max_words: int = 7) -> str:
    """Turn a first message into a short, readable title (no model call).

    Beats the old raw ``text[:40]`` truncation, which produced titles like
    "can you help me with my python code th...".
    """
    t = re.sub(r'\s+', ' ', (text or '').strip())
    if not t:
        return ""
    # drop a leading filler phrase (may be chained: "hey, can you ...")
    for _ in range(3):
        new = _TITLE_FILLER.sub('', t).strip()
        if new == t or not new:
            break
        t = new
    # strip code fences / urls / markdown noise for a cleaner title
    t = re.sub(r'```.*?```', ' ', t, flags=re.DOTALL)
    t = re.sub(r'https?://\S+', ' ', t)
    t = re.sub(r'[*_`#>\[\]()]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip(' .,:;!?-')
    if not t:
        return ""
    words = t.split(' ')
    title = ' '.join(words[:max_words])
    if len(words) > max_words:
        title += '…'
    return title[0].upper() + title[1:] if title else ""


def _set_conversation_title(cid: str, title: str) -> bool:
    """Set a conversation title (only if it still looks auto/default)."""
    global _conversations_dirty
    if not cid or not title:
        return False
    _ensure_cache()
    with _cache_lock:
        conv = _conversations_cache.get(cid)
        if not conv:
            return False
        conv["title"] = title[:80]
        _conversations_dirty = True
    save_conversations_async(_conversations_cache)
    return True


def _llm_title(provider, model, api_key, user_text, bot_text) -> Optional[str]:
    """Ask the current model for a 3–6 word title. Best-effort; returns None on any issue."""
    prompt = (
        "Write a short title (3-6 words, no quotes, no trailing period) for a "
        "conversation that starts like this.\n\n"
        "USER: " + (user_text or "")[:600] + "\n"
        "ASSISTANT: " + (bot_text or "")[:600] + "\n\n"
        "Title:"
    )
    kwargs = {"model": model}
    if api_key:
        kwargs["api_key"] = api_key
    kwargs["max_tokens"] = 24
    out = provider.generate([{"role": "user", "content": prompt}], **kwargs)
    title = re.sub(r'^[\s"\'`*#-]+|[\s"\'`*.]+$', '', (out or '').strip().splitlines()[0] if out else '')
    title = re.sub(r'^(title|conversation)\s*[:\-]\s*', '', title, flags=re.IGNORECASE)
    if 2 <= len(title) <= 90 and len(title.split()) <= 10:
        return title
    return None


def _auto_title(cid, user_text, bot_text, provider_name=None, model=None, api_key=None):
    """Name the conversation on its first exchange.

    Always applies a fast heuristic title. With TRIOFORGE_AUTO_TITLE=llm it ALSO
    asks the model to refine it in a background thread (so the reply is never
    delayed, and a local single-slot server is never blocked).
    """
    base = _heuristic_title(user_text)
    if base:
        _set_conversation_title(cid, base)

    # Only name on the FIRST exchange (2 messages: the user's + the reply).
    try:
        if len(get_messages(cid)) > 2:
            return
    except Exception:
        pass

    mode = (os.environ.get("TRIOFORGE_AUTO_TITLE", "heuristic") or "heuristic").strip().lower()
    if mode != "llm" or not provider_name:
        return
    try:
        provider = providers.get(provider_name)
    except Exception:
        return
    if provider is None:
        return

    def run():
        try:
            better = _llm_title(provider, model or current_model, api_key, user_text, bot_text)
            if better:
                _set_conversation_title(cid, better)
        except Exception as e:
            logger.debug("Auto-title (llm) failed: %s", e)

    threading.Thread(target=run, daemon=True).start()


# ── Export / import ──────────────────────────────────────────────────────────
def _conversation_payload(cid: str) -> dict:
    """Full exportable conversation (metadata + all messages)."""
    conv = load_conversations().get(cid) or {}
    msgs = []
    for m in get_messages(cid):
        entry = {"role": m.get("role"), "text": m.get("text", ""), "ts": m.get("ts", "")}
        if m.get("images"):
            entry["images"] = [{"name": i.get("name"), "mime": i.get("mime")} for i in m["images"]]
        if m.get("files"):
            entry["files"] = [{"name": f.get("name"), "mime": f.get("mime")} for f in m["files"]]
        if m.get("meta"):
            entry["meta"] = m["meta"]
        msgs.append(entry)
    return {
        "trioforge_export": 1,
        "id": cid,
        "title": conv.get("title", "Untitled"),
        "created": conv.get("created", ""),
        "exported_at": datetime.now().isoformat(),
        "message_count": len(msgs),
        "messages": msgs,
    }


def _conversation_markdown(cid: str) -> str:
    """Human-readable Markdown transcript of one conversation."""
    p = _conversation_payload(cid)
    out = ["# " + (p.get("title") or "Untitled"), ""]
    out.append("_Exported from TrioForge · %s · %d messages_" % (p.get("created", ""), p["message_count"]))
    out.append("")
    for m in p["messages"]:
        role = (m.get("role") or "").lower()
        who = "🧑 **You**" if role == "user" else "🤖 **Assistant**"
        out.append("## " + who + (("  ·  " + m["ts"]) if m.get("ts") else ""))
        out.append("")
        out.append(m.get("text") or "")
        out.append("")
        if m.get("images") or m.get("files"):
            names = [x.get("name", "?") for x in (m.get("images") or []) + (m.get("files") or [])]
            out.append("_Attachments: " + ", ".join(names) + "_")
            out.append("")
    return "\n".join(out)


def _download(text: str, filename: str, mimetype: str):
    # content_type= (not mimetype=) so Flask doesn't append a SECOND charset.
    resp = Response(text, content_type=mimetype)
    resp.headers["Content-Disposition"] = 'attachment; filename="%s"' % filename
    return resp


@app.route('/conversations/<cid>/export', methods=['GET'])
def export_conversation(cid):
    """Download ONE conversation as Markdown (default) or JSON."""
    if cid not in load_conversations():
        return jsonify({"error": "Conversation not found"}), 404
    fmt = (request.args.get('format') or 'md').lower()
    safe = re.sub(r'[^A-Za-z0-9._-]+', '-', (load_conversations()[cid].get('title') or 'chat'))[:60].strip('-') or 'chat'
    if fmt == 'json':
        return _download(json_dumps_pretty(_conversation_payload(cid)),
                         safe + '.json', 'application/json')
    return _download(_conversation_markdown(cid), safe + '.md', 'text/markdown; charset=utf-8')


@app.route('/conversations/export', methods=['GET'])
def export_all_conversations():
    """Download EVERY conversation as one Markdown or JSON file."""
    convs = get_sorted_conversations()
    fmt = (request.args.get('format') or 'md').lower()
    stamp = datetime.now().strftime('%Y%m%d-%H%M')
    if fmt == 'json':
        payload = {
            "trioforge_export": 1,
            "exported_at": datetime.now().isoformat(),
            "conversation_count": len(convs),
            "conversations": [_conversation_payload(c["id"]) for c in convs],
        }
        return _download(json_dumps_pretty(payload),
                         'trioforge-chats-%s.json' % stamp, 'application/json')
    parts = ["# TrioForge — all conversations", "",
             "_Exported %s · %d conversation(s)_" % (datetime.now().isoformat(timespec='seconds'), len(convs)), "", "---", ""]
    for c in convs:
        parts.append(_conversation_markdown(c["id"]))
        parts.append("\n---\n")
    return _download("\n".join(parts), 'trioforge-chats-%s.md' % stamp, 'text/markdown; charset=utf-8')


@app.route('/conversations/import', methods=['POST'])
def import_conversations():
    """Import conversations from an export (JSON body or an uploaded .json file).

    Accepts either a single conversation object or a bundle with "conversations".
    Always creates NEW ids, so importing can never overwrite existing chats.
    """
    data = request.get_json(silent=True)
    if data is None:
        f = request.files.get('file')
        if f is None:
            return jsonify({"ok": False, "error": "Send JSON (body) or a .json file"}), 400
        try:
            data = json_loads(f.read().decode('utf-8', 'replace'))
        except Exception as e:
            return jsonify({"ok": False, "error": "Invalid JSON: %s" % e}), 400

    items = data.get("conversations") if isinstance(data, dict) and "conversations" in data else [data]
    if not isinstance(items, list):
        return jsonify({"ok": False, "error": "Expected a conversation object or list"}), 400

    imported = []
    for item in items[:500]:
        if not isinstance(item, dict):
            continue
        msgs = item.get("messages") or []
        if not msgs:
            continue
        title = (item.get("title") or "Imported chat")[:80]
        new_id = create_conversation(title=title + (" (imported)" if "imported" not in title.lower() else ""))
        for m in msgs:
            role = m.get("role") or "user"
            text = m.get("text")
            if text is None:
                text = m.get("content", "")
            add_message(new_id, role, text or "")
        imported.append({"id": new_id, "title": title, "messages": len(msgs)})

    if not imported:
        return jsonify({"ok": False, "error": "No messages found in the import"}), 400
    logger.info("Imported %d conversation(s).", len(imported))
    return jsonify({"ok": True, "imported": len(imported), "conversations": imported})


@app.route('/conversations/search', methods=['GET'])
def search_conversations():
    query = request.args.get('q', '').strip().lower()
    if not query:
        return jsonify([])
    convs = load_conversations()
    results = []
    for cid, conv in convs.items():
        title_match = query in conv.get('title', '').lower()
        if title_match:
            results.append({
                "id": conv["id"],
                "title": conv.get("title", "Untitled"),
                "created": conv.get("created", ""),
                "order": conv.get("order", 0)
            })
    with _sqlite_lock:
        cur = _sqlite_conn.cursor()
        cur.execute(
            "SELECT DISTINCT conversation_id FROM messages WHERE LOWER(content) LIKE ?",
            ('%' + query + '%',)
        )
        cids_with_match = [row[0] for row in cur.fetchall()]
    for cid in cids_with_match:
        if cid not in [r["id"] for r in results]:
            conv = convs.get(cid)
            if conv:
                results.append({
                    "id": conv["id"],
                    "title": conv.get("title", "Untitled"),
                    "created": conv.get("created", ""),
                    "order": conv.get("order", 0)
                })
    results.sort(key=lambda c: (c.get('order', 0), c.get('created', '')))
    return jsonify(results)

# ── NEW ROUTE: Get conversation tree for import ──
@app.route('/api/conversations/<cid>/tree', methods=['GET'])
def conversation_tree(cid):
    if cid not in load_conversations():
        return jsonify({"error": "Conversation not found"}), 404
    with _sqlite_lock:
        cur = _sqlite_conn.cursor()
        cur.execute(
            "SELECT id, role, content, created_at FROM messages WHERE conversation_id = ? ORDER BY created_at",
            (cid,)
        )
        rows = cur.fetchall()
    if not rows:
        return jsonify({"error": "No messages found for this conversation"}), 404
    nodes = []
    prev_id = None
    for row in rows:
        content = row[2] or ""
        title = content[:40] + ("…" if len(content) > 40 else "") or f"{row[1]} message"
        node = {
            "id": str(row[0]),
            "parent_id": prev_id,
            "role": row[1],
            "content": content,
            "title": title,
            "created_at": row[3]
        }
        nodes.append(node)
        prev_id = str(row[0])
    return jsonify({"nodes": nodes})

# ── Workspaces (folder + thinking + dependencies) ──
def _ensure_workspace_default():
    os.makedirs(WORKSPACES_DIR, exist_ok=True)
    default = os.path.join(WORKSPACES_DIR, "default")
    os.makedirs(default, exist_ok=True)
    cfg_path = os.path.join(default, "config.json")
    if not os.path.exists(cfg_path):
        with open(cfg_path, "w", encoding="utf-8") as f:
            std_json.dump({"id": "default", "name": "Default",
                           "folder": "", "folder_mode": "read",
                           "thinking": "high", "dependencies": [],
                           "full_access": False}, f, indent=2)
    if not os.path.exists(CURRENT_WORKSPACE_FILE):
        with open(CURRENT_WORKSPACE_FILE, "w", encoding="utf-8") as f:
            f.write("default")


def _list_workspaces():
    _ensure_workspace_default()
    workspaces = []
    for name in sorted(os.listdir(WORKSPACES_DIR)):
        wdir = os.path.join(WORKSPACES_DIR, name)
        cfg_path = os.path.join(wdir, "config.json")
        if os.path.isdir(wdir) and os.path.exists(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = std_json.load(f)
                workspaces.append({
                    "id": cfg.get("id", name),
                    "name": cfg.get("name", name),
                    "folder": cfg.get("folder", ""),
                    "folder_mode": cfg.get("folder_mode", "read"),
                    "thinking": cfg.get("thinking", "high"),
                    "dependencies": cfg.get("dependencies", []),
                    "full_access": bool(cfg.get("full_access", False)),
                })
            except Exception:
                continue
    return workspaces


def _current_workspace_id():
    _ensure_workspace_default()
    try:
        with open(CURRENT_WORKSPACE_FILE, "r", encoding="utf-8") as f:
            wid = f.read().strip()
        return wid or "default"
    except Exception:
        return "default"


def _get_workspace(wid):
    cfg_path = os.path.join(WORKSPACES_DIR, wid, "config.json")
    if not os.path.exists(cfg_path):
        return None
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            return std_json.load(f)
    except Exception:
        return None


def _workspace_setting(wid, key, default=None):
    cfg = _get_workspace(wid)
    if cfg:
        return cfg.get(key, default)
    return default


# ── Live coding (agent edits) ──────────────────────────
# The agent's write_file / edit_file calls append a diff entry here, so the UI
# can show a live "what the model is changing" panel. Edits are also persisted
# to sqlite_data/edits.db (via edits_store) so they survive a restart.
AGENT_EDITS = []
AGENT_EDITS_LOCK = threading.Lock()
_agent_edit_seq = 0


def _record_edit(tool, path, before="", after="", detail=""):
    """Record one agent file mutation with a unified diff for the live panel."""
    entry = edits_store.insert(tool, path, before=before, after=after, detail=detail)
    with AGENT_EDITS_LOCK:
        AGENT_EDITS.append(entry)
        # Keep the buffer bounded (last 200 edits).
        if len(AGENT_EDITS) > 200:
            del AGENT_EDITS[0 : len(AGENT_EDITS) - 200]
    return entry


_EXT_MAP = {
    "python": "py", "py": "py", "javascript": "js", "js": "js",
    "typescript": "ts", "ts": "ts", "json": "json", "html": "html",
    "css": "css", "bash": "sh", "sh": "sh", "shell": "sh",
    "ruby": "rb", "go": "go", "rust": "rs", "rs": "rs",
    "java": "java", "c": "c", "cpp": "cpp", "c++": "cpp",
    "csharp": "cs", "cs": "cs", "yaml": "yaml", "yml": "yml",
    "toml": "toml", "md": "md", "markdown": "md", "sql": "sql",
    "xml": "xml", "php": "php", "swift": "swift", "kt": "kt",
    "kotlin": "kt", "dart": "dart", "r": "r", "perl": "pl",
}

# Lines that look like real code (not prose). Used to decide whether a chunk of
# the reply is code even when it isn't fenced or indented.
_CODE_HINT_RE = re.compile(r"^(def |class |import |from |function |const |let |var |return |print\(|if |else|elif |for |while |try:|except|public |private |#include|using |fn |func )", re.M)


def _ext_for_lang(lang):
    lang = (lang or "").lower().strip()
    if not lang:
        return "txt"
    return _EXT_MAP.get(lang, lang.split(".")[-1].split("-")[0] or "txt")


def _record_code_blocks(text):
    """Capture code the model emits (big or small) as live "write" edits.

    Handles three shapes, so it isn't limited to one provider's formatting:
      1. Fenced blocks:      ```lang ... ```
      2. Indented blocks:    lines with 4+ leading spaces (pasted code)
      3. Code-heavy replies: a chunk that is mostly code-looking lines

    Returns the number of blocks captured.
    """
    if not text:
        return 0
    blocks = []  # (label, code)
    # 1) Fenced ``` ... ``` blocks.
    for lang, body in re.findall(r"```([A-Za-z0-9_+\-]*)\s*\n(.*?)```", text, re.DOTALL):
        body = body.rstrip("\n")
        if body.strip():
            blocks.append((lang, body))
    # 2) Indented code blocks (4+ spaces or a tab) — collect contiguous lines.
    indented = []
    for ln in text.split("\n"):
        if re.match(r"^(?: {4,}|\t)\S", ln):
            indented.append(ln.strip())
        else:
            if len(indented) >= 2:
                blocks.append(("", "\n".join(indented)))
            indented = []
    if len(indented) >= 2:
        blocks.append(("", "\n".join(indented)))
    # 3) Fallback: if the reply itself is mostly code-looking, record it whole.
    if not blocks and len(text.split("\n")) >= 2:
        code_hits = len(_CODE_HINT_RE.findall(text))
        if code_hits >= 2:
            blocks.append(("", text.strip()))

    count = 0
    for i, (lang, body) in enumerate(blocks, 1):
        if not body:
            continue
        ext = _ext_for_lang(lang)
        path = "generated/code_{:02d}.{}".format(i, ext)
        _record_edit("write_file", path, "", body, detail="auto-captured code ({})".format(lang or "code"))
        count += 1
    return count


# ── LLM tool definitions (workspace folder access) ──
WORKSPACE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and folders in the workspace folder, optionally recursively under a sub-path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "relative sub-folder to list (empty for the root)"},
                    "recursive": {"type": "boolean", "description": "list the whole tree recursively"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search file names and contents in the workspace folder for a term or regular expression.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "text or regex to search for"},
                    "regex": {"type": "boolean", "description": "treat query as a regular expression"},
                },
                "required": ["query"], "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file from the workspace folder.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "relative path inside the workspace folder"}},
                "required": ["path"], "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write (create or overwrite) a text file in the workspace folder. Only allowed when folder access is readwrite.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "relative path inside the workspace folder"},
                    "content": {"type": "string", "description": "full text content to write"},
                },
                "required": ["path", "content"], "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Make a surgical text replacement in a file: replace the first exact occurrence of old_string with new_string. Safer than rewriting the whole file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "relative path inside the workspace folder"},
                    "old_string": {"type": "string", "description": "exact text to replace"},
                    "new_string": {"type": "string", "description": "replacement text"},
                },
                "required": ["path", "old_string", "new_string"], "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command inside the workspace folder and return its stdout/stderr (bounded to a few seconds). Use for building, testing, git, or inspecting files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "the shell command to run"},
                },
                "required": ["command"], "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web (DuckDuckGo) and return the top snippet(s) for up-to-date facts, docs, or APIs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "the search query"},
                },
                "required": ["query"], "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_url",
            "description": "Open a URL in the default web browser (requires full access).",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "the URL to open"}},
                "required": ["url"], "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_app",
            "description": "Open an installed application by name or path (requires full access).",
            "parameters": {
                "type": "object",
                "properties": {"app": {"type": "string", "description": "app name or path, e.g. notepad, calc, chrome"}},
                "required": ["app"], "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Type text into the currently focused window (requires full access + keyboard automation).",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string", "description": "text to type"}},
                "required": ["text"], "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_keys",
            "description": "Press a key combination, e.g. 'ctrl+c', 'enter', 'alt+tab' (requires full access + keyboard automation).",
            "parameters": {
                "type": "object",
                "properties": {"keys": {"type": "string", "description": "key combo, e.g. 'ctrl+c'"}},
                "required": ["keys"], "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": "Take a screenshot of the screen and save it (requires full access + screenshot lib). Returns the saved path.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
]


def _list_files_recursive(base, rel=""):
    """Return a sorted list of relative paths (dirs end with '/') under `base`."""
    out = []
    try:
        entries = sorted(os.listdir(os.path.join(base, rel) if rel else base))
    except OSError as e:
        return [{"error": str(e)}]
    for name in entries:
        if name.startswith('.git') or name in ('__pycache__', 'node_modules', '.venv', 'venv'):
            continue
        p = os.path.join(base, rel, name) if rel else os.path.join(base, name)
        relp = (rel + "/" + name) if rel else name
        if os.path.isdir(p):
            out.append(relp + "/")
            out.extend(_list_files_recursive(base, relp))
        else:
            out.append(relp)
    return out


def _execute_tool(name, args):
    """Run a workspace-folder tool and return a JSON-serializable result."""
    wid = _current_workspace_id()
    base = _workspace_setting(wid, "folder", "") or ""

    if name == "list_files":
        if not base or not os.path.isdir(base):
            return {"error": "No workspace folder configured."}
        sub = args.get("path") or ""
        target, err = _resolve_workspace_file(wid, sub)
        if err:
            return {"error": err}
        if not os.path.isdir(target):
            return {"error": "Not a folder: {}".format(sub)}
        if args.get("recursive"):
            return {"files": _list_files_recursive(target)}
        return {"files": sorted(os.listdir(target))}

    if name == "search_files":
        if not base or not os.path.isdir(base):
            return {"error": "No workspace folder configured."}
        query = args.get("query", "")
        if not query:
            return {"error": "Query is required."}
        use_regex = bool(args.get("regex"))
        try:
            rx = re.compile(query) if use_regex else None
        except re.error as e:
            return {"error": "Invalid regex: {}".format(e)}
        matches = []
        for relp in _list_files_recursive(base):
            if relp.endswith("/"):
                continue
            full = os.path.join(base, relp)
            hit = False
            if rx:
                hit = bool(rx.search(relp))
            else:
                hit = query.lower() in relp.lower()
            if not hit:
                try:
                    with open(full, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read(20000)
                    hit = (bool(rx.search(text)) if rx else (query.lower() in text.lower()))
                except Exception:
                    hit = False
            if hit:
                matches.append(relp)
            if len(matches) >= 50:
                break
        return {"matches": matches}

    if name == "read_file":
        target, err = _resolve_workspace_file(wid, args.get("path", ""))
        if err:
            return {"error": err}
        if not os.path.isfile(target):
            return {"error": "File not found: {}".format(args.get("path"))}
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                return {"content": f.read()[:20000]}
        except Exception as e:
            return {"error": str(e)}

    if name == "write_file":
        if _workspace_setting(wid, "folder_mode", "read") != "readwrite":
            return {"error": "Workspace folder is read-only."}
        target, err = _resolve_workspace_file(wid, args.get("path", ""))
        if err:
            return {"error": err}
        try:
            before = ""
            if os.path.isfile(target):
                try:
                    with open(target, "r", encoding="utf-8", errors="replace") as f:
                        before = f.read()[:4000]
                except Exception:
                    before = ""
            after = args.get("content", "")
            os.makedirs(os.path.dirname(target), exist_ok=True) if os.path.dirname(target) else None
            with open(target, "w", encoding="utf-8") as f:
                f.write(after)
            _record_edit("write_file", args.get("path", ""), before, after)
            return {"ok": True, "path": args.get("path", "")}
        except Exception as e:
            return {"error": str(e)}

    if name == "edit_file":
        if _workspace_setting(wid, "folder_mode", "read") != "readwrite":
            return {"error": "Workspace folder is read-only."}
        target, err = _resolve_workspace_file(wid, args.get("path", ""))
        if err:
            return {"error": err}
        if not os.path.isfile(target):
            return {"error": "File not found: {}".format(args.get("path"))}
        old = args.get("old_string", "")
        new = args.get("new_string", "")
        if old == "" or old == new:
            return {"error": "old_string must be non-empty and different from new_string."}
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception as e:
            return {"error": str(e)}
        count = text.count(old)
        if count == 0:
            return {"error": "old_string not found in file."}
        if count > 1:
            return {"error": "old_string is ambiguous (found {} times). Provide a longer, unique string.".format(count)}
        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(text.replace(old, new, 1))
            _record_edit("edit_file", args.get("path", ""), old, new)
            return {"ok": True, "path": args.get("path", ""), "replaced": 1}
        except Exception as e:
            return {"error": str(e)}

    if name == "run_command":
        if not base or not os.path.isdir(base):
            return {"error": "No workspace folder configured."}
        cmd = args.get("command", "")
        if not cmd:
            return {"error": "Command is required."}
        try:
            proc = subprocess.run(
                cmd, shell=True, cwd=base, capture_output=True, text=True,
                timeout=30, errors="replace",
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            return {"exit_code": proc.returncode, "output": out[-4000:]}
        except subprocess.TimeoutExpired:
            return {"error": "Command timed out after 30s."}
        except Exception as e:
            return {"error": str(e)}

    if name == "web_search":
        query = (args.get("query") or "").strip()
        if not query:
            return {"error": "Query is required."}
        if not SEARCH_AVAILABLE:
            return {"error": "Web search unavailable (install duckduckgo-search)."}
        try:
            future = _executor.submit(lambda: DDGS().text(query, max_results=3))
            results = future.result(timeout=3)
            snippets = [r.get("body", "") for r in results if r.get("body")]
            return {"results": snippets[:3]}
        except Exception as e:
            return {"error": "Search failed: {}".format(e)}

    # ── Computer control (gated by workspace "full_access") ──
    if name in ("open_url", "open_app", "type_text", "press_keys", "screenshot"):
        if _workspace_setting(wid, "full_access", False) is not True:
            return {"error": "Full computer access is disabled. Enable it in Workspace settings (⚙️)."}

    if name == "open_url":
        url = (args.get("url") or "").strip()
        if not url:
            return {"error": "url is required."}
        if not re.match(r"^https?://", url, re.I):
            return {"error": "Only http(s) URLs are allowed."}
        try:
            import webbrowser
            webbrowser.open(url)
            return {"ok": True, "opened": url}
        except Exception as e:
            return {"error": str(e)}

    if name == "open_app":
        app = (args.get("app") or "").strip()
        if not app:
            return {"error": "app is required."}
        try:
            if os.name == "nt":
                # Open by name (notepad, calc, …) or by path, but without spawning a
                # visible console window - `cmd /c start` used to flash one every time
                # the agent opened an app.
                subprocess.Popen(["cmd", "/c", "start", "", app], shell=False,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                subprocess.Popen(["xdg-open", app] if not os.path.isfile(app) else [app])
            return {"ok": True, "opened": app}
        except Exception as e:
            return {"error": str(e)}

    if name == "type_text":
        text = args.get("text", "")
        if not text:
            return {"error": "text is required."}
        try:
            import pyautogui
            pyautogui.write(text, interval=0.02)
            return {"ok": True}
        except ImportError:
            return {"error": "pyautogui not installed. Run: pip install pyautogui"}
        except Exception as e:
            return {"error": str(e)}

    if name == "press_keys":
        keys = (args.get("keys") or "").strip()
        if not keys:
            return {"error": "keys is required."}
        try:
            import pyautogui
            combo = [k.strip() for k in keys.split("+") if k.strip()]
            pyautogui.hotkey(*combo)
            return {"ok": True, "pressed": keys}
        except ImportError:
            return {"error": "pyautogui not installed. Run: pip install pyautogui"}
        except Exception as e:
            return {"error": str(e)}

    if name == "screenshot":
        try:
            import pyautogui
            out_dir = root_path("static", "uploads", "screenshots")
            os.makedirs(out_dir, exist_ok=True)
            fname = uuid.uuid4().hex + ".png"
            path = os.path.join(out_dir, fname)
            pyautogui.screenshot(path)
            return {"ok": True, "path": path, "url": f"/static/uploads/screenshots/{fname}"}
        except ImportError:
            return {"error": "pyautogui not installed. Run: pip install pyautogui"}
        except Exception as e:
            return {"error": str(e)}

    return {"error": "Unknown tool: {}".format(name)}


def _run_chat_with_tools(provider, messages, extra_kwargs, max_steps=20):
    """Run an OpenAI-style tool-calling loop against an OpenAI-compatible provider.

    max_steps caps the number of model→tool round-trips (a "coding agent" loop:
    read, edit, run, repeat until done). 20 is enough for a multi-file task.
    """
    if isinstance(provider, ClaudeProvider):
        return _run_chat_with_tools_claude(provider, messages, extra_kwargs, max_steps)
    messages = list(messages)
    for _ in range(max_steps):
        resp = provider.generate_raw(messages, tools=WORKSPACE_TOOLS, **extra_kwargs)
        content = resp.get("content")
        tool_calls = resp.get("tool_calls") or []
        if not tool_calls:
            return content or ""
        assistant = {"role": "assistant", "content": content}
        assistant["tool_calls"] = [{
            "id": tc["id"], "type": "function",
            "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]},
        } for tc in tool_calls]
        messages.append(assistant)
        for tc in tool_calls:
            raw_args = tc["function"]["arguments"]
            if isinstance(raw_args, dict):
                args = raw_args
            else:
                try:
                    args = std_json.loads(raw_args or "{}")
                except Exception:
                    args = {}
            result = _execute_tool(tc["function"]["name"], args)
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": std_json.dumps(result, ensure_ascii=False)})
    return "The model did not finish within the tool-call limit."


def _run_chat_with_tools_claude(provider, messages, extra_kwargs, max_steps=20):
    """Run the workspace-tool loop using Anthropic's native tool_use / tool_result format."""
    key = extra_kwargs.get("api_key") or getattr(provider, "_default_key", None)
    if not key:
        return "Claude API key is required to use workspace tools."
    model = extra_kwargs.get("model") or "claude-sonnet-4-5-20250929"
    temperature = extra_kwargs.get("temperature", provider.DEFAULT_TEMPERATURE)
    max_tokens = min(extra_kwargs.get("max_tokens", provider.DEFAULT_MAX_TOKENS), provider.MAX_OUTPUT_TOKENS)
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }

    anthropic_tools = []
    for t in WORKSPACE_TOOLS:
        fn = t.get("function", {})
        anthropic_tools.append({
            "name": fn.get("name"),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })

    system = ""
    claude_msgs = []
    for m in messages:
        if m.get("role") == "system":
            system = (system + "\n\n" + m.get("content", "")).strip()
        elif m.get("role") in ("user", "assistant"):
            claude_msgs.append({"role": m["role"], "content": m.get("content", "")})

    for _ in range(max_steps):
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": claude_msgs,
            "tools": anthropic_tools,
        }
        if system:
            payload["system"] = system
        try:
            resp = requests.post("https://api.anthropic.com/v1/messages",
                                 headers=headers, json=payload, timeout=120)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.HTTPError as e:
            detail = ""
            if e.response is not None:
                try:
                    detail = (e.response.text or "").strip()[:400]
                except Exception:
                    detail = ""
            return f"Claude workspace-tool error: {e}" + (f" {detail}" if detail else "")
        except requests.exceptions.RequestException as e:
            return f"Cannot reach Claude: {e}"

        stop = data.get("stop_reason")
        blocks = data.get("content", [])
        text_parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
        tool_uses = [b for b in blocks if b.get("type") == "tool_use"]

        if stop != "tool_use" or not tool_uses:
            return "".join(text_parts).strip() or "(Claude returned no text)"

        claude_msgs.append({"role": "assistant", "content": blocks})
        tool_results = []
        for tu in tool_uses:
            args = tu.get("input") or {}
            result = _execute_tool(tu.get("name"), args)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.get("id"),
                "content": std_json.dumps(result, ensure_ascii=False),
            })
        claude_msgs.append({"role": "user", "content": tool_results})

    return "The model did not finish within the tool-call limit."


# ── Route helpers ──
def _run_web_search(user_message: str, enabled: bool) -> str:
    """Return up to 3 web-search snippets joined into one context string."""
    if not (enabled and SEARCH_AVAILABLE and user_message.strip()):
        return ""
    try:
        future = _executor.submit(lambda: DDGS().text(user_message, max_results=3))
        results = future.result(timeout=3)
        snippets = [r['body'] for r in results if 'body' in r]
        return " ".join(snippets[:3])
    except Exception as e:
        logger.error("Search error: %s", e)
        return ""


def _build_final_prompt(system_prompt: str, user_message: str, files: List[dict], search_context: str) -> str:
    """Assemble the user-facing prompt: system prompt + search context + message + file text."""
    final_prompt = system_prompt + "\n\n"
    if search_context:
        final_prompt += (
            f"Web search results for '{user_message}':\n{search_context}\n\n"
            f"Based on these results, answer the user's question: {user_message}"
        )
    else:
        final_prompt += user_message
    for f in files:
        final_prompt += "\n\n" + describe_or_extract_file(
            f.get('name', 'file'), f.get('b64', ''), f.get('mime', '')
        )
    return final_prompt


def _build_messages(conv_id: str, system_prompt: str, final_prompt: str, include_system: bool = True) -> List[dict]:
    """Build the full message list for a provider call from stored history."""
    messages = []
    if include_system and system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    for msg in get_messages(conv_id):
        if msg['role'] == 'user':
            messages.append({"role": "user", "content": msg['text']})
        elif msg['role'] == 'bot':
            messages.append({"role": "assistant", "content": msg['text']})
    messages = trim_conversation_history(messages)
    messages.append({"role": "user", "content": final_prompt})
    return messages


def _build_vision_messages(messages, images):
    """Rebuild a message list so the LAST user message carries images as an
    OpenAI multimodal content array (image_url parts + the text).

    Used by the streaming path so attached images / extracted video frames are
    actually sent to the model — otherwise a "what do you see here" video question
    reaches the model as text-only and it returns an empty response.
    """
    if not images:
        return messages
    msgs = [dict(m) for m in messages]
    parts = []
    for img in images:
        b64 = img.get("b64") or ""
        if "," in b64:
            b64 = b64.split(",", 1)[1]
        mime = img.get("mime") or "image/jpeg"
        name = img.get("name") or "image"
        parts.append({"type": "text", "text": f"[image: {name}]"})
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    # Append the user's text as the final text part.
    last_text = ""
    if msgs:
        last_text = msgs[-1].get("content", "")
    if isinstance(last_text, str):
        parts.append({"type": "text", "text": last_text})
    if msgs:
        msgs[-1] = {"role": msgs[-1].get("role", "user"), "content": parts}
    return msgs


def _split_audio_files(files):
    """Split attached files into (audio_files, other_files).

    Audio clips are detected by MIME type (audio/*) or extension; only those are
    fed to the model as actual audio (input_audio). Everything else stays on the
    text path (describe_or_extract_file) and is NOT turned into audio.
    """
    audio, other = [], []
    for f in files or []:
        if video_to_text.is_audio_file(f.get("name", ""), f.get("mime", "")):
            audio.append(f)
        else:
            other.append(f)
    return audio, other


def _model_has_audio(provider_name, model):
    """True if the selected llama.cpp model's folder declares audio input."""
    if provider_name != "llamacpp":
        return False
    try:
        llcpp = providers.get("llamacpp")
        path = llcpp._resolve_model_path(model)
        return "audio" in llamacpp_service.model_capabilities(path)
    except Exception:
        return False


def _check_input_capability(provider_name, model, has_image, has_video, has_audio):
    """Return an error string if the model's folder does not declare support for
    the given input, else None. Only llama.cpp has folder-based capability
    restriction; other providers accept anything they natively support."""
    if provider_name != "llamacpp":
        return None
    try:
        llcpp = providers.get("llamacpp")
        path = llcpp._resolve_model_path(model)
    except Exception:
        return None
    caps = llamacpp_service.model_capabilities(path)
    if has_video and "video" not in caps:
        return ("This model is in a text/image folder and cannot read video. "
                "Move it into video_model/ (video-only) or universal_models_to_text/ (all inputs).")
    if has_audio and "audio" not in caps:
        return ("This model's folder does not declare audio input. "
                "Use a model in universal_models_to_text/ for audio-to-text.")
    if has_image and "image" not in caps:
        return ("This model's folder does not declare image input. "
                "Use a model in models/ or universal_models_to_text/ for image-to-text.")
    return None


def _user_attachments(images, files, videos):
    """Build the stored attachment lists for a user message.

    Original images stay images; files stay files; and each attached VIDEO is kept
    as a single file attachment (with its video mime) so it renders/playable as one
    file instead of being broken into frame thumbnails.
    """
    stored_files = list(files or [])
    for v in videos or []:
        entry = {
            "name": v.get("name", "video.mp4"),
            "b64": v.get("b64", ""),
            "mime": v.get("mime", "video/mp4"),
        }
        # An attachment uploaded by id already lives in the attachments store: record
        # that file so the message still points at it, instead of duplicating a huge
        # recording as base64 (which is exactly what the 25 MB body cap prevents).
        if v.get("id"):
            entry["file"] = os.path.basename(str(v["id"]))
        stored_files.append(entry)
    return images or [], stored_files


def _build_log_filters(conv_filter: str, date_from: str, date_to: str) -> tuple:
    """Return (where_clause, params) shared by the log list and CSV export routes."""
    clauses = []
    params = []
    if conv_filter:
        clauses.append("conversation_id = ?")
        params.append(conv_filter)
    if date_from:
        clauses.append("created_at >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("created_at <= ?")
        params.append(date_to)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


# ── SQLite Logs API ──
@app.route('/api/logs')
def get_logs():
    page = request.args.get('page', 1, type=int)
    per_page = 50
    offset = (page - 1) * per_page
    conv_filter = request.args.get('conv_id', '').strip()
    date_from = request.args.get('from', '')
    date_to = request.args.get('to', '')
    where, params = _build_log_filters(conv_filter, date_from, date_to)
    with _sqlite_lock:
        cur = _sqlite_conn.cursor()
        query = "SELECT id, conversation_id, role, content, created_at FROM messages" + where
        count_query = "SELECT COUNT(*) FROM messages" + where
        count_params = list(params)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([per_page, offset])
        cur.execute(query, params)
        rows = cur.fetchall()
        cur.execute(count_query, count_params)
        total = cur.fetchone()[0]
    logs = [{
        'id': row[0],
        'conversation_id': row[1],
        'role': row[2],
        'content': row[3],
        'created_at': row[4]
    } for row in rows]
    return jsonify({
        'logs': logs,
        'total': total,
        'page': page,
        'per_page': per_page
    })


@app.route('/api/logs/server')
def get_server_logs():
    """Return the tail of the server log file for the in-app Logs viewer."""
    lines = request.args.get('lines', 300, type=int)
    lines = max(1, min(lines, 2000))
    tail = []
    try:
        with open(_SERVER_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail = [l.rstrip("\n") for l in all_lines[-lines:]]
    except Exception:
        pass
    return jsonify({"lines": tail, "path": _SERVER_LOG_PATH})


@app.route('/api/logs/export')
def export_logs_csv():
    conv_filter = request.args.get('conv_id', '').strip()
    date_from = request.args.get('from', '')
    date_to = request.args.get('to', '')
    where, params = _build_log_filters(conv_filter, date_from, date_to)
    with _sqlite_lock:
        cur = _sqlite_conn.cursor()
        query = "SELECT id, conversation_id, role, content, created_at FROM messages" + where + " ORDER BY created_at DESC"
        cur.execute(query, params)
        rows = cur.fetchall()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Conversation ID', 'Role', 'Content', 'Created At'])
    for row in rows:
        writer.writerow(row)
    output.seek(0)
    return Response(output.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=conversation_logs.csv'})

# ── Voice agent logs ──
def _parse_voice_turns(text):
    """Extract only the real USER/ASSISTANT turns from the raw agent log."""
    turns = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[VOICE]"):
            line = line[len("[VOICE]"):].strip()
        elif line.startswith("[LLM]"):
            continue
        if line.startswith("USER: "):
            turns.append({"role": "user", "text": line[len("USER: "):].strip()})
        elif line.startswith("ASSISTANT: "):
            turns.append({"role": "assistant", "text": line[len("ASSISTANT: "):].strip()})
    return turns


@app.route('/api/voice/logs', methods=['GET'])
def voice_logs():
    """Return the parsed voice conversation (clean turns) for the UI."""
    log_dir = root_path("voiceguide_llama.cpp_guide")

    def read_text(name, lines=10000):
        path = os.path.join(log_dir, name)
        if not os.path.exists(path):
            return ""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return "".join(f.readlines()[-lines:])
        except Exception:
            return ""

    conv_text = read_text("conversations.log")
    return jsonify({
        "conversation": _parse_voice_turns(conv_text),
        "exists": os.path.exists(os.path.join(log_dir, "conversations.log")),
    })


@app.route('/api/voice/raw', methods=['GET'])
def voice_raw_logs():
    """Return the raw voice agent log (LLM + VOICE lines) for the Logs viewer."""
    log_dir = root_path("voiceguide_llama.cpp_guide")
    path = os.path.join(log_dir, "voice_agent.log")
    lines = request.args.get('lines', 300, type=int)
    lines = max(1, min(lines, 2000))
    tail = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail = [l.rstrip("\n") for l in all_lines[-lines:]]
    except Exception:
        pass
    return jsonify({"lines": tail, "exists": os.path.exists(path)})


@app.route('/api/voice/command', methods=['POST'])
def voice_command():
    """Handle slash commands typed in the voice-to-voice chat."""
    data = request.get_json(silent=True) or {}
    raw = (data.get('command') or '').strip()
    cmd = raw.lstrip('/').strip().lower()
    log_dir = root_path("voiceguide_llama.cpp_guide")
    os.makedirs(log_dir, exist_ok=True)
    control = os.path.join(log_dir, "control.txt")
    conv = os.path.join(log_dir, "conversations.log")

    if cmd in ('bye', 'stop', 'quit', 'exit'):
        try:
            with open(control, "w", encoding="utf-8") as f:
                f.write("bye\n")
            return jsonify({"ok": True, "message": "🛑 Stop signal sent. The voice agent will shut down within a moment."})
        except Exception as e:
            return jsonify({"ok": False, "message": "Could not send stop signal: {}".format(e)}), 500

    if cmd in ('clear', 'reset'):
        try:
            open(conv, "w", encoding="utf-8").close()
            return jsonify({"ok": True, "message": "🧹 Voice conversation cleared."})
        except Exception as e:
            return jsonify({"ok": False, "message": "Could not clear: {}".format(e)}), 500

    if cmd in ('help', ''):
        return jsonify({"ok": True, "message": "Voice commands:\n/bye  — stop the voice agent\n/clear — clear the conversation log\n/open <url-or-app> — open a website or app (needs full access)\n/help — show this"})

    if cmd.startswith('open '):
        target = raw[len('/open '):].strip()
        if not target:
            return jsonify({"ok": False, "message": "Usage: /open <url or app name>"}), 400
        if _workspace_setting(_current_workspace_id(), "full_access", False) is not True:
            return jsonify({"ok": False, "message": "Full computer access is disabled. Enable it in Workspace settings (⚙️)."})
        if re.match(r"^https?://", target, re.I):
            res = _execute_tool("open_url", {"url": target})
        else:
            res = _execute_tool("open_app", {"app": target})
        if res.get("ok"):
            return jsonify({"ok": True, "message": "✅ Opened " + target})
        return jsonify({"ok": False, "message": "Could not open: " + str(res.get("error", res))})

    return jsonify({"ok": False, "message": "Unknown command '{}'. Try /help.".format(raw)}), 400


# ── A/B model compare ─────────────────────────────────────────
def _compare_single(provider_name, model, api_key, system_prompt, user_message, persona, persona_custom):
    """Run one model and return {text, reasoning, error, duration_sec}."""
    provider = providers.get(provider_name)
    if not provider:
        return {"error": f"Unknown provider: {provider_name}"}
    if api_key and hasattr(provider, '_default_key'):
        provider._default_key = api_key
    sys_prompt = provider.get_system_prompt()
    _persona = personas.chat_block(persona, persona_custom) if provider_name in API_PROVIDERS else None
    if _persona:
        sys_prompt = _persona + "\n\n" + sys_prompt
    messages = [{"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_message}]
    kwargs = {"model": model}
    if api_key:
        kwargs['api_key'] = api_key
    start = time.time()
    try:
        text = provider.generate(messages, **kwargs)
        reasoning = getattr(provider, "last_reasoning", "") or ""
        return {"text": text or "", "reasoning": reasoning,
                "duration_sec": round(time.time() - start, 2)}
    except Exception as e:
        return {"error": str(e), "duration_sec": round(time.time() - start, 2)}


@app.route('/api/compare', methods=['POST'])
@rate_limited(max_per_minute=30)
def compare_models():
    """Run two models side-by-side on the same prompt."""
    data = request.get_json(force=True, silent=True) or {}
    user_message = (data.get('message') or '').strip()
    if not user_message:
        return jsonify({'error': 'Message is required'}), 400
    a = data.get('a') or {}
    b = data.get('b') or {}
    a_provider = a.get('provider', 'ollama')
    b_provider = b.get('provider', 'ollama')
    if a_provider == b_provider and (a.get('model') == b.get('model')):
        return jsonify({'error': 'Choose two different models (or providers) to compare.'}), 400

    persona = data.get('persona') or ''
    persona_custom = data.get('persona_custom') or ''

    from concurrent.futures import ThreadPoolExecutor
    def run(side):
        p, m = (a_provider, a.get('model')) if side == 'A' else (b_provider, b.get('model'))
        key = (a.get('api_key') if side == 'A' else b.get('api_key'))
        key = sanitize_api_key(key)
        return _compare_single(p, m, key, None, user_message, persona, persona_custom)

    with ThreadPoolExecutor(max_workers=2) as ex:
        fa = ex.submit(run, 'A')
        fb = ex.submit(run, 'B')
        res_a = fa.result()
        res_b = fb.result()

    return jsonify({
        'prompt': user_message,
        'a': {'provider': a_provider, 'model': a.get('model'), **res_a},
        'b': {'provider': b_provider, 'model': b.get('model'), **res_b},
    })


@app.route('/api/agent/multi', methods=['POST'])
@rate_limited(max_per_minute=30)
def multi_agent():
    """Run N models in parallel on one task and return a merged summary.

    Body: {message, agents: [{provider, model, api_key, role}, ...]}
    Each agent answers independently (optionally with a role/persona prefix).
    """
    data = request.get_json(force=True, silent=True) or {}
    user_message = (data.get('message') or '').strip()
    agents = data.get('agents') or []
    if not user_message:
        return jsonify({'error': 'Message is required'}), 400
    if not isinstance(agents, list) or len(agents) < 2:
        return jsonify({'error': 'Provide at least 2 agents.'}), 400
    agents = agents[:6]  # cap concurrency

    from concurrent.futures import ThreadPoolExecutor

    def run(agent):
        provider_name = agent.get('provider', 'ollama')
        model = agent.get('model')
        api_key = sanitize_api_key(agent.get('api_key'))
        role = (agent.get('role') or '').strip()
        prompt = user_message
        if role:
            prompt = f"[You are acting as: {role}]\n\n{user_message}"
        provider = providers.get(provider_name)
        if not provider:
            return {"provider": provider_name, "model": model, "role": role,
                    "error": f"Unknown provider: {provider_name}"}
        if api_key and hasattr(provider, '_default_key'):
            provider._default_key = api_key
        kwargs = {"model": model}
        if api_key:
            kwargs['api_key'] = api_key
        sys_prompt = provider.get_system_prompt()
        messages = [{"role": "system", "content": sys_prompt},
                    {"role": "user", "content": prompt}]
        start = time.time()
        try:
            text = provider.generate(messages, **kwargs)
            return {"provider": provider_name, "model": model, "role": role,
                    "text": text or "", "reasoning": getattr(provider, "last_reasoning", "") or "",
                    "duration_sec": round(time.time() - start, 2)}
        except Exception as e:
            return {"provider": provider_name, "model": model, "role": role,
                    "error": str(e), "duration_sec": round(time.time() - start, 2)}

    with ThreadPoolExecutor(max_workers=len(agents)) as ex:
        results = list(ex.map(run, agents))

    return jsonify({'prompt': user_message, 'agents': results})


# ── Workspace / API-key routes ──
@app.route('/api/workspaces', methods=['GET'])
def list_workspaces():
    return jsonify({"workspaces": _list_workspaces(), "current": _current_workspace_id()})


@app.route('/api/workspaces', methods=['POST'])
def create_workspace():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or 'New Workspace').strip()
    wid = re.sub(r'[^a-z0-9_-]+', '-', name.lower()).strip('-') or 'workspace'
    wdir = os.path.join(WORKSPACES_DIR, wid)
    if os.path.exists(wdir):
        return jsonify({"error": "Workspace '{}' already exists".format(name)}), 400
    os.makedirs(wdir, exist_ok=True)
    with open(os.path.join(wdir, "config.json"), "w", encoding="utf-8") as f:
        std_json.dump({"id": wid, "name": name, "folder": "", "folder_mode": "read",
                       "thinking": "high", "dependencies": []}, f, indent=2)
    return jsonify({"ok": True, "id": wid})


@app.route('/api/workspaces/<wid>/select', methods=['POST'])
def select_workspace(wid):
    _ensure_workspace_default()
    if not os.path.isdir(os.path.join(WORKSPACES_DIR, wid)):
        return jsonify({"error": "Workspace not found"}), 404
    with open(CURRENT_WORKSPACE_FILE, "w", encoding="utf-8") as f:
        f.write(wid)
    return jsonify({"ok": True, "current": wid})


@app.route('/api/workspaces/<wid>', methods=['PUT'])
def update_workspace(wid):
    _ensure_workspace_default()
    wdir = os.path.join(WORKSPACES_DIR, wid)
    if not os.path.isdir(wdir):
        return jsonify({"error": "Workspace not found"}), 404
    data = request.get_json(silent=True) or {}
    cfg = _get_workspace(wid) or {"id": wid, "name": wid, "folder": "", "folder_mode": "read",
                                  "thinking": "high", "dependencies": []}
    if "name" in data and data["name"]:
        cfg["name"] = data["name"]
    if "thinking" in data and data["thinking"] in ("low", "mid", "high"):
        cfg["thinking"] = data["thinking"]
    if "folder" in data:
        cfg["folder"] = data["folder"]
    if "folder_mode" in data and data["folder_mode"] in ("read", "readwrite"):
        cfg["folder_mode"] = data["folder_mode"]
    if "dependencies" in data and isinstance(data["dependencies"], list):
        cfg["dependencies"] = [str(d) for d in data["dependencies"]]
    if "full_access" in data:
        cfg["full_access"] = bool(data["full_access"])
    with open(os.path.join(wdir, "config.json"), "w", encoding="utf-8") as f:
        std_json.dump(cfg, f, indent=2, ensure_ascii=False)
    return jsonify({"ok": True})


# ── Workspace folder access ──
def _resolve_workspace_file(wid, rel_path):
    """Resolve a path inside the workspace folder, blocking traversal."""
    base = _workspace_setting(wid, "folder", "") or ""
    if not base:
        return None, "No folder configured for this workspace."
    base = os.path.abspath(base)
    if not os.path.isdir(base):
        return None, "Configured folder does not exist: {}".format(base)
    target = os.path.abspath(os.path.join(base, rel_path or ""))
    if target != base and not target.startswith(base + os.sep):
        return None, "Access denied: path is outside the configured folder."
    return target, None


@app.route('/api/workspace/files', methods=['GET'])
def workspace_files():
    wid = _current_workspace_id()
    base = _workspace_setting(wid, "folder", "") or ""
    if not base:
        return jsonify({"error": "No folder configured for this workspace."}), 400
    base = os.path.abspath(base)
    if not os.path.isdir(base):
        return jsonify({"error": "Configured folder does not exist: {}".format(base)}), 400
    items = []
    for name in sorted(os.listdir(base)):
        p = os.path.join(base, name)
        items.append({"name": name, "is_dir": os.path.isdir(p),
                      "size": os.path.getsize(p) if os.path.isfile(p) else 0})
    return jsonify({"folder": base, "mode": _workspace_setting(wid, "folder_mode", "read"), "files": items})


# ── Plugins ───────────────────────────────────────────
@app.route('/api/plugins', methods=['GET'])
def api_plugins():
    """List loaded plugins (for the services/plugins panel)."""
    return jsonify(plugin_loader.list_loaded())


# ── First-run setup checker ───────────────────────────
@app.route('/api/setup/check', methods=['GET'])
def setup_check_status():
    """Return which local services/files are present vs. missing (with links)."""
    return jsonify(setup_check.summary())


# ── Live coding (agent edits) ─────────────────────────
@app.route('/api/agent/edits', methods=['GET'])
def agent_edits():
    """Return the recent agent file edits (for the live coding panel)."""
    since = request.args.get('since', type=float, default=0)
    limit = request.args.get('limit', type=int, default=200)
    items = edits_store.list_since(since, limit=min(max(limit, 1), 500))
    return jsonify({"edits": items, "count": len(items)})


@app.route('/api/agent/edits/clear', methods=['POST'])
def agent_edits_clear():
    edits_store.clear()
    with AGENT_EDITS_LOCK:
        AGENT_EDITS.clear()
    return jsonify({"ok": True})


@app.route('/api/livecode/capture', methods=['POST'])
def livecode_capture():
    """Frontend-side capture of code blocks the model printed. The backend
    already tries to auto-capture on /chat and /chat_stream, but this lets the
    UI report code blocks from ANY reply path so nothing is missed."""
    data = request.get_json(silent=True) or {}
    text = data.get('text') or ''
    n = _record_code_blocks(text)
    return jsonify({"ok": True, "captured": n})


# ── RAG document chat ───────────────────────────────────────────
@app.route('/api/rag/documents', methods=['GET'])
def rag_documents():
    """List indexed documents (for the RAG panel)."""
    return jsonify(rag.list_documents())


@app.route('/api/rag/index', methods=['POST'])
def rag_index():
    """Index one uploaded document (base64)."""
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or 'document').strip()
    b64 = data.get('b64') or ''
    if not name or not b64:
        return jsonify({'error': 'name and b64 are required'}), 400
    try:
        if ',' in b64:
            b64 = b64.split(',', 1)[1]
        raw = base64.b64decode(b64)
    except Exception as e:
        return jsonify({'error': 'Invalid base64: {}'.format(e)}), 400
    try:
        n = rag.index_document(name, raw)
        return jsonify({'ok': True, 'name': name, 'chunks': n})
    except ValueError as ve:
        return jsonify({'error': str(ve), 'chunks': 0}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/rag/delete', methods=['POST'])
def rag_delete():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name is required'}), 400
    rag.delete_document(name)
    return jsonify({'ok': True})


# ── Video-to-text (extract frames for a vision model) ───────────
@app.route('/api/video/frames', methods=['POST'])
def video_frames():
    """Extract sample frames from {b64} video (via ffmpeg, auto-detected)."""
    data = request.get_json(silent=True) or {}
    b64 = data.get('b64') or ''
    if not b64:
        return jsonify({'error': 'b64 is required'}), 400
    frames = video_to_text.extract_frames(b64)
    if not frames:
        return jsonify({'error': 'ffmpeg not available or video could not be decoded', 'frames': []}), 400
    return jsonify({'ok': True, 'frames': frames})


@app.route('/api/workspace/read', methods=['POST'])
def workspace_read_file():
    data = request.get_json(silent=True) or {}
    wid = _current_workspace_id()
    target, err = _resolve_workspace_file(wid, data.get("path", ""))
    if err:
        return jsonify({"error": err}), 400
    if not os.path.isfile(target):
        return jsonify({"error": "File not found: {}".format(data.get("path"))}), 404
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            return jsonify({"ok": True, "content": f.read()[:20000]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/workspace/write', methods=['POST'])
def workspace_write_file():
    data = request.get_json(silent=True) or {}
    wid = _current_workspace_id()
    mode = _workspace_setting(wid, "folder_mode", "read")
    if mode != "readwrite":
        return jsonify({"error": "This workspace folder is read-only."}), 403
    target, err = _resolve_workspace_file(wid, data.get("path", ""))
    if err:
        return jsonify({"error": err}), 400
    try:
        with open(target, "w", encoding="utf-8") as f:
            f.write(data.get("content", ""))
        _record_edit("write_file", data.get("path", ""), "", data.get("content", ""))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Chat endpoints ──
@app.route('/chat', methods=['POST'])
@rate_limited(max_per_minute=20)
def chat():
    global current_model
    try:
        data = request.get_json(force=True, silent=True) or {}
        user_message = data.get('message', '').strip()
        # Attachments above 8 MB arrive uploaded-by-id with no base64 (the request
        # body is capped at 25 MB); images and non-audio files need their bytes back.
        images = _rehydrate_uploads(data.get('images', []))
        files = _rehydrate_uploads(data.get('files', []))
        videos = data.get('videos', [])
        conv_id = data.get('conversation_id')
        search_enabled = data.get('search', False)
        rag_enabled = data.get('rag', False)
        provider_name = data.get('provider', 'ollama')
        # Only llama.cpp gets native audio input; every other provider still
        # receives the audio file as a described text attachment (not dropped).
        if provider_name == 'llamacpp':
            audio_files, non_audio_files = _split_audio_files(files)
        else:
            audio_files, non_audio_files = [], list(files)
        model = data.get('model', None)
        api_key = sanitize_api_key(data.get('api_key', None))
        persona = data.get('persona') or ''
        persona_custom = data.get('persona_custom') or ''

        if not user_message and not images and not files and not videos:
            return jsonify({'error': 'Nothing to send'}), 400

        # Folder-based capability restriction for llama.cpp: reject an input the
        # selected model's folder doesn't declare (e.g. video to a text/image model).
        cap_err = _check_input_capability(
            provider_name, model,
            has_image=bool(images), has_video=bool(videos), has_audio=bool(audio_files),
        )
        if cap_err:
            return jsonify({'error': cap_err}), 400

        if not conv_id:
            conv_id = create_conversation()
        else:
            conv = get_conversation(conv_id)
            if conv is None:
                return jsonify({'error': 'Conversation not found'}), 404

        if is_ollama_command(user_message):
            output = execute_ollama_command_sync(user_message)
            add_message(conv_id, "user", user_message, [], [])
            add_message(conv_id, "bot", output, [], [])
            return jsonify({'response': output})

        search_context = _run_web_search(user_message, search_enabled)
        rag_context = rag.build_context(user_message) if rag_enabled else ""

        provider = providers.get(provider_name)
        if not provider:
            return jsonify({'error': f'Unknown provider: {provider_name}'}), 400

        # Track the user-entered API key on the provider instance so status checks
        # and subsequent requests reflect it (not just this one request).
        if api_key and hasattr(provider, '_default_key'):
            provider._default_key = api_key

        system_prompt = provider.get_system_prompt()
        _persona = personas.chat_block(persona, persona_custom) if provider_name in API_PROVIDERS else None
        if _persona:
            system_prompt = _persona + "\n\n" + system_prompt
        final_prompt = _build_final_prompt(system_prompt, user_message, non_audio_files, search_context)
        if rag_context:
            final_prompt += (
                "\n\n[The user's uploaded documents are provided below]\n"
                + rag_context
                + "\n\nThese are documents the user uploaded (RAG). Read them and use them to answer. "
                  "When the user says 'read it', 'read my rag', 'the material', 'what's in the documents' "
                  "or asks about the material, READ the content above and answer from it directly — do not "
                  "search for the question's exact words. Give the answer from the provided content, and "
                  "do not ask for confirmation or what the user wants."
            )
        # llama.cpp GGUF chat templates often require strictly alternating
        # user/assistant roles, so drop the separate "system" role there (the
        # system prompt is already folded into final_prompt by _build_final_prompt).
        messages = _build_messages(conv_id, system_prompt, final_prompt,
                                   include_system=(provider_name != 'llamacpp'))

        extra_kwargs = {"model": model}
        if api_key:
            extra_kwargs['api_key'] = api_key
        thinking = _workspace_setting(_current_workspace_id(), "thinking", "high")
        if thinking:
            extra_kwargs['thinking'] = thinking

        if provider_name == 'ollama':
            mem_settings = get_ollama_memory_settings()
            extra_kwargs['num_gpu'] = mem_settings['num_gpu']
            extra_kwargs['low_vram'] = mem_settings['low_vram']

        use_tools = (not images and not videos and not audio_files) and provider_name in ("deepseek", "groq", "ollama", "llamacpp", "claude", "openrouter") \
            and bool(_workspace_setting(_current_workspace_id(), "folder", ""))

        # llama.cpp is auto-started (and kept running) whenever it's the provider,
        # so the user never has to launch it manually.
        if provider_name == 'llamacpp':
            st = llamacpp_service.start(model=model)
            if st.get('error'):
                return jsonify({'error': 'llama.cpp failed to start: ' + st['error']}), 500

        start_time = time.time()
        # Extract video frames for the MODEL request only. The original video stays
        # a single playable file for storage/display (vision_images vs images/videos).
        vision_images = list(images)
        # Transcription sources for llama.cpp: explicit audio clips plus the audio
        # track of any attached video (video-to-text reads the soundtrack). Only
        # llama.cpp universal models (gemma-4 E2B/E4B/12B) declare audio input.
        transcription_sources = []
        if provider_name == 'llamacpp':
            # _with_source_path() resolves an attachment uploaded by id to a local
            # file, so a big recording is read from disk instead of base64 JSON.
            transcription_sources = [_with_source_path(a) for a in audio_files]
            if videos and _model_has_audio(provider_name, model):
                transcription_sources.extend(_with_source_path(v) for v in videos)
        reply = None
        if transcription_sources:
            try:
                reply = provider.generate_with_audio(messages, transcription_sources, **extra_kwargs)
            except Exception as e:
                # A silent clip, a video with no audio track, or a model without audio
                # input all land here. That must not discard the answer when there is
                # still something to look at - fall through to the frames path below.
                if not (images or videos):
                    raise
                logger.info("Audio transcription produced nothing (%s); "
                            "answering from the attached frames instead.", e)
        if reply is None and (images or videos):
            frame_images = []
            if videos:
                for v in videos:
                    frame_images.extend(video_to_text.extract_frames(
                        v.get("b64", ""), path=_attachment_path(v)))
            if frame_images:
                vision_images = images + frame_images
            if cached_vision_check(provider_name, model):
                reply = provider.generate_multimodal(messages, vision_images, videos, **extra_kwargs)
            elif vision_images:
                future = _executor.submit(describe_image_with_llava, vision_images[0]["b64"])
                description = future.result(timeout=60)
                if description:
                    inject = f"[Image description]\n{description.strip()}\n\n[User question]\n"
                else:
                    inject = "[Image description unavailable]\n\n[User question]\n"
                messages[-1]['content'] = inject + messages[-1]['content']
                reply = provider.generate(messages, **extra_kwargs)
            else:
                inject = "[Video attached but this provider/model does not support video input]\n\n[User question]\n"
                messages[-1]['content'] = inject + messages[-1]['content']
                reply = provider.generate(messages, **extra_kwargs)
        elif reply is None and use_tools:
            reply = _run_chat_with_tools(provider, messages, extra_kwargs)
        elif reply is None:
            reply = provider.generate(messages, **extra_kwargs)
        end_time = time.time()

        token_estimate = len(reply.split()) / 0.75
        duration = end_time - start_time if end_time > start_time else 1
        usage = {"tokens": int(token_estimate), "duration_sec": round(duration, 2)}

        # Record token/cost estimate for the stats panel.
        record_usage(provider_name, model, conv_id, _estimate_tokens(final_prompt), usage["tokens"])

        original_message = data.get('message', '').strip()

        store_images, store_files = _user_attachments(images, files, videos)
        if not add_message(conv_id, "user", original_message, store_images, store_files):
            return jsonify({'error': f'Failed to save user message to {conv_id}'}), 500
        reasoning = getattr(provider, "last_reasoning", "") or ""
        if not add_message(conv_id, "bot", reply, [], [], meta=_bot_meta(provider_name, model, reasoning)):
            return jsonify({'error': f'Failed to save bot message to {conv_id}'}), 500
        _record_code_blocks(reply)
        _auto_title(conv_id, original_message, reply, provider_name, model, api_key)

        return jsonify({'response': reply, 'usage': usage, 'reasoning': reasoning})

    except requests.exceptions.ConnectionError:
        return jsonify({'error': 'Cannot connect to Ollama. Make sure it is running.'}), 503
    except requests.exceptions.Timeout:
        return jsonify({'error': 'Request timed out. Try a shorter message.'}), 504
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            return jsonify({'error': f'Model "{model}" not found in Ollama. Please pull it first.'}), 404
        raise
    except Exception as e:
        logger.error("Error: %s", e)
        return jsonify({'error': str(e)}), 500

def _openai_stream_target(provider_name, api_key):
    """Return (url, headers) for an OpenAI-compatible chat.completions stream, or None."""
    if provider_name == "llamacpp":
        lp = providers.get("llamacpp")
        url = (getattr(lp, "server_url", "http://127.0.0.1:8080/v1")).rstrip("/") + "/chat/completions"
        return url, {"Content-Type": "application/json"}
    if provider_name == "deepseek":
        return "https://api.deepseek.com/v1/chat/completions", {
            "Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    if provider_name == "groq":
        return "https://api.groq.com/openai/v1/chat/completions", {
            "Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    if provider_name == "openrouter":
        return "https://openrouter.ai/api/v1/chat/completions", {
            "Content-Type": "application/json", "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://trioforge.local", "X-Title": "TrioForge"}
    if provider_name == "huggingface":
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return "https://router.huggingface.co/v1/chat/completions", headers
    return None


def _iter_openai_stream(url, headers, payload):
    """Yield {'reasoning': …} / {'token': …} dicts from an OpenAI-compatible SSE stream."""
    resp = requests.post(url, headers=headers, json=payload, stream=True, timeout=300)
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        body = ""
        try:
            body = (e.response.text or "").strip()[:600]
        except Exception:
            body = ""
        raise ProviderError("HTTP {} — {}".format(e.response.status_code, body or e)) from e
    for raw in resp.iter_lines():
        if not raw:
            continue
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if data_str == "[DONE]":
            break
        try:
            obj = std_json.loads(data_str)
        except Exception:
            continue
        delta = (obj.get("choices") or [{}])[0].get("delta") or {}
        reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
        content = delta.get("content") or ""
        if reasoning:
            yield {"reasoning": reasoning}
        if content:
            yield {"token": content}


@app.route('/chat_stream', methods=['POST'])
@rate_limited(max_per_minute=20)
def chat_stream():
    try:
        data = request.get_json(force=True, silent=True) or {}
        user_message = data.get('message', '').strip()
        # Attachments above 8 MB arrive uploaded-by-id with no base64 (the request
        # body is capped at 25 MB); images and non-audio files need their bytes back.
        images = _rehydrate_uploads(data.get('images', []))
        files = _rehydrate_uploads(data.get('files', []))
        videos = data.get('videos', [])
        conv_id = data.get('conversation_id')
        search_enabled = data.get('search', False)
        rag_enabled = data.get('rag', False)
        model = data.get('model', current_model)
        api_key = sanitize_api_key(data.get('api_key', None))
        persona = data.get('persona') or ''
        persona_custom = data.get('persona_custom') or ''

        provider_name = data.get('provider', 'ollama')
        # Only llama.cpp gets native audio input; other providers get the audio
        # described as a text attachment (not silently dropped).
        if provider_name == 'llamacpp':
            audio_files, non_audio_files = _split_audio_files(files)
        else:
            audio_files, non_audio_files = [], list(files)

        if not user_message and not images and not files and not videos:
            return jsonify({'error': 'Nothing to send'}), 400

        # Folder-based capability restriction for llama.cpp (same as /chat).
        cap_err = _check_input_capability(
            provider_name, model,
            has_image=bool(images), has_video=bool(videos), has_audio=bool(audio_files),
        )
        if cap_err:
            return jsonify({'error': cap_err}), 400

        if not conv_id:
            conv_id = create_conversation()
        else:
            conv = get_conversation(conv_id)
            if conv is None:
                return jsonify({'error': 'Conversation not found'}), 404

        # Video → frames: sample the clip into images so a vision model can
        # "see" it (ffmpeg auto-detected). The frames are used ONLY for the model
        # request (vision_images) — the ORIGINAL video stays a single playable
        # file and is what gets stored/displayed, so the chat doesn't show a stack
        # of broken-out frame thumbnails.
        vision_images = list(images)
        if videos:
            frame_images = []
            for v in videos:
                frame_images.extend(video_to_text.extract_frames(
                        v.get("b64", ""), path=_attachment_path(v)))
            if frame_images:
                vision_images = images + frame_images
                videos = list(videos)  # keep original for storage (no longer frames-only)

        if is_ollama_command(user_message) and provider_name == 'ollama':
            return Response(
                handle_ollama_command_stream(conv_id, user_message, vision_images, files),
                mimetype='text/event-stream'
            )

        search_context = _run_web_search(user_message, search_enabled)
        rag_context = rag.build_context(user_message) if rag_enabled else ""

        provider = providers.get(provider_name)
        if not provider:
            return jsonify({'error': f'Unknown provider: {provider_name}'}), 400
        if api_key and hasattr(provider, '_default_key'):
            provider._default_key = api_key

        system_prompt = provider.get_system_prompt()
        _persona = personas.chat_block(persona, persona_custom) if provider_name in API_PROVIDERS else None
        if _persona:
            system_prompt = _persona + "\n\n" + system_prompt
        final_prompt = _build_final_prompt(system_prompt, user_message, non_audio_files, search_context)
        if rag_context:
            final_prompt += (
                "\n\n[The user's uploaded documents are provided below]\n"
                + rag_context
                + "\n\nThese are documents the user uploaded (RAG). Read them and use them to answer. "
                  "When the user says 'read it', 'read my rag', 'the material', 'what's in the documents' "
                  "or asks about the material, READ the content above and answer from it directly — do not "
                  "search for the question's exact words. Give the answer from the provided content, and "
                  "do not ask for confirmation or what the user wants."
            )
        include_system = (provider_name != 'llamacpp')
        messages = _build_messages(conv_id, system_prompt, final_prompt, include_system=include_system)

        mem_settings = get_ollama_memory_settings()
        # Transcription sources (llama.cpp): explicit audio clips + the audio track
        # of any attached video (video-to-text reads the soundtrack).
        transcription_sources = []
        if provider_name == "llamacpp":
            # _with_source_path() resolves an attachment uploaded by id to a local
            # file, so a big recording is read from disk instead of base64 JSON.
            transcription_sources = [_with_source_path(a) for a in audio_files]
            if videos and _model_has_audio(provider_name, model):
                transcription_sources.extend(_with_source_path(v) for v in videos)
        use_tools = (not vision_images and not audio_files and not transcription_sources) and provider_name in ("deepseek", "groq", "ollama", "llamacpp", "claude", "openrouter") \
            and bool(_workspace_setting(_current_workspace_id(), "folder", ""))

        # llama.cpp is auto-started (and kept running) whenever it's the provider,
        # so the user never has to launch it manually. This MUST happen in the
        # streaming path too — without it the model list shows, but every send
        # fails with a connection refused.
        if provider_name == 'llamacpp':
            st = llamacpp_service.start(model=model)
            if st.get('error') and not st.get('running'):
                yield_error = f"data: {json_dumps({'error': 'llama.cpp failed to start: ' + st['error']})}\n\n"
                return Response(yield_error, mimetype='text/event-stream')

        # Workspace tools are resolved non-streaming first, then we stream the
        # final answer so the client keeps its normal token-by-token UI.
        tool_final_text = None
        tool_reasoning = ""
        if use_tools:
            extra_kwargs = {"model": model or current_model}
            if api_key:
                extra_kwargs['api_key'] = api_key
            try:
                tool_final_text = _run_chat_with_tools(provider, messages, extra_kwargs)
                # The tool loop calls provider.generate_raw(...), which populates
                # provider.last_reasoning with the model's chain-of-thought. Capture
                # it here so the thinking block is shown even on the tools path.
                tool_reasoning = getattr(provider, "last_reasoning", "") or ""
            except Exception as e:
                tool_final_text = f"[tool error] {e}"

        # Audio (llama.cpp universal models): full-length transcription runs
        # non-streaming (chunked into 30 s segments, then stitched), and the result
        # is streamed back as one message — same pattern as the tools path above.
        # Sources are explicit audio clips plus the audio track of attached videos.
        audio_final_text = None
        audio_reasoning = ""
        if transcription_sources:
            extra_kwargs_audio = {"model": model or current_model}
            if api_key:
                extra_kwargs_audio['api_key'] = api_key
            try:
                audio_final_text = provider.generate_with_audio(messages, transcription_sources, **extra_kwargs_audio)
                audio_reasoning = getattr(provider, "last_reasoning", "") or ""
            except Exception as e:
                # Audio/video transcription can legitimately produce nothing (silent
                # clip, video with no audio track, model without audio input). When we
                # still have frames to look at, answer from those instead of dumping
                # an error in place of the answer.
                if vision_images:
                    logger.info("Audio transcription produced nothing (%s); "
                                "answering from the attached frames instead.", e)
                    audio_final_text = None
                else:
                    audio_final_text = f"[audio error] {e}"

        def generate():
            full_response = ""
            thinking_acc = ""
            if tool_final_text is not None:
                if tool_reasoning:
                    yield f"data: {json_dumps({'reasoning': tool_reasoning})}\n\n"
                yield f"data: {json_dumps({'token': tool_final_text})}\n\n"
                yield f"data: {json_dumps({'done': True, 'full_response': tool_final_text, 'usage': {}, 'reasoning': tool_reasoning})}\n\n"
                store_images, store_files = _user_attachments(images, files, videos)
                add_message(conv_id, "user", user_message, store_images, store_files)
                add_message(conv_id, "bot", tool_final_text, [], [], meta=_bot_meta(provider_name, model, tool_reasoning))
                record_usage(provider_name, model, conv_id, _estimate_tokens(user_message), _estimate_tokens(tool_final_text))
                return

            if audio_final_text is not None:
                if audio_reasoning:
                    yield f"data: {json_dumps({'reasoning': audio_reasoning})}\n\n"
                yield f"data: {json_dumps({'token': audio_final_text})}\n\n"
                yield f"data: {json_dumps({'done': True, 'full_response': audio_final_text, 'usage': {}, 'reasoning': audio_reasoning})}\n\n"
                store_images, store_files = _user_attachments(images, files, videos)
                add_message(conv_id, "user", user_message, store_images, store_files)
                add_message(conv_id, "bot", audio_final_text, [], [], meta=_bot_meta(provider_name, model, audio_reasoning))
                record_usage(provider_name, model, conv_id, _estimate_tokens(user_message), _estimate_tokens(audio_final_text))
                return

            # ── Ollama: native NDJSON streaming with live `thinking` ──
            if provider_name == "ollama":
                payload = {
                    "model": model or current_model,
                    "messages": messages,
                    "stream": True,
                    "options": {
                        "temperature": 0.7, "num_predict": 16384, "num_ctx": 16384,
                        "num_gpu": mem_settings['num_gpu'],
                    }
                }
                if vision_images:
                    b64_list = [i["b64"].split(",", 1)[1] if "," in i["b64"] else i["b64"] for i in vision_images]
                    payload["messages"][-1] = {"role": "user", "content": messages[-1]["content"], "images": b64_list}
                try:
                    r = requests.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload, stream=True, timeout=300)
                    r.raise_for_status()
                    for line in r.iter_lines():
                        if not line:
                            continue
                        chunk = json_loads(line)
                        msg = chunk.get("message") or {}
                        if msg.get("thinking"):
                            thinking_acc += msg["thinking"]
                            yield f"data: {json_dumps({'reasoning': msg['thinking']})}\n\n"
                        if msg.get("content"):
                            full_response += msg["content"]
                            yield f"data: {json_dumps({'token': msg['content']})}\n\n"
                        if chunk.get("done", False):
                            usage = {}
                            if "eval_count" in chunk and "eval_duration" in chunk:
                                usage = {"tokens": chunk.get("eval_count", 0),
                                         "duration_sec": chunk.get("eval_duration", 0) / 1e9}
                            yield f"data: {json_dumps({'done': True, 'full_response': full_response, 'usage': usage, 'reasoning': thinking_acc})}\n\n"
                            break
                except Exception as e:
                    yield f"data: {json_dumps({'error': str(e)})}\n\n"

            # ── OpenAI-compatible providers: live reasoning_content deltas ──
            else:
                target = _openai_stream_target(provider_name, api_key)
                if target is None:
                    yield f"data: {json_dumps({'error': 'Streaming not supported for ' + provider_name})}\n\n"
                else:
                    url, headers = target
                    # llama.cpp registers the model by its FULL path, not the bare
                    # filename the dropdown sends. Resolve it the same way the
                    # non-streaming path does, or llama-server returns 500.
                    stream_model = model
                    if provider_name == "llamacpp":
                        try:
                            stream_model = providers["llamacpp"]._resolve_model_path(model)
                        except Exception:
                            stream_model = model
                    payload = {"model": stream_model, "messages": messages, "stream": True, "temperature": 0.7}
                    # Give the model room to finish: a reasoning-heavy model (e.g.
                    # VideoGuard / Qwen3.5) spends a lot of its budget on
                    # reasoning_content, and if max_tokens is too small it runs out
                    # before producing any content → an "(empty response)" to the user.
                    try:
                        prov = providers.get(provider_name)
                        max_out = getattr(prov, "DEFAULT_MAX_TOKENS", 4096)
                    except Exception:
                        max_out = 4096
                    payload["max_tokens"] = int(max_out)
                    # Vision: when images (or extracted video frames) are attached,
                    # rebuild the last user message as an OpenAI multimodal content
                    # array so the model actually "sees" them. Without this a video
                    # question reaches the model as text-only and it returns empty.
                    if vision_images:
                        payload["messages"] = _build_vision_messages(messages, vision_images)
                    try:
                        got_content = False
                        for ev in _iter_openai_stream(url, headers, payload):
                            if "reasoning" in ev:
                                thinking_acc += ev["reasoning"]
                                yield f"data: {json_dumps({'reasoning': ev['reasoning']})}\n\n"
                            if "token" in ev:
                                got_content = True
                                full_response += ev["token"]
                                yield f"data: {json_dumps({'token': ev['token']})}\n\n"
                        # Some reasoning models (e.g. VideoGuard) put the whole answer
                        # in reasoning_content and leave content empty. If we got no
                        # content tokens, surface the reasoning as the answer so the
                        # chat isn't blank.
                        if not got_content and thinking_acc and not full_response:
                            full_response = thinking_acc
                            yield f"data: {json_dumps({'token': full_response})}\n\n"
                        yield f"data: {json_dumps({'done': True, 'full_response': full_response, 'usage': {'tokens': _estimate_tokens(full_response), 'duration_sec': 0}, 'reasoning': thinking_acc})}\n\n"
                    except Exception as e:
                        detail = str(e)
                        if provider_name == "llamacpp":
                            detail += " — see logs/llamacpp.log for the server's full output."
                        yield f"data: {json_dumps({'error': detail})}\n\n"

            # If the model produced no content (e.g. it put everything in its
            # reasoning chain), fall back to the reasoning so the user gets an
            # answer instead of a bare "(empty response)".
            if not full_response and thinking_acc:
                full_response = thinking_acc
            store_images, store_files = _user_attachments(images, files, videos)
            add_message(conv_id, "user", user_message, store_images, store_files)
            add_message(conv_id, "bot", full_response or "(empty response)", [], [],
                        meta=_bot_meta(provider_name, model, thinking_acc))
            _record_code_blocks(full_response)
            record_usage(provider_name, model, conv_id, _estimate_tokens(user_message), _estimate_tokens(full_response))
            _auto_title(conv_id, user_message, full_response, provider_name, model, api_key)

        return Response(generate(), mimetype='text/event-stream')

    except requests.exceptions.ConnectionError:
        return jsonify({'error': 'Cannot connect to Ollama. Make sure it is running.'}), 503
    except requests.exceptions.Timeout:
        return jsonify({'error': 'Request timed out. Try a shorter message.'}), 504
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            return jsonify({'error': f'Model "{model}" not found. Please check the model name.'}), 404
        raise
    except Exception as e:
        logger.error("chat_stream error: %s", e)
        return jsonify({'error': str(e)}), 500

# ─── UNCENSORED VISION MODELS ──────────────────
UNCENSORED_VISION_MODELS = [
    "mikemikeok/Qwythos-9B-Uncensored",
    "baytout3/ultragemma4-12b-heretic-uncensored",
    "maxwellb/gemma4-12b-it-oym",
    "tinyrick/gemma-4-31B-it-uncensored-heretic-vision-llmfan46",
    "tinyrick/Qwen3.6-35B-A3B-uncensored-heretic-vision-llmfan46",
    "dzgg/Qwen3.5-Uncensored-HauhauCS-Aggressive",
    "krishairnd/Gemma-4-Uncensored",
    "trinhnv1205/Qwen3.5-9B-Uncensored-ctx64k",
    "dzgg/Gemma-4-Uncensored-HauhauCS-Aggressive",
    "frob/davidau-qwen3.6-uncensored",
    "tinyrick/Gemma-4-Harmonia-31B-uncensored-heretic-vision-llmfan46",
    "fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive",
    "fredrezones55/Qwen3.6-27B-Uncensored-HauhauCS-Balanced",
    "fredrezones55/Gemma-4-Uncensored-HauhauCS-Aggressive",
    "fredrezones55/Qwen3.5-Uncensored-HauhauCS-Aggressive",
    "vaultbox/qwen3.5-uncensored",
    "baytout3/qwen3.5-uncensored",
    "joe-speedboat/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive",
    "joe-speedboat/Gemma-4-Uncensored-HauhauCS-Aggressive",
    "Agen/gemma-4-26B-A4B-it-uncensored-heretic",
    "nexusriot/Gemma-4-Uncensored-HauhauCS-Aggressive",
    "baytout3/Qwen3.6-27B-Uncensored-HauhauCS-Balanced",
    "nexusriot/Qwen3.5-Uncensored-HauhauCS-Aggressive",
    "baytout3/gemma-4-26B-A4B-it-uncensored-heretic",
    "baytout3/Qwen3.5-Uncensored-HauhauCS-Aggressive",
    "baytout3/Gemma-4-Uncensored-HauhauCS-Aggressive",
    "studiobrn/uncensoredmodAI",
    "ramitmitra/qwen3.5-uncensored-9b-baburao",
    "kaelri/qwen3.5-mt",
    "GX-Telecom/Qwen3.6-35B-APEX-Uncensored",
    "aeline/Omega",
    "mdq100/Gemma3-Instruct-Abliterated",
    "redule26/huihui_ai_qwen2.5-vl-7b-abliterated",
    "valkyriesys/eudaimonia-dryad3-vision",
    "jayeshpandit2480/gemma3-UNCENSORED",
    "austinlaw076/gemma-4-31B-it-Mystery-Fine-Tune-HERETIC-UNCENSORED-Thinking-Instruct-GGUF-Q6_K",
    "rafw007/Qwen3.6-35B-A3B-mlx-claude-coder-abliterated",
    "aratan/Qwen3.6-abliterated",
    "HammerAI/qwen3.5-abliterated",
    "bozstvimluvil0a/qwen3.5-abliterated",
    "aratan/qwen3.5-9b-abliterated-flash",
    "levy52/Qwen3.6-abliterated",
    "maxwellb/gemma4-12b-it-dn",
    "huihui_ai/gemma-4-abliterated",
    "huihui_ai/qwen3.5-abliterated",
    "huihui_ai/Qwen3.6-abliterated",
    "dzgg/gemma-4-abliterated",
    "dzgg/qwen3.5-abliterated",
    "alexanderschneider/gemma-4-abliterated",
    "lukey03/qwen3.5-9b-abliterated-vision",
    "charaf/Huihui-Qwen3.6-35B-A3B-abliterated-mlx",
    "Jarcgon/qwen3.6-abliterated-27b",
    "charaf/Huihui-Qwen3.6-27B-abliterated-mlx-nvfp4",
    "kiwi_kiwi/qwen3.5-abliterated",
    "nexusriot/gemma-4-abliterated",
    "aratan/qwen3.5-a3b-abliterated",
    "kaelri/qwen3.5-abliterated-nonthinking",
    "kiwi_kiwi/qwen3.5-abliterated-vision",
    "nexusriot/qwen3.5-abliterated",
    "kiwi_kiwi/gemma-4-abliterated-8b",
    "Jarcgon/gemma-4-abliterated",
    "oroboroslabs/qwen3.5-abliterated-47-4",
    "kiwi_kiwi/Qwen3.6-abliterated",
    "kiwi_kiwi/gemma-4-abliterated-q4",
    "vishalraj/gemma3-27b-abliterated",
    "nexusriot/gemma3-abliterated",
    "oroboroslabs/qwen3.5-abliterated-27-4",
    "huihui_ai/qwen3-vl-abliterated",
    "huihui_ai/qwen2.5-vl-abliterated",
    "hrbrmstr/qwen3.5-abliterated",
    "huihui_ai/fara-abliterated",
    "seamon67/Gemma3-Abliterated",
    "seamon67/Qwen2.5VL-Abliterated",
    "huihui_ai/gemma3-abliterated",
    "Drews54/llama3.2-vision-abliterated",
    "pidrilkin/gemma3_27b_abliterated",
    "huihui_ai/granite3.2-vision-abliterated",
    "Ryan512FL/llama3-GHAI-abliterated",
    "rjmalagon/gemma-3-abliterated",
    "rosemarla/devstral-abliterated-vision",
]

if "ollama" in VISION_MODELS:
    current = VISION_MODELS["ollama"]
    if not isinstance(current, list):
        current = list(current)
    current_set = set(current)
    for m in UNCENSORED_VISION_MODELS:
        if m not in current_set:
            current.append(m)
            current_set.add(m)
    VISION_MODELS["ollama"] = current
else:
    VISION_MODELS["ollama"] = list(UNCENSORED_VISION_MODELS)

# ── Backup / restore API (auto-archived deleted data) ──
@app.route('/api/backup/conversations', methods=['GET'])
def backup_list_conversations():
    return jsonify(backup_store.list_conversations())


@app.route('/api/backup/conversations/<cid>/restore', methods=['POST'])
def backup_restore_conversation(cid):
    global _conversations_dirty
    data = backup_store.get_conversation(cid)
    if not data:
        return jsonify({'error': 'Not found in backup'}), 404
    meta, messages = data
    _ensure_cache()
    already = cid in _conversations_cache
    with _cache_lock:
        if not already:
            orders = [c.get('order', 0) for c in _conversations_cache.values()]
            max_order = max(orders) if orders else 0
            _conversations_cache[cid] = {
                "id": cid,
                "title": meta.get("title") or "Recovered Chat",
                "created": meta.get("created") or datetime.now().isoformat(),
                "order": max_order + 1,
                "last_activity": meta.get("last_activity") or datetime.now().isoformat(),
            }
            _conversations_dirty = True
    if not already:
        with _sqlite_lock:
            for m in messages:
                _sqlite_conn.execute(
                    "INSERT INTO messages (conversation_id, role, content, attachments, created_at) VALUES (?, ?, ?, ?, ?)",
                    (cid, m.get("role"), m.get("content"), m.get("attachments"), m.get("created_at")),
                )
            _sqlite_conn.commit()
    save_conversations_async(_conversations_cache)
    backup_store.purge_conversation(cid)
    return jsonify({'ok': True, 'id': cid})


@app.route('/api/backup/conversations/<cid>', methods=['DELETE'])
def backup_purge_conversation(cid):
    backup_store.purge_conversation(cid)
    return jsonify({'ok': True})


@app.route('/api/backup/notes', methods=['GET'])
def backup_list_notes():
    return jsonify(backup_store.list_notes())


@app.route('/api/backup/notes/<note_id>/restore', methods=['POST'])
def backup_restore_note(note_id):
    note = backup_store.get_note(note_id)
    if not note:
        return jsonify({'error': 'Not found in backup'}), 404
    upsert_note(note_id, {
        "title": note.get("title", "Untitled"),
        "content": note.get("content", ""),
        "created": note.get("created"),
        "order": note.get("order", 0),
        "pinned": note.get("pinned", False),
        "color": note.get("color", "default"),
        "tags": note.get("tags", []),
        "embedding": note.get("embedding"),
    }, created=note.get("created"))
    backup_store.purge_note(note_id)
    return jsonify({'ok': True, 'id': note_id})


@app.route('/api/backup/notes/<note_id>', methods=['DELETE'])
def backup_purge_note(note_id):
    backup_store.purge_note(note_id)
    return jsonify({'ok': True})


@app.route('/api/backup/pins', methods=['GET'])
def backup_list_pins():
    return jsonify(backup_store.list_pins())


@app.route('/api/backup/pins/<pin_id>/restore', methods=['POST'])
def backup_restore_pin(pin_id):
    pin, links = backup_store.get_pin(pin_id)
    if pin is None:
        return jsonify({'error': 'Not found in backup'}), 404
    upsert_pin(pin_id, {
        "title": pin.get("title", "Untitled"),
        "content": pin.get("content", ""),
        "x": pin.get("x", 0),
        "y": pin.get("y", 0),
        "width": pin.get("width", 220),
        "height": pin.get("height", 200),
        "color": pin.get("color", "yellow"),
        "rotation": pin.get("rotation", 0),
        "tags": pin.get("tags", []),
        "type": pin.get("type", "note"),
        "filename": pin.get("filename"),
        "image_url": pin.get("image_url"),
        "embedding": pin.get("embedding"),
    }, now=pin.get("created"))
    for l in links:
        a, b = l.get("from"), l.get("to")
        if a and b:
            add_link(a, b, l.get("color", "black"))
    backup_store.purge_pin(pin_id)
    return jsonify({'ok': True, 'id': pin_id})


@app.route('/api/backup/pins/<pin_id>', methods=['DELETE'])
def backup_purge_pin(pin_id):
    backup_store.purge_pin(pin_id)
    return jsonify({'ok': True})


setup_viewer(app, get_conversation, get_messages)


def _auto_open_browser(url: str) -> None:
    """Open the app in the default browser shortly after startup (unless disabled).

    This makes the launcher a true "press the app and it opens itself" experience —
    no need to manually open a browser tab. Set TRIOFORGE_NO_BROWSER=1 to disable,
    which is what `start.vbs` does: there TrioForge opens in its own window, so a
    browser tab as well would be one window too many.
    """
    if os.environ.get("TRIOFORGE_NO_BROWSER") == "1":
        logger.info("Not opening a browser (TRIOFORGE_NO_BROWSER is set).")
        return

    def _open():
        import time
        import webbrowser
        time.sleep(2.0)
        try:
            webbrowser.open(url)
            logger.info("Opened your browser at %s", url)
        except Exception as exc:
            logger.warning("Could not open a browser: %s", exc)

    threading.Thread(target=_open, daemon=True).start()


APP_ID = "TrioForge.Desktop"


def _register_app_identity() -> None:
    """Tell Windows this process is TrioForge (name + icon, not "Python").

    The launcher runs the server from TrioForge.exe, whose version resource already
    says TrioForge. This adds the explicit AppUserModelID on top: it is the identity
    Windows uses for the taskbar and for grouping in Task Manager, and registering a
    DisplayName with it means no view of this process falls back to "Python".
    """
    try:
        import winreg
        ico = root_path("static", "logo", "triorforge.ico")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              r"Software\Classes\AppUserModelId\%s" % APP_ID) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "TrioForge")
            if os.path.isfile(ico):
                winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, ico)
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


# ── Port selection ───────────────────────────────────────────────────────────
# If the preferred port is held by ANOTHER app, move to the next free one instead
# of wrongly reporting "TrioForge is already running" and exiting. Cross-platform:
# this is what bit users behind Docker relays / other local servers on any OS.
def _port_is_free(port, host='127.0.0.1', timeout=0.6):
    import socket as _s
    s = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) != 0
    except Exception:
        return True
    finally:
        try:
            s.close()
        except Exception:
            pass


def _is_trioforge_on(port, host='127.0.0.1', timeout=1.5):
    """True only if TrioForge itself answers on this port (via /api/ping).

    Tries BOTH schemes. On Windows TrioForge serves HTTPS by default, and while
    this only spoke http the check never matched a running instance - so a second
    launch decided the port was "busy with another app", picked 5004, then 5005,
    and every launch piled up another server, another console window and another
    firewall prompt.
    """
    import json as _json
    import urllib.request
    import ssl as _ssl
    ctx = _ssl._create_unverified_context()
    for scheme in ('http', 'https'):
        try:
            with urllib.request.urlopen('%s://%s:%d/api/ping' % (scheme, host, port),
                                        timeout=timeout, context=ctx) as r:
                data = _json.loads(r.read(400).decode('utf-8', 'replace'))
            if data.get('app') == 'trioforge':
                return True
        except Exception:
            continue
    return False


def choose_port(preferred, host='127.0.0.1', tries=20):
    """Return (port, reason).

    reason: 'free'    → nothing listening; use it
            'running' → TrioForge is already there (open the browser, then exit)
            'moved'   → another app owns it; moved to the next free port
            'busy'    → no free port found in range
    """
    if _port_is_free(preferred, host):
        return preferred, 'free'
    if _is_trioforge_on(preferred, host):
        return preferred, 'running'
    for p in range(preferred + 1, preferred + 1 + tries):
        if _port_is_free(p, host):
            return p, 'moved'
    return preferred, 'busy'


if __name__ == '__main__':
    # Port is configurable via TRIOFORGE_PORT (default 5003).
    # If that port is held by ANOTHER app (a stale Docker/wslrelay relay, some
    # other local server, …) we move to the next free port instead of wrongly
    # saying "already running". Only a real TrioForge on the port triggers the
    # "already running" path.
    PORT_PREF = int(os.environ.get('TRIOFORGE_PORT', '5003') or 5003)
    PORT, port_reason = choose_port(PORT_PREF)
    if port_reason == 'moved':
        logger.warning("Port %d is in use by another app — using %d instead.", PORT_PREF, PORT)
        print("[TrioForge] Port %d is busy (another app) — using %d instead." % (PORT_PREF, PORT))
    elif port_reason == 'busy':
        logger.error("Ports %d-%d are all in use.", PORT_PREF, PORT_PREF + 20)
        print("[TrioForge] Ports %d-%d are all busy. Set TRIOFORGE_PORT=<free port> and re-run."
              % (PORT_PREF, PORT_PREF + 20))
        sys.exit(1)

    # ── Decide HTTP vs HTTPS ────────────────────────────────────────────────
    # localhost is a "secure context", so plain HTTP has no scary browser warning
    # and mic/clipboard/crypto still work. HTTPS is only used when the mkcert local
    # CA is trusted (so browsers DON'T show a "not secure" page) or when
    # TRIOFORGE_SSL=1 is explicitly set. Default to HTTP to avoid the
    # "Your connection is not private" scare on a fresh Linux/macOS machine.
    ssl_env = os.environ.get('TRIOFORGE_SSL', '').strip().lower()
    if ssl_env in ('1', 'true', 'on'):
        want_https = True
    elif ssl_env in ('0', 'false', 'off'):
        want_https = False
    else:
        # Linux/macOS default to plain HTTP (no scary cert warning — those
        # platforms have flaky mkcert-CA trust, especially Firefox's own store).
        # Windows keeps HTTPS by default (the CA is usually trust-installed with
        # admin there). Override anytime with TRIOFORGE_SSL=0/1.
        want_https = (platform.system() == 'Windows')

    cert_file = root_path('cert_store', 'localhost+1.pem')
    key_file  = root_path('cert_store', 'localhost+1-key.pem')

    if want_https:
        # Ensure certs exist; if generation fails, fall back to HTTP gracefully.
        if not (os.path.exists(cert_file) and os.path.exists(key_file)):
            ensure_certificates()
        if os.path.exists(cert_file) and os.path.exists(key_file):
            ssl_context = (cert_file, key_file)
            scheme = 'https'
            logger.info("Running with HTTPS (SSL enabled)")
        else:
            ssl_context = None
            scheme = 'http'
            logger.warning("HTTPS requested but certs unavailable — falling back to HTTP.")
    else:
        ssl_context = None
        scheme = 'http'
        logger.info("Running with HTTP — localhost is a secure context, no cert warning.")

    url = "%s://localhost:%d" % (scheme, PORT)

    # Already-running guard: exit only when it really IS TrioForge on the port
    # (confirmed via /api/ping in choose_port), so we never spawn a duplicate
    # process fighting over the database — and never refuse to start just because
    # some unrelated app happens to own the port.
    if port_reason == 'running':
        print("TrioForge is already running at %s — opening it." % url)
        try:
            if os.environ.get('TRIOFORGE_NO_BROWSER', '').strip() in ('1', 'true', 'on'):
                print("[TrioForge] (not opening a browser: TRIOFORGE_NO_BROWSER is set)")
            else:
                import webbrowser
                webbrowser.open(url)  # synchronous: opens before we exit
        except Exception:
            pass
        sys.exit(0)

    try:
        from version import __version__ as _trio_version
    except Exception:
        _trio_version = "?"
    logger.info("TrioForge v%s", _trio_version)
    logger.info("AI CHAT Interfacing Loading... - Multi-Conversation")
    logger.info("Default model : %s", DEFAULT_MODEL)
    logger.info("Current model : %s", current_model)
    logger.info("Storage       : %s (metadata only), SQLite for messages", CONVERSATIONS_FILE)

    logger.info("Open your browser at: %s", url)
    _auto_open_browser(url)

    # Give this process TrioForge's own identity. Task Manager / the taskbar name a
    # process from its executable, and the server used to run as pythonw.exe; the
    # launcher's TrioForge.exe carries the right version resource, and this adds the
    # explicit app identity on top so nothing resolves to "Python".
    _register_app_identity()

    # ── Which interface to listen on ─────────────────────────────────────────
    # Default is localhost only, exactly like Ollama's default. Listening on
    # 0.0.0.0 on every launch is what made Windows Defender Firewall ask "allow
    # this app to communicate on public networks?" every single time - and it also
    # put the whole workspace on the local network without asking.
    # Reachable-by-others is opt-in: TRIOFORGE_HOST=0.0.0.0, or host mode
    # (TRIOFORGE_PASSWORD set), which also puts a password in front of it.
    HOST = os.environ.get('TRIOFORGE_HOST', '').strip()
    if not HOST:
        HOST = '0.0.0.0' if os.environ.get('TRIOFORGE_PASSWORD', '').strip() else '127.0.0.1'
    logger.info("Listening on %s:%d", HOST, PORT)

    # For production, use gunicorn or waitress instead of app.run.
    # Example: gunicorn -w 4 -b 0.0.0.0:%d app:app  (PORT)
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False, ssl_context=ssl_context, threaded=True)