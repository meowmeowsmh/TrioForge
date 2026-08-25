# backup_store.py – automatic backup of deleted conversations, notes and corkboard pins.
#
# When the UI deletes a conversation / message / note / pin, the deleted data is
# archived here (sqlite_data/backup/backup.db) instead of being lost for good.
# The Restore UI then lets the user browse and bring anything back.
#
# This is a *storage* layer only. It has no Flask routes and no knowledge of the
# main app's caches – callers pass the raw rows in and receive plain dicts out.

import os
import sqlite3
import threading
from datetime import datetime

from paths import root_path
from common import json_loads, json_dumps

BACKUP_DIR = root_path("sqlite_data", "backup")
BACKUP_DB = os.path.join(BACKUP_DIR, "backup.db")
os.makedirs(BACKUP_DIR, exist_ok=True)

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS deleted_conversations (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT '',
    created       TEXT,
    "order"       INTEGER NOT NULL DEFAULT 0,
    last_activity TEXT,
    deleted_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deleted_messages (
    conversation_id TEXT NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT,
    attachments     TEXT,
    created_at      TEXT,
    deleted_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deleted_msgs_conv ON deleted_messages(conversation_id);

CREATE TABLE IF NOT EXISTS deleted_notes (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT '',
    content       TEXT NOT NULL DEFAULT '',
    created       TEXT,
    last_modified TEXT,
    order_idx     INTEGER NOT NULL DEFAULT 0,
    pinned        INTEGER NOT NULL DEFAULT 0,
    color         TEXT NOT NULL DEFAULT 'default',
    tags          TEXT NOT NULL DEFAULT '[]',
    embedding     TEXT,
    deleted_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deleted_pins (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT '',
    content       TEXT NOT NULL DEFAULT '',
    x             REAL NOT NULL DEFAULT 0,
    y             REAL NOT NULL DEFAULT 0,
    width         INTEGER NOT NULL DEFAULT 220,
    height        INTEGER NOT NULL DEFAULT 200,
    color         TEXT NOT NULL DEFAULT 'yellow',
    rotation      INTEGER NOT NULL DEFAULT 0,
    created       TEXT,
    last_modified TEXT,
    tags          TEXT NOT NULL DEFAULT '[]',
    type          TEXT NOT NULL DEFAULT 'note',
    filename      TEXT,
    image_url     TEXT,
    embedding     TEXT,
    deleted_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deleted_links (
    from_id    TEXT NOT NULL,
    to_id      TEXT NOT NULL,
    color      TEXT NOT NULL DEFAULT 'black',
    deleted_at TEXT NOT NULL
);
"""


def _now():
    return datetime.now().isoformat()


def get_conn():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    conn = sqlite3.connect(BACKUP_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _lock:
        conn = get_conn()
        conn.executescript(_SCHEMA)
        conn.commit()
        conn.close()


init_db()


# --------------------------------------------------------------------------
# Archive (called *before* the main-DB delete so the data survives)
# --------------------------------------------------------------------------

def archive_conversation(meta, messages):
    """meta: dict with keys id/title/created/order/last_activity.
    messages: list of dicts with keys role/content/attachments/created_at."""
    now = _now()
    with _lock:
        conn = get_conn()
        conn.execute(
            'DELETE FROM deleted_messages WHERE conversation_id = ?', (meta.get("id"),)
        )
        conn.execute(
            """INSERT OR REPLACE INTO deleted_conversations
               (id, title, created, "order", last_activity, deleted_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                meta.get("id"),
                meta.get("title", ""),
                meta.get("created"),
                meta.get("order", 0),
                meta.get("last_activity"),
                now,
            ),
        )
        for m in messages:
            conn.execute(
                """INSERT INTO deleted_messages
                   (conversation_id, role, content, attachments, created_at, deleted_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    meta.get("id"),
                    m.get("role", ""),
                    m.get("content"),
                    m.get("attachments"),
                    m.get("created_at"),
                    now,
                ),
            )
        conn.commit()
        conn.close()


def archive_message(conversation_id, message):
    """message: dict with keys role/content/attachments/created_at."""
    with _lock:
        conn = get_conn()
        conn.execute(
            """INSERT INTO deleted_messages
               (conversation_id, role, content, attachments, created_at, deleted_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                conversation_id,
                message.get("role", ""),
                message.get("content"),
                message.get("attachments"),
                message.get("created_at"),
                _now(),
            ),
        )
        conn.commit()
        conn.close()


def archive_note(note):
    """note: dict with the note's columns (tags may be a list or JSON string)."""
    tags = note.get("tags", [])
    if not isinstance(tags, str):
        tags = json_dumps(tags if isinstance(tags, list) else [])
    embedding = note.get("embedding")
    if embedding is not None and not isinstance(embedding, str):
        embedding = json_dumps(embedding)
    with _lock:
        conn = get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO deleted_notes
               (id, title, content, created, last_modified, order_idx, pinned, color,
                tags, embedding, deleted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                note.get("id"),
                note.get("title", ""),
                note.get("content", ""),
                note.get("created"),
                note.get("last_modified"),
                note.get("order", 0),
                1 if note.get("pinned") else 0,
                note.get("color", "default"),
                tags,
                embedding,
                _now(),
            ),
        )
        conn.commit()
        conn.close()


def archive_pin(pin, links=None):
    """pin: dict with the pin's columns. links: optional list of {from,to,color}."""
    tags = pin.get("tags", [])
    if not isinstance(tags, str):
        tags = json_dumps(tags if isinstance(tags, list) else [])
    embedding = pin.get("embedding")
    if embedding is not None and not isinstance(embedding, str):
        embedding = json_dumps(embedding)
    now = _now()
    with _lock:
        conn = get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO deleted_pins
               (id, title, content, x, y, width, height, color, rotation,
                created, last_modified, tags, type, filename, image_url, embedding, deleted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pin.get("id"),
                pin.get("title", ""),
                pin.get("content", ""),
                pin.get("x", 0),
                pin.get("y", 0),
                pin.get("width", 220),
                pin.get("height", 200),
                pin.get("color", "yellow"),
                pin.get("rotation", 0),
                pin.get("created"),
                pin.get("last_modified"),
                tags,
                pin.get("type", "note"),
                pin.get("filename"),
                pin.get("image_url"),
                embedding,
                now,
            ),
        )
        conn.execute("DELETE FROM deleted_links WHERE from_id = ? OR to_id = ?",
                     (pin.get("id"), pin.get("id")))
        for l in (links or []):
            conn.execute(
                "INSERT INTO deleted_links (from_id, to_id, color, deleted_at) VALUES (?, ?, ?, ?)",
                (l.get("from"), l.get("to"), l.get("color", "black"), now),
            )
        conn.commit()
        conn.close()


# --------------------------------------------------------------------------
# List (for the Restore UI)
# --------------------------------------------------------------------------

def list_conversations():
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            """SELECT dc.*, (SELECT COUNT(*) FROM deleted_messages dm
                             WHERE dm.conversation_id = dc.id) AS message_count
               FROM deleted_conversations dc ORDER BY dc.deleted_at DESC"""
        ).fetchall()
        out = [dict(r) for r in rows]
        conn.close()
    return out


