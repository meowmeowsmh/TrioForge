from flask import Flask, request, jsonify
import requests
import base64
import os
import json
from datetime import datetime
import uuid
import psutil
import subprocess
import re

# ── NVIDIA GPU support (optional) ──
try:
    import pynvml
    pynvml.nvmlInit()
    NVML_AVAILABLE = True
except:
    NVML_AVAILABLE = False
    print("⚠️ NVML not available – GPU VRAM monitoring disabled.")

app = Flask(__name__)

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
DEFAULT_MODEL = "vaultbox/qwen3.5-uncensored:9b"
CONVERSATIONS_FILE = "json_configuration/conversations.json"
MODEL_CONFIG_FILE = "json_configuration/model_config.json"

try:
    from duckduckgo_search import DDGS
    SEARCH_AVAILABLE = True
except ImportError:
    SEARCH_AVAILABLE = False

# ── Ensure the json_configuration folder exists ──
os.makedirs(os.path.dirname(CONVERSATIONS_FILE), exist_ok=True)
os.makedirs(os.path.dirname(MODEL_CONFIG_FILE), exist_ok=True)

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
def load_conversations():
    if os.path.exists(CONVERSATIONS_FILE):
        try:
            with open(CONVERSATIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ Error loading conversations: {e}")
            return {}
    return {}

def save_conversations(convs):
    try:
        with open(CONVERSATIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(convs, f, ensure_ascii=False, indent=2)
        print(f"✅ Saved conversations ({len(convs)} items)")
    except Exception as e:
        print(f"❌ Failed to save conversations: {e}")
        raise

def create_conversation(title=None):
    convs = load_conversations()
    cid = str(uuid.uuid4())
    convs[cid] = {
        "id": cid,
        "title": title or "New Chat",
        "created": datetime.now().isoformat(),
        "messages": []
    }
    save_conversations(convs)
    print(f"🆕 Created conversation {cid}")
    return cid

def get_conversation(cid):
    convs = load_conversations()
    return convs.get(cid)

def add_message(cid, role, text, images=None, files=None, ts=None):
    convs = load_conversations()
    if cid not in convs:
        print(f"❌ add_message: conversation {cid} not found")
        return False
    if images is None:
        images = []
    if files is None:
        files = []
    if ts is None:
        ts = datetime.now().strftime("%H:%M")
    file_meta = [{"name": f["name"], "mime": f.get("mime", "application/octet-stream")} for f in files]
    convs[cid]["messages"].append({
        "role": role,
        "text": text,
        "images": images,
        "files": file_meta,
        "ts": ts
    })
    if role == "user" and len(convs[cid]["messages"]) == 1:
        convs[cid]["title"] = text[:40] + ("..." if len(text) > 40 else "")
    save_conversations(convs)
    print(f"📝 Added {role} message to {cid} (now {len(convs[cid]['messages'])} messages)")
    return True

def delete_conversation(cid):
    convs = load_conversations()
    if cid in convs:
        del convs[cid]
        save_conversations(convs)
        return True
    return False

# ── Build HTML (with resource display) ──
def build_html(model_name):
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Qwen Chat · Multi‑Conversation</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
html, body { height:100%; font-family:'Segoe UI',sans-serif; background:#0a0c10; color:#e6edf3; }
.app { display:flex; height:100%; }

/* ─── SIDEBAR ─── */
.sidebar {
    width:260px; background:#161b22; border-right:1px solid #30363d;
    display:flex; flex-direction:column; flex-shrink:0;
}
.sidebar-header {
    padding:16px 16px 8px; border-bottom:1px solid #30363d;
    display:flex; align-items:center; gap:10px; flex-wrap:wrap;
}
.sidebar-header h2 { font-size:16px; font-weight:600; color:#58a6ff; }
.new-chat-btn {
    background:#1f6feb; color:white; border:none; border-radius:8px;
    padding:6px 14px; font-size:13px; font-weight:600; cursor:pointer;
    margin-left:auto; white-space:nowrap;
}
.new-chat-btn:hover { background:#388bfd; }
.conv-list {
    flex:1; overflow-y:auto; padding:8px 0;
}
.conv-list::-webkit-scrollbar { width:4px; }
.conv-list::-webkit-scrollbar-thumb { background:#30363d; border-radius:2px; }
.conv-item {
    display:flex; align-items:center; padding:8px 16px; cursor:pointer;
    border-left:3px solid transparent; transition:0.15s;
    gap:8px;
}
.conv-item:hover { background:#1c2333; }
.conv-item.active { background:#1c2333; border-left-color:#58a6ff; }
.conv-item .title {
    flex:1; font-size:14px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
    color:#e6edf3;
}
.conv-item .del {
    background:transparent; border:none; color:#f85149; font-size:16px;
    cursor:pointer; opacity:0.4; padding:0 4px;
}
.conv-item .del:hover { opacity:1; }
.conv-item .time {
    font-size:11px; color:#8b949e; margin-right:6px; white-space:nowrap;
}
.sidebar-footer {
    padding:12px 16px; border-top:1px solid #30363d; font-size:12px; color:#8b949e;
    text-align:center;
}

/* ─── MAIN CHAT ─── */
.main {
    flex:1; display:flex; flex-direction:column; min-width:0;
}
.top-bar {
    background:#161b22; border-bottom:1px solid #30363d;
    padding:12px 24px; display:flex; align-items:center; justify-content:space-between;
    flex-shrink:0;
    gap:12px;
    flex-wrap:wrap;
}
.top-bar .left {
    display:flex; align-items:center; gap:12px;
}
.top-bar h1 {
    font-size:18px; background:linear-gradient(135deg,#58a6ff,#3fb950);
    -webkit-background-clip:text; -webkit-text-fill-color:transparent; font-weight:700;
}
.top-bar .right { display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
.model-select {
    background:#0d1117; border:1px solid #30363d; border-radius:8px;
    color:#e6edf3; padding:4px 8px; font-size:13px; outline:none;
    max-width:200px;
}
.model-select:focus { border-color:#58a6ff; }
.clear-btn {
    background:#21262d; border:1px solid #30363d; color:#f85149;
    border-radius:8px; padding:4px 12px; font-size:12px; cursor:pointer;
}
.clear-btn:hover { background:#2d1117; }

/* ─── SEARCH TOGGLE LABEL ─── */
.search-label {
    color:#8b949e;
    font-size:13px;
    display:flex;
    align-items:center;
    gap:4px;
    cursor:pointer;
    user-select:none;
}
.search-label input[type="checkbox"] {
    width:16px;
    height:16px;
    accent-color:#1f6feb;
    cursor:pointer;
}

.chat-area {
    flex:1; overflow-y:auto; padding:24px 40px;
    display:flex; flex-direction:column; gap:16px;
}
.chat-area::-webkit-scrollbar { width:6px; }
.chat-area::-webkit-scrollbar-thumb { background:#30363d; border-radius:3px; }

.msg {
    padding:12px 18px; border-radius:14px; max-width:80%;
    line-height:1.6; font-size:15px; white-space:pre-wrap; word-wrap:break-word;
}
.msg.user { align-self:flex-end; background:#1f6feb; color:white; }
.msg.bot  { align-self:flex-start; background:#1c2333; border:1px solid #30363d; }
.msg .ts  { font-size:10px; opacity:0.5; margin-top:6px; }
.msg img  { max-width:240px; max-height:240px; border-radius:8px; display:block; margin-bottom:8px; border:1px solid #30363d; }
.msg .file-chip {
    display:inline-flex; align-items:center; gap:6px;
    background:#0d1117; border:1px solid #30363d; border-radius:8px;
    padding:4px 10px; font-size:13px; margin-bottom:6px;
}

.attachments {
    display:flex; flex-wrap:wrap; gap:8px;
    padding:0 40px 8px; background:#161b22;
}
.att-thumb {
    position:relative; display:inline-flex; align-items:center;
    background:#0d1117; border:1px solid #30363d; border-radius:8px;
    padding:4px 8px; gap:6px; font-size:12px; color:#8b949e;
}
.att-thumb img { height:44px; border-radius:6px; }
.att-thumb .remove {
    background:#f85149; color:white; border:none; border-radius:50%;
    width:16px; height:16px; font-size:11px; cursor:pointer;
    line-height:16px; text-align:center; flex-shrink:0;
}

.input-bar {
    background:#161b22; border-top:1px solid #30363d;
    padding:12px 40px 16px; display:flex; gap:10px;
    align-items:flex-end; flex-shrink:0;
}
.attach-btn {
    background:#21262d; border:1px solid #30363d; color:#8b949e;
    border-radius:10px; width:44px; height:44px; font-size:20px;
    cursor:pointer; display:flex; align-items:center; justify-content:center;
    flex-shrink:0; transition:color 0.2s;
}
.attach-btn:hover { color:#58a6ff; border-color:#58a6ff; }
#msgInput {
    flex:1; background:#0d1117; border:1px solid #30363d;
    border-radius:10px; color:#e6edf3; font-size:15px;
    padding:10px 14px; resize:none; font-family:inherit;
    min-height:44px; max-height:120px; outline:none;
}
#msgInput:focus { border-color:#58a6ff; }
#sendBtn {
    background:#1f6feb; color:white; border:none; border-radius:10px;
    padding:10px 24px; font-size:15px; font-weight:600;
    cursor:pointer; height:44px; white-space:nowrap; flex-shrink:0;
}
#sendBtn:hover { background:#388bfd; }
#sendBtn:disabled { opacity:0.5; cursor:not-allowed; }

/* ─── Status bar with resource monitor ─── */
#statusBar {
    display:flex;
    justify-content:space-between;
    align-items:center;
    padding:6px 40px;
    background:#161b22;
    border-top:1px solid #30363d;
    font-size:12px;
    color:#8b949e;
    flex-shrink:0;
}
#resourceDisplay {
    font-family:monospace;
    font-size:11px;
}

/* ─── Voice Record Button ─── */
.record-btn {
    background:#21262d;
    border:1px solid #30363d;
    color:#8b949e;
    border-radius:10px;
    width:44px;
    height:44px;
    font-size:20px;
    cursor:pointer;
    display:flex;
    align-items:center;
    justify-content:center;
    flex-shrink:0;
    transition: color 0.2s, background 0.2s, transform 0.2s;
}
.record-btn:hover {
    color:#58a6ff;
    border-color:#58a6ff;
}
.record-btn.recording {
    background:#f85149;
    color:white;
    border-color:#f85149;
    animation: pulse 1s infinite;
}
@keyframes pulse {
    0% { transform: scale(1); }
    50% { transform: scale(1.1); }
    100% { transform: scale(1); }
}
.record-btn:disabled {
    opacity:0.5;
    cursor:not-allowed;
}
</style>
</head>
<body>
<div class="app">

  <!-- SIDEBAR -->
  <div class="sidebar">
    <div class="sidebar-header">
      <h2>💬 Chats</h2>
      <button class="new-chat-btn" onclick="newChat()">+ New</button>
    </div>
    <div class="conv-list" id="convList"></div>
    <div class="sidebar-footer">Conversations are saved</div>
  </div>

  <!-- MAIN -->
  <div class="main">
    <div class="top-bar">
      <div class="left">
        <h1>🧠 Ollama Custom Chat</h1>
      </div>
      <div class="right">
        <!-- SEARCH TOGGLE -->
        <label class="search-label">
          <input type="checkbox" id="searchToggle" checked> 🌐 Search
        </label>
        <select id="modelSelect" class="model-select" title="Select model"></select>
        <button class="clear-btn" onclick="clearAllChats()">🗑 Clear All</button>
      </div>
    </div>

    <div class="chat-area" id="chatArea">
      <div class="msg bot">👋 Hello! Select or create a chat from the sidebar.</div>
    </div>

    <div class="attachments" id="attachments"></div>

    <div class="input-bar">
      <button class="attach-btn" title="Attach image or file" onclick="document.getElementById('fileInput').click()">📎</button>
      <input type="file" id="fileInput" accept="image/*,.pdf,.txt,.md,.py,.js,.csv,.json" multiple style="display:none"/>
      <textarea id="msgInput" placeholder="Type your message... (Enter to send, Shift+Enter for new line)"></textarea>
      <!-- Voice Record Button -->
      <button id="recordBtn" class="record-btn" title="Click to record voice input">🎤</button>
      <button id="sendBtn">Send</button>
    </div>

    <!-- Status bar with resource monitor -->
    <div id="statusBar">
      <span id="status">✅ Ready</span>
      <span id="resourceDisplay">💾 RAM: -- | 🎮 VRAM: --</span>
    </div>
  </div>
</div>

<script>
var chatArea    = document.getElementById('chatArea');
var msgInput    = document.getElementById('msgInput');
var sendBtn     = document.getElementById('sendBtn');
var status      = document.getElementById('status');
var fileInput   = document.getElementById('fileInput');
var attachments = document.getElementById('attachments');
var convList    = document.getElementById('convList');
var modelSelect = document.getElementById('modelSelect');
var busy        = false;
var currentConv = null;
var pending     = [];

// ── Load models ──
function loadModels() {
    fetch('/models')
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                status.textContent = '⚠️ ' + data.error;
                return;
            }
            var current = modelSelect.value;
            modelSelect.innerHTML = '';
            data.forEach(m => {
                var opt = document.createElement('option');
                opt.value = m;
                opt.textContent = m;
                modelSelect.appendChild(opt);
            });
            if (current && data.includes(current)) {
                modelSelect.value = current;
            } else {
                fetch('/current_model')
                    .then(r => r.json())
                    .then(res => {
                        if (res.model && data.includes(res.model)) {
                            modelSelect.value = res.model;
                        } else if (data.length > 0) {
                            modelSelect.value = data[0];
                        }
                    });
            }
        })
        .catch(err => {
            status.textContent = '⚠️ Could not load models: ' + err;
        });
}

// ── Model change ──
modelSelect.addEventListener('change', function() {
    var model = this.value;
    if (!model) return;
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
            status.textContent = '❌ ' + (data.error || 'Failed to set model');
        }
    })
    .catch(err => {
        status.textContent = '❌ Error setting model: ' + err;
    });
});

// ── Load conversations ──
function loadConversations() {
    fetch('/conversations')
        .then(r => r.json())
        .then(data => {
            renderConvList(data);
            if (data.length > 0) {
                selectConversation(data[0].id);
            } else {
                newChat();
            }
        });
}

function renderConvList(convs) {
    convList.innerHTML = '';
    convs.forEach(conv => {
        const div = document.createElement('div');
        div.className = 'conv-item' + (conv.id === currentConv ? ' active' : '');
        div.dataset.id = conv.id;

        const titleSpan = document.createElement('span');
        titleSpan.className = 'title';
        titleSpan.textContent = conv.title || 'Untitled';
        div.appendChild(titleSpan);

        const timeSpan = document.createElement('span');
        timeSpan.className = 'time';
        const d = new Date(conv.created);
        timeSpan.textContent = d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
        div.appendChild(timeSpan);

        const delBtn = document.createElement('button');
        delBtn.className = 'del';
        delBtn.textContent = '×';
        delBtn.title = 'Delete this chat';
        delBtn.onclick = (e) => { e.stopPropagation(); deleteChat(conv.id); };
        div.appendChild(delBtn);

        div.addEventListener('click', () => selectConversation(conv.id));
        convList.appendChild(div);
    });
}

function selectConversation(id) {
    if (id === currentConv) return;
    currentConv = id;
    document.querySelectorAll('.conv-item').forEach(el => {
        el.classList.toggle('active', el.dataset.id === id);
    });
    fetch('/conversations/' + id + '/messages')
        .then(r => r.json())
        .then(messages => {
            chatArea.innerHTML = '';
            if (messages.length === 0) {
                chatArea.innerHTML = '<div class="msg bot">💬 No messages yet. Say something!</div>';
            } else {
                messages.forEach(msg => renderMsg(msg.role, msg));
            }
            chatArea.scrollTop = chatArea.scrollHeight;
        });
}

function newChat() {
    fetch('/conversations', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
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

function renderMsg(role, entry) {
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
    body.textContent = entry.text || '';
    div.appendChild(body);

    var ts = document.createElement('div');
    ts.className = 'ts';
    ts.textContent = entry.ts || '';
    div.appendChild(ts);

    chatArea.appendChild(div);
    chatArea.scrollTop = chatArea.scrollHeight;
    return div;
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
    msgInput.style.height = 'auto';
    msgInput.style.height = Math.min(msgInput.scrollHeight, 120) + 'px';
});
sendBtn.addEventListener('click', doSend);

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
    renderMsg('user', userEntry);

    msgInput.value = '';
    msgInput.style.height = 'auto';
    pending = [];
    attachments.innerHTML = '';

    var botDiv = renderMsg('bot', { role:'bot', text:'⏳ Thinking...', ts:'' });
    busy = true;
    sendBtn.disabled = true;
    status.textContent = '⏳ Waiting for Ollama...';

    var xhr = new XMLHttpRequest();
    xhr.open('POST', '/chat', true);
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.timeout = 180000;

    xhr.onload = function() {
        busy = false;
        sendBtn.disabled = false;
        if (xhr.status === 200) {
            try {
                var data = JSON.parse(xhr.responseText);
                if (data.error) {
                    botDiv.querySelector('.body').textContent = '❌ ' + data.error;
                    status.textContent = '❌ ' + data.error;
                } else {
                    var botEntry = { role:'bot', text: data.response || '(no response)', ts: new Date().toLocaleTimeString() };
                    botDiv.querySelector('.body').textContent = botEntry.text;
                    botDiv.querySelector('.ts').textContent  = botEntry.ts;
                    status.textContent = '✅ Done';
                    loadConversations();
                }
            } catch(e) {
                botDiv.querySelector('.body').textContent = '❌ Parse error';
                status.textContent = '❌ Parse error';
            }
        } else {
            botDiv.querySelector('.body').textContent = '❌ Server error: ' + xhr.status;
            status.textContent = '❌ HTTP ' + xhr.status;
        }
        chatArea.scrollTop = chatArea.scrollHeight;
    };
    xhr.onerror   = function() { busy=false; sendBtn.disabled=false; botDiv.querySelector('.body').textContent='❌ Network error'; status.textContent='❌ Network error'; };
    xhr.ontimeout = function() { busy=false; sendBtn.disabled=false; botDiv.querySelector('.body').textContent='❌ Timed out'; status.textContent='❌ Timeout'; };

    // ── READ SEARCH TOGGLE STATE ──
    var searchEnabled = document.getElementById('searchToggle').checked;

    xhr.send(JSON.stringify({
        conversation_id: currentConv,
        message: text,
        images: images.map(i => ({ b64: i.b64, name: i.name })),
        files: files.map(f => ({ b64: f.b64, name: f.name, mime: f.mime })),
        search: searchEnabled
    }));
}

// ─── VOICE RECORDING ──────────────────────────────────────────────
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
            if (event.results[i].isFinal) {
                transcript += event.results[i][0].transcript;
            } else {
                var interim = event.results[i][0].transcript;
                msgInput.value = interim;
                msgInput.style.height = 'auto';
                msgInput.style.height = Math.min(msgInput.scrollHeight, 120) + 'px';
                status.textContent = '🎤 Listening... (interim)';
            }
        }
        if (transcript) {
            msgInput.value = transcript;
            msgInput.style.height = 'auto';
            msgInput.style.height = Math.min(msgInput.scrollHeight, 120) + 'px';
            status.textContent = '✅ Voice recognized! Press Send or speak again.';
        }
    };

    recognition.onend = function() {
        isRecording = false;
        recordBtn.classList.remove('recording');
        recordBtn.textContent = '🎤';
        if (msgInput.value.trim() === '') {
            status.textContent = '⏹️ Recording stopped (no input)';
        } else {
            status.textContent = '✅ Voice input ready. Press Send to chat.';
        }
    };

    recognition.onerror = function(event) {
        console.error('Speech recognition error:', event.error);
        isRecording = false;
        recordBtn.classList.remove('recording');
        recordBtn.textContent = '🎤';
        var errorMessages = {
            'not-allowed': '❌ Microphone access denied. Please allow microphone permissions.',
            'no-speech': '⏹️ No speech detected. Try again.',
            'audio-capture': '❌ No microphone found. Please connect a microphone.',
            'network': '❌ Network error. Check your connection.'
        };
        status.textContent = errorMessages[event.error] || '❌ Speech error: ' + event.error;
    };

    recordBtn.addEventListener('click', function() {
        if (isRecording) {
            recognition.stop();
            return;
        }
        try {
            recognition.start();
            isRecording = true;
            recordBtn.classList.add('recording');
            recordBtn.textContent = '⏹';
            msgInput.value = '';
            status.textContent = '🎤 Listening... Speak now.';
        } catch (e) {
            console.error('Failed to start recording:', e);
            status.textContent = '❌ Failed to start recording: ' + e.message;
        }
    });

    console.log('🎤 Voice recording is ready!');
} else {
    recordBtn.style.display = 'none';
    console.warn('⚠️ Speech recognition not supported in this browser.');
    status.textContent = '⚠️ Voice recording not supported. Use Chrome or Edge.';
}
// ─── END VOICE RECORDING ──────────────────────────────────────────

// ─── RESOURCE MONITOR (RAM / VRAM) ──────────────────────────────
function updateResources() {
    fetch('/resources')
        .then(r => r.json())
        .then(data => {
            const display = document.getElementById('resourceDisplay');
            if (data.error) {
                display.textContent = '⚠️ ' + data.error;
                return;
            }
            let ramText = data.ram_used !== null ? `${data.ram_used.toFixed(1)}GB` : '--';
            let vramText = data.vram_used !== null ? `${data.vram_used.toFixed(1)}GB` : '--';
            display.textContent = `💾 RAM: ${ramText} | 🎮 VRAM: ${vramText}`;
        })
        .catch(err => {
            console.log('Resource update failed:', err);
        });
}

// Update every 5 seconds
setInterval(updateResources, 5000);

window.addEventListener('load', function() {
    loadModels();
    loadConversations();
    msgInput.focus();
    setTimeout(updateResources, 500);
});
</script>
</body>
</html>"""

# ── Routes ──────────────────────────────────────────────────────

@app.route('/')
def index():
    return build_html(current_model)

@app.route('/resources', methods=['GET'])
def get_resources():
    """Return current RAM and VRAM usage (display only, with fallback)."""
    try:
        ram = psutil.virtual_memory()
        ram_used_gb = (ram.total - ram.available) / (1024**3)

        # Try NVML first
        vram_used_gb = None
        if NVML_AVAILABLE:
            try:
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                vram_used_gb = info.used / (1024**3)
            except Exception as e:
                print(f"⚠️ NVML query failed: {e}")

        # Fallback: parse nvidia-smi if NVML failed or not available
        if vram_used_gb is None:
            try:
                result = subprocess.run(
                    ['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0:
                    vram_mb = float(result.stdout.strip().split('\n')[0])
                    vram_used_gb = vram_mb / 1024
                    print(f"✅ VRAM from nvidia-smi: {vram_used_gb:.2f}GB")
            except Exception as e:
                print(f"⚠️ nvidia-smi fallback failed: {e}")

        return jsonify({
            'ram_used': ram_used_gb,
            'vram_used': vram_used_gb,
            'ram_total': ram.total / (1024**3)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Model endpoints ──
@app.route('/models', methods=['GET'])
def get_models():
    try:
        resp = requests.get("http://127.0.0.1:11434/api/tags", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        models = [m['name'] for m in data.get('models', [])]
        return jsonify(models)
    except requests.exceptions.ConnectionError:
        return jsonify({'error': 'Cannot connect to Ollama'}), 503
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/current_model', methods=['GET'])
def get_current_model():
    return jsonify({'model': current_model})

@app.route('/set_model', methods=['POST'])
def set_model():
    global current_model
    data = request.get_json()
    model = data.get('model')
    if not model:
        return jsonify({'error': 'No model provided'}), 400
    try:
        resp = requests.get("http://127.0.0.1:11434/api/tags", timeout=5)
        resp.raise_for_status()
        models = [m['name'] for m in resp.json().get('models', [])]
        if model not in models:
            return jsonify({'error': f'Model "{model}" not found in Ollama'}), 400
    except:
        pass
    current_model = model
    save_model_config(model)
    return jsonify({'ok': True, 'model': model})

# ── Conversations API ──
@app.route('/conversations', methods=['GET'])
def list_conversations():
    convs = load_conversations()
    sorted_list = sorted(convs.values(), key=lambda c: c.get('created', ''), reverse=True)
    result = [{"id": c["id"], "title": c.get("title", "Untitled"), "created": c.get("created", "")} for c in sorted_list]
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

# ── Chat endpoint ──
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

        print(f"📩 Received chat request: conv_id={conv_id}, search={search_enabled}, message='{user_message[:30]}...'")

        if not user_message and not images and not files:
            return jsonify({'error': 'Nothing to send'}), 400

        if not conv_id:
            print("🆕 No conv_id, creating new conversation")
            conv_id = create_conversation()
        else:
            conv = get_conversation(conv_id)
            if conv is None:
                print(f"❌ Conversation {conv_id} not found")
                return jsonify({'error': 'Conversation not found'}), 404
            else:
                print(f"✅ Found conversation {conv_id} with {len(conv.get('messages', []))} messages")

        # ── SEARCH ENGINE (only if enabled) ──
        search_context = ""
        if SEARCH_AVAILABLE and search_enabled and user_message.strip():
            try:
                print(f"🌐 Searching DuckDuckGo for: {user_message[:50]}...")
                with DDGS() as ddgs:
                    results = ddgs.text(user_message, max_results=3)
                    snippets = [r['body'] for r in results if 'body' in r]
                    if snippets:
                        search_context = " ".join(snippets[:3])
                        print(f"✅ Found {len(snippets)} search results.")
                    else:
                        print("⚠️ No search results found.")
            except Exception as e:
                print(f"❌ Search error: {e}")

        # ── Build the prompt ──
        if search_context:
            final_prompt = f"Web search results for '{user_message}':\n{search_context}\n\nBased on these results, answer the user's question: {user_message}"
        else:
            final_prompt = user_message

        # ── Attach files ──
        for f in files:
            try:
                raw = base64.b64decode(f['b64']).decode('utf-8', errors='replace')
                final_prompt += f"\n\n--- File: {f['name']} ---\n{raw[:4000]}"
            except:
                final_prompt += f"\n\n[Attached file: {f['name']} — binary, cannot read as text]"

        # ── Ollama payload ──
        payload = {
            "model": current_model,
            "prompt": final_prompt,
            "stream": False,
            "keep_alive": 0,
            "options": {"temperature": 0.7, "num_predict": 2048, "num_ctx": 16384}
        }
        if images:
            payload["images"] = [i['b64'] for i in images]

        print(f"📨 Sending to Ollama (model {current_model}): {final_prompt[:60]}")
        resp = requests.post(OLLAMA_URL, json=payload, timeout=180)
        resp.raise_for_status()
        reply = resp.json().get('response', 'No response.')
        print(f"✅ Got reply: {reply[:60]}")

        ts = datetime.now().strftime("%H:%M")

        if not add_message(conv_id, "user", user_message, images, files, ts):
            return jsonify({'error': f'Failed to save user message to conversation {conv_id}'}), 500

        if not add_message(conv_id, "bot", reply, [], [], ts):
            return jsonify({'error': f'Failed to save bot message to conversation {conv_id}'}), 500

        print(f"💾 Saved both messages to {conv_id}")
        return jsonify({'response': reply})

    except requests.exceptions.ConnectionError:
        print("❌ Cannot connect to Ollama")
        return jsonify({'error': 'Cannot connect to Ollama. Make sure it is running.'}), 503
    except requests.exceptions.Timeout:
        return jsonify({'error': 'Ollama timed out. Try a shorter message.'}), 504
    except Exception as e:
        print(f"❌ Error: {e}")
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    print("\n" + "="*50)
    print("🚀  AI CHAT Interfacing Loading... · Multi‑Conversation")
    print("="*50)
    print(f"  Default model : {DEFAULT_MODEL}")
    print(f"  Current model : {current_model}")
    print(f"  URL           : https://localhost:5000")
    print(f"  Storage       : {CONVERSATIONS_FILE}")
    print("="*50 + "\n")
    app.run(host='127.0.0.1', port=5000, debug=True, ssl_context=('cert_store/localhost+1.pem', 'cert_store/localhost+1-key.pem'))