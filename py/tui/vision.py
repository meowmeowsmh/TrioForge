"""Turn a path in a message into an image the model can actually see.

A terminal has no drag-and-drop, so the terminal client takes the other route: the
user names a file (or a folder), and it is attached to that message as an
OpenAI-style ``image_url`` part. ``what is in ~/Pictures/cat.jpg?`` then works with
no attachment UI at all, and naming a folder attaches the images inside it.

Only a model with a vision projector (mmproj) can read the result — a text-only
GGUF simply ignores the part — so the caller is expected to warn before sending.
"""

from __future__ import annotations

import base64
import mimetypes
import os
import re

#: Extensions llama.cpp's vision path accepts.
IMAGE_EXTS = frozenset((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"))

#: How many images one message may carry. More than a few is a load spike on a
#: small card, and paragraphs of description nobody asked for.
MAX_IMAGES = 4

#: path -> (mtime, size, data-url). The same message is re-sent on every round of
#: a turn, so encode each file once rather than on every request.
_CACHE: dict[str, tuple[float, int, str]] = {}

#: Every word, with a quoted phrase as one token so a path containing spaces works.
_TOKEN_RE = re.compile(r"\"([^\"]+)\"|'([^']+)'|(\S+)")

#: Trailing punctuation a sentence puts after a path: "see /tmp/a.png?".
_TRAILING = "),.;:!?"


def _is_image(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in IMAGE_EXTS


def paths_in(text: str, limit: int = MAX_IMAGES) -> list[str]:
    """Image files named in ``text`` — a direct path, or the images in a folder.

    Every token is simply TESTED as a path, which is what makes all the shapes
    people actually type work: ``~/Pictures/cat.jpg``, ``./shot.png``,
    ``screenshots/cat.jpg``, a bare ``cat.jpg``, and a folder. An earlier version
    only matched absolute paths and silently missed the rest. Wrap a path in quotes
    when it contains spaces.

    A folder is read as "these are the images I mean" — sorted, so the order is at
    least stable, capped at ``limit``.
    """
    found: list[str] = []
    for quoted, single, bare in _TOKEN_RE.findall(text or ""):
        if quoted or single:
            path = os.path.expanduser(quoted or single)
        else:
            path = os.path.expanduser(bare.rstrip(_TRAILING))
        try:
            if os.path.isfile(path):
                if _is_image(path):
                    found.append(os.path.abspath(path))
            elif os.path.isdir(path):
                for name in sorted(os.listdir(path)):
                    if _is_image(name):
                        found.append(os.path.abspath(os.path.join(path, name)))
                        if len(found) >= limit:
                            break
        except OSError:
            continue                        # unreadable path: just not an image
        if len(found) >= limit:
            break

    # De-duplicate, keeping the order the user mentioned them in.
    seen: set[str] = set()
    unique: list[str] = []
    for path in found:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique[:limit]


def data_url(path: str) -> str:
    """``data:image/png;base64,…`` for ``path``, or "" when it cannot be read."""
    try:
        stat = os.stat(path)
    except OSError:
        return ""
    hit = _CACHE.get(path)
    if hit and hit[0] == stat.st_mtime and hit[1] == stat.st_size:
        return hit[2]
    try:
        with open(path, "rb") as fh:
            payload = fh.read()
    except OSError:
        return ""
    mime = mimetypes.guess_type(path)[0] or "image/png"
    url = "data:{};base64,{}".format(mime, base64.b64encode(payload).decode("ascii"))
    _CACHE[path] = (stat.st_mtime, stat.st_size, url)
    return url


def attach(message: dict, limit: int = MAX_IMAGES) -> dict:
    """Return ``message`` with any named images attached as ``image_url`` parts.

    A message naming no image is returned untouched, so a plain chat keeps the
    simple string content every endpoint understands.
    """
    text = message.get("content")
    if not isinstance(text, str) or not text:
        return message
    parts: list[dict] = []
    for path in paths_in(text, limit):
        url = data_url(path)
        if url:
            # Name the file next to its pixels: with several images the model
            # otherwise cannot say which one it is describing.
            parts.append({"type": "text", "text": "Image: " + os.path.basename(path)})
            parts.append({"type": "image_url", "image_url": {"url": url}})
    if not parts:
        return message
    out = dict(message)
    out["content"] = [{"type": "text", "text": text}] + parts
    return out
