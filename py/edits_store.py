"""
Persistent store for the live-coding "agent edits" panel.

The panel previously kept edits only in memory (AGENT_EDITS), so they vanished
on restart. This module persists edits to `sqlite_data/edits.db` so they survive
a restart (and multiple browser sessions can see them). Records are inserted with
a monotonically increasing id and a timestamp; the UI polls by `since=<ts>`.

We keep an in-memory mirror (a single process, but per-thread SQLite isn't needed
for the tiny record set) for fast reads, and flush inserts to SQLite.
"""

import os
import threading
import time
import difflib
from typing import List, Optional

from common import get_conn
from paths import root_path

_DB_PATH = root_path("sqlite_data", "edits.db")

_lock = threading.Lock()
_initialized = False

# In-memory mirror, newest first. Rebuilt from SQLite at startup.
_edits: List[dict] = []
_seq = 0


def _ensure():
    global _initialized
    if _initialized:
        return
    with _lock:
        if _initialized:
            return
        os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
        c = get_conn(_DB_PATH)
        c.execute("""
            CREATE TABLE IF NOT EXISTS agent_edits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool TEXT,
                path TEXT,
                detail TEXT,
                diff TEXT,
                before TEXT,
                after TEXT,
                ts REAL
            )
        """)
        c.commit()
        _initialized = True


def _load_from_db():
    """Load persisted edits into the in-memory mirror (newest first)."""
    global _edits, _seq
    _ensure()
    c = get_conn(_DB_PATH)
    rows = c.execute(
        "SELECT id, tool, path, detail, diff, before, after, ts "
        "FROM agent_edits ORDER BY id DESC LIMIT 500"
    ).fetchall()
    _edits = []
    for r in rows:
        _edits.append({
            "id": r["id"], "tool": r["tool"], "path": r["path"],
            "detail": r["detail"], "diff": r["diff"], "before": r["before"],
            "after": r["after"], "ts": r["ts"],
        })
    _seq = _edits[0]["id"] if _edits else 0


_initialized_flag_init = False


def init():
    """Load persisted edits once (call at app startup)."""
    global _initialized_flag_init
    if not _initialized_flag_init:
        _load_from_db()


def insert(tool: str, path: str, before: str = "", after: str = "", detail: str = "") -> dict:
    """Persist one edit and return it. Uses a unified diff for the panel."""
    diff = "\n".join(difflib.unified_diff(
        (before or "").splitlines(), (after or "").splitlines(),
        fromfile=path or "", tofile=path or "", lineterm="",
    ))
    entry = {
        "tool": tool, "path": path, "detail": detail or "",
        "diff": diff, "before": (before or "")[:4000], "after": (after or "")[:4000],
        "ts": time.time(),
    }
    with _lock:
        _ensure()
        c = get_conn(_DB_PATH)
        cur = c.execute(
            "INSERT INTO agent_edits (tool, path, detail, diff, before, after, ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (entry["tool"], entry["path"], entry["detail"], entry["diff"],
             entry["before"], entry["after"], entry["ts"]),
        )
        c.commit()
        entry["id"] = cur.lastrowid
        _edits.insert(0, entry)  # newest first
        if len(_edits) > 500:
            _edits.pop()
    return entry


def list_since(since_ts: float = 0, limit: int = 200) -> List[dict]:
    """Return edits newer than `since_ts`, newest first."""
    _ensure()
    with _lock:
        items = [e for e in _edits if e.get("ts", 0) > since_ts]
        # The UI prepends newest-first; return newest first.
        return items[:limit]


def clear() -> None:
    """Remove all edits (both SQLite and the mirror)."""
    _ensure()
    with _lock:
        c = get_conn(_DB_PATH)
        c.execute("DELETE FROM agent_edits")
        c.commit()
        _edits.clear()


def count() -> int:
    _ensure()
    with _lock:
        return len(_edits)
