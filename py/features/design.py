# design.py – OpenDesign-style "design a prototype from a brief" generator.
#
#   POST /api/design/generate
#       {"prompt": "...", "provider": "deepseek", "model": "deepseek-flash",
#        "api_key": "...", "existing_html": "..."}
#       -> {"ok": true, "project": "project-<ts>-<id>",
#           "files": [{"path": "index.html", "url": "..."}, ...],
#           "url": "<main html url or null>", "html": "<main html text>"}
#
# The model now produces a PROJECT: one or more files (a webpage is index.html +
# optional style.css/app.js; a Python program is main.py + modules + requirements.txt).
# Every generation lands in its own folder under static/uploads/generated/designs, so
# the 📁 Files tab shows folders, not a flat pile of html files.

import os
import re
import sys
import time
import uuid
import queue
import logging
import threading
import subprocess

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

# Live run sessions: a Python project can be executed in the studio and driven
# line-by-line from the browser, so a tic-tac-toe game is actually playable rather
# than a static block of code. Each session keeps its subprocess + an output queue.
_RUNS = {}
_RUNS_LOCK = threading.Lock()
_RUN_TIMEOUT_SECONDS = 120

# Condensed from OpenDesign's design-brief skill: resolve the brief into concrete
# design tokens BEFORE writing. The discipline is what stops "make it professional"
# from producing a vague, broken page - and the same care now applies to code projects.
DESIGN_SYSTEM_PROMPT = (
    "You are a senior product-design engineer and software architect. You turn a "
    "plain-language brief into a finished, self-contained project.\n\n"
    "STEP 1 - decide what kind of project the brief asks for:\n"
    "- a WEB PAGE or prototype -> build it as HTML/CSS/JS.\n"
    "- a PROGRAM (a game, a script, a tool, 'python tic-tac-toe', etc.) -> write the "
    "code. If the user names a language use it, otherwise Python. List any libraries "
    "it needs in requirements.txt.\n\n"
    "STEP 2 - for a WEB PAGE, resolve the design system first:\n"
    "- palette (light_clean #FFFFFF/#F8FAFC/#0F172A, monochrome_dark #09090B/#18181B/#FAFAFA, "
    "navy_and_white #0F172A/#1E293B/#F8FAFC, earth_tones #FFFBEB/#FEF3C7/#451A03)\n"
    "- accent (coral #F97316, electric_blue #3B82F6, emerald #10B981, muted_sage #84A98C)\n"
    "- typography scale, spacing scale, radii, one shadow level\n"
    "Emit those as CSS custom properties in :root and use ONLY those tokens. Write a "
    "REAL stylesheet (at least 120 lines), semantic HTML5, hover/:focus-visible states, "
    "and real responsive breakpoints (640px, 1024px). Real content, never lorem ipsum.\n\n"
    "STEP 3 - for a PROGRAM, write clean, runnable code: real logic (no TODOs or stubs), "
    "sensible names, a working entry point in main.py, and comments only where they help. "
    "If it needs libraries, list them in requirements.txt (name==version).\n\n"
    "OUTPUT FORMAT (critical - this is parsed, not read):\n"
    "Emit each file as a fenced code block whose first line is EXACTLY the file path. "
    "Nothing else on that line, no language tag, no prose between blocks:\n"
    "```index.html\n"
    "<!doctype html>...\n"
    "```\n"
    "```main.py\n"
    "import ...\n"
    "```\n"
    "A webpage's index.html is the file the Preview shows. Never wrap the whole answer "
    "in one big fence, and never write anything outside a file block.\n"
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
    fences = re.findall(r"```(?:html|htm|html5)?\s*\n?(.*?)```", t, re.S | re.I)
    if fences:
        t = max(fences, key=len).strip()
        if not t:
            return None
    low = t.lower()
    starts = [low.find(n) for n in ("<!doctype", "<html", "<head", "<style", "<body")]
    starts = [i for i in starts if i != -1]
    if starts:
        t = t[min(starts):].strip()
    else:
        m = re.search(r"<\s*(main|div|section|header|nav|article|form|table|h1)\b", low)
        if not m:
            return None
        t = t[m.start():].strip()
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
    """True when the page has structure but essentially no stylesheet."""
    low = html.lower()
    if "<link" in low and "stylesheet" in low:
        return False
    css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S | re.I))
    return css.count("{") < 8


