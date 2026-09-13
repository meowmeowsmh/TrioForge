"""TrioForge's version, in one place the app can read at runtime.

Keep in sync with pyproject.toml (that is what packaging tools read; this is what the
running app reports). It shows up in the start-up log and in `--status`, which is the
first thing worth knowing when someone reports a problem.
"""

__version__ = "1.0.2"
