"""Plugins, skills, MCP servers, extensions and connectors: the /api/plugins, /api/skills,
/api/mcp, /api/extensions and /api/connectors routes."""
import os
import re
import subprocess

from flask import Blueprint, jsonify, request

import extensions
import mcp_client
import plugin_loader
import skills_loader

extensions_bp = Blueprint("extensions_bp", __name__)


@extensions_bp.route('/api/plugins', methods=['GET'])
def api_plugins():
    """List loaded plugins (for the services/plugins panel)."""
    return jsonify(plugin_loader.list_loaded())


@extensions_bp.route('/api/skills', methods=['GET'])
def api_skills():
    """List installed skills (name + description; bodies load on demand)."""
    return jsonify({"skills": skills_loader.list_loaded()})


@extensions_bp.route('/api/skills/reload', methods=['POST'])
def api_skills_reload():
    """Re-scan skills/ so a dropped-in file takes effect without a restart."""
    results = skills_loader.load_all()
    errors = [r for r in results if r.get("error")]
    return jsonify({
        "count": len(skills_loader.all_skills()),
        "errors": errors,
        "skills": skills_loader.list_loaded(),
    })


@extensions_bp.route('/api/skills/open-folder', methods=['POST'])
def api_skills_open_folder():
    """Reveal skills/ in the file manager so adding one is drag-and-drop."""
    path = skills_loader.SKILLS_DIR
    try:
        os.makedirs(path, exist_ok=True)
        if os.name == 'nt':
            os.startfile(path)                       # noqa: S606 (Windows only)
        else:
            subprocess.Popen(['xdg-open', path])
        return jsonify({"ok": True, "path": path})
    except Exception as e:
        return jsonify({"error": str(e)})


@extensions_bp.route('/api/skills/<name>', methods=['GET'])
def api_skill_body(name):
    """The full instructions of one skill, for the UI's detail view."""
    skill = skills_loader.get(name)
    if skill is None:
        return jsonify({"error": "No skill named '{}'.".format(name)}), 404
    return jsonify({k: v for k, v in skill.items()})


@extensions_bp.route('/api/mcp', methods=['GET'])
def api_mcp():
    """List configured MCP servers and how each one is doing."""
    return jsonify({
        "servers": mcp_client.list_servers(),
        "config": mcp_client.config_path(),
    })


@extensions_bp.route('/api/mcp/servers', methods=['POST'])
def api_mcp_add():
    """Add or replace one MCP server, then connect in the background."""
    body = request.get_json(silent=True) or {}
    sid = (body.get("id") or "").strip()
    sid = re.sub(r"[^A-Za-z0-9._-]+", "-", sid).strip("-")
    if not sid:
        return jsonify({"error": "An id is required."}), 400

    spec = {}
    for key in ("command", "url", "cwd", "token"):
        value = (body.get(key) or "").strip() if isinstance(body.get(key), str) else body.get(key)
        if value:
            spec[key] = value
    for key in ("args", "env", "headers"):
        value = body.get(key)
        if value:
            spec[key] = value
    if "enabled" in body:
        spec["enabled"] = bool(body["enabled"])

    if not spec.get("command") and not spec.get("url"):
        return jsonify({"error": "Give either a command (local server) or a url (remote server)."}), 400
    if spec.get("args") and not isinstance(spec["args"], list):
        return jsonify({"error": "args must be a list."}), 400

    return jsonify(mcp_client.add_server(sid, spec))


@extensions_bp.route('/api/mcp/servers/<sid>', methods=['DELETE'])
def api_mcp_remove(sid):
    """Remove a server from the config and stop it."""
    return jsonify({"removed": mcp_client.remove_server(sid)})


@extensions_bp.route('/api/mcp/reconnect', methods=['POST'])
def api_mcp_reconnect():
    """Retry every configured server."""
    return jsonify({"servers": mcp_client.reconnect_all()})


@extensions_bp.route('/api/extensions', methods=['GET'])
def api_extensions():
    """One inventory of everything installed: skills, plugins and MCP servers."""
    return jsonify({"extensions": extensions.inventory()})


@extensions_bp.route('/api/extensions/catalog', methods=['GET'])
def api_extensions_catalog():
    """The browsable catalog shipped with the app, each entry with installed state."""
    return jsonify({"catalog": extensions.catalog()})


@extensions_bp.route('/api/extensions/toggle', methods=['POST'])
def api_extensions_toggle():
    """Enable or disable one extension."""
    body = request.get_json(silent=True) or {}
    kind = (body.get("kind") or "").strip()
    sid = (body.get("id") or "").strip()
    if not kind or not sid:
        return jsonify({"error": "kind and id are required."}), 400
    return jsonify(extensions.set_enabled(kind, sid, bool(body.get("enabled", True))))


@extensions_bp.route('/api/extensions/install', methods=['POST'])
def api_extensions_install():
    """Install from a local folder or a Git URL."""
    body = request.get_json(silent=True) or {}
    return jsonify(extensions.install(body.get("source", "")))


@extensions_bp.route('/api/extensions/<kind>/<sid>', methods=['DELETE'])
def api_extensions_remove(kind, sid):
    """Uninstall one extension."""
    return jsonify(extensions.remove(kind, sid))


@extensions_bp.route('/api/connectors', methods=['GET'])
def api_connectors():
    """List the CONNECTORS: services the user signs into, not every plugin with tools.

    A plugin is the package; a connector is the account it manages. Gmail is a
    connector; a hypothetical "read local files" plugin that exposes tools is a
    plugin, not a connector, and belongs in the Plugins panel instead.
    """
    items = []
    for p in plugin_loader.list_loaded():
        if not p.get("connector"):
            continue
        items.append({
            "id": p.get("id"),
            "title": p.get("title", p.get("id")),
            "description": p.get("description", ""),
            "tools": p.get("tool_names") or [],
            "credentials": p.get("credentials") or [],
            "guide": p.get("guide") or [],
            "enable_url": p.get("enable_url") or "",
            "consent_url": p.get("consent_url") or "",
        })
    return jsonify({"connectors": items})
