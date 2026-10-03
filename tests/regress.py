#!/usr/bin/env python3
"""Consolidated regression tests for TrioForge.

    .venv-linux/bin/python tests/regress.py

No pytest, no network, no GPU, no model: every test drives the code with fakes
and asserts the behaviour that has actually broken before. Run it before a
commit; CI runs it too.

The tests live in the repository rather than in /tmp because /tmp gets wiped
(three times in one session), and a regression suite that evaporates is not a
regression suite.

Each test prints a line when it passes, so a failure is obvious about WHERE.
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "py"))

GB = 1073741824


def _title(name: str) -> None:
    print(f"\n== {name} ==")


# ---------------------------------------------------------------------------
# tools.parse_text_calls — the wire format the text protocol depends on
# ---------------------------------------------------------------------------
def test_parse_text_calls() -> None:
    _title("tool-call parsing")
    from tui.tools import parse_text_calls

    cases = [
        # the documented nesting
        ('```tool\n{"name": "memory", "args": {"action": "remember", "key": "k"}}\n```',
         [("memory", {"action": "remember", "key": "k"})]),
        # FLAT: what small models actually emit. This used to hand the tool an
        # empty dict, so a whole round trip was wasted working out the nesting.
        ('```tool\n{"name": "memory", "action": "remember", "key": "user_name", "value": "Tze Cherng"}\n```',
         [("memory", {"action": "remember", "key": "user_name", "value": "Tze Cherng"})]),
        # arguments as a JSON string
        ('```tool\n{"name": "bash", "arguments": "{\\"command\\": \\"ls\\"}"}\n```',
         [("bash", {"command": "ls"})]),
        # no arguments at all
        ('```tool\n{"name": "specs"}\n```', [("specs", {})]),
        # a bare object in prose is NOT a call - the fence is required
        ('{"name": "grep", "pattern": "x"}', []),
    ]
    for text, want in cases:
        got = parse_text_calls(text)
        assert got == want, f"{text!r} -> {got!r}, wanted {want!r}"
    print("  nested, flat, json-string, no-args, unfenced -> OK")


# ---------------------------------------------------------------------------
# agent — cancellation and the wire ids
# ---------------------------------------------------------------------------
def test_agent_cancel() -> None:
    _title("agent cancellation")
    from tui.agent import Agent
    from tui.session import Session

    class Stream:
        model = "fake"
        def __init__(self, flag=None, n=50):
            self.flag, self.n = flag, n
        def stream(self, messages):
            for i in range(self.n):
                yield "content", f"c{i}"
                if self.flag is not None and i == 4:
                    self.flag["stop"] = True

    def agent(backend):
        return Agent(backend, Session(model="f", where="local", system="s"),
                     use_tools=False, native_tools=False,
                     approve=lambda *a: True, persist=None)

    t = agent(Stream()).turn(lambda k, p: None, should_stop=lambda: True)
    assert t.stopped and t.text == "", (t.stopped, t.text)

    flag = {"stop": False}
    t = agent(Stream(flag)).turn(lambda k, p: None, should_stop=lambda: flag["stop"])
    assert t.stopped and t.text == "c0c1c2c3c4", (t.stopped, t.text)

    t = agent(Stream(None, 8)).turn(lambda k, p: None)
    assert not t.stopped and t.text == "".join(f"c{i}" for i in range(8))
    print("  stop-before / stop-mid-stream / no-stop -> OK")


def test_agent_wire_ids() -> None:
    _title("agent tool-call ids")
    from tui.agent import Agent

    ag = Agent.__new__(Agent)
    ag.native_tools = True
    msg = ag._assistant_message("", [("bash", {}), ("view", {})])
    assert msg["tool_calls"][0]["id"] == "call_0"
    assert msg["tool_calls"][1]["id"] == "call_1"
    # the result must name the same id, or hosted APIs reject it outright
    assert ag._tool_message("bash", "o", 0)["tool_call_id"] == "call_0"
    assert ag._tool_message("view", "o", 1)["tool_call_id"] == "call_1"
    print("  tool_call_id matches the assistant message -> OK")


def test_known_facts() -> None:
    _title("known facts in the system prompt")
    import memory
    from tui.agent import Agent
    from tui.session import Session

    class B:
        model = "m"; base_url = "http://127.0.0.1:1/v1"; tools = []

    BLOCK = "Facts the user explicitly asked to be remembered"

    def prompt(rows, available=True):
        memory.entries = lambda prefix="", limit=40: rows
        memory.available = lambda: available
        return Agent(B(), Session(model="m", where="local", system=""),
                     use_tools=True, native_tools=False,
                     approve=lambda *a: True, persist=None).system_prompt()

    p = prompt([("user_name", "Tze Cherng")])
    assert BLOCK in p and "user_name: Tze Cherng" in p
    assert BLOCK not in prompt([])                     # empty vault adds nothing
    assert BLOCK not in prompt([("x", "y")], available=False)

    p = prompt([(f"k{i}", "v" * 500) for i in range(200)])
    block = p.split(BLOCK)[1]
    lines = [l for l in block.splitlines() if l.startswith("- ")]
    assert len(lines) <= 40 and len(block) <= 4300, (len(lines), len(block))
    print("  injected, bounded, silent when empty/broken -> OK")


def test_memory_is_optin() -> None:
    """Memory must never be written on the model's own initiative.

    Both the system rule and the tool description used to say "store durable
    facts the user tells you about themselves", which reads as an instruction to
    save things nobody asked to keep - and an unasked-for memory then appears in
    every later conversation.
    """
    _title("memory is opt-in")
    from tui import agent as A
    from tui.tools import TOOLS

    prompt = A.SYSTEM_PROMPT
    assert "MEMORY IS OPT-IN" in prompt, "the rule is gone"
    assert "Never record anything on your own initiative" in prompt
    assert "explicitly asks you to remember" in prompt
    # the shorthand the user actually types must be a recognised trigger
    for word in ('"remember"', '"rmb"', '"note this down"'):
        assert word in prompt, word
    assert "is NOT an instruction to store" in prompt   # wraps across a line
    # the old wording that invited auto-saving must not come back
    assert "When the user states something lasting" not in prompt

    desc = TOOLS["memory"].description
    assert "ONLY when the" in desc and "user asks you to remember" in desc, desc[:90]
    assert "durable facts the user tells you" not in desc
    print("  system rule + tool description both say 'only when asked' -> OK")


# ---------------------------------------------------------------------------
# llamacpp_service — where the weights will live
# ---------------------------------------------------------------------------
def test_plan_load() -> None:
    _title("GPU/CPU load plan")
    import llamacpp_service as svc

    # the real case that prompted this: 8.17 GB model + 1.57 GB projector,
    # 7.51 GB VRAM free -> about half the weights end up on the CPU
    big = svc._plan_load(int(8.17 * GB), int(7.51 * GB), int(11.96 * GB),
                         int(1.57 * GB), ngl_auto=True)
    assert big["split"], big
    assert big["gpu_bytes"] + big["cpu_bytes"] == int(8.17 * GB), big

    small = svc._plan_load(int(3 * GB), int(9 * GB), int(16 * GB), 0, ngl_auto=True)
    assert small["offload"] and not small["split"] and small["cpu_bytes"] == 0, small
    print(f"  8.17 GB on 7.51 GB free -> split, {big['cpu_bytes'] / GB:.1f} GB on the CPU")
    print("  a model that fits -> offload, 0 GB on the CPU -> OK")


# ---------------------------------------------------------------------------
# the terminal UI
# ---------------------------------------------------------------------------
async def _tui_checks() -> None:
    _title("terminal client")
    from tui import providers, session as sess_mod, textual_app as ta
    from tui.backend import OpenAICompatBackend

    cfg = providers.load()
    args = SimpleNamespace(timeout=5.0, temperature=0.7, system=None)
    b = OpenAICompatBackend("http://127.0.0.1:8080/v1", "gemma", api_key="")
    app = ta.ForgeApp(args, cfg, b,
                      sess_mod.Session(model="gemma", where="local", system="s"))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.4)

        # every wrapper is mounted, and the keybind bar sits ON SCREEN - a fixed
        # footer height once pushed it one row below the viewport
        for sel in ("#chat", "#side", "#sidebody", "#face", "#activity",
                    "#statusline", "#prompt", "#keys"):
            app.query_one(sel)
        keys = app.query_one("#keys")
        assert keys.region.y + keys.region.height <= 40, keys.region
        print("  all widgets mount; #keys is on screen -> OK")

        # /help lists the terminal-only commands with an example
        app._command("/help")
        await pilot.pause(0.5)
        body = _last_card(app)
        for want in ("/team", "/auto", "/route", "e.g. /team"):
            assert want in body, want
        app._command("/help /team")
        await pilot.pause(0.4)
        assert "/team" in _last_card(app)
        print("  /help lists /team,/auto,/route + examples; /help <cmd> -> OK")

        # meta lines are dim, not green bot bubbles
        assert "from-meta" in app._add_meta("x").classes
        assert "from-bot" in app._add_plain("x").classes
        print("  meta lines are not bot bubbles -> OK")

        # a split load warns, a fitting one does not
        import llamacpp_service as svc
        app.session.model = "nemotron"
        app._load_plan = svc._plan_load(int(8.17 * GB), int(7.51 * GB),
                                        int(11.96 * GB), int(1.57 * GB), ngl_auto=True)
        app._warn_if_slow_load()
        await pilot.pause(0.3)
        assert "has to run on the CPU" in _last_card(app)
        app._warned_load = None
        app._load_plan = svc._plan_load(int(6.8 * GB), int(7.51 * GB),
                                        int(11.96 * GB), 0, ngl_auto=True)
        n = len(_cards(app))
        app._warn_if_slow_load()
        await pilot.pause(0.3)
        assert len(_cards(app)) == n, "warned about a model that fits"
        print("  split load warns once; a fitting model is left alone -> OK")

        # the scrollbar is continuous and the layout does not shift
        chat = app.query_one("#chat", ta.VerticalScroll)
        card = app.query_one("#chat Markdown")
        fits = (chat.virtual_size.width, card.size.width, card.region.x)
        for n2 in range(30):
            app._add_plain(f"filler {n2}")
        await pilot.pause(0.7)
        over = (chat.virtual_size.width, card.size.width, card.region.x)
        assert fits == over, ("the layout shifts when the scrollbar appears", fits, over)
        sb = chat._vertical_scrollbar
        painted = sum(1 for y in range(sb.size.height)
                      if (sb.render_line(y)._segments[0].style.bgcolor is not None))
        assert painted == sb.size.height, f"gaps in the scrollbar: {painted}"
        print("  scrollbar continuous, no layout shift -> OK")


def _cards(app):
    from tui import textual_app as ta
    chat = app.query_one("#chat", ta.VerticalScroll)
    out = []
    for w in chat.children:
        for attr in ("content", "source"):
            v = getattr(w, attr, None)
            if v is not None:
                out.append(str(getattr(v, "plain", v)))
                break
    return out


def _last_card(app):
    return _cards(app)[-1]


# ---------------------------------------------------------------------------
# team — the senior/junior directives (a parallel pair must be told to WORK)
# ---------------------------------------------------------------------------
def test_team_directives() -> None:
    _title("team directives")
    from tui import team

    task = "create a new folder for the project"
    senior = team.senior_task(task)
    junior = team.junior_opinion(task)

    # both wrap the user's actual words, not replace them
    assert senior.endswith(task) and junior.endswith(task)

    # the senior is authoritative and has write tools: it must make the REAL
    # thing, not an empty folder / a list / a plan / a question back. This is
    # the exact regression: it once got the bare prompt and replied "Created
    # <empty folder>. Done." with zero code.
    for phrase in ("Do this task yourself now", "write/edit to create",
                   "bash to run and verify", "not an empty folder, a list, a plan",
                   "Do not ask the user for clarification"):
        assert phrase in senior, phrase

    # the junior is read-only, so its finished work must be inline in the text
    assert "Answer this task yourself, now" in junior
    assert "Give the COMPLETE answer inline" in junior
    assert "not a list of files" in junior
    assert "Do not ask the user for clarification" in junior
    print("  senior=do-the-work, junior=full-inline-answer -> OK")


# ---------------------------------------------------------------------------
# session — the sliding window (a long session must not feed the model its
# own stale turns)
# ---------------------------------------------------------------------------
def test_session_window() -> None:
    _title("session sliding window")
    from tui.session import Message, window

    msgs = [Message("user", f"u{i}") for i in range(10)]

    # count limit keeps the newest, drops the oldest
    got = window(msgs, max_messages=4, max_chars=10 ** 9)
    assert [m.content for m in got] == ["u6", "u7", "u8", "u9"], got

    # character budget trims oldest-first but NEVER drops the newest message
    got = window(msgs, max_messages=10, max_chars=6)
    assert got[-1].content == "u9", got
    assert sum(len(m.content) for m in got) <= 6, got

    # a short conversation passes through untouched (and is not mutated)
    short = [Message("user", "hi"), Message("assistant", "yo")]
    before = [m.content for m in short]
    got = window(short, max_messages=12, max_chars=24000)
    assert [m.content for m in got] == ["hi", "yo"]
    assert [m.content for m in short] == before, "mutated input"

    # the system prompt is NOT part of the window: a caller prepends it
    assert window([], max_messages=4, max_chars=100) == []
    print("  count + char budget, newest kept, system untouched -> OK")


# ---------------------------------------------------------------------------
# tools + backend — a `write` that loses its arguments must not run on the cwd
# ---------------------------------------------------------------------------
def test_write_empty_args() -> None:
    _title("write arguments + token cap")
    from tui.tools import TOOLS, t_write
    from tui import backend

    # The bug this locks: a truncated native tool call arrived as args={}, the
    # write tool resolved "" to the cwd, and the agent looped on
    # "error: C:\Users\user is a folder, not a file". Now an empty path says so.
    out = t_write(file_path="", content="x")
    assert "needs a file_path" in out, out

    # a real path with no content is still a valid (empty) file, not an error
    # about being a folder - the folder error was a symptom, not the diagnosis.

    # the tool-calling token cap must be larger than the chat cap, or a whole
    # HTML file inside the write arguments is truncated mid-string and json.loads
    # fails -> args={}. This is the other half of the same bug.
    assert backend.MAX_TOKENS_WITH_TOOLS > backend.MAX_TOKENS, (
        backend.MAX_TOKENS_WITH_TOOLS, backend.MAX_TOKENS)

    # the bash tool must name the real shell so the model stops emitting
    # PowerShell cmdlets (Select-Object / Format-Table) into cmd.exe.
    assert "cmd.exe" in TOOLS["bash"].description
    assert "powershell -Command" in TOOLS["bash"].description
    print("  empty write path, tool token cap, cmd.exe bash docs -> OK")


# ---------------------------------------------------------------------------
# tools — bash output must be cleaned BEFORE truncation, or a sixel image
# (fastfetch) dumps its whole payload into the model's context
# ---------------------------------------------------------------------------
def test_bash_clean_before_truncate() -> None:
    _title("bash output cleaning")
    from tui.tools import _clean, _truncate

    # A sixel image is ONE DCS string: "\x1bPq...\x1b\". fastfetch prints the
    # image (~900KB) and THEN the readable text. Truncating first drops the
    # closing "\x1b\" (it sits far past both ends of the kept head+tail), so the
    # regex cannot match the whole string and the raw payload reaches the model.
    esc = "\x1b"
    sixel = esc + 'Pq"1;1;100;100#0;2;0;0;0#1;2;0;0;20' + "x" * 10000 + esc + "\\"
    # text AFTER the image, like fastfetch's box-drawing readout - longer than
    # half MAX_OUTPUT so the image's terminator is not in the truncation tail
    raw = sixel + ("Y" * 5000)
    assert raw.startswith(esc + "Pq\"") and raw.count(esc) >= 2

    # clean-then-truncate (the fixed order) drops the whole sixel string
    cleaned = _clean(raw)
    assert 'q"1;1' not in cleaned and esc not in cleaned, cleaned[:80]

    # the reverse order - truncate first - is the bug: the closing ESC\ is gone,
    # so the DCS intro is stripped but the sixel payload ("q\"1;1...") survives
    # and reaches the model as garbage
    bad = _clean(_truncate(raw))
    assert 'q"1;1' in bad, "truncate-first no longer leaks sixel - re-check the guard"

    # utf-8 decode must not produce cp1252 mojibake
    assert "\u00e2" not in _clean("box \u2502 char")
    print("  sixel stripped before truncate, utf-8 not cp1252 -> OK")


# ---------------------------------------------------------------------------
def main() -> int:
    # Tests must not read or write the user's real configuration.
    os.environ.setdefault("FORGE_CONFIG_DIR", str(Path(__file__).parent / ".tmp"))
    tests = [test_parse_text_calls, test_agent_cancel, test_agent_wire_ids,
             test_known_facts, test_memory_is_optin, test_plan_load,
             test_team_directives, test_session_window, test_write_empty_args,
             test_bash_clean_before_truncate]
    failed = []
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            failed.append((t.__name__, exc))
            print(f"  FAIL {t.__name__}: {exc}")
    try:
        asyncio.run(_tui_checks())
    except Exception as exc:  # noqa: BLE001
        failed.append(("_tui_checks", exc))
        print(f"  FAIL _tui_checks: {exc}")

    print()
    if failed:
        print(f"FAILED: {', '.join(n for n, _ in failed)}")
        return 1
    print("ALL REGRESSION TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
