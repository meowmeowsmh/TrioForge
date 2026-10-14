"""One place for Windows console hiding and process-tree cleanup.

There are deliberately TWO policies here, because TrioForge needs both:

* :func:`hidden_flags` - ALWAYS CREATE_NO_WINDOW. For anything the user must never
  see: the dependency resolver, MCP servers, version probes.
* :func:`no_window_flags` - CREATE_NO_WINDOW only when THIS process has no console
  of its own. For a child whose output should still land in the terminal the user
  is watching (git during a foreground update, the app under run.sh).

These used to be two near-identically named modules (``py/proc_util.py`` and
``py/tools/procutil.py``) plus a third private copy inside ``tools/launcher.py``.
Everything now lives here; ``py/tools/procutil.py`` only re-exports, so old import
paths keep working.

:func:`install_hidden_consoles` is the belt-and-braces net: it patches
``subprocess.Popen`` so any spawn that does not set its own creationflags is
hidden. Explicit flags always win, so it never fights a call site that knows
better.
"""
import os
import subprocess
import sys

_installed = False


def hidden_flags() -> int:
    """creationflags that stop a console program from opening a cmd window (0 off Windows)."""
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def no_window_flags() -> int:
    """CREATE_NO_WINDOW only when we have no console of our own.

    Deliberately NOT an alias of :func:`hidden_flags`: when a launcher runs inside
    a terminal, that terminal IS the point, so the child's output must still reach
    it instead of being muted.
    """
    if os.name != "nt":
        return 0
    try:
        if sys.stdout is None or not sys.stdout.isatty():
            return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    except Exception:
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def hidden_popen_kwargs() -> dict:
    """Popen kwargs for a detached, windowless child with no inherited handles."""
    flags = no_window_flags()
    if os.name == "nt":
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    return {
        "creationflags": flags,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }


def hidden_tree_kwargs(devnull: bool = True) -> dict:
    """Popen kwargs that hide a console program AND every console child it spawns.

    Use this for a long-running SERVER (ComfyUI's ``main.py``, an MCP server), not
    ``hidden_flags`` and not ``hidden_popen_kwargs``:

    * ``CREATE_NO_WINDOW`` gives the child NO console, so a console grandchild has
      nothing to inherit and Windows hands it a brand-new VISIBLE window.
    * ``hidden_popen_kwargs`` uses ``no_window_flags``, which returns 0 whenever a
      terminal is attached - so launching from a terminal popped a real window with
      the server's own directory as its title. That is exactly what happened to the
      ComfyUI auto-start.

    ``CREATE_NEW_CONSOLE`` + ``STARTUPINFO(SW_HIDE)`` creates ONE console for the
    whole tree, hidden, so every descendant inherits the hidden one. Same fix
    ``mcp_client`` already uses for npx trees. On POSIX it is just the DEVNULL
    handles.
    """
    kwargs: dict = {}
    if devnull:
        kwargs["stdin"] = subprocess.DEVNULL
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    if os.name != "nt":
        return kwargs
    kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = si
    except Exception:  # pragma: no cover - STARTUPINFO always exists on Windows
        # Never risk a VISIBLE console: fall back to NO console at all.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return kwargs


def install_hidden_consoles() -> None:
    """Make every subprocess.Popen in this process hidden unless it sets its own flags."""
    global _installed
    if _installed or os.name != "nt":
        return
    _installed = True
    original = subprocess.Popen.__init__

    def _init(self, *args, **kwargs):
        if not kwargs.get("creationflags"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        original(self, *args, **kwargs)

    subprocess.Popen.__init__ = _init


def kill_tree(pid: int, timeout: float = 15) -> None:
    """Kill a process and all of its descendants."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=timeout, creationflags=hidden_flags())
            return
        import psutil
        proc = psutil.Process(pid)
        for p in proc.children(recursive=True) + [proc]:
            try:
                p.kill()
            except Exception:
                pass
    except Exception:
        pass


def kill_children(timeout: float = 3.0) -> None:
    """Terminate (then kill) every descendant of the current process."""
    try:
        import psutil
        kids = psutil.Process().children(recursive=True)
        for k in kids:
            try:
                k.terminate()
            except Exception:
                pass
        _, alive = psutil.wait_procs(kids, timeout=timeout)
        for k in alive:
            try:
                k.kill()
            except Exception:
                pass
    except Exception:
        pass
