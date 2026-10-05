"""Unified extension inventory for TrioForge.

Three things can be installed - a Skill (Markdown), a Plugin (Python), and an
MCP server (a config). Each has its own loader, but to the user they are one
category: *things I added to make TrioForge do more*. This module is the single
inventory they all appear in, and the single place they are enabled, disabled,
removed and installed - Obsidian's community-plugins list, but for three file
formats instead of one.

**Enable/disable is a rename, not a config file.** Every loader already skips
entries whose name starts with an underscore, so disabling a skill or plugin is
``_tdd`` instead of ``tdd``. That has two virtues over a hidden setting list:
the state is visible in the file manager (``skills/_tdd`` is obviously off), and
there is no registry that can drift out of sync with what is actually on disk.

Native plugins are imported and registered at startup, so toggling one needs a
restart to take effect - the same as Obsidian, which restarts plugins on toggle.
Skills and MCP servers apply immediately (skills are re-read per load, MCP
connections are torn down and rebuilt).
"""

import ast
import json
import logging
import os
import shutil
import subprocess
import tempfile
from typing import Dict, List, Optional, Tuple

import plugin_loader
import skills_loader
import mcp_client
from paths import root_path

logger = logging.getLogger(__name__)

SKILL = "skill"
PLUGIN = "plugin"
MCP = "mcp"

SKILLS_DIR = skills_loader.SKILLS_DIR
PLUGINS_DIR = plugin_loader.PLUGINS_DIR

#: The browsable catalog shipped with the app: catalog.json plus a folder per
#: entry. Installing from it is a local copy, so it works offline and can never
#: 404 the way a remote package can.
CATALOG_DIR = root_path("catalog")
CATALOG_FILE = os.path.join(CATALOG_DIR, "catalog.json")

#: Installed plugin paths to send back are relative to the project so they can
#: be shown without leaking an absolute path, yet stay clickable in the UI.
_PROJECT = root_path()


def _rel(path: str) -> str:
    try:
        return os.path.relpath(path, _PROJECT).replace(os.sep, "/")
    except ValueError:
        # On Windows, relpath raises across drives (C: vs D:). A path outside the
        # project is legitimate (a skill installed elsewhere), so show it whole.
        return str(path).replace(os.sep, "/")


# ── reading metadata off disk (for disabled items, which never get imported) ──

def _manifest_meta(path: str) -> dict:
    """Read a plugin's MANIFEST via ast, so a disabled plugin is described
    without importing it (importing runs its module body)."""
    meta = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            tree = ast.parse(fh.read())
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if "MANIFEST" in targets:
                    try:
                        value = ast.literal_eval(node.value)
                    except Exception:
                        continue
                    if isinstance(value, dict):
                        meta = value
                        break
    except Exception as e:
        logger.warning("Could not read manifest of %s: %s", path, e)
    return meta


def _disabled_skills() -> List[dict]:
    out = []
    try:
        entries = sorted(os.listdir(SKILLS_DIR))
    except OSError:
        return out
    for entry in entries:
        if not entry.startswith("_") or entry == "_":
            continue
        # entry is "_tdd" (a folder) or "_note.md". Its live name drops the "_".
        full = os.path.join(SKILLS_DIR, entry)
        if os.path.isdir(full):
            target = os.path.join(full, "SKILL.md")
            if not os.path.isfile(target):
                md = [f for f in os.listdir(full) if f.lower().endswith(".md")]
                target = os.path.join(full, md[0]) if md else None
        elif entry.lower().endswith(".md"):
            target = full
        else:
            continue
        name = entry[1:]
        if entry.lower().endswith(".md"):
            name = name[:-3]
        title, description = name, ""
        if target and os.path.isfile(target):
            info = skills_loader._load_skill(target, name)
            title = info.get("title") or name
            description = info.get("description", "")
        out.append({"kind": SKILL, "id": name, "title": title,
                    "description": description, "enabled": False,
                    "path": _rel(full), "target": _rel(full)})
    return out