def list_notes():
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT id, title, deleted_at FROM deleted_notes ORDER BY deleted_at DESC"
        ).fetchall()
        out = [dict(r) for r in rows]
        conn.close()
    return out


def list_pins():
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT id, title, type, deleted_at FROM deleted_pins ORDER BY deleted_at DESC"
        ).fetchall()
        out = [dict(r) for r in rows]
        conn.close()
    return out


# --------------------------------------------------------------------------
# Retrieve (decode raw rows back into the shape the app expects)
# --------------------------------------------------------------------------

def _decode_tags(raw):
    if not raw:
        return []
    try:
        v = json_loads(raw)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _decode_embedding(raw):
    if not raw:
        return None
    try:
        return json_loads(raw)
    except Exception:
        return None


def get_conversation(cid):
    with _lock:
        conn = get_conn()
        meta = conn.execute(
            "SELECT id, title, created, \"order\", last_activity FROM deleted_conversations WHERE id = ?",
            (cid,),
        ).fetchone()
        if meta is None:
            conn.close()
            return None
        msgs = conn.execute(
            """SELECT role, content, attachments, created_at FROM deleted_messages
               WHERE conversation_id = ? ORDER BY created_at""",
            (cid,),
        ).fetchall()
        messages = [dict(m) for m in msgs]
        conn.close()
    return dict(meta), messages


def get_note(note_id):
    with _lock:
        conn = get_conn()
        row = conn.execute("SELECT * FROM deleted_notes WHERE id = ?", (note_id,)).fetchone()
        conn.close()
    if row is None:
        return None
    d = dict(row)
    d["tags"] = _decode_tags(d.get("tags"))
    d["embedding"] = _decode_embedding(d.get("embedding"))
    d["pinned"] = bool(d.get("pinned"))
    d.pop("deleted_at", None)
    return d


def get_pin(pin_id):
    with _lock:
        conn = get_conn()
        row = conn.execute("SELECT * FROM deleted_pins WHERE id = ?", (pin_id,)).fetchone()
        links = conn.execute(
            "SELECT from_id AS 'from', to_id AS 'to', color FROM deleted_links "
            "WHERE from_id = ? OR to_id = ?",
            (pin_id, pin_id),
        ).fetchall()
        conn.close()
    if row is None:
        return None, []
    d = dict(row)
    d["tags"] = _decode_tags(d.get("tags"))
    d["embedding"] = _decode_embedding(d.get("embedding"))
    d.pop("deleted_at", None)
    return d, [dict(l) for l in links]


# --------------------------------------------------------------------------
# Purge (remove an entry from the backup once restored, or if the user discards)
# --------------------------------------------------------------------------

def purge_conversation(cid):
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM deleted_messages WHERE conversation_id = ?", (cid,))
        conn.execute("DELETE FROM deleted_conversations WHERE id = ?", (cid,))
        conn.commit()
        conn.close()


def purge_note(note_id):
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM deleted_notes WHERE id = ?", (note_id,))
        conn.commit()
        conn.close()


def purge_pin(pin_id):
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM deleted_pins WHERE id = ?", (pin_id,))
        conn.execute("DELETE FROM deleted_links WHERE from_id = ? OR to_id = ?", (pin_id, pin_id))
        conn.commit()
        conn.close()
