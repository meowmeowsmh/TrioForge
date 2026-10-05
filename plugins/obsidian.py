"""obsidian.py — a TrioForge CONNECTOR for a local Obsidian vault.

Gives the agent read and write access to the notes in one Obsidian vault, so
"what did I write about memory management?" or "add that to my notes" works on
real files instead of being a refusal.

This is deliberately NOT the Obsidian community-plugin format. An Obsidian
plugin (manifest.json + main.js) is code that runs *inside Obsidian*; this is a
TrioForge plugin that runs inside TrioForge and reads the vault's folder. The two
are unrelated, which is why an Obsidian plugin never appears in TrioForge's list.

Safety: every path is resolved and confined to the vault, only .md files are
touched, and the vault's own config (.obsidian), trash and VCS folders are never
written. Nothing leaves the machine - this is pure local file access.
"""

import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CRED_PATH = os.path.join(REPO_ROOT, "json_configuration", "obsidian_credentials.json")

#: Folders inside a vault that are not notes.
SKIP_DIRS = {".obsidian", ".trash", ".git", ".smart-env", "node_modules", ".stfolder"}

#: Refuse to hand the model an enormous note, and cap how much a search reads.
MAX_NOTE_BYTES = 400_000
MAX_SEARCH_SCAN = 3000
MAX_RESULTS = 100

