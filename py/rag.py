"""
RAG (Retrieval-Augmented Generation) for TrioForge.

Lets users upload documents (PDF, Word, text, code, Markdown) and then ask
questions about them. Documents are split into overlapping chunks, embedded
(lazily, when sentence-transformers is installed), and the most relevant chunks
are injected into the chat prompt.

Storage: an in-memory store plus a small SQLite table (`rag_chunks`) so the
index survives a restart. The SQLite lives in `sqlite_data/rag.db`.

If the heavy embedding stack (sentence-transformers / numpy / sklearn) is NOT
installed, we fall back to a fast lexical (keyword-overlap) scorer, so document
chat still works out of the box with zero extra dependencies.
"""

import os
import re
import threading
import logging
from typing import List, Optional, Tuple

from common import embed_text, get_conn
from paths import root_path

logger = logging.getLogger(__name__)

_rag_lock = threading.Lock()
_rag_initialized = False
_rag_init_lock = threading.Lock()

_DB_PATH = root_path("sqlite_data", "rag.db")

CHUNK_SIZE = 600      # characters per chunk
CHUNK_OVERLAP = 120   # overlap between chunks
TOP_K = 6             # chunks retrieved per query


def _conn():
    """Return a per-thread SQLite connection (common.get_conn is thread-local).

    Flask serves each request on a different thread, so we must NOT cache a
    single connection globally — SQLite objects can only be used on the thread
    that created them.
    """
    global _rag_initialized
    if not _rag_initialized:
        with _rag_init_lock:
            if not _rag_initialized:
                os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
                c = get_conn(_DB_PATH)
                c.execute("""
                    CREATE TABLE IF NOT EXISTS rag_chunks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        doc_name TEXT NOT NULL,
                        chunk_index INTEGER NOT NULL,
                        content TEXT NOT NULL,
                        embedding TEXT
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_rag_doc ON rag_chunks(doc_name)")
                c.commit()
                _rag_initialized = True
    return get_conn(_DB_PATH)


def _chunk_text(text: str) -> List[str]:
    """Split text into overlapping chunks at paragraph/sentence boundaries."""
    text = (text or "").strip()
    if not text:
        return []
    # Normalise whitespace but keep line structure for code/markdown.
    text = text.replace("\r\n", "\n")
    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + CHUNK_SIZE, n)
        if end < n:
            # Try to break at a paragraph, then a newline, then a sentence.
            for sep in ("\n\n", "\n", ". "):
                pos = text.rfind(sep, start, end)
                if pos > start + CHUNK_SIZE // 2:
                    end = pos + len(sep)
                    break
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= n:
            break
        start = max(start + 1, end - CHUNK_OVERLAP)
    return chunks


def _extract_text(name: str, data: bytes) -> str:
    """Extract plain text from a raw file by name/extension."""
    lower = (name or "").lower()
    if lower.endswith(".pdf"):
        try:
            from pypdf import PdfReader
            import io
            reader = PdfReader(io.BytesIO(data))
            return "\n\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception as e:
            logger.warning("PDF extract failed for %s: %s", name, e)
            return ""
    if lower.endswith((".docx",)):
        try:
            # Minimal .docx text extraction (it's a zip of XML).
            import zipfile
            import io
            from xml.etree import ElementTree
            z = zipfile.ZipFile(io.BytesIO(data))
            parts = []
            for item in z.namelist():
                if item.startswith("word/") and item.endswith(".xml") and "document" in item:
                    root = ElementTree.fromstring(z.read(item))
                    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
                    for t in root.iter(ns + "t"):
                        if t.text:
                            parts.append(t.text)
            return "\n".join(parts)
        except Exception as e:
            logger.warning("DOCX extract failed for %s: %s", name, e)
            return ""
    # Everything else: treat as UTF-8 text (covers .txt/.md/.py/.js/.json/.csv/.html).
    try:
        return data.decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning("Text decode failed for %s: %s", name, e)
        return ""


def _lexical_score(query_terms: List[str], chunk: str) -> float:
    """Cheap keyword-overlap score (fallback when embeddings aren't available)."""
    low = chunk.lower()
    score = 0.0
    for term in query_terms:
        if term in low:
            score += 1.0
    return score


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def index_document(name: str, data: bytes) -> int:
    """Index one document; returns the number of chunks stored.

    Raises a ValueError with a clear message when no text could be extracted
    (e.g. a scanned/image-only PDF, an encrypted PDF, or an unsupported binary).
    """
    text = _extract_text(name, data)
    if not text.strip():
        ext = os.path.splitext(name or "")[1].lower()
        if ext == ".pdf":
            raise ValueError(
                "Could not extract text from this PDF. It may be scanned/image-only "
                "(no text layer) or encrypted. For scanned PDFs, OCR is required — "
                "paste the text into a .txt/.md file instead."
            )
        raise ValueError(
            f"Could not extract text from {name or 'file'}. It may be an unsupported "
            "or binary format. Supported: PDF, DOCX, TXT, MD, code, CSV, HTML."
        )
    chunks = _chunk_text(text)
    if not chunks:
        raise ValueError("Document produced no usable text chunks.")
    c = _conn()
    with _rag_lock:
        # Replace any previous chunks for this doc so re-uploads don't duplicate.
        c.execute("DELETE FROM rag_chunks WHERE doc_name = ?", (name,))
        for i, chunk in enumerate(chunks):
            emb = embed_text(chunk)
            c.execute(
                "INSERT INTO rag_chunks (doc_name, chunk_index, content, embedding) VALUES (?,?,?,?)",
                (name, i, chunk, _emb_str(emb)),
            )
        c.commit()
    return len(chunks)


def _emb_str(emb: Optional[List[float]]) -> Optional[str]:
    if emb is None:
        return None
    import json
    return json.dumps(emb)


def _emb_list(raw: Optional[str]) -> Optional[List[float]]:
    if not raw:
        return None
    import json
    try:
        return json.loads(raw)
    except Exception:
        return None


def list_documents() -> List[dict]:
    """Return indexed documents with chunk counts."""
    c = _conn()
    rows = c.execute(
        "SELECT doc_name, COUNT(*) AS n FROM rag_chunks GROUP BY doc_name ORDER BY doc_name"
    ).fetchall()
    return [{"name": r["doc_name"], "chunks": r["n"]} for r in rows]


def delete_document(name: str) -> bool:
    c = _conn()
    with _rag_lock:
        c.execute("DELETE FROM rag_chunks WHERE doc_name = ?", (name,))
        c.commit()
    return True


def retrieve(query: str, top_k: int = TOP_K) -> List[str]:
    """Return the most relevant chunk texts for `query`."""
    if not query.strip():
        return []
    c = _conn()
    rows = c.execute("SELECT doc_name, content, embedding FROM rag_chunks").fetchall()
    if not rows:
        return []
    query_terms = [t.lower() for t in re.findall(r"[A-Za-z0-9_]+", query) if len(t) > 2]
    q_emb = embed_text(query)

    scored: List[Tuple[float, str]] = []
    for r in rows:
        content = r["content"]
        if q_emb is not None:
            emb = _emb_list(r["embedding"])
            score = _cosine(q_emb, emb) if emb else _lexical_score(query_terms, content)
        else:
            score = _lexical_score(query_terms, content)
        if score > 0:
            scored.append((score, content))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [content for _, content in scored[:top_k]]


def build_context(query: str, top_k: int = TOP_K) -> str:
    """Return a prompt-ready context string of relevant chunks (or '')."""
    chunks = retrieve(query, top_k)
    if not chunks:
        return ""
    parts = []
    for i, c in enumerate(chunks, 1):
        parts.append(f"[Doc snippet {i}]\n{c}")
    return "\n\n".join(parts)
