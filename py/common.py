# common.py – shared JSON, SQLite, and embedding helpers used across TrioForge.
import json as std_json
import sqlite3
import threading
import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

# ── JSON helpers (orjson when available, standard json fallback) ──
try:
    import orjson

    def json_dumps(obj: Any) -> str:
        return orjson.dumps(obj).decode('utf-8')

    def json_loads(s: str) -> Any:
        return orjson.loads(s)

    logger.info("Using orjson for faster JSON")
except ImportError:

    def json_dumps(obj: Any) -> str:
        return std_json.dumps(obj)

    def json_loads(s: str) -> Any:
        return std_json.loads(s)

    logger.info("Using standard json (install orjson for better performance)")


# ── SQLite connections (one per thread, keyed by path) ──
_local = threading.local()


def get_conn(db_path: str) -> sqlite3.Connection:
    """Return a per-thread SQLite connection for `db_path` (thread-safe)."""
    conns = getattr(_local, 'conns', None)
    if conns is None:
        conns = {}
        _local.conns = conns
    conn = conns.get(db_path)
    if conn is None:
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        # WAL / synchronous pragmas are unreliable on some filesystems (notably
        # WSL's /mnt/<drive> mount, which throws "disk I/O error" for journal and
        # synchronous tweaks). Apply each one defensively and fall back to the
        # default when the filesystem rejects it, so the app still starts.
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            logger.warning(
                "WAL journal mode unavailable for %s (filesystem does not support it); "
                "using the default journal mode.", db_path
            )
        try:
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.OperationalError:
            logger.warning(
                "synchronous=NORMAL unavailable for %s; using the default.", db_path
            )
        try:
            conn.execute("PRAGMA foreign_keys=ON")
        except sqlite3.OperationalError:
            logger.warning("foreign_keys pragma unavailable for %s.", db_path)
        conns[db_path] = conn
    return conn


# ── Embedding model (lazy-loaded, thread-safe) ──
# numpy, scikit-learn, and sentence-transformers are heavy imports (they pull
# in pandas, torch, transformers), so we only *probe* for them here and do the
# real imports lazily inside the functions below — keeping app startup fast.


def _module_available(name: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(name) is not None


EMBED_AVAILABLE = (
    _module_available("sentence_transformers")
    and _module_available("numpy")
    and _module_available("sklearn")
)
if not EMBED_AVAILABLE:
    logger.warning(
        "sentence-transformers, numpy, or scikit-learn not installed. "
        "Run: pip install sentence-transformers scikit-learn numpy"
    )

_embed_model = None
_embed_model_lock = threading.Lock()


def get_embedder() -> Any:
    """Return the shared sentence-transformers model (lazy, thread-safe), or None."""
    global _embed_model
    if not EMBED_AVAILABLE:
        return None
    with _embed_model_lock:
        if _embed_model is None:
            from sentence_transformers import SentenceTransformer
            _embed_model = SentenceTransformer('all-MiniLM-L6-v2')
        return _embed_model


def embed_text(text: Optional[str]) -> Optional[List[float]]:
    """Embed a string; returns a list of floats, or None when unavailable/empty."""
    if not EMBED_AVAILABLE:
        return None
    model = get_embedder()
    text = (text or '').strip()[:1000]
    if not text:
        return None
    return model.encode(text).tolist()
