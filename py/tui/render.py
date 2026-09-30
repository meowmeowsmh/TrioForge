"""All terminal output goes through here.

Keeping every ``print`` in one module means the look is consistent and there is
exactly one place to change it. Nothing here reads input - that is app.py.
"""

from __future__ import annotations

import shutil

from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from . import theme as T
from . import faces

_console: Console | None = None


def console() -> Console:
    """One shared Console so the theme and width are decided once."""
    global _console
    if _console is None:
        _console = Console(theme=T.THEME, highlight=False)
    return _console


def width() -> int:
    return console().width


def _chip(label: str, bg: str, fg: str = "#16161e") -> Text:
    """A small filled label, like a pill: ' you ' on a coloured background."""
    return Text(f" {label} ", style=f"bold {fg} on {bg}")


def _wordmark() -> Text:
    """The TRIO wordmark, painted with a left-to-right gradient."""
    out = Text()
    lines = T.LOGO
    cols = max(len(l) for l in lines) or 1
    stops = T.GRADIENT
    for li, line in enumerate(lines):
        for ci, ch in enumerate(line):
            if ch == " ":
                out.append(" ")
                continue
            idx = min(len(stops) - 1, int(ci / max(1, cols - 1) * (len(stops) - 1)))
            out.append(ch, style=f"bold {stops[idx]}")
        if li != len(lines) - 1:
            out.append("\n")
    return out


def _gradient_rule(cols: int | None = None) -> Text:
    """A horizontal rule that fades left to right.

    The parameter is named `cols`, not `width` - naming it `width` shadowed the
    module-level width() function and made it uncallable inside here.
    """
    cols = max(1, min(cols or width(), width()))
    stops = T.gradient_stops(T.BLUE, cols)
    out = Text()
    for i in range(cols):
        out.append("─", style=stops[i])
    return out


def banner(model: str, where: str, version: str = "") -> None:
    """The startup card: gradient wordmark, context beside it, fading rule under."""
    from pathlib import Path

    c = console()
    try:
        rel = Path.cwd().relative_to(Path.home())
        cwd = "~" if str(rel) == "." else f"~/{rel}"
    except ValueError:
        cwd = str(Path.cwd())

    shown_model = model
    if model and model.endswith(".gguf"):
        import os as _os
        shown_model = _os.path.basename(model)[:-5]

    cloud = not (where.startswith("http://127") or where.startswith("http://local"))
    kind = "cloud" if cloud else "local"

    info = Text()
    info.append("TrioForge", style=f"bold {T.WHITE}")
    info.append(f"  v{version}\n", style=T.GREY)
    info.append(shown_model or "no model", style=f"bold {T.GREEN}")
    info.append("\n")
    info.append(f"{T.ICON_CHIP}  {kind}  {T.GLYPH_DOT}  {where}\n", style=T.GREY)
    info.append(f"{T.ICON_FOLDER}  {cwd}", style=T.YELLOW)
    info.append("\n")
    info.append(f"{T.ICON_BOLT}  /help for commands", style=f"{T.PURPLE}")

    dead = Text()
    dead.append(faces.face("dead"), style=T.GREY)
    dead.append("\n")
    dead.append(f"{faces.emoji('dead')} dead", style=T.GREY)

    grid = Table.grid(padding=(0, 3))
    grid.add_column(no_wrap=True)
    grid.add_column()
    grid.add_column(no_wrap=True, justify="right")
    grid.add_row(_wordmark(), info, dead)

    c.print()
    c.print(grid)
    c.print(_gradient_rule(width()))
    c.print()


def user(text: str) -> None:
    """The user's turn, in a bordered card."""
    from rich.box import ROUNDED

    c = console()
    c.print()
    c.print(Panel(
        Text(text, style=T.WHITE),
        box=ROUNDED,
        border_style=T.CYAN,
        title=f"[bold {T.CYAN}] you [/]",
        title_align="left",
        padding=(0, 1),
    ))


def assistant_card(text: str, model: str = "", elapsed: float = 0.0,
                   tokens: int = 0, streaming: bool = False) -> Panel:
    """Build the assistant's card. Returned, not printed, so Live can update it.

    One card is used for both the streaming view and the final output: spawning a
    second card at the end left BOTH on screen.
    """
    from rich.box import ROUNDED

    bits = []
    if model:
        bits.append(model)
    if elapsed > 0.2:
        bits.append(f"{elapsed:.1f}s")
    if tokens and elapsed > 0.2:
        bits.append(f"{tokens} tok · {tokens / elapsed:.0f} tok/s")

    label = f"[bold {T.GREEN}] {T.ICON_CHAT} trio [/]"
    if bits:
        label += f"[{T.GREY}] {' · '.join(bits)} [/]"

    if text.strip():
        body = Markdown(text + (" ▌" if streaming else ""),
                        code_theme="monokai", inline_code_theme="monokai")
    else:
        body = Text("thinking…", style=f"dim {T.PURPLE}")

    return Panel(body, box=ROUNDED, border_style=T.GREEN, title=label,
                 title_align="left", padding=(0, 1))


