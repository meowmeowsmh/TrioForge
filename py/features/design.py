# design.py – OpenDesign-style "design a prototype from a brief" generator.
#
#   POST /api/design/generate
#       {"prompt": "...", "provider": "deepseek", "model": "deepseek-flash",
#        "api_key": "...", "existing_html": "..."}   # existing_html = refine
#       -> {"ok": true, "url": "/static/uploads/generated/designs/<id>.html",
#           "id": "...", "html": "..."}
#
# The model is asked to produce ONE self-contained HTML file (inline CSS, real
# content, no build step) and the app saves it and returns a URL the GUI renders in
# a sandboxed <iframe> with live preview + refine. This is OpenDesign's "prototype
# composer with live preview", reduced to a single button and one prompt.

import os
import re
import time
import uuid
import logging

from flask import Blueprint, request, jsonify

from paths import root_path
from providers.llm_providers import get_provider, sanitize_api_key

logger = logging.getLogger(__name__)

design_bp = Blueprint("design", __name__, url_prefix="/api/design")

DESIGNS_DIR = root_path("static", "uploads", "generated", "designs")

# Condensed from OpenDesign's design-brief skill: resolve the brief into concrete
# design tokens (palette / typography / layout / mood / density) BEFORE writing, and
# emit one complete HTML document. The discipline is what stops "make it
# professional" from producing a vague, broken page.
DESIGN_SYSTEM_PROMPT = (
    "You are a senior product-design engineer. You turn a plain-language design "
    "brief into ONE polished, self-contained HTML prototype.\n\n"
    "Before writing, resolve these design dimensions from the brief (fill any that "
    "are missing with a sensible default):\n"
    "- palette (light_clean #FFFFFF/#F8FAFC/#0F172A, monochrome_dark #09090B/#18181B/#FAFAFA, "
    "navy_and_white #0F172A/#1E293B/#F8FAFC, earth_tones #FFFBEB/#FEF3C7/#451A03)\n"
    "- accent (coral #F97316, electric_blue #3B82F6, emerald #10B981, muted_sage #84A98C)\n"
    "- typography (inter, system-ui, dm-sans, georgia) and display font\n"
    "- layout (single column, two column, asymmetric)\n"
    "- mood (professional_minimal, playful, brutalist, editorial)\n"
    "- density (compact 48px, balanced 72px, spacious 96px section spacing)\n\n"
    "Rules:\n"
    "1. Output ONLY the HTML document, starting with <!doctype html>. No preamble, "
    "no markdown fences, no explanation.\n"
    "2. Inline all CSS in a <style> block. Real, specific content (no lorem ipsum).\n"
    "3. Self-contained: no build step, no external JS libraries, no external images "
    "(use CSS, SVG, gradients and emoji instead). Google Fonts via <link> are allowed.\n"
    "4. Responsive: readable at 360px and 1280px. All interactive elements need a "
    ":focus-visible outline. Accent color appears at most a few times per viewport.\n"
    "5. Use only the resolved palette and fonts; do not invent colours outside it.\n"
    "6. For a 'refine' instruction, edit the provided HTML and return the whole file "
    "again, keeping everything that was not explicitly changed.\n"
)


def _extract_html(text):
    """Pull a usable HTML document out of whatever the model returned."""
    t = (text or "").strip()
    if not t:
        return None
    # 1) markdown code fence (most common failure mode)
    m = re.search(r"```(?:html|htm|html5)?\s*\n?(.*?)```", t, re.S | re.I)
    if m:
        return m.group(1).strip()
    # 2) a full document
    low = t.lower()
    if "<!doctype" in low or "<html" in low:
        return t
    # 3) a fragment that starts with a tag (keep it whole: it may carry a <style>
    #    block that a body-only slice would otherwise drop)
    if t.startswith("<"):
        return t
    # 4) body-only (prose + a <body> document)
    if "<body" in low:
        i = low.find("<body")
        j = low.find("</body>")
        return t[i:j + 7] if j != -1 else t[i:]
    # 5) prose before the first tag
    idx = t.find("<")
    if idx != -1 and re.match(r"<\s*(div|section|main|header|nav|h1|p|a|style|table|form)", t[idx:], re.I):
        return t[idx:]
    return None