MANIFEST = {
    "name": "obsidian",
    "title": "Obsidian",
    "version": "1.0.0",
    "description": "Read, search and write notes in a local Obsidian vault.",
    "connector": True,
    "credentials": [
        {
            "key": "vault",
            "label": "Vault folder (the one containing .obsidian)",
            "type": "text",
            "placeholder": "D:\\Store\\james",
            "hint": "Full path to the vault folder. No account or sign-in is needed - "
                    "this is a folder on this machine.",
        },
    ],
    "guide": [
        "1. Find your vault folder: in Obsidian, right-click the vault name -> "
        "'Show in system explorer'. It is the folder that contains a hidden .obsidian folder.",
        "2. Paste that full path here -> Save settings.",
        "3. Ask the agent something like 'search my notes for memory management' or "
        "'append this to my Essay note'.",
        "Notes are plain .md files, so you can also edit them by hand - nothing is hidden "
        "in a database.",
    ],
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "obsidian_search",
            "description": "Search the Obsidian vault for notes matching a term, by file name "
                           "and by content. Use this first when the user refers to their notes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "text to look for"},
                    "max": {"type": "integer", "description": "max matches (default 20)"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "obsidian_read",
            "description": "Read one note from the vault by its path relative to the vault, "
                           "e.g. 'Essay.md' or 'Data/notes.md'.",
            "parameters": {
                "type": "object",
                "properties": {"note": {"type": "string", "description": "note path, .md optional"}},
                "required": ["note"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "obsidian_write",
            "description": "Create a note, or overwrite it completely. For adding to an existing "
                           "note, use obsidian_append instead - overwriting loses what was there.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {"type": "string", "description": "note path, .md optional"},
                    "content": {"type": "string", "description": "full markdown content"},
                },
                "required": ["note", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "obsidian_append",
            "description": "Append markdown to the end of a note, creating it if it does not exist. "
                           "This is the safe way to add to a note the user already has.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {"type": "string", "description": "note path, .md optional"},
                    "content": {"type": "string", "description": "markdown to add"},
                },
                "required": ["note", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "obsidian_list",
            "description": "List notes in the vault, optionally under one folder.",
            "parameters": {
                "type": "object",
                "properties": {"folder": {"type": "string", "description": "sub-folder, empty for the whole vault"}},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "obsidian_status",
            "description": "Check which Obsidian vault is connected and how many notes it holds.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]


# ── config ────────────────────────────────────────────────────────────────────
def _cfg():
    if os.path.isfile(_CRED_PATH):
        try:
            with open(_CRED_PATH, encoding="utf-8") as fh:
                return json.load(fh) or {}
        except Exception:
            return {}
    return {}


def _save_cfg(updates):
    cfg = _cfg()
    cfg.update(updates)
    os.makedirs(os.path.dirname(_CRED_PATH), exist_ok=True)
    with open(_CRED_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


def _detect_vault():
    """Best-effort guess, so an unconfigured connector can still be useful.

    Looks for a folder containing .obsidian in the usual places, one level deep -
    enough to find "~/Documents/MyVault" without walking a whole drive.
    """
    roots = [
        os.environ.get("OBSIDIAN_VAULT", ""),
        os.path.expanduser("~"),
        os.path.join(os.path.expanduser("~"), "Documents"),
        os.path.join(os.path.expanduser("~"), "Obsidian"),
        os.path.join(os.path.expanduser("~"), "Desktop"),
    ]
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        if os.path.isdir(os.path.join(root, ".obsidian")):
            return root
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for entry in entries:
            child = os.path.join(root, entry)
            if os.path.isdir(os.path.join(child, ".obsidian")):
                return child
    return None


def _vault():
    """The configured vault folder. Returns (path, error)."""
    raw = (_cfg().get("vault") or "").strip()
    if not raw:
        guess = _detect_vault()
        if guess:
            return guess, None
        return None, ("No vault set. Paste the folder that contains .obsidian into the "
                      "Obsidian connector, or set the OBSIDIAN_VAULT environment variable.")
    path = os.path.abspath(os.path.expanduser(raw))
    if not os.path.isdir(path):
        return None, "That folder does not exist: {}".format(path)
    if not os.path.isdir(os.path.join(path, ".obsidian")) and not _count_notes(path):
        return None, ("{} has no .obsidian folder and no markdown notes - is that the "
                      "vault root?".format(path))
    return path, None


# ── vault access ──────────────────────────────────────────────────────────────
def _count_notes(vault):
    total = 0
    for dirpath, dirnames, filenames in os.walk(vault):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        total += sum(1 for f in filenames if f.lower().endswith(".md"))
        if total > 5:
            return total
    return total


def _all_notes(vault):
    """Every note, as a vault-relative posix path, sorted."""
    out = []
    for dirpath, dirnames, filenames in os.walk(vault):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name.lower().endswith(".md"):
                full = os.path.join(dirpath, name)
                out.append(os.path.relpath(full, vault).replace(os.sep, "/"))
    out.sort()
    return out


def _safe_note(vault, name):
    """Resolve a note name inside the vault. Returns (full_path, error).

    Confinement is the point: the model supplies this string, so `..`, an
    absolute path, or a drive letter must all be rejected rather than trusted.
    """
    rel = (name or "").strip().replace("\\", "/")
    if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
        return None, "Give the note path relative to the vault, e.g. 'Essay.md'."
    if not rel.lower().endswith(".md"):
        rel += ".md"
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts:
        return None, "A note name is required."
    if any(p == ".." for p in parts):
        return None, "That path leaves the vault."
    if any(p in SKIP_DIRS for p in parts):
        return None, "That is not a note (it is vault configuration)."
    base = os.path.abspath(vault)
    full = os.path.abspath(os.path.join(base, *parts))
    if full != base and not full.startswith(base + os.sep):
        return None, "That path leaves the vault."
    return full, None


def _rel(vault, full):
    return os.path.relpath(full, vault).replace(os.sep, "/")


def _status():
    vault, err = _vault()
    if err:
        return {"connected": False, "configured": bool((_cfg().get("vault") or "").strip()),
                "needs_setup": True, "error": err, "account": None, "method": "folder"}
    notes = _all_notes(vault)
    return {"connected": True, "configured": True, "needs_setup": False,
            "account": vault, "method": "folder", "notes": len(notes)}


def _list_notes(folder):
    vault, err = _vault()
    if err:
        return {"error": err}
    notes = _all_notes(vault)
    sub = (folder or "").strip().strip("/\\")
    if sub:
        prefix = sub.replace("\\", "/") + "/"
        notes = [n for n in notes if n.startswith(prefix)]
    total = len(notes)
    return {"vault": vault, "count": total, "notes": notes[:MAX_RESULTS],
            "truncated": total > MAX_RESULTS}


def _read_note(note):
    vault, err = _vault()
    if err:
        return {"error": err}
    full, err = _safe_note(vault, note)
    if err:
        return {"error": err}
    if not os.path.isfile(full):
        return {"error": "No note at '{}'.".format(_rel(vault, full)), "vault": vault}
    try:
        size = os.path.getsize(full)
        if size > MAX_NOTE_BYTES:
            return {"error": "That note is {:.0f} KB, too big to read whole.".format(size / 1024)}
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as e:
        return {"error": "Could not read it: {}".format(e)}
    return {"note": _rel(vault, full), "characters": len(text), "content": text}


def _write_note(note, content, append=False):
    vault, err = _vault()
    if err:
        return {"error": err}
    full, err = _safe_note(vault, note)
    if err:
        return {"error": err}
    existed = os.path.isfile(full)
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        if append:
            prefix = ""
            if existed and os.path.getsize(full) > 0:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    if not fh.read().endswith("\n"):
                        prefix = "\n"
            with open(full, "a", encoding="utf-8") as fh:
                fh.write(prefix + (content or "") + "\n")
        else:
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(content or "")
    except OSError as e:
        return {"error": "Could not write it: {}".format(e)}
    return {"note": _rel(vault, full), "action": ("appended" if append else "written"),
            "created": not existed, "bytes": os.path.getsize(full)}


def _search(query, max_results):
    vault, err = _vault()
    if err:
        return {"error": err}
    q = (query or "").strip().lower()
    if not q:
        return {"error": "A search term is required."}
    limit = max(1, min(int(max_results or 20), MAX_RESULTS))

    by_name, by_content = [], []
    notes = _all_notes(vault)
    for rel in notes:
        if q in rel.lower():
            by_name.append(rel)
        if len(by_name) >= limit and len(by_content) >= limit:
            break
    # Content search reads files, so it is bounded: names first, then content up
    # to a cap, newest-first would need mtimes and this stays predictable instead.
    for rel in notes[:MAX_SEARCH_SCAN]:
        if len(by_content) >= limit:
            break
        if rel in by_name:
            continue
        full = os.path.join(vault, rel.replace("/", os.sep))
        try:
            if os.path.getsize(full) > MAX_NOTE_BYTES:
                continue
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        idx = text.lower().find(q)
        if idx != -1:
            start = max(0, idx - 60)
            snippet = " ".join(text[start:idx + 120].split())
            by_content.append({"note": rel, "snippet": snippet})

    return {"query": query, "vault": vault, "vault_notes": len(notes),
            "match_name": by_name[:limit],
            "match_content": by_content[:limit]}


def dispatch(tool_name, args):
    args = args or {}
    if tool_name == "obsidian_status":
        return _status()
    if tool_name == "obsidian_list":
        return _list_notes(args.get("folder"))
    if tool_name == "obsidian_read":
        return _read_note(args.get("note"))
    if tool_name == "obsidian_write":
        return _write_note(args.get("note"), args.get("content"), append=False)
    if tool_name == "obsidian_append":
        return _write_note(args.get("note"), args.get("content"), append=True)
    if tool_name == "obsidian_search":
        return _search(args.get("query"), args.get("max"))
    return {"error": "unknown tool " + str(tool_name)}


def connect_info():
    """The TUI's /connectors view: a folder needs no sign-in, so this is status."""
    st = _status()
    if st.get("connected"):
        return {"connected": True, "account": st.get("account"), "method": "folder"}
    return {"error": st.get("error") or "no vault configured"}


def register(app):
    """Add this connector's HTTP routes.

    Every view function is prefixed `_obsidian_` on purpose: Flask keys routes by
    the view function's NAME, not by path, so a bare `_status_route` here would
    collide with Gmail's and make one of the two connectors fail to load
    entirely. Unique names are what let two connectors coexist.
    """
    from flask import jsonify, request

    @app.route("/api/connectors/obsidian/status")
    def _obsidian_status_route():
        return jsonify(_status())

    @app.route("/api/connectors/obsidian/credentials", methods=["POST"])
    def _obsidian_credentials_route():
        data = request.get_json(silent=True) or {}
        vault = (data.get("vault") or "").strip().strip('"')
        if not vault:
            return jsonify({"error": "A vault folder is required."}), 400
        path = os.path.abspath(os.path.expanduser(vault))
        if not os.path.isdir(path):
            return jsonify({"error": "That folder does not exist: {}".format(path)}), 400
        if not os.path.isdir(os.path.join(path, ".obsidian")) and not _count_notes(path):
            return jsonify({"error": "{} does not look like an Obsidian vault - it has no "
                                     ".obsidian folder and no notes.".format(path)}), 400
        _save_cfg({"vault": path})
        return jsonify({"ok": True, "vault": path, "notes": _status().get("notes")})

    @app.route("/api/connectors/obsidian/notes")
    def _obsidian_notes_route():
        return jsonify(_list_notes(request.args.get("folder", "")))

    @app.route("/api/connectors/obsidian/note")
    def _obsidian_note_route():
        return jsonify(_read_note(request.args.get("path", "")))

    @app.route("/api/connectors/obsidian/disconnect", methods=["POST"])
    def _obsidian_disconnect_route():
        _save_cfg({"vault": ""})
        return jsonify({"ok": True})