def assistant(text: str, model: str = "", elapsed: float = 0.0,
              tokens: int = 0) -> None:
    """Print a finished assistant turn (one-shot mode)."""
    c = console()
    c.print()
    c.print(assistant_card(text, model, elapsed, tokens))


def status_bar(model: str, kind: str, turns: int, context: str = "") -> Text:
    """The always-visible strip under the input."""
    cols = width()
    left = Text()
    left.append(f" {T.ICON_BOLT} ", style=f"bold {T.PURPLE}")
    left.append(model or "no model", style=f"bold {T.GREEN}")
    left.append(f"  {T.GLYPH_DOT}  {kind}", style=T.GREY)
    if context:
        left.append(f"  {T.GLYPH_DOT}  {context}", style=T.GREY)

    right = Text()
    right.append(f"{turns} turns", style=T.GREY)
    right.append(f"  {T.GLYPH_DOT}  ", style=T.GREY)
    right.append("/help", style=f"bold {T.PURPLE}")
    right.append(" ")

    gap = max(1, cols - len(left.plain) - len(right.plain))
    out = Text()
    out.append_text(left)
    out.append(" " * gap)
    out.append_text(right)
    return out


def stream_view(text: str, model: str) -> Panel:
    """A live-updating panel used while the reply is streaming in."""
    body = Markdown(text + " ▌", code_theme="monokai", inline_code_theme="monokai") \
        if text.strip() else Text("thinking…", style="dim")
    return Panel(
        body,
        title=f"[assistant]{T.GLYPH_BOT} {T.TAG_BOT}[/] [dim]{model}[/]",
        title_align="left",
        border_style=T.GREY,
        padding=(0, 1),
    )


def info(msg: str) -> None:
    console().print(f"[dim]{T.GLYPH_DOT} {msg}[/]")


def ok_line(msg: str) -> None:
    console().print(f"  [ok]\u2714[/] {msg}")


def error_line(msg: str) -> None:
    console().print(f"  [error]\u2716[/] {msg}")


def blank() -> None:
    console().print()


def heading(text: str) -> None:
    """A section title for the setup wizard."""
    console().print(f"[bold {T.PURPLE}]{text}[/]")
    console().print(Rule(style=T.GREY))


def option(num: str, name: str, desc: str, current: bool = False) -> None:
    """One row of a wizard menu."""
    c = console()
    if current:
        c.print(f"  [{T.GREEN}]▶[/] [bold {T.CYAN}]{num}[/]  "
                f"[bold {T.FG}]{name}[/]  [dim]{desc}[/]")
    else:
        c.print(f"    [{T.GREY}]{num}[/]  [fg]{name}[/]  [dim]{desc}[/]")


def ok(msg: str) -> None:
    console().print(f"[ok]{T.GLYPH_OK}[/] {msg}")


def warn(msg: str) -> None:
    console().print(f"[warn]{T.GLYPH_WARN}[/] {msg}")


def error(msg: str) -> None:
    console().print(f"[error]{T.GLYPH_ERR} {msg}[/]")


def rule() -> None:
    console().print(Rule(style=T.GREY))


def help_panel(rows: list[tuple[str, str]]) -> None:
    body = Text()
    for cmd, desc in rows:
        body.append(f"  {cmd:<20}", style="cmd")
        body.append(f"{desc}\n", style="fg")
    console().print(Panel(body, title="[cmd]commands[/]", title_align="left",
                          border_style=T.GREY, padding=(0, 1)))


def table(title: str, rows: list[tuple[str, str]]) -> None:
    from rich.table import Table

    t = Table(title=title, title_justify="left", border_style=T.GREY,
              show_header=False, box=None, padding=(0, 2))
    t.add_column(style="dim", justify="right")
    t.add_column(style="fg")
    for k, v in rows:
        t.add_row(k, v)
    console().print(t)


def status_line(model: str, where: str, turns: int) -> str:
    """The string shown under the input box."""
    cols = shutil.get_terminal_size((80, 24)).columns
    left = f"{T.GLYPH_PROMPT} ask anything"
    right = f"{model} {T.GLYPH_DOT} {turns} turns {T.GLYPH_DOT} {where}"
    gap = max(1, cols - len(left) - len(right) - 4)
    return f"[dim]{left}{' ' * gap}{right}[/]"


def clear() -> None:
    console().clear()
