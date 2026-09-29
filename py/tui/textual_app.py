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

import subprocess
import time
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Input, Markdown, Static

from . import localmodels, providers, theme as T
from .backend import (BackendError, EchoBackend, OpenAICompatBackend,
                      auto_model, fetch_models)
from .session import Session

DEFAULT_SYSTEM = (
    "You are TrioForge, a precise, practical assistant running in the user's "
    "terminal. Answer in Markdown. Be concise unless asked to expand; show code "
    "in fenced blocks with the language tag."
)

KEYBINDS = (" esc cancel  ·  tab chat  ·  ctrl+p commands  ·  ctrl+l model  ·  "
            "shift+enter newline  ·  ctrl+n new  ·  ctrl+q quit")

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

/* The input keeps the SAME border when unfocused: a focus-coloured border made
   the line appear to break whenever focus moved with tab. Focus is shown by the
   block cursor instead, which does not disturb the frame. */
#prompt {{
    margin: 0 1;
    border: round {T.PURPLE};
    background: #16161e;
    color: {T.WHITE};
    padding: 0 1;
    height: 3;
}}
#prompt:focus {{
    border: round {T.PURPLE};
    background: #1a1b26;
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


class ForgeApp(App):
    """Full-screen TrioForge client, laid out like Crush."""

    CSS = CSS
    TITLE = "TrioForge"

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+l", "pick_model", "Model"),
        ("ctrl+n", "new_session", "New"),
        ("ctrl+p", "show_help", "Commands"),
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
        self._last_tick = 0.0
        self._activity = ""       # shown in the sidebar while a turn runs
        self._thinking = ""

    # ------------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            with VerticalScroll(id="chat"):
                yield Static(" ", id="spacer")
            with Vertical(id="side"):
                yield Static(self._sidebar(), id="sidebody")
        # The prompt and the keybind bar go in ONE docked container. Docking both
        # separately let the keybind bar overlap the prompt's bottom border,
        # which is why the purple line looked cut off.
        with Vertical(id="footer"):
            yield Input(placeholder=self._hint(), id="prompt")
            yield Static(KEYBINDS, id="keys")

    def on_mount(self) -> None:
        self.query_one("#prompt", Input).focus()
        self._greet()

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
        if self._busy and self._activity:
            out.append(f"{self._activity}\n", style=f"bold {T.YELLOW}")
        out.append("\n")

        # ---- offline models on disk
        models = localmodels.available()
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
        try:
            r = subprocess.run(["git", "status", "--short"], cwd=Path.cwd(),
                               capture_output=True, text=True, timeout=3)
            return [l for l in r.stdout.splitlines() if l.strip()][:5]
        except Exception:  # noqa: BLE001 - git absent, or not a repo
            return []

    def _elapsed(self) -> str:
        secs = int(time.time() - self._started)
        return f"{secs // 60}m{secs % 60:02d}s" if secs >= 60 else f"{secs}s"

    # ------------------------------------------------------------------ helpers
    def _short_model(self) -> str:
        m = self.session.model or getattr(self.backend, "name", "?")
        if m.endswith(".gguf"):
            m = m.rsplit("/", 1)[-1][:-5]
        return m or "no model"

    def _is_local(self) -> bool:
        url = self.cfg.base_url
        return "127.0.0.1" in url or "localhost" in url

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

    # ------------------------------------------------------------------ events
    @on(Input.Submitted, "#prompt")
    def _submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        self.query_one("#prompt", Input).value = ""
        if not text or self._busy:
            return
        if text.startswith("/"):
            self._command(text)
            self._refresh()
            return
        self.session.add_user(text)
        self._add(text, "user")
        self._ask(text)

    # ------------------------------------------------------------------- reply
    @work(exclusive=True)
    async def _ask(self, text: str) -> None:
        self._busy = True
        model = self._short_model()
        card = self._add("", "bot")
        started = time.time()
        chunks: list[str] = []
        thoughts: list[str] = []
        try:
            async for kind, piece in self._stream():
                if kind == "reasoning":
                    thoughts.append(piece)
                    self._thinking = "".join(thoughts)
                else:
                    chunks.append(piece)
                    self._thinking = ""       # answer started: reasoning is done
                card.update(self._compose(thoughts, chunks))
                self.query_one("#chat", VerticalScroll).scroll_end(animate=False)
                self._tick(started, len("".join(chunks)) // 4)
            reply = self._compose(thoughts, chunks).strip()
            elapsed = time.time() - started
            if not reply:
                card.update("*the model returned nothing — ctrl+p → /status*")
            else:
                self.session.add_assistant(reply)
                card.update(f"{reply}\n\n---\n_{model} · {elapsed:.1f}s · "
                            f"{len(reply) // 4} tok_")
        except BackendError as exc:
            card.update(f"**request failed** — {exc}\n\n"
                        f"_check the endpoint, or run with `--echo`_")
        except Exception as exc:  # noqa: BLE001
            card.update(f"**unexpected error** — {exc}")
        finally:
            self._busy = False
            self._refresh()
            self.query_one("#prompt", Input).focus()

    def _compose(self, thoughts: list[str], chunks: list[str]) -> str:
        """Chain of thought as a quote block, above the answer.

        Quoted rather than merged, so it is always obvious which text is the
        model thinking and which is the reply.
        """
        think = "".join(thoughts).strip()
        answer = "".join(chunks)
        if not think:
            return answer
        quoted = "\n".join("> " + l for l in think.splitlines() if l.strip())
        return f"> 🧠 **thinking**\n>\n{quoted}\n\n{answer}"

    def _tick(self, started: float, tokens: int) -> None:
        """Cheap live status, throttled - updating the sidebar per chunk is too
        expensive, and the user only needs it to change about twice a second."""
        now = time.time()
        if now - self._last_tick < 0.5:
            return
        self._last_tick = now
        secs = now - started
        self._activity = (f"{self._spinner()}  {secs:4.1f}s  ·  "
                          f"{tokens} tok"
                          + (f"  ·  {tokens / secs:.0f} tok/s" if secs > 0.6 else ""))
        self._refresh()

    def _spinner(self) -> str:
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        return frames[int(time.time() * 10) % len(frames)]

    async def _stream(self):
        """Run the blocking backend stream in a worker thread."""
        import asyncio

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        payload = self.session.payload()

        def produce():
            try:
                for piece in self.backend.stream(payload):
                    loop.call_soon_threadsafe(queue.put_nowait, ("chunk", piece))
            except Exception as exc:  # noqa: BLE001
                loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))

        loop.run_in_executor(None, produce)
        while True:
            kind, value = await queue.get()
            if kind == "chunk":
                yield value
            elif kind == "error":
                raise value
            else:
                return

    # --------------------------------------------------------------- commands
    def _command(self, line: str) -> None:
        """Slash commands: the SAME dispatch table the plain UI uses."""
        from . import commands as C

        name, _, arg = line.partition(" ")
        arg = arg.strip()

        if name in ("/quit", "/exit", "/q"):
            self.exit()
            return
        if name == "/clear":
            self.action_new_session()
            return
        if name == "/model" and not arg:
            self.action_pick_model()
            return
        if name in ("/help", "/?"):
            self.action_show_help()
            return

        class _Ctx:
            pass

        ctx = _Ctx()
        ctx.session, ctx.cfg, ctx.args = self.session, self.cfg, self.args
        ctx.backend, ctx.real_backend = self.backend, self.real_backend
        ctx.running = True
        try:
            C.handle(line, ctx)
        except Exception as exc:  # noqa: BLE001
            self._add(f"command failed: {exc}", "bot")
            return
        self.backend, self.real_backend = ctx.backend, ctx.real_backend

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
        self.query_one("#prompt", Input).focus()

    def action_pick_model(self) -> None:
        names = fetch_models(self.cfg.base_url, self.cfg.api_key) \
            or list(self.cfg.current.get("models") or [])
        if not names:
            names = [m.name for m in localmodels.available()]
        if not names:
            self._add("_no models to switch to — ctrl+p → /setup_", "bot")
            return
        cur = self._short_model()
        idx = (names.index(cur) + 1) % len(names) if cur in names else 0
        self.session.model = names[idx]
        self.cfg.model = names[idx]
        if not isinstance(self.backend, EchoBackend):
            self.backend = OpenAICompatBackend(
                base_url=self.cfg.base_url, model=names[idx],
                api_key=self.cfg.api_key,
                timeout=getattr(self.args, "timeout", 120.0),
                temperature=getattr(self.args, "temperature", 0.7))
            self.real_backend = self.backend
        providers.save(self.cfg)
        self._add(f"_model → **{names[idx]}**_", "bot")
        self._refresh()


def run(args) -> int:
    """Entry point used by app.main() for the full-screen UI."""
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
