"""Python release support window, so the floor's end-of-life cannot sneak up.

python.org runs a fixed cadence: a release every October, each supported ~5 years
(bugfix, then security-only). ``pyproject.toml`` declares the floor with
``requires-python``, and that floor must move up before its version's EOL month.

This module is the calendar. It answers "is the floor about to die, and what
should it become?" It is pure (a ``today`` can be injected), so it is unit-tested
and also powers ``tools/check_python_floor.py``, which CI runs on every push.
"""

from __future__ import annotations

import datetime as _dt

#: python.org release schedule: (major, minor) -> (status, (eol_year, eol_month)).
#: EOL is MONTH-granularity on purpose: the final security release lands in that
#: month, but its exact day is not fixed years ahead. Source:
#: https://devguide.python.org/versions/ (kept here so the check has no network
#: dependency). Add a row for 3.16 here when it is released, in the usual October.
PYTHON_RELEASES = {
    (3, 10): ("end-of-life", (2026, 10)),
    (3, 11): ("security",     (2027, 10)),
    (3, 12): ("security",     (2028, 10)),
    (3, 13): ("security",     (2029, 10)),
    (3, 14): ("bugfix",       (2030, 10)),
    (3, 15): ("bugfix",       (2031, 10)),
}

#: How far ahead of EOL the check starts nagging. At 12 months the warning shows up
#: in CI on every push, which is the moment to schedule the bump - a warning is a
#: reminder, and only a PAST-EOL floor is a hard failure.
WARN_MONTHS = 12


def months_to(eol: tuple[int, int], today: _dt.date | None = None) -> int:
    """Whole months from ``today`` until the EOL month. Negative once past."""
    today = today or _dt.date.today()
    return (eol[0] - today.year) * 12 + (eol[1] - today.month)


def support(major: int, minor: int, today: _dt.date | None = None) -> dict:
    """The support status of one interpreter version.

    Returns ``{"status", "eol", "months", "eol_passed", "near_eol"}``. A version
    not in the table returns ``eol=None`` / ``months=None`` and neither flag set,
    so a floor bumped to a version nobody recorded fails LOUDLY (unknown) rather
    than guessing a date that looks authoritative.
    """
    today = today or _dt.date.today()
    key = (major, minor)
    if key not in PYTHON_RELEASES:
        return {"status": "unknown", "eol": None, "months": None,
                "eol_passed": False, "near_eol": False}
    status, eol = PYTHON_RELEASES[key]
    months = months_to(eol, today)
    return {
        "status": status,
        "eol": eol,
        "months": months,
        "eol_passed": months < 0,
        "near_eol": 0 <= months <= WARN_MONTHS,
    }


def recommended_floor(today: _dt.date | None = None) -> tuple[int, int] | None:
    """The oldest version a floor should sit on TODAY.

    Walks the schedule upward and stops at the first version that is neither past
    its EOL nor inside the warning window - i.e. still has a real support window
    ahead. This is what makes the shift automatic: today it answers 3.12, and the
    moment 3.12 enters its final year it answers 3.13, and so on. Returns None when
    every recorded version is EOL or about to be - add a row to the table.
    """
    today = today or _dt.date.today()
    for key in sorted(PYTHON_RELEASES):
        info = support(*key, today)
        if not info["eol_passed"] and info["months"] > WARN_MONTHS:
            return key
    return None