def _safe_rel(path):
    """Sanitize a model-supplied file path into a safe relative path."""
    p = (path or "").replace("\\", "/").strip().strip("'\"").lstrip("./")
    parts = [seg for seg in p.split("/") if seg and seg not in (".", "..")]
    return "/".join(parts) or "file.txt"


def _looks_python(text):
    return bool(re.search(r"^\s*(import\s+\w+|from\s+\w+\s+import|def\s+\w+\s*\(|"
                          r"class\s+\w+|print\s*\()", text, re.M))


# A fenced file block: ```path<newline>content```
_FENCE_RE = re.compile(r"```([^\s`][^`\n]*?)\s*\n(.*?)```", re.S)


def _parse_project(text):
    """Turn the model's answer into [(relative_path, content), ...]."""
    t = (text or "").strip()
    if not t:
        return []

    files, seen = [], set()
    for m in _FENCE_RE.finditer(t):
        info = m.group(1).strip()
        if "." not in info:                      # a language tag, not a file path
            continue
        rel = _safe_rel(info)
        content = m.group(2).rstrip("\n")
        if rel in seen or not content.strip():
            continue
        seen.add(rel)
        files.append((rel, content))

    if files:
        return files

    # No fenced files: the whole answer is one document.
    html = _extract_html(t)
    if html:
        return [("index.html", html)]

    m = re.search(r"```(?:python|py|html|js|css)?\s*\n(.*?)```", t, re.S | re.I)
    if m and m.group(1).strip():
        body = m.group(1).strip()
        return [("main.py" if _looks_python(body) else "index.html", body)]
    return []


def _files_quality(files):
    """'good' or 'bad' - bad means the caller should retry once."""
    if not files:
        return "bad"
    main = next((c for p, c in files if p.lower().endswith((".html", ".htm"))), None)
    if main is not None:
        return "bad" if _looks_unstyled(main) else "good"
    # A code project just needs real content, not a length threshold: a tic-tac-toe is
    # short and still complete.
    total = sum(len(c) for _, c in files)
    return "good" if total > 40 else "bad"


def _model_answer(provider, messages, model, api_key, max_tokens=DESIGN_MAX_TOKENS):
    """Run the model and return the actual answer text.

    Thinking models put their chain-of-thought in ``reasoning_content`` and the answer
    in ``content``, but occasionally the answer lands in ``reasoning_content`` with an
    empty ``content``. Read the raw message so neither field is lost. The token budget
    is generous because a thinking model spends it on reasoning FIRST.
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
            kwargs.pop("max_tokens", None)
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
    """Guarantee a standalone document without fighting the model's own styling."""
    low = html.lower()
    if "<!doctype" in low or "<html" in low:
        if "<!doctype" not in low:
            return "<!doctype html>\n" + html
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


def _finalize_files(files):
    out = []
    for rel, content in files:
        if rel.lower().endswith((".html", ".htm")):
            content = _wrap_html(content)
        out.append((rel, content))
    return out


@design_bp.route("/artifacts", methods=["GET"])
def artifacts():
    """List the saved design projects (folders), plus any legacy loose html files."""
    projects, loose = [], []
    try:
        names = os.listdir(DESIGNS_DIR)
    except Exception:
        names = []
    for name in names:
        if name.startswith('.'):
            continue
        p = os.path.join(DESIGNS_DIR, name)
        try:
            st = os.stat(p)
        except Exception:
            continue
        if os.path.isdir(p):
            try:
                inner = [f for f in os.listdir(p) if not f.startswith('.')]
            except Exception:
                inner = []
            main = next((f for f in inner if f.lower().endswith(('.html', '.htm'))), None)
            projects.append({
                "name": name, "is_dir": True, "mtime": st.st_mtime,
                "count": len(inner), "main": main,
                "url": ("/static/uploads/generated/designs/{}/{}".format(name, main)
                        if main else None),
            })
        elif name.lower().endswith((".html", ".htm")):
            loose.append({"name": name, "is_dir": False, "mtime": st.st_mtime,
                          "size": st.st_size,
                          "url": "/static/uploads/generated/designs/" + name})
    projects.sort(key=lambda it: it["mtime"], reverse=True)
    loose.sort(key=lambda it: it["mtime"], reverse=True)
    return jsonify({"folder": DESIGNS_DIR, "count": len(projects) + len(loose),
                    "projects": projects, "files": loose})


