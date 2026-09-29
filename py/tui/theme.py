"""Colours, glyphs and wording for the terminal client.

The palette is Tokyo Night - the same one the web UI, the kitty config and the
oh-my-posh prompt use - so the terminal client does not look like a different
program from the rest of TrioForge.
"""

from rich.theme import Theme

# ---------------------------------------------------------------- palette
FG = "#c0caf5"
BG = "#1a1b26"
GREY = "#565f89"
BLUE = "#7aa2f7"
CYAN = "#7dcfff"
GREEN = "#9ece6a"
YELLOW = "#e0af68"
ORANGE = "#ff9e64"
RED = "#f7768e"
PURPLE = "#bb9af7"

# ---------------------------------------------------------------- rich theme
# Registered once with the Console so markup like [accent]...[/] works and so
# Markdown gets sane colours (the defaults are tuned for light backgrounds).
THEME = Theme(
    {
        "fg": FG,
        "dim": GREY,
        "accent": f"bold {BLUE}",
        "user": f"bold {CYAN}",
        "assistant": f"bold {GREEN}",
        "error": f"bold {RED}",
        "warn": YELLOW,
        "ok": f"bold {GREEN}",
        "cmd": f"bold {PURPLE}",
        "num": ORANGE,
        # Markdown element styles
        "markdown.h1": f"bold {PURPLE}",
        "markdown.h2": f"bold {BLUE}",
        "markdown.h3": f"bold {CYAN}",
        "markdown.code": CYAN,
        "markdown.code_block": f"{FG} on #16161e",
        "markdown.block_quote": GREY,
        "markdown.link": f"underline {BLUE}",
        "markdown.item.bullet": f"bold {BLUE}",
        "markdown.hr": GREY,
    },
    inherit=True,
)

# ---------------------------------------------------------------- glyphs
# All of these are plain Unicode, NOT Nerd Font private-use codepoints, so the
# client looks right even in a terminal without the font installed.
GLYPH_PROMPT = "›"
GLYPH_USER = "❯"
GLYPH_BOT = "◆"
GLYPH_TOOL = "⚙"
GLYPH_OK = "✔"
GLYPH_ERR = "✖"
GLYPH_WARN = "!"
GLYPH_DOT = "·"

# Nerd Font icons for the header and status bar. Verified present in
# JetBrainsMono Nerd Font Mono with fontTools - if the font is missing they show
# as tofu, so anything load-bearing keeps a plain-Unicode fallback beside it.
ICON_BOLT = "\uf0e7"
ICON_FOLDER = "\uf07b"
ICON_CHIP = "\uf2db"
ICON_CLOUD = "\uf0c2"
ICON_CLOCK = "\uf017"
ICON_CHAT = "\uf086"

WHITE = "#e8ecf8"

TAG_USER = "you"
TAG_BOT = "trio"

# ---------------------------------------------------------------- banner
# A proper ASCII wordmark, drawn with a per-character gradient. Six lines is a
# lot, but this is the one moment the screen is empty - the same trade every
# neofetch-style rice makes. --no-banner skips it for scripts.
LOGO = [
    "████████╗██████╗ ██╗ ██████╗ ",
    "╚══██╔══╝██╔══██╗██║██╔═══██╗",
    "   ██║   ██████╔╝██║██║   ██║",
    "   ██║   ██╔══██╗██║██║   ██║",
    "   ██║   ██║  ██║██║╚██████╔╝",
    "   ╚═╝   ╚═╝  ╚═╝╚═╝ ╚═════╝ ",
]

# The gradient the wordmark is painted with, left to right.
GRADIENT = ["#bb9af7", "#9d7cd8", "#7aa2f7", "#5eb0f5", "#7dcfff", "#8de6e6"]


def gradient_stops(colour: str, n: int) -> list[str]:
    """Blend one colour towards a lighter version of itself, ``n`` steps.

    Used for the rule under the header and the status bar so the chrome has a
    direction to it instead of being one flat grey.
    """
    def hex_rgb(h: str) -> tuple[int, int, int]:
        h = h.lstrip("#")
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

    r, g, b = hex_rgb(colour)
    out = []
    for i in range(max(1, n)):
        t = i / max(1, n - 1)
        # fade towards a cool highlight so it reads as light falling across it
        out.append("#%02x%02x%02x" % (
            int(r + (141 - r) * t * 0.55),
            int(g + (230 - g) * t * 0.55),
            int(b + (230 - b) * t * 0.55),
        ))
    return out
