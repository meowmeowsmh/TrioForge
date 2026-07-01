# app.py – with automatic memory management (no manual sliders)
from flask import Flask, request, jsonify, Response
from flask import send_from_directory
import requests
import base64
import os
import json
from datetime import datetime
import uuid
import psutil
import subprocess
import re
import urllib.request
import platform
import sys
import time


# ── Import the provider classes ──
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

# ── NVIDIA GPU support (optional) ──
try:
    import pynvml
    pynvml.nvmlInit()
    NVML_AVAILABLE = True
except:
    NVML_AVAILABLE = False
    print("⚠️ NVML not available – GPU VRAM monitoring disabled.")

app = Flask(__name__)

DEFAULT_MODEL = "vaultbox/qwen3.5-uncensored:9b"
CONVERSATIONS_FILE = "json_configuration/conversations.json"
MODEL_CONFIG_FILE = "json_configuration/model_config.json"

# ── System prompt to encourage Markdown formatting ──
SYSTEM_PROMPT = (
    "Always format tabular data as Markdown tables with headers. "
    "Use **bold** for important terms or emphasis. "
    "Use bullet lists (- or *) for enumerations. "
    "Keep your responses clear, structured, and easy to read."
)

try:
    from duckduckgo_search import DDGS
    SEARCH_AVAILABLE = True
except ImportError:
    SEARCH_AVAILABLE = False

# ── Ensure the json_configuration folder exists ──
os.makedirs(os.path.dirname(CONVERSATIONS_FILE), exist_ok=True)
os.makedirs(os.path.dirname(MODEL_CONFIG_FILE), exist_ok=True)

# ── Create empty JSON files if they don't exist ──
if not os.path.exists(CONVERSATIONS_FILE):
    with open(CONVERSATIONS_FILE, "w", encoding="utf-8") as f:
        json.dump({}, f, ensure_ascii=False, indent=2)
    print(f"✅ Created {CONVERSATIONS_FILE}")

if not os.path.exists(MODEL_CONFIG_FILE):
    with open(MODEL_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"model": DEFAULT_MODEL}, f, ensure_ascii=False, indent=2)
    print(f"✅ Created {MODEL_CONFIG_FILE}")

# ── NOTES storage (built-in) ─────────────────────────────────
NOTES_FILE = "json_configuration/notes.json"
os.makedirs(os.path.dirname(NOTES_FILE), exist_ok=True)
if not os.path.exists(NOTES_FILE):
    with open(NOTES_FILE, "w", encoding="utf-8") as f:
        json.dump({}, f, indent=2)

