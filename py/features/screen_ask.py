# screen_ask.py – "what is on my screen?" answered by the LOCAL vision model.
#
# Privacy win: the screenshot never leaves the machine. Uses whichever screenshot
# tool exists, then hands the image to the llama.cpp vision provider (mmproj).
#
#   POST /api/screen/ask   {"question": "..."}  -> {"answer": "...", "capture": tool}
#
# Bind this to a keyboard shortcut:
#   sh -c 'curl -s -X POST http://127.0.0.1:5003/api/screen/ask \
#          -H "Content-Type: application/json" \
#          -d "{\"question\":\"What is on my screen?\"}" | ...'

import os
import base64
import logging
import subprocess
import tempfile

from flask import Blueprint, request, jsonify

from providers.llm_providers import get_provider

import llamacpp_service

logger = logging.getLogger(__name__)

screen_bp = Blueprint('screen_ask', __name__, url_prefix='/api/screen')

DEFAULT_QUESTION = "Describe what is on my screen and point out anything important."

# Tried in order; first one that produces a non-empty file wins.
CAPTURE_COMMANDS = (
    ("xfce4-screenshooter", ["xfce4-screenshooter", "-f", "-s", "{out}"]),
    ("scrot",               ["scrot", "{out}"]),
    ("import",              ["import", "-window", "root", "{out}"]),
    ("gnome-screenshot",    ["gnome-screenshot", "-f", "{out}"]),
    ("spectacle",           ["spectacle", "-b", "-n", "-o", "{out}"]),
    ("grim",                ["grim", "{out}"]),
)


# Screenshots are 1920x1200+; the vision projector runs on the CPU here, so image
# encoding dominates the runtime. Shrinking first cuts it dramatically. Pillow is
# optional on purpose: requirements.txt avoids it because it needs Visual C++ build
# tools on Windows, so if it is missing we just send the full-size image.
SHRINK_WIDTH = 1280


def _shrink(path):
    """Return (path, was_shrunk). Best-effort - never fatal."""
    try:
        from PIL import Image
    except Exception:
        return path, False
    try:
        with Image.open(path) as im:
            if im.width <= SHRINK_WIDTH:
                return path, False
            height = int(im.height * (SHRINK_WIDTH / float(im.width)))
            small = im.convert("RGB").resize((SHRINK_WIDTH, height), Image.LANCZOS)
            out = path.rsplit(".", 1)[0] + "-small.jpg"
            small.save(out, "JPEG", quality=85, optimize=True)
        return out, True
    except Exception as exc:
        logger.warning("could not shrink screenshot: %s", exc)
        return path, False


def _capture(path):
    """Take a full-screen screenshot. Returns the tool name, or (None, reason)."""
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")
    errors = []
    for name, template in CAPTURE_COMMANDS:
        cmd = [a.replace("{out}", path) for a in template]
        if not _which(cmd[0]):
            continue
        try:
            subprocess.run(cmd, env=env, timeout=20,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except Exception as exc:
            errors.append("%s: %s" % (name, exc))
            continue
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return name, None
        errors.append("%s produced no file" % name)
    return None, "; ".join(errors) or "no screenshot tool found"


def _which(prog):
    from shutil import which
    return which(prog)


def _vision_answer(question, image_b64, model=None):
    """Send the screenshot to the local vision model.

    llama-server must be RUNNING first - a vision request against a server that
    was never started just waits for the readiness timeout and fails. start()
    also pairs the model's mmproj projector, which vision needs.
    """
    state = llamacpp_service.start(model=model or None)
    if state.get("error") and not state.get("running"):
        raise RuntimeError(state.get("error") or "llama.cpp failed to start")
    provider = get_provider("llamacpp")
    provider._check_server()          # wait for the model to finish loading
    messages = [{"role": "user", "content": question}]
    images = [{"b64": image_b64, "mime": "image/png", "name": "screen.png"}]
    return provider.generate_with_image(messages, images)


@screen_bp.route('/ask', methods=['POST'])
def ask():
    data = request.get_json(silent=True) or {}
    question = (data.get('question') or '').strip() or DEFAULT_QUESTION

    fd, path = tempfile.mkstemp(prefix="trio-screen-", suffix=".png")
    os.close(fd)
    try:
        tool, error = _capture(path)
        if not tool:
            return jsonify({"error": "could not capture the screen",
                            "detail": error}), 500
        small, shrunk = _shrink(path)
        with open(small, "rb") as fh:
            raw = fh.read()
        if shrunk:
            logger.info("screenshot shrunk for vision (%d bytes)", len(raw))
    finally:
        for candidate in (path, path.rsplit(".", 1)[0] + "-small.jpg"):
            try:
                os.unlink(candidate)
            except OSError:
                pass

    if not raw:
        return jsonify({"error": "captured an empty screenshot"}), 500

    b64 = base64.b64encode(raw).decode("ascii")
    try:
        answer = _vision_answer(question, b64, data.get('model'))
    except Exception as exc:
        logger.error("vision answer failed: %s", exc)
        return jsonify({"error": "vision model failed: %s" % exc,
                        "capture": tool}), 502

    return jsonify({"answer": (answer or "").strip(), "capture": tool,
                    "question": question})


@screen_bp.route('/tools', methods=['GET'])
def tools():
    """Which screenshot backends are available on this machine."""
    return jsonify({name: bool(_which(cmd[0])) for name, cmd in CAPTURE_COMMANDS})
