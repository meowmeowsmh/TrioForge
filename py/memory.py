"""TrioForge's offline memory vault: a DuckDB table behind a Bloom filter gate.

Two layers, deliberately:

* **The gate** - a Bloom filter held in RAM. It answers "is this key *definitely*
  absent?" in microseconds, with no disk access at all. A miss ends the lookup
  right there; only a possible hit is allowed through to DuckDB.
* **The vault** - a DuckDB table keyed by ``memory_key``. DuckDB compresses that
  text column itself (dictionary + ZSTD), so nothing here has to zip or unzip by
  hand.

The gate is *probabilistic*, so its two answers are not symmetric:

    gate says NO   -> the key is definitely absent.  Skip the query.   (exact)
    gate says YES  -> the key is *probably* present. Query and confirm. (maybe)

A false positive costs one wasted query and never returns wrong data, because the
DuckDB lookup still has to match the key. That asymmetry is the whole point: the
gate can be wrong in the cheap direction only.

Three things this does differently from the draft it grew out of:

1. **The filter is persisted.** It is written as a BLOB and read back in a single
   small query. Re-deriving it at boot by scanning every key would cost exactly
   the disk I/O the gate exists to avoid, and would get slower as the vault
   grows. Only a rebuild (or the very first boot) touches the key column.
2. **``INSERT OR REPLACE`` is SQLite syntax**; DuckDB wants ``ON CONFLICT ...
   DO UPDATE``.
3. **The filter is rebuilt when it fills.** A saturated Bloom filter answers YES
   to everything, and would quietly turn every lookup into a disk read.

A Bloom filter can only test an *exact* key, so "what was my port setting again?"
never reaches it directly. :func:`rewrite` maps a sentence onto candidate keys
first - purely in RAM - and only those candidates are offered to the gate.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from pathlib import Path
from typing import Iterable, Optional

from paths import root_path

DB_PATH = root_path("sqlite_data", "memory.duckdb")

# Filter sizing. ``capacity`` is how many keys the filter is built for and
# ``fp`` the false-positive rate at that capacity; both feed the standard Bloom
# formulas below. Growing the capacity costs a few KB of RAM, nothing more.
DEFAULT_CAPACITY = 10_000
TARGET_FP = 0.01

# Rebuild once the filter is this full. Past ~60% a Bloom filter's error rate
# climbs fast, and at 100% every answer is YES.
MAX_FILL = 0.6

MAX_KEY_CHARS = 120
MAX_VALUE_CHARS = 8_000        # one stored entry
MAX_CONTEXT_CHARS = 4_000      # what recall feeds an agent in one go
MIN_SCORE = 1.0                # below this a rewritten candidate is noise

# Purely grammatical: these never help match a key. Domain words such as
# "setting" or "config" are deliberately NOT here - they are often part of a key.
STOPWORDS = frozenset("""
a an the and or but of to in on at for with from by as is are was were be been
am do does did doing have has had i me my mine you your yours it its this that
these those there here what which who whom whose how when where why can could
should would will shall may might must please tell give show about again still
just really very much many any some
""".split())


# ------------------------------------------------------------------ the gate


def sizing(capacity: int, fp: float) -> tuple[int, int]:
    """Bits and hash count for ``capacity`` keys at false-positive rate ``fp``.

    The two standard results: m = -n*ln(p) / (ln2)^2 and k = (m/n)*ln2.
    """
    capacity = max(1, int(capacity))
    fp = min(max(float(fp), 1e-9), 0.5)
    bits = max(64, math.ceil(-(capacity * math.log(fp)) / (math.log(2) ** 2)))
    hashes = max(1, min(32, round((bits / capacity) * math.log(2))))
    return bits, hashes


class BloomGate:
    """A Bloom filter over the vault's keys, serialisable to bytes.

    Bit positions come from a single 16-byte blake2b digest split in half: h1 and
    h2, then h_i = h1 + i*h2 (Kirsch-Mitzenmacher double hashing). That yields k
    independent-enough positions from one digest instead of k separate hashes.
    """

    __slots__ = ("_bits_count", "bits", "capacity", "fp", "hashes", "nkeys")

    def __init__(self, capacity: int = DEFAULT_CAPACITY, fp: float = TARGET_FP,
                 bits: Optional[bytes] = None, nkeys: int = 0) -> None:
        self.capacity = max(1, int(capacity))
        self.fp = float(fp)
        self._bits_count, self.hashes = sizing(self.capacity, self.fp)
        if bits is None:
            self.bits = bytearray((self._bits_count + 7) // 8)
        else:
            # The bit count stays whatever sizing() derived from capacity/fp -
            # those are stored next to the blob, so this reproduces the modulus
            # used when the bits were set.
            #
            # Deriving it from len(bits) * 8 instead adds the byte padding to the
            # modulus (95851 -> 95856 for the default sizing) and shifts EVERY
            # position, so a reloaded gate rejects keys that are plainly set. It
            # fails silently and in the worst direction: memory would never hit.
            needed = (self._bits_count + 7) // 8
            self.bits = bytearray(bits[:needed])
            self.bits.extend(b"\x00" * (needed - len(self.bits)))
        self.nkeys = max(0, int(nkeys))

    # -- internals
    def _positions(self, key: str) -> Iterable[int]:
        digest = hashlib.blake2b(key.encode("utf-8"), digest_size=16).digest()
        h1 = int.from_bytes(digest[:8], "big")
        h2 = int.from_bytes(digest[8:], "big") | 1     # odd stride, never 0
        count = self._bits_count
        for i in range(self.hashes):
            yield (h1 + i * h2) % count

    # -- api
    def add(self, key: str) -> None:
        for pos in self._positions(key):
            self.bits[pos >> 3] |= 1 << (pos & 7)
        self.nkeys += 1

    def contains(self, key: str) -> bool:
        """False means *definitely* absent. True means *maybe* present."""
        for pos in self._positions(key):
            if not (self.bits[pos >> 3] >> (pos & 7)) & 1:
                return False
        return True

    @property
    def fill(self) -> float:
        """Fraction of bits set. Compare against MAX_FILL before trusting it."""
        return int.from_bytes(self.bits, "big").bit_count() / self._bits_count

    def to_bytes(self) -> bytes:
        return bytes(self.bits)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"BloomGate(keys={self.nkeys}, bits={self._bits_count}, "
                f"hashes={self.hashes}, fill={self.fill:.1%})")


# ------------------------------------------------------------- the rewriter

_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def tokens(text: str) -> list[str]:
    """Lowercased content words from a sentence, stopwords removed."""
    return [t for t in _TOKEN_RE.findall((text or "").lower())
            if t not in STOPWORDS]


def key_parts(key: str) -> list[str]:
    """``ui_layout_config`` -> ``['ui', 'layout', 'config']``."""
    return [p for p in _SPLIT_RE.split((key or "").lower()) if p]


def _part_match(token: str, part: str) -> float:
    """How well one token matches one part of a key. 0 when unrelated.

    Exact beats prefix beats substring, and a short token is not allowed to
    swallow a long part ("port" matches "port"/"ports", not "support").
    """
    if token == part:
        return 1.0
    shorter, longer = sorted((token, part), key=len)
    if len(shorter) >= 3 and longer.startswith(shorter):
        return 0.7
    if len(token) >= 4 and token in part:
        return 0.5
    return 0.0


def _coverage(sentence_tokens: list[str], parts: list[str]) -> float:
    """Mean best-match of every part against the sentence's words."""
    if not parts:
        return 0.0
    total = 0.0
    for part in parts:
        total += max((_part_match(t, part) for t in sentence_tokens), default=0.0)
    return total / len(parts)


