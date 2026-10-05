"""Skills loader for TrioForge.

A *skill* is Markdown that teaches the model how to do one job well. It is the
same idea as a Claude "plugin" whose only content is instructions: no Python, no
build step, no registry - drop a folder in ``skills/`` and it exists.

Layout (both forms are accepted, so a one-file skill needs no folder):

    skills/
        frontend-design/
            SKILL.md
        commit.md

``SKILL.md`` starts with a small front-matter block::

    ---
    name: frontend-design
    description: One line, always visible to the model.
    when_to_use: Phrasing that should make the model reach for this skill.
    ---

    # Frontend Design
    ...the actual instructions...

Why front-matter instead of loading everything: **progressive disclosure**. Only
the name/description/when_to_use of every skill goes into the system prompt; the
body is fetched on demand through the ``use_skill`` tool. Advertising 60 skills
therefore costs a couple of hundred tokens, and the expensive part (the body) is
paid only for the one skill the model actually decided it needs. Loading every
body up front would blow the context window on a local model.

No YAML dependency: only flat ``key: value`` pairs are supported, which is all a
skill header needs. A file with no front-matter is still valid - its first
heading becomes the name and its first paragraph the description - so a plain
Markdown note can be dropped in as a skill.
"""

import logging
import os
import re
from typing import Dict, List

from paths import root_path

logger = logging.getLogger(__name__)

SKILLS_DIR = root_path("skills")

#: How many skills the system prompt advertises in full. Past this the model is
#: told to call ``list_skills`` instead - a reminder list longer than the task
#: stops being a hint and starts being noise.
CATALOGUE_LIMIT = 60

_loaded: Dict[str, dict] = {}

#: Skills that were found but could not be loaded, keyed by path. Kept so the UI
#: can say "this one failed, here is why" rather than the skill silently not
#: being there - a missing feature with no explanation is the worst outcome.
_failed: Dict[str, dict] = {}


def _slug(text: str) -> str:
    """Lowercase a name into a skill id. Empty when nothing usable survives.

    Names are normalised rather than rejected, so ``name: My Skill!`` is a usable
    ``my-skill`` instead of an error the user has to decode. Lookups go through
    the same function, so ``use_skill("My Skill!")`` still resolves.
    """
    return re.sub(r"[^a-z0-9._-]+", "-", (text or "").strip().lower()).strip("-")


def _parse_front_matter(text: str):
    """Split ``---`` front-matter from the body. Returns (meta, body)."""
    meta = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            block = text[3:end]
            body = text[end + 4:]
            for line in block.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                key, _, value = line.partition(":")
                value = value.strip().strip('"').strip("'")
                meta[key.strip().lower()] = value
    return meta, body.lstrip("\n")


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip()
        if line:
            return line[:80]
    return ""


def _first_paragraph(body: str) -> str:
    """First non-heading, non-empty run of text - the fallback description."""
    lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if lines:
                break
            continue
        if not stripped:
            if lines:
                break
            continue
        lines.append(stripped)
    return " ".join(lines)[:300]


