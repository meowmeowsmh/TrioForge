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


# ---------------------------------------------------------------------------
# agent — a model that only reads must be told to act (the observed stall:
# seven view/grep rounds on a one-button fix, and never a single write)
# ---------------------------------------------------------------------------
def test_agent_read_only_stall() -> None:
    _title("agent read-only stall")
    from tui.agent import Agent, READ_ONLY_NUDGE_AT, STALL_NUDGE
    from tui.session import Session

    class ReadOnlyBackend:
        """Always asks to view a file - never writes. The flailing model."""
        model = "fake"
        where = "local"
        tools = []

        def __init__(self):
            self.seen = []          # every user message the agent sent

        def stream(self, messages):
            self.seen = [m for m in messages if m.get("role") == "user"]
            # a text-protocol read call, so native tools are irrelevant
            yield "content", '```tool\n{"name": "view", "args": {"file_path": "x.py"}}\n```'
            yield "finish", "stop"

    be = ReadOnlyBackend()
    events = []
    ag = Agent(be, Session(model="f", where="local", system="s"),
               use_tools=True, native_tools=False,
               approve=lambda *a: True, persist=None)
    ag.turn(lambda kind, payload: events.append(kind))

    # the nudge fired, and it told the model to stop reading and act
    nudges = [m["content"] for m in be.seen if "STOP READING" in m.get("content", "")]
    assert nudges, "a pure read-only loop was never nudged"
    assert "apply the fix with write/edit" in nudges[0]
    assert "stall" in events, "the stall was not reported to the UI"
    assert READ_ONLY_NUDGE_AT >= 2, "nudging after one read is too eager"
    assert "Do not view, grep or glob again" in STALL_NUDGE
    print("  read-only loop gets nudged to act, stall reported -> OK")


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
# agent — the system prompt tells the model to verify, not declare, and to
# open with motion instead of recap (ported from DeepSeek-TUI's base prompt)
# ---------------------------------------------------------------------------
def test_agent_verify_and_preamble() -> None:
    _title("agent verify + preamble rules")
    from tui import agent as A
    from tui import textual_app as ta

    prompt = A.SYSTEM_PROMPT
    # the exact failure seen live: write said "created ... (0 lines)" and the
    # model still declared the task done. Verify-before-declare must be in the
    # prompt, and it must name the tool output, not just the exit code.
    assert "VERIFY, DON'T DECLARE" in prompt
    assert "read its OUTPUT, not just the" in prompt
    assert "exit=0 (no output)" in prompt
    # "never present a partial result as the whole" + the three-way handback,
    # ported from Codewhale's "Truthful completion" constitution clause
    assert "what you did NOT verify" in prompt
    assert "Never present a partial result as the whole" in prompt

    # the "which project? what do you want?" garbage: the model must open with
    # an action line and never recap or ask what it can infer.
    assert "OPEN WITH MOTION, NOT RECAP" in prompt
    assert "repeat the user's request back" in prompt
    assert "pick the obvious default and proceed" in prompt

    # narrate the task, not the tool plumbing (Codewhale's output-formatting law)
    assert "NARRATE THE TASK, NOT THE PLUMBING" in prompt
    assert "not narrate" in prompt

    # read-once-then-write: the observed failure where a model made 20
    # read-only calls and never wrote a fix
    assert "READ ONCE, THEN WRITE" in prompt
    assert "A turn that only reads and never writes has failed" in prompt

    # the terminal is not a browser: wide markdown tables don't align there
    assert "prefer short" in ta.DEFAULT_SYSTEM
    assert "over wide Markdown tables" in ta.DEFAULT_SYSTEM
    print("  verify-not-declare, motion-not-recap, terminal formatting -> OK")


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

        # busy guard: a submitted message during a running turn must NOT be
        # erased (the gemini chat TUI disables its input; forge used to clear
        # the prompt and drop the message). Disable the prompt, submit, and the
        # text must survive untouched.
        prompt = app.query_one("#prompt", ta.PromptArea)
        prompt.text = "queued while busy"
        app._busy = True
        prompt.disabled = True
        # simulate the submit handler's guard directly
        before = prompt.text
        app._submitted(ta.PromptArea.Submitted(prompt, before))
        assert prompt.text == before, "busy submit erased the queued message"
        prompt.disabled = False
        app._busy = False
        print("  busy submit keeps the queued message -> OK")

        # team sessions carry recent history: the throwaway session a team peer
        # runs on must be seeded with the conversation BEFORE the current turn,
        # so "the just now folder" resolves instead of "which folder?"
        import asyncio as _aio
        app.session.add_user("make me a shop in D:\\reseller-shop")
        app.session.add_assistant("created D:\\reseller-shop")
        app.session.add_user("now fix the x in that folder")   # current raw msg
        from tui import team as _team, session as _sess
        seeded = _sess.Session(system=app.session.system)
        from tui.session import window
        for m in window(app.session.messages[:-1]):
            seeded.messages.append(m)
        seeded.add_user(_team.task_directive("fix the x in that folder"))
        # history is present AND the wrapped task is the last message
        assert any("D:\\reseller-shop" in m.content for m in seeded.messages), \
            "team session lost the earlier 'D:\\reseller-shop' context"
        assert seeded.messages[-1].content.endswith("fix the x in that folder")
        assert "READ ONCE, THEN WRITE" in seeded.messages[-1].content
        print("  team session carries history, wrapped task last -> OK")

        # routing must NEVER erase the configured model. The bug: a pool with no
        # local entry returned model="" for a local route, which was saved to
        # disk - the gguf path was gone for good and every launch showed
        # "default". Force a local route with an empty local model and check the
        # config survives.
        from tui import router as _router
        saved_pool, saved_model = app._route_pool, app.cfg.model
        app.cfg.model = "D:\\models\\gemma-4-12B-it-qat-UD.gguf"
        app._route_pool = ["deepseek:deepseek-flash"]      # no local: entry
        _real_reachable = _router.reachable
        _router.reachable = lambda url, timeout=3.0: False  # force the local route
        try:
            app._route("hello")
        finally:
            _router.reachable = _real_reachable
        assert app.cfg.model == "D:\\models\\gemma-4-12B-it-qat-UD.gguf", \
            "routing to local erased the configured model: {!r}".format(app.cfg.model)
        import json as _json
        from tui.providers import CONFIG_FILE
        _on_disk = _json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        assert _on_disk.get("model") == "D:\\models\\gemma-4-12B-it-qat-UD.gguf", \
            "an empty model was written to disk: {!r}".format(_on_disk.get("model"))
        app._route_pool, app.cfg.model = saved_pool, saved_model
        print("  routing never erases the configured model -> OK")

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
# team — one directive for every peer (no senior/junior; whoever is inside
# does the job)
# ---------------------------------------------------------------------------
def test_team_directives() -> None:
    _title("team directives")
    from tui import team

    task = "create a new folder for the project"
    directive = team.task_directive(task)

    # wraps the user's actual words, not replace them
    assert directive.endswith(task)

    # every model gets the SAME do-the-work instruction: make the REAL thing,
    # not an empty folder / a list / a plan / a question back. This is the
    # exact regression: it once got the bare prompt and replied "Created
    # <empty folder>. Done." with zero code.
    for phrase in ("Do this task yourself now", "write/edit to create",
                   "bash to run and verify", "not an empty folder, a list, a plan",
                   "Do not ask the user for clarification"):
        assert phrase in directive, phrase

    # the read-once-then-write rule: a model must not burn its whole turn
    # investigating (the observed failure: 20 read-only tool calls, zero writes,
    # final answer was just its opening preamble line)
    assert "READ ONCE, THEN WRITE" in directive
    assert "Do not re-read a file you already saw" in directive
    assert "the deliverable is the fixed file" in directive

    # no senior/junior left anywhere in the module's API
    assert not hasattr(team, "senior_task")
    assert not hasattr(team, "junior_opinion")
    assert not hasattr(team, "SENIOR_TASK_DIRECTIVE")
    assert not hasattr(team, "JUNIOR_OPINION_DIRECTIVE")
    print("  one directive for all peers, senior/junior gone -> OK")


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
# history — the TUI transcript persists across restarts, so forge stops
# forgetting what it was working on
# ---------------------------------------------------------------------------
def test_history_persists() -> None:
    _title("tui history persistence")
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["TRIOFORGE_HISTORY_DB"] = str(Path(tmp) / "hist.db")
        # fresh import so DB_PATH picks up the env override
        import importlib
        import tui.history as hist
        importlib.reload(hist)

        from tui.session import Session, Message

        # a session that persists itself via on_change
        changes = []

        def hook(s):
            changes.append(len(s.messages))

        s = Session()
        s.on_change = hook
        s.add_user("make me a shop in D:\\reseller-shop")
        s.add_assistant("created D:\\reseller-shop")
        assert changes == [1, 2], "on_change did not fire per mutation"

        # wire it to the real store and reload in a FRESH session
        s2 = Session()
        s2.on_change = lambda s: hist.save(s.messages)
        s2.add_user("make me a shop in D:\\reseller-shop")
        s2.add_assistant("created D:\\reseller-shop")

        got = hist.load()
        assert got == [("user", "make me a shop in D:\\reseller-shop"),
                       ("assistant", "created D:\\reseller-shop")], got

        # clear persists as empty, and drop_last removes the trailing message
        s2.add_user("fix the x")
        s2.drop_last()
        assert hist.load()[-1] == ("assistant", "created D:\\reseller-shop")
        s2.clear()
        assert hist.load() == []
    os.environ.pop("TRIOFORGE_HISTORY_DB", None)
    print("  history survives save/load, clear and drop_last -> OK")


