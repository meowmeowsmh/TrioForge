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
import traceback
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "py"))

GB = 1073741824


def _title(name: str) -> None:
    print(f"\n== {name} ==")


def _csrf_ok(client):
    """Give a test client the token a browser sends on state-changing requests.

    A real page fetches /api/csrf and echoes the token; a client that skips that
    now gets a 403, which is the point of the protection. Doing the round trip
    here keeps the tests honest instead of switching CSRF off for them.
    """
    token = (client.get("/api/csrf").get_json() or {}).get("token") or ""
    client.environ_base["HTTP_X_CSRF_TOKEN"] = token
    return token


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
def test_python_floor_calendar() -> None:
    """The support calendar warns, fails and advances the floor on schedule."""
    _title("python support calendar")
    import datetime as dt

    from python_support import recommended_floor, support

    # 3.10 is end-of-life: the month after its October 2026 EOL it is gone.
    assert support(3, 10, dt.date(2026, 11, 1))["eol_passed"] is True
    # The floor (3.12) has 24 months left today: Oct 2026 -> Oct 2028.
    info = support(3, 12, dt.date(2026, 10, 14))
    assert info["months"] == 24 and info["near_eol"] is False, info
    # A year out it starts nagging; past EOL it is a hard failure.
    assert support(3, 12, dt.date(2027, 10, 1))["near_eol"] is True
    assert support(3, 12, dt.date(2028, 11, 1))["eol_passed"] is True
    # A version nobody recorded reports "unknown", not a made-up date.
    assert support(3, 16, dt.date(2026, 10, 14))["eol"] is None
    # The auto-advance: 3.12 today, 3.13 once 3.12 is inside its final year,
    # 3.14 once 3.12 is gone - the shift the maintainer used to have to remember.
    assert recommended_floor(dt.date(2026, 10, 14)) == (3, 12)
    assert recommended_floor(dt.date(2027, 11, 1)) == (3, 13)
    assert recommended_floor(dt.date(2028, 11, 1)) == (3, 14)
    print("  floor calendar warns, fails and advances on schedule -> OK")


# ---------------------------------------------------------------------------
def test_comfyui_auto_start() -> None:
    """ComfyUI is auto-started instead of erroring, and failures stay clear."""
    _title("comfyui auto-start")
    import unittest.mock as mock

    import comfyui_service as cs

    # Already running -> return immediately, never touch the install.
    with mock.patch.object(cs, "is_comfyui_running", return_value=True), \
            mock.patch.object(cs, "find_comfyui_install") as find:
        assert cs.ensure_comfyui_running() is True
        find.assert_not_called()

    # Not running + no install -> a message that says how to fix it.
    with mock.patch.object(cs, "is_comfyui_running", return_value=False), \
            mock.patch.object(cs, "find_comfyui_install", return_value=None):
        try:
            cs.ensure_comfyui_running(timeout=1)
            raise AssertionError("expected RuntimeError")
        except RuntimeError as e:
            assert "not installed" in str(e), e

    # Not running + a real install -> starts main.py, polls until it answers.
    n = {"i": 0}

    def fake_running(*_a, **_k):
        n["i"] += 1
        return n["i"] > 2              # False, False, then True after "start"

    fake_proc = mock.MagicMock()
    fake_proc.poll.return_value = None
    with mock.patch.object(cs, "is_comfyui_running", side_effect=fake_running), \
            mock.patch.object(cs, "find_comfyui_install", return_value="/fake/install"), \
            mock.patch.object(cs, "_comfyui_entrypoint", return_value="/fake/install/main.py"), \
            mock.patch.object(cs, "_comfyui_python", return_value="python"), \
            mock.patch.object(cs, "hidden_tree_kwargs", return_value={}), \
            mock.patch.object(cs.subprocess, "Popen", return_value=fake_proc) as popen:
        assert cs.ensure_comfyui_running(timeout=5) is True
        assert popen.call_count == 1, popen.call_count
    print("  comfyui auto-starts in the background instead of erroring -> OK")


# ---------------------------------------------------------------------------
def test_comfyui_paths_cross_platform() -> None:
    """The python/entrypoint finders resolve on every OS layout."""
    _title("comfyui paths (cross-platform)")
    import unittest.mock as mock

    import comfyui_service as cs

    j = os.path.join

    # macOS / Linux manual install: venv/bin/python + main.py at the root.
    with mock.patch.object(cs.os.path, "isfile",
                           side_effect=lambda p: p in {j("/c", "main.py"),
                                                       j("/c", "venv", "bin", "python")}):
        assert cs._comfyui_entrypoint("/c") == j("/c", "main.py")
        assert cs._comfyui_python("/c") == j("/c", "venv", "bin", "python")

    # Windows manual install: venv/Scripts/python.exe (same function, other layout).
    with mock.patch.object(cs.os.path, "isfile",
                           side_effect=lambda p: p in {j("C:\\c", "venv", "Scripts", "python.exe")}):
        assert cs._comfyui_python("C:\\c") == j("C:\\c", "venv", "Scripts", "python.exe")

    # Comfy Desktop's REAL layout: the venv is named ".venv" (with a dot). This is
    # what the shipped installer actually uses, and the finder used to miss it.
    with mock.patch.object(cs.os.path, "isfile",
                           side_effect=lambda p: p in {j("C:\\c", ".venv", "Scripts", "python.exe")}):
        assert cs._comfyui_python("C:\\c") == j("C:\\c", ".venv", "Scripts", "python.exe")

    # Comfy Desktop embedded python (macOS/Linux): python/bin/python3 in a subfolder.
    with mock.patch.object(cs.os.path, "isfile",
                           side_effect=lambda p: p in {j("/c", "ComfyUI", "main.py"),
                                                       j("/c", "ComfyUI", "python", "bin", "python3")}):
        assert cs._comfyui_entrypoint("/c") == j("/c", "ComfyUI", "main.py")
        assert cs._comfyui_python("/c") == j("/c", "ComfyUI", "python", "bin", "python3")

    print("  comfyui python/entrypoint resolve on win + mac/linux -> OK")


# ---------------------------------------------------------------------------
def test_comfyui_widget_mapping_and_errors() -> None:
    """A COMBO input is a widget, and a rejected prompt says WHAT is wrong."""
    _title("comfyui widget mapping + error text")
    import comfyui_service as cs

    # Modern ComfyUI reports a dropdown as the STRING "COMBO" (options nested in
    # the second element). Counting only the legacy LIST form dropped the combo
    # and shifted every positional widgets_values by one, so a live run sent
    # megapixels="nearest-exact" (a string into a FLOAT) and left upscale_method
    # missing - ComfyUI answered 400 "Prompt outputs failed validation".
    modern = {"N": {"input": {"required": {
        "image": ["IMAGE"],
        "upscale_method": ["COMBO", {"options": ["nearest-exact", "bilinear"]}],
        "megapixels": ["FLOAT", {"default": 1.0}],
    }}}}
    assert cs._widget_names_for_type(modern, "N") == ["upscale_method", "megapixels"]
    assert cs._widget_value_map(modern, "N", ["nearest-exact", 1.0]) == \
        {"upscale_method": "nearest-exact", "megapixels": 1.0}

    # The legacy list-of-options form must still count.
    legacy = {"N": {"input": {"required": {
        "mode": [["a", "b"], {}], "strength": ["FLOAT", {}]}}}}
    assert cs._widget_names_for_type(legacy, "N") == ["mode", "strength"]

    # A 400 body becomes something actionable instead of "400 Client Error".
    class _Resp:
        status_code = 400

        def json(self):
            return {
                "error": {"message": "Prompt outputs failed validation"},
                "node_errors": {"1": {"class_type": "CLIPLoader", "errors": [{
                    "type": "value_not_in_list",
                    "details": "clip_name: 'qwen_3_4b.safetensors' not in []",
                    "extra_info": {"input_name": "clip_name", "input_config": [[], {}],
                                   "received_value": "qwen_3_4b.safetensors"},
                }]}},
            }

    msg = cs._validation_error(_Resp())
    assert "missing model file: qwen_3_4b.safetensors" in msg, msg
    assert "Prompt outputs failed validation" in msg, msg
    print("  COMBO counts as a widget; a 400 names the missing file -> OK")


# ---------------------------------------------------------------------------
def test_comfyui_workflow_readiness() -> None:
    """A workflow whose models are not installed is never ready."""
    _title("comfyui workflow readiness")
    import json
    import os
    import tempfile

    import comfyui_service as cs

    # UNETLoader uses the modern "COMBO" form; VAELoader the legacy list form.
    oi = {
        "UNETLoader": {"input": {"required": {
            "unet_name": ["COMBO", {"options": ["have.safetensors"]}]}}},
        "VAELoader": {"input": {"required": {
            "vae_name": [["have_vae.safetensors"], {}]}}},
    }
    made = []

    def wf(nodes):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8")
        json.dump({"nodes": nodes}, fh)
        fh.close()
        made.append(fh.name)
        return {"path": fh.name, "name": "probe"}

    try:
        ok = wf([{"type": "UNETLoader", "widgets_values": ["have.safetensors"]},
                 {"type": "VAELoader", "widgets_values": ["have_vae.safetensors"]}])
        assert cs.workflow_ready(ok, oi) is True

        # One missing model file is enough to make the whole workflow unusable -
        # this is why "FHDR-Setup" (T5 + CLIP-L + a GGUF) must not be offered on an
        # install that only has the Z-Image models.
        missing = wf([{"type": "UNETLoader", "widgets_values": ["have.safetensors"]},
                      {"type": "VAELoader", "widgets_values": ["absent_vae.safetensors"]}])
        assert cs.workflow_ready(missing, oi) is False

        # Non-model widgets (a float, a combo of modes) must not be judged.
        oi2 = dict(oi)
        oi2["ImageScale"] = {"input": {"required": {"megapixels": ["FLOAT", {}],
                                                     "method": ["COMBO", {"options": ["a"]}]}}}
        plain = wf([{"type": "ImageScale", "widgets_values": [1.0, "a"]}])
        assert cs.workflow_ready(plain, oi2) is True
    finally:
        for p in made:
            try:
                os.unlink(p)
            except OSError:
                pass
    print("  a workflow with a missing model is not ready -> OK")


# ---------------------------------------------------------------------------
def test_comfyui_model_autoscan() -> None:
    """Missing models are detected, resolved to a URL, and never guessed at."""
    _title("comfyui auto-scan (missing models)")
    import json
    import os
    import tempfile

    import comfyui_service as cs

    oi = {"UNETLoader": {"input": {"required": {
        "unet_name": ["COMBO", {"options": ["have.safetensors"]}]}}}}
    fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump({"nodes": [{"type": "UNETLoader",
                          "widgets_values": ["absent.safetensors"]}]}, fh)
    fh.close()
    wf = {"path": fh.name, "name": "probe"}

    # Hermetic: this must not care whether a ComfyUI happens to be running on the
    # machine that runs the suite, and it must never reach the network.
    real_manager, real_install, real_mgr_install = (
        cs.manager_version, cs.install_model, cs.manager_install_model)
    fetched = []
    cs.manager_version = lambda *a, **k: None
    cs.manager_install_model = lambda *a, **k: fetched.append(a[:3])
    cs.install_model = lambda *a, **k: fetched.append(a[:1])
    try:
        assert cs.missing_models(wf, oi) == ["absent.safetensors"]

        # Every model the SHIPPED workflows reference must have a known source, or
        # auto-scan cannot heal it - this list is what makes FHDR reachable.
        for name in ("qwen_3_4b.safetensors", "z_image_turbo_bf16.safetensors",
                     "ae.safetensors", "clip_l.safetensors",
                     "t5xxl_fp8_e4m3fn.safetensors", "FHDR_ComfyUI-Q4_K_M.gguf"):
            assert name in cs.MODEL_SOURCES, name

        # A name with NO known source is REPORTED, never guessed at or fetched.
        assert cs.ensure_models(wf, oi) == ([], [], ["absent.safetensors"])
        assert fetched == [], fetched

        # A name WITH a source, but no ComfyUI-Manager running, is reported as
        # "not fetched" (with the reason) unless direct fetching was opted into:
        # a bare install must never silently download tens of GB.
        cs.MODEL_SOURCES["absent.safetensors"] = (
            "Comfy-Org/z_image_turbo", "split_files/vae", "vae", "model")
        try:
            installed, failed, unknown = cs.ensure_models(wf, oi)
            assert not installed and not unknown, (installed, unknown)
            assert len(failed) == 1 and failed[0][0] == "absent.safetensors", failed
            assert "TRIOFORGE_AUTO_MODELS" in failed[0][1], failed
            assert fetched == [], "it must not fetch without opt-in or a Manager"

            # Opt-in direct fetching calls the direct downloader, and the URL is
            # built from the source (never invented).
            os.environ["TRIOFORGE_AUTO_MODELS"] = "1"
            try:
                assert cs.ensure_models(wf, oi)[0] == ["absent.safetensors"]
                assert fetched and fetched[0][0] == "absent.safetensors", fetched
            finally:
                os.environ.pop("TRIOFORGE_AUTO_MODELS", None)
            fetched.clear()

            # With ComfyUI-Manager answering, ComfyUI does the download - and it
            # is handed the URL the WORKFLOW declares, not one we chose.
            declared = "https://example.invalid/declared.safetensors"
            with open(fh.name, "w", encoding="utf-8") as f:
                json.dump({"nodes": [{"type": "UNETLoader",
                                      "widgets_values": ["absent.safetensors"]}],
                           "definitions": {"subgraphs": [{"nodes": [
                               {"properties": {"models": [{
                                   "name": "absent.safetensors",
                                   "directory": "unet",
                                   "url": declared}]}}]}]}}, f)
            assert cs.workflow_model_sources(wf) == {
                "absent.safetensors": (declared, "unet")}
            cs.manager_version = lambda *a, **k: "V4.2.2"
            assert cs.ensure_models(wf, oi)[0] == ["absent.safetensors"]
            assert fetched == [("absent.safetensors", declared, "unet")], fetched
        finally:
            cs.MODEL_SOURCES.pop("absent.safetensors", None)
    finally:
        cs.manager_version, cs.install_model, cs.manager_install_model = (
            real_manager, real_install, real_mgr_install)
        try:
            os.unlink(fh.name)
        except OSError:
            pass
    print("  missing models map to a source; unknown ones are reported -> OK")


# ---------------------------------------------------------------------------
def test_comfyui_model_paths() -> None:
    """TrioForge must see the models ComfyUI Desktop actually installed.

    Comfy Desktop keeps models in a SHARED store and points each ComfyUI
    instance at it with --extra-model-paths-config. Starting ComfyUI without
    that flag made TrioForge's own headless server report every model missing
    even though 151 GB of them were installed one directory away - so it
    re-downloaded ~30 GB into a second location. These are the paths that keep
    the two in sync, and the flag is what makes "works without opening ComfyUI".
    """
    _title("comfyui desktop model paths")
    import inspect
    import os

    import comfyui_service as cs

    # Never raises when Desktop is not installed at all (CI, plain ComfyUI).
    assert cs.desktop_model_paths_config() in (None,) or os.path.exists(
        cs.desktop_model_paths_config())
    assert cs.shared_models_dir() in (None,) or os.path.isdir(cs.shared_models_dir())
    roots = cs._model_roots()
    assert isinstance(roots, list)
    assert all(isinstance(r, str) and r for r in roots), roots
    assert len(set(roots)) == len(roots), f"roots must be de-duplicated: {roots}"

    # The install's own models/ folder is always a candidate, shared store or not.
    install = cs.find_comfyui_install()
    if install:
        assert os.path.join(install, "models") in roots, roots

    # The auto-start command MUST pass the config through, or the server it spawns
    # cannot see a single installed model.
    src = inspect.getsource(cs.ensure_comfyui_running)
    assert "--extra-model-paths-config" in src, "auto-start drops Desktop's model paths"
    assert "desktop_model_paths_config()" in src, "the flag is never populated"

    # --cache-none costs reproducibility (measured: with it two identical
    # requests differ, without it they are byte-identical), so the trade-off has
    # to stay switchable rather than be decided silently for the user.
    assert "--cache-none" in src, "the memory fix disappeared"
    assert "TRIOFORGE_COMFYUI_CACHE" in src, "the cache trade-off is not switchable"

    # _models_dir prefers the shared store, so a download joins Desktop's models
    # instead of becoming a second copy in the install. With no ComfyUI at all
    # there is nowhere to put one, and it must say so rather than raise something
    # else - CI has no ComfyUI, so this branch is the one that actually runs there.
    shared = cs.shared_models_dir()
    install = cs.find_comfyui_install()
    if shared:
        assert cs._models_dir() == shared, cs._models_dir()
    elif install:
        assert cs._models_dir() == os.path.join(install, "models"), cs._models_dir()
    else:
        try:
            cs._models_dir()
            raise AssertionError("_models_dir() must refuse when nothing is installed")
        except RuntimeError as e:
            assert "not found" in str(e), e
    print("  desktop shared store is located, and auto-start passes it on -> OK")


