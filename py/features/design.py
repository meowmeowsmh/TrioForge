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

# A complete prototype is a big document AND the model reasons before writing it, so
# this budget has to cover both. Measured: 8,192 total truncated a landing page
# mid-CSS; 65,536 finished it (finish_reason="stop") with room to spare.
DESIGN_MAX_TOKENS = 65536

# Providers whose design output should badge as ☁️ API rather than 🖥️ Local.
_API_KINDS = {"deepseek", "groq", "claude", "gemini", "openrouter", "huggingface"}

# Condensed from OpenDesign's design-brief skill: resolve the brief into concrete
# design tokens (palette / typography / layout / mood / density) BEFORE writing, and
# emit one complete HTML document. The discipline is what stops "make it
# professional" from producing a vague, broken page.
DESIGN_SYSTEM_PROMPT = (
    "You are a senior product-design engineer and front-end architect. You turn a "
    "plain-language design brief into ONE finished, production-quality, self-contained "
    "HTML page.\n\n"
    "STEP 1 - resolve the design system before you write any markup:\n"
    "- palette (light_clean #FFFFFF/#F8FAFC/#0F172A, monochrome_dark #09090B/#18181B/#FAFAFA, "
    "navy_and_white #0F172A/#1E293B/#F8FAFC, earth_tones #FFFBEB/#FEF3C7/#451A03)\n"
    "- accent (coral #F97316, electric_blue #3B82F6, emerald #10B981, muted_sage #84A98C)\n"
    "- typography scale (display / body / small) and a mono face\n"
    "- spacing scale, radii, and one shadow level\n"
    "- layout model and density (section spacing 48px compact / 72px balanced / 96px spacious)\n"
    "Emit those choices as CSS custom properties in :root and use ONLY those tokens.\n\n"
    "STEP 2 - write the page. It must be a COMPLETE, finished page, not a sketch:\n"
    "1. Output ONLY the HTML. The very first characters must be <!doctype html>. No "
    "preamble, no explanation, no markdown fences, nothing after </html>.\n"
    "2. One <style> block in the <head> with a REAL stylesheet: at least 120 lines of "
    "CSS covering the reset, tokens, layout, every component, and responsive rules. "
    "A page whose classes have no CSS is a failed answer.\n"
    "3. Semantic HTML5: header/nav/main/section/article/footer, one h1, real landmarks, "
    "aria-label on nav and icon-only controls, alt/aria-hidden on decorative SVG. No div soup.\n"
    "4. Style every component you emit: header with sticky/blur, nav links with hover and "
    ":focus-visible, hero with a clear type hierarchy and a primary + secondary CTA, "
    "feature cards, and a footer. Buttons and links need hover, active and :focus-visible states.\n"
    "5. Responsive for real: mobile-first with at least two breakpoints (640px, 1024px). "
    "Grid or flex layouts, fluid type with clamp(), and any mobile menu must be styled and "
    "usable, never a bare unhidden list.\n"
    "6. Content: real, specific, well-written copy for the brief. Never lorem ipsum, never "
    "'Feature 1 / Feature 2'. Real names, real numbers, real labels.\n"
    "7. Self-contained: no build step and no external JS libraries. Inline SVG / CSS "
    "gradients instead of images. Google Fonts via <link> are allowed, always with a "
    "system-font fallback stack.\n"
    "8. Craft details: max-width container, consistent vertical rhythm, WCAG AA contrast, "
    "cursor:pointer on interactive elements, smooth transitions (150-250ms), and no layout "
    "shift. Respect prefers-reduced-motion if you animate.\n"
    "9. For a 'refine' instruction, edit the provided HTML and return the whole file again "
    "with everything not mentioned left intact.\n"
)


def _extract_html(text):
    """Pull a usable HTML document out of whatever the model returned.

    Two failures this must not repeat: keeping the model's 'Here is your page:' prose in
    front of the doctype (it renders as visible text in quirks mode), and slicing from
    ``<body>`` - which throws away the ``<style>`` block in the head and leaves a fully
    structured page with NO styling at all.
    """
    t = (text or "").strip()
    if not t:
        return None

    # 0) If the answer is fenced, take the largest fenced block.
    fences = re.findall(r"```(?:html|htm|html5)?\s*\n?(.*?)```", t, re.S | re.I)
    if fences:
        t = max(fences, key=len).strip()
        if not t:
            return None

    low = t.lower()

    # 1) Start at the document, or at the FIRST structural tag - whichever comes first.
    #    Starting at <body> when a <style> sits above it is the bug that lost the CSS.
    starts = [low.find(n) for n in ("<!doctype", "<html", "<head", "<style", "<body")]
    starts = [i for i in starts if i != -1]
    if starts:
        t = t[min(starts):].strip()
    else:
        m = re.search(r"<\s*(main|div|section|header|nav|article|form|table|h1)\b", low)
        if not m:
            return None
        t = t[m.start():].strip()

    # 2) Drop anything the model wrote after the document (closing commentary).
    low = t.lower()
    end = low.rfind("</html>")
    if end != -1:
        t = t[:end + len("</html>")]
    elif "<html" not in low:
        end = low.rfind("</body>")
        if end != -1:
            t = t[:end + len("</body>")]
    return t.strip() or None