# ---------------------------------------------------------------------------
# router — a repair task must go to the cloud model, not the weak local one
# ---------------------------------------------------------------------------
def test_router_repair_is_complex() -> None:
    _title("router repair -> complex")
    from tui.router import classify

    # The observed failure: "fix the X button" classified simple, so auto-route
    # sent it to the local gemma, which read files and never wrote a fix.
    for task in ("fix the x button nothing is clickable make it work",
                 "why is the shop broken",
                 "fix a bug in my code",
                 "repair the broken shop",
                 "the app is not working",
                 "debug the crash"):
        verdict, reasons = classify(task)
        assert verdict == "complex", (task, verdict, reasons)

    # small talk still stays simple, and the grammar fix stays simple even
    # though "fix" is now a complex hint - the grammar hint must win
    assert classify("hello")[0] == "simple"
    assert classify("fix the grammar in this sentence")[0] == "simple"
    print("  repair verbs route complex, grammar/hello stay simple -> OK")


def test_window_slot_is_exclusive() -> None:
    _title("one app window only")
    import subprocess
    import sys
    import tempfile
    from pathlib import Path as _P

    from tools import app_window

    # The reported failure: double-clicking the desktop app six times started six
    # window processes, because the "a window is already open" marker was written
    # seconds late AND with a plain write_text - so simultaneous clicks all saw an
    # empty slot and all won it. Six processes then fought over one port and one
    # locked WebView2 profile, and the app never appeared at all.
    with tempfile.TemporaryDirectory() as tmp:
        marker = _P(tmp) / "app_window.pid"
        real = app_window.window_pid_file
        app_window.window_pid_file = lambda: marker
        try:
            # A LIVE owner must win, and everybody else must back off.
            child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
            try:
                marker.write_text("{}  0\n".format(child.pid), encoding="utf-8")
                path, already_open = app_window.claim_window_slot("http://127.0.0.1:5003")
                assert (path, already_open) == (None, True), (path, already_open)
                assert str(child.pid) in marker.read_text(encoding="utf-8"), \
                    "the loser overwrote the live owner's marker"
            finally:
                child.kill()
                child.wait(timeout=20)

            # A dead owner is stale: the next launch must take the slot over, or the
            # app could never start again after a crash.
            path, already_open = app_window.claim_window_slot("http://127.0.0.1:5003")
            assert already_open is False, already_open
            assert path == marker, (path, marker)
            parts = marker.read_text(encoding="utf-8").split()
            assert int(parts[0]) == os.getpid(), parts
            assert len(parts) >= 3 and parts[2].isdigit(), parts

            # An unreadable/garbage marker must never lock the user out of their app.
            marker.write_text("not a pid\n", encoding="utf-8")
            path, already_open = app_window.claim_window_slot("")
            assert already_open is False and path == marker, (path, already_open)
        finally:
            app_window.window_pid_file = real

    # Structural guard for the original defect: the slot must be claimed BEFORE the
    # slow work (spawning the server, creating the window), not after it.
    src = (_P(app_window.__file__).read_text(encoding="utf-8"))
    body = src[src.index("def main("):]
    # Comments are stripped first: the explanation of this very fix names both calls,
    # and a comment is not a call site.
    code = "\n".join(line for line in body.splitlines()
                     if not line.lstrip().startswith("#"))
    claim = code.index("claim_window_slot(")
    assert claim < code.index("_spawn_server("), "slot claimed after the server spawn"
    assert claim < code.index("create_window("), "slot claimed after create_window"
    print("  exclusive slot: live owner wins, stale owner replaced -> OK")