def score_key(sentence_tokens: list[str], key: str, tags: str = "") -> float:
    """Rank one stored key against the user's words. Offline and explainable.

    A verbatim mention wins outright. Otherwise the score is how much of the
    key's own parts the sentence covers, with tag words as a weaker bonus.

    Key parts and tags are scored *separately* and then combined, rather than
    concatenated into one list: mixing them let a noisy tag outvote the key, so
    "what was my port setting again?" scored the same as a tag-only coincidence.
    """
    lowered = (key or "").lower()
    if lowered and lowered in " ".join(sentence_tokens):
        return 10.0

    key_frac = _coverage(sentence_tokens, key_parts(key))
    if key_frac <= 0.0 and not tags:
        return 0.0
    tag_frac = _coverage(sentence_tokens, tokens(tags))
    # Covering every part of the key is worth more than the sum of its parts.
    bonus = 1.5 if key_frac > 0.999 else 0.0
    return 4.0 * key_frac + bonus + 2.0 * tag_frac


def rewrite(sentence: str, keys_: Iterable[tuple[str, str]],
            limit: int = 3) -> list[tuple[str, float]]:
    """Map a raw sentence onto candidate keys, best first.

    ``keys_`` is an iterable of ``(key, tags)``. This never touches disk and never
    consults the gate - it only decides *which* keys are worth asking about.
    """
    sentence_tokens = tokens(sentence)
    if not sentence_tokens:
        return []
    scored = []
    for key, tags in keys_:
        score = score_key(sentence_tokens, key, tags)
        if score >= MIN_SCORE:
            scored.append((key, score))
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored[:max(1, int(limit))]


