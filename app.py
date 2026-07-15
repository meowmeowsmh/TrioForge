# app.py – chat + notes + cork board + integrated weather toast (full animation – guaranteed working)
from flask import Flask, request, jsonify, Response
import requests
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
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
import threading
import sqlite3

# ── Try orjson ──
try:
    import orjson
    def json_dumps(obj):
        return orjson.dumps(obj).decode('utf-8')
    def json_dumps_pretty(obj):
        return orjson.dumps(obj, option=orjson.OPT_INDENT_2).decode('utf-8')
    def json_loads(s):
        return orjson.loads(s)
    print("🚀 Using orjson for faster JSON")
except ImportError:
    json_dumps = std_json.dumps
    def json_dumps_pretty(obj):
        return std_json.dumps(obj, ensure_ascii=False, indent=2)
    json_loads = std_json.loads
    print("ℹ️ Using standard json (install orjson for better performance)")

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
)
from notes import notes_bp
from cork_board import corkboard_bp
from zoompicleftandright import setup_viewer, get_viewer_html

try:
    import pynvml
    pynvml.nvmlInit()
    NVML_AVAILABLE = True
except:
    NVML_AVAILABLE = False

app = Flask(__name__)
app.register_blueprint(notes_bp)
app.register_blueprint(corkboard_bp)