def test_window_ready_means_visible() -> None:
    _title("window readiness = a visible window")
    import threading
    import time as _time

    from tools import app_window

    # The reported failure: six double-clicks left six TrioForge processes, none of
    # which ever showed a window. pywebview creates the WinForms form first and only
    # Show()s it after WebView2 initialises; when that wedged, the form object existed
    # for ever, `_window_ready` was already True (it only checked window.native), so
    # the hang, blank and fallback watchdogs all concluded the app was healthy - and
    # the process sat invisible in Task Manager instead of falling back to a browser.
    saved = {name: getattr(app_window, name) for name in
             ("_find_own_window_hwnd", "apply_window_icon", "hang_watchdog",
              "blank_page_watchdog", "size_watchdog")}
    started = []
    icons = []

    class _FakeWindow:
        def __init__(self):
            self.native = object()          # the form EXISTS from the first moment
            self.shown = 0

        def show(self):
            self.shown += 1

    try:
        app_window.apply_window_icon = lambda window, ico: icons.append(ico) or True
        app_window.hang_watchdog = lambda get_handle, url, **kw: started.append("hang")
        app_window.blank_page_watchdog = lambda get_handle, url, **kw: started.append("blank")
        app_window.size_watchdog = lambda window, get_handle, w, h, **kw: started.append("size")
        app_window._find_own_window_hwnd = lambda: 0

        win = _FakeWindow()
        app_window._window_ready = False
        ok = app_window.apply_icon_when_ready(win, Path("x.ico"), "http://127.0.0.1:5003",
                                             timeout=0.8)
        assert ok is False, "a form that was never shown must not count as ready"
        assert app_window._window_ready is False, \
            "readiness must stay false, or no watchdog ever fires"
        assert win.shown >= 1, "a lingering hidden form must be asked to show itself"
        assert started == [], started

        # A real, visible window: readiness flips and both watchdogs start, with the
        # OS handle (no pythonnet IntPtr to mis-convert).
        app_window._find_own_window_hwnd = lambda: 4242
        app_window._window_ready = False
        ok = app_window.apply_icon_when_ready(win, Path("x.ico"), "http://127.0.0.1:5003",
                                             timeout=1.0)
        assert ok is True and app_window._window_ready is True, (ok, app_window._window_ready)
        assert started == ["hang", "blank", "size"], started

        # Icon work assigns WinForms properties from a non-UI thread, which blocks for
        # ever against a UI thread stuck in WebView2 init. Readiness must not wait on it.
        blocked = threading.Event()
        app_window.apply_window_icon = lambda window, ico: blocked.wait(30)
        app_window._window_ready = False
        began = _time.time()
        ok = app_window.apply_icon_when_ready(win, Path("x.ico"), "http://127.0.0.1:5003",
                                             timeout=1.0)
        took = _time.time() - began
        assert ok is True and took < 3.0, (ok, took)
        blocked.set()

        # And the fallback must actually rescue the user when no window ever shows:
        # one clean retry with a fresh profile, then the browser - never a silent,
        # invisible process left behind.
        import webbrowser
        events = []
        real_exit, real_execv = os._exit, os.execv
        real_open = webbrowser.open
        real_rotate = app_window.rotate_profile
        app_window._window_ready = False
        os.environ.pop("TRIOFORGE_WINDOW_RETRIED", None)
        try:
            os._exit = lambda code: events.append(("exit", code))
            os.execv = lambda path, argv: events.append(("execv", None))
            webbrowser.open = lambda url: events.append(("browser", url))
            app_window.rotate_profile = lambda storage: events.append(("rotate", storage))
            app_window.fallback_watchdog("http://127.0.0.1:5003", timeout=0.2,
                                         storage=Path("profile"))
            assert ("rotate", Path("profile")) in events, events
            assert ("execv", None) in events, events
            assert os.environ.get("TRIOFORGE_WINDOW_RETRIED") == "1", \
                "the retry must not be able to loop for ever"
            events[:] = []
            os.environ["TRIOFORGE_WINDOW_RETRIED"] = "1"
            app_window.fallback_watchdog("http://127.0.0.1:5003", timeout=0.2,
                                         storage=Path("profile"))
            assert ("browser", "http://127.0.0.1:5003") in events, events
            assert ("exit", 1) in events, events
        finally:
            os._exit, os.execv = real_exit, real_execv
            webbrowser.open = real_open
            app_window.rotate_profile = real_rotate
            os.environ.pop("TRIOFORGE_WINDOW_RETRIED", None)
    finally:
        for name, value in saved.items():
            setattr(app_window, name, value)
        app_window._window_ready = False
    # A hang must not dock the GPU for ever: the marker this machine was carrying was
    # eighteen days old and still forcing SwiftShader (CPU) rendering on every launch.
    import tempfile as _tempfile
    with _tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "window_hung.txt"
        real_marker = app_window.hang_marker
        app_window.hang_marker = lambda: marker
        try:
            os.environ.pop("TRIOFORGE_WINDOW_HARDWARE", None)
            os.environ.pop("TRIOFORGE_WINDOW_SOFTWARE", None)
            assert app_window.hardware_gpu_allowed() is True, "no marker: hardware"
            marker.write_text("1 hung\n", encoding="utf-8")
            assert app_window.hardware_gpu_allowed() is False, "fresh hang: software"
            old = _time.time() - (app_window.HANG_MEMORY_SECONDS + 60)
            os.utime(str(marker), (old, old))
            assert app_window.hardware_gpu_allowed() is True, "stale hang must expire"
            app_window.clear_hang_marker()
            assert not marker.exists(), "a painted window must clear the hang memory"
        finally:
            app_window.hang_marker = real_marker

    print("  hidden form is not ready, visible window is, icons cannot block it -> OK")


