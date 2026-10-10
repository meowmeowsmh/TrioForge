"""The Crush-style full-screen interface.

Crush (github.com/charmbracelet/crush) is a Go TUI on Charm's bubbletea +
lipgloss. The Python equivalent of that stack is Textual, so this module uses it
to reproduce the same shape - which is TWO panes, not one:

    +-- chat (flexible) ---------------+-- sidebar (fixed) ---+
    |                                  |  ██████  TRIO        |
    |  +- you --------------------+    |  ██████████████      |
    |  +---------------------------+   |                      |
    |  +- trio · gemma · 2.1s -----+   |  New Session         |
    |  +---------------------------+   |  ~/TrioForge         |
    |                                  |  ● gemma-3-12b-it    |
    |                                  |  local · 0 turns     |
    |                                  |                      |
    |                                  |  Models              |
    |                                  |  ───────────         |
    |                                  |  gemma-3-12b-it      |
    |                                  |  6.8 GB · text+image |
    +----------------------------------+----------------------+
    | ▌ ask anything…                                          |
    +----------------------------------------------------------+
    | ctrl+c cancel · ctrl+q quit · tab chat · ctrl+p commands  |
    +----------------------------------------------------------+

Everything below the widget layer - backend.py, providers.py, session.py,
localmodels.py - is unchanged and UI-agnostic, which is why this could be added
without touching how a request is actually made.
"""

from __future__ import annotations

import os
import random
import subprocess
import threading
import time
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (Button, Collapsible, Input, Label, ListItem, ListView,
                             Markdown, Static, TextArea)

from . import agent as agent_mod
from . import faces, localmodels, providers, team, theme as T
from .backend import (EchoBackend, OpenAICompatBackend, auto_model,
                      fetch_models)
from .commands import COMMANDS  # built once so the first ctrl+p is instant
from .session import Message as SessionMessage, Session

DEFAULT_SYSTEM = (
    "You are TrioForge, a precise, practical assistant running in the user's "
    "terminal. Answer in Markdown but keep it terminal-friendly: prefer short "
    "paragraphs, bulleted lists and fenced code blocks over wide Markdown tables "
    "(they rarely align in a monospace terminal). Be concise unless asked to "
    "expand; show code in fenced blocks with the language tag."
)

# Ordered by consequence, not by feature list: the bar truncates on a narrow
# terminal, so the keys that stop things come before the ones that start them.
KEYBINDS = (" enter send  ·  ctrl+j newline  ·  ctrl+c cancel  ·  ctrl+q quit  ·  ctrl+y copy  ·  "
            "ctrl+a auto-route  ·  tab chat  ·  ctrl+p commands  ·  ctrl+l model  ·  ctrl+n new")

# How long the pleading "Don't!!!" face stays on screen before the app exits.
# Long enough to actually read: the plea is the point, so nothing except a
# deliberate ctrl+q / ctrl+c cuts it short.
GOODBYE_HOLD = 2.5

# Pressing Enter on any of these commands pleads before the app exits. Typing
# them on their own does nothing - only the submit does.
QUIT_ALIASES = ("/q", "/quit", "/exit")

# Crush's "working" spinner (internal/ui/anim + chat/assistant.go): an animated
# frame, a label with cycling ellipsis, and a live elapsed timer as the suffix.
SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

# Extra ctrl+p palette entries that are edits, not slash commands. Keys here are
# dispatched by _palette; they never reach _command().
PALETTE_ACTIONS = [
    ("set key", "set/change the API key for the current provider"),
    ("add model", "add a model id to the current provider"),
    ("remove model", "remove a model id from the current provider"),
]


# Commands that take an argument. Picked from ctrl+p, the argument is asked for
# instead of silently running the command with none. "mode: local" lists the
# .gguf files on disk; everything else is a one-line text prompt.
ARG_PROMPTS: dict[str, dict] = {
    "/keys":     {"prompt": "provider [value] - blank lists every key"},
    "/base-url": {"prompt": "endpoint URL", "prefill": "base_url"},
    "/system":   {"prompt": "new system prompt", "prefill": "system"},
    "/save":     {"prompt": "path to write the transcript"},
    "/start":    {"mode": "local"},
}

CSS = f"""
Screen {{ background: {T.BG}; color: {T.FG}; }}

#main {{ height: 1fr; }}

#chat {{
    width: 1fr;
    padding: 1 2;
    background: {T.BG};
    /* A VISIBLE track. With scrollbar-background set to the chat's own colour
       the track was invisible, so the whole scrollbar was one lone purple block
       that slid up and down as you scrolled - it read as a glitchy broken line
       rather than a scrollbar. Track + thumb together stay continuous. */
    scrollbar-color: {T.PURPLE};
    scrollbar-background: #1f2335;
    scrollbar-color-hover: #cba6f7;
    scrollbar-background-hover: #1f2335;
    scrollbar-size-vertical: 1;
    /* Reserve the scrollbar column ALWAYS. Without this the content width
       flips 60 -> 59 the moment the transcript overflows, so every card
       reflows by a column and the whole right edge - card borders and the
       scrollbar itself - jumps. That jump is the "line is not consistent". */
    scrollbar-gutter: stable;
}}

#side {{
    width: 36;
    padding: 0 1;
    background: #16161e;
    border-left: solid #2f3549;
    color: {T.GREY};
}}

/* 1fr, not the default auto. The panels are taller than the pane on a short
   terminal, and with auto height #sidebody grew past the bottom of #side and
   pushed the mood face and the activity spinner out of the viewport with it -
   the face would simply not be drawn. Taking the leftover space keeps those two
   rows alive; the panels clip instead, which is the cheaper thing to lose. */
#sidebody {{ height: 1fr; }}

/* width: 1fr (not auto) so text WRAPS inside the pane instead of
   growing past it and getting clipped. */
.msg {{ margin: 1 0 0 0; padding: 0 1; width: 1fr; }}
.from-user {{ border: round {T.CYAN}; color: {T.WHITE}; }}
.from-bot  {{ border: round {T.GREEN}; }}
/* Meta lines (routing, status, team steps) are commentary, not a model's
   answer - no green border, just dim grey. The old default put a green rounded
   border on every one of them and stacked them into a noisy column. */
.from-meta {{ color: {T.GREY}; border: none; margin: 0; }}
.role {{ color: {T.GREY}; }}
.thinking {{ color: {T.GREY}; }}

/* Textual's default title paints the block cursor on :focus, so ONE click on
   "Thinking… (311 chars)" left a solid #0178D4 block there permanently - the
   click focuses the title and nothing ever clears it. The title must stay
   focusable (enter toggles it from the keyboard), so focus is shown with colour
   and weight instead of a filled bar, and neither state paints a background. */
CollapsibleTitle {{
    color: {T.GREY};
    text-style: none;
    &:hover {{ color: {T.WHITE}; background: transparent; }}
    &:focus {{ color: {T.CYAN}; text-style: bold; background: transparent; }}
}}

/* The input keeps the SAME border when unfocused: a focus-coloured border made
   the line appear to break whenever focus moved with tab. Focus is shown by the
   block cursor instead, which does not disturb the frame. */
#prompt {{
    margin: 0 1;
    border: round {T.PURPLE};
    background: #16161e;
    color: {T.WHITE};
    padding: 0 1;
    height: auto;
    min-height: 3;
    max-height: 10;
    scrollbar-size-vertical: 1;
    scrollbar-gutter: stable;   /* same reason as #chat: no text jump */
}}
#prompt:focus {{
    border: round {T.PURPLE};
    background: #1a1b26;
}}
/* the multi-line editor's own chrome, toned down to match the old single line */
#prompt .text-area--cursor-line {{ background: #1a1b26; }}
#prompt .text-area--selection {{ background: {T.PURPLE} 40%; }}

.from-tool {{
    border: round #e0af68;
    color: {T.GREY};
}}
.from-tool Markdown {{
    background: {T.BG};
}}

#pickerbox {{
    align: center middle;
    background: #16161e;
    border: round {T.PURPLE};
    padding: 0 1;
    width: 72;
    height: 22;
}}
#pickerhead {{ height: 2; }}
#pickerfilter {{
    border: round {T.BLUE};
    background: {T.BG};
    height: 3;
}}
#pickerlist {{
    height: 1fr;
    background: #16161e;
    scrollbar-size-vertical: 1;
}}
#pickerhint {{ height: 1; margin: 1 0 0 0; }}
#pickerlist > ListItem {{ padding: 0 1; }}
#pickerlist > ListItem.--highlight {{ background: {T.PURPLE}; color: #16161e; }}

#activity {{ height: 1; color: {T.YELLOW}; }}
#statusline {{ height: 1; color: {T.GREY}; padding: 0 2; }}

/* width: 1fr is required for the centring to do anything: a Static defaults to
   its content width (~6 columns for the face), so text-align had nothing to
   centre within and the face sat against the left edge of the 34-column pane. */
#face {{ width: 1fr; text-align: center; }}

#confirmbox {{
    align: center middle;
    background: #16161e;
    border: round {T.YELLOW};
    padding: 1 2;
    width: 64;
    height: auto;
}}
#confirmbuttons {{ height: auto; margin: 1 0 0 0; }}
#confirmbuttons Button {{ margin: 0 2 0 0; min-width: 12; height: 3; }}
#routerbox {{
    align: center middle;
    background: #16161e;
    border: round {T.YELLOW};
    padding: 1 2;
    width: 72;
    height: 80%;
}}
#routerhead {{ height: 2; }}
#routerstatus {{ height: 1; margin: 0 0 1 0; }}
#routerlist {{ height: 1fr; border: round #2f3549; padding: 0 1; }}
#routerlist > ListItem {{ padding: 0 1; }}
#routerlist > ListItem.--highlight {{ background: {T.PURPLE}; color: #16161e; }}
#routerhint {{ height: 1; margin: 1 0 0 0; }}

#inputbox {{
    align: center middle;
    background: #16161e;
    border: round {T.PURPLE};
    padding: 1 2;
    width: 64;
    height: auto;
}}
#inputfield {{
    border: round {T.BLUE};
    background: {T.BG};
    height: 3;
    margin: 1 0;
}}

#keys {{
    height: 1;
    padding: 0 2;
    background: {T.BG};
    color: #414868;
}}

/* auto, NOT the old fixed 4. That 4 was exactly prompt(3) + keys(1); adding the
   status line made the content 5 rows, so #keys was laid out one row BELOW the
   viewport and the whole keybind bar silently disappeared. Let it size to its
   children instead of hard-coding a number that has to be kept in step. */
#footer {{
    dock: bottom;
    height: auto;
    background: {T.BG};
}}
"""


_TOOL_ICON = {
    "ls": "📂",
    "view": "📄",
    "write": "✏️",
    "edit": "🔧",
    "bash": "⏣",
    "grep": "🔎",
    "glob": "🧭",
    "todos": "☑",
}


def _summarise(name: str, args: dict) -> str:
    """One short line describing a tool call, shown on its transcript card."""
    from . import tools as TL
    tool = TL.TOOLS.get(name)
    if tool is not None:
        try:
            summary = tool.summary(args or {})
            if summary:
                return summary
        except Exception:  # noqa: BLE001 - a bad summary must not break the UI
            pass
    if args:
        return f"{name} {str(args)[:80]}"
    return name


def _short_gpu(name: str) -> str:
    """A GPU name that survives the 36-column sidebar."""
    for junk in ("NVIDIA ", "GeForce ", "Laptop ", "AMD ", "Intel "):
        name = name.replace(junk, "")
    name = name.split("(")[0].strip()       # drop the driver codename
    return name[:34] or "GPU"


def _gpu_inuse_note(d: dict) -> str:
    """The live "used/total + util%" line for the primary (in-use) GPU.

    Reads NVML in-process so the figure moves on every sidebar redraw, instead of
    the static "free" number that looked frozen while the card worked. Falls back
    to the ``specs()`` figure (used derived from free) when NVML does not answer.
    """
    live = None
    try:
        import hardware
        live = hardware.gpu_live()
    except Exception:  # noqa: BLE001 - a live read is a nicety, never fatal
        live = None
    if live and live.get("total"):
        used = live["used"] / (1024 ** 3)
        total = live["total"] / (1024 ** 3)
        return f"{used:.1f}/{total:.1f}GB {live['util']}%"
    used = max(0.0, d.get("total_gb", 0) - d.get("free_gb", 0))
    return f"{used:.1f}/{d['total_gb']:.1f}GB"


