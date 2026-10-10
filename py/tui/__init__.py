"""TrioForge terminal client.

A Claude-Code-style TUI that lives entirely in the terminal and is INDEPENDENT of
the Flask web GUI: it shares no routes, no templates and no static files. The only
things it reuses are the model providers, so "what model is configured" stays in
one place.

Run it with the ``forge`` script at the repo root, or::

    PYTHONPATH=py .venv-linux/bin/python -m tui
"""

__version__ = "1.5.1"