def test_deepseek_catalog() -> None:
    _title("deepseek model catalogue")
    from providers.llm_providers import (
        DeepSeekProvider, deepseek_reasoning_effort, model_supports_vision,
    )

    # The harness's advisory catalogue: named, metadata-rich entries, not bare ids.
    cat = {m["id"]: m for m in DeepSeekProvider().model_catalog()}
    assert set(cat) == {"deepseek-flash", "deepseek-v4-pro"}, set(cat)
    assert cat["deepseek-flash"]["name"] == "DeepSeek-V41-Flash"
    assert cat["deepseek-flash"]["contextWindow"] == 1_000_000
    assert "image" in cat["deepseek-flash"]["inputModalities"], \
        "flash is text+image in the harness"
    assert cat["deepseek-v4-pro"]["name"] == "DeepSeek-V4-Pro"
    assert cat["deepseek-v4-pro"]["reasoning"]["defaultEffort"] == "high"
    efforts = [e["id"] for e in cat["deepseek-v4-pro"]["reasoning"]["efforts"]]
    assert efforts == ["off", "low", "high", "max"], efforts

    # UI effort ids map onto the /v1 reasoning_effort the endpoint accepts.
    assert deepseek_reasoning_effort("off") is None
    assert deepseek_reasoning_effort("low") == "low"
    assert deepseek_reasoning_effort("mid") == "medium"
    assert deepseek_reasoning_effort("high") == "high"
    assert deepseek_reasoning_effort("max") == "high"
    assert deepseek_reasoning_effort("") is None

    # Capability (vision badge / image routing) comes from the catalogue.
    assert model_supports_vision("deepseek", "deepseek-flash") is True
    assert model_supports_vision("deepseek", "deepseek-v4-pro") is False
    print("  named entries, 1M context, image flag, effort map -> OK")


