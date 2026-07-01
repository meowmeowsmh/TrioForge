# notes.py
import os
import json
import uuid
from datetime import datetime
from flask import Blueprint, request, jsonify

NOTES_FILE = "json_configuration/notes.json"

# Ensure the folder and file exist
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

@notes_bp.route('', methods=['GET'])
def get_notes():
    """Return all notes as JSON."""
    return jsonify(load_notes())

@notes_bp.route('', methods=['POST'])
def create_note():
    """Create a new note."""
    data = request.get_json()
    notes = load_notes()
    note_id = str(uuid.uuid4())
    notes[note_id] = {
        "id": note_id,
        "title": data.get("title", "Untitled"),
        "content": data.get("content", ""),
        "created": datetime.now().isoformat()
    }
    save_notes(notes)
    return jsonify({"id": note_id, "ok": True})

@notes_bp.route('/<note_id>', methods=['PUT'])
def update_note(note_id):
    """Update a note's title and/or content."""
    data = request.get_json()
    notes = load_notes()
    if note_id not in notes:
        return jsonify({"error": "Note not found"}), 404
    if "title" in data:
        notes[note_id]["title"] = data["title"]
    if "content" in data:
        notes[note_id]["content"] = data["content"]
    save_notes(notes)
    return jsonify({"ok": True})

@notes_bp.route('/<note_id>', methods=['DELETE'])
def delete_note(note_id):
    """Delete a note."""
    notes = load_notes()
    if note_id not in notes:
        return jsonify({"error": "Note not found"}), 404
    del notes[note_id]
    save_notes(notes)
    return jsonify({"ok": True})