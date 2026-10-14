#!/usr/bin/env python3
"""CI gate: warn when the Python floor nears end-of-life, fail once it is past.

Reads ``requires-python`` from ``pyproject.toml``, consults the calendar in
``py/python_support.py``, and emits a GitHub workflow annotation (or plain text):

    - floor has a healthy window  -> exit 0, prints the OK line
    - floor within WARN_MONTHS     -> exit 0, a ``::warning::`` annotation (loud)
    - floor past EOL               -> exit 1, an ``::error::`` annotation (fails)

The point is that bumping the floor stops being something to remember and becomes
something CI raises, on schedule, with the target version already worked out.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from python_support import WARN_MONTHS, recommended_floor, support  # noqa: E402


def floor_from_pyproject() -> tuple[int, int] | None:
    """The ``>=3.X`` floor declared in pyproject.toml, or None if unreadable."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'requires-python\s*=\s*">=(\d+)\.(\d+)"', text)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)))


def _emit(kind: str, message: str) -> None:
    """A GitHub workflow command when in Actions, plain text otherwise."""
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::{kind} file=pyproject.toml::{message}")
    else:
        print(f"{'ERROR' if kind == 'error' else 'WARNING'}: {message}")


def _version(ver: tuple[int, int] | None) -> str:
    return f"{ver[0]}.{ver[1]}" if ver else "?"


def main() -> int:
    floor = floor_from_pyproject()
    if floor is None:
        _emit("error", "could not read requires-python from pyproject.toml")
        return 1

    info = support(*floor)
    recommended = recommended_floor()
    label = _version(floor)
    eol = f"{info['eol'][0]}-{info['eol'][1]:02d}"

    if info["eol_passed"]:
        _emit("error", f"Python {label} is end-of-life (EOL {eol}). Bump the "
                       f"floor to {_version(recommended)} — see docs/UPGRADING_PYTHON.md.")
        return 1
    if info["near_eol"]:
        _emit("warning", f"Python {label} goes end-of-life {eol} "
                         f"({info['months']} months). Bump the floor to "
                         f"{_version(recommended)} — see docs/UPGRADING_PYTHON.md.")
        return 0
    print(f"Python {label} is supported until {eol} ({info['months']} months). OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
