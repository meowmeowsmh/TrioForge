# cork_board.py – advanced with Markdown, tags, search, resizable pins, modal editor,
# Red Thread links, and AI assistance with dynamic model selection.
# STORAGE: SQLite (sqlite_data/notes.db) — viewable offline with any SQLite browser
# (DB Browser for SQLite, "SQLite Viewer" VSCode extension, etc.) while the app is closed.
# OPTIMIZED: WAL mode, indexed lookups, targeted single-row writes (no full-board rewrites),
# lazy embedding, throttled drag updates, AI action history log.
# + IMAGE UPLOAD support – drag/drop images to create picture pins.
# + WEATHER WIDGET integrated at top‑right corner (persistent across pages).

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

# ---------- HTML template (with image drag support and weather widget) ----------
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

/* ── Weather toggle button ── */
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

/* ── WEATHER WIDGET (compact, fixed top‑right, toggleable) ── */
.weather-widget {
    position: fixed;
    top: 70px;
    right: 16px;
    z-index: 99999;
    max-width: 280px;
    width: auto;
    pointer-events: none;
}
.weather-widget .toast {
    background: rgba(22, 27, 34, 0.94);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 18px;
    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.7);
    color: #e1e4e8;
    pointer-events: auto;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    padding: 0;
    position: relative;     /* for absolute child positioning */
}
.weather-widget .toast-scene {
    height: 90px;
    flex-shrink: 0;
    background: #1a1a2e;
    position: relative;
    overflow: hidden;
}
.weather-widget .toast-canvas {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    pointer-events: none;
    display: block;
}
.weather-widget .toast-scene::after {
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
.weather-widget .toast-content {
    position: relative;
    z-index: 3;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 14px 10px 12px;
}
.weather-widget .toast-icon {
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
.weather-widget .toast-text {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 0px;
    min-width: 0;
}
.weather-widget .toast-text .main {
    font-size: 12px;
    line-height: 1.2;
    font-weight: 500;
    color: #e1e4e8;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.weather-widget .toast-text .main .highlight {
    font-weight: 700;
    color: #fff;
}
.weather-widget .toast-text .sub {
    font-size: 10px;
    color: rgba(255,255,255,0.6);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.weather-widget .toast-text .time-row {
    display: flex;
    align-items: baseline;
    gap: 4px;
}
.weather-widget .toast-text .time-row .clock {
    font-size: 14px;
    font-weight: 700;
    color: #fff;
    letter-spacing: 0.3px;
    font-variant-numeric: tabular-nums;
}
.weather-widget .toast-text .time-row .date {
    font-size: 9px;
    color: rgba(255,255,255,0.5);
}
.weather-widget .toast-text .weather-row {
    display: flex;
    align-items: center;
    gap: 4px;
    font-size: 11px;
    color: rgba(255,255,255,0.8);
}
.weather-widget .toast-text .weather-row .temp {
    font-weight: 700;
    font-size: 13px;
    color: #fff;
}
.weather-widget .toast-text .weather-row .condition {
    font-size: 10px;
    color: rgba(255,255,255,0.6);
}
.weather-widget .toast-text .weather-row .weather-emoji {
    font-size: 14px;
}
.weather-widget .toast-text .fetch-status {
    font-size: 9px;
    color: rgba(255,255,255,0.4);
    font-style: italic;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.weather-widget .toast-progress {
    position: absolute;
    bottom: 0;
    left: 0;
    width: 0%;
    height: 3px;
    background: linear-gradient(90deg, #58a6ff, #3fb950);
    border-radius: 0;                /* rely on parent clipping */
    transition: width 0.4s ease;
    z-index: 3;
    pointer-events: none;
    max-width: 100%;
    will-change: width;
}
.weather-widget .close-btn {
    position: absolute;
    top: 4px;
    right: 4px;
    background: rgba(0,0,0,0.35);
    border: none;
    color: rgba(255,255,255,0.7);
    cursor: pointer;
    font-size: 12px;
    padding: 2px 6px;
    border-radius: 20px;
    transition: background 0.2s;
    line-height: 1;
    z-index: 4;
}
.weather-widget .close-btn:hover {
    background: rgba(255,0,0,0.35);
    color: #fff;
}
.weather-widget .spinner {
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

/* Weather controls – placed inside top bar */
.weather-controls {
    display: flex;
    align-items: center;
    gap: 6px;
}
.weather-controls select {
    background: rgba(0,0,0,0.4);
    border: 1px solid rgba(255,255,255,0.15);
    color: #e1e4e8;
    border-radius: 40px;
    padding: 4px 12px 4px 16px;
    font-size: 13px;
    font-family: inherit;
    cursor: pointer;
    outline: none;
    max-width: 150px;
    appearance: none;
    -webkit-appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='white' stroke-width='1.5' fill='none'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 8px center;
    padding-right: 28px;
}
.weather-controls select option {
    background: #1a1a2e;
    color: #e1e4e8;
}
.weather-controls select:hover {
    border-color: rgba(255,255,255,0.3);
}
body.light-mode .weather-widget .toast {
    background: rgba(255,255,255,0.92);
    border-color: rgba(0,0,0,0.06);
    color: #24292f;
}
body.light-mode .weather-widget .toast-text .main { color: #24292f; }
body.light-mode .weather-widget .toast-text .main .highlight { color: #000; }
body.light-mode .weather-widget .toast-text .time-row .clock { color: #000; }
body.light-mode .weather-widget .toast-text .weather-row .temp { color: #000; }
body.light-mode .weather-controls select {
    background: rgba(255,255,255,0.8);
    color: #1a1a2e;
    border-color: rgba(0,0,0,0.15);
}
body.light-mode .weather-controls select option {
    background: #fff;
    color: #1a1a2e;
}

@media (max-width: 600px) {
    .weather-widget {
        top: 60px;
        right: 8px;
        max-width: 94vw;
    }
    .weather-widget .toast-scene {
        height: 70px;
    }
    .weather-widget .toast-content {
        padding: 6px 10px 8px 8px;
    }
    .weather-widget .toast-text .main { font-size: 11px; }
    .weather-widget .toast-text .weather-row .temp { font-size: 12px; }
    .weather-controls select {
        max-width: 120px;
        font-size: 12px;
        padding: 3px 24px 3px 10px;
    }
}
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
            <!-- Weather country selector -->
            <div class="weather-controls">
                <select id="countrySelect" aria-label="Select country"></select>
            </div>
            <!-- Weather toggle button -->
            <button class="weather-toggle-btn" id="weatherToggleBtn" onclick="toggleWeather()" title="Show/hide weather">🌤️</button>
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

<!-- ─── WEATHER WIDGET (fixed top‑right, compact) ─── -->
<div class="weather-widget" id="weatherWidget" style="display:block;">
    <div class="toast" id="weatherToast">
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
    el.querySelectorAll('.pin-content img').forEach(function(imgEl) {
        imgEl.style.cursor = 'zoom-in';
        imgEl.addEventListener('click', function(e) {
            e.stopPropagation();
            openPinImageViewer(imgEl.getAttribute('src'));
        });
    });
    el.dataset.lastModified = pin.last_modified || '';
}

function escapeHtml(text) {
    var d = document.createElement('div');
    d.textContent = text || '';
    return d.innerHTML;
}

// ─── Image Viewer for pin images – zoom-to-cursor, pinch, clamped pan ──
function collectPinImages() {
    return Object.values(boardData.pins)
        .filter(function(p) { return !!p.image_url; })
        .sort(function(a, b) { return (a.created || '').localeCompare(b.created || ''); })
        .map(function(p) { return { url: p.image_url, name: p.filename || p.title || 'image' }; });
}

function openPinImageViewer(imageUrl) {
    var images = collectPinImages();
    if (!images.length) return;
    var idx = images.findIndex(function(i) { return i.url === imageUrl; });
    if (idx === -1) idx = 0;
    pinImageViewer.open(images, idx);
}

var pinImageViewer = {
    images: [],
    currentIndex: 0,
    scale: 1,
    panX: 0,
    panY: 0,
    minScale: 1,
    maxScale: 8,
    isDragging: false,
    startX: 0, startY: 0,
    startPanX: 0, startPanY: 0,
    imgElement: null,
    container: null,
    counterElement: null,
    loaderElement: null,
    currentSrc: null,
    rafId: null,
    _preloadCache: {},
    _pinch: null,

    init: function() {
        if (!document.getElementById('pinImageViewer')) this.buildModal();
        this.imgElement = document.getElementById('pinViewerImage');
        this.container = document.getElementById('pinViewerContainer');
        this.counterElement = document.getElementById('pinViewerCounter');
        this.loaderElement = document.getElementById('pinViewerLoader');
        this.attachEvents();
        this.imgElement.style.willChange = 'transform';
    },

    buildModal: function() {
        var modal = document.createElement('div');
        modal.id = 'pinImageViewer';
        modal.style.cssText = 'display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.9); z-index:99999; backdrop-filter:blur(5px); align-items:center; justify-content:center; flex-direction:column; touch-action:none;';
        modal.innerHTML = `
            <div style="position:absolute; top:20px; right:30px; z-index:100000;">
                <button onclick="pinImageViewer.close()" style="background:none; border:none; color:#fff; font-size:32px; cursor:pointer;">✕</button>
            </div>
            <div style="position:absolute; top:20px; left:30px; z-index:100000; color:#fff; font-size:18px;" id="pinViewerCounter">1 / 1</div>
            <div style="position:absolute; top:56px; left:30px; z-index:100000; color:#ccc; font-size:13px;" id="pinViewerZoomLabel">100%</div>
            <div style="display:flex; align-items:center; justify-content:center; width:100%; height:calc(100% - 120px);">
                <button onclick="pinImageViewer.prev()" style="background:rgba(255,255,255,0.2); border:none; color:#fff; font-size:48px; padding:20px; border-radius:50%; cursor:pointer; margin:0 20px;">‹</button>
                <div style="position:relative; width:80%; height:100%; overflow:hidden; display:flex; align-items:center; justify-content:center;" id="pinViewerContainer">
                    <div id="pinViewerLoader" style="display:none; position:absolute; width:44px; height:44px; border:4px solid rgba(255,255,255,0.25); border-top-color:#fff; border-radius:50%; animation:pinViewerSpin 0.8s linear infinite;"></div>
                    <img id="pinViewerImage" src="" alt="Image" draggable="false" style="max-width:90%; max-height:90%; object-fit:contain; cursor:grab; transform-origin:center center; will-change:transform; backface-visibility:hidden; opacity:1; transition:opacity 0.15s ease;">
                </div>
                <button onclick="pinImageViewer.next()" style="background:rgba(255,255,255,0.2); border:none; color:#fff; font-size:48px; padding:20px; border-radius:50%; cursor:pointer; margin:0 20px;">›</button>
            </div>
            <div style="position:absolute; bottom:30px; left:50%; transform:translateX(-50%); display:flex; gap:20px; color:#fff; font-size:16px;">
                <button onclick="pinImageViewer.zoomIn()" style="background:rgba(255,255,255,0.2); border:none; color:#fff; padding:8px 16px; border-radius:8px; cursor:pointer;">🔍+</button>
                <button onclick="pinImageViewer.zoomOut()" style="background:rgba(255,255,255,0.2); border:none; color:#fff; padding:8px 16px; border-radius:8px; cursor:pointer;">🔍−</button>
                <button onclick="pinImageViewer.reset()" style="background:rgba(255,255,255,0.2); border:none; color:#fff; padding:8px 16px; border-radius:8px; cursor:pointer;">⟲ Reset</button>
                <button onclick="pinImageViewer.download()" style="background:rgba(255,255,255,0.2); border:none; color:#fff; padding:8px 16px; border-radius:8px; cursor:pointer;">⬇ Save</button>
            </div>
        `;
        document.body.appendChild(modal);

        if (!document.getElementById('pinViewerSpinStyle')) {
            var style = document.createElement('style');
            style.id = 'pinViewerSpinStyle';
            style.textContent = '@keyframes pinViewerSpin { to { transform: rotate(360deg); } }';
            document.head.appendChild(style);
        }
    },

    open: function(images, index) {
        this.images = images;
        this.currentIndex = index || 0;
        this._preloadCache = {};
        this.currentSrc = null;
        this._loadCurrent(true);
        document.getElementById('pinImageViewer').style.display = 'flex';
        document.body.style.overflow = 'hidden';
        document.addEventListener('keydown', this.keyHandler);
        this._preloadNeighbors();
    },

    close: function() {
        document.getElementById('pinImageViewer').style.display = 'none';
        document.body.style.overflow = '';
        document.removeEventListener('keydown', this.keyHandler);
        if (this.rafId) {
            cancelAnimationFrame(this.rafId);
            this.rafId = null;
        }
    },

    keyHandler: function(e) {
        if (e.key === 'Escape') pinImageViewer.close();
        else if (e.key === 'ArrowLeft') pinImageViewer.prev();
        else if (e.key === 'ArrowRight') pinImageViewer.next();
        else if (e.key === '+' || e.key === '=') pinImageViewer.zoomIn();
        else if (e.key === '-') pinImageViewer.zoomOut();
        else if (e.key === '0') pinImageViewer.reset();
    },

    // Pin images are plain URLs (not base64), unlike the chat viewer
    _srcFor: function(img) {
        return img.url;
    },

    _loadCurrent: function(resetView) {
        if (!this.images.length) return;
        var img = this.images[this.currentIndex];
        var newSrc = this._srcFor(img);
        var self = this;

        if (resetView) {
            this.scale = this.minScale;
            this.panX = 0;
            this.panY = 0;
        }
        this.counterElement.textContent = (this.currentIndex + 1) + ' / ' + this.images.length;

        if (newSrc === this.currentSrc) {
            this._applyTransform();
            return;
        }

        var cached = this._preloadCache[this.currentIndex];
        if (cached && cached.complete) {
            this._swapSrc(newSrc);
            return;
        }

        this.loaderElement.style.display = 'block';
        this.imgElement.style.opacity = '0';
        var loader = new Image();
        loader.onload = function() {
            if (self.images[self.currentIndex] !== img) return;
            self._preloadCache[self.currentIndex] = loader;
            self._swapSrc(newSrc);
        };
        loader.onerror = function() {
            self.loaderElement.style.display = 'none';
        };
        loader.src = newSrc;
    },

    _swapSrc: function(newSrc) {
        this.imgElement.src = newSrc;
        this.currentSrc = newSrc;
        this.loaderElement.style.display = 'none';
        this.imgElement.style.opacity = '1';
        this._applyTransform();
    },

    _preloadNeighbors: function() {
        var self = this;
        [this.currentIndex - 1, this.currentIndex + 1].forEach(function(i) {
            if (i < 0 || i >= self.images.length || self._preloadCache[i]) return;
            var img = new Image();
            img.src = self._srcFor(self.images[i]);
            self._preloadCache[i] = img;
        });
    },

    _clampPan: function() {
        if (!this.container) return;
        var rect = this.container.getBoundingClientRect();
        var imgRect = this.imgElement.getBoundingClientRect();
        if (!imgRect.width || !imgRect.height) return;
        var baseW = imgRect.width / this.scale;
        var baseH = imgRect.height / this.scale;
        var scaledW = baseW * this.scale;
        var scaledH = baseH * this.scale;
        var maxX = Math.max(0, (scaledW - rect.width) / 2);
        var maxY = Math.max(0, (scaledH - rect.height) / 2);
        this.panX = Math.max(-maxX, Math.min(maxX, this.panX));
        this.panY = Math.max(-maxY, Math.min(maxY, this.panY));
    },

    _applyTransform: function() {
        this._clampPan();
        this.imgElement.style.transform = 'translate3d(' + this.panX + 'px, ' + this.panY + 'px, 0) scale(' + this.scale + ')';
        var label = document.getElementById('pinViewerZoomLabel');
        if (label) label.textContent = Math.round(this.scale * 100) + '%';
    },

    update: function() {
        if (this.rafId) return;
        var self = this;
        this.rafId = requestAnimationFrame(function() {
            self._applyTransform();
            self.rafId = null;
        });
    },

    next: function() {
        if (this.currentIndex < this.images.length - 1) {
            this.currentIndex++;
            this._loadCurrent(true);
            this._preloadNeighbors();
        }
    },

    prev: function() {
        if (this.currentIndex > 0) {
            this.currentIndex--;
            this._loadCurrent(true);
            this._preloadNeighbors();
        }
    },

    zoomAt: function(clientX, clientY, newScale) {
        newScale = Math.max(this.minScale, Math.min(this.maxScale, newScale));
        var rect = this.container.getBoundingClientRect();
        var cx = clientX - (rect.left + rect.width / 2);
        var cy = clientY - (rect.top + rect.height / 2);
        var ratio = newScale / this.scale;
        this.panX = cx - (cx - this.panX) * ratio;
        this.panY = cy - (cy - this.panY) * ratio;
        this.scale = newScale;
        if (this.scale <= this.minScale) { this.panX = 0; this.panY = 0; }
        this.update();
    },

    _withTransition: function(fn) {
        var self = this;
        this.imgElement.style.transition = 'transform 0.15s ease, opacity 0.15s ease';
        fn();
        setTimeout(function() { self.imgElement.style.transition = 'opacity 0.15s ease'; }, 160);
    },

    zoomIn: function() {
        var rect = this.container.getBoundingClientRect();
        this._withTransition(function() {
            pinImageViewer.zoomAt(rect.left + rect.width / 2, rect.top + rect.height / 2, pinImageViewer.scale * 1.5);
        });
    },

    zoomOut: function() {
        var rect = this.container.getBoundingClientRect();
        this._withTransition(function() {
            pinImageViewer.zoomAt(rect.left + rect.width / 2, rect.top + rect.height / 2, pinImageViewer.scale / 1.5);
        });
    },

    reset: function() {
        this._withTransition(function() {
            pinImageViewer.scale = pinImageViewer.minScale;
            pinImageViewer.panX = 0;
            pinImageViewer.panY = 0;
            pinImageViewer.update();
        });
    },

    download: function() {
        if (!this.images.length) return;
        var img = this.images[this.currentIndex];
        var a = document.createElement('a');
        a.href = this._srcFor(img);
        a.download = img.name || 'image';
        a.target = '_blank';
        document.body.appendChild(a);
        a.click();
        a.remove();
    },

    attachEvents: function() {
        var self = this;
        var imgEl = this.imgElement;
        var container = this.container;

        imgEl.addEventListener('dblclick', function(e) {
            e.stopPropagation();
            self._withTransition(function() {
                if (self.scale <= self.minScale + 0.01) {
                    self.zoomAt(e.clientX, e.clientY, 2.5);
                } else {
                    self.scale = self.minScale;
                    self.panX = 0;
                    self.panY = 0;
                    self.update();
                }
            });
        });

        imgEl.addEventListener('mousedown', function(e) {
            if (self.scale <= self.minScale) return;
            self.isDragging = true;
            self.startX = e.clientX;
            self.startY = e.clientY;
            self.startPanX = self.panX;
            self.startPanY = self.panY;
            imgEl.style.transition = 'none';
            imgEl.style.cursor = 'grabbing';
            e.stopPropagation();
            e.preventDefault();
        });

        document.addEventListener('mousemove', function(e) {
            if (!self.isDragging) return;
            self.panX = self.startPanX + (e.clientX - self.startX);
            self.panY = self.startPanY + (e.clientY - self.startY);
            self.update();
            e.stopPropagation();
            e.preventDefault();
        });

        document.addEventListener('mouseup', function() {
            if (self.isDragging) {
                self.isDragging = false;
                imgEl.style.cursor = 'grab';
            }
        });

        var wheelTimeout = null;
        container.addEventListener('wheel', function(e) {
            e.preventDefault();
            e.stopPropagation();
            if (wheelTimeout) return;
            wheelTimeout = setTimeout(function() { wheelTimeout = null; }, 16);
            imgEl.style.transition = 'none';
            var factor = e.deltaY > 0 ? 0.9 : 1.1;
            self.zoomAt(e.clientX, e.clientY, self.scale * factor);
        }, { passive: false });

        container.addEventListener('touchstart', function(e) {
            imgEl.style.transition = 'none';
            if (e.touches.length === 1 && self.scale > self.minScale) {
                var t = e.touches[0];
                self.isDragging = true;
                self.startX = t.clientX;
                self.startY = t.clientY;
                self.startPanX = self.panX;
                self.startPanY = self.panY;
            } else if (e.touches.length === 2) {
                self.isDragging = false;
                var t0 = e.touches[0], t1 = e.touches[1];
                self._pinch = {
                    startDist: Math.hypot(t1.clientX - t0.clientX, t1.clientY - t0.clientY),
                    startScale: self.scale,
                    cx: (t0.clientX + t1.clientX) / 2,
                    cy: (t0.clientY + t1.clientY) / 2
                };
            }
        }, { passive: true });

        container.addEventListener('touchmove', function(e) {
            if (e.touches.length === 1 && self.isDragging) {
                e.preventDefault();
                var t = e.touches[0];
                self.panX = self.startPanX + (t.clientX - self.startX);
                self.panY = self.startPanY + (t.clientY - self.startY);
                self.update();
            } else if (e.touches.length === 2 && self._pinch) {
                e.preventDefault();
                var t0 = e.touches[0], t1 = e.touches[1];
                var dist = Math.hypot(t1.clientX - t0.clientX, t1.clientY - t0.clientY);
                var newScale = self._pinch.startScale * (dist / self._pinch.startDist);
                self.zoomAt(self._pinch.cx, self._pinch.cy, newScale);
            }
        }, { passive: false });

        container.addEventListener('touchend', function(e) {
            if (e.touches.length === 0) {
                self.isDragging = false;
                self._pinch = null;
            }
        }, { passive: true });
    }
};

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

// ─── WEATHER WIDGET ──────────────────────────────────────
// POLYFILL for roundRect
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

// Country data (simplified)
const countryList = [
    { code: 'AF', name: 'Afghanistan' }, { code: 'AL', name: 'Albania' }, { code: 'DZ', name: 'Algeria' },
    { code: 'AD', name: 'Andorra' }, { code: 'AO', name: 'Angola' }, { code: 'AG', name: 'Antigua and Barbuda' },
    { code: 'AR', name: 'Argentina' }, { code: 'AM', name: 'Armenia' }, { code: 'AU', name: 'Australia' },
    { code: 'AT', name: 'Austria' }, { code: 'AZ', name: 'Azerbaijan' }, { code: 'BS', name: 'Bahamas' },
    { code: 'BH', name: 'Bahrain' }, { code: 'BD', name: 'Bangladesh' }, { code: 'BB', name: 'Barbados' },
    { code: 'BY', name: 'Belarus' }, { code: 'BE', name: 'Belgium' }, { code: 'BZ', name: 'Belize' },
    { code: 'BJ', name: 'Benin' }, { code: 'BT', name: 'Bhutan' }, { code: 'BO', name: 'Bolivia' },
    { code: 'BA', name: 'Bosnia and Herzegovina' }, { code: 'BW', name: 'Botswana' }, { code: 'BR', name: 'Brazil' },
    { code: 'BN', name: 'Brunei' }, { code: 'BG', name: 'Bulgaria' }, { code: 'BF', name: 'Burkina Faso' },
    { code: 'BI', name: 'Burundi' }, { code: 'KH', name: 'Cambodia' }, { code: 'CM', name: 'Cameroon' },
    { code: 'CA', name: 'Canada' }, { code: 'CV', name: 'Cape Verde' }, { code: 'CF', name: 'Central African Republic' },
    { code: 'TD', name: 'Chad' }, { code: 'CL', name: 'Chile' }, { code: 'CN', name: 'China' },
    { code: 'CO', name: 'Colombia' }, { code: 'KM', name: 'Comoros' }, { code: 'CG', name: 'Congo' },
    { code: 'CD', name: 'DR Congo' }, { code: 'CR', name: 'Costa Rica' }, { code: 'HR', name: 'Croatia' },
    { code: 'CU', name: 'Cuba' }, { code: 'CY', name: 'Cyprus' }, { code: 'CZ', name: 'Czech Republic' },
    { code: 'DK', name: 'Denmark' }, { code: 'DJ', name: 'Djibouti' }, { code: 'DM', name: 'Dominica' },
    { code: 'DO', name: 'Dominican Republic' }, { code: 'EC', name: 'Ecuador' }, { code: 'EG', name: 'Egypt' },
    { code: 'SV', name: 'El Salvador' }, { code: 'GQ', name: 'Equatorial Guinea' }, { code: 'ER', name: 'Eritrea' },
    { code: 'EE', name: 'Estonia' }, { code: 'SZ', name: 'Eswatini' }, { code: 'ET', name: 'Ethiopia' },
    { code: 'FJ', name: 'Fiji' }, { code: 'FI', name: 'Finland' }, { code: 'FR', name: 'France' },
    { code: 'GA', name: 'Gabon' }, { code: 'GM', name: 'Gambia' }, { code: 'GE', name: 'Georgia' },
    { code: 'DE', name: 'Germany' }, { code: 'GH', name: 'Ghana' }, { code: 'GR', name: 'Greece' },
    { code: 'GD', name: 'Grenada' }, { code: 'GT', name: 'Guatemala' }, { code: 'GN', name: 'Guinea' },
    { code: 'GW', name: 'Guinea-Bissau' }, { code: 'GY', name: 'Guyana' }, { code: 'HT', name: 'Haiti' },
    { code: 'HN', name: 'Honduras' }, { code: 'HU', name: 'Hungary' }, { code: 'IS', name: 'Iceland' },
    { code: 'IN', name: 'India' }, { code: 'ID', name: 'Indonesia' }, { code: 'IR', name: 'Iran' },
    { code: 'IQ', name: 'Iraq' }, { code: 'IE', name: 'Ireland' }, { code: 'IL', name: 'Israel' },
    { code: 'IT', name: 'Italy' }, { code: 'JM', name: 'Jamaica' }, { code: 'JP', name: 'Japan' },
    { code: 'JO', name: 'Jordan' }, { code: 'KZ', name: 'Kazakhstan' }, { code: 'KE', name: 'Kenya' },
    { code: 'KI', name: 'Kiribati' }, { code: 'KP', name: 'North Korea' }, { code: 'KR', name: 'South Korea' },
    { code: 'KW', name: 'Kuwait' }, { code: 'KG', name: 'Kyrgyzstan' }, { code: 'LA', name: 'Laos' },
    { code: 'LV', name: 'Latvia' }, { code: 'LB', name: 'Lebanon' }, { code: 'LS', name: 'Lesotho' },
    { code: 'LR', name: 'Liberia' }, { code: 'LY', name: 'Libya' }, { code: 'LI', name: 'Liechtenstein' },
    { code: 'LT', name: 'Lithuania' }, { code: 'LU', name: 'Luxembourg' }, { code: 'MG', name: 'Madagascar' },
    { code: 'MW', name: 'Malawi' }, { code: 'MY', name: 'Malaysia' }, { code: 'MV', name: 'Maldives' },
    { code: 'ML', name: 'Mali' }, { code: 'MT', name: 'Malta' }, { code: 'MH', name: 'Marshall Islands' },
    { code: 'MR', name: 'Mauritania' }, { code: 'MU', name: 'Mauritius' }, { code: 'MX', name: 'Mexico' },
    { code: 'FM', name: 'Micronesia' }, { code: 'MD', name: 'Moldova' }, { code: 'MC', name: 'Monaco' },
    { code: 'MN', name: 'Mongolia' }, { code: 'ME', name: 'Montenegro' }, { code: 'MA', name: 'Morocco' },
    { code: 'MZ', name: 'Mozambique' }, { code: 'MM', name: 'Myanmar' }, { code: 'NA', name: 'Namibia' },
    { code: 'NR', name: 'Nauru' }, { code: 'NP', name: 'Nepal' }, { code: 'NL', name: 'Netherlands' },
    { code: 'NZ', name: 'New Zealand' }, { code: 'NI', name: 'Nicaragua' }, { code: 'NE', name: 'Niger' },
    { code: 'NG', name: 'Nigeria' }, { code: 'MK', name: 'North Macedonia' }, { code: 'NO', name: 'Norway' },
    { code: 'OM', name: 'Oman' }, { code: 'PK', name: 'Pakistan' }, { code: 'PW', name: 'Palau' },
    { code: 'PA', name: 'Panama' }, { code: 'PG', name: 'Papua New Guinea' }, { code: 'PY', name: 'Paraguay' },
    { code: 'PE', name: 'Peru' }, { code: 'PH', name: 'Philippines' }, { code: 'PL', name: 'Poland' },
    { code: 'PT', name: 'Portugal' }, { code: 'QA', name: 'Qatar' }, { code: 'RO', name: 'Romania' },
    { code: 'RU', name: 'Russia' }, { code: 'RW', name: 'Rwanda' }, { code: 'KN', name: 'Saint Kitts and Nevis' },
    { code: 'LC', name: 'Saint Lucia' }, { code: 'VC', name: 'Saint Vincent and the Grenadines' },
    { code: 'WS', name: 'Samoa' }, { code: 'SM', name: 'San Marino' }, { code: 'ST', name: 'Sao Tome and Principe' },
    { code: 'SA', name: 'Saudi Arabia' }, { code: 'SN', name: 'Senegal' }, { code: 'RS', name: 'Serbia' },
    { code: 'SC', name: 'Seychelles' }, { code: 'SL', name: 'Sierra Leone' }, { code: 'SG', name: 'Singapore' },
    { code: 'SK', name: 'Slovakia' }, { code: 'SI', name: 'Slovenia' }, { code: 'SB', name: 'Solomon Islands' },
    { code: 'SO', name: 'Somalia' }, { code: 'ZA', name: 'South Africa' }, { code: 'SS', name: 'South Sudan' },
    { code: 'ES', name: 'Spain' }, { code: 'LK', name: 'Sri Lanka' }, { code: 'SD', name: 'Sudan' },
    { code: 'SR', name: 'Suriname' }, { code: 'SE', name: 'Sweden' }, { code: 'CH', name: 'Switzerland' },
    { code: 'SY', name: 'Syria' }, { code: 'TW', name: 'Taiwan' }, { code: 'TJ', name: 'Tajikistan' },
    { code: 'TZ', name: 'Tanzania' }, { code: 'TH', name: 'Thailand' }, { code: 'TL', name: 'Timor-Leste' },
    { code: 'TG', name: 'Togo' }, { code: 'TO', name: 'Tonga' }, { code: 'TT', name: 'Trinidad and Tobago' },
    { code: 'TN', name: 'Tunisia' }, { code: 'TR', name: 'Turkey' }, { code: 'TM', name: 'Turkmenistan' },
    { code: 'TV', name: 'Tuvalu' }, { code: 'UG', name: 'Uganda' }, { code: 'UA', name: 'Ukraine' },
    { code: 'AE', name: 'United Arab Emirates' }, { code: 'GB', name: 'United Kingdom' },
    { code: 'US', name: 'United States' }, { code: 'UY', name: 'Uruguay' }, { code: 'UZ', name: 'Uzbekistan' },
    { code: 'VU', name: 'Vanuatu' }, { code: 'VA', name: 'Vatican City' }, { code: 'VE', name: 'Venezuela' },
    { code: 'VN', name: 'Vietnam' }, { code: 'YE', name: 'Yemen' }, { code: 'ZM', name: 'Zambia' },
    { code: 'ZW', name: 'Zimbabwe' }
];

const flagMap = {};
countryList.forEach(c => {
    const code = c.code.toUpperCase();
    const flag = String.fromCodePoint(0x1F1E6 + code.charCodeAt(0) - 65, 0x1F1E6 + code.charCodeAt(1) - 65);
    flagMap[c.code] = flag;
});

const countryLatMap = {
    'AF':33.9,'AL':41.2,'DZ':28.0,'AD':42.5,'AO':-12.5,'AG':17.1,'AR':-35.0,'AM':40.2,'AU':-25.0,'AT':47.5,
    'AZ':40.5,'BS':25.0,'BH':26.0,'BD':24.0,'BB':13.2,'BY':53.0,'BE':50.8,'BZ':17.2,'BJ':9.5,'BT':27.5,
    'BO':-17.0,'BA':44.0,'BW':-22.0,'BR':-14.0,'BN':4.5,'BG':42.7,'BF':12.0,'BI':-3.5,'KH':13.0,'CM':6.0,
    'CA':56.0,'CV':15.0,'CF':6.5,'TD':15.0,'CL':-30.0,'CN':35.0,'CO':4.0,'KM':-12.2,'CG':-1.0,'CD':-4.0,
    'CR':10.0,'HR':45.2,'CU':22.0,'CY':35.0,'CZ':49.8,'DK':56.0,'DJ':11.8,'DM':15.4,'DO':18.7,'EC':-2.0,
    'EG':26.0,'SV':13.8,'GQ':1.5,'ER':15.0,'EE':58.5,'SZ':-26.5,'ET':8.0,'FJ':-17.0,'FI':63.0,'FR':46.6,
    'GA':-1.0,'GM':13.5,'GE':42.0,'DE':51.0,'GH':7.8,'GR':38.0,'GD':12.1,'GT':15.8,'GN':10.0,'GW':12.0,
    'GY':5.0,'HT':19.0,'HN':14.0,'HU':47.0,'IS':65.0,'IN':20.0,'ID':-5.0,'IR':32.0,'IQ':33.0,'IE':53.0,
    'IL':31.5,'IT':42.0,'JM':18.1,'JP':36.0,'JO':31.0,'KZ':48.0,'KE':0.0,'KI':1.0,'KP':40.0,'KR':36.5,
    'KW':29.5,'KG':41.5,'LA':18.0,'LV':57.0,'LB':34.0,'LS':-29.5,'LR':6.5,'LY':26.0,'LI':47.2,'LT':55.0,
    'LU':49.8,'MG':-19.0,'MW':-13.5,'MY':2.5,'MV':3.2,'ML':17.0,'MT':35.9,'MH':7.0,'MR':20.0,'MU':-20.2,
    'MX':23.0,'FM':7.0,'MD':47.0,'MC':43.7,'MN':46.0,'ME':42.5,'MA':31.0,'MZ':-18.0,'MM':22.0,'NA':-22.0,
    'NR':-0.5,'NP':28.0,'NL':52.3,'NZ':-41.0,'NI':13.0,'NE':17.0,'NG':9.0,'MK':41.6,'NO':61.0,'OM':21.0,
    'PK':30.0,'PW':7.5,'PA':8.5,'PG':-6.0,'PY':-23.0,'PE':-9.0,'PH':13.0,'PL':52.0,'PT':39.5,'QA':25.5,
    'RO':46.0,'RU':61.0,'RW':-2.0,'KN':17.3,'LC':13.9,'VC':13.2,'WS':-13.5,'SM':43.9,'ST':0.2,'SA':24.0,
    'SN':14.0,'RS':44.0,'SC':-4.6,'SL':8.5,'SG':1.3,'SK':48.7,'SI':46.0,'SB':-9.0,'SO':6.0,'ZA':-30.0,
    'SS':7.0,'ES':40.0,'LK':7.5,'SD':15.0,'SR':4.0,'SE':62.0,'CH':46.8,'SY':35.0,'TW':23.5,'TJ':39.0,
    'TZ':-6.0,'TH':14.0,'TL':-8.9,'TG':8.5,'TO':-21.0,'TT':10.5,'TN':34.0,'TR':39.0,'TM':40.0,'TV':-7.0,
    'UG':1.0,'UA':49.0,'AE':24.0,'GB':54.0,'US':38.0,'UY':-33.0,'UZ':41.0,'VU':-16.0,'VA':41.9,'VE':7.0,
    'VN':16.0,'YE':15.5,'ZM':-14.0,'ZW':-19.0
};

// Weather widget state
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

let blobInstances = [];
let canvasAnimId = null;
let toastTimer = null;
let activeToast = null; // we'll use the widget directly

// DOM references for weather
const container = document.getElementById('weatherWidget');
const canvas = document.getElementById('sceneCanvas');
const ctx = canvas.getContext('2d');
const toast = document.getElementById('weatherToast');
const iconEl = document.getElementById('toastIcon');
const mainEl = document.getElementById('toastMain');
const subEl = document.getElementById('toastSub');
const progressEl = document.getElementById('toastProgress');
const weatherRow = document.getElementById('weatherRow');
const weatherEmoji = document.getElementById('weatherEmoji');
const weatherTemp = document.getElementById('weatherTemp');
const weatherCondition = document.getElementById('weatherCondition');
const fetchStatus = document.getElementById('fetchStatus');
const clockText = document.getElementById('clockText');
const dateText = document.getElementById('dateText');
const closeBtn = document.getElementById('closeToastBtn');
const countrySelect = document.getElementById('countrySelect');

// ─── Toggle weather visibility ─────────────────────
function toggleWeather() {
    if (container.style.display === 'none') {
        container.style.display = 'block';
        // If no active data, re-detect location
        if (!currentCountryCode) {
            detectLocation();
        } else {
            // Re-show the toast (if hidden)
            toast.style.display = 'flex';
            // Optionally refresh weather
            updateFromCountry(currentCountryCode);
        }
    } else {
        container.style.display = 'none';
        if (activeToast) activeToast.close();
    }
}

// ─── Clock update ──────────────────────────────────
function startClock() {
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
        clockText.textContent = timeStr;
        dateText.textContent = dateStr;
    };
    tick();
    setInterval(tick, 1000);
}
startClock();

// ─── Update widget UI ──────────────────────────────
function updateWidget(mainText, subText = '', iconHtml = null, progress = null) {
    mainEl.textContent = mainText;
    if (subText) {
        subEl.textContent = subText;
        subEl.style.opacity = '1';
    } else {
        subEl.style.opacity = '0';
    }
    if (iconHtml !== null) iconEl.innerHTML = iconHtml;
    // Update progress bar – hide when not active
    if (progress !== null && progress > 0) {
        progressEl.style.width = Math.min(progress, 100) + '%';
        progressEl.style.display = 'block';
    } else {
        progressEl.style.width = '0%';
        progressEl.style.display = 'none';
    }
}

function updateWeather(tempC, condition, emoji) {
    weatherRow.style.display = 'flex';
    weatherTemp.textContent = `${Math.round(tempC)}°C`;
    weatherCondition.textContent = condition || '';
    weatherEmoji.textContent = emoji || '🌤️';
}

function setStatus(text) {
    fetchStatus.textContent = text;
}

// ─── Canvas sizing ──────────────────────────────────
function sizeCanvas() {
    const rect = canvas.parentElement.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const cssW = rect.width, cssH = rect.height;
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    canvas.style.width = cssW + 'px';
    canvas.style.height = cssH + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { w: cssW, h: cssH };
}

// ─── Blob drawing (simplified) ─────────────────────
function createBlob(x, y, emoji, color, speed = 1.0, size = 22) {
    return {
        x, y, baseY: y, emoji, color, speed: speed * (0.6 + Math.random() * 0.5), size,
        direction: Math.random() > 0.5 ? 1 : -1,
        stepPhase: Math.random() * Math.PI * 2,
        walkCycle: Math.random() * 100,
        armSwing: 0, legOffset: 0,
        pauseTimer: 0, isPaused: false, pauseDuration: 0,
        eyeColor: '#2b2b2b', blush: true,
        hasDrink: Math.random() > 0.7,
        isDrinking: false, drinkTimer: 0, drinkCooldown: 100 + Math.random() * 200, drinkProgress: 0
    };
}

function updateBlob(blob, w, h, t, speedMul = 1) {
    if (!blob) return;
    const spd = blob.speed * speedMul * 0.6;
    if (blob.hasDrink && blob.isDrinking) {
        blob.drinkTimer += 1;
        blob.drinkProgress = Math.min(1, blob.drinkTimer / 30);
        if (blob.drinkTimer > 70) {
            blob.isDrinking = false;
            blob.drinkTimer = 0;
            blob.drinkProgress = 0;
            blob.drinkCooldown = 150 + Math.random() * 250;
        }
        return;
    }
    if (blob.hasDrink && !blob.isDrinking) {
        blob.drinkCooldown -= 1;
        if (blob.drinkCooldown <= 0) {
            blob.isDrinking = true;
            blob.isPaused = false;
            blob.drinkTimer = 0;
            blob.drinkProgress = 0;
            return;
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
    const margin = 30 + blob.size;
    if (blob.x > w - margin) { blob.direction = -1; blob.x = w - margin; }
    if (blob.x < margin) { blob.direction = 1; blob.x = margin; }
}

function drawWalkingBlob(ctx, blob, t) {
    const { x, y, size: r, color, emoji, direction, walkCycle, isDrinking } = blob;
    const d = direction;
    const drinking = isDrinking;
    const lift = drinking ? Math.min(1, blob.drinkProgress * 1.3) : 0;

    ctx.save();
    ctx.translate(x, y);

    // shadow
    ctx.fillStyle = 'rgba(0,0,0,0.10)';
    ctx.beginPath();
    ctx.ellipse(0, r * 1.15, r * 0.8, r * 0.25, 0, 0, Math.PI * 2);
    ctx.fill();

    // legs
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

    // feet
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.ellipse(lx2, ly2 + legThick*0.5, r*0.2, r*0.12, 0, 0, Math.PI*2);
    ctx.fill();
    ctx.beginPath();
    ctx.ellipse(rx2, ry2 + legThick*0.5, r*0.2, r*0.12, 0, 0, Math.PI*2);
    ctx.fill();

    // body
    ctx.fillStyle = color;
    ctx.shadowBlur = 10;
    ctx.shadowOffsetY = 3;
    ctx.beginPath();
    ctx.ellipse(0, 0, r, r * 1.05 * (1 + Math.sin(walkCycle)*0.04), 0, 0, Math.PI*2);
    ctx.fill();

    // highlight
    ctx.shadowBlur = 0;
    ctx.fillStyle = 'rgba(255,255,255,0.20)';
    ctx.beginPath();
    ctx.ellipse(-r*0.3*d, -r*0.35, r*0.25, r*0.15, -0.3*d, 0, Math.PI*2);
    ctx.fill();

    // arms
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

    // drink can
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

    // eyes
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

    // blush
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

    // mouth
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

    // emoji
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

// ─── Season drawing functions ──────────────────────
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

// ─── Drawing helpers ──────────────────────────────
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

// ─── Render scene ──────────────────────────────────
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

// ─── Animation loop ─────────────────────────────────
function animateScene() {
    if (canvasAnimId) cancelAnimationFrame(canvasAnimId);
    if (!canvas) return;
    let { w, h } = sizeCanvas();
    const onResize = () => {
        if (canvas && document.body.contains(canvas)) {
            ({ w, h } = sizeCanvas());
            blobInstances.forEach(b => {
                b.baseY = h*0.70 + (b.baseY % (h*0.12));
                b.y = b.baseY;
            });
        }
    };
    window.addEventListener('resize', onResize);

    let start = performance.now();
    let season = 'summer'; // will be updated
    let lastRenderTime = 0;

    function frame(now) {
        if (!canvas || !document.body.contains(canvas)) {
            cancelAnimationFrame(canvasAnimId);
            window.removeEventListener('resize', onResize);
            return;
        }
        // decorative scene, cap at ~30fps instead of 60fps to save main-thread work
        if (now - lastRenderTime < 32) {
            canvasAnimId = requestAnimationFrame(frame);
            return;
        }
        lastRenderTime = now;
        const t = (now - start) * 0.6;
        renderScene(ctx, w, h, season, t);
        canvasAnimId = requestAnimationFrame(frame);
    }
    frame(start);
    window._setSeason = (s) => { season = s; };
}

// ─── Weather API helpers ────────────────────────────
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
        0:'Clear sky',1:'Mainly clear',2:'Partly cloudy',3:'Overcast',
        45:'Fog',48:'Rime fog',51:'Light drizzle',53:'Drizzle',55:'Dense drizzle',
        56:'Freezing drizzle',57:'Freezing drizzle',61:'Slight rain',63:'Rain',65:'Heavy rain',
        66:'Freezing rain',67:'Freezing rain',71:'Slight snow',73:'Snow',75:'Heavy snow',
        77:'Snow grains',80:'Rain showers',81:'Rain showers',82:'Violent showers',
        85:'Snow showers',86:'Snow showers',95:'Thunderstorm',96:'Thunderstorm',99:'Thunderstorm'
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

// ─── Show weather widget ────────────────────────────
async function showWeatherWidget(season, city, country, code, region, lat, manual = false) {
    const flag = getFlagFromCode(code) || '🌍';
    // Avoid duplication: if country is already in city, skip it
    let locationDisplay = city;
    if (region && region !== city && region !== 'Unknown') {
        locationDisplay += `, ${region}`;
    }
    const mainText = `${flag} ${locationDisplay}`;
    const subText = `${season === 'spring' ? '🌸' : season === 'summer' ? '☀️' : season === 'autumn' ? '🍂' : '❄️'} ${season.charAt(0).toUpperCase() + season.slice(1)}`;
    updateWidget(mainText, subText, subText.split(' ')[0], 80);

    if (currentTemp !== null && currentTemp !== undefined) {
        updateWeather(currentTemp, currentCondition || '', currentWeatherEmoji || '🌤️');
        setStatus('✅ Weather loaded');
    } else {
        setStatus('⏳ Loading weather...');
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
                updateWeather(currentTemp, currentCondition, currentWeatherEmoji);
                setStatus('✅ Weather loaded');
            } else {
                setStatus('⚠️ Weather unavailable');
            }
        } catch (e) {
            setStatus('⚠️ Weather unavailable');
        }
    }

    if (window._setSeason) window._setSeason(season);

    if (code) {
        const option = countrySelect.querySelector(`option[value="${code}"]`);
        if (option) countrySelect.value = code;
    }
}

// ─── Update from country select ─────────────────────
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
    await showWeatherWidget(season, city, countryName, code, region, lat, true);
}

// ─── Exact location (browser geolocation + reverse geocoding) ─────
function getBrowserLocation() {
    return new Promise((resolve) => {
        if (!navigator.geolocation) { resolve(null); return; }
        navigator.geolocation.getCurrentPosition(
            (pos) => resolve({ lat: pos.coords.latitude, lon: pos.coords.longitude }),
            () => resolve(null),                       // denied / unavailable / timeout
            { enableHighAccuracy: true, timeout: 8000, maximumAge: 300000 }
        );
    });
}

async function reverseGeocode(lat, lon) {
    try {
        const url = `https://api.bigdatacloud.net/data/reverse-geocode-client?latitude=${lat}&longitude=${lon}&localityLanguage=en`;
        const res = await fetch(url);
        if (!res.ok) throw new Error('Reverse geocode error');
        const data = await res.json();
        return {
            countryCode: data.countryCode || null,
            countryName: data.countryName || null,
            city: data.city || data.locality || null,
            region: data.principalSubdivision || ''
        };
    } catch (e) { return null; }
}

async function useExactLocation(lat, lon) {
    const geo = await reverseGeocode(lat, lon);
    const code = (geo && geo.countryCode) || null;
    const country = code ? countryList.find(c => c.code === code) : null;

    currentCountryCode = code || currentCountryCode;
    currentCountry = (geo && geo.countryName) || (country ? country.name : currentCountry);
    currentCity = (geo && geo.city) || currentCity || currentCountry;
    currentRegion = (geo && geo.region) || '';
    currentFlag = getFlagFromCode(currentCountryCode);
    currentLat = lat;
    currentLon = lon;

    const wData = await fetchWeather(lat, lon);
    if (wData && wData.tempC !== null) {
        currentTemp = wData.tempC;
        currentCondition = wData.condition || '';
        currentWeatherEmoji = wData.emoji || '🌤️';
        currentTimezone = wData.timezone || currentTimezone;
    }
    const season = getSeasonForCountry(currentCountry, currentCountryCode, currentLat);
    blobInstances = [];
    await showWeatherWidget(season, currentCity, currentCountry, currentCountryCode, currentRegion, currentLat, true);
}

// ─── Detect location ────────────────────────────────
async function detectLocation() {
    // 1) Prefer the browser's exact location (GPS / Wi-Fi positioning)
    const exact = await getBrowserLocation();
    if (exact) {
        try {
            await useExactLocation(exact.lat, exact.lon);
            return;
        } catch (e) { console.warn('Exact-location lookup failed:', e); }
    }

    // 2) Fall back to coarse IP-based geolocation
    try {
        const res = await fetch('https://ip-api.com/json/');
        if (!res.ok) throw new Error('IP API error');
        const data = await res.json();
        if (data.status === 'success') {
            const code = data.countryCode;
            const country = countryList.find(c => c.code === code);
            if (country) {
                currentCountryCode = code;
                currentCountry = country.name;
                currentCity = data.city || country.name;
                currentRegion = data.regionName || '';
                currentFlag = getFlagFromCode(code);
                currentLat = data.lat || 0;
                currentLon = data.lon || 0;
                if (!currentLat || Math.abs(currentLat) < 0.01) {
                    currentLat = countryLatMap[code] || 30;
                }
                const wData = await fetchWeather(data.lat || currentLat, data.lon || 0);
                if (wData && wData.tempC !== null) {
                    currentTemp = wData.tempC;
                    currentCondition = wData.condition || '';
                    currentWeatherEmoji = wData.emoji || '🌤️';
                    if (wData.lat) currentLat = wData.lat;
                    if (wData.lon) currentLon = wData.lon;
                }
                currentTimezone = (wData && wData.timezone) || data.timezone || currentTimezone;

                const season = getSeasonForCountry(currentCountry, currentCountryCode, currentLat);
                blobInstances = [];
                await showWeatherWidget(season, currentCity, currentCountry, currentCountryCode, currentRegion, currentLat, true);
                return;
            }
        }
    } catch (e) {
        console.warn('IP geolocation failed:', e);
    }

    // 3) Last resort: default to Malaysia instead of the US
    const fallbackCode = 'MY';
    await updateFromCountry(fallbackCode);
}

// ─── Init weather widget ────────────────────────────
function initWeatherWidget() {
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

    closeBtn.addEventListener('click', function() {
        // Hide the toast but keep container visible – user can toggle back
        toast.style.display = 'none';
        if (activeToast) activeToast.close();
    });

    animateScene();
    detectLocation();
}

// ─── INIT ────────────────────────────────────────────
window.addEventListener('load', function() {
    loadBoard();
    pinImageViewer.init();
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

    // Init weather widget after board loads
    initWeatherWidget();
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