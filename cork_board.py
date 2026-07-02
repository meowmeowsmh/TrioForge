# cork_board.py – advanced with Markdown, tags, search, resizable pins, modal editor, and Red Thread links
import os
import json
import uuid
import random
from datetime import datetime
from flask import Blueprint, request, jsonify, render_template_string

CORKBOARD_FILE = "json_configuration/corkboard.json"
os.makedirs(os.path.dirname(CORKBOARD_FILE), exist_ok=True)
if not os.path.exists(CORKBOARD_FILE):
    with open(CORKBOARD_FILE, "w", encoding="utf-8") as f:
        json.dump({"pins": {}, "links": []}, f, indent=2)


def load_board():
    with open(CORKBOARD_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("pins", {})
    data.setdefault("links", [])
    return data


def save_board(data):
    with open(CORKBOARD_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


corkboard_bp = Blueprint('corkboard', __name__, url_prefix='/corkboard')

# ---------- Serve the corkboard HTML page ----------
@corkboard_bp.route('')
def corkboard_page():
    return render_template_string(CORKBOARD_HTML)


# ---------- API: full board ----------
@corkboard_bp.route('/api', methods=['GET'])
def get_board():
    return jsonify(load_board())


# ---------- API: search pins ----------
@corkboard_bp.route('/api/search', methods=['GET'])
def search_pins():
    query = request.args.get('q', '').strip().lower()
    tag = request.args.get('tag', '').strip().lower()
    board = load_board()
    results = {}
    for pid, pin in board['pins'].items():
        if tag:
            pin_tags = [t.lower() for t in pin.get('tags', [])]
            if tag not in pin_tags:
                continue
        if query:
            title_match = query in pin.get('title', '').lower()
            content_match = query in pin.get('content', '').lower()
            tag_match = any(query in t.lower() for t in pin.get('tags', []))
            if not (title_match or content_match or tag_match):
                continue
        results[pid] = pin
    # Keep links that reference pins that still exist
    board['links'] = [l for l in board['links'] if l['from'] in results and l['to'] in results]
    return jsonify({"pins": results, "links": board['links']})


# ---------- API: pins ----------
@corkboard_bp.route('/api/pins', methods=['POST'])
def create_pin():
    data = request.get_json() or {}
    board = load_board()
    pin_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    board['pins'][pin_id] = {
        "id": pin_id,
        "title": data.get("title", "Untitled"),
        "content": data.get("content", ""),
        "x": data.get("x", random.randint(40, 480)),
        "y": data.get("y", random.randint(40, 280)),
        "width": data.get("width", 220),
        "height": data.get("height", 160),
        "color": data.get("color", "yellow"),
        "rotation": data.get("rotation", random.choice([-3, -2, -1, 0, 1, 2, 3])),
        "created": now,
        "last_modified": now,
        "tags": data.get("tags", []),
        "type": data.get("type", "note"),
        "filename": data.get("filename")
    }
    save_board(board)
    return jsonify({"id": pin_id, "ok": True, "pin": board['pins'][pin_id]})


@corkboard_bp.route('/api/pins/<pin_id>', methods=['PUT'])
def update_pin(pin_id):
    data = request.get_json() or {}
    board = load_board()
    if pin_id not in board['pins']:
        return jsonify({"error": "Pin not found"}), 404
    pin = board['pins'][pin_id]
    for field in ("title", "content", "x", "y", "width", "height", "color", "rotation", "tags"):
        if field in data:
            pin[field] = data[field]
    pin["last_modified"] = datetime.now().isoformat()
    save_board(board)
    return jsonify({"ok": True})


@corkboard_bp.route('/api/pins/<pin_id>', methods=['DELETE'])
def delete_pin(pin_id):
    board = load_board()
    if pin_id not in board['pins']:
        return jsonify({"error": "Pin not found"}), 404
    del board['pins'][pin_id]
    board['links'] = [l for l in board['links'] if l['from'] != pin_id and l['to'] != pin_id]
    save_board(board)
    return jsonify({"ok": True})


# ---------- API: links (string between pins) with color support ----------
@corkboard_bp.route('/api/links', methods=['POST'])
def toggle_link():
    data = request.get_json() or {}
    a, b = data.get('from'), data.get('to')
    new_color = data.get('color', 'black')  # Default to black if not provided
    if not a or not b or a == b:
        return jsonify({"error": "Invalid link"}), 400
    board = load_board()
    if a not in board['pins'] or b not in board['pins']:
        return jsonify({"error": "Pin not found"}), 404
    existing = None
    for l in board['links']:
        if (l['from'] == a and l['to'] == b) or (l['from'] == b and l['to'] == a):
            existing = l
            break
    if existing:
        board['links'].remove(existing)
        linked = False
    else:
        board['links'].append({"from": a, "to": b, "color": new_color})
        linked = True
    save_board(board)
    return jsonify({"ok": True, "linked": linked, "color": new_color})


# ---------- (Optional) API: change link color ----------
@corkboard_bp.route('/api/links/color', methods=['PUT'])
def change_link_color():
    data = request.get_json() or {}
    a, b, new_color = data.get('from'), data.get('to'), data.get('color', 'red')
    if not a or not b:
        return jsonify({"error": "Missing from/to"}), 400
    board = load_board()
    for l in board['links']:
        if (l['from'] == a and l['to'] == b) or (l['from'] == b and l['to'] == a):
            l['color'] = new_color
            save_board(board)
            return jsonify({"ok": True, "color": new_color})
    return jsonify({"error": "Link not found"}), 404


# ---------- API: clear all ----------
@corkboard_bp.route('/api/clear_all', methods=['POST'])
def clear_all():
    save_board({"pins": {}, "links": []})
    return jsonify({"ok": True})


# ---------- API: file import (md, txt, ipynb, pdf) ----------
ALLOWED_EXT = {"txt", "md", "ipynb", "pdf"}


@corkboard_bp.route('/api/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files['file']
    filename = f.filename or "file"
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''

    if ext not in ALLOWED_EXT:
        return jsonify({"error": f"Unsupported file type: .{ext}. Use md, txt, ipynb or pdf."}), 400

    content = ""
    try:
        if ext in ('txt', 'md'):
            content = f.read().decode('utf-8', errors='replace')

        elif ext == 'ipynb':
            nb = json.load(f)
            parts = []
            for cell in nb.get('cells', []):
                src = cell.get('source', [])
                src_text = ''.join(src) if isinstance(src, list) else str(src)
                if cell.get('cell_type') == 'markdown':
                    parts.append(src_text)
                else:
                    parts.append("```\n" + src_text + "\n```")
            content = "\n\n".join(parts)

        elif ext == 'pdf':
            reader_cls = None
            try:
                from pypdf import PdfReader as reader_cls
            except ImportError:
                try:
                    from PyPDF2 import PdfReader as reader_cls
                except ImportError:
                    reader_cls = None
            if reader_cls is None:
                return jsonify({
                    "error": "PDF support needs a library. Run: pip install pypdf"
                }), 400
            reader = reader_cls(f)
            pages = [(page.extract_text() or "") for page in reader.pages]
            content = "\n\n".join(pages)

    except Exception as e:
        return jsonify({"error": f"Failed to parse file: {e}"}), 400

    board = load_board()
    pin_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    color = {"pdf": "pink", "ipynb": "green", "md": "blue", "txt": "yellow"}.get(ext, "yellow")
    board['pins'][pin_id] = {
        "id": pin_id,
        "title": filename,
        "content": content[:30000],
        "x": random.randint(40, 480),
        "y": random.randint(40, 280),
        "width": 280,
        "height": 200,
        "color": color,
        "rotation": random.choice([-3, -2, -1, 0, 1, 2, 3]),
        "created": now,
        "last_modified": now,
        "tags": [ext],
        "type": "file",
        "filename": filename
    }
    save_board(board)
    return jsonify({"ok": True, "id": pin_id, "pin": board['pins'][pin_id]})


# ---------- HTML template for the cork board page ----------
CORKBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E📌%3C/text%3E%3C/svg%3E">
<title>Cork Board · Advanced</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
/* ── Base (same as app.py) ─────────────────────────── */
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
.app { display:flex; flex-direction:column; height:100%; backdrop-filter: blur(2px); }

/* ── Top bar (identical to app.py) ───────────────── */
.top-bar {
    display:grid; grid-template-columns: 1fr auto 1fr; align-items:center;
    background: rgba(22,27,34,0.7); backdrop-filter: blur(20px);
    border-bottom: 1px solid rgba(255,255,255,0.05);
    padding: 12px 24px; gap:12px; flex-shrink:0;
    box-shadow: 0 4px 12px rgba(0,0,0,0.3);
    transition: background 0.3s, border-color 0.3s;
}
.top-bar .left { display:flex; align-items:center; gap:12px; justify-self:start; }
.top-bar .left h1 {
    font-size:19px; background: linear-gradient(135deg,#58a6ff,#a371f7);
    -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text; font-weight:700;
}
.center-tabs {
    display:flex; gap:4px; background: rgba(255,255,255,0.06); padding:4px;
    border-radius:30px; backdrop-filter: blur(5px); border:1px solid rgba(255,255,255,0.06); justify-self:center;
}
.center-tabs .tab-btn {
    background:transparent; border:none; padding:6px 18px; border-radius:20px; font-size:14px;
    font-weight:500; color:#8b949e; cursor:pointer; transition: all .2s;
    text-decoration:none; display:inline-block;
}
.center-tabs .tab-btn:hover { color:#c9d1d9; background: rgba(255,255,255,0.05); }
.center-tabs .tab-btn.active { background:#1f6feb; color:#fff; box-shadow: 0 2px 8px rgba(31,111,235,0.3); }
.top-bar .right {
    display:flex; align-items:center; gap:10px; flex-wrap:wrap; justify-self:end;
}
.clear-btn {
    background: rgba(33,38,45,0.7); border:1px solid rgba(248,81,73,0.3); color:#f85149;
    border-radius:10px; padding:6px 14px; font-size:12px; cursor:pointer; transition: all .2s; backdrop-filter: blur(5px);
}
.clear-btn:hover { background: rgba(248,81,73,0.15); border-color:#f85149; }

/* ── Theme toggle (exact copy from app.py) ──────── */
.theme-toggle-wrapper { display: inline-block; vertical-align: middle; }
.toggle-outer {
    position:relative; width:140px; height:56px; border-radius:999px;
    background: hsl(220 18% 82%);
    box-shadow: 2px 2px 8px rgba(0,0,0,0.12), -2px -2px 6px rgba(255,255,255,0.5),
                inset 1px 1px 3px rgba(0,0,0,0.08), inset -1px -1px 3px rgba(255,255,255,0.4);
    cursor:pointer; user-select:none; flex-shrink:0;
}
.toggle-inner { position:absolute; inset:5px; border-radius:999px; overflow:hidden; }
.night-bg { position:absolute; inset:0; background: hsl(220 35% 18%); opacity:1; transition: opacity .3s ease; }
.stars-layer { position:absolute; inset:0; opacity:1; transition: opacity .3s ease; pointer-events:none; }
.star { position:absolute; background:white; border-radius:50%; }
.sparkle { position:absolute; color:white; font-size:7px; line-height:1; }
.day-bg { position:absolute; inset:0; opacity:0; transition: opacity .3s ease; pointer-events:none; }
.sky-layer { position:absolute; inset:0; background: hsl(205 70% 62%); }
.sky-mid { position:absolute; bottom:0; left:0; right:0; height:50%; background: hsl(205 60% 72%); border-radius:40% 40% 0 0/30% 30% 0 0; }
.cloud { position:absolute; background: rgba(255,255,255,0.88); border-radius:999px; }
.astronaut, .biplane { position:absolute; z-index:4; pointer-events:none; transition: opacity .3s ease; }
.astronaut { left:48px; top:50%; transform:translateY(-55%); width:22px; height:26px; opacity:1; animation:float 3s ease-in-out infinite; }
.biplane { left:44px; top:38%; transform:translateY(-50%); width:30px; height:18px; opacity:0; animation:fly 3s ease-in-out infinite; }
@keyframes float { 0%,100%{transform:translateY(-55%)} 50%{transform:translateY(-65%)} }
@keyframes fly { 0%,100%{transform:translateY(-50%) rotate(-1deg)} 50%{transform:translateY(-60%) rotate(1deg)} }
.knob {
    position:absolute; top:50%; width:40px; height:40px; border-radius:50%; transform:translateY(-50%);
    z-index:10; cursor:grab; transition: left .4s cubic-bezier(.34,1.2,.64,1); left:3px;
}
.knob:active { cursor:grabbing; }
.knob-moon {
    position:absolute; inset:0; border-radius:50%; background: hsl(220 10% 82%);
    box-shadow: 2px 2px 4px rgba(255,255,255,0.9) inset, -2px -2px 4px rgba(0,0,0,0.18) inset;
    transition: opacity .3s ease;
}
.knob-moon .crater {
    position:absolute; border-radius:50%; background: hsl(220 8% 67%);
    box-shadow: 1px 1px 2px rgba(255,255,255,0.4) inset, -1px -1px 2px rgba(0,0,0,0.2) inset;
}
.knob-sun {
    position:absolute; inset:0; border-radius:50%; background: hsl(44 100% 58%);
    box-shadow: 2px 2px 6px rgba(255,255,180,0.9) inset, -2px -2px 4px rgba(180,100,0,0.3) inset,
                0 0 12px hsl(44 100% 70% / .5);
    opacity:0; transition: opacity .3s ease;
}
.toggle-outer.day .night-bg { opacity:0; }
.toggle-outer.day .stars-layer { opacity:0; }
.toggle-outer.day .day-bg { opacity:1; }
.toggle-outer.day .knob { left:93px; }
.toggle-outer.day .knob-moon { opacity:0; }
.toggle-outer.day .knob-sun { opacity:1; }
.toggle-outer.day .astronaut { opacity:0; }
.toggle-outer.day .biplane { opacity:1; }

/* ── Toolbar (below top bar) ──────────────────────── */
.toolbar {
    display:flex;
    align-items:center;
    gap:10px;
    padding:8px 24px;
    background: rgba(0,0,0,0.2);
    border-bottom: 1px solid rgba(255,255,255,0.05);
    flex-wrap:wrap;
    backdrop-filter: blur(5px);
}
.toolbar .search-input {
    background: rgba(13,17,23,0.7);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 20px;
    padding: 6px 14px;
    color: #e6edf3;
    font-size: 13px;
    outline: none;
    width: 160px;
    transition: border-color 0.2s, width 0.2s;
}
.toolbar .search-input:focus { border-color:#58a6ff; width: 220px; }
.toolbar .search-input::placeholder { color:#8b949e; }
.toolbar .top-btn {
    background: rgba(33,38,45,0.7); border:1px solid rgba(255,255,255,0.1); color:#c9d1d9;
    border-radius:10px; padding:6px 14px; font-size:12px; cursor:pointer; transition: all .2s; backdrop-filter: blur(5px);
    white-space:nowrap;
}
.toolbar .top-btn:hover { background: rgba(88,166,255,0.15); border-color:#58a6ff; }
.toolbar .top-btn.linking { background:#1f6feb; border-color:#1f6feb; color:#fff; }
.toolbar .top-btn.red-thread-active {
    background:#da3633;
    border-color:#da3633;
    color:#fff;
    box-shadow: 0 0 12px rgba(218,54,51,0.5);
}
.toolbar .file-input-wrapper { position:relative; display:inline-block; }
.toolbar .file-input-wrapper input[type="file"] {
    position:absolute; left:0; top:0; opacity:0; width:100%; height:100%; cursor:pointer;
}

/* ── Tag filter bar ───────────────────────────────── */
.tag-filter {
    padding: 6px 24px;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    background: rgba(0,0,0,0.15);
    border-bottom: 1px solid rgba(255,255,255,0.05);
    backdrop-filter: blur(5px);
    min-height: 34px;
    align-items: center;
}
.tag-filter .tag-pill {
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 20px;
    padding: 3px 12px;
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
.tag-filter .filter-label { font-size: 11px; color: #8b949e; margin-right: 4px; }

/* ── Board area ───────────────────────────────────── */
.board-wrap {
    flex:1;
    position:relative;
    overflow:auto;
    padding: 20px 30px;
    background: rgba(10,10,15,0.7);
    backdrop-filter: blur(10px);
    transition: background 0.3s ease;
}
body.light-mode .board-wrap {
    background: rgba(255,255,255,0.85);
}

.board {
    position:relative;
    width:100%;
    height:100%;
    min-height: 600px;
    min-width: 800px;
    background-color: #5a3a24;
    background-image:
        radial-gradient(circle at 20% 30%, rgba(0,0,0,0.15) 0, transparent 3px),
        radial-gradient(circle at 65% 15%, rgba(0,0,0,0.12) 0, transparent 3px),
        radial-gradient(circle at 40% 70%, rgba(0,0,0,0.15) 0, transparent 4px),
        radial-gradient(circle at 85% 55%, rgba(0,0,0,0.1) 0, transparent 3px),
        radial-gradient(circle at 10% 85%, rgba(0,0,0,0.12) 0, transparent 3px),
        radial-gradient(circle at 55% 45%, rgba(0,0,0,0.1) 0, transparent 3px),
        linear-gradient(rgba(0,0,0,0.06), rgba(0,0,0,0.06));
    background-size: 140px 140px, 160px 160px, 130px 130px, 150px 150px, 170px 170px, 120px 120px, cover;
    border-radius: 16px;
    border: 2px solid rgba(255,255,255,0.06);
    box-shadow: 0 8px 32px rgba(0,0,0,0.4);
    transition: background 0.3s, border-color 0.3s;
}
body.light-mode .board {
    background-color: #d9b382;
    border-color: rgba(0,0,0,0.08);
    box-shadow: 0 4px 16px rgba(0,0,0,0.1);
}

svg.link-layer {
    position:absolute; inset:0; width:100%; height:100%; pointer-events:none; z-index:1;
}
.link-line {
    stroke-width:2.5;
    stroke-dasharray:6 4;
    opacity:0.8;
}
.link-line.red {
    stroke:#ff3b30;
    stroke-width:3.5;
    stroke-dasharray:none;
    opacity:0.95;
    filter: drop-shadow(0 0 4px rgba(255,59,48,0.4));
}
.link-line.black {
    stroke:#e8d9b5;
    stroke-width:2;
    stroke-dasharray:6 4;
}
.arrow-marker { fill:#e8d9b5; }
.arrow-marker.red { fill:#ff3b30; }

/* ── Pins ──────────────────────────────────────────── */
.pin {
    position:absolute;
    border-radius:2px;
    cursor:grab;
    z-index:2;
    box-shadow: 3px 5px 10px rgba(0,0,0,0.45);
    font-size:13px;
    color:#2b2b2b;
    transition: box-shadow .15s ease, transform .1s;
    padding: 22px 12px 10px;
    overflow:hidden;
    display:flex;
    flex-direction:column;
}
.pin.dragging { cursor:grabbing; z-index:50; box-shadow: 6px 10px 22px rgba(0,0,0,0.6); }
.pin.link-source { outline:3px solid #58a6ff; outline-offset:2px; }
.pin.color-yellow { background: linear-gradient(#fff8b0,#fdec8f); }
.pin.color-blue   { background: linear-gradient(#bfe3ff,#9fd3ff); }
.pin.color-green  { background: linear-gradient(#c8f0c2,#a9e6a1); }
.pin.color-pink   { background: linear-gradient(#ffd0e0,#ffb3cd); }
.pin.color-orange { background: linear-gradient(#ffd9a8,#ffc27a); }
.pin.color-default{ background: linear-gradient(#e8e8e8,#d0d0d0); }

.pin-nail {
    position:absolute; top:-8px; left:50%; transform:translateX(-50%);
    width:16px; height:16px; border-radius:50%;
    background: radial-gradient(circle at 35% 30%, #f2413a, #8c1a14);
    box-shadow: 0 3px 4px rgba(0,0,0,0.5);
}
.pin-title {
    font-weight:700; font-size:15px; margin-bottom:4px; word-break:break-word;
    outline:none;
    cursor:default;
}
.pin-content {
    flex:1;
    overflow-y:auto;
    font-size:13px;
    line-height:1.5;
    word-break:break-word;
    outline:none;
    cursor:default;
}
.pin-content p { margin:4px 0; }
.pin-content ul, .pin-content ol { padding-left:18px; margin:4px 0; }
.pin-content code { background:rgba(0,0,0,0.08); padding:1px 4px; border-radius:3px; }
.pin-content pre { background:rgba(0,0,0,0.08); padding:6px; border-radius:4px; overflow-x:auto; }
.pin-tags {
    display:flex;
    flex-wrap:wrap;
    gap:4px;
    margin-top:6px;
}
.pin-tags .tag-label {
    font-size:10px;
    background:rgba(0,0,0,0.1);
    padding:1px 8px;
    border-radius:12px;
    color:#3d3d3d;
}
.pin-timestamp {
    font-size:9px;
    color:#5a5a5a;
    margin-top:4px;
    text-align:right;
    opacity:0.5;
}
.pin-toolbar {
    display:flex;
    gap:4px;
    margin-top:6px;
    justify-content:flex-end;
    opacity:0;
    transition: opacity .2s;
}
.pin:hover .pin-toolbar { opacity:1; }
.pin-toolbar button {
    background: rgba(0,0,0,0.08);
    border:none;
    border-radius:4px;
    padding:2px 6px;
    font-size:11px;
    cursor:pointer;
    color:#2b2b2b;
}
.pin-toolbar button:hover { background: rgba(0,0,0,0.18); }
.pin-toolbar .del-pin { color:#b33; }

.empty-hint {
    position:absolute; top:40px; left:40px; color:rgba(255,255,255,0.55); font-size:14px;
    background: rgba(0,0,0,0.25); padding:10px 14px; border-radius:8px; max-width:320px;
}
body.light-mode .empty-hint { color:rgba(0,0,0,0.5); background: rgba(255,255,255,0.2); }

/* ── Edit Modal ────────────────────────────────────── */
.modal-overlay {
    display:none;
    position:fixed; top:0; left:0; right:0; bottom:0;
    background: rgba(0,0,0,0.7);
    backdrop-filter: blur(8px);
    z-index:1000;
    align-items:center;
    justify-content:center;
}
.modal-overlay.active { display:flex; }
.modal {
    background: #1c2333;
    border-radius:16px;
    padding:24px 28px;
    max-width:600px;
    width:90%;
    max-height:90vh;
    overflow-y:auto;
    box-shadow: 0 20px 60px rgba(0,0,0,0.6);
    color:#e1e4e8;
    transition: background 0.3s;
}
body.light-mode .modal {
    background: #f0f2f5;
    color:#24292f;
}
.modal h2 { margin-bottom:16px; }
.modal label {
    display:block;
    font-size:13px;
    font-weight:600;
    margin:10px 0 4px;
}
.modal input[type="text"],
.modal textarea {
    width:100%;
    padding:8px 12px;
    border-radius:8px;
    border:1px solid rgba(255,255,255,0.1);
    background: rgba(0,0,0,0.3);
    color:#e6edf3;
    font-size:14px;
    outline:none;
    transition: border-color .2s;
}
.modal input[type="text"]:focus,
.modal textarea:focus { border-color:#58a6ff; }
body.light-mode .modal input[type="text"],
body.light-mode .modal textarea {
    background: #fff;
    color:#24292f;
    border-color:rgba(0,0,0,0.12);
}
.modal textarea { min-height:120px; resize:vertical; }
.modal .preview-box {
    background: rgba(0,0,0,0.2);
    border-radius:8px;
    padding:8px 12px;
    margin-top:6px;
    max-height:150px;
    overflow-y:auto;
    font-size:14px;
    line-height:1.5;
    display:none;
}
.modal .preview-box.visible { display:block; }
.modal .row {
    display:flex;
    gap:12px;
    flex-wrap:wrap;
    align-items:center;
}
.modal .row .col { flex:1; min-width:120px; }
.modal .color-picker {
    display:flex;
    gap:8px;
    flex-wrap:wrap;
    margin:6px 0;
}
.modal .color-option {
    width:28px; height:28px; border-radius:50%;
    cursor:pointer;
    border:2px solid transparent;
    transition: 0.2s;
}
.modal .color-option:hover { transform:scale(1.1); }
.modal .color-option.active { border-color:#58a6ff; box-shadow:0 0 10px rgba(88,166,255,0.4); }
.color-default { background:#b0b0b0; }
.color-yellow { background:#fdec8f; }
.color-blue   { background:#9fd3ff; }
.color-green  { background:#a9e6a1; }
.color-pink   { background:#ffb3cd; }
.color-orange { background:#ffc27a; }

.modal .modal-actions {
    display:flex;
    justify-content:flex-end;
    gap:10px;
    margin-top:16px;
}
.modal .modal-actions button {
    padding:8px 20px;
    border-radius:8px;
    border:none;
    font-size:14px;
    cursor:pointer;
    transition:0.2s;
}
.modal .modal-actions .save-btn { background:#1f6feb; color:#fff; }
.modal .modal-actions .save-btn:hover { background:#388bfd; }
.modal .modal-actions .cancel-btn { background:rgba(255,255,255,0.1); color:#8b949e; }
.modal .modal-actions .cancel-btn:hover { background:rgba(255,255,255,0.2); }
.modal .modal-actions .delete-btn { background:rgba(248,81,73,0.2); color:#f85149; }
.modal .modal-actions .delete-btn:hover { background:rgba(248,81,73,0.4); }

/* ── LIGHT MODE overrides ───────────────────────────── */
body.light-mode {
    background: #f6f8fa;
    color: #24292f;
}
body.light-mode .top-bar {
    background: rgba(255,255,255,0.9);
    border-bottom-color: rgba(0,0,0,0.08);
}
body.light-mode .top-bar .left h1 {
    background: linear-gradient(135deg,#1f6feb,#a371f7);
    -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text;
}
body.light-mode .center-tabs {
    background: rgba(0,0,0,0.04);
    border-color: rgba(0,0,0,0.06);
}
body.light-mode .center-tabs .tab-btn {
    color: #57606a;
}
body.light-mode .center-tabs .tab-btn.active {
    background: #1f6feb;
    color: #fff;
}
body.light-mode .toolbar .top-btn {
    background: rgba(0,0,0,0.04);
    color: #24292f;
    border-color: rgba(0,0,0,0.12);
}
body.light-mode .clear-btn {
    background: rgba(0,0,0,0.04);
    border-color: rgba(248,81,73,0.3);
    color: #f85149;
}
body.light-mode .clear-btn:hover {
    background: rgba(248,81,73,0.08);
}
body.light-mode .toolbar .search-input {
    background: rgba(255,255,255,0.8);
    color:#24292f;
    border-color:rgba(0,0,0,0.12);
}
body.light-mode .tag-filter {
    background: rgba(0,0,0,0.03);
}
body.light-mode .tag-filter .tag-pill {
    background: rgba(0,0,0,0.04);
    border-color:rgba(0,0,0,0.08);
    color:#57606a;
}
body.light-mode .tag-filter .tag-pill.active {
    background:#1f6feb;
    color:#fff;
}
</style>
</head>
<body>
<div class="app">
    <!-- TOP BAR (clean – matches app.py) -->
    <div class="top-bar">
        <div class="left">
            <h1>📌 Cork Board</h1>
        </div>
        <div class="center-tabs">
            <a href="/" class="tab-btn" style="text-decoration:none;">💬 Chat</a>
            <a href="/notes" class="tab-btn" style="text-decoration:none;">📝 Notes</a>
            <button class="tab-btn active">📌 Cork Board</button>
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
                        <div class="astronaut">
                            <svg viewBox="0 0 44 54" width="22" height="26" xmlns="http://www.w3.org/2000/svg">
                                <ellipse cx="22" cy="36" rx="13" ry="14" fill="#e8e8e8"/>
                                <circle cx="22" cy="18" r="13" fill="#d0d8e8"/>
                                <circle cx="22" cy="18" r="10" fill="#c8d8f0" opacity="0.4"/>
                                <ellipse cx="22" cy="19" rx="7" ry="6" fill="#5a7ab0" opacity="0.85"/>
                                <circle cx="22" cy="20" r="5" fill="#c8844a"/>
                                <circle cx="20" cy="18.5" r="1.2" fill="#7a3a0a"/>
                                <circle cx="24" cy="18.5" r="1.2" fill="#7a3a0a"/>
                                <ellipse cx="22" cy="21" rx="2" ry="1.2" fill="#b06030"/>
                                <circle cx="10" cy="11" r="3.5" fill="#d0d8e8"/>
                                <circle cx="34" cy="11" r="3.5" fill="#d0d8e8"/>
                                <text x="22" y="37" text-anchor="middle" font-size="8" fill="#bbb">★</text>
                                <ellipse cx="9" cy="36" rx="4" ry="8" fill="#e0e0e0" transform="rotate(-10 9 36)"/>
                                <ellipse cx="35" cy="36" rx="4" ry="8" fill="#e0e0e0" transform="rotate(10 35 36)"/>
                                <ellipse cx="16" cy="49" rx="5" ry="5" fill="#d0d0d0"/>
                                <ellipse cx="28" cy="49" rx="5" ry="5" fill="#d0d0d0"/>
                                <ellipse cx="16" cy="52" rx="6" ry="3" fill="#b0b0b8"/>
                                <ellipse cx="28" cy="52" rx="6" ry="3" fill="#b0b0b8"/>
                                <ellipse cx="22" cy="28" rx="9" ry="3" fill="none" stroke="#c0c8d8" stroke-width="2"/>
                            </svg>
                        </div>
                        <div class="biplane">
                            <svg viewBox="0 0 70 42" width="30" height="18" xmlns="http://www.w3.org/2000/svg">
                                <rect x="14" y="4" width="42" height="8" rx="4" fill="#d0d8e0"/>
                                <ellipse cx="35" cy="22" rx="22" ry="9" fill="#e8e0d8"/>
                                <ellipse cx="58" cy="22" rx="8" ry="6" fill="#d0c8c0"/>
                                <polygon points="8,14 14,20 8,26" fill="#c8d0d8"/>
                                <rect x="4" y="15" width="12" height="5" rx="2" fill="#c0c8d0"/>
                                <rect x="18" y="26" width="34" height="6" rx="3" fill="#c8d0d8"/>
                                <line x1="22" y1="12" x2="22" y2="26" stroke="#aab0b8" stroke-width="1.5"/>
                                <line x1="48" y1="12" x2="48" y2="26" stroke="#aab0b8" stroke-width="1.5"/>
                                <ellipse cx="44" cy="17" rx="7" ry="5" fill="#7aaecc" opacity="0.8"/>
                                <circle cx="44" cy="15" r="5" fill="#c8844a"/>
                                <circle cx="42.5" cy="13.5" r="1" fill="#6b3a1f"/>
                                <circle cx="45.5" cy="13.5" r="1" fill="#6b3a1f"/>
                                <ellipse cx="44" cy="16" rx="1.5" ry="1" fill="#b06030"/>
                                <circle cx="40" cy="11" r="2" fill="#c8844a"/>
                                <circle cx="48" cy="11" r="2" fill="#c8844a"/>
                                <line x1="66" y1="13" x2="66" y2="31" stroke="#8a7060" stroke-width="3" stroke-linecap="round"/>
                                <circle cx="66" cy="22" r="2.5" fill="#6a5040"/>
                            </svg>
                        </div>
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
            <button class="clear-btn" onclick="clearAllPins()">🗑 Clear All</button>
        </div>
    </div>

    <!-- TOOLBAR (actions) -->
    <div class="toolbar" id="toolbar">
        <input type="text" class="search-input" id="searchInput" placeholder="🔍 Search pins..." oninput="searchPins()">
        <button class="top-btn" onclick="createNewPin()">+ New Note</button>
        <span class="file-input-wrapper">
            <button class="top-btn">📎 Import File</button>
            <input type="file" accept=".md,.txt,.ipynb,.pdf" onchange="handleFileUpload(event)">
        </span>
        <button class="top-btn" id="linkBtn" onclick="toggleLinkMode()">🔗 Link Mode</button>
        <button class="top-btn" id="redThreadBtn" onclick="toggleRedThread()">🔴 Red Thread</button>
    </div>

    <!-- TAG FILTER -->
    <div class="tag-filter" id="tagFilterContainer"></div>

    <!-- BOARD AREA -->
    <div class="board-wrap" id="boardWrap">
        <div class="board" id="board">
            <svg class="link-layer" id="linkLayer">
                <defs>
                    <marker id="arrowhead-black" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto">
                        <polygon points="0 0, 10 3.5, 0 7" class="arrow-marker" />
                    </marker>
                    <marker id="arrowhead-red" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto">
                        <polygon points="0 0, 10 3.5, 0 7" class="arrow-marker red" />
                    </marker>
                </defs>
            </svg>
            <div class="empty-hint" id="emptyHint" style="display:none;">
                📌 Empty board. Click "+ New Note" or "📎 Import File" to pin something.
                Drag to arrange, double‑click to edit.
            </div>
        </div>
    </div>
</div>

<!-- EDIT MODAL -->
<div class="modal-overlay" id="editModal">
    <div class="modal">
        <h2 id="modalTitle">Edit Pin</h2>
        <label for="pinTitle">Title</label>
        <input type="text" id="pinTitle" placeholder="Title...">
        <label for="pinContent">Content (Markdown)</label>
        <textarea id="pinContent" placeholder="Write your note in Markdown..."></textarea>
        <div style="margin-top:4px;">
            <button onclick="togglePreview()" style="background:rgba(255,255,255,0.05); border:1px solid rgba(255,255,255,0.1); border-radius:6px; padding:4px 12px; color:#8b949e; cursor:pointer;">👁️ Preview</button>
        </div>
        <div class="preview-box" id="pinPreview"></div>
        <label>Tags (comma separated)</label>
        <input type="text" id="pinTags" placeholder="e.g. project, idea">
        <div class="row">
            <div class="col">
                <label>Colour</label>
                <div class="color-picker" id="pinColorPicker">
                    <span class="color-option color-default" data-color="default" title="Default"></span>
                    <span class="color-option color-yellow" data-color="yellow" title="Yellow"></span>
                    <span class="color-option color-blue" data-color="blue" title="Blue"></span>
                    <span class="color-option color-green" data-color="green" title="Green"></span>
                    <span class="color-option color-pink" data-color="pink" title="Pink"></span>
                    <span class="color-option color-orange" data-color="orange" title="Orange"></span>
                </div>
            </div>
            <div class="col">
                <label>Width</label>
                <input type="number" id="pinWidth" min="120" max="500" value="220">
            </div>
            <div class="col">
                <label>Height</label>
                <input type="number" id="pinHeight" min="100" max="600" value="160">
            </div>
        </div>
        <div class="modal-actions">
            <button class="delete-btn" id="modalDeleteBtn" onclick="deleteCurrentPin()">🗑 Delete</button>
            <button class="cancel-btn" onclick="closeModal()">Cancel</button>
            <button class="save-btn" onclick="savePinFromModal()">💾 Save</button>
        </div>
    </div>
</div>

<script>
// ─── THEME (exact copy from app.py) ─────────────────
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

// ─── STATE ────────────────────────────────────────────
var boardEl = document.getElementById('board');
var linkLayer = document.getElementById('linkLayer');
var boardData = { pins: {}, links: [] };
var linkMode = false;
var linkSourceId = null;
var saveTimers = {};
var activeTagFilter = '';
var searchQuery = '';
var editingPinId = null;
var redThreadMode = false;

// ─── Load & render ────────────────────────────────────
function loadBoard() {
    var query = document.getElementById('searchInput').value.trim();
    var tag = activeTagFilter;
    var url = '/corkboard/api/search?q=' + encodeURIComponent(query) + '&tag=' + encodeURIComponent(tag);
    fetch(url)
        .then(r => r.json())
        .then(data => {
            boardData = data;
            renderAll();
            renderTagFilter();
        })
        .catch(e => console.error('Failed to load board:', e));
}

function renderAll() {
    document.querySelectorAll('.pin').forEach(el => el.remove());
    var ids = Object.keys(boardData.pins);
    document.getElementById('emptyHint').style.display = ids.length ? 'none' : 'block';
    ids.forEach(id => renderPin(boardData.pins[id]));
    renderLinks();
}

function renderPin(pin) {
    var div = document.createElement('div');
    var colorClass = 'color-' + (pin.color || 'yellow');
    div.className = 'pin ' + colorClass;
    div.dataset.id = pin.id;
    var w = pin.width || 220;
    var h = pin.height || 160;
    div.style.width = w + 'px';
    div.style.height = h + 'px';
    div.style.left = (pin.x || 40) + 'px';
    div.style.top = (pin.y || 40) + 'px';
    div.style.transform = 'rotate(' + (pin.rotation || 0) + 'deg)';

    var contentHtml = marked.parse(pin.content || '');
    var tagsHtml = (pin.tags || []).map(t => `<span class="tag-label">#${escapeHtml(t)}</span>`).join('');

    div.innerHTML = `
        <div class="pin-nail"></div>
        <div class="pin-title">${escapeHtml(pin.title)}</div>
        <div class="pin-content">${contentHtml}</div>
        ${tagsHtml ? `<div class="pin-tags">${tagsHtml}</div>` : ''}
        <div class="pin-timestamp">${new Date(pin.last_modified || pin.created).toLocaleString()}</div>
        <div class="pin-toolbar">
            <button class="edit-pin" title="Edit">✏️</button>
            <button class="del-pin" title="Delete">🗑</button>
        </div>
    `;
    boardEl.appendChild(div);

    div.querySelector('.edit-pin').addEventListener('click', function(e) {
        e.stopPropagation();
        openEditModal(pin.id);
    });
    div.querySelector('.del-pin').addEventListener('click', function(e) {
        e.stopPropagation();
        if (confirm('Delete this pin?')) deletePin(pin.id);
    });
    div.addEventListener('dblclick', function(e) {
        e.stopPropagation();
        openEditModal(pin.id);
    });
    div.addEventListener('mousedown', e => dragStart(e, div, pin.id));
    div.addEventListener('touchstart', e => dragStart(e, div, pin.id), { passive: false });

    div.addEventListener('click', function(e) {
        if (!linkMode) return;
        if (e.target.closest('.pin-title') || e.target.closest('.pin-content') ||
            e.target.closest('.pin-tags') || e.target.closest('.pin-timestamp') ||
            e.target.closest('.pin-toolbar')) return;
        handleLinkClick(pin.id, div);
    });
}

function escapeHtml(text) {
    var d = document.createElement('div');
    d.textContent = text || '';
    return d.innerHTML;
}

// ─── DRAG ─────────────────────────────────────────────
var dragCtx = null;
function dragStart(e, el, id) {
    if (e.target.closest('.pin-title') || e.target.closest('.pin-content') ||
        e.target.closest('.pin-tags') || e.target.closest('.pin-timestamp') ||
        e.target.closest('.pin-toolbar')) return;
    if (linkMode) return;
    e.preventDefault();
    var point = e.touches ? e.touches[0] : e;
    var rect = el.getBoundingClientRect();
    var boardRect = boardEl.getBoundingClientRect();
    dragCtx = {
        el: el, id: id,
        offsetX: point.clientX - rect.left,
        offsetY: point.clientY - rect.top,
        boardRect: boardRect
    };
    el.classList.add('dragging');
    window.addEventListener('mousemove', dragMove);
    window.addEventListener('mouseup', dragEnd);
    window.addEventListener('touchmove', dragMove, { passive: false });
    window.addEventListener('touchend', dragEnd);
}
function dragMove(e) {
    if (!dragCtx) return;
    e.preventDefault();
    var point = e.touches ? e.touches[0] : e;
    var x = point.clientX - dragCtx.boardRect.left - dragCtx.offsetX;
    var y = point.clientY - dragCtx.boardRect.top - dragCtx.offsetY;
    x = Math.max(0, x); y = Math.max(0, y);
    dragCtx.el.style.left = x + 'px';
    dragCtx.el.style.top = y + 'px';
    boardData.pins[dragCtx.id].x = x;
    boardData.pins[dragCtx.id].y = y;
    renderLinks();
}
function dragEnd() {
    if (!dragCtx) return;
    dragCtx.el.classList.remove('dragging');
    var id = dragCtx.id;
    var pin = boardData.pins[id];
    savePin(id, { x: pin.x, y: pin.y }, true);
    dragCtx = null;
    window.removeEventListener('mousemove', dragMove);
    window.removeEventListener('mouseup', dragEnd);
    window.removeEventListener('touchmove', dragMove);
    window.removeEventListener('touchend', dragEnd);
}

// ─── SAVE ─────────────────────────────────────────────
function savePin(id, fields, immediate) {
    Object.assign(boardData.pins[id], fields);
    if (saveTimers[id]) clearTimeout(saveTimers[id]);
    var run = () => {
        fetch('/corkboard/api/pins/' + id, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(fields)
        }).catch(e => console.error('Save failed:', e));
    };
    if (immediate) run(); else saveTimers[id] = setTimeout(run, 400);
}

// ─── CRUD ────────────────────────────────────────────
function createNewPin() {
    var wrap = document.getElementById('boardWrap');
    fetch('/corkboard/api/pins', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            title: 'New Note',
            content: 'Click to edit...',
            x: wrap.scrollLeft + 60,
            y: wrap.scrollTop + 60,
            width: 220,
            height: 160
        })
    }).then(r => r.json()).then(data => {
        if (data.ok) {
            boardData.pins[data.id] = data.pin;
            renderAll();
            renderTagFilter();
        }
    });
}

function deletePin(id) {
    if (!confirm('Delete this pin?')) return;
    fetch('/corkboard/api/pins/' + id, { method: 'DELETE' })
        .then(r => r.json()).then(data => {
            if (data.ok) {
                delete boardData.pins[id];
                renderAll();
                renderTagFilter();
            }
        });
}

function clearAllPins() {
    if (!confirm('Delete ALL pins and links?')) return;
    fetch('/corkboard/api/clear_all', { method: 'POST' })
        .then(r => r.json()).then(data => {
            if (data.ok) {
                boardData = { pins: {}, links: [] };
                renderAll();
                renderTagFilter();
            }
        });
}

// ─── FILE IMPORT ─────────────────────────────────────
function handleFileUpload(e) {
    var file = e.target.files[0];
    if (!file) return;
    var formData = new FormData();
    formData.append('file', file);
    fetch('/corkboard/api/upload', { method: 'POST', body: formData })
        .then(r => r.json())
        .then(data => {
            if (data.ok) {
                boardData.pins[data.id] = data.pin;
                renderAll();
                renderTagFilter();
            } else alert(data.error || 'Upload failed');
        })
        .catch(err => alert('Upload failed: ' + err));
    e.target.value = '';
}

// ─── SEARCH & TAG FILTER ──────────────────────────────
function searchPins() {
    loadBoard();
}

function setTagFilter(tag) {
    activeTagFilter = tag;
    loadBoard();
}

function renderTagFilter() {
    var container = document.getElementById('tagFilterContainer');
    var tagSet = new Set();
    Object.values(boardData.pins).forEach(p => {
        if (p.tags) p.tags.forEach(t => tagSet.add(t));
    });
    var tags = Array.from(tagSet).sort();
    var html = '';
    if (tags.length) {
        html += `<span class="filter-label">Filter:</span>`;
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

// ─── RED THREAD TOGGLE ──────────────────────────────
function toggleRedThread() {
    redThreadMode = !redThreadMode;
    document.getElementById('redThreadBtn').classList.toggle('red-thread-active', redThreadMode);
    document.getElementById('redThreadBtn').title = redThreadMode ? 'Red Thread ON' : 'Red Thread OFF';
}

// ─── LINKING ─────────────────────────────────────────
function toggleLinkMode() {
    linkMode = !linkMode;
    linkSourceId = null;
    document.getElementById('linkBtn').classList.toggle('linking', linkMode);
    document.querySelectorAll('.pin').forEach(p => p.classList.remove('link-source'));
}
function handleLinkClick(id, el) {
    if (!linkSourceId) {
        linkSourceId = id;
        el.classList.add('link-source');
        return;
    }
    if (linkSourceId === id) {
        linkSourceId = null;
        el.classList.remove('link-source');
        return;
    }
    var color = redThreadMode ? 'red' : 'black';
    fetch('/corkboard/api/links', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ from: linkSourceId, to: id, color: color })
    }).then(r => r.json()).then(data => {
        if (data.ok) {
            if (data.linked) {
                boardData.links.push({ from: linkSourceId, to: id, color: color });
            } else {
                boardData.links = boardData.links.filter(l =>
                    !((l.from === linkSourceId && l.to === id) || (l.from === id && l.to === linkSourceId)));
            }
            renderLinks();
        }
        document.querySelectorAll('.pin').forEach(p => p.classList.remove('link-source'));
        linkSourceId = null;
    });
}

function renderLinks() {
    linkLayer.innerHTML = `
        <defs>
            <marker id="arrowhead-black" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto">
                <polygon points="0 0, 10 3.5, 0 7" class="arrow-marker" />
            </marker>
            <marker id="arrowhead-red" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto">
                <polygon points="0 0, 10 3.5, 0 7" class="arrow-marker red" />
            </marker>
        </defs>
    `;
    boardData.links.forEach(link => {
        var a = boardData.pins[link.from], b = boardData.pins[link.to];
        if (!a || !b) return;
        var x1 = a.x + (a.width || 220)/2, y1 = a.y + (a.height || 160)/2;
        var x2 = b.x + (b.width || 220)/2, y2 = b.y + (b.height || 160)/2;
        var line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', x1); line.setAttribute('y1', y1);
        line.setAttribute('x2', x2); line.setAttribute('y2', y2);
        var isRed = (link.color === 'red');
        line.setAttribute('class', isRed ? 'link-line red' : 'link-line black');
        line.setAttribute('marker-end', isRed ? 'url(#arrowhead-red)' : 'url(#arrowhead-black)');
        linkLayer.appendChild(line);
    });
}

// ─── EDIT MODAL ──────────────────────────────────────
function openEditModal(id) {
    var pin = boardData.pins[id];
    if (!pin) return;
    editingPinId = id;
    document.getElementById('pinTitle').value = pin.title || '';
    document.getElementById('pinContent').value = pin.content || '';
    document.getElementById('pinTags').value = (pin.tags || []).join(', ');
    document.getElementById('pinWidth').value = pin.width || 220;
    document.getElementById('pinHeight').value = pin.height || 160;
    var color = pin.color || 'default';
    document.querySelectorAll('#pinColorPicker .color-option').forEach(el => {
        el.classList.toggle('active', el.dataset.color === color);
    });
    document.getElementById('modalTitle').textContent = 'Edit Pin';
    document.getElementById('modalDeleteBtn').style.display = 'inline-block';
    document.getElementById('pinPreview').innerHTML = '';
    document.getElementById('pinPreview').classList.remove('visible');
    document.getElementById('editModal').classList.add('active');
}

function closeModal() {
    document.getElementById('editModal').classList.remove('active');
    editingPinId = null;
}

function togglePreview() {
    var preview = document.getElementById('pinPreview');
    var content = document.getElementById('pinContent').value;
    preview.innerHTML = marked.parse(content || '');
    preview.classList.toggle('visible');
}

function savePinFromModal() {
    if (!editingPinId) return;
    var title = document.getElementById('pinTitle').value.trim() || 'Untitled';
    var content = document.getElementById('pinContent').value;
    var tags = document.getElementById('pinTags').value.split(',').map(s => s.trim()).filter(Boolean);
    var width = parseInt(document.getElementById('pinWidth').value) || 220;
    var height = parseInt(document.getElementById('pinHeight').value) || 160;
    var color = document.querySelector('#pinColorPicker .color-option.active')?.dataset.color || 'default';

    var pin = boardData.pins[editingPinId];
    var fields = { title, content, tags, width, height, color };
    fetch('/corkboard/api/pins/' + editingPinId, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(fields)
    }).then(r => r.json()).then(data => {
        if (data.ok) {
            Object.assign(pin, fields);
            pin.last_modified = new Date().toISOString();
            renderAll();
            renderTagFilter();
            closeModal();
        } else alert('Save failed');
    }).catch(err => alert('Error: ' + err));
}

function deleteCurrentPin() {
    if (!editingPinId) return;
    if (confirm('Delete this pin?')) {
        deletePin(editingPinId);
        closeModal();
    }
}

// ─── Colour picker in modal ──────────────────────────
document.querySelectorAll('#pinColorPicker .color-option').forEach(el => {
    el.addEventListener('click', function() {
        document.querySelectorAll('#pinColorPicker .color-option').forEach(c => c.classList.remove('active'));
        this.classList.add('active');
    });
});

// ─── INIT ────────────────────────────────────────────
window.addEventListener('load', function() {
    loadBoard();
    document.getElementById('editModal').addEventListener('click', function(e) {
        if (e.target === this) closeModal();
    });
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') closeModal();
    });
    // Ensure red thread button starts off
    document.getElementById('redThreadBtn').classList.remove('red-thread-active');
    document.getElementById('redThreadBtn').title = 'Red Thread OFF';
    redThreadMode = false;
});
</script>
</body>
</html>
"""