def _model_answer(provider, messages, model, api_key):
    """Run the model and return the actual answer text.

    Thinking models (DeepSeek-V4 Flash/Pro) put their chain-of-thought in
    ``reasoning_content`` and the answer in ``content`` — but occasionally the answer
    lands in ``reasoning_content`` with an empty ``content`` (the same case the chat
    stream already papers over). Read the raw message so neither field is lost.
    """
    kwargs = {"model": model} if model else {}
    if api_key:
        kwargs["api_key"] = api_key
    raw = None
    if hasattr(provider, "generate_raw"):
        try:
            raw = provider.generate_raw(messages, **kwargs)
        except Exception:
            raw = None
    if isinstance(raw, dict):
        content = (raw.get("content") or "").strip()
        reasoning = (raw.get("reasoning_content") or raw.get("reasoning") or "").strip()
        return content or reasoning
    if raw is not None:
        return (raw or "").strip()
    return (provider.generate(messages, **kwargs) or "").strip()


def _wrap_html(html):
    """Guarantee a standalone document so the iframe renders without quirks."""
    low = html.lower()
    if "<!doctype" in low or "<html" in low:
        return html
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<style>body{margin:0;font-family:system-ui,-apple-system,sans-serif;"
        "line-height:1.6;background:#fff;color:#111;}</style></head>"
        f"<body>{html}</body></html>"
    )


@design_bp.route("/generate", methods=["POST"])
def generate():
    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "A design brief is required."}), 400

    provider_name = (data.get("provider") or "deepseek").strip().lower()
    model = (data.get("model") or "").strip()
    api_key = sanitize_api_key(data.get("api_key", None))
    existing_html = (data.get("existing_html") or "").strip()

    try:
        provider = get_provider(provider_name, api_key or None)
    except Exception as exc:
        return jsonify({"error": f"Unknown provider: {provider_name} ({exc})"}), 400

    if existing_html:
        user_msg = (
            "Refine the design below according to this instruction:\n\n"
            f"{prompt}\n\nExisting HTML:\n```html\n{existing_html}\n```"
        )
    else:
        user_msg = f"Design brief:\n{prompt}"

    messages = [
        {"role": "system", "content": DESIGN_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    try:
        html = None
        last_exc = None
        # A thinking model occasionally echoes its input instead of answering (the
        # intermittent DeepSeek failure). One retry is enough to clear most of them,
        # and it only ever fires when the first answer was unusable.
        for _ in range(2):
            try:
                answer = _model_answer(provider, messages, model, api_key)
                html = _extract_html(answer)
                if html:
                    break
            except Exception as exc:
                last_exc = exc
                logger.warning("design attempt failed (%s/%s): %s", provider_name, model, exc)
        if html is None:
            detail = f": {last_exc}" if last_exc else " (the model returned no HTML)"
            return jsonify({"error": "The model did not produce a design" + detail}), 502
        html = _wrap_html(html)
    except Exception as exc:
        logger.warning("design generation failed (%s/%s): %s", provider_name, model, exc)
        return jsonify({"error": f"The model failed: {exc}"}), 502

    os.makedirs(DESIGNS_DIR, exist_ok=True)
    name = "design-{}-{}.html".format(int(time.time()), uuid.uuid4().hex[:6])
    path = os.path.join(DESIGNS_DIR, name)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
    except Exception as exc:
        logger.warning("could not save design artifact: %s", exc)
        return jsonify({"error": "Could not save the generated design."}), 500

    url = "/static/uploads/generated/designs/" + name
    return jsonify({"ok": True, "url": url, "id": name, "html": html})