def _looks_unstyled(html):
    """True when the page has structure but essentially no stylesheet.

    A prototype whose classes carry no CSS renders as unstyled browser defaults, which
    is exactly the 'the code quality is terrible' report. Used to retry once.
    """
    low = html.lower()
    if "<link" in low and "stylesheet" in low:
        return False
    css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S | re.I))
    return css.count("{") < 8



def _model_answer(provider, messages, model, api_key, max_tokens=DESIGN_MAX_TOKENS):
    """Run the model and return the actual answer text.

    Thinking models (DeepSeek-V4 Flash/Pro) put their chain-of-thought in
    ``reasoning_content`` and the answer in ``content`` — but occasionally the answer
    lands in ``reasoning_content`` with an empty ``content`` (the same case the chat
    stream already papers over). Read the raw message so neither field is lost.

    ``max_tokens`` is generous on purpose: a thinking model spends this budget on its
    reasoning FIRST, so a chat-sized budget leaves the page truncated mid-stylesheet.
    """
    kwargs = {"model": model} if model else {}
    if api_key:
        kwargs["api_key"] = api_key
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    raw = None
    if hasattr(provider, "generate_raw"):
        try:
            raw = provider.generate_raw(messages, **kwargs)
        except TypeError:
            kwargs.pop("max_tokens", None)        # provider takes no max_tokens
            try:
                raw = provider.generate_raw(messages, **kwargs)
            except Exception:
                raw = None
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
    """Guarantee a standalone document without fighting the model's own styling.

    A fragment keeps its own ``<style>`` blocks - they are moved into the head rather
    than left in the body - and the only fallback CSS is a zero margin. An earlier
    version injected ``background:#fff;color:#111``, which silently wrecked dark-mode
    designs whenever the model's stylesheet had been dropped upstream.
    """
    low = html.lower()
    if "<!doctype" in low or "<html" in low:
        if "<!doctype" not in low:
            return "<!doctype html>\n" + html          # quirks mode otherwise
        return html
    styles = re.findall(r"<style[^>]*>.*?</style>", html, re.S | re.I)
    body = html
    for block in styles:
        body = body.replace(block, "", 1)
    head = "\n".join(styles)
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"{head}<style>html,body{{margin:0;padding:0}}</style></head>"
        f"<body>{body}</body></html>"
    )


@design_bp.route("/artifacts", methods=["GET"])
def artifacts():
    """List the saved design files - the folder the Design Studio writes into.

    Every page generated from the chat lands in ``static/uploads/generated/designs``;
    this is what the 📁 button in the studio header browses, so a design can be found
    again after the chat scrolls away.
    """
    items = []
    try:
        names = os.listdir(DESIGNS_DIR)
    except Exception:
        names = []
    for name in names:
        if not name.lower().endswith((".html", ".htm")):
            continue
        path = os.path.join(DESIGNS_DIR, name)
        try:
            st = os.stat(path)
        except Exception:
            continue
        items.append({
            "id": name,
            "url": "/static/uploads/generated/designs/" + name,
            "size": st.st_size,
            "mtime": st.st_mtime,
        })
    items.sort(key=lambda it: it["mtime"], reverse=True)
    return jsonify({"folder": DESIGNS_DIR, "count": len(items), "files": items})


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
    conversation_id = (data.get("conversation_id") or "").strip()

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
        best = None
        last_exc = None
        # Two attempts, because two different failures are common: a thinking model
        # occasionally echoes its input instead of answering, and the first answer can
        # come back as structure with no stylesheet. The second attempt is kept only if
        # it is actually better (a styled page beats an unstyled one).
        for _ in range(2):
            try:
                answer = _model_answer(provider, messages, model, api_key)
            except Exception as exc:
                last_exc = exc
                logger.warning("design attempt failed (%s/%s): %s", provider_name, model, exc)
                continue
            candidate = _extract_html(answer)
            if not candidate:
                continue
            if best is None or (_looks_unstyled(best) and not _looks_unstyled(candidate)):
                best = candidate
            if not _looks_unstyled(candidate):
                break
        if best is None:
            detail = f": {last_exc}" if last_exc else " (the model returned no HTML)"
            return jsonify({"error": "The model did not produce a design" + detail}), 502
        html = _wrap_html(best)
        if _looks_unstyled(html):
            logger.warning("design came back without a stylesheet (%s/%s)", provider_name, model)
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

    # Persist into the conversation so a design survives a reload. The HTML already
    # lives in static/uploads (permanent); this writes the brief + a bot message whose
    # meta.design lets the UI re-render the live preview from history.
    if conversation_id:
        try:
            from app import add_message
            add_message(conversation_id, "user", prompt)
            add_message(
                conversation_id,
                "bot",
                "🎨 Design generated — open in the 🎨 Design Studio to edit.",
                meta={"design": url, "kind": "api" if provider_name in _API_KINDS else "local"},
            )
        except Exception as exc:
            logger.warning("design conversation save failed: %s", exc)

    return jsonify({"ok": True, "url": url, "id": name, "html": html})
