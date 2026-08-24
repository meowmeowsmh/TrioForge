# app.py – chat + notes + cork board + integrated weather toast (performance-optimized)
from flask import Flask, request, jsonify, Response
from flask_compress import Compress
import requests
import hashlib
import base64
import os
import json as std_json
import sys
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
from io import StringIO
from typing import Any, Dict, List, Optional

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

# ── Imports ──
from llm_providers import (
    LLMProvider,
    OllamaProvider,
    LlamaCppProvider,
    HuggingFaceProvider,
    GroqProvider,
    DeepSeekProvider,
    ClaudeProvider,
    model_supports_vision,
    VISION_MODELS,
    describe_or_extract_file,
    sanitize_api_key,
)
from notes import notes_bp
from cork_board import corkboard_bp
from zoompicleftandright import setup_viewer

try:
    import pynvml
    pynvml.nvmlInit()
    NVML_AVAILABLE = True
except Exception:
    NVML_AVAILABLE = False

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024  # 25 MB request body cap (uploads + chat JSON)
Compress(app)
app.register_blueprint(notes_bp)
app.register_blueprint(corkboard_bp)

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
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response

DEFAULT_MODEL = "vaultbox/qwen3.5-uncensored:9b"
OLLAMA_BASE_URL = os.environ.get('OLLAMA_BASE_URL', 'http://127.0.0.1:11434')
CONVERSATIONS_FILE = "json_configuration/conversations.json"
MODEL_CONFIG_FILE = "json_configuration/model_config.json"
ATTACHMENTS_DIR = "json_configuration/attachments"
SQLITE_DIR = "sqlite_data"
SQLITE_DB_PATH = os.path.join(SQLITE_DIR, "conversations.db")

try:
    from duckduckgo_search import DDGS
    SEARCH_AVAILABLE = True
except ImportError:
    SEARCH_AVAILABLE = False

os.makedirs(os.path.dirname(CONVERSATIONS_FILE), exist_ok=True)
os.makedirs(os.path.dirname(MODEL_CONFIG_FILE), exist_ok=True)
os.makedirs(ATTACHMENTS_DIR, exist_ok=True)
os.makedirs(SQLITE_DIR, exist_ok=True)

# ── SQLite ──
_sqlite_conn = sqlite3.connect(SQLITE_DB_PATH, check_same_thread=False)
_sqlite_lock = threading.Lock()

def _init_sqlite():
    with _sqlite_lock:
        _sqlite_conn.execute("PRAGMA journal_mode=WAL;")
        _sqlite_conn.execute("PRAGMA synchronous=NORMAL;")
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
        cur = _sqlite_conn.cursor()
        cur.execute("PRAGMA table_info(messages)")
        columns = [col[1] for col in cur.fetchall()]
        if 'attachments' not in columns:
            _sqlite_conn.execute("ALTER TABLE messages ADD COLUMN attachments TEXT")
        _sqlite_conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);")
        _sqlite_conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);")
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
def ensure_certificates():
    cert_dir = 'cert_store'
    cert_file = os.path.join(cert_dir, 'localhost+1.pem')
    key_file = os.path.join(cert_dir, 'localhost+1-key.pem')
    if os.path.exists(cert_file) and os.path.exists(key_file):
        return True
    logger.info("Certificates not found. Auto-generating...")
    os.makedirs(cert_dir, exist_ok=True)
    if platform.system() != "Windows":
        logger.warning("Auto-cert generation is only supported on Windows.")
        return False
    mkcert_exe = "mkcert.exe"
    if not os.path.exists(mkcert_exe):
        logger.info("Downloading mkcert...")
        url = "https://github.com/FiloSottile/mkcert/releases/latest/download/mkcert-v1.4.4-windows-amd64.exe"
        try:
            urllib.request.urlretrieve(url, mkcert_exe)
        except Exception as e:
            logger.error("Failed to download mkcert: %s", e)
            return False
    try:
        subprocess.run([mkcert_exe, "-install"], check=True, capture_output=True)
        subprocess.run([mkcert_exe, "localhost", "127.0.0.1"], check=True)
        if os.path.exists("localhost+1.pem"):
            os.rename("localhost+1.pem", cert_file)
        if os.path.exists("localhost+1-key.pem"):
            os.rename("localhost+1-key.pem", key_file)
        return True
    except Exception as e:
        logger.error("Certificate generation failed: %s", e)
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
            "SELECT role, content, attachments, created_at FROM messages WHERE conversation_id = ? ORDER BY created_at",
            (cid,)
        )
        rows = cur.fetchall()
    msgs = []
    for role, content, attachments_json, created_at in rows:
        msg = {"role": role, "text": content or "", "ts": created_at}
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
            except Exception:
                pass
        msgs.append(msg)
    return msgs