DEFAULT_MODEL = "vaultbox/qwen3.5-uncensored:9b"
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
                created_at      TEXT NOT NULL
            );
        """)
        _sqlite_conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);")
        _sqlite_conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);")
        _sqlite_conn.commit()
_init_sqlite()

def log_message_to_sqlite(cid, role, text):
    def _write():
        try:
            with _sqlite_lock:
                _sqlite_conn.execute(
                    "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                    (cid, role, text, datetime.now().isoformat())
                )
                _sqlite_conn.commit()
        except Exception as e:
            print(f"⚠️ Failed to log message to sqlite: {e}")
    _save_executor.submit(_write)

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
    print("🔑 Certificates not found. Auto‑generating...")
    os.makedirs(cert_dir, exist_ok=True)
    if platform.system() != "Windows":
        print("⚠️  Auto‑cert generation is only supported on Windows.")
        return False
    mkcert_exe = "mkcert.exe"
    if not os.path.exists(mkcert_exe):
        print("📥 Downloading mkcert...")
        url = "https://github.com/FiloSottile/mkcert/releases/latest/download/mkcert-v1.4.4-windows-amd64.exe"
        try:
            urllib.request.urlretrieve(url, mkcert_exe)
        except Exception as e:
            print(f"❌ Failed to download mkcert: {e}")
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
        print(f"❌ Certificate generation failed: {e}")
        return False

# ── Model persistence ──
def load_model_config():
    if os.path.exists(MODEL_CONFIG_FILE):
        try:
            with open(MODEL_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = std_json.load(f)
                return data.get("model", DEFAULT_MODEL)
        except:
            pass
    return DEFAULT_MODEL
def save_model_config(model):
    with open(MODEL_CONFIG_FILE, "w", encoding="utf-8") as f:
        std_json.dump({"model": model}, f, ensure_ascii=False, indent=2)
current_model = load_model_config()

# ── Conversation storage ──
_conversations_cache = {}
_cache_loaded = False
_cache_lock = threading.Lock()

def _save_attachment_to_disk(b64_data, hint_name=""):
    if not b64_data:
        return ""
    ext = os.path.splitext(hint_name)[1] or ".bin"
    fname = f"{uuid.uuid4().hex}{ext}"
    path = os.path.join(ATTACHMENTS_DIR, fname)
    try:
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64_data))
        return fname
    except Exception as e:
        print(f"⚠️ Failed to persist attachment: {e}")
        return ""

def _load_attachment_from_disk(fname):
    if not fname:
        return ""
    path = os.path.join(ATTACHMENTS_DIR, fname)
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        print(f"⚠️ Failed to read attachment {fname}: {e}")
        return ""

def _strip_blobs_for_disk(convs):
    lean = {}
    for cid, conv in convs.items():
        lean_conv = dict(conv)
        lean_messages = []
        for msg in conv.get("messages", []):
            lean_msg = dict(msg)
            lean_msg["images"] = [
                ({**im, "b64": ""} if im.get("file") else im) for im in msg.get("images", [])
            ]
            lean_msg["files"] = [
                ({**f, "b64": ""} if f.get("file") else f) for f in msg.get("files", [])
            ]
            lean_messages.append(lean_msg)
        lean_conv["messages"] = lean_messages
        lean[cid] = lean_conv
    return lean

def _hydrate_blobs_from_disk(convs):
    for conv in convs.values():
        for msg in conv.get("messages", []):
            for im in msg.get("images", []):
                if im.get("file") and not im.get("b64"):
                    im["b64"] = _load_attachment_from_disk(im["file"])
            for f in msg.get("files", []):
                if f.get("file") and not f.get("b64"):
                    f["b64"] = _load_attachment_from_disk(f["file"])

def _ensure_cache():
    global _conversations_cache, _cache_loaded
    if _cache_loaded:
        return
    with _cache_lock:
        if _cache_loaded:
            return
        if os.path.exists(CONVERSATIONS_FILE):
            try:
                with open(CONVERSATIONS_FILE, "r", encoding="utf-8") as f:
                    _conversations_cache = json_loads(f.read())
                _hydrate_blobs_from_disk(_conversations_cache)
            except Exception as e:
                print(f"⚠️ Error loading conversations: {e}")
                _conversations_cache = {}
        _cache_loaded = True

def load_conversations():
    _ensure_cache()
    return _conversations_cache

_save_executor = ThreadPoolExecutor(max_workers=1)

def save_conversations_async(convs):
    def _save():
        try:
            lean = _strip_blobs_for_disk(convs)
            temp_file = CONVERSATIONS_FILE + ".tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                f.write(json_dumps_pretty(lean))
            os.replace(temp_file, CONVERSATIONS_FILE)
        except Exception as e:
            print(f"❌ Failed to save conversations: {e}")
    _save_executor.submit(_save)

def create_conversation(title=None):
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
            "messages": [],
            "order": new_order
        }
    save_conversations_async(_conversations_cache)
    return cid

def get_conversation(cid):
    _ensure_cache()
    return _conversations_cache.get(cid)

setup_viewer(app, get_conversation)

def add_message(cid, role, text, images=None, files=None, ts=None):
    _ensure_cache()
    if cid not in _conversations_cache:
        return False
    if images is None: images = []
    if files is None: files = []
    if ts is None: ts = datetime.now().strftime("%H:%M")
    stored_images = []
    for img in images:
        b64 = img.get("b64", "")
        fname = _save_attachment_to_disk(b64, img.get("name", "image.png"))
        stored_images.append({
            "name": img.get("name", "image"),
            "b64": b64,
            "mime": img.get("mime", "image/png"),
            "file": fname
        })
    stored_files = []
    for f in files:
        b64 = f.get("b64", "")
        fname = _save_attachment_to_disk(b64, f.get("name", "file.bin"))
        stored_files.append({
            "name": f.get("name", "file"),
            "b64": b64,
            "mime": f.get("mime", "application/octet-stream"),
            "file": fname
        })
    with _cache_lock:
        _conversations_cache[cid]["messages"].append({
            "role": role,
            "text": text,
            "images": stored_images,
            "files": stored_files,
            "ts": ts
        })
        if role == "user" and len(_conversations_cache[cid]["messages"]) == 1:
            _conversations_cache[cid]["title"] = text[:40] + ("..." if len(text) > 40 else "")
    save_conversations_async(_conversations_cache)
    log_message_to_sqlite(cid, role, text)
    return True

def delete_conversation(cid):
    _ensure_cache()
    if cid in _conversations_cache:
        with _cache_lock:
            del _conversations_cache[cid]
        save_conversations_async(_conversations_cache)
        return True
    return False

def clear_conversation_messages(cid):
    _ensure_cache()
    if cid not in _conversations_cache:
        return False
    with _cache_lock:
        _conversations_cache[cid]["messages"] = []
    save_conversations_async(_conversations_cache)
    return True

def strip_c_comments(text):
    text = re.sub(r'//.*', '', text)
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    text = '\n'.join(line for line in text.splitlines() if line.strip())
    return text

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
        resp = requests.post("http://127.0.0.1:11434/api/generate", json=payload, timeout=60)
        resp.raise_for_status()
        return resp.json().get("response", "")
    except Exception as e:
        print(f"⚠️ llava fallback failed: {e}")
        return ""

_executor = ThreadPoolExecutor(max_workers=2)

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
            except:
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
            r = requests.get("http://127.0.0.1:11434/api/tags", timeout=5)
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
            r = requests.post("http://127.0.0.1:11434/api/show", json={"name": model}, timeout=10)
            r.raise_for_status()
            return json_dumps(r.json())
        elif cmd in ('rm', 'delete'):
            if not args:
                return "❌ Usage: ollama rm <model>"
            model = args[0]
            r = requests.delete("http://127.0.0.1:11434/api/delete", json={"name": model}, timeout=10)
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
            r = requests.post("http://127.0.0.1:11434/api/pull", json={"name": model}, stream=True, timeout=600)
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
            r = requests.post("http://127.0.0.1:11434/api/push", json=payload, headers=headers, stream=True, timeout=600)
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
                r = requests.post("http://127.0.0.1:11434/api/pull", json={"name": model}, stream=True, timeout=600)
                r.raise_for_status()
                for line in r.iter_lines():
                    if line:
                        chunk = json_loads(line)
                        status = chunk.get('status', '')
                        if status:
                            full_response += status + "\n"
                            yield f"data: {json_dumps({'token': status + '\n'})}\n\n"
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
                r = requests.post("http://127.0.0.1:11434/api/push", json=payload, headers=headers, stream=True, timeout=600)
                r.raise_for_status()
                for line in r.iter_lines():
                    if line:
                        chunk = json_loads(line)
                        status = chunk.get('status', '')
                        if status:
                            full_response += status + "\n"
                            yield f"data: {json_dumps({'token': status + '\n'})}\n\n"
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
                yield f"data: {json_dumps({'token': line + '\n'})}\n\n"
        yield f"data: {json_dumps({'done': True, 'full_response': full_response})}\n\n"
    except Exception as e:
        err = f"❌ Command failed: {e}"
        yield f"data: {json_dumps({'error': err})}\n\n"
    ts = datetime.now().strftime("%H:%M")
    if conv_id:
        add_message(conv_id, "user", user_message, images, files, ts)
        add_message(conv_id, "bot", full_response, [], [], ts)

# ── Build HTML (Chat, Notes, Cork Board, plus integrated Weather) ──
def build_html(model_name):
    html = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E🤖%3C/text%3E%3C/svg%3E">
<title>TrioForge chat interface</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/atom-one-dark.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<style>
/* ===== all styles (without notes-panel styles) ===== */
* { margin:0; padding:0; box-sizing:border-box; }
html, body {
    height:100%;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
    background: #0a0a0f;
    color: #e1e4e8;
    overflow: hidden;
    transition: background 0.3s ease, color 0.3s ease;
}
body::before {
    content: '';
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: radial-gradient(ellipse at 20% 50%, #1a1a2e 0%, transparent 50%),
                radial-gradient(ellipse at 80% 20%, #16213e 0%, transparent 50%);
    animation: bgMove 20s ease infinite;
    z-index: -1;
    transition: opacity 0.4s ease;
}
body.light-mode::before { opacity: 0; }
@keyframes bgMove {
    0% { transform: scale(1); }
    50% { transform: scale(1.05); }
    100% { transform: scale(1); }
}
.app {
    display:flex; height:100%;
    backdrop-filter: blur(2px);
}
/* ── Sidebar ─────────────────────────────────── */
.sidebar {
    width: 280px;
    background: rgba(18, 18, 26, 0.85);
    backdrop-filter: blur(20px);
    border-right: 1px solid rgba(255,255,255,0.05);
    display:flex; flex-direction:column; flex-shrink:0;
    box-shadow: 0 0 20px rgba(0,0,0,0.4);
    transition: width 0.25s ease, margin 0.25s ease, background 0.3s ease;
    overflow: hidden;
}
.sidebar.hidden {
    width: 0;
    margin: 0;
    border: none;
    overflow: hidden;
    padding: 0;
}
.sidebar-header {
    padding: 20px 16px 12px;
    border-bottom: 1px solid rgba(255,255,255,0.05);
    display:flex; align-items:center; gap:10px; flex-wrap:wrap;
}
.sidebar-header h2 {
    font-size: 17px;
    font-weight: 600;
    background: linear-gradient(135deg, #58a6ff, #3fb950);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
}
.new-chat-btn {
    background: linear-gradient(135deg, #1f6feb, #388bfd);
    color: white;
    border: none;
    border-radius: 10px;
    padding: 8px 16px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    margin-left: auto;
    white-space: nowrap;
    box-shadow: 0 4px 12px rgba(31,111,235,0.4);
    transition: all 0.2s;
}
.new-chat-btn:hover {
    box-shadow: 0 6px 16px rgba(31,111,235,0.6);
    transform: translateY(-1px);
}
.search-box {
    padding: 8px 16px;
}
.search-box input {
    width: 100%;
    padding: 8px 12px;
    border-radius: 20px;
    border: 1px solid rgba(255,255,255,0.1);
    background: rgba(13,17,23,0.7);
    color: #e6edf3;
    font-size: 13px;
    outline: none;
    transition: border-color 0.2s, background 0.3s, color 0.3s;
}
.search-box input:focus { border-color: #58a6ff; }
.search-box input::placeholder { color: #8b949e; }
.conv-list {
    flex:1; overflow-y:auto; padding: 8px;
}
.conv-list::-webkit-scrollbar { width: 4px; }
.conv-list::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 2px; }
.no-results {
    padding: 20px 12px;
    text-align: center;
    color: #8b949e;
    font-size: 14px;
}
.group-heading {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: #8b949e;
    padding: 12px 12px 6px;
    border-bottom: 1px solid rgba(255,255,255,0.05);
    margin-top: 8px;
}
.group-heading:first-of-type { margin-top: 0; }
.conv-item {
    display:flex; align-items:center; padding: 8px 12px; cursor:grab;
    border-radius: 10px; margin-bottom: 2px; transition: background 0.2s;
    gap: 6px;
    background: transparent;
    user-select: none;
}
.conv-item:hover { background: rgba(255,255,255,0.05); }
.conv-item.active {
    background: rgba(31,111,235,0.15);
    border: 1px solid rgba(31,111,235,0.3);
}
.conv-item.dragging { opacity: 0.4; }
.conv-item.drag-over { border: 2px dashed #58a6ff; }
.conv-item .title {
    flex:1; font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    color: #c9d1d9;
}
.conv-item.active .title { color: white; }
.conv-item .rename-btn {
    background: transparent; border: none; color: #8b949e; font-size: 13px;
    cursor: pointer; opacity: 0.4; padding: 0 4px; transition: opacity 0.2s;
}
.conv-item .rename-btn:hover { opacity: 1; color: #58a6ff; }
.conv-item .del {
    background: transparent; border: none; color: #f85149; font-size: 16px;
    cursor: pointer; opacity: 0.4; padding: 0 4px; transition: opacity 0.2s;
}
.conv-item .del:hover { opacity: 1; }
.conv-item .time {
    font-size: 11px; color: #8b949e; margin-right: 4px; white-space: nowrap;
}
.sidebar-footer {
    padding: 12px 16px;
    border-top: 1px solid rgba(255,255,255,0.05);
    font-size: 12px;
    color: #8b949e;
    text-align: center;
    backdrop-filter: blur(10px);
    transition: background 0.3s, color 0.3s;
}
/* ── Main content ────────────────────────────── */
.main {
    flex:1; display:flex; flex-direction:column; min-width:0;
    background: rgba(10,10,15,0.7);
    backdrop-filter: blur(10px);
    transition: background 0.3s ease;
}
/* ── Top bar with CSS grid for perfect centering ── */
.top-bar {
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    align-items: center;
    background: rgba(22, 27, 34, 0.7);
    backdrop-filter: blur(20px);
    border-bottom: 1px solid rgba(255,255,255,0.05);
    padding: 12px 24px;
    gap: 12px;
    flex-shrink: 0;
    box-shadow: 0 4px 12px rgba(0,0,0,0.3);
    transition: background 0.3s, border-color 0.3s;
}
.top-bar .left {
    display: flex;
    align-items: center;
    gap: 12px;
    justify-self: start;
}
.top-bar .left h1 {
    font-size: 19px;
    background: linear-gradient(135deg, #58a6ff, #a371f7);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    font-weight: 700;
}
/* ── Centered tab buttons (pill style) ── only Chat, Notes, Cork Board ── */
.top-bar .center-tabs {
    display: flex;
    gap: 4px;
    background: rgba(255,255,255,0.06);
    padding: 4px;
    border-radius: 30px;
    backdrop-filter: blur(5px);
    border: 1px solid rgba(255,255,255,0.06);
    justify-self: center;
}
.center-tabs .tab-btn {
    background: transparent;
    border: none;
    padding: 6px 18px;
    border-radius: 20px;
    font-size: 14px;
    font-weight: 500;
    color: #8b949e;
    cursor: pointer;
    transition: all 0.2s;
    white-space: nowrap;
    text-decoration: none;
    display: inline-block;
}
.center-tabs .tab-btn:hover {
    color: #c9d1d9;
    background: rgba(255,255,255,0.05);
}
.center-tabs .tab-btn.active {
    background: #1f6feb;
    color: #fff;
    box-shadow: 0 2px 8px rgba(31,111,235,0.3);
}
body.light-mode .center-tabs {
    background: rgba(0,0,0,0.04);
    border-color: rgba(0,0,0,0.06);
}
body.light-mode .center-tabs .tab-btn {
    color: #57606a;
}
body.light-mode .center-tabs .tab-btn:hover {
    background: rgba(0,0,0,0.04);
    color: #1f6feb;
}
body.light-mode .center-tabs .tab-btn.active {
    background: #1f6feb;
    color: #fff;
}
/* ── Right side of top bar ───────────────────── */
.top-bar .right {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    justify-self: end;
}
.sidebar-toggle {
    background: transparent;
    border: none;
    color: #8b949e;
    cursor: pointer;
    padding: 6px;
    transition: color 0.2s;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 6px;
    outline: none;
}
.sidebar-toggle:hover { color: #58a6ff; background: rgba(255,255,255,0.05); }
.model-select, .provider-select, .api-key-input {
    background: rgba(13, 17, 23, 0.8);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 10px;
    color: #e6edf3;
    padding: 6px 10px;
    font-size: 13px;
    outline: none;
    transition: border-color 0.2s, background 0.3s, color 0.3s;
    backdrop-filter: blur(5px);
}
.model-select:focus, .provider-select:focus, .api-key-input:focus {
    border-color: #58a6ff;
}
.api-key-input { display:none; }
.clear-btn, .unload-btn {
    background: rgba(33,38,45,0.7);
    border: 1px solid rgba(248,81,73,0.3);
    color: #f85149;
    border-radius: 10px;
    padding: 6px 14px;
    font-size: 12px;
    cursor: pointer;
    transition: all 0.2s;
    backdrop-filter: blur(5px);
}
.clear-btn:hover, .unload-btn:hover { background: rgba(248,81,73,0.15); border-color: #f85149; }
.unload-btn { display: none; }
.vision-badge {
    font-size: 11px;
    padding: 2px 10px;
    border-radius: 20px;
    background: rgba(63,185,80,0.15);
    border: 1px solid rgba(63,185,80,0.4);
    color: #3fb950;
    display: none;
    white-space: nowrap;
    backdrop-filter: blur(4px);
}
.vision-badge.visible { display:inline-block; }
/* ===== THEME TOGGLE (sliding) ===== */
.theme-toggle-wrapper { display: inline-block; vertical-align: middle; }
.toggle-outer {
    position: relative;
    width: 140px;
    height: 56px;
    border-radius: 999px;
    background: hsl(220 18% 82%);
    box-shadow: 2px 2px 8px rgba(0,0,0,0.12), -2px -2px 6px rgba(255,255,255,0.5),
                inset 1px 1px 3px rgba(0,0,0,0.08), inset -1px -1px 3px rgba(255,255,255,0.4);
    cursor: pointer;
    user-select: none;
    flex-shrink: 0;
}
.toggle-inner {
    position: absolute;
    inset: 5px;
    border-radius: 999px;
    overflow: hidden;
}
.night-bg { position: absolute; inset: 0; background: hsl(220 35% 18%); opacity:1; transition: opacity 0.3s ease; }
.stars-layer { position: absolute; inset: 0; opacity:1; transition: opacity 0.3s ease; pointer-events:none; }
.star { position: absolute; background: white; border-radius:50%; }
.sparkle { position: absolute; color: white; font-size: 7px; line-height:1; }
.day-bg { position: absolute; inset: 0; opacity:0; transition: opacity 0.3s ease; pointer-events:none; }
.sky-layer { position: absolute; inset: 0; background: hsl(205 70% 62%); }
.sky-mid { position: absolute; bottom:0; left:0; right:0; height:50%; background: hsl(205 60% 72%); border-radius: 40% 40% 0 0 / 30% 30% 0 0; }
.cloud { position: absolute; background: rgba(255,255,255,0.88); border-radius: 999px; }
.astronaut, .biplane {
    position: absolute;
    z-index: 4;
    pointer-events: none;
    transition: opacity 0.3s ease;
}
.astronaut {
    left: 48px;
    top: 50%;
    transform: translateY(-55%);
    width: 22px; height: 26px;
    opacity:1;
    animation: float 3s ease-in-out infinite;
}
.biplane {
    left: 44px;
    top: 38%;
    transform: translateY(-50%);
    width: 30px; height: 18px;
    opacity:0;
    animation: fly 3s ease-in-out infinite;
}
@keyframes float {
    0%,100% { transform: translateY(-55%); }
    50% { transform: translateY(-65%); }
}
@keyframes fly {
    0%,100% { transform: translateY(-50%) rotate(-1deg); }
    50% { transform: translateY(-60%) rotate(1deg); }
}
.knob {
    position: absolute;
    top: 50%;
    width: 40px;
    height: 40px;
    border-radius: 50%;
    transform: translateY(-50%);
    z-index: 10;
    cursor: grab;
    transition: left 0.4s cubic-bezier(0.34, 1.2, 0.64, 1);
    left: 3px;
}
.knob:active { cursor: grabbing; }
.knob-moon {
    position: absolute; inset:0; border-radius:50%;
    background: hsl(220 10% 82%);
    box-shadow: 2px 2px 4px rgba(255,255,255,0.9) inset, -2px -2px 4px rgba(0,0,0,0.18) inset;
    transition: opacity 0.3s ease;
}
.knob-moon .crater {
    position: absolute; border-radius:50%;
    background: hsl(220 8% 67%);
    box-shadow: 1px 1px 2px rgba(255,255,255,0.4) inset, -1px -1px 2px rgba(0,0,0,0.2) inset;
}
.knob-sun {
    position: absolute; inset:0; border-radius:50%;
    background: hsl(44 100% 58%);
    box-shadow: 2px 2px 6px rgba(255,255,180,0.9) inset, -2px -2px 4px rgba(180,100,0,0.3) inset,
                0 0 12px hsl(44 100% 70% / 0.5);
    opacity: 0;
    transition: opacity 0.3s ease;
}
.toggle-outer.day .night-bg { opacity: 0; }
.toggle-outer.day .stars-layer { opacity: 0; }
.toggle-outer.day .day-bg { opacity: 1; }
.toggle-outer.day .knob { left: 93px; }
.toggle-outer.day .knob-moon { opacity: 0; }
.toggle-outer.day .knob-sun { opacity: 1; }
.toggle-outer.day .astronaut { opacity: 0; }
.toggle-outer.day .biplane { opacity: 1; }
/* ── Chat panel ────────────────────────────────── */
.chat-panel {
    flex:1; display:flex; flex-direction:column; min-height:0;
}
.chat-area {
    flex:1; overflow-y:auto; padding: 24px 40px;
    display:flex; flex-direction:column; gap: 16px;
    will-change: transform;
    contain: layout style;
}
.chat-area::-webkit-scrollbar { width: 6px; }
.chat-area::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 3px; }
.msg {
    padding: 14px 20px;
    border-radius: 16px;
    max-width: 75%;
    line-height: 1.65;
    font-size: 15px;
    word-wrap: break-word;
    box-shadow: 0 4px 12px rgba(0,0,0,0.2);
    animation: fadeIn 0.3s ease;
    transition: background 0.3s, color 0.3s, border-color 0.3s, box-shadow 0.3s;
    position: relative;
}
@keyframes fadeIn {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
}
.msg.user {
    align-self: flex-end;
    background: linear-gradient(135deg, #1f6feb, #388bfd);
    color: white;
    border-bottom-right-radius: 4px;
}
.msg.bot {
    align-self: flex-start;
    background: rgba(28, 35, 51, 0.8);
    backdrop-filter: blur(10px);
    border: 1px solid rgba(255,255,255,0.08);
    border-bottom-left-radius: 4px;
}
.msg .ts {
    font-size: 10px;
    opacity: 0.5;
    margin-top: 8px;
    text-align: right;
}
.msg img {
    max-width: 240px; max-height: 240px;
    border-radius: 12px; display: block; margin-bottom: 10px;
    border: 1px solid rgba(255,255,255,0.1);
}
.msg .file-chip {
    display: inline-flex; align-items: center; gap: 6px;
    background: rgba(13,17,23,0.6);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 10px;
    padding: 6px 12px;
    font-size: 13px;
    margin-bottom: 8px;
    backdrop-filter: blur(5px);
}
.msg.bot table {
    border-collapse: collapse;
    width: 100%;
    margin: 12px 0;
    font-size: 14px;
}
.msg.bot th, .msg.bot td {
    border: 1px solid rgba(255,255,255,0.15);
    padding: 8px 12px;
    text-align: left;
}
.msg.bot th {
    background: rgba(255,255,255,0.08);
    font-weight: 600;
}
.msg.bot ul, .msg.bot ol {
    padding-left: 24px;
    margin: 8px 0;
}
.msg.bot li {
    margin: 4px 0;
}
.msg.bot strong, .msg.bot b {
    font-weight: 700;
    color: #58a6ff;
}
.msg.bot code {
    background: rgba(255,255,255,0.1);
    padding: 0 4px;
    border-radius: 4px;
    font-family: monospace;
}
body.light-mode .msg.bot th {
    background: rgba(0,0,0,0.05);
}
body.light-mode .msg.bot strong,
body.light-mode .msg.bot b {
    color: #1f6feb;
}
body.light-mode .msg.bot code {
    background: rgba(0,0,0,0.06);
}
/* ===== CODE BLOCK ENHANCEMENTS ===== */
.code-block-wrapper {
    background: #1e1e1e;
    border-radius: 8px;
    margin: 12px 0;
    overflow: hidden;
    border: 1px solid rgba(255,255,255,0.1);
    box-shadow: 0 4px 12px rgba(0,0,0,0.3);
    font-size: 0;
}
.code-header {
    background: #2d2d2d;
    padding: 6px 12px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid rgba(255,255,255,0.05);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 12px;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
}
.language-label {
    color: #8b949e;
    text-transform: uppercase;
    font-weight: 600;
    letter-spacing: 0.5px;
}
.copy-code-btn {
    background: rgba(255,255,255,0.08);
    border: none;
    color: #c9d1d9;
    padding: 4px 12px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 11px;
    font-weight: 500;
    transition: all 0.2s ease;
    font-family: inherit;
}
.copy-code-btn:hover { background: rgba(255,255,255,0.15); color: #fff; }
.copy-code-btn.copied { background: #3fb950; color: #fff; }
.code-block-wrapper pre {
    margin: 0 !important;
    padding: 16px !important;
    border-radius: 0 !important;
    background: #1e1e1e !important;
}
.code-block-wrapper pre code {
    font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace !important;
    font-size: 13px !important;
    line-height: 1.6 !important;
}
body.light-mode .code-block-wrapper {
    background: #f6f8fa;
    border-color: rgba(0,0,0,0.1);
    box-shadow: 0 2px 8px rgba(0,0,0,0.05);
}
body.light-mode .code-header {
    background: #e1e4e8;
    border-bottom-color: rgba(0,0,0,0.08);
}
body.light-mode .language-label { color: #57606a; }
body.light-mode .copy-code-btn { background: rgba(0,0,0,0.05); color: #24292f; }
body.light-mode .copy-code-btn:hover { background: rgba(0,0,0,0.1); }
/* ========================================================== */
.msg .msg-actions {
    display: none;
    position: absolute;
    top: 4px;
    right: 10px;
    gap: 6px;
}
.msg:hover .msg-actions {
    display: flex;
}
.msg .edit-btn, .msg .delete-btn {
    background: rgba(255,255,255,0.1);
    border: none;
    color: #8b949e;
    font-size: 14px;
    cursor: pointer;
    padding: 2px 6px;
    border-radius: 6px;
    transition: 0.2s;
}
.msg .edit-btn:hover { color: #58a6ff; background: rgba(88,166,255,0.15); }
.msg .delete-btn:hover { color: #f85149; background: rgba(248,81,73,0.15); }
.edit-textarea {
    width: 100%;
    background: rgba(13,17,23,0.9);
    color: #e1e4e8;
    border: 1px solid #58a6ff;
    border-radius: 8px;
    padding: 8px;
    font-size: inherit;
    resize: vertical;
}
.msg.user .msg-actions {
    display: flex !important;
}
.msg.bot .msg-actions {
    display: none !important;
}
.attachments {
    display:flex; flex-wrap:wrap; gap: 8px;
    padding: 0 40px 10px;
    background: transparent;
}
.att-thumb {
    position:relative; display:inline-flex; align-items:center;
    background: rgba(13,17,23,0.7);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 10px;
    padding: 6px 10px; gap: 8px; font-size: 12px; color: #8b949e;
    backdrop-filter: blur(5px);
}
.att-thumb img { height: 44px; border-radius: 8px; }
.att-thumb .remove {
    background: #f85149; color: white; border: none; border-radius: 50%;
    width: 18px; height: 18px; font-size: 11px; cursor: pointer;
    line-height: 18px; text-align: center; flex-shrink: 0;
}
.input-bar {
    background: rgba(22, 27, 34, 0.7);
    backdrop-filter: blur(20px);
    border-top: 1px solid rgba(255,255,255,0.05);
    padding: 14px 40px 18px;
    display: flex; gap: 10px;
    align-items: flex-end; flex-shrink: 0;
    transition: background 0.3s, border-color 0.3s;
}
.search-toggle-btn {
    background: rgba(33,38,45,0.6);
    border: 1px solid rgba(255,255,255,0.1);
    color: #8b949e;
    border-radius: 12px;
    width: 46px;
    height: 46px;
    font-size: 20px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    transition: all 0.2s;
    backdrop-filter: blur(5px);
}
.search-toggle-btn:hover {
    color: #58a6ff;
    border-color: #58a6ff;
    background: rgba(88,166,255,0.1);
}
.search-toggle-btn.active {
    border-color: #3fb950;
    color: #3fb950;
    background: rgba(63,185,80,0.15);
}
.attach-btn, .record-btn, .voice-toggle {
    background: rgba(33,38,45,0.6);
    border: 1px solid rgba(255,255,255,0.1);
    color: #8b949e;
    border-radius: 12px;
    width: 46px; height: 46px;
    font-size: 20px;
    cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    flex-shrink: 0;
    transition: all 0.2s;
    backdrop-filter: blur(5px);
}
.attach-btn:hover, .record-btn:hover, .voice-toggle:hover {
    color: #58a6ff;
    border-color: #58a6ff;
    background: rgba(88,166,255,0.1);
}
.voice-toggle.active {
    color: #3fb950;
    border-color: #3fb950;
    background: rgba(63,185,80,0.15);
}
#stopSpeakBtn {
    color: #f85149;
    display: none;
}
#stopSpeakBtn:hover {
    border-color: #f85149;
    background: rgba(248,81,73,0.15);
}
#msgInput {
    flex:1;
    background: rgba(13, 17, 23, 0.7);
    backdrop-filter: blur(10px);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 14px;
    color: #e6edf3;
    font-size: 15px;
    padding: 12px 16px;
    resize: none;
    font-family: inherit;
    min-height: 46px;
    max-height: 140px;
    height: 46px;
    overflow-y: hidden;
    outline: none;
    transition: border-color 0.2s, background 0.3s, color 0.3s;
    will-change: height;
}
#msgInput:focus { border-color: #58a6ff; }
.input-bar .model-select {
    background: rgba(13, 17, 23, 0.8);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 12px;
    color: #e6edf3;
    padding: 4px 8px;
    font-size: 13px;
    height: 46px;
    min-width: 120px;
    max-width: 180px;
    outline: none;
    transition: border-color 0.2s, background 0.3s, color 0.3s;
    backdrop-filter: blur(5px);
    cursor: pointer;
}
.input-bar .model-select:focus {
    border-color: #58a6ff;
}
#sendBtn {
    background: linear-gradient(135deg, #1f6feb, #388bfd);
    color: white;
    border: none;
    border-radius: 14px;
    padding: 0 28px;
    font-size: 15px;
    font-weight: 600;
    cursor: pointer;
    height: 46px;
    white-space: nowrap;
    flex-shrink: 0;
    box-shadow: 0 4px 12px rgba(31,111,235,0.4);
    transition: all 0.2s;
}
#sendBtn:hover { box-shadow: 0 6px 16px rgba(31,111,235,0.6); transform: translateY(-1px); }
#sendBtn:disabled { opacity: 0.5; cursor: not-allowed; }
#statusBar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 6px 40px;
    background: rgba(22, 27, 34, 0.6);
    backdrop-filter: blur(10px);
    border-top: 1px solid rgba(255,255,255,0.05);
    font-size: 12px;
    color: #8b949e;
    flex-shrink: 0;
    transition: background 0.3s, color 0.3s, border-color 0.3s;
}
#resourceDisplay {
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 11px;
    background: rgba(13,17,23,0.6);
    padding: 2px 12px;
    border-radius: 20px;
    border: 1px solid rgba(255,255,255,0.05);
    transition: background 0.3s, border-color 0.3s;
}
#tokenSpeed {
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 11px;
    margin-left: 12px;
    color: #3fb950;
}
.record-btn.recording {
    background: #f85149;
    color: white;
    border-color: #f85149;
    animation: pulse 1.2s infinite;
}
@keyframes pulse {
    0% { box-shadow: 0 0 0 0 rgba(248,81,73,0.5); }
    70% { box-shadow: 0 0 0 10px rgba(248,81,73,0); }
    100% { box-shadow: 0 0 0 0 rgba(248,81,73,0); }
}
.thinking-dots::after {
    content: '';
    animation: dots 1.4s infinite;
}
@keyframes dots {
    0%   { content: ''; }
    25%  { content: '.'; }
    50%  { content: '..'; }
    75%  { content: '...'; }
    100% { content: ''; }
}
#scrollBottomBtn {
    position: fixed;
    bottom: 100px;
    right: 20px;
    display: none;
    z-index: 10;
    border-radius: 50%;
    width: 48px;
    height: 48px;
    background: #1f6feb;
    color: #fff;
    border: none;
    font-size: 24px;
    cursor: pointer;
    box-shadow: 0 4px 12px rgba(0,0,0,0.4);
    transition: transform 0.2s;
}
#scrollBottomBtn:hover { transform: scale(1.05); }
#dropOverlay {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.75);
    backdrop-filter: blur(8px);
    z-index: 9999;
    display: none;
    align-items: center;
    justify-content: center;
    flex-direction: column;
    color: white;
    font-size: 24px;
    pointer-events: none;
}
#dropOverlay.active {
    display: flex;
    pointer-events: auto;
}
#dropOverlay .icon {
    font-size: 64px;
    margin-bottom: 20px;
}
#dropOverlay .sub {
    font-size: 16px;
    opacity: 0.7;
    margin-top: 10px;
}
body.light-mode #dropOverlay {
    background: rgba(255,255,255,0.85);
    color: #24292f;
}
/* ===== LIGHT MODE OVERRIDES ===== */
body.light-mode {
    background: #f6f8fa;
    color: #24292f;
}
body.light-mode .sidebar {
    background: rgba(255, 255, 255, 0.92);
    border-right-color: rgba(0,0,0,0.08);
}
body.light-mode .sidebar .sidebar-header {
    border-bottom-color: rgba(0,0,0,0.06);
}
body.light-mode .sidebar .group-heading {
    color: #57606a;
    border-bottom-color: rgba(0,0,0,0.06);
}
body.light-mode .conv-item:hover {
    background: rgba(0,0,0,0.04);
}
body.light-mode .conv-item.active {
    background: rgba(31,111,235,0.12);
    border-color: rgba(31,111,235,0.3);
}
body.light-mode .conv-item .title {
    color: #24292f;
}
body.light-mode .conv-item .time {
    color: #57606a;
}
body.light-mode .conv-item .rename-btn {
    color: #57606a;
}
body.light-mode .sidebar-footer {
    color: #57606a;
    border-top-color: rgba(0,0,0,0.06);
}
body.light-mode .main {
    background: rgba(255,255,255,0.85);
}
body.light-mode .top-bar {
    background: rgba(255, 255, 255, 0.9);
    border-bottom-color: rgba(0,0,0,0.08);
    box-shadow: 0 1px 4px rgba(0,0,0,0.05);
}
body.light-mode .top-bar .left h1 {
    background: linear-gradient(135deg, #1f6feb, #a371f7);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
}
body.light-mode .model-select,
body.light-mode .provider-select,
body.light-mode .api-key-input {
    background: rgba(255,255,255,0.8);
    color: #24292f;
    border-color: rgba(0,0,0,0.15);
}
body.light-mode .clear-btn,
body.light-mode .unload-btn {
    background: rgba(0,0,0,0.05);
    border-color: rgba(248,81,73,0.3);
    color: #f85149;
}
body.light-mode .clear-btn:hover,
body.light-mode .unload-btn:hover {
    background: rgba(248,81,73,0.08);
}
body.light-mode .search-box input {
    background: rgba(255,255,255,0.8);
    color: #24292f;
    border-color: rgba(0,0,0,0.12);
}
body.light-mode .search-box input::placeholder {
    color: #8b949e;
}
body.light-mode .msg.bot {
    background: rgba(240, 243, 246, 0.9);
    border-color: rgba(0,0,0,0.06);
    color: #24292f;
    box-shadow: 0 2px 8px rgba(0,0,0,0.06);
}
body.light-mode .msg.user {
    background: linear-gradient(135deg, #1f6feb, #388bfd);
    color: white;
}
body.light-mode .input-bar {
    background: rgba(255, 255, 255, 0.9);
    border-top-color: rgba(0,0,0,0.06);
}
body.light-mode .attach-btn,
body.light-mode .record-btn,
body.light-mode .voice-toggle,
body.light-mode .search-toggle-btn {
    background: rgba(255,255,255,0.6);
    border-color: rgba(0,0,0,0.1);
    color: #57606a;
}
body.light-mode .attach-btn:hover,
body.light-mode .record-btn:hover,
body.light-mode .voice-toggle:hover,
body.light-mode .search-toggle-btn:hover {
    color: #1f6feb;
    border-color: #1f6feb;
    background: rgba(31,111,235,0.05);
}
body.light-mode .search-toggle-btn.active {
    border-color: #1e7e34;
    color: #1e7e34;
    background: rgba(63,185,80,0.12);
}
body.light-mode .input-bar .model-select {
    background: rgba(255,255,255,0.8);
    color: #24292f;
    border-color: rgba(0,0,0,0.15);
}
body.light-mode .input-bar .model-select:focus {
    border-color: #1f6feb;
}
body.light-mode #msgInput {
    background: rgba(255,255,255,0.8);
    color: #24292f;
    border-color: rgba(0,0,0,0.12);
}
body.light-mode #statusBar {
    background: rgba(255, 255, 255, 0.85);
    color: #57606a;
    border-top-color: rgba(0,0,0,0.06);
}
body.light-mode #resourceDisplay {
    background: rgba(0,0,0,0.04);
    border-color: rgba(0,0,0,0.06);
    color: #57606a;
}
body.light-mode .att-thumb {
    background: rgba(255,255,255,0.8);
    border-color: rgba(0,0,0,0.08);
    color: #24292f;
}
body.light-mode .msg .file-chip {
    background: rgba(0,0,0,0.04);
    border-color: rgba(0,0,0,0.06);
}
body.light-mode .chat-area::-webkit-scrollbar-thumb {
    background: rgba(0,0,0,0.12);
}
body.light-mode #scrollBottomBtn {
    background: #1f6feb;
    color: #fff;
}
body.light-mode .vision-badge {
    background: rgba(63,185,80,0.12);
    border-color: rgba(63,185,80,0.25);
    color: #1e7e34;
}

/* ===== WEATHER WIDGET STYLES – COMPACT (same as Notes/Cork Board) ===== */
.toast-container {
    position: fixed;
    top: 70px;
    right: 16px;
    z-index: 99999;
    display: flex;
    flex-direction: column;
    gap: 12px;
    pointer-events: none;
    max-width: 280px;
    width: auto;
}
.toast {
    background: rgba(22, 27, 34, 0.94);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 18px;
    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.7);
    color: #e1e4e8;
    pointer-events: auto;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    padding: 0;
    position: relative;
    min-width: 0;
    transform: translateX(0);
    opacity: 1;
    transition: opacity 0.5s ease;
}
.toast.show {
    opacity: 1;
}
.toast-scene {
    height: 90px;
    flex-shrink: 0;
    background: #1a1a2e;
    position: relative;
    overflow: hidden;
}
.toast-canvas {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    pointer-events: none;
    display: block;
}
.toast-scene::after {
    content: '';
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    height: 20px;
    background: linear-gradient(to bottom, rgba(22, 27, 34, 0), rgba(22, 27, 34, 0.95));
    pointer-events: none;
    z-index: 2;
}
.toast-content {
    position: relative;
    z-index: 3;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 14px 10px 12px;
}
.toast-icon {
    width: 32px;
    height: 32px;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 20px;
    border-radius: 50%;
    background: rgba(0,0,0,0.4);
}
.toast-text {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 0px;
    min-width: 0;
}
.toast-text .main {
    font-size: 12px;
    line-height: 1.2;
    font-weight: 500;
    color: #e1e4e8;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.toast-text .main .highlight {
    font-weight: 700;
    color: #fff;
}
.toast-text .sub {
    font-size: 10px;
    color: rgba(255,255,255,0.6);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    opacity: 0;
    transition: opacity 0.4s ease;
}
.toast-text .sub.visible {
    opacity: 1;
}
.toast-text .time-row {
    display: flex;
    align-items: baseline;
    gap: 4px;
    margin-top: 0;
}
.toast-text .time-row .clock {
    font-size: 14px;
    font-weight: 700;
    color: #fff;
    letter-spacing: 0.3px;
    font-variant-numeric: tabular-nums;
}
.toast-text .time-row .date {
    font-size: 9px;
    color: rgba(255,255,255,0.5);
}
.toast-text .weather-row {
    display: flex;
    align-items: center;
    gap: 4px;
    font-size: 11px;
    color: rgba(255,255,255,0.8);
}
.toast-text .weather-row .temp {
    font-weight: 700;
    font-size: 13px;
    color: #fff;
}
.toast-text .weather-row .condition {
    font-size: 10px;
    color: rgba(255,255,255,0.6);
}
.toast-text .weather-row .weather-emoji {
    font-size: 14px;
}
.toast-text .fetch-status {
    font-size: 9px;
    color: rgba(255,255,255,0.4);
    font-style: italic;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.toast-progress {
    position: absolute;
    bottom: 0;
    left: 0;
    width: 0%;
    height: 3px;
    background: linear-gradient(90deg, #58a6ff, #3fb950);
    border-radius: 0;
    transition: width 0.4s ease;
    z-index: 3;
    pointer-events: none;
    max-width: 100%;
    will-change: width;
}
.toast .close-btn {
    position: absolute;
    top: 4px;
    right: 4px;
    background: rgba(0, 0, 0, 0.35);
    border: none;
    color: rgba(255, 255, 255, 0.7);
    cursor: pointer;
    font-size: 12px;
    padding: 2px 6px;
    border-radius: 20px;
    transition: background 0.2s;
    line-height: 1;
    z-index: 4;
}
.toast .close-btn:hover {
    background: rgba(255, 0, 0, 0.35);
    color: #fff;
}
.spinner {
    width: 18px;
    height: 18px;
    border: 2px solid rgba(255,255,255,0.15);
    border-top-color: #fff;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
}
@keyframes spin {
    to { transform: rotate(360deg); }
}
.weather-toggle-btn {
    background: rgba(33,38,45,0.6);
    border: 1px solid rgba(255,255,255,0.1);
    color: #8b949e;
    border-radius: 12px;
    width: 46px;
    height: 46px;
    font-size: 22px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all 0.2s;
    backdrop-filter: blur(5px);
}
.weather-toggle-btn:hover {
    color: #58a6ff;
    border-color: #58a6ff;
    background: rgba(88,166,255,0.1);
}
body.light-mode .weather-toggle-btn {
    background: rgba(255,255,255,0.6);
    border-color: rgba(0,0,0,0.1);
    color: #57606a;
}
body.light-mode .weather-toggle-btn:hover {
    color: #1f6feb;
    border-color: #1f6feb;
    background: rgba(31,111,235,0.05);
}
.weather-country-select {
    background: rgba(13,17,23,0.8);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 10px;
    color: #e6edf3;
    padding: 6px 10px;
    font-size: 13px;
    outline: none;
    transition: border-color 0.2s, background 0.3s, color 0.3s;
    backdrop-filter: blur(5px);
    max-width: 160px;
    cursor: pointer;
}
.weather-country-select:focus {
    border-color: #58a6ff;
}
body.light-mode .weather-country-select {
    background: rgba(255,255,255,0.8);
    color: #24292f;
    border-color: rgba(0,0,0,0.15);
}
body.light-mode .weather-country-select:focus {
    border-color: #1f6feb;
}
.weather-country-select option {
    background: #1a1a2e;
    color: #e1e4e8;
}
body.light-mode .weather-country-select option {
    background: #fff;
    color: #24292f;
}
</style>
</head>
<body>
<div class="app">
  <div class="sidebar" id="sidebar">
    <div class="sidebar-header">
      <h2>💬 Chats</h2>
      <button class="new-chat-btn" onclick="newChat()">+ New</button>
    </div>
    <div class="search-box">
      <input type="text" id="searchInput" placeholder="🔍 Search chats... (title & messages)" oninput="searchChats()">
    </div>
    <div class="conv-list" id="convList"></div>
    <div class="sidebar-footer">Drag to reorder · ✏️ to rename</div>
  </div>

  <div class="main">
    <!-- TOP BAR WITH CENTER TABS -->
    <div class="top-bar">
      <div class="left">
        <button class="sidebar-toggle" onclick="toggleSidebar()" title="Toggle sidebar">
          <svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect>
            <line x1="9" y1="3" x2="9" y2="21"></line>
          </svg>
        </button>
        <h1>🧠 Trio-Forge Custom Chat</h1>
      </div>

      <!-- CENTER TABS – only Chat, Notes, Cork Board -->
      <div class="center-tabs">
        <button class="tab-btn active">💬 Chat</button>
        <a href="/notes" class="tab-btn" style="text-decoration:none;">📝 Notes</a>
        <a href="/corkboard" class="tab-btn" style="text-decoration:none;">📌 Cork Board</a>
      </div>

      <div class="right">
        <div class="theme-toggle-wrapper">
          <div class="toggle-outer" id="themeToggleOuter" onclick="handleThemeClick(event)">
            <div class="toggle-inner">
              <div class="night-bg"></div>
              <div class="stars-layer" id="themeStars"></div>
              <div class="day-bg">
                <div class="sky-layer"></div>
                <div class="sky-mid"></div>
                <div class="cloud" style="width:36px;height:14px;bottom:3px;right:0px;"></div>
                <div class="cloud" style="width:26px;height:10px;bottom:14px;right:22px;opacity:.85;"></div>
                <div class="cloud" style="width:20px;height:8px;bottom:22px;left:4px;opacity:.7;"></div>
              </div>
              <div class="astronaut">
                <svg viewBox="0 0 44 54" width="22" height="26" xmlns="http://www.w3.org/2000/svg">
                  <ellipse cx="22" cy="36" rx="13" ry="14" fill="#e8e8e8"/>
                  <circle cx="22" cy="18" r="13" fill="#d0d8e8"/>
                  <circle cx="22" cy="18" r="10" fill="#c8d8f0" opacity="0.4"/>
                  <ellipse cx="22" cy="19" rx="7" ry="6" fill="#5a7ab0" opacity="0.85"/>
                  <circle cx="22" cy="20" r="5" fill="#c8844a"/>
                  <circle cx="20" cy="18.5" r="1.2" fill="#7a3a0a"/>
                  <circle cx="24" cy="18.5" r="1.2" fill="#7a3a0a"/>
                  <ellipse cx="22" cy="21" rx="2" ry="1.2" fill="#b06030"/>
                  <circle cx="10" cy="11" r="3.5" fill="#d0d8e8"/>
                  <circle cx="34" cy="11" r="3.5" fill="#d0d8e8"/>
                  <text x="22" y="37" text-anchor="middle" font-size="8" fill="#bbb">★</text>
                  <ellipse cx="9" cy="36" rx="4" ry="8" fill="#e0e0e0" transform="rotate(-10 9 36)"/>
                  <ellipse cx="35" cy="36" rx="4" ry="8" fill="#e0e0e0" transform="rotate(10 35 36)"/>
                  <ellipse cx="16" cy="49" rx="5" ry="5" fill="#d0d0d0"/>
                  <ellipse cx="28" cy="49" rx="5" ry="5" fill="#d0d0d0"/>
                  <ellipse cx="16" cy="52" rx="6" ry="3" fill="#b0b0b8"/>
                  <ellipse cx="28" cy="52" rx="6" ry="3" fill="#b0b0b8"/>
                  <ellipse cx="22" cy="28" rx="9" ry="3" fill="none" stroke="#c0c8d8" stroke-width="2"/>
                </svg>
              </div>
              <div class="biplane">
                <svg viewBox="0 0 70 42" width="30" height="18" xmlns="http://www.w3.org/2000/svg">
                  <rect x="14" y="4" width="42" height="8" rx="4" fill="#d0d8e0"/>
                  <ellipse cx="35" cy="22" rx="22" ry="9" fill="#e8e0d8"/>
                  <ellipse cx="58" cy="22" rx="8" ry="6" fill="#d0c8c0"/>
                  <polygon points="8,14 14,20 8,26" fill="#c8d0d8"/>
                  <rect x="4" y="15" width="12" height="5" rx="2" fill="#c0c8d0"/>
                  <rect x="18" y="26" width="34" height="6" rx="3" fill="#c8d0d8"/>
                  <line x1="22" y1="12" x2="22" y2="26" stroke="#aab0b8" stroke-width="1.5"/>
                  <line x1="48" y1="12" x2="48" y2="26" stroke="#aab0b8" stroke-width="1.5"/>
                  <ellipse cx="44" cy="17" rx="7" ry="5" fill="#7aaecc" opacity="0.8"/>
                  <circle cx="44" cy="15" r="5" fill="#c8844a"/>
                  <circle cx="42.5" cy="13.5" r="1" fill="#6b3a1f"/>
                  <circle cx="45.5" cy="13.5" r="1" fill="#6b3a1f"/>
                  <ellipse cx="44" cy="16" rx="1.5" ry="1" fill="#b06030"/>
                  <circle cx="40" cy="11" r="2" fill="#c8844a"/>
                  <circle cx="48" cy="11" r="2" fill="#c8844a"/>
                  <line x1="66" y1="13" x2="66" y2="31" stroke="#8a7060" stroke-width="3" stroke-linecap="round"/>
                  <circle cx="66" cy="22" r="2.5" fill="#6a5040"/>
                </svg>
              </div>
              <div class="knob" id="themeKnob">
                <div class="knob-moon">
                  <div class="crater" style="width:10px;height:10px;top:8px;left:7px;"></div>
                  <div class="crater" style="width:8px;height:8px;top:22px;left:11px;"></div>
                  <div class="crater" style="width:5px;height:5px;top:18px;left:25px;"></div>
                </div>
                <div class="knob-sun"></div>
              </div>
            </div>
          </div>
        </div>

        <select id="providerSelect" class="provider-select">
          <option value="ollama">Ollama</option>
          <option value="llamacpp">llama.cpp</option>
          <option value="huggingface">Hugging Face</option>
          <option value="groq">Groq</option>
          <option value="deepseek">DeepSeek</option>
          <option value="claude">Claude (Anthropic)</option>
        </select>
        <input type="password" id="apiKeyInput" class="api-key-input" placeholder="Enter API Key">
        <button class="unload-btn" id="unloadBtn" title="Unload current Ollama model from memory">🗑 Unload</button>
        <span id="visionBadge" class="vision-badge">👁 Vision</span>
        <button class="clear-btn" onclick="clearAllChats()" title="Clears only the currently open chat's messages">🗑 Clear Chat</button>
        <span id="deepseekStatus" style="font-size:12px; margin-left:10px;"></span>
        <span id="modelInfo" style="display:none;"></span>

        <!-- Weather toggle and country dropdown -->
        <button class="weather-toggle-btn" id="weatherToggleBtn" onclick="toggleWeather()" title="Show/hide weather">🌤️</button>
        <select id="weatherCountrySelect" class="weather-country-select" aria-label="Select country"></select>
      </div>
    </div>

    <!-- CHAT PANEL -->
    <div id="chatPanel" class="chat-panel">
      <div class="chat-area" id="chatArea">
        <div class="msg bot">👋 Hello! Select or create a chat from the sidebar.
        <br>You can also type <code>ollama pull &lt;model&gt;</code>, <code>ollama list</code>, etc.</div>
      </div>
      <div class="attachments" id="attachments"></div>
      <div class="input-bar">
        <button class="search-toggle-btn" id="searchToggleBtn" title="Toggle web search">🔍</button>
        <button class="attach-btn" title="Attach image or file" onclick="document.getElementById('fileInput').click()">📎</button>
        <input type="file" id="fileInput" accept="image/*,.pdf,.txt,.md,.py,.js,.csv,.json,.c,.cpp,.h,.hpp" multiple style="display:none"/>
        <textarea id="msgInput" placeholder="Type your message... (Enter to send, Shift+Enter for new line)"></textarea>
        <select id="modelSelect" class="model-select" title="Select model"></select>
        <button id="recordBtn" class="record-btn" title="Click to record voice input">🎤</button>
        <button id="speakToggleBtn" class="voice-toggle" title="Toggle AI voice output" onclick="toggleVoice()">🔊</button>
        <button id="stopSpeakBtn" class="voice-toggle" title="Stop speaking" style="display:none;" onclick="stopSpeaking()">⏹️</button>
        <button id="sendBtn">Send</button>
      </div>
    </div>

    <!-- Weather Toast Container -->
    <div class="toast-container" id="toastContainer"></div>

    <div id="statusBar">
      <span id="status">✅ Ready</span>
      <span id="resourceDisplay">💾 RAM: -- | 🎮 VRAM: --</span>
      <span id="tokenSpeed">⏱️ 0 tok/s | 0 tokens</span>
    </div>
  </div>
</div>

<button id="scrollBottomBtn" title="Scroll to bottom">↓</button>
<div id="dropOverlay">
  <div class="icon">📂</div>
  <div>Drop files or folders here</div>
  <div class="sub">We'll read your documents for study assistance</div>
</div>

<script>
/* ─── CHAT JAVASCRIPT (existing) ────────────────────────────── */
var chatArea    = document.getElementById('chatArea');
var msgInput    = document.getElementById('msgInput');
var sendBtn     = document.getElementById('sendBtn');
var status      = document.getElementById('status');
var fileInput   = document.getElementById('fileInput');
var attachments = document.getElementById('attachments');
var convList    = document.getElementById('convList');
var modelSelect = document.getElementById('modelSelect');
var providerSelect = document.getElementById('providerSelect');
var apiKeyInput = document.getElementById('apiKeyInput');
var visionBadge = document.getElementById('visionBadge');
var busy        = false;
var currentConv = null;
var pending     = [];
var conversations = [];
var searchQuery = '';
var searchEnabled = true;
var unloadBtn = document.getElementById('unloadBtn');

function smoothScrollToBottom() {
    chatArea.scrollTop = chatArea.scrollHeight;
}

var resizePending = false;
msgInput.addEventListener('input', function() {
    if (resizePending) return;
    resizePending = true;
    requestAnimationFrame(function() {
        resizePending = false;
        var el = msgInput;
        el.style.height = 'auto';
        el.style.height = Math.min(el.scrollHeight, 140) + 'px';
    });
});

function processCodeBlocks(root) {
    var codeBlocks = root.querySelectorAll('pre code');
    if (codeBlocks.length === 0) return;
    codeBlocks.forEach(function(codeBlock) {
        var parentPre = codeBlock.parentElement;
        if (parentPre.classList.contains('processed')) return;
        parentPre.classList.add('processed');
        var lang = '';
        var classMatch = codeBlock.className.match(/language-(\w+)/);
        if (classMatch) lang = classMatch[1];
        if (!lang) lang = 'code';
        var wrapper = document.createElement('div');
        wrapper.className = 'code-block-wrapper';
        var header = document.createElement('div');
        header.className = 'code-header';
        var label = document.createElement('span');
        label.className = 'language-label';
        label.textContent = lang;
        var copyBtn = document.createElement('button');
        copyBtn.className = 'copy-code-btn';
        copyBtn.textContent = 'Copy';
        copyBtn.onclick = function(e) {
            e.stopPropagation();
            var text = codeBlock.textContent;
            if (navigator.clipboard) {
                navigator.clipboard.writeText(text).then(function() {
                    copyBtn.textContent = 'Copied!';
                    copyBtn.classList.add('copied');
                    setTimeout(function() {
                        copyBtn.textContent = 'Copy';
                        copyBtn.classList.remove('copied');
                    }, 2500);
                }, function() {
                    fallbackCopy(text, copyBtn);
                });
            } else {
                fallbackCopy(text, copyBtn);
            }
        };
        function fallbackCopy(text, btn) {
            var textarea = document.createElement('textarea');
            textarea.value = text;
            document.body.appendChild(textarea);
            textarea.select();
            try {
                document.execCommand('copy');
                btn.textContent = 'Copied!';
                btn.classList.add('copied');
                setTimeout(function() {
                    btn.textContent = 'Copy';
                    btn.classList.remove('copied');
                }, 2500);
            } catch (err) {
                btn.textContent = 'Error';
            }
            document.body.removeChild(textarea);
        }
        header.appendChild(label);
        header.appendChild(copyBtn);
        parentPre.parentNode.insertBefore(wrapper, parentPre);
        wrapper.appendChild(header);
        wrapper.appendChild(parentPre);
        if (window.hljs) {
            hljs.highlightElement(codeBlock);
        }
    });
}

var MERMAID_DECLARATION_RE = /^\s*(graph|flowchart|sequenceDiagram|classDiagram|stateDiagram(-v2)?|erDiagram|journey|gantt|pie|gitGraph|mindmap|timeline|quadrantChart|requirementDiagram|C4Context|C4Container|C4Component|C4Dynamic|C4Deployment|sankey-beta|block-beta|xychart-beta)\b/i;

function ensureMermaidDeclaration(text) {
    var trimmed = text.replace(/^\s+/, '');
    if (MERMAID_DECLARATION_RE.test(trimmed)) {
        return text;
    }
    if (/--[->.]|===>/.test(trimmed)) {
        return 'graph LR\n' + text;
    }
    return text;
}

function renderMermaidDiagrams(root) {
    var mermaidBlocks = root.querySelectorAll('pre code.language-mermaid');
    if (mermaidBlocks.length === 0) return;
    mermaidBlocks.forEach(function(code) {
        var pre = code.parentElement;
        var div = document.createElement('div');
        div.className = 'mermaid';
        div.textContent = ensureMermaidDeclaration(code.textContent);
        pre.replaceWith(div);
    });
    if (mermaidBlocks.length > 0) {
        if (window.mermaid) {
            mermaid.run({ nodes: root.querySelectorAll('.mermaid') });
        }
    }
}

function checkDeepSeekStatus() {
    const statusSpan = document.getElementById('deepseekStatus');
    if (providerSelect.value !== 'deepseek') {
        statusSpan.textContent = '';
        return;
    }
    fetch('/deepseek/status')
        .then(r => r.json())
        .then(data => {
            if (data.ok) {
                statusSpan.textContent = '✅ ' + (data.message || 'API online');
                statusSpan.style.color = '#3fb950';
            } else {
                statusSpan.textContent = '⚠️ ' + (data.message || 'API unavailable');
                statusSpan.style.color = '#f85149';
            }
        })
        .catch(() => {
            statusSpan.textContent = '⚠️ Status check failed';
            statusSpan.style.color = '#f85149';
        });
}

unloadBtn.addEventListener('click', function() {
    if (!confirm('Unload current Ollama model from memory?')) return;
    fetch('/unload_model', { method: 'POST' })
        .then(() => {
            status.textContent = '✅ Model unloaded (memory freed)';
        })
        .catch(() => status.textContent = '❌ Unload failed');
});

var dropOverlay = document.getElementById('dropOverlay');
var dragCounter = 0;
function showDropOverlay() { dropOverlay.classList.add('active'); }
function hideDropOverlay() { dropOverlay.classList.remove('active'); }
function hasFiles(e) {
    if (!e.dataTransfer) return false;
    if (e.dataTransfer.types) {
        for (var i = 0; i < e.dataTransfer.types.length; i++) {
            if (e.dataTransfer.types[i] === 'Files') return true;
        }
    }
    if (e.dataTransfer.items) {
        for (var i = 0; i < e.dataTransfer.items.length; i++) {
            if (e.dataTransfer.items[i].kind === 'file') return true;
        }
    }
    return false;
}
document.addEventListener('dragenter', function(e) {
    e.preventDefault();
    if (!hasFiles(e)) return;
    dragCounter++;
    if (dragCounter === 1) showDropOverlay();
});
document.addEventListener('dragover', function(e) {
    e.preventDefault();
    if (!hasFiles(e)) return;
});
document.addEventListener('dragleave', function(e) {
    e.preventDefault();
    if (!hasFiles(e)) return;
    dragCounter--;
    if (dragCounter === 0) hideDropOverlay();
});
document.addEventListener('drop', function(e) {
    e.preventDefault();
    dragCounter = 0;
    hideDropOverlay();
    if (!hasFiles(e)) return;
    var items = e.dataTransfer.items;
    if (items) {
        processDropItems(items);
    } else {
        var files = e.dataTransfer.files;
        if (files && files.length) {
            for (var i = 0; i < files.length; i++) {
                addDroppedFile(files[i]);
            }
        }
    }
});

function processDropItems(items) {
    var entries = [];
    for (var i = 0; i < items.length; i++) {
        var item = items[i];
        var entry = item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
        if (entry) entries.push(entry);
    }
    if (entries.length === 0) {
        for (var i = 0; i < items.length; i++) {
            var file = items[i].getAsFile ? items[i].getAsFile() : null;
            if (file) addDroppedFile(file);
        }
        return;
    }
    var fileQueue = [];
    var pendingReads = 0;
    var maxFiles = 100;
    function traverseEntry(entry, path) {
        if (fileQueue.length >= maxFiles) return;
        if (entry.isFile) {
            entry.file(function(file) {
                fileQueue.push(file);
                pendingReads--;
                if (pendingReads === 0) {
                    fileQueue.forEach(f => addDroppedFile(f));
                }
            }, function(err) {
                console.warn('Error reading file:', err);
                pendingReads--;
                if (pendingReads === 0) {
                    fileQueue.forEach(f => addDroppedFile(f));
                }
            });
            pendingReads++;
        } else if (entry.isDirectory) {
            var reader = entry.createReader();
            var allEntries = [];
            function readEntries() {
                reader.readEntries(function(results) {
                    if (results.length === 0) {
                        allEntries.forEach(function(subEntry) {
                            traverseEntry(subEntry, path + entry.name + '/');
                        });
                    } else {
                        allEntries = allEntries.concat(results);
                        readEntries();
                    }
                }, function(err) {
                    console.warn('Error reading directory:', err);
                });
            }
            readEntries();
        }
    }
    entries.forEach(function(entry) {
        traverseEntry(entry, '');
    });
    if (pendingReads === 0 && fileQueue.length === 0) {
        status.textContent = '📁 No files found in drop.';
    }
    setTimeout(function() {
        if (pendingReads === 0 && fileQueue.length > 0) {
            fileQueue.forEach(f => addDroppedFile(f));
        }
    }, 100);
}

function addDroppedFile(file) {
    var reader = new FileReader();
    reader.onload = function(ev) {
        var b64 = ev.target.result.split(',')[1] || '';
        var isImage = file.type.startsWith('image/');
        pending.push({ type: isImage ? 'image' : 'file', name: file.name, b64: b64, mime: file.type });
        var thumb = document.createElement('div');
        thumb.className = 'att-thumb';
        if (isImage) {
            var img = document.createElement('img');
            img.src = ev.target.result;
            thumb.appendChild(img);
        } else {
            thumb.appendChild(document.createTextNode('📄 ' + file.name));
        }
        var rm = document.createElement('button');
        rm.className = 'remove';
        rm.textContent = '×';
        rm.onclick = function() {
            var idx = pending.findIndex(p => p.name === file.name && p.b64 === b64);
            if (idx > -1) pending.splice(idx, 1);
            thumb.remove();
        };
        thumb.appendChild(rm);
        attachments.appendChild(thumb);
        status.textContent = '📎 Added ' + file.name;
    };
    reader.readAsDataURL(file);
}

var themeOuter = document.getElementById('themeToggleOuter');
var themeKnob = document.getElementById('themeKnob');
var isLight = localStorage.getItem('theme') === 'light';
function applyTheme(light) {
    document.body.classList.toggle('light-mode', light);
    localStorage.setItem('theme', light ? 'light' : 'dark');
    themeOuter.classList.toggle('day', light);
}
applyTheme(isLight);
var draggedTheme = false;
var isDraggingTheme = false;
var startXTheme = 0, startLeftTheme = 0;
function handleThemeClick(e) {
    if (draggedTheme) return;
    var newLight = !document.body.classList.contains('light-mode');
    applyTheme(newLight);
}
const MIN_LEFT_THEME = 3;
const MAX_LEFT_THEME = 93;
themeKnob.addEventListener('mousedown', dragStartTheme);
themeKnob.addEventListener('touchstart', dragStartTheme, { passive: true });
function dragStartTheme(e) {
    isDraggingTheme = true;
    draggedTheme = false;
    themeKnob.style.transition = 'none';
    startXTheme = e.touches ? e.touches[0].clientX : e.clientX;
    startLeftTheme = document.body.classList.contains('light-mode') ? MAX_LEFT_THEME : MIN_LEFT_THEME;
    e.stopPropagation();
    window.addEventListener('mousemove', dragMoveTheme);
    window.addEventListener('mouseup', dragEndTheme);
    window.addEventListener('touchmove', dragMoveTheme, { passive: true });
    window.addEventListener('touchend', dragEndTheme);
}
function dragMoveTheme(e) {
    if (!isDraggingTheme) return;
    const clientX = e.touches ? e.touches[0].clientX : e.clientX;
    const dx = clientX - startXTheme;
    if (Math.abs(dx) > 4) draggedTheme = true;
    let newLeft = Math.min(MAX_LEFT_THEME, Math.max(MIN_LEFT_THEME, startLeftTheme + dx));
    themeKnob.style.left = newLeft + 'px';
    const progress = (newLeft - MIN_LEFT_THEME) / (MAX_LEFT_THEME - MIN_LEFT_THEME);
    document.querySelector('.night-bg').style.opacity = 1 - progress;
    document.querySelector('.stars-layer').style.opacity = 1 - progress;
    document.querySelector('.day-bg').style.opacity = progress;
    document.querySelector('.knob-moon').style.opacity = 1 - progress;
    document.querySelector('.knob-sun').style.opacity = progress;
    document.querySelector('.astronaut').style.opacity = progress < 0.5 ? 1 : 0;
    document.querySelector('.biplane').style.opacity = progress >= 0.5 ? 1 : 0;
}
function dragEndTheme(e) {
    if (!isDraggingTheme) return;
    isDraggingTheme = false;
    themeKnob.style.transition = '';
    themeKnob.style.left = '';
    document.querySelector('.night-bg').style.opacity = '';
    document.querySelector('.stars-layer').style.opacity = '';
    document.querySelector('.day-bg').style.opacity = '';
    document.querySelector('.knob-moon').style.opacity = '';
    document.querySelector('.knob-sun').style.opacity = '';
    document.querySelector('.astronaut').style.opacity = '';
    document.querySelector('.biplane').style.opacity = '';
    const rect = themeKnob.getBoundingClientRect();
    const outerRect = themeOuter.getBoundingClientRect();
    const currentLeft = rect.left - outerRect.left - 5;
    const midpoint = (MIN_LEFT_THEME + MAX_LEFT_THEME) / 2;
    const newLight = currentLeft > midpoint;
    applyTheme(newLight);
    window.removeEventListener('mousemove', dragMoveTheme);
    window.removeEventListener('mouseup', dragEndTheme);
    window.removeEventListener('touchmove', dragMoveTheme);
    window.removeEventListener('touchend', dragEndTheme);
}
function makeThemeStars() {
    const layer = document.getElementById('themeStars');
    const pts = [
        {x:26,y:10,s:1},{x:34,y:14,s:0.8},{x:40,y:7,s:1.2},{x:46,y:17,s:0.8},
        {x:62,y:18,s:0.8},{x:76,y:16,s:0.8},{x:86,y:6,s:1},
        {x:98,y:13,s:1},{x:112,y:10,s:1},{x:124,y:18,s:0.8},
    ];
    pts.forEach(d => {
        const s = document.createElement('div');
        s.className = 'star';
        s.style.cssText = `width:${d.s}px;height:${d.s}px;left:${d.x}px;top:${d.y}px;`;
        layer.appendChild(s);
    });
    [{x:38,y:12},{x:76,y:18},{x:114,y:20}].forEach(p => {
        const sp = document.createElement('div');
        sp.className = 'sparkle';
        sp.style.cssText = `left:${p.x}px;top:${p.y}px;`;
        sp.innerHTML = '✦';
        layer.appendChild(sp);
    });
}
makeThemeStars();

var sidebar = document.getElementById('sidebar');
var sidebarVisible = localStorage.getItem('sidebarVisible') !== 'false';
function toggleSidebar() {
    sidebarVisible = !sidebarVisible;
    localStorage.setItem('sidebarVisible', sidebarVisible);
    sidebar.classList.toggle('hidden', !sidebarVisible);
}
if (!sidebarVisible) sidebar.classList.add('hidden');

var scrollBtn = document.getElementById('scrollBottomBtn');
var _scrollRaf = false;
chatArea.addEventListener('scroll', function() {
    if (_scrollRaf) return;
    _scrollRaf = true;
    requestAnimationFrame(function() {
        _scrollRaf = false;
        var threshold = 80;
        var atBottom = chatArea.scrollHeight - chatArea.scrollTop - chatArea.clientHeight < threshold;
        scrollBtn.style.display = atBottom ? 'none' : 'block';
    });
});
scrollBtn.addEventListener('click', function() {
    chatArea.scrollTo({ top: chatArea.scrollHeight, behavior: 'smooth' });
});
document.addEventListener('keydown', function(e) {
    if (e.altKey && e.key === 'ArrowDown') {
        e.preventDefault();
        chatArea.scrollTo({ top: chatArea.scrollHeight, behavior: 'smooth' });
    }
});

var voiceEnabled = false;
var speaking = false;
var currentUtterance = null;
const speakToggleBtn = document.getElementById('speakToggleBtn');
const stopSpeakBtn = document.getElementById('stopSpeakBtn');
if (!('speechSynthesis' in window)) {
    speakToggleBtn.style.display = 'none';
}
function toggleVoice() {
    voiceEnabled = !voiceEnabled;
    if (voiceEnabled) {
        speakToggleBtn.classList.add('active');
        speakToggleBtn.textContent = '🔊';
        status.textContent = '🔊 Voice output ON';
    } else {
        speakToggleBtn.classList.remove('active');
        speakToggleBtn.textContent = '🔇';
        status.textContent = '🔇 Voice output OFF';
        stopSpeaking();
    }
}
function stopSpeaking() {
    if (currentUtterance && speaking) {
        window.speechSynthesis.cancel();
        speaking = false;
        stopSpeakBtn.style.display = 'none';
    }
}

function cleanForSpeech(text) {
    var lines = text.split('\n');
    var cleanedLines = [];
    for (var i = 0; i < lines.length; i++) {
        var line = lines[i].trim();
        if (/^[=*_\-#]+$/.test(line)) {
            continue;
        }
        line = line.replace(/([=*_\-#])\1{2,}/g, ' ');
        cleanedLines.push(line);
    }
    return cleanedLines.join('\n').trim();
}

function speakText(text) {
    if (!voiceEnabled || !text) return;
    var cleaned = cleanForSpeech(text);
    if (!cleaned.trim()) return;
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(cleaned);
    utterance.lang = 'en-US';
    utterance.rate = 1.0;
    utterance.pitch = 1.0;
    utterance.onstart = function() {
        speaking = true;
        stopSpeakBtn.style.display = 'flex';
        status.textContent = '🔊 Speaking...';
    };
    utterance.onend = function() {
        speaking = false;
        stopSpeakBtn.style.display = 'none';
        status.textContent = voiceEnabled ? '🔊 Voice output ON (idle)' : '✅ Done';
    };
    utterance.onerror = function(e) {
        console.warn('TTS error:', e.error);
        speaking = false;
        stopSpeakBtn.style.display = 'none';
    };
    currentUtterance = utterance;
    window.speechSynthesis.speak(utterance);
}

function saveApiKey(provider, key) {
    if (key && key.trim()) localStorage.setItem('api_key_' + provider, key.trim());
    else localStorage.removeItem('api_key_' + provider);
}
function loadApiKey(provider) {
    return localStorage.getItem('api_key_' + provider) || '';
}

function updateVisionBadge() {
    var provider = providerSelect.value;
    var model = modelSelect.value;
    if (!model) { visionBadge.classList.remove('visible'); return; }
    var apiKey = apiKeyInput.value;
    fetch('/check_vision', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider: provider, model: model, api_key: apiKey })
    })
        .then(r => r.json())
        .then(data => {
            if (data.vision) visionBadge.classList.add('visible');
            else visionBadge.classList.remove('visible');
        })
        .catch(() => visionBadge.classList.remove('visible'));
}

function loadModels() {
    var provider = providerSelect.value;
    var apiKey = apiKeyInput.value;
    fetch('/providers/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider: provider, api_key: apiKey })
    })
        .then(r => r.json())
        .then(data => {
            if (data.error) { status.textContent = '⚠️ ' + data.error; return; }
            var current = modelSelect.value;
            modelSelect.innerHTML = '';
            if (data.models && data.models.length) {
                data.models.forEach(m => {
                    var opt = document.createElement('option');
                    opt.value = m; opt.textContent = m;
                    modelSelect.appendChild(opt);
                });
                modelSelect.value = (current && data.models.includes(current)) ? current : data.models[0];
            } else {
                var opt = document.createElement('option');
                opt.value = ''; opt.textContent = 'No models found';
                modelSelect.appendChild(opt);
            }
            updateVisionBadge();
            if (provider === 'ollama') {
                unloadBtn.style.display = 'inline-block';
            } else {
                unloadBtn.style.display = 'none';
            }
            if (provider === 'deepseek' && modelSelect.value) {
                checkDeepSeekStatus();
            } else {
                document.getElementById('modelInfo').textContent = '';
            }
        })
        .catch(err => { status.textContent = '⚠️ Could not load models: ' + err; });
}

providerSelect.addEventListener('change', function() {
    var provider = this.value;
    var keyInput = document.getElementById('apiKeyInput');
    if (provider === 'groq' || provider === 'huggingface' || provider === 'deepseek' || provider === 'claude') {
        keyInput.style.display = 'inline-block';
        var placeholder = '';
        if (provider === 'groq') placeholder = 'Enter Groq API Key';
        else if (provider === 'huggingface') placeholder = 'Enter HF Token (optional)';
        else if (provider === 'deepseek') placeholder = 'Enter DeepSeek API Key';
        else if (provider === 'claude') placeholder = 'Enter Anthropic API Key';
        keyInput.placeholder = placeholder;
        keyInput.value = loadApiKey(provider);
    } else {
        keyInput.style.display = 'none';
    }
    loadModels();
    if (provider === 'deepseek') {
        checkDeepSeekStatus();
    } else {
        document.getElementById('deepseekStatus').textContent = '';
        document.getElementById('modelInfo').textContent = '';
    }
});
apiKeyInput.addEventListener('blur', function() {
    var provider = providerSelect.value;
    if (provider === 'groq' || provider === 'huggingface' || provider === 'deepseek' || provider === 'claude') {
        saveApiKey(provider, this.value);
        if (provider === 'deepseek') checkDeepSeekStatus();
    }
});
modelSelect.addEventListener('change', function() {
    const provider = providerSelect.value;
    const model = this.value;
    updateVisionBadge();
    if (provider === 'ollama') {
        fetch('/set_model', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({model: model})
        })
        .then(r => r.json())
        .then(data => {
            if (data.ok) {
                status.textContent = '✅ Model switched to ' + model;
            } else {
                status.textContent = '❌ ' + (data.error || 'Failed');
            }
        })
        .catch(err => { status.textContent = '❌ Error: ' + err; });
    }
    document.getElementById('modelInfo').textContent = '';
});

function getDateGroup(dateStr) {
    var now = new Date();
    var date = new Date(dateStr);
    var today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    var yesterday = new Date(today);
    yesterday.setDate(yesterday.getDate() - 1);
    var weekStart = new Date(today);
    weekStart.setDate(weekStart.getDate() - today.getDay());
    var lastWeekStart = new Date(weekStart);
    lastWeekStart.setDate(lastWeekStart.getDate() - 7);
    var d = new Date(date.getFullYear(), date.getMonth(), date.getDate());
    if (d.getTime() === today.getTime()) return 'Today';
    if (d.getTime() === yesterday.getTime()) return 'Yesterday';
    if (d >= weekStart) return 'This Week';
    if (d >= lastWeekStart) return 'Last Week';
    return 'Older';
}

var searchTimeout = null;
function searchChats() {
    var input = document.getElementById('searchInput');
    var query = input.value.trim();
    searchQuery = query;
    if (searchTimeout) clearTimeout(searchTimeout);
    searchTimeout = setTimeout(function() {
        if (query.length === 0) {
            renderConvList(conversations);
            return;
        }
        fetch('/conversations/search?q=' + encodeURIComponent(query))
            .then(r => r.json())
            .then(data => {
                if (data.error) {
                    status.textContent = '⚠️ ' + data.error;
                    return;
                }
                renderConvList(data);
            })
            .catch(err => {
                status.textContent = '⚠️ Search error: ' + err;
            });
    }, 300);
}

function loadConversations() {
    fetch('/conversations')
        .then(r => r.json())
        .then(data => {
            conversations = data;
            var query = document.getElementById('searchInput').value.trim();
            if (query.length > 0) {
                searchChats();
            } else {
                renderConvList(conversations);
            }
            if (data.length > 0) {
                if (!currentConv || !data.find(c => c.id === currentConv)) {
                    selectConversation(data[0].id);
                } else {
                    selectConversation(currentConv);
                }
            } else {
                newChat();
            }
        });
}
function renderConvList(convs) {
    if (!convs || convs.length === 0) {
        convList.innerHTML = '<div class="no-results">🔍 No chats found</div>';
        return;
    }
    var groups = {};
    convs.forEach(conv => {
        var group = getDateGroup(conv.created);
        if (!groups[group]) groups[group] = [];
        groups[group].push(conv);
    });
    var groupOrder = ['Today', 'Yesterday', 'This Week', 'Last Week', 'Older'];
    convList.innerHTML = '';
    groupOrder.forEach(groupName => {
        if (groups[groupName] && groups[groupName].length) {
            var heading = document.createElement('div');
            heading.className = 'group-heading';
            heading.textContent = groupName;
            convList.appendChild(heading);
            groups[groupName].forEach(conv => {
                var div = document.createElement('div');
                div.className = 'conv-item' + (conv.id === currentConv ? ' active' : '');
                div.dataset.id = conv.id;
                div.draggable = true;
                var titleSpan = document.createElement('span');
                titleSpan.className = 'title';
                titleSpan.textContent = conv.title || 'Untitled';
                div.appendChild(titleSpan);
                var renameBtn = document.createElement('button');
                renameBtn.className = 'rename-btn';
                renameBtn.textContent = '✏️';
                renameBtn.title = 'Rename this chat';
                renameBtn.onclick = function(e) {
                    e.stopPropagation();
                    renameChat(conv.id);
                };
                div.appendChild(renameBtn);
                var timeSpan = document.createElement('span');
                timeSpan.className = 'time';
                var d = new Date(conv.created);
                timeSpan.textContent = d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
                div.appendChild(timeSpan);
                var delBtn = document.createElement('button');
                delBtn.className = 'del';
                delBtn.textContent = '×';
                delBtn.title = 'Delete this chat';
                delBtn.onclick = function(e) {
                    e.stopPropagation();
                    deleteChat(conv.id);
                };
                div.appendChild(delBtn);
                div.addEventListener('dragstart', handleDragStart);
                div.addEventListener('dragend', handleDragEnd);
                div.addEventListener('dragover', handleDragOver);
                div.addEventListener('drop', handleDrop);
                div.addEventListener('click', function() {
                    selectConversation(conv.id);
                });
                convList.appendChild(div);
            });
        }
    });
}
var dragSrcId = null;
function handleDragStart(e) {
    dragSrcId = this.dataset.id;
    this.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', this.dataset.id);
}
function handleDragEnd(e) {
    this.classList.remove('dragging');
    document.querySelectorAll('.conv-item').forEach(el => el.classList.remove('drag-over'));
}
function handleDragOver(e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    document.querySelectorAll('.conv-item').forEach(el => el.classList.remove('drag-over'));
    this.classList.add('drag-over');
}
function handleDrop(e) {
    e.preventDefault();
    this.classList.remove('drag-over');
    var targetId = this.dataset.id;
    if (dragSrcId === targetId) return;
    var srcIndex = conversations.findIndex(c => c.id === dragSrcId);
    var targetIndex = conversations.findIndex(c => c.id === targetId);
    if (srcIndex === -1 || targetIndex === -1) return;
    var moved = conversations.splice(srcIndex, 1)[0];
    conversations.splice(targetIndex, 0, moved);
    var newOrder = {};
    conversations.forEach((c, idx) => {
        newOrder[c.id] = idx;
    });
    fetch('/conversations/reorder', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({order: newOrder})
    })
    .then(r => r.json())
    .then(data => {
        if (data.ok) {
            conversations.forEach(c => c.order = newOrder[c.id]);
            var query = document.getElementById('searchInput').value.trim();
            if (query.length > 0) searchChats();
            else renderConvList(conversations);
            status.textContent = '✅ Order updated';
        } else {
            status.textContent = '❌ Failed to reorder';
        }
    })
    .catch(err => {
        status.textContent = '❌ Error: ' + err;
    });
}
function renameChat(id) {
    var conv = conversations.find(c => c.id === id);
    if (!conv) return;
    var newTitle = prompt('Rename chat:', conv.title);
    if (newTitle === null || newTitle.trim() === '') return;
    newTitle = newTitle.trim();
    fetch('/conversations/' + id + '/rename', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({title: newTitle})
    })
    .then(r => r.json())
    .then(data => {
        if (data.ok) {
            conv.title = newTitle;
            var query = document.getElementById('searchInput').value.trim();
            if (query.length > 0) searchChats();
            else renderConvList(conversations);
            status.textContent = '✅ Renamed to "' + newTitle + '"';
        } else {
            status.textContent = '❌ Rename failed';
        }
    })
    .catch(err => {
        status.textContent = '❌ Error: ' + err;
    });
}
function selectConversation(id) {
    if (id === currentConv) return;
    currentConv = id;
    document.querySelectorAll('.conv-item').forEach(el => el.classList.toggle('active', el.dataset.id === id));
    fetch('/conversations/' + id + '/messages')
        .then(r => r.json())
        .then(messages => {
            chatArea.innerHTML = '';
            if (messages.length === 0) {
                chatArea.innerHTML = '<div class="msg bot">💬 No messages yet. Say something!<br>You can also type <code>ollama pull &lt;model&gt;</code>, etc.</div>';
            } else {
                messages.forEach((msg, index) => renderMsg(msg.role, msg, index));
            }
            smoothScrollToBottom();
        });
}
function newChat() {
    fetch('/conversations', { method: 'POST' })
        .then(r => r.json())
        .then(() => {
            document.getElementById('searchInput').value = '';
            searchQuery = '';
            loadConversations();
        });
}
function deleteChat(id) {
    if (!confirm('Delete this conversation?')) return;
    fetch('/conversations/' + id, { method: 'DELETE' })
        .then(() => {
            if (currentConv === id) currentConv = null;
            loadConversations();
        });
}
function clearAllChats() {
    if (!currentConv) {
        alert('Open a chat first, then Clear will empty that chat only.');
        return;
    }
    if (!confirm('Clear all messages in this chat only? Other chats will not be affected.')) return;
    fetch('/clear_all', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cid: currentConv })
    })
        .then(() => {
            chatArea.innerHTML = '<div class="msg bot">🗑 This chat\'s messages were cleared. Other chats are untouched.</div>';
        });
}
function renderMsg(role, entry, msgIndex) {
    var div = document.createElement('div');
    div.className = 'msg ' + (role === 'user' ? 'user' : 'bot');
    if (entry.images && entry.images.length) {
        entry.images.forEach(im => {
            var img = document.createElement('img');
            img.loading = 'lazy';
            img.decoding = 'async';
            img.src = 'data:' + (im.mime || 'image/png') + ';base64,' + (im.b64 || '');
            img.style.cursor = 'pointer';
            img.onclick = function() {
                openImageViewerFromChat(im.b64, im.mime);
            };
            div.appendChild(img);
        });
    }
    if (entry.files && entry.files.length) {
        entry.files.forEach(f => {
            var chip = document.createElement('div');
            chip.className = 'file-chip';
            chip.textContent = '📄 ' + (f.name || 'file');
            div.appendChild(chip);
        });
    }
    var body = document.createElement('div');
    body.className = 'body';
    if (role === 'bot') {
        body.innerHTML = marked.parse(entry.text || '');
        renderMermaidDiagrams(body);
    } else {
        body.textContent = entry.text || '';
    }
    div.appendChild(body);
    var ts = document.createElement('div');
    ts.className = 'ts';
    ts.textContent = entry.ts || '';
    div.appendChild(ts);
    if (role === 'user') {
        var actions = document.createElement('div');
        actions.className = 'msg-actions';
        var editBtn = document.createElement('button');
        editBtn.className = 'edit-btn';
        editBtn.innerHTML = '✏️';
        editBtn.title = 'Edit message';
        editBtn.onclick = function(e) {
            e.stopPropagation();
            startEditMessage(div, role, entry, msgIndex);
        };
        var delBtn = document.createElement('button');
        delBtn.className = 'delete-btn';
        delBtn.innerHTML = '🗑️';
        delBtn.title = 'Delete message';
        delBtn.onclick = function(e) {
            e.stopPropagation();
            deleteMessage(div, msgIndex);
        };
        actions.appendChild(editBtn);
        actions.appendChild(delBtn);
        div.appendChild(actions);
    }
    chatArea.appendChild(div);
    if (role === 'bot') {
        processCodeBlocks(div);
        renderMermaidDiagrams(div);
    }
    return div;
}
function reloadCurrentChat() {
    if (!currentConv) return;
    fetch('/conversations/' + currentConv + '/messages')
        .then(r => r.json())
        .then(messages => {
            chatArea.innerHTML = '';
            if (messages.length === 0) {
                chatArea.innerHTML = '<div class="msg bot">👋 Hello! Select or create a chat from the sidebar.<br>You can also type <code>ollama pull &lt;model&gt;</code>, <code>ollama list</code>, etc.</div>';
            } else {
                messages.forEach((msg, index) => renderMsg(msg.role, msg, index));
            }
            smoothScrollToBottom();
            processCodeBlocks(chatArea);
            renderMermaidDiagrams(chatArea);
        });
}
async function startEditMessage(msgDiv, role, entry, idx) {
    var body = msgDiv.querySelector('.body');
    var oldText = body.textContent.trim();
    var textarea = document.createElement('textarea');
    textarea.className = 'edit-textarea';
    textarea.value = oldText;
    body.replaceWith(textarea);
    var btnRow = document.createElement('div');
    btnRow.style.marginTop = '6px';
    var saveBtn = document.createElement('button');
    saveBtn.textContent = 'Save & Resend';
    saveBtn.className = 'new-chat-btn';
    saveBtn.onclick = async function() {
        var newText = textarea.value.trim();
        if (!newText) return;
        var msgs = await fetch(`/conversations/${currentConv}/messages`).then(r => r.json());
        for (let i = msgs.length - 1; i >= idx; i--) {
            await fetch(`/conversations/${currentConv}/messages/${i}`, {method: 'DELETE'});
        }
        chatArea.innerHTML = '';
        var remaining = await fetch(`/conversations/${currentConv}/messages`).then(r => r.json());
        remaining.forEach((msg, index) => renderMsg(msg.role, msg, index));
        smoothScrollToBottom();
        msgInput.value = newText;
        pending = [];
        attachments.innerHTML = '';
        doSend();
    };
    var cancelBtn = document.createElement('button');
    cancelBtn.textContent = 'Cancel';
    cancelBtn.style.cssText = 'background:transparent; color:#8b949e; border:none; margin-left:8px; cursor:pointer;';
    cancelBtn.onclick = () => reloadCurrentChat();
    btnRow.appendChild(saveBtn);
    btnRow.appendChild(cancelBtn);
    textarea.after(btnRow);
    textarea.focus();
}
async function deleteMessage(msgDiv, idx) {
    if (!confirm('Delete this message?')) return;
    var msgs = await fetch(`/conversations/${currentConv}/messages`).then(r => r.json());
    var bodyEl = msgDiv.querySelector('.body');
    var msgText = bodyEl ? bodyEl.textContent.trim() : '';
    var realIdx = idx;
    if (realIdx < 0 || realIdx >= msgs.length) {
        realIdx = msgs.findIndex(m => m.role === 'user' && m.text && m.text.trim() === msgText);
    }
    if (realIdx < 0 || realIdx >= msgs.length) {
        status.textContent = 'Could not find message to delete';
        return;
    }
    var res = await fetch(`/conversations/${currentConv}/messages/${realIdx}`, {method: 'DELETE'});
    if (res.ok) {
        reloadCurrentChat();
    }
}
fileInput.addEventListener('change', function() {
    var files = Array.from(fileInput.files);
    files.forEach(function(file) {
        var reader = new FileReader();
        reader.onload = function(ev) {
            var b64 = ev.target.result.split(',')[1];
            var isImage = file.type.startsWith('image/');
            pending.push({ type: isImage ? 'image' : 'file', name: file.name, b64: b64, mime: file.type });
            var thumb = document.createElement('div');
            thumb.className = 'att-thumb';
            if (isImage) {
                var img = document.createElement('img');
                img.src = ev.target.result;
                thumb.appendChild(img);
            } else {
                thumb.appendChild(document.createTextNode('📄 ' + file.name));
            }
            var rm = document.createElement('button');
            rm.className = 'remove';
            rm.textContent = '×';
            rm.onclick = function() {
                var idx = pending.findIndex(p => p.name === file.name);
                if (idx > -1) pending.splice(idx, 1);
                thumb.remove();
            };
            thumb.appendChild(rm);
            attachments.appendChild(thumb);
        };
        reader.readAsDataURL(file);
    });
    fileInput.value = '';
});
sendBtn.addEventListener('click', doSend);
document.getElementById('searchToggleBtn').addEventListener('click', function() {
    searchEnabled = !searchEnabled;
    this.classList.toggle('active', searchEnabled);
    this.textContent = searchEnabled ? '🔍' : '🔍 off';
    status.textContent = searchEnabled ? '🔍 Web search ON' : '🔍 Web search OFF';
});

function doSend() {
    var text = msgInput.value.trim();
    if ((!text && pending.length === 0) || busy) return;
    if (!currentConv) {
        fetch('/conversations', { method: 'POST' })
            .then(r => r.json())
            .then(data => {
                currentConv = data.id;
                loadConversations();
                actuallySend(text);
            });
        return;
    }
    actuallySend(text);
}
var tokenCount = 0;
var startTimeToken = null;
var speedInterval = null;

function actuallySend(text) {
    var images = pending.filter(p => p.type === 'image');
    var files  = pending.filter(p => p.type === 'file');
    var userEntry = {
        role: 'user',
        text: text,
        images: images.map(i => ({ b64: i.b64, name: i.name })),
        files: files.map(f => ({ name: f.name, mime: f.mime })),
        ts: new Date().toLocaleTimeString()
    };
    var userDiv = renderMsg('user', userEntry, -1);
    msgInput.value = '';
    msgInput.style.height = '46px';
    pending = [];
    attachments.innerHTML = '';
    var botDiv = renderMsg('bot', { role:'bot', text:'⏳ Thinking...', ts:'' }, -1);
    smoothScrollToBottom();
    var bodyEl = botDiv.querySelector('.body');
    bodyEl.classList.add('thinking-dots');
    busy = true;
    sendBtn.disabled = true;
    status.textContent = '⏳ Generating...';
    var provider = providerSelect.value;
    var model = modelSelect.value;
    var apiKey = apiKeyInput.value;
    if (provider === 'groq' || provider === 'huggingface' || provider === 'deepseek' || provider === 'claude') {
        saveApiKey(provider, apiKey);
    }
    var endpoint = (provider === 'ollama') ? '/chat_stream' : '/chat';
    tokenCount = 0;
    startTimeToken = Date.now();
    if (speedInterval) clearInterval(speedInterval);
    var tokenSpeedSpan = document.getElementById('tokenSpeed');
    speedInterval = setInterval(function() {
        var elapsed = (Date.now() - startTimeToken) / 1000;
        if (elapsed > 0) {
            var speed = (tokenCount / elapsed).toFixed(1);
            tokenSpeedSpan.textContent = `⏱️ ${speed} tok/s | ${tokenCount} tokens`;
        }
    }, 200);

    function handleSendError(errMsg) {
        clearInterval(speedInterval);
        if (userDiv && userDiv.parentNode) userDiv.remove();
        if (botDiv && botDiv.parentNode) botDiv.remove();
        busy = false;
        sendBtn.disabled = false;
        msgInput.value = text;
        msgInput.style.height = Math.min(msgInput.scrollHeight, 140) + 'px';
        status.textContent = '❌ ' + errMsg;
        if (chatArea.children.length === 0) {
            chatArea.innerHTML = '<div class="msg bot">👋 Hello! Select or create a chat from the sidebar.<br>You can also type <code>ollama pull &lt;model&gt;</code>, <code>ollama list</code>, etc.</div>';
        }
    }

    fetch(endpoint, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            conversation_id: currentConv,
            message: text,
            images: images.map(i => ({ b64: i.b64, name: i.name })),
            files: files.map(f => ({ b64: f.b64, name: f.name, mime: f.mime })),
            search: searchEnabled,
            provider: provider,
            model: model,
            api_key: apiKey
        })
    })
    .then(response => {
        if (!response.ok) {
            return response.json().catch(() => ({})).then(errData => {
                throw new Error(errData.error || 'HTTP ' + response.status);
            });
        }
        var contentType = response.headers.get('content-type') || '';
        if (contentType.includes('text/event-stream')) {
            var reader = response.body.getReader();
            var decoder = new TextDecoder();
            var fullText = '';
            var sseBuffer = '';
            var tokenQueue = [];
            var rafId = null;
            var botBody = botDiv.querySelector('.body');
            var textNode = null;
            botBody.innerHTML = '';
            textNode = document.createTextNode('');
            botBody.appendChild(textNode);

            function flushTokens() {
                rafId = null;
                if (tokenQueue.length === 0) return;
                for (var i = 0; i < tokenQueue.length; i++) {
                    fullText += tokenQueue[i];
                }
                tokenQueue.length = 0;
                textNode.textContent = fullText;
                botBody.classList.remove('thinking-dots');
                var threshold = 80;
                var atBottom = chatArea.scrollHeight - chatArea.scrollTop - chatArea.clientHeight < threshold;
                if (atBottom) {
                    chatArea.scrollTop = chatArea.scrollHeight;
                }
            }
            function scheduleFlush() {
                if (!rafId) {
                    rafId = requestAnimationFrame(flushTokens);
                }
            }
            function readStream() {
                reader.read().then(({done, value}) => {
                    if (done) {
                        if (sseBuffer.startsWith('data: ')) {
                            try {
                                var data = JSON.parse(sseBuffer.substring(6));
                                if (data.token) tokenQueue.push(data.token);
                            } catch(e) {}
                        }
                        if (rafId) {
                            cancelAnimationFrame(rafId);
                            rafId = null;
                        }
                        flushTokens();
                        botBody.innerHTML = marked.parse(fullText || '(empty response)');
                        botDiv.querySelector('.ts').textContent = new Date().toLocaleTimeString();
                        processCodeBlocks(botDiv);
                        renderMermaidDiagrams(botDiv);
                        status.textContent = '✅ Done';
                        busy = false;
                        sendBtn.disabled = false;
                        smoothScrollToBottom();
                        speakText(fullText);
                        loadConversations();
                        return;
                    }
                    sseBuffer += decoder.decode(value, {stream: true});
                    var lines = sseBuffer.split('\n');
                    sseBuffer = lines.pop();
                    for (var line of lines) {
                        if (line.startsWith('data: ')) {
                            var jsonStr = line.substring(6);
                            try {
                                var data = JSON.parse(jsonStr);
                                if (data.token) {
                                    tokenCount++;
                                    tokenQueue.push(data.token);
                                    scheduleFlush();
                                }
                                if (data.error) {
                                    handleSendError(data.error);
                                    return;
                                }
                                if (data.done && data.usage) {
                                    finalizeStats(data.usage);
                                }
                            } catch(e) {}
                        }
                    }
                    readStream();
                });
            }
            readStream();
        } else {
            response.json().then(data => {
                if (data.error) {
                    handleSendError(data.error);
                } else {
                    var text = data.response || '(no response)';
                    botDiv.querySelector('.body').classList.remove('thinking-dots');
                    botDiv.querySelector('.body').innerHTML = marked.parse(text);
                    botDiv.querySelector('.ts').textContent = new Date().toLocaleTimeString();
                    processCodeBlocks(botDiv);
                    renderMermaidDiagrams(botDiv);
                    status.textContent = '✅ Done';
                    loadConversations();
                    speakText(text);
                    if (data.usage) finalizeStats(data.usage);
                    else finalizeStats({tokens: text.split(' ').length, duration_sec: 1});
                    busy = false; sendBtn.disabled = false;
                }
            });
        }
    })
    .catch(err => {
        handleSendError(err.message || 'Connection failed');
    });
}

function finalizeStats(usage) {
    clearInterval(speedInterval);
    var tokenSpeedSpan = document.getElementById('tokenSpeed');
    var tokens = usage.tokens || tokenCount;
    var secs = usage.duration_sec || ((Date.now() - startTimeToken) / 1000);
    var speed = secs > 0 ? (tokens / secs).toFixed(1) : '?';
    tokenSpeedSpan.textContent = `⏱️ ${speed} tok/s | ${tokens} tokens`;
}

var recordBtn = document.getElementById('recordBtn');
var recognition = null;
var isRecording = false;
if ('webkitSpeechRecognition' in window || 'SpeechRecognition' in window) {
    var SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    recognition = new SpeechRecognition();
    recognition.lang = 'en-US';
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;
    recognition.onresult = function(event) {
        var transcript = '';
        for (var i = event.resultIndex; i < event.results.length; i++) {
            if (event.results[i].isFinal) transcript += event.results[i][0].transcript;
            else {
                msgInput.value = event.results[i][0].transcript;
                status.textContent = '🎤 Listening... (interim)';
            }
        }
        if (transcript) {
            msgInput.value = transcript;
            status.textContent = '✅ Voice recognized!';
        }
    };
    recognition.onend = function() {
        isRecording = false;
        recordBtn.classList.remove('recording');
        recordBtn.textContent = '🎤';
        if (msgInput.value.trim() === '') status.textContent = '⏹️ Recording stopped (no input)';
        else status.textContent = '✅ Voice input ready.';
    };
    recognition.onerror = function(event) {
        isRecording = false;
        recordBtn.classList.remove('recording');
        recordBtn.textContent = '🎤';
        var errors = { 'not-allowed': '❌ Microphone access denied.', 'no-speech': '⏹️ No speech detected.', 'audio-capture': '❌ No microphone found.', 'network': '❌ Network error.' };
        status.textContent = errors[event.error] || '❌ Speech error: ' + event.error;
    };
    recordBtn.addEventListener('click', function() {
        if (isRecording) { recognition.stop(); return; }
        try {
            recognition.start();
            isRecording = true;
            recordBtn.classList.add('recording');
            recordBtn.textContent = '⏹';
            msgInput.value = '';
            status.textContent = '🎤 Listening... Speak now.';
        } catch (e) { status.textContent = '❌ Failed to start recording: ' + e.message; }
    });
} else {
    recordBtn.style.display = 'none';
    status.textContent = '⚠️ Voice recording not supported.';
}

var resourceIntervalId = null;
function updateResources() {
    fetch('/resources')
        .then(r => r.json())
        .then(data => {
            var disp = document.getElementById('resourceDisplay');
            if (!disp) return;
            if (data.error) { disp.textContent = '⚠️ ' + data.error; return; }
            let ram = data.ram_used !== null ? data.ram_used.toFixed(1) + 'GB' : '--';
            let vram = data.vram_used !== null ? data.vram_used.toFixed(1) + 'GB' : '--';
            disp.textContent = `💾 RAM: ${ram} | 🎮 VRAM: ${vram}`;
        })
        .catch(err => console.log('Resource update failed:', err));
}
window.addEventListener('beforeunload', function() {
    if (resourceIntervalId) { clearInterval(resourceIntervalId); resourceIntervalId = null; }
    if (providerSelect && providerSelect.value === 'ollama') {
        navigator.sendBeacon('/unload_model');
    }
});
resourceIntervalId = setInterval(updateResources, 15000);

window.addEventListener('load', function() {
    var provider = providerSelect.value;
    if (provider === 'groq' || provider === 'huggingface' || provider === 'deepseek' || provider === 'claude') {
        apiKeyInput.value = loadApiKey(provider);
    }
    loadModels();
    loadConversations();
    msgInput.focus();
    setTimeout(updateResources, 500);
    if (provider === 'deepseek') {
        checkDeepSeekStatus();
    }
    var searchBtn = document.getElementById('searchToggleBtn');
    searchBtn.classList.toggle('active', searchEnabled);
    searchBtn.textContent = searchEnabled ? '🔍' : '🔍 off';
    status.textContent = searchEnabled ? '🔍 Web search ON' : '🔍 Web search OFF';

    // Init weather after chat is ready
    initWeather();
});
</script>

""" + get_viewer_html() + """

<script>
/* ─── WEATHER JAVASCRIPT (merged from weather.html) ────────────── */

// Polyfill for roundRect
if (!CanvasRenderingContext2D.prototype.roundRect) {
    CanvasRenderingContext2D.prototype.roundRect = function(x, y, w, h, radii) {
        const r = typeof radii === 'number' ? radii : (radii || 0);
        this.moveTo(x + r, y);
        this.arcTo(x + w, y, x + w, y + h, r);
        this.arcTo(x + w, y + h, x, y + h, r);
        this.arcTo(x, y + h, x, y, r);
        this.arcTo(x, y, x + w, y, r);
        return this;
    };
}

// ── Country data ──
const countryList = [
    { code: 'AF', name: 'Afghanistan' },
    { code: 'AL', name: 'Albania' },
    { code: 'DZ', name: 'Algeria' },
    { code: 'AD', name: 'Andorra' },
    { code: 'AO', name: 'Angola' },
    { code: 'AG', name: 'Antigua and Barbuda' },
    { code: 'AR', name: 'Argentina' },
    { code: 'AM', name: 'Armenia' },
    { code: 'AU', name: 'Australia' },
    { code: 'AT', name: 'Austria' },
    { code: 'AZ', name: 'Azerbaijan' },
    { code: 'BS', name: 'Bahamas' },
    { code: 'BH', name: 'Bahrain' },
    { code: 'BD', name: 'Bangladesh' },
    { code: 'BB', name: 'Barbados' },
    { code: 'BY', name: 'Belarus' },
    { code: 'BE', name: 'Belgium' },
    { code: 'BZ', name: 'Belize' },
    { code: 'BJ', name: 'Benin' },
    { code: 'BT', name: 'Bhutan' },
    { code: 'BO', name: 'Bolivia' },
    { code: 'BA', name: 'Bosnia and Herzegovina' },
    { code: 'BW', name: 'Botswana' },
    { code: 'BR', name: 'Brazil' },
    { code: 'BN', name: 'Brunei' },
    { code: 'BG', name: 'Bulgaria' },
    { code: 'BF', name: 'Burkina Faso' },
    { code: 'BI', name: 'Burundi' },
    { code: 'KH', name: 'Cambodia' },
    { code: 'CM', name: 'Cameroon' },
    { code: 'CA', name: 'Canada' },
    { code: 'CV', name: 'Cape Verde' },
    { code: 'CF', name: 'Central African Republic' },
    { code: 'TD', name: 'Chad' },
    { code: 'CL', name: 'Chile' },
    { code: 'CN', name: 'China' },
    { code: 'CO', name: 'Colombia' },
    { code: 'KM', name: 'Comoros' },
    { code: 'CG', name: 'Congo' },
    { code: 'CD', name: 'DR Congo' },
    { code: 'CR', name: 'Costa Rica' },
    { code: 'HR', name: 'Croatia' },
    { code: 'CU', name: 'Cuba' },
    { code: 'CY', name: 'Cyprus' },
    { code: 'CZ', name: 'Czech Republic' },
    { code: 'DK', name: 'Denmark' },
    { code: 'DJ', name: 'Djibouti' },
    { code: 'DM', name: 'Dominica' },
    { code: 'DO', name: 'Dominican Republic' },
    { code: 'EC', name: 'Ecuador' },
    { code: 'EG', name: 'Egypt' },
    { code: 'SV', name: 'El Salvador' },
    { code: 'GQ', name: 'Equatorial Guinea' },
    { code: 'ER', name: 'Eritrea' },
    { code: 'EE', name: 'Estonia' },
    { code: 'SZ', name: 'Eswatini' },
    { code: 'ET', name: 'Ethiopia' },
    { code: 'FJ', name: 'Fiji' },
    { code: 'FI', name: 'Finland' },
    { code: 'FR', name: 'France' },
    { code: 'GA', name: 'Gabon' },
    { code: 'GM', name: 'Gambia' },
    { code: 'GE', name: 'Georgia' },
    { code: 'DE', name: 'Germany' },
    { code: 'GH', name: 'Ghana' },
    { code: 'GR', name: 'Greece' },
    { code: 'GD', name: 'Grenada' },
    { code: 'GT', name: 'Guatemala' },
    { code: 'GN', name: 'Guinea' },
    { code: 'GW', name: 'Guinea-Bissau' },
    { code: 'GY', name: 'Guyana' },
    { code: 'HT', name: 'Haiti' },
    { code: 'HN', name: 'Honduras' },
    { code: 'HU', name: 'Hungary' },
    { code: 'IS', name: 'Iceland' },
    { code: 'IN', name: 'India' },
    { code: 'ID', name: 'Indonesia' },
    { code: 'IR', name: 'Iran' },
    { code: 'IQ', name: 'Iraq' },
    { code: 'IE', name: 'Ireland' },
    { code: 'IL', name: 'Israel' },
    { code: 'IT', name: 'Italy' },
    { code: 'JM', name: 'Jamaica' },
    { code: 'JP', name: 'Japan' },
    { code: 'JO', name: 'Jordan' },
    { code: 'KZ', name: 'Kazakhstan' },
    { code: 'KE', name: 'Kenya' },
    { code: 'KI', name: 'Kiribati' },
    { code: 'KP', name: 'North Korea' },
    { code: 'KR', name: 'South Korea' },
    { code: 'KW', name: 'Kuwait' },
    { code: 'KG', name: 'Kyrgyzstan' },
    { code: 'LA', name: 'Laos' },
    { code: 'LV', name: 'Latvia' },
    { code: 'LB', name: 'Lebanon' },
    { code: 'LS', name: 'Lesotho' },
    { code: 'LR', name: 'Liberia' },
    { code: 'LY', name: 'Libya' },
    { code: 'LI', name: 'Liechtenstein' },
    { code: 'LT', name: 'Lithuania' },
    { code: 'LU', name: 'Luxembourg' },
    { code: 'MG', name: 'Madagascar' },
    { code: 'MW', name: 'Malawi' },
    { code: 'MY', name: 'Malaysia' },
    { code: 'MV', name: 'Maldives' },
    { code: 'ML', name: 'Mali' },
    { code: 'MT', name: 'Malta' },
    { code: 'MH', name: 'Marshall Islands' },
    { code: 'MR', name: 'Mauritania' },
    { code: 'MU', name: 'Mauritius' },
    { code: 'MX', name: 'Mexico' },
    { code: 'FM', name: 'Micronesia' },
    { code: 'MD', name: 'Moldova' },
    { code: 'MC', name: 'Monaco' },
    { code: 'MN', name: 'Mongolia' },
    { code: 'ME', name: 'Montenegro' },
    { code: 'MA', name: 'Morocco' },
    { code: 'MZ', name: 'Mozambique' },
    { code: 'MM', name: 'Myanmar' },
    { code: 'NA', name: 'Namibia' },
    { code: 'NR', name: 'Nauru' },
    { code: 'NP', name: 'Nepal' },
    { code: 'NL', name: 'Netherlands' },
    { code: 'NZ', name: 'New Zealand' },
    { code: 'NI', name: 'Nicaragua' },
    { code: 'NE', name: 'Niger' },
    { code: 'NG', name: 'Nigeria' },
    { code: 'MK', name: 'North Macedonia' },
    { code: 'NO', name: 'Norway' },
    { code: 'OM', name: 'Oman' },
    { code: 'PK', name: 'Pakistan' },
    { code: 'PW', name: 'Palau' },
    { code: 'PA', name: 'Panama' },
    { code: 'PG', name: 'Papua New Guinea' },
    { code: 'PY', name: 'Paraguay' },
    { code: 'PE', name: 'Peru' },
    { code: 'PH', name: 'Philippines' },
    { code: 'PL', name: 'Poland' },
    { code: 'PT', name: 'Portugal' },
    { code: 'QA', name: 'Qatar' },
    { code: 'RO', name: 'Romania' },
    { code: 'RU', name: 'Russia' },
    { code: 'RW', name: 'Rwanda' },
    { code: 'KN', name: 'Saint Kitts and Nevis' },
    { code: 'LC', name: 'Saint Lucia' },
    { code: 'VC', name: 'Saint Vincent and the Grenadines' },
    { code: 'WS', name: 'Samoa' },
    { code: 'SM', name: 'San Marino' },
    { code: 'ST', name: 'Sao Tome and Principe' },
    { code: 'SA', name: 'Saudi Arabia' },
    { code: 'SN', name: 'Senegal' },
    { code: 'RS', name: 'Serbia' },
    { code: 'SC', name: 'Seychelles' },
    { code: 'SL', name: 'Sierra Leone' },
    { code: 'SG', name: 'Singapore' },
    { code: 'SK', name: 'Slovakia' },
    { code: 'SI', name: 'Slovenia' },
    { code: 'SB', name: 'Solomon Islands' },
    { code: 'SO', name: 'Somalia' },
    { code: 'ZA', name: 'South Africa' },
    { code: 'SS', name: 'South Sudan' },
    { code: 'ES', name: 'Spain' },
    { code: 'LK', name: 'Sri Lanka' },
    { code: 'SD', name: 'Sudan' },
    { code: 'SR', name: 'Suriname' },
    { code: 'SE', name: 'Sweden' },
    { code: 'CH', name: 'Switzerland' },
    { code: 'SY', name: 'Syria' },
    { code: 'TW', name: 'Taiwan' },
    { code: 'TJ', name: 'Tajikistan' },
    { code: 'TZ', name: 'Tanzania' },
    { code: 'TH', name: 'Thailand' },
    { code: 'TL', name: 'Timor-Leste' },
    { code: 'TG', name: 'Togo' },
    { code: 'TO', name: 'Tonga' },
    { code: 'TT', name: 'Trinidad and Tobago' },
    { code: 'TN', name: 'Tunisia' },
    { code: 'TR', name: 'Turkey' },
    { code: 'TM', name: 'Turkmenistan' },
    { code: 'TV', name: 'Tuvalu' },
    { code: 'UG', name: 'Uganda' },
    { code: 'UA', name: 'Ukraine' },
    { code: 'AE', name: 'United Arab Emirates' },
    { code: 'GB', name: 'United Kingdom' },
    { code: 'US', name: 'United States' },
    { code: 'UY', name: 'Uruguay' },
    { code: 'UZ', name: 'Uzbekistan' },
    { code: 'VU', name: 'Vanuatu' },
    { code: 'VA', name: 'Vatican City' },
    { code: 'VE', name: 'Venezuela' },
    { code: 'VN', name: 'Vietnam' },
    { code: 'YE', name: 'Yemen' },
    { code: 'ZM', name: 'Zambia' },
    { code: 'ZW', name: 'Zimbabwe' }
];

const flagMap = {};
countryList.forEach(c => {
    const code = c.code.toUpperCase();
    const flag = String.fromCodePoint(0x1F1E6 + code.charCodeAt(0) - 65, 0x1F1E6 + code.charCodeAt(1) - 65);
    flagMap[c.code] = flag;
});

const countryLatMap = {
    'AF': 33.9, 'AL': 41.2, 'DZ': 28.0, 'AD': 42.5, 'AO': -12.5,
    'AG': 17.1, 'AR': -35.0, 'AM': 40.2, 'AU': -25.0, 'AT': 47.5,
    'AZ': 40.5, 'BS': 25.0, 'BH': 26.0, 'BD': 24.0, 'BB': 13.2,
    'BY': 53.0, 'BE': 50.8, 'BZ': 17.2, 'BJ': 9.5, 'BT': 27.5,
    'BO': -17.0, 'BA': 44.0, 'BW': -22.0, 'BR': -14.0, 'BN': 4.5,
    'BG': 42.7, 'BF': 12.0, 'BI': -3.5, 'KH': 13.0, 'CM': 6.0,
    'CA': 56.0, 'CV': 15.0, 'CF': 6.5, 'TD': 15.0, 'CL': -30.0,
    'CN': 35.0, 'CO': 4.0, 'KM': -12.2, 'CG': -1.0, 'CD': -4.0,
    'CR': 10.0, 'HR': 45.2, 'CU': 22.0, 'CY': 35.0, 'CZ': 49.8,
    'DK': 56.0, 'DJ': 11.8, 'DM': 15.4, 'DO': 18.7, 'EC': -2.0,
    'EG': 26.0, 'SV': 13.8, 'GQ': 1.5, 'ER': 15.0, 'EE': 58.5,
    'SZ': -26.5, 'ET': 8.0, 'FJ': -17.0, 'FI': 63.0, 'FR': 46.6,
    'GA': -1.0, 'GM': 13.5, 'GE': 42.0, 'DE': 51.0, 'GH': 7.8,
    'GR': 38.0, 'GD': 12.1, 'GT': 15.8, 'GN': 10.0, 'GW': 12.0,
    'GY': 5.0, 'HT': 19.0, 'HN': 14.0, 'HU': 47.0, 'IS': 65.0,
    'IN': 20.0, 'ID': -5.0, 'IR': 32.0, 'IQ': 33.0, 'IE': 53.0,
    'IL': 31.5, 'IT': 42.0, 'JM': 18.1, 'JP': 36.0, 'JO': 31.0,
    'KZ': 48.0, 'KE': 0.0, 'KI': 1.0, 'KP': 40.0, 'KR': 36.5,
    'KW': 29.5, 'KG': 41.5, 'LA': 18.0, 'LV': 57.0, 'LB': 34.0,
    'LS': -29.5, 'LR': 6.5, 'LY': 26.0, 'LI': 47.2, 'LT': 55.0,
    'LU': 49.8, 'MG': -19.0, 'MW': -13.5, 'MY': 2.5, 'MV': 3.2,
    'ML': 17.0, 'MT': 35.9, 'MH': 7.0, 'MR': 20.0, 'MU': -20.2,
    'MX': 23.0, 'FM': 7.0, 'MD': 47.0, 'MC': 43.7, 'MN': 46.0,
    'ME': 42.5, 'MA': 31.0, 'MZ': -18.0, 'MM': 22.0, 'NA': -22.0,
    'NR': -0.5, 'NP': 28.0, 'NL': 52.3, 'NZ': -41.0, 'NI': 13.0,
    'NE': 17.0, 'NG': 9.0, 'MK': 41.6, 'NO': 61.0, 'OM': 21.0,
    'PK': 30.0, 'PW': 7.5, 'PA': 8.5, 'PG': -6.0, 'PY': -23.0,
    'PE': -9.0, 'PH': 13.0, 'PL': 52.0, 'PT': 39.5, 'QA': 25.5,
    'RO': 46.0, 'RU': 61.0, 'RW': -2.0, 'KN': 17.3, 'LC': 13.9,
    'VC': 13.2, 'WS': -13.5, 'SM': 43.9, 'ST': 0.2, 'SA': 24.0,
    'SN': 14.0, 'RS': 44.0, 'SC': -4.6, 'SL': 8.5, 'SG': 1.3,
    'SK': 48.7, 'SI': 46.0, 'SB': -9.0, 'SO': 6.0, 'ZA': -30.0,
    'SS': 7.0, 'ES': 40.0, 'LK': 7.5, 'SD': 15.0, 'SR': 4.0,
    'SE': 62.0, 'CH': 46.8, 'SY': 35.0, 'TW': 23.5, 'TJ': 39.0,
    'TZ': -6.0, 'TH': 14.0, 'TL': -8.9, 'TG': 8.5, 'TO': -21.0,
    'TT': 10.5, 'TN': 34.0, 'TR': 39.0, 'TM': 40.0, 'TV': -7.0,
    'UG': 1.0, 'UA': 49.0, 'AE': 24.0, 'GB': 54.0, 'US': 38.0,
    'UY': -33.0, 'UZ': 41.0, 'VU': -16.0, 'VA': 41.9, 'VE': 7.0,
    'VN': 16.0, 'YE': 15.5, 'ZM': -14.0, 'ZW': -19.0
};

// ── State ──
let currentCity = 'Unknown';
let currentCountry = 'Unknown';
let currentCountryCode = '';
let currentRegion = '';
let currentTemp = null;
let currentCondition = '';
let currentWeatherEmoji = '🌤️';
let currentFlag = '🌍';
let currentLat = 0;
let currentLon = 0;
let currentTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';

let activeToast = null;
let activeCanvas = null;
let activeSeason = 'summer';
let canvasAnimId = null;
let toastTimer = null;
let blobInstances = [];

const container = document.getElementById('toastContainer');
const countrySelect = document.getElementById('weatherCountrySelect');

// ── Toast controller ──
function createToast() {
    if (activeToast) {
        activeToast.close();
        activeToast = null;
    }
    container.innerHTML = '';
    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.innerHTML = `
        <div class="toast-scene">
            <canvas class="toast-canvas" id="sceneCanvas"></canvas>
        </div>
        <div class="toast-content">
            <div class="toast-icon" id="toastIcon"><div class="spinner"></div></div>
            <div class="toast-text">
                <div class="main" id="toastMain">⏳ Detecting...</div>
                <div class="sub" id="toastSub"></div>
                <div class="time-row" id="timeRow">
                    <span class="clock" id="clockText">--:--</span>
                    <span class="date" id="dateText"></span>
                </div>
                <div class="weather-row" id="weatherRow" style="display:none;">
                    <span class="weather-emoji" id="weatherEmoji">🌤️</span>
                    <span class="temp" id="weatherTemp">--°C</span>
                    <span class="condition" id="weatherCondition">--</span>
                </div>
                <div class="fetch-status" id="fetchStatus"></div>
            </div>
            <button class="close-btn" id="closeToastBtn">✕</button>
        </div>
        <div class="toast-progress" id="toastProgress"></div>
    `;
    container.appendChild(toast);

    const canvas = document.getElementById('sceneCanvas');
    const ctx = canvas.getContext('2d');

    const obj = {
        toast,
        canvas,
        ctx,
        icon: document.getElementById('toastIcon'),
        main: document.getElementById('toastMain'),
        sub: document.getElementById('toastSub'),
        progress: document.getElementById('toastProgress'),
        weatherRow: document.getElementById('weatherRow'),
        weatherEmoji: document.getElementById('weatherEmoji'),
        weatherTemp: document.getElementById('weatherTemp'),
        weatherCondition: document.getElementById('weatherCondition'),
        fetchStatus: document.getElementById('fetchStatus'),
        closeBtn: document.getElementById('closeToastBtn'),
        clockText: document.getElementById('clockText'),
        dateText: document.getElementById('dateText'),
        _clockInterval: null,
        startClock() {
            const tick = () => {
                const now = new Date();
                let timeStr, dateStr;
                try {
                    timeStr = new Intl.DateTimeFormat([], { hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: currentTimezone }).format(now);
                    dateStr = new Intl.DateTimeFormat([], { weekday: 'short', month: 'short', day: 'numeric', timeZone: currentTimezone }).format(now);
                } catch (e) {
                    timeStr = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
                    dateStr = now.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
                }
                if (this.clockText) this.clockText.textContent = timeStr;
                if (this.dateText) this.dateText.textContent = dateStr;
            };
            tick();
            this._clockInterval = setInterval(tick, 1000);
        },
        update(mainText, subText = '', iconHtml = null, progress = null) {
            this.main.textContent = mainText;
            if (subText) {
                this.sub.textContent = subText;
                this.sub.classList.add('visible');
            } else {
                this.sub.classList.remove('visible');
            }
            if (iconHtml !== null) this.icon.innerHTML = iconHtml;
            if (progress !== null && progress > 0) {
                this.progress.style.width = Math.min(progress, 100) + '%';
                this.progress.style.display = 'block';
            } else {
                this.progress.style.width = '0%';
                this.progress.style.display = 'none';
            }
        },
        updateWeather(tempC, condition, emoji) {
            this.weatherRow.style.display = 'flex';
            this.weatherTemp.textContent = `${Math.round(tempC)}°C`;
            this.weatherCondition.textContent = condition || '';
            this.weatherEmoji.textContent = emoji || '🌤️';
        },
        setStatus(text) {
            this.fetchStatus.textContent = text;
        },
        close() {
            this.toast.classList.remove('show');
            if (toastTimer) {
                clearTimeout(toastTimer);
                toastTimer = null;
            }
            if (canvasAnimId) {
                cancelAnimationFrame(canvasAnimId);
                canvasAnimId = null;
            }
            if (this._resizeHandler) {
                window.removeEventListener('resize', this._resizeHandler);
                this._resizeHandler = null;
            }
            if (this._clockInterval) {
                clearInterval(this._clockInterval);
                this._clockInterval = null;
            }
            setTimeout(() => {
                if (this.toast.parentNode) this.toast.remove();
                if (activeToast === this) activeToast = null;
            }, 600);
        }
    };

    obj.closeBtn.addEventListener('click', () => obj.close());
    obj.startClock();

    toast.classList.add('show');

    activeToast = obj;
    activeCanvas = canvas;
    return obj;
}

// ── Canvas sizing ──
function sizeCanvas(canvas) {
    const rect = canvas.parentElement.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const cssW = rect.width, cssH = rect.height;
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    canvas.style.width = cssW + 'px';
    canvas.style.height = cssH + 'px';
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { w: cssW, h: cssH };
}

// ── Walking blob ──
function createBlob(x, y, emoji, color, speed = 1.0, size = 22) {
    return {
        x, y, baseY: y, emoji, color,
        speed: speed * (0.6 + Math.random() * 0.5),
        size,
        direction: Math.random() > 0.5 ? 1 : -1,
        stepPhase: Math.random() * Math.PI * 2,
        walkCycle: Math.random() * 100,
        armSwing: 0,
        legOffset: 0,
        pauseTimer: 0,
        isPaused: false,
        pauseDuration: 0,
        eyeColor: '#2b2b2b',
        blush: true,
        hasDrink: false,
        isDrinking: false,
        drinkTimer: 0,
        drinkCooldown: 100 + Math.random() * 200,
        drinkProgress: 0
    };
}
function updateBlob(blob, w, h, t, speedMul = 1) {
    if (!blob) return;
    const spd = blob.speed * speedMul * 0.6;
    if (blob.hasDrink) {
        if (blob.isDrinking) {
            blob.drinkTimer += 1;
            blob.drinkProgress = Math.min(1, blob.drinkTimer / 30);
            if (blob.drinkTimer > 70) {
                blob.isDrinking = false;
                blob.drinkTimer = 0;
                blob.drinkProgress = 0;
                blob.drinkCooldown = 150 + Math.random() * 250;
            }
            return;
        } else {
            blob.drinkCooldown -= 1;
            if (blob.drinkCooldown <= 0) {
                blob.isDrinking = true;
                blob.isPaused = false;
                blob.drinkTimer = 0;
                blob.drinkProgress = 0;
                return;
            }
        }
    }
    if (blob.isPaused) {
        blob.pauseTimer += 1;
        if (blob.pauseTimer > blob.pauseDuration) {
            blob.isPaused = false;
            blob.pauseTimer = 0;
            if (Math.random() > 0.5) blob.direction *= -1;
        }
        return;
    }
    if (Math.random() < 0.003) {
        blob.isPaused = true;
        blob.pauseTimer = 0;
        blob.pauseDuration = 40 + Math.random() * 80;
        return;
    }
    blob.x += blob.direction * spd * 1.2;
    blob.stepPhase += spd * 0.06;
    blob.walkCycle += spd * 0.04;
    blob.armSwing = Math.sin(blob.walkCycle) * 0.3;
    blob.legOffset = Math.sin(blob.walkCycle);
    const bobAmt = 2.5 + Math.abs(Math.sin(blob.walkCycle)) * 2;
    blob.y = blob.baseY + Math.sin(blob.walkCycle) * bobAmt;
    blob.headTilt = Math.sin(blob.walkCycle * 0.5) * 0.04;
    const margin = 30 + blob.size;
    if (blob.x > w - margin) { blob.direction = -1; blob.x = w - margin; }
    if (blob.x < margin) { blob.direction = 1; blob.x = margin; }
}

// ─── Full Walking Blob Drawing ──
function drawWalkingBlob(ctx, blob, t) {
    const { x, y, size: r, color, emoji, direction, walkCycle, isDrinking } = blob;
    const d = direction;
    const drinking = isDrinking;
    const lift = drinking ? Math.min(1, blob.drinkProgress * 1.3) : 0;

    ctx.save();
    ctx.translate(x, y);

    ctx.fillStyle = 'rgba(0,0,0,0.10)';
    ctx.beginPath();
    ctx.ellipse(0, r * 1.15, r * 0.8, r * 0.25, 0, 0, Math.PI * 2);
    ctx.fill();

    const legLen = r * 0.7;
    const legThick = r * 0.16;
    const legPhase = drinking ? 0.3 : walkCycle;
    ctx.strokeStyle = color;
    ctx.lineWidth = legThick;
    ctx.lineCap = 'round';
    const lx1 = -r * 0.2, ly1 = r * 0.75;
    const lx2 = lx1 + Math.sin(legPhase + 0.3) * r * 0.4 * d * (drinking ? 0 : 1);
    const ly2 = ly1 + legLen * 0.8 + Math.abs(Math.sin(legPhase + 0.3)) * r * 0.15 * (drinking ? 0.3 : 1);
    ctx.beginPath();
    ctx.moveTo(lx1, ly1);
    ctx.quadraticCurveTo((lx1+lx2)/2 + Math.sin(legPhase+0.3)*r*0.2*d*(drinking?0:1), ly1+legLen*0.5, lx2, ly2);
    ctx.stroke();

    const rx1 = r * 0.2, ry1 = r * 0.75;
    const rx2 = rx1 + Math.sin(legPhase + 0.3 + Math.PI) * r * 0.4 * d * (drinking ? 0 : 1);
    const ry2 = ry1 + legLen * 0.8 + Math.abs(Math.sin(legPhase + 0.3 + Math.PI)) * r * 0.15 * (drinking ? 0.3 : 1);
    ctx.beginPath();
    ctx.moveTo(rx1, ry1);
    ctx.quadraticCurveTo((rx1+rx2)/2 + Math.sin(legPhase+0.3+Math.PI)*r*0.2*d*(drinking?0:1), ry1+legLen*0.5, rx2, ry2);
    ctx.stroke();

    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.ellipse(lx2, ly2 + legThick*0.5, r*0.2, r*0.12, 0, 0, Math.PI*2);
    ctx.fill();
    ctx.beginPath();
    ctx.ellipse(rx2, ry2 + legThick*0.5, r*0.2, r*0.12, 0, 0, Math.PI*2);
    ctx.fill();

    ctx.fillStyle = color;
    ctx.shadowBlur = 10;
    ctx.shadowOffsetY = 3;
    ctx.beginPath();
    ctx.ellipse(0, 0, r, r * 1.05 * (1 + Math.sin(walkCycle)*0.04), 0, 0, Math.PI*2);
    ctx.fill();

    ctx.shadowBlur = 0;
    ctx.fillStyle = 'rgba(255,255,255,0.20)';
    ctx.beginPath();
    ctx.ellipse(-r*0.3*d, -r*0.35, r*0.25, r*0.15, -0.3*d, 0, Math.PI*2);
    ctx.fill();

    ctx.strokeStyle = color;
    ctx.lineWidth = r * 0.14;
    ctx.lineCap = 'round';
    const freeArmSide = -d;
    const flx1 = freeArmSide * r * 0.85, fly1 = -r * 0.15;
    const flx2 = flx1 + (drinking ? 0 : Math.sin(walkCycle+0.8) * r * 0.45 * freeArmSide);
    const fly2 = fly1 + r * 0.5 + (drinking ? 0 : Math.abs(Math.sin(walkCycle+0.8)) * r * 0.1);
    ctx.beginPath();
    ctx.moveTo(flx1, fly1);
    ctx.quadraticCurveTo((flx1+flx2)/2, fly1+r*0.3, flx2, fly2);
    ctx.stroke();
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(flx2, fly2, r*0.1, 0, Math.PI*2);
    ctx.fill();

    const drinkSide = d;
    const dax1 = drinkSide * r * 0.85, day1 = -r * 0.15;
    let dax2, day2;
    if (blob.hasDrink) {
        const restX = dax1 + Math.sin(walkCycle+0.8+Math.PI) * r * 0.45 * drinkSide;
        const restY = day1 + r * 0.5 + Math.abs(Math.sin(walkCycle+0.8+Math.PI)) * r * 0.1;
        const raisedX = drinkSide * r * 0.35;
        const raisedY = -r * 0.55;
        dax2 = restX + (raisedX - restX) * lift;
        day2 = restY + (raisedY - restY) * lift;
    } else {
        dax2 = dax1 + Math.sin(walkCycle+0.8+Math.PI) * r * 0.45 * drinkSide;
        day2 = day1 + r * 0.5 + Math.abs(Math.sin(walkCycle+0.8+Math.PI)) * r * 0.1;
    }
    ctx.beginPath();
    ctx.moveTo(dax1, day1);
    ctx.quadraticCurveTo((dax1+dax2)/2 + (lift>0?0:Math.sin(walkCycle+0.8+Math.PI)*r*0.2*drinkSide), day1+r*0.3, dax2, day2);
    ctx.stroke();
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(dax2, day2, r*0.1, 0, Math.PI*2);
    ctx.fill();

    if (blob.hasDrink) {
        ctx.save();
        ctx.translate(dax2, day2);
        ctx.rotate(-drinkSide * (0.15 + lift * 0.9));
        const canW = r*0.22, canH = r*0.5;
        const canGrad = ctx.createLinearGradient(-canW/2, 0, canW/2, 0);
        canGrad.addColorStop(0, '#e53935');
        canGrad.addColorStop(0.5, '#ff6f60');
        canGrad.addColorStop(1, '#c62828');
        ctx.fillStyle = canGrad;
        ctx.shadowColor = 'rgba(0,0,0,0.25)';
        ctx.shadowBlur = 3;
        ctx.beginPath();
        ctx.roundRect(-canW/2, -canH*0.75, canW, canH, canW*0.25);
        ctx.fill();
        ctx.shadowBlur = 0;
        ctx.fillStyle = '#c0c0c0';
        ctx.beginPath();
        ctx.ellipse(0, -canH*0.75, canW*0.28, canW*0.12, 0, 0, Math.PI*2);
        ctx.fill();
        ctx.fillStyle = 'rgba(255,255,255,0.85)';
        ctx.fillRect(-canW/2, -canH*0.15, canW, canH*0.16);
        ctx.restore();
        if (lift > 0.7) {
            ctx.fillStyle = 'rgba(255,255,255,0.7)';
            for (let i=0; i<3; i++) {
                const bx = dax2 + Math.sin(t*0.1 + i) * r * 0.15;
                const by = day2 - r*0.3 - i*r*0.12 - (t*0.05) % (r*0.3);
                ctx.beginPath();
                ctx.arc(bx, by, r*0.03, 0, Math.PI*2);
                ctx.fill();
            }
        }
    }

    const eyeY = -r * 0.05;
    const eyeSpacing = r * 0.32;
    const eyeR = r * 0.22;
    const pupilR = r * 0.11;
    const lookX = d * 0.15;
    const blinkCycle = Math.sin(t * 0.03 + 2.3);
    const eyeSquash = drinking ? (0.3 + (1-lift)*0.5) : (blinkCycle > 0.965 ? 0.12 : 1);

    ctx.fillStyle = '#fff';
    ctx.shadowBlur = 2;
    ctx.shadowOffsetY = 1;
    ctx.beginPath();
    ctx.ellipse(-eyeSpacing, eyeY, eyeR, eyeR * eyeSquash, 0, 0, Math.PI*2);
    ctx.fill();
    ctx.beginPath();
    ctx.ellipse(eyeSpacing, eyeY, eyeR, eyeR * eyeSquash, 0, 0, Math.PI*2);
    ctx.fill();

    if (eyeSquash > 0.4) {
        ctx.fillStyle = blob.eyeColor || '#2b2b2b';
        ctx.shadowBlur = 0;
        ctx.beginPath();
        ctx.arc(-eyeSpacing + lookX*eyeR*0.5, eyeY, pupilR, 0, Math.PI*2);
        ctx.fill();
        ctx.beginPath();
        ctx.arc(eyeSpacing + lookX*eyeR*0.5, eyeY, pupilR, 0, Math.PI*2);
        ctx.fill();
        ctx.fillStyle = 'rgba(255,255,255,0.5)';
        ctx.beginPath();
        ctx.arc(-eyeSpacing + lookX*eyeR*0.5 - pupilR*0.3, eyeY - pupilR*0.35, pupilR*0.3, 0, Math.PI*2);
        ctx.fill();
        ctx.beginPath();
        ctx.arc(eyeSpacing + lookX*eyeR*0.5 - pupilR*0.3, eyeY - pupilR*0.35, pupilR*0.3, 0, Math.PI*2);
        ctx.fill();
    }

    if (blob.blush !== false) {
        ctx.fillStyle = 'rgba(255,110,110,0.25)';
        ctx.shadowBlur = 0;
        ctx.beginPath();
        ctx.ellipse(-eyeSpacing - r*0.25, r*0.2, r*0.2, r*0.12, 0, 0, Math.PI*2);
        ctx.fill();
        ctx.beginPath();
        ctx.ellipse(eyeSpacing + r*0.25, r*0.2, r*0.2, r*0.12, 0, 0, Math.PI*2);
        ctx.fill();
    }

    ctx.strokeStyle = '#2b2b2b';
    ctx.lineWidth = Math.max(1.5, r*0.06);
    ctx.shadowBlur = 0;
    if (drinking && lift > 0.5) {
        ctx.beginPath();
        ctx.arc(0, r*0.28, r*0.09, 0, Math.PI*2);
        ctx.stroke();
    } else {
        ctx.beginPath();
        ctx.arc(0, r*0.25, r*0.2, 0.15*Math.PI, 0.85*Math.PI);
        ctx.stroke();
    }

    ctx.shadowBlur = 6;
    ctx.shadowOffsetY = 2;
    ctx.font = `${Math.round(r*0.9)}px "Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji", sans-serif`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'bottom';
    ctx.fillStyle = '#fff';
    ctx.shadowColor = 'rgba(0,0,0,0.4)';
    ctx.fillText(emoji, 0, -r*1.2 - Math.abs(Math.sin(walkCycle))*2);

    ctx.restore();
}

// ─── Full Season drawing functions ──
function drawSummer(ctx, w, h, t, blobs) {
    const sky = ctx.createLinearGradient(0, 0, 0, h);
    sky.addColorStop(0, '#4a90d9');
    sky.addColorStop(0.6, '#87CEEB');
    sky.addColorStop(1, '#cdeffd');
    ctx.fillStyle = sky;
    ctx.fillRect(0, 0, w, h);

    const sunX = w*0.82, sunY = h*0.18;
    const grd = ctx.createRadialGradient(sunX, sunY, 0, sunX, sunY, 60);
    grd.addColorStop(0, 'rgba(255,240,150,1)');
    grd.addColorStop(0.5, 'rgba(255,200,50,0.8)');
    grd.addColorStop(1, 'rgba(255,200,50,0)');
    ctx.fillStyle = grd;
    ctx.beginPath();
    ctx.arc(sunX, sunY, 60, 0, Math.PI*2);
    ctx.fill();
    ctx.fillStyle = '#fdd835';
    ctx.beginPath();
    ctx.arc(sunX, sunY, 28, 0, Math.PI*2);
    ctx.fill();

    const seaTop = h*0.50, seaBottom = h*0.68;
    const sea = ctx.createLinearGradient(0, seaTop, 0, seaBottom);
    sea.addColorStop(0, '#1e88e5');
    sea.addColorStop(0.5, '#29b6f6');
    sea.addColorStop(1, '#4fc3f7');
    ctx.fillStyle = sea;
    ctx.fillRect(0, seaTop, w, seaBottom - seaTop);
    for (let i=0; i<4; i++) {
        const wy = seaTop + (i+1)*(seaBottom-seaTop)/5;
        ctx.strokeStyle = `rgba(255,255,255,${0.35 - i*0.06})`;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        for (let x=0; x<=w; x+=6) {
            const yy = wy + Math.sin(x*0.05 + t*0.02 + i) * 2.2;
            if (x===0) ctx.moveTo(x, yy);
            else ctx.lineTo(x, yy);
        }
        ctx.stroke();
    }
    ctx.fillStyle = '#f0d9a8';
    ctx.fillRect(0, seaBottom, w, h - seaBottom);
    drawHouse(ctx, w*0.22, seaBottom + (h-seaBottom)*0.62, 0.85, t, { wall:'#f5e6ca', roof:'#4fc3f7', trim:'#e8d5b5', flowers:true, lights:false });

    blobs.forEach(b => {
        updateBlob(b, w, h, t, 0.9);
        drawWalkingBlob(ctx, b, t);
    });
}

function drawWinter(ctx, w, h, t, blobs) {
    const sky = ctx.createLinearGradient(0, 0, 0, h);
    sky.addColorStop(0, '#0b1a2e');
    sky.addColorStop(0.3, '#1a2a4a');
    sky.addColorStop(0.6, '#2d4a6a');
    sky.addColorStop(1, '#4a6a8a');
    ctx.fillStyle = sky;
    ctx.fillRect(0, 0, w, h);

    for (let i=0; i<20; i++) {
        const x = (i*37+13)%w;
        const y = (i*29+7)%(h*0.5);
        const tw = 0.3 + Math.abs(Math.sin(t*0.015 + i*1.3))*0.7;
        ctx.fillStyle = `rgba(255,255,255,${tw})`;
        ctx.beginPath();
        ctx.arc(x, y, 0.8+tw*0.6, 0, Math.PI*2);
        ctx.fill();
    }
    const groundY = h*0.70;
    ctx.fillStyle = '#e8edf2';
    ctx.beginPath();
    ctx.moveTo(0, groundY);
    for (let x=0; x<=w; x+=6) {
        const yOff = Math.sin(x*0.04 + t*0.008)*3 + Math.sin(x*0.07 + t*0.015)*1.5;
        ctx.lineTo(x, groundY + yOff);
    }
    ctx.lineTo(w, h);
    ctx.lineTo(0, h);
    ctx.closePath();
    ctx.fill();

    drawHouse(ctx, w*0.22, groundY+6, 0.85, t, { wall:'#d5c8b0', roof:'#6d4c2f', trim:'#c9b89a', snow:true, lights:true, smoke:true });
    drawSnowman(ctx, w*0.72, groundY+4, 0.6, t);

    blobs.forEach(b => {
        updateBlob(b, w, h, t, 0.6);
        drawWalkingBlob(ctx, b, t);
    });
}

function drawAutumn(ctx, w, h, t, blobs) {
    const sky = ctx.createLinearGradient(0, 0, 0, h);
    sky.addColorStop(0, '#9bb7d4');
    sky.addColorStop(0.5, '#c9d9e8');
    sky.addColorStop(1, '#e8f0e8');
    ctx.fillStyle = sky;
    ctx.fillRect(0, 0, w, h);
    const grd = ctx.createLinearGradient(0, h*0.74, 0, h);
    grd.addColorStop(0, '#7cb342');
    grd.addColorStop(1, '#558b2f');
    ctx.fillStyle = grd;
    ctx.fillRect(0, h*0.74, w, h*0.26);

    drawTree(ctx, w*0.78, h*0.74, 1.0, t, 'maple');
    drawHouse(ctx, w*0.22, h*0.72, 0.9, t, { wall:'#e8d5b5', roof:'#8d6e63', trim:'#d4a373', flowers:true, lights:false });

    blobs.forEach(b => {
        updateBlob(b, w, h, t, 0.85);
        drawWalkingBlob(ctx, b, t);
    });
}

function drawSpring(ctx, w, h, t, blobs) {
    const sky = ctx.createLinearGradient(0, 0, 0, h);
    sky.addColorStop(0, '#81c784');
    sky.addColorStop(0.5, '#b2dfdb');
    sky.addColorStop(1, '#e0f7fa');
    ctx.fillStyle = sky;
    ctx.fillRect(0, 0, w, h);
    const grd = ctx.createLinearGradient(0, h*0.73, 0, h);
    grd.addColorStop(0, '#7cb342');
    grd.addColorStop(1, '#4caf50');
    ctx.fillStyle = grd;
    ctx.fillRect(0, h*0.73, w, h*0.27);

    drawTree(ctx, w*0.78, h*0.725, 1.0, t, 'sakura');
    drawHouse(ctx, w*0.22, h*0.71, 0.9, t, { wall:'#f5e6ca', roof:'#6d4c2f', trim:'#d7ccc8', flowers:true, lights:false });

    blobs.forEach(b => {
        updateBlob(b, w, h, t, 1.0);
        drawWalkingBlob(ctx, b, t);
    });
}

function drawHouse(ctx, x, y, s, t, opts) {
    const wall = opts.wall || '#f5e6ca';
    const roof = opts.roof || '#b23b3b';
    const trim = opts.trim || '#e8d5b5';
    const hasSnow = opts.snow || false;
    const hasLights = opts.lights || false;
    const chimneySmoke = opts.smoke || false;

    ctx.save();
    ctx.translate(x, y);
    const W = 58*s, H = 48*s;
    const hw = W/2, hh = H/2;

    ctx.shadowColor = 'rgba(0,0,0,0.15)';
    ctx.shadowBlur = 16*s;
    ctx.shadowOffsetY = 4*s;

    ctx.fillStyle = wall;
    ctx.shadowColor = 'rgba(0,0,0,0.10)';
    ctx.shadowBlur = 12*s;
    ctx.shadowOffsetY = 3*s;
    ctx.beginPath();
    ctx.roundRect(-hw, -hh+4*s, W, H-4*s, 2*s);
    ctx.fill();

    ctx.shadowColor = 'rgba(0,0,0,0.15)';
    ctx.shadowBlur = 10*s;
    ctx.shadowOffsetY = 4*s;
    ctx.fillStyle = roof;
    ctx.beginPath();
    ctx.moveTo(-hw-10*s, -hh+4*s);
    ctx.lineTo(0, -hh-22*s);
    ctx.lineTo(hw+10*s, -hh+4*s);
    ctx.closePath();
    ctx.fill();

    ctx.shadowBlur = 6*s;
    ctx.fillStyle = trim;
    ctx.beginPath();
    ctx.moveTo(-hw-11*s, -hh+6*s);
    ctx.lineTo(-hw-6*s, -hh+2*s);
    ctx.lineTo(hw+6*s, -hh+2*s);
    ctx.lineTo(hw+11*s, -hh+6*s);
    ctx.closePath();
    ctx.fill();

    ctx.shadowBlur = 6*s;
    ctx.shadowOffsetY = 2*s;
    ctx.fillStyle = '#8a6a52';
    ctx.beginPath();
    ctx.roundRect(hw-10*s, -hh-18*s, 7*s, 16*s, 1*s);
    ctx.fill();
    ctx.fillStyle = '#7a5a42';
    ctx.fillRect(hw-11*s, -hh-20*s, 9*s, 3*s);

    if (chimneySmoke) {
        ctx.shadowBlur = 0;
        for (let i=0; i<4; i++) {
            const st = t*0.02 + i*1.7;
            const sx = hw-6*s + Math.sin(st)*5*s;
            const sy = -hh-22*s - i*7*s - (t*0.01)%6*s;
            const sr = 3*s + i*2.5*s + Math.sin(st*0.5)*2*s;
            const alpha = 0.25 - i*0.05;
            ctx.fillStyle = `rgba(200,200,200,${Math.max(0, alpha)})`;
            ctx.beginPath();
            ctx.arc(sx, sy, sr, 0, Math.PI*2);
            ctx.fill();
        }
    }

    ctx.shadowBlur = 4*s;
    ctx.shadowOffsetY = 2*s;
    ctx.fillStyle = '#6d4c2f';
    ctx.beginPath();
    ctx.roundRect(-5*s, -hh+18*s, 10*s, 18*s, 1.5*s);
    ctx.fill();
    ctx.fillStyle = '#5a3d2b';
    ctx.fillRect(-4*s, -hh+20*s, 3.5*s, 6*s);
    ctx.fillRect(0.5*s, -hh+20*s, 3.5*s, 6*s);
    ctx.fillRect(-4*s, -hh+28*s, 3.5*s, 6*s);
    ctx.fillRect(0.5*s, -hh+28*s, 3.5*s, 6*s);
    ctx.fillStyle = '#f0c040';
    ctx.shadowBlur = 0;
    ctx.beginPath();
    ctx.arc(4*s, -hh+25*s, 1.2*s, 0, Math.PI*2);
    ctx.fill();

    ctx.shadowBlur = 4*s;
    ctx.shadowOffsetY = 2*s;
    const winColor = hasLights ? '#ffdd77' : '#b3d9ff';
    ctx.fillStyle = winColor;
    ctx.beginPath();
    ctx.roundRect(-hw+6*s, -hh+12*s, 11*s, 12*s, 1*s);
    ctx.fill();
    ctx.strokeStyle = 'rgba(0,0,0,0.20)';
    ctx.lineWidth = 1.5*s;
    ctx.strokeRect(-hw+6*s, -hh+12*s, 11*s, 12*s);
    ctx.beginPath();
    ctx.moveTo(-hw+6*s, -hh+18*s);
    ctx.lineTo(-hw+17*s, -hh+18*s);
    ctx.moveTo(-hw+11.5*s, -hh+12*s);
    ctx.lineTo(-hw+11.5*s, -hh+24*s);
    ctx.stroke();

    ctx.fillStyle = winColor;
    ctx.beginPath();
    ctx.roundRect(hw-17*s, -hh+12*s, 11*s, 12*s, 1*s);
    ctx.fill();
    ctx.strokeStyle = 'rgba(0,0,0,0.20)';
    ctx.lineWidth = 1.5*s;
    ctx.strokeRect(hw-17*s, -hh+12*s, 11*s, 12*s);
    ctx.beginPath();
    ctx.moveTo(hw-17*s, -hh+18*s);
    ctx.lineTo(hw-6*s, -hh+18*s);
    ctx.moveTo(hw-11.5*s, -hh+12*s);
    ctx.lineTo(hw-11.5*s, -hh+24*s);
    ctx.stroke();

    if (hasLights) {
        ctx.shadowBlur = 0;
        const glow = ctx.createRadialGradient(x, y-hh+18*s, 0, x, y-hh+18*s, 20*s);
        glow.addColorStop(0, 'rgba(255,220,100,0.15)');
        glow.addColorStop(1, 'rgba(255,220,100,0)');
        ctx.fillStyle = glow;
        ctx.beginPath();
        ctx.arc(x, y-hh+18*s, 20*s, 0, Math.PI*2);
        ctx.fill();
    }

    if (hasSnow) {
        ctx.shadowBlur = 0;
        ctx.fillStyle = 'rgba(255,255,255,0.85)';
        ctx.beginPath();
        ctx.moveTo(-hw-8*s, -hh+5*s);
        ctx.quadraticCurveTo(-hw*0.4, -hh-16*s, 0, -hh-20*s);
        ctx.quadraticCurveTo(hw*0.4, -hh-16*s, hw+8*s, -hh+5*s);
        ctx.quadraticCurveTo(hw*0.6, -hh+2*s, 0, -hh+4*s);
        ctx.quadraticCurveTo(-hw*0.6, -hh+2*s, -hw-8*s, -hh+5*s);
        ctx.closePath();
        ctx.fill();
        ctx.fillStyle = 'rgba(255,255,255,0.8)';
        ctx.beginPath();
        ctx.ellipse(hw-6.5*s, -hh-20*s, 6*s, 2.5*s, 0, 0, Math.PI*2);
        ctx.fill();
    }

    if (opts.flowers) {
        ctx.shadowBlur = 0;
        const flowerColors = ['#e91e63','#ff5722','#ffeb3b','#4caf50','#9c27b0'];
        for (let side=-1; side<=1; side+=2) {
            const bx = side * (hw-14*s);
            ctx.fillStyle = '#6d4c2f';
            ctx.fillRect(bx-3*s, -hh+10*s, 6*s, 3*s);
            for (let f=0; f<3; f++) {
                const fx = bx + (f-1)*2*s + Math.sin(t*0.02+f+side)*0.3*s;
                const fy = -hh+8*s + Math.sin(t*0.025+f*1.2+side)*0.5*s;
                ctx.fillStyle = flowerColors[(f+side)%flowerColors.length];
                ctx.beginPath();
                ctx.arc(fx, fy, 1.8*s, 0, Math.PI*2);
                ctx.fill();
                ctx.fillStyle = '#4caf50';
                ctx.fillRect(fx-0.3*s, fy+1.5*s, 0.6*s, 2*s);
            }
        }
    }

    ctx.restore();
}

function drawTree(ctx, x, y, s, t, type) {
    ctx.save();
    ctx.translate(x, y);

    const sway = Math.sin(t*0.012)*0.035;
    ctx.rotate(sway);

    ctx.shadowColor = 'rgba(0,0,0,0.15)';
    ctx.shadowBlur = 14*s;
    ctx.shadowOffsetY = 4*s;
    ctx.fillStyle = 'rgba(0,0,0,0.12)';
    ctx.beginPath();
    ctx.ellipse(0, 4*s, 26*s, 8*s, 0, 0, Math.PI*2);
    ctx.fill();
    ctx.shadowBlur = 0;
    ctx.shadowOffsetY = 0;

    const trunkH = 42*s;
    ctx.strokeStyle = '#6d4c2f';
    ctx.lineWidth = 7*s;
    ctx.lineCap = 'round';
    ctx.beginPath();
    ctx.moveTo(0, 4*s);
    ctx.lineTo(-2*s, -trunkH);
    ctx.stroke();

    ctx.lineWidth = 4*s;
    ctx.beginPath();
    ctx.moveTo(-1*s, -trunkH*0.55);
    ctx.quadraticCurveTo(-16*s, -trunkH*0.7, -20*s, -trunkH*0.95);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(-1.5*s, -trunkH*0.8);
    ctx.quadraticCurveTo(14*s, -trunkH*0.92, 20*s, -trunkH*1.1);
    ctx.stroke();

    let colors, highlight;
    if (type === 'maple') {
        colors = ['#b23b3b','#d84315','#e64a19','#a0392f','#c62828'];
        highlight = 'rgba(255,200,150,0.18)';
    } else {
        colors = ['#f8bbd0','#f48fb1','#fce4ec','#f06292','#f5c1d9'];
        highlight = 'rgba(255,255,255,0.35)';
    }

    const canopyY = -trunkH - 6*s;
    const clusters = [
        { dx:0, dy:-8, r:24 },
        { dx:-20, dy:2, r:17 },
        { dx:20, dy:0, r:18 },
        { dx:-10, dy:14, r:15 },
        { dx:12, dy:15, r:15 },
        { dx:0, dy:6, r:20 }
    ];

    clusters.forEach((c, i) => {
        const wob = Math.sin(t*0.01 + i*1.7)*1.2*s;
        ctx.fillStyle = colors[i % colors.length];
        ctx.shadowColor = 'rgba(0,0,0,0.10)';
        ctx.shadowBlur = 6*s;
        ctx.shadowOffsetY = 2*s;
        ctx.beginPath();
        ctx.arc(c.dx*s + wob, canopyY + c.dy*s, c.r*s, 0, Math.PI*2);
        ctx.fill();
    });

    ctx.shadowBlur = 0;
    ctx.fillStyle = highlight;
    ctx.beginPath();
    ctx.arc(-8*s, canopyY-10*s, 14*s, 0, Math.PI*2);
    ctx.fill();

    ctx.restore();

    const particleCount = type === 'maple' ? 12 : 18;
    for (let i=0; i<particleCount; i++) {
        const seed = i*13.7;
        const fallT = (t*0.02 + seed) % 40;
        const px = x + Math.sin(seed)*34*s + Math.sin(t*0.006+seed)*10*s;
        const py = y - trunkH*0.6 + (fallT/40)*(trunkH*1.15);
        const drift = Math.sin(t*0.03 + seed*2)*6*s;
        const alpha = Math.max(0, 1 - fallT/40);
        if (alpha <= 0) continue;
        ctx.save();
        ctx.translate(px + drift, py);
        ctx.rotate(t*0.02 + seed);
        ctx.globalAlpha = alpha * 0.85;
        ctx.fillStyle = colors[i % colors.length];
        if (type === 'maple') {
            ctx.beginPath();
            ctx.ellipse(0, 0, 2.6*s, 1.6*s, 0, 0, Math.PI*2);
            ctx.fill();
        } else {
            ctx.beginPath();
            ctx.ellipse(0, 0, 2*s, 1.3*s, 0, 0, Math.PI*2);
            ctx.fill();
            ctx.beginPath();
            ctx.ellipse(0, -1.6*s, 1.6*s, 1*s, 0, 0, Math.PI*2);
            ctx.fill();
        }
        ctx.restore();
    }
}

function drawSnowman(ctx, x, y, s, t) {
    ctx.save();
    ctx.translate(x, y);
    const wobble = Math.sin(t*0.015)*1.2;
    ctx.fillStyle = 'rgba(0,0,0,0.08)';
    ctx.beginPath();
    ctx.ellipse(0, 38*s, 24*s, 6*s, 0, 0, Math.PI*2);
    ctx.fill();
    ctx.fillStyle = '#f0f4f8';
    ctx.shadowColor = 'rgba(0,0,0,0.08)';
    ctx.shadowBlur = 6*s;
    ctx.shadowOffsetY = 2*s;
    ctx.beginPath();
    ctx.arc(0, 24*s + wobble*0.3, 22*s, 0, Math.PI*2);
    ctx.fill();
    ctx.beginPath();
    ctx.arc(0, 4*s + wobble*0.5, 16*s, 0, Math.PI*2);
    ctx.fill();
    ctx.beginPath();
    ctx.arc(0, -16*s + wobble*0.7, 11*s, 0, Math.PI*2);
    ctx.fill();

    ctx.shadowBlur = 0;
    ctx.fillStyle = '#37474f';
    ctx.fillRect(-16*s, -26*s, 32*s, 5*s);
    ctx.fillRect(-10*s, -38*s, 20*s, 14*s);

    ctx.fillStyle = '#e53935';
    ctx.fillRect(-18*s, -6*s + wobble*0.4, 36*s, 5*s);
    ctx.fillRect(-16*s, -4*s + wobble*0.4, 6*s, 10*s);

    ctx.fillStyle = '#1a1a1a';
    ctx.beginPath();
    ctx.arc(-5*s, -18*s + wobble*0.6, 2*s, 0, Math.PI*2);
    ctx.fill();
    ctx.beginPath();
    ctx.arc(5*s, -18*s + wobble*0.6, 2*s, 0, Math.PI*2);
    ctx.fill();

    ctx.fillStyle = '#ff8a65';
    ctx.beginPath();
    ctx.moveTo(0, -14*s + wobble*0.6);
    ctx.lineTo(12*s, -15*s + wobble*0.5);
    ctx.lineTo(0, -16*s + wobble*0.5);
    ctx.closePath();
    ctx.fill();

    ctx.strokeStyle = '#1a1a1a';
    ctx.lineWidth = 1.5*s;
    ctx.beginPath();
    ctx.arc(0, -12*s + wobble*0.5, 6*s, 0.1*Math.PI, 0.9*Math.PI);
    ctx.stroke();

    ctx.fillStyle = '#1a1a1a';
    for (let i=-1; i<=1; i++) {
        ctx.beginPath();
        ctx.arc(0, (2+i*6)*s + wobble*0.4, 1.8*s, 0, Math.PI*2);
        ctx.fill();
    }
    ctx.restore();
}

function renderScene(ctx, w, h, season, time) {
    ctx.clearRect(0, 0, w, h);

    if (blobInstances.length === 0) {
        const blobConfigs = [
            { x: w*0.7, y: h*0.72, emoji: '🌻', color: '#ffb74d', speed: 1.0, size: 20 },
            { x: w*0.15, y: h*0.78, emoji: '🐸', color: '#a5d6a7', speed: 0.8, size: 16 },
            { x: w*0.45, y: h*0.76, emoji: '🐝', color: '#fdd835', speed: 1.2, size: 15 },
        ];
        blobInstances = blobConfigs.map(cfg => createBlob(cfg.x, cfg.y, cfg.emoji, cfg.color, cfg.speed, cfg.size));
        blobInstances.forEach(b => {
            b.x = 30 + Math.random() * (w - 60);
            b.baseY = h*0.70 + Math.random()*0.12*h;
            b.y = b.baseY;
        });
    }

    blobInstances.forEach(b => {
        if (Math.abs(b.baseY - h*0.73) > 20) {
            b.baseY = h*0.70 + (b.baseY % (h*0.12));
            b.y = b.baseY;
        }
    });

    switch (season) {
        case 'summer': drawSummer(ctx, w, h, time, blobInstances); break;
        case 'winter': drawWinter(ctx, w, h, time, blobInstances); break;
        case 'autumn': drawAutumn(ctx, w, h, time, blobInstances); break;
        case 'spring': drawSpring(ctx, w, h, time, blobInstances); break;
        default: drawSummer(ctx, w, h, time, blobInstances);
    }
}

// ─── LOCATION & WEATHER helpers ──
async function getLocationFromIP() {
    try {
        const res = await fetch('https://ip-api.com/json/');
        if (!res.ok) throw new Error('IP API error');
        const data = await res.json();
        if (data.status === 'success') {
            return {
                city: data.city || 'Unknown',
                country: data.country || 'Unknown',
                countryCode: data.countryCode || '',
                lat: data.lat || 0,
                lon: data.lon || 0,
                region: data.regionName || '',
                timezone: data.timezone || null
            };
        }
        return null;
    } catch (e) {
        console.warn('IP geolocation failed:', e);
        return null;
    }
}

function getFlagFromCode(code) {
    if (!code) return '🌍';
    const upper = code.toUpperCase();
    if (upper.length === 2) {
        try {
            return String.fromCodePoint(0x1F1E6 + upper.charCodeAt(0) - 65, 0x1F1E6 + upper.charCodeAt(1) - 65);
        } catch (e) { return '🌍'; }
    }
    return '🌍';
}

function getSeasonForCountry(country, code, lat) {
    const month = new Date().getMonth() + 1;
    let effectiveLat = lat;
    if (!effectiveLat || Math.abs(effectiveLat) < 0.01) {
        if (code && countryLatMap[code]) {
            effectiveLat = countryLatMap[code];
        } else {
            effectiveLat = 30;
        }
    }
    const isNorth = effectiveLat > 0;
    if (Math.abs(effectiveLat) < 10) {
        if (month >= 11 || month <= 2) return 'winter';
        if (month >= 3 && month <= 5) return 'spring';
        if (month >= 6 && month <= 8) return 'summer';
        return 'autumn';
    }
    if (isNorth) {
        if (month >= 3 && month <= 5) return 'spring';
        if (month >= 6 && month <= 8) return 'summer';
        if (month >= 9 && month <= 11) return 'autumn';
        return 'winter';
    } else {
        if (month >= 3 && month <= 5) return 'autumn';
        if (month >= 6 && month <= 8) return 'winter';
        if (month >= 9 && month <= 11) return 'spring';
        return 'summer';
    }
}

function weatherEmojiFromCode(code) {
    if (code === 0) return '☀️';
    if (code >= 1 && code <= 3) return '⛅';
    if (code === 45 || code === 48) return '🌫️';
    if (code >= 51 && code <= 67) return '🌧️';
    if (code >= 71 && code <= 77) return '❄️';
    if (code >= 80 && code <= 82) return '🌧️';
    if (code >= 85 && code <= 86) return '❄️';
    if (code >= 95) return '⛈️';
    return '🌤️';
}
function weatherDescFromCode(code) {
    const map = {
        0: 'Clear sky', 1: 'Mainly clear', 2: 'Partly cloudy', 3: 'Overcast',
        45: 'Fog', 48: 'Rime fog',
        51: 'Light drizzle', 53: 'Drizzle', 55: 'Dense drizzle',
        56: 'Freezing drizzle', 57: 'Freezing drizzle',
        61: 'Slight rain', 63: 'Rain', 65: 'Heavy rain',
        66: 'Freezing rain', 67: 'Freezing rain',
        71: 'Slight snow', 73: 'Snow', 75: 'Heavy snow', 77: 'Snow grains',
        80: 'Rain showers', 81: 'Rain showers', 82: 'Violent showers',
        85: 'Snow showers', 86: 'Snow showers',
        95: 'Thunderstorm', 96: 'Thunderstorm', 99: 'Thunderstorm'
    };
    return map[code] || '--';
}

async function fetchWeather(lat, lon) {
    try {
        const url = `https://api.open-meteo.com/v1/forecast?latitude=${lat}&longitude=${lon}&current=temperature_2m,weather_code&timezone=auto`;
        const res = await fetch(url);
        if (!res.ok) throw new Error('Weather API error');
        const data = await res.json();
        const current = data.current || {};
        const tempC = typeof current.temperature_2m === 'number' ? current.temperature_2m : null;
        const code = current.weather_code;
        return {
            tempC,
            condition: weatherDescFromCode(code),
            emoji: weatherEmojiFromCode(code),
            region: '',
            city: '',
            country: '',
            lat, lon,
            timezone: data.timezone || null
        };
    } catch (err) {
        return null;
    }
}

async function fetchWeatherByCity(city) {
    try {
        const geoUrl = `https://geocoding-api.open-meteo.com/v1/search?name=${encodeURIComponent(city)}&count=1&language=en&format=json`;
        const geoRes = await fetch(geoUrl);
        if (!geoRes.ok) throw new Error('Geocoding error');
        const geoData = await geoRes.json();
        const place = geoData.results?.[0];
        if (!place) return null;
        const lat = place.latitude, lon = place.longitude;
        const wUrl = `https://api.open-meteo.com/v1/forecast?latitude=${lat}&longitude=${lon}&current=temperature_2m,weather_code&timezone=auto`;
        const wRes = await fetch(wUrl);
        if (!wRes.ok) throw new Error('Weather API error');
        const wData = await wRes.json();
        const current = wData.current || {};
        const tempC = typeof current.temperature_2m === 'number' ? current.temperature_2m : null;
        const code = current.weather_code;
        return {
            tempC,
            condition: weatherDescFromCode(code),
            emoji: weatherEmojiFromCode(code),
            region: place.admin1 || '',
            city: place.name || city,
            country: place.country || '',
            lat, lon,
            timezone: wData.timezone || null
        };
    } catch (err) {
        return null;
    }
}

// ── SHOW TOAST ──
async function showSeasonToast(season, city, country, code, region, lat, manual = false) {
    const toast = createToast();
    const canvas = toast.canvas;
    const emoji = season === 'spring' ? '🌸' : season === 'summer' ? '☀️' : season === 'autumn' ? '🍂' : '❄️';
    const name = season.charAt(0).toUpperCase() + season.slice(1);
    const flag = getFlagFromCode(code) || '🌍';
    let locationDisplay = city;
    if (!locationDisplay || locationDisplay === country || locationDisplay === code.toLowerCase()) {
        locationDisplay = country;
    } else if (region && region !== city && region !== 'Unknown') {
        locationDisplay += ', ' + region;
    }

    const mainText = `${flag} ${locationDisplay}`;
    const subText = `${emoji} ${name}`;

    toast.update(mainText, subText, emoji, 80);

    if (currentTemp !== null && currentTemp !== undefined) {
        toast.updateWeather(currentTemp, currentCondition || '', currentWeatherEmoji || '🌤️');
        toast.setStatus('✅ Weather loaded');
    } else {
        toast.setStatus('⏳ Loading weather...');
        try {
            let wData = null;
            if (city && city !== 'Unknown') {
                wData = await fetchWeatherByCity(city);
            }
            if ((!wData || wData.tempC === null) && lat) {
                wData = await fetchWeather(lat, 0);
            }
            if (wData && wData.tempC !== null) {
                currentTemp = wData.tempC;
                currentCondition = wData.condition || '';
                currentWeatherEmoji = wData.emoji || '🌤️';
                if (wData.region) currentRegion = wData.region;
                if (wData.city && wData.city !== 'Unknown') currentCity = wData.city;
                if (wData.country) currentCountry = wData.country;
                if (wData.lat) currentLat = wData.lat;
                if (wData.lon) currentLon = wData.lon;
                toast.updateWeather(currentTemp, currentCondition, currentWeatherEmoji);
                toast.setStatus('✅ Weather loaded');
            } else {
                toast.setStatus('⚠️ Weather unavailable');
            }
        } catch (e) {
            toast.setStatus('⚠️ Weather unavailable');
        }
    }

    // ─── Start animation ───
    blobInstances = [];

    function startAnimation() {
        const rect = canvas.parentElement.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0) {
            const dpr = window.devicePixelRatio || 1;
            canvas.width = rect.width * dpr;
            canvas.height = rect.height * dpr;
            canvas.style.width = rect.width + 'px';
            canvas.style.height = rect.height + 'px';
            const ctx = canvas.getContext('2d');
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            animateScene(canvas, season);
            return true;
        }
        return false;
    }

    if (!startAnimation()) {
        setTimeout(() => {
            if (!startAnimation()) {
                const ro = new ResizeObserver(() => {
                    if (startAnimation()) ro.disconnect();
                });
                ro.observe(canvas);
                setTimeout(() => ro.disconnect(), 2000);
            }
        }, 150);
    }

    if (!manual) {
        if (toastTimer) clearTimeout(toastTimer);
        toastTimer = setTimeout(() => {
            if (activeToast === toast) toast.close();
        }, 10000);
    }

    if (code) {
        const option = countrySelect.querySelector(`option[value="${code}"]`);
        if (option) countrySelect.value = code;
    }

    return toast;
}

// ── UPDATE FROM COUNTRY SELECT ──
async function updateFromCountry(code) {
    const country = countryList.find(c => c.code === code);
    if (!country) return;
    const name = country.name;
    const wData = await fetchWeatherByCity(name);
    let city = name;
    let region = '';
    let temp = null, cond = '', emoji = '🌤️';
    let lat = 0, lon = 0;
    let countryName = name;
    if (wData && wData.tempC !== null) {
        temp = wData.tempC;
        cond = wData.condition || '';
        emoji = wData.emoji || '🌤️';
        if (wData.city && wData.city !== name) city = wData.city;
        if (wData.region) region = wData.region;
        if (wData.country) countryName = wData.country;
        lat = wData.lat || 0;
        lon = wData.lon || 0;
        if (wData.timezone) currentTimezone = wData.timezone;
    }
    if (!lat || Math.abs(lat) < 0.01) {
        lat = countryLatMap[code] || 30;
    }
    currentCity = city;
    currentCountry = countryName;
    currentCountryCode = code;
    currentRegion = region;
    currentTemp = temp;
    currentCondition = cond;
    currentWeatherEmoji = emoji;
    currentFlag = getFlagFromCode(code);
    currentLat = lat;
    currentLon = lon;
    const season = getSeasonForCountry(countryName, code, lat);
    blobInstances = [];
    await showSeasonToast(season, city, countryName, code, region, lat, true);
}

// ── DETECT LOCATION ──
async function detectLocation() {
    let loc = await getLocationFromIP();
    if (loc && loc.countryCode) {
        const code = loc.countryCode;
        const country = countryList.find(c => c.code === code);
        if (country) {
            currentCountryCode = code;
            currentCountry = country.name;
            currentCity = loc.city || country.name;
            currentRegion = loc.region || '';
            currentFlag = getFlagFromCode(code);
            currentLat = loc.lat || 0;
            currentLon = loc.lon || 0;
            if (!currentLat || Math.abs(currentLat) < 0.01) {
                currentLat = countryLatMap[code] || 30;
            }
            const wData = await fetchWeather(loc.lat || currentLat, loc.lon || 0);
            if (wData && wData.tempC !== null) {
                currentTemp = wData.tempC;
                currentCondition = wData.condition || '';
                currentWeatherEmoji = wData.emoji || '🌤️';
                if (wData.city) currentCity = wData.city;
                if (wData.region) currentRegion = wData.region;
                if (wData.lat) currentLat = wData.lat;
                if (wData.lon) currentLon = wData.lon;
            }
            currentTimezone = (wData && wData.timezone) || loc.timezone || currentTimezone;
            const season = getSeasonForCountry(currentCountry, currentCountryCode, currentLat);
            blobInstances = [];
            await showSeasonToast(season, currentCity, currentCountry, currentCountryCode, currentRegion, currentLat, true);
            return;
        }
    }
    if (navigator.geolocation) {
        try {
            const pos = await new Promise((resolve, reject) => {
                navigator.geolocation.getCurrentPosition(resolve, reject, { timeout: 8000 });
            });
            const { latitude, longitude } = pos.coords;
            const wData = await fetchWeather(latitude, longitude);
            if (wData && wData.tempC !== null) {
                const city = wData.city || 'Unknown';
                const country = wData.country || 'Unknown';
                let code = '';
                const found = countryList.find(c => c.name === country);
                if (found) code = found.code;
                if (!code) {
                    for (const c of countryList) {
                        if (country.toLowerCase().includes(c.name.toLowerCase()) ||
                            c.name.toLowerCase().includes(country.toLowerCase())) {
                            code = c.code;
                            break;
                        }
                    }
                }
                currentCity = city;
                currentCountry = country;
                currentCountryCode = code;
                currentRegion = wData.region || '';
                currentTemp = wData.tempC;
                currentCondition = wData.condition || '';
                currentWeatherEmoji = wData.emoji || '🌤️';
                currentFlag = getFlagFromCode(code);
                currentLat = wData.lat || latitude;
                currentLon = wData.lon || longitude;
                currentTimezone = wData.timezone || currentTimezone;
                const season = getSeasonForCountry(country, code, currentLat);
                blobInstances = [];
                await showSeasonToast(season, city, country, code, currentRegion, currentLat, true);
                return;
            }
        } catch (e) {
            console.warn('Geolocation failed:', e);
        }
    }
    const fallbackCode = 'US';
    await updateFromCountry(fallbackCode);
}

// ── ANIMATION ──
function animateScene(canvas, season) {
    if (canvasAnimId) cancelAnimationFrame(canvasAnimId);
    if (!canvas) return;
    let { w, h } = sizeCanvas(canvas);
    const onResize = () => {
        if (canvas && document.body.contains(canvas)) {
            ({ w, h } = sizeCanvas(canvas));
            blobInstances.forEach(b => {
                b.baseY = h*0.70 + (b.baseY % (h*0.12));
                b.y = b.baseY;
            });
        }
    };
    window.addEventListener('resize', onResize);

    let start = performance.now();
    let activeSeason = season;

    function frame(now) {
        if (!canvas || !document.body.contains(canvas)) {
            cancelAnimationFrame(canvasAnimId);
            window.removeEventListener('resize', onResize);
            return;
        }
        const t = (now - start) * 0.6;
        const ctx = canvas.getContext('2d');
        const dpr = window.devicePixelRatio || 1;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        renderScene(ctx, w, h, activeSeason, t);
        canvasAnimId = requestAnimationFrame(frame);
    }
    frame(start);
}

// ── TOGGLE WEATHER ──
function toggleWeather() {
    if (container.style.display === 'none') {
        container.style.display = 'flex';
        if (!activeToast) {
            if (currentCountryCode) {
                updateFromCountry(currentCountryCode);
            } else {
                detectLocation();
            }
        } else {
            if (!document.querySelector('.toast')) {
                if (currentCountryCode) {
                    updateFromCountry(currentCountryCode);
                } else {
                    detectLocation();
                }
            }
        }
    } else {
        container.style.display = 'none';
        if (activeToast) activeToast.close();
    }
}

// ── INIT WEATHER ──
function initWeather() {
    countryList.sort((a, b) => a.name.localeCompare(b.name));
    countryList.forEach(c => {
        const opt = document.createElement('option');
        opt.value = c.code;
        const flag = getFlagFromCode(c.code);
        opt.textContent = `${flag} ${c.name}`;
        countrySelect.appendChild(opt);
    });
    countrySelect.addEventListener('change', (e) => {
        const code = e.target.value;
        if (code) {
            updateFromCountry(code);
        }
    });
    detectLocation();
}

// ── Override detectLocation to ensure container is shown ──
const originalDetect = detectLocation;
detectLocation = async function() {
    container.style.display = 'flex';
    await originalDetect();
};

console.log('🌍 Weather widget integrated with TrioForge');
</script>

</body>
</html>"""
    return html