def _disabled_plugins() -> List[dict]:
    out = []
    try:
        entries = sorted(os.listdir(PLUGINS_DIR))
    except OSError:
        return out
    for entry in entries:
        if not (entry.startswith("_") and entry.endswith(".py")):
            continue
        full = os.path.join(PLUGINS_DIR, entry)
        meta = _manifest_meta(full)
        pid = meta.get("name") or entry[1:-3]
        out.append({
            "kind": PLUGIN, "id": pid, "title": meta.get("title", pid),
            "description": meta.get("description", ""),
            "enabled": False, "path": _rel(full), "target": _rel(full),
            "connector": bool(meta.get("connector")) or bool(meta.get("credentials")),
            "needs_restart": True,
        })
    return out


# ── inventory ───────────────────────────────────────────────────────────────

def inventory() -> List[dict]:
    """Every installed extension, enabled and disabled, from every source."""
    out = []

    for s in skills_loader.all_skills():
        out.append({"kind": SKILL, "id": s["id"], "title": s.get("title", s["id"]),
                    "description": s.get("description", ""), "enabled": True,
                    "path": _rel(s["path"]), "target": _rel(s["path"]),
                    "chars": s.get("chars", 0), "needs_restart": False})
    out.extend(_disabled_skills())

    for p in plugin_loader.list_loaded():
        filepath = os.path.join(PLUGINS_DIR, p.get("file", ""))
        out.append({"kind": PLUGIN, "id": p["id"], "title": p.get("title", p["id"]),
                    "description": p.get("description", ""), "enabled": True,
                    "path": _rel(filepath), "target": _rel(filepath),
                    "connector": bool(p.get("connector")),
                    "tool_names": p.get("tool_names") or [],
                    "needs_restart": True})
    out.extend(_disabled_plugins())

    for m in mcp_client.list_servers():
        out.append({"kind": MCP, "id": m["id"], "title": m["id"],
                    "description": "MCP server ({}): {} tool(s)".format(
                        m["transport"], m["tool_count"]),
                    "enabled": m["enabled"],
                    "path": mcp_client.CONFIG_PATH, "target": mcp_client.CONFIG_PATH,
                    "status": m["status"], "error": m.get("error", ""),
                    "tool_names": m.get("tool_names") or [],
                    "needs_restart": False})

    return out


# ── enable / disable ─────────────────────────────────────────────────────────

def _toggle_skill(sid: str, enable: bool) -> Tuple[bool, str]:
    # The skill is either a folder skills/<id> or a file skills/<id>.md.
    candidates = [os.path.join(SKILLS_DIR, sid),
                  os.path.join(SKILLS_DIR, sid + ".md")]
    if not enable:
        candidates = [c for c in candidates if os.path.exists(c)]
        if not candidates:
            return False, "No skill named '{}' is installed.".format(sid)
        src = candidates[0]
        dst = os.path.join(SKILLS_DIR, "_" + os.path.basename(src))
    else:
        disabled = [os.path.join(SKILLS_DIR, "_" + sid),
                    os.path.join(SKILLS_DIR, "_" + sid + ".md")]
        disabled = [c for c in disabled if os.path.exists(c)]
        if not disabled:
            return False, "No disabled skill named '{}' was found.".format(sid)
        src = disabled[0]
        dst = os.path.join(SKILLS_DIR, os.path.basename(src)[1:])
    os.rename(src, dst)
    return True, dst


def _toggle_plugin(pid: str, enable: bool) -> Tuple[bool, str]:
    if not enable:
        src = _plugin_path_by_id(pid)
        if src is None:
            return False, "No plugin named '{}' is installed.".format(pid)
        dst = os.path.join(PLUGINS_DIR, "_" + os.path.basename(src))
    else:
        for entry in sorted(os.listdir(PLUGINS_DIR)):
            if entry.startswith("_") and entry.endswith(".py"):
                full = os.path.join(PLUGINS_DIR, entry)
                meta = _manifest_meta(full)
                if (meta.get("name") or entry[1:-3]) == pid:
                    src = full
                    dst = os.path.join(PLUGINS_DIR, entry[1:])
                    break
        else:
            return False, "No disabled plugin named '{}' was found.".format(pid)
    os.rename(src, dst)
    return True, dst


def _plugin_path_by_id(pid: str) -> Optional[str]:
    for info in plugin_loader.list_loaded():
        if info["id"] == pid:
            return os.path.join(PLUGINS_DIR, info.get("file", ""))
    for entry in sorted(os.listdir(PLUGINS_DIR)):
        if entry.endswith(".py") and not entry.startswith("_"):
            meta = _manifest_meta(os.path.join(PLUGINS_DIR, entry))
            if (meta.get("name") or entry[:-3]) == pid:
                return os.path.join(PLUGINS_DIR, entry)
    return None