# ---------------------------------------------------------------------------
def test_comfyui_manager_model_install() -> None:
    """The Manager v2 API is spoken to correctly, and its failures are reported."""
    _title("comfyui manager model install")
    import comfyui_service as cs

    # URLs must be plain https://huggingface.co/... - Manager downloads only
    # those itself, and sends anything else to its "download by agent" setting.
    url = cs._model_url("Comfy-Org/z_image_turbo", "split_files/vae",
                        "ae.safetensors", "model")
    assert url == ("https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main"
                   "/split_files/vae/ae.safetensors"), url
    # A bucket has no /main/ segment: /resolve/main/<file> answers 404 there.
    bucket = cs._model_url("shawen666/FHDR_Uncensored-bucket", None,
                           "FHDR_ComfyUI-Q4_K_M.gguf", "bucket")
    assert bucket == ("https://huggingface.co/buckets/shawen666/"
                      "FHDR_Uncensored-bucket/resolve/FHDR_ComfyUI-Q4_K_M.gguf"), bucket
    for name in cs.MODEL_SOURCES:
        if name == "clip_l.safetensors":
            continue
        assert " " not in name

    # A missing Manager is "no", not an exception - callers degrade to reporting.
    assert cs.manager_version(timeout=0.01) in (None, "") or isinstance(
        cs.manager_version(timeout=0.01), str)

    # The folder a URL implies is only ever taken from real ComfyUI folders:
    # Manager drops an unknown type into models/etc, where nothing can use it.
    assert cs._directory_from_url(
        "https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/vae/ae.safetensors"
    ) == "vae"
    assert cs._directory_from_url("https://example.invalid/a/b/thing.bin") == ""
    assert cs._directory_from_url("not a url") == ""

    # Every source resolves to a real URL with the filename at the end.
    for name, (repo, repo_path, folder, kind) in cs.MODEL_SOURCES.items():
        built = cs._model_url(repo, repo_path, name, kind)
        assert built.startswith("https://huggingface.co/"), built
        assert built.endswith(name), built
        assert folder and "/" not in folder and "\\" not in folder, folder
    print("  manager URLs, folders and absence handling are all correct -> OK")


# ---------------------------------------------------------------------------
def test_comfyui_unload_is_safe() -> None:
    """unload_models() frees memory without ever raising (server up or down)."""
    _title("comfyui unload models")
    import comfyui_service as cs
    assert cs.unload_models() is None     # best-effort: never raises
    print("  unload_models() is a safe best-effort no-op -> OK")


# ---------------------------------------------------------------------------
def test_gpu_probe_never_blocks() -> None:
    """A stale GPU cache must not block its caller.

    _probe_gpu() spawns llama-server --list-devices and measured 1.4-3.3 s here.
    The terminal client redraws its sidebar every 2 s, which asks for specs, so
    probing inline froze the UI for over a second every 5 s - typing there queued
    up and arrived in bursts (worst keystroke measured: 1655 ms). Stale entries
    are now handed back as-is while a worker thread re-probes.
    """
    _title("GPU probe never blocks the caller")
    import time

    import hardware

    hardware.gpu()                      # warm it: the FIRST call may block
    first = hardware._gpu_cache["value"]
    assert first is not None, "gpu() returned nothing on a machine with a GPU"

    # Make the probe take a SECOND on purpose. Asking "did the caller block?" as a
    # 50 ms stopwatch question is really asking "was this runner idle?": starting
    # the worker thread alone can overrun a tight budget on a loaded runner. A
    # one-second probe makes the question structural - the caller cannot have
    # waited for it and still return in half the time.
    real_probe = hardware._probe_gpu

    def slow_probe():
        time.sleep(1.0)
        return real_probe()

    hardware._probe_gpu = slow_probe
    try:
        hardware._gpu_cache["at"] = 0.0     # force the entry stale
        t0 = time.perf_counter()
        value = hardware.gpu()
        elapsed = time.perf_counter() - t0
    finally:
        hardware._probe_gpu = real_probe
    assert value is not None, "a stale gpu() returned nothing"
    assert elapsed < 0.5, \
        f"a stale gpu() blocked on the {1.0}s probe for {elapsed:.3f}s"
    print(f"  stale entry returned in {elapsed * 1000:.1f} ms, "
          f"not waiting for a 1000 ms probe -> OK")


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

    # The 250 ms timer must survive running BEFORE the widgets exist. It fires soon
    # after mount, while Textual is still mounting the screen's children, and
    # _paint_face's unguarded query_one("#face") then raised NoMatches OUT of the
    # timer - which escaped run_test() and failed this suite on the slowest CI
    # runner only, so it read as a flake for two commits. An app that was never
    # mounted reproduces it deterministically.
    unmounted = ta.ForgeApp(args, cfg, b,
                            sess_mod.Session(model="gemma", where="local", system="s"))
    unmounted._paint_face()          # used to raise NoMatches
    unmounted._refresh()
    unmounted._tick_clock()
    assert unmounted._paint_widget("#nothing-here", "x") is False, \
        "painting a missing widget must report False, not raise"
    print("  the timer survives a tick before the widgets are mounted -> OK")
    async with app.run_test(size=(120, 40)) as pilot:
        # WAIT for the app to finish composing rather than trusting one fixed pause.
        # On a loaded CI runner 0.4 s was not always enough and the suite failed with
        # "No nodes match '#face'" before it had asserted anything - a flake in the
        # harness, not a bug in the app. Poll for the whole set instead.
        wanted = ("#chat", "#side", "#sidebody", "#face", "#activity",
                  "#statusline", "#prompt", "#keys")
        missing = list(wanted)
        for _ in range(80):
            missing = [sel for sel in wanted if not app.query(sel)]
            if not missing:
                break
            await pilot.pause(0.1)

        # Report WHICH are absent. "No nodes match '#face'" on its own could not say
        # whether compose had not finished or had stopped EARLY; the missing set can -
        # it names every widget from the failure onwards, in compose order.
        assert not missing, f"widgets never mounted: {missing}"
        keys = app.query_one("#keys")
        assert keys.region.y + keys.region.height <= 40, keys.region
        print("  all widgets mount; #keys is on screen -> OK")

        async def settle(pred, tries=60, step=0.1):
            """Wait until ``pred()`` holds - never trust a single fixed pause.

            Every ``await pilot.pause(0.2)`` is a bet that one message round-trip
            finished inside 200 ms. On the slowest CI runner that bet lost: the
            suite failed there twice while the faster ones passed, and one of
            those was a docs-only commit - so it was a timing race, not a
            regression in the code under test.
            """
            for _ in range(tries):
                if pred():
                    return True
                await pilot.pause(step)
            return bool(pred())

        # /help lists the terminal-only commands with an example
        app._command("/help")
        assert await settle(lambda: "/team" in _last_card(app)), _last_card(app)[:160]
        body = _last_card(app)
        for want in ("/team", "/auto", "/route", "/roles", "e.g. /team"):
            assert want in body, want
        app._command("/help /roles")
        assert await settle(lambda: "/roles" in _last_card(app)), _last_card(app)[:160]
        print("  /help lists /team,/auto,/route,/roles + examples; /help <cmd> -> OK")

        # Slash autocomplete: the Claude-Code-style list that appears while a
        # bare command is being typed. Matching is pure, so it is asserted here
        # with no UI; the widget wiring is asserted below.
        from tui.commands import slash_commands, slash_matches
        assert len(slash_matches("/")) == len(slash_commands()) > 20, \
            (len(slash_matches("/")), len(slash_commands()))
        assert [n for n, _d in slash_matches("/ro")] == ["/route", "/roles"], \
            slash_matches("/ro")
        # An exact name KEEPS its row: hiding it made the list vanish the moment
        # the command was finished, which reads as broken. Note "/set" is also a
        # PREFIX of "/setup", so both match and the row the user actually typed
        # has to be the selected one - otherwise enter runs "/setup".
        assert [n for n, _d in slash_matches("/set")] == ["/setup", "/set"], \
            slash_matches("/set")
        assert [n for n, _d in slash_matches("/roles")] == ["/roles"], slash_matches("/roles")
        assert slash_matches("/roles now") == [], "a space means arguments"
        assert slash_matches("hello") == [], slash_matches("hello")
        assert slash_matches("/zzz") == [], slash_matches("/zzz")

        menu = app.query_one("#slashmenu", ta.SlashMenu)
        prompt = app.query_one("#prompt", ta.PromptArea)
        prompt.text = "/ro"
        assert await settle(lambda: prompt.menu_open and menu.option_count == 2), \
            f"menu did not open on /ro: open={prompt.menu_open} rows={menu.option_count}"
        assert menu.has_class("-on"), "menu reports open but is not painted"
        assert menu.chosen == "/route", menu.chosen

        # Layout: the list sits ABOVE the prompt (so it never covers the line
        # being typed) and is fully on screen - the old fixed footer height once
        # pushed #keys below the viewport, so this is worth pinning.
        assert menu.region.y + menu.region.height <= prompt.region.y, \
            (menu.region, prompt.region)
        assert menu.region.height >= 2, menu.region
        assert prompt.region.y + prompt.region.height <= 40, prompt.region

        # ...and it must NOT resize as the filter narrows. With height: auto the
        # box tracked the match count (29 -> 8 -> 3 rows), which dragged the
        # transcript viewport with it and repainted the whole conversation on
        # every keystroke: the line being typed looked written-then-wiped.
        chat = app.query_one("#chat")
        settled = (menu.region.y, menu.region.height, chat.region.height)
        for narrowing in ("/s", "/se"):
            want_rows = len(slash_matches(narrowing))
            prompt.text = narrowing
            assert await settle(lambda: menu.option_count == want_rows), \
                f"{narrowing}: menu shows {menu.option_count}, expected {want_rows}"
            moved = (menu.region.y, menu.region.height, chat.region.height)
            assert moved == settled, f"{narrowing} reflowed the layout: {settled} -> {moved}"

        prompt.text = "/ro"          # back to two matches for the tests below
        assert await settle(lambda: menu.option_count == 2 and menu.chosen == "/route"), \
            f"menu did not come back to /ro: {menu.option_count} rows, {menu.chosen}"

        # down moves the highlight; tab fills the name in WITHOUT running it
        app._menu_key(ta.PromptArea.MenuKey(prompt, "down"))
        assert menu.chosen == "/roles", menu.chosen
        app._menu_key(ta.PromptArea.MenuKey(prompt, "accept"))
        assert prompt.text == "/roles", repr(prompt.text)
        # The completed name is an exact command, so its own row stays selected
        # rather than the list closing under the user.
        assert prompt.menu_open and menu.chosen == "/roles", menu.chosen

        # escape closes the list for that text only - typing more brings it back
        prompt.text = "/mo"
        assert await settle(lambda: prompt.menu_open and menu.has_class("-on")), \
            (prompt.text, prompt.menu_open, menu.has_class("-on"))
        app._menu_key(ta.PromptArea.MenuKey(prompt, "dismiss"))
        assert not prompt.menu_open, "escape did not close the menu"
        # ...and it must be off SCREEN too, not just un-flagged: leaving the rows
        # painted meant a visible list that no longer answered any key.
        assert not menu.has_class("-on"), "escape left the list painted"
        assert menu.option_count == 0, menu.option_count
        prompt.text = "/mod"
        assert await settle(lambda: prompt.menu_open), \
            "typing after escape did not bring the menu back"

        # enter runs the highlighted command. /help on purpose: a command that
        # opens a modal would move focus and hide a real key-routing bug.
        prompt.text = "/he"
        assert await settle(lambda: menu.chosen == "/help"), menu.chosen
        app._menu_key(ta.PromptArea.MenuKey(prompt, "run"))
        assert await settle(lambda: prompt.text == "" and not prompt.menu_open), \
            (prompt.text, prompt.menu_open)
        assert app.focused is prompt, "running left focus off the prompt"
        print("  slash autocomplete filters, completes, escapes, runs -> OK")

        # The REAL keys must reach the list. The prompt keeps focus while typing,
        # so if TextArea ate them the caret would move / focus would leave the box
        # instead - this is the part that cannot be checked by calling handlers.
        prompt.text = "/ro"
        assert await settle(lambda: prompt.menu_open and menu.chosen == "/route"), \
            "menu did not reopen after the run"
        await pilot.press("down")
        assert await settle(lambda: menu.chosen == "/roles"), \
            "down did not move the highlight"

        await pilot.press("tab")                  # tab fills the highlight in
        assert await settle(lambda: prompt.text == "/roles"), repr(prompt.text)
        # The finished command keeps its row - this was the reported bug: the
        # list vanished the instant the name was complete.
        assert prompt.menu_open and menu.chosen == "/roles", menu.chosen
        assert menu.option_count == 1, menu.option_count

        # The reported bug, typed for real: completing a command name must not
        # empty the list. "/se" -> "t" used to make every row disappear. "/set"
        # also matches "/setup", and the SELECTED row must still be "/set".
        prompt.text = "/se"
        # /setup, /search and /set all start with "/se"
        assert await settle(lambda: prompt.menu_open and menu.option_count == 3), \
            (prompt.menu_open, menu.option_count)
        # setting .text leaves the caret at column 0; a typist's is at the end
        prompt.move_cursor((0, len(prompt.text)))
        await pilot.press("t")
        assert await settle(lambda: prompt.text == "/set"), repr(prompt.text)
        assert prompt.menu_open, "the list vanished on a complete command"
        assert menu.chosen == "/set", menu.chosen
        assert menu.option_count == 2, menu.option_count

        # escape closes the list and leaves the typed text alone
        prompt.text = "/mo"
        assert await settle(lambda: prompt.menu_open), (prompt.text, prompt.menu_open)
        await pilot.press("escape")
        assert await settle(lambda: not prompt.menu_open and not menu.has_class("-on")), \
            (prompt.menu_open, menu.has_class("-on"))
        assert prompt.text == "/mo", "escape wiped the typed text"

        # ...and it returns as soon as the text changes again
        prompt.text = "/mod"
        assert await settle(lambda: prompt.menu_open), \
            "typing after escape did not reopen the list"
        await pilot.press("tab")                  # this one takes args -> a space
        assert await settle(lambda: prompt.text == "/model "), repr(prompt.text)

        prompt.text = "/he"          # enter RUNS the highlighted command
        assert await settle(lambda: menu.chosen == "/help"), menu.chosen
        await pilot.press("enter")
        assert await settle(lambda: prompt.text == "" and not prompt.menu_open), \
            (prompt.text, prompt.menu_open)
        assert "/help" in _last_card(app) or "commands" in _last_card(app)
        print("  slash autocomplete answers real up/down/tab/escape/enter -> OK")

        # The sidebar is a FIXED 36 columns, so any value longer than its text
        # width used to WRAP: a long .gguf name rendered as
        # "gemma-4-E4B-it-ultra-uncensored-h" / "eretic", and the wrapped row
        # pushed its "5.4 GB" line out of line. Every value goes through _fit().
        from rich.cells import cell_len

        class _Long:
            def __init__(self, name, size, caps):
                self.name, self.size_gb, self.caps_label = name, size, caps

        long_models = [
            _Long("gemma-4-E4B-it-ultra-uncensored-heretic", 5.4,
                  "text+image+video+audio"),
            _Long("VideoGuard-Qwen3.5-9B-Safety-RL-Uncensored_Q4_K_M", 5.2, "Video"),
            _Long("a-model-name-that-just-keeps-going-and-going-Q8_0", 9.9, "text"),
        ]
        real_models, real_model = app._models, app.session.model
        app._models = lambda: long_models
        # A Windows model path must reduce to the NAME: rsplit("/") alone left the
        # whole "D:\...\Ornith-1.5-9B-Q4_K_M.gguf" here, which is what wrapped.
        app.session.model = (r"D:\models\Ornith"
                             r"\Ornith-1.5-9B-Q4_K_M.gguf")
        assert app._short_model() == "Ornith-1.5-9B-Q4_K_M", app._short_model()
        body = app._sidebar()
        over = [str(line) for line in body.split("\n", allow_blank=True)
                if line.cell_len > ta.SIDEBAR_TEXT]
        assert not over, f"sidebar lines wider than {ta.SIDEBAR_TEXT}: {over}"
        # _fit counts CELLS, not characters. 💻 is two columns wide (len() says
        # one), and how wide an emoji draws varies by terminal and platform - so
        # a len()-based cut could still overflow by a column on Linux or macOS
        # while looking fine on Windows. This runs in CI on both.
        assert cell_len("💻") == 2, cell_len("💻")
        assert cell_len(ta._fit("💻" * 20, 10)) <= 10, ta._fit("💻" * 20, 10)
        assert cell_len(ta._fit("x" * 200)) <= ta.SIDEBAR_TEXT
        app._models, app.session.model = real_models, real_model
        print("  sidebar fits its 34 columns; Windows model paths shorten -> OK")

        # ...and the panel steps aside when the window cannot afford both panes.
        # At 60 columns it kept its 36 and left the transcript 24. Poll for the
        # relayout instead of trusting one pause - same reason as the compose wait.
        await pilot.resize_terminal(60, 20)
        for _ in range(30):
            if not app.query_one("#side").display:
                break
            await pilot.pause(0.1)
        assert not app.query_one("#side").display, "sidebar crushed the chat at 60 cols"
        assert app.query_one("#chat").region.width > 40, "chat did not take the room"
        await pilot.resize_terminal(120, 40)
        for _ in range(30):
            if app.query_one("#side").display:
                break
            await pilot.pause(0.1)
        assert app.query_one("#side").display, "sidebar did not come back on grow"
        print("  sidebar hides below 80 cols and returns on resize -> OK")

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

        # roles mode: the pipeline is pure and testable with no model. Each role's
        # prompt must carry its directive, the task, and only the artefacts that
        # came before it (planner sees nothing, coder sees the plan, reviewer sees
        # plan + code, fixer sees plan + code + issues).
        from tui import team_roles as _roles
        assert _roles.ROLE_ORDER == ("planner", "coder", "reviewer", "fixer")
        task = "add a /health route to the app"
        plan = "1. view py/app.py\n2. add the route\n3. run the server"
        code = "added /health returning ok"
        issues = "ISSUES:\n1. /health returns 200 even when the db is down"
        p_plan = _roles.role_prompt("planner", task, {})
        assert "PLANNER" in p_plan and task in p_plan and plan not in p_plan
        p_coder = _roles.role_prompt("coder", task, {"plan": plan})
        assert plan in p_coder and "CODER" in p_coder
        p_review = _roles.role_prompt("reviewer", task, {"plan": plan, "code": code})
        assert "CODE" in p_review and code in p_review and "REVIEWER" in p_review
        p_fix = _roles.role_prompt("fixer", task, {"plan": plan, "code": code, "issues": issues})
        assert "ISSUES" in p_fix and issues in p_fix and "FIXER" in p_fix
        # the senior/junior split is explicit: senior plans+reviews, junior codes+fixes
        assert "SENIOR" in p_plan and "SENIOR" in p_review
        assert "JUNIOR" in p_coder and "JUNIOR" in p_fix
        assert _roles.ROLE_AGENT == {"planner": "senior", "coder": "junior",
                                     "reviewer": "senior", "fixer": "junior"}
        print("  roles pipeline prompts carry directive + task + prior artefacts -> OK")
        print("  roles are an explicit senior/junior pair -> OK")

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
        assert await settle(lambda: "has to run on the CPU" in _last_card(app)), \
            _last_card(app)[:160]
        app._warned_load = None
        app._load_plan = svc._plan_load(int(6.8 * GB), int(7.51 * GB),
                                        int(11.96 * GB), 0, ngl_auto=True)
        n = len(_cards(app))
        app._warn_if_slow_load()
        # A NEGATIVE assertion, so there is nothing to poll for - waiting longer is
        # the only way to mean "and nothing turned up".
        await pilot.pause(0.6)
        assert len(_cards(app)) == n, "warned about a model that fits"
        print("  split load warns once; a fitting model is left alone -> OK")

        # the scrollbar is continuous and the layout does not shift
        chat = app.query_one("#chat", ta.VerticalScroll)
        card = app.query_one("#chat Markdown")
        fits = (chat.virtual_size.width, card.size.width, card.region.x)
        before_cards = len(_cards(app))
        for n2 in range(30):
            app._add_plain(f"filler {n2}")
        # Wait for the MOUNTS (which are what trigger the re-layout) instead of
        # guessing a duration for them.
        assert await settle(lambda: len(_cards(app)) == before_cards + 30), \
            f"cards did not mount: {len(_cards(app))} vs {before_cards + 30}"
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
    # "error: C:\Users\someone is a folder, not a file". Now an empty path says so.
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