@design_bp.route("/artifacts/<name>", methods=["GET"])
def project_files(name):
    """List the files inside one saved project folder."""
    name = _safe_rel(name)
    base = os.path.join(DESIGNS_DIR, name)
    if not os.path.isdir(base):
        return jsonify({"error": "Not a project folder: {}".format(name)}), 404
    files = []
    for root, _dirs, fns in os.walk(base):
        for fn in fns:
            if fn.startswith('.'):
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, base).replace('\\', '/')
            try:
                size = os.path.getsize(full)
            except Exception:
                size = 0
            files.append({"path": rel, "size": size,
                          "url": "/static/uploads/generated/designs/{}/{}".format(name, rel)})
    files.sort(key=lambda f: f["path"])
    return jsonify({"project": name, "files": files})


@design_bp.route("/run", methods=["POST"])
def run_project():
    """Start a project's entry file as a subprocess and drive it over polling."""
    data = request.get_json(silent=True) or {}
    project = _safe_rel(data.get("project") or "")
    entry = _safe_rel(data.get("entry") or "main.py")
    base = os.path.join(DESIGNS_DIR, project)
    if not project or not os.path.isdir(base):
        return jsonify({"error": "Project not found: {}".format(project)}), 404
    entry_path = os.path.join(base, entry)
    if not os.path.isfile(entry_path):
        return jsonify({"error": "Entry file not found: {}".format(entry)}), 404

    # Only Python (or any text script runnable by the interpreter) is executed. A
    # binary/asset is never a runnable entry point.
    if not entry.lower().endswith((".py", ".pyw")):
        return jsonify({"error": "Only Python files can be run (got {})".format(entry)}), 400

    # Cap concurrency so an abandoned tab cannot pile up processes.
    with _RUNS_LOCK:
        for sid, run in list(_RUNS.items()):
            if run["proc"].poll() is not None:
                _RUNS.pop(sid, None)
        if len(_RUNS) >= 8:
            return jsonify({"error": "Too many runs; stop one first."}), 429

    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", os.path.basename(entry_path)],
            cwd=base, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=1, universal_newlines=True,
            encoding="utf-8", errors="replace",
        )
    except Exception as exc:
        return jsonify({"error": "Could not start the program: {}".format(exc)}), 500

    out = queue.Queue()
    sid = uuid.uuid4().hex[:12]

    def _reader():
        try:
            for line in iter(proc.stdout.readline, ""):
                out.put(line)
        except Exception:
            pass
        finally:
            out.put(None)                       # EOF sentinel
            try:
                proc.stdout.close()
            except Exception:
                pass

    threading.Thread(target=_reader, daemon=True).start()
    with _RUNS_LOCK:
        _RUNS[sid] = {"proc": proc, "out": out, "project": project, "entry": entry}

    # A watchdog kills a run left dangling (the poller tab closed, the game hung).
    def _watchdog():
        time.sleep(_RUN_TIMEOUT_SECONDS)
        with _RUNS_LOCK:
            run = _RUNS.get(sid)
        if run and run["proc"].poll() is None:
            try:
                run["proc"].kill()
            except Exception:
                pass

    threading.Thread(target=_watchdog, daemon=True).start()
    return jsonify({"ok": True, "session": sid, "project": project, "entry": entry})


@design_bp.route("/run/<sid>/output", methods=["GET"])
def run_output(sid):
    with _RUNS_LOCK:
        run = _RUNS.get(sid)
    if run is None:
        return jsonify({"error": "No such run", "done": True}), 404
    chunks = []
    done = False
    try:
        while True:
            item = run["out"].get_nowait()
            if item is None:
                done = True
                break
            chunks.append(item)
    except queue.Empty:
        pass
    alive = run["proc"].poll() is None
    return jsonify({"chunks": chunks, "done": done, "alive": alive})