def test_design_feature() -> None:
    _title("design generator")
    from features.design import (
        _extract_html, _wrap_html, _model_answer, _looks_unstyled, DESIGN_MAX_TOKENS,
    )

    # HTML extraction from every shape a model actually returns.
    assert _extract_html("```html\n<div>hi</div>\n```") == "<div>hi</div>"
    assert _extract_html("<!doctype html><html></html>") == "<!doctype html><html></html>"
    frag = "<style>body{}</style><div>x</div>"
    assert _extract_html(frag) == frag, "a tag-leading fragment must keep its <style>"
    assert _extract_html("intro text\n<div class=\"x\">a</div>") == "<div class=\"x\">a</div>"
    assert _extract_html("just prose, no tags") is None

    # The model's "Here is your page:" preamble must NOT end up in the file: in front
    # of the doctype it renders as visible text in quirks mode.
    out = _extract_html("Sure! Here is the page:\n\n<!doctype html><html><body>x</body></html>")
    assert out.startswith("<!doctype html>") and "Sure!" not in out, out[:60]

    # A <style> block above <body> must survive extraction: slicing from <body> threw
    # the whole stylesheet away and produced a structured but completely unstyled page.
    css = "".join(".c%d{color:#%03d}" % (i, i) for i in range(12))
    out = _extract_html("Here you go:\n<style>" + css + "</style>\n<body><header>x</header></body>")
    assert "<style>" in out and "{" in out, "the stylesheet was dropped"
    wrapped = _wrap_html(out)
    assert wrapped.index("<style>") < wrapped.index("<body>"), "styles belong in the head"

    # Trailing commentary after the document is trimmed.
    out = _extract_html("<!doctype html><html><body>x</body></html>\n\nWant changes?")
    assert out.endswith("</html>"), out[-25:]

    # _wrap_html leaves a document alone and shells a fragment.
    assert _wrap_html("<!doctype html><html></html>").startswith("<!doctype html>")
    wrapped = _wrap_html("<div>x</div>")
    assert wrapped.startswith("<!doctype html>") and "<body><div>x</div></body>" in wrapped

    # A page with structure but no real stylesheet is detected (and retried).
    assert _looks_unstyled("<body><div class='a'></div></body>") is True
    assert _looks_unstyled("<style>" + css + "</style><body>x</body>") is False
    assert _looks_unstyled('<link rel="stylesheet" href="x.css">') is False

    # The design budget must clear a thinking model's reasoning PLUS a whole page:
    # 8192 truncated a landing page mid-stylesheet, so keep it generous.
    assert DESIGN_MAX_TOKENS >= 32768, DESIGN_MAX_TOKENS

    # Thinking models put the answer in content OR reasoning_content.
    class _Reasoned:
        def generate_raw(self, messages, **kw):
            return {"content": "", "reasoning_content": "<div>reasoned</div>"}
    assert _model_answer(_Reasoned(), [], "", "") == "<div>reasoned</div>"

    class _Plain:
        def generate_raw(self, messages, **kw):
            return {"content": "<div>plain</div>", "reasoning_content": "think"}
    assert _model_answer(_Plain(), [], "", "") == "<div>plain</div>"
    print("  extractor, wrap, reasoning fallback -> OK")


