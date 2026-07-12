# notes.py – advanced with Markdown, tags, pinning, colours, AI assistance, semantic search
# STORAGE: SQLite (sqlite_data/notes.db) + JSON backup (json_configuration/notes.json)
# JSON is written on every change as a human‑readable backup.

import os
import json as std_json
import sqlite3
import threading
import uuid
import random
from datetime import datetime
from flask import Blueprint, request, jsonify, render_template_string
from concurrent.futures import ThreadPoolExecutor

# ---------- Try orjson for faster JSON ----------
try:
    import orjson
    def json_dumps(obj):
        return orjson.dumps(obj).decode('utf-8')
    def json_loads(s):
        return orjson.loads(s)
    print("🚀 notes: using orjson")
except ImportError:
    json_dumps = std_json.dumps
    json_loads = std_json.loads
    print("ℹ️ notes: using standard json (install orjson for faster I/O)")

# ---------- Embedding (semantic search) ----------
try:
    from sentence_transformers import SentenceTransformer
    import numpy as np
    from sklearn.metrics.pairwise import cosine_similarity
    EMBED_AVAILABLE = True
except ImportError:
    EMBED_AVAILABLE = False
    print("⚠️ sentence-transformers or scikit-learn not installed. Run: pip install sentence-transformers scikit-learn")

# ---------- LLM providers ----------
from llm_providers import (
    OllamaProvider,
    DeepSeekProvider,
    ClaudeProvider,
    HuggingFaceProvider,
    GroqProvider,
    LlamaCppProvider,
)

# ======================================================================
# SQLite storage layer
# ======================================================================
DB_DIR = "sqlite_data"
DB_PATH = os.path.join(DB_DIR, "notes.db")
os.makedirs(DB_DIR, exist_ok=True)

# JSON backup path
NOTES_JSON = "json_configuration/notes.json"
os.makedirs(os.path.dirname(NOTES_JSON), exist_ok=True)

_local = threading.local()
_write_lock = threading.Lock()

def get_conn():
    """One connection per thread (SQLite connections aren't thread-safe to share)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")       # readers don't block writers
        conn.execute("PRAGMA synchronous=NORMAL")     # fast + safe enough with WAL
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn

def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS notes (
            id            TEXT PRIMARY KEY,
            title         TEXT NOT NULL DEFAULT '',
            content       TEXT NOT NULL DEFAULT '',
            created       TEXT NOT NULL,
            last_modified TEXT NOT NULL,
            order_idx     INTEGER NOT NULL DEFAULT 0,
            pinned        INTEGER NOT NULL DEFAULT 0,
            color         TEXT NOT NULL DEFAULT 'default',
            tags          TEXT NOT NULL DEFAULT '[]',
            embedding     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_notes_modified ON notes(last_modified);
    """)
    conn.commit()
    print("✅ SQLite notes table ready.")

init_db()

# ---------- Row <-> dict helpers ----------
def _note_row_to_dict(row):
    d = dict(row)
    d["tags"] = json_loads(d["tags"]) if d.get("tags") else []
    d["embedding"] = json_loads(d["embedding"]) if d.get("embedding") else None
    d["pinned"] = bool(d["pinned"])
    d["order"] = d.pop("order_idx", 0)
    return d

def _note_to_row_values(note_id, data, created=None, existing=None):
    now = datetime.now().isoformat()
    if existing:
        created = existing.get("created", now)
    else:
        created = created or data.get("created", now)
    tags = data.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    embedding = data.get("embedding")
    return {
        "id": note_id,
        "title": data.get("title", "Untitled"),
        "content": data.get("content", ""),
        "created": created,
        "last_modified": now,
        "order_idx": data.get("order", 0),
        "pinned": 1 if data.get("pinned", False) else 0,
        "color": data.get("color", "default"),
        "tags": json_dumps(tags),
        "embedding": json_dumps(embedding) if embedding is not None else None
    }

# ---------- Core DB operations ----------
def upsert_note(note_id, data, created=None):
    """Insert or replace a note. `created` can be passed for migrations."""
    vals = _note_to_row_values(note_id, data, created)
    with _write_lock:
        conn = get_conn()
        conn.execute("""
            INSERT INTO notes (id, title, content, created, last_modified, order_idx, pinned, color, tags, embedding)
            VALUES (:id, :title, :content, :created, :last_modified, :order_idx, :pinned, :color, :tags, :embedding)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                content=excluded.content,
                last_modified=excluded.last_modified,
                order_idx=excluded.order_idx,
                pinned=excluded.pinned,
                color=excluded.color,
                tags=excluded.tags,
                embedding=excluded.embedding
        """, vals)
        conn.commit()
    write_json_backup()