@design_bp.route("/run/<sid>/input", methods=["POST"])
def run_input(sid):
    data = request.get_json(silent=True) or {}
    line = data.get("line", "")
    with _RUNS_LOCK:
        run = _RUNS.get(sid)
    if run is None:
        return jsonify({"error": "No such run"}), 404
    proc = run["proc"]
    if proc.poll() is not None:
        return jsonify({"error": "The program has ended"}), 400
    try:
        proc.stdin.write(line + "\n")
        proc.stdin.flush()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True})


@design_bp.route("/run/<sid>/stop", methods=["POST"])
def run_stop(sid):
    with _RUNS_LOCK:
        run = _RUNS.pop(sid, None)
    if run and run["proc"].poll() is None:
        try:
            run["proc"].kill()
        except Exception:
            pass
    return jsonify({"ok": True})


@design_bp.route("/generate", methods=["POST"])
def generate():
    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "A brief is required."}), 400

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
            "Refine the project below according to this instruction:\n\n"
            f"{prompt}\n\nExisting HTML:\n```index.html\n{existing_html}\n```"
        )
    else:
        user_msg = f"Brief:\n{prompt}"

    messages = [
        {"role": "system", "content": DESIGN_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    try:
        best_files, best_q = None, "bad"
        last_exc = None
        for _ in range(2):
            try:
                answer = _model_answer(provider, messages, model, api_key)
            except Exception as exc:
                last_exc = exc
                logger.warning("design attempt failed (%s/%s): %s", provider_name, model, exc)
                continue
            files = _parse_project(answer)
            if not files:
                continue
            q = _files_quality(files)
            if best_files is None or (best_q == "bad" and q == "good"):
                best_files, best_q = files, q
            if q == "good":
                break
        if best_files is None:
            detail = f": {last_exc}" if last_exc else " (the model returned nothing)"
            return jsonify({"error": "The model did not produce a project" + detail}), 502
        files = _finalize_files(best_files)
    except Exception as exc:
        logger.warning("design generation failed (%s/%s): %s", provider_name, model, exc)
        return jsonify({"error": f"The model failed: {exc}"}), 502

    os.makedirs(DESIGNS_DIR, exist_ok=True)
    project = "project-{}-{}".format(int(time.time()), uuid.uuid4().hex[:6])
    proj_dir = os.path.join(DESIGNS_DIR, project)
    try:
        os.makedirs(proj_dir, exist_ok=True)
    except Exception as exc:
        logger.warning("could not create project folder: %s", exc)
        return jsonify({"error": "Could not create the project folder."}), 500

    saved = []
    try:
        for rel, content in files:
            rel = _safe_rel(rel)
            dest = os.path.join(proj_dir, rel)
            parent = os.path.dirname(dest)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(content)
            saved.append({"path": rel,
                          "url": "/static/uploads/generated/designs/{}/{}".format(project, rel)})
    except Exception as exc:
        logger.warning("could not save design files: %s", exc)
        return jsonify({"error": "Could not save the generated files."}), 500

    main_url = next((s["url"] for s in saved if s["path"].lower().endswith((".html", ".htm"))), None)
    main_html = next((c for p, c in files if p.lower().endswith((".html", ".htm"))), "")

    # Persist into the conversation so a design survives a reload.
    if conversation_id:
        try:
            from app import add_message
            add_message(conversation_id, "user", prompt)
            if main_url:
                add_message(conversation_id, "bot",
                            "🎨 Project generated — open in the 🎨 Design Studio.",
                            meta={"design": main_url, "kind": "api" if provider_name in _API_KINDS else "local"})
            else:
                file_names = ", ".join(s["path"] for s in saved[:6])
                add_message(conversation_id, "bot",
                            "🎨 Project generated (" + file_names + ") — open in the 🎨 Design Studio.",
                            meta={"design_project": project, "kind": "api" if provider_name in _API_KINDS else "local"})
        except Exception as exc:
            logger.warning("design conversation save failed: %s", exc)

    resp = {"ok": True, "project": project, "files": saved, "url": main_url, "id": project}
    if main_html:
        resp["html"] = main_html
    return jsonify(resp)