def test_launcher_recognises_its_own_exe() -> None:
    """The single-instance guard must recognise the SHIPPED launcher's name.

    The allow-list in _pid_is_our_app() read "triorforge.exe" - one extra 'r', 14
    characters where the real file is 13. _pid_is_our_app() therefore always
    returned False for the launcher that actually ships, so:
      * the single-instance guard never fired,
      * every second click deleted the marker and started a WHOLE second instance,
      * the two fought over port 5003 and the one locked WebView2 profile, so
        neither window painted - the reported "I launch it and it does not respond".

    test_window_slot_is_exclusive() did not catch it because it plays the live
    owner with sys.executable (python.exe), which IS in the list. This test uses
    the real launcher name, which is the string that was wrong.
    """
    _title("launcher recognises its own executable")
    from tools import app_window, autostart

    names = app_window._OUR_EXE_NAMES
    assert "python.exe" in names and "pythonw.exe" in names, names

    # The launcher name must be DERIVED from the product name, so the typo that
    # caused this cannot be reintroduced. Any literal here is a future outage.
    derived = [n for n in names if n not in ("python.exe", "pythonw.exe")]
    assert derived == [app_window.APP_NAME.lower() + ".exe"], derived
    assert app_window.APP_NAME == autostart.APP_NAME, (
        app_window.APP_NAME, autostart.APP_NAME)

    # And it must match the launcher that actually ships on disk.
    root = Path(__file__).resolve().parent.parent
    shim = root / ".venv" / "Scripts" / (app_window.APP_NAME + ".exe")
    if shim.is_file():
        assert shim.name.lower() in names, (shim.name, names)
        assert len(shim.name) == len(app_window.APP_NAME) + 4, shim.name
    else:
        # No shim on this machine: the derivation still has to be self-consistent.
        assert (app_window.APP_NAME + ".exe").lower() in names, names

    print("  allow-list derives {!r} and matches the shipped launcher -> OK".format(
        app_window.APP_NAME.lower() + ".exe"))


# ---------------------------------------------------------------------------
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
                _csrf_ok(client)          # state-changing, so it needs the token
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

            # The shipped catalog is browsable and installs as a local copy.
            cat = {c["id"]: c for c in extensions.catalog()}
            assert "refactor" in cat and cat["refactor"]["installed"] is False, cat
            res = extensions.install("catalog:refactor")
            assert res.get("ok") and "refactor" in res.get("skills", []), res
            assert skills_loader.get("refactor") is not None
            assert {c["id"]: c for c in extensions.catalog()}["refactor"]["installed"] is True
            assert "error" in extensions.install("catalog:does-not-exist")
        finally:
            skills_loader.SKILLS_DIR = real_skill_dir
            extensions.SKILLS_DIR = real_skill_dir
            extensions.PLUGINS_DIR = real_plugin_dir
            skills_loader.load_all()
    print("  inventory, toggle, install-from-folder, remove -> OK")


# ---------------------------------------------------------------------------
def test_tui_plugin_commands() -> None:
    _title("tui plugin commands (/plugins, /connectors)")
    import contextlib
    import io
    from tui import commands

    # Discoverable through the same table /help and the palette read.
    assert commands.lookup("plugins") is not None
    assert commands.lookup("connectors") is not None
    assert "/plugins" in commands._TABLE and "/connectors" in commands._TABLE

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.session = ctx.cfg = ctx.args = ctx.backend = ctx.real_backend = None
    ctx.running = True
    ctx.is_tui = True

    # Skip the lazy loader's real network/subprocess side effects (it would fetch
    # MCP servers and re-import plugins); the command plumbing is under test, not
    # the loaders themselves - those are covered by test_extensions / test_mcp_client.
    commands._ext_ready = True
    try:
        for line in ("/plugins browse", "/plugins", "/plugins disable skill nope"):
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                assert commands.handle(line, ctx) is True
    finally:
        commands._ext_ready = False
    print("  /plugins + /connectors discoverable and runnable -> OK")


# ---------------------------------------------------------------------------
def test_obsidian_connector() -> None:
    _title("obsidian connector (reads and writes a vault)")
    import importlib.util
    import tempfile

    repo = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("obs_probe", repo / "plugins" / "obsidian.py")
    mod = importlib.util.module_from_spec(spec)
    # Load the file WITHOUT running register(): the test wants the vault helpers,
    # not a Flask route table.
    spec.loader.exec_module(mod)

    with tempfile.TemporaryDirectory() as vault, tempfile.TemporaryDirectory() as cfgdir:
        mod._CRED_PATH = os.path.join(cfgdir, "obsidian_credentials.json")
        os.makedirs(os.path.join(vault, ".obsidian"))
        os.makedirs(os.path.join(vault, "Data"))
        with open(os.path.join(vault, "Essay.md"), "w", encoding="utf-8") as fh:
            fh.write("# Essay\n\nSomething about memory management and caches.\n")
        with open(os.path.join(vault, "Data", "notes.md"), "w", encoding="utf-8") as fh:
            fh.write("# Notes\n")
        with open(os.path.join(vault, ".obsidian", "app.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")

        mod._save_cfg({"vault": vault})

        st = mod.dispatch("obsidian_status", {})
        assert st["connected"] is True, st
        assert st["notes"] == 2, st

        # .obsidian is not a note, and never leaks into the listing.
        listing = mod.dispatch("obsidian_list", {})
        assert listing["count"] == 2, listing
        assert listing["notes"] == ["Data/notes.md", "Essay.md"], listing["notes"]
        assert mod.dispatch("obsidian_list", {"folder": "Data"})["count"] == 1

        got = mod.dispatch("obsidian_read", {"note": "Essay.md"})
        assert "memory management" in got["content"], got
        # The extension is optional, and a missing note is an error not a crash.
        assert mod.dispatch("obsidian_read", {"note": "Data/notes"})["note"] == "Data/notes.md"
        assert "error" in mod.dispatch("obsidian_read", {"note": "nope.md"})

        # The model supplies these paths, so confinement is the security boundary.
        for bad in ("../escape", "..\\escape", "Data/../../escape",
                    "D:/windows/evil", "/etc/passwd", ".obsidian/app"):
            res = mod.dispatch("obsidian_read", {"note": bad})
            assert "error" in res, "should have refused {!r}: {}".format(bad, res)

        # Write creates, append preserves what was there.
        res = mod.dispatch("obsidian_write", {"note": "New Note", "content": "hello\n"})
        assert res["created"] is True and res["note"] == "New Note.md", res
        assert os.path.isfile(os.path.join(vault, "New Note.md"))

        res = mod.dispatch("obsidian_append", {"note": "Essay.md", "content": "appended line"})
        assert res["action"] == "appended", res
        after = mod.dispatch("obsidian_read", {"note": "Essay.md"})["content"]
        assert "memory management" in after and "appended line" in after, after
        assert mod.dispatch("obsidian_list", {})["count"] == 3

        # Search hits names and content.
        found = mod.dispatch("obsidian_search", {"query": "memory"})
        assert any(m["note"] == "Essay.md" for m in found["match_content"]), found
        by_name = mod.dispatch("obsidian_search", {"query": "notes"})
        assert "Data/notes.md" in by_name["match_name"], by_name

        assert "error" in mod.dispatch("obsidian_not_a_tool", {})
        assert mod.connect_info()["connected"] is True

        # Unset vault -> a clear message, not a stack trace.
        mod._save_cfg({"vault": ""})
        mod._detect_vault = lambda: None
        st = mod.dispatch("obsidian_status", {})
        assert st["connected"] is False and "vault" in st["error"].lower(), st
    print("  status, list, read/write/append, search, traversal refused -> OK")


# ---------------------------------------------------------------------------
def test_shipped_plugins_load() -> None:
    """Every plugin that ships must load - and register() must not collide.

    Flask keys routes by the view function's NAME, not by path, so two plugins
    that both define `_status_route` cannot coexist: the second one fails to
    register and is silently dropped from the app. That actually happened when
    the Obsidian connector was added next to Gmail, and nothing caught it because
    each plugin passed its own tests in isolation. This is the test that does.
    """
    _title("every shipped plugin loads")
    import plugin_loader
    from flask import Flask

    app = Flask("regress-plugins")
    results = plugin_loader.load_all(app)
    broken = [(r.get("id"), r.get("error")) for r in results if r.get("error")]
    assert not broken, "these plugins failed to load: {}".format(broken)

    ids = sorted(r["id"] for r in results)
    assert "gmail" in ids and "obsidian" in ids, ids
    assert "discord" in ids, ids

    # Both connectors reach the agent as callable tools.
    names = {t["function"]["name"] for t in plugin_loader.collect_tools()}
    for expected in ("gmail_status", "obsidian_status", "obsidian_read",
                     "discord_send", "discord_status"):
        assert expected in names, "{} missing from {}".format(expected, sorted(names))

    # The connector card fetches these for EVERY connector, so a missing one is a
    # button that 404s. This is the route-contract half of the Obsidian bug: the
    # card assumed Gmail's routes and would have broken on any other connector.
    rules = {str(r.rule) for r in app.url_map.iter_rules()}
    for info in results:
        if not info.get("connector"):
            continue
        for suffix in ("status", "credentials", "disconnect"):
            want = "/api/connectors/{}/{}".format(info["id"], suffix)
            assert want in rules, "connector '{}' has no route {}".format(info["id"], want)
    print("  {} plugin(s) loaded, no route-name collisions -> OK".format(len(results)))


# ---------------------------------------------------------------------------
def test_failures_are_visible() -> None:
    """A plugin or skill that fails to load must be VISIBLE, not just absent.

    This is the bug class behind the Obsidian connector going missing: the loader
    knew it had failed, logged a warning, and then every UI-facing list omitted
    it. "Not installed" and "installed but broken" need completely different
    fixes, so the second one has to be shown with its reason.
    """
    _title("a broken extension is visible, not silently absent")
    import tempfile
    import extensions
    import plugin_loader
    import skills_loader
    from flask import Flask

    real = (plugin_loader.PLUGINS_DIR, skills_loader.SKILLS_DIR,
            extensions.PLUGINS_DIR, extensions.SKILLS_DIR)
    with tempfile.TemporaryDirectory() as pl, tempfile.TemporaryDirectory() as sk:
        plugin_loader.PLUGINS_DIR = pl
        skills_loader.SKILLS_DIR = sk
        extensions.PLUGINS_DIR = pl
        extensions.SKILLS_DIR = sk
        try:
            # A plugin that raises on import.
            with open(os.path.join(pl, "brokenplugin.py"), "w", encoding="utf-8") as fh:
                fh.write("MANIFEST = {'name': 'brokenplugin'}\nraise RuntimeError('boom')\n")
            # Two skills claiming one name - the second cannot be loaded.
            for folder in ("dup-a", "dup-b"):
                os.makedirs(os.path.join(sk, folder))
                with open(os.path.join(sk, folder, "SKILL.md"), "w", encoding="utf-8") as fh:
                    fh.write("---\nname: duplicate\ndescription: x\n---\nbody\n")

            results = plugin_loader.load_all(Flask("probe-fail"))
            assert any(r.get("error") for r in results), results
            assert not any(p["id"] == "brokenplugin" for p in plugin_loader.list_loaded())
            failed = plugin_loader.list_failed()
            assert failed and "boom" in failed[0].get("error", ""), failed

            skills_loader.load_all()
            assert len(skills_loader.all_skills()) == 1, skills_loader.all_skills()
            assert skills_loader.list_failed(), "the duplicate skill must be recorded"

            inv = extensions.inventory()
            broken_plugins = [e for e in inv if e.get("kind") == "plugin" and e.get("failed")]
            broken_skills = [e for e in inv if e.get("kind") == "skill" and e.get("failed")]
            assert broken_plugins, "a broken plugin must appear in the inventory"
            assert "boom" in broken_plugins[0]["error"], broken_plugins[0]
            assert broken_skills, "a broken skill must appear in the inventory"
            assert "duplicate" in broken_skills[0]["error"].lower(), broken_skills[0]
        finally:
            (plugin_loader.PLUGINS_DIR, skills_loader.SKILLS_DIR,
             extensions.PLUGINS_DIR, extensions.SKILLS_DIR) = real
            plugin_loader.load_all(Flask("probe-fail-restore"))
            skills_loader.load_all()
    print("  loader failure + duplicate name both reach the panel -> OK")


# ---------------------------------------------------------------------------
def test_tool_calls_are_recorded() -> None:
    """Tool calls - especially connector ones - are saved in conversations.db.

    Before this, a turn that read the user's Gmail or Obsidian vault left only
    prose behind: the messages table had the answer and nothing about how it was
    produced, so "did it actually read my notes?" was unanswerable.
    """
    _title("tool calls are saved in conversations.db")
    import app as appmod

    conv = "regress-toolcalls"
    try:
        appmod.start_tool_tracking(conv)
        res = appmod._execute_tool("list_skills", {})
        assert isinstance(res, dict), res

        tracked = appmod.tracked_tool_calls()
        assert tracked and tracked[0]["tool"] == "list_skills", tracked
        assert tracked[0]["kind"] == "skill", tracked[0]

        rows = appmod.tool_calls_for(conv)
        assert rows and rows[-1]["tool"] == "list_skills", rows
        assert rows[-1]["source_kind"] == "skill", rows[-1]
        assert rows[-1]["ok"] is True

        # A source is tagged, so a connector's use is distinguishable from a guess.
        kinds = {n: appmod._tool_source(n, {})[0]
                 for n in ("list_skills", "read_file", "mcp__memory__read_graph")}
        assert kinds["list_skills"] == "skill", kinds
        assert kinds["read_file"] == "workspace", kinds
        assert kinds["mcp__memory__read_graph"] == "mcp", kinds

        # Arguments are logged, with secrets replaced rather than stored.
        appmod.record_tool_call("probe", "connector", "probeid",
                                {"api_key": "sk-do-not-store", "query": "hi"},
                                {"result": "fine"}, 12)
        row = appmod.tool_calls_for(conv)[-1]
        assert row["arguments"]["api_key"] == "***", row["arguments"]
        assert row["arguments"]["query"] == "hi", row["arguments"]
        assert row["source_id"] == "probeid"

        # A failing call is recorded as failed, not silently dropped.
        appmod.record_tool_call("probe", "connector", "probeid", {},
                                {"error": "vault missing"}, 3)
        last = appmod.tool_calls_for(conv)[-1]
        assert last["ok"] is False and "vault missing" in (last["error"] or ""), last
    finally:
        with appmod._sqlite_lock:
            appmod._sqlite_conn.execute(
                "DELETE FROM tool_calls WHERE conversation_id = ?", (conv,))
            appmod._sqlite_conn.commit()
    assert appmod.tool_calls_for(conv) == [], "the test must clean up after itself"
    print("  connector/tool calls, source tags, redacted args -> OK")


# ---------------------------------------------------------------------------
# vision — a path in a message becomes an image the model can actually see
# ---------------------------------------------------------------------------
def _tiny_png(path: Path) -> None:
    """A real 2x2 PNG, built by hand so the test needs no image library."""
    import struct
    import zlib

    raw = b"".join(b"\x00" + bytes([255, 0, 0, 0, 255, 0]) for _ in range(2))

    def chunk(tag: bytes, body: bytes) -> bytes:
        block = tag + body
        return (struct.pack(">I", len(body)) + block
                + struct.pack(">I", zlib.crc32(block) & 0xFFFFFFFF))

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b""))


