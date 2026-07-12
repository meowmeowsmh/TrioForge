# cork_board.py – advanced with Markdown, tags, search, resizable pins, modal editor,
# Red Thread links, and AI assistance with dynamic model selection.
# STORAGE: SQLite (sqlite_data/notes.db) — viewable offline with any SQLite browser
# (DB Browser for SQLite, "SQLite Viewer" VSCode extension, etc.) while the app is closed.
# OPTIMIZED: WAL mode, indexed lookups, targeted single-row writes (no full-board rewrites),
# lazy embedding, throttled drag updates, AI action history log.
# + IMAGE UPLOAD support – drag/drop images to create picture pins.

import os
import json as std_json          # used for tags/embedding JSON columns + misc parsing
import sqlite3
import threading
import uuid
import random
from datetime import datetime
from flask import Blueprint, request, jsonify, render_template_string

# ---------- Try orjson for faster JSON (tags/embedding columns) ----------
try:
    import orjson
    def json_dumps(obj):
        return orjson.dumps(obj).decode('utf-8')
    def json_loads(s):
        return orjson.loads(s)
    print("🚀 corkboard: using orjson")
except ImportError:
    json_dumps = std_json.dumps
    json_loads = std_json.loads
    print("ℹ️ corkboard: using standard json (install orjson for faster I/O)")

# ---------- Embedding (semantic search) ----------
try:
    from sentence_transformers import SentenceTransformer
    import numpy as np
    from sklearn.metrics.pairwise import cosine_similarity
    EMBED_AVAILABLE = True
except ImportError:
    EMBED_AVAILABLE = False
    print("⚠️ sentence-transformers or scikit-learn not installed. Run: pip install sentence-transformers scikit-learn")

# ---------- LLM for AI assistance ----------
from llm_providers import (
    LLMProvider,
    OllamaProvider,
    LlamaCppProvider,
    HuggingFaceProvider,
    GroqProvider,
    DeepSeekProvider,
    ClaudeProvider,
)

# ======================================================================
# SQLite storage layer
# ======================================================================
DB_DIR = "sqlite_data"
DB_PATH = os.path.join(DB_DIR, "notes.db")
os.makedirs(DB_DIR, exist_ok=True)

_local = threading.local()
_write_lock = threading.Lock()  # SQLite allows 1 writer at a time; serialize writes only


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
        CREATE TABLE IF NOT EXISTS pins (
            id            TEXT PRIMARY KEY,
            title         TEXT NOT NULL DEFAULT '',
            content       TEXT NOT NULL DEFAULT '',
            x             REAL NOT NULL DEFAULT 0,
            y             REAL NOT NULL DEFAULT 0,
            width         INTEGER NOT NULL DEFAULT 220,
            height        INTEGER NOT NULL DEFAULT 160,
            color         TEXT NOT NULL DEFAULT 'yellow',
            rotation      INTEGER NOT NULL DEFAULT 0,
            created       TEXT NOT NULL,
            last_modified TEXT NOT NULL,
            tags          TEXT NOT NULL DEFAULT '[]',   -- JSON array
            type          TEXT NOT NULL DEFAULT 'note',
            filename      TEXT,
            image_url     TEXT,
            embedding     TEXT                          -- JSON array of floats, nullable
        );

        CREATE TABLE IF NOT EXISTS links (
            from_id TEXT NOT NULL,
            to_id   TEXT NOT NULL,
            color   TEXT NOT NULL DEFAULT 'black',       -- 'red' = Red Thread
            PRIMARY KEY (from_id, to_id)
        );

        -- Every AI action (summarise / suggest_tags / improve / suggest_links)
        -- is logged here so past AI output survives even if a pin is later edited.
        CREATE TABLE IF NOT EXISTS ai_history (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            pin_id   TEXT NOT NULL,
            action   TEXT NOT NULL,
            provider TEXT,
            model    TEXT,
            result   TEXT,
            created  TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_links_from ON links(from_id);
        CREATE INDEX IF NOT EXISTS idx_links_to   ON links(to_id);
        CREATE INDEX IF NOT EXISTS idx_ai_history_pin ON ai_history(pin_id);
        CREATE INDEX IF NOT EXISTS idx_pins_modified ON pins(last_modified);
    """)
    conn.commit()


init_db()

# ---------- Row <-> dict helpers ----------
def _pin_row_to_dict(row):
    d = dict(row)
    d["tags"] = json_loads(d["tags"]) if d.get("tags") else []
    if d.get("embedding"):
        d["embedding"] = json_loads(d["embedding"])
    else:
        d.pop("embedding", None)
    return d


def _pin_to_row_values(pin_id, data, now, existing=None):
    """Build the full column tuple for an insert/replace, falling back to
    existing values (or sane defaults) for anything not supplied in `data`."""
    e = existing or {}
    def g(key, default):
        return data.get(key, e.get(key, default))
    return {
        "id": pin_id,
        "title": g("title", "Untitled"),
        "content": g("content", ""),
        "x": g("x", random.randint(40, 480)),
        "y": g("y", random.randint(40, 280)),
        "width": g("width", 220),
        "height": g("height", 160),
        "color": g("color", "yellow"),
        "rotation": g("rotation", random.choice([-3, -2, -1, 0, 1, 2, 3])),
        "created": e.get("created", now),
        "last_modified": now,
        "tags": json_dumps(g("tags", [])),
        "type": g("type", "note"),
        "filename": g("filename", None),
        "image_url": g("image_url", None),
        "embedding": json_dumps(e["embedding"]) if e.get("embedding") else None,
    }


def load_board():
    """Build the {"pins": {...}, "links": [...]} shape the rest of the app
    (search / semantic search / ai_assist) works with, read straight from SQLite."""
    conn = get_conn()
    pins = {}
    for row in conn.execute("SELECT * FROM pins"):
        pin = _pin_row_to_dict(row)
        pins[pin["id"]] = pin
    links = [dict(row) for row in conn.execute("SELECT from_id AS 'from', to_id AS 'to', color FROM links")]
    return {"pins": pins, "links": links}


def get_pin(pin_id):
    row = get_conn().execute("SELECT * FROM pins WHERE id = ?", (pin_id,)).fetchone()
    return _pin_row_to_dict(row) if row else None


def upsert_pin(pin_id, data, now=None, existing=None):
    now = now or datetime.now().isoformat()
    vals = _pin_to_row_values(pin_id, data, now, existing)
    with _write_lock:
        conn = get_conn()
        conn.execute("""
            INSERT INTO pins (id, title, content, x, y, width, height, color, rotation,
                               created, last_modified, tags, type, filename, image_url, embedding)
            VALUES (:id, :title, :content, :x, :y, :width, :height, :color, :rotation,
                    :created, :last_modified, :tags, :type, :filename, :image_url, :embedding)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title, content=excluded.content, x=excluded.x, y=excluded.y,
                width=excluded.width, height=excluded.height, color=excluded.color,
                rotation=excluded.rotation, last_modified=excluded.last_modified,
                tags=excluded.tags, type=excluded.type, filename=excluded.filename,
                image_url=excluded.image_url, embedding=excluded.embedding
        """, vals)
        conn.commit()
    return vals


def update_pin_fields(pin_id, fields):
    """Partial update of just the given columns (used by PUT /api/pins/<id>)."""
    if not fields:
        return
    fields = dict(fields)
    if "tags" in fields:
        fields["tags"] = json_dumps(fields["tags"])
    fields["last_modified"] = datetime.now().isoformat()
    set_clause = ", ".join(f"{k} = :{k}" for k in fields)
    fields["id"] = pin_id
    with _write_lock:
        conn = get_conn()
        conn.execute(f"UPDATE pins SET {set_clause}, embedding = NULL WHERE id = :id", fields)
        conn.commit()


def set_pin_embedding(pin_id, embedding):
    with _write_lock:
        conn = get_conn()
        conn.execute("UPDATE pins SET embedding = ? WHERE id = ?",
                      (json_dumps(embedding) if embedding is not None else None, pin_id))
        conn.commit()


def delete_pin_row(pin_id):
    with _write_lock:
        conn = get_conn()
        conn.execute("DELETE FROM pins WHERE id = ?", (pin_id,))
        conn.execute("DELETE FROM links WHERE from_id = ? OR to_id = ?", (pin_id, pin_id))
        conn.commit()


def clear_all_rows():
    with _write_lock:
        conn = get_conn()
        conn.execute("DELETE FROM pins")
        conn.execute("DELETE FROM links")
        conn.commit()
        # ai_history is kept intentionally as a durable log of past AI activity


def find_link(a, b):
    conn = get_conn()
    row = conn.execute("""
        SELECT from_id AS 'from', to_id AS 'to', color FROM links
        WHERE (from_id = ? AND to_id = ?) OR (from_id = ? AND to_id = ?)
    """, (a, b, b, a)).fetchone()
    return dict(row) if row else None


def add_link(a, b, color):
    with _write_lock:
        conn = get_conn()
        conn.execute("INSERT OR REPLACE INTO links (from_id, to_id, color) VALUES (?, ?, ?)", (a, b, color))
        conn.commit()


def remove_link(a, b):
    with _write_lock:
        conn = get_conn()
        conn.execute("DELETE FROM links WHERE (from_id=? AND to_id=?) OR (from_id=? AND to_id=?)", (a, b, b, a))
        conn.commit()


def update_link_color(a, b, color):
    with _write_lock:
        conn = get_conn()
        cur = conn.execute("""
            UPDATE links SET color = ?
            WHERE (from_id=? AND to_id=?) OR (from_id=? AND to_id=?)
        """, (color, a, b, b, a))
        conn.commit()
        return cur.rowcount > 0


def log_ai_history(pin_id, action, provider, model, result):
    """Persist every AI action (summarise, tag suggestions, rewrite, link suggestions)
    to the database so it's kept even if the pin content later changes."""
    with _write_lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO ai_history (pin_id, action, provider, model, result, created) VALUES (?, ?, ?, ?, ?, ?)",
            (pin_id, action, provider, model, result, datetime.now().isoformat())
        )
        conn.commit()


# ---------- Embedding model (lazy loaded, thread‑safe) ----------
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


def embed_pin(pin):
    """Generate embedding for a pin (title + content). Returns list of floats or None."""
    if not EMBED_AVAILABLE:
        return None
    model = get_embedder()
    text = (pin.get('title', '') + ' ' + pin.get('content', '')).strip()[:1000]
    if not text:
        return None
    return model.encode(text).tolist()


def compute_all_embeddings():
    """Optional background precompute of embeddings for any pin missing one."""
    board = load_board()
    for pid, pin in board['pins'].items():
        if pin.get('embedding') is None:
            emb = embed_pin(pin)
            if emb is not None:
                set_pin_embedding(pid, emb)
    print("✅ Precomputed embeddings for all pins.")

