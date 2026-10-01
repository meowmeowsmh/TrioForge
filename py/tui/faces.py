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
    "no": {
        "emoji": "🥺",
        "variants": [
            ("╥", "╥", "  ﹏"),
            ("T", "T", "  ﹏"),
            (">", "<", "  ︵"),
        ],
    },
    "dead": {
        "emoji": "💀",
        "variants": [
            ("×", "×", "  ─"),
            ("✕", "✕", "  ﹏"),
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


# Face geometry. The mouth strings below were authored against a 3-space eye gap
# and 2 leading spaces, so widening the gap has to shift EVERY mouth by the same
# amount - otherwise the mouth drifts off the centre line between the eyes.
# (The music variants indent by 3 because "🎧●" is two columns; a constant shift
# preserves that too.) One place to change, all variants stay aligned.
_EYE_GAP = 7
_MOUTH_PAD = (_EYE_GAP - 3) // 2


def render_face(left_eye: str, right_eye: str, mouth: str) -> str:
    """Format eyes and mouth into the multi-line face."""
    return (f"{left_eye}{' ' * _EYE_GAP}{right_eye}\n"
            f"{' ' * _MOUTH_PAD}{mouth}")


def face(emotion: str, index: int = 0) -> str:
    """The multi-line face for this emotion at variant ``index``."""
    eyes = variants(emotion)
    left, right, mouth = eyes[index % len(eyes)]
    return render_face(left, right, mouth)


def label(emotion: str) -> str:
    """One small line - emoji + name - for under the face."""
    return f"{emoji(emotion)} {emotion.replace('_', ' ')}"


# ---------------------------------------------------------------------------
# Idle "text face" actions: the bot acts out a hobby while waiting. Each action
# is three frames; the frame advances ~1.2 s and the action changes every ~3 min.
# ---------------------------------------------------------------------------
IDLE_ACTIONS = [
    ("coffee", "☕", [
        ("●", "●", "  ᴗ  ☕"),
        ("◕", "◕", " 口 ☕"),
        ("u", "u", "  ᴗ ~☕"),
    ]),
    ("reading", "📖", [
        ("•", "•", "  ᴗ 📖"),
        ("•", "•", "  ─ 📖"),
        ("◕", "◕", "  o 📖"),
    ]),
    ("gaming", "🎮", [
        ("●", "●", "  ─ 🎮"),
        (">", "<", " 口 🎮"),
        ("^", "^", "  ᴗ 🎮"),
    ]),
    ("music", "🎧", [
        ("🎧●", "●", "   ᴗ ♪"),
        ("🎧>", "<", "   ω ♫"),
        ("🎧^", "^", "   ᴗ ♪"),
    ]),
    ("coding", "💻", [
        ("●", "●", "  ─ 💻"),
        ("•", "•", "  ω ⌨"),
        ("◕", "◕", "  ! 💻"),
    ]),
    ("snacks", "🍿", [
        ("●", "●", "  o 🍿"),
        ("●", "●", " 口 🍿"),
        ("u", "u", "  ω 🍿"),
    ]),
    ("painting", "🎨", [
        ("•", "•", "  ᴗ 🖌"),
        ("◕", "◕", "  ─ 🎨"),
        ("^", "^", "  ω 🎨"),
    ]),
    ("workout", "🏋", [
        ("●", "●", "  ─ 🏋"),
        (">", "<", " 口 🏋"),
        ("^", "^", "  ᴗ 🏋"),
    ]),
    ("gardening", "🪴", [
        ("●", "●", "  ᴗ 🪴"),
        ("◕", "◕", "  o 💧"),
        ("^", "^", "  🌸 🪴"),
    ]),
    ("guitar", "🎸", [
        ("●", "●", "  ᴗ 🎸"),
        (">", "<", " 口 🎶"),
        ("^", "^", "  ω 🎸"),
    ]),
    ("photo", "📷", [
        ("●", "●", "  ᴗ 📷"),
        (">", "●", "  ─ 📸"),
        ("◕", "◕", "  O ✨"),
    ]),
    ("fishing", "🎣", [
        ("●", "●", "  ─ 🎣"),
        ("O", "O", " 口 🎣"),
        ("^", "^", "  ω 🐟"),
    ]),
    ("soup", "🍲", [
        ("●", "●", "  ᴗ 🥄"),
        ("◕", "◕", "  o 💨"),
        ("u", "u", "  ω 😋"),
    ]),
    ("napping", "💤", [
        ("-", "-", "  ᴗ"),
        ("-", "-", "  ᴗ z"),
        ("-", "-", "  ᴗ Z"),
    ]),
    ("blinkwave", "👋", [
        ("●", "●", "  ᴗ"),
        ("-", "-", "  ᴗ"),
        ("●", "●", "  ᴗ 🖐"),
    ]),
]


def idle_frame(index: int, frame: int = 0) -> str:
    """The ``frame`` (0–2) of idle action ``index``, as a two-line face."""
    _name, _emoji, frames = IDLE_ACTIONS[index % len(IDLE_ACTIONS)]
    left, right, mouth = frames[frame % len(frames)]
    return render_face(left, right, mouth)


def idle_label(index: int) -> str:
    """``emoji name`` for an idle action."""
    _name, emoji_, _frames = IDLE_ACTIONS[index % len(IDLE_ACTIONS)]
    return f"{emoji_} {_name}"