def test_vision_paths() -> None:
    _title("images by path")
    import tempfile

    from tui import vision
    from tui.commands import is_command
    from tui.tools import t_view

    def names(paths):
        return [Path(p).name for p in paths]

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _tiny_png(root / "shot.png")
        _tiny_png(root / "second.png")
        (root / "notes.txt").write_text("not an image", encoding="utf-8")

        # An absolute path, a folder, and a quoted path containing spaces.
        assert names(vision.paths_in(f"{root}/shot.png what is this?")) == ["shot.png"]
        assert names(vision.paths_in(f"describe {root}")) == ["second.png", "shot.png"]
        spaced = root / "my pics"
        spaced.mkdir()
        _tiny_png(spaced / "a.png")
        assert names(vision.paths_in(f'look at "{spaced}/a.png"')) == ["a.png"]

        # A text file, and a plain sentence, name no image.
        assert vision.paths_in(f"read {root}/notes.txt") == []
        assert vision.paths_in("hello there") == []
        print("  file / folder / quoted paths found; text and chat ignored -> OK")

        # The message goes multimodal; a message with no image is left alone.
        msg = vision.attach({"role": "user", "content": f"what is {root}/shot.png?"})
        kinds = [p["type"] for p in msg["content"]]
        assert kinds[0] == "text" and "image_url" in kinds, kinds
        url = [p for p in msg["content"] if p["type"] == "image_url"][0]["image_url"]["url"]
        assert url.startswith("data:image/png;base64,"), url[:40]
        plain = vision.attach({"role": "user", "content": "hi"})
        assert isinstance(plain["content"], str), "a plain chat must stay a string"
        print("  image attaches as a data: image_url part -> OK")

        # Pixels read as text are noise, and a model asked to explain noise invents
        # an answer ("it appears to be an image of a cat"). view must refuse.
        refusal = t_view(str(root / "shot.png"))
        assert "is an image" in refusal and "NOT guess" in refusal, refusal
        print("  view refuses an image instead of inventing a description -> OK")

    # A path is not a slash command: pasting one used to answer "unknown command".
    assert is_command("/model") is True
    assert is_command("/home/tc/Pictures/shot.png") is False
    print("  an absolute path is not a slash command -> OK")


# ---------------------------------------------------------------------------
# role alternation — gemma's chat template rejects two "user" turns in a row
# ---------------------------------------------------------------------------
def test_role_alternation() -> None:
    _title("role alternation (gemma's chat template)")
    from tui.agent import coalesce

    # The exact shape that produced HTTP 400: one round with two tool calls, each
    # result appended as a user message, then the anti-stall nudge right after.
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "fix the button"},
        {"role": "assistant", "content": "looking"},
        {"role": "user", "content": '<tool_result name="view">a</tool_result>'},
        {"role": "user", "content": '<tool_result name="grep">b</tool_result>'},
        {"role": "assistant", "content": "writing"},
        {"role": "user", "content": '<tool_result name="write">ok</tool_result>'},
        {"role": "user", "content": "STOP READING. Act now."},
    ]
    out = coalesce(msgs)
    roles = [m["role"] for m in out]
    assert not any(roles[i] == "user" and roles[i + 1] == "user"
                   for i in range(len(roles) - 1)), roles
    flat = "\n".join(str(m["content"]) for m in out)
    assert all(t in flat for t in ("view", "grep", "ok", "STOP READING")), flat
    print("  consecutive users merged, nothing lost -> OK")

    # The sliding window trims by size, so it can begin mid-pair. Gemma needs the
    # conversation to start with a user turn.
    trimmed = [{"role": "system", "content": "s"},
               {"role": "assistant", "content": "old answer"},
               {"role": "user", "content": "hi"}]
    assert [m["role"] for m in coalesce(trimmed)] == ["system", "user"]
    print("  a window starting on an answer drops it -> OK")

    # An attached image must survive the merge as a PART, not be stringified.
    merged = coalesce([
        {"role": "user", "content": [{"type": "text", "text": "a"},
                                     {"type": "image_url",
                                      "image_url": {"url": "data:x"}}]},
        {"role": "user", "content": "and this"},
    ])
    assert [p["type"] for p in merged[0]["content"]] == ["text", "image_url", "text"]
    print("  a multimodal turn stays multimodal -> OK")


