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
    | esc cancel · tab chat · ctrl+p commands · ctrl+q quit     |
    +----------------------------------------------------------+

Everything below the widget layer - backend.py, providers.py, session.py,
localmodels.py - is unchanged and UI-agnostic, which is why this could be added
without touching how a request is actually made.
"""

from __future__ import annotations

import os
import random
import subprocess
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
from . import faces, localmodels, providers, theme as T
from .backend import (EchoBackend, OpenAICompatBackend, auto_model,
                      fetch_models)
from .commands import COMMANDS  # built once so the first ctrl+p is instant
from .session import Session

DEFAULT_SYSTEM = (
    "You are TrioForge, a precise, practical assistant running in the user's "
    "terminal. Answer in Markdown. Be concise unless asked to expand; show code "
    "in fenced blocks with the language tag."
)

KEYBINDS = (" enter send  ·  ctrl+j newline  ·  ctrl+y copy answer  ·  tab chat  ·  "
            "ctrl+p commands  ·  ctrl+l model  ·  ctrl+n new  ·  ctrl+q quit")

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
    scrollbar-color: {T.PURPLE};
    scrollbar-background: {T.BG};
    scrollbar-size-vertical: 1;
}}

#side {{
    width: 36;
    padding: 0 1;
    background: #16161e;
    border-left: solid #2f3549;
    color: {T.GREY};
}}

/* width: 1fr (not auto) so text WRAPS inside the pane instead of
   growing past it and getting clipped. */
.msg {{ margin: 1 0 0 0; padding: 0 1; width: 1fr; }}
.from-user {{ border: round {T.CYAN}; color: {T.WHITE}; }}
.from-bot  {{ border: round {T.GREEN}; }}
.role {{ color: {T.GREY}; }}
.thinking {{ color: {T.GREY}; }}

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

#footer {{
    dock: bottom;
    height: 4;
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
        # ctrl+c stays "quit" for when nothing is selected; the Screen binds it
        # to copy_text first and that raises SkipAction without a selection, so
        # selecting text with the mouse and pressing ctrl+c copies instead.
        ("ctrl+c", "quit", "Quit/copy"),
        ("ctrl+y", "copy_reply", "Copy answer"),
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
        self._tool_cards: dict = {}
        self._active = None
        self._spinner_i = 0
        self._git_cache: list[str] = []
        self._git_ts = 0.0
        self._models_cache: list = []
        self._models_ts = 0.0
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
            if spec.get("gpu_name"):
                out.append(f"{_short_gpu(spec['gpu_name'])}\n", style=T.FG)
                kind = "unified" if spec.get("gpu_unified") else "vram"
                out.append(f"{spec.get('vram_total_gb', 0):.1f} GB {kind}", style=T.GREY)
                if spec.get("gpu_multi"):
                    out.append("  ·  2 GPUs", style=T.YELLOW)
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
            has = bool(self.cfg.providers[name].get("api_key"))
            current = name == self.cfg.provider
            out.append("● " if has else "○ ",
                       style=T.GREEN if has else (T.YELLOW if current else "#414868"))
            out.append(f"{name}", style=f"bold {T.FG}" if current else T.GREY)
            out.append(f"  {'key' if has else '—'}\n", style="#414868")
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
            self.query_one("#sidebody", Static).update(self._sidebar())

    # ------------------------------------------------------------------ helpers
    def _short_model(self) -> str:
        m = self.session.model or getattr(self.backend, "name", "?")
        if m.endswith(".gguf"):
            m = m.rsplit("/", 1)[-1][:-5]
        return m or "no model"

    def _is_local(self) -> bool:
        url = self.cfg.base_url
        return "127.0.0.1" in url or "localhost" in url

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
        self.query_one("#sidebody", Static).update(self._sidebar())
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

    def _add(self, text: str, role: str) -> Markdown:
        chat = self.query_one("#chat", VerticalScroll)
        card = Markdown(text or "…", classes=f"msg from-{role}")
        chat.mount(card)
        chat.scroll_end(animate=False)
        return card

    def _add_plain(self, content, role: str = "bot") -> Static:
        """A transcript card for text that is already laid out - Markdown would
        reflow a panel or a table and destroy its alignment."""
        chat = self.query_one("#chat", VerticalScroll)
        card = Static(content, classes=f"msg from-{role}")
        chat.mount(card)
        chat.scroll_end(animate=False)
        return card

    # ------------------------------------------------------------------ events
    @on(PromptArea.CopyRequested, "#prompt")
    def _on_copy_requested(self, event: PromptArea.CopyRequested) -> None:
        event.stop()
        self.action_copy_reply()

    @on(PromptArea.Submitted, "#prompt")
    def _submitted(self, event: PromptArea.Submitted) -> None:
        text = (event.value or "").strip()
        self.query_one("#prompt", PromptArea).text = ""
        if not text or self._busy:
            return
        if text.startswith("/"):
            self._command(text)
            self._refresh()
            return
        self.session.add_user(text)
        self._add(text, "user")
        self._ask(text)

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
            self._add_plain("nothing to copy yet — ask something first")
            return
        which = max(1, min(which, len(self._replies)))
        text = self._replies[-which]
        self._copy_text(text)
        turn = "last answer" if which == 1 else f"answer {which} back"
        self._add_plain(f"copied the {turn} — {len(text)} characters, "
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
        self._set_mood("thinking")
        started = time.time()
        card = self._add("", "bot")
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
            turn = await self._run_agent(on_event)
            final = turn.text.strip()
            if final:
                self.session.add_assistant(final)
                # Kept so the answer can be copied without selecting it by hand.
                self._replies.append(final)
                await state["card"].update(
                    f"{final}\n\n---\n_{self._short_model()} · "
                    f"{time.time() - started:.1f}s · {len(turn.steps)} tool"
                    f"{'s' if len(turn.steps) != 1 else ''}_")
            elif turn.steps:
                await state["card"].update(
                    f"_finished after {len(turn.steps)} tool call(s), no summary_")
            self._set_mood("happy")
        except Exception as exc:  # noqa: BLE001
            await state["card"].update(f"**unexpected error** — {exc}")
            self._set_mood("sad")
        finally:
            self._busy = False
            self._active = None
            if state.get("think") is not None:
                state["think"].title = f"Thinking… ({len(state['reasoning'])} chars)"
            self._refresh()
            self.query_one("#prompt", PromptArea).focus()

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
                state["think_body"].update(state["reasoning"])
        elif kind == "content":
            state["text"] += payload["text"]
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
        elif kind == "error":
            await state["card"].update(f"**request failed** — {payload['message']}")
            self._set_mood("sad")
        state["tokens"] = len(state["text"]) // 4
        self.query_one("#chat", VerticalScroll).scroll_end(animate=False)
        self._refresh_activity()

    async def _run_agent(self, on_event):
        """The agent's ``turn`` is blocking, so it runs in a thread."""
        import asyncio

        loop = asyncio.get_running_loop()
        result: dict = {}

        def work():
            try:
                ag = agent_mod.Agent(
                    self.backend, self.session,
                    use_tools=True,
                    native_tools=agent_mod.supports_native_tools(self.backend),
                    approve=self._approve_blocking,
                    persist=lambda: providers.save(self.cfg),
                )
                result["turn"] = ag.turn(on_event)
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
        chat.scroll_end(animate=False)
        self._tool_cards[name] = card

    async def _finish_tool_card(self, name: str, output: str, denied: bool) -> None:
        card = self._tool_cards.get(name)
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
            self.action_show_help()
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
                self._add_plain("usage: /copy [how many answers back, e.g. /copy 2]")
                return
            self._copy_reply(n)
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
        self._refresh()

    # ---------------------------------------------------------------- actions
    def action_show_help(self) -> None:
        from .commands import COMMANDS
        # A Markdown LIST, with a blank line around it. Joining plain lines with
        # \n made Markdown treat them as ONE paragraph, so every command ran
        # together on one line and the tail was clipped.
        rows = "\n".join(f"- `{c}` — {d}" for c, d in COMMANDS)
        self._add(f"### commands\n\n{rows}\n", "bot")

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
            options.append((m.name, f"{m.size_gb:.1f} GB · {m.caps_label} · local"))
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
        if not isinstance(self.backend, EchoBackend):
            self.backend = OpenAICompatBackend(
                base_url=self.cfg.base_url, model=self.cfg.model,
                api_key=self.cfg.api_key,
                timeout=getattr(self.args, "timeout", 120.0),
                temperature=getattr(self.args, "temperature", 0.7))
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
            has = "key" if entry.get("api_key") else "no key"
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
                temperature=getattr(self.args, "temperature", 0.7))
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
        options = [(m.name, f"{m.size_gb:.1f} GB · {m.caps_label}")
                   for m in localmodels.available()]
        if not options:
            self._add("_no offline .gguf models found_", "bot")
            return
        choice = await self.push_screen_wait(Picker("load which offline model?", options))
        if not choice:
            return
        self._command(f"/start {choice}")

    def action_palette(self) -> None:
        """ctrl+p - searchable command palette, instead of remembering names."""
        from .commands import COMMANDS
        self.run_worker(self._palette(COMMANDS), exclusive=False)

    async def _palette(self, commands) -> None:
        options = [(c.split()[0], d) for c, d in commands]
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
            options = [(m.name, f"{m.size_gb:.1f} GB · {m.caps_label}")
                       for m in localmodels.available()]
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
            temperature=getattr(args, "temperature", 0.7))

    session = Session(
        model=model or getattr(backend, "name", "?"),
        where=cfg.base_url if not isinstance(backend, EchoBackend) else backend.where,
        system=args.system if args.system is not None else DEFAULT_SYSTEM,
    )
    ForgeApp(args, cfg, backend, session).run()
    return 0
