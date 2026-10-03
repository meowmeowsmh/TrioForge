"""Automatic local/cloud routing for a single prompt.

Decides whether a prompt should run on a cheap **local** model (llama.cpp) or an
expensive **cloud** API, from two facts and nothing else:

    1. Is the cloud API reachable right now?   offline  -> local, always
    2. Is the task complex?                     complex + online -> cloud

No model is asked to make the decision — it is a few regexes and one TCP connect,
so routing itself costs nothing and works offline. This is the same idea as the
open-source routers Wayfinder, cortiq-gateway and sriti-core: a logic gate in
front of a pool of models, not another model in the loop.

The user-facing answer is a plain dict (``engine``, ``provider``, ``model``,
``reason``, ``complexity``) so both the terminal client and the web app can render
it, and the tests can assert it without any network or GPU.
"""

from __future__ import annotations

import re
import socket
import urllib.parse

# --- "complex": a bigger model earns its keep ---------------------------------
_COMPLEX_HINTS = (
    "architecture", "algorithm", "design a system", "design an api",
    "refactor", "optimize", "prove", "proof", "derive", "theorem",
    "recursion", "dynamic programming", "concurrency", "thread", "async",
    "deadlock", "database schema", "sql query", "machine learning",
    "neural net", "train a", "derivative", "integral", "linear algebra",
    "matrix", "solve", "equation", "debug", "why is this failing",
    "explain this code", "time complexity", "big o",
    # Repair and coding verbs. Without these, "fix the X button", "the shop is
    # broken", "make it work" all classified as simple and went to the weak
    # local model - which read files and never wrote the fix, so the user saw
    # "who is coding?" and a turn full of nothing. A repair needs a model that
    # can actually edit code.
    "fix", "broken", "not working", "doesn't work", "does not work",
    "make it work", "repair", "won't work", "is not working", "crash",
    "error", "exception", "failing", "failure",
)

# --- "simple": a small local model is plenty --------------------------------
_SIMPLE_HINTS = (
    "boilerplate", "format", "rename", "clean up", "list files", "delete",
    "summarize", "rewrite", "translate", "what is", "hello", "hi",
    "short", "simple", "take notes", "title", "fix the grammar",
)

# Code that is being written or reasoned about, not just mentioned.
_CODE_SHAPE = (
    re.compile(r"\b(def|class|function|fn|func)\s+\w+"),
    re.compile(r"[{};]\s*$", re.M),
    re.compile(r"\b(import|from|require|include|package)\b"),
)


def _contains_word(text: str, hint: str) -> bool:
    """True when ``hint`` appears as whole words in ``text``.

    Not a bare substring: without this, the simple hint "hi" matched inside
    "arc**hi**tecture" and cancelled the "architecture" signal.
    """
    return re.search(
        r"(?<![a-z0-9])" + re.escape(hint) + r"(?![a-z0-9])", text) is not None


def classify(prompt: str) -> tuple[str, list[str]]:
    """Return ``("complex"|"simple", reasons)`` for a prompt.

    Deliberately conservative: only a clear signal tips it to "complex", because a
    wrong guess that sends every small job to a paid API is worse than one that
    leaves a slightly-hard task on a capable local model.
    """
    text = (prompt or "").strip()
    if not text:
        return "simple", ["empty"]
    low = text.lower()
    reasons: list[str] = []
    score = 0

    for hint in _COMPLEX_HINTS:
        if _contains_word(low, hint):
            score += 3                      # one clear signal is enough
            reasons.append(hint)
    for pattern in _CODE_SHAPE:
        if pattern.search(text):
            score += 2                      # code shape alone is often boilerplate
            reasons.append("code shape")
    if len(text) > 400:
        score += 2
        reasons.append("long/detailed")
    for hint in _SIMPLE_HINTS:
        if _contains_word(low, hint) and len(text) < 250:
            score -= 2

    if score >= 3:
        return "complex", reasons[:6]
    return "simple", reasons[:6]


def reachable(url: str, timeout: float = 3.0) -> bool:
    """Can we open a TCP connection to ``url``'s host? Cheap, offline-safe.

    A bare connect is enough: the question is "is this machine online and is the
    host listening", not "is the API healthy". No HTTP, no cert, no request.
    """
    if not url:
        return False
    host = urllib.parse.urlparse(url).hostname
    port = urllib.parse.urlparse(url).port or 443
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def decide(prompt: str, *, cloud_provider: str, cloud_url: str, cloud_model: str,
           local_model: str = "", cloud_reachable: bool | None = None) -> dict:
    """Resolve one prompt to ``local`` or ``cloud``.

    Order of the rules, matching the flowchart:

    1. cloud unreachable          -> local  ("offline")
    2. complex task               -> cloud  ("deep math / architecture")
    3. otherwise                  -> local  ("small task")

    ``cloud_reachable`` may be passed in to avoid a real connect in tests; when it
    is ``None`` the network is actually probed.
    """
    verdict, reasons = classify(prompt)
    if cloud_reachable is None:
        cloud_reachable = reachable(cloud_url)

    if not cloud_reachable:
        return {"engine": "local", "provider": "local", "model": local_model,
                "reason": "offline — cloud unreachable", "complexity": verdict}
    if verdict == "complex":
        return {"engine": "cloud", "provider": cloud_provider, "model": cloud_model,
                "reason": "complex task: {}".format(", ".join(reasons[:3]) or "signal"),
                "complexity": verdict}
    return {"engine": "local", "provider": "local", "model": local_model,
            "reason": "simple task — local is plenty", "complexity": verdict}


def explain(decision: dict) -> str:
    """One short line for the transcript, naming where the message is going."""
    arrow = "☁" if decision.get("engine") == "cloud" else "💻"
    model = decision.get("model") or ""
    return "{} → {} · {}".format(arrow, model or decision["engine"],
                                 decision.get("reason", ""))
