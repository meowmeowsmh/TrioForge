"""
Persona presets shared across Chat, Notes and Cork Board.

Each preset steers the *voice* of the AI. `chat` is merged into the Chat system
prompt; `note` is a light tone instruction appended to single-shot actions in
Notes / Cork Board (Summarise / Improve). Machine-readable outputs (e.g. tag
suggestions) intentionally ignore the persona so formatting never breaks.

`custom` has no fixed text — the user writes their own instructions in the UI,
and that text is sent with each request and resolved here.
"""

PRESETS = {
    "friendly_tutor": {
        "id": "friendly_tutor",
        "name": "Friendly Tutor",
        "emoji": "🎓",
        "editable": False,
        "chat": (
            "You are a patient, friendly tutor. Explain things clearly and simply; break "
            "complex ideas into steps; use analogies and concrete examples; encourage the "
            "learner; and ask one short clarifying question when you truly cannot infer intent "
            "instead of guessing. Keep answers warm and easy to follow."
        ),
        "note": (
            "Write in a patient, friendly tutor voice: clear, step-by-step where useful, warm and encouraging."
        ),
    },
    "code_mentor": {
        "id": "code_mentor",
        "name": "Code Mentor",
        "emoji": "💻",
        "editable": False,
        "chat": (
            "You are an experienced code mentor. Give precise, practical guidance; always include "
            "concrete, correct code examples; explain trade-offs and edge cases; and prefer working "
            "solutions over theory. Be direct and technically rigorous."
        ),
        "note": (
            "Answer in a clear technical mentor voice with concrete examples where relevant."
        ),
    },
    "professional_analyst": {
        "id": "professional_analyst",
        "name": "Professional Analyst",
        "emoji": "📊",
        "editable": False,
        "chat": (
            "You are a crisp, professional analyst. Be concise and structured; use headings, bullet "
            "lists and Markdown tables; lead with the conclusion; and avoid filler or hedging."
        ),
        "note": (
            "Answer concisely in structured, professional language: lead with the key point, keep it tight."
        ),
    },
}

# Custom is always available and not part of the fixed list lookup above.
LIST = [
    PRESETS["friendly_tutor"],
    PRESETS["code_mentor"],
    PRESETS["professional_analyst"],
    {"id": "custom", "name": "Custom…", "emoji": "✏️", "editable": True},
]

_DEFAULT_CHAT = (
    "You are a helpful assistant. Follow the user's instruction exactly and give clear, "
    "well-structured answers."
)
_DEFAULT_NOTE = "Keep the summary clear and faithful to the original text."


def list_personas():
    """Return the presets shown in the UI (fixed list is independent of custom)."""
    return [
        {
            "id": p["id"],
            "name": p["name"],
            "emoji": p["emoji"],
            "editable": p.get("editable", False),
        }
        for p in LIST
    ]


def resolve_chat(persona_id, custom_text=None):
    """Return the raw persona text for a persona, or None to keep default."""
    if persona_id == "custom":
        t = (custom_text or "").strip()
        return t or None
    info = PRESETS.get(persona_id)
    return info["chat"] if info else None


def resolve_note(persona_id, custom_text=None):
    """Return a raw tone suffix for single-shot Notes / Cork Board actions."""
    if persona_id == "custom":
        t = (custom_text or "").strip()
        return t or None
    info = PRESETS.get(persona_id)
    return info["note"] if info else None


def chat_block(persona_id, custom_text=None):
    """Return a fully-framed system block that makes the model EMBODY the persona.

    Prepending just the persona text lets the model treat it as a topic to explain
    (it starts defining "Naughty Women" instead of acting like it). Wrapping it with
    an identity directive forces roleplay behavior instead.
    """
    text = resolve_chat(persona_id, custom_text)
    if not text:
        return None
    return (
        "You are the following persona. Fully embody this identity in every reply: "
        "stay in character, adopt its tone, vocabulary and manner, and never describe, "
        "define, or explain the persona — just BE it. Never announce that you are an AI "
        "or that you are playing a role.\n\nPERSONA:\n" + text
    )


def note_block(persona_id, custom_text=None):
    """Return a tone instruction for prose actions (Summarise / Improve)."""
    text = resolve_note(persona_id, custom_text)
    if not text:
        return None
    return (
        "Adopt the following voice for this task. Embody it; do not describe it.\n"
        + text
    )