# ---------------------------------------------------------------------------
# run.sh through a symlink — the `trioforge-web` command depends on it
# ---------------------------------------------------------------------------
def test_launcher_symlink_resolution() -> None:
    """`run.sh` must find the REPO through a symlink, not the link's directory.

    install.sh drops `trioforge-web` in ~/.local/bin pointing at run.sh. Because
    run.sh used `cd "$(dirname "$0")"`, $0 was the LINK, so the project root became
    ~/.local/bin and creating the venv died with "Read-only file system".
    """
    _title("run.sh through a symlink (trioforge-web)")
    import subprocess
    import tempfile

    root = Path(__file__).resolve().parent.parent
    if not (root / ".venv-linux" / "bin" / "python").exists():
        print("  skipped (no .venv-linux yet - run.sh has not been run here)")
        return

    with tempfile.TemporaryDirectory() as tmp:
        link = Path(tmp) / "trioforge-web"
        link.symlink_to(root / "run.sh")
        try:
            proc = subprocess.run([str(link), "--status"], cwd=tmp, timeout=240,
                                  capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            raise AssertionError("run.sh through a symlink did not finish")
        out = (proc.stdout or "") + (proc.stderr or "")
        assert "Read-only file system" not in out, out[-400:]
        assert str(root) in out, out[-400:]      # it found the real project root
        print("  resolves the repo root through the link -> OK")


# ---------------------------------------------------------------------------
# CSRF — a state-changing request must prove it came from our own page
# ---------------------------------------------------------------------------
def test_csrf_protection() -> None:
    _title("CSRF on state-changing requests")
    import app as forge_app

    with forge_app.app.test_client() as client:
        # No token: refused, and told how to get one.
        r = client.post("/api/design/run", json={"project": "x", "entry": "y.py"})
        assert r.status_code == 403, r.status_code
        assert "CSRF" in ((r.get_json() or {}).get("error") or ""), r.get_json()
        print("  a tokenless POST is refused with 403 -> OK")

        # With the token: past the gate and into the route, which answers
        # "no such project" rather than 403.
        token = _csrf_ok(client)
        assert token, "no token was handed out"
        r = client.post("/api/design/run",
                        json={"project": "definitely-missing", "entry": "y.py"})
        assert r.status_code != 403, r.status_code
        print("  the same POST with X-CSRF-Token reaches the route -> OK")

        # Reading never needs one.
        assert client.get("/api/csrf").status_code == 200
        print("  GET is unaffected -> OK")

    # The exemptions, checked on the gate itself: calling /v1/ for real would
    # start llama-server, which a test has no business doing.
    with forge_app.app.test_request_context("/v1/chat/completions", method="POST"):
        assert forge_app._csrf_protect() is None
    with forge_app.app.test_request_context("/login", method="POST"):
        assert forge_app._csrf_protect() is None
    with forge_app.app.test_request_context("/api/design/run", method="POST"):
        assert forge_app._csrf_protect() is not None
    print("  /v1/ and /login exempt; everything else guarded -> OK")

    # ── The pages that are their OWN documents must ship a token too ──────────
    # The shell wraps window.fetch for itself; a page in an iframe (or opened
    # directly) has its own window and sent nothing, so EVERY save on the Cork
    # Board and in Notes came back "403 invalid or missing CSRF token": no pin
    # could be created or edited, no thread drawn, no note saved or deleted.
    # Nothing noticed because reads kept working - the board still drew itself.
    with forge_app.app.test_client() as client:
        token = _csrf_ok(client)
        for page in ("/corkboard", "/notes"):
            page_html = client.get(page)
            assert page_html.status_code == 200, (page, page_html.status_code)
            assert "/static/csrf-fetch.js" in page_html.get_data(as_text=True), \
                "%s does not load the CSRF wrapper, so its saves are all 403" % page

        js = client.get("/static/csrf-fetch.js")
        assert js.status_code == 200, js.status_code
        body = js.get_data(as_text=True)
        for bit in ("window.fetch", "X-CSRF-Token", "/api/csrf"):
            assert bit in body, "the shared wrapper lost %r" % bit

        # And the endpoints those pages call really do let a tokened request in
        # (4xx for a missing id, but no longer 403), while a tokenless one is
        # still refused - the protection is not weakened by any of this.
        client.environ_base.pop("HTTP_X_CSRF_TOKEN", None)
        for url in ("/corkboard/api/pins/no-such-pin", "/notes/api/no-such-note"):
            refused = client.put(url, json={})
            assert refused.status_code == 403, (url, refused.status_code)
        client.environ_base["HTTP_X_CSRF_TOKEN"] = token
        for url in ("/corkboard/api/pins/no-such-pin", "/notes/api/no-such-note"):
            allowed = client.put(url, json={})
            assert allowed.status_code != 403, (url, allowed.status_code)
        print("  cork board + notes ship the wrapper; saves pass with a token -> OK")


# ---------------------------------------------------------------------------
def test_image_steps_default_to_the_workflow() -> None:
    """A blank Steps box must leave the workflow's own step count alone.

    FHDR-Setup is tuned for 20 steps, Z-Image-Turbo for 8. The UI used to send its
    own default (4) for EVERY workflow, and 4 steps on a Flux-family GGUF is
    under-denoised: measured with the same prompt and seed at 1024x1024, 4 steps
    produced a soft, featureless mush and 20 steps a crisp, detailed portrait -
    reproduced on two seeds. That is the "why is my picture blurry?" report: the
    same workflow run in ComfyUI uses its own 20 and looks right.
    """
    _title("image steps fall back to the workflow's own")
    import re
    from pathlib import Path

    import app as forge_app
    import comfyui_service as cs

    # The graph-level contract: steps 0/None changes nothing, an explicit value does.
    # Built as a ComfyUI prompt graph (what _inject() is handed), so this needs no
    # workflow file and no ComfyUI install - it runs the same way on CI.
    def sampler_graph():
        return {"3": {"class_type": "KSampler", "inputs": {
            "seed": 0, "steps": 20, "cfg": 1.0, "sampler_name": "euler",
            "scheduler": "simple", "denoise": 1, "model": ["1", 0],
            "positive": ["2", 0], "negative": ["4", 0], "latent_image": ["5", 0]}}}

    for absent in (0, None):
        pg = sampler_graph()
        cs._inject(pg, prompt_text="x", steps=absent, seed=1)
        got = [d["inputs"]["steps"] for d in pg.values()
               if d.get("class_type") == "KSampler"]
        assert got == [20], f"steps={absent!r} must not override the workflow: {got}"
        assert pg["3"]["inputs"]["seed"] == 1, pg["3"]["inputs"]["seed"]
    pg = sampler_graph()
    cs._inject(pg, prompt_text="x", steps=7, seed=1)
    got = [d["inputs"]["steps"] for d in pg.values()
           if d.get("class_type") == "KSampler"]
    assert got == [7], f"an explicit step count must still win: {got}"
    print("  blank leaves the workflow's steps; an explicit value wins -> OK")

    # The endpoint's own default must be "unspecified", not a number of its own.
    seen = {}
    real_gen = cs.generate_image

    def spy(prompt, output_path, **kw):
        seen.update(kw)
        raise RuntimeError("stop before doing any real work")

    cs.generate_image = spy
    try:
        with forge_app.app.test_client() as client:
            token = _csrf_ok(client)
            r = client.post("/api/generate_image",
                            json={"prompt": "probe", "backend": "comfyui"},
                            headers={"X-CSRF-Token": token})
            assert r.status_code != 200, r.status_code
            assert seen.get("steps") == 0, f"no steps field must mean 'own steps': {seen}"
            seen.clear()
            client.post("/api/generate_image",
                        json={"prompt": "probe", "backend": "comfyui", "steps": 25},
                        headers={"X-CSRF-Token": token})
            assert seen.get("steps") == 25, seen
    finally:
        cs.generate_image = real_gen
    print("  the API treats an absent steps field as 'use the workflow's' -> OK")

    # The markup must not hard-code a step count again, and a generated image must
    # not be displayed at the 240px attachment-thumbnail size: that rule's
    # max-height survived the inline max-width, so a 1024px render appeared 240px
    # wide and read as blurry next to the same file shown full size in ComfyUI.
    html = (Path(__file__).resolve().parent.parent / "templates" / "index.html"
            ).read_text(encoding="utf-8")
    steps_input = re.search(r'id="imageSteps"[^>]*>', html)
    assert steps_input, "the Steps box is gone"
    assert "value=" not in steps_input.group(0), steps_input.group(0)
    assert ".msg img.tf-gen" in html, "generated images lost their own size rule"
    assert 'class="tf-gen"' in html, "the generated image is not tagged for sizing"
    rule = re.search(r"\.msg img\.tf-gen \{(.*?)\}", html, re.S)
    assert rule, "the generated-image rule vanished"
    # BOTH caps have to be lifted: fixing only max-height left `.msg img`'s 240px
    # max-width in force, so the image was still drawn 240px wide.
    assert "max-width: 100%" in rule.group(1), rule.group(1)
    assert "max-height" in rule.group(1), rule.group(1)
    # Which workflow ran must be visible, and the choice must be remembered: the
    # default is the fast (censored) model, so re-picking the uncensored workflow
    # after every reload was the friction behind "it ignores what I type".
    assert "tf-gen-cap" in html, "the caption naming the workflow is gone"
    assert "d.label" in html, "the caption is never filled in"
    assert "tf.imageWorkflow" in html, "the image workflow choice is not remembered"
    print("  no hard-coded step default; generated images get their own size -> OK")

    # The endpoint must report WHICH workflow produced the image.
    class _FakeWf(dict):
        pass

    def spy2(prompt, output_path, **kw):
        with open(output_path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")
        return _FakeWf({"name": "FHDR-Setup", "id": "fhdr-setup"})

    cs.generate_image = spy2
    try:
        with forge_app.app.test_client() as client:
            token = _csrf_ok(client)
            r = client.post("/api/generate_image",
                            json={"prompt": "probe", "backend": "comfyui"},
                            headers={"X-CSRF-Token": token})
            body = r.get_json() or {}
            assert r.status_code == 200, body
            assert "FHDR-Setup" in (body.get("label") or ""), body
    finally:
        cs.generate_image = real_gen
    print("  the response names the workflow that actually ran -> OK")


# ---------------------------------------------------------------------------
def test_inject_preserves_workflow_prompts() -> None:
    """The app must not erase a workflow's own negative prompt.

    FHDR-Setup ships a positive ('nude women ') and a negative ('sex in bed').
    _inject() replaced the positive - correct - but also blanked the negative to
    "", so the same workflow produced a different picture here than in ComfyUI,
    where the authored negative still applies. For a cfg=1 Flux workflow that is
    nearly invisible; for any workflow with a real negative (LTX, Wan, SDXL) it
    changes the output. An explicit negative= still overrides, and "" still clears.
    """
    _title("the workflow's own prompts survive injection")
    import comfyui_service as cs

    def two_prompt_graph():
        return {
            "3": {"class_type": "KSampler", "inputs": {
                "seed": 0, "steps": 20, "cfg": 3.5, "sampler_name": "euler",
                "scheduler": "simple", "denoise": 1, "model": ["1", 0],
                "positive": ["7", 0], "negative": ["8", 0], "latent_image": ["5", 0]}},
            "7": {"class_type": "CLIPTextEncode",
                  "inputs": {"clip": ["9", 0], "text": "the author's sample prompt"}},
            "8": {"class_type": "CLIPTextEncode",
                  "inputs": {"clip": ["9", 0], "text": "the author's negative"}},
        }

    pg = two_prompt_graph()
    cs._inject(pg, prompt_text="my prompt")
    assert pg["7"]["inputs"]["text"] == "my prompt", pg["7"]["inputs"]
    assert pg["8"]["inputs"]["text"] == "the author's negative", pg["8"]["inputs"]
    print("  the positive is replaced, the workflow's negative is kept -> OK")

    pg = two_prompt_graph()
    cs._inject(pg, prompt_text="my prompt", negative="my negative")
    assert pg["8"]["inputs"]["text"] == "my negative", pg["8"]["inputs"]
    pg = two_prompt_graph()
    cs._inject(pg, prompt_text="my prompt", negative="")
    assert pg["8"]["inputs"]["text"] == "", pg["8"]["inputs"]
    print("  an explicit negative overrides, and \"\" still clears -> OK")

    # Nothing else about the sampler may be touched when not asked for.
    pg = two_prompt_graph()
    cs._inject(pg, prompt_text="my prompt")
    assert pg["3"]["inputs"]["steps"] == 20, pg["3"]["inputs"]
    assert pg["3"]["inputs"]["cfg"] == 3.5, pg["3"]["inputs"]
    assert pg["3"]["inputs"]["sampler_name"] == "euler", pg["3"]["inputs"]
    print("  steps, cfg and sampler are left as the workflow authored them -> OK")


# ---------------------------------------------------------------------------
def test_image_panel_choices() -> None:
    """Every runnable text-to-image workflow is offered, and the seed is reported.

    The dropdown used to hold exactly two entries (Z-Image-Turbo and FHDR) even
    though the install it drives has Z-Image-Base, Qwen-Image, Flux.2, Krea and
    more - so the app looked like it had fewer models than ComfyUI itself. The
    seed matters for the same reason: without it being named, a picture you liked
    could never be asked for again, and every run silently re-rolled.
    """
    _title("image panel: workflow list + seed")
    import app as forge_app
    import comfyui_service as cs

    def wf(name, z=False, f=False):
        return {"name": name, "id": name, "path": "x", "kind": "blueprint",
                "is_z_image": z, "is_fhdr": f}

    fake = [wf("Audio Generation (Stable Audio 3 Medium)"),
            wf("Text to Image (Qwen-Image 2512)"),
            wf("Text to Image (Z-Image-Base)", z=True),
            wf("FHDR-Setup", f=True),
            wf("Text to Image (Z-Image-Turbo)", z=True),
            wf("Frame Interpolation")]
    real_disc, real_ready = cs.discover_workflows, cs.workflow_ready
    cs.discover_workflows = lambda *a, **k: fake
    cs.workflow_ready = lambda *a, **k: True
    try:
        with forge_app.app.test_client() as c:
            opts = c.get("/api/image_models").get_json()
    finally:
        cs.discover_workflows, cs.workflow_ready = real_disc, real_ready

    labels = [o["label"] for o in opts]
    assert labels[0] == "Text to Image (Z-Image-Turbo)", labels
    assert labels[1] == "FHDR-Setup", labels
    assert "Text to Image (Z-Image-Base)" in labels, labels
    assert "Text to Image (Qwen-Image 2512)" in labels, labels
    assert not any(l.startswith(("Audio", "Frame")) for l in labels), labels
    assert all(o["value"].startswith("comfyui::") for o in opts), opts
    print(f"  {len(labels)} runnable workflows offered, fast default first -> OK")

    # A run reports the seed it used, and an explicit seed is honoured.
    class FakeWf(dict):
        pass

    seen = {}

    def spy(prompt, output_path, **kw):
        seen.update(kw)
        with open(output_path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")
        return FakeWf({"name": "Text to Image (Z-Image-Base)", "id": "x"})

    real_gen = cs.generate_image
    cs.generate_image = spy
    try:
        with forge_app.app.test_client() as client:
            token = _csrf_ok(client)
            body = client.post("/api/generate_image",
                               json={"prompt": "p", "backend": "comfyui"},
                               headers={"X-CSRF-Token": token}).get_json()
            assert body["seed"] == seen["seed"], (body, seen)
            assert f"seed {body['seed']}" in body["label"], body["label"]
            assert "Z-Image-Base" in body["label"], body["label"]
            seen.clear()
            body2 = client.post("/api/generate_image",
                                json={"prompt": "p", "backend": "comfyui", "seed": 4242},
                                headers={"X-CSRF-Token": token}).get_json()
            assert body2["seed"] == 4242 and seen["seed"] == 4242, (body2, seen)
    finally:
        cs.generate_image = real_gen
    print("  the seed used is named under the image, and a fixed one is kept -> OK")


# ---------------------------------------------------------------------------
def test_auto_detect_prefers_the_users_own_workflow() -> None:
    """Auto-detect must run the workflow the user saved, not the shipped default.

    The app always picked Z-Image-Turbo because its name says "turbo", so the
    FHDR-Setup workflow the user actually works with was never selected and its
    picture never appeared - which reads as "the app ignores my prompt". A saved
    workflow is an explicit choice; a blueprint is just what ComfyUI shipped.
    Audio/video workflows are skipped so an image request cannot land on one.
    """
    _title("auto-detected workflow: the user's own wins")
    import comfyui_service as cs

    def wf(name, kind, z=False, f=False):
        return {"name": name, "id": name, "path": "x", "kind": kind,
                "is_z_image": z, "is_fhdr": f}

    pool = [wf("Audio Generation (Stable Audio 3 Medium)", "blueprint"),
            wf("Text to Image (Z-Image-Turbo)", "blueprint", z=True),
            wf("Text to Image (Z-Image-Base)", "blueprint", z=True),
            wf("FHDR-Setup", "user", f=True),
            wf("audiostable", "user"),
            wf("txt_to_music", "user")]
    real_ready = cs.workflow_ready
    cs.workflow_ready = lambda *a, **k: True
    try:
        chosen = cs.select_workflow(pool)
        assert chosen["name"] == "FHDR-Setup", chosen
        # An explicit request still beats the preference, by name and by keyword.
        assert cs.select_workflow(pool, "z-image")["name"].startswith("Text to Image"), \
            cs.select_workflow(pool, "z-image")
        assert cs.select_workflow(pool, "FHDR-Setup")["name"] == "FHDR-Setup"

        # No user IMAGE workflow: the shipped default still applies, rather than
        # falling through to one of the user's audio workflows.
        no_user_image = [wf("Text to Image (Z-Image-Turbo)", "blueprint", z=True),
                         wf("audiostable", "user"),
                         wf("txt_to_music", "user")]
        assert cs.select_workflow(no_user_image)["name"] == "Text to Image (Z-Image-Turbo)", \
            cs.select_workflow(no_user_image)

        # Everything is a blueprint: unchanged behaviour order (Z-Image first).
        only_bp = [wf("FHDR-Setup", "blueprint", f=True),
                   wf("Text to Image (Z-Image-Turbo)", "blueprint", z=True)]
        assert cs.select_workflow(only_bp)["name"] == "Text to Image (Z-Image-Turbo)", \
            cs.select_workflow(only_bp)

        # Audio: the user's saved audio workflow beats the bundled pick too.
        audio = [wf("Audio Generation (Stable Audio 3 Medium)", "blueprint"),
                 wf("Text to Audio (ACE-Step 1.5)", "blueprint"),
                 wf("audio_stable_audio_3_medium", "user")]
        assert cs.select_audio_workflow(audio)["name"] == "audio_stable_audio_3_medium", \
            cs.select_audio_workflow(audio)
        assert cs.select_audio_workflow(
            [w for w in audio if w["kind"] == "blueprint"])["name"] \
            == "Text to Audio (ACE-Step 1.5)"
    finally:
        cs.workflow_ready = real_ready
    print("  the user's own workflow wins; explicit picks and fallbacks intact -> OK")


# ---------------------------------------------------------------------------
def test_discord_plugin_stays_isolated() -> None:
    """The Discord plugin must never do Discord's work inside the app.

    Plugins run in-process with full permissions, so a bot that hangs, loops or
    crashes there takes TrioForge with it. The plugin is therefore a shim: the bot
    lives in its own process and this file only spawns it with a timeout. Each
    assertion below is one of the properties that keeps that true, because losing
    any single one silently removes the isolation:

      * no network client is imported here at all
      * the child call is bounded by a timeout
      * the bot token is never asked for, stored, or read by the app
      * a broken/missing bridge comes back as an error string, not an exception
    """
    _title("discord plugin is an isolated shim")
    import os
    import tempfile

    import plugin_loader
    from flask import Flask

    plugin_loader.load_all(Flask("regress-discord"))
    mod = plugin_loader._mods["discord"]
    src = open(mod.__file__, encoding="utf-8").read()

    for banned in ("import requests", "import socket", "import discord",
                   "urllib.request", "http.client"):
        assert banned not in src, f"the plugin must not {banned} - that is the bridge's job"
    assert "subprocess.run" in src and "timeout=timeout" in src, \
        "the child call must be a bounded subprocess.run"
    print("  no network client in-process; the child call is timeout-bounded -> OK")

    # The token belongs to the bridge's own .env. If the app ever starts collecting
    # it, it would land in TrioForge's config (and therefore in its backups).
    keys = {f["key"] for f in mod.MANIFEST.get("credentials", [])}
    assert keys == {"bridge_dir"}, f"unexpected credential fields: {keys}"
    # It may NAME the variable in the setup guide - that is how the user is told
    # what to call it in .env - but it must never read or store it.
    token_lines = [ln.strip() for ln in src.splitlines() if "DISCORD_TOKEN" in ln]
    assert token_lines, "the guide should tell the user the variable name"
    for line in token_lines:
        assert "environ" not in line and "get(" not in line, \
            f"the plugin must not read the bot token: {line}"
    print("  the app stores a folder path, never the bot token -> OK")

    # A missing or broken bridge is a REPORTED string, not a raised exception.
    real_path = mod._SETTINGS_PATH
    tmp = tempfile.mkdtemp(prefix="discord-plugin-test-")
    mod._SETTINGS_PATH = os.path.join(tmp, "discord_bridge.json")
    try:
        mod._save_settings({"bridge_dir": os.path.join(tmp, "nowhere")})
        st = mod._status()
        assert st["connected"] is False and st.get("error"), st
        assert mod.dispatch("discord_send", {"text": "hi"}).get("error")
        assert mod.dispatch("discord_status", {}).get("connected") is False
        assert mod.connect_info().get("error")
        # An unset folder must not raise either.
        mod._save_settings({"bridge_dir": ""})
        assert mod._status()["connected"] is False
    finally:
        mod._SETTINGS_PATH = real_path
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print("  a missing bridge is reported, never raised -> OK")


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
def test_restore_list_supports_bulk_cleanup() -> None:
    """The restore-list trash must be cleanable in one go, and only after a confirm.

    Every delete archives another copy, so trying one prompt repeatedly leaves
    dozens of near-identical rows - 129 on the machine this was written on, 107 of
    them titled the same, 56 deleted in a single day. One row at a time is not a
    usable clean-up. Asserted on the markup because the browser check
    (tools/restore-bulk-check.js) needs a running app and cannot run in CI.
    """
    _title("trash list: bulk select")
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent / "templates" / "index.html"
            ).read_text(encoding="utf-8")
    for needle in ("bulkRestoreAction", "data-restore-row", "toggleRestoreAll",
                   "restoreSelection", "Delete selected", "Restore selected"):
        assert needle in html, "{} missing from the restore list".format(needle)

    body = html.split("async function bulkRestoreAction", 1)[1].split("\n}", 1)[0]
    # Judge the CODE, not the comments: this function's comment explains why it is
    # not Promise.all, and a naive substring check read that as a violation.
    code = "\n".join(ln for ln in body.splitlines()
                     if not ln.strip().startswith("//"))
    assert "await fetch" in code, "the bulk action must await each write"
    assert "Promise.all(" not in code, \
        "dozens of parallel writes is how SQLite starts refusing them"
    assert "confirm(" in code, "a destructive bulk action must ask first"
    assert "ids.length" in code, "the confirm must say how many items are affected"
    # The selection is per list, so Notes' select-all can never touch the chats.
    assert "restoreSelection[containerId]" in html, "the selection must be per list"
    print("  per-row checkboxes, select-all and one confirmed bulk action -> OK")

def test_daily_briefing_plugin() -> None:
    """The daily briefing writes ONE pin a day, from whatever is connected.

    The properties that matter, each one a way this feature could quietly go wrong:

      * it is shipped disabled and loads without side effects
      * a source that is not connected is left out - not nagged about every morning
      * a source that is connected but failing is reported inside the pin
      * rewriting the same day updates the pin in place (never a second one)
      * a model is optional polish: if it fails, the plain pin is still written
      * old briefings are pruned only if nobody touched them
      * the scheduler obeys the hour, the switch, the existing pin and the retry cap

    Everything runs against a temp database and temp config: the user's real board
    and settings are never opened for writing.
    """
    _title("daily briefing plugin")
    import importlib.util
    import json
    import tempfile
    from datetime import datetime, timedelta

    from flask import Flask

    repo = Path(__file__).resolve().parent.parent
    path = repo / "plugins" / "_daily_briefing.py"
    assert path.is_file(), "the plugin must ship (disabled, behind the underscore)"
    assert not (repo / "plugins" / "daily_briefing.py").exists(), \
        "it must ship DISABLED - enabling is the user's choice"
    spec = importlib.util.spec_from_file_location("briefing_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # loading must not start a thread or touch a file
    assert mod.MANIFEST["name"] == "daily-briefing"
    assert [t["function"]["name"] for t in mod.TOOLS] == ["daily_briefing_make"]
    print("  ships disabled; loads without side effects -> OK")

    from features import cork_board as cork
    real_db, real_archive = cork.DB_PATH, cork.backup_store.archive_pin
    real_cfg, real_claim = mod._CFG_PATH, mod._CLAIM_PATH
    real_tool, real_env = mod._tool, os.environ.get("TRIOFORGE_NO_BRIEFING")
    with tempfile.TemporaryDirectory() as tmp:
        cork.DB_PATH = os.path.join(tmp, "notes.db")
        cork.init_db()
        cork.backup_store.archive_pin = lambda *a, **k: None   # no backups of test pins
        mod._CFG_PATH = os.path.join(tmp, "daily_briefing.json")
        mod._CLAIM_PATH = os.path.join(tmp, ".claim")
        os.environ["TRIOFORGE_NO_BRIEFING"] = "1"               # register() starts no thread
        try:
            now = datetime(2026, 10, 17, 8, 0)
            old = (now - timedelta(days=60)).isoformat()
            fresh = (now - timedelta(days=2)).isoformat()
            long_body = "An idea worth coming back to, long enough to count."
            cork.upsert_pin("p-old", {"title": "Old idea", "content": long_body}, now=old)
            cork.upsert_pin("p-old2", {"title": "Older idea", "content": long_body}, now=old)
            cork.upsert_pin("p-fresh", {"title": "Fresh", "content": long_body}, now=fresh)
            cork.upsert_pin("p-short", {"title": "Short", "content": "tiny"}, now=old)
            cork.upsert_pin("p-img", {"title": "Photo", "content": long_body, "type": "image"}, now=old)
            cork.upsert_pin("p-tagged", {"title": "Prior", "content": long_body,
                                         "tags": ["briefing"]}, now=old)

            calls = []
            sources = {
                "calendar_status": {"connected": True},
                "gmail_status": {"connected": True},
                "calendar_list_events": {"events": [
                    {"summary": "Standup", "start": "2026-10-17T09:30:00+08:00", "location": "Zoom"},
                    {"summary": "Holiday", "start": "2026-10-17"}]},
                "gmail_list_inbox": {"messages": [
                    {"from": '"Ada Lovelace" <ada@example.com>', "subject": "Notes", "unread": True},
                    {"from": "bob@example.com", "subject": "Already read", "unread": False}]},
            }

            def fake_tool(name, args=None):
                calls.append((name, args))
                return sources.get(name)
            mod._tool = fake_tool

            # --- content -------------------------------------------------
            title, body, n, notes = mod.build(mod._cfg(), use_model=False, now=now)
            assert title == "☀️ Briefing — Sat 17 Oct", title
            assert "09:30 — Standup (Zoom)" in body and "all day — Holiday" in body, body
            assert "**Ada Lovelace** — Notes" in body and "Already read" not in body, body
            assert body.count("**Old idea**") + body.count("**Older idea**") == 2, body
            for absent in ("Fresh", "Short", "Photo", "Prior"):
                assert "**{}**".format(absent) not in body, "{} must not be revisited".format(absent)
            assert n == 3 and notes == [], (n, notes)
            assert mod.build(mod._cfg(), False, now)[1] == body, \
                "the same day must pick the same pins"
            print("  calendar + unread mail + idle pins, filtered correctly -> OK")

            # --- sources: absent vs failing --------------------------------
            sources["gmail_status"] = {"connected": False}
            calls.clear()
            body = mod.build(mod._cfg(), False, now)[1]
            assert "Unread" not in body and "Skipped" not in body, body
            assert "gmail_list_inbox" not in [c[0] for c in calls], \
                "a disconnected source must not even be queried"
            sources["gmail_status"] = {"connected": True}
            sources["gmail_list_inbox"] = {"error": "token expired " + "x" * 200}
            _, body, _, notes = mod.build(mod._cfg(), False, now)
            assert notes and notes[0].startswith("Gmail (token expired") and len(notes[0]) < 100, notes
            assert "_Skipped: Gmail" in body, body
            sources["gmail_list_inbox"] = {"messages": []}
            assert "No unread mail." in mod.build(mod._cfg(), False, now)[1]
            print("  disconnected = silent, failing = reported, empty = said so -> OK")

            # --- nothing at all --------------------------------------------
            sources.update(calendar_status={"connected": False}, gmail_status={"connected": False})
            for pid in ("p-old", "p-old2"):
                cork.delete_pin_row(pid)
            body = mod.build(mod._cfg(), False, now)[1]
            assert "Nothing connected yet" in body, body
            print("  a bare install still gets an explanatory pin -> OK")

            # --- one pin per day, rewritten in place -----------------------
            cork.upsert_pin("p-old", {"title": "Old idea", "content": long_body}, now=old)
            sources.update(calendar_status={"connected": True}, gmail_status={"connected": True},
                           gmail_list_inbox={"messages": [{"from": "a@b.c", "subject": "One",
                                                           "unread": True}]})
            res = mod.make_today(use_model=False)
            pid = mod._pin_id(datetime.now().date())
            assert res.get("ok") and res["pin_id"] == pid, res
            pin = cork.get_pin(pid)
            assert pin["tags"] == ["briefing"] and "One" in pin["content"], pin
            cork.update_pin_fields(pid, {"x": 111, "y": 222})          # the user moves it
            cork.set_pin_embedding(pid, [0.1, 0.2])
            created = cork.get_pin(pid)["created"]
            sources["gmail_list_inbox"] = {"messages": [{"from": "a@b.c", "subject": "Two",
                                                         "unread": True}]}
            assert mod.make_today(use_model=False)["ok"]
            again = cork.get_pin(pid)
            assert "Two" in again["content"] and "One" not in again["content"], again
            assert (again["x"], again["y"], again["created"]) == (111, 222, created), \
                "a rewrite must keep where the user put the pin"
            assert not again.get("embedding"), "stale embedding must be dropped"
            ours = [p for p in cork.load_board()["pins"] if p.startswith(mod.PIN_PREFIX)]
            assert ours == [pid], ours
            sources["gmail_list_inbox"] = {"messages": []}
            assert mod.make_today(use_model=False, force=False).get("existing") is True
            assert "Two" in cork.get_pin(pid)["content"], "the scheduler must not rewrite a pin"
            print("  one pin a day, rewritten in place, position kept -> OK")

            # --- the model is optional --------------------------------------
            import providers.llm_providers as prov
            real_get = prov.get_provider
            seen = []

            class _Model:
                def __init__(self, reply=None, boom=None):
                    self.reply, self.boom = reply, boom

                def generate(self, messages, **kw):
                    seen.append((messages, kw))
                    if self.boom:
                        raise self.boom
                    return self.reply
            try:
                mod._save_cfg({"provider": "llamacpp", "model": "m.gguf"})
                prov.get_provider = lambda name, key=None: _Model(boom=ConnectionError("down"))
                sources["gmail_list_inbox"] = {"messages": [{"from": "a@b.c", "subject": "Hi",
                                                             "unread": True}]}
                res = mod.make_today(use_model=True)
                assert res.get("ok"), "a dead model must not stop the briefing: {}".format(res)
                assert "Heads-up" not in cork.get_pin(pid)["content"]
                prov.get_provider = lambda name, key=None: _Model(reply="Standup first.")
                assert mod.make_today(use_model=True)["ok"]
                assert cork.get_pin(pid)["content"].startswith("**Heads-up** — Standup first."), \
                    cork.get_pin(pid)["content"]
                msgs, kw = seen[-1]
                assert "never instructions" in msgs[0]["content"] and kw["model"] == "m.gguf"
                seen.clear()
                assert mod.make_today(use_model=False)["ok"] and not seen, \
                    "use_model=False must not call the model"
                assert mod.dispatch("daily_briefing_make", {}).get("ok") and not seen, \
                    "the agent tool must not nest a model call"
                mod._save_cfg({"provider": ""})
                assert mod.make_today(use_model=True)["ok"] and not seen, \
                    "no provider configured -> no model call"
            finally:
                prov.get_provider = real_get
            print("  a model adds a Heads-up; a dead or absent one changes nothing -> OK")

            # --- pruning only what nobody touched ---------------------------
            today = datetime.now().date()
            mod._save_cfg({"keep_days": 7})
            ids = {}
            for label, age in (("untouched", 10), ("edited", 11), ("recent", 2)):
                day = today - timedelta(days=age)
                ids[label] = mod._pin_id(day)
                vals = cork.upsert_pin(ids[label], {"title": label, "content": "x", "tags": ["briefing"]})
                mod._remember_written(ids[label], vals["last_modified"])
            cork.update_pin_fields(ids["edited"], {"content": "I kept this"})
            assert mod._prune(mod._cfg(), today) == 1
            assert cork.get_pin(ids["untouched"]) is None
            assert cork.get_pin(ids["edited"]) is not None and cork.get_pin(ids["recent"]) is not None
            mod._save_cfg({"keep_days": 0})
            assert mod._prune(mod._cfg(), today + timedelta(days=400)) == 0, "keep_days 0 keeps all"
            assert cork.get_pin("p-fresh") is not None, "only briefings are ever pruned"
            print("  prunes untouched old briefings, keeps edited, recent and everything else -> OK")

            # --- the scheduler's rules --------------------------------------
            cfg = dict(mod._cfg(), auto=True, hour=7)
            mod._runtime.update(attempts={}, last_try=0.0)
            at = lambda h: datetime(2026, 10, 17, h, 0)               # noqa: E731
            assert mod._due(cfg, at(8), have_pin=False) is True
            assert mod._due(cfg, at(6), have_pin=False) is False, "before the hour"
            assert mod._due(cfg, at(8), have_pin=True) is False, "already have today's"
            assert mod._due(dict(cfg, auto=False), at(8), have_pin=False) is False, "switched off"
            mod._runtime.update(attempts={"2026-10-17": 1}, last_try=mod.time.time())
            assert mod._due(cfg, at(8), have_pin=False) is False, "retry waits"
            mod._runtime["last_try"] = mod.time.time() - mod._RETRY_AFTER - 1
            assert mod._due(cfg, at(8), have_pin=False) is True
            mod._runtime["attempts"] = {"2026-10-17": mod._MAX_AUTO_ATTEMPTS}
            assert mod._due(cfg, at(8), have_pin=False) is False, "capped for the day"
            mod._runtime.update(attempts={}, last_try=0.0)
            print("  scheduler honours the hour, the switch, the pin and the retry cap -> OK")

            # --- two workers must not both write -----------------------------
            assert mod._claim() is True and mod._claim() is False
            mod._release()
            assert mod._claim() is True
            stale = mod.time.time() - mod._CLAIM_STALE - 5
            os.utime(mod._CLAIM_PATH, (stale, stale))
            assert mod._claim() is True, "a dead process's claim must expire"
            mod._release()
            print("  a live claim blocks a second writer; a dead one expires -> OK")

            # --- HTTP -------------------------------------------------------
            app = Flask("regress-briefing")
            mod.register(app)
            assert mod._runtime["started"] is False, "TRIOFORGE_NO_BRIEFING must stop the thread"
            c = app.test_client()
            st = c.get("/api/plugin/daily-briefing/status").get_json()
            assert st["calendar_connected"] and st["config"]["hour"] == 7, st
            r = c.post("/api/plugin/daily-briefing/config",
                       json={"hour": 99, "keep_days": "3", "provider": " ollama ", "auto": False})
            cfgd = r.get_json()["config"]
            assert (cfgd["hour"], cfgd["keep_days"], cfgd["provider"], cfgd["auto"]) == \
                (23, 3, "ollama", False), cfgd
            assert c.post("/api/plugin/daily-briefing/config", json={"hour": "soon"}).status_code == 400
            assert c.post("/api/plugin/daily-briefing/run", json={"llm": False}).get_json()["ok"]
            saved = json.load(open(mod._CFG_PATH, encoding="utf-8"))
            assert saved["hour"] == 23 and "written" in saved, "config edits must not drop state"
            print("  routes validate input and never lose the state file -> OK")

            # --- the real loader accepts the file once enabled ---------------
            copy = os.path.join(tmp, "daily_briefing.py")
            open(copy, "w", encoding="utf-8").write(path.read_text(encoding="utf-8"))
            import plugin_loader
            plugin_loader.set_app(Flask("regress-briefing-loader"))
            info = plugin_loader._load_plugin(copy)
            assert not info.get("error"), info
            assert info["id"] == "daily-briefing" and info["connector"] is False, info
            print("  loads through the real plugin loader once enabled -> OK")
        finally:
            # Windows cannot delete a database that still has an open connection.
            try:
                cork.get_conn().close()
            except Exception:  # noqa: BLE001
                pass
            from common import _local as _db_local
            getattr(_db_local, "conns", {}).pop(cork.DB_PATH, None)
            cork.DB_PATH = real_db
            cork.backup_store.archive_pin = real_archive
            mod._CFG_PATH, mod._CLAIM_PATH, mod._tool = real_cfg, real_claim, real_tool
            if real_env is None:
                os.environ.pop("TRIOFORGE_NO_BRIEFING", None)
            else:
                os.environ["TRIOFORGE_NO_BRIEFING"] = real_env


# ---------------------------------------------------------------------------
def test_briefing_panel_is_wired() -> None:
    """The briefing plugin's routes must be reachable from the UI.

    The plugin arrived with three routes and nothing calling them, so the feature
    could only be used with curl. It also ships switched OFF on purpose (the leading
    underscore keeps every loader away from it), which makes a 404 from its status
    route a NORMAL state - the panel has to say so and offer the switch, instead of
    looking broken.
    """
    _title("daily briefing panel")
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    html = (root / "templates" / "index.html").read_text(encoding="utf-8")
    for needle in ("briefingPanel", "toggleBriefingPanel", "runBriefingNow",
                   "saveBriefingConfig", "enableBriefingPlugin",
                   "/api/plugin/daily-briefing/status",
                   "/api/plugin/daily-briefing/run",
                   "/api/plugin/daily-briefing/config",
                   "/api/extensions/toggle"):
        assert needle in html, "{} missing - the panel is not fully wired".format(needle)

    body = html.split("async function loadBriefing", 1)[1].split("\n}", 1)[0]
    assert "404" in body, "the ships-disabled state must be handled"
    assert "Not enabled" in body, "and it must say so in words"

    # Enabling stays the user's choice: the plugin must still ship behind the underscore.
    plugins = root / "plugins"
    assert (plugins / "_daily_briefing.py").is_file(), "the plugin must ship"
    assert not (plugins / "daily_briefing.py").exists(), \
        "it must ship DISABLED - enabling is the user's choice"
    print("  the ☀️ panel reaches all three routes and explains 'not enabled' -> OK")


# ---------------------------------------------------------------------------
def test_human_text_fields_are_spellcheckable() -> None:
    """Chat, note and pin text must be spellcheckable, code fields must not.

    The app also runs in a WebView2 window (--window), where spellcheck is not
    enabled unless the field says so - which is why "there is no autocorrecting".
    The attribute is therefore explicit in BOTH directions: on for human text,
    off for code/command fields where a squiggle under every identifier would be
    noise.
    """
    _title("spellcheck on human text")
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for label, path, needles in (
        ("chat", "templates/index.html",
         ['id="msgInput" spellcheck="true"']),
        ("notes", "py/features/notes.py",
         ['id="noteTitleInput" spellcheck="true"',
          'id="noteContentInput" spellcheck="true"']),
        ("corkboard", "py/features/cork_board.py",
         ['id="pinTitle" spellcheck="true"',
          'id="pinContent" spellcheck="true"']),
    ):
        src = (root / path).read_text(encoding="utf-8")
        for needle in needles:
            assert needle in src, "{}: {} missing".format(label, needle)

    idx = (root / "templates" / "index.html").read_text(encoding="utf-8")
    after = idx.split('id="designRunInput"', 1)[1][:200]
    assert 'spellcheck="false"' in after, "code/command fields must keep spellcheck off"
    print("  chat, note and pin text spellcheck; code fields stay off -> OK")


# ---------------------------------------------------------------------------
def test_memory_panel_and_free_are_honest() -> None:
    """The top-bar memory badge and free button report only what really happened.

    "freed: ComfyUI" when ComfyUI was never running is a lie the user notices, so
    the route checks each stop's result instead of assuming. And the badge must
    show RAM even when there is no GPU at all.
    """
    _title("memory badge + free button")
    import app as forge_app

    with forge_app.app.test_client() as client:
        m = client.get("/api/memory").get_json()
        assert "ram_used_gb" in m and "ram_total_gb" in m and "ram_percent" in m, m
        assert m["ram_total_gb"] > 0, m

        real_llama = forge_app.llamacpp_service.stop
        real_comfy = forge_app.comfyui_service.stop_comfyui
        forge_app.llamacpp_service.stop = lambda: {"running": False, "message": "stopped",
                                                   "killed": []}
        forge_app.comfyui_service.stop_comfyui = lambda: {
            "running": False, "message": "ComfyUI is not running, or was started outside TrioForge"}
        try:
            token = _csrf_ok(client)
            body = client.post("/api/memory/free", headers={"X-CSRF-Token": token}).get_json()
            assert body["ok"] and body["freed"] == [], body
            forge_app.llamacpp_service.stop = lambda: {"running": False, "message": "stopped",
                                                       "killed": [123]}
            forge_app.comfyui_service.stop_comfyui = lambda: {"running": False,
                                                             "message": "ComfyUI stopped."}
            body = client.post("/api/memory/free", headers={"X-CSRF-Token": token}).get_json()
            assert "llama.cpp" in body["freed"] and "ComfyUI" in body["freed"], body
        finally:
            forge_app.llamacpp_service.stop = real_llama
            forge_app.comfyui_service.stop_comfyui = real_comfy
    # The RESULT must not be printed into the toolbar: a red "free failed" where the
    # RAM numbers live was the ugliest thing in the top bar. Outcomes go to a toast.
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "templates" / "index.html"
            ).read_text(encoding="utf-8")
    assert "tfToast" in html, "outcomes belong in a toast"
    # Asserted on BEHAVIOUR, not on the words: the old "free failed" text still
    # appears in the comments explaining why it was removed, so matching the string
    # only ever tests the documentation. What matters is that free() reports through
    # a toast, never writes into the badge, and turns a stale process into advice.
    free_fn = html.split("async function freeMemoryNow", 1)[1].split("\n}", 1)[0]
    assert "tfToast(" in free_fn, "free must report through a toast"
    assert "el.textContent" not in free_fn, "free must not write its result into the badge"
    assert "404" in free_fn, "a stale process must say 'restart', not 'failed'"
    print("  badge reads RAM/VRAM; free reports only what actually stopped -> OK")


# ---------------------------------------------------------------------------
def test_comfyui_idle_unload_wiring() -> None:
    """ComfyUI must unload itself after idling, the way llama.cpp already does.

    --cache-none frees the MODELS but the server process keeps ~2 GB resident. The
    watchdog stops only the server TrioForge started (a Desktop-started one has no
    _started_proc, so it is never touched) and can be turned off or retimed.
    """
    _title("comfyui idle unload")
    import time

    import comfyui_service as cs

    t0 = time.time()
    assert cs.touch() is None
    assert cs._comfyui_last_used >= t0 - 1, "touch() must stamp the last-use clock"
    assert cs.ensure_comfyui_idle_watchdog() is None
    assert cs.ensure_comfyui_idle_watchdog() is None, "starting it twice is a no-op"
    assert cs._idle_watchdog_started is True

    import inspect
    src = inspect.getsource(cs._idle_watchdog)
    assert "TRIOFORGE_COMFYUI_IDLE_UNLOAD" in src, "the idle time must be tunable"
    assert "stop_comfyui()" in src, "the watchdog must stop the server"
    assert "_started_proc" in src, "it must only touch a server TrioForge started"
    print("  watchdog armed once, tunable, and limited to TrioForge-started servers -> OK")


# ---------------------------------------------------------------------------
def test_chat_draft_and_copy_wiring() -> None:
    """A half-typed chat survives a reload, and every message can be copied.

    Asserted on the markup: these are browser behaviours with no server half, and a
    real browser run cannot execute in CI. The rule that matters beyond "it exists"
    is that the draft is cleared when a message is sent - otherwise a reload would
    resurrect the text you just sent.
    """
    _title("chat draft + copy")
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent / "templates" / "index.html"
            ).read_text(encoding="utf-8")
    for needle in ("tf.chatDraft", "clearChatDraft", "makeCopyButton",
                   "copyToClipboard", "navigator.clipboard", "fallbackCopy",
                   "copy-btn"):
        assert needle in html, "{} missing".format(needle)
    send = html.split("function doSend()", 1)[1].split("\n}", 1)[0]
    assert "clearChatDraft()" in send, "sending must clear the saved draft"
    assert ".msg.bot .msg-actions" not in html, \
        "the old rule that hid bot actions must be gone"
    copy = html.split("function makeCopyButton", 1)[1].split("\n}", 1)[0]
    assert "innerText" in copy, "bot copy must be the rendered text"
    print("  draft persists and clears on send; every message is copyable -> OK")


# ---------------------------------------------------------------------------
def test_local_timeout_and_output_cap_knobs() -> None:
    """A slow local model must not be able to write past its own timeout.

    The provider allows 4096 output tokens, but this project's test card does ~5
    tok/s on a 12B split across GPU and CPU - so ~1500 tokens fit in the 300 s wait
    and anything longer died as "llama.cpp server timed out", which reads like a bug
    when it is only a long answer. The knobs are TRIOFORGE_MAX_TOKENS (cap the reply
    so it FINISHES) and TRIOFORGE_LLAMA_TIMEOUT (wait longer), and junk must never
    become a broken setting - a negative timeout fails every request instantly,
    which looks exactly like a dead server.
    """
    _title("local model timeout knobs")
    import os

    from providers import llm_providers as L

    keys = ("TRIOFORGE_MAX_TOKENS", "TRIOFORGE_LLAMA_TIMEOUT")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        assert L._local_output_cap(4096) == 4096, "unset means no extra cap"
        assert L._local_timeout() == 300
        os.environ["TRIOFORGE_MAX_TOKENS"] = "1024"
        assert L._local_output_cap(4096) == 1024
        assert L._local_output_cap(512) == 512, "a cap must never raise a smaller one"
        os.environ["TRIOFORGE_MAX_TOKENS"] = "junk"
        assert L._local_output_cap(4096) == 4096
        os.environ["TRIOFORGE_LLAMA_TIMEOUT"] = "600"
        assert L._local_timeout() == 600
        for bad in ("", "soon", "-5", "0"):
            os.environ["TRIOFORGE_LLAMA_TIMEOUT"] = bad
            assert L._local_timeout() == 300, bad
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("  output can be capped, the wait is tunable, junk falls back -> OK")


# ---------------------------------------------------------------------------
def test_local_request_has_a_hard_deadline() -> None:
    """A trickling server must not hold a request open for half an hour.

    requests' timeout is per socket read, so a server that sends a byte every so
    often - or a machine that starts swapping mid-answer with a 32K context -
    resets it forever. That is the reported "llama.cpp generating half way for
    30 min". The budget is now checked against the clock, so it holds however the
    bytes arrive, and a normal fast response still works unchanged.
    """
    _title("local request hard deadline")
    import json as _json
    import tempfile
    import shutil
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import requests

    from providers import llm_providers as L

    class Handler(BaseHTTPRequestHandler):
        mode = "fast"

        def log_message(self, *args):      # keep the test output clean
            return

        def do_GET(self):
            # /health: the provider waits for this before it sends a prompt.
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            if Handler.mode == "fast":
                body = _json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            # Trickle: every gap is well under any read timeout, the total is not.
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "4096")
            self.end_headers()
            for _ in range(60):
                try:
                    self.wfile.write(b" ")
                    self.wfile.flush()
                except Exception:  # noqa: BLE001 - the client hung up, fine
                    return
                time.sleep(0.2)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:{}".format(port)

    # Wait until the stub ACCEPTS before anything is timed. A slower machine can
    # lose this race, and then the provider's readiness poll - not the budget -
    # would be what the stopwatch measures.
    ready_by = time.time() + 15
    while time.time() < ready_by:
        try:
            if requests.get(base + "/health", timeout=1).status_code == 200:
                break
        except Exception:      # noqa: BLE001 - not listening yet
            pass
        time.sleep(0.1)
    else:
        raise AssertionError("the stub server never started listening")

    # A model has to EXIST for the provider to build a request. A fresh clone has an
    # empty models/ (weights are git-ignored), which is what made this pass on a
    # developer machine and fail on CI; a stub file in a temp dir fixes that.
    models_dir = tempfile.mkdtemp(prefix="tf-deadline-models-")
    stub_model = os.path.join(models_dir, "stub.gguf")
    with open(stub_model, "wb") as fh:
        fh.write(b"GGUF")
    provider = L.LlamaCppProvider(models_dir=models_dir, server_url=base,
                                  context_length=4096)
    saved = os.environ.get("TRIOFORGE_LLAMA_TIMEOUT")
    os.environ["TRIOFORGE_LLAMA_TIMEOUT"] = "1"
    try:
        Handler.mode = "fast"
        assert provider.generate([{"role": "user", "content": "hi"}], model="stub.gguf") == "hi"

        Handler.mode = "slow"
        started = time.time()
        try:
            provider.generate([{"role": "user", "content": "hi"}], model="stub.gguf")
            raise AssertionError("a trickling server must hit the budget")
        except L.ProviderError as exc:
            assert "timed out" in str(exc).lower(), exc
        elapsed = time.time() - started
        # 1s budget. If the cleanup ever moves back onto the caller's path (closing a
        # streaming response blocks until the body ends) this jumps to ~12s.
        assert elapsed < 8, "the budget must stop it, elapsed={:.1f}s".format(elapsed)
    finally:
        if saved is None:
            os.environ.pop("TRIOFORGE_LLAMA_TIMEOUT", None)
        else:
            os.environ["TRIOFORGE_LLAMA_TIMEOUT"] = saved
        server.shutdown()
        shutil.rmtree(models_dir, ignore_errors=True)
    print("  a trickling response stops at the budget; fast ones unaffected -> OK")