def add_message(cid: str, role: str, text: str, images: Optional[List[dict]] = None, files: Optional[List[dict]] = None) -> bool:
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

    attachments_json = std_json.dumps({"images": stored_images, "files": stored_files}) if (stored_images or stored_files) else None

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
                conv["title"] = text[:40] + ("..." if len(text) > 40 else "")
            conv["last_activity"] = datetime.now().isoformat()
            _conversations_dirty = True
    save_conversations_async(_conversations_cache)
    return True

def delete_conversation(cid: str) -> bool:
    _ensure_cache()
    if cid in _conversations_cache:
        with _cache_lock:
            del _conversations_cache[cid]
            _conversations_dirty = True
        save_conversations_async(_conversations_cache)
        return True
    return False

def clear_conversation_messages(cid: str) -> bool:
    with _sqlite_lock:
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
_cached_html_model = None

def get_cached_html():
    global _cached_html, _cached_html_model
    if _cached_html is None or _cached_html_model != current_model:
        _cached_html = build_html(current_model)
        _cached_html_model = current_model
    return _cached_html

# ── Build HTML (served from templates/index.html) ──
CHAT_HTML_PATH = os.path.join(os.path.dirname(__file__), "templates", "index.html")

def build_html(model_name=None):
    with open(CHAT_HTML_PATH, "r", encoding="utf-8") as f:
        return f.read()

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
}

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

@lru_cache(maxsize=128)
def _cached_models(provider_name, api_key):
    provider = providers.get(provider_name)
    if not provider:
        return []
    try:
        return provider.list_models(api_key=api_key if api_key != 'None' else None)
    except Exception:
        return []

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
    current_model = model
    save_model_config(model)
    providers["ollama"].model = model
    cached_vision_check.cache_clear()
    _cached_models.cache_clear()
    _cached_html = None
    return jsonify({'ok': True, 'model': model})

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
    return jsonify(provider.get_status())

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
        cur.execute("SELECT id FROM messages WHERE conversation_id = ? ORDER BY created_at", (cid,))
        rowids = [row[0] for row in cur.fetchall()]
        if idx < 0 or idx >= len(rowids):
            return jsonify({'error': 'Index out of range'}), 400
        rowid = rowids[idx]
        cur.execute("DELETE FROM messages WHERE id = ?", (rowid,))
        _sqlite_conn.commit()
    return jsonify({'ok': True})

@app.route('/conversations/<cid>/rename', methods=['PUT'])
def rename_conversation(cid):
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


def _build_messages(conv_id: str, system_prompt: str, final_prompt: str) -> List[dict]:
    """Build the full message list for a provider call from stored history."""
    messages = []
    for msg in get_messages(conv_id):
        if msg['role'] == 'user':
            messages.append({"role": "user", "content": msg['text']})
        elif msg['role'] == 'bot':
            messages.append({"role": "assistant", "content": msg['text']})
    messages = [{"role": "system", "content": system_prompt}] + messages
    messages = trim_conversation_history(messages)
    messages.append({"role": "user", "content": final_prompt})
    return messages


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
@app.route('/api/voice/logs', methods=['GET'])
def voice_logs():
    """Return the saved local voice-agent conversation logs for the UI."""
    root = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(root, "voiceguide_llama.cpp_guide")

    def read_tail(name, lines=600):
        path = os.path.join(log_dir, name)
        if not os.path.exists(path):
            return ""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return "".join(f.readlines()[-lines:])
        except Exception:
            return ""

    return jsonify({
        "conversations": read_tail("conversations.log"),
        "log": read_tail("voice_agent.log"),
        "exists": any(os.path.exists(os.path.join(log_dir, n))
                      for n in ("conversations.log", "voice_agent.log")),
    })


