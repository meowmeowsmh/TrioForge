"""The read-eval-print loop.

Layout note: the input is INLINE - it scrolls with the transcript - rather than
pinned to the bottom of the screen the way Claude Code pins its box. That is
deliberate: inline keeps the terminal's own scrollback working, so the mouse
wheel and Ctrl+Shift+Up still review old replies. A pinned input needs the
alternate screen, which throws the scrollback away.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.live import Live

from . import commands, providers, render, wizard
from . import theme as T
from . import __version__
from .backend import (BackendError, EchoBackend, OpenAICompatBackend,
                      auto_model, fetch_models, make_backend)
from .cli import DEFAULT_BASE_URL
from .session import Session

DEFAULT_SYSTEM = (
    "You are TrioForge, a precise, practical assistant running in the user's "
    "terminal. Answer in Markdown. Be concise unless asked to expand; show code "
    "in fenced blocks with the language tag."
)

HISTORY_FILE = Path.home() / ".trio_history"

PROMPT_STYLE = Style.from_dict({
    "prompt": "bold #7dcfff",
    "bottom-toolbar": "bg:#16161e #565f89",
    "hint": "#565f89 italic",
})


@dataclass
class Context:
    """What the slash commands are allowed to touch."""

    session: Session
    backend: object
    running: bool = True
    real_backend: object | None = None
    cfg: object | None = None      # providers.Config
    args: object | None = None     # parsed CLI args


def _make_prompt_session() -> PromptSession:
    kb = KeyBindings()

    @kb.add("c-c")
    def _interrupt(event):  # noqa: ANN001
        event.app.exit(exception=KeyboardInterrupt)

    @kb.add("c-d")
    def _eof(event):  # noqa: ANN001
        event.app.exit(exception=EOFError)

    return PromptSession(
        history=FileHistory(str(HISTORY_FILE)),
        key_bindings=kb,
        style=PROMPT_STYLE,
        complete_while_typing=False,
        enable_open_in_editor=True,   # Ctrl-X Ctrl-E opens $EDITOR for long prompts
        mouse_support=False,
    )


# Shown as greyed-out placeholder text in an empty input, the way Claude Code
# shows 'Try "fix lint errors"'.
#
# These are deliberately REAL questions you might want answered about THIS repo
# and THIS machine - not exercises. No "Try ..." prefix: an actual sentence is
# more inviting than an instruction, and it shows what the thing is for.
HINTS = [
    # about the app
    "explain this repo to me like I just woke up",
    "what would you change about py/features/?",
    "find something in here that will break later",
    "give me 3 features TrioForge is missing",
    "roast my folder structure",
    "what should I build next?",
    # about the machine / models
    "why is my model 6.8 GB and still slow?",
    "how much VRAM does gemma actually need?",
    "which of my models fits on a 8 GB card?",
    "make gemma answer faster",
    # playful
    "write a haiku about my swap usage",
    "describe this project as a movie trailer",
    "if this codebase were a person, what would it be like?",
    "summarise today's git diff in one sentence",
]


def _ask(prompt_session: PromptSession, ctx: Context) -> str:
    """Read one line. The status bar under the input is live."""
    import random

    hint = random.choice(HINTS)

    def toolbar():
        # bottom_toolbar accepts prompt_toolkit formatted text, so the strip can
        # be styled. (A plain str would lose the colours.)
        from prompt_toolkit.formatted_text import ANSI
        model = ctx.session.model or getattr(ctx.backend, "name", "?")
        if model.endswith(".gguf"):
            model = model.rsplit("/", 1)[-1][:-5]
        local = "127.0.0.1" in ctx.cfg.base_url or "localhost" in ctx.cfg.base_url
        with render.console().capture() as cap:
            render.console().print(render.status_bar(
                model, "local" if local else "cloud", ctx.session.turns))
        return ANSI(cap.get().rstrip("\n"))

    return prompt_session.prompt(
        HTML("<prompt>&gt;</prompt> "),
        placeholder=HTML(f"<hint>{hint}</hint>"),
        bottom_toolbar=toolbar,
    )


def _one_turn(ctx: Context, text: str) -> None:
    """Send one user message and stream the reply."""
    render.user(text)
    ctx.session.add_user(text)

    model = ctx.session.model or getattr(ctx.backend, "name", "?")
    if model.endswith(".gguf"):
        model = model.rsplit("/", 1)[-1][:-5]
    chunks: list[str] = []
    thoughts: list[str] = []
    started = time.time()
    try:
        # One card, updated as chunks arrive, LEFT in place at the end. Using a
        # separate final print left two cards on screen.
        with Live(
            render.assistant_card("", model, streaming=True),
            console=render.console(),
            refresh_per_second=12,
            transient=False,
            vertical_overflow="visible",
        ) as live:
            for kind, piece in ctx.backend.stream(ctx.session.payload()):
                if kind == "reasoning":
                    thoughts.append(piece)
                    # Show the chain of thought while it is being produced, in
                    # the same card, above the (still empty) answer.
                    live.update(render.assistant_card(
                        _format(thoughts, chunks), model,
                        elapsed=time.time() - started,
                        tokens=len("".join(chunks)) // 4, streaming=True))
                    continue
                chunks.append(piece)
                live.update(render.assistant_card(
                    _format(thoughts, chunks), model,
                    elapsed=time.time() - started,
                    tokens=len("".join(chunks)) // 4,
                    streaming=True))
    except BackendError as exc:
        ctx.session.drop_last()
        render.error(f"request failed: {exc}")
        render.info("check the server with /status, or type /echo to work offline")
        return
    except KeyboardInterrupt:
        ctx.session.drop_last()
        render.warn("cancelled")
        return

    elapsed = time.time() - started
    reply = _format(thoughts, chunks).strip()
    if not reply:
        ctx.session.drop_last()
        render.warn("the model returned nothing — try /status")
        return

    ctx.session.add_assistant(reply)
    # Redraw once without the cursor mark and with the final timing.
    render.console().print(render.assistant_card(
        reply, model, elapsed=elapsed, tokens=len(reply) // 4))


def _needs_setup(cfg) -> bool:
    """True when there is nothing usable to talk to yet."""
    if cfg.model:
        return False
    local = "127.0.0.1" in cfg.base_url or "localhost" in cfg.base_url
    if local:
        return not fetch_models(cfg.base_url, cfg.api_key)
    return not cfg.has_key()


def _format(thoughts: list[str], chunks: list[str]) -> str:
    """Reasoning as a quoted block above the answer, so the two never blur."""
    think = "".join(thoughts).strip()
    answer = "".join(chunks)
    if not think:
        return answer
    quoted = "\n".join("> " + l for l in think.splitlines() if l.strip())
    return f"> 🧠 **thinking**\n>\n{quoted}\n\n{answer}"


def run(args) -> int:
    # ---- resolve configuration: CLI flags win over the saved config ----------
    cfg = providers.load()

    if getattr(args, "provider", ""):
        if args.provider in cfg.providers:
            cfg.provider = args.provider
        else:
            render.warn(f"unknown provider {args.provider!r} — using {cfg.provider}")
    if getattr(args, "base_url", "") and args.base_url != DEFAULT_BASE_URL:
        cfg.set_base_url(args.base_url)
    if getattr(args, "api_key", ""):
        cfg.set_key(args.api_key)

    # An API key given once on the command line should not have to be retyped.
    if getattr(args, "save", False) or getattr(args, "api_key", ""):
        try:
            providers.save(cfg)
        except OSError:
            pass

    # If load() had to repair an old config, say so once - a silently changed
    # provider list is worse than a noisy one.
    for note in getattr(cfg, "notes", []):
        render.warn(f"config: {note}")

    warn = providers.check_permissions()
    if warn:
        render.warn(warn)

    # Guided setup: explicitly with --setup, or automatically the first time
    # when there is nothing reachable to talk to and no model chosen.
    if not getattr(args, "echo", False):
        if getattr(args, "setup", False) or _needs_setup(cfg):
            if not getattr(args, "setup", False):
                render.blank()
                render.info("no model is configured yet — let's set one up")
            try:
                wizard.setup(cfg)
            except wizard.Cancelled:
                render.warn("setup cancelled — nothing changed")
                return 130
            except Exception as exc:  # noqa: BLE001 - never crash on setup
                render.error(f"setup failed: {exc}")
                return 1

    # ---- pick a model --------------------------------------------------------
    model = args.model or cfg.model
    if not model:
        model = auto_model(cfg.base_url, cfg.api_key)
    if model:
        cfg.model = model

    if getattr(args, "echo", False):
        backend = EchoBackend()
    else:
        backend = OpenAICompatBackend(
            base_url=cfg.base_url,
            model=model or "",
            api_key=cfg.api_key,
            timeout=getattr(args, "timeout", 120.0),
            temperature=getattr(args, "temperature", 0.7),
        )

    session = Session(
        model=model or getattr(backend, "name", "?"),
        where=cfg.base_url if not isinstance(backend, EchoBackend) else backend.where,
        system=args.system if args.system is not None else DEFAULT_SYSTEM,
    )
    ctx = Context(session=session, backend=backend, real_backend=backend)
    ctx.cfg = cfg
    ctx.args = args

    if not args.no_banner:
        render.banner(session.model, session.where, __version__)

    # One-shot mode: `forge what is a GGUF?` answers and exits. Handy in scripts
    # and pipes, and it is the easiest way to test without a TTY.
    if args.prompt:
        _one_turn(ctx, " ".join(args.prompt))
        return 0

    # Full-screen Crush-style UI by default. It needs a real TTY, so anything
    # piped falls back to the scrolling UI rather than failing to start.
    use_full = (not getattr(args, "classic", False)
                and sys.stdout.isatty() and sys.stdin.isatty())
    if use_full:
        try:
            from . import textual_app
            return textual_app.run(args)
        except ImportError as exc:
            render.warn(f"full-screen UI unavailable ({exc}) — using the plain one")

    prompt_session = _make_prompt_session()

    while ctx.running:
        try:
            line = _ask(prompt_session, ctx)
        except KeyboardInterrupt:
            render.info("(Ctrl-D or /quit to leave)")
            continue
        except EOFError:
            break

        line = line.strip()
        if not line:
            continue
        if line.startswith("/"):
            commands.handle(line, ctx)
            continue
        _one_turn(ctx, line)

    render.info("bye")
    return 0


def main(argv: list[str] | None = None) -> int:
    from .cli import parse_args          # local import keeps startup fast

    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return run(args)
    except KeyboardInterrupt:
        return 130