# ---------------------------------------------------------------------------
def test_ai_voice_dropdown_is_wired() -> None:
    """The AI voice must be pickable (male / female), left of the country picker.

    Speech output is the BROWSER's engine: the app always spoke with whatever the
    default voice was and hardcoded `en-US`, so a machine with David (male), Mark
    (male) and Zira (female) installed could only ever get one of them. The dropdown
    lists what the machine really has, labels the well-known names with ♂/♀ because a
    voice name is the only gender hint available, remembers the choice, and speaks a
    sample so the pick is audible. Asserted on the markup - there is no server half.
    """
    _title("AI voice dropdown")
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent / "templates" / "index.html"
            ).read_text(encoding="utf-8")
    for needle in ('id="voiceSelect"', "function populateVoiceSelect", "VOICE_MALE_HINTS",
                   "VOICE_FEMALE_HINTS", "tf.ttsVoice", "utterance.voice = chosenVoice",
                   "utterance.lang = chosenVoice.lang"):
        assert needle in html, "{} missing".format(needle)

    assert html.index('id="voiceSelect"') < html.index('id="weatherCountrySelect"'), \
        "the voice picker belongs to the LEFT of the country picker"
    # A machine (or WebView2) with no voices must degrade to one honest option.
    assert "Default voice" in html, "no-voice engines need a fallback entry"
    # Picking a voice has to be audible, or the choice cannot be judged.
    assert "This is my voice." in html, "choosing a voice should speak a sample"
    print("  male/female voices listed, remembered, sampled; left of the country -> OK")


