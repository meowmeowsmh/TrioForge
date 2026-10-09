"""deepseek_harness.py — a TrioForge PLUGIN that delegates tasks to DeepSeek Harness.

DeepSeek Harness (`dsh`) is a full coding agent: it has its own tools, its own
model loop and its own context. This plugin gives TrioForge's agent a way to hand
a *whole task* to it — "refactor this module and run the tests" — and read back
the finished result.

It shells out to the headless profile, which answers one task, prints the result
and exits:

    dsh headless "run the tests"

Setup (once, on the machine that runs TrioForge):
    dsh headless --from-default-profile web     # create the profile if missing
    dsh --version                              # sanity check it is on PATH

The task runs synchronously and can take minutes, so the tool takes a timeout and
returns whatever dsh printed. Each call spends DeepSeek tokens — it is a
delegation, not a cheap lookup.
"""

import glob
import os
import shutil
import subprocess

#: Where the npx cache puts the dsh launcher when it is not installed globally.
_NPX_GLOBS = (
    os.path.expanduser("~/.npm/_npx/*/node_modules/.bin/dsh"),
    os.path.expanduser("~/.npm/_npx/*/node_modules/.bin/dsh.cmd"),
)

DEFAULT_TIMEOUT = 300
MAX_TIMEOUT = 1800

MANIFEST = {
    "name": "deepseek-harness",
    "title": "DeepSeek Harness",
    "version": "1.0.0",
    "description": "Delegate a whole task to the DeepSeek Harness coding agent (dsh).",
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "dsh_run",
            "description": "Hand a whole coding task to DeepSeek Harness and get its finished result. It is a full agent, so this is slow (minutes) and spends DeepSeek tokens — use it for real multi-step work, not quick lookups.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "the task to hand to DeepSeek Harness, e.g. 'run the test suite and fix what fails'"},
                    "timeout": {"type": "integer", "description": "seconds to wait (default 300, max 1800)"},
                },
                "required": ["task"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dsh_status",
            "description": "Check whether DeepSeek Harness (dsh) is installed and which profiles exist.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]


def _is_deepseek_harness(exe):
    """True only for DeepSeek Harness.

    Debian/Ubuntu ship a DIFFERENT program also called ``dsh`` — "Distributed
    Shell / Dancer's shell" — which is first on PATH and answers
    "dsh: no machine specified" to anything it is given. Checking the version
    banner keeps us from driving that one by mistake.
    """
    try:
        p = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15)
        out = ((p.stdout or "") + (p.stderr or "")).lower()
    except Exception:
        return False
    if not out.strip():
        return False
    return "dancer" not in out and "distributed shell" not in out


def _find_dsh():
    """The DeepSeek Harness launcher: the npx cache first, then a verified PATH dsh."""
    for pattern in _NPX_GLOBS:
        for cand in sorted(glob.glob(pattern), reverse=True):
            if os.path.isfile(cand):
                return cand
    exe = shutil.which("dsh")
    if exe and _is_deepseek_harness(exe):
        return exe
    return None


def _conflicting_dsh():
    """A different `dsh` on PATH (Dancer's shell), when there is one."""
    exe = shutil.which("dsh")
    if exe and not _is_deepseek_harness(exe):
        return exe
    return ""


def _dsh_home():
    return os.environ.get("DSH_HOME") or os.path.expanduser("~/.dsh")


def _profiles():
    try:
        return sorted(e for e in os.listdir(os.path.join(_dsh_home(), "profiles"))
                      if not e.startswith(".") and e != "node_modules")
    except OSError:
        return []


def _status():
    exe = _find_dsh()
    profs = _profiles()
    return {
        "installed": bool(exe),
        "path": exe or "",
        "profiles": profs,
        "headless_ready": "headless" in profs,
        "dsh_home": _dsh_home(),
        "conflicting_dsh": _conflicting_dsh(),
    }


def _run(task, timeout):
    if not task or not str(task).strip():
        return {"error": "no task given"}
    exe = _find_dsh()
    if not exe:
        return {"error": "DeepSeek Harness (dsh) was not found on PATH or in the npx "
                         "cache. Install it, then try again."}
    try:
        timeout = min(max(int(timeout or DEFAULT_TIMEOUT), 10), MAX_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    st = _status()
    if not st["headless_ready"]:
        return {"error": "the 'headless' profile does not exist yet. Create it once with:\n"
                         "  dsh headless --from-default-profile web",
                "profiles": st["profiles"]}

    try:
        proc = subprocess.run([exe, "headless", str(task).strip()],
                              capture_output=True, text=True, timeout=timeout,
                              cwd=os.path.expanduser("~"))
    except subprocess.TimeoutExpired:
        return {"error": "DeepSeek Harness did not finish within {}s. Raise the "
                         "timeout, or hand it a smaller task.".format(timeout)}
    except Exception as exc:  # noqa: BLE001 - the failure is the result
        return {"error": "{}: {}".format(type(exc).__name__, exc)}

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    result = {"exit_code": proc.returncode,
              "output": out[-20000:] if out else "",
              "duration_note": "ran via: dsh headless"}
    if proc.returncode != 0:
        result["error"] = (err or out or "dsh exited {}".format(proc.returncode))[-2000:]
    elif not out:
        result["output"] = "(dsh finished with no output)"
    return result


def dispatch(tool_name, args):
    args = args or {}
    if tool_name == "dsh_run":
        return _run(args.get("task"), args.get("timeout"))
    if tool_name == "dsh_status":
        st = _status()
        if not st["installed"]:
            return {"summary": "DeepSeek Harness (dsh) is NOT installed / not found."}
        return dict(st, summary="DeepSeek Harness found at {}. Profiles: {}.{}".format(
            st["path"], ", ".join(st["profiles"]) or "(none)",
            "" if st["headless_ready"] else " No 'headless' profile yet — create it with "
            "'dsh headless --from-default-profile web'."))
    return {"error": "unknown tool " + str(tool_name)}