# (Uncomment to enable background precomputation)
# threading.Thread(target=compute_all_embeddings, daemon=True).start()

corkboard_bp = Blueprint('corkboard', __name__, url_prefix='/corkboard')

# ---------- Serve the corkboard HTML page ----------
@corkboard_bp.route('')
def corkboard_page():
    return render_template_string(CORKBOARD_HTML)

# ---------- API: full board ----------
@corkboard_bp.route('/api', methods=['GET'])
def get_board():
    return jsonify(load_board())

# ---------- API: keyword search ----------
@corkboard_bp.route('/api/search', methods=['GET'])
def search_pins():
    query = request.args.get('q', '').strip().lower()
    tag = request.args.get('tag', '').strip().lower()
    board = load_board()
    results = {}
    for pid, pin in board['pins'].items():
        if tag:
            pin_tags = [t.lower() for t in pin.get('tags', [])]
            if tag not in pin_tags:
                continue
        if query:
            title_match = query in pin.get('title', '').lower()
            content_match = query in pin.get('content', '').lower()
            tag_match = any(query in t.lower() for t in pin.get('tags', []))
            if not (title_match or content_match or tag_match):
                continue
        results[pid] = pin
    links = [l for l in board['links'] if l['from'] in results and l['to'] in results]
    return jsonify({"pins": results, "links": links})

# ---------- API: semantic search (lazy embedding) ----------
@corkboard_bp.route('/api/semantic_search', methods=['GET'])
def semantic_search_pins():
    if not EMBED_AVAILABLE:
        return jsonify({"error": "Embedding model not available"}), 503
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify([])
    board = load_board()
    if not board['pins']:
        return jsonify([])

    model = get_embedder()
    q_emb = model.encode(query).reshape(1, -1)

    candidates = []
    for pid, pin in board['pins'].items():
        emb = pin.get('embedding')
        if emb is None:
            emb = embed_pin(pin)
            if emb is not None:
                set_pin_embedding(pid, emb)
        if emb:
            candidates.append((pid, pin, emb))

    if not candidates:
        return jsonify([])

    emb_matrix = np.array([c[2] for c in candidates])
    similarities = cosine_similarity(q_emb, emb_matrix).flatten()
    sorted_idx = np.argsort(similarities)[::-1]

    results = []
    for idx in sorted_idx[:10]:
        if similarities[idx] > 0.2:
            pid, pin, _ = candidates[idx]
            results.append({
                "id": pid,
                "title": pin["title"],
                "content": pin["content"][:200] + "..." if len(pin["content"]) > 200 else pin["content"],
                "score": float(similarities[idx])
            })

    return jsonify(results)

# ---------- Provider cache for AI assist ----------
_provider_cache = {}

def get_provider(provider_name, api_key=None):
    key = (provider_name, api_key)
    if key in _provider_cache:
        return _provider_cache[key]
    provider_map = {
        "ollama": OllamaProvider,
        "llamacpp": LlamaCppProvider,
        "huggingface": HuggingFaceProvider,
        "groq": GroqProvider,
        "deepseek": DeepSeekProvider,
        "claude": ClaudeProvider,
    }
    cls = provider_map.get(provider_name)
    if not cls:
        raise ValueError(f"Unknown provider: {provider_name}")
    inst = cls()
    _provider_cache[key] = inst
    return inst

# ---------- API: AI assistance ----------
@corkboard_bp.route('/api/ai_assist', methods=['POST'])
def ai_assist():
    data = request.get_json()
    pin_id = data.get('pin_id')
    action = data.get('action')
    provider_name = data.get('provider', 'ollama')
    model = data.get('model', 'llama3.2')
    api_key = data.get('api_key', None)

    if not pin_id or not action:
        return jsonify({"error": "Missing pin_id or action"}), 400

    pin = get_pin(pin_id)
    if not pin:
        return jsonify({"error": "Pin not found"}), 404

    content = pin.get('content', '').strip()
    title = pin.get('title', '').strip()
    if not content and not title:
        return jsonify({"error": "Pin is empty"}), 400

    if action == 'suggest_links':
        if not EMBED_AVAILABLE:
            return jsonify({"error": "Embedding model not available"}), 503
        board = load_board()
        pin_emb = pin.get('embedding')
        if pin_emb is None:
            pin_emb = embed_pin(pin)
            if pin_emb is not None:
                set_pin_embedding(pin_id, pin_emb)
        if pin_emb is None:
            return jsonify({"error": "Could not generate embedding for this pin"}), 500
        pin_emb = np.array(pin_emb).reshape(1, -1)

        candidates = []
        for pid, other in board['pins'].items():
            if pid == pin_id:
                continue
            emb = other.get('embedding')
            if emb is None:
                emb = embed_pin(other)
                if emb is not None:
                    set_pin_embedding(pid, emb)
            if emb:
                candidates.append((pid, other, emb))

        if not candidates:
            return jsonify({"suggestions": []})

        emb_matrix = np.array([c[2] for c in candidates])
        similarities = cosine_similarity(pin_emb, emb_matrix).flatten()
        sorted_idx = np.argsort(similarities)[::-1]
        suggestions = []
        for idx in sorted_idx[:10]:
            if similarities[idx] > 0.3:
                pid, other, _ = candidates[idx]
                suggestions.append({
                    "id": pid,
                    "title": other["title"],
                    "score": float(similarities[idx])
                })
        log_ai_history(pin_id, action, provider_name, model, json_dumps(suggestions))
        return jsonify({"suggestions": suggestions})

    # LLM actions
    system_prompt = (
        "You are a helpful assistant. Follow the user's instruction exactly. "
        "Do not add extra commentary. Output only the requested information in the specified format."
    )
    if action == 'summarise':
        user_prompt = f"""Summarise the following pin in one short paragraph (max 50 words). 
Do not include any additional text, only the summary.

Title: {title}
Content: {content}

Summary:"""
    elif action == 'suggest_tags':
        user_prompt = f"""Suggest up to 5 short tags (comma separated) for this pin. 
Output only the tags, separated by commas. Do not include any other text.

Title: {title}
Content: {content}

Tags:"""
    elif action == 'improve':
        user_prompt = f"""Rewrite the following pin to improve clarity, grammar, and flow. 
Keep the same meaning but make it more concise and professional. 
Output only the improved version.

{content}

Improved version:"""
    else:
        return jsonify({"error": "Invalid action"}), 400

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]

    try:
        provider = get_provider(provider_name, api_key)
        response = provider.generate(messages, model=model, api_key=api_key)
        result = response.strip()
        print(f"AI response for {action}: {result[:200]}...")
    except Exception as e:
        print(f"AI error: {e}")
        return jsonify({"error": f"AI service error: {str(e)}"}), 503

    if not result:
        return jsonify({"error": "AI returned an empty response. Try a different model or provider."}), 500

    # Persist every AI action to the ai_history table (summaries, tag suggestions, rewrites)
    log_ai_history(pin_id, action, provider_name, model, result)

    if action == 'suggest_tags':
        tags = [t.strip() for t in result.split(',') if t.strip()]
        if tags:
            existing = set(pin.get('tags', []))
            new_tags = [t for t in tags if t not in existing]
            if new_tags:
                update_pin_fields(pin_id, {"tags": list(existing.union(new_tags))})
                return jsonify({"result": result, "tags": new_tags})
        return jsonify({"result": result, "tags": []})

    return jsonify({"result": result})

# ---------- API: AI history for a pin (view past summaries / suggestions) ----------
@corkboard_bp.route('/api/ai_history/<pin_id>', methods=['GET'])
def get_ai_history(pin_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, pin_id, action, provider, model, result, created FROM ai_history "
        "WHERE pin_id = ? ORDER BY created DESC", (pin_id,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])

# ---------- API: pins ----------
@corkboard_bp.route('/api/pins', methods=['POST'])
def create_pin():
    data = request.get_json() or {}
    pin_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    vals = upsert_pin(pin_id, data, now)
    pin = get_pin(pin_id)
    return jsonify({"id": pin_id, "ok": True, "pin": pin})

@corkboard_bp.route('/api/pins/<pin_id>', methods=['PUT'])
def update_pin(pin_id):
    data = request.get_json() or {}
    existing = get_pin(pin_id)
    if not existing:
        return jsonify({"error": "Pin not found"}), 404
    fields = {}
    for field in ("title", "content", "x", "y", "width", "height", "color", "rotation", "tags", "image_url"):
        if field in data:
            fields[field] = data[field]
    update_pin_fields(pin_id, fields)
    return jsonify({"ok": True})

@corkboard_bp.route('/api/pins/<pin_id>', methods=['DELETE'])
def delete_pin(pin_id):
    existing = get_pin(pin_id)
    if not existing:
        return jsonify({"error": "Pin not found"}), 404
    delete_pin_row(pin_id)
    return jsonify({"ok": True})

# ---------- API: links (Red Thread = color: 'red') ----------
@corkboard_bp.route('/api/links', methods=['POST'])
def toggle_link():
    data = request.get_json() or {}
    a, b = data.get('from'), data.get('to')
    new_color = data.get('color', 'black')
    if not a or not b or a == b:
        return jsonify({"error": "Invalid link"}), 400
    if not get_pin(a) or not get_pin(b):
        return jsonify({"error": "Pin not found"}), 404
    existing = find_link(a, b)
    if existing:
        remove_link(a, b)
        linked = False
    else:
        add_link(a, b, new_color)
        linked = True
    return jsonify({"ok": True, "linked": linked, "color": new_color})

@corkboard_bp.route('/api/links/color', methods=['PUT'])
def change_link_color():
    data = request.get_json() or {}
    a, b, new_color = data.get('from'), data.get('to'), data.get('color', 'red')
    if not a or not b:
        return jsonify({"error": "Missing from/to"}), 400
    if update_link_color(a, b, new_color):
        return jsonify({"ok": True, "color": new_color})
    return jsonify({"error": "Link not found"}), 404

# ---------- API: clear all ----------
@corkboard_bp.route('/api/clear_all', methods=['POST'])
def clear_all():
    clear_all_rows()
    return jsonify({"ok": True})

# ---------- API: file upload (supports images) ----------
ALLOWED_EXT = {"txt", "md", "ipynb", "pdf", "png", "jpg", "jpeg", "gif", "svg", "webp"}

