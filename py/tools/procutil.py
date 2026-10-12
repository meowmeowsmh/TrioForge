"""Deprecated alias for the one shared helper, :mod:`proc_util`.

Everything TrioForge starts (git, uv, pip, mkcert, llama-server, the app itself) is
a *console* program. When the parent has no console of its own - which is the case
for every hidden launch: TrioForge-Web.bat under pythonw, the low-level pythonw
path - Windows gives each console child a brand-new console window. On a machine
whose default terminal is Windows Terminal that appears as a terminal window titled
with the child's path, which is exactly what "why is a command popping up" looks
like. ``no_window_flags()`` suppresses it; when the parent *does* have a console
(someone ran a launcher in a terminal) the flag is left off, so output still lands
in the terminal they are watching.

This file used to carry its own copy of those helpers, which left two
near-identically named modules - ``procutil`` here and ``proc_util`` in ``py/`` -
doing the same job. The implementation now lives in ``py/proc_util.py`` and this
module only re-exports it, so the existing ``from procutil import no_window_flags``
imports in ``py/tools/updater.py`` and ``py/tools/shortcuts.py`` keep working
unchanged.

New code should import from ``proc_util`` directly.
"""
import os
import sys

# Make py/ importable: this file sits in py/tools/, one level below the module it
# re-exports. Checked for shadowing first - py/ and py/tools/ share no module names
# other than __init__.
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from proc_util import (                    # noqa: E402,F401
    hidden_flags,
    hidden_popen_kwargs,
    install_hidden_consoles,
    kill_children,
    kill_tree,
    no_window_flags,
)