def test_neural_voices_are_offered() -> None:
    """The voice picker must offer real neural voices, not just the PC's three.

    Windows installs three SAPI voices and the browser can only speak with what the
    OS has, so "David / Mark / Zira" looked like a hard limit of the app. It is not:
    the neural voices are served by the app itself through /api/tts/voices and
    /api/tts/speak, and the page lists both groups with the neural one first.

    Nothing here touches the network - the test proves the offline, switched-off and
    hostile-input paths, and that the wiring exists. A real synthesis is checked by
    hand because it needs internet (316 voices on the machine this was written on).
    """
    _title("neural voices")
    from pathlib import Path
    import neural_voice

    # 1. Switched off must mean switched off: no import, no request, no audio.
    os.environ["TRIOFORGE_TTS"] = "off"
    try:
        assert neural_voice.available() is False, "TRIOFORGE_TTS=off must disable it"
        reason = neural_voice.status()["reason"]
        assert "switched off" in reason, reason
        try:
            neural_voice.synthesize("hello", "en-US-AvaNeural")
            raise AssertionError("synthesis must refuse while switched off")
        except neural_voice.NeuralVoiceError as exc:
            assert "switched off" in str(exc), exc
    finally:
        del os.environ["TRIOFORGE_TTS"]

    # 2. A voice id becomes SSML inside edge-tts, so anything of the wrong shape has
    #    to be refused BEFORE a request is made - never interpolated.
    for bad in ("../../evil", "en-US-Ava<script>alert(1)</script>", "en-US A", "a"):
        try:
            neural_voice.synthesize("hi", bad)
            raise AssertionError("accepted a bad voice id: {!r}".format(bad))
        except neural_voice.NeuralVoiceError:
            pass
    # A blank voice means "use the default", not "reject" - the UI sends it when the
    # stored choice was cleared. Verified through the off-switch above, where the
    # error names the switch rather than the empty id.

    # 3. The default reads every language, because replies quote Malay and Chinese.
    assert "Multilingual" in neural_voice.DEFAULT_VOICE, neural_voice.DEFAULT_VOICE
    assert neural_voice._friendly("en-US-AvaMultilingualNeural") == "Ava (multilingual)"
    assert neural_voice._friendly("ms-MY-YasminNeural") == "Yasmin"

    html = (Path(__file__).resolve().parent.parent / "templates" / "index.html"
            ).read_text(encoding="utf-8")
    for needle in ("fetchNeuralVoices", "/api/tts/voices", "/api/tts/speak",
                   "'neural:' + v.id", "'local:' + (v.voiceURI || v.name)",
                   "Neural voices (online", "function speakNeural", "function speakLocal",
                   "TTS_CHUNK_CHARS", "function _ttsChunks"):
        assert needle in html, "{} missing".format(needle)
    # A reply must not wait for the whole thing to be synthesised before it speaks.
    assert "prefetch while this one is speaking" in html, "chunked playback lost"
    # The picker says out loud that a neural voice sends the text to Microsoft.
    assert "sent to Microsoft" in html, "privacy notice missing from the neural group"

    src_app = (Path(__file__).resolve().parent.parent / "py" / "app.py"
               ).read_text(encoding="utf-8")
    assert "@app.route('/api/tts/voices'" in src_app
    assert "@app.route('/api/tts/speak'" in src_app
    assert src_app.index("neural_voice.synthesize") > 0, "route must call the module"

    req = (Path(__file__).resolve().parent.parent / "requirements.txt"
           ).read_text(encoding="utf-8")
    assert "edge-tts" in req, "fresh installs would never get the neural voices"

    # 4. With the feature switched off the routes still answer - honestly, not with
    #    a 500 - and a POST is still CSRF-protected like every other state change.
    os.environ["TRIOFORGE_TTS"] = "off"
    try:
        import app as forge_app
        with forge_app.app.test_client() as client:
            d = client.get("/api/tts/voices").get_json() or {}
            assert d.get("ok") is True, d
            assert d.get("available") is False, d
            assert d.get("voices") == [], d
            assert "switched off" in (d.get("reason") or ""), d
            token = (client.get("/api/csrf").get_json() or {}).get("token") or ""
            client.environ_base["HTTP_X_CSRF_TOKEN"] = token
            r = client.post("/api/tts/speak",
                            json={"text": "hello", "voice": "en-US-AvaNeural"})
            assert r.status_code == 503, r.status_code
            assert "switched off" in ((r.get_json() or {}).get("error") or "")
        with forge_app.app.test_client() as bare:
            assert bare.post("/api/tts/speak", json={"text": "hi"}).status_code == 403, \
                "speech synthesis must be CSRF-protected too"
    finally:
        del os.environ["TRIOFORGE_TTS"]
    print("  neural list served by the app, offline/off/garbage handled -> OK")


# ---------------------------------------------------------------------------
# resolve_model — a SHORT name must map to a real .gguf, not a bogus path
# ---------------------------------------------------------------------------
def test_resolve_model_short_names() -> None:
    _title("resolve_model maps short names")
    import os
    import tempfile

    import llamacpp_service as svc

    # Hermetic: a temp models/ dir, not the real one, so the test runs the same on
    # a fresh clone and on a machine full of models. (It used to probe hardcoded
    # model names, which only passed on CI because CI has no models and hit the
    # skip branch — a machine WITH models but without those exact three failed.)
    with tempfile.TemporaryDirectory(prefix="tf-resolve-") as tmp:
        model = os.path.join(tmp, "qwen2.5-7b-instruct-Q4_K_M.gguf")
        projector = os.path.join(tmp, "mmproj-qwen2.5-7b-instruct-BF16.gguf")
        for p in (model, projector):
            with open(p, "w", encoding="utf-8"):
                pass
        saved = svc._model_roots
        svc._model_roots = lambda: [tmp]
        try:
            # The pickers show SHORT names (no .gguf, no quant suffix); these used
            # to be treated as relative paths, so start() auto-picked a different
            # model than the one chosen.
            got = svc.resolve_model("qwen2.5-7b-instruct")
            assert got and os.path.isfile(got), f"short name -> {got!r}"
            assert got == model, f"short name -> {got!r}, expected the model file"
            # The paired projector shares the base name and sorts FIRST, so it must
            # never win the short-name match — only find_mmproj returns it.
            assert "mmproj" not in os.path.basename(got).lower(), got
            # The exact full filename still resolves too.
            assert svc.resolve_model("qwen2.5-7b-instruct-Q4_K_M.gguf") == model
        finally:
            svc._model_roots = saved
    print("  short names resolve to the real model; the projector never wins -> OK")


# ---------------------------------------------------------------------------
# the gguf scan must behave the SAME on case-sensitive and -insensitive systems
# ---------------------------------------------------------------------------
def test_scan_case_insensitive() -> None:
    _title("gguf scan is case-insensitive")
    import os

    import llamacpp_service as svc

    # A .GGUF (uppercase) file is invisible to glob("*.gguf") on Linux but visible
    # on Windows/macOS — the same model folder behaved differently per OS. The scan
    # must find it everywhere, so a probe file proves it on whatever this runs on.
    models = Path(__file__).resolve().parent.parent / "models"
    os.makedirs(models, exist_ok=True)
    probe = models / "case_probe_upper.GGUF"
    try:
        probe.write_text("", encoding="utf-8")
        found = [f for f in svc._list_gguf_files() if os.path.basename(f) == probe.name]
        assert found, "an uppercase .GGUF must be found on every OS"
        print("  an uppercase .GGUF is found (same on all OSes) -> OK")
    finally:
        probe.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# the web model list must reflect files dropped in / deleted from models/
# ---------------------------------------------------------------------------
def test_model_list_rescans_live() -> None:
    _title("model list rescans live")
    import os
    import tempfile

    from providers.llm_providers import LlamaCppProvider

    # A temp models_dir, and a server_url that is refused instantly (port 1), so
    # this never writes into the real models/ folder and the provider's background
    # server-poll returns immediately instead of probing a closed 8080 port.
    with tempfile.TemporaryDirectory(prefix="tf-live-models-") as tmp:
        provider = LlamaCppProvider(models_dir=tmp,
                                    server_url="http://127.0.0.1:1/v1")
        probe = os.path.join(tmp, "live_probe.GGUF")   # uppercase too
        with open(probe, "w", encoding="utf-8"):
            pass
        assert any("live_probe" in m.lower() for m in provider.list_models()), \
            "a dropped GGUF must appear without a restart"
        os.remove(probe)
        assert not any("live_probe" in m.lower() for m in provider.list_models()), \
            "a deleted GGUF must disappear without a restart"
        print("  dropped -> appears, deleted -> disappears, no restart -> OK")


# ---------------------------------------------------------------------------
# comfyui /object_info must not cache forever (a dropped model shows up live)
# ---------------------------------------------------------------------------
def test_comfyui_object_info_ttl() -> None:
    _title("comfyui object_info TTL")
    import time
    from unittest import mock

    import requests
    import comfyui_service as cs

    saved = (cs._object_info_cache, cs._object_info_fetched_at)
    try:
        with mock.patch.object(cs.requests, "get",
                               side_effect=requests.ConnectionError("down")):
            # fresh cache -> served with no network round-trip
            cs._object_info_cache = {"fresh": 1}
            cs._object_info_fetched_at = time.time()
            assert cs.get_object_info() == {"fresh": 1}
            # stale cache + down server -> keeps the last good list, no raise
            cs._object_info_fetched_at = time.time() - 999
            assert cs.get_object_info() == {"fresh": 1}
            # empty cache + down server -> still raises (caller decides)
            cs._object_info_cache = None
            try:
                cs.get_object_info()
                raise AssertionError("empty cache + down server must raise")
            except requests.ConnectionError:
                pass
    finally:
        cs._object_info_cache, cs._object_info_fetched_at = saved
    print("  re-asks after the TTL, keeps the stale list on failure -> OK")


# ---------------------------------------------------------------------------
# Ctrl+R must reload the app (which re-scans models) and be listed in the help
# ---------------------------------------------------------------------------
def test_ctrl_r_refresh_shortcut() -> None:
    _title("Ctrl+R refreshes and rescans")
    import re

    html = (Path(__file__).resolve().parent.parent / "templates" / "index.html"
            ).read_text(encoding="utf-8")
    assert re.search(r"e\.key === 'r' \|\| e\.key === 'R'", html), "Ctrl+R handler is gone"
    assert "location.reload()" in html, "Ctrl+R no longer reloads the app"
    assert "Refresh everything" in html, "the help entry for Ctrl+R is gone"
    assert 'Ctrl</kbd> + <kbd class="hp-key">R</kbd>' in html, \
        "Ctrl+R is not listed in the shortcuts panel"
    print("  Ctrl+R reloads + rescans, and is listed in the help -> OK")


