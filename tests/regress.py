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

    # Multi-file project parsing: a Python program becomes main.py + requirements.txt,
    # a webpage becomes index.html, and a bare HTML doc is wrapped as index.html.
    from features.design import _parse_project, _safe_rel, _files_quality
    files = _parse_project(
        "```index.html\n<!doctype html><html><body>x</body></html>\n```\n"
        "```style.css\nbody{color:red}\n```\n"
        "```main.py\nimport random\nprint('hi')\n```")
    assert [p for p, _ in files] == ["index.html", "style.css", "main.py"], files
    py = _parse_project("```main.py\nimport pygame\ndef main():\n    print('tick')\n```\n"
                        "```requirements.txt\npygame==2.5.2\n```")
    assert [p for p, _ in py] == ["main.py", "requirements.txt"], py
    assert _files_quality(py) == "good"
    assert _parse_project("Sure! <!doctype html><html><body>x</body></html>")[0][0] == "index.html"
    assert _safe_rel("../../etc/passwd") == "etc/passwd", _safe_rel("../../etc/passwd")
    print("  extractor, wrap, reasoning fallback, multi-file parse -> OK")


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


def test_design_artifacts_listing() -> None:
    _title("design artifacts folder")
    import app as forge_app

    # The 📁 tab lists the projects the studio has written - folders for multi-file
    # projects, plus any legacy loose html files - newest first.
    with forge_app.app.test_client() as client:
        r = client.get("/api/design/artifacts")
        assert r.status_code == 200, r.status_code
        d = r.get_json()
        assert isinstance(d.get("folder"), str) and d["folder"], d
        projects = d.get("projects")
        loose = d.get("files")
        assert isinstance(projects, list) and isinstance(loose, list), d
        assert d.get("count") == len(projects) + len(loose), (d.get("count"), len(projects), len(loose))
        pm = [p["mtime"] for p in projects]
        assert pm == sorted(pm, reverse=True), "projects newest first"
        for p in projects:
            assert p["is_dir"] is True and isinstance(p["name"], str) and p["count"] >= 0
        for f in loose:
            assert f["name"].lower().endswith((".html", ".htm")), f
            assert f["url"] == "/static/uploads/generated/designs/" + f["name"], f
    print("  project folders + loose html, newest first -> OK")


def test_design_run_terminal() -> None:
    _title("run a generated program")
    import time
    import tempfile
    import app as forge_app
    from features import design as design_mod

    saved_dir = design_mod.DESIGNS_DIR
    try:
        with tempfile.TemporaryDirectory() as tmp:
            design_mod.DESIGNS_DIR = tmp
            proj = os.path.join(tmp, "p1")
            os.makedirs(proj)
            with open(os.path.join(proj, "main.py"), "w", encoding="utf-8") as f:
                f.write("print('hello')\nx = input('? ')\nprint('got', x)\n")

            with forge_app.app.test_client() as client:
                r = client.post("/api/design/run", json={"project": "p1", "entry": "main.py"})
                assert r.status_code == 200, r.status_code
                sid = r.get_json()["session"]

                def poll_until(fragment):
                    acc = ""
                    for _ in range(60):
                        o = client.get("/api/design/run/" + sid + "/output").get_json()
                        acc += "".join(o.get("chunks", []))
                        if fragment in acc:
                            return acc
                        time.sleep(0.1)
                    return acc

                out = poll_until("?")
                assert "hello" in out, out
                assert client.post("/api/design/run/" + sid + "/input",
                                   json={"line": "yes"}).status_code == 200
                out = poll_until("got yes")
                assert "got yes" in out, out
                client.post("/api/design/run/" + sid + "/stop")
    finally:
        design_mod.DESIGNS_DIR = saved_dir
    print("  run, poll output, send input round-trip -> OK")


def test_design_runner_languages() -> None:
    _title("run language detection")
    from features import design as design_mod

    # Python uses our own interpreter; node/js/ts use node (installed here); sh uses
    # bash; and anything without a runtime on this machine is refused with None rather
    # than failing cryptically.
    assert design_mod._runner_for("main.py") is not None
    assert design_mod._runner_for("main.js") is not None
    assert design_mod._runner_for("main.ts") is not None
    assert design_mod._runner_for("index.mjs") is not None
    assert design_mod._runner_for("MainActivity.kt") is None      # android
    assert design_mod._runner_for("app.java") is None
    assert design_mod._runner_for("main.rs") is None
    assert design_mod._runner_for("index.html") is None           # web -> iframe, not terminal
    print("  py/js/ts runnable, android/java/rust/html refused cleanly -> OK")


