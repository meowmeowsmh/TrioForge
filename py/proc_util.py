"""One place for Windows console hiding and process-tree cleanup."""
import os
import subprocess

_installed = False


def hidden_flags() -> int:
    """creationflags that stop a console program from opening a cmd window (0 off Windows)."""
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


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