# ---------------------------------------------------------------------------
# The message box must stay usable: it grows with the text, but it can always be
# resized by hand and never hides what you typed.
#
# What went wrong: #msgInput was `flex: 1 1 0%` with `resize: none` and
# `overflow-y: hidden`, next to a row of flex-shrink:0 controls. On a narrow
# window the box was squeezed to 34px wide, a pasted 300-character string then
# wrapped one character per line (6024px of scrollHeight in a 138px box), and
# because there was no scrollbar and no handle there was no way to fix it. In a
# real browser (headless Chrome, 1032px window) the box was 34px wide before and
# 390px after, and dragging the handle resized it 64px -> 315px.
# ---------------------------------------------------------------------------
def test_composer_cannot_be_crushed_or_hidden() -> None:
    _title("the message box stays usable and resizable")
    import re

    html = (Path(__file__).resolve().parent.parent / "templates" / "index.html"
            ).read_text(encoding="utf-8")

    box = re.search(r"#msgInput \{(.*?)\n\}", html, re.S)
    assert box, "#msgInput CSS block is gone"
    css = box.group(1)

    # 1. It must not collapse: a real flex basis plus a minimum width.
    assert re.search(r"flex:\s*1\s+1\s+\d+px", css), \
        "a zero flex-basis lets the controls squeeze the box to nothing"
    assert re.search(r"min-width:\s*[2-9]\d\dpx", css), "the box has no min-width"
    # 2. A long unbroken paste must wrap instead of forming a 1-character column.
    assert "overflow-wrap: anywhere" in css, "long pasted words no longer wrap"
    # 3. The bar wraps its controls rather than crushing the box.
    bar = re.search(r"\.input-bar \{(.*?)\n\}", html, re.S)
    assert bar and "flex-wrap: wrap" in bar.group(1), ".input-bar does not wrap"

    # 4. Overflow must be reachable: JS turns the scrollbar on when it matters.
    assert "function applyComposerHeight" in html, "the composer sizing helper is gone"
    assert re.search(r"overflowing\(\w+\) \? 'auto' : 'hidden'", html), \
        "the box no longer un-hides its scrollbar when the text overflows"
    assert re.search(r"!!msgInput\.value && msgInput\.scrollHeight", html), \
        "an empty box would show a scrollbar for its placeholder"

    # 5. A real, discoverable resize handle (auto-grow alone cannot be undone).
    assert 'id="composerGrip"' in html, "the resize handle is gone"
    assert ".composer-grip" in html and "ns-resize" in html, "the handle has no resize cursor"
    assert "function composerMax" in html and "function resetComposerHeight" in html, \
        "composer height helpers are missing"

    # 6. The height the user drags to wins over auto-grow, and survives a reload.
    assert "composerUserH" in html, "the user's chosen height is not tracked"
    assert "tf.composerHeight" in html, "the chosen height is not remembered"
    assert "msgInput.style.height = '46px'" not in html, \
        "a hard 46px reset would throw away the height the user dragged to"

    print("  can be crushed -> no; hidden overflow -> scrollbar; handle -> yes -> OK")


# ---------------------------------------------------------------------------
# A small window must stay usable: the sidebar drawer has to be closable, and the
# top bar must not stack its rows on top of each other.
#
# What went wrong: under 900px the sidebar becomes an overlay drawer drawn over the
# ☰ that opened it, and the click-away scrim was a body::after pseudo-element, so it
# could not receive a click. Measured in headless Chrome on an 886x593 window:
# pressing ☰ put an H2 (the drawer's own heading) on top of the toggle, clicking the
# toggle again did nothing, Esc did nothing, clicking the scrim did nothing, and
# widening the window left the drawer marked open. The only escape was picking a
# conversation. On the same window the Chat/Notes/Cork Board pills were painted over
# the icon toolbar (tabs 74-110px, icons 50-133px) because .center-tabs was given
# row 2 and the toolbar was auto-placed into the same row.
# ---------------------------------------------------------------------------
def test_small_window_chrome_stays_usable() -> None:
    _title("a small window can always get its sidebar back")
    import re

    html = (Path(__file__).resolve().parent.parent / "templates" / "index.html"
            ).read_text(encoding="utf-8")

    # 1. The scrim is a real element that closes the drawer (a ::after cannot).
    assert "body.sidebar-open::after" not in html, \
        "the scrim is still a pseudo-element, so clicking it closes nothing"
    assert re.search(r'id="sidebarScrim"', html), "the click-away scrim is gone"
    assert re.search(r'id="sidebarScrim"[^>]*onclick="closeSidebarDrawer\(\)"', html), \
        "clicking the scrim does not close the drawer"
    assert "function closeSidebarDrawer" in html, "closeSidebarDrawer() is gone"
    # 2. ...and the drawer carries its own ✕, because it covers the toggle.
    assert re.search(r'id="sidebarCloseBtn"[^>]*onclick="closeSidebarDrawer\(\)"', html), \
        "the drawer has no close button of its own"
    # 3. Esc closes it.
    assert re.search(r"e\.key === 'Escape'[^\n]*\n?[^\n]*sidebar-open", html) or \
        ("'Escape'" in html and "classList.contains('sidebar-open')" in html), \
        "Escape does not close the drawer"
    # 4. Widening the window clears the state instead of leaving it armed.
    assert re.search(r"addEventListener\('change'", html) and "isNarrow()" in html, \
        "the drawer state is not cleared when the window gets wide again"

    # 5. No stacked grid rows: every top-bar child has its own row under 900px.
    #    There is more than one 900px block in the file (the logo rule is another),
    #    so all of them are collected rather than trusting the first.
    blocks = re.findall(r"@media \(max-width: 900px\) \{(.*?)\n\}", html, re.S)
    assert blocks, "the small-window media query is gone"
    block = "\n".join(blocks)
    for sel, row in ((".top-bar .left", 1), (".center-tabs", 2),
                     (".top-bar-icons", 3), (".top-bar .right", 4)):
        assert re.search(re.escape(sel) + r"[^}]*grid-row:\s*%d" % row, block), \
            "%s is not pinned to grid row %d, so rows can overlap" % (sel, row)

    # 6. The icon toolbar is one scrollable row instead of two wrapped ones.
    assert "flex-wrap: nowrap" in block and "overflow-x: auto" in block, \
        "the icon toolbar wraps again, costing vertical space on a small window"

    # 7. The model controls get their own row, leaving buttons + box together.
    assert re.search(r'class="composer-controls"', html), "the controls wrapper is gone"
    assert re.search(r"\.composer-controls \{ display: contents; \}", html), \
        "the wrapper must be display:contents so the wide layout is unchanged"
    assert re.search(r"\.composer-controls \{[^}]*flex: 1 1 100%", block), \
        "the controls do not take their own row on a small window"

    print("  drawer: scrim + ✕ + Esc + resize; top bar: one row per child -> OK")


# ---------------------------------------------------------------------------
# The window can be dragged very small - WebView2 ignores pywebview's min_size -
# and the app has to survive it.
#
# Measured in headless Chrome before this:
#   650x420  top bar 230px, conversation 31px
#   560x380  composer 167px tall and its last row - Send included - painted BELOW
#            the window (bar top 260 + 167 = 427 in a 380px window)
#   470x320  the provider/persona/voice/country row wrapped onto four lines, top
#            bar 268px, Send off-screen
# and the scroll-to-bottom arrow sat at a fixed bottom:100px, i.e. on top of the
# message box on any window where the composer was taller than that.
# After: composer 115px (two rows) at every size, Send inside the window at
# 400x300, top bar capped at 42vh, conversation 85px at 650x420.
# ---------------------------------------------------------------------------
def test_tiny_window_stays_usable() -> None:
    _title("a very small window keeps its composer and its conversation")
    import re

    root = Path(__file__).resolve().parent.parent
    html = (root / "templates" / "index.html").read_text(encoding="utf-8")
    win = (root / "py" / "tools" / "app_window.py").read_text(encoding="utf-8")

    # 1. The dropdown row scrolls instead of wrapping into four lines, and the bar
    #    may never take more than ~42% of the window.
    assert re.search(r"\.top-bar \{ max-height: 42vh; overflow-y: auto; \}", html), \
        "the top bar can grow without limit again on a short window"
    assert re.search(r"\.top-bar \.right \{[^}]*flex-wrap: nowrap[^}]*overflow-x: auto", html), \
        "the dropdown row wraps onto more lines instead of scrolling"
    # 2. The chat area is allowed to shrink rather than pushing the composer out.
    assert ".chat-area { min-height: 0; }" in html, \
        "the chat area can still push the composer below the window"
    # 3. Narrow windows: drop the model filter and let the pickers squeeze, so the
    #    composer stays two rows tall.
    narrow = re.search(r"@media \(max-width: 620px\) \{(.*?)\n\}", html, re.S)
    assert narrow, "the 620px block is gone"
    nb = narrow.group(1)
    assert re.search(r"#modelSearchInput \{ display: none; \}", nb), \
        "the model filter still costs a composer row on a narrow window"
    assert re.search(r"\.input-bar \.model-select \{[^}]*min-width: 80px", nb), \
        "the pickers keep their 120px floor and wrap Send onto a third row"

    # 4. The scroll arrow is measured off the composer, not fixed at 100px.
    assert re.search(r"scrollBtn\.style\.bottom = Math\.round\(lift\)", html), \
        "the scroll arrow is positioned by a fixed offset again"
    assert "bar.offsetHeight" in html and "statusEl.offsetHeight" in html, \
        "the arrow offset does not measure the composer"

    # 5. A real minimum size, enforced where min_size actually is not.
    assert re.search(r"^MIN_USABLE_W = \d+", win, re.M) and \
        re.search(r"^MIN_USABLE_H = \d+", win, re.M), "the usable minimum size is gone"
    assert "min_size" in win, "the reason min_size cannot do this is no longer documented"
    from tools import app_window
    # A normal window is left alone; a user-shrunk one is nudged to the floor and
    # nothing more; the engine collapse still goes back to the requested size.
    assert app_window._size_target(1400, 800, 1400, 800) == (None, ""), "a good window was resized"
    assert app_window._size_target(app_window.MIN_USABLE_W, app_window.MIN_USABLE_H,
                                   1400, 800) == (None, ""), "the floor itself must be allowed"
    floor = app_window._size_target(400, 300, 1400, 800)
    assert floor == ((app_window.MIN_USABLE_W, app_window.MIN_USABLE_H), "floor"), floor
    collapsed = app_window._size_target(160, 28, 1400, 800)
    assert collapsed == ((1400, 800), "collapsed"), collapsed

    print("  composer two rows; Send on screen; bar capped; arrow above it -> OK")


# ---------------------------------------------------------------------------
# "💡 Suggest Links" was a dead button: it popped an alert telling you to use a
# different button, and even that one only printed "• Title (72%)" plus "use the
# 💡 button in the toolbar to create them" - which did nothing. A button that
# cannot do its job should not be in the toolbar.
# ---------------------------------------------------------------------------
def test_suggest_links_actually_creates_threads() -> None:
    _title("Suggest Links finds related pins and draws the threads")
    import re

    root = Path(__file__).resolve().parent.parent
    src = (root / "py" / "features" / "cork_board.py").read_text(encoding="utf-8")

    # 1. No placeholder left anywhere.
    assert "will be implemented soon" not in src, "the stub alert is still there"
    assert "Use \"💡 Suggest Links\" button in toolbar to create them" not in src, \
        "the per-pin result still points at the button instead of offering the action"

    # 2. The whole-board endpoint exists and is wired to the toolbar button.
    assert "@corkboard_bp.route('/api/link_suggestions'" in src, "the endpoint is gone"
    assert re.search(r"function openLinkSuggestions\(\) \{[\s\S]{0,600}?/corkboard/api/link_suggestions",
                     src), "the toolbar button does not call the endpoint"
    assert "function createAllSuggestedLinks" in src, "there is no way to accept them all"
    assert re.search(r"dataset\.from = p\.a", src) and re.search(r"dataset\.from = editingPinId", src), \
        "the suggestion rows lost their Create-thread buttons"

    # 3. The pairing logic itself, on fake embeddings - no model, no network.
    sys.path.insert(0, str(root / "py"))
    sys.path.insert(0, str(root / "py" / "tools"))
    from features import cork_board as cb

    def pin(vec, title="t"):
        return {"title": title, "content": "", "embedding": vec}

    pins = {
        "a": pin([1.0, 0.0, 0.0], "prayer"),
        "b": pin([0.98, 0.02, 0.0], "praising"),
        "c": pin([0.0, 1.0, 0.0], "database migration"),
        "d": pin([1.0, 0.0, 0.0], "same as a"),
        "e": {"title": "no embedding", "content": "x"},      # must be skipped
    }
    got = cb.suggest_link_pairs(pins)
    assert got, "the obvious duplicates were not suggested"
    scores = [s for _, _, s in got]
    assert scores == sorted(scores, reverse=True), scores
    assert got[0][2] > 0.99 and {got[0][0], got[0][1]} == {"a", "d"}, got[0]
    assert all(0.3 <= s <= 1.0001 for _, _, s in got), scores
    keys = {frozenset((x, y)) for x, y, _ in got}
    assert frozenset(("c", "e")) not in keys and not any("e" in k for k in keys), \
        "a pin without an embedding was compared"
    assert frozenset(("a", "c")) not in keys, "an unrelated pair (<0.3) was suggested"

    # A pair that is already threaded is not offered again, in either direction.
    skipped = cb.suggest_link_pairs(pins, existing=[("d", "a")])
    assert not any({x, y} == {"a", "d"} for x, y, _ in skipped), skipped

    # The limit is honoured, and a high threshold narrows the list (a and d are the
    # same vector, so they survive any threshold; b sits at 0.9998).
    assert len(cb.suggest_link_pairs(pins, limit=1)) == 1
    tight = cb.suggest_link_pairs(pins, threshold=0.9999)
    assert len(tight) == 1 and {tight[0][0], tight[0][1]} == {"a", "d"}, tight

    # 4. The pure-Python fallback (no numpy) must agree with the numpy path, since
    #    that is what runs if numpy is ever missing.
    with_np = cb.suggest_link_pairs(pins)
    saved = cb._np
    try:
        cb._np = None
        without_np = cb.suggest_link_pairs(pins)
    finally:
        cb._np = saved
    assert [(x, y) for x, y, _ in with_np] == [(x, y) for x, y, _ in without_np], \
        (with_np, without_np)
    assert all(abs(a[2] - b[2]) < 1e-9 for a, b in zip(with_np, without_np)), (with_np, without_np)

    print("  pairs found, sorted, thresholded, deduped; both code paths agree -> OK")


# ---------------------------------------------------------------------------
def main() -> int:
    # Tests must not read or write the user's real configuration.
    os.environ.setdefault("FORGE_CONFIG_DIR", str(Path(__file__).parent / ".tmp"))
    # A Windows console defaults to cp1252, so a test that prints an emoji passes on
    # the machine it was written on and dies with UnicodeEncodeError only on the
    # runner - the hardest kind of CI-only failure. Force UTF-8 on both streams.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:      # noqa: BLE001 - a redirected stream may refuse
                pass
    tests = [test_parse_text_calls, test_agent_cancel, test_agent_wire_ids,
             test_agent_read_only_stall,
             test_known_facts, test_memory_is_optin, test_agent_verify_and_preamble,
             test_plan_load, test_python_floor_calendar, test_comfyui_auto_start,
             test_comfyui_paths_cross_platform, test_comfyui_widget_mapping_and_errors,
             test_comfyui_workflow_readiness, test_comfyui_model_autoscan,
             test_comfyui_model_paths, test_comfyui_manager_model_install,
             test_image_steps_default_to_the_workflow,
             test_inject_preserves_workflow_prompts,
             test_image_panel_choices,
             test_auto_detect_prefers_the_users_own_workflow,
             test_discord_plugin_stays_isolated,
             test_restore_list_supports_bulk_cleanup,
             test_daily_briefing_plugin, test_briefing_panel_is_wired, test_human_text_fields_are_spellcheckable, test_memory_panel_and_free_are_honest, test_comfyui_idle_unload_wiring, test_chat_draft_and_copy_wiring, test_local_timeout_and_output_cap_knobs, test_local_request_has_a_hard_deadline, test_ai_voice_dropdown_is_wired,
               test_neural_voices_are_offered,

             test_comfyui_unload_is_safe, test_gpu_probe_never_blocks,
             test_team_directives, test_session_window,
             test_write_empty_args, test_bash_clean_before_truncate,
             test_history_persists, test_router_repair_is_complex,
             test_window_slot_is_exclusive, test_window_ready_means_visible,
             test_launcher_recognises_its_own_exe,
             test_deepseek_catalog, test_design_feature, test_workspace_path_guard,
             test_design_persists_in_conversation, test_design_artifacts_listing,
             test_design_run_terminal, test_design_runner_languages,
             test_plugin_tools, test_skills, test_mcp_client,
             test_toolbar_icons_unique, test_extensions,
             test_tui_plugin_commands, test_obsidian_connector,
             test_shipped_plugins_load, test_failures_are_visible,
             test_tool_calls_are_recorded, test_vision_paths,
             test_role_alternation, test_csrf_protection,
             test_launcher_symlink_resolution, test_resolve_model_short_names,
             test_scan_case_insensitive, test_model_list_rescans_live,
             test_comfyui_object_info_ttl, test_ctrl_r_refresh_shortcut,
             test_composer_cannot_be_crushed_or_hidden,
             test_small_window_chrome_stays_usable,
             test_tiny_window_stays_usable,
             test_suggest_links_actually_creates_threads]
    failed = []
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            failed.append((t.__name__, exc))
            print(f"  FAIL {t.__name__}: {exc}")
            traceback.print_exc()
    try:
        asyncio.run(_tui_checks())
    except Exception as exc:  # noqa: BLE001
        failed.append(("_tui_checks", exc))
        print(f"  FAIL _tui_checks: {exc}")
        # Print WHERE it failed. A bare message like "No nodes match '#face'" left
        # no way to tell a flaky harness wait from a real regression.
        traceback.print_exc()

    print()
    if failed:
        print(f"FAILED: {', '.join(n for n, _ in failed)}")
        return 1
    print("ALL REGRESSION TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
