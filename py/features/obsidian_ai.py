# obsidian_ai.py – point TrioForge at your Obsidian vault.
#
# notes.py already imports/exports the whole vault. This adds the AI-facing half:
#   GET  /api/obsidian/status            – is the vault found? how many notes?
#   GET  /api/obsidian/search?q=...      – search note names + contents
#   GET  /api/obsidian/note?path=...     – read one note
#   POST /api/obsidian/save              – save an AI reply as a new note
#   POST /api/obsidian/context           – build context from relevant notes (for chat)

import os
import re
import logging
from datetime import datetime
from pathlib import Path

from flask import Blueprint, request, jsonify

from features.notes import get_vault_path, set_vault_path

logger = logging.getLogger(__name__)

obsidian_bp = Blueprint('obsidian_ai', __name__, url_prefix='/api/obsidian')

MAX_NOTE_BYTES = 2 * 1024 * 1024        # refuse absurd files
SNIPPET = 240


def _candidates():
    home = Path.home()
    return [
        home / "Documents" / "Obsidian Vault",
        home / "Obsidian Vault",
        home / "Obsidian",
        home / "Documents" / "Obsidian",
        home / "vault",
    ]


def _vault():
    """Configured vault if valid, else the first auto-detected one."""
    configured = None
    try:
        configured = get_vault_path()
    except Exception:
        configured = None
    if configured and os.path.isdir(configured):
        return os.path.normpath(configured)
    for path in _candidates():
        if path.is_dir():
            return str(path)
    return None


def _notes(vault):
    """Every .md file in the vault, skipping Obsidian's own internals."""
    out = []
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if d not in (".obsidian", ".trash", ".git")]
        for name in files:
            if name.lower().endswith(".md"):
                out.append(os.path.join(root, name))
    return out


def _safe_join(vault, rel):
    """Resolve rel inside the vault, refusing anything that escapes it."""
    target = os.path.normpath(os.path.join(vault, rel))
    if not target.startswith(os.path.normpath(vault) + os.sep):
        return None
    return target


def _slug(title):
    slug = re.sub(r'[^\w\s-]', '', title or '').strip().replace(' ', '-')
    return slug or "note"


@obsidian_bp.route('/status', methods=['GET'])
def status():
    vault = _vault()
    if not vault:
        return jsonify({"found": False, "vault": None, "notes": 0,
                        "hint": "Set the vault path or create ~/Documents/Obsidian Vault"})
    notes = _notes(vault)
    return jsonify({"found": True, "vault": vault, "notes": len(notes)})


@obsidian_bp.route('/search', methods=['GET'])
def search():
    vault = _vault()
    if not vault:
        return jsonify({"error": "no vault found"}), 404
    query = (request.args.get('q') or '').strip()
    try:
        limit = max(1, min(int(request.args.get('limit', 15)), 100))
    except ValueError:
        limit = 15
    if not query:
        return jsonify({"query": "", "results": []})

    needle = query.lower()
    scored = []
    for path in _notes(vault):
        rel = os.path.relpath(path, vault)
        title = os.path.splitext(os.path.basename(path))[0]
        try:
            text = Path(path).read_text(encoding='utf-8', errors='replace')
        except Exception:
            continue
        low = text.lower()
        hits = low.count(needle)
        name_hit = needle in title.lower()
        if not hits and not name_hit:
            continue
        idx = low.find(needle)
        snippet = ""
        if idx >= 0:
            snippet = text[max(0, idx - SNIPPET // 2): idx + SNIPPET].replace('\n', ' ').strip()
        scored.append({
            "path": rel.replace(os.sep, '/'),
            "title": title,
            "hits": hits,
            "score": hits + (5 if name_hit else 0),
            "snippet": snippet,
        })

    scored.sort(key=lambda r: (-r["score"], r["title"].lower()))
    return jsonify({"query": query, "results": scored[:limit]})


@obsidian_bp.route('/note', methods=['GET'])
def read_note():
    vault = _vault()
    if not vault:
        return jsonify({"error": "no vault found"}), 404
    rel = (request.args.get('path') or '').strip()
    if not rel:
        return jsonify({"error": "path is required"}), 400
    target = _safe_join(vault, rel)
    if not target or not os.path.isfile(target):
        return jsonify({"error": "note not found: %s" % rel}), 404
    if os.path.getsize(target) > MAX_NOTE_BYTES:
        return jsonify({"error": "note is too large to open"}), 413
    try:
        text = Path(target).read_text(encoding='utf-8', errors='replace')
    except Exception as exc:
        return jsonify({"error": "could not read note: %s" % exc}), 500
    return jsonify({"path": rel, "content": text,
                    "modified": datetime.fromtimestamp(
                        os.path.getmtime(target)).isoformat(timespec='seconds')})


@obsidian_bp.route('/save', methods=['POST'])
def save_note():
    vault = _vault()
    if not vault:
        return jsonify({"error": "no vault found"}), 404
    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip() or "TrioForge note"
    content = data.get('content') or ''
    folder = (data.get('folder') or '').strip()
    tags = data.get('tags') or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(',') if t.strip()]
    if not content.strip():
        return jsonify({"error": "content is required"}), 400

    rel = os.path.join(folder, _slug(title) + ".md") if folder else _slug(title) + ".md"
    target = _safe_join(vault, rel)
    if not target:
        return jsonify({"error": "invalid destination folder"}), 400

    # Never clobber an existing note - add a numeric suffix instead.
    base, ext = os.path.splitext(target)
    n = 2
    while os.path.exists(target):
        target = "%s-%d%s" % (base, n, ext)
        n += 1

    front = ""
    if tags:
        front = "---\ntags: [%s]\ncreated: %s\n---\n\n" % (
            ", ".join(tags), datetime.now().isoformat(timespec='seconds'))

    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        Path(target).write_text(front + content.rstrip() + "\n", encoding='utf-8')
    except Exception as exc:
        return jsonify({"error": "could not write note: %s" % exc}), 500

    return jsonify({"ok": True, "path": os.path.relpath(target, vault).replace(os.sep, '/')})


@obsidian_bp.route('/context', methods=['POST'])
def context():
    """Return concatenated excerpts of the most relevant notes, for the chat to use."""
    vault = _vault()
    if not vault:
        return jsonify({"error": "no vault found"}), 404
    data = request.get_json(silent=True) or {}
    query = (data.get('query') or '').strip()
    try:
        limit = max(1, min(int(data.get('limit', 5)), 20))
    except (TypeError, ValueError):
        limit = 5
    if not query:
        return jsonify({"error": "query is required"}), 400

    needle = query.lower()
    picked = []
    for path in _notes(vault):
        try:
            text = Path(path).read_text(encoding='utf-8', errors='replace')
        except Exception:
            continue
        if needle in text.lower():
            picked.append((text.lower().count(needle),
                           os.path.relpath(path, vault), text))
    picked.sort(key=lambda t: -t[0])

    blocks = []
    for _, rel, text in picked[:limit]:
        blocks.append("### %s\n%s" % (rel, text.strip()[:2000]))
    return jsonify({"query": query, "notes_used": len(blocks),
                    "context": "\n\n".join(blocks)})