# ---------------------------------------------------------------------------
def test_plugin_tools() -> None:
    _title("plugin tools (agent reads plugins)")
    import tempfile
    import plugin_loader

    # A plugin declares TOOLS + dispatch(); the loader hands them to the agent so a
    # "read my gmail" request becomes a real function call instead of a refusal.
    with tempfile.TemporaryDirectory() as tmp:
        src = (
            "MANIFEST = {'name': 'probe', 'title': 'Probe'}\n"
            "TOOLS = [{'type': 'function', 'function': {'name': 'probe_echo', "
            "'description': 'echo', 'parameters': {'type': 'object', 'properties': {}}}}]\n"
            "def dispatch(name, args):\n"
            "    return {'echoed': args.get('x')}\n"
        )
        path = os.path.join(tmp, "probe.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(src)

        info = plugin_loader._load_plugin(path)
        assert info.get("error") is None, info
        assert [t["function"]["name"] for t in info["tools"]] == ["probe_echo"]
        plugin_loader._register_tools(info)
        assert plugin_loader.execute_tool("probe_echo", {"x": 1}) == {"echoed": 1}
        assert plugin_loader.execute_tool("no_such_tool", {}) is None
        assert "probe_echo" in [t["function"]["name"] for t in plugin_loader.collect_tools()]
        plugin_loader._tool_owners.pop("probe_echo", None)
        plugin_loader._tool_defs[:] = [t for t in plugin_loader._tool_defs
                                       if t["function"]["name"] != "probe_echo"]
    print("  plugin TOOLS collected + dispatched, unknown tool -> None -> OK")


# ---------------------------------------------------------------------------
def test_skills() -> None:
    _title("skills (markdown instruction packs)")
    import tempfile
    import skills_loader

    # A skill is Markdown, not code: front-matter is advertised to the model, the
    # body only arrives through use_skill. This checks the whole round trip.
    real_dir = skills_loader.SKILLS_DIR
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "probe-skill"))
        with open(os.path.join(tmp, "probe-skill", "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: probe-skill\ndescription: Probe things.\n"
                     "when_to_use: probing\n---\n\n# Probe\n\nDo the probe.\n")
        # A loose .md with no front-matter still loads: heading -> title, first
        # paragraph -> description. That is what makes "drop a note in" work.
        with open(os.path.join(tmp, "loose.md"), "w", encoding="utf-8") as fh:
            fh.write("# Loose Skill\n\nA note dropped straight into skills/.\n")
        # README.md is documentation, never a skill.
        with open(os.path.join(tmp, "README.md"), "w", encoding="utf-8") as fh:
            fh.write("# How to write a skill\n")

        skills_loader.SKILLS_DIR = tmp
        try:
            results = skills_loader.load_all()
            assert len(results) == 2, [r.get("id") for r in results]
            ids = sorted(s["id"] for s in skills_loader.all_skills())
            assert ids == ["loose", "probe-skill"], ids

            probe = skills_loader.get("probe-skill")
            assert probe["description"] == "Probe things.", probe
            assert "Do the probe." in probe["body"], probe
            assert skills_loader.get("loose")["description"].startswith("A note dropped"), \
                skills_loader.get("loose")

            # Advertised cheaply: names + descriptions, never the body.
            block = skills_loader.catalogue_block()
            assert "probe-skill" in block and "Do the probe." not in block, block

            assert [t["function"]["name"] for t in skills_loader.collect_tools()] == \
                ["list_skills", "use_skill"]

            loaded = skills_loader.execute_tool("use_skill", {"name": "probe-skill"})
            assert "Do the probe." in loaded["instructions"], loaded
            assert skills_loader.execute_tool("use_skill", {"name": "nope"}).get("error")
            assert skills_loader.execute_tool("list_skills", {})["count"] == 2
            # Not a skill tool -> None, so the next source in _execute_tool gets a turn.
            assert skills_loader.execute_tool("read_file", {"path": "x"}) is None

            # A messy name is normalised, not rejected - and lookups go through the
            # same normalisation, so the model can reach it either way.
            with open(os.path.join(tmp, "messy.md"), "w", encoding="utf-8") as fh:
                fh.write('---\nname: "My Skill!"\ndescription: messy\n---\nbody\n')
            skills_loader.load_all()
            assert skills_loader.get("My Skill!") is not None
            assert "my-skill" in [s["id"] for s in skills_loader.all_skills()]

            # A duplicated name is the real user error (a copied folder that was
            # never renamed). It is reported, and the folder skill keeps the name.
            with open(os.path.join(tmp, "dupe.md"), "w", encoding="utf-8") as fh:
                fh.write("---\nname: probe-skill\ndescription: a stray copy\n---\nbody\n")
            results = skills_loader.load_all()
            assert any("duplicate" in (r.get("error") or "") for r in results), results
            assert skills_loader.get("probe-skill")["description"] == "Probe things.", \
                skills_loader.get("probe-skill")

            # With nothing installed there is no prompt section and no tools at all,
            # so an empty skills/ folder costs the model nothing.
            skills_loader._loaded.clear()
            assert skills_loader.catalogue_block() == ""
            assert skills_loader.collect_tools() == []
        finally:
            skills_loader.SKILLS_DIR = real_dir
            skills_loader.load_all()
    assert len(skills_loader.all_skills()) >= 1
    # Auto matching is deterministic and conservative: a shipped trigger fires,
    # a short trigger never fires inside a longer word.
    m = skills_loader.match_text("build me a landing page")
    assert m and m[0]["id"] == "frontend-design", m
    assert skills_loader.match_text("commit and push to github")[0]["id"] == "commit"
    assert any(x["id"] == "code-review" for x in skills_loader.match_text("review this code"))
    assert not any(x["id"] == "frontend-design" for x in skills_loader.match_text("quick question"))
    # Explicit selection wins over (and dedupes with) auto.
    picked = skills_loader.select_skills("landing page", explicit=["tdd"])
    assert picked[0]["id"] == "tdd" and picked[1]["id"] == "frontend-design", picked
    print("  front-matter, loose notes, on-demand bodies, auto match -> OK")


# ---------------------------------------------------------------------------
#: A minimal MCP server, used as the counterparty for the client test. It speaks
#: the real protocol over stdio, and prints a non-JSON banner first so the test
#: also proves the client ignores noise on the stream.
_FAKE_MCP = r'''
import json, sys
sys.stdout.write("a banner line that is not JSON\n")
sys.stdout.flush()
def send(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try: msg = json.loads(line)
    except ValueError: continue
    mid = msg.get("id"); method = msg.get("method")
    if method == "initialize":
        send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":"2024-11-05",
              "capabilities":{"tools":{}},"serverInfo":{"name":"fake","version":"1"}}})
    elif method == "tools/list":
        send({"jsonrpc":"2.0","id":mid,"result":{"tools":[
            {"name":"echo","description":"Echo text back",
             "inputSchema":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]}},
            {"name":"boom","description":"Always errors",
             "inputSchema":{"type":"object","properties":{}}}]}})
    elif method == "tools/call":
        p = msg.get("params") or {}
        if p.get("name") == "boom":
            send({"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text","text":"it broke"}],"isError":True}})
        else:
            send({"jsonrpc":"2.0","id":mid,"result":{"content":[
                {"type":"text","text":"echo:" + str((p.get("arguments") or {}).get("text"))}]}})
    elif mid is not None:
        send({"jsonrpc":"2.0","id":mid,"error":{"code":-32601,"message":"no such method"}})
'''


def _sse_handler_class():
    """A remote MCP server answering JSON-RPC over an SSE stream."""
    import json as _json
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):        # silence the test output
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            msg = _json.loads(self.rfile.read(n) or b"{}")
            mid, method = msg.get("id"), msg.get("method")
            if method == "initialize":
                body = {"jsonrpc": "2.0", "id": mid, "result": {
                    "protocolVersion": "2024-11-05", "capabilities": {},
                    "serverInfo": {"name": "fake-http", "version": "1"}}}
            elif method == "tools/list":
                body = {"jsonrpc": "2.0", "id": mid, "result": {"tools": [
                    {"name": "web_search", "description": "Search the web",
                     "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}}]}}
            else:
                body = {"jsonrpc": "2.0", "id": mid, "result": {"content": [
                    {"type": "text", "text": "found it"}]}}
            payload = ("event: message\ndata: " + _json.dumps(body) + "\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def test_mcp_client() -> None:
    _title("mcp client (borrowed tools, not reinvented ones)")
    import json as _json
    import sys as _sys
    import tempfile
    import threading
    from http.server import HTTPServer
    import mcp_client

    real_path = mcp_client.CONFIG_PATH
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "fake_mcp.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(_FAKE_MCP)

        httpd = HTTPServer(("127.0.0.1", 0), _sse_handler_class())
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        mcp_client.CONFIG_PATH = os.path.join(tmp, "mcp_servers.json")
        try:
            # save_config takes the inner map but must tolerate the whole file
            # object, which is what a caller naturally hands it.
            mcp_client.save_config({"servers": {
                "fake": {"command": _sys.executable, "args": [script]},
                "web": {"url": "http://127.0.0.1:{}/mcp".format(port)},
                "dead": {"command": "definitely-not-a-real-binary-xyz"},
            }})
            with open(mcp_client.CONFIG_PATH, encoding="utf-8") as fh:
                written = _json.load(fh)
            assert set(written["servers"]) == {"fake", "web", "dead"}, written["servers"]

            mcp_client.load_all()
            assert mcp_client.wait_ready(45), mcp_client.list_servers()

            by_id = {s["id"]: s for s in mcp_client.list_servers()}
            # stdio and HTTP both reach "ready"; the bad command is reported, not fatal.
            assert by_id["fake"]["status"] == "ready", by_id["fake"]
            assert by_id["web"]["status"] == "ready", by_id["web"]
            assert by_id["dead"]["status"] == "error", by_id["dead"]
            assert "was not found" in by_id["dead"]["error"], by_id["dead"]

            names = sorted(t["function"]["name"] for t in mcp_client.collect_tools())
            assert names == ["mcp__fake__boom", "mcp__fake__echo",
                             "mcp__web__web_search"], names
            # A broken server contributes nothing to the agent's tool list.
            assert not any("dead" in n for n in names), names

            assert mcp_client.execute_tool("mcp__fake__echo", {"text": "hi"}) == \
                {"result": "echo:hi"}
            assert mcp_client.execute_tool("mcp__web__web_search", {"q": "x"}) == \
                {"result": "found it"}
            # isError in the MCP result becomes an error the model can act on.
            boom = mcp_client.execute_tool("mcp__fake__boom", {})
            assert "it broke" in boom.get("error", ""), boom
            # An unknown prefixed name is reported, and a non-MCP name is not ours.
            assert "error" in mcp_client.execute_tool("mcp__ghost__nope", {})
            assert mcp_client.execute_tool("read_file", {"path": "x"}) is None

            desc = mcp_client.add_server("extra", {"command": _sys.executable, "args": [script]})
            assert desc["status"] in ("connecting", "ready"), desc
            mcp_client.wait_ready(30)
            assert any(t["function"]["name"] == "mcp__extra__echo"
                       for t in mcp_client.collect_tools())
            assert mcp_client.remove_server("extra") is True
            mcp_client.wait_ready(5)
            assert not any(t["function"]["name"].startswith("mcp__extra__")
                           for t in mcp_client.collect_tools())
        finally:
            mcp_client.shutdown()
            httpd.shutdown()
            mcp_client.CONFIG_PATH = real_path
            mcp_client.load_all()
    print("  stdio + SSE handshake, tools/list, calls, dead server isolated -> OK")


# ---------------------------------------------------------------------------
def test_toolbar_icons_unique() -> None:
    _title("toolbar icons (no two controls look the same)")
    import re as _re

    html_path = Path(__file__).resolve().parent.parent / "templates" / "index.html"
    html = html_path.read_text(encoding="utf-8")
    block = html[html.index('id="topBarIcons"'):html.index('id="viewHost"')]

    # The icon is the first non-ASCII run after the tag. Attribute text is skipped
    # because [^>]* cannot cross the closing bracket, so an em dash inside a title
    # is not mistaken for the icon. Buttons drawn as inline SVG simply do not match.
    icons = _re.findall(r"<(?:button|span)\b[^>]*>\s*([^\x00-\x7f]+)", block)
    assert len(icons) > 20, "toolbar extraction found too few icons: {}".format(icons)

    seen, dupes = set(), []
    for icon in icons:
        if icon in seen:
            dupes.append(icon)
        seen.add(icon)
    # Two different features behind one icon is a real usability bug: the toolbar is
    # icon-only, so a duplicate is indistinguishable without hovering every button.
    assert not dupes, "one icon is used by more than one toolbar control: {}".format(dupes)
    print("  {} toolbar icons, all distinct -> OK".format(len(icons)))


# ---------------------------------------------------------------------------
def test_extensions() -> None:
    _title("extensions (one list, install / toggle / remove)")
    import tempfile
    import extensions
    import skills_loader

    real_skill_dir = skills_loader.SKILLS_DIR
    real_plugin_dir = extensions.PLUGINS_DIR
    with tempfile.TemporaryDirectory() as sk, \
            tempfile.TemporaryDirectory() as pl, \
            tempfile.TemporaryDirectory() as bundle:
        skills_loader.SKILLS_DIR = sk
        extensions.SKILLS_DIR = sk
        extensions.PLUGINS_DIR = pl
        try:
            # A skill appears in the inventory and can be toggled off (a `_`
            # rename the loader already skips) and back on.
            os.makedirs(os.path.join(sk, "my-skill"))
            with open(os.path.join(sk, "my-skill", "SKILL.md"), "w", encoding="utf-8") as fh:
                fh.write("---\nname: my-skill\ndescription: A test skill.\n---\n# My Skill\n")
            skills_loader.load_all()

            by_id = {i["id"] + ":" + i["kind"]: i for i in extensions.inventory()}
            assert by_id["my-skill:skill"]["enabled"] is True, by_id.keys()

            r = extensions.set_enabled("skill", "my-skill", False)
            assert r.get("ok") and r.get("restart") is False, r
            assert skills_loader.get("my-skill") is None          # gone from the agent
            by_id = {i["id"] + ":" + i["kind"]: i for i in extensions.inventory()}
            assert by_id["my-skill:skill"]["enabled"] is False    # still listed

            assert extensions.set_enabled("skill", "my-skill", True).get("ok")
            assert skills_loader.get("my-skill") is not None

            # A plugin is identified by ast (no import), and only MANIFEST files count.
            with open(os.path.join(bundle, "thing.py"), "w", encoding="utf-8") as fh:
                fh.write("MANIFEST = {'name': 'thing', 'title': 'Thing'}\n")
            with open(os.path.join(bundle, "junk.py"), "w", encoding="utf-8") as fh:
                fh.write("print('hi')\n")
            assert extensions._manifest_meta(os.path.join(bundle, "thing.py"))["name"] == "thing"
            assert extensions._is_plugin_asset(os.path.join(bundle, "thing.py")) is True
            assert extensions._is_plugin_asset(os.path.join(bundle, "junk.py")) is False

            # A bundle installs both skills/ and plugins/ subfolders at once.
            os.makedirs(os.path.join(bundle, "skills", "extra-skill"))
            with open(os.path.join(bundle, "skills", "extra-skill", "SKILL.md"),
                      "w", encoding="utf-8") as fh:
                fh.write("---\nname: extra-skill\ndescription: extra\n---\n# Extra\n")
            os.makedirs(os.path.join(bundle, "plugins"))
            with open(os.path.join(bundle, "plugins", "bundled.py"), "w", encoding="utf-8") as fh:
                fh.write("MANIFEST = {'name': 'bundled'}\n")
            res = extensions.install(bundle)
            assert res.get("ok"), res
            assert "extra-skill" in res.get("skills", []), res
            assert "bundled" in res.get("plugins", []), res
            assert skills_loader.get("extra-skill") is not None
            assert os.path.isfile(os.path.join(pl, "bundled.py"))
            # A plugin install flags a restart, because plugins import at startup.
            assert res.get("restart") is True

            r = extensions.remove("skill", "extra-skill")
            assert r.get("ok"), r
            assert skills_loader.get("extra-skill") is None

            # A folder with nothing recognisable refuses cleanly.
            empty = os.path.join(bundle, "empty")
            os.makedirs(empty)
            assert "error" in extensions.install(empty)
        finally:
            skills_loader.SKILLS_DIR = real_skill_dir
            extensions.SKILLS_DIR = real_skill_dir
            extensions.PLUGINS_DIR = real_plugin_dir
            skills_loader.load_all()
    print("  inventory, toggle, install-from-folder, remove -> OK")


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
             test_design_persists_in_conversation, test_design_artifacts_listing,
             test_design_run_terminal, test_design_runner_languages,
             test_plugin_tools, test_skills, test_mcp_client,
             test_toolbar_icons_unique, test_extensions]
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