@corkboard_bp.route('/api/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files['file']
    filename = f.filename or "file"
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''

    if ext not in ALLOWED_EXT:
        return jsonify({"error": f"Unsupported file type: .{ext}. Allowed: txt, md, ipynb, pdf, png, jpg, jpeg, gif, svg, webp."}), 400

    # Handle images
    if ext in {'png', 'jpg', 'jpeg', 'gif', 'svg', 'webp'}:
        upload_dir = os.path.join('static', 'uploads', 'corkboard')
        os.makedirs(upload_dir, exist_ok=True)
        unique = str(uuid.uuid4()) + '.' + ext
        path = os.path.join(upload_dir, unique)
        f.save(path)
        image_url = f'/static/uploads/corkboard/{unique}'
        pin_id = str(uuid.uuid4())
        now = datetime.now().isoformat()
        data = {
            "title": filename,
            "content": f"![{filename}]({image_url})",
            "width": 280,
            "height": 200,
            "color": "yellow",
            "tags": ["image", ext],
            "type": "image",
            "filename": filename,
            "image_url": image_url,
        }
        upsert_pin(pin_id, data, now)
        return jsonify({"ok": True, "id": pin_id, "pin": get_pin(pin_id)})

    # Text files (unchanged)
    content = ""
    try:
        if ext in ('txt', 'md'):
            content = f.read().decode('utf-8', errors='replace')
        elif ext == 'ipynb':
            nb = json_loads(f.read())
            parts = []
            for cell in nb.get('cells', []):
                src = cell.get('source', [])
                src_text = ''.join(src) if isinstance(src, list) else str(src)
                if cell.get('cell_type') == 'markdown':
                    parts.append(src_text)
                else:
                    parts.append("```\n" + src_text + "\n```")
            content = "\n\n".join(parts)
        elif ext == 'pdf':
            reader_cls = None
            try:
                from pypdf import PdfReader as reader_cls
            except ImportError:
                try:
                    from PyPDF2 import PdfReader as reader_cls
                except ImportError:
                    reader_cls = None
            if reader_cls is None:
                return jsonify({"error": "PDF support needs a library. Run: pip install pypdf"}), 400
            reader = reader_cls(f)
            pages = [(page.extract_text() or "") for page in reader.pages]
            content = "\n\n".join(pages)
    except Exception as e:
        return jsonify({"error": f"Failed to parse file: {e}"}), 400

    pin_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    color = {"pdf": "pink", "ipynb": "green", "md": "blue", "txt": "yellow"}.get(ext, "yellow")
    data = {
        "title": filename,
        "content": content[:30000],
        "width": 280,
        "height": 200,
        "color": color,
        "tags": [ext],
        "type": "file",
        "filename": filename,
    }
    upsert_pin(pin_id, data, now)
    return jsonify({"ok": True, "id": pin_id, "pin": get_pin(pin_id)})

# ---------- HTML template (with image drag support) ----------
CORKBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E📌%3C/text%3E%3C/svg%3E">
<title>Trio-Forge Cork Board · AI‑Powered</title>
<script src="/static/vendor/marked.min.js"></script>
<style>
/* ── Base (same as original) ─────────────────────────── */
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
.app { display:flex; flex-direction:column; height:100%; backdrop-filter: blur(2px); }

/* ── Top bar ────────────────────────────────────────── */
.top-bar {
    display:grid; grid-template-columns: 1fr auto 1fr; align-items:center;
    background: rgba(22,27,34,0.7); backdrop-filter: blur(20px);
    border-bottom: 1px solid rgba(255,255,255,0.05);
    padding: 12px 24px; gap:12px; flex-shrink:0;
    box-shadow: 0 4px 12px rgba(0,0,0,0.3);
    transition: background 0.3s, border-color 0.3s;
}
.top-bar .left { display:flex; align-items:center; gap:12px; justify-self:start; }
.top-bar .left h1 {
    font-size:19px; background: linear-gradient(135deg,#58a6ff,#a371f7);
    -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text; font-weight:700;
}
.center-tabs {
    display:flex; gap:4px; background: rgba(255,255,255,0.06); padding:4px;
    border-radius:30px; backdrop-filter: blur(5px); border:1px solid rgba(255,255,255,0.06); justify-self:center;
}
.center-tabs .tab-btn {
    background:transparent; border:none; padding:6px 18px; border-radius:20px; font-size:14px;
    font-weight:500; color:#8b949e; cursor:pointer; transition: all .2s;
    text-decoration:none; display:inline-block;
}
.center-tabs .tab-btn:hover { color:#c9d1d9; background: rgba(255,255,255,0.05); }
.center-tabs .tab-btn.active { background:#1f6feb; color:#fff; box-shadow: 0 2px 8px rgba(31,111,235,0.3); }
.top-bar .right {
    display:flex; align-items:center; gap:10px; flex-wrap:wrap; justify-self:end;
}
.clear-btn {
    background: rgba(33,38,45,0.7); border:1px solid rgba(248,81,73,0.3); color:#f85149;
    border-radius:10px; padding:6px 14px; font-size:12px; cursor:pointer; transition: all .2s; backdrop-filter: blur(5px);
}
.clear-btn:hover { background: rgba(248,81,73,0.15); border-color:#f85149; }

/* ── Theme toggle ───────────────────────────────────── */
.theme-toggle-wrapper { display: inline-block; vertical-align: middle; }
.toggle-outer {
    position:relative; width:140px; height:56px; border-radius:999px;
    background: hsl(220 18% 82%);
    box-shadow: 2px 2px 8px rgba(0,0,0,0.12), -2px -2px 6px rgba(255,255,255,0.5),
                inset 1px 1px 3px rgba(0,0,0,0.08), inset -1px -1px 3px rgba(255,255,255,0.4);
    cursor:pointer; user-select:none; flex-shrink:0;
}
.toggle-inner { position:absolute; inset:5px; border-radius:999px; overflow:hidden; }
.night-bg { position:absolute; inset:0; background: hsl(220 35% 18%); opacity:1; transition: opacity .3s ease; }
.stars-layer { position:absolute; inset:0; opacity:1; transition: opacity .3s ease; pointer-events:none; }
.star { position:absolute; background:white; border-radius:50%; }
.sparkle { position:absolute; color:white; font-size:7px; line-height:1; }
.day-bg { position:absolute; inset:0; opacity:0; transition: opacity .3s ease; pointer-events:none; }
.sky-layer { position:absolute; inset:0; background: hsl(205 70% 62%); }
.sky-mid { position:absolute; bottom:0; left:0; right:0; height:50%; background: hsl(205 60% 72%); border-radius:40% 40% 0 0/30% 30% 0 0; }
.cloud { position:absolute; background: rgba(255,255,255,0.88); border-radius:999px; }
.astronaut, .biplane { position:absolute; z-index:4; pointer-events:none; transition: opacity .3s ease; }
.astronaut { left:48px; top:50%; transform:translateY(-55%); width:22px; height:26px; opacity:1; animation:float 3s ease-in-out infinite; }
.biplane { left:44px; top:38%; transform:translateY(-50%); width:30px; height:18px; opacity:0; animation:fly 3s ease-in-out infinite; }
@keyframes float { 0%,100%{transform:translateY(-55%)} 50%{transform:translateY(-65%)} }
@keyframes fly { 0%,100%{transform:translateY(-50%) rotate(-1deg)} 50%{transform:translateY(-60%) rotate(1deg)} }
.knob {
    position:absolute; top:50%; width:40px; height:40px; border-radius:50%; transform:translateY(-50%);
    z-index:10; cursor:grab; transition: left .4s cubic-bezier(.34,1.2,.64,1); left:3px;
}
.knob:active { cursor:grabbing; }
.knob-moon {
    position:absolute; inset:0; border-radius:50%; background: hsl(220 10% 82%);
    box-shadow: 2px 2px 4px rgba(255,255,255,0.9) inset, -2px -2px 4px rgba(0,0,0,0.18) inset;
    transition: opacity .3s ease;
}
.knob-moon .crater {
    position:absolute; border-radius:50%; background: hsl(220 8% 67%);
    box-shadow: 1px 1px 2px rgba(255,255,255,0.4) inset, -1px -1px 2px rgba(0,0,0,0.2) inset;
}
.knob-sun {
    position:absolute; inset:0; border-radius:50%; background: hsl(44 100% 58%);
    box-shadow: 2px 2px 6px rgba(255,255,180,0.9) inset, -2px -2px 4px rgba(180,100,0,0.3) inset,
                0 0 12px hsl(44 100% 70% / .5);
    opacity:0; transition: opacity .3s ease;
}
.toggle-outer.day .night-bg { opacity:0; }
.toggle-outer.day .stars-layer { opacity:0; }
.toggle-outer.day .day-bg { opacity:1; }
.toggle-outer.day .knob { left:93px; }
.toggle-outer.day .knob-moon { opacity:0; }
.toggle-outer.day .knob-sun { opacity:1; }
.toggle-outer.day .astronaut { opacity:0; }
.toggle-outer.day .biplane { opacity:1; }

/* ── Toolbar ────────────────────────────────────────── */
.toolbar {
    display:flex;
    align-items:center;
    gap:10px;
    padding:8px 24px;
    background: rgba(0,0,0,0.2);
    border-bottom: 1px solid rgba(255,255,255,0.05);
    flex-wrap:wrap;
    backdrop-filter: blur(5px);
}
.toolbar .search-input {
    background: rgba(13,17,23,0.7);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 20px;
    padding: 6px 14px;
    color: #e6edf3;
    font-size: 13px;
    outline: none;
    width: 160px;
    transition: border-color 0.2s, width 0.2s;
}
.toolbar .search-input:focus { border-color:#58a6ff; width: 220px; }
.toolbar .search-input::placeholder { color:#8b949e; }
.toolbar .top-btn {
    background: rgba(33,38,45,0.7); border:1px solid rgba(255,255,255,0.1); color:#c9d1d9;
    border-radius:10px; padding:6px 14px; font-size:12px; cursor:pointer; transition: all .2s; backdrop-filter: blur(5px);
    white-space:nowrap;
}
.toolbar .top-btn:hover { background: rgba(88,166,255,0.15); border-color:#58a6ff; }
.toolbar .top-btn.linking { background:#1f6feb; border-color:#1f6feb; color:#fff; }
.toolbar .top-btn.red-thread-active {
    background:#da3633;
    border-color:#da3633;
    color:#fff;
    box-shadow: 0 0 12px rgba(218,54,51,0.5);
}
.toolbar .file-input-wrapper { position:relative; display:inline-block; }
.toolbar .file-input-wrapper input[type="file"] {
    position:absolute; left:0; top:0; opacity:0; width:100%; height:100%; cursor:pointer;
}
/* Search mode toggle inside toolbar */
.toolbar .search-mode-toggle {
    display:inline-flex;
    gap:2px;
    background:rgba(0,0,0,0.2);
    border-radius:20px;
    padding:2px;
}
.toolbar .search-mode-toggle button {
    background:transparent;
    border:none;
    border-radius:18px;
    padding:4px 12px;
    font-size:12px;
    color:#8b949e;
    cursor:pointer;
    transition:0.2s;
}
.toolbar .search-mode-toggle button.active {
    background:#1f6feb;
    color:#fff;
}
.toolbar .search-mode-toggle button:hover { background:rgba(255,255,255,0.05); }

/* ── Tag filter ────────────────────────────────────── */
.tag-filter {
    padding: 6px 24px;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    background: rgba(0,0,0,0.15);
    border-bottom: 1px solid rgba(255,255,255,0.05);
    backdrop-filter: blur(5px);
    min-height: 34px;
    align-items: center;
}
.tag-filter .tag-pill {
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 20px;
    padding: 3px 12px;
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
.tag-filter .filter-label { font-size: 11px; color: #8b949e; margin-right: 4px; }

/* ── Board ──────────────────────────────────────────── */
.board-wrap {
    flex:1;
    position:relative;
    overflow:auto;
    padding: 20px 30px;
    background: rgba(10,10,15,0.7);
    backdrop-filter: blur(10px);
    transition: background 0.3s ease;
}
body.light-mode .board-wrap {
    background: rgba(255,255,255,0.85);
}

.board {
    position:relative;
    min-width: 100%;
    min-height: 100%;
    background-color: #5a3a24;
    background-image:
        radial-gradient(circle at 20% 30%, rgba(0,0,0,0.15) 0, transparent 3px),
        radial-gradient(circle at 65% 15%, rgba(0,0,0,0.12) 0, transparent 3px),
        radial-gradient(circle at 40% 70%, rgba(0,0,0,0.15) 0, transparent 4px),
        radial-gradient(circle at 85% 55%, rgba(0,0,0,0.1) 0, transparent 3px),
        radial-gradient(circle at 10% 85%, rgba(0,0,0,0.12) 0, transparent 3px),
        radial-gradient(circle at 55% 45%, rgba(0,0,0,0.1) 0, transparent 3px),
        linear-gradient(rgba(0,0,0,0.06), rgba(0,0,0,0.06));
    background-size: 140px 140px, 160px 160px, 130px 130px, 150px 150px, 170px 170px, 120px 120px, cover;
    border-radius: 16px;
    border: 2px solid rgba(255,255,255,0.06);
    box-shadow: 0 8px 32px rgba(0,0,0,0.4);
    transition: background 0.3s, border-color 0.3s;
    will-change: transform;
    contain: layout;
}
body.light-mode .board {
    background-color: #d9b382;
    border-color: rgba(0,0,0,0.08);
    box-shadow: 0 4px 16px rgba(0,0,0,0.1);
}

svg.link-layer {
    position:absolute; inset:0; width:100%; height:100%; pointer-events:none; z-index:1;
}
.link-line {
    fill:none;
    stroke-width:1.5;
    stroke-dasharray:6 4;
    opacity:0.75;
    cursor: pointer;
}
.link-line.red {
    stroke:#e0281c;
    stroke-width:3;
    stroke-dasharray:none;
    opacity:0.95;
    filter: drop-shadow(0 1px 2px rgba(0,0,0,0.4));
}
.link-line.black {
    stroke:#e8d9b5;
    stroke-width:1.5;
    stroke-dasharray:6 4;
}
.arrow-marker { fill:#e8d9b5; }
.arrow-marker.red { fill:#c8281e; }

/* ── Pins ──────────────────────────────────────────── */
.pin {
    position:absolute;
    left:0; top:0;
    will-change: transform;
    border-radius:2px;
    cursor:grab;
    z-index:2;
    box-shadow: 3px 5px 10px rgba(0,0,0,0.45);
    font-size:13px;
    color:#2b2b2b;
    transition: box-shadow .15s ease, transform .1s;
    padding: 22px 12px 10px;
    overflow:hidden;
    display:flex;
    flex-direction:column;
}
.pin.dragging { cursor:grabbing; z-index:50; box-shadow: 6px 10px 22px rgba(0,0,0,0.6); transition: none; }
.pin.link-source { outline:3px solid #58a6ff; outline-offset:2px; }
.pin.color-yellow { background: linear-gradient(#fff8b0,#fdec8f); }
.pin.color-blue   { background: linear-gradient(#bfe3ff,#9fd3ff); }
.pin.color-green  { background: linear-gradient(#c8f0c2,#a9e6a1); }
.pin.color-pink   { background: linear-gradient(#ffd0e0,#ffb3cd); }
.pin.color-orange { background: linear-gradient(#ffd9a8,#ffc27a); }
.pin.color-default{ background: linear-gradient(#e8e8e8,#d0d0d0); }

.pin-nail {
    position:absolute; top:-8px; left:50%; transform:translateX(-50%);
    width:16px; height:16px; border-radius:50%;
    background: radial-gradient(circle at 35% 30%, #f2413a, #8c1a14);
    box-shadow: 0 3px 4px rgba(0,0,0,0.5);
}
.pin-title {
    font-weight:700; font-size:15px; margin-bottom:4px; word-break:break-word;
    outline:none;
    cursor:default;
}
.pin-content {
    flex:1;
    overflow-y:auto;
    font-size:13px;
    line-height:1.5;
    word-break:break-word;
    outline:none;
    cursor:default;
}
.pin-content img {
    max-width:100%;
    height:auto;
    border-radius:4px;
}
.pin-content p { margin:4px 0; }
.pin-content ul, .pin-content ol { padding-left:18px; margin:4px 0; }
.pin-content code { background:rgba(0,0,0,0.08); padding:1px 4px; border-radius:3px; }
.pin-content pre { background:rgba(0,0,0,0.08); padding:6px; border-radius:4px; overflow-x:auto; }
.pin-tags {
    display:flex;
    flex-wrap:wrap;
    gap:4px;
    margin-top:6px;
}
.pin-tags .tag-label {
    font-size:10px;
    background:rgba(0,0,0,0.1);
    padding:1px 8px;
    border-radius:12px;
    color:#3d3d3d;
}
.pin-timestamp {
    font-size:9px;
    color:#5a5a5a;
    margin-top:4px;
    text-align:right;
    opacity:0.5;
}
.pin-toolbar {
    display:flex;
    gap:4px;
    margin-top:6px;
    justify-content:flex-end;
    opacity:0;
    transition: opacity .2s;
}
.pin:hover .pin-toolbar { opacity:1; }
.pin-toolbar button {
    background: rgba(0,0,0,0.08);
    border:none;
    border-radius:4px;
    padding:2px 6px;
    font-size:11px;
    cursor:pointer;
    color:#2b2b2b;
}
.pin-toolbar button:hover { background: rgba(0,0,0,0.18); }
.pin-toolbar .del-pin { color:#b33; }

.empty-hint {
    position:absolute; top:40px; left:40px; color:rgba(255,255,255,0.55); font-size:14px;
    background: rgba(0,0,0,0.25); padding:10px 14px; border-radius:8px; max-width:320px;
}
body.light-mode .empty-hint { color:rgba(0,0,0,0.5); background: rgba(255,255,255,0.2); }

/* ── Edit Modal ────────────────────────────────────── */
.modal-overlay {
    display:none;
    position:fixed; top:0; left:0; right:0; bottom:0;
    background: rgba(0,0,0,0.7);
    backdrop-filter: blur(8px);
    z-index:1000;
    align-items:center;
    justify-content:center;
}
.modal-overlay.active { display:flex; }
.modal {
    background: #1c2333;
    border-radius:16px;
    padding:24px 28px;
    max-width:700px;
    width:90%;
    max-height:90vh;
    overflow-y:auto;
    box-shadow: 0 20px 60px rgba(0,0,0,0.6);
    color:#e1e4e8;
    transition: background 0.3s;
}
body.light-mode .modal {
    background: #f0f2f5;
    color:#24292f;
}
.modal h2 { margin-bottom:16px; }
.modal label {
    display:block;
    font-size:13px;
    font-weight:600;
    margin:10px 0 4px;
}
.modal input[type="text"],
.modal textarea,
.modal input[type="number"],
.modal input[type="password"] {
    width:100%;
    padding:8px 12px;
    border-radius:8px;
    border:1px solid rgba(255,255,255,0.1);
    background: rgba(0,0,0,0.3);
    color:#e6edf3;
    font-size:14px;
    outline:none;
    transition: border-color .2s;
}
.modal input[type="text"]:focus,
.modal textarea:focus,
.modal input[type="number"]:focus,
.modal input[type="password"]:focus { border-color:#58a6ff; }
body.light-mode .modal input[type="text"],
body.light-mode .modal textarea,
body.light-mode .modal input[type="number"],
body.light-mode .modal input[type="password"] {
    background: #fff;
    color:#24292f;
    border-color:rgba(0,0,0,0.12);
}
.modal textarea { min-height:120px; resize:vertical; }
.modal .preview-box {
    background: rgba(0,0,0,0.2);
    border-radius:8px;
    padding:8px 12px;
    margin-top:6px;
    max-height:150px;
    overflow-y:auto;
    font-size:14px;
    line-height:1.5;
    display:none;
}
.modal .preview-box.visible { display:block; }
.modal .row {
    display:flex;
    gap:12px;
    flex-wrap:wrap;
    align-items:center;
}
.modal .row .col { flex:1; min-width:120px; }
.modal .color-picker {
    display:flex;
    gap:8px;
    flex-wrap:wrap;
    margin:6px 0;
}
.modal .color-option {
    width:28px; height:28px; border-radius:50%;
    cursor:pointer;
    border:2px solid transparent;
    transition: 0.2s;
}
.modal .color-option:hover { transform:scale(1.1); }
.modal .color-option.active { border-color:#58a6ff; box-shadow:0 0 10px rgba(88,166,255,0.4); }
.color-default { background:#b0b0b0; }
.color-yellow { background:#fdec8f; }
.color-blue   { background:#9fd3ff; }
.color-green  { background:#a9e6a1; }
.color-pink   { background:#ffb3cd; }
.color-orange { background:#ffc27a; }

/* AI section inside modal */
.modal .ai-section {
    margin-top:16px;
    padding-top:12px;
    border-top:1px solid rgba(255,255,255,0.1);
}
.modal .ai-section .ai-row {
    display:flex;
    gap:8px;
    flex-wrap:wrap;
    align-items:center;
}
.modal .ai-section select,
.modal .ai-section input[type="password"] {
    background: #2d2d3d;  
    border:1px solid rgba(255,255,255,0.1);
    border-radius:6px;
    color:#e6edf3;
    padding:4px 8px;
    font-size:13px;
    outline:none;
    height:32px;
}
.modal .ai-section select { max-width:130px; }
.modal .ai-section input[type="password"] { max-width:150px; display:none; }
.modal .ai-section .ai-btn {
    background:#6f42c1;
    border:none;
    border-radius:6px;
    color:#fff;
    padding:4px 16px;
    font-size:13px;
    cursor:pointer;
    transition:0.2s;
}
.modal .ai-section .ai-btn:hover { background:#8b5cf6; }
.modal .ai-section .ai-result {
    margin-top:8px;
    padding:8px 12px;
    border-radius:8px;
    background:rgba(255,255,255,0.05);
    border:1px solid rgba(255,255,255,0.08);
    color:#e1e4e8;
    font-size:14px;
    white-space:pre-wrap;
    display:none;
}
body.light-mode .modal .ai-section select,
body.light-mode .modal .ai-section input[type="password"] {
    background: #fff;
    color:#24292f;
    border-color:rgba(0,0,0,0.12);
}
body.light-mode .modal .ai-section .ai-result {
    background:rgba(0,0,0,0.03);
    color:#24292f;
}

/* ── Suggest Links modal ───────────────────────────── */
.link-suggestions-overlay {
    display:none;
    position:fixed; top:0; left:0; right:0; bottom:0;
    background: rgba(0,0,0,0.6);
    backdrop-filter: blur(5px);
    z-index:2000;
    align-items:center;
    justify-content:center;
}
.link-suggestions-overlay.active { display:flex; }
.link-suggestions-box {
    background: #1c2333;
    border-radius:16px;
    padding:24px 28px;
    max-width:500px;
    width:90%;
    max-height:80vh;
    overflow-y:auto;
    color:#e1e4e8;
    box-shadow:0 20px 60px rgba(0,0,0,0.6);
}
body.light-mode .link-suggestions-box {
    background:#f0f2f5;
    color:#24292f;
}
.link-suggestions-box h3 { margin-bottom:12px; }
.link-suggestions-box .suggestion-item {
    display:flex;
    justify-content:space-between;
    align-items:center;
    padding:6px 8px;
    border-bottom:1px solid rgba(255,255,255,0.06);
}
.link-suggestions-box .suggestion-item .title { flex:1; }
.link-suggestions-box .suggestion-item .score {
    font-size:11px;
    color:#8b949e;
    margin-right:8px;
}
.link-suggestions-box .suggestion-item .link-btn {
    background:#1f6feb;
    border:none;
    border-radius:4px;
    color:#fff;
    padding:2px 12px;
    font-size:12px;
    cursor:pointer;
}
.link-suggestions-box .suggestion-item .link-btn:hover { background:#388bfd; }
.link-suggestions-box .close-suggestions {
    margin-top:16px;
    text-align:right;
}
.link-suggestions-box .close-suggestions button {
    background:rgba(255,255,255,0.1);
    border:none;
    border-radius:6px;
    padding:6px 20px;
    color:#8b949e;
    cursor:pointer;
}
body.light-mode .link-suggestions-box .close-suggestions button { background:rgba(0,0,0,0.05); }

.modal .modal-actions {
    display:flex;
    justify-content:flex-end;
    gap:10px;
    margin-top:16px;
}
.modal .modal-actions button {
    padding:8px 20px;
    border-radius:8px;
    border:none;
    font-size:14px;
    cursor:pointer;
    transition:0.2s;
}
.modal .modal-actions .save-btn { background:#1f6feb; color:#fff; }
.modal .modal-actions .save-btn:hover { background:#388bfd; }
.modal .modal-actions .cancel-btn { background:rgba(255,255,255,0.1); color:#8b949e; }
.modal .modal-actions .cancel-btn:hover { background:rgba(255,255,255,0.2); }
.modal .modal-actions .delete-btn { background:rgba(248,81,73,0.2); color:#f85149; }
.modal .modal-actions .delete-btn:hover { background:rgba(248,81,73,0.4); }

/* ── LIGHT MODE overrides ──────────────────────────── */
body.light-mode {
    background: #f6f8fa;
    color: #24292f;
}
body.light-mode .top-bar {
    background: rgba(255,255,255,0.9);
    border-bottom-color: rgba(0,0,0,0.08);
}
body.light-mode .top-bar .left h1 {
    background: linear-gradient(135deg,#1f6feb,#a371f7);
    -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text;
}
body.light-mode .center-tabs {
    background: rgba(0,0,0,0.04);
    border-color: rgba(0,0,0,0.06);
}
body.light-mode .center-tabs .tab-btn { color: #57606a; }
body.light-mode .center-tabs .tab-btn.active { background:#1f6feb; color:#fff; }
body.light-mode .toolbar .top-btn {
    background: rgba(0,0,0,0.04);
    color: #24292f;
    border-color: rgba(0,0,0,0.12);
}
body.light-mode .clear-btn {
    background: rgba(0,0,0,0.04);
    border-color: rgba(248,81,73,0.3);
    color: #f85149;
}
body.light-mode .clear-btn:hover { background: rgba(248,81,73,0.08); }
body.light-mode .toolbar .search-input {
    background: rgba(255,255,255,0.8);
    color:#24292f;
    border-color:rgba(0,0,0,0.12);
}
body.light-mode .tag-filter {
    background: rgba(0,0,0,0.03);
}
body.light-mode .tag-filter .tag-pill {
    background: rgba(0,0,0,0.04);
    border-color:rgba(0,0,0,0.08);
    color:#57606a;
}
body.light-mode .tag-filter .tag-pill.active {
    background:#1f6feb;
    color:#fff;
}
body.light-mode .modal {
    background:#f0f2f5;
    color:#24292f;
}
body.light-mode .modal input, body.light-mode .modal textarea {
    background:#fff;
    color:#24292f;
    border-color:rgba(0,0,0,0.12);
}
body.light-mode .modal .ai-section .ai-result {
    background:rgba(0,0,0,0.03);
    color:#24292f;
}
body.light-mode .modal .ai-section select,
body.light-mode .modal .ai-section input[type="password"] {
    background: #fff;
    color:#24292f;
}
body.light-mode .link-suggestions-box {
    background:#f0f2f5;
    color:#24292f;
}
body.light-mode .link-suggestions-box .suggestion-item {
    border-bottom-color:rgba(0,0,0,0.06);
}
body.light-mode .toolbar .search-mode-toggle button { color:#57606a; }
body.light-mode .toolbar .search-mode-toggle button.active { background:#1f6feb; color:#fff; }

</style>
</head>
<body>
<div class="app">
    <!-- TOP BAR -->
    <div class="top-bar">
        <div class="left">
            <h1>📌 Cork Board</h1>
        </div>
        <div class="center-tabs">
            <a href="/" class="tab-btn" style="text-decoration:none;">💬 Chat</a>
            <a href="/notes" class="tab-btn" style="text-decoration:none;">📝 Notes</a>
            <button class="tab-btn active">📌 Cork Board</button>
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
            <button class="clear-btn" onclick="clearAllPins()">🗑 Clear All</button>
        </div>
    </div>

    <!-- TOOLBAR -->
    <div class="toolbar" id="toolbar">
        <input type="text" class="search-input" id="searchInput" placeholder="🔍 Search pins..." oninput="searchPins()">
        <span class="search-mode-toggle">
            <button id="searchModeKeyword" class="active" onclick="setSearchMode('keyword')">Keyword</button>
            <button id="searchModeSemantic" onclick="setSearchMode('semantic')">🧠 Semantic</button>
        </span>
        <button class="top-btn" onclick="createNewPin()">+ New Note</button>
        <span class="file-input-wrapper">
            <button class="top-btn">📎 Import File</button>
            <input type="file" accept=".md,.txt,.ipynb,.pdf,.png,.jpg,.jpeg,.gif,.svg,.webp" onchange="handleFileUpload(event)">
        </span>
        <button class="top-btn" id="linkBtn" onclick="toggleLinkMode()">🔗 Link Mode</button>
        <button class="top-btn" id="redThreadBtn" onclick="toggleRedThread()">🔴 Red Thread</button>
        <button class="top-btn" id="suggestLinksBtn" onclick="openLinkSuggestions()">💡 Suggest Links</button>
    </div>

    <!-- TAG FILTER -->
    <div class="tag-filter" id="tagFilterContainer"></div>

    <!-- BOARD AREA (with drag-and-drop support for images) -->
    <div class="board-wrap" id="boardWrap" ondragover="event.preventDefault();" ondrop="handleDropFile(event)">
        <div class="board" id="board">
            <svg class="link-layer" id="linkLayer">
                <defs>
                    <marker id="arrowhead-black" markerWidth="6" markerHeight="5" refX="5" refY="2.5" orient="auto">
                        <polygon points="0 0, 6 2.5, 0 5" class="arrow-marker" />
                    </marker>
                    <marker id="arrowhead-red" markerWidth="6" markerHeight="5" refX="5" refY="2.5" orient="auto">
                        <polygon points="0 0, 6 2.5, 0 5" class="arrow-marker red" />
                    </marker>
                </defs>
            </svg>
            <div class="empty-hint" id="emptyHint" style="display:none;">
                📌 Empty board. Click "+ New Note" or "📎 Import File" to pin something.
                Drag to arrange, double‑click to edit. Drop an image here to add it.
                <br><small>Right‑click a link line to remove it.</small>
            </div>
        </div>
    </div>
</div>

<!-- EDIT MODAL -->
<div class="modal-overlay" id="editModal">
    <div class="modal">
        <h2 id="modalTitle">Edit Pin</h2>
        <label for="pinTitle">Title</label>
        <input type="text" id="pinTitle" placeholder="Title...">
        <label for="pinContent">Content (Markdown)</label>
        <textarea id="pinContent" placeholder="Write your note in Markdown..."></textarea>
        <div style="margin-top:4px;">
            <button onclick="togglePreview()" style="background:rgba(255,255,255,0.05); border:1px solid rgba(255,255,255,0.1); border-radius:6px; padding:4px 12px; color:#8b949e; cursor:pointer;">👁️ Preview</button>
        </div>
        <div class="preview-box" id="pinPreview"></div>
        <label>Tags (comma separated)</label>
        <input type="text" id="pinTags" placeholder="e.g. project, idea">
        <div class="row">
            <div class="col">
                <label>Colour</label>
                <div class="color-picker" id="pinColorPicker">
                    <span class="color-option color-default" data-color="default" title="Default"></span>
                    <span class="color-option color-yellow" data-color="yellow" title="Yellow"></span>
                    <span class="color-option color-blue" data-color="blue" title="Blue"></span>
                    <span class="color-option color-green" data-color="green" title="Green"></span>
                    <span class="color-option color-pink" data-color="pink" title="Pink"></span>
                    <span class="color-option color-orange" data-color="orange" title="Orange"></span>
                </div>
            </div>
            <div class="col">
                <label>Width</label>
                <input type="number" id="pinWidth" min="120" max="500" value="220">
            </div>
            <div class="col">
                <label>Height</label>
                <input type="number" id="pinHeight" min="100" max="600" value="160">
            </div>
        </div>

        <!-- AI ASSIST SECTION (with provider, api key, model dropdowns) -->
        <div class="ai-section">
            <div class="ai-row">
                <label style="margin:0; font-weight:500;">✨ AI Assist:</label>
                <select id="aiActionSelect">
                    <option value="summarise">Summarise</option>
                    <option value="suggest_tags">Suggest Tags</option>
                    <option value="improve">Improve Writing</option>
                    <option value="suggest_links">Suggest Links for this Pin</option>
                </select>
                <select id="aiProviderSelect">
                    <option value="ollama">Ollama</option>
                    <option value="llamacpp">llama.cpp</option>
                    <option value="huggingface">Hugging Face</option>
                    <option value="groq">Groq</option>
                    <option value="deepseek">DeepSeek</option>
                    <option value="claude">Claude (Anthropic)</option>
                </select>
                <input type="password" id="aiApiKeyInput" placeholder="API Key (if required)" style="display:none; max-width:150px;">
                <select id="aiModelSelect" title="Select model">
                    <option value="">Loading models...</option>
                </select>
                <button class="ai-btn" id="aiAssistBtn">Run AI</button>
            </div>
            <div class="ai-result" id="aiResult"></div>
        </div>

        <div class="modal-actions">
            <button class="delete-btn" id="modalDeleteBtn" onclick="deleteCurrentPin()">🗑 Delete</button>
            <button class="cancel-btn" onclick="closeModal()">Cancel</button>
            <button class="save-btn" onclick="savePinFromModal()">💾 Save</button>
        </div>
    </div>
</div>

<!-- LINK SUGGESTIONS MODAL -->
<div class="link-suggestions-overlay" id="linkSuggestionsOverlay">
    <div class="link-suggestions-box">
        <h3>💡 Suggested Links</h3>
        <div id="suggestionsList"></div>
        <div class="close-suggestions">
            <button onclick="closeLinkSuggestions()">Close</button>
        </div>
    </div>
</div>

<script>
// ─── THEME (unchanged) ─────────────────────────────────────
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

// ─── STATE ────────────────────────────────────────────
var boardEl = document.getElementById('board');
var linkLayer = document.getElementById('linkLayer');
var boardData = { pins: {}, links: [] };
var linkMode = false;
var linkSourceId = null;
var saveTimers = {};
var activeTagFilter = '';
var searchQuery = '';
var editingPinId = null;
var redThreadMode = false;
var isDragging = false;
var searchDebounceTimer = null;
var searchMode = 'keyword';
var linkElementMap = {};

// ─── DYNAMIC BOARD SIZE ──────────────────────────────
function updateBoardSize() {
    const wrap = document.getElementById('boardWrap');
    if (!wrap) return;
    const wrapStyle = getComputedStyle(wrap);
    const padLeft = parseFloat(wrapStyle.paddingLeft) || 0;
    const padRight = parseFloat(wrapStyle.paddingRight) || 0;
    const padTop = parseFloat(wrapStyle.paddingTop) || 0;
    const padBottom = parseFloat(wrapStyle.paddingBottom) || 0;
    const contentWidth = wrap.clientWidth - padLeft - padRight;
    const contentHeight = wrap.clientHeight - padTop - padBottom;
    let maxX = contentWidth;
    let maxY = contentHeight;
    Object.values(boardData.pins).forEach(pin => {
        const r = (pin.width || 220) + (pin.x || 0);
        const b = (pin.height || 160) + (pin.y || 0);
        if (r + 200 > maxX) maxX = r + 200;
        if (b + 200 > maxY) maxY = b + 200;
    });
    boardEl.style.width = maxX + 'px';
    boardEl.style.height = maxY + 'px';
}
let boardResizeRAF = null;
function scheduleBoardSizeUpdate() {
    if (boardResizeRAF) cancelAnimationFrame(boardResizeRAF);
    boardResizeRAF = requestAnimationFrame(() => {
        updateBoardSize();
        boardResizeRAF = null;
    });
}

// ─── Load & render ────────────────────────────────────
function loadBoard() {
    var query = document.getElementById('searchInput').value.trim();
    var tag = activeTagFilter;
    var url;
    if (query && searchMode === 'semantic') {
        url = '/corkboard/api/semantic_search?q=' + encodeURIComponent(query);
        fetch(url)
            .then(r => r.json())
            .then(results => {
                if (results.error) { console.warn(results.error); loadAllAndFilter(query, tag, null); return; }
                loadAllAndFilter(query, tag, results);
            })
            .catch(() => loadAllAndFilter(query, tag, null));
    } else {
        url = '/corkboard/api/search?q=' + encodeURIComponent(query) + '&tag=' + encodeURIComponent(tag);
        fetch(url)
            .then(r => r.json())
            .then(data => {
                boardData = data;
                renderAll();
                renderTagFilter();
            })
            .catch(e => console.error('Failed to load board:', e));
    }
}

function loadAllAndFilter(query, tag, semanticResults) {
    fetch('/corkboard/api?q=&tag=')
        .then(r => r.json())
        .then(allData => {
            var allPins = allData.pins || {};
            var filteredPins = {};
            if (semanticResults && semanticResults.length) {
                semanticResults.forEach(item => {
                    if (allPins[item.id]) filteredPins[item.id] = allPins[item.id];
                });
            } else {
                for (var pid in allPins) {
                    var pin = allPins[pid];
                    var match = true;
                    if (tag && !pin.tags.map(t => t.toLowerCase()).includes(tag)) match = false;
                    if (query && match) {
                        var q = query.toLowerCase();
                        var titleMatch = pin.title.toLowerCase().includes(q);
                        var contentMatch = pin.content.toLowerCase().includes(q);
                        var tagMatch = pin.tags.some(t => t.toLowerCase().includes(q));
                        if (!(titleMatch || contentMatch || tagMatch)) match = false;
                    }
                    if (match) filteredPins[pid] = pin;
                }
            }
            boardData.pins = filteredPins;
            boardData.links = allData.links.filter(l => filteredPins[l.from] && filteredPins[l.to]);
            renderAll();
            renderTagFilter();
        });
}

function renderAll() {
    var existingPins = document.querySelectorAll('.pin');
    var keepIds = new Set(Object.keys(boardData.pins));
    existingPins.forEach(el => {
        if (!keepIds.has(el.dataset.id)) el.remove();
    });
    Object.values(boardData.pins).forEach(pin => {
        var el = document.querySelector(`.pin[data-id="${pin.id}"]`);
        if (el) updatePinElement(el, pin);
        else renderPin(pin);
    });
    document.getElementById('emptyHint').style.display = keepIds.size ? 'none' : 'block';
    renderLinks();
    scheduleBoardSizeUpdate();
}

function renderPin(pin) {
    var div = document.createElement('div');
    div.dataset.id = pin.id;
    div.className = 'pin';
    boardEl.appendChild(div);
    updatePinElement(div, pin);
    div.addEventListener('dblclick', function(e) {
        e.stopPropagation();
        openEditModal(pin.id);
    });
    div.addEventListener('mousedown', e => dragStart(e, div, pin.id));
    div.addEventListener('touchstart', e => dragStart(e, div, pin.id), { passive: false });
    div.addEventListener('click', function(e) {
        if (isDragging) return;
        if (!linkMode) return;
        if (e.target.closest('.pin-title') || e.target.closest('.pin-content') ||
            e.target.closest('.pin-tags') || e.target.closest('.pin-timestamp') ||
            e.target.closest('.pin-toolbar')) return;
        handleLinkClick(pin.id, div);
    });
}

function updatePinElement(el, pin) {
    var lastModified = el.dataset.lastModified || '';
    if (lastModified === pin.last_modified) return;
    var colorClass = 'color-' + (pin.color || 'yellow');
    el.className = 'pin ' + colorClass;
    var w = pin.width || 220;
    var h = pin.height || 160;
    el.style.width = w + 'px';
    el.style.height = h + 'px';
    el.style.transform = 'translate3d(' + (pin.x || 40) + 'px, ' + (pin.y || 40) + 'px, 0) rotate(' + (pin.rotation || 0) + 'deg)';

    var contentHtml = marked.parse(pin.content || '');
    var tagsHtml = (pin.tags || []).map(t => `<span class="tag-label">#${escapeHtml(t)}</span>`).join('');

    el.innerHTML = `
        <div class="pin-nail"></div>
        <div class="pin-title">${escapeHtml(pin.title)}</div>
        <div class="pin-content">${contentHtml}</div>
        ${tagsHtml ? `<div class="pin-tags">${tagsHtml}</div>` : ''}
        <div class="pin-timestamp">${new Date(pin.last_modified || pin.created).toLocaleString()}</div>
        <div class="pin-toolbar">
            <button class="edit-pin" title="Edit">✏️</button>
            <button class="del-pin" title="Delete">🗑</button>
        </div>
    `;
    el.querySelector('.edit-pin').addEventListener('click', function(e) {
        e.stopPropagation();
        openEditModal(pin.id);
    });
    el.querySelector('.del-pin').addEventListener('click', function(e) {
        e.stopPropagation();
        if (confirm('Delete this pin?')) deletePin(pin.id);
    });
    el.dataset.lastModified = pin.last_modified || '';
}

function escapeHtml(text) {
    var d = document.createElement('div');
    d.textContent = text || '';
    return d.innerHTML;
}

// ─── Links ─────────────────────────────────────────────
function hashStr(s) {
    var h = 0;
    for (var i = 0; i < s.length; i++) { h = (h * 31 + s.charCodeAt(i)) | 0; }
    return Math.abs(h);
}
function buildLinkPath(link, a, b) {
    var x1 = a.x + (a.width || 220)/2, y1 = a.y + (a.height || 160)/2;
    var x2 = b.x + (b.width || 220)/2, y2 = b.y + (b.height || 160)/2;
    var dx = x2 - x1, dy = y2 - y1;
    var dist = Math.max(Math.sqrt(dx*dx + dy*dy), 1);
    var px = -dy / dist, py = dx / dist;
    var seed = hashStr(link.from + '-' + link.to);
    var side = ((seed % 200) / 100) - 1;
    var wave = side * Math.min(dist * 0.28, 70);
    var sag = Math.min(dist * 0.12, 36);
    var cp1x = x1 + dx * 0.33 + px * wave;
    var cp1y = y1 + dy * 0.33 + py * wave + sag;
    var cp2x = x1 + dx * 0.66 - px * wave;
    var cp2y = y1 + dy * 0.66 - py * wave + sag;
    return `M ${x1} ${y1} C ${cp1x} ${cp1y}, ${cp2x} ${cp2y}, ${x2} ${y2}`;
}

function renderLinks() {
    var defs = linkLayer.querySelector('defs');
    if (!defs) {
        defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
        defs.innerHTML = `
            <marker id="arrowhead-black" markerWidth="6" markerHeight="5" refX="5" refY="2.5" orient="auto">
                <polygon points="0 0, 6 2.5, 0 5" class="arrow-marker" />
            </marker>
            <marker id="arrowhead-red" markerWidth="6" markerHeight="5" refX="5" refY="2.5" orient="auto">
                <polygon points="0 0, 6 2.5, 0 5" class="arrow-marker red" />
            </marker>
        `;
        linkLayer.prepend(defs);
    }

    var activeIds = new Set();
    boardData.links.forEach(link => { activeIds.add(`${link.from}-${link.to}`); });

    var existingLines = linkLayer.querySelectorAll('.link-line');
    existingLines.forEach(line => {
        var id = line.dataset.linkId;
        if (!activeIds.has(id)) {
            line.remove();
            delete linkElementMap[id];
        }
    });

    boardData.links.forEach(link => {
        var a = boardData.pins[link.from], b = boardData.pins[link.to];
        if (!a || !b) return;
        var id = `${link.from}-${link.to}`;
        var line = linkElementMap[id];
        if (!line) {
            line = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            line.setAttribute('class', 'link-line');
            line.dataset.linkId = id;
            line.addEventListener('contextmenu', function(e) {
                e.preventDefault();
                e.stopPropagation();
                if (confirm('Remove this link?')) removeLink(link.from, link.to);
            });
            linkLayer.appendChild(line);
            linkElementMap[id] = line;
        }
        var isRed = (link.color === 'red');
        var curved = buildLinkPath(link, a, b);
        line.setAttribute('class', isRed ? 'link-line red' : 'link-line black');
        line.setAttribute('d', curved);

    });
    boardEl.addEventListener('contextmenu', function(e) {
        if (e.target.closest('.link-line')) e.preventDefault();
    });
}

function updateLinksForPin(pinId) {
    boardData.links.forEach(link => {
        if (link.from !== pinId && link.to !== pinId) return;
        var a = boardData.pins[link.from], b = boardData.pins[link.to];
        if (!a || !b) return;
        var id = `${link.from}-${link.to}`;
        var line = linkElementMap[id];
        if (line) line.setAttribute('d', buildLinkPath(link, a, b));
    });
}

// ─── DRAG ─────────────────────────────────────────────
var dragCtx = null;
var dragRAF = null;
function dragStart(e, el, id) {
    if (e.target.closest('.pin-title') || e.target.closest('.pin-content') ||
        e.target.closest('.pin-tags') || e.target.closest('.pin-timestamp') ||
        e.target.closest('.pin-toolbar')) return;
    if (linkMode) return;
    e.preventDefault();
    isDragging = true;
    var point = e.touches ? e.touches[0] : e;
    var rect = el.getBoundingClientRect();
    var boardRect = boardEl.getBoundingClientRect();
    dragCtx = { el: el, id: id, offsetX: point.clientX - rect.left, offsetY: point.clientY - rect.top, boardRect: boardRect };
    el.classList.add('dragging');
    window.addEventListener('mousemove', dragMove);
    window.addEventListener('mouseup', dragEnd);
    window.addEventListener('touchmove', dragMove, { passive: false });
    window.addEventListener('touchend', dragEnd);
}
function dragMove(e) {
    if (!dragCtx) return;
    e.preventDefault();
    var point = e.touches ? e.touches[0] : e;
    dragCtx.lastClientX = point.clientX;
    dragCtx.lastClientY = point.clientY;
    if (dragRAF) return;
    dragRAF = requestAnimationFrame(applyDragMove);
}
function applyDragMove() {
    dragRAF = null;
    if (!dragCtx) return;
    var x = dragCtx.lastClientX - dragCtx.boardRect.left - dragCtx.offsetX;
    var y = dragCtx.lastClientY - dragCtx.boardRect.top - dragCtx.offsetY;
    x = Math.max(0, x); y = Math.max(0, y);
    dragCtx.el.style.transform = 'translate3d(' + x + 'px, ' + y + 'px, 0) rotate(' + (boardData.pins[dragCtx.id].rotation || 0) + 'deg)';
    boardData.pins[dragCtx.id].x = x;
    boardData.pins[dragCtx.id].y = y;
    updateLinksForPin(dragCtx.id);
}
function dragEnd() {
    if (!dragCtx) return;
    if (dragRAF) { cancelAnimationFrame(dragRAF); dragRAF = null; }
    dragCtx.el.classList.remove('dragging');
    var id = dragCtx.id;
    var pin = boardData.pins[id];
    savePin(id, { x: pin.x, y: pin.y }, true);
    updateLinksForPin(id);
    renderLinks();
    scheduleBoardSizeUpdate();
    dragCtx = null;
    isDragging = false;
    window.removeEventListener('mousemove', dragMove);
    window.removeEventListener('mouseup', dragEnd);
    window.removeEventListener('touchmove', dragMove);
    window.removeEventListener('touchend', dragEnd);
}

// ─── SAVE ─────────────────────────────────────────────
function savePin(id, fields, immediate) {
    Object.assign(boardData.pins[id], fields);
    if (saveTimers[id]) clearTimeout(saveTimers[id]);
    var run = () => {
        fetch('/corkboard/api/pins/' + id, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(fields)
        }).catch(e => console.error('Save failed:', e));
    };
    if (immediate) run(); else saveTimers[id] = setTimeout(run, 400);
}

// ─── CRUD ────────────────────────────────────────────
function createNewPin() {
    var wrap = document.getElementById('boardWrap');
    fetch('/corkboard/api/pins', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            title: 'New Note',
            content: 'Click to edit...',
            x: wrap.scrollLeft + 60,
            y: wrap.scrollTop + 60,
            width: 220,
            height: 160
        })
    }).then(r => r.json()).then(data => {
        if (data.ok) {
            boardData.pins[data.id] = data.pin;
            renderAll();
            renderTagFilter();
        }
    });
}

function deletePin(id) {
    if (!confirm('Delete this pin?')) return;
    fetch('/corkboard/api/pins/' + id, { method: 'DELETE' })
        .then(r => r.json()).then(data => {
            if (data.ok) {
                delete boardData.pins[id];
                boardData.links = boardData.links.filter(l => l.from !== id && l.to !== id);
                for (var key in linkElementMap) {
                    if (key.startsWith(id + '-') || key.endsWith('-' + id)) delete linkElementMap[key];
                }
                renderAll();
                renderTagFilter();
            }
        });
}

function clearAllPins() {
    if (!confirm('Delete ALL pins and links?')) return;
    fetch('/corkboard/api/clear_all', { method: 'POST' })
        .then(r => r.json()).then(data => {
            if (data.ok) {
                boardData = { pins: {}, links: [] };
                linkElementMap = {};
                renderAll();
                renderTagFilter();
            }
        });
}

// ─── FILE UPLOAD ─────────────────────────────────────
function handleFileUpload(e) {
    var file = e.target.files[0];
    if (!file) return;
    uploadFile(file);
    e.target.value = '';
}

function handleDropFile(e) {
    e.preventDefault();
    var files = e.dataTransfer.files;
    if (files.length) uploadFile(files[0]);
}

function uploadFile(file) {
    var formData = new FormData();
    formData.append('file', file);
    fetch('/corkboard/api/upload', { method: 'POST', body: formData })
        .then(r => r.json())
        .then(data => {
            if (data.ok) {
                boardData.pins[data.id] = data.pin;
                renderAll();
                renderTagFilter();
            } else alert(data.error || 'Upload failed');
        })
        .catch(err => alert('Upload failed: ' + err));
}

// ─── SEARCH & TAG FILTER ──────────────────────────────
function searchPins() {
    clearTimeout(searchDebounceTimer);
    searchDebounceTimer = setTimeout(loadBoard, 300);
}

function setTagFilter(tag) {
    activeTagFilter = tag;
    loadBoard();
}

function renderTagFilter() {
    var container = document.getElementById('tagFilterContainer');
    var tagSet = new Set();
    Object.values(boardData.pins).forEach(p => { if (p.tags) p.tags.forEach(t => tagSet.add(t)); });
    var tags = Array.from(tagSet).sort();
    var html = '';
    if (tags.length) {
        html += `<span class="filter-label">Filter:</span>`;
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

// ─── Search mode toggle ──────────────────────────────
function setSearchMode(mode) {
    searchMode = mode;
    document.getElementById('searchModeKeyword').classList.toggle('active', mode === 'keyword');
    document.getElementById('searchModeSemantic').classList.toggle('active', mode === 'semantic');
    loadBoard();
}

// ─── RED THREAD ───────────────────────────────────────
function toggleRedThread() {
    redThreadMode = !redThreadMode;
    document.getElementById('redThreadBtn').classList.toggle('red-thread-active', redThreadMode);
    document.getElementById('redThreadBtn').title = redThreadMode ? 'Red Thread ON' : 'Red Thread OFF';
}

// ─── LINK MODE ────────────────────────────────────────
function toggleLinkMode() {
    linkMode = !linkMode;
    linkSourceId = null;
    document.getElementById('linkBtn').classList.toggle('linking', linkMode);
    document.querySelectorAll('.pin').forEach(p => p.classList.remove('link-source'));
}
function handleLinkClick(id, el) {
    if (!linkSourceId) { linkSourceId = id; el.classList.add('link-source'); return; }
    if (linkSourceId === id) { linkSourceId = null; el.classList.remove('link-source'); return; }
    var color = redThreadMode ? 'red' : 'black';
    fetch('/corkboard/api/links', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ from: linkSourceId, to: id, color: color })
    }).then(r => r.json()).then(data => {
        if (data.ok) {
            if (data.linked) {
                boardData.links.push({ from: linkSourceId, to: id, color: color });
            } else {
                boardData.links = boardData.links.filter(l =>
                    !((l.from === linkSourceId && l.to === id) || (l.from === id && l.to === linkSourceId)));
                var key1 = `${linkSourceId}-${id}`;
                var key2 = `${id}-${linkSourceId}`;
                delete linkElementMap[key1];
                delete linkElementMap[key2];
            }
            renderLinks();
        }
        document.querySelectorAll('.pin').forEach(p => p.classList.remove('link-source'));
        linkSourceId = null;
    });
}

function removeLink(from, to) {
    fetch('/corkboard/api/links', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ from: from, to: to, color: 'black' })
    }).then(r => r.json()).then(data => {
        if (data.ok) {
            boardData.links = boardData.links.filter(l =>
                !((l.from === from && l.to === to) || (l.from === to && l.to === from)));
            var key = `${from}-${to}`;
            delete linkElementMap[key];
            renderLinks();
        }
    }).catch(e => console.error('Failed to remove link:', e));
}

// ─── Load models dynamically ─────────────────────────
function loadOllamaModels(provider, apiKey) {
    const select = document.getElementById('aiModelSelect');
    select.innerHTML = '<option value="">Loading...</option>';
    fetch('/providers/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider: provider, api_key: apiKey || '' })
    })
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                select.innerHTML = '<option value="">⚠️ ' + data.error + '</option>';
                console.warn('Model loading error:', data.error);
                return;
            }
            if (!data.models || data.models.length === 0) {
                select.innerHTML = '<option value="">No models found</option>';
                return;
            }
            const saved = localStorage.getItem('corkboard_ai_model') || '';
            let options = '';
            data.models.forEach(m => {
                const selected = (m === saved) ? 'selected' : '';
                options += `<option value="${m}" ${selected}>${m}</option>`;
            });
            select.innerHTML = options;
            if (!saved || !data.models.includes(saved)) {
                if (data.models.length) {
                    select.value = data.models[0];
                    localStorage.setItem('corkboard_ai_model', data.models[0]);
                }
            }
        })
        .catch(err => {
            console.error('Failed to fetch models:', err);
            select.innerHTML = '<option value="">⚠️ Cannot reach provider</option>';
        });
}

// ─── Provider change in AI section ──────────────────
document.getElementById('aiProviderSelect').addEventListener('change', function() {
    var provider = this.value;
    var apiKeyInput = document.getElementById('aiApiKeyInput');
    if (['groq', 'huggingface', 'deepseek', 'claude'].includes(provider)) {
        apiKeyInput.style.display = 'inline-block';
        var placeholder = '';
        if (provider === 'groq') placeholder = 'Groq API Key';
        else if (provider === 'huggingface') placeholder = 'HF Token';
        else if (provider === 'deepseek') placeholder = 'DeepSeek API Key';
        else if (provider === 'claude') placeholder = 'Anthropic API Key';
        apiKeyInput.placeholder = placeholder;
        var savedKey = localStorage.getItem('corkboard_api_key_' + provider) || '';
        apiKeyInput.value = savedKey;
    } else {
        apiKeyInput.style.display = 'none';
        apiKeyInput.value = '';
    }
    var apiKey = apiKeyInput.value;
    loadOllamaModels(provider, apiKey);
    localStorage.setItem('corkboard_ai_provider', provider);
});

document.getElementById('aiApiKeyInput').addEventListener('blur', function() {
    var provider = document.getElementById('aiProviderSelect').value;
    var key = this.value.trim();
    if (key) {
        localStorage.setItem('corkboard_api_key_' + provider, key);
    } else {
        localStorage.removeItem('corkboard_api_key_' + provider);
    }
    loadOllamaModels(provider, key);
});

document.getElementById('aiModelSelect').addEventListener('change', function() {
    localStorage.setItem('corkboard_ai_model', this.value);
});

// ─── EDIT MODAL ──────────────────────────────────────
function openEditModal(id) {
    var pin = boardData.pins[id];
    if (!pin) return;
    editingPinId = id;
    document.getElementById('pinTitle').value = pin.title || '';
    document.getElementById('pinContent').value = pin.content || '';
    document.getElementById('pinTags').value = (pin.tags || []).join(', ');
    document.getElementById('pinWidth').value = pin.width || 220;
    document.getElementById('pinHeight').value = pin.height || 160;
    var color = pin.color || 'default';
    document.querySelectorAll('#pinColorPicker .color-option').forEach(el => {
        el.classList.toggle('active', el.dataset.color === color);
    });
    document.getElementById('modalTitle').textContent = 'Edit Pin';
    document.getElementById('modalDeleteBtn').style.display = 'inline-block';
    document.getElementById('pinPreview').innerHTML = '';
    document.getElementById('pinPreview').classList.remove('visible');
    document.getElementById('aiResult').style.display = 'none';
    var savedProvider = localStorage.getItem('corkboard_ai_provider') || 'ollama';
    document.getElementById('aiProviderSelect').value = savedProvider;
    var evt = new Event('change');
    document.getElementById('aiProviderSelect').dispatchEvent(evt);
    var savedModel = localStorage.getItem('corkboard_ai_model') || '';
    if (savedModel) {
        var modelSelect = document.getElementById('aiModelSelect');
        for (var i = 0; i < modelSelect.options.length; i++) {
            if (modelSelect.options[i].value === savedModel) {
                modelSelect.value = savedModel;
                break;
            }
        }
    }
    document.getElementById('editModal').classList.add('active');
}

function closeModal() {
    document.getElementById('editModal').classList.remove('active');
    editingPinId = null;
}

function togglePreview() {
    var preview = document.getElementById('pinPreview');
    var content = document.getElementById('pinContent').value;
    preview.innerHTML = marked.parse(content || '');
    preview.classList.toggle('visible');
}

// ─── AI Assist in Modal ──────────────────────────────
document.getElementById('aiAssistBtn').addEventListener('click', function() {
    if (!editingPinId) {
        alert('Please save the pin first, then use AI assist.');
        return;
    }
    var action = document.getElementById('aiActionSelect').value;
    var provider = document.getElementById('aiProviderSelect').value;
    var apiKeyInput = document.getElementById('aiApiKeyInput');
    var apiKey = apiKeyInput.style.display !== 'none' ? apiKeyInput.value : '';
    var modelSelect = document.getElementById('aiModelSelect');
    var model = modelSelect.value;
    if (!model) {
        alert('No model selected. Please wait for models to load or select one.');
        return;
    }
    var resultDiv = document.getElementById('aiResult');
    resultDiv.style.display = 'block';
    resultDiv.textContent = '⏳ Thinking...';

    fetch('/corkboard/api/ai_assist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            pin_id: editingPinId,
            action: action,
            provider: provider,
            api_key: apiKey,
            model: model
        })
    })
    .then(r => r.json())
    .then(data => {
        if (data.error) {
            resultDiv.textContent = '❌ ' + data.error;
            return;
        }
        if (action === 'suggest_tags' && data.tags) {
            var tagsInput = document.getElementById('pinTags');
            var existing = tagsInput.value.split(',').map(s => s.trim()).filter(Boolean);
            var newTags = data.tags.filter(t => !existing.includes(t));
            if (newTags.length) {
                tagsInput.value = existing.concat(newTags).join(', ');
            }
            resultDiv.textContent = '✅ Suggested tags: ' + data.tags.join(', ') + ' (added to tags field)';
        } else if (action === 'suggest_links' && data.suggestions) {
            var list = data.suggestions.map(s => `• ${s.title} (${(s.score*100).toFixed(0)}%)`).join('\n');
            resultDiv.textContent = '🔗 Suggested links:\n' + list + '\n\nUse "💡 Suggest Links" button in toolbar to create them.';
        } else {
            resultDiv.textContent = data.result || 'Done.';
        }
    })
    .catch(err => {
        resultDiv.textContent = '❌ Error: ' + err;
    });
});

// ─── SAVE PIN FROM MODAL ─────────────────────────────
function savePinFromModal() {
    if (!editingPinId) return;
    var title = document.getElementById('pinTitle').value.trim() || 'Untitled';
    var content = document.getElementById('pinContent').value;
    var tags = document.getElementById('pinTags').value.split(',').map(s => s.trim()).filter(Boolean);
    var width = parseInt(document.getElementById('pinWidth').value) || 220;
    var height = parseInt(document.getElementById('pinHeight').value) || 160;
    var color = document.querySelector('#pinColorPicker .color-option.active')?.dataset.color || 'default';

    var pin = boardData.pins[editingPinId];
    var fields = { title, content, tags, width, height, color };
    fetch('/corkboard/api/pins/' + editingPinId, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(fields)
    }).then(r => r.json()).then(data => {
        if (data.ok) {
            Object.assign(pin, fields);
            pin.last_modified = new Date().toISOString();
            renderAll();
            renderTagFilter();
            closeModal();
        } else alert('Save failed');
    }).catch(err => alert('Error: ' + err));
}

function deleteCurrentPin() {
    if (!editingPinId) return;
    if (confirm('Delete this pin?')) {
        deletePin(editingPinId);
        closeModal();
    }
}

// ─── LINK SUGGESTIONS (global) ──────────────────────
function openLinkSuggestions() {
    alert('Global link suggestions will be implemented soon. Use "Suggest Links for this Pin" from the edit modal.');
}

function closeLinkSuggestions() {
    document.getElementById('linkSuggestionsOverlay').classList.remove('active');
}

function createLink(from, to) {
    var color = redThreadMode ? 'red' : 'black';
    fetch('/corkboard/api/links', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ from: from, to: to, color: color })
    }).then(r => r.json()).then(data => {
        if (data.ok && data.linked) {
            boardData.links.push({ from: from, to: to, color: color });
            renderLinks();
            var items = document.querySelectorAll('.suggestion-item');
            items.forEach(function(item) {
                var btn = item.querySelector('.link-btn');
                if (btn && btn.dataset.from === from && btn.dataset.to === to) {
                    item.remove();
                }
            });
        }
    });
}

// ─── Colour picker in modal ──────────────────────────
document.querySelectorAll('#pinColorPicker .color-option').forEach(el => {
    el.addEventListener('click', function() {
        document.querySelectorAll('#pinColorPicker .color-option').forEach(c => c.classList.remove('active'));
        this.classList.add('active');
    });
});

// ─── INIT ────────────────────────────────────────────
window.addEventListener('load', function() {
    loadBoard();
    var savedProvider = localStorage.getItem('corkboard_ai_provider') || 'ollama';
    document.getElementById('aiProviderSelect').value = savedProvider;
    var evt = new Event('change');
    document.getElementById('aiProviderSelect').dispatchEvent(evt);
    // Close the edit modal ONLY on an actual double-click on the dark background —
    // never on a single click, and never as a side-effect of a text-selection drag
    // that happens to release near/past the edge (mousedown target is checked too,
    // so a drag that starts inside the textarea and ends on the backdrop won't close it).
    var editModalEl = document.getElementById('editModal');
    var overlayMouseDownOnSelf = false;
    editModalEl.addEventListener('mousedown', function(e) {
        overlayMouseDownOnSelf = (e.target === this);
    });
    editModalEl.addEventListener('dblclick', function(e) {
        if (e.target === this && overlayMouseDownOnSelf) closeModal();
    });
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') closeModal();
    });
    document.getElementById('redThreadBtn').classList.remove('red-thread-active');
    document.getElementById('redThreadBtn').title = 'Red Thread OFF';
    redThreadMode = false;
});

let resizeTimer;
window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(scheduleBoardSizeUpdate, 100);
});
</script>
</body>
</html>
"""