# ── Routes ──
@app.route('/unload_model', methods=['POST'])
def unload_model():
    try:
        requests.post(
            "http://127.0.0.1:11434/api/generate",
            json={"model": current_model, "prompt": "", "keep_alive": 0},
            timeout=3
        )
    except:
        pass
    return '', 204

providers = {
    "ollama": OllamaProvider(model=current_model),
    "llamacpp": LlamaCppProvider(),
    "huggingface": HuggingFaceProvider(),
    "groq": GroqProvider(),
    "deepseek": DeepSeekProvider(),
    "claude": ClaudeProvider(),
}

@app.route('/')
def index():
    return build_html(current_model)

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
            except:
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
            except:
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
            resp = requests.post("http://127.0.0.1:11434/api/show", json={"name": model}, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                details = data.get("details", {})
                caps = details.get("capabilities", [])
                if "vision" in caps:
                    return True
                family = details.get("family", "").lower()
                vision_families = VISION_MODELS["ollama"]
                return any(kw in family for kw in vision_families)
        except:
            pass
    return model_supports_vision(provider_name, model)

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
    api_key = data.get('api_key', None)
    models = _cached_models(provider_name, api_key or 'None')
    return jsonify({'models': models})

@lru_cache(maxsize=128)
def _cached_models(provider_name, api_key):
    provider = providers.get(provider_name)
    if not provider:
        return []
    try:
        return provider.list_models(api_key=api_key if api_key != 'None' else None)
    except:
        return []

@app.route('/set_model', methods=['POST'])
def set_model():
    global current_model
    data = request.get_json()
    model = data.get('model')
    if not model:
        return jsonify({'error': 'No model provided'}), 400
    try:
        resp = requests.get("http://127.0.0.1:11434/api/tags", timeout=3)
        if resp.status_code == 200:
            models = [m['name'] for m in resp.json().get('models', [])]
            if model not in models:
                return jsonify({'error': f'Model "{model}" not found in Ollama. Please pull it first.'}), 400
    except:
        pass
    current_model = model
    save_model_config(model)
    providers["ollama"].model = model
    cached_vision_check.cache_clear()
    _cached_models.cache_clear()
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
    api_key = provider._default_key
    if api_key:
        try:
            headers = provider._get_headers(api_key)
            resp = requests.get("https://api.deepseek.com/v1/models", headers=headers, timeout=5)
            if resp.status_code == 200:
                return jsonify({"ok": True, "message": "API online"})
            else:
                return jsonify({"ok": False, "message": "API returned error"})
        except:
            return jsonify({"ok": False, "message": "API unreachable or invalid key"})
    else:
        return jsonify({"ok": False, "message": "No API key provided"})

@app.route('/conversations', methods=['GET'])
def list_conversations():
    convs = load_conversations()
    sorted_list = sorted(convs.values(), key=lambda c: (c.get('order', 0), c.get('created', '')))
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
def get_messages(cid):
    conv = get_conversation(cid)
    if conv is None:
        return jsonify([])
    return jsonify(conv.get("messages", []))

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
    convs = load_conversations()
    conv = convs.get(cid)
    if not conv:
        return jsonify({'error': 'Conversation not found'}), 404
    msgs = conv.get('messages', [])
    if idx < 0 or idx >= len(msgs):
        return jsonify({'error': 'Index out of range'}), 400
    msgs[idx]['text'] = new_text
    save_conversations_async(convs)
    return jsonify({'ok': True})

@app.route('/conversations/<cid>/messages/<int:idx>', methods=['DELETE'])
def delete_message(cid, idx):
    convs = load_conversations()
    conv = convs.get(cid)
    if not conv:
        return jsonify({'error': 'Conversation not found'}), 404
    msgs = conv.get('messages', [])
    if idx < 0 or idx >= len(msgs):
        return jsonify({'error': 'Index out of range'}), 400
    msgs.pop(idx)
    save_conversations_async(convs)
    return jsonify({'ok': True})

@app.route('/conversations/<cid>/rename', methods=['PUT'])
def rename_conversation(cid):
    data = request.get_json()
    new_title = data.get('title', '').strip()
    if not new_title:
        return jsonify({'error': 'Title cannot be empty'}), 400
    convs = load_conversations()
    if cid not in convs:
        return jsonify({'error': 'Conversation not found'}), 404
    convs[cid]['title'] = new_title
    save_conversations_async(convs)
    return jsonify({'ok': True})

@app.route('/conversations/reorder', methods=['POST'])
def reorder_conversations():
    data = request.get_json()
    order_map = data.get('order')
    if not order_map or not isinstance(order_map, dict):
        return jsonify({'error': 'Invalid order data'}), 400
    convs = load_conversations()
    for cid, new_order in order_map.items():
        if cid in convs:
            convs[cid]['order'] = int(new_order)
    save_conversations_async(convs)
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
        msg_match = False
        for msg in conv.get('messages', []):
            text = msg.get('text', '').lower()
            if query in text:
                msg_match = True
                break
        if title_match or msg_match:
            results.append({
                "id": conv["id"],
                "title": conv.get("title", "Untitled"),
                "created": conv.get("created", ""),
                "order": conv.get("order", 0)
            })
    results.sort(key=lambda c: (c.get('order', 0), c.get('created', '')))
    return jsonify(results)

# ── SQLite Logs API ──
@app.route('/api/logs')
def get_logs():
    page = request.args.get('page', 1, type=int)
    per_page = 50
    offset = (page - 1) * per_page
    conv_filter = request.args.get('conv_id', '').strip()
    date_from = request.args.get('from', '')
    date_to = request.args.get('to', '')
    with _sqlite_lock:
        cur = _sqlite_conn.cursor()
        query = "SELECT id, conversation_id, role, content, created_at FROM messages WHERE 1=1"
        count_query = "SELECT COUNT(*) FROM messages WHERE 1=1"
        params = []
        count_params = []
        if conv_filter:
            query += " AND conversation_id = ?"
            params.append(conv_filter)
            count_query += " AND conversation_id = ?"
            count_params.append(conv_filter)
        if date_from:
            query += " AND created_at >= ?"
            params.append(date_from)
            count_query += " AND created_at >= ?"
            count_params.append(date_from)
        if date_to:
            query += " AND created_at <= ?"
            params.append(date_to)
            count_query += " AND created_at <= ?"
            count_params.append(date_to)
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
    with _sqlite_lock:
        cur = _sqlite_conn.cursor()
        query = "SELECT id, conversation_id, role, content, created_at FROM messages WHERE 1=1"
        params = []
        if conv_filter:
            query += " AND conversation_id = ?"
            params.append(conv_filter)
        if date_from:
            query += " AND created_at >= ?"
            params.append(date_from)
        if date_to:
            query += " AND created_at <= ?"
            params.append(date_to)
        query += " ORDER BY created_at DESC"
        cur.execute(query, params)
        rows = cur.fetchall()
    import csv
    from io import StringIO
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Conversation ID', 'Role', 'Content', 'Created At'])
    for row in rows:
        writer.writerow(row)
    output.seek(0)
    return Response(output.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=conversation_logs.csv'})

# ── Chat endpoints ──
@app.route('/chat', methods=['POST'])
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
        api_key = data.get('api_key', None)

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
            ts = datetime.now().strftime("%H:%M")
            add_message(conv_id, "user", user_message, [], [], ts)
            add_message(conv_id, "bot", output, [], [], ts)
            return jsonify({'response': output})

        search_context = ""
        if SEARCH_AVAILABLE and search_enabled and user_message.strip():
            try:
                with DDGS() as ddgs:
                    results = ddgs.text(user_message, max_results=3)
                    snippets = [r['body'] for r in results if 'body' in r]
                    if snippets:
                        search_context = " ".join(snippets[:3])
            except Exception as e:
                print(f"❌ Search error: {e}")

        provider = providers.get(provider_name)
        if not provider:
            return jsonify({'error': f'Unknown provider: {provider_name}'}), 400

        system_prompt = provider.get_system_prompt()
        final_prompt = system_prompt + "\n\n"
        if search_context:
            final_prompt += (
                f"Web search results for '{user_message}':\n{search_context}\n\n"
                f"Based on these results, answer the user's question: {user_message}"
            )
        else:
            final_prompt += user_message

        for f in files:
            try:
                raw = base64.b64decode(f['b64']).decode('utf-8', errors='replace')
                if f['name'].lower().endswith(('.c', '.cpp', '.h', '.hpp')):
                    raw = strip_c_comments(raw)
                final_prompt += f"\n\n--- File: {f['name']} ---\n{raw[:8000]}"
            except:
                final_prompt += f"\n\n[Attached file: {f['name']} — binary]"

        conv = get_conversation(conv_id)
        messages = []
        if conv:
            for msg in conv.get('messages', []):
                if msg['role'] == 'user':
                    messages.append({"role": "user", "content": msg['text']})
                elif msg['role'] == 'bot':
                    messages.append({"role": "assistant", "content": msg['text']})

        messages = [{"role": "system", "content": system_prompt}] + messages
        messages = trim_conversation_history(messages)
        messages.append({"role": "user", "content": final_prompt})

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

        ts = datetime.now().strftime("%H:%M")
        original_message = data.get('message', '').strip()

        if not add_message(conv_id, "user", original_message, images, files, ts):
            return jsonify({'error': f'Failed to save user message to {conv_id}'}), 500
        if not add_message(conv_id, "bot", reply, [], [], ts):
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
        print(f"❌ Error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/chat_stream', methods=['POST'])
def chat_stream():
    try:
        data = request.get_json(force=True, silent=True) or {}
        user_message = data.get('message', '').strip()
        images = data.get('images', [])
        files = data.get('files', [])
        conv_id = data.get('conversation_id')
        search_enabled = data.get('search', False)
        model = data.get('model', current_model)
        api_key = data.get('api_key', None)

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

        search_context = ""
        if SEARCH_AVAILABLE and search_enabled and user_message.strip():
            try:
                with DDGS() as ddgs:
                    results = ddgs.text(user_message, max_results=3)
                    snippets = [r['body'] for r in results if 'body' in r]
                    if snippets:
                        search_context = " ".join(snippets[:3])
            except Exception as e:
                print(f"❌ Search error: {e}")

        provider = providers.get(provider_name)
        if not provider:
            return jsonify({'error': f'Unknown provider: {provider_name}'}), 400

        system_prompt = provider.get_system_prompt()
        final_prompt = system_prompt + "\n\n"
        if search_context:
            final_prompt += (
                f"Web search results for '{user_message}':\n{search_context}\n\n"
                f"Based on these results, answer the user's question: {user_message}"
            )
        else:
            final_prompt += user_message

        for f in files:
            try:
                raw = base64.b64decode(f['b64']).decode('utf-8', errors='replace')
                if f['name'].lower().endswith(('.c', '.cpp', '.h', '.hpp')):
                    raw = strip_c_comments(raw)
                final_prompt += f"\n\n--- File: {f['name']} ---\n{raw[:8000]}"
            except:
                final_prompt += f"\n\n[Attached file: {f['name']} — binary]"

        conv = get_conversation(conv_id)
        messages = []
        if conv:
            for msg in conv.get('messages', []):
                if msg['role'] == 'user':
                    messages.append({"role": "user", "content": msg['text']})
                elif msg['role'] == 'bot':
                    messages.append({"role": "assistant", "content": msg['text']})

        messages = [{"role": "system", "content": system_prompt}] + messages
        messages = trim_conversation_history(messages)
        messages.append({"role": "user", "content": final_prompt})

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
                    "http://127.0.0.1:11434/api/chat",
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

            ts = datetime.now().strftime("%H:%M")
            add_message(conv_id, "user", user_message, images, files, ts)
            add_message(conv_id, "bot", full_response, [], [], ts)

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
        print(f"❌ chat_stream error: {e}")
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

if __name__ == '__main__':
    print("\n" + "="*50)
    print("🚀  AI CHAT Interfacing Loading... · Multi-Conversation")
    print("="*50)
    print(f"  Default model : {DEFAULT_MODEL}")
    print(f"  Current model : {current_model}")
    print(f"  Storage       : {CONVERSATIONS_FILE}")
    print("="*50 + "\n")

    cert_file = 'cert_store/localhost+1.pem'
    key_file  = 'cert_store/localhost+1-key.pem'

    if os.path.exists(cert_file) and os.path.exists(key_file):
        ssl_context = (cert_file, key_file)
        print("🔒 Running with HTTPS (SSL enabled)")
        url = "https://localhost:5001"
    else:
        if ensure_certificates():
            ssl_context = (cert_file, key_file)
            print("🔒 Running with HTTPS (SSL enabled)")
            url = "https://localhost:5001"
        else:
            ssl_context = None
            print("⚠️  Running with HTTP (SSL unavailable)")
            url = "http://localhost:5001"

    print(f"🌐 Open your browser at: {url}")
    print("="*50 + "\n")

    app.run(host='127.0.0.1', port=5001, debug=True, use_reloader=False, ssl_context=ssl_context, threaded=True)