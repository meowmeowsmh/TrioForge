# notes.py – advanced version with Markdown, tags, pinning, colours
import os
import json
import uuid
from datetime import datetime
from flask import Blueprint, request, jsonify, render_template_string

NOTES_FILE = "json_configuration/notes.json"
os.makedirs(os.path.dirname(NOTES_FILE), exist_ok=True)
if not os.path.exists(NOTES_FILE):
    with open(NOTES_FILE, "w", encoding="utf-8") as f:
        json.dump({}, f, indent=2)

def load_notes():
    with open(NOTES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_notes(notes):
    with open(NOTES_FILE, "w", encoding="utf-8") as f:
        json.dump(notes, f, indent=2)

notes_bp = Blueprint('notes', __name__, url_prefix='/notes')

# ---------- Serve the notes HTML page ----------
@notes_bp.route('')
def notes_page():
    return render_template_string(NOTES_HTML)

# ---------- API routes ----------
@notes_bp.route('/api', methods=['GET'])
def get_notes():
    return jsonify(load_notes())

@notes_bp.route('/api', methods=['POST'])
def create_note():
    data = request.get_json()
    notes = load_notes()
    note_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    notes[note_id] = {
        "id": note_id,
        "title": data.get("title", "Untitled"),
        "content": data.get("content", ""),
        "created": now,
        "last_modified": now,
        "order": len(notes),
        "tags": data.get("tags", []),
        "pinned": data.get("pinned", False),
        "color": data.get("color", "default")
    }
    save_notes(notes)
    return jsonify({"id": note_id, "ok": True})

@notes_bp.route('/api/<note_id>', methods=['PUT'])
def update_note(note_id):
    data = request.get_json()
    notes = load_notes()
    if note_id not in notes:
        return jsonify({"error": "Note not found"}), 404
    note = notes[note_id]
    # Update only provided fields
    if "title" in data:
        note["title"] = data["title"]
    if "content" in data:
        note["content"] = data["content"]
    if "tags" in data:
        note["tags"] = data["tags"]
    if "pinned" in data:
        note["pinned"] = data["pinned"]
    if "color" in data:
        note["color"] = data["color"]
    note["last_modified"] = datetime.now().isoformat()
    save_notes(notes)
    return jsonify({"ok": True})

@notes_bp.route('/api/<note_id>', methods=['DELETE'])
def delete_note(note_id):
    notes = load_notes()
    if note_id not in notes:
        return jsonify({"error": "Note not found"}), 404
    del notes[note_id]
    save_notes(notes)
    return jsonify({"ok": True})

@notes_bp.route('/api/search', methods=['GET'])
def search_notes():
    query = request.args.get('q', '').strip().lower()
    tag = request.args.get('tag', '').strip().lower()
    notes = load_notes()
    results = {}
    for nid, note in notes.items():
        # Filter by tag if provided
        if tag and tag not in [t.lower() for t in note.get("tags", [])]:
            continue
        # Search query
        if query:
            title_match = query in note.get('title', '').lower()
            content_match = query in note.get('content', '').lower()
            tag_match = any(query in t.lower() for t in note.get("tags", []))
            if not (title_match or content_match or tag_match):
                continue
        results[nid] = note
    return jsonify(results)

@notes_bp.route('/api/clear_all', methods=['POST'])
def clear_all_notes():
    save_notes({})
    return jsonify({"ok": True})

@notes_bp.route('/api/reorder', methods=['POST'])
def reorder_notes():
    data = request.get_json()
    order_map = data.get('order')
    if not order_map or not isinstance(order_map, dict):
        return jsonify({'error': 'Invalid order data'}), 400

    notes = load_notes()
    for nid, new_order in order_map.items():
        if nid in notes:
            notes[nid]['order'] = int(new_order)
    save_notes(notes)
    return jsonify({'ok': True})

# ---------- HTML template (improved) ----------
NOTES_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
    <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E📝%3C/text%3E%3C/svg%3E">
    <title>Notes · Advanced</title>
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <style>
        /* ── base – same as before, but with extra styles ── */
        * { margin:0; padding:0; box-sizing:border-box; }
        html, body {
            height:100%;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: #0a0a0f;
            color: #e1e4e8;
            overflow: hidden;
            transition: background 0.3s ease, color 0.3s ease;
        }
        body::before {
            content: '';
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: radial-gradient(ellipse at 20% 50%, #1a1a2e 0%, transparent 50%),
                        radial-gradient(ellipse at 80% 20%, #16213e 0%, transparent 50%);
            animation: bgMove 20s ease infinite;
            z-index: -1;
            transition: opacity 0.4s ease;
        }
        body.light-mode::before { opacity: 0; }
        @keyframes bgMove {
            0% { transform: scale(1); }
            50% { transform: scale(1.05); }
            100% { transform: scale(1); }
        }
        .app { display:flex; height:100%; backdrop-filter: blur(2px); }

        /* ── Sidebar (same structure) ── */
        .sidebar {
            width: 300px;
            background: rgba(18, 18, 26, 0.85);
            backdrop-filter: blur(20px);
            border-right: 1px solid rgba(255,255,255,0.05);
            display:flex; flex-direction:column; flex-shrink:0;
            box-shadow: 0 0 20px rgba(0,0,0,0.4);
            transition: width 0.25s ease, margin 0.25s ease, background 0.3s ease;
            overflow: hidden;
        }
        .sidebar.hidden { width: 0; margin: 0; border: none; overflow: hidden; padding: 0; }
        .sidebar-header {
            padding: 20px 16px 12px;
            border-bottom: 1px solid rgba(255,255,255,0.05);
            display:flex; align-items:center; gap:10px; flex-wrap:wrap;
        }
        .sidebar-header h2 {
            font-size: 17px; font-weight: 600;
            background: linear-gradient(135deg, #58a6ff, #3fb950);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
        }
        .new-note-btn {
            background: linear-gradient(135deg, #1f6feb, #388bfd);
            color: white; border: none; border-radius: 10px; padding: 8px 16px;
            font-size: 13px; font-weight: 600; cursor: pointer; margin-left: auto; white-space: nowrap;
            box-shadow: 0 4px 12px rgba(31,111,235,0.4); transition: all 0.2s;
        }
        .new-note-btn:hover { box-shadow: 0 6px 16px rgba(31,111,235,0.6); transform: translateY(-1px); }
        .search-box { padding: 8px 16px; }
        .search-box input {
            width: 100%; padding: 8px 12px; border-radius: 20px;
            border: 1px solid rgba(255,255,255,0.1); background: rgba(13,17,23,0.7);
            color: #e6edf3; font-size: 13px; outline: none;
            transition: border-color 0.2s, background 0.3s, color 0.3s;
        }
        .search-box input:focus { border-color: #58a6ff; }
        .search-box input::placeholder { color: #8b949e; }

        .tag-filter {
            padding: 4px 16px 12px;
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
            border-bottom: 1px solid rgba(255,255,255,0.05);
        }
        .tag-filter .tag-pill {
            background: rgba(255,255,255,0.06);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 20px;
            padding: 4px 12px;
            font-size: 12px;
            color: #8b949e;
            cursor: pointer;
            transition: all 0.2s;
            user-select: none;
        }
        .tag-filter .tag-pill:hover { background: rgba(255,255,255,0.12); }
        .tag-filter .tag-pill.active {
            background: #1f6feb;
            color: #fff;
            border-color: #1f6feb;
        }
        .tag-filter .tag-pill.clear-tag { border-color: rgba(248,81,73,0.3); color: #f85149; }
        .tag-filter .tag-pill.clear-tag:hover { background: rgba(248,81,73,0.15); }

        .notes-sidebar-list {
            flex:1;
            overflow-y:auto;
            padding: 8px;
        }
        .notes-sidebar-list::-webkit-scrollbar { width: 4px; }
        .notes-sidebar-list::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 2px; }

        .group-heading {
            font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: #8b949e;
            padding: 12px 12px 6px;
            border-bottom: 1px solid rgba(255,255,255,0.05);
            margin-top: 8px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .group-heading:first-of-type { margin-top: 0; }
        .group-heading .badge {
            font-size: 10px;
            background: rgba(255,255,255,0.06);
            padding: 0 8px;
            border-radius: 10px;
            color: #8b949e;
        }

        .note-item-sidebar {
            display:flex; align-items:center; padding: 8px 12px; cursor:grab;
            border-radius: 10px; margin-bottom: 2px; transition: background 0.2s; gap: 6px;
            background: transparent; user-select: none;
        }
        .note-item-sidebar:hover { background: rgba(255,255,255,0.05); }
        .note-item-sidebar.active { background: rgba(31,111,235,0.15); border: 1px solid rgba(31,111,235,0.3); }
        .note-item-sidebar.dragging { opacity: 0.4; }
        .note-item-sidebar.drag-over { border: 2px dashed #58a6ff; }
        .note-item-sidebar .pin-indicator { font-size: 12px; opacity: 0.4; margin-right: 2px; }
        .note-item-sidebar .title {
            flex:1; font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: #c9d1d9;
        }
        .note-item-sidebar .tags-mini {
            font-size: 10px;
            color: #8b949e;
            margin-right: 4px;
            max-width: 60px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            opacity: 0.6;
        }
        .note-item-sidebar .rename-btn {
            background: transparent; border: none; color: #8b949e; font-size: 13px;
            cursor: pointer; opacity: 0.4; padding: 0 4px; transition: opacity 0.2s;
        }
        .note-item-sidebar .rename-btn:hover { opacity: 1; color: #58a6ff; }
        .note-item-sidebar .del {
            background: transparent; border: none; color: #f85149; font-size: 16px;
            cursor: pointer; opacity: 0.4; padding: 0 4px; transition: opacity 0.2s;
        }
        .note-item-sidebar .del:hover { opacity: 1; }
        .note-item-sidebar .time {
            font-size: 10px;
            color: #8b949e;
            margin-right: 4px;
            white-space: nowrap;
        }
        .sidebar-footer {
            padding: 12px 16px;
            border-top: 1px solid rgba(255,255,255,0.05);
            font-size: 12px;
            color: #8b949e;
            text-align: center;
            backdrop-filter: blur(10px);
            transition: background 0.3s, color 0.3s;
        }

        /* ── Main panel ── */
        .main {
            flex:1;
            display:flex;
            flex-direction:column;
            min-width:0;
            background: rgba(10,10,15,0.7);
            backdrop-filter: blur(10px);
            transition: background 0.3s ease;
        }

        /* Top bar (same as before) */
        .top-bar {
            display: grid;
            grid-template-columns: 1fr auto 1fr;
            align-items: center;
            background: rgba(22, 27, 34, 0.7);
            backdrop-filter: blur(20px);
            border-bottom: 1px solid rgba(255,255,255,0.05);
            padding: 12px 24px;
            gap: 12px;
            flex-shrink: 0;
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
            transition: background 0.3s, border-color 0.3s;
        }
        .top-bar .left { display: flex; align-items: center; gap: 12px; justify-self: start; }
        .top-bar .left h1 {
            font-size: 19px;
            background: linear-gradient(135deg, #58a6ff, #a371f7);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            font-weight: 700;
        }
        .sidebar-toggle {
            background: transparent;
            border: none;
            color: #8b949e;
            cursor: pointer;
            padding: 6px;
            transition: color 0.2s;
            display: flex;
            align-items: center;
            justify-content: center;
            border-radius: 6px;
            outline: none;
        }
        .sidebar-toggle:hover { color: #58a6ff; background: rgba(255,255,255,0.05); }
        .top-bar .center-tabs {
            display: flex;
            gap: 4px;
            background: rgba(255,255,255,0.06);
            padding: 4px;
            border-radius: 30px;
            backdrop-filter: blur(5px);
            border: 1px solid rgba(255,255,255,0.06);
            justify-self: center;
        }
        .center-tabs .tab-btn {
            background: transparent;
            border: none;
            padding: 6px 18px;
            border-radius: 20px;
            font-size: 14px;
            font-weight: 500;
            color: #8b949e;
            cursor: pointer;
            transition: all 0.2s;
            white-space: nowrap;
            text-decoration: none;
            display: inline-block;
        }
        .center-tabs .tab-btn:hover { color: #c9d1d9; background: rgba(255,255,255,0.05); }
        .center-tabs .tab-btn.active { background: #1f6feb; color: #fff; box-shadow: 0 2px 8px rgba(31,111,235,0.3); }
        body.light-mode .center-tabs { background: rgba(0,0,0,0.04); border-color: rgba(0,0,0,0.06); }
        body.light-mode .center-tabs .tab-btn { color: #57606a; }
        body.light-mode .center-tabs .tab-btn:hover { background: rgba(0,0,0,0.04); color: #1f6feb; }
        body.light-mode .center-tabs .tab-btn.active { background: #1f6feb; color: #fff; }
        .top-bar .right {
            display: flex;
            align-items: center;
            gap: 10px;
            flex-wrap: wrap;
            justify-self: end;
            justify-content: flex-end;
        }
        .clear-btn {
            background: rgba(33,38,45,0.7);
            border: 1px solid rgba(248,81,73,0.3);
            color: #f85149;
            border-radius: 10px;
            padding: 6px 14px;
            font-size: 12px;
            cursor: pointer;
            transition: all 0.2s;
            backdrop-filter: blur(5px);
        }
        .clear-btn:hover { background: rgba(248,81,73,0.15); border-color: #f85149; }

        /* Theme toggle (exact copy from notes.py) */
        .theme-toggle-wrapper { display: inline-block; vertical-align: middle; }
        .toggle-outer {
            position: relative; width: 140px; height: 56px; border-radius: 999px; background: hsl(220 18% 82%);
            box-shadow: 2px 2px 8px rgba(0,0,0,0.12), -2px -2px 6px rgba(255,255,255,0.5),
                        inset 1px 1px 3px rgba(0,0,0,0.08), inset -1px -1px 3px rgba(255,255,255,0.4);
            cursor: pointer; user-select: none; flex-shrink: 0;
        }
        .toggle-inner { position: absolute; inset: 5px; border-radius: 999px; overflow: hidden; }
        .night-bg { position: absolute; inset: 0; background: hsl(220 35% 18%); opacity:1; transition: opacity 0.3s ease; }
        .stars-layer { position: absolute; inset: 0; opacity:1; transition: opacity 0.3s ease; pointer-events:none; }
        .star { position: absolute; background: white; border-radius:50%; }
        .sparkle { position: absolute; color: white; font-size: 7px; line-height:1; }
        .day-bg { position: absolute; inset: 0; opacity:0; transition: opacity 0.3s ease; pointer-events:none; }
        .sky-layer { position: absolute; inset: 0; background: hsl(205 70% 62%); }
        .sky-mid { position: absolute; bottom:0; left:0; right:0; height:50%; background: hsl(205 60% 72%); border-radius: 40% 40% 0 0 / 30% 30% 0 0; }
        .cloud { position: absolute; background: rgba(255,255,255,0.88); border-radius: 999px; }
        .astronaut, .biplane { position: absolute; z-index: 4; pointer-events: none; transition: opacity 0.3s ease; }
        .astronaut { left: 48px; top: 50%; transform: translateY(-55%); width: 22px; height: 26px; opacity:1; animation: float 3s ease-in-out infinite; }
        .biplane { left: 44px; top: 38%; transform: translateY(-50%); width: 30px; height: 18px; opacity:0; animation: fly 3s ease-in-out infinite; }
        @keyframes float { 0%,100% { transform: translateY(-55%); } 50% { transform: translateY(-65%); } }
        @keyframes fly { 0%,100% { transform: translateY(-50%) rotate(-1deg); } 50% { transform: translateY(-60%) rotate(1deg); } }
        .knob {
            position: absolute; top: 50%; width: 40px; height: 40px; border-radius: 50%; transform: translateY(-50%);
            z-index: 10; cursor: grab; transition: left 0.4s cubic-bezier(0.34, 1.2, 0.64, 1); left: 3px;
        }
        .knob:active { cursor: grabbing; }
        .knob-moon {
            position: absolute; inset:0; border-radius:50%; background: hsl(220 10% 82%);
            box-shadow: 2px 2px 4px rgba(255,255,255,0.9) inset, -2px -2px 4px rgba(0,0,0,0.18) inset;
            transition: opacity 0.3s ease;
        }
        .knob-moon .crater {
            position: absolute; border-radius:50%; background: hsl(220 8% 67%);
            box-shadow: 1px 1px 2px rgba(255,255,255,0.4) inset, -1px -1px 2px rgba(0,0,0,0.2) inset;
        }
        .knob-sun {
            position: absolute; inset:0; border-radius:50%; background: hsl(44 100% 58%);
            box-shadow: 2px 2px 6px rgba(255,255,180,0.9) inset, -2px -2px 4px rgba(180,100,0,0.3) inset,
                        0 0 12px hsl(44 100% 70% / 0.5);
            opacity: 0; transition: opacity 0.3s ease;
        }
        .toggle-outer.day .night-bg { opacity: 0; }
        .toggle-outer.day .stars-layer { opacity: 0; }
        .toggle-outer.day .day-bg { opacity: 1; }
        .toggle-outer.day .knob { left: 93px; }
        .toggle-outer.day .knob-moon { opacity: 0; }
        .toggle-outer.day .knob-sun { opacity: 1; }
        .toggle-outer.day .astronaut { opacity: 0; }
        .toggle-outer.day .biplane { opacity: 1; }

        /* ── Notes editor & preview ── */
        .notes-panel {
            flex:1;
            overflow-y:auto;
            padding: 24px 40px;
            display:flex;
            flex-direction:column;
            gap:16px;
        }
        .notes-panel .note-editor {
            background: rgba(13,17,23,0.7);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 12px;
            padding: 16px;
            margin-bottom: 10px;
            transition: background 0.3s, border-color 0.3s;
        }
        .notes-panel .note-editor .editor-header {
            display:flex;
            align-items:center;
            gap:10px;
            margin-bottom:8px;
            flex-wrap:wrap;
        }
        .notes-panel .note-editor .editor-header input[type="text"] {
            flex:1;
            background: transparent;
            border: none;
            color: #e6edf3;
            font-weight: 600;
            font-size: 18px;
            outline: none;
            min-width: 120px;
        }
        .notes-panel .note-editor .editor-header .toolbar {
            display:flex;
            gap:4px;
            flex-wrap:wrap;
        }
        .notes-panel .note-editor .editor-header .toolbar button {
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 6px;
            color: #8b949e;
            padding: 4px 8px;
            font-size: 13px;
            cursor: pointer;
            transition: 0.2s;
        }
        .notes-panel .note-editor .editor-header .toolbar button:hover {
            background: rgba(255,255,255,0.12);
            color: #e6edf3;
        }
        .notes-panel .note-editor .editor-header .toolbar button.active {
            background: #1f6feb;
            color: #fff;
            border-color: #1f6feb;
        }
        .notes-panel .note-editor .editor-body {
            display:flex;
            gap:12px;
            min-height:200px;
        }
        .notes-panel .note-editor .editor-body textarea {
            flex:1;
            background: transparent;
            border: none;
            color: #e6edf3;
            font-size: 14px;
            font-family: inherit;
            outline: none;
            resize: vertical;
            min-height: 180px;
            padding: 4px;
            line-height: 1.6;
        }
        .notes-panel .note-editor .editor-body .preview {
            flex:1;
            background: rgba(0,0,0,0.2);
            border-radius: 8px;
            padding: 8px 12px;
            overflow-y:auto;
            color: #e1e4e8;
            font-size: 14px;
            line-height: 1.6;
            display:none; /* hidden by default, toggled by preview button */
        }
        .notes-panel .note-editor .editor-body .preview.visible {
            display:block;
        }
        .notes-panel .note-editor .editor-footer {
            display:flex;
            align-items:center;
            gap:12px;
            margin-top:12px;
            flex-wrap:wrap;
        }
        .notes-panel .note-editor .editor-footer .tags-input {
            flex:1;
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 20px;
            padding: 4px 12px;
            color: #e6edf3;
            font-size: 13px;
            outline: none;
            min-width: 80px;
        }
        .notes-panel .note-editor .editor-footer .tags-input:focus { border-color: #58a6ff; }
        .notes-panel .note-editor .editor-footer .color-picker {
            display:flex;
            gap:6px;
        }
        .notes-panel .note-editor .editor-footer .color-picker .color-option {
            width:22px;
            height:22px;
            border-radius:50%;
            cursor:pointer;
            border:2px solid transparent;
            transition: 0.2s;
        }
        .notes-panel .note-editor .editor-footer .color-picker .color-option:hover { transform:scale(1.15); }
        .notes-panel .note-editor .editor-footer .color-picker .color-option.active {
            border-color: #58a6ff;
            box-shadow: 0 0 8px rgba(88,166,255,0.4);
        }
        .color-default { background: #8b949e; }
        .color-yellow { background: #fdec8f; }
        .color-blue   { background: #9fd3ff; }
        .color-green  { background: #a9e6a1; }
        .color-pink   { background: #ffb3cd; }
        .color-orange { background: #ffc27a; }

        .notes-panel .note-editor .editor-footer .pin-toggle {
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 20px;
            padding: 4px 14px;
            color: #8b949e;
            font-size: 13px;
            cursor: pointer;
            transition: 0.2s;
        }
        .notes-panel .note-editor .editor-footer .pin-toggle.active {
            background: rgba(31,111,235,0.2);
            border-color: #1f6feb;
            color: #58a6ff;
        }
        .notes-panel .note-editor .editor-footer .pin-toggle:hover { background: rgba(255,255,255,0.1); }

        .notes-panel .note-editor .note-actions {
            display:flex;
            gap:8px;
            justify-content:flex-end;
            margin-top:12px;
        }
        .notes-panel .note-editor .note-actions button {
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.1);
            color: #8b949e;
            border-radius: 8px;
            padding: 6px 18px;
            cursor: pointer;
            font-size: 13px;
            transition: 0.2s;
        }
        .notes-panel .note-editor .note-actions .save-note { background: #1f6feb; color: white; border-color: #1f6feb; }
        .notes-panel .note-editor .note-actions .save-note:hover { background: #388bfd; }
        .notes-panel .note-editor .note-actions button:hover { background: rgba(255,255,255,0.1); }

        .notes-panel .note-item {
            background: rgba(28, 35, 51, 0.6);
            backdrop-filter: blur(8px);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 12px;
            padding: 16px 20px;
            transition: 0.2s;
            position:relative;
        }
        .notes-panel .note-item:hover { background: rgba(28, 35, 51, 0.8); }
        .notes-panel .note-item .note-title {
            font-weight: 600;
            font-size: 16px;
            margin-bottom: 4px;
            color: #e6edf3;
        }
        .notes-panel .note-item .note-meta {
            font-size: 11px;
            color: #8b949e;
            margin-bottom: 6px;
            display:flex;
            gap:8px;
            flex-wrap:wrap;
        }
        .notes-panel .note-item .note-meta .tag {
            background: rgba(255,255,255,0.06);
            padding: 0 8px;
            border-radius: 12px;
            color: #58a6ff;
        }
        .notes-panel .note-item .note-content {
            font-size: 14px;
            color: #8b949e;
            white-space: pre-wrap;
            word-wrap: break-word;
            max-height: 120px;
            overflow:hidden;
        }
        .notes-panel .note-item .note-actions {
            margin-top: 10px;
            display: flex;
            gap: 8px;
        }
        .notes-panel .note-item .note-actions button {
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.1);
            color: #8b949e;
            border-radius: 8px;
            padding: 4px 12px;
            cursor: pointer;
            font-size: 12px;
            transition: 0.2s;
        }
        .notes-panel .note-item .note-actions button:hover { background: rgba(255,255,255,0.1); color: #58a6ff; }
        .notes-panel .note-item .note-actions .delete-note { color: #f85149; }
        .notes-panel .note-item .note-actions .delete-note:hover { background: rgba(248,81,73,0.15); border-color: #f85149; }

        /* Light mode overrides (keep same as before but adapt) */
        body.light-mode {
            background: #f6f8fa;
            color: #24292f;
        }
        body.light-mode .sidebar { background: rgba(255, 255, 255, 0.92); border-right-color: rgba(0,0,0,0.08); }
        body.light-mode .sidebar .sidebar-header { border-bottom-color: rgba(0,0,0,0.06); }
        body.light-mode .sidebar .group-heading { color: #57606a; border-bottom-color: rgba(0,0,0,0.06); }
        body.light-mode .note-item-sidebar:hover { background: rgba(0,0,0,0.04); }
        body.light-mode .note-item-sidebar.active { background: rgba(31,111,235,0.12); border-color: rgba(31,111,235,0.3); }
        body.light-mode .note-item-sidebar .title { color: #24292f; }
        body.light-mode .note-item-sidebar .time { color: #57606a; }
        body.light-mode .note-item-sidebar .rename-btn { color: #57606a; }
        body.light-mode .sidebar-footer { color: #57606a; border-top-color: rgba(0,0,0,0.06); }
        body.light-mode .main { background: rgba(255,255,255,0.85); }
        body.light-mode .top-bar { background: rgba(255, 255, 255, 0.9); border-bottom-color: rgba(0,0,0,0.08); box-shadow: 0 1px 4px rgba(0,0,0,0.05); }
        body.light-mode .top-bar .left h1 {
            background: linear-gradient(135deg, #1f6feb, #a371f7);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
        }
        body.light-mode .clear-btn { background: rgba(0,0,0,0.05); border-color: rgba(248,81,73,0.3); color: #f85149; }
        body.light-mode .clear-btn:hover { background: rgba(248,81,73,0.08); }
        body.light-mode .search-box input { background: rgba(255,255,255,0.8); color: #24292f; border-color: rgba(0,0,0,0.12); }
        body.light-mode .search-box input::placeholder { color: #8b949e; }
        body.light-mode .notes-panel .note-item { background: rgba(255,255,255,0.8); border-color: rgba(0,0,0,0.06); }
        body.light-mode .notes-panel .note-item:hover { background: rgba(255,255,255,0.95); }
        body.light-mode .notes-panel .note-title { color: #24292f; }
        body.light-mode .notes-panel .note-content { color: #57606a; }
        body.light-mode .notes-panel .note-editor { background: rgba(255,255,255,0.8); border-color: rgba(0,0,0,0.08); }
        body.light-mode .notes-panel .note-editor input,
        body.light-mode .notes-panel .note-editor textarea,
        body.light-mode .notes-panel .note-editor .editor-body .preview { color: #24292f; }
        body.light-mode .notes-panel .note-editor .editor-body .preview { background: rgba(0,0,0,0.03); }
        body.light-mode .notes-panel .note-editor .editor-header .toolbar button { color: #57606a; }
        body.light-mode .notes-panel .note-editor .editor-header .toolbar button:hover { background: rgba(0,0,0,0.06); }
        body.light-mode .notes-panel .note-editor .editor-footer .tags-input { background: rgba(0,0,0,0.04); color: #24292f; }
        body.light-mode .notes-panel .note-editor .editor-footer .pin-toggle { color: #57606a; }
        body.light-mode .notes-panel .note-editor .editor-footer .pin-toggle.active { background: rgba(31,111,235,0.1); color: #1f6feb; }
        body.light-mode .tag-filter .tag-pill { background: rgba(0,0,0,0.04); border-color: rgba(0,0,0,0.08); color: #57606a; }
        body.light-mode .tag-filter .tag-pill.active { background: #1f6feb; color: #fff; border-color: #1f6feb; }
        body.light-mode .tag-filter .tag-pill.clear-tag { border-color: rgba(248,81,73,0.3); color: #f85149; }
        body.light-mode .tag-filter .tag-pill.clear-tag:hover { background: rgba(248,81,73,0.08); }
    </style>
</head>
<body>
<div class="app">
    <!-- SIDEBAR -->
    <div class="sidebar" id="sidebar">
        <div class="sidebar-header">
            <h2>📖 Notes</h2>
            <button class="new-note-btn" onclick="createNewNote()">+ New</button>
        </div>
        <div class="search-box">
            <input type="text" id="searchInput" placeholder="🔍 Search notes..." oninput="searchNotes()">
        </div>
        <div class="tag-filter" id="tagFilterContainer"></div>
        <div class="notes-sidebar-list" id="notesSidebarList"></div>
        <div class="sidebar-footer">Drag to reorder · ✏️ to rename · 🏷️ tags</div>
    </div>

    <!-- MAIN PANEL -->
    <div class="main">
        <!-- Top bar -->
        <div class="top-bar">
            <div class="left">
                <button class="sidebar-toggle" onclick="toggleSidebar()" title="Toggle sidebar">
                    <svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                        <rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect>
                        <line x1="9" y1="3" x2="9" y2="21"></line>
                    </svg>
                </button>
                <h1>📚 Trio-llama Custom Notes</h1>
            </div>

            <div class="center-tabs">
                <a href="/" class="tab-btn" style="text-decoration:none;">💬 Chat</a>
                <button class="tab-btn active">📝 Notes</button>
                <a href="/corkboard" class="tab-btn" style="text-decoration:none;">📌 Cork Board</a>
            </div>

            <div class="right">
                <div class="theme-toggle-wrapper">
                    <div class="toggle-outer" id="themeToggleOuter" onclick="handleThemeClick(event)">
                        <div class="toggle-inner">
                            <div class="night-bg"></div>
                            <div class="stars-layer" id="themeStars"></div>
                            <div class="day-bg">
                                <div class="sky-layer"></div>
                                <div class="sky-mid"></div>
                                <div class="cloud" style="width:36px;height:14px;bottom:3px;right:0px;"></div>
                                <div class="cloud" style="width:26px;height:10px;bottom:14px;right:22px;opacity:.85;"></div>
                                <div class="cloud" style="width:20px;height:8px;bottom:22px;left:4px;opacity:.7;"></div>
                            </div>
                            <div class="astronaut"><!-- SVG same as before --></div>
                            <div class="biplane"><!-- SVG same as before --></div>
                            <div class="knob" id="themeKnob">
                                <div class="knob-moon">
                                    <div class="crater" style="width:10px;height:10px;top:8px;left:7px;"></div>
                                    <div class="crater" style="width:8px;height:8px;top:22px;left:11px;"></div>
                                    <div class="crater" style="width:5px;height:5px;top:18px;left:25px;"></div>
                                </div>
                                <div class="knob-sun"></div>
                            </div>
                        </div>
                    </div>
                </div>
                <button class="clear-btn" onclick="clearAllNotes()">🗑 Clear All</button>
            </div>
        </div>

        <!-- Notes panel -->
        <div class="notes-panel" id="notesPanel">
            <div class="note-editor" id="noteEditor" style="display:none;">
                <div class="editor-header">
                    <input type="text" id="noteTitleInput" placeholder="Note title...">
                    <div class="toolbar" id="toolbar">
                        <button data-cmd="bold" title="Bold">B</button>
                        <button data-cmd="italic" title="Italic">I</button>
                        <button data-cmd="heading" title="Heading">H</button>
                        <button data-cmd="list" title="Bullet list">•</button>
                        <button data-cmd="code" title="Code block">{ }</button>
                        <button data-cmd="preview" title="Toggle preview" id="previewToggle">👁️</button>
                    </div>
                </div>
                <div class="editor-body">
                    <textarea id="noteContentInput" placeholder="Write your note in Markdown..."></textarea>
                    <div class="preview" id="notePreview"></div>
                </div>
                <div class="editor-footer">
                    <input type="text" class="tags-input" id="noteTagsInput" placeholder="Tags (comma separated)">
                    <div class="color-picker" id="colorPicker">
                        <span class="color-option color-default active" data-color="default" title="Default"></span>
                        <span class="color-option color-yellow" data-color="yellow" title="Yellow"></span>
                        <span class="color-option color-blue" data-color="blue" title="Blue"></span>
                        <span class="color-option color-green" data-color="green" title="Green"></span>
                        <span class="color-option color-pink" data-color="pink" title="Pink"></span>
                        <span class="color-option color-orange" data-color="orange" title="Orange"></span>
                    </div>
                    <button class="pin-toggle" id="pinToggle">📌 Pin</button>
                </div>
                <div class="note-actions">
                    <button class="save-note" id="saveNoteBtn">💾 Save Note</button>
                    <button id="cancelNoteBtn" style="display:none;">Cancel</button>
                </div>
            </div>
            <div id="notesList"></div>
        </div>
    </div>
</div>

<script>
    // ─── Theme (same as before) ───────────────────────────
    var themeOuter = document.getElementById('themeToggleOuter');
    var themeKnob = document.getElementById('themeKnob');
    var isLight = localStorage.getItem('theme') === 'light';
    function applyTheme(light) {
        document.body.classList.toggle('light-mode', light);
        localStorage.setItem('theme', light ? 'light' : 'dark');
        themeOuter.classList.toggle('day', light);
    }
    applyTheme(isLight);
    var draggedTheme = false;
    var isDraggingTheme = false;
    var startXTheme = 0, startLeftTheme = 0;
    function handleThemeClick(e) {
        if (draggedTheme) return;
        var newLight = !document.body.classList.contains('light-mode');
        applyTheme(newLight);
    }
    const MIN_LEFT_THEME = 3;
    const MAX_LEFT_THEME = 93;
    themeKnob.addEventListener('mousedown', dragStartTheme);
    themeKnob.addEventListener('touchstart', dragStartTheme, { passive: true });
    function dragStartTheme(e) {
        isDraggingTheme = true;
        draggedTheme = false;
        themeKnob.style.transition = 'none';
        startXTheme = e.touches ? e.touches[0].clientX : e.clientX;
        startLeftTheme = document.body.classList.contains('light-mode') ? MAX_LEFT_THEME : MIN_LEFT_THEME;
        e.stopPropagation();
        window.addEventListener('mousemove', dragMoveTheme);
        window.addEventListener('mouseup', dragEndTheme);
        window.addEventListener('touchmove', dragMoveTheme, { passive: true });
        window.addEventListener('touchend', dragEndTheme);
    }
    function dragMoveTheme(e) {
        if (!isDraggingTheme) return;
        const clientX = e.touches ? e.touches[0].clientX : e.clientX;
        const dx = clientX - startXTheme;
        if (Math.abs(dx) > 4) draggedTheme = true;
        let newLeft = Math.min(MAX_LEFT_THEME, Math.max(MIN_LEFT_THEME, startLeftTheme + dx));
        themeKnob.style.left = newLeft + 'px';
        const progress = (newLeft - MIN_LEFT_THEME) / (MAX_LEFT_THEME - MIN_LEFT_THEME);
        document.querySelector('.night-bg').style.opacity = 1 - progress;
        document.querySelector('.stars-layer').style.opacity = 1 - progress;
        document.querySelector('.day-bg').style.opacity = progress;
        document.querySelector('.knob-moon').style.opacity = 1 - progress;
        document.querySelector('.knob-sun').style.opacity = progress;
        document.querySelector('.astronaut').style.opacity = progress < 0.5 ? 1 : 0;
        document.querySelector('.biplane').style.opacity = progress >= 0.5 ? 1 : 0;
    }
    function dragEndTheme(e) {
        if (!isDraggingTheme) return;
        isDraggingTheme = false;
        themeKnob.style.transition = '';
        themeKnob.style.left = '';
        document.querySelector('.night-bg').style.opacity = '';
        document.querySelector('.stars-layer').style.opacity = '';
        document.querySelector('.day-bg').style.opacity = '';
        document.querySelector('.knob-moon').style.opacity = '';
        document.querySelector('.knob-sun').style.opacity = '';
        document.querySelector('.astronaut').style.opacity = '';
        document.querySelector('.biplane').style.opacity = '';
        const rect = themeKnob.getBoundingClientRect();
        const outerRect = themeOuter.getBoundingClientRect();
        const currentLeft = rect.left - outerRect.left - 5;
        const midpoint = (MIN_LEFT_THEME + MAX_LEFT_THEME) / 2;
        const newLight = currentLeft > midpoint;
        applyTheme(newLight);
        window.removeEventListener('mousemove', dragMoveTheme);
        window.removeEventListener('mouseup', dragEndTheme);
        window.removeEventListener('touchmove', dragMoveTheme);
        window.removeEventListener('touchend', dragEndTheme);
    }
    function makeThemeStars() {
        const layer = document.getElementById('themeStars');
        const pts = [
            {x:26,y:10,s:1},{x:34,y:14,s:0.8},{x:40,y:7,s:1.2},{x:46,y:17,s:0.8},
            {x:62,y:18,s:0.8},{x:76,y:16,s:0.8},{x:86,y:6,s:1},
            {x:98,y:13,s:1},{x:112,y:10,s:1},{x:124,y:18,s:0.8},
        ];
        pts.forEach(d => {
            const s = document.createElement('div');
            s.className = 'star';
            s.style.cssText = `width:${d.s}px;height:${d.s}px;left:${d.x}px;top:${d.y}px;`;
            layer.appendChild(s);
        });
        [{x:38,y:12},{x:76,y:18},{x:114,y:20}].forEach(p => {
            const sp = document.createElement('div');
            sp.className = 'sparkle';
            sp.style.cssText = `left:${p.x}px;top:${p.y}px;`;
            sp.innerHTML = '✦';
            layer.appendChild(sp);
        });
    }
    makeThemeStars();

    // ─── Sidebar toggle ────────────────────────────────
    var sidebar = document.getElementById('sidebar');
    var sidebarVisible = localStorage.getItem('notesSidebarVisible') !== 'false';
    function toggleSidebar() {
        sidebarVisible = !sidebarVisible;
        localStorage.setItem('notesSidebarVisible', sidebarVisible);
        sidebar.classList.toggle('hidden', !sidebarVisible);
    }
    if (!sidebarVisible) sidebar.classList.add('hidden');

    // ─── State ──────────────────────────────────────────
    var notesData = {};
    var editingNoteId = null;
    var currentColor = 'default';
    var currentPinned = false;
    var searchTimeout = null;
    var activeTagFilter = ''; // empty = all

    // ─── Markdown preview ──────────────────────────────
    function updatePreview() {
        var content = document.getElementById('noteContentInput').value;
        var preview = document.getElementById('notePreview');
        preview.innerHTML = marked.parse(content || '');
    }
    document.getElementById('noteContentInput').addEventListener('input', updatePreview);

    // ─── Toolbar commands ──────────────────────────────
    document.querySelectorAll('#toolbar button[data-cmd]').forEach(btn => {
        btn.addEventListener('click', function() {
            var cmd = this.dataset.cmd;
            var textarea = document.getElementById('noteContentInput');
            var start = textarea.selectionStart;
            var end = textarea.selectionEnd;
            var selected = textarea.value.substring(start, end);
            var replacement = '';
            if (cmd === 'bold') replacement = '**' + selected + '**';
            else if (cmd === 'italic') replacement = '*' + selected + '*';
            else if (cmd === 'heading') replacement = '# ' + selected;
            else if (cmd === 'list') replacement = '- ' + selected;
            else if (cmd === 'code') replacement = '```\n' + selected + '\n```';
            else if (cmd === 'preview') {
                document.getElementById('notePreview').classList.toggle('visible');
                this.classList.toggle('active');
                return;
            }
            if (replacement) {
                textarea.value = textarea.value.substring(0, start) + replacement + textarea.value.substring(end);
                textarea.focus();
                textarea.selectionStart = start + replacement.length;
                textarea.selectionEnd = start + replacement.length;
                updatePreview();
            }
        });
    });

    // ─── Color picker ──────────────────────────────────
    document.querySelectorAll('#colorPicker .color-option').forEach(el => {
        el.addEventListener('click', function() {
            document.querySelectorAll('#colorPicker .color-option').forEach(c => c.classList.remove('active'));
            this.classList.add('active');
            currentColor = this.dataset.color;
        });
    });

    // ─── Pin toggle ────────────────────────────────────
    document.getElementById('pinToggle').addEventListener('click', function() {
        currentPinned = !currentPinned;
        this.classList.toggle('active', currentPinned);
        this.textContent = currentPinned ? '📌 Pinned' : '📌 Pin';
    });

    // ─── Load & render notes ───────────────────────────
    function loadNotes() {
        var query = document.getElementById('searchInput').value.trim();
        var tag = activeTagFilter;
        var url = '/notes/api/search?q=' + encodeURIComponent(query) + '&tag=' + encodeURIComponent(tag);
        fetch(url)
            .then(r => r.json())
            .then(notes => {
                notesData = notes;
                renderSidebar(notes);
                renderMain(notes);
                renderTagFilter(notes);
            })
            .catch(e => console.error('Failed to load notes:', e));
    }

    function getDateGroup(dateStr) {
        var now = new Date();
        var date = new Date(dateStr);
        var today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        var yesterday = new Date(today);
        yesterday.setDate(yesterday.getDate() - 1);
        var weekStart = new Date(today);
        weekStart.setDate(weekStart.getDate() - today.getDay());
        var lastWeekStart = new Date(weekStart);
        lastWeekStart.setDate(lastWeekStart.getDate() - 7);
        var d = new Date(date.getFullYear(), date.getMonth(), date.getDate());
        if (d.getTime() === today.getTime()) return 'Today';
        if (d.getTime() === yesterday.getTime()) return 'Yesterday';
        if (d >= weekStart) return 'This Week';
        if (d >= lastWeekStart) return 'Last Week';
        return 'Older';
    }

    function renderSidebar(notes) {
        const listEl = document.getElementById('notesSidebarList');
        listEl.innerHTML = '';
        const ids = Object.keys(notes);
        if (!ids.length) {
            listEl.innerHTML = '<div class="no-results">🔍 No notes found</div>';
            return;
        }
        // Separate pinned and unpinned
        var pinned = [];
        var unpinned = [];
        ids.forEach(id => {
            if (notes[id].pinned) pinned.push(id);
            else unpinned.push(id);
        });
        // Sort pinned by order, then unpinned by order
        var sortFn = (a, b) => {
            var orderA = notes[a].order || 0;
            var orderB = notes[b].order || 0;
            if (orderA !== orderB) return orderA - orderB;
            return new Date(notes[b].created) - new Date(notes[a].created);
        };
        pinned.sort(sortFn);
        unpinned.sort(sortFn);
        var allIds = pinned.concat(unpinned);

        var groups = {};
        allIds.forEach(id => {
            var note = notes[id];
            var group = getDateGroup(note.created);
            if (!groups[group]) groups[group] = [];
            groups[group].push({ id, note });
        });

        var groupOrder = ['Today', 'Yesterday', 'This Week', 'Last Week', 'Older'];
        groupOrder.forEach(groupName => {
            var items = groups[groupName];
            if (!items) return;
            var heading = document.createElement('div');
            heading.className = 'group-heading';
            heading.textContent = groupName;
            // Show pinned count if any in this group
            var pinnedCount = items.filter(({note}) => note.pinned).length;
            if (pinnedCount) {
                var badge = document.createElement('span');
                badge.className = 'badge';
                badge.textContent = '📌 ' + pinnedCount;
                heading.appendChild(badge);
            }
            listEl.appendChild(heading);

            items.forEach(({id, note}) => {
                var div = document.createElement('div');
                div.className = 'note-item-sidebar';
                if (id === editingNoteId) div.classList.add('active');
                div.dataset.id = id;
                div.draggable = true;

                if (note.pinned) {
                    var pin = document.createElement('span');
                    pin.className = 'pin-indicator';
                    pin.textContent = '📌';
                    div.appendChild(pin);
                }

                var titleSpan = document.createElement('span');
                titleSpan.className = 'title';
                titleSpan.textContent = note.title || 'Untitled';
                div.appendChild(titleSpan);

                // tags mini
                if (note.tags && note.tags.length) {
                    var tagSpan = document.createElement('span');
                    tagSpan.className = 'tags-mini';
                    tagSpan.textContent = note.tags.join(', ');
                    div.appendChild(tagSpan);
                }

                var renameBtn = document.createElement('button');
                renameBtn.className = 'rename-btn';
                renameBtn.textContent = '✏️';
                renameBtn.title = 'Rename this note';
                renameBtn.onclick = function(e) { e.stopPropagation(); editNoteFromSidebar(id); };
                div.appendChild(renameBtn);

                var timeSpan = document.createElement('span');
                timeSpan.className = 'time';
                var d = new Date(note.last_modified || note.created);
                timeSpan.textContent = d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
                div.appendChild(timeSpan);

                var delBtn = document.createElement('button');
                delBtn.className = 'del';
                delBtn.textContent = '×';
                delBtn.title = 'Delete this note';
                delBtn.onclick = function(e) { e.stopPropagation(); deleteNote(id); };
                div.appendChild(delBtn);

                // Drag & drop
                div.addEventListener('dragstart', handleDragStart);
                div.addEventListener('dragend', handleDragEnd);
                div.addEventListener('dragover', handleDragOver);
                div.addEventListener('drop', handleDrop);
                div.addEventListener('click', function() { selectNote(id); });

                listEl.appendChild(div);
            });
        });
    }

    function renderMain(notes) {
        const listEl = document.getElementById('notesList');
        listEl.innerHTML = '';
        const ids = Object.keys(notes);
        if (!ids.length) {
            listEl.innerHTML = '<div class="note-item" style="text-align:center;color:#8b949e;">📝 No notes yet. Create one above!</div>';
            return;
        }
        var sortedIds = ids.sort((a, b) => {
            var orderA = notes[a].order || 0;
            var orderB = notes[b].order || 0;
            if (orderA !== orderB) return orderA - orderB;
            return new Date(notes[b].created) - new Date(notes[a].created);
        });
        sortedIds.forEach(id => {
            const note = notes[id];
            const div = document.createElement('div');
            div.className = 'note-item';
            // colour border
            if (note.color && note.color !== 'default') {
                div.style.borderLeft = '4px solid ' + getColorHex(note.color);
            }
            div.innerHTML = `
                <div class="note-title">${escapeHtml(note.title)} ${note.pinned ? '📌' : ''}</div>
                <div class="note-meta">
                    <span>${new Date(note.last_modified || note.created).toLocaleString()}</span>
                    ${note.tags && note.tags.length ? note.tags.map(t => `<span class="tag">#${escapeHtml(t)}</span>`).join('') : ''}
                </div>
                <div class="note-content">${escapeHtml(note.content)}</div>
                <div class="note-actions">
                    <button class="edit-note" data-id="${id}">✏️ Edit</button>
                    <button class="delete-note" data-id="${id}">🗑️ Delete</button>
                </div>
            `;
            listEl.appendChild(div);
        });
        document.querySelectorAll('.edit-note').forEach(btn => {
            btn.addEventListener('click', function() { editNoteFromMain(this.dataset.id); });
        });
        document.querySelectorAll('.delete-note').forEach(btn => {
            btn.addEventListener('click', function() {
                const id = this.dataset.id;
                if (confirm('Delete this note?')) deleteNote(id);
            });
        });
    }

    function getColorHex(color) {
        const map = {
            'default': '#8b949e',
            'yellow': '#fdec8f',
            'blue': '#9fd3ff',
            'green': '#a9e6a1',
            'pink': '#ffb3cd',
            'orange': '#ffc27a'
        };
        return map[color] || '#8b949e';
    }

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // ─── Tag filter ──────────────────────────────────────
    function renderTagFilter(notes) {
        const container = document.getElementById('tagFilterContainer');
        // Collect all tags
        var tagSet = new Set();
        Object.values(notes).forEach(n => {
            if (n.tags) n.tags.forEach(t => tagSet.add(t));
        });
        var tags = Array.from(tagSet).sort();
        var html = '';
        if (tags.length) {
            html += `<span class="tag-pill ${activeTagFilter === '' ? 'active' : ''}" data-tag="" onclick="setTagFilter('')">All</span>`;
            tags.forEach(t => {
                const active = t === activeTagFilter ? 'active' : '';
                html += `<span class="tag-pill ${active}" data-tag="${escapeHtml(t)}" onclick="setTagFilter('${escapeHtml(t)}')">#${escapeHtml(t)}</span>`;
            });
            if (activeTagFilter) {
                html += `<span class="tag-pill clear-tag" onclick="setTagFilter('')">✕ Clear</span>`;
            }
        } else {
            html = '<span style="font-size:12px;color:#8b949e;padding:2px 6px;">No tags yet</span>';
        }
        container.innerHTML = html;
    }

    function setTagFilter(tag) {
        activeTagFilter = tag;
        loadNotes();
    }

    // ─── Search ──────────────────────────────────────────
    function searchNotes() {
        if (searchTimeout) clearTimeout(searchTimeout);
        searchTimeout = setTimeout(loadNotes, 300);
    }

    // ─── CRUD ────────────────────────────────────────────
    function createNewNote() {
        document.getElementById('noteEditor').style.display = 'block';
        document.getElementById('noteTitleInput').value = '';
        document.getElementById('noteContentInput').value = '';
        document.getElementById('noteTagsInput').value = '';
        document.getElementById('notePreview').innerHTML = '';
        document.getElementById('notePreview').classList.remove('visible');
        document.querySelector('#toolbar [data-cmd="preview"]').classList.remove('active');
        editingNoteId = null;
        currentColor = 'default';
        document.querySelectorAll('#colorPicker .color-option').forEach(c => c.classList.remove('active'));
        document.querySelector('#colorPicker .color-default').classList.add('active');
        currentPinned = false;
        document.getElementById('pinToggle').classList.remove('active');
        document.getElementById('pinToggle').textContent = '📌 Pin';
        document.getElementById('saveNoteBtn').textContent = '💾 Save Note';
        document.getElementById('cancelNoteBtn').style.display = 'inline-block';
        document.getElementById('noteEditor').scrollIntoView({ behavior: 'smooth' });
        document.getElementById('noteTitleInput').focus();
    }

    function editNoteFromSidebar(id) {
        const note = notesData[id];
        if (!note) return;
        document.getElementById('noteEditor').style.display = 'block';
        document.getElementById('noteTitleInput').value = note.title || '';
        document.getElementById('noteContentInput').value = note.content || '';
        document.getElementById('noteTagsInput').value = (note.tags || []).join(', ');
        editingNoteId = id;
        currentColor = note.color || 'default';
        document.querySelectorAll('#colorPicker .color-option').forEach(c => {
            c.classList.toggle('active', c.dataset.color === currentColor);
        });
        currentPinned = note.pinned || false;
        document.getElementById('pinToggle').classList.toggle('active', currentPinned);
        document.getElementById('pinToggle').textContent = currentPinned ? '📌 Pinned' : '📌 Pin';
        document.getElementById('saveNoteBtn').textContent = '✏️ Update Note';
        document.getElementById('cancelNoteBtn').style.display = 'inline-block';
        updatePreview();
        document.getElementById('noteEditor').scrollIntoView({ behavior: 'smooth' });
        document.getElementById('noteTitleInput').focus();
    }

    function editNoteFromMain(id) {
        editNoteFromSidebar(id);
    }

    function selectNote(id) {
        editNoteFromSidebar(id);
        // highlight in sidebar
        document.querySelectorAll('.note-item-sidebar').forEach(el => el.classList.toggle('active', el.dataset.id === id));
    }

    function deleteNote(id) {
        if (!confirm('Delete this note?')) return;
        fetch('/notes/api/' + id, { method: 'DELETE' })
            .then(r => r.json())
            .then(data => {
                if (data.ok) {
                    if (editingNoteId === id) {
                        document.getElementById('noteEditor').style.display = 'none';
                        editingNoteId = null;
                    }
                    loadNotes();
                }
            });
    }

    function clearAllNotes() {
        if (!confirm('Delete ALL notes?')) return;
        fetch('/notes/api/clear_all', { method: 'POST' })
            .then(r => r.json())
            .then(data => {
                if (data.ok) {
                    document.getElementById('noteEditor').style.display = 'none';
                    editingNoteId = null;
                    loadNotes();
                }
            });
    }

    // ─── Save note ───────────────────────────────────────
    document.getElementById('saveNoteBtn').addEventListener('click', function() {
        const title = document.getElementById('noteTitleInput').value.trim() || 'Untitled';
        const content = document.getElementById('noteContentInput').value.trim();
        if (!content && !title) { alert('Please add some content or a title.'); return; }
        const tags = document.getElementById('noteTagsInput').value.split(',').map(s => s.trim()).filter(Boolean);
        const payload = {
            title,
            content,
            tags,
            color: currentColor,
            pinned: currentPinned
        };
        const method = editingNoteId ? 'PUT' : 'POST';
        const url = editingNoteId ? '/notes/api/' + editingNoteId : '/notes/api';
        fetch(url, {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        })
        .then(r => r.json())
        .then(data => {
            if (data.ok || data.id) {
                document.getElementById('noteEditor').style.display = 'none';
                editingNoteId = null;
                loadNotes();
            }
        });
    });

    document.getElementById('cancelNoteBtn').addEventListener('click', function() {
        document.getElementById('noteEditor').style.display = 'none';
        editingNoteId = null;
        loadNotes();
    });

    // ─── Drag & drop reorder ────────────────────────────
    var dragSrcId = null;
    function handleDragStart(e) {
        dragSrcId = this.dataset.id;
        this.classList.add('dragging');
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', this.dataset.id);
    }
    function handleDragEnd(e) {
        this.classList.remove('dragging');
        document.querySelectorAll('.note-item-sidebar').forEach(el => el.classList.remove('drag-over'));
    }
    function handleDragOver(e) {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        document.querySelectorAll('.note-item-sidebar').forEach(el => el.classList.remove('drag-over'));
        this.classList.add('drag-over');
    }
    function handleDrop(e) {
        e.preventDefault();
        this.classList.remove('drag-over');
        var targetId = this.dataset.id;
        if (dragSrcId === targetId) return;
        var ids = Object.keys(notesData);
        var srcIndex = ids.indexOf(dragSrcId);
        var targetIndex = ids.indexOf(targetId);
        if (srcIndex === -1 || targetIndex === -1) return;
        var newOrder = {};
        ids.forEach((id, idx) => { newOrder[id] = idx; });
        newOrder[dragSrcId] = targetIndex;
        newOrder[targetId] = srcIndex;
        fetch('/notes/api/reorder', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({order: newOrder})
        })
        .then(r => r.json())
        .then(data => { if (data.ok) loadNotes(); else console.error('Reorder failed'); })
        .catch(err => console.error('Error reordering:', err));
    }

    // ─── Init ────────────────────────────────────────────
    window.addEventListener('load', function() {
        loadNotes();
        document.getElementById('noteTitleInput').focus();
    });
</script>
</body>
</html>
"""