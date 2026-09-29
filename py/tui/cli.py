"""Command-line arguments.

Kept separate from app.py so ``--help`` and argument errors work even if the UI
libraries somehow fail to import.
"""

from __future__ import annotations

import argparse
import os

from . import __version__

DEFAULT_BASE_URL = os.environ.get(
    "FORGE_BASE_URL",
    os.environ.get("TRIO_BASE_URL", "http://127.0.0.1:8080/v1"),
)


def prog_name() -> str:
    """The name the user typed - ``trioforge`` or ``forge``.

    The launcher exports FORGE_PROG; ``python -m tui`` has no useful argv[0], so
    that falls back to the app's own name.
    """
    return os.environ.get("FORGE_PROG", "").strip() or "trioforge"


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog=prog_name(),
        description="TrioForge terminal client — a Claude-style chat UI for your models.",
        epilog="With no arguments it starts an interactive session. "
               "Give it words and it sends one message, prints the reply and exits.",
    )
    p.add_argument("prompt", nargs="*",
                   help="one-shot message; omit for interactive mode")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL,
                   help=f"OpenAI-compatible endpoint (default: {DEFAULT_BASE_URL})")
    p.add_argument("--provider", default="",
                   help="which provider to use (local, deepseek, groq, openrouter, ...)")
    p.add_argument("--model", default="",
                   help="model name; empty means ask the server (default)")
    p.add_argument("--setup", action="store_true",
                   help="guided setup: pick a provider, a model and paste the key")
    p.add_argument("--save", action="store_true",
                   help="remember --provider/--api-key/--base-url in the config file")
    p.add_argument("--api-key", default=os.environ.get("FORGE_API_KEY", ""),
                   help="bearer token for hosted endpoints")
    p.add_argument("--system", default=None,
                   help="system prompt (pass '' for none)")
    p.add_argument("--temperature", type=float, default=0.7)
    p.add_argument("--timeout", type=float, default=120.0,
                   help="per-request timeout in seconds")
    p.add_argument("--classic", action="store_true",
                   help="use the plain scroll-back UI instead of the full-screen one")
    p.add_argument("--echo", action="store_true",
                   help="offline stub: no model contacted, useful for a demo")
    p.add_argument("--no-banner", action="store_true",
                   help="skip the header (for scripts)")
    p.add_argument("--install-llama", action="store_true",
                   help="download the prebuilt llama.cpp for this machine and exit "
                        "(it is fetched automatically on first use anyway)")
    p.add_argument("--specs", action="store_true",
                   help="print the hardware that was detected (GPU, memory) and what "
                        "fits, then exit")
    p.add_argument("--version", action="version",
                   version=f"{prog_name()} {__version__}")
    return p.parse_args(argv)