# ── Chat endpoints ──
@app.route('/chat', methods=['POST'])
@rate_limited(max_per_minute=20)
def chat():
    global current_model
    try:
        data = request.get_json(force=True, silent=True) or {}
        user_message = data.get('message', '').strip()
        images = data.get('images', [])
        files = data.get('files', [])
        conv_id = data.get('conversation_id')
        search_enabled = data.get('search', False)
        provider_name = data.get('provider', 'ollama')
        model = data.get('model', None)
        api_key = sanitize_api_key(data.get('api_key', None))

        if not user_message and not images and not files:
            return jsonify({'error': 'Nothing to send'}), 400

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

        provider = providers.get(provider_name)
        if not provider:
            return jsonify({'error': f'Unknown provider: {provider_name}'}), 400

        system_prompt = provider.get_system_prompt()
        final_prompt = _build_final_prompt(system_prompt, user_message, files, search_context)
        messages = _build_messages(conv_id, system_prompt, final_prompt)

        extra_kwargs = {"model": model}
        if api_key:
            extra_kwargs['api_key'] = api_key

        if provider_name == 'ollama':
            mem_settings = get_ollama_memory_settings()
            extra_kwargs['num_gpu'] = mem_settings['num_gpu']
            extra_kwargs['low_vram'] = mem_settings['low_vram']

        start_time = time.time()
        if images:
            if cached_vision_check(provider_name, model):
                reply = provider.generate_with_image(messages, images, **extra_kwargs)
            else:
                future = _executor.submit(describe_image_with_llava, images[0]["b64"])
                description = future.result(timeout=60)
                if description:
                    inject = f"[Image description]\n{description.strip()}\n\n[User question]\n"
                else:
                    inject = "[Image description unavailable]\n\n[User question]\n"
                messages[-1]['content'] = inject + messages[-1]['content']
                reply = provider.generate(messages, **extra_kwargs)
        else:
            reply = provider.generate(messages, **extra_kwargs)
        end_time = time.time()

        token_estimate = len(reply.split()) / 0.75
        duration = end_time - start_time if end_time > start_time else 1
        usage = {"tokens": int(token_estimate), "duration_sec": round(duration, 2)}

        original_message = data.get('message', '').strip()

        if not add_message(conv_id, "user", original_message, images, files):
            return jsonify({'error': f'Failed to save user message to {conv_id}'}), 500
        if not add_message(conv_id, "bot", reply, [], []):
            return jsonify({'error': f'Failed to save bot message to {conv_id}'}), 500

        return jsonify({'response': reply, 'usage': usage})

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