def test_workspace_path_guard() -> None:
    _title("workspace path resolution")
    import tempfile
    import app as forge_app

    saved = forge_app._workspace_setting

    def with_folder(folder):
        forge_app._workspace_setting = lambda wid, key, default=None: (
            folder if key == "folder" else default)

    try:
        # A workspace pointed at a drive ROOT is the common case here (folder "D:\\"),
        # and `base_real + os.sep` built "D:\\\\" - so EVERY real path under it failed
        # the prefix check and the whole folder was denied, for the browser and the
        # agent's own file tools alike.
        root = "C:\\" if os.name == "nt" else os.sep
        with_folder(root)
        target, err = forge_app._resolve_workspace_file("default", "sub/file.txt")
        assert err is None, err
        assert target == os.path.realpath(os.path.join(root, "sub", "file.txt")), target
        target, err = forge_app._resolve_workspace_file("default", "")
        assert err is None and target == os.path.realpath(root), (target, err)

        # A real escape from a normal folder must still be refused.
        with tempfile.TemporaryDirectory() as tmp:
            with_folder(tmp)
            target, err = forge_app._resolve_workspace_file("default", "inside.txt")
            assert err is None and target == os.path.realpath(os.path.join(tmp, "inside.txt"))
            _, err = forge_app._resolve_workspace_file(
                "default", os.path.join("..", "..", "escape.txt"))
            assert err is not None, "path traversal must stay blocked"
            _, err = forge_app._resolve_workspace_file("default", os.path.join("..", "escape.txt"))
            assert err is not None, "path traversal must stay blocked"
    finally:
        forge_app._workspace_setting = saved
    print("  drive-root folder resolves, traversal blocked -> OK")