def load_notes():
    with open(NOTES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_notes(notes):
    with open(NOTES_FILE, "w", encoding="utf-8") as f:
        json.dump(notes, f, indent=2)

# ── Auto‑SSL certificate generation ──
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
        print("   Install mkcert manually or run with HTTP.")
        return False

    mkcert_exe = "mkcert.exe"
    if not os.path.exists(mkcert_exe):
        print("📥 Downloading mkcert...")
        url = "https://github.com/FiloSottile/mkcert/releases/latest/download/mkcert-v1.4.4-windows-amd64.exe"
        try:
            urllib.request.urlretrieve(url, mkcert_exe)
            print("✅ mkcert downloaded")
        except Exception as e:
            print(f"❌ Failed to download mkcert: {e}")
            return False

    try:
        print("🔐 Installing Local Certificate Authority...")
        subprocess.run([mkcert_exe, "-install"], check=True, capture_output=True)

        print("📜 Generating certificates...")
        subprocess.run([mkcert_exe, "localhost", "127.0.0.1"], check=True)

        if os.path.exists("localhost+1.pem"):
            os.rename("localhost+1.pem", cert_file)
        if os.path.exists("localhost+1-key.pem"):
            os.rename("localhost+1-key.pem", key_file)

        print("✅ Certificates generated successfully!")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ Certificate generation failed: {e.stderr.decode() if e.stderr else ''}")
        return False

# ── Model persistence ──
def load_model_config():
    if os.path.exists(MODEL_CONFIG_FILE):
        try:
            with open(MODEL_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("model", DEFAULT_MODEL)
        except:
            pass
    return DEFAULT_MODEL

def save_model_config(model):
    with open(MODEL_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"model": model}, f, ensure_ascii=False, indent=2)

current_model = load_model_config()

# ── Conversation storage ──
_conversations_cache: dict = {}
_cache_loaded: bool = False

def _ensure_cache():
    global _conversations_cache, _cache_loaded
    if _cache_loaded:
        return
    if os.path.exists(CONVERSATIONS_FILE):
        try:
            with open(CONVERSATIONS_FILE, "r", encoding="utf-8") as f:
                _conversations_cache = json.load(f)
        except Exception as e:
            print(f"⚠️ Error loading conversations: {e}")
            _conversations_cache = {}
    _cache_loaded = True

def load_conversations():
    _ensure_cache()
    return _conversations_cache

def save_conversations(convs):
    global _conversations_cache
    _conversations_cache = convs
    try:
        with open(CONVERSATIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(convs, f, ensure_ascii=False, indent=2)
        print(f"✅ Saved conversations ({len(convs)} items)")
    except Exception as e:
        print(f"❌ Failed to save conversations: {e}")
        raise

def create_conversation(title=None):
    _ensure_cache()
    cid = str(uuid.uuid4())
    orders = [c.get('order', 0) for c in _conversations_cache.values()]
    max_order = max(orders) if orders else 0
    new_order = max_order + 1

    _conversations_cache[cid] = {
        "id": cid,
        "title": title or "New Chat",
        "created": datetime.now().isoformat(),
        "messages": [],
        "order": new_order
    }
    save_conversations(_conversations_cache)
    print(f"🆕 Created conversation {cid} with order {new_order}")
    return cid

def get_conversation(cid):
    _ensure_cache()
    return _conversations_cache.get(cid)

def add_message(cid, role, text, images=None, files=None, ts=None):
    _ensure_cache()
    if cid not in _conversations_cache:
        print(f"❌ add_message: conversation {cid} not found")
        return False
    if images is None:
        images = []
    if files is None:
        files = []
    if ts is None:
        ts = datetime.now().strftime("%H:%M")
    file_meta = [{"name": f["name"], "mime": f.get("mime", "application/octet-stream")} for f in files]
    image_meta = [{"name": img.get("name", "image")} for img in images]
    _conversations_cache[cid]["messages"].append({
        "role": role,
        "text": text,
        "images": image_meta,
        "files": file_meta,
        "ts": ts
    })
    if role == "user" and len(_conversations_cache[cid]["messages"]) == 1:
        _conversations_cache[cid]["title"] = text[:40] + ("..." if len(text) > 40 else "")
    save_conversations(_conversations_cache)
    print(f"📝 Added {role} message to {cid} (now {len(_conversations_cache[cid]['messages'])} messages)")
    return True

def delete_conversation(cid):
    _ensure_cache()
    if cid in _conversations_cache:
        del _conversations_cache[cid]
        save_conversations(_conversations_cache)
        return True
    return False

# ── C/C++ comment stripper ──────────────────────────────────────
def strip_c_comments(text: str) -> str:
    text = re.sub(r'//.*', '', text)
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    text = '\n'.join(line for line in text.splitlines() if line.strip())
    return text

# ── Vision fallback (llava description) ──
def describe_image_with_llava(image_b64: str) -> str:
    vision_model = "llava:7b"
    vision_prompt = (
        "Describe this image in detail. "
        "Include objects, colors, layout, text, and any notable features."
    )
    payload = {
        "model": vision_model,
        "prompt": vision_prompt,
        "images": [image_b64],
        "stream": False,
        "options": {"temperature": 0.3}
    }
    try:
        resp = requests.post("http://127.0.0.1:11434/api/generate",
                             json=payload, timeout=60)
        resp.raise_for_status()
        return resp.json().get("response", "")
    except Exception as e:
        print(f"⚠️ llava fallback failed: {e}")
        return ""

# ── Automatic memory settings for Ollama ──────────────────────────
def get_ollama_memory_settings() -> dict:
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
            num_gpu = 99
            low_vram = True
        elif low_ram and not vram_available:
            num_gpu = 0
            low_vram = True
        else:
            num_gpu = 99 if vram_available else 0
            low_vram = False

        return {"num_gpu": num_gpu, "low_vram": low_vram}
    except Exception:
        return {"num_gpu": 99, "low_vram": False}

# ── Ollama command detection & execution ────────────────────────
def is_ollama_command(text: str) -> bool:
    return text.strip().lower().startswith("ollama ")

def execute_ollama_command_sync(text: str) -> str:
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
            return json.dumps(r.json(), indent=2)

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
                    chunk = json.loads(line)
                    if 'status' in chunk:
                        last_status = chunk['status']
                    if 'error' in chunk:
                        return f"❌ Error pulling '{model}': {chunk['error']}"
            return f"✅ Model '{model}' pulled successfully.\nLast status: {last_status}"

        elif cmd == 'push':
            return "❌ Push command requires authentication and is not supported in this interface."

        else:
            return f"❌ Unknown command: {cmd}"

    except Exception as e:
        return f"❌ Command failed: {str(e)}"

def handle_ollama_command_stream(conv_id: str, user_message: str,
                                 images: list, files: list):
    parts = user_message.strip().split()
    if len(parts) < 2:
        yield f"data: {json.dumps({'token': '❌ Usage: ollama <pull|list|ps|rm|push|stop|show> ...'})}\n\n"
        yield f"data: {json.dumps({'done': True, 'full_response': 'Invalid command.'})}\n\n"
        return

    cmd = parts[1].lower()
    args = parts[2:]
    full_response = ""

    try:
        if cmd == 'pull':
            if not args:
                full_response = "❌ Usage: ollama pull <model>"
                yield f"data: {json.dumps({'token': full_response})}\n\n"
            else:
                model = args[0]
                r = requests.post("http://127.0.0.1:11434/api/pull",
                                  json={"name": model}, stream=True, timeout=600)
                r.raise_for_status()
                for line in r.iter_lines():
                    if line:
                        chunk = json.loads(line)
                        status = chunk.get('status', '')
                        if status:
                            full_response += status + "\n"
                            yield f"data: {json.dumps({'token': status + '\n'})}\n\n"
                        if 'error' in chunk:
                            err = '❌ ' + chunk['error']
                            full_response += err
                            yield f"data: {json.dumps({'token': err})}\n\n"
                final = f"\n✅ Model '{model}' pulled successfully."
                full_response += final
                yield f"data: {json.dumps({'token': final})}\n\n"

        else:
            output = execute_ollama_command_sync(user_message)
            full_response = output
            for line in output.splitlines():
                yield f"data: {json.dumps({'token': line + '\n'})}\n\n"

        yield f"data: {json.dumps({'done': True, 'full_response': full_response})}\n\n"

    except Exception as e:
        err = f"❌ Command failed: {e}"
        yield f"data: {json.dumps({'error': err})}\n\n"

    ts = datetime.now().strftime("%H:%M")
    if conv_id:
        add_message(conv_id, "user", user_message, images, files, ts)
        add_message(conv_id, "bot", full_response, [], [], ts)

# ── Build HTML (with Notes tab centred in top bar) ──
def build_html(model_name):
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E🤖%3C/text%3E%3C/svg%3E">
<title>Qwen Chat · Multi‑Conversation</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
/* ===== all styles, with updated top-bar using CSS grid for perfect centering ===== */
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
/* ── NEW: Top bar with CSS grid for perfect centering ── */
.top-bar {
    display: grid;
    grid-template-columns: 1fr auto 1fr;  /* left, center, right */
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
    justify-self: start;   /* left aligned */
}
.top-bar .left h1 {
    font-size: 19px;
    background: linear-gradient(135deg, #58a6ff, #a371f7);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    font-weight: 700;
}
/* ── Centered tab buttons (pill style) ──────── */
.top-bar .center-tabs {
    display: flex;
    gap: 4px;
    background: rgba(255,255,255,0.06);
    padding: 4px;
    border-radius: 30px;
    backdrop-filter: blur(5px);
    border: 1px solid rgba(255,255,255,0.06);
    justify-self: center;   /* forces perfect centre */
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
    justify-self: end;   /* right aligned */
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
/* ── Chat panel & notes panel ───────────────── */
.chat-panel {
    flex:1; display:flex; flex-direction:column; min-height:0;
}
.notes-panel {
    flex:1; overflow-y:auto; padding: 24px 40px; display:none;
    flex-direction:column; gap:16px;
}
.notes-panel .note-editor {
    background: rgba(13,17,23,0.7);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 12px;
    padding: 16px;
    margin-bottom: 10px;
}
.notes-panel .note-editor input,
.notes-panel .note-editor textarea {
    width: 100%;
    background: transparent;
    border: none;
    color: #e6edf3;
    font-size: 14px;
    font-family: inherit;
    outline: none;
}
.notes-panel .note-editor input {
    font-weight: 600;
    font-size: 18px;
    margin-bottom: 8px;
}
.notes-panel .note-editor textarea {
    resize: vertical;
    min-height: 100px;
}
.notes-panel .note-editor .note-actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
    margin-top: 12px;
}
.notes-panel .note-editor .note-actions button {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.1);
    color: #8b949e;
    border-radius: 8px;
    padding: 6px 18px;
    cursor: pointer;
    font-size: 13px;
    transition: 0.2s;
}
.notes-panel .note-editor .note-actions .save-note {
    background: #1f6feb;
    color: white;
    border-color: #1f6feb;
}
.notes-panel .note-editor .note-actions .save-note:hover {
    background: #388bfd;
}
.notes-panel .note-editor .note-actions button:hover {
    background: rgba(255,255,255,0.1);
}
.notes-panel .note-item {
    background: rgba(28, 35, 51, 0.6);
    backdrop-filter: blur(8px);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 12px;
    padding: 16px 20px;
    transition: 0.2s;
}
.notes-panel .note-item:hover {
    background: rgba(28, 35, 51, 0.8);
}
.notes-panel .note-title {
    font-weight: 600;
    font-size: 16px;
    margin-bottom: 6px;
    color: #e6edf3;
}
.notes-panel .note-content {
    font-size: 14px;
    color: #8b949e;
    white-space: pre-wrap;
    word-wrap: break-word;
}
.notes-panel .note-actions {
    margin-top: 10px;
    display: flex;
    gap: 8px;
}
.notes-panel .note-actions button {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.1);
    color: #8b949e;
    border-radius: 8px;
    padding: 4px 12px;
    cursor: pointer;
    font-size: 12px;
    transition: 0.2s;
}
.notes-panel .note-actions button:hover {
    background: rgba(255,255,255,0.1);
    color: #58a6ff;
}
.notes-panel .note-actions .delete-note {
    color: #f85149;
}
.notes-panel .note-actions .delete-note:hover {
    background: rgba(248,81,73,0.15);
    border-color: #f85149;
}
body.light-mode .notes-panel .note-item {
    background: rgba(255,255,255,0.8);
    border-color: rgba(0,0,0,0.06);
}
body.light-mode .notes-panel .note-item:hover {
    background: rgba(255,255,255,0.95);
}
body.light-mode .notes-panel .note-title { color: #24292f; }
body.light-mode .notes-panel .note-content { color: #57606a; }
body.light-mode .notes-panel .note-editor {
    background: rgba(255,255,255,0.8);
    border-color: rgba(0,0,0,0.08);
}
body.light-mode .notes-panel .note-editor input,
body.light-mode .notes-panel .note-editor textarea {
    color: #24292f;
}
/* ── Chat area ────────────────────────────────── */
.chat-area {
    flex:1; overflow-y:auto; padding: 24px 40px;
    display:flex; flex-direction:column; gap: 16px;
    will-change: transform;
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
/* ── Message actions ─────────────────────────── */
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
/* ── Attachments ─────────────────────────────── */
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
/* ── Input bar ────────────────────────────────── */
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
/* ── Status bar ───────────────────────────────── */
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
/* ── Voice recording animation ───────────────── */
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
    <!-- TOP BAR WITH CENTER TABS (grid ensures perfect centering) -->
    <div class="top-bar">
      <div class="left">
        <button class="sidebar-toggle" onclick="toggleSidebar()" title="Toggle sidebar">
          <svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect>
            <line x1="9" y1="3" x2="9" y2="21"></line>
          </svg>
        </button>
        <h1>🧠 Trio-llama Custom Chat</h1>
      </div>

      <!-- CENTER TABS (pill style) – perfectly centred -->
      <div class="center-tabs">
        <button class="tab-btn active" data-tab="chat">💬 Chat</button>
        <button class="tab-btn" data-tab="notes">📝 Notes</button>
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
        <button class="clear-btn" onclick="clearAllChats()">🗑 Clear All</button>
        <div id="modelInfo" style="font-size:12px; color:#8b949e; max-width:200px; display:inline-block; vertical-align:middle; margin-left:10px;"></div>
        <span id="deepseekStatus" style="font-size:12px; margin-left:10px;"></span>
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

    <!-- NOTES PANEL -->
    <div class="notes-panel" id="notesPanel">
      <div class="note-editor" id="noteEditor">
        <input type="text" id="noteTitleInput" placeholder="Note title...">
        <textarea id="noteContentInput" placeholder="Write your note here..."></textarea>
        <div class="note-actions">
          <button class="save-note" id="saveNoteBtn">💾 Save Note</button>
          <button id="cancelNoteBtn" style="display:none;">Cancel</button>
        </div>
      </div>
      <div id="notesList"></div>
    </div>

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
/* ─── JavaScript ────────────────────────────── */
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

// ── DeepSeek status ──
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

// Unload model
unloadBtn.addEventListener('click', function() {
    if (!confirm('Unload current Ollama model from memory?')) return;
    fetch('/unload_model', { method: 'POST' })
        .then(() => {
            status.textContent = '✅ Model unloaded (memory freed)';
        })
        .catch(() => status.textContent = '❌ Unload failed');
});

// ── DROP OVERLAY LOGIC ─────────────────────────
var dropOverlay = document.getElementById('dropOverlay');
var dragCounter = 0;
function showDropOverlay() { dropOverlay.classList.add('active'); }
function hideDropOverlay() { dropOverlay.classList.remove('active'); }
document.addEventListener('dragenter', function(e) {
    e.preventDefault();
    dragCounter++;
    if (dragCounter === 1) showDropOverlay();
});
document.addEventListener('dragover', function(e) { e.preventDefault(); });
document.addEventListener('dragleave', function(e) {
    e.preventDefault();
    dragCounter--;
    if (dragCounter === 0) hideDropOverlay();
});
document.addEventListener('drop', function(e) {
    e.preventDefault();
    dragCounter = 0;
    hideDropOverlay();
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

// ── THEME ──────────────────────────────────────
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

// ── Sidebar toggle ─────────────────────────────
var sidebar = document.getElementById('sidebar');
var sidebarVisible = localStorage.getItem('sidebarVisible') !== 'false';
function toggleSidebar() {
    sidebarVisible = !sidebarVisible;
    localStorage.setItem('sidebarVisible', sidebarVisible);
    sidebar.classList.toggle('hidden', !sidebarVisible);
}
if (!sidebarVisible) sidebar.classList.add('hidden');

// ── Scroll button ───────────────────────────────
var scrollBtn = document.getElementById('scrollBottomBtn');
chatArea.addEventListener('scroll', function() {
    var threshold = 80;
    var atBottom = chatArea.scrollHeight - chatArea.scrollTop - chatArea.clientHeight < threshold;
    scrollBtn.style.display = atBottom ? 'none' : 'block';
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
function scrollToBottomIfNeeded() {
    var threshold = 80;
    var atBottom = chatArea.scrollHeight - chatArea.scrollTop - chatArea.clientHeight < threshold;
    if (atBottom) chatArea.scrollTop = chatArea.scrollHeight;
}

// ─── Voice Output (TTS) ─────────────────────────
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
function speakText(text) {
    if (!voiceEnabled || !text) return;
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = 'en-US';
    utterance.rate = 1.0;
    utterance.pitch = 1.0;
    utterance.onstart = () => {
        speaking = true;
        stopSpeakBtn.style.display = 'flex';
        status.textContent = '🔊 Speaking...';
    };
    utterance.onend = () => {
        speaking = false;
        stopSpeakBtn.style.display = 'none';
        status.textContent = voiceEnabled ? '🔊 Voice output ON (idle)' : '✅ Done';
    };
    utterance.onerror = (e) => {
        console.warn('TTS error:', e.error);
        speaking = false;
        stopSpeakBtn.style.display = 'none';
    };
    currentUtterance = utterance;
    window.speechSynthesis.speak(utterance);
}

// ── API Key helpers ─────────────────────────────
function saveApiKey(provider, key) {
    if (key && key.trim()) localStorage.setItem('api_key_' + provider, key.trim());
    else localStorage.removeItem('api_key_' + provider);
}
function loadApiKey(provider) {
    return localStorage.getItem('api_key_' + provider) || '';
}

// ── Vision badge ────────────────────────────────
function updateVisionBadge() {
    var provider = providerSelect.value;
    var model = modelSelect.value;
    if (!model) { visionBadge.classList.remove('visible'); return; }
    var apiKey = apiKeyInput.value;
    fetch('/check_vision?provider=' + encodeURIComponent(provider)
          + '&model=' + encodeURIComponent(model)
          + '&api_key=' + encodeURIComponent(apiKey))
        .then(r => r.json())
        .then(data => {
            if (data.vision) visionBadge.classList.add('visible');
            else visionBadge.classList.remove('visible');
        })
        .catch(() => visionBadge.classList.remove('visible'));
}

// ── Load models ─────────────────────────────────
function loadModels() {
    var provider = providerSelect.value;
    var apiKey = apiKeyInput.value;
    fetch('/providers/models?provider=' + encodeURIComponent(provider) + '&api_key=' + encodeURIComponent(apiKey))
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
                fetchModelInfo(modelSelect.value);
                checkDeepSeekStatus();
            } else {
                document.getElementById('modelInfo').textContent = '';
            }
        })
        .catch(err => { status.textContent = '⚠️ Could not load models: ' + err; });
}
function fetchModelInfo(model) {
    if (providerSelect.value !== 'deepseek' || !model) {
        document.getElementById('modelInfo').textContent = '';
        return;
    }
    fetch('/deepseek/model_info?model=' + encodeURIComponent(model))
        .then(r => r.json())
        .then(info => {
            const infoDiv = document.getElementById('modelInfo');
            if (info.error) {
                infoDiv.textContent = '⚠️ ' + info.error;
                return;
            }
            infoDiv.innerHTML = `
                <strong>${model}</strong><br>
                ${info.description}<br>
                Capabilities: ${info.capabilities.join(', ')}<br>
                Pricing: Input ${info.pricing.input}, Output ${info.pricing.output}
            `;
        })
        .catch(() => {
            document.getElementById('modelInfo').textContent = '⚠️ Could not load model info';
        });
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
            status.textContent = data.ok ? '✅ Model switched to ' + model : '❌ ' + (data.error || 'Failed');
        })
        .catch(err => { status.textContent = '❌ Error: ' + err; });
    }
    if (provider === 'deepseek') {
        fetchModelInfo(model);
    } else {
        document.getElementById('modelInfo').textContent = '';
    }
});

// ── Date grouping helpers ──────────────────────
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

// ── Search ──────────────────────────────────────
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

// ── Conversations ──────────────────────────────
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
            scrollToBottomIfNeeded();
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
    if (!confirm('Delete ALL conversations?')) return;
    fetch('/clear_all', { method: 'POST' })
        .then(() => {
            currentConv = null;
            loadConversations();
            chatArea.innerHTML = '<div class="msg bot">🗑 All chats cleared. Start a new one!</div>';
        });
}
function renderMsg(role, entry, msgIndex) {
    var div = document.createElement('div');
    div.className = 'msg ' + (role === 'user' ? 'user' : 'bot');
    if (entry.images && entry.images.length) {
        entry.images.forEach(im => {
            var img = document.createElement('img');
            img.src = 'data:image/png;base64,' + im.b64;
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
    scrollToBottomIfNeeded();
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
            scrollToBottomIfNeeded();
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
msgInput.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); doSend(); }
});
msgInput.addEventListener('input', function() {
    this.style.height = Math.min(this.scrollHeight, 140) + 'px';
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
    botDiv.querySelector('.body').classList.add('thinking-dots');
    busy = true;
    sendBtn.disabled = true;
    status.textContent = '⏳ Generating...';
    var searchEnabled = window.searchEnabled;
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
            function readStream() {
                reader.read().then(({done, value}) => {
                    if (done) {
                        finishStream(fullText, botDiv);
                        return;
                    }
                    var chunk = decoder.decode(value, {stream: true});
                    var lines = chunk.split('\n');
                    for (var line of lines) {
                        if (line.startsWith('data: ')) {
                            var jsonStr = line.substring(6);
                            try {
                                var data = JSON.parse(jsonStr);
                                if (data.token) {
                                    tokenCount++;
                                    fullText += data.token;
                                    botDiv.querySelector('.body').classList.remove('thinking-dots');
                                    botDiv.querySelector('.body').textContent = fullText;
                                    scrollToBottomIfNeeded();
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
function finishStream(fullText, botDiv) {
    botDiv.querySelector('.body').classList.remove('thinking-dots');
    botDiv.querySelector('.body').innerHTML = marked.parse(fullText || '(empty response)');
    botDiv.querySelector('.ts').textContent = new Date().toLocaleTimeString();
    status.textContent = '✅ Done';
    busy = false;
    sendBtn.disabled = false;
    scrollToBottomIfNeeded();
    speakText(fullText);
    loadConversations();
}

// ── Voice recording ────────────────────────────
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
                msgInput.style.height = Math.min(msgInput.scrollHeight, 140) + 'px';
                status.textContent = '🎤 Listening... (interim)';
            }
        }
        if (transcript) {
            msgInput.value = transcript;
            msgInput.style.height = Math.min(msgInput.scrollHeight, 140) + 'px';
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

// ── Resource monitor ────────────────────────────
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
    navigator.sendBeacon('/unload_model');
});
resourceIntervalId = setInterval(updateResources, 5000);

// ─── Notes ──────────────────────────────────────
var notesList = document.getElementById('notesList');
var noteTitleInput = document.getElementById('noteTitleInput');
var noteContentInput = document.getElementById('noteContentInput');
var saveNoteBtn = document.getElementById('saveNoteBtn');
var cancelNoteBtn = document.getElementById('cancelNoteBtn');
var editingNoteId = null;

function loadNotes() {
    fetch('/notes')
        .then(r => r.json())
        .then(notes => {
            renderNotes(notes);
        })
        .catch(err => console.error('Failed to load notes:', err));
}
function renderNotes(notes) {
    notesList.innerHTML = '';
    const ids = Object.keys(notes);
    if (ids.length === 0) {
        notesList.innerHTML = '<div class="note-item" style="text-align:center;color:#8b949e;">📝 No notes yet. Create one above!</div>';
        return;
    }
    ids.sort((a, b) => new Date(notes[b].created) - new Date(notes[a].created));
    ids.forEach(id => {
        const note = notes[id];
        const div = document.createElement('div');
        div.className = 'note-item';
        div.innerHTML = `
            <div class="note-title">${escapeHtml(note.title)}</div>
            <div class="note-content">${escapeHtml(note.content)}</div>
            <div class="note-actions">
                <button class="edit-note" data-id="${id}">✏️ Edit</button>
                <button class="delete-note" data-id="${id}">🗑️ Delete</button>
            </div>
        `;
        notesList.appendChild(div);
    });
    notesList.querySelectorAll('.edit-note').forEach(btn => {
        btn.addEventListener('click', function() {
            const id = this.dataset.id;
            const note = notes[id];
            if (note) {
                noteTitleInput.value = note.title;
                noteContentInput.value = note.content;
                editingNoteId = id;
                saveNoteBtn.textContent = '✏️ Update Note';
                cancelNoteBtn.style.display = 'inline-block';
                document.getElementById('noteEditor').scrollIntoView({ behavior: 'smooth' });
            }
        });
    });
    notesList.querySelectorAll('.delete-note').forEach(btn => {
        btn.addEventListener('click', function() {
            const id = this.dataset.id;
            if (confirm('Delete this note?')) {
                fetch('/notes/' + id, { method: 'DELETE' })
                    .then(r => r.json())
                    .then(data => {
                        if (data.ok) loadNotes();
                    });
            }
        });
    });
}
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}
saveNoteBtn.addEventListener('click', function() {
    const title = noteTitleInput.value.trim() || 'Untitled';
    const content = noteContentInput.value.trim();
    if (!content && !title) {
        alert('Please add some content or a title.');
        return;
    }
    const method = editingNoteId ? 'PUT' : 'POST';
    const url = editingNoteId ? '/notes/' + editingNoteId : '/notes';
    fetch(url, {
        method: method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, content })
    })
    .then(r => r.json())
    .then(data => {
        if (data.ok || data.id) {
            noteTitleInput.value = '';
            noteContentInput.value = '';
            editingNoteId = null;
            saveNoteBtn.textContent = '💾 Save Note';
            cancelNoteBtn.style.display = 'none';
            loadNotes();
        }
    });
});
cancelNoteBtn.addEventListener('click', function() {
    noteTitleInput.value = '';
    noteContentInput.value = '';
    editingNoteId = null;
    saveNoteBtn.textContent = '💾 Save Note';
    cancelNoteBtn.style.display = 'none';
});

// ─── Tab switching ──────────────────────────────
const tabBtns = document.querySelectorAll('.tab-btn');
const chatPanel = document.getElementById('chatPanel');
const notesPanel = document.getElementById('notesPanel');
tabBtns.forEach(btn => {
    btn.addEventListener('click', function() {
        tabBtns.forEach(b => b.classList.remove('active'));
        this.classList.add('active');
        const tab = this.dataset.tab;
        if (tab === 'chat') {
            chatPanel.style.display = 'flex';
            notesPanel.style.display = 'none';
            msgInput.focus();
        } else if (tab === 'notes') {
            chatPanel.style.display = 'none';
            notesPanel.style.display = 'flex';
            loadNotes();
        }
    });
});

// ─── Initialisation ─────────────────────────────
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
});
</script>
</body>
</html>"""

# ── Routes ──────────────────────────────────────────────────────

@app.route('/unload_model', methods=['POST'])
def unload_model():
    try:
        requests.post(
            "http://127.0.0.1:11434/api/generate",
            json={"model": current_model, "prompt": "", "keep_alive": 0},
            timeout=3
        )
        print(f"🧹 Unloaded '{current_model}' from Ollama RAM")
    except Exception as e:
        print(f"⚠️ Could not unload model: {e}")
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

# ── Notes routes (built‑in) ────────────────────────────────────
@app.route('/notes', methods=['GET'])
def notes_get_all():
    return jsonify(load_notes())

@app.route('/notes', methods=['POST'])
def notes_create():
    data = request.get_json()
    notes = load_notes()
    note_id = str(uuid.uuid4())
    notes[note_id] = {
        "id": note_id,
        "title": data.get("title", "Untitled"),
        "content": data.get("content", ""),
        "created": datetime.now().isoformat()
    }
    save_notes(notes)
    return jsonify({"id": note_id, "ok": True})

@app.route('/notes/<note_id>', methods=['PUT'])
def notes_update(note_id):
    data = request.get_json()
    notes = load_notes()
    if note_id not in notes:
        return jsonify({"error": "Note not found"}), 404
    if "title" in data:
        notes[note_id]["title"] = data["title"]
    if "content" in data:
        notes[note_id]["content"] = data["content"]
    save_notes(notes)
    return jsonify({"ok": True})

@app.route('/notes/<note_id>', methods=['DELETE'])
def notes_delete(note_id):
    notes = load_notes()
    if note_id not in notes:
        return jsonify({"error": "Note not found"}), 404
    del notes[note_id]
    save_notes(notes)
    return jsonify({"ok": True})

# ── End of notes routes ────────────────────────────────────────

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
            except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
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

@app.route('/check_vision', methods=['GET'])
def check_vision():
    provider_name = request.args.get('provider', 'ollama')
    model = request.args.get('model', '')

    if provider_name == 'ollama' and model:
        try:
            resp = requests.post(
                "http://127.0.0.1:11434/api/show",
                json={"name": model}, timeout=5
            )
            if resp.status_code == 200:
                data = resp.json()
                details = data.get("details", {})
                caps = details.get("capabilities", [])
                if "vision" in caps:
                    return jsonify({"vision": True})
                family = details.get("family", "").lower()
                vision_families = VISION_MODELS["ollama"]
                has_vision = any(kw in family for kw in vision_families)
                return jsonify({"vision": has_vision})
        except Exception:
            pass
    
    has_vision = model_supports_vision(provider_name, model)
    return jsonify({"vision": has_vision})

@app.route('/providers/models', methods=['GET'])
def get_provider_models():
    provider_name = request.args.get('provider', 'ollama')
    api_key = request.args.get('api_key', None)
    provider = providers.get(provider_name)
    if not provider:
        return jsonify({'error': 'Unknown provider'}), 400
    try:
        models = provider.list_models(api_key=api_key)
        return jsonify({'models': models})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

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
        else:
            print("⚠️ Cannot verify model existence – Ollama not responding.")
    except Exception as e:
        print(f"⚠️ Error verifying model: {e}")

    current_model = model
    save_model_config(model)
    providers["ollama"].model = model
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
        except Exception:
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
    save_conversations({})
    return jsonify({"ok": True})

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
    save_conversations(convs)
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
    save_conversations(convs)
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
    save_conversations(convs)
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
    save_conversations(convs)
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

        final_prompt = SYSTEM_PROMPT + "\n\n"
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

        provider = providers.get(provider_name)
        if not provider:
            return jsonify({'error': f'Unknown provider: {provider_name}'}), 400

        conv = get_conversation(conv_id)
        messages = []
        if conv:
            for msg in conv.get('messages', []):
                if msg['role'] == 'user':
                    messages.append({"role": "user", "content": msg['text']})
                elif msg['role'] == 'bot':
                    messages.append({"role": "assistant", "content": msg['text']})
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
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
            if model_supports_vision(provider_name, model):
                reply = provider.generate_with_image(messages, images, **extra_kwargs)
            else:
                description = describe_image_with_llava(images[0]["b64"])
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

        print(f"✅ Reply: {reply[:60]}")

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

        final_prompt = SYSTEM_PROMPT + "\n\n"
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
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
        messages.append({"role": "user", "content": final_prompt})

        mem_settings = get_ollama_memory_settings()

        payload = {
            "model": model or current_model,
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": 0.7,
                "num_predict": 2048,
                "num_ctx": 4096,
                "num_gpu": mem_settings['num_gpu'],
                "low_vram": mem_settings['low_vram'],
            }
        }
        if images:
            last_msg = messages[-1]
            content_parts = []
            for img in images:
                b64 = img["b64"]
                if "," in b64:
                    b64 = b64.split(",", 1)[1]
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
                })
            content_parts.append({"type": "text", "text": last_msg["content"]})
            payload["messages"][-1] = {"role": "user", "content": content_parts}

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
                        chunk = json.loads(line)
                        if "message" in chunk and "content" in chunk["message"]:
                            token = chunk["message"]["content"]
                            if token:
                                full_response += token
                                yield f"data: {json.dumps({'token': token})}\n\n"
                        if chunk.get("done", False):
                            usage = {}
                            if "eval_count" in chunk and "eval_duration" in chunk:
                                duration_sec = chunk.get("eval_duration", 0) / 1e9
                                token_count = chunk.get("eval_count", 0)
                                usage = {"tokens": token_count, "duration_sec": duration_sec}
                            yield f"data: {json.dumps({'done': True, 'full_response': full_response, 'usage': usage})}\n\n"
                            break
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

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

if __name__ == '__main__':
    print("\n" + "="*50)
    print("🚀  AI CHAT Interfacing Loading... · Multi‑Conversation")
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

    app.run(host='127.0.0.1', port=5001, debug=True, ssl_context=ssl_context)