def set_enabled(kind: str, sid: str, enabled: bool) -> dict:
    if kind == MCP:
        if not mcp_client.set_enabled(sid, enabled):
            return {"error": "No MCP server named '{}' is configured.".format(sid)}
        return {"ok": True, "restart": False}
    try:
        if kind == SKILL:
            ok, target = _toggle_skill(sid, enabled)
        elif kind == PLUGIN:
            ok, target = _toggle_plugin(sid, enabled)
        else:
            return {"error": "Unknown kind '{}'.".format(kind)}
    except OSError as e:
        return {"error": str(e)}
    if not ok:
        return {"error": target}
    if kind == SKILL:
        skills_loader.load_all()
    if kind == PLUGIN and enabled:
        # Only disabling is safe at runtime; enabling would need an import that
        # can add routes, and registering twice raises, so both go the restart way.
        pass
    return {"ok": True, "path": _rel(target),
            "restart": (kind == PLUGIN)}


# ── remove ───────────────────────────────────────────────────────────────────

def remove(kind: str, sid: str) -> dict:
    if kind == MCP:
        return {"ok": True, "removed": mcp_client.remove_server(sid)}
    if kind == SKILL:
        candidates = [os.path.join(SKILLS_DIR, sid), os.path.join(SKILLS_DIR, sid + ".md"),
                      os.path.join(SKILLS_DIR, "_" + sid),
                      os.path.join(SKILLS_DIR, "_" + sid + ".md")]
    elif kind == PLUGIN:
        path = _plugin_path_by_id(sid)
        if path is None:
            # it may already be disabled (_prefixed)
            for entry in sorted(os.listdir(PLUGINS_DIR)):
                if entry.startswith("_") and entry.endswith(".py") and \
                        (_manifest_meta(os.path.join(PLUGINS_DIR, entry)).get("name")
                         or entry[1:-3]) == sid:
                    path = os.path.join(PLUGINS_DIR, entry)
                    break
        candidates = [path] if path else []
    else:
        return {"error": "Unknown kind '{}'.".format(kind)}

    removed = []
    for cand in candidates:
        if cand and os.path.exists(cand):
            try:
                if os.path.isdir(cand):
                    shutil.rmtree(cand)
                else:
                    os.remove(cand)
                removed.append(_rel(cand))
            except OSError as e:
                return {"error": "Could not remove {}: {}".format(cand, e)}
    if not removed:
        return {"error": "Nothing to remove for '{}'.".format(sid)}
    if kind == SKILL:
        skills_loader.load_all()
    return {"ok": True, "removed": removed, "restart": (kind == PLUGIN)}


# ── catalog ──────────────────────────────────────────────────────────────────

def _catalog_installed(kind: str, eid: str) -> bool:
    if kind == "skill":
        return skills_loader.get(eid) is not None
    if kind == "plugin":
        return any(p["id"] == eid for p in plugin_loader.list_loaded())
    if kind == MCP:
        return any(m["id"] == eid for m in mcp_client.list_servers())
    return False