@app.route('/chat_stream', methods=['POST'])
@rate_limited(max_per_minute=20)
def chat_stream():
    try:
        data = request.get_json(force=True, silent=True) or {}
        user_message = data.get('message', '').strip()
        images = data.get('images', [])
        files = data.get('files', [])
        conv_id = data.get('conversation_id')
        search_enabled = data.get('search', False)
        model = data.get('model', current_model)
        api_key = sanitize_api_key(data.get('api_key', None))

        provider_name = data.get('provider', 'ollama')
        if provider_name != 'ollama':
            return jsonify({'error': 'Streaming only supported for Ollama in this version.'}), 400

        if not user_message and not images and not files:
            return jsonify({'error': 'Nothing to send'}), 400

        if not conv_id:
            conv_id = create_conversation()
        else:
            conv = get_conversation(conv_id)
            if conv is None:
                return jsonify({'error': 'Conversation not found'}), 404

        if is_ollama_command(user_message):
            return Response(
                handle_ollama_command_stream(conv_id, user_message, images, files),
                mimetype='text/event-stream'
            )

        search_context = _run_web_search(user_message, search_enabled)

        provider = providers.get(provider_name)
        if not provider:
            return jsonify({'error': f'Unknown provider: {provider_name}'}), 400

        system_prompt = provider.get_system_prompt()
        final_prompt = _build_final_prompt(system_prompt, user_message, files, search_context)
        messages = _build_messages(conv_id, system_prompt, final_prompt)

        mem_settings = get_ollama_memory_settings()

        payload = {
            "model": model or current_model,
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": 0.7,
                "num_predict": 16384,
                "num_ctx": 16384,
                "num_gpu": mem_settings['num_gpu'],
            }
        }
        if images:
            last_msg = messages[-1]
            b64_list = []
            for img in images:
                b64 = img["b64"]
                if "," in b64:
                    b64 = b64.split(",", 1)[1]
                b64_list.append(b64)
            payload["messages"][-1] = {
                "role": "user",
                "content": last_msg["content"],
                "images": b64_list
            }

        def generate():
            full_response = ""
            try:
                r = requests.post(
                    f"{OLLAMA_BASE_URL}/api/chat",
                    json=payload,
                    stream=True,
                    timeout=300
                )
                r.raise_for_status()
                for line in r.iter_lines():
                    if line:
                        chunk = json_loads(line)
                        if "message" in chunk and "content" in chunk["message"]:
                            token = chunk["message"]["content"]
                            if token:
                                full_response += token
                                yield f"data: {json_dumps({'token': token})}\n\n"
                        if chunk.get("done", False):
                            usage = {}
                            if "eval_count" in chunk and "eval_duration" in chunk:
                                duration_sec = chunk.get("eval_duration", 0) / 1e9
                                token_count = chunk.get("eval_count", 0)
                                usage = {"tokens": token_count, "duration_sec": duration_sec}
                            yield f"data: {json_dumps({'done': True, 'full_response': full_response, 'usage': usage})}\n\n"
                            break
            except Exception as e:
                yield f"data: {json_dumps({'error': str(e)})}\n\n"

            add_message(conv_id, "user", user_message, images, files)
            add_message(conv_id, "bot", full_response, [], [])

        return Response(generate(), mimetype='text/event-stream')

    except requests.exceptions.ConnectionError:
        return jsonify({'error': 'Cannot connect to Ollama. Make sure it is running.'}), 503
    except requests.exceptions.Timeout:
        return jsonify({'error': 'Request timed out. Try a shorter message.'}), 504
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            return jsonify({'error': f'Model "{model}" not found in Ollama. Please pull it first.'}), 404
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

setup_viewer(app, get_conversation, get_messages)

if __name__ == '__main__':
    logger.info("AI CHAT Interfacing Loading... - Multi-Conversation")
    logger.info("Default model : %s", DEFAULT_MODEL)
    logger.info("Current model : %s", current_model)
    logger.info("Storage       : %s (metadata only), SQLite for messages", CONVERSATIONS_FILE)

    cert_file = 'cert_store/localhost+1.pem'
    key_file  = 'cert_store/localhost+1-key.pem'

    if os.path.exists(cert_file) and os.path.exists(key_file):
        ssl_context = (cert_file, key_file)
        logger.info("Running with HTTPS (SSL enabled)")
        url = "https://localhost:5001"
    else:
        if ensure_certificates():
            ssl_context = (cert_file, key_file)
            logger.info("Running with HTTPS (SSL enabled)")
            url = "https://localhost:5001"
        else:
            ssl_context = None
            logger.warning("Running with HTTP (SSL unavailable)")
            url = "http://localhost:5001"

    logger.info("Open your browser at: %s", url)

    # For production, use gunicorn or waitress instead of app.run.
    # Example: gunicorn -w 4 -b 0.0.0.0:5001 app:app
    app.run(host='0.0.0.0', port=5001, debug=False, use_reloader=False, ssl_context=ssl_context, threaded=True)