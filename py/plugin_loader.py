"""
Plugin loader for TrioForge.

A plugin is a single Python file placed in the top-level `plugins/` folder.
It may (optionally) define:

    MANIFEST = {
        "name": "my-plugin",          # required — unique id
        "title": "My Plugin",         # optional — human name
        "version": "1.0.0",           # optional
        "description": "…",           # optional
    }

    def register(app):                # optional — receives the Flask app
        ...                           # add routes, register blueprints, etc.

Plugins are loaded at startup (best-effort: a broken plugin is reported and
skipped, it never crashes the app). The app exposes `/api/plugins` so the UI
can list what loaded.

Security note: plugins are trusted code that runs inside the app process.
Only install plugins you trust, exactly like browser extensions or VSCode
extensions.
"""

import importlib.util
import logging
import os
import sys
import traceback
from typing import Dict, List

from paths import root_path

logger = logging.getLogger(__name__)

PLUGINS_DIR = root_path("plugins")

_loaded: Dict[str, dict] = {}

# Agent-facing tools that plugins expose. A plugin may define TOOLS (a list of
# OpenAI function-calling tool objects) plus a dispatch(tool_name, args) callable;
# the loader collects them here so the agent loop can offer and run them.
_tool_defs: List[dict] = []
_tool_owners: Dict[str, dict] = {}


def _load_plugin(path: str) -> dict:
    """Import one plugin file and call its register() hook (if any)."""
    name = os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location("trioforge_plugin_" + name, path)
    if spec is None or spec.loader is None:
        return {"id": name, "error": "could not build import spec"}
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        return {"id": name, "error": f"import failed: {e}"}

    manifest = getattr(mod, "MANIFEST", None) or {}
    if not isinstance(manifest, dict):
        manifest = {}
    pid = manifest.get("name") or name

    register = getattr(mod, "register", None)
    if register is not None:
        try:
            # Import Flask lazily here to avoid a hard dependency for plugins
            # that only want to be registered (the app passes itself in).
            register(_app())
        except Exception as e:
            return {"id": pid, "error": f"register() failed: {e}", "traceback": traceback.format_exc()}

    tools = getattr(mod, "TOOLS", None) or []
    dispatch = getattr(mod, "dispatch", None)
    creds = manifest.get("credentials") or []
    return {
        "id": pid,
        "title": manifest.get("title", pid),
        "version": manifest.get("version", ""),
        "description": manifest.get("description", ""),
        "file": os.path.basename(path),
        "tools": tools,
        "dispatch": dispatch,
        "credentials": creds,
        "guide": manifest.get("guide") or [],
    }


_app_ref = None


def set_app(app):
    """Store the Flask app instance so plugins' register() receives it."""
    global _app_ref
    _app_ref = app


def _app():
    return _app_ref


def load_all(app) -> List[dict]:
    """Load every plugin in plugins/ (idempotent-ish; returns the result list)."""
    set_app(app)
    results = []
    _loaded.clear()
    _tool_defs.clear()
    _tool_owners.clear()
    os.makedirs(PLUGINS_DIR, exist_ok=True)
    try:
        entries = sorted(os.listdir(PLUGINS_DIR))
    except OSError as e:
        logger.warning("Cannot list plugins dir: %s", e)
        return results

    for entry in entries:
        if not entry.endswith(".py") or entry.startswith("_"):
            continue
        path = os.path.join(PLUGINS_DIR, entry)
        if not os.path.isfile(path):
            continue
        try:
            info = _load_plugin(path)
        except Exception as e:
            info = {"id": entry, "error": str(e)}
        if info.get("error"):
            logger.warning("Plugin %s failed to load: %s", entry, info["error"])
        else:
            _loaded[info["id"]] = info
            logger.info("Loaded plugin: %s", info["id"])
            _register_tools(info)
        results.append(info)
    return results


def _register_tools(info: dict) -> None:
    """Index the tools a loaded plugin advertises so the agent can run them."""
    pid = info.get("id")
    for tool in info.get("tools") or []:
        fn = (tool.get("function") or {})
        name = fn.get("name")
        if not name:
            continue
        _tool_defs.append(tool)
        _tool_owners[name] = {"plugin": pid, "dispatch": info.get("dispatch")}


def collect_tools() -> List[dict]:
    """Every agent-facing tool the loaded plugins expose."""
    return list(_tool_defs)


def execute_tool(name: str, args: dict):
    """Run a plugin tool by name; returns None when it is not a plugin tool."""
    owner = _tool_owners.get(name)
    if owner is None:
        return None
    dispatch = owner.get("dispatch")
    if dispatch is None:
        return {"error": "plugin '{}' has no dispatch for '{}'".format(owner["plugin"], name)}
    try:
        result = dispatch(name, args)
    except Exception as e:
        return {"error": "{}: {}".format(type(e).__name__, e)}
    if not isinstance(result, dict):
        result = {"result": result}
    return result


def list_loaded() -> List[dict]:
    # `dispatch` is a callable, so it cannot be jsonify'd; the UI only needs the
    # metadata plus a tool-name list.
    out = []
    for info in _loaded.values():
        clean = {k: v for k, v in info.items() if k != "dispatch"}
        names = [t.get("function", {}).get("name") for t in (info.get("tools") or [])]
        clean["tool_names"] = [n for n in names if n]
        out.append(clean)
    return out