def catalog() -> List[dict]:
    """The browsable catalog, each entry with its installed state."""
    try:
        with open(CATALOG_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        eid = e.get("id", "")
        out.append({
            "id": eid,
            "title": e.get("title", eid),
            "kind": e.get("kind", "skill"),
            "description": e.get("description", ""),
            "author": e.get("author", ""),
            "installed": _catalog_installed(e.get("kind", "skill"), eid),
        })
    return out


# ── install ──────────────────────────────────────────────────────────────────

def _git_clone(url: str, dest: str) -> None:
    if not shutil.which("git"):
        raise RuntimeError("Git is required to install from a URL, and it is not on PATH.")
    subprocess.run(["git", "clone", "--depth", "1", url, dest],
                   check=True, capture_output=True, text=True, encoding="utf-8",
                   errors="replace")


def _is_skill_asset(path: str) -> bool:
    if os.path.isdir(path):
        return os.path.isfile(os.path.join(path, "SKILL.md")) or \
            any(f.lower().endswith(".md") for f in os.listdir(path))
    return path.lower().endswith(".md")


def _is_plugin_asset(path: str) -> bool:
    if not path.endswith(".py") or os.path.basename(path).startswith("_"):
        return False
    meta = _manifest_meta(path)
    return bool(meta)


def install(source: str) -> dict:
    """Install extensions from a local folder or a Git URL.

    The source may contain ``skills/`` and ``plugins/`` subfolders (a bundle), or
    loose ``SKILL.md`` files and ``*.py`` plugins at its top level. It is never
    executed during install - it is copied in, and plugins run only on restart.
    """
    src = (source or "").strip()
    if not src:
        return {"error": "Give a folder path, a Git URL, or a catalog: name."}

    tmp = None
    catalog_id = None
    try:
        if src.startswith("catalog:"):
            catalog_id = src[len("catalog:"):].strip()
            root = os.path.join(CATALOG_DIR, catalog_id)
            if not os.path.isdir(root):
                return {"error": "No catalog entry named '{}'.".format(catalog_id)}
        elif src.startswith(("http://", "https://", "git@", "ssh://", "git://")):
            tmp = tempfile.mkdtemp(prefix="trioforge-install-")
            _git_clone(src, tmp)
            root = tmp
        else:
            if not os.path.isdir(src):
                return {"error": "That is not a folder, and not a Git URL: {}".format(src)}
            root = src
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or "").strip().splitlines()
        return {"error": "git clone failed: {}".format(detail[-1] if detail else e)}
    except Exception as e:
        return {"error": str(e)}

    installed = {"skills": [], "plugins": [], "skipped": []}

    def _collect(root_dir: str):
        if not os.path.isdir(root_dir):
            return
        for entry in sorted(os.listdir(root_dir)):
            if entry.startswith(".") or entry == "__pycache__":
                continue
            full = os.path.join(root_dir, entry)
            if os.path.isdir(full) and entry.lower() == "skills":
                for sub in sorted(os.listdir(full)):
                    p = os.path.join(full, sub)
                    if _is_skill_asset(p):
                        installed["skills"].append(_install_skill(p, sub))
            elif os.path.isdir(full) and entry.lower() == "plugins":
                for sub in sorted(os.listdir(full)):
                    p = os.path.join(full, sub)
                    if _is_plugin_asset(p):
                        installed["plugins"].append(_install_plugin(p))
            elif _is_skill_asset(full):
                installed["skills"].append(_install_skill(full, entry))
            elif _is_plugin_asset(full):
                installed["plugins"].append(_install_plugin(full))
            else:
                installed["skipped"].append(entry)

    if catalog_id and os.path.isfile(os.path.join(root, "SKILL.md")):
        # A catalog entry is one skill folder whose id is its own name; install
        # the folder as skills/<id>, not its SKILL.md as a stray loose file.
        installed["skills"].append(_install_skill(root, catalog_id))
    else:
        _collect(root)
        # A repo may nest everything under one more directory (the top-level
        # README plus a subfolder), so if nothing was recognised, look one deeper.
        if not installed["skills"] and not installed["plugins"]:
            try:
                children = [os.path.join(root, d) for d in os.listdir(root)
                            if os.path.isdir(os.path.join(root, d))]
            except OSError:
                children = []
            for child in children:
                _collect(child)

    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)

    if not installed["skills"] and not installed["plugins"]:
        return {"error": "Nothing installable was found there. It must contain "
                         "skills (SKILL.md) and/or plugins (*.py with a MANIFEST)."}
    if installed["skills"]:
        skills_loader.load_all()
    return {"ok": True, **installed,
            "restart": bool(installed["plugins"])}


def _install_skill(path: str, fallback: str) -> str:
    name = fallback
    if os.path.isdir(path):
        name = name.rstrip("/\\")
        if name.startswith("_"):
            name = name[1:]
        dest = os.path.join(SKILLS_DIR, name)
        if os.path.exists(dest):
            shutil.rmtree(dest)
        shutil.copytree(path, dest)
    else:
        name = os.path.splitext(name)[0].lstrip("_")
        dest = os.path.join(SKILLS_DIR, os.path.basename(path))
        shutil.copy2(path, dest)
    return name


def _install_plugin(path: str) -> str:
    dest = os.path.join(PLUGINS_DIR, os.path.basename(path).lstrip("_"))
    shutil.copy2(path, dest)
    meta = _manifest_meta(dest)
    return meta.get("name") or os.path.splitext(os.path.basename(dest))[0]