def test_design_persists_in_conversation() -> None:
    _title("a design is saved in the chat")
    import app as forge_app

    # The bug: the design generator wrote its HTML to static/uploads but NEVER wrote a
    # message, so after a reload the brief and the result were gone from the chat for
    # good. Everything the UI needs to re-render the preview must round-trip through
    # the database, including meta.design.
    cid = forge_app.create_conversation("regress-design")
    url = "/static/uploads/generated/designs/design-test.html"
    try:
        assert forge_app.add_message(cid, "user", "make me a coffee shop page") is True
        assert forge_app.add_message(
            cid, "bot", "Design generated", meta={"design": url, "kind": "api"}) is True

        msgs = forge_app.get_messages(cid)
        assert len(msgs) == 2, msgs
        assert msgs[0]["role"] == "user" and "coffee shop" in msgs[0]["text"]
        bot = msgs[-1]
        assert bot["role"] == "bot"
        assert (bot.get("meta") or {}).get("design") == url, bot.get("meta")
    finally:
        try:
            forge_app.delete_conversation(cid)
        except Exception:
            pass
    print("  brief + result + meta.design survive a reload -> OK")


# ---------------------------------------------------------------------------
def main() -> int:
    # Tests must not read or write the user's real configuration.
    os.environ.setdefault("FORGE_CONFIG_DIR", str(Path(__file__).parent / ".tmp"))
    tests = [test_parse_text_calls, test_agent_cancel, test_agent_wire_ids,
             test_agent_read_only_stall,
             test_known_facts, test_memory_is_optin, test_agent_verify_and_preamble,
             test_plan_load, test_team_directives, test_session_window,
             test_write_empty_args, test_bash_clean_before_truncate,
             test_history_persists, test_router_repair_is_complex,
             test_window_slot_is_exclusive, test_window_ready_means_visible,
             test_deepseek_catalog, test_design_feature, test_workspace_path_guard,
             test_design_persists_in_conversation]
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
