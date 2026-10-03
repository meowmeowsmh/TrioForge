"""Persistent conversation history for the terminal client.

The web app has ``conversations.db``; the terminal client had nothing - its
``Session`` lived only in RAM, so closing forge forgot every turn. This module
gives the TUI the same kind of memory: a SQLite table of the current
conversation, rewritten as the user talks and restored at startup.

One rolling conversation, one flat table. ``save`` rewrites the whole thing on
every change (a chat transcript is tens of messages, so the cost is trivial)
which keeps it correct under ``clear`` and ``drop_last`` for free: whatever the
in-memory session holds IS the history.

SQLite rather than DuckDB here: the history is plain append-and-read text with
no probabilistic gate and no compression needs, so the heavier DuckDB dependency
adds nothing.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone

from paths import root_path

DB_PATH = os.environ.get("TRIOFORGE_HISTORY_DB") or root_path(
    "sqlite_data", "tui_history.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

# One lock for the whole store: writes are tiny and infrequent, and the agent
# runs on worker threads while the UI runs on the main thread, so the same path
# can be hit from both. Serialising is simpler than connection-per-thread.
_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute(_SCHEMA)
    # Defensive pragmas, same pattern as common.py: WAL is unavailable on some
    # filesystems (WSL /mnt/<drive>), so fall back rather than fail to start.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.OperationalError:
        pass
    return conn


def save(messages) -> None:
    """Rewrite the history to exactly ``messages`` (a list of role/content pairs).

    Called as the session's on-change hook; a clear() therefore persists as an
    empty history. Never raises - a history write must not break a turn.
    """
    try:
        with _LOCK:
            conn = _connect()
            try:
                conn.execute("DELETE FROM messages")
                conn.executemany(
                    "INSERT INTO messages (role, content, created_at) VALUES (?, ?, ?)",
                    [(m.role, m.content, _now()) for m in messages],
                )
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass


def load() -> list[tuple[str, str]]:
    """The persisted conversation as ``(role, content)`` pairs, oldest first."""
    try:
        with _LOCK:
            conn = _connect()
            try:
                rows = conn.execute(
                    "SELECT role, content FROM messages ORDER BY id").fetchall()
                return [(r, c) for r, c in rows]
            finally:
                conn.close()
    except Exception:
        return []
