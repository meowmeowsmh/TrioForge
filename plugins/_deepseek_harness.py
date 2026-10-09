"""deepseek_harness.py — a TrioForge PLUGIN that delegates tasks to DeepSeek Harness.

DeepSeek Harness (`dsh`) is a full coding agent: it has its own tools, its own
model loop and its own context. This plugin gives TrioForge's agent a way to hand
a *whole task* to it — "refactor this module and run the tests" — and read back
the finished result.

It shells out to the headless profile, which answers one task, prints the result
and exits:

    dsh headless "run the tests"

`headless` is a SHIPPED profile — there is nothing to create or configure first,
and `--from-default-profile` is rejected for it ("shipped and cannot be a custom
profile target"). Just run it. The only real prerequisites are that `dsh` is
installed and its credentials are already set up (the ones the web profile uses).

The task runs synchronously and can take minutes, so the tool takes a timeout and
returns whatever dsh printed. Each call spends DeepSeek tokens — it is a
delegation, not a cheap lookup.
"""

import glob
import os
import re
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
    # Not a connector: dsh is a local tool, not a service you sign into. Whether
    # the dsh binary is present is detected by the 🚀 Setup panel (setup_check.py),
    # which reuses this plugin's own _find_dsh()/_dsh_version().
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


def _dsh_version(exe):
    """(major, minor, patch) for DeepSeek Harness, or None for a different `dsh`.

    Debian/Ubuntu ship a DIFFERENT program also called ``dsh`` — "Distributed
    Shell / Dancer's shell" — which is first on PATH and answers
    "dsh: no machine specified" to whatever it is given. Its banner is how we
    tell the two apart.
    """
    try:
        p = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20)
        text = ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception:
        return None
    if not text:
        return None
    if "dancer" in text.lower() or "distributed" in text.lower():
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def _is_deepseek_harness(exe):
    return _dsh_version(exe) is not None


def _dsh_candidates():
    seen, out = set(), []
    for pattern in _NPX_GLOBS:
        for cand in glob.glob(pattern):
            if os.path.isfile(cand) and cand not in seen:
                seen.add(cand)
                out.append(cand)
    exe = shutil.which("dsh")
    if exe and exe not in seen:
        out.append(exe)
    return out


_find_cache = {"done": False, "exe": None}


def _find_dsh():
    """The NEWEST DeepSeek Harness launcher.

    The npx cache can hold several versions side by side (0.1.x next to 0.2.x),
    and PATH may hold an unrelated namesake, so pick the highest real version
    rather than whichever path sorts first.
    """
    if _find_cache["done"]:
        return _find_cache["exe"]
    best, best_v = None, None
    for cand in _dsh_candidates():
        v = _dsh_version(cand)
        if v is None:
            continue
        if best_v is None or v > best_v:
            best, best_v = cand, v
    _find_cache.update(done=True, exe=best)
    return best


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
        # `headless` is a shipped profile, so it is always available even when it
        # has never been run and has no directory under $DSH_HOME/profiles yet.
        "headless_available": True,
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

    # No profile check: `headless` ships with dsh and is never created by hand
    # (--from-default-profile refuses it as a shipped profile). A missing local
    # profile dir means "not run yet", not "not set up".
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
        summary = ("DeepSeek Harness found at {}. dsh_run works out of the box — the "
                   "headless profile ships with dsh, nothing to create.").format(st["path"])
        if st.get("conflicting_dsh"):
            summary += (" Note: a DIFFERENT program named dsh is on PATH ({}); this "
                        "plugin ignores it.").format(st["conflicting_dsh"])
        return dict(st, summary=summary)
    return {"error": "unknown tool " + str(tool_name)}