def get_note_from_db(note_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    return _note_row_to_dict(row) if row else None

def get_all_notes():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM notes ORDER BY order_idx, created").fetchall()
    notes = {}
    for row in rows:
        n = _note_row_to_dict(row)
        notes[n["id"]] = n
    return notes

def delete_note_from_db(note_id):
    with _write_lock:
        conn = get_conn()
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        conn.commit()
    write_json_backup()

def clear_all_notes_db():
    with _write_lock:
        conn = get_conn()
        conn.execute("DELETE FROM notes")
        conn.commit()
    write_json_backup()

def set_note_embedding_db(note_id, embedding):
    with _write_lock:
        conn = get_conn()
        conn.execute("UPDATE notes SET embedding = ? WHERE id = ?",
                     (json_dumps(embedding) if embedding is not None else None, note_id))
        conn.commit()
    write_json_backup()

def update_note_fields_db(note_id, fields):
    """Partial update of note fields."""
    if not fields:
        return
    fields = dict(fields)
    if "tags" in fields:
        fields["tags"] = json_dumps(fields["tags"])
    if "pinned" in fields:
        fields["pinned"] = 1 if fields["pinned"] else 0
    if "order" in fields:
        fields["order_idx"] = fields.pop("order")
    fields["last_modified"] = datetime.now().isoformat()
    set_clause = ", ".join(f"{k} = :{k}" for k in fields if k != "id")
    fields["id"] = note_id
    with _write_lock:
        conn = get_conn()
        conn.execute(f"UPDATE notes SET {set_clause}, embedding = NULL WHERE id = :id", fields)
        conn.commit()
    write_json_backup()

# ---------- JSON backup writer ----------
def write_json_backup():
    """Write the entire notes dictionary to the JSON file (sync)."""
    notes = get_all_notes()
    try:
        with open(NOTES_JSON, "w", encoding="utf-8") as f:
            std_json.dump(notes, f, indent=2)
    except Exception as e:
        print(f"❌ Failed to write JSON backup: {e}")

# ---------- Migration from JSON (if DB empty) ----------
def migrate_from_json_if_needed():
    """If SQLite has no notes and JSON file exists, import all notes."""
    conn = get_conn()
    count = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    if count > 0:
        return
    if not os.path.exists(NOTES_JSON):
        return
    try:
        with open(NOTES_JSON, "r", encoding="utf-8") as f:
            data = json_loads(f.read())
        if not data:
            return
        for note_id, note in data.items():
            # Recreate the note in SQLite, preserving created date
            upsert_note(note_id, note, created=note.get("created"))
        print(f"✅ Migrated {len(data)} notes from JSON to SQLite")
    except Exception as e:
        print(f"❌ Migration failed: {e}")

# ---------- In‑memory cache (optional) ----------
_notes_cache = None
_cache_lock = threading.Lock()

def load_notes():
    """Load notes into cache from SQLite (or JSON fallback)."""
    global _notes_cache
    if _notes_cache is not None:
        return _notes_cache
    with _cache_lock:
        if _notes_cache is not None:
            return _notes_cache
        # Try SQLite first
        notes = get_all_notes()
        if not notes:
            # fallback to JSON (if DB empty and JSON exists)
            if os.path.exists(NOTES_JSON):
                try:
                    with open(NOTES_JSON, "r", encoding="utf-8") as f:
                        notes = json_loads(f.read())
                    # re-insert into DB
                    for nid, n in notes.items():
                        upsert_note(nid, n, created=n.get("created"))
                    print("ℹ️ Loaded notes from JSON backup into SQLite")
                except:
                    pass
        _notes_cache = notes
        return _notes_cache

# (Compatibility wrappers for old async/sync save functions)
def save_notes_async(notes_data=None):
    write_json_backup()

def save_notes_sync(notes_data=None):
    write_json_backup()

# ---------- Embedding model (lazy) ----------
_embed_model = None
_embed_model_lock = threading.Lock()

def get_embedder():
    global _embed_model
    if not EMBED_AVAILABLE:
        return None
    with _embed_model_lock:
        if _embed_model is None:
            _embed_model = SentenceTransformer('all-MiniLM-L6-v2')
        return _embed_model

def embed_note(note):
    if not EMBED_AVAILABLE:
        return None
    model = get_embedder()
    text = (note.get('title', '') + ' ' + note.get('content', '')).strip()[:1000]
    if not text:
        return None
    return model.encode(text).tolist()

# ---------- Flask Blueprint ----------
notes_bp = Blueprint('notes', __name__, url_prefix='/notes')

# Serve the notes HTML page (unchanged)
@notes_bp.route('')
def notes_page():
    return render_template_string(NOTES_HTML)

# ---------- API routes ----------
@notes_bp.route('/api', methods=['GET'])
def get_notes():
    return jsonify(load_notes())

@notes_bp.route('/api', methods=['POST'])
def create_note():
    data = request.get_json()
    note_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    note = {
        "id": note_id,
        "title": data.get("title", "Untitled"),
        "content": data.get("content", ""),
        "created": now,
        "last_modified": now,
        "order": len(load_notes()),
        "tags": data.get("tags", []),
        "pinned": data.get("pinned", False),
        "color": data.get("color", "default")
    }
    upsert_note(note_id, note, created=now)
    # Invalidate cache
    global _notes_cache
    with _cache_lock:
        _notes_cache = None
    return jsonify({"id": note_id, "ok": True})

@notes_bp.route('/api/<note_id>', methods=['PUT'])
def update_note(note_id):
    data = request.get_json()
    note = get_note_from_db(note_id)
    if not note:
        return jsonify({"error": "Note not found"}), 404
    fields = {}
    for key in ("title", "content", "tags", "pinned", "color", "order"):
        if key in data:
            fields[key] = data[key]
    if fields:
        update_note_fields_db(note_id, fields)
        global _notes_cache
        with _cache_lock:
            _notes_cache = None
    return jsonify({"ok": True})

@notes_bp.route('/api/<note_id>', methods=['DELETE'])
def delete_note(note_id):
    note = get_note_from_db(note_id)
    if not note:
        return jsonify({"error": "Note not found"}), 404
    delete_note_from_db(note_id)
    global _notes_cache
    with _cache_lock:
        _notes_cache = None
    return jsonify({"ok": True})

# ---------- Keyword search ----------
@notes_bp.route('/api/search', methods=['GET'])
def search_notes():
    query = request.args.get('q', '').strip().lower()
    tag = request.args.get('tag', '').strip().lower()
    notes = load_notes()
    results = {}
    for nid, note in notes.items():
        if tag and tag not in [t.lower() for t in note.get("tags", [])]:
            continue
        if query:
            title_match = query in note.get('title', '').lower()
            content_match = query in note.get('content', '').lower()
            tag_match = any(query in t.lower() for t in note.get("tags", []))
            if not (title_match or content_match or tag_match):
                continue
        results[nid] = note
    return jsonify(results)

# ---------- Semantic search ----------
@notes_bp.route('/api/semantic_search', methods=['GET'])
def semantic_search_notes():
    if not EMBED_AVAILABLE:
        return jsonify({"error": "Embedding model not available"}), 503
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify([])
    notes = load_notes()
    if not notes:
        return jsonify([])

    model = get_embedder()
    q_emb = model.encode(query).reshape(1, -1)

    candidates = []
    for nid, note in notes.items():
        emb = note.get('embedding')
        if emb is None:
            emb = embed_note(note)
            if emb is not None:
                note['embedding'] = emb
                set_note_embedding_db(nid, emb)
        if emb:
            candidates.append((nid, note, emb))

    if not candidates:
        return jsonify([])

    emb_matrix = np.array([c[2] for c in candidates])
    similarities = cosine_similarity(q_emb, emb_matrix).flatten()
    sorted_idx = np.argsort(similarities)[::-1]

    results = []
    for idx in sorted_idx[:10]:
        if similarities[idx] > 0.2:
            nid, note, _ = candidates[idx]
            results.append({
                "id": nid,
                "title": note["title"],
                "content": note["content"][:200] + "..." if len(note["content"]) > 200 else note["content"],
                "score": float(similarities[idx])
            })
    return jsonify(results)

# ---------- AI Assistance ----------
@notes_bp.route('/api/ai_assist', methods=['POST'])
def ai_assist():
    data = request.get_json()
    note_id = data.get('note_id')
    action = data.get('action')
    provider_name = data.get('provider', 'ollama')
    model = data.get('model', 'llama3.2')
    api_key = data.get('api_key', None)

    if not note_id or not action:
        return jsonify({"error": "Missing note_id or action"}), 400

    note = get_note_from_db(note_id)
    if not note:
        return jsonify({"error": "Note not found"}), 404

    content = note.get('content', '').strip()
    title = note.get('title', '').strip()
    if not content and not title:
        return jsonify({"error": "Note is empty"}), 400

    prompts = {
        "summarise": f"Summarise the following note in one short paragraph (max 50 words):\n\nTitle: {title}\nContent: {content}\n\nSummary:",
        "suggest_tags": f"Suggest up to 5 short tags (comma separated) for this note:\n\nTitle: {title}\nContent: {content}\n\nTags:",
        "improve": f"Rewrite the following note to improve clarity, grammar, and flow. Keep the same meaning but make it more concise and professional:\n\n{content}\n\nImproved version:"
    }
    user_prompt = prompts.get(action)
    if not user_prompt:
        return jsonify({"error": "Invalid action"}), 400

    messages = [
        {"role": "system", "content": "You are a helpful assistant that helps improve notes."},
        {"role": "user", "content": user_prompt}
    ]

    try:
        if provider_name == 'ollama':
            provider = OllamaProvider(model=model)
        elif provider_name == 'deepseek':
            provider = DeepSeekProvider(api_key=api_key)
        elif provider_name == 'claude':
            provider = ClaudeProvider(api_key=api_key)
        elif provider_name == 'huggingface':
            provider = HuggingFaceProvider(api_token=api_key)
        elif provider_name == 'groq':
            provider = GroqProvider(api_key=api_key)
        elif provider_name == 'llamacpp':
            provider = LlamaCppProvider()
        else:
            return jsonify({"error": f"Unsupported provider: {provider_name}"}), 400

        response = provider.generate(messages, model=model, api_key=api_key)
        result = response.strip()
    except Exception as e:
        return jsonify({"error": f"AI service error: {str(e)}"}), 503

    if action == 'suggest_tags' and result:
        tags = [t.strip() for t in result.split(',') if t.strip()]
        if tags:
            existing = set(note.get('tags', []))
            new_tags = [t for t in tags if t not in existing]
            if new_tags:
                update_note_fields_db(note_id, {"tags": list(existing.union(new_tags))})
                global _notes_cache
                with _cache_lock:
                    _notes_cache = None
                return jsonify({"result": result, "tags": new_tags})

    return jsonify({"result": result})

# ---------- Image upload ----------
@notes_bp.route('/api/upload_image', methods=['POST'])
def upload_image():
    if 'image' not in request.files:
        return jsonify({"error": "No image file"}), 400
    f = request.files['image']
    if f.filename == '':
        return jsonify({"error": "No file selected"}), 400
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
    if ext not in {'png', 'jpg', 'jpeg', 'gif', 'svg', 'webp'}:
        return jsonify({"error": "Unsupported image format"}), 400
    upload_dir = os.path.join('static', 'uploads', 'notes')
    os.makedirs(upload_dir, exist_ok=True)
    unique = str(uuid.uuid4()) + '.' + ext
    path = os.path.join(upload_dir, unique)
    f.save(path)
    url = f'/static/uploads/notes/{unique}'
    return jsonify({"url": url, "ok": True})

# ---------- Clear all ----------
@notes_bp.route('/api/clear_all', methods=['POST'])
def clear_all_notes():
    clear_all_notes_db()
    global _notes_cache
    with _cache_lock:
        _notes_cache = {}
    return jsonify({"ok": True})

# ---------- Reorder ----------
@notes_bp.route('/api/reorder', methods=['POST'])
def reorder_notes():
    data = request.get_json()
    order_map = data.get('order')
    if not order_map or not isinstance(order_map, dict):
        return jsonify({'error': 'Invalid order data'}), 400
    for nid, new_order in order_map.items():
        update_note_fields_db(nid, {"order": int(new_order)})
    global _notes_cache
    with _cache_lock:
        _notes_cache = None
    return jsonify({'ok': True})

# ---------- Run migration on startup ----------
migrate_from_json_if_needed()

# ===========================================================================
# HTML TEMPLATE (unchanged – copy exactly from your original notes.py)
# ===========================================================================
NOTES_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E📝%3C/text%3E%3C/svg%3E">
<title>Notes · Advanced + AI</title>
<script src="/static/vendor/marked.min.js"></script>
<style>
/* ── base – same as before ── */
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
.app { display:flex; height:100%; backdrop-filter: blur(2px); }

/* ── Sidebar ── */
.sidebar {
    width: 300px;
    background: rgba(18, 18, 26, 0.85);
    backdrop-filter: blur(20px);
    border-right: 1px solid rgba(255,255,255,0.05);
    display:flex; flex-direction:column; flex-shrink:0;
    box-shadow: 0 0 20px rgba(0,0,0,0.4);
    transition: width 0.25s ease, margin 0.25s ease, background 0.3s ease;
    overflow: hidden;
}
.sidebar.hidden { width: 0; margin: 0; border: none; overflow: hidden; padding: 0; }
.sidebar-header {
    padding: 20px 16px 12px;
    border-bottom: 1px solid rgba(255,255,255,0.05);
    display:flex; align-items:center; gap:10px; flex-wrap:wrap;
}
.sidebar-header h2 {
    font-size: 17px; font-weight: 600;
    background: linear-gradient(135deg, #58a6ff, #3fb950);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
}
.new-note-btn {
    background: linear-gradient(135deg, #1f6feb, #388bfd);
    color: white; border: none; border-radius: 10px; padding: 8px 16px;
    font-size: 13px; font-weight: 600; cursor: pointer; margin-left: auto; white-space: nowrap;
    box-shadow: 0 4px 12px rgba(31,111,235,0.4); transition: all 0.2s;
}
.new-note-btn:hover { box-shadow: 0 6px 16px rgba(31,111,235,0.6); transform: translateY(-1px); }
.search-box { padding: 8px 16px; }
.search-box input {
    width: 100%; padding: 8px 12px; border-radius: 20px;
    border: 1px solid rgba(255,255,255,0.1); background: rgba(13,17,23,0.7);
    color: #e6edf3; font-size: 13px; outline: none;
    transition: border-color 0.2s, background 0.3s, color 0.3s;
}
.search-box input:focus { border-color: #58a6ff; }
.search-box input::placeholder { color: #8b949e; }

.search-mode-toggle {
    display: flex;
    gap: 4px;
    padding: 4px 16px;
    border-bottom: 1px solid rgba(255,255,255,0.05);
}
.search-mode-toggle button {
    background: transparent;
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 16px;
    padding: 4px 14px;
    font-size: 12px;
    color: #8b949e;
    cursor: pointer;
    transition: 0.2s;
}
.search-mode-toggle button.active {
    background: #1f6feb;
    border-color: #1f6feb;
    color: #fff;
}
.search-mode-toggle button:hover { background: rgba(255,255,255,0.05); }

.tag-filter {
    padding: 4px 16px 12px;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    border-bottom: 1px solid rgba(255,255,255,0.05);
}
.tag-filter .tag-pill {
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 20px;
    padding: 4px 12px;
    font-size: 12px;
    color: #8b949e;
    cursor: pointer;
    transition: all 0.2s;
    user-select: none;
}
.tag-filter .tag-pill:hover { background: rgba(255,255,255,0.12); }
.tag-filter .tag-pill.active {
    background: #1f6feb;
    color: #fff;
    border-color: #1f6feb;
}
.tag-filter .tag-pill.clear-tag { border-color: rgba(248,81,73,0.3); color: #f85149; }
.tag-filter .tag-pill.clear-tag:hover { background: rgba(248,81,73,0.15); }

.notes-sidebar-list {
    flex:1;
    overflow-y:auto;
    padding: 8px;
}
.notes-sidebar-list::-webkit-scrollbar { width: 4px; }
.notes-sidebar-list::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 2px; }

.group-heading {
    font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: #8b949e;
    padding: 12px 12px 6px;
    border-bottom: 1px solid rgba(255,255,255,0.05);
    margin-top: 8px;
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.group-heading:first-of-type { margin-top: 0; }
.group-heading .badge {
    font-size: 10px;
    background: rgba(255,255,255,0.06);
    padding: 0 8px;
    border-radius: 10px;
    color: #8b949e;
}

/* ─── SIDEBAR NOTE ITEMS – ENHANCED SPACING ─── */
.note-item-sidebar {
    display:flex;
    align-items:center;
    padding: 12px 16px;
    cursor:grab;
    border-radius: 10px;
    margin-bottom: 4px;
    transition: background 0.2s;
    gap: 10px;
    background: transparent;
    user-select: none;
    min-height: 44px;
}
.note-item-sidebar:hover { background: rgba(255,255,255,0.05); }
.note-item-sidebar.active { background: rgba(31,111,235,0.15); border: 1px solid rgba(31,111,235,0.3); }
.note-item-sidebar.dragging { opacity: 0.4; }
.note-item-sidebar.drag-over { border: 2px dashed #58a6ff; }
.note-item-sidebar .pin-indicator { font-size: 13px; opacity: 0.5; margin-right: 2px; }
.note-item-sidebar .title {
    flex:1;
    font-size: 15px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    color: #c9d1d9;
    font-weight: 500;
}
.note-item-sidebar .tags-mini {
    font-size: 10px;
    color: #8b949e;
    margin-right: 4px;
    max-width: 70px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    opacity: 0.6;
}
.note-item-sidebar .rename-btn,
.note-item-sidebar .del {
    background: transparent;
    border: none;
    font-size: 14px;
    cursor: pointer;
    opacity: 0.4;
    padding: 4px 6px;
    transition: opacity 0.2s, color 0.2s;
    border-radius: 4px;
}
.note-item-sidebar .rename-btn:hover {
    opacity: 1;
    color: #58a6ff;
    background: rgba(88,166,255,0.1);
}
.note-item-sidebar .del {
    color: #f85149;
    font-size: 18px;
}
.note-item-sidebar .del:hover {
    opacity: 1;
    background: rgba(248,81,73,0.15);
}
.note-item-sidebar .time {
    font-size: 10px;
    color: #8b949e;
    margin-right: 4px;
    white-space: nowrap;
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

/* ── Main panel ── */
.main {
    flex:1;
    display:flex;
    flex-direction:column;
    min-width:0;
    background: rgba(10,10,15,0.7);
    backdrop-filter: blur(10px);
    transition: background 0.3s ease;
}

/* Top bar */
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
.top-bar .left { display: flex; align-items: center; gap: 12px; justify-self: start; }
.top-bar .left h1 {
    font-size: 19px;
    background: linear-gradient(135deg, #58a6ff, #a371f7);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    font-weight: 700;
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
.center-tabs .tab-btn:hover { color: #c9d1d9; background: rgba(255,255,255,0.05); }
.center-tabs .tab-btn.active { background: #1f6feb; color: #fff; box-shadow: 0 2px 8px rgba(31,111,235,0.3); }
body.light-mode .center-tabs { background: rgba(0,0,0,0.04); border-color: rgba(0,0,0,0.06); }
body.light-mode .center-tabs .tab-btn { color: #57606a; }
body.light-mode .center-tabs .tab-btn:hover { background: rgba(0,0,0,0.04); color: #1f6feb; }
body.light-mode .center-tabs .tab-btn.active { background: #1f6feb; color: #fff; }
.top-bar .right {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    justify-self: end;
    justify-content: flex-end;
}
.clear-btn {
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
.clear-btn:hover { background: rgba(248,81,73,0.15); border-color: #f85149; }

/* Theme toggle */
.theme-toggle-wrapper { display: inline-block; vertical-align: middle; }
.toggle-outer {
    position: relative; width: 140px; height: 56px; border-radius: 999px; background: hsl(220 18% 82%);
    box-shadow: 2px 2px 8px rgba(0,0,0,0.12), -2px -2px 6px rgba(255,255,255,0.5),
                inset 1px 1px 3px rgba(0,0,0,0.08), inset -1px -1px 3px rgba(255,255,255,0.4);
    cursor: pointer; user-select: none; flex-shrink:0;
}
.toggle-inner { position: absolute; inset: 5px; border-radius: 999px; overflow: hidden; }
.night-bg { position: absolute; inset: 0; background: hsl(220 35% 18%); opacity:1; transition: opacity 0.3s ease; }
.stars-layer { position: absolute; inset: 0; opacity:1; transition: opacity 0.3s ease; pointer-events:none; }
.star { position: absolute; background: white; border-radius:50%; }
.sparkle { position: absolute; color: white; font-size: 7px; line-height:1; }
.day-bg { position: absolute; inset: 0; opacity:0; transition: opacity 0.3s ease; pointer-events:none; }
.sky-layer { position: absolute; inset: 0; background: hsl(205 70% 62%); }
.sky-mid { position: absolute; bottom:0; left:0; right:0; height:50%; background: hsl(205 60% 72%); border-radius: 40% 40% 0 0 / 30% 30% 0 0; }
.cloud { position: absolute; background: rgba(255,255,255,0.88); border-radius: 999px; }
.astronaut, .biplane { position: absolute; z-index: 4; pointer-events: none; transition: opacity 0.3s ease; }
.astronaut { left: 48px; top: 50%; transform: translateY(-55%); width: 22px; height: 26px; opacity:1; animation: float 3s ease-in-out infinite; }
.biplane { left: 44px; top: 38%; transform: translateY(-50%); width: 30px; height: 18px; opacity:0; animation: fly 3s ease-in-out infinite; }
@keyframes float { 0%,100% { transform: translateY(-55%); } 50% { transform: translateY(-65%); } }
@keyframes fly { 0%,100% { transform: translateY(-50%) rotate(-1deg); } 50% { transform: translateY(-60%) rotate(1deg); } }
.knob {
    position: absolute; top: 50%; width: 40px; height: 40px; border-radius: 50%; transform: translateY(-50%);
    z-index: 10; cursor: grab; transition: left 0.4s cubic-bezier(0.34, 1.2, 0.64, 1); left: 3px;
}
.knob:active { cursor: grabbing; }
.knob-moon {
    position: absolute; inset:0; border-radius:50%; background: hsl(220 10% 82%);
    box-shadow: 2px 2px 4px rgba(255,255,255,0.9) inset, -2px -2px 4px rgba(0,0,0,0.18) inset;
    transition: opacity 0.3s ease;
}
.knob-moon .crater {
    position: absolute; border-radius:50%; background: hsl(220 8% 67%);
    box-shadow: 1px 1px 2px rgba(255,255,255,0.4) inset, -1px -1px 2px rgba(0,0,0,0.2) inset;
}
.knob-sun {
    position: absolute; inset:0; border-radius:50%; background: hsl(44 100% 58%);
    box-shadow: 2px 2px 6px rgba(255,255,180,0.9) inset, -2px -2px 4px rgba(180,100,0,0.3) inset,
                0 0 12px hsl(44 100% 70% / 0.5);
    opacity: 0; transition: opacity 0.3s ease;
}
.toggle-outer.day .night-bg { opacity: 0; }
.toggle-outer.day .stars-layer { opacity: 0; }
.toggle-outer.day .day-bg { opacity: 1; }
.toggle-outer.day .knob { left: 93px; }
.toggle-outer.day .knob-moon { opacity: 0; }
.toggle-outer.day .knob-sun { opacity: 1; }
.toggle-outer.day .astronaut { opacity: 0; }
.toggle-outer.day .biplane { opacity: 1; }

/* ── Notes editor & preview ── */
.notes-panel {
    flex:1;
    overflow-y:auto;
    padding: 28px 40px 40px;
    display:flex;
    flex-direction:column;
    gap: 24px;
}
.notes-panel .note-editor {
    background: rgba(13,17,23,0.7);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 14px;
    padding: 24px 24px 20px;
    margin-bottom: 8px;
    transition: background 0.3s, border-color 0.3s;
}
.notes-panel .note-editor .editor-header {
    display:flex;
    align-items:center;
    gap: 12px;
    margin-bottom: 12px;
    flex-wrap:wrap;
}
.notes-panel .note-editor .editor-header input[type="text"] {
    flex:1;
    background: transparent;
    border: none;
    color: #e6edf3;
    font-weight: 600;
    font-size: 20px;
    outline: none;
    min-width: 120px;
    padding: 4px 0;
}
.notes-panel .note-editor .editor-header .toolbar {
    display:flex;
    gap: 6px;
    flex-wrap:wrap;
    align-items:center;
}
.notes-panel .note-editor .editor-header .toolbar button {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 6px;
    color: #8b949e;
    padding: 5px 10px;
    font-size: 13px;
    cursor: pointer;
    transition: 0.2s;
}
.notes-panel .note-editor .editor-header .toolbar button:hover {
    background: rgba(255,255,255,0.12);
    color: #e6edf3;
}
.notes-panel .note-editor .editor-header .toolbar button.active {
    background: #1f6feb;
    color: #fff;
    border-color: #1f6feb;
}
.notes-panel .note-editor .editor-header .toolbar .ai-assist-btn {
    background: #6f42c1;
    border-color: #6f42c1;
    color: #fff;
}
.notes-panel .note-editor .editor-header .toolbar .ai-assist-btn:hover {
    background: #8b5cf6;
}
.notes-panel .note-editor .editor-header .toolbar select,
.notes-panel .note-editor .editor-header .toolbar input[type="password"] {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 6px;
    color: #8b949e;
    padding: 5px 10px;
    font-size: 13px;
    outline: none;
    max-width: 140px;
}
.notes-panel .note-editor .editor-header .toolbar select:focus,
.notes-panel .note-editor .editor-header .toolbar input[type="password"]:focus {
    border-color: #58a6ff;
}
.notes-panel .note-editor .editor-header .toolbar input[type="password"] {
    max-width: 120px;
    display: none;
}
body.light-mode .notes-panel .note-editor .editor-header .toolbar select,
body.light-mode .notes-panel .note-editor .editor-header .toolbar input[type="password"] {
    background: rgba(0,0,0,0.04);
    color: #24292f;
}

.notes-panel .note-editor .editor-body {
    display:flex;
    gap: 16px;
    min-height: 220px;
}
.notes-panel .note-editor .editor-body textarea {
    flex:1;
    background: transparent;
    border: none;
    color: #e6edf3;
    font-size: 14px;
    font-family: inherit;
    outline: none;
    resize: vertical;
    min-height: 200px;
    padding: 8px 8px 4px;
    line-height: 1.7;
}
.notes-panel .note-editor .editor-body .preview {
    flex:1;
    background: rgba(0,0,0,0.2);
    border-radius: 8px;
    padding: 12px 16px;
    overflow-y:auto;
    color: #e1e4e8;
    font-size: 14px;
    line-height: 1.7;
    display:none;
}
.notes-panel .note-editor .editor-body .preview.visible {
    display:block;
}
.notes-panel .note-editor .editor-footer {
    display:flex;
    align-items:center;
    gap: 16px;
    margin-top: 16px;
    flex-wrap:wrap;
}
.notes-panel .note-editor .editor-footer .tags-input {
    flex:1;
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 20px;
    padding: 6px 14px;
    color: #e6edf3;
    font-size: 13px;
    outline: none;
    min-width: 80px;
}
.notes-panel .note-editor .editor-footer .tags-input:focus { border-color: #58a6ff; }
.notes-panel .note-editor .editor-footer .color-picker {
    display:flex;
    gap: 8px;
}
.notes-panel .note-editor .editor-footer .color-picker .color-option {
    width: 24px;
    height: 24px;
    border-radius:50%;
    cursor:pointer;
    border:2px solid transparent;
    transition: 0.2s;
}
.notes-panel .note-editor .editor-footer .color-picker .color-option:hover { transform:scale(1.15); }
.notes-panel .note-editor .editor-footer .color-picker .color-option.active {
    border-color: #58a6ff;
    box-shadow: 0 0 8px rgba(88,166,255,0.4);
}
.color-default { background: #8b949e; }
.color-yellow { background: #fdec8f; }
.color-blue   { background: #9fd3ff; }
.color-green  { background: #a9e6a1; }
.color-pink   { background: #ffb3cd; }
.color-orange { background: #ffc27a; }

.notes-panel .note-editor .editor-footer .pin-toggle {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 20px;
    padding: 6px 16px;
    color: #8b949e;
    font-size: 13px;
    cursor: pointer;
    transition: 0.2s;
}
.notes-panel .note-editor .editor-footer .pin-toggle.active {
    background: rgba(31,111,235,0.2);
    border-color: #1f6feb;
    color: #58a6ff;
}
.notes-panel .note-editor .editor-footer .pin-toggle:hover { background: rgba(255,255,255,0.1); }

.notes-panel .note-editor .note-actions {
    display:flex;
    gap: 10px;
    justify-content:flex-end;
    margin-top: 16px;
}
.notes-panel .note-editor .note-actions button {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.1);
    color: #8b949e;
    border-radius: 8px;
    padding: 8px 22px;
    cursor: pointer;
    font-size: 14px;
    transition: 0.2s;
}
.notes-panel .note-editor .note-actions .save-note { background: #1f6feb; color: white; border-color: #1f6feb; }
.notes-panel .note-editor .note-actions .save-note:hover { background: #388bfd; }
.notes-panel .note-editor .note-actions button:hover { background: rgba(255,255,255,0.1); }

/* ─── MAIN NOTE ITEMS – MORE SPACING ─── */
.notes-panel .note-item {
    background: rgba(28, 35, 51, 0.6);
    backdrop-filter: blur(8px);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 14px;
    padding: 22px 28px;
    transition: 0.2s;
    margin-bottom: 16px;
    position:relative;
}
.notes-panel .note-item:hover { background: rgba(28, 35, 51, 0.8); }
.notes-panel .note-item .note-title {
    font-weight: 600;
    font-size: 18px;
    margin-bottom: 8px;
    color: #e6edf3;
}
.notes-panel .note-item .note-meta {
    font-size: 12px;
    color: #8b949e;
    margin-bottom: 10px;
    display:flex;
    gap: 12px;
    flex-wrap:wrap;
}
.notes-panel .note-item .note-meta .tag {
    background: rgba(255,255,255,0.06);
    padding: 0 12px;
    border-radius: 12px;
    color: #58a6ff;
}
.notes-panel .note-item .note-content {
    font-size: 14px;
    color: #8b949e;
    white-space: pre-wrap;
    word-wrap: break-word;
    max-height: 140px;
    overflow:hidden;
    line-height: 1.7;
}
.notes-panel .note-item .note-actions {
    margin-top: 14px;
    display: flex;
    gap: 12px;
}
.notes-panel .note-item .note-actions button {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.1);
    color: #8b949e;
    border-radius: 8px;
    padding: 6px 16px;
    cursor: pointer;
    font-size: 13px;
    transition: 0.2s;
}
.notes-panel .note-item .note-actions button:hover { background: rgba(255,255,255,0.1); color: #58a6ff; }
.notes-panel .note-item .note-actions .delete-note { color: #f85149; }
.notes-panel .note-item .note-actions .delete-note:hover { background: rgba(248,81,73,0.15); border-color: #f85149; }

/* Light mode overrides */
body.light-mode {
    background: #f6f8fa;
    color: #24292f;
}
body.light-mode .sidebar { background: rgba(255, 255, 255, 0.92); border-right-color: rgba(0,0,0,0.08); }
body.light-mode .sidebar .sidebar-header { border-bottom-color: rgba(0,0,0,0.06); }
body.light-mode .sidebar .group-heading { color: #57606a; border-bottom-color: rgba(0,0,0,0.06); }
body.light-mode .note-item-sidebar:hover { background: rgba(0,0,0,0.04); }
body.light-mode .note-item-sidebar.active { background: rgba(31,111,235,0.12); border-color: rgba(31,111,235,0.3); }
body.light-mode .note-item-sidebar .title { color: #24292f; }
body.light-mode .note-item-sidebar .time { color: #57606a; }
body.light-mode .note-item-sidebar .rename-btn { color: #57606a; }
body.light-mode .sidebar-footer { color: #57606a; border-top-color: rgba(0,0,0,0.06); }
body.light-mode .main { background: rgba(255,255,255,0.85); }
body.light-mode .top-bar { background: rgba(255, 255, 255, 0.9); border-bottom-color: rgba(0,0,0,0.08); box-shadow: 0 1px 4px rgba(0,0,0,0.05); }
body.light-mode .top-bar .left h1 {
    background: linear-gradient(135deg, #1f6feb, #a371f7);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
}
body.light-mode .clear-btn { background: rgba(0,0,0,0.05); border-color: rgba(248,81,73,0.3); color: #f85149; }
body.light-mode .clear-btn:hover { background: rgba(248,81,73,0.08); }
body.light-mode .search-box input { background: rgba(255,255,255,0.8); color: #24292f; border-color: rgba(0,0,0,0.12); }
body.light-mode .search-box input::placeholder { color: #8b949e; }
body.light-mode .notes-panel .note-item { background: rgba(255,255,255,0.8); border-color: rgba(0,0,0,0.06); }
body.light-mode .notes-panel .note-item:hover { background: rgba(255,255,255,0.95); }
body.light-mode .notes-panel .note-title { color: #24292f; }
body.light-mode .notes-panel .note-content { color: #57606a; }
body.light-mode .notes-panel .note-editor { background: rgba(255,255,255,0.8); border-color: rgba(0,0,0,0.08); }
body.light-mode .notes-panel .note-editor input,
body.light-mode .notes-panel .note-editor textarea,
body.light-mode .notes-panel .note-editor .editor-body .preview { color: #24292f; }
body.light-mode .notes-panel .note-editor .editor-body .preview { background: rgba(0,0,0,0.03); }
body.light-mode .notes-panel .note-editor .editor-header .toolbar button { color: #57606a; }
body.light-mode .notes-panel .note-editor .editor-header .toolbar button:hover { background: rgba(0,0,0,0.06); }
body.light-mode .notes-panel .note-editor .editor-footer .tags-input { background: rgba(0,0,0,0.04); color: #24292f; }
body.light-mode .notes-panel .note-editor .editor-footer .pin-toggle { color: #57606a; }
body.light-mode .notes-panel .note-editor .editor-footer .pin-toggle.active { background: rgba(31,111,235,0.1); color: #1f6feb; }
body.light-mode .tag-filter .tag-pill { background: rgba(0,0,0,0.04); border-color: rgba(0,0,0,0.08); color: #57606a; }
body.light-mode .tag-filter .tag-pill.active { background: #1f6feb; color: #fff; border-color: #1f6feb; }
body.light-mode .tag-filter .tag-pill.clear-tag { border-color: rgba(248,81,73,0.3); color: #f85149; }
body.light-mode .tag-filter .tag-pill.clear-tag:hover { background: rgba(248,81,73,0.08); }
.search-mode-toggle button { background: transparent; border: 1px solid rgba(255,255,255,0.1); border-radius: 16px; padding: 4px 14px; font-size: 12px; color: #8b949e; cursor: pointer; transition: 0.2s; }
.search-mode-toggle button.active { background: #1f6feb; border-color: #1f6feb; color: #fff; }
.search-mode-toggle button:hover { background: rgba(255,255,255,0.05); }
body.light-mode .search-mode-toggle button { color: #57606a; border-color: rgba(0,0,0,0.1); }
body.light-mode .search-mode-toggle button.active { background: #1f6feb; color: #fff; border-color: #1f6feb; }

/* Fix for Edit & Delete buttons in light mode */
body.light-mode .notes-panel .note-item .note-actions button {
    background: rgba(0, 0, 0, 0.05);
    border: 1px solid rgba(0, 0, 0, 0.1);
    color: #24292f;
}
body.light-mode .notes-panel .note-item .note-actions button:hover {
    background: rgba(0, 0, 0, 0.1);
    color: #1f6feb;
    border-color: #1f6feb;
}
body.light-mode .notes-panel .note-item .note-actions .delete-note {
    color: #f85149;
}
body.light-mode .notes-panel .note-item .note-actions .delete-note:hover {
    background: rgba(248, 81, 73, 0.1);
    border-color: #f85149;
}

/* Image upload button */
.image-upload-btn {
    background: rgba(88,166,255,0.15);
    border-color: #58a6ff;
    color: #58a6ff;
}
.image-upload-btn:hover {
    background: rgba(88,166,255,0.25);
}
</style>
</head>
<body>
<div class="app">
    <!-- SIDEBAR -->
    <div class="sidebar" id="sidebar">
        <div class="sidebar-header">
            <h2>📖 Notes</h2>
            <button class="new-note-btn" onclick="createNewNote()">+ New</button>
        </div>
        <div class="search-box">
            <input type="text" id="searchInput" placeholder="🔍 Search notes..." oninput="searchNotes()">
        </div>
        <!-- Search mode toggle -->
        <div class="search-mode-toggle">
            <button id="searchModeKeyword" class="active" onclick="setSearchMode('keyword')">Keyword</button>
            <button id="searchModeSemantic" onclick="setSearchMode('semantic')">🧠 Semantic</button>
        </div>
        <div class="tag-filter" id="tagFilterContainer"></div>
        <div class="notes-sidebar-list" id="notesSidebarList"></div>
        <div class="sidebar-footer">Drag to reorder · ✏️ to rename · 🏷️ tags</div>
    </div>

    <!-- MAIN PANEL -->
    <div class="main">
        <!-- Top bar -->
        <div class="top-bar">
            <div class="left">
                <button class="sidebar-toggle" onclick="toggleSidebar()" title="Toggle sidebar">
                    <svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                        <rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect>
                        <line x1="9" y1="3" x2="9" y2="21"></line>
                    </svg>
                </button>
                <h1>📚 Trio-Forge Custom Notes</h1>
            </div>

            <div class="center-tabs">
                <a href="/" class="tab-btn" style="text-decoration:none;">💬 Chat</a>
                <button class="tab-btn active">📝 Notes</button>
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
                <button class="clear-btn" onclick="clearAllNotes()">🗑 Clear All</button>
            </div>
        </div>

        <!-- Notes panel -->
        <div class="notes-panel" id="notesPanel">
            <div class="note-editor" id="noteEditor" style="display:none;">
                <div class="editor-header">
                    <input type="text" id="noteTitleInput" placeholder="Note title...">
                    <div class="toolbar" id="toolbar">
                        <button data-cmd="bold" title="Bold">B</button>
                        <button data-cmd="italic" title="Italic">I</button>
                        <button data-cmd="heading" title="Heading">H</button>
                        <button data-cmd="list" title="Bullet list">•</button>
                        <button data-cmd="code" title="Code block">{ }</button>
                        <button data-cmd="preview" title="Toggle preview" id="previewToggle">👁️</button>
                        <!-- Image Upload Button -->
                        <button class="image-upload-btn" id="imageUploadBtn" title="Insert Image">🖼️</button>
                        <input type="file" id="imageFileInput" accept="image/*" style="display:none;">
                        <!-- AI Assistance -->
                        <select id="aiActionSelect" style="background:rgba(255,255,255,0.05); border:1px solid rgba(255,255,255,0.1); border-radius:6px; color:#8b949e; padding:4px 8px; font-size:13px;">
                            <option value="summarise">Summarise</option>
                            <option value="suggest_tags">Suggest Tags</option>
                            <option value="improve">Improve Writing</option>
                        </select>
                        <select id="aiProviderSelect" style="background:rgba(255,255,255,0.05); border:1px solid rgba(255,255,255,0.1); border-radius:6px; color:#8b949e; padding:4px 8px; font-size:13px;">
                            <option value="ollama">Ollama</option>
                            <option value="llamacpp">llama.cpp</option>
                            <option value="huggingface">Hugging Face</option>
                            <option value="groq">Groq</option>
                            <option value="deepseek">DeepSeek</option>
                            <option value="claude">Claude</option>
                        </select>
                        <input type="password" id="aiApiKeyInput" placeholder="API Key" style="max-width:120px; display:none;">
                        <select id="aiModelSelect" title="Select model for AI assistance" style="max-width:160px;">
                            <option value="">Loading models...</option>
                        </select>
                        <button class="ai-assist-btn" id="aiAssistBtn" title="Run AI assistance">✨ AI</button>
                    </div>
                </div>
                <div class="editor-body">
                    <textarea id="noteContentInput" placeholder="Write your note in Markdown..."></textarea>
                    <div class="preview" id="notePreview"></div>
                </div>
                <div class="editor-footer">
                    <input type="text" class="tags-input" id="noteTagsInput" placeholder="Tags (comma separated)">
                    <div class="color-picker" id="colorPicker">
                        <span class="color-option color-default active" data-color="default" title="Default"></span>
                        <span class="color-option color-yellow" data-color="yellow" title="Yellow"></span>
                        <span class="color-option color-blue" data-color="blue" title="Blue"></span>
                        <span class="color-option color-green" data-color="green" title="Green"></span>
                        <span class="color-option color-pink" data-color="pink" title="Pink"></span>
                        <span class="color-option color-orange" data-color="orange" title="Orange"></span>
                    </div>
                    <button class="pin-toggle" id="pinToggle">📌 Pin</button>
                </div>
                <div class="note-actions">
                    <button class="save-note" id="saveNoteBtn">💾 Save Note</button>
                    <button id="cancelNoteBtn" style="display:none;">Cancel</button>
                </div>
                <!-- AI result area -->
                <div id="aiResult" style="display:none; margin-top:12px; padding:10px; background:rgba(255,255,255,0.05); border-radius:8px; border:1px solid rgba(255,255,255,0.1); color:#e1e4e8; font-size:14px; white-space:pre-wrap;"></div>
            </div>
            <div id="notesList"></div>
        </div>
    </div>
</div>

<script>
    // ─── Theme ───────────────────────────
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

    // ─── Sidebar toggle ────────────────────────────────
    var sidebar = document.getElementById('sidebar');
    var sidebarVisible = localStorage.getItem('notesSidebarVisible') !== 'false';
    function toggleSidebar() {
        sidebarVisible = !sidebarVisible;
        localStorage.setItem('notesSidebarVisible', sidebarVisible);
        sidebar.classList.toggle('hidden', !sidebarVisible);
    }
    if (!sidebarVisible) sidebar.classList.add('hidden');

    // ─── State ──────────────────────────────────────────
    var notesData = {};
    var editingNoteId = null;
    var currentColor = 'default';
    var currentPinned = false;
    var searchTimeout = null;
    var activeTagFilter = '';
    var searchMode = 'keyword';

    // ─── Markdown preview ──────────────────────────────
    function updatePreview() {
        var content = document.getElementById('noteContentInput').value;
        var preview = document.getElementById('notePreview');
        preview.innerHTML = marked.parse(content || '');
    }
    document.getElementById('noteContentInput').addEventListener('input', updatePreview);

    // ─── Toolbar commands ──────────────────────────────
    document.querySelectorAll('#toolbar button[data-cmd]').forEach(btn => {
        btn.addEventListener('click', function() {
            var cmd = this.dataset.cmd;
            var textarea = document.getElementById('noteContentInput');
            var start = textarea.selectionStart;
            var end = textarea.selectionEnd;
            var selected = textarea.value.substring(start, end);
            var replacement = '';
            if (cmd === 'bold') replacement = '**' + selected + '**';
            else if (cmd === 'italic') replacement = '*' + selected + '*';
            else if (cmd === 'heading') replacement = '# ' + selected;
            else if (cmd === 'list') replacement = '- ' + selected;
            else if (cmd === 'code') replacement = '```\n' + selected + '\n```';
            else if (cmd === 'preview') {
                document.getElementById('notePreview').classList.toggle('visible');
                this.classList.toggle('active');
                return;
            }
            if (replacement) {
                textarea.value = textarea.value.substring(0, start) + replacement + textarea.value.substring(end);
                textarea.focus();
                textarea.selectionStart = start + replacement.length;
                textarea.selectionEnd = start + replacement.length;
                updatePreview();
            }
        });
    });

    // ─── Image Upload ──────────────────────────────────
    document.getElementById('imageUploadBtn').addEventListener('click', function() {
        document.getElementById('imageFileInput').click();
    });
    document.getElementById('imageFileInput').addEventListener('change', function(e) {
        var file = e.target.files[0];
        if (!file) return;
        var formData = new FormData();
        formData.append('image', file);
        var textarea = document.getElementById('noteContentInput');
        var start = textarea.selectionStart;
        var end = textarea.selectionEnd;
        var filename = file.name.replace(/\.[^.]+$/, '');
        fetch('/notes/api/upload_image', {
            method: 'POST',
            body: formData
        })
        .then(r => r.json())
        .then(data => {
            if (data.ok) {
                var markdown = `![${filename}](${data.url})`;
                textarea.value = textarea.value.substring(0, start) + markdown + textarea.value.substring(end);
                textarea.focus();
                textarea.selectionStart = start + markdown.length;
                textarea.selectionEnd = start + markdown.length;
                updatePreview();
            } else {
                alert('Image upload failed: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(err => alert('Upload error: ' + err));
        e.target.value = '';
    });

    // ─── Multi-provider AI setup ──────────────────────
    var aiProviderSelect = document.getElementById('aiProviderSelect');
    var aiApiKeyInput = document.getElementById('aiApiKeyInput');
    var aiModelSelect = document.getElementById('aiModelSelect');

    function loadAIPreferences() {
        var provider = localStorage.getItem('notes_ai_provider') || 'ollama';
        var apiKey = localStorage.getItem('notes_ai_api_key_' + provider) || '';
        aiProviderSelect.value = provider;
        aiApiKeyInput.value = apiKey;
        toggleApiKeyVisibility(provider);
        loadModelsForProvider(provider, apiKey);
    }

    function toggleApiKeyVisibility(provider) {
        var show = ['groq', 'huggingface', 'deepseek', 'claude'].includes(provider);
        aiApiKeyInput.style.display = show ? 'inline-block' : 'none';
    }

    function loadModelsForProvider(provider, apiKey) {
        fetch('/providers/models', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ provider: provider, api_key: apiKey })
        })
            .then(r => r.json())
            .then(data => {
                if (data.error) {
                    aiModelSelect.innerHTML = '<option value="">⚠️ ' + data.error + '</option>';
                    return;
                }
                var models = data.models || [];
                var current = localStorage.getItem('notes_ai_model') || '';
                aiModelSelect.innerHTML = '';
                if (models.length) {
                    models.forEach(m => {
                        var opt = document.createElement('option');
                        opt.value = m;
                        opt.textContent = m;
                        aiModelSelect.appendChild(opt);
                    });
                    if (current && models.includes(current)) {
                        aiModelSelect.value = current;
                    } else {
                        aiModelSelect.value = models[0];
                        localStorage.setItem('notes_ai_model', models[0]);
                    }
                } else {
                    aiModelSelect.innerHTML = '<option value="">No models found</option>';
                }
            })
            .catch(err => {
                console.error('Failed to load models:', err);
                aiModelSelect.innerHTML = '<option value="">⚠️ Cannot reach server</option>';
            });
    }

    aiProviderSelect.addEventListener('change', function() {
        var provider = this.value;
        var apiKey = aiApiKeyInput.value;
        localStorage.setItem('notes_ai_provider', provider);
        toggleApiKeyVisibility(provider);
        loadModelsForProvider(provider, apiKey);
    });

    aiApiKeyInput.addEventListener('blur', function() {
        var provider = aiProviderSelect.value;
        localStorage.setItem('notes_ai_api_key_' + provider, this.value);
    });

    aiModelSelect.addEventListener('change', function() {
        localStorage.setItem('notes_ai_model', this.value);
    });

    // ─── AI Assist ──────────────────────────────────
    document.getElementById('aiAssistBtn').addEventListener('click', function() {
        if (!editingNoteId) {
            alert('Please save the note first, then use AI assist.');
            return;
        }
        var action = document.getElementById('aiActionSelect').value;
        var provider = aiProviderSelect.value;
        var model = aiModelSelect.value;
        var apiKey = aiApiKeyInput.value;

        if (!model) {
            alert('No model selected. Please wait for models to load or select one.');
            return;
        }

        var resultDiv = document.getElementById('aiResult');
        resultDiv.style.display = 'block';
        resultDiv.textContent = '⏳ Thinking...';
        fetch('/notes/api/ai_assist', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                note_id: editingNoteId,
                action: action,
                provider: provider,
                model: model,
                api_key: apiKey
            })
        })
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                resultDiv.textContent = '❌ ' + data.error;
                return;
            }
            if (action === 'suggest_tags' && data.tags) {
                var tagsInput = document.getElementById('noteTagsInput');
                var existing = tagsInput.value.split(',').map(s => s.trim()).filter(Boolean);
                var newTags = data.tags.filter(t => !existing.includes(t));
                if (newTags.length) {
                    tagsInput.value = existing.concat(newTags).join(', ');
                }
                resultDiv.textContent = '✅ Suggested tags: ' + data.tags.join(', ') + ' (added to tags field)';
            } else {
                resultDiv.textContent = data.result || 'Done.';
            }
        })
        .catch(err => {
            resultDiv.textContent = '❌ Error: ' + err;
        });
    });

    // ─── Color picker ──────────────────────────────────
    document.querySelectorAll('#colorPicker .color-option').forEach(el => {
        el.addEventListener('click', function() {
            document.querySelectorAll('#colorPicker .color-option').forEach(c => c.classList.remove('active'));
            this.classList.add('active');
            currentColor = this.dataset.color;
        });
    });

    // ─── Pin toggle ────────────────────────────────────
    document.getElementById('pinToggle').addEventListener('click', function() {
        currentPinned = !currentPinned;
        this.classList.toggle('active', currentPinned);
        this.textContent = currentPinned ? '📌 Pinned' : '📌 Pin';
    });

    // ─── Load & render notes ───────────────────────────
    function loadNotes() {
        var query = document.getElementById('searchInput').value.trim();
        var tag = activeTagFilter;
        var url;
        if (query && searchMode === 'semantic') {
            url = '/notes/api/semantic_search?q=' + encodeURIComponent(query);
            fetch(url)
                .then(r => r.json())
                .then(results => {
                    loadAllNotesAndFilter(query, tag, results);
                })
                .catch(e => {
                    console.error('Semantic search failed:', e);
                    loadAllNotesAndFilter(query, tag, null);
                });
        } else {
            url = '/notes/api/search?q=' + encodeURIComponent(query) + '&tag=' + encodeURIComponent(tag);
            fetch(url)
                .then(r => r.json())
                .then(notes => {
                    notesData = notes;
                    renderSidebar(notes);
                    renderMain(notes);
                    renderTagFilter(notes);
                })
                .catch(e => console.error('Failed to load notes:', e));
        }
    }

    function loadAllNotesAndFilter(query, tag, semanticResults) {
        fetch('/notes/api?q=&tag=')
            .then(r => r.json())
            .then(allNotes => {
                var filtered = {};
                if (semanticResults && semanticResults.length) {
                    var ids = semanticResults.map(r => r.id);
                    ids.forEach(id => {
                        if (allNotes[id]) filtered[id] = allNotes[id];
                    });
                } else {
                    for (var nid in allNotes) {
                        var note = allNotes[nid];
                        var match = true;
                        if (tag && !note.tags.map(t => t.toLowerCase()).includes(tag)) match = false;
                        if (query && match) {
                            var q = query.toLowerCase();
                            var titleMatch = note.title.toLowerCase().includes(q);
                            var contentMatch = note.content.toLowerCase().includes(q);
                            var tagMatch = note.tags.some(t => t.toLowerCase().includes(q));
                            if (!(titleMatch || contentMatch || tagMatch)) match = false;
                        }
                        if (match) filtered[nid] = note;
                    }
                }
                notesData = filtered;
                renderSidebar(filtered);
                renderMain(filtered);
                renderTagFilter(filtered);
            });
    }

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

    function renderSidebar(notes) {
        const listEl = document.getElementById('notesSidebarList');
        listEl.innerHTML = '';
        const ids = Object.keys(notes);
        if (!ids.length) {
            listEl.innerHTML = '<div class="no-results">🔍 No notes found</div>';
            return;
        }
        var pinned = [];
        var unpinned = [];
        ids.forEach(id => {
            if (notes[id].pinned) pinned.push(id);
            else unpinned.push(id);
        });
        var sortFn = (a, b) => {
            var orderA = notes[a].order || 0;
            var orderB = notes[b].order || 0;
            if (orderA !== orderB) return orderA - orderB;
            return new Date(notes[b].created) - new Date(notes[a].created);
        };
        pinned.sort(sortFn);
        unpinned.sort(sortFn);
        var allIds = pinned.concat(unpinned);

        var groups = {};
        allIds.forEach(id => {
            var note = notes[id];
            var group = getDateGroup(note.created);
            if (!groups[group]) groups[group] = [];
            groups[group].push({ id, note });
        });

        var groupOrder = ['Today', 'Yesterday', 'This Week', 'Last Week', 'Older'];
        groupOrder.forEach(groupName => {
            var items = groups[groupName];
            if (!items) return;
            var heading = document.createElement('div');
            heading.className = 'group-heading';
            heading.textContent = groupName;
            var pinnedCount = items.filter(({note}) => note.pinned).length;
            if (pinnedCount) {
                var badge = document.createElement('span');
                badge.className = 'badge';
                badge.textContent = '📌 ' + pinnedCount;
                heading.appendChild(badge);
            }
            listEl.appendChild(heading);

            items.forEach(({id, note}) => {
                var div = document.createElement('div');
                div.className = 'note-item-sidebar';
                if (id === editingNoteId) div.classList.add('active');
                div.dataset.id = id;
                div.draggable = true;

                if (note.pinned) {
                    var pin = document.createElement('span');
                    pin.className = 'pin-indicator';
                    pin.textContent = '📌';
                    div.appendChild(pin);
                }

                var titleSpan = document.createElement('span');
                titleSpan.className = 'title';
                titleSpan.textContent = note.title || 'Untitled';
                div.appendChild(titleSpan);

                if (note.tags && note.tags.length) {
                    var tagSpan = document.createElement('span');
                    tagSpan.className = 'tags-mini';
                    tagSpan.textContent = note.tags.join(', ');
                    div.appendChild(tagSpan);
                }

                var renameBtn = document.createElement('button');
                renameBtn.className = 'rename-btn';
                renameBtn.textContent = '✏️';
                renameBtn.title = 'Rename this note';
                renameBtn.onclick = function(e) { e.stopPropagation(); editNoteFromSidebar(id); };
                div.appendChild(renameBtn);

                var timeSpan = document.createElement('span');
                timeSpan.className = 'time';
                var d = new Date(note.last_modified || note.created);
                timeSpan.textContent = d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
                div.appendChild(timeSpan);

                var delBtn = document.createElement('button');
                delBtn.className = 'del';
                delBtn.textContent = '×';
                delBtn.title = 'Delete this note';
                delBtn.onclick = function(e) { e.stopPropagation(); deleteNote(id); };
                div.appendChild(delBtn);

                div.addEventListener('dragstart', handleDragStart);
                div.addEventListener('dragend', handleDragEnd);
                div.addEventListener('dragover', handleDragOver);
                div.addEventListener('drop', handleDrop);
                div.addEventListener('click', function() { selectNote(id); });

                listEl.appendChild(div);
            });
        });
    }

    function renderMain(notes) {
        const listEl = document.getElementById('notesList');
        listEl.innerHTML = '';
        const ids = Object.keys(notes);
        if (!ids.length) {
            listEl.innerHTML = '<div class="note-item" style="text-align:center;color:#8b949e;">📝 No notes yet. Create one above!</div>';
            return;
        }
        var sortedIds = ids.sort((a, b) => {
            var orderA = notes[a].order || 0;
            var orderB = notes[b].order || 0;
            if (orderA !== orderB) return orderA - orderB;
            return new Date(notes[b].created) - new Date(notes[a].created);
        });
        sortedIds.forEach(id => {
            const note = notes[id];
            const div = document.createElement('div');
            div.className = 'note-item';
            if (note.color && note.color !== 'default') {
                div.style.borderLeft = '4px solid ' + getColorHex(note.color);
            }
            div.innerHTML = `
                <div class="note-title">${escapeHtml(note.title)} ${note.pinned ? '📌' : ''}</div>
                <div class="note-meta">
                    <span>${new Date(note.last_modified || note.created).toLocaleString()}</span>
                    ${note.tags && note.tags.length ? note.tags.map(t => `<span class="tag">#${escapeHtml(t)}</span>`).join('') : ''}
                </div>
                <div class="note-content">${escapeHtml(note.content)}</div>
                <div class="note-actions">
                    <button class="edit-note" data-id="${id}">✏️ Edit</button>
                    <button class="delete-note" data-id="${id}">🗑️ Delete</button>
                </div>
            `;
            listEl.appendChild(div);
        });
        document.querySelectorAll('.edit-note').forEach(btn => {
            btn.addEventListener('click', function() { editNoteFromMain(this.dataset.id); });
        });
        document.querySelectorAll('.delete-note').forEach(btn => {
            btn.addEventListener('click', function() {
                const id = this.dataset.id;
                if (confirm('Delete this note?')) deleteNote(id);
            });
        });
    }

    function getColorHex(color) {
        const map = {
            'default': '#8b949e',
            'yellow': '#fdec8f',
            'blue': '#9fd3ff',
            'green': '#a9e6a1',
            'pink': '#ffb3cd',
            'orange': '#ffc27a'
        };
        return map[color] || '#8b949e';
    }

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // ─── Tag filter ──────────────────────────────────────
    function renderTagFilter(notes) {
        const container = document.getElementById('tagFilterContainer');
        var tagSet = new Set();
        Object.values(notes).forEach(n => {
            if (n.tags) n.tags.forEach(t => tagSet.add(t));
        });
        var tags = Array.from(tagSet).sort();
        var html = '';
        if (tags.length) {
            html += `<span class="tag-pill ${activeTagFilter === '' ? 'active' : ''}" data-tag="" onclick="setTagFilter('')">All</span>`;
            tags.forEach(t => {
                const active = t === activeTagFilter ? 'active' : '';
                html += `<span class="tag-pill ${active}" data-tag="${escapeHtml(t)}" onclick="setTagFilter('${escapeHtml(t)}')">#${escapeHtml(t)}</span>`;
            });
            if (activeTagFilter) {
                html += `<span class="tag-pill clear-tag" onclick="setTagFilter('')">✕ Clear</span>`;
            }
        } else {
            html = '<span style="font-size:12px;color:#8b949e;padding:2px 6px;">No tags yet</span>';
        }
        container.innerHTML = html;
    }

    function setTagFilter(tag) {
        activeTagFilter = tag;
        loadNotes();
    }

    // ─── Search mode toggle ────────────────────────────
    function setSearchMode(mode) {
        searchMode = mode;
        document.getElementById('searchModeKeyword').classList.toggle('active', mode === 'keyword');
        document.getElementById('searchModeSemantic').classList.toggle('active', mode === 'semantic');
        loadNotes();
    }

    // ─── Search ──────────────────────────────────────────
    function searchNotes() {
        if (searchTimeout) clearTimeout(searchTimeout);
        searchTimeout = setTimeout(loadNotes, 300);
    }

    // ─── CRUD ────────────────────────────────────────────
    function createNewNote() {
        document.getElementById('noteEditor').style.display = 'block';
        document.getElementById('noteTitleInput').value = '';
        document.getElementById('noteContentInput').value = '';
        document.getElementById('noteTagsInput').value = '';
        document.getElementById('notePreview').innerHTML = '';
        document.getElementById('notePreview').classList.remove('visible');
        document.querySelector('#toolbar [data-cmd="preview"]').classList.remove('active');
        document.getElementById('aiResult').style.display = 'none';
        editingNoteId = null;
        currentColor = 'default';
        document.querySelectorAll('#colorPicker .color-option').forEach(c => c.classList.remove('active'));
        document.querySelector('#colorPicker .color-default').classList.add('active');
        currentPinned = false;
        document.getElementById('pinToggle').classList.remove('active');
        document.getElementById('pinToggle').textContent = '📌 Pin';
        document.getElementById('saveNoteBtn').textContent = '💾 Save Note';
        document.getElementById('cancelNoteBtn').style.display = 'inline-block';
        document.getElementById('noteEditor').scrollIntoView({ behavior: 'smooth' });
        document.getElementById('noteTitleInput').focus();
    }

    function editNoteFromSidebar(id) {
        const note = notesData[id];
        if (!note) return;
        document.getElementById('noteEditor').style.display = 'block';
        document.getElementById('noteTitleInput').value = note.title || '';
        document.getElementById('noteContentInput').value = note.content || '';
        document.getElementById('noteTagsInput').value = (note.tags || []).join(', ');
        document.getElementById('aiResult').style.display = 'none';
        editingNoteId = id;
        currentColor = note.color || 'default';
        document.querySelectorAll('#colorPicker .color-option').forEach(c => {
            c.classList.toggle('active', c.dataset.color === currentColor);
        });
        currentPinned = note.pinned || false;
        document.getElementById('pinToggle').classList.toggle('active', currentPinned);
        document.getElementById('pinToggle').textContent = currentPinned ? '📌 Pinned' : '📌 Pin';
        document.getElementById('saveNoteBtn').textContent = '✏️ Update Note';
        document.getElementById('cancelNoteBtn').style.display = 'inline-block';
        updatePreview();
        document.getElementById('noteEditor').scrollIntoView({ behavior: 'smooth' });
        document.getElementById('noteTitleInput').focus();
    }

    function editNoteFromMain(id) {
        editNoteFromSidebar(id);
    }

    function selectNote(id) {
        editNoteFromSidebar(id);
        document.querySelectorAll('.note-item-sidebar').forEach(el => el.classList.toggle('active', el.dataset.id === id));
    }

    function deleteNote(id) {
        if (!confirm('Delete this note?')) return;
        fetch('/notes/api/' + id, { method: 'DELETE' })
            .then(r => r.json())
            .then(data => {
                if (data.ok) {
                    if (editingNoteId === id) {
                        document.getElementById('noteEditor').style.display = 'none';
                        editingNoteId = null;
                    }
                    loadNotes();
                }
            });
    }

    function clearAllNotes() {
        if (!confirm('Delete ALL notes?')) return;
        fetch('/notes/api/clear_all', { method: 'POST' })
            .then(r => r.json())
            .then(data => {
                if (data.ok) {
                    document.getElementById('noteEditor').style.display = 'none';
                    editingNoteId = null;
                    loadNotes();
                }
            });
    }

    // ─── Save note ───────────────────────────────────────
    document.getElementById('saveNoteBtn').addEventListener('click', function() {
        const title = document.getElementById('noteTitleInput').value.trim() || 'Untitled';
        const content = document.getElementById('noteContentInput').value.trim();
        if (!content && !title) { alert('Please add some content or a title.'); return; }
        const tags = document.getElementById('noteTagsInput').value.split(',').map(s => s.trim()).filter(Boolean);
        const payload = {
            title,
            content,
            tags,
            color: currentColor,
            pinned: currentPinned
        };
        const method = editingNoteId ? 'PUT' : 'POST';
        const url = editingNoteId ? '/notes/api/' + editingNoteId : '/notes/api';
        fetch(url, {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        })
        .then(r => r.json())
        .then(data => {
            if (data.ok || data.id) {
                document.getElementById('noteEditor').style.display = 'none';
                editingNoteId = null;
                loadNotes();
            }
        });
    });

    document.getElementById('cancelNoteBtn').addEventListener('click', function() {
        document.getElementById('noteEditor').style.display = 'none';
        editingNoteId = null;
        loadNotes();
    });

    // ─── Drag & drop reorder ────────────────────────────
    var dragSrcId = null;
    function handleDragStart(e) {
        dragSrcId = this.dataset.id;
        this.classList.add('dragging');
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', this.dataset.id);
    }
    function handleDragEnd(e) {
        this.classList.remove('dragging');
        document.querySelectorAll('.note-item-sidebar').forEach(el => el.classList.remove('drag-over'));
    }
    function handleDragOver(e) {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        document.querySelectorAll('.note-item-sidebar').forEach(el => el.classList.remove('drag-over'));
        this.classList.add('drag-over');
    }
    function handleDrop(e) {
        e.preventDefault();
        this.classList.remove('drag-over');
        var targetId = this.dataset.id;
        if (dragSrcId === targetId) return;
        var ids = Object.keys(notesData);
        var srcIndex = ids.indexOf(dragSrcId);
        var targetIndex = ids.indexOf(targetId);
        if (srcIndex === -1 || targetIndex === -1) return;
        var newOrder = {};
        ids.forEach((id, idx) => { newOrder[id] = idx; });
        newOrder[dragSrcId] = targetIndex;
        newOrder[targetId] = srcIndex;
        fetch('/notes/api/reorder', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({order: newOrder})
        })
        .then(r => r.json())
        .then(data => { if (data.ok) loadNotes(); else console.error('Reorder failed'); })
        .catch(err => console.error('Error reordering:', err));
    }

    // ─── Init ────────────────────────────────────────────
    window.addEventListener('load', function() {
        loadAIPreferences();
        loadNotes();
        document.getElementById('noteTitleInput').focus();
    });
</script>
</body>
</html>
"""

# (End of NOTES_HTML)