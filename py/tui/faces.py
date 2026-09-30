"""ASCII text faces for the sidebar mood indicator.

The face sits under Providers and reflects what the agent is doing, frame by
frame: it thinks while reasoning, "glitches" while a tool runs, beams when a
turn finishes, frowns on an error, and sits neutral when idle. Variants are
cycled on each tick so the face animates while it is on screen.
"""

from __future__ import annotations

# (left_eye, right_eye, mouth) per emotion, in animation order.
TEXT_FACES = {
    "happy": {
        "emoji": "😊",
        "variants": [
            ("●", "●", "  ᴗ"),
            ("^", "^", "  ᴗ"),
            ("•", "•", "  ω"),
            ("◕", "◕", "  ᴗ"),
            (">", "<", "  ᴗ"),
        ],
    },
    "neutral": {
        "emoji": "😐",
        "variants": [
            ("●", "●", "  ─"),
            ("-", "-", "  ─"),
            ("•", "•", "  ─"),
            ("o", "o", "  _"),
        ],
    },
    "sad": {
        "emoji": "😢",
        "variants": [
            ("●", "●", "  ︵"),
            ("•", "•", "  ︶"),
            ("╥", "╥", "  ︵"),
            ("T", "T", "  ﹏"),
        ],
    },
    "angry": {
        "emoji": "😡",
        "variants": [
            (">", "<", "  ︿"),
            ("ಠ", "ಠ", "  ╰"),
            ("¬", "¬", "  ︿"),
            (">", ">", "  ─"),
        ],
    },
    "surprised": {
        "emoji": "😮",
        "variants": [
            ("O", "O", "  O"),
            ("o", "o", "  0"),
            ("●", "●", "  O"),
        ],
    },
    "sleepy": {
        "emoji": "😴",
        "variants": [
            ("-", "-", "  ᴗ"),
            ("-", "-", "  z"),
            ("﹏", "﹏", "  ᴗ"),
        ],
    },
    "thinking": {
        "emoji": "🤔",
        "variants": [
            ("●", "●", "  ~"),
            ("o", "●", "  ?"),
            ("•", "•", "  ─"),
        ],
    },
    "thinking_finished": {
        "emoji": "💡",
        "variants": [
            ("●", "●  !", "  ᴗ"),
            ("✧", "✧", "  ∀"),
            ("◕", "◕  !", "  ᴗ"),
            ("o", "●", "  💡"),
        ],
    },
    "cool": {
        "emoji": "😎",
        "variants": [
            ("■", "■", "  ᴗ"),
            ("█", "█", "  ─"),
        ],
    },
    "smug": {
        "emoji": "😏",
        "variants": [
            ("¬", "¬", "  ᴗ"),
            ("•", "¬", "  ᴗ"),
            (">", "•", "  ᴗ"),
        ],
    },
    "crying": {
        "emoji": "😭",
        "variants": [
            ("T", "T", "  ︵"),
            ("╥", "╥", "  ︵"),
        ],
    },
    "scared": {
        "emoji": "😱",
        "variants": [
            ("O", "O", "  ︵"),
            ("o", "o", "  ▽"),
        ],
    },
    "weird": {
        "emoji": "🤪",
        "variants": [
            (">", "<", "  3"),
            ("●", "O", "  ~"),
        ],
    },
    "embarrassed": {
        "emoji": "😳",
        "variants": [
            ("●", "●", "  ᴖ"),
            (">", "<", "  ᴖ"),
        ],
    },
    "love": {
        "emoji": "🥰",
        "variants": [
            ("♥", "♥", "  ᴗ"),
            ("♥", "♥", "  ω"),
        ],
    },
    "evil": {
        "emoji": "😈",
        "variants": [
            (">", ">", "  ᴗ"),
            ("¬", "¬", "  ∇"),
        ],
    },
    "glitch": {
        "emoji": "🤖",
        "variants": [
            ("0", "1", "  X"),
            ("1", "0", "  #"),
            ("▓", "░", "  ▒"),
            ("Ø", "×", "  #"),
            ("§", "∆", "  ⚡"),
            ("0", "1", "  ▓"),
        ],
    },
}

DEFAULT = "neutral"


def variants(emotion: str) -> list[tuple[str, str, str]]:
    """The (left, right, mouth) variants for an emotion, never empty."""
    data = TEXT_FACES.get(emotion, TEXT_FACES[DEFAULT])
    return data["variants"]


def emoji(emotion: str) -> str:
    """The emoji that labels this emotion."""
    return TEXT_FACES.get(emotion, TEXT_FACES[DEFAULT])["emoji"]


def render_face(left_eye: str, right_eye: str, mouth: str) -> str:
    """Format eyes and mouth into the multi-line face."""
    return f"{left_eye}   {right_eye}\n{mouth}"


def face(emotion: str, index: int = 0) -> str:
    """The multi-line face for this emotion at variant ``index``."""
    eyes = variants(emotion)
    left, right, mouth = eyes[index % len(eyes)]
    return render_face(left, right, mouth)


def label(emotion: str) -> str:
    """One small line - emoji + name - for under the face."""
    return f"{emoji(emotion)} {emotion.replace('_', ' ')}"