class PromptArea(TextArea):
    """Multi-line prompt: Enter sends the message, ctrl+j inserts a newline.

    Shift+Enter does NOT work, and cannot: a terminal sends the same byte (\\r)
    for Enter and Shift+Enter, and this Textual version has no kitty-keyboard
    protocol support to tell them apart. ctrl+j sends \\n, which every terminal
    delivers as a distinct key - so that is the newline key.
    """

    class Submitted(Message):
        """Posted when Enter is pressed - not when a newline is inserted."""

        def __init__(self, area: "PromptArea", value: str) -> None:
            self.area = area
            self.value = value
            super().__init__()

        @property
        def control(self) -> "PromptArea":
            # what @on(..., "#prompt") matches against
            return self.area

    class CopyRequested(Message):
        """Posted on ctrl+y - the app owns the clipboard, not this widget."""

        def __init__(self, area: "PromptArea") -> None:
            self.area = area
            super().__init__()

        @property
        def control(self) -> "PromptArea":
            return self.area

    class RouteRequested(Message):
        """Posted on ctrl+a - the app owns the local/cloud routing."""

        def __init__(self, area: "PromptArea") -> None:
            self.area = area
            super().__init__()

        @property
        def control(self) -> "PromptArea":
            return self.area

    class ScrollRequested(Message):
        """Posted on PageUp/PageDown - scroll the TRANSCRIPT, not the cursor."""

        def __init__(self, area: "PromptArea", direction: int) -> None:
            self.area = area
            self.direction = direction
            super().__init__()

        @property
        def control(self) -> "PromptArea":
            return self.area

    async def _on_key(self, event) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self, self.text))
            return
        # TextArea binds ctrl+y to "redo", so as the focused widget it eats the
        # key and the app-level binding never fires. Copy the answer from here.
        if event.key == "ctrl+y":
            event.stop()
            event.prevent_default()
            self.post_message(self.CopyRequested(self))
            return
        # TextArea binds ctrl+a to "cursor to start of line", so the focused
        # prompt eats it before the app's binding. Route the message from here.
        if event.key == "ctrl+a":
            event.stop()
            event.prevent_default()
            self.post_message(self.RouteRequested(self))
            return
        # TextArea binds PageUp/PageDown to move the CURSOR a page - useless in a
        # three-line prompt box, and it meant the transcript could not be
        # scrolled at all while typing. Send them to the chat instead.
        if event.key in ("pageup", "pagedown"):
            event.stop()
            event.prevent_default()
            self.post_message(self.ScrollRequested(
                self, -1 if event.key == "pageup" else 1))
            return
        # ctrl+j is the reliable newline. The other two are best-effort: they
        # only ever arrive if a terminal opts into an extended keyboard mode.
        if event.key in ("ctrl+j", "shift+enter", "alt+enter"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        await super()._on_key(event)


class ForgeApp(App):
    """Full-screen TrioForge client, laid out like Crush."""

    CSS = CSS
    TITLE = "TrioForge"
    # Textual ships its own ctrl+p command palette; ours is the searchable list
    # of slash commands, so disable the built-in to stop ctrl+p opening both.
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        # Textual's Screen binds ctrl+c to copy_text, so with text selected
        # ctrl+c copies; without a selection that action raises SkipAction and
        # falls through to here. So: copy if you selected something, otherwise
        # STOP the turn - ctrl+q is the way out, because in every terminal
        # ctrl+c means "interrupt", not "exit".
        ("ctrl+c", "cancel_turn", "Cancel the running turn"),
        ("ctrl+y", "copy_reply", "Copy answer"),
        ("ctrl+a", "auto_route", "Auto-route"),
        ("pageup", "scroll_chat(-1)", "Scroll up"),
        ("pagedown", "scroll_chat(1)", "Scroll down"),
        ("ctrl+l", "pick_model", "Model"),
        ("ctrl+n", "new_session", "New"),
        ("ctrl+p", "palette", "Commands"),
        ("ctrl+o", "pick_provider", "Provider"),
        ("tab", "focus_chat", "Chat"),
        ("escape", "focus_prompt", "Prompt"),
    ]

    def __init__(self, args, cfg, backend, session: Session):
        super().__init__()
        self.args = args
        self.cfg = cfg
        self.backend = backend
        self.real_backend = backend
        self.session = session
        self._busy = False
        self._started = time.time()
        self._activity = ""       # shown in the sidebar while a turn runs
        self._replies: list[str] = []   # finished answers, newest last (for /copy)
        self._auto_route = bool(getattr(self.cfg, "route_enabled", False))
        self._route_pool = list(getattr(self.cfg, "route_pool", []) or [])
        self._team_mode = bool(getattr(self.cfg, "team_enabled", False))
        # Set by ctrl+c; polled by the agent between streamed chunks so a long
        # local generation can be abandoned. threading.Event, not a bool: the
        # agent reads it from a worker thread.
        self._stop = threading.Event()
        # The live routing/server line, and the routing note for the current send.
        self._status_text = ""
        self._route_note = ""
        # Set when llama.cpp starts, read by _warn_if_slow_load().
        self._load_plan = None
        self._warned_load = None
        self._tool_cards: dict = {}
        self._active = None
        self._spinner_i = 0
        self._git_cache: list[str] = []
        self._git_ts = 0.0
        self._models_cache: list = []
        self._models_ts = 0.0
        # provider -> (fetched_at, [models]); Ollama's list is only knowable by
        # asking it, so it is cached rather than read from the config.
        self._providers_cache: dict = {}
        self._sidebar_ts = 0.0
        # The mood face under Providers: emotion, animation frame, and when a
        # terminal mood (happy/sad) was set so the tick can fade it to neutral.
        self._mood = "neutral"
        self._face_i = 0
        self._face_done_at = 0.0
        self._last_face = ""
        # Idle act: which hobby the bot is acting out, and when this one started
        # so the tick can advance frames every ~1.2 s and switch acts every ~3 min.
        self._idle_action = random.randrange(len(faces.IDLE_ACTIONS))
        self._idle_started = time.time()
        # Once set, the tick plays the pleading "Don't!!!" face until we exit.
        self._goodbye = False
        self._goodbye_at = 0.0

    # ------------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            with VerticalScroll(id="chat"):
                yield Static(" ", id="spacer")
            with Vertical(id="side"):
                yield Static(self._sidebar(), id="sidebody")
                yield Static(faces.idle_frame(self._idle_action) + "\n"
                             + faces.idle_label(self._idle_action), id="face")
                yield Static("", id="activity")
        # The prompt and the keybind bar go in ONE docked container. Docking both
        # separately let the keybind bar overlap the prompt's bottom border,
        # which is why the purple line looked cut off.
        with Vertical(id="footer"):
            # One live line for routing + server state. These used to be two
            # PERMANENT cards per message in the transcript, so a 20-message
            # session carried 40 lines of chrome you scrolled past forever.
            yield Static("", id="statusline")
            yield PromptArea(placeholder=self._hint(), id="prompt")
            yield Static(KEYBINDS, id="keys")

    def on_mount(self) -> None:
        self.query_one("#prompt", PromptArea).focus()
        self._greet()
        # Live spinner + elapsed timer while a turn runs. Cheap no-op when idle.
        self.set_interval(0.25, self._tick_clock)

    # ----------------------------------------------------------------- sidebar
    def _sidebar(self) -> Text:
        """The right-hand panel: wordmark, session, models, providers, git."""
        out = Text()

        # ---- wordmark with the same left-to-right gradient as the banner
        cols = max(len(l) for l in T.LOGO) or 1
        for line in T.LOGO:
            for ci, ch in enumerate(line):
                if ch == " ":
                    out.append(" ")
                    continue
                idx = min(len(T.GRADIENT) - 1,
                          int(ci / max(1, cols - 1) * (len(T.GRADIENT) - 1)))
                out.append(ch, style=f"bold {T.GRADIENT[idx]}")
            out.append("\n")
        out.append("\n")

        def section(title: str) -> None:
            out.append(f"{title}\n", style=f"bold {T.FG}")
            out.append("─" * max(4, len(title) + 6) + "\n", style="#2f3549")

        try:
            rel = Path.cwd().relative_to(Path.home())
            cwd = "~" if str(rel) == "." else f"~/{rel}"
        except ValueError:
            cwd = str(Path.cwd())

        # ---- session
        out.append("New Session\n", style=f"bold {T.PURPLE}")
        out.append(f"{cwd}\n", style=T.GREY)
        out.append("● ", style=T.GREEN)
        out.append(f"{self._short_model()}\n", style=f"bold {T.FG}")
        kind = "local" if self._is_local() else "cloud"
        out.append(f"{kind}  ·  {self.session.turns} turns  ·  "
                   f"{self._elapsed()}\n", style=T.GREY)
        if self._team_mode:
            out.append("TEAM MODE ON\n", style=f"bold {T.GREEN}")
        if self._auto_route:
            out.append("AUTO-ROUTE ON\n", style=f"bold {T.YELLOW}")
            # say where the next message goes, so "is it the right way?" is
            # answerable at a glance rather than only after the fact
            local, cloud = self._pool_models()
            if cloud:
                out.append(f"  ☁ complex → {cloud[2]}\n", style=T.GREY)
            if local:
                out.append(f"  💻 else → {local}\n", style=T.GREY)
            elif self._is_local():
                out.append(f"  💻 else → {self._short_model()}\n", style=T.GREY)
        out.append("\n")

        # ---- the machine: the numbers /specs and the fit verdicts come from
        spec = self._specs()
        if spec:
            section("Machine")
            # Used/total with a percentage, like a task manager, and "available"
            # spelled out. The raw "free" figure disagrees with every other memory
            # readout on the machine - the page cache counts as used there.
            out.append(f"{spec.get('ram_used_gb', 0):.1f} / "
                       f"{spec.get('ram_total_gb', 0):.1f} GB RAM "
                       f"{spec.get('ram_percent', 0):.0f}%\n", style=T.GREY)
            out.append(f"{spec.get('ram_available_gb', 0):.1f} GB available\n",
                       style=T.GREY)
            devices = spec.get("gpu_devices") or []
            if devices:
                # Every GPU, not just the one that won: with two cards the
                # questions are which is in use, and whether the other can be
                # used at all. "1 of 2" raised that question and never answered
                # it. A card llama.cpp lists is usable; an integrated one is
                # usable but adds no memory, because that memory is the system
                # RAM already reported a line above.
                pool = spec.get("gpu_usable_count", 0)
                head = "1 GPU" if len(devices) == 1 else f"{len(devices)} GPUs"
                if pool >= 2:
                    head += f" · {pool} poolable"
                out.append(head + "\n", style=T.GREY)
                for d in devices:
                    if d.get("in_use"):
                        mark, style = "-> ", T.GREEN
                        note = _gpu_inuse_note(d)
                    elif d.get("adds_memory"):
                        mark, style = "   ", T.FG
                        note = f"{d['free_gb']:.1f}/{d['total_gb']:.1f}GB idle"
                    else:
                        mark, style = "   ", T.GREY
                        # An integrated GPU's "memory" is system RAM that is
                        # ALREADY counted in the RAM line above - llama.cpp even
                        # reports the whole shared GTT (7-8 GB) as its "total",
                        # which would double-count it as VRAM. So name what it
                        # does (shares RAM) instead of printing a misleading size.
                        # `--specs` keeps the full wording and the numbers.
                        note = "shares RAM"
                    out.append(f"{mark}{_short_gpu(d['name'])[:14]:<14} ", style=style)
                    out.append(f"{note}\n", style=T.GREY)
                out.append("\n")
            else:
                out.append("no GPU — CPU only\n", style=T.YELLOW)
            out.append("\n")

        # ---- offline models on disk
        models = self._models()
        if models:
            section("Models")
            for m in models[:4]:
                active = m.name in (self.session.model or "")
                out.append("● " if active else "○ ",
                           style=T.GREEN if active else "#414868")
                out.append(f"{m.name}\n", style=T.FG)
                out.append(f"  {m.size_gb:.1f} GB · {m.caps_label}\n", style=T.GREY)
            out.append("\n")

        # ---- providers and whether each has a key
        section("Providers")
        for name in sorted(self.cfg.providers)[:6]:
            raw = self.cfg.providers[name].get("api_key", "")
            # Resolve ${VAR}: a placeholder whose env var is unset is NOT a key,
            # so the sidebar must say "env unset" rather than lying "key".
            has = bool(providers.resolve(raw))
            current = name == self.cfg.provider
            out.append("● " if has else "○ ",
                       style=T.GREEN if has else (T.YELLOW if current else "#414868"))
            out.append(f"{name}", style=f"bold {T.FG}" if current else T.GREY)
            label = "key" if has else ("env unset" if raw else "—")
            out.append(f"  {label}\n", style="#414868")
        out.append("\n")

        # ---- git: the files an assistant would actually be asked about
        changed = self._git_status()
        if changed:
            section("Modified")
            for line in changed[:5]:
                out.append(f"{line}\n", style=T.YELLOW)
        return out

    def _git_status(self) -> list[str]:
        # Cache for a few seconds: the sidebar now re-renders on every tick while
        # a turn runs, and we must not fork `git status` at 4 Hz.
        now = time.time()
        if now - self._git_ts < 5.0:
            return self._git_cache
        try:
            r = subprocess.run(["git", "status", "--short"], cwd=Path.cwd(),
                               capture_output=True, text=True, timeout=3)
            self._git_cache = [l for l in r.stdout.splitlines() if l.strip()][:5]
        except Exception:  # noqa: BLE001 - git absent, or not a repo
            self._git_cache = []
        self._git_ts = now
        return self._git_cache

    def _models(self) -> list:
        # Cache briefly. The sidebar rebuilds on state changes and used to call
        # localmodels.available() at 4 Hz while a turn ran - a call that can
        # round-trip to the local llama.cpp server. On Windows the slow console
        # redraw makes that visible as a stutter; caching removes it.
        now = time.time()
        if now - self._models_ts < 10.0:
            return self._models_cache
        self._models_cache = localmodels.available()
        self._models_ts = now
        return self._models_cache

    def _elapsed(self) -> str:
        secs = int(time.time() - self._started)
        return f"{secs // 60}m{secs % 60:02d}s" if secs >= 60 else f"{secs}s"

    def _fmt_elapsed(self, secs: float) -> str:
        """Crush-style turn timer: 12s · 1m 5s · 1h 2m."""
        s = int(secs)
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m {s % 60}s"
        return f"{s // 3600}h {(s % 3600) // 60}m"

    def _refresh_activity(self) -> None:
        """Redraw the sidebar working line: spinner + label + live timer."""
        state = self._active
        if not self._busy or not state:
            return
        self._spinner_i = (self._spinner_i + 1) % len(SPINNER)
        frame = SPINNER[self._spinner_i]
        elapsed = time.time() - state["started"]
        label = "thinking" if (state["reasoning"] and not state["text"]) else "working"
        dots = "." * (int(elapsed / 0.4) % 4)
        self._activity = (f"{frame} {label}{dots}  {self._fmt_elapsed(elapsed)}"
                          f"  ·  ~{state.get('tokens', 0)} tok")
        # Only the activity line changes on this tick. Rebuilding the whole
        # sidebar here re-enumerated models at 4 Hz while a turn ran - visible as
        # a stutter on Windows. Update just the tiny activity widget instead.
        self.query_one("#activity", Static).update(self._activity)

    def _tick_clock(self) -> None:
        """Interval callback: keep the working timer moving between tokens."""
        self._refresh_activity()
        self._tick_face()
        now = time.time()
        # While a turn is running, keep the local llama.cpp warm. The idle watchdog
        # unloads it after ~5 minutes of no model request, but a long turn never
        # asks the local model while a cloud peer is reasoning, or while a bash
        # tool is running - so the watchdog unloaded the local model mid-turn and
        # the next request died with "WinError 10061 ... actively refused". Marking
        # it in use here (every tick while busy) means the unload can only ever
        # happen between turns, never inside one.
        if self._busy:
            try:
                import llamacpp_service as svc
                svc.touch()
            except Exception:
                pass
        # The plea's deadline is enforced here as well as by its one-shot timer.
        # This tick is the one callback the app is known to keep running, so the
        # exit cannot be lost if a timer is missed - being unable to quit is a far
        # worse failure than a face that lingers a beat too long.
        if self._goodbye and now - self._goodbye_at >= GOODBYE_HOLD:
            self.exit()
            return
        # Also refresh the sidebar on a slow cadence so the RAM / model figures
        # stay live. The expensive parts (models, git, GPU) are cached, so a 2 s
        # refresh is cheap - unlike the 4 Hz full rebuild this replaced.
        if now - self._sidebar_ts >= 2.0:
            self._sidebar_ts = now
            self.query_one("#sidebody", Static).update(self._sidebar_fitted())

    # ------------------------------------------------------------------ helpers
    def _short_model(self) -> str:
        m = self.session.model or getattr(self.backend, "name", "?")
        if m.endswith(".gguf"):
            m = m.rsplit("/", 1)[-1][:-5]
        return m or "no model"

    def _is_local(self) -> bool:
        url = self.cfg.base_url
        return "127.0.0.1" in url or "localhost" in url

    def _ensure_local_server(self) -> str | None:
        """Start llama-server when the local provider is down. Blocks; call in a thread.

        A "local" provider just POSTs to 127.0.0.1:8080. If nothing was ever
        started there, the very first message dies with "Connection refused" and
        the user is left to discover /start. Instead, start the model, wait for
        /health, and only let the request through when it is ready. Returns an
        error string on failure, None when the server is up.
        """
        import llamacpp_service as svc
        from urllib.parse import urlparse

        url = urlparse(self.cfg.base_url or "")
        host = url.hostname or "127.0.0.1"
        port = url.port or 8080

        model = (self.session.model or "").strip() or getattr(self.cfg, "model", "")
        if not model:
            if svc.server_ready(host, port, timeout=2):
                return None                  # something is up - better than nothing
            return ("no local model is selected — press ctrl+l to pick one, "
                    "or run /start <model>")

        # The picker and the route pool hold SHORT names ("qwen2.5-7b-instruct"),
        # which are not paths and never match what /v1/models reports
        # ("qwen2.5-7b-instruct-q4_k_m.gguf"). Resolve to a real .gguf first, or the
        # comparison below is always false and the server would restart every turn.
        from . import localmodels as lm

        found = lm.find(model)
        model_path = found.path if found else (svc.resolve_model(model) or model)

        # A local llama-server IGNORES the request's "model" field: it answers with
        # whatever GGUF is loaded. So "the port is up" is NOT "the model you picked
        # is loaded". Returning early on server_ready() alone is why /model looked
        # like it worked while the OLD model kept replying — 37 s a turn when that
        # one did not fit in VRAM. Reuse a server only when it already serves the
        # RIGHT model; otherwise hand it to start(), which swaps it out.
        if svc.serves_model(host, port, model_path):
            return None

        if not os.path.isfile(model_path):
            return "local model not found: {}".format(model)

        try:
            result = svc.start(model_path, ctx_size=self.cfg.ctx_size or None)
        except Exception as exc:            # noqa: BLE001
            return "could not start llama.cpp: {}".format(exc)
        if not result.get("running"):
            return result.get("error") or "llama.cpp failed to start"
        # Keep the load plan: a split load is roughly an order of magnitude slower
        # per token, and this was previously only ever written to the log file, so
        # the first symptom was replies taking minutes with no explanation.
        self._load_plan = result.get("plan")
        if svc.server_ready(host, port, timeout=600):
            return None
        return "llama.cpp did not become ready in time — see logs/llamacpp.log"

    def _warn_if_slow_load(self) -> None:
        """Say it out loud when most of the model went to the CPU.

        Only when at least a quarter of the weights did: the VRAM arithmetic
        carries a deliberate 1.5 GB headroom, so a model that only just fits is
        predicted to split when llama.cpp will in fact keep all of it on the card.
        Warning on that would be crying wolf; a 12B with half its weights in
        system RAM is not a false alarm.
        """
        plan = getattr(self, "_load_plan", None)
        if not plan or not plan.get("split"):
            return
        model = (self.session.model or getattr(self.cfg, "model", "")) or ""
        size = plan.get("gpu_bytes", 0) + plan.get("cpu_bytes", 0)
        cpu = plan.get("cpu_bytes", 0)
        if not size or cpu * 4 < size:
            return                      # under a quarter on the CPU: not the story
        if getattr(self, "_warned_load", None) == model:
            return                      # once per model, not once per message
        self._warned_load = model
        gb = 1073741824.0
        self._add_meta(
            "⚠ {:.1f} GB of this {:.1f} GB model has to run on the CPU "
            "({:.1f} GB fits in VRAM) — CPU layers are several times slower per "
            "token, so replies will drag.\n"
            "   Faster: a smaller quant (Q4_K_M), a smaller model, or a lower "
            "context serve the whole model from the GPU.".format(
                cpu / gb, size / gb, plan.get("gpu_bytes", 0) / gb))

    def _specs(self) -> dict:
        """Hardware specs for the sidebar - cached inside hardware.py, because
        the sidebar redraws four times a second while a turn runs."""
        try:
            import hardware
            return hardware.specs()
        except Exception:  # noqa: BLE001 - a machine we cannot read is not fatal
            return {}

    def _hint(self) -> str:
        import random
        return "▌ " + random.choice([
            "ask me anything about this repo…",
            "why is my model 6.8 GB and still slow?",
            "explain this repo to me like I just woke up",
            "find something in here that will break later",
            "write a haiku about my swap usage",
            "which of my models fits on a 8 GB card?",
        ])

    def _refresh(self) -> None:
        self.query_one("#sidebody", Static).update(self._sidebar_fitted())
        self.query_one("#activity", Static).update(self._activity)

    def _set_mood(self, mood: str) -> None:
        """Switch the face emotion. Terminal moods (happy/sad) are time-stamped
        so the tick fades them back to neutral ten seconds later."""
        if mood == self._mood:
            return
        self._mood = mood
        self._face_i = 0
        if mood in ("happy", "sad"):
            self._face_done_at = time.time()

    def _face_text(self) -> str:
        """The current face: the eyes/mouth, then a small emoji + name line."""
        return f"{faces.face(self._mood, self._face_i)}\n{faces.label(self._mood)}"

    def _face_now(self) -> str:
        """The face text for the current state. Draws nothing, advances nothing."""
        if self._goodbye:
            # The pleading eyes wobble a little slower than a busy spinner.
            step = int((time.time() - self._goodbye_at) / 0.45)
            return faces.face("no", step) + "\nDon't!!!"
        if self._mood == "neutral":
            frame = int((time.time() - self._idle_started) / 1.2) % 3
            return (faces.idle_frame(self._idle_action, frame) + "\n"
                    + faces.idle_label(self._idle_action))
        return self._face_text()

    def _paint_face(self) -> None:
        """Redraw the face now rather than on the next tick - the face must not
        lag 250 ms behind the keypress that changed it."""
        text = self._face_now()
        if text != self._last_face:
            self._last_face = text
            self.query_one("#face", Static).update(text)

    def _tick_face(self) -> None:
        """Advance the face and redraw it when it changed.

        While a turn runs the emotion cycles its variants. When idle the bot acts
        out a hobby (coffee, reading, gaming, ...): frames step every ~1.2 s and
        the act switches to a different one every ~3 minutes. Once the user asks
        to quit, the pleading "Don't!!!" face plays until the app exits.
        """
        now = time.time()

        if not self._goodbye:
            if self._mood in ("happy", "sad") and now - self._face_done_at >= 10.0:
                self._mood = "neutral"
                self._idle_started = now
            if self._mood == "neutral" and now - self._idle_started >= 180.0:
                nxt = random.randrange(len(faces.IDLE_ACTIONS))
                while nxt == self._idle_action and len(faces.IDLE_ACTIONS) > 1:
                    nxt = random.randrange(len(faces.IDLE_ACTIONS))
                self._idle_action = nxt
                self._idle_started = now
            if self._mood != "neutral":
                self._face_i += 1

        self._paint_face()

    def action_cancel_turn(self) -> None:
        """ctrl+c: stop the turn that is running. ctrl+q is the way out.

        ctrl+c means "interrupt" in every terminal, so binding it to quit meant
        the key you press to STOP something killed the whole session - and a
        local turn can run for minutes. It now abandons the turn at the next
        streamed chunk (the partial answer is kept); when nothing is running it
        just says so.
        """
        if not self._busy:
            self.notify("nothing is running — ctrl+q quits", timeout=3)
            return
        self._stop.set()
        self._activity = "⏹ stopping…"
        try:
            self.query_one("#activity", Static).update(self._activity)
        except Exception:  # noqa: BLE001 - the line is cosmetic
            pass

    def action_quit(self) -> None:
        """ctrl+q / ctrl+c: plead, then quit - or leave at once if already pleading."""
        if self._goodbye:
            self.exit()
            return
        self._begin_goodbye()

    def _begin_goodbye(self) -> None:
        """Play the plea once, then exit.

        Reached from Enter on a quit command (/q, /quit, /exit) and from the
        ctrl+q / ctrl+c bindings - never from typing alone, so the command can sit
        in the prompt untouched until it is submitted.

        Repeats are ignored, NOT treated as "leave now": the plea has to last long
        enough to be read. ctrl+q / ctrl+c is the deliberate way out if you do not
        want to wait.

        Deliberately NOT an async worker. ``@work(exclusive=True)`` joins the
        shared "default" group - the same one a running turn uses - so the plea
        could be cancelled before it ever drew, and a worker that dies before
        reaching ``self.exit()`` leaves the app unable to quit at all.
        """
        if self._goodbye:
            return
        self._goodbye = True
        self._goodbye_at = time.time()
        self._last_face = ""
        self._paint_face()
        self._add_plain(f"{faces.emoji('no')}  Don't!!!  ·  ctrl+q to leave now",
                        role="bot")
        self.set_timer(GOODBYE_HOLD, self.exit)

    def _greet(self) -> None:
        self.query_one("#chat", VerticalScroll).mount(Static(
            f"[{T.GREY}]Welcome. Type below, or press [/]"
            f"[bold {T.PURPLE}]ctrl+p[/][{T.GREY}] for commands "
            f"and [/][bold {T.PURPLE}]ctrl+l[/][{T.GREY}] to switch model.[/]",
            classes="role"))

    def _ensure_card(self, state: dict):
        """The turn's answer card, creating it when the turn has none yet.

        Team mode deliberately creates no card up front (the team log goes
        first), so an error path doing state["card"].update(...) dereferenced
        None and replaced the real message with "unexpected error".
        """
        card = state.get("card")
        if card is None:
            card = self._add("", "bot")
            state["card"] = card
        return card

    def _sidebar_fitted(self) -> Text:
        """The sidebar, trimmed to the pane with a marker when it overflows.

        #sidebody is height: 1fr so the face and the spinner stay on screen, which
        means a short terminal silently CLIPPED the panels - Models, Providers and
        Modified just vanished with nothing to say they had. Keep the top and say
        how much was cut. allow_blank=True is required: Text.split() drops blank
        lines by default, which would both miscount and delete the spacing.
        """
        text = self._sidebar()
        try:
            height = self.query_one("#sidebody", Static).size.height
        except Exception:  # noqa: BLE001 - not mounted yet, or no screen
            return text
        if height <= 1:
            return text
        parts = text.split("\n", allow_blank=True)
        if len(parts) <= height:
            return text
        keep = height - 1
        out = Text()
        for part in parts[:keep]:
            out.append_text(part)
            out.append("\n")
        out.append(f"… {len(parts) - keep} more line(s) hidden — "
                   f"make the window taller\n", style=T.GREY)
        return out

    def _set_status(self, text: str) -> None:
        """Write the live status line. It is OVERWRITTEN, never appended to."""
        self._status_text = text
        try:
            self.query_one("#statusline", Static).update(text)
        except Exception:  # noqa: BLE001 - the line is cosmetic, never fatal
            pass

    def _status_combined(self) -> str:
        """Why this message went where it did, plus whether that is usable."""
        bits = [b for b in (self._route_note, self._target_status()) if b]
        return "   ·   ".join(bits)

    def _stick_chat_bottom(self, force: bool = False) -> None:
        """Scroll the transcript to the bottom - but only when already there.

        During a turn (especially /team) cards mount rapidly; unconditional
        scroll_end made the view snap to the bottom on every one, so you could
        not scroll up to read the earlier steps. Only stick when the user is
        already near the bottom, unless force=True (their own send, the final
        answer).
        """
        chat = self.query_one("#chat", VerticalScroll)
        if force:
            chat.scroll_end(animate=False)
            return
        vs = getattr(chat, "virtual_size", None)
        max_y = max(0, vs.height - chat.size.height) if vs is not None else 0
        if chat.scroll_offset.y >= max_y - 2:
            chat.scroll_end(animate=False)

    def _add(self, text: str, role: str, *, force_scroll: bool = False) -> Markdown:
        chat = self.query_one("#chat", VerticalScroll)
        card = Markdown(text or "…", classes=f"msg from-{role}")
        chat.mount(card)
        self._stick_chat_bottom(force=force_scroll)
        return card

    def _add_meta(self, content: str) -> Static:
        """A dim, borderless commentary line (routing, status, team steps)."""
        chat = self.query_one("#chat", VerticalScroll)
        card = Static(content, classes="msg from-meta")
        chat.mount(card)
        self._stick_chat_bottom()
        return card

    def _add_plain(self, content, role: str = "bot") -> Static:
        """A transcript card for text that is already laid out - Markdown would
        reflow a panel or a table and destroy its alignment."""
        chat = self.query_one("#chat", VerticalScroll)
        card = Static(content, classes=f"msg from-{role}")
        chat.mount(card)
        self._stick_chat_bottom()
        return card

    # ------------------------------------------------------------------ events
    @on(PromptArea.CopyRequested, "#prompt")
    def _on_copy_requested(self, event: PromptArea.CopyRequested) -> None:
        event.stop()
        self.action_copy_reply()

    @on(PromptArea.RouteRequested, "#prompt")
    def _on_route_requested(self, event: PromptArea.RouteRequested) -> None:
        event.stop()
        self.action_auto_route()

    @on(PromptArea.ScrollRequested, "#prompt")
    def _on_scroll_requested(self, event: PromptArea.ScrollRequested) -> None:
        event.stop()
        self.action_scroll_chat(event.direction)

    def action_scroll_chat(self, direction) -> None:
        """Scroll the transcript by a screenful (PageUp/PageDown)."""
        chat = self.query_one("#chat", VerticalScroll)
        step = max(1, chat.size.height - 4)
        chat.scroll_relative(y=int(direction) * step, animate=False)

    # ---------------------------------------------------------------- routing
    def _cloud_target(self) -> tuple[str, str, str]:
        """The default cloud endpoint: the first keyed provider, or deepseek.

        Used only when the routing pool is empty - a chosen cloud model in the
        pool always wins over this default.
        """
        for name in ("deepseek", "claude", "groq", "gemini", "openrouter",
                     "huggingface"):
            entry = self.cfg.providers.get(name, {})
            if providers.resolve(entry.get("api_key", "")) and entry.get("base_url"):
                model = (entry.get("models") or [""])[0]
                return name, entry["base_url"], model
        for name, entry in self.cfg.providers.items():
            if name == "local":
                continue
            if entry.get("base_url"):
                model = (entry.get("models") or [""])[0]
                return name, entry["base_url"], model
        return "deepseek", "https://api.deepseek.com/v1", "deepseek-v4-pro"

    def _live_provider_models(self) -> dict:
        """{provider: [models it actually offers]} - cached, BLOCKING.

        Ollama's speed is entirely a function of which model is pulled (a 0.5B is
        instant, a 70B crawls), so the picker has to show what is installed on
        THIS machine, not a name written into a config file. An empty list means
        "could not ask", and the caller falls back to the config's names.

        Call it through ``asyncio.to_thread``: a provider that is not running
        would otherwise freeze the panel while it times out.
        """
        now = time.time()
        out = {}
        for name, entry in (self.cfg.providers or {}).items():
            if name == "local" or not entry.get("base_url"):
                continue
            cached = self._providers_cache.get(name)
            if cached and now - cached[0] < 120:
                out[name] = cached[1]
                continue
            live = fetch_models(entry["base_url"], entry.get("api_key", ""))
            self._providers_cache[name] = (now, live)
            out[name] = live
        return out

    def _route_candidates(self, live=None):
        """Every model auto-route may pick from, as (key, label) pairs.

        Offline: each .gguf in models/, plus whatever Ollama has pulled. Cloud:
        the models each provider ACTUALLY offers (``live``, fetched by the
        caller), falling back to the config's names when it cannot be asked.
        Keys are ``local:<name>`` or ``<provider>:<model>``.
        """
        from . import localmodels as lm
        live = live or {}
        offline = {"local", "ollama"}
        out = []
        for m in lm.available():
            out.append((f"local:{m.name}", f"💻 {m.name}  · {m.size_gb:.1f} GB"))
        for name in ("deepseek", "claude", "groq", "gemini", "openrouter",
                     "huggingface", "ollama"):
            entry = self.cfg.providers.get(name, {})
            if not entry.get("base_url"):
                continue
            has = "key" if providers.resolve(entry.get("api_key", "")) else "no key"
            kind = "💻" if name in offline else "☁"
            models = live.get(name) or (entry.get("models") or [])
            for m in models[:6]:
                out.append((f"{name}:{m}", f"{kind} {name}:{m}  · {has}"))
        return out

    def _pool_models(self):
        """Resolve the selected pool into (local_model, cloud provider,url,model)."""
        local_model, cloud = "", None
        for key in self._route_pool:
            if key.startswith("local:"):
                local_model = local_model or key.split(":", 1)[1]
            elif ":" in key and cloud is None:
                provider, _, model = key.partition(":")
                entry = self.cfg.providers.get(provider, {})
                if entry.get("base_url"):
                    cloud = (provider, entry["base_url"], model)
        return local_model, cloud

    def _sync_route_pool_local(self, model: str = "") -> None:
        """Point the route pool's local entry at ``model`` (default: the current one).

        The pool is a SEPARATE persisted list from the model, and with auto-route ON
        the router reads it on EVERY send. So changing the model without changing
        the pool means the very next message routes straight back to the old one:
        the switch silently undoes itself and the sidebar's green "active" marker
        never moves. Every path that changes the model must come through here.
        """
        if not self._auto_route or not self._is_local():
            return
        want = (model or self.session.model or "").strip()
        if not want:
            return
        rest = [k for k in self._route_pool if not k.startswith("local:")]
        new_pool = ["local:" + want] + rest
        if new_pool != self._route_pool:
            self._route_pool = new_pool
            self.cfg.route_pool = list(new_pool)
            providers.save(self.cfg)

    async def _router_panel(self) -> None:
        """ctrl+a with no prompt: pick models and toggle auto-route."""
        import asyncio

        # Ask each provider what it actually offers, off the UI thread: an
        # unreachable one (Ollama not running) would otherwise freeze the panel
        # for as long as its timeout.
        live = await asyncio.to_thread(self._live_provider_models)
        candidates = self._route_candidates(live)
        if not candidates:
            self._add_meta("no models to route between — download a .gguf or add a key")
            return
        result = await self.push_screen_wait(
            RouterScreen(candidates, self._route_pool, self._auto_route))
        if result is None:
            return
        enabled, selected = result
        self._auto_route = enabled
        self._route_pool = selected
        self.cfg.route_enabled = enabled
        self.cfg.route_pool = selected
        providers.save(self.cfg)
        self._add_meta(f"auto-route {'ON' if enabled else 'OFF'} · "
                        f"{len(selected)} model(s) in the pool")
        self._refresh()

    def _target_status(self) -> str:
        """Check the destination is actually usable, and say so in one line.

        This is a real check, not a label: for a cloud target it opens a TCP
        connection to the endpoint and looks at whether a key is set; for a local
        target it asks llama.cpp whether its server is up. Printed right after
        Enter, so "did it go the right way" has a factual answer.
        """
        from . import router
        engine = "local" if self._is_local() else "cloud"
        # The model being routed TO, not a stale session value - and shortened,
        # because a local model is a full .gguf path.
        model = self.cfg.model or self._short_model()
        if model.endswith(".gguf"):
            model = model.rsplit("/", 1)[-1][:-5]
        entry = self.cfg.providers.get(self.cfg.provider, {})
        url = entry.get("base_url", "")

        if engine == "cloud":
            key = "key set" if providers.resolve(entry.get("api_key", "")) else "NO KEY"
            up = router.reachable(url, timeout=3.0)
            verdict = "reachable" if up else "UNREACHABLE"
            return f"status: ☁ {self.cfg.provider} · {model} · {verdict} · {key}"

        try:
            import llamacpp_service as svc
            running = bool(svc.status().get("running"))
        except Exception:                       # noqa: BLE001
            running = router.reachable(url, timeout=1.0)
        verdict = "server up" if running else "server down (it will be started)"
        return f"status: 💻 local · {model} · {verdict}"

    def _backend_for(self, provider: str, model: str):
        """Build the backend for an ARBITRARY provider, not just the current one."""
        entry = self.cfg.providers.get(provider, {})
        url = entry.get("base_url", "")
        key = providers.resolve(entry.get("api_key", ""))
        return OpenAICompatBackend(
            base_url=url, model=model, api_key=key,
            timeout=getattr(self.args, "timeout", 120.0),
            temperature=getattr(self.args, "temperature", 0.7),
            max_tokens=self.cfg.max_tokens)

    def _route(self, text: str) -> dict:
        """Decide local vs cloud for ``text`` and point the app at the winner.

        The chosen models come from the ctrl+a pool; when nothing is selected the
        defaults are used (current local model, first keyed cloud provider) so
        routing still works out of the box.
        """
        from . import router
        local_model, cloud = self._pool_models()
        cloud_provider, cloud_url, cloud_model = self._cloud_target()
        if cloud is not None:
            cloud_provider, cloud_url, cloud_model = cloud
        if not local_model:
            # The pool may hold NO local entry (the user picked cloud models
            # only). A local route then has no model of its own, so fall back to
            # the configured one - and if even that is unset, ask the machine
            # rather than routing to a nameless "default".
            local_model = self.cfg.model or auto_model(self.cfg.base_url,
                                                       self.cfg.api_key) or ""
        decision = router.decide(
            text, cloud_provider=cloud_provider, cloud_url=cloud_url,
            cloud_model=cloud_model, local_model=local_model)

        self.cfg.provider = decision["provider"]
        model = decision.get("model") or ""
        if not model and decision["provider"] == "local":
            # An empty decision must NEVER erase the configured model. It used
            # to: a pool with no local entry returned "" for a local route, and
            # saving that wrote model:"" to disk - the gguf path was gone for
            # good and every later launch showed the model as "default".
            model = self.cfg.model or auto_model(self.cfg.base_url,
                                                 self.cfg.api_key) or ""
        if decision["provider"] == "local" and model:
            # The routing pool holds the picker's SHORT name ("gemma-3-12b-it"),
            # but llama.cpp needs a real path: resolve_model() treats a bare name
            # as a relative path, fails, and the turn dies with "model not
            # found". _set_model already does this conversion; routing must too.
            from . import localmodels as lm
            found = lm.find(model)
            if found:
                model = found.path
        if model:
            # Only ever assign a real model; an empty one leaves the config and
            # the session exactly as they were.
            self.cfg.model = model
            self.session.model = model
        if not isinstance(self.backend, EchoBackend):
            self.backend = self._backend_for(decision["provider"], self.cfg.model)
            self.real_backend = self.backend
        providers.save(self.cfg)
        self._route_note = router.explain(decision)
        self._set_status(self._status_combined())
        self._refresh()
        return decision

    def action_auto_route(self) -> None:
        """ctrl+a - route the typed message, or open the pool picker when empty."""
        if self._busy:
            return
        area = self.query_one("#prompt", PromptArea)
        text = (area.text or "").strip()
        if not text:
            # no prompt: this is the control panel — pick models, toggle on/off
            self.run_worker(self._router_panel(), exclusive=False)
            return
        area.text = ""
        self.session.add_user(text)
        self._add(text, "user", force_scroll=True)
        self._route(text)
        self._set_status(self._status_combined())
        self._warn_if_image_blind(text)
        self._ask(text)

    @on(PromptArea.Submitted, "#prompt")
    def _submitted(self, event: PromptArea.Submitted) -> None:
        # Busy = a turn is running. The gemini chat TUI disables its input while
        # the model responds; forge used to CLEAR the prompt here and then drop
        # the message on the floor, so a message typed during a long turn was
        # silently erased. Keep the text and do nothing instead - the prompt is
        # re-enabled (and refocused) when the turn ends.
        if self._busy:
            return
        text = (event.value or "").strip()
        self.query_one("#prompt", PromptArea).text = ""
        if not text:
            return
        if text.startswith("/"):
            self._command(text)
            self._refresh()
            return
        self.session.add_user(text)
        self._add(text, "user", force_scroll=True)
        self._route_note = ""      # never let the previous send's route leak here
        if self._auto_route:
            self._route(text)      # /auto: decide local vs cloud on every send
        # Enter is the moment to say where this actually went, and whether that
        # destination is usable right now.
        self._set_status(self._status_combined())
        self._warn_if_image_blind(text)
        self._ask(text)

    def _warn_if_image_blind(self, text: str) -> None:
        """Say so when the message names an image the selected model cannot see.

        A text-only GGUF does not reject an ``image_url`` part — it ignores it — so
        without this the answer arrives confidently and describes nothing.
        """
        from . import vision

        if not vision.paths_in(text, limit=1):
            return
        if not self._is_local():
            return                      # a hosted vision model decides for itself
        try:
            import llamacpp_service as svc

            if svc.find_mmproj(self.cfg.model):
                return                  # this one really can see
        except Exception:               # noqa: BLE001 - a failed probe is not a reason to nag
            return
        self._add_meta(
            "⚠ {} has no vision projector — the image will be ignored. Load a model "
            "marked 'vision projector paired' (e.g. /start gemma-3-12b-it) to have it "
            "read.".format(self._short_model()))

    # ------------------------------------------------------------------ copy
    def _copy_text(self, text: str) -> bool:
        """Put text on the system clipboard. True when a clipboard tool did it.

        Textual's copy_to_clipboard writes an OSC 52 escape sequence, which is
        the only option here (no xclip/xsel/wl-copy is installed) and the one
        that also works over SSH. Terminals cap the length they will accept, so
        the caller is told which route was used.
        """
        self.copy_to_clipboard(text)
        return False

    def _copy_reply(self, which: int = 1) -> None:
        """Copy an answer to the clipboard: 1 = the last one, 2 = the one before."""
        if not self._replies:
            self._add_meta("nothing to copy yet — ask something first")
            return
        which = max(1, min(which, len(self._replies)))
        text = self._replies[-which]
        self._copy_text(text)
        turn = "last answer" if which == 1 else f"answer {which} back"
        self._add_meta(f"copied the {turn} — {len(text)} characters, "
                        f"{text.count(chr(10)) + 1} line(s) — now paste with ctrl+v")

    def action_copy_reply(self) -> None:
        self._copy_reply(1)

    # ------------------------------------------------------------------- reply
    @work(exclusive=True)
    async def _ask(self, text: str) -> None:
        """Run one agent turn, drawing each tool call as it happens.

        The loop lives in agent.py; this only renders its events and answers the
        permission questions. That split is why the same agent could be driven
        by the plain UI too.
        """
        self._busy = True
        self._stop.clear()      # a new turn is never born cancelled
        self._set_mood("thinking")
        # Disable the prompt while the turn runs (like the gemini TUI): typing is
        # still possible but Enter cannot send, so a queued message is never
        # eaten mid-generation. Re-enabled in the finally block below.
        prompt = self.query_one("#prompt", PromptArea)
        prompt.disabled = True
        started = time.time()
        team = self._team_mode and self._is_local()
        # In team mode the answer card is created at the END: mounting it first
        # put the reply above the peer steps that produced it.
        card = None if team else self._add("", "bot")
        state = {"text": "", "reasoning": "", "card": card,
                 "think": None, "think_body": None,
                 "started": started, "tokens": 0}
        self._active = state

        def on_event(kind: str, payload: dict) -> None:
            # ``turn()`` runs in a worker thread, so every render is routed back
            # onto the app's thread and awaited there. This is also what makes
            # Markdown.update() actually apply - it returns an awaitable.
            self.call_from_thread(self._on_event_ui, state, kind, payload)

        try:
            # A "local" provider posts to llama-server on this machine. If it was
            # never started, the first message dies with "Connection refused" and
            # the user has to know to run /start. Start it and wait instead; the
            # spinner shows this whole time. Runs in a thread because the server
            # may take a while to load and must not freeze the UI.
            if self._is_local():
                import asyncio as _aio
                self._activity = "starting llama.cpp…"
                self._refresh_activity()
                err = await _aio.to_thread(self._ensure_local_server)
                if err:
                    await self._ensure_card(state).update(
                        f"**request failed** — {err}")
                    self._set_mood("sad")
                    return
                self._activity = ""
                self._warn_if_slow_load()

            # Team mode: every model in the pool does the same task, first
            # finished answer ships. Only applies on a local turn; a cloud turn
            # is already a single model.
            if team:
                answer = await self._team_turn(text, state)
                # The team loop bails at its next await once ctrl+c is pressed,
                # so its partial answer must not be dressed up as a finished one.
                if self._stop.is_set():
                    note = "_stopped by you — this answer is incomplete_"
                    if answer:
                        self._replies.append(answer)
                        self._add(f"{answer}\n\n{note}", "bot", force_scroll=True)
                    else:
                        self._add(note, "bot", force_scroll=True)
                    self._set_mood("neutral")
                    return
                if answer:
                    self.session.add_assistant(answer)
                    self._replies.append(answer)
                    self._add(
                        f"{answer}\n\n_team · {time.time() - started:.1f}s_",
                        "bot", force_scroll=True)
                    self._set_mood("happy")
                    return
                # The team produced nothing - most often a cloud key is missing
                # or invalid. Mount a clear note and STOP: falling through to the
                # non-team path here crashes, because team mode never created the
                # content card that path updates.
                self._add("_the team produced no answer — a provider key is "
                          "probably missing or invalid (set it, e.g. "
                          "DEEPSEEK_API_KEY, or pick another provider)_", "bot",
                          force_scroll=True)
                self._set_mood("sad")
                return

            turn = await self._run_agent(on_event)
            final = turn.text.strip()
            if turn.stopped:
                # Whatever streamed before ctrl+c is still worth keeping - just
                # say plainly that it is incomplete.
                note = "_stopped by you — this answer is incomplete_"
                if final:
                    self._replies.append(final)
                    await self._ensure_card(state).update(f"{final}\n\n{note}")
                else:
                    await self._ensure_card(state).update(note)
                self._set_mood("neutral")
                return
            if final:
                self.session.add_assistant(final, turn.last_reasoning)
                # Kept so the answer can be copied without selecting it by hand.
                self._replies.append(final)
                # The route note names the destination AND why; fall back to the
                # bare model when nothing was routed.
                head = self._route_note or self._short_model()
                await state["card"].update(
                    f"{final}\n\n_{head} · "
                    f"{time.time() - started:.1f}s · {len(turn.steps)} tool"
                    f"{'s' if len(turn.steps) != 1 else ''}_")
            elif turn.steps:
                await state["card"].update(
                    f"_finished after {len(turn.steps)} tool call(s), no summary_")
            self._set_mood("happy")
        except Exception as exc:  # noqa: BLE001
            # In team mode no card was created up front, so one may be needed here
            # - otherwise the handler itself raised and hid the real failure.
            card = state.get("card")
            if card is None:
                state["card"] = self._add(f"**unexpected error** — {exc}", "bot")
            else:
                await card.update(f"**unexpected error** — {exc}")
            self._set_mood("sad")
        finally:
            self._busy = False
            self._active = None
            if state.get("think") is not None:
                state["think"].title = f"Thinking… ({len(state['reasoning'])} chars)"
                # The body was throttled while hidden (collapsed); paint the full
                # reasoning now so expanding the dropdown later shows all of it,
                # not just the first chunk.
                try:
                    if state.get("think_body") is not None:
                        state["think_body"].update(state["reasoning"])
                except Exception:  # noqa: BLE001 - cosmetic, never fatal
                    pass
            self._refresh()
            # Re-enable the prompt and hand focus back, so the user can type the
            # next message immediately after the answer lands.
            try:
                prompt = self.query_one("#prompt", PromptArea)
                prompt.disabled = False
                prompt.focus()
            except Exception:  # noqa: BLE001 - never fatal
                pass

    # ------------------------------------------------------------------- team
    async def _call_text(self, backend, messages, state, limit: int = 700) -> str:
        """One plain, tool-free model call. Returns '' when it cannot answer."""
        import asyncio

        def work():
            parts = []
            for kind, text in backend.stream(messages):
                if kind == "content":
                    parts.append(text)
                    if sum(len(p) for p in parts) > limit * 4:
                        break           # a model that rambles is not worth waiting for
            return "".join(parts).strip()

        try:
            out = await asyncio.to_thread(work)
        except Exception:  # noqa: BLE001 - an unreachable model must not stop the work
            return ""
        state["tokens"] = state.get("tokens", 0) + max(0, len(out) // 4)
        return out

    def _team_event(self, kind: str, payload: dict) -> None:
        """Show a team peer's tool calls, so you can see it actually working.

        Called from the agent's WORKER THREAD (``turn()`` runs in an executor),
        so every widget call has to be handed to the app thread - mounting from
        here raised "no running event loop" and killed the turn.
        """
        def ui(fn, *args) -> None:
            # Already on the app thread (a direct call, or a future sync path)?
            # Then call it; call_from_thread refuses same-thread calls.
            if getattr(self, "_thread_id", None) == threading.get_ident():
                fn(*args)
            else:
                self.call_from_thread(fn, *args)

        if kind == "tool_start":
            ui(self._tool_card, payload["name"], payload["args"], None)
        elif kind == "tool_end":
            ui(self._finish_tool_card, payload["name"], payload["output"],
               payload.get("denied", False))

    async def _agent_text(self, backend, task: str, state, use_tools: bool = True) -> str:
        """A full agent turn against ``backend``, by its text.

        Runs on a throwaway session: a half-finished attempt must not become part
        of the conversation the user keeps. ``use_tools=False`` gives a read-only
        model (it can still read, but cannot write/edit/bash).

        The throwaway session IS seeded with the recent conversation (same
        sliding window the main loop uses), so a follow-up like "the just now
        folder" resolves to the folder the user actually meant - without this the
        model had no history and asked "which folder?" forever.
        """
        from .session import Session, window
        sess = Session(model=getattr(backend, "model", ""),
                       where=getattr(backend, "where", ""),
                       system=self.session.system)
        # Seed with the recent history BEFORE the current turn. The main session
        # already holds this turn's raw user message as its last entry, but the
        # task here is the WRAPPED team directive - so copy everything except
        # that last message, then append the wrapped task below. Without this the
        # model had zero history and asked "which folder?" forever.
        for m in window(self.session.messages[:-1]):
            sess.messages.append(m)
        sess.add_user(task)
        def on_event(kind, payload):
            # Count content as it streams, so the sidebar's token counter moves
            # DURING the turn instead of sitting at "~0 tok" for minutes while
            # the model is actually generating (team mode never updated it).
            if kind == "content" and isinstance(payload, dict):
                state["tokens"] = state.get("tokens", 0) + max(0, len(payload.get("text", "")) // 4)
            self._team_event(kind, payload)

        turn = await self._run_agent(on_event, backend=backend,
                                     session=sess, use_tools=use_tools)
        text = (turn.text or "").strip()
        return text, list(turn.steps)

    async def _team_turn(self, text: str, state) -> str:
        """Team mode: every model in the pool works the task as a peer.

        No senior, no junior - every model gets the same directive and its own
        tools, and they all start at once. The first finished answer ships and
        the turn returns; the rest keep going in the background and their answers
        are appended as notes when they land. This is the DeepSeek Harness idea:
        named models, no rank - whatever AI is inside does the job.
        """
        import asyncio

        from . import localmodels as lm

        peers = []                       # [(label, backend)]
        for key in self._route_pool:
            if key.startswith("local:"):
                name = key.split(":", 1)[1]
                found = lm.find(name) if name else None
                model = found.path if found else (name or self.cfg.model)
                peers.append(("local", self._backend_for("local", model)))
            elif ":" in key:
                provider, _, model = key.partition(":")
                if self.cfg.providers.get(provider, {}).get("base_url"):
                    peers.append((provider, self._backend_for(provider, model)))
        if not peers:
            # Nothing in the pool: fall back to the current backend so team mode
            # still does something rather than returning empty.
            peers.append((self.cfg.provider, self.backend))

        def who(backend):
            name = getattr(backend, "model", "") or ""
            if name.endswith(".gguf"):
                name = name.rsplit("/", 1)[-1][:-5]
            return name or "model"

        self._set_mood("thinking")
        names = " + ".join(f"**{who(b)}**" for _l, b in peers)
        self._add_meta(f"👥 team: {names} all started")

        # Launch every peer at once; the first to finish ships.
        tasks = {asyncio.create_task(self._agent_text(
            b, team.task_directive(text), state)): (label, b)
            for label, b in peers}

        first = None
        while tasks and first is None:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                label, b = tasks.pop(t)
                try:
                    answer = (t.result())[0] or ""
                except Exception:
                    answer = ""
                if answer.strip():
                    first = (label, b, answer)
                    break
            # keep only the still-running peers for the followup notes
            tasks = {t: v for t, v in tasks.items() if not t.done()}

        if first is None:
            return ""

        first_label, first_backend, answer = first
        if self._stop.is_set():
            for t in tasks:
                t.cancel()
            return answer

        self._add_meta(f"✅ **{who(first_backend)}** finished first")

        async def _followups():
            import llamacpp_service as svc
            # Keep the local server warm while the other peers still run, so the
            # idle watchdog cannot unload a model mid-turn.
            while tasks:
                try:
                    svc.touch()
                except Exception:
                    pass
                done, _ = await asyncio.wait(
                    list(tasks), timeout=20,
                    return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    _l, b = tasks.pop(t)
                    try:
                        note = (t.result())[0] or ""
                    except Exception:
                        note = ""
                    if note.strip():
                        self._add_meta(
                            f"💬 **{who(b)}** also finished: "
                            + " ".join(note.strip().split())[:160])

        if tasks:
            self.run_worker(_followups(), exclusive=False)

        return answer

    async def _on_event_ui(self, state: dict, kind: str, payload: dict) -> None:
        """Apply one agent event on the app thread (see ``_ask``)."""
        if kind == "reasoning":
            state["reasoning"] += payload["text"]
            self._set_mood("thinking")
            # Thinking goes in a collapsed dropdown above the answer, so the chat
            # shows the reply and the reasoning only if the user expands it.
            if state["think"] is None:
                body = Static(state["reasoning"], classes="thinking")
                think = Collapsible(body, title="Thinking…", collapsed=True)
                self.query_one("#chat", VerticalScroll).mount(think, before=state["card"])
                state["think"] = think
                state["think_body"] = body
            else:
                # The body is hidden while collapsed, so repainting it on every
                # token is pure waste; when expanded, coalesce to ~4 paints/second.
                if not state["think"].collapsed and \
                        time.time() - state.get("_think_last", 0) >= 0.25:
                    state["_think_last"] = time.time()
                    state["think_body"].update(state["reasoning"])
        elif kind == "content":
            state["text"] += payload["text"]
            # Re-parsing the whole Markdown card on every token floods the loop
            # with layout work (the card grows each time), which is what made the
            # interface flicker and blank during a stream. Coalesce to ~8 renders
            # per second; the final card.update at the end of the turn paints the
            # tail this deliberately skips.
            if state["card"] is not None and \
                    time.time() - state.get("_card_last", 0) >= 0.12:
                state["_card_last"] = time.time()
                await state["card"].update(state["text"])
        elif kind == "tool_start":
            self._set_mood("glitch")
            self._tool_card(payload["name"], payload["args"], None)
            # The pre-tool prose card is empty when the reasoning went to the
            # dropdown; drop it instead of leaving an empty bubble.
            if not state["text"] and state["card"] is not None:
                state["card"].remove()
            state["card"] = self._add("", "bot")   # next prose gets a new card
        elif kind == "tool_end":
            await self._finish_tool_card(payload["name"], payload["output"],
                                         payload.get("denied", False))
            self._set_mood("thinking")
        elif kind == "stall":
            # The loop caught the model reading without acting and told it to
            # stop. Say so, so a stall reads as a stall and not as "thinking".
            self._add_meta(
                f"⏸ {payload['rounds']} rounds of reading with no change — "
                "told it to apply the fix now")
        elif kind == "error":
            await self._ensure_card(state).update(
                f"**request failed** — {payload['message']}")
            self._set_mood("sad")
        state["tokens"] = len(state["text"]) // 4
        self._stick_chat_bottom()
        self._refresh_activity()

    async def _run_agent(self, on_event, backend=None, session=None, use_tools=True):
        """The agent's ``turn`` is blocking, so it runs in a thread.

        ``backend``/``session`` default to the current ones; team mode passes its
        own so each peer's attempt can run on a throwaway session.
        """
        import asyncio

        backend = backend if backend is not None else self.backend
        session = session if session is not None else self.session
        loop = asyncio.get_running_loop()
        result: dict = {}

        def work():
            try:
                ag = agent_mod.Agent(
                    backend, session,
                    use_tools=use_tools,
                    native_tools=agent_mod.supports_native_tools(backend),
                    approve=self._approve_blocking,
                    persist=lambda: providers.save(self.cfg),
                )
                result["turn"] = ag.turn(
                    on_event, should_stop=self._stop.is_set)
            except Exception as exc:  # noqa: BLE001
                result["error"] = exc

        await loop.run_in_executor(None, work)
        if "error" in result:
            raise result["error"]
        return result["turn"]

    def _approve_blocking(self, name: str, args: dict, summary: str):
        """Ask before a dangerous tool runs.

        Called from the agent's worker thread, so the modal has to be pushed
        onto the app from the event loop and waited on. Returns True (allow
        once), False (deny) or agent_mod.APPROVE_ALL (allow the rest).
        """
        import asyncio
        import concurrent.futures

        fut: concurrent.futures.Future = concurrent.futures.Future()

        def push():
            async def ask():
                try:
                    fut.set_result(await self.push_screen_wait(
                        ConfirmTool(name, args, summary)))
                except Exception as exc:  # noqa: BLE001
                    fut.set_exception(exc)
            self.run_worker(ask(), exclusive=False)

        self.call_from_thread(push)
        try:
            return fut.result(timeout=300)
        except Exception:  # noqa: BLE001 - a timeout means "no"
            return False

    # ------------------------------------------------------------ tool cards
    def _tool_card(self, name: str, args: dict, _output) -> None:
        """A tool call gets its own card, so the transcript shows the work."""
        chat = self.query_one("#chat", VerticalScroll)
        tool = _TOOL_ICON.get(name, "⚙")
        summary = _summarise(name, args)
        card = Markdown(f"{tool} **{name}**  `{summary}`", classes="msg from-tool")
        chat.mount(card)
        self._stick_chat_bottom()
        self._tool_cards[name] = card

    async def _finish_tool_card(self, name: str, output: str, denied: bool) -> None:
        card = self._tool_cards.get(name)
        try:
            # Second line of defence: a stray escape sequence here would reach
            # the terminal itself (mouse reporting, cursor moves) and wreck the
            # UI, so clean it even though tools.execute() already does.
            from .tools import _clean
            output = _clean(output) or ""
        except Exception:  # noqa: BLE001
            pass
        lines = output.splitlines()
        shown = "\n".join(lines[:12])
        if len(lines) > 12:
            shown += f"\n_… {len(lines) - 12} more lines_"
        tool = _TOOL_ICON.get(name, "⚙")
        head = "⛔ **denied**" if denied else f"{tool} **{name}**"
        if card is not None:
            try:
                await card.update(f"{head} — done\n\n```\n{shown}\n```")
            except Exception:  # noqa: BLE001
                pass

    # --------------------------------------------------------------- commands
    def _capture_render(self, fn) -> Text:
        """Run ``fn`` with render's Console redirected; return the output as Text.

        render's Console writes to stdout, which is invisible inside a Textual
        app (and corrupts the screen). Swapping it for a buffer is what makes
        every classic command's output visible in the transcript.
        """
        import io

        from rich.console import Console

        from . import render

        chat = self.query_one("#chat", VerticalScroll)
        buffer = io.StringIO()
        previous = render._console
        render._console = Console(
            file=buffer, theme=T.THEME, highlight=False,
            force_terminal=True, color_system="truecolor",
            width=max(40, (chat.size.width or 80) - 6))
        try:
            fn()
        finally:
            render._console = previous
        return Text.from_ansi(buffer.getvalue().rstrip("\n"))

    def _command(self, line: str) -> None:
        """Slash commands: the SAME dispatch table the plain UI uses.

        Commands whose handler opens its own prompt_toolkit prompt (which fights
        Textual for the terminal) are intercepted here and re-run with the app's
        own pickers instead.
        """
        from . import commands as C

        name, _, arg = line.partition(" ")
        arg = arg.strip()
        words = arg.lower().split()

        if name.lower() in QUIT_ALIASES:
            self._begin_goodbye()
            return
        if name == "/clear":
            self.action_new_session()
            return
        if name in ("/help", "/?"):
            self.action_show_help(arg)
            return
        if name == "/model" and not arg:
            self.action_pick_model()
            return
        if name == "/provider" and not arg:
            self.action_pick_provider()
            return
        if name == "/setup":
            self.run_worker(self._setup_flow(), exclusive=False)
            return
        if name == "/start" and not arg:
            self.run_worker(self._start_flow(), exclusive=False)
            return
        if name in ("/download", "/pull"):
            self.run_worker(self._download_flow(arg), exclusive=False)
            return
        if name == "/keys" and not arg:
            self.run_worker(self._keys_flow(), exclusive=False)
            return
        if name == "/keys" and words and words[0] in ("clear", "reset", "wipe"):
            self.run_worker(self._keys_clear_flow("all" in words), exclusive=False)
            return
        if name in ("/copy", "/yank"):
            # The app owns the clipboard here, so this cannot go through the
            # shared table (which has no terminal to write OSC 52 to).
            n = 1
            if arg.isdigit():
                n = int(arg)
            elif arg and arg.lower() not in ("last", "answer"):
                self._add_meta("usage: /copy [how many answers back, e.g. /copy 2]")
                return
            self._copy_reply(n)
            return
        if name == "/team":
            self._team_mode = not self._team_mode
            self.cfg.team_enabled = self._team_mode
            providers.save(self.cfg)
            self._add_meta(
                "team mode " + ("ON — every model in the pool does the task, "
                                "first finished answer ships"
                                if self._team_mode else "OFF — one model per message"))
            self._refresh()
            return
        if name == "/auto":
            self._auto_route = not self._auto_route
            self._add_meta("auto-route " + ("ON — every message is sent to "
                             "local or cloud automatically" if self._auto_route
                             else "OFF"))
            self._refresh()
            return
        if name == "/route":
            if not arg:
                self._add_meta("usage: /route <your prompt>")
                return
            self.session.add_user(arg)
            self._add(arg, "user")
            self._route(arg)
            self._ask(arg)
            return

        class _Ctx:
            pass

        ctx = _Ctx()
        ctx.session, ctx.cfg, ctx.args = self.session, self.cfg, self.args
        ctx.backend, ctx.real_backend = self.backend, self.real_backend
        ctx.running = True

        try:
            captured = self._capture_render(lambda: C.handle(line, ctx))
        except Exception as exc:  # noqa: BLE001
            self._add(f"command failed: {exc}", "bot")
            return

        self.backend, self.real_backend = ctx.backend, ctx.real_backend
        if captured.plain.strip():
            self._add_plain(captured, "bot")
        # Any command that can change the model must leave the route pool pointing
        # at it. With auto-route ON the router reads the pool on every send, so a
        # stale entry silently reverts the switch and freezes the sidebar's green
        # "active" marker on the old model. /start was the worst case: the server
        # really was serving the new model while the UI still named the old one.
        if name in ("/model", "/start", "/provider"):
            self._sync_route_pool_local()
        self._refresh()

    # ---------------------------------------------------------------- actions
    def action_show_help(self, query: str = "") -> None:
        """Every command grouped with a runnable example, or one in detail.

        Both views come from commands.COMMAND_GROUPS. /team, /auto and /route
        used to exist only as palette entries, so /help never mentioned them.
        """
        from .commands import COMMAND_GROUPS, lookup

        query = (query or "").strip()
        if query:
            found = lookup(query)
            if found is None:
                self._add_meta(f"no command called `{query}` — `/help` lists them all")
                return
            lines = [f"### `{found.usage}`", "", found.desc, "",
                     "**example**", "", "```", found.example, "```"]
            if found.client == "tui":
                lines += ["", "_terminal client only — run `forge` to use it_"]
            self._add("\n".join(lines) + "\n", "bot")
            return

        out = ["### commands", "",
               "_`/help <command>` explains any one of them._", ""]
        for title, rows in COMMAND_GROUPS:
            out += [f"**{title}**", ""]
            for c in rows:
                tag = "  _(terminal client only)_" if c.client == "tui" else ""
                out.append(f"- `{c.usage}` — {c.desc}{tag}  ·  `e.g. {c.example}`")
            out.append("")
        self._add("\n".join(out) + "\n", "bot")

    def action_new_session(self) -> None:
        self.session.clear()
        self.query_one("#chat", VerticalScroll).remove_children()
        self._greet()
        self._refresh()

    def action_focus_chat(self) -> None:
        self.query_one("#chat", VerticalScroll).focus()

    def action_focus_prompt(self) -> None:
        self.query_one("#prompt", PromptArea).focus()

    def action_pick_model(self) -> None:
        """ctrl+l - a searchable list of every model actually reachable."""
        self.run_worker(self._pick_model(), exclusive=False)

    async def _pick_model(self) -> None:
        from .backend import fetch_models

        options: list[tuple[str, str]] = []
        seen = set()

        # local .gguf files first - they are the ones that need no key
        for m in localmodels.available():
            bits = [f"{m.size_gb:.1f} GB", m.caps_label, "local"]
            badge = localmodels.fit_badge(m.size_gb)
            if badge:
                bits.append(badge)
            options.append((m.name, " · ".join(bits)))
            seen.add(m.name)

        # then whatever the current endpoint offers
        for name in fetch_models(self.cfg.base_url, self.cfg.api_key):
            if name not in seen:
                options.append((name, f"{self.cfg.provider} · remote"))
                seen.add(name)

        # then cached suggestions from the config
        for name in self.cfg.current.get("models") or []:
            if name not in seen:
                options.append((name, f"{self.cfg.provider} · suggested"))
                seen.add(name)

        if not options:
            self._add("_no models found — ctrl+o to switch provider, "
                      "or run /setup_", "bot")
            return

        choice = await self.push_screen_wait(
            Picker("switch model", options, current=self._short_model()))
        if not choice:
            return
        self._set_model(choice)

    def _set_model(self, name: str) -> None:
        """Point the session (and the backend) at a different model."""
        from . import localmodels as lm

        offline = lm.find(name)
        self.session.model = name
        self.cfg.model = offline.path if offline else name
        if offline:
            # a local model means the local endpoint, whatever was selected
            self.cfg.provider = "local"
            self.cfg.set_base_url("http://127.0.0.1:8080/v1")
        # The ctrl+l picker is just as much a model change as /model is, so the
        # route pool has to follow it too or auto-route reverts it on the next send.
        self._sync_route_pool_local(name)
        if not isinstance(self.backend, EchoBackend):
            self.backend = OpenAICompatBackend(
                base_url=self.cfg.base_url, model=self.cfg.model,
                api_key=self.cfg.api_key,
                timeout=getattr(self.args, "timeout", 120.0),
                temperature=getattr(self.args, "temperature", 0.7),
                max_tokens=self.cfg.max_tokens)
            self.real_backend = self.backend
        providers.save(self.cfg)
        self._add(f"_model → **{name}**_", "bot")
        self._refresh()

    def action_pick_provider(self) -> None:
        """ctrl+o - switch provider, then offer its models."""
        self.run_worker(self._pick_provider(), exclusive=False)

    async def _pick_provider(self, then_model: bool = True) -> bool:
        options = []
        for name in sorted(self.cfg.providers):
            entry = self.cfg.providers[name]
            has = "key" if providers.resolve(entry.get("api_key", "")) else "no key"
            options.append((name, f"{entry.get('label','')} · {has}"))
        choice = await self.push_screen_wait(
            Picker("switch provider", options, current=self.cfg.provider))
        if not choice:
            return False
        self.cfg.provider = choice
        self.cfg.model = ""
        self.session.model = ""
        if not isinstance(self.backend, EchoBackend):
            self.backend = OpenAICompatBackend(
                base_url=self.cfg.base_url, model="",
                api_key=self.cfg.api_key,
                timeout=getattr(self.args, "timeout", 120.0),
                temperature=getattr(self.args, "temperature", 0.7),
                max_tokens=self.cfg.max_tokens)
            self.real_backend = self.backend
        providers.save(self.cfg)
        self._add(f"_provider → **{choice}**  ({self.cfg.base_url})_", "bot")
        self._refresh()
        # a fresh provider usually needs a model picked; offer it right away
        if then_model:
            await self._pick_model()
        return True

    async def _setup_flow(self) -> None:
        """TUI /setup: provider, then its key, then a model.

        Order matters - the endpoint's model list needs the key first.
        """
        if not await self._pick_provider(then_model=False):
            return
        await self._edit_key()
        await self._pick_model()

    async def _keys_flow(self) -> None:
        """TUI /keys: print the same table, then let a key be changed."""
        from . import render

        def show():
            render.table("saved keys", [
                (f"{'▶' if n == self.cfg.provider else ' '} {n}",
                 providers.key_state(self.cfg, n))
                for n in sorted(self.cfg.providers)])

        captured = self._capture_render(show)
        if captured.plain.strip():
            self._add_plain(captured, "bot")

        options = [(n, providers.key_state(self.cfg, n))
                   for n in sorted(self.cfg.providers)]
        choice = await self.push_screen_wait(
            Picker("change which key? (esc = keep as is)", options,
                   current=self.cfg.provider))
        if not choice:
            return
        raw = self.cfg.providers[choice].get("api_key", "")
        placeholder = ("paste new key — current: " + providers.masked(raw)) if raw \
            else "paste key (none set)"
        value = await self._ask_input(f"key · {choice}", placeholder=placeholder,
                                      password=True)
        if not value or not value.strip():
            return
        self.cfg.providers[choice]["api_key"] = value.strip()
        providers.save(self.cfg)
        self._add(f"_key saved for **{choice}**_", "bot")
        self._refresh()

    async def _keys_clear_flow(self, everything: bool) -> None:
        """TUI /keys clear [all], with the confirmation the classic prompt did."""
        keep = "" if everything else self.cfg.provider
        doomed = [n for n in sorted(self.cfg.providers)
                  if n != keep and self.cfg.providers[n].get("api_key", "")]
        if not doomed:
            self._add("_nothing to clear — no other provider has a key saved_", "bot")
            return
        options = [("yes", f"remove {len(doomed)} key(s): " + ", ".join(doomed)),
                   ("no", "keep them")]
        choice = await self.push_screen_wait(
            Picker(f"clear keys — keeping {keep or 'nothing'}", options))
        if choice != "yes":
            self._add("_cancelled_", "bot")
            return
        for name in doomed:
            self.cfg.providers[name]["api_key"] = ""
        providers.save(self.cfg)
        self._add(f"_cleared keys for: {', '.join(doomed)}_", "bot")
        self._refresh()

    async def _start_flow(self) -> None:
        """TUI /start: pick an offline .gguf and load it into llama-server."""
        options = []
        for m in localmodels.available():
            bits = [f"{m.size_gb:.1f} GB", m.caps_label]
            badge = localmodels.fit_badge(m.size_gb)
            if badge:
                bits.append(badge)
            options.append((m.name, " · ".join(bits)))
        if not options:
            self._add("_no offline .gguf models found_", "bot")
            return
        choice = await self.push_screen_wait(Picker("load which offline model?", options))
        if not choice:
            return
        self._command(f"/start {choice}")

    async def _download_flow(self, query: str) -> None:
        """TUI /download: search Hugging Face, pick a repo + file, then pull it."""
        from . import model_download

        query = (query or "").strip()
        if not query:
            self._add("_usage: /download <search query>  —  e.g. /download qwen 7b_", "bot")
            return

        self._add(f"_searching Hugging Face for GGUF models matching {query!r}…_", "bot")
        import asyncio
        try:
            results = await asyncio.to_thread(model_download.search, query)
        except Exception as exc:  # noqa: BLE001
            self._add(f"_search failed: {exc}_", "bot")
            return
        if not results:
            self._add("_no GGUF models found — try a shorter or different query_", "bot")
            return

        repo_opts = [(r, f"{dl:,} downloads" if dl else "") for r, dl in results[:20]]
        repo = await self.push_screen_wait(Picker("pick a model repo", repo_opts))
        if not repo:
            return

        self._add(f"_listing GGUF files in {repo}…_", "bot")
        try:
            repo_files = await asyncio.to_thread(model_download.files, repo)
        except Exception as exc:  # noqa: BLE001
            self._add(f"_listing failed: {exc}_", "bot")
            return
        if not repo_files:
            self._add("_no .gguf files in that repo_", "bot")
            return

        file_opts = [(n, f"{s:.2f} GB") for n, s in repo_files[:20]]
        filename = await self.push_screen_wait(Picker("pick a file to download", file_opts))
        if not filename:
            return

        self._add(f"_downloading {filename}… (this can take a while)_", "bot")
        try:
            path = await asyncio.to_thread(model_download.download, repo, filename)
        except Exception as exc:  # noqa: BLE001
            self._add(f"_download failed: {exc}_", "bot")
            return
        self._add(f"_downloaded → {path}_", "bot")
        self._add("_load it with /start, or pick it with ctrl+l_", "bot")

    def action_palette(self) -> None:
        """ctrl+p - searchable command palette, instead of remembering names."""
        from .commands import COMMANDS
        self.run_worker(self._palette(COMMANDS), exclusive=False)

    async def _palette(self, commands) -> None:
        # One entry per COMMAND, not per usage row: /keys and /memory each
        # document several forms, and repeating them would bury the rest.
        seen, options = set(), []
        for usage, desc in commands:
            name = usage.split()[0]
            if name in seen:
                continue
            seen.add(name)
            options.append((name, desc))
        options += PALETTE_ACTIONS
        choice = await self.push_screen_wait(Picker("commands", options))
        if not choice:
            return
        if choice in ("set key", "/key"):
            await self._edit_key()
        elif choice == "add model":
            await self._add_model()
        elif choice == "remove model":
            await self._remove_model()
        elif choice == "/model":
            self.action_pick_model()
        elif choice == "/provider":
            self.action_pick_provider()
        elif choice in ARG_PROMPTS:
            arg = await self._resolve_arg(choice, ARG_PROMPTS[choice])
            if arg:
                self._command(f"{choice} {arg}")
        elif choice.startswith("/"):
            self._command(choice)

    async def _resolve_arg(self, cmd: str, spec: dict):
        """Ask for a command's argument. Returns the string, or None if cancelled."""
        if spec.get("mode") == "local":
            options = []
            for m in localmodels.available():
                bits = [f"{m.size_gb:.1f} GB", m.caps_label]
                badge = localmodels.fit_badge(m.size_gb)
                if badge:
                    bits.append(badge)
                options.append((m.name, " · ".join(bits)))
            if not options:
                self._add("_no local .gguf models found_", "bot")
                return None
            return await self.push_screen_wait(Picker("load local model", options))
        # The current value goes in the PLACEHOLDER, not the field: a pre-filled
        # field makes typing append to the old value instead of replacing it.
        prompt = spec.get("prompt", "value")
        prefill = spec.get("prefill")
        shown = prompt
        if prefill == "base_url" and self.cfg.base_url:
            shown = f"current: {self.cfg.base_url}"
        elif prefill == "system" and self.session.system:
            shown = f"current: {self.session.system[:60]}"
        return await self._ask_input(f"{cmd} — {prompt}", placeholder=shown)

    # ------------------------------------------------------------ palette edits
    async def _ask_input(self, title: str, *, placeholder: str = "",
                         value: str = "", password: bool = False):
        """Push the one-line input modal; returns the string or None."""
        return await self.push_screen_wait(
            InputDialog(title, value=value, placeholder=placeholder,
                        password=password))

    async def _edit_key(self) -> None:
        """Set/change the API key for the CURRENT provider."""
        raw = self.cfg.raw_key
        placeholder = ("paste new key — current: " + providers.masked(raw)) if raw else \
                      "paste key (none set)"
        value = await self._ask_input(
            f"API key · {self.cfg.provider}", placeholder=placeholder,
            password=True)
        if value is None or not value.strip():
            return
        self.cfg.set_key(value.strip())
        providers.save(self.cfg)
        self._add(f"_key saved for **{self.cfg.provider}**_", "bot")
        self._refresh()

    async def _add_model(self) -> None:
        """Add a model id to the current provider's cached list."""
        name = await self._ask_input(
            f"add model · {self.cfg.provider}", placeholder="model id, e.g. deepseek-chat")
        if not name or not name.strip():
            return
        name = name.strip()
        models = self.cfg.current.setdefault("models", [])
        if name in models:
            self._add(f"_`{name}` is already listed_", "bot")
            return
        models.append(name)
        providers.save(self.cfg)
        self._add(f"_added model **{name}**_", "bot")
        self._refresh()

    async def _remove_model(self) -> None:
        """Remove one model id from the current provider's cached list."""
        models = self.cfg.current.get("models") or []
        if not models:
            self._add("_no cached models to remove — use /models for the "
                      "endpoint list_", "bot")
            return
        options = [(m, f"{self.cfg.provider} · cached") for m in models]
        choice = await self.push_screen_wait(Picker("remove model", options))
        if not choice:
            return
        self.cfg.current["models"].remove(choice)
        providers.save(self.cfg)
        self._add(f"_removed model **{choice}**_", "bot")
        self._refresh()


class Picker(ModalScreen):
    """Searchable list: type to filter, click or arrow to move, enter to pick.

    ``push_screen_wait`` resolves to the chosen key (a string), or ``None``.
    """

    BINDINGS = [
        ("enter", "choose", "Choose"),
        ("escape", "cancel", "Cancel"),
        # The filter box holds focus, so these never reach the ListView by
        # themselves. Without them the only way to pick was to type enough of a
        # name to filter the list down to one row and then press enter - you
        # could not simply move to the row you wanted.
        ("up", "move(-1)", "Up"),
        ("down", "move(1)", "Down"),
        ("pageup", "move(-8)", "Page up"),
        ("pagedown", "move(8)", "Page down"),
    ]

    # Two clicks on the same row within this many seconds count as a double
    # click. Generous on purpose: it has to survive a slow hand.
    _DOUBLE_CLICK_S = 0.6

    def __init__(self, title: str, options, current: str = ""):
        super().__init__()
        self._title = title
        self._options = list(options)   # (key, description) pairs
        self._current = current or ""
        self._keys: list[str] = []
        self._last_click: tuple = (None, 0.0)

    def compose(self) -> ComposeResult:
        with Vertical(id="pickerbox"):
            yield Static(f"[bold {T.PURPLE}]{self._title}[/]", id="pickerhead")
            yield Input(placeholder="type to filter…", id="pickerfilter")
            yield ListView(id="pickerlist")
            yield Static(
                f"[{T.GREY}]click a row to move to it · click it again (or enter) "
                f"to pick · ↑↓/pgup/pgdn · esc cancels[/]", id="pickerhint")

    def on_mount(self) -> None:
        self.query_one("#pickerfilter", Input).focus()
        self._rebuild("")

    @on(Input.Changed, "#pickerfilter")
    def _on_changed(self, event: Input.Changed) -> None:
        self._rebuild(event.value)

    # The filter Input keeps focus, so Enter arrives as a submit, not as the
    # screen-level "enter" binding. Handle both so picking always works.
    @on(Input.Submitted, "#pickerfilter")
    def _on_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._choose()

    def _rebuild(self, query: str) -> None:
        q = query.strip().lower()
        lv = self.query_one("#pickerlist", ListView)
        lv.clear()
        self._keys = []
        for key, desc in self._options:
            if q and q not in key.lower() and q not in desc.lower():
                continue
            mark = "● " if key == self._current else "  "
            self._keys.append(key)
            lv.append(ListItem(Label(f"{mark}{key}   {desc}")))
        if self._keys:
            lv.index = 0

    def action_move(self, delta: int) -> None:
        """Move the highlight, clamped to the (filtered) list."""
        if not self._keys:
            return
        lv = self.query_one("#pickerlist", ListView)
        idx = (lv.index if lv.index is not None else 0) + delta
        lv.index = max(0, min(len(self._keys) - 1, idx))

    @on(Click, "#pickerlist ListItem")
    def _on_item_click(self, event: Click) -> None:
        """Mouse: click a row to move to it, click the same row again to pick it.

        The ListView has already moved its own highlight by the time this runs,
        so "was this row already selected?" cannot be answered from lv.index -
        the previous click is remembered instead. That also means a real double
        click picks you the row, whether or not the terminal reports a click
        chain, and a single click on a *different* row only moves the highlight.
        """
        event.stop()
        lv = self.query_one("#pickerlist", ListView)
        idx = next((i for i, child in enumerate(lv.children)
                    if child is event.control), None)
        if idx is None or idx >= len(self._keys):
            return
        now = time.monotonic()
        last_idx, last_at = self._last_click
        again = (last_idx == idx and now - last_at <= self._DOUBLE_CLICK_S)
        lv.index = idx
        self._last_click = (idx, now)
        if again or (getattr(event, "chain", 1) or 1) >= 2:
            self._choose()

    def _choose(self) -> None:
        lv = self.query_one("#pickerlist", ListView)
        idx = lv.index
        if self._keys and idx is not None and 0 <= idx < len(self._keys):
            self.dismiss(self._keys[idx])

    def action_choose(self) -> None:
        self._choose()

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmTool(ModalScreen):
    """Ask before a dangerous tool runs: enter=once, a=allow all, n=no."""

    BINDINGS = [
        ("enter", "allow", "Allow"),
        ("a", "allow_all", "Allow all"),
        ("n", "deny", "Deny"),
        ("escape", "deny", "Deny"),
    ]

    def __init__(self, name: str, args: dict, summary: str):
        super().__init__()
        self._name = name
        self._args = args or {}
        self._summary = summary or ""

    def compose(self) -> ComposeResult:
        import json
        shown = json.dumps(self._args, indent=2)
        if len(shown) > 400:
            shown = shown[:400] + "\n…"
        with Vertical(id="confirmbox"):
            yield Static(f"[bold {T.YELLOW}]Allow `{self._name}`?[/]")
            if self._summary:
                yield Static(f"[{T.GREY}]{self._summary}[/]")
            yield Static(shown)
            # Clickable as well as keyed: the same three choices, so a mouse is
            # enough to answer the prompt.
            with Horizontal(id="confirmbuttons"):
                yield Button("allow", id="allow", variant="success")
                yield Button("allow all", id="allowall", variant="warning")
                yield Button("deny", id="deny", variant="error")
            yield Static(f"[{T.GREY}]enter allow · a allow all · n deny · esc deny[/]")

    @on(Button.Pressed)
    def _on_button(self, event: Button.Pressed) -> None:
        event.stop()
        action = {"allow": self.action_allow,
                  "allowall": self.action_allow_all,
                  "deny": self.action_deny}.get(event.button.id or "")
        (action or self.action_deny)()

    def action_allow(self) -> None:
        self.dismiss(True)

    def action_allow_all(self) -> None:
        self.dismiss(agent_mod.APPROVE_ALL)

    def action_deny(self) -> None:
        self.dismiss(False)


class InputDialog(ModalScreen):
    """One-line input: title + Input, enter=submit, esc=cancel.

    ``push_screen_wait`` resolves to the typed string, or ``None`` when cancelled.
    """

    BINDINGS = [
        ("enter", "submit", "OK"),
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, title: str, value: str = "", placeholder: str = "",
                 password: bool = False):
        super().__init__()
        self._title = title
        self._value = value
        self._placeholder = placeholder
        self._password = password

    def compose(self) -> ComposeResult:
        with Vertical(id="inputbox"):
            yield Static(f"[bold {T.PURPLE}]{self._title}[/]", id="inputtitle")
            yield Input(value=self._value, placeholder=self._placeholder,
                        password=self._password, id="inputfield")
            yield Static(f"[{T.GREY}]enter ok · esc cancel[/]", id="inputhint")

    def on_mount(self) -> None:
        inp = self.query_one("#inputfield", Input)
        inp.focus()
        inp.cursor_position = len(self._value)

    @on(Input.Submitted, "#inputfield")
    def _on_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value)

    def action_submit(self) -> None:
        self.dismiss(self.query_one("#inputfield", Input).value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class RouterScreen(ModalScreen):
    """ctrl+a (empty prompt): choose which models auto-route may use.

    ``space`` / click toggles a model's checkbox; ``a`` toggles auto-route itself;
    ``enter`` saves; ``escape`` cancels. ``push_screen_wait`` resolves to
    ``(enabled, [selected keys])`` or ``None`` when cancelled.
    """

    BINDINGS = [
        ("space", "toggle", "Toggle"),
        ("enter", "save", "Done"),
        ("escape", "cancel", "Cancel"),
        ("a", "toggle_auto", "Auto on/off"),
    ]

    def __init__(self, candidates, selected, enabled: bool):
        super().__init__()
        self._candidates = list(candidates)      # [(key, label)]
        self._selected = set(selected or [])
        self._enabled = bool(enabled)
        self._keys: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="routerbox"):
            yield Static(f"[bold {T.YELLOW}]auto-route[/]", id="routerhead")
            yield Static("", id="routerstatus")
            yield ListView(id="routerlist")
            yield Static(f"[{T.GREY}]space/click toggle a model · a auto on/off · "
                         f"enter done · esc cancel[/]", id="routerhint")

    def on_mount(self) -> None:
        self._rebuild()
        self.query_one("#routerlist", ListView).focus()

    def _rebuild(self) -> None:
        lv = self.query_one("#routerlist", ListView)
        lv.clear()
        self._keys = []
        for key, label in self._candidates:
            mark = "☑" if key in self._selected else "☐"
            self._keys.append(key)
            lv.append(ListItem(Label(f"{mark}  {label}")))
        if self._keys:
            lv.index = 0
        self._status()

    def _status(self) -> None:
        on = self._enabled
        style = f"bold {T.YELLOW}" if on else T.GREY
        self.query_one("#routerstatus", Static).update(
            f"[{style}]auto-route {'ON' if on else 'OFF'}[/] · "
            f"{len(self._selected)} model(s) selected")

    def _toggle_at(self, idx) -> None:
        if not (0 <= idx < len(self._keys)):
            return
        key = self._keys[idx]
        if key in self._selected:
            self._selected.discard(key)
        else:
            self._selected.add(key)
        self._rebuild()
        self.query_one("#routerlist", ListView).index = idx

    def action_toggle(self) -> None:
        lv = self.query_one("#routerlist", ListView)
        self._toggle_at(lv.index if lv.index is not None else 0)

    @on(Click, "#routerlist ListItem")
    def _on_item_click(self, event: Click) -> None:
        event.stop()
        lv = self.query_one("#routerlist", ListView)
        idx = next((i for i, c in enumerate(lv.children) if c is event.control), None)
        if idx is not None:
            self._toggle_at(idx)

    # ListView binds enter to "select", so the focused list eats it before the
    # screen's enter->save binding can fire. Selecting a row = "done".
    @on(ListView.Selected, "#routerlist")
    def _on_selected(self, event: ListView.Selected) -> None:
        event.stop()
        self.action_save()

    def action_toggle_auto(self) -> None:
        self._enabled = not self._enabled
        self._status()

    def action_save(self) -> None:
        self.dismiss((self._enabled, sorted(self._selected)))

    def action_cancel(self) -> None:
        self.dismiss(None)


def _disable_quickedit() -> None:
    """On Windows, stop the console's QuickEdit selection mode.

    cmd.exe (and the legacy conhost) turn a mouse click into text-selection by
    default, and while a selection is active the console blocks the app. Textual's
    mouse input therefore never receives a click, and the UI looks frozen. This is
    what makes the full-screen client clickable on Windows; on other platforms it
    is a no-op.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        ENABLE_QUICK_EDIT_MODE = 0x0040
        ENABLE_EXTENDED_FLAGS = 0x0080
        k32 = ctypes.windll.kernel32
        handle = k32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint()
        if k32.GetConsoleMode(handle, ctypes.byref(mode)):
            k32.SetConsoleMode(
                handle,
                ctypes.c_uint((mode.value & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS),
            )
    except Exception:
        pass  # a console tweak must never stop the client from starting


def run(args) -> int:
    """Entry point used by app.main() for the full-screen UI."""
    _disable_quickedit()
    cfg = providers.load()

    if getattr(args, "provider", "") and args.provider in cfg.providers:
        cfg.provider = args.provider
    if getattr(args, "api_key", ""):
        cfg.set_key(args.api_key)
    if getattr(args, "save", False) or getattr(args, "api_key", ""):
        try:
            providers.save(cfg)
        except OSError:
            pass

    model = args.model or cfg.model
    if not model:
        model = auto_model(cfg.base_url, cfg.api_key)

    if getattr(args, "echo", False):
        backend = EchoBackend()
    else:
        backend = OpenAICompatBackend(
            base_url=cfg.base_url, model=model or "", api_key=cfg.api_key,
            timeout=getattr(args, "timeout", 120.0),
            temperature=getattr(args, "temperature", 0.7),
            max_tokens=cfg.max_tokens)

    session = Session(
        model=model or getattr(backend, "name", "?"),
        where=cfg.base_url if not isinstance(backend, EchoBackend) else backend.where,
        system=args.system if args.system is not None else DEFAULT_SYSTEM,
    )
    # Restore the last conversation, then keep it persisted. This is what makes
    # "it keeps forgetting" stop: the transcript outlives the process.
    from . import history
    for role, content in history.load():
        if role == "user":
            session.messages.append(SessionMessage("user", content))
        elif role == "assistant":
            session.messages.append(SessionMessage("assistant", content))
    session.on_change = lambda s: history.save(s.messages)
    ForgeApp(args, cfg, backend, session).run()
    return 0