def _load_skill(path: str, fallback_name: str):
    """Read one skill file. Returns a dict, or one with 'error' set."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError as e:
        return {"id": fallback_name, "error": "cannot read: {}".format(e)}

    meta, body = _parse_front_matter(raw)
    # A declared name wins, but a pile of punctuation in it falls back to the
    # filename rather than producing a skill nobody can refer to.
    name = _slug(meta.get("name") or "") or _slug(fallback_name) or "skill"
    description = meta.get("description") or _first_paragraph(body) or _first_heading(body)
    title = meta.get("title") or _first_heading(body) or name
    return {
        "id": name,
        "title": title,
        "description": description,
        "when_to_use": meta.get("when_to_use") or meta.get("when") or "",
        "triggers": meta.get("triggers") or "",
        "version": meta.get("version", ""),
        "body": body.strip(),
        "path": path,
        "chars": len(body),
    }


def _discover() -> List[tuple]:
    """Every candidate skill file. Returns [(path, fallback_name)] sorted."""
    found = []
    try:
        entries = sorted(os.listdir(SKILLS_DIR))
    except OSError:
        return found
    for entry in entries:
        if entry.startswith("_") or entry.startswith("."):
            continue
        full = os.path.join(SKILLS_DIR, entry)
        if os.path.isdir(full):
            # A folder skill: SKILL.md wins, otherwise the first .md in it.
            preferred = os.path.join(full, "SKILL.md")
            if os.path.isfile(preferred):
                found.append((preferred, entry))
                continue
            try:
                inner = sorted(f for f in os.listdir(full) if f.lower().endswith(".md"))
            except OSError:
                continue
            if inner:
                found.append((os.path.join(full, inner[0]), entry))
        elif entry.lower().endswith(".md") and entry.lower() != "readme.md":
            found.append((full, os.path.splitext(entry)[0]))
    # A folder skill (SKILL.md) wins a tie against a loose file of the same name,
    # so a stray copy dropped into skills/ cannot silently shadow the real one.
    found.sort(key=lambda pair: (os.path.basename(pair[0]) != "SKILL.md", pair[1]))
    return found


def load_all() -> List[dict]:
    """Load every skill in skills/. Best-effort: a broken one is skipped."""
    _loaded.clear()
    _failed.clear()
    results = []
    os.makedirs(SKILLS_DIR, exist_ok=True)
    for path, fallback in _discover():
        try:
            info = _load_skill(path, fallback)
        except Exception as e:  # a malformed file must never stop startup
            info = {"id": fallback, "path": path,
                    "error": "{}: {}".format(type(e).__name__, e)}
        if info.get("error"):
            logger.warning("Skill %s failed to load: %s", path, info["error"])
            _failed[path] = info
        elif info["id"] in _loaded:
            # The likely user error: a copied skill folder that was never renamed.
            info["error"] = "duplicate skill name '{}' (already loaded from {})".format(
                info["id"], _loaded[info["id"]].get("path"))
            logger.warning("Duplicate skill name: %s", info["id"])
            _failed[path] = info
        else:
            _loaded[info["id"]] = info
            logger.info("Loaded skill: %s", info["id"])
        results.append(info)
    return results


def all_skills() -> List[dict]:
    return list(_loaded.values())


def get(name: str):
    return _loaded.get(_slug(name or ""))


def _matches(info: dict, query: str) -> int:
    """Cheap keyword score, so list_skills(query=...) can narrow a long list."""
    terms = [t for t in re.split(r"[^a-z0-9]+", (query or "").lower()) if len(t) > 1]
    if not terms:
        return 1
    haystack = " ".join([
        info.get("id", ""), info.get("title", ""), info.get("description", ""),
        info.get("when_to_use", ""),
    ]).lower()
    body = (info.get("body") or "").lower()
    score = 0
    for term in terms:
        if term in haystack:
            score += 3
        elif term in body:
            score += 1
    return score


def list_loaded() -> List[dict]:
    """Metadata for the UI - the body is summarised, never shipped whole."""
    out = []
    for info in _loaded.values():
        clean = {k: v for k, v in info.items() if k != "body"}
        out.append(clean)
    return out


def list_failed() -> List[dict]:
    """Skills that were present but could not be loaded, with the reason.

    So a broken skill shows as failed instead of just being absent - "not
    installed" and "installed but broken" need completely different fixes.
    """
    out = []
    for info in _failed.values():
        clean = {k: v for k, v in info.items() if k != "body"}
        out.append(clean)
    return out


def catalogue_block() -> str:
    """The always-on advert: one line per skill, no bodies.

    Returns "" when nothing is installed so the caller can skip the whole
    section rather than leaving an empty heading in the prompt.
    """
    if not _loaded:
        return ""
    lines = []
    for info in sorted(_loaded.values(), key=lambda s: s["id"])[:CATALOGUE_LIMIT]:
        line = "- {}: {}".format(info["id"], info.get("description") or info.get("title", ""))
        if info.get("when_to_use"):
            line += " (use when: {})".format(info["when_to_use"])
        lines.append(line)
    block = (
        "You have SKILLS installed: extra instructions for specific jobs. This is "
        "the list - the contents are NOT loaded yet.\n"
        + "\n".join(lines)
        + "\nWhen a task matches a skill, call use_skill with its name FIRST and "
          "follow what it says. If nothing matches, work normally and do not call "
          "use_skill at all. Use list_skills to search when the list above is not "
          "enough."
    )
    if len(_loaded) > CATALOGUE_LIMIT:
        block += "\n({} more skills are installed - call list_skills to see them.)".format(
            len(_loaded) - CATALOGUE_LIMIT)
    return block


def system_block() -> str:
    return catalogue_block()


# Tool definitions follow the same OpenAI function-calling shape used by
# WORKSPACE_TOOLS and plugin TOOLS, so the agent loop treats skills, native
# plugins and MCP servers identically.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_skills",
            "description": "List or search the installed skills (extra instructions for specific jobs). "
                           "Returns names and descriptions only, not their contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "optional words to filter by, e.g. 'review' or 'css'"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "use_skill",
            "description": "Load the full instructions of one installed skill. Call this before starting a task "
                           "that a skill covers, then follow the returned instructions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "the skill id, as shown by list_skills"},
                },
                "required": ["name"], "additionalProperties": False,
            },
        },
    },
]


def collect_tools() -> List[dict]:
    """Skills advertise tools only when at least one skill is installed."""
    return list(TOOLS) if _loaded else []


def execute_tool(name: str, args: dict):
    """Run a skill tool. Returns None when the name is not ours."""
    if name not in ("list_skills", "use_skill"):
        return None
    args = args or {}

    if name == "list_skills":
        query = args.get("query") or ""
        scored = [(s, _matches(s, query)) for s in _loaded.values()]
        if query:
            scored = [(s, sc) for s, sc in scored if sc > 0]
        scored.sort(key=lambda pair: (-pair[1], pair[0]["id"]))
        return {
            "count": len(scored),
            "skills": [{
                "name": s["id"],
                "description": s.get("description", ""),
                "when_to_use": s.get("when_to_use", ""),
            } for s, _ in scored],
        }

    skill = get(args.get("name", ""))
    if skill is None:
        return {
            "error": "No skill named '{}'.".format(args.get("name", "")),
            "available": sorted(_loaded.keys()),
        }
    return {
        "skill": skill["id"],
        "instructions": skill["body"],
    }


# ── choosing skills for a message (manual + auto) ───────────────────────────

def match_text(text: str, limit: int = 3) -> List[dict]:
    """Skills whose declared triggers appear in the text, best match first.

    This is the deterministic auto path - it does not ask the model to volunteer,
    which is exactly what "auto" has to be if it is going to be trusted. A trigger
    of more than one word matches as a phrase; a single-word trigger matches on
    word boundaries, so a short trigger like ``ui`` cannot fire inside ``quick``.
    """
    text_l = (text or "").lower()
    if not text_l:
        return []
    scored = []
    for s in _loaded.values():
        raw = s.get("triggers") or ""
        triggers = [t.strip().lower() for t in raw.split(",") if t.strip()]
        hits = []
        for t in triggers:
            if " " in t:
                if t in text_l:
                    hits.append(t)
            elif re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", text_l):
                hits.append(t)
        # The id or title spoken verbatim is a strong signal even with no trigger,
        # so "do it with tdd" or "code review this" works on skills that ship none.
        named = any(tok and tok in text_l
                    for tok in (s.get("id", "").lower(), s.get("title", "").lower()))
        if hits or named:
            scored.append({"id": s["id"], "score": len(hits) + (1 if named else 0),
                           "matched": hits, "named": named})
    scored.sort(key=lambda x: (-x["score"], x["id"]))
    return scored[:limit]


def select_skills(message: str, explicit=(), limit: int = 3) -> List[dict]:
    """The skills to apply to one message: explicitly chosen first, then the best
    automatic match, deduped. Capped, because stacking skills is how a prompt
    turns into noise."""
    chosen: List[dict] = []
    for name in explicit:
        skill = get(name)
        if skill and skill["id"] not in [c["id"] for c in chosen]:
            chosen.append(skill)
    for match in match_text(message, limit=limit):
        skill = get(match["id"])
        if skill and skill["id"] not in [c["id"] for c in chosen]:
            chosen.append(skill)
        if len(chosen) >= limit:
            break
    return chosen


def skill_prompt_block(skills: List[dict]) -> str:
    """The text prepended to the user message that carries the chosen bodies."""
    if not skills:
        return ""
    parts = []
    for s in skills:
        parts.append("[SKILL: {id}]\n{body}\n[/SKILL: {id}]".format(
            id=s["id"], body=s["body"]))
    header = ("You have been given skill instructions for this task. Follow them "
              "closely. Where a skill instruction and the user's explicit request "
              "conflict, the user's request wins.")
    return header + "\n\n" + "\n\n".join(parts)
