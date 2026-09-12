"""Process helpers for launching things without a console window.

Everything TrioForge starts (git, uv, pip, mkcert, llama-server, the app itself) is
a *console* program. When the parent has no console of its own - which is the case
for every hidden launch: start.vbs, start.bat, the low-level pythonw path - Windows
gives each console child a brand-new console window. On a machine whose default
terminal is Windows Terminal that appears as a terminal window titled with the
child's path, which is exactly what "why is a command popping up" looks like.

CREATE_NO_WINDOW suppresses it. When the parent *does* have a console (someone ran
application.bat in a terminal) we leave the flag off, so output still lands in the
terminal they are watching.
"""

import os
import subprocess
import sys


def no_window_flags() -> int:
    """Creation flags that stop a console child from opening its own window."""
    if os.name != "nt":
        return 0
    try:
        if sys.stdout is None or not sys.stdout.isatty():
            return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    except Exception:
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def hidden_popen_kwargs() -> dict:
    """Keyword arguments for Popen: detached, no console, no inherited handles."""
    flags = no_window_flags()
    if os.name == "nt":
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    return {
        "creationflags": flags,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
