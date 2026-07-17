# notes.py – full Obsidian‑style knowledge management
# STORAGE: SQLite (sqlite_data/notes.db) + JSON backup (json_configuration/notes.json)
# FEATURES: Markdown, tags, pinning, colours, AI assistance, semantic search,
#           bidirectional [[wiki links]], ![[embeds]], backlinks, graph view.
# NEW: Obsidian vault sync (import/export .md files with frontmatter)
# OPTIMISED: in‑memory cache, async debounced JSON writes, no redundant DB queries.

import os
import json as std_json
import sqlite3
import threading
import uuid
import random
import re
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

# ---------- Obsidian sync imports ----------
import yaml
import frontmatter
from pathlib import Path

# ======================================================================
# SQLite storage layer
# ======================================================================
DB_DIR = "sqlite_data"
DB_PATH = os.path.join(DB_DIR, "notes.db")
os.makedirs(DB_DIR, exist_ok=True)

# JSON backup path
NOTES_JSON = "json_configuration/notes.json"
os.makedirs(os.path.dirname(NOTES_JSON), exist_ok=True)

# Sync config
SYNC_CONFIG_PATH = "json_configuration/sync_config.json"
os.makedirs(os.path.dirname(SYNC_CONFIG_PATH), exist_ok=True)

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

        -- OBSIDIAN-STYLE LINK TABLE
        CREATE TABLE IF NOT EXISTS note_links (
            from_note_id TEXT NOT NULL,
            to_note_id   TEXT NOT NULL,
            link_type    TEXT NOT NULL DEFAULT 'wiki',  -- 'wiki' or 'embed'
            PRIMARY KEY (from_note_id, to_note_id, link_type)
        );
        CREATE INDEX IF NOT EXISTS idx_note_links_from ON note_links(from_note_id);
        CREATE INDEX IF NOT EXISTS idx_note_links_to ON note_links(to_note_id);
    """)
    conn.commit()
    print("✅ SQLite notes + links table ready.")

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
    """Insert or replace a note, updating both DB and in‑memory cache.
       JSON backup is scheduled asynchronously.
    """
    global _notes_cache
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

    # Update cache immediately
    if _notes_cache is None:
        load_notes()   # ensure cache exists
    with _cache_lock:
        # Build a note dict from the row values
        note = {
            "id": vals["id"],
            "title": vals["title"],
            "content": vals["content"],
            "created": vals["created"],
            "last_modified": vals["last_modified"],
            "order": vals["order_idx"],
            "pinned": bool(vals["pinned"]),
            "color": vals["color"],
            "tags": json_loads(vals["tags"]),
            "embedding": json_loads(vals["embedding"]) if vals["embedding"] else None,
        }
        _notes_cache[note_id] = note

    _schedule_backup()

def get_note_from_db(note_id):
    # Try cache first
    if _notes_cache is not None and note_id in _notes_cache:
        return _notes_cache[note_id]
    # Fallback to DB if not in cache
    conn = get_conn()
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    return _note_row_to_dict(row) if row else None

def get_all_notes():
    """Read all notes from SQLite (used only for initial load)."""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM notes ORDER BY order_idx, created").fetchall()
    notes = {}
    for row in rows:
        n = _note_row_to_dict(row)
        notes[n["id"]] = n
    return notes

def delete_note_from_db(note_id):
    """Delete from DB and cache, schedule backup."""
    global _notes_cache
    with _write_lock:
        conn = get_conn()
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        conn.execute("DELETE FROM note_links WHERE from_note_id = ? OR to_note_id = ?", (note_id, note_id))
        conn.commit()
    if _notes_cache is not None and note_id in _notes_cache:
        with _cache_lock:
            del _notes_cache[note_id]
    _schedule_backup()

def clear_all_notes_db():
    """Clear all notes and links, update cache."""
    global _notes_cache
    with _write_lock:
        conn = get_conn()
        conn.execute("DELETE FROM notes")
        conn.execute("DELETE FROM note_links")
        conn.commit()
    with _cache_lock:
        _notes_cache = {}
    _schedule_backup()

def set_note_embedding_db(note_id, embedding):
    """Update embedding in DB and cache."""
    global _notes_cache
    with _write_lock:
        conn = get_conn()
        conn.execute("UPDATE notes SET embedding = ? WHERE id = ?",
                     (json_dumps(embedding) if embedding is not None else None, note_id))
        conn.commit()
    if _notes_cache is not None and note_id in _notes_cache:
        with _cache_lock:
            _notes_cache[note_id]["embedding"] = embedding
    _schedule_backup()

def update_note_fields_db(note_id, fields):
    """Partial update of note fields, update cache."""
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

    # Update cache
    if _notes_cache is not None and note_id in _notes_cache:
        with _cache_lock:
            note = _notes_cache[note_id]
            for k, v in fields.items():
                if k == "tags":
                    note["tags"] = json_loads(v)
                elif k == "pinned":
                    note["pinned"] = bool(v)
                elif k == "order_idx":
                    note["order"] = v
                elif k == "last_modified":
                    note["last_modified"] = v
                else:
                    note[k] = v
            note["embedding"] = None   # embedding invalidated
    _schedule_backup()

# ---------- JSON backup (async, debounced) ----------
_backup_timer = None
_backup_lock = threading.Lock()
BACKUP_DELAY = 1.0  # seconds

def _schedule_backup():
    """Schedule an asynchronous JSON backup after a short delay."""
    global _backup_timer
    with _backup_lock:
        if _backup_timer is not None:
            _backup_timer.cancel()
        _backup_timer = threading.Timer(BACKUP_DELAY, _write_backup_task)
        _backup_timer.daemon = True
        _backup_timer.start()

def _write_backup_task():
    """Write the current cache to JSON (runs in a background thread)."""
    global _backup_timer
    with _backup_lock:
        _backup_timer = None
    # Use the cache directly – no DB query
    try:
        with open(NOTES_JSON, "w", encoding="utf-8") as f:
            std_json.dump(_notes_cache, f, indent=2)
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
            upsert_note(note_id, note, created=note.get("created"))
        print(f"✅ Migrated {len(data)} notes from JSON to SQLite")
    except Exception as e:
        print(f"❌ Migration failed: {e}")

# ---------- In‑memory cache ----------
_notes_cache = None
_cache_lock = threading.Lock()

def load_notes():
    """Load notes into cache from SQLite once, then always return the cache."""
    global _notes_cache
    if _notes_cache is not None:
        return _notes_cache
    with _cache_lock:
        if _notes_cache is not None:
            return _notes_cache
        notes = get_all_notes()
        if not notes and os.path.exists(NOTES_JSON):
            try:
                with open(NOTES_JSON, "r", encoding="utf-8") as f:
                    notes = json_loads(f.read())
                # Import JSON into SQLite
                for nid, n in notes.items():
                    upsert_note(nid, n, created=n.get("created"))
                print("ℹ️ Loaded notes from JSON backup into SQLite")
            except:
                pass
        _notes_cache = notes
        return _notes_cache

def save_notes_async(notes_data=None):
    """Legacy: schedule a backup."""
    _schedule_backup()

def save_notes_sync(notes_data=None):
    """Legacy: force a synchronous backup (rarely used)."""
    _write_backup_task()

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

# ======================================================================
# OBSIDIAN‑STYLE WIKI LINKS & BACKLINKS
# ======================================================================
WIKI_LINK_RE = re.compile(r'\[\[([^\]]+)\]\]')
EMBED_LINK_RE = re.compile(r'!\[\[([^\]]+)\]\]')

def resolve_note_by_title(title):
    """Case‑insensitive lookup of a note ID by title."""
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM notes WHERE LOWER(title) = LOWER(?)",
        (title.strip(),)
    ).fetchone()
    return row[0] if row else None

def update_note_links(note_id: str, content: str):
    """Parse [[Title]] and ![[Title]] from content and update note_links table."""
    wiki_targets = set(WIKI_LINK_RE.findall(content))
    embed_targets = set(EMBED_LINK_RE.findall(content))
    
    conn = get_conn()
    conn.execute("DELETE FROM note_links WHERE from_note_id = ?", (note_id,))
    
    for title in wiki_targets:
        target_id = resolve_note_by_title(title)
        if target_id and target_id != note_id:
            conn.execute(
                "INSERT OR IGNORE INTO note_links (from_note_id, to_note_id, link_type) VALUES (?, ?, 'wiki')",
                (note_id, target_id)
            )
    for title in embed_targets:
        target_id = resolve_note_by_title(title)
        if target_id and target_id != note_id:
            conn.execute(
                "INSERT OR IGNORE INTO note_links (from_note_id, to_note_id, link_type) VALUES (?, ?, 'embed')",
                (note_id, target_id)
            )
    conn.commit()

def get_backlinks(note_id: str):
    """Return all notes that link to this one."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT n.id, n.title, nl.link_type
        FROM note_links nl
        JOIN notes n ON n.id = nl.from_note_id
        WHERE nl.to_note_id = ?
        ORDER BY n.title
    """, (note_id,)).fetchall()
    return [dict(row) for row in rows]

def get_graph_data():
    """Fetch all notes and links for the graph view."""
    conn = get_conn()
    nodes = conn.execute("SELECT id, title FROM notes ORDER BY title").fetchall()
    edges = conn.execute("""
        SELECT from_note_id AS 'from', to_note_id AS 'to', link_type
        FROM note_links
    """).fetchall()
    return {
        "nodes": [{"id": row[0], "label": row[1]} for row in nodes],
        "edges": [{"from": row[0], "to": row[1], "title": row[2]} for row in edges]
    }

# ======================================================================
# OBSIDIAN VAULT SYNC (import/export) – IMPROVED
# ======================================================================

def get_vault_path():
    """Return the Obsidian vault path from config, or None."""
    try:
        with open(SYNC_CONFIG_PATH, "r") as f:
            config = std_json.load(f)
        return config.get("vault_path")
    except:
        return None

def set_vault_path(path):
    """Save vault path to config."""
    with open(SYNC_CONFIG_PATH, "w") as f:
        std_json.dump({"vault_path": path}, f, indent=2)

def import_from_obsidian(vault_path=None):
    """Import all .md files from vault_path into the app."""
    if vault_path is None:
        vault_path = get_vault_path()
    if not vault_path or not os.path.isdir(vault_path):
        return {"error": f"Invalid vault path: {vault_path}"}

    print(f"🔍 Scanning vault: {vault_path}")
    vault_path = os.path.normpath(vault_path)
    md_files = list(Path(vault_path).glob("*.md"))
    print(f"📄 Found {len(md_files)} .md files")

    imported = 0
    skipped = 0
    errors = []

    for md_file in md_files:
        try:
            with open(md_file, "r", encoding="utf-8") as f:
                raw = f.read()
            try:
                post = frontmatter.loads(raw)
                title = post.get("title", md_file.stem)
                content = post.content
                tags = post.get("tags", [])
                if isinstance(tags, str):
                    tags = [t.strip() for t in tags.split(",") if t.strip()]
                pinned = post.get("pinned", False)
                color = post.get("color", "default")
            except Exception:
                title = md_file.stem
                content = raw
                tags = []
                pinned = False
                color = "default"

            last_mod = datetime.fromtimestamp(md_file.stat().st_mtime).isoformat()
            conn = get_conn()
            row = conn.execute("SELECT id FROM notes WHERE LOWER(title) = LOWER(?)", (title,)).fetchone()
            if row:
                note_id = row[0]
                existing = get_note_from_db(note_id)
                if existing and existing.get("last_modified", "") >= last_mod:
                    skipped += 1
                    continue
                upsert_note(note_id, {
                    "title": title,
                    "content": content,
                    "tags": tags,
                    "pinned": pinned,
                    "color": color,
                    "last_modified": last_mod,
                })
                imported += 1
            else:
                note_id = str(uuid.uuid4())
                upsert_note(note_id, {
                    "title": title,
                    "content": content,
                    "tags": tags,
                    "pinned": pinned,
                    "color": color,
                    "created": last_mod,
                    "last_modified": last_mod,
                })
                imported += 1
        except Exception as e:
            errors.append(str(e))

    # Refresh cache after import
    global _notes_cache
    with _cache_lock:
        _notes_cache = None
    load_notes()   # reload from DB

    return {
        "imported": imported,
        "skipped": skipped,
        "errors": errors,
        "total_files": len(md_files)
    }

def export_to_obsidian(vault_path=None):
    """Export all app notes to markdown files in the vault."""
    if vault_path is None:
        vault_path = get_vault_path()
    if not vault_path or not os.path.isdir(vault_path):
        return {"error": "Invalid vault path"}

    notes = load_notes()   # use cache
    exported = 0
    for note_id, note in notes.items():
        title = note.get("title", "Untitled")
        safe_title = "".join(c for c in title if c.isalnum() or c in " _-").strip()
        if not safe_title:
            safe_title = note_id
        file_path = Path(vault_path) / (safe_title + ".md")
        frontmatter_dict = {
            "title": title,
            "tags": note.get("tags", []),
            "pinned": note.get("pinned", False),
            "color": note.get("color", "default"),
            "created": note.get("created"),
            "last_modified": note.get("last_modified"),
        }
        frontmatter_dict = {k:v for k,v in frontmatter_dict.items() if v is not None}
        content = note.get("content", "")
        post = frontmatter.Post(content, **frontmatter_dict)
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
            exported += 1
        except Exception as e:
            print(f"⚠️ Failed to export {file_path}: {e}")
    return {"exported": exported}

# ======================================================================
# Flask Blueprint
# ======================================================================
notes_bp = Blueprint('notes', __name__, url_prefix='/notes')

# Serve the notes HTML page
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
    content = data.get("content", "")
    note = {
        "id": note_id,
        "title": data.get("title", "Untitled"),
        "content": content,
        "created": now,
        "last_modified": now,
        "order": len(load_notes()),
        "tags": data.get("tags", []),
        "pinned": data.get("pinned", False),
        "color": data.get("color", "default")
    }
    upsert_note(note_id, note, created=now)
    update_note_links(note_id, content)
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
        if "content" in fields:
            update_note_links(note_id, fields["content"])
    return jsonify({"ok": True})

@notes_bp.route('/api/<note_id>', methods=['DELETE'])
def delete_note(note_id):
    note = get_note_from_db(note_id)
    if not note:
        return jsonify({"error": "Note not found"}), 404
    delete_note_from_db(note_id)
    return jsonify({"ok": True})

# ---------- Keyword search ----------
@notes_bp.route('/api/search', methods=['GET'])
def search_notes():
    query = request.args.get('q', '').strip().lower()
    tag = request.args.get('tag', '').strip().lower()
    notes = load_notes()   # fast cache
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
    return jsonify({'ok': True})

# ---------- OBSIDIAN API ----------
@notes_bp.route('/api/backlinks/<note_id>', methods=['GET'])
def backlinks_api(note_id):
    return jsonify(get_backlinks(note_id))

@notes_bp.route('/api/graph', methods=['GET'])
def graph_api():
    return jsonify(get_graph_data())

# ---------- OBSIDIAN SYNC API ----------
@notes_bp.route('/api/sync_config', methods=['GET', 'POST'])
def sync_config():
    if request.method == 'GET':
        return jsonify({"vault_path": get_vault_path()})
    else:
        data = request.get_json()
        path = data.get('vault_path')
        if not path:
            return jsonify({"error": "vault_path required"}), 400
        set_vault_path(path)
        return jsonify({"ok": True})

@notes_bp.route('/api/sync_obsidian', methods=['POST'])
def sync_obsidian():
    data = request.get_json()
    direction = data.get('direction')  # 'import' or 'export'
    vault_path = data.get('vault_path')
    if direction == 'import':
        result = import_from_obsidian(vault_path)
    elif direction == 'export':
        result = export_to_obsidian(vault_path)
    else:
        return jsonify({"error": "Invalid direction. Use 'import' or 'export'."}), 400
    return jsonify(result)

# ---------- Run migration on startup ----------
migrate_from_json_if_needed()
load_notes()   # preload cache

# ===========================================================================
# HTML TEMPLATE – unchanged (same as before, but we keep it here for completeness)
# ===========================================================================
NOTES_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E📝%3C/text%3E%3C/svg%3E">
<title>Notes · Obsidian + AI + Weather</title>
<script src="/static/vendor/marked.min.js"></script>
<!-- vis-network for graph view -->
<script src="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.2/vis-network.min.js"></script>
<link href="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.2/dist/vis-network.min.css" rel="stylesheet" type="text/css" />
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

/* ----- Obsidian Sync Button ----- */
.sync-controls {
    display: flex;
    gap: 6px;
    align-items: center;
}
.sync-btn {
    background: rgba(33,38,45,0.7);
    border: 1px solid rgba(255,255,255,0.1);
    color: #8b949e;
    border-radius: 10px;
    padding: 6px 14px;
    font-size: 12px;
    cursor: pointer;
    backdrop-filter: blur(5px);
    transition: all 0.2s;
}
.sync-btn:hover {
    background: rgba(88,166,255,0.15);
    border-color: #58a6ff;
    color: #58a6ff;
}
#syncStatus {
    font-size: 11px;
    color: #8b949e;
    display: none;
}
.sync-popup {
    position: fixed;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    background: rgba(22,27,34,0.95);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 16px;
    padding: 24px;
    z-index: 99999;
    min-width: 320px;
    max-width: 90vw;
    box-shadow: 0 20px 60px rgba(0,0,0,0.8);
    backdrop-filter: blur(20px);
}
.sync-popup h3 {
    margin: 0 0 12px;
    color: #e1e4e8;
}
.sync-popup p {
    color: #8b949e;
    font-size: 13px;
    margin: 0 0 8px;
}
.sync-popup input {
    width: 100%;
    padding: 10px;
    border-radius: 10px;
    border: 1px solid rgba(255,255,255,0.15);
    background: rgba(0,0,0,0.3);
    color: #e1e4e8;
    margin-bottom: 12px;
    box-sizing: border-box;
}
.sync-popup .actions {
    display: flex;
    gap: 10px;
    justify-content: flex-end;
    margin-top: 8px;
}
.sync-popup .actions button {
    padding: 8px 20px;
    border-radius: 10px;
    border: none;
    cursor: pointer;
    font-size: 13px;
}
.sync-popup .actions .cancel-sync {
    background: transparent;
    color: #8b949e;
}
.sync-popup .sync-btns {
    display: flex;
    gap: 10px;
    margin: 12px 0;
}
.sync-popup .sync-btns button {
    flex: 1;
    padding: 8px;
    border: none;
    border-radius: 8px;
    cursor: pointer;
    font-weight: 500;
}
.sync-popup .sync-btns .import-btn { background: #1f6feb; color: #fff; }
.sync-popup .sync-btns .export-btn { background: #3fb950; color: #fff; }
.sync-popup #syncResult {
    margin-top: 12px;
    font-size: 13px;
    color: #8b949e;
}
body.light-mode .sync-popup {
    background: rgba(255,255,255,0.95);
    border-color: rgba(0,0,0,0.1);
}
body.light-mode .sync-popup h3 { color: #24292f; }
body.light-mode .sync-popup input {
    background: rgba(0,0,0,0.04);
    color: #24292f;
    border-color: rgba(0,0,0,0.15);
}
body.light-mode .sync-popup .actions .cancel-sync { color: #57606a; }
/* end sync styles */

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

/* Theme toggle (unchanged) */
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
    z-index: 10; cursor: grab; transition: left 0.4s cubic-bezier(.34,1.2,.64,1); left: 3px;
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
                0 0 12px hsl(44 100% 70% / .5);
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

/* ── Obsidian-style rendering inside the preview pane ── */
.preview h1, .preview h2, .preview h3, .preview h4, .preview h5, .preview h6 {
    color: #e6edf3;
    font-weight: 600;
    margin: 18px 0 8px;
    line-height: 1.3;
}
.preview h1 { font-size: 1.6em; padding-bottom: 6px; border-bottom: 1px solid rgba(255,255,255,0.1); }
.preview h2 { font-size: 1.35em; padding-bottom: 4px; border-bottom: 1px solid rgba(255,255,255,0.07); }
.preview h3 { font-size: 1.15em; }
.preview h1:first-child, .preview h2:first-child, .preview h3:first-child { margin-top: 0; }
.preview p { margin: 8px 0; }
.preview ul, .preview ol { padding-left: 24px; margin: 8px 0; }
.preview li { margin: 3px 0; }
.preview li > input[type="checkbox"] {
    margin-right: 6px;
    accent-color: #58a6ff;
    transform: translateY(1px);
}
.preview a { color: #58a6ff; text-decoration: none; }
.preview a:hover { text-decoration: underline; }
.preview hr {
    border: none;
    border-top: 1px solid rgba(255,255,255,0.12);
    margin: 16px 0;
}
.preview code {
    background: rgba(255,255,255,0.08);
    color: #ffa657;
    padding: 1px 5px;
    border-radius: 4px;
    font-family: 'SFMono-Regular', Consolas, 'Courier New', monospace;
    font-size: 0.9em;
}
.preview pre {
    background: rgba(0,0,0,0.35);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 6px;
    padding: 12px 14px;
    overflow-x: auto;
    margin: 10px 0;
}
.preview pre code {
    background: none;
    color: #e6edf3;
    padding: 0;
}
.preview blockquote {
    border-left: 3px solid #58a6ff;
    padding: 4px 12px;
    margin: 10px 0;
    background: rgba(255,255,255,0.03);
    border-radius: 0 4px 4px 0;
    color: #b6c2cf;
}
/* Obsidian-style ==highlight== */
.preview mark, .obsidian-highlight {
    background: #ffd82480;
    color: #1a1a1a;
    padding: 1px 3px;
    border-radius: 3px;
}
/* Obsidian-style markdown tables */
.preview table {
    border-collapse: collapse;
    width: 100%;
    margin: 12px 0;
    font-size: 0.93em;
}
.preview table th, .preview table td {
    border: 1px solid rgba(255,255,255,0.14);
    padding: 6px 10px;
    text-align: left;
}
.preview table th {
    background: rgba(255,255,255,0.06);
    font-weight: 600;
    color: #e6edf3;
}
.preview table tr:nth-child(even) td {
    background: rgba(255,255,255,0.02);
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

/* Backlinks container (Obsidian style) */
#backlinksContainer {
    width:100%;
    margin-top:8px;
    display:none;
    border-top:1px solid rgba(255,255,255,0.05);
    padding-top:8px;
}
#backlinksContainer .backlinks-label {
    font-size:12px;
    color:#8b949e;
    margin-bottom:4px;
}
#backlinksList {
    display:flex;
    flex-wrap:wrap;
    gap:6px;
}
#backlinksList .tag-pill {
    background:rgba(255,255,255,0.06);
    border:1px solid rgba(255,255,255,0.08);
    border-radius:20px;
    padding:3px 12px;
    font-size:12px;
    color:#8b949e;
    cursor:pointer;
    transition:all 0.2s;
    user-select:none;
}
#backlinksList .tag-pill:hover {
    background:rgba(255,255,255,0.12);
    color:#58a6ff;
}

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
body.light-mode .preview h1, body.light-mode .preview h2, body.light-mode .preview h3 { color: #24292f; border-bottom-color: rgba(0,0,0,0.1); }
body.light-mode .preview code { background: rgba(0,0,0,0.06); color: #b35900; }
body.light-mode .preview pre { background: rgba(0,0,0,0.05); border-color: rgba(0,0,0,0.08); }
body.light-mode .preview pre code { background: none; color: #24292f; }
body.light-mode .preview blockquote { background: rgba(0,0,0,0.03); color: #57606a; }
body.light-mode .preview table th, body.light-mode .preview table td { border-color: rgba(0,0,0,0.12); }
body.light-mode .preview table th { background: rgba(0,0,0,0.05); color: #24292f; }
body.light-mode .preview table tr:nth-child(even) td { background: rgba(0,0,0,0.015); }
body.light-mode .preview mark, body.light-mode .obsidian-highlight { background: #ffe066; color: #1a1a1a; }
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
.image-upload-btn {
    background: rgba(88,166,255,0.15);
    border-color: #58a6ff;
    color: #58a6ff;
}
.image-upload-btn:hover {
    background: rgba(88,166,255,0.25);
}
#backlinksContainer .tag-pill {
    background:rgba(255,255,255,0.06);
    border:1px solid rgba(255,255,255,0.08);
    border-radius:20px;
    padding:3px 12px;
    font-size:12px;
    color:#8b949e;
    cursor:pointer;
    transition:all 0.2s;
    user-select:none;
}
#backlinksContainer .tag-pill:hover {
    background:rgba(255,255,255,0.12);
    color:#58a6ff;
}
body.light-mode #backlinksContainer .tag-pill {
    background:rgba(0,0,0,0.04);
    border-color:rgba(0,0,0,0.08);
    color:#57606a;
}
body.light-mode #backlinksContainer .tag-pill:hover {
    background:rgba(0,0,0,0.08);
    color:#1f6feb;
}

/* ── Graph Modal ── */
#graphModal {
    display:none;
    position:fixed;
    top:0; left:0;
    width:100%; height:100%;
    background:rgba(0,0,0,0.85);
    z-index:20000;
    backdrop-filter:blur(5px);
    padding:20px;
}
#graphModal .graph-header {
    display:flex;
    justify-content:space-between;
    align-items:center;
    color:#fff;
    padding:10px 20px;
    background:rgba(0,0,0,0.5);
    border-radius:12px;
    margin-bottom:16px;
}
#graphModal .graph-header h2 { margin:0; }
#graphModal .graph-header button {
    background:none; border:none; color:#fff; font-size:28px; cursor:pointer;
}
#graphContainer {
    width:100%;
    height:calc(100% - 80px);
    background:rgba(255,255,255,0.02);
    border-radius:12px;
    border:1px solid rgba(255,255,255,0.1);
}

/* ── Weather Widget (compact, fixed top‑right) ── */
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
    position: relative;
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
.weather-widget .toast-text .main .highlight { font-weight: 700; color: #fff; }
.weather-widget .toast-text .sub {
    font-size: 10px;
    color: rgba(255,255,255,0.6);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.weather-widget .toast-text .time-row { display:flex; align-items:baseline; gap:4px; }
.weather-widget .toast-text .time-row .clock {
    font-size:14px; font-weight:700; color:#fff; letter-spacing:0.3px; font-variant-numeric:tabular-nums;
}
.weather-widget .toast-text .time-row .date { font-size:9px; color:rgba(255,255,255,0.5); }
.weather-widget .toast-text .weather-row {
    display:flex; align-items:center; gap:4px; font-size:11px; color:rgba(255,255,255,0.8);
}
.weather-widget .toast-text .weather-row .temp { font-weight:700; font-size:13px; color:#fff; }
.weather-widget .toast-text .weather-row .condition { font-size:10px; color:rgba(255,255,255,0.6); }
.weather-widget .toast-text .weather-row .weather-emoji { font-size:14px; }
.weather-widget .toast-text .fetch-status {
    font-size:9px; color:rgba(255,255,255,0.4); font-style:italic; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}
.weather-widget .toast-progress {
    position:absolute; bottom:0; left:0; width:0%; height:3px;
    background:linear-gradient(90deg,#58a6ff,#3fb950); border-radius:0; transition:width .4s ease;
    z-index:3; pointer-events:none; max-width:100%; will-change:width;
}
.weather-widget .close-btn {
    position:absolute; top:4px; right:4px; background:rgba(0,0,0,0.35); border:none;
    color:rgba(255,255,255,0.7); cursor:pointer; font-size:12px; padding:2px 6px; border-radius:20px;
    transition:background .2s; line-height:1; z-index:4;
}
.weather-widget .close-btn:hover { background:rgba(255,0,0,0.35); color:#fff; }
.weather-widget .spinner {
    width:18px; height:18px; border:2px solid rgba(255,255,255,0.15); border-top-color:#fff;
    border-radius:50%; animation:spin 0.8s linear infinite;
}
@keyframes spin { to { transform:rotate(360deg); } }

.weather-controls {
    display:flex; align-items:center; gap:6px;
}
.weather-controls select {
    background:rgba(0,0,0,0.4); border:1px solid rgba(255,255,255,0.15); color:#e1e4e8;
    border-radius:40px; padding:4px 12px 4px 16px; font-size:13px; font-family:inherit;
    cursor:pointer; outline:none; max-width:150px; appearance:none; -webkit-appearance:none;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='white' stroke-width='1.5' fill='none'/%3E%3C/svg%3E");
    background-repeat:no-repeat; background-position:right 8px center; padding-right:28px;
}
.weather-controls select option { background:#1a1a2e; color:#e1e4e8; }
.weather-controls select:hover { border-color:rgba(255,255,255,0.3); }
body.light-mode .weather-widget .toast {
    background:rgba(255,255,255,0.92); border-color:rgba(0,0,0,0.06); color:#24292f;
}
body.light-mode .weather-widget .toast-text .main { color:#24292f; }
body.light-mode .weather-widget .toast-text .main .highlight { color:#000; }
body.light-mode .weather-widget .toast-text .time-row .clock { color:#000; }
body.light-mode .weather-widget .toast-text .weather-row .temp { color:#000; }
body.light-mode .weather-controls select {
    background:rgba(255,255,255,0.8); color:#1a1a2e; border-color:rgba(0,0,0,0.15);
}
body.light-mode .weather-controls select option { background:#fff; color:#1a1a2e; }
@media (max-width:600px) {
    .weather-widget { top:60px; right:8px; max-width:94vw; }
    .weather-widget .toast-scene { height:70px; }
    .weather-widget .toast-content { padding:6px 10px 8px 8px; }
    .weather-widget .toast-text .main { font-size:11px; }
    .weather-widget .toast-text .weather-row .temp { font-size:12px; }
    .weather-controls select { max-width:120px; font-size:12px; padding:3px 24px 3px 10px; }
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
                <div class="sync-controls">
                    <button class="sync-btn" id="syncBtn" title="Sync with Obsidian vault">🔄 Sync</button>
                    <span id="syncStatus"></span>
                </div>
                <div class="weather-controls">
                    <select id="countrySelect" aria-label="Select country"></select>
                </div>
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
                        <button data-cmd="highlight" title="Highlight">🖍️</button>
                        <button data-cmd="table" title="Insert table">▦</button>
                        <button data-cmd="preview" title="Toggle preview" id="previewToggle">👁️</button>
                        <button class="image-upload-btn" id="imageUploadBtn" title="Insert Image">🖼️</button>
                        <input type="file" id="imageFileInput" accept="image/*" style="display:none;">
                        <!-- GRAPH VIEW BUTTON (Obsidian style) -->
                        <button id="graphViewBtn" title="Open Graph View">📊 Graph</button>
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
                    <textarea id="noteContentInput" placeholder="Write your note in Markdown... (use [[Wiki Links]])"></textarea>
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
                    <!-- BACKLINKS CONTAINER (Obsidian style) -->
                    <div id="backlinksContainer">
                        <div class="backlinks-label">🔗 Linked from:</div>
                        <div id="backlinksList"></div>
                    </div>
                </div>
                <div class="note-actions">
                    <button class="save-note" id="saveNoteBtn">💾 Save Note</button>
                    <button id="cancelNoteBtn" style="display:none;">Cancel</button>
                </div>
                <div id="aiResult" style="display:none; margin-top:12px; padding:10px; background:rgba(255,255,255,0.05); border-radius:8px; border:1px solid rgba(255,255,255,0.1); color:#e1e4e8; font-size:14px; white-space:pre-wrap;"></div>
            </div>
            <div id="notesList"></div>
        </div>
    </div>
</div>

<!-- ─── GRAPH VIEW MODAL (Obsidian) ─── -->
<div id="graphModal">
    <div class="graph-header">
        <h2>📊 Knowledge Graph</h2>
        <button onclick="closeGraph()">✕</button>
    </div>
    <div id="graphContainer"></div>
</div>

<!-- ─── WEATHER WIDGET ─── -->
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
    // ─── Theme (unchanged) ───────────────────────────
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

    // ─── Obsidian ==highlight== syntax ──────────────────
    function convertHighlights(text) {
        return (text || '').replace(/==([^=\n]+)==/g, '<mark>$1</mark>');
    }

    // ─── Markdown preview with Obsidian links ──────────
    function renderWikiPreview(htmlContent) {
        // Replace ![[Title]] with embedded blockquote
        htmlContent = htmlContent.replace(/!\[\[([^\]]+)\]\]/g, function(match, title) {
            var foundId = null;
            for (var id in notesData) {
                if (notesData[id].title.toLowerCase() === title.toLowerCase()) {
                    foundId = id;
                    break;
                }
            }
            if (foundId) {
                var note = notesData[foundId];
                var snippet = note.content.substring(0, 300) + (note.content.length > 300 ? '…' : '');
                return `<blockquote style="border-left:3px solid #58a6ff; padding-left:12px; margin:8px 0; background:rgba(255,255,255,0.03); border-radius:4px;">
                    📄 <strong><a href="#" onclick="selectNote('${foundId}')">${title}</a></strong><br>
                    ${marked.parse(convertHighlights(snippet))}
                </blockquote>`;
            }
            return `<span style="color:#f85149;">[![[${title}]]]</span>`;
        });
        // Replace [[Title]] with clickable link
        htmlContent = htmlContent.replace(/\[\[([^\]]+)\]\]/g, function(match, title) {
            var foundId = null;
            for (var id in notesData) {
                if (notesData[id].title.toLowerCase() === title.toLowerCase()) {
                    foundId = id;
                    break;
                }
            }
            if (foundId) {
                return `<a href="#" onclick="selectNote('${foundId}')" style="color:#58a6ff; text-decoration:underline;">${title}</a>`;
            }
            return `<span style="color:#f85149;">[[${title}]]</span>`;
        });
        return htmlContent;
    }

    function updatePreview() {
        var content = document.getElementById('noteContentInput').value;
        var preview = document.getElementById('notePreview');
        var rendered = marked.parse(convertHighlights(content || ''));
        rendered = renderWikiPreview(rendered);
        preview.innerHTML = rendered;
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
            else if (cmd === 'highlight') replacement = '==' + selected + '==';
            else if (cmd === 'table') {
                replacement = selected ? selected + '\n\n' : '';
                replacement += '| Column A | Column B | Column C |\n' +
                               '| -------- | -------- | -------- |\n' +
                               '| Row 1    | Data     | Data     |\n' +
                               '| Row 2    | Data     | Data     |\n';
            }
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

    // ─── GRAPH VIEW ──────────────────────────────────────
    function openGraphView() {
        var modal = document.getElementById('graphModal');
        modal.style.display = 'block';
        var container = document.getElementById('graphContainer');
        container.innerHTML = '<div style="color:#8b949e; padding:40px; text-align:center;">⏳ Loading graph...</div>';
        
        fetch('/notes/api/graph')
            .then(r => r.json())
            .then(data => {
                var nodes = new vis.DataSet(data.nodes.map(n => ({ ...n, shape: 'dot', size: 22, font: { color: '#e1e4e8' } })));
                var edges = new vis.DataSet(data.edges);
                var options = {
                    nodes: { shape: 'dot', size: 20, font: { size: 14, color: '#e1e4e8' } },
                    edges: { smooth: false, arrows: { to: { enabled: true, scaleFactor: 0.5 } } },
                    physics: { stabilization: { enabled: true, iterations: 100 } },
                    interaction: { hover: true, tooltipDelay: 100 },
                    layout: { improvedLayout: true }
                };
                var network = new vis.Network(container, { nodes, edges }, options);
                network.on('click', function(params) {
                    if (params.nodes.length) {
                        var id = params.nodes[0];
                        closeGraph();
                        selectNote(id);
                    }
                });
            })
            .catch(err => {
                container.innerHTML = '<div style="color:#f85149; padding:40px; text-align:center;">❌ Failed to load graph: ' + err + '</div>';
            });
    }

    function closeGraph() {
        document.getElementById('graphModal').style.display = 'none';
        document.getElementById('graphContainer').innerHTML = '';
    }

    document.getElementById('graphViewBtn').addEventListener('click', openGraphView);

    // ─── BACKLINKS ──────────────────────────────────────
    function loadBacklinks(noteId) {
        if (!noteId) {
            document.getElementById('backlinksContainer').style.display = 'none';
            return;
        }
        fetch('/notes/api/backlinks/' + noteId)
            .then(r => r.json())
            .then(data => {
                var container = document.getElementById('backlinksContainer');
                var list = document.getElementById('backlinksList');
                list.innerHTML = '';
                if (data.length === 0) {
                    container.style.display = 'none';
                    return;
                }
                container.style.display = 'block';
                data.forEach(function(item) {
                    var pill = document.createElement('span');
                    pill.className = 'tag-pill';
                    pill.textContent = '📄 ' + item.title;
                    pill.style.cursor = 'pointer';
                    pill.title = 'Type: ' + (item.link_type || 'wiki');
                    pill.onclick = function() { selectNote(item.id); };
                    list.appendChild(pill);
                });
            })
            .catch(() => { document.getElementById('backlinksContainer').style.display = 'none'; });
    }

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
        document.getElementById('backlinksContainer').style.display = 'none';
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
        // load backlinks for this note
        loadBacklinks(id);
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

    // ─── Obsidian Sync ────────────────────────────────────
    var vaultPath = null;

    function loadSyncConfig() {
        fetch('/notes/api/sync_config')
            .then(r => r.json())
            .then(data => { if (data.vault_path) vaultPath = data.vault_path; });
    }

    function showSyncPopup() {
        var overlay = document.createElement('div');
        overlay.className = 'sync-popup';
        overlay.id = 'syncPopup';
        overlay.innerHTML = `
            <h3>🔄 Obsidian Sync</h3>
            <p>Vault path on server:</p>
            <input type="text" id="vaultPathInput" placeholder="/path/to/your/obsidian/vault" value="${vaultPath || ''}">
            <div class="sync-btns">
                <button class="import-btn" id="importBtn">📥 Import from Vault</button>
                <button class="export-btn" id="exportBtn">📤 Export to Vault</button>
            </div>
            <div class="actions">
                <button class="cancel-sync" id="cancelSync">Cancel</button>
            </div>
            <div id="syncResult"></div>
        `;
        document.body.appendChild(overlay);

        document.getElementById('vaultPathInput').addEventListener('change', function() {
            var path = this.value.trim();
            if (path) {
                fetch('/notes/api/sync_config', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({vault_path: path})
                }).then(r => r.json()).then(data => { if (data.ok) vaultPath = path; });
            }
        });

        function runSync(direction) {
            var path = document.getElementById('vaultPathInput').value.trim();
            if (!path) {
                document.getElementById('syncResult').textContent = '⚠️ Please set a vault path.';
                return;
            }
            document.getElementById('syncResult').textContent = '⏳ Syncing...';
            var statusEl = document.getElementById('syncStatus');
            statusEl.textContent = '⏳ Syncing...';
            statusEl.style.display = 'inline';
            fetch('/notes/api/sync_obsidian', {
                method: 'POST',
                headers: {'Content-Type':'application/json'},
                body: JSON.stringify({direction: direction, vault_path: path})
            })
            .then(r => r.json())
            .then(data => {
                if (data.error) {
                    document.getElementById('syncResult').textContent = '❌ ' + data.error;
                    statusEl.textContent = '❌ Sync failed';
                    return;
                }
                if (direction === 'import') {
                    document.getElementById('syncResult').textContent = `✅ Imported ${data.imported} notes (${data.skipped} skipped) – ${data.total_files} files found`;
                    statusEl.textContent = `✅ Imported ${data.imported}`;
                } else {
                    document.getElementById('syncResult').textContent = `✅ Exported ${data.exported} notes`;
                    statusEl.textContent = `✅ Exported ${data.exported}`;
                }
                loadNotes();
                setTimeout(() => { statusEl.style.display = 'none'; }, 5000);
            })
            .catch(err => {
                document.getElementById('syncResult').textContent = '❌ Error: ' + err;
                statusEl.textContent = '❌ Error';
            });
        }

        document.getElementById('importBtn').addEventListener('click', function() { runSync('import'); });
        document.getElementById('exportBtn').addEventListener('click', function() { runSync('export'); });
        document.getElementById('cancelSync').addEventListener('click', function() {
            overlay.remove();
        });
    }

    document.getElementById('syncBtn').addEventListener('click', showSyncPopup);
    loadSyncConfig();

    // ─── WEATHER WIDGET ──────────────────────────────────
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
    let activeToast = null;

    // DOM refs for weather
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

    function toggleWeather() {
        if (container.style.display === 'none') {
            container.style.display = 'block';
            if (!currentCountryCode) {
                detectLocation();
            } else {
                toast.style.display = 'flex';
                updateFromCountry(currentCountryCode);
            }
        } else {
            container.style.display = 'none';
            if (activeToast) activeToast.close();
        }
    }

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

    function updateWidget(mainText, subText = '', iconHtml = null, progress = null) {
        mainEl.textContent = mainText;
        if (subText) { subEl.textContent = subText; subEl.style.opacity = '1'; } else { subEl.style.opacity = '0'; }
        if (iconHtml !== null) iconEl.innerHTML = iconHtml;
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

    function setStatus(text) { fetchStatus.textContent = text; }

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
        blobs.forEach(b => { updateBlob(b, w, h, t, 0.9); drawWalkingBlob(ctx, b, t); });
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
        blobs.forEach(b => { updateBlob(b, w, h, t, 0.6); drawWalkingBlob(ctx, b, t); });
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
        blobs.forEach(b => { updateBlob(b, w, h, t, 0.85); drawWalkingBlob(ctx, b, t); });
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
        blobs.forEach(b => { updateBlob(b, w, h, t, 1.0); drawWalkingBlob(ctx, b, t); });
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
        let season = 'summer';
        function frame(now) {
            if (!canvas || !document.body.contains(canvas)) {
                cancelAnimationFrame(canvasAnimId);
                window.removeEventListener('resize', onResize);
                return;
            }
            const t = (now - start) * 0.6;
            renderScene(ctx, w, h, season, t);
            canvasAnimId = requestAnimationFrame(frame);
        }
        frame(start);
        window._setSeason = (s) => { season = s; };
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
            if (code && countryLatMap[code]) effectiveLat = countryLatMap[code];
            else effectiveLat = 30;
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
            return { tempC, condition: weatherDescFromCode(code), emoji: weatherEmojiFromCode(code), lat, lon, timezone: data.timezone || null };
        } catch (err) { return null; }
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
                tempC, condition: weatherDescFromCode(code), emoji: weatherEmojiFromCode(code),
                region: place.admin1 || '', city: place.name || city, country: place.country || '',
                lat, lon, timezone: wData.timezone || null
            };
        } catch (err) { return null; }
    }

    async function showWeatherWidget(season, city, country, code, region, lat, manual = false) {
        const flag = getFlagFromCode(code) || '🌍';
        let locationDisplay = city;
        if (region && region !== city && region !== 'Unknown') locationDisplay += `, ${region}`;
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
                if (city && city !== 'Unknown') wData = await fetchWeatherByCity(city);
                if ((!wData || wData.tempC === null) && lat) wData = await fetchWeather(lat, 0);
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
            } catch (e) { setStatus('⚠️ Weather unavailable'); }
        }
        if (window._setSeason) window._setSeason(season);
        if (code) {
            const option = countrySelect.querySelector(`option[value="${code}"]`);
            if (option) countrySelect.value = code;
        }
    }

    async function updateFromCountry(code) {
        const country = countryList.find(c => c.code === code);
        if (!country) return;
        const name = country.name;
        const wData = await fetchWeatherByCity(name);
        let city = name, region = '', temp = null, cond = '', emoji = '🌤️';
        let lat = 0, lon = 0, countryName = name;
        if (wData && wData.tempC !== null) {
            temp = wData.tempC; cond = wData.condition || ''; emoji = wData.emoji || '🌤️';
            if (wData.city && wData.city !== name) city = wData.city;
            if (wData.region) region = wData.region;
            if (wData.country) countryName = wData.country;
            lat = wData.lat || 0; lon = wData.lon || 0;
            if (wData.timezone) currentTimezone = wData.timezone;
        }
        if (!lat || Math.abs(lat) < 0.01) lat = countryLatMap[code] || 30;
        currentCity = city; currentCountry = countryName; currentCountryCode = code; currentRegion = region;
        currentTemp = temp; currentCondition = cond; currentWeatherEmoji = emoji;
        currentFlag = getFlagFromCode(code); currentLat = lat; currentLon = lon;
        const season = getSeasonForCountry(countryName, code, lat);
        blobInstances = [];
        await showWeatherWidget(season, city, countryName, code, region, lat, true);
    }

    async function detectLocation() {
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
                    if (!currentLat || Math.abs(currentLat) < 0.01) currentLat = countryLatMap[code] || 30;
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
        } catch (e) { console.warn('IP geolocation failed:', e); }
        const fallbackCode = 'US';
        await updateFromCountry(fallbackCode);
    }

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
            if (code) updateFromCountry(code);
        });
        closeBtn.addEventListener('click', function() {
            toast.style.display = 'none';
            if (activeToast) activeToast.close();
        });
        animateScene();
        detectLocation();
    }

    // ─── Init ────────────────────────────────────────────
    window.addEventListener('load', function() {
        loadAIPreferences();
        loadNotes();
        document.getElementById('noteTitleInput').focus();
        initWeatherWidget();
    });
</script>
</body>
</html>
"""
# End of notes.py