# ---------------------------------------------------------------- the engine


def _norm_key(key: str) -> str:
    """The one way every public method normalises a key.

    ``remember`` used to truncate over-long keys to ``MAX_KEY_CHARS`` while
    ``lookup``/``forget`` only stripped them, so a >120-char key was stored under
    a shortened name that could never be read back or deleted by the name the
    caller used.
    """
    return (key or "").strip()[:MAX_KEY_CHARS]


class MemoryEngine:
    """DuckDB vault + RAM Bloom gate + RAM key index. Thread-safe.

    One DuckDB connection guarded by one re-entrant lock: DuckDB connections are
    not safe to use from several threads at once, and TrioForge serves the web UI
    from a thread pool.

    DuckDB also allows only ONE writing process per file. If another TrioForge
    (the web app while ``forge`` is open, say) already holds the vault, this
    reopens it read-only instead of failing, and reports ``writable = False``.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = Path(db_path or DB_PATH)
        self._lock = threading.RLock()
        self._conn = None
        self._gate: Optional[BloomGate] = None
        self._index: dict[str, str] = {}          # key -> tags, needed by rewrite
        self.writable = True
        self.reason = ""
        self.counters = {
            "lookups": 0,        # gated lookups asked for
            "gate_skips": 0,     # answered NO in RAM: no disk touched
            "gate_misses": 0,    # said YES, query found nothing (false positive)
            "db_reads": 0,       # rows actually fetched
            "writes": 0,
            "rebuilds": 0,
            "recalls": 0,
            "skipped_values": 0,  # recall candidates rejected by the gate
        }

    # -- connection
    def _db(self):
        """Open (once) and return the DuckDB connection."""
        if self._conn is not None:
            return self._conn

        import duckdb

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = duckdb.connect(str(self.db_path))
        except Exception as exc:  # noqa: BLE001 - usually the cross-process lock
            if not _is_lock_error(exc):
                raise
            conn = duckdb.connect(str(self.db_path), read_only=True)
            self.writable = False
            self.reason = ("another TrioForge process holds the vault; "
                           "opened read-only")
        self._conn = conn
        if self.writable:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ai_harness_memory (
                    memory_key VARCHAR PRIMARY KEY,
                    context_data TEXT NOT NULL,
                    tags VARCHAR DEFAULT '',
                    created_at TIMESTAMP DEFAULT current_timestamp,
                    updated_at TIMESTAMP DEFAULT current_timestamp
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_filter (
                    id INTEGER PRIMARY KEY,
                    capacity INTEGER NOT NULL,
                    fp DOUBLE NOT NULL,
                    bits BLOB NOT NULL,
                    index_blob BLOB NOT NULL,
                    updated_at TIMESTAMP DEFAULT current_timestamp
                )
            """)
        # Read-only: the other process owns the tables, so just read what is there.
        self._load()
        return conn

    def _load(self) -> None:
        """Read the gate + key index back from ONE small row.

        This is the whole point of persisting the filter: boot is a single row
        read, not a scan over every stored value.
        """
        row = None
        try:
            row = self._conn.execute(
                "SELECT capacity, fp, bits, index_blob FROM memory_filter "
                "WHERE id = 1").fetchone()
        except Exception:  # noqa: BLE001 - no filter table yet (very first boot)
            row = None

        if row is not None:
            capacity, fp, bits, index_blob = row
            self._gate = BloomGate(capacity, fp, bits=bytes(bits))
            try:
                self._index = dict(json.loads(bytes(index_blob).decode("utf-8")))
            except Exception:  # noqa: BLE001 - unreadable index: rebuild it
                self._index = {}
            self._gate.nkeys = len(self._index)
            if self._index:
                # A crash between a write and the filter save leaves the persisted
                # index blob out of step with the table: the row exists but the
                # gate/index do not know it, so after a restart the key is
                # permanently invisible. Trust the table's row count - one cheap
                # COUNT(*) - and rebuild only when it disagrees with the blob.
                count = None
                try:
                    count = self._conn.execute(
                        "SELECT COUNT(*) FROM ai_harness_memory").fetchone()[0]
                except Exception:  # noqa: BLE001 - table may not exist yet
                    count = None
                if count == len(self._index):
                    return
        else:
            self._gate = BloomGate()

        self._reindex()

    def _reindex(self) -> None:
        """Rebuild the gate and RAM index from the vault's key column.

        Only runs on the first boot, after a corruption, or when the filter is
        grown - never on every start. Values are not read: keys and tags only.
        """
        self._index = {}
        try:
            rows = self._conn.execute(
                "SELECT memory_key, tags FROM ai_harness_memory").fetchall()
        except Exception:  # noqa: BLE001 - table missing on a fresh vault
            rows = []
        for key, tags in rows:
            self._index[str(key)] = str(tags or "")
        self._gate = BloomGate(self._grow_to(len(self._index)))
        for key in self._index:
            self._gate.add(key)
        self._save_filter()

    def _grow_to(self, count: int) -> int:
        """Pick a capacity that keeps the false-positive rate near the target."""
        capacity = DEFAULT_CAPACITY
        while capacity < max(1, count) * 2:
            capacity *= 2
        return capacity

    def _save_filter(self) -> None:
        if not self.writable:
            return
        index_blob = json.dumps(self._index, separators=(",", ":")).encode("utf-8")
        self._conn.execute(
            "INSERT INTO memory_filter (id, capacity, fp, bits, index_blob, "
            "updated_at) VALUES (1, ?, ?, ?, ?, current_timestamp) "
            "ON CONFLICT (id) DO UPDATE SET capacity = excluded.capacity, "
            "fp = excluded.fp, bits = excluded.bits, "
            "index_blob = excluded.index_blob, updated_at = excluded.updated_at",
            [self._gate.capacity, self._gate.fp, self._gate.to_bytes(),
             index_blob])

    def _maybe_rebuild(self) -> None:
        """Grow the filter before it saturates, so the gate stays meaningful."""
        if self._gate.nkeys <= self._gate.capacity and self._gate.fill < MAX_FILL:
            return
        self.counters["rebuilds"] += 1
        gate = BloomGate(self._grow_to(len(self._index)))
        for key in self._index:
            gate.add(key)
        self._gate = gate
        self._save_filter()

    # -- writes
    def remember(self, key: str, value: str, tags: str = "") -> dict:
        """Store one entry and flip its bits in the gate."""
        key = _norm_key(key)
        if not key:
            return {"ok": False, "error": "a memory needs a key"}
        value = value if isinstance(value, str) else str(value)
        truncated = len(value) > MAX_VALUE_CHARS
        if truncated:
            value = value[:MAX_VALUE_CHARS]

        with self._lock:
            self._db()
            if not self.writable:
                return {"ok": False, "error": self.reason}
            self._conn.execute(
                "INSERT INTO ai_harness_memory (memory_key, context_data, tags, "
                "updated_at) VALUES (?, ?, ?, current_timestamp) "
                "ON CONFLICT (memory_key) DO UPDATE SET "
                "context_data = excluded.context_data, tags = excluded.tags, "
                "updated_at = excluded.updated_at",
                [key, value, tags or ""])
            existed = key in self._index
            self._index[key] = tags or ""
            if not existed:
                self._gate.add(key)       # an overwrite cannot add a new key
            self.counters["writes"] += 1
            self._maybe_rebuild()
            self._save_filter()
            return {"ok": True, "key": key, "updated": existed,
                    "truncated": truncated, "keys": len(self._index)}

    def forget(self, key: str) -> bool:
        """Delete one entry.

        A Bloom filter cannot clear a bit - other keys share those positions - so
        the gate keeps saying YES for the deleted key. Lookups for it then miss in
        DuckDB, which is a false positive, not a wrong answer.
        """
        key = _norm_key(key)
        with self._lock:
            self._db()
            if not self.writable:
                return False
            found = self._conn.execute(
                "SELECT 1 FROM ai_harness_memory WHERE memory_key = ?",
                [key]).fetchone() is not None
            if not found:
                return False
            self._conn.execute(
                "DELETE FROM ai_harness_memory WHERE memory_key = ?", [key])
            self._index.pop(key, None)
            self.counters["writes"] += 1
            self._save_filter()
            return True

    # -- reads
    def lookup(self, key: str) -> Optional[str]:
        """Gate first, then one indexed row fetch. Returns None when absent."""
        key = _norm_key(key)
        if not key:
            return None
        with self._lock:
            self._db()
            self.counters["lookups"] += 1
            if not self._gate.contains(key):
                self.counters["gate_skips"] += 1
                return None
            row = self._conn.execute(
                "SELECT context_data FROM ai_harness_memory WHERE memory_key = ?",
                [key]).fetchone()
            self.counters["db_reads"] += 1
            if row is None:
                self.counters["gate_misses"] += 1
                return None
            return row[0]

    def keys(self, prefix: str = "") -> list[str]:
        """Every stored key, from RAM - no query."""
        with self._lock:
            self._db()
            if not prefix:
                return sorted(self._index)
            low = prefix.lower()
            return sorted(k for k in self._index if k.lower().startswith(low))

    def entries(self, prefix: str = "", limit: int = 40) -> list[tuple[str, str]]:
        """``(key, value)`` rows for the UI, newest first, with a hard limit."""
        keys = self.keys(prefix)
        if not keys:
            return []
        with self._lock:
            self._db()
            marks = ",".join("?" for _ in keys)
            # LIMIT in SQL: slicing the (alphabetically sorted) keys first took
            # the first N by NAME and only then sorted by time, so "newest first"
            # returned an arbitrary alphabetical slice. Limit after ORDER BY.
            rows = self._conn.execute(
                f"SELECT memory_key, context_data FROM ai_harness_memory "
                f"WHERE memory_key IN ({marks}) ORDER BY updated_at DESC LIMIT ?",
                keys + [max(1, int(limit))]).fetchall()
            self.counters["db_reads"] += len(rows)
            return [(str(k), str(v)) for k, v in rows]

    def recall(self, sentence: str, limit: int = 3) -> list[tuple[str, str, float]]:
        """Sentence in, matching entries out. This is the keyword scanner.

        The sentence is rewritten into candidate keys in RAM; only then is each
        candidate offered to the gate, and only a candidate the gate accepts costs
        a query. Candidates the gate rejects are counted as skipped, not read.
        """
        with self._lock:
            self._db()
            self.counters["recalls"] += 1
            candidates = rewrite(sentence, self._index.items(), limit=limit)
            hits: list[tuple[str, str, float]] = []
            for key, score in candidates:
                self.counters["lookups"] += 1
                if not self._gate.contains(key):
                    self.counters["gate_skips"] += 1
                    self.counters["skipped_values"] += 1
                    continue
                row = self._conn.execute(
                    "SELECT context_data FROM ai_harness_memory "
                    "WHERE memory_key = ?", [key]).fetchone()
                self.counters["db_reads"] += 1
                if row is None:
                    self.counters["gate_misses"] += 1
                    continue
                hits.append((key, row[0], score))
            return hits

    def context(self, sentence: str, limit: int = 3,
                budget: int = MAX_CONTEXT_CHARS) -> str:
        """Recall formatted for a prompt, or "" when nothing matches."""
        hits = self.recall(sentence, limit=limit)
        if not hits:
            return ""
        lines = ["Remembered facts:"]
        used = len(lines[0])
        for key, value, _score in hits:
            entry = f"- {key}: {value.strip()}"
            if used + len(entry) > budget:
                lines.append(f"- ... {len(hits)} matches, budget reached")
                break
            lines.append(entry)
            used += len(entry) + 1
        return "\n".join(lines)

    # -- reporting
    def stats(self) -> dict:
        with self._lock:
            self._db()
            try:
                size = self.db_path.stat().st_size
            except OSError:
                size = 0
            lookups = self.counters["lookups"]
            return {
                "db": str(self.db_path),
                "db_bytes": size,
                "writable": self.writable,
                "reason": self.reason,
                "keys": len(self._index),
                "capacity": self._gate.capacity,
                "bits": self._gate._bits_count,
                "bytes": len(self._gate.bits),
                "hashes": self._gate.hashes,
                "fill": self._gate.fill,
                "target_fp": self._gate.fp,
                "lookups": lookups,
                "gate_skips": self.counters["gate_skips"],
                "gate_misses": self.counters["gate_misses"],
                "db_reads": self.counters["db_reads"],
                "writes": self.counters["writes"],
                "rebuilds": self.counters["rebuilds"],
                "recalls": self.counters["recalls"],
                "saved_pct": (100.0 * self.counters["gate_skips"] / lookups)
                             if lookups else 0.0,
            }


def _is_lock_error(exc: Exception) -> bool:
    """True when DuckDB refused the file because another process holds it."""
    text = str(exc).lower()
    return "lock" in text or "conflict" in text


# ------------------------------------------------------------- module façade

_ENGINE: Optional[MemoryEngine] = None
_ENGINE_LOCK = threading.Lock()


def available() -> bool:
    """True when the DuckDB driver is importable."""
    try:
        import duckdb  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def missing_reason() -> str:
    from common import pip_hint
    return ("the memory vault needs DuckDB. Run: "
            + pip_hint("duckdb")
            + "  (or re-run install.sh / install.ps1)")


def engine() -> MemoryEngine:
    """The process-wide engine, opened on first use."""
    global _ENGINE
    if _ENGINE is None:
        with _ENGINE_LOCK:
            if _ENGINE is None:
                _ENGINE = MemoryEngine()
    return _ENGINE


def reset() -> None:
    """Drop the cached engine. For tests, and after the vault file changes."""
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is not None:
            try:
                if _ENGINE._conn is not None:
                    _ENGINE._conn.close()
            except Exception:  # noqa: BLE001
                pass
        _ENGINE = None


def remember(key: str, value: str, tags: str = "") -> dict:
    return engine().remember(key, value, tags)


def lookup(key: str) -> Optional[str]:
    return engine().lookup(key)


def forget(key: str) -> bool:
    return engine().forget(key)


def keys(prefix: str = "") -> list[str]:
    return engine().keys(prefix)


def entries(prefix: str = "", limit: int = 40) -> list[tuple[str, str]]:
    return engine().entries(prefix, limit)


def recall(sentence: str, limit: int = 3) -> list[tuple[str, str, float]]:
    return engine().recall(sentence, limit)


def context(sentence: str, limit: int = 3, budget: int = MAX_CONTEXT_CHARS) -> str:
    return engine().context(sentence, limit, budget)


def stats() -> dict:
    return engine().stats()
