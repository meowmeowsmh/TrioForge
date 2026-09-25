"""Integrity checks: a hash baseline for the app's own files, plus a scan for the
shapes injected code takes.

Two independent jobs, both run on demand from the app (or `launcher.py --verify`):

1. HASHES. A baseline of SHA-256 hashes over the app's own files - the Python code,
   the templates, the vendored JavaScript libraries, the launcher scripts. Anything
   modified, added or removed since the baseline was written stands out. That is how
   you notice that something edited your app behind your back, which is exactly what
   a hijack looks like.

2. PATTERNS. A scan for the fingerprints of injected code: eval(), atob(),
   String.fromCharCode, new Function, document.write, remote <script> tags,
   javascript: URLs, very long base64 blobs, dynamic imports of system modules - and
   HTML/script sequences embedded inside an .exe/.dll/.vbs, which is what "a sequence
   from another HTML file" ends up looking like once it is stashed in a binary.

Honest limits, stated in the UI too: this is NOT an antivirus. It cannot see inside a
running process, it does not know about malware outside this folder, and a clever
edit that updates the baseline afterwards would pass. It detects the common cases and
it never claims more than it checked. Line endings are normalised before hashing so a
clone on Windows and one on Linux produce the same hashes.
"""

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

MANIFEST_NAME = "integrity-manifest.json"

# What counts as "the app's own files": code, templates, the app's own static assets
# (stylesheets, scripts, the vendored libraries, the icons) and the launcher scripts.
# Deliberately excluded: user data, models, logs, uploads, the venv and generated
# output - changes there are normal and would drown the signal. static/uploads and
# static/generated* are named in SKIP_PREFIXES below, so widening this to "static" is
# safe: it is what puts themes.css and theme-loader.js under the hash check.
INCLUDE_DIRS = ("py", "templates", "static", "docker")
INCLUDE_FILES = (
    "TrioForge.bat", "voice_agent.bat", ".gitattributes",
    "run.sh", "requirements.txt", "requirements-ml.txt", "pyproject.toml",
)
# Skipped by their path FROM THE REPO ROOT only. Matching on any path component named
# "tools" would also have excluded py/tools/ - the launcher, this file, the shortcut and
# autostart code - so an edit there went unnoticed. One directory name must never be
# enough to hide code.
SKIP_PREFIXES = (
    "tools", "models", "universal_models_to_text", "video_model",
    "logs", "sqlite_data", "json_configuration", "cert_store", "plugins",
    ".git", ".venv", ".venv-linux", "node_modules",
    "static/uploads", "static/generated", "static/generated_video", "static/generated_audio",
)
SKIP_ANY_COMPONENT = ("__pycache__", ".git", "node_modules")

# The patterns worth a human look. Each has a plain-language reason.
SUSPICIOUS_PATTERNS = (
    (r"\beval\s*\(", "eval() - runs a string as code"),
    (r"\batob\s*\(", "atob() - decodes text hidden in the file"),
    (r"String\.fromCharCode", "fromCharCode - assembles a string to hide it"),
    (r"new\s+Function\s*\(", "new Function - builds code from a string"),
    (r"document\.write\s*\(", "document.write - injects markup at runtime"),
    (r"<script[^>]{0,200}src\s*=\s*[\"']https?://", "a <script> tag loading from the internet"),
    (r"javascript:\s*[A-Za-z(]", "a javascript: URL"),
    (r"[A-Za-z0-9+/]{800,}={0,2}", "a very long base64-looking blob"),
    (r"__import__\s*\(\s*[\"'](?:os|subprocess|socket|ctypes)", "dynamic import of a system module"),
    (r"\bcurl\b[^\n]{0,80}\|\s*(?:sh|bash)", "piping a download straight into a shell"),
    (r"\bwget\b[^\n]{0,80}\|\s*(?:sh|bash)", "piping a download straight into a shell"),
)

# Sequences that should never appear inside a binary this app ships.
BINARY_MARKERS = (b"<script", b"javascript:", b"document.write", b"eval(", b"<iframe")

EXECUTABLE_EXTS = (".exe", ".dll", ".pyd", ".so", ".dylib", ".bat", ".cmd", ".vbs", ".ps1")
TEXT_EXTS = (".py", ".html", ".js", ".css", ".json", ".txt", ".md", ".bat", ".cmd",
             ".vbs", ".ps1", ".sh", ".yml", ".yaml", ".toml", ".cfg", ".ini")


def manifest_path(project):
    return Path(project) / MANIFEST_NAME


def _normalised(path):
    """File bytes with CRLF/CR folded to LF, so a Windows and a Linux checkout agree."""
    with open(path, "rb") as fh:
        return fh.read().replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _sha256(path):
    return hashlib.sha256(_normalised(path)).hexdigest()


def _skip_dir(rel_posix):
    """True if this repo-relative directory should not be walked."""
    if not rel_posix:
        return False
    if any(part in SKIP_ANY_COMPONENT for part in rel_posix.split("/")):
        return True
    return any(rel_posix == pre or rel_posix.startswith(pre + "/") for pre in SKIP_PREFIXES)


def _in_scope(rel_posix):
    """True if a repo-relative path is one of the app's own files."""
    if any(part in SKIP_ANY_COMPONENT for part in rel_posix.split("/")):
        return False
    if rel_posix in INCLUDE_FILES:
        return True
    return any(rel_posix == d or rel_posix.startswith(d + "/") for d in INCLUDE_DIRS)


def collect(project):
    """{repo-relative path: sha256} for every file in scope."""
    project = Path(project)
    found = {}
    for root, dirs, files in os.walk(project):
        rel_root = os.path.relpath(root, project).replace("\\", "/")
        if rel_root == ".":
            rel_root = ""
        dirs[:] = [d for d in dirs
                   if not _skip_dir((rel_root + "/" + d).lstrip("/"))]
        for name in files:
            full = Path(root) / name
            try:
                rel = full.relative_to(project).as_posix()
            except ValueError:
                continue
            if not _in_scope(rel):
                continue
            try:
                found[rel] = _sha256(full)
            except OSError:
                continue
    return found


def write_baseline(project, note=""):
    """Record the current state as the baseline. Returns the manifest dict."""
    files = collect(project)
    manifest = {
        "app": "TrioForge",
        "generated": datetime.now().isoformat(timespec="seconds"),
        "note": note,
        "files": files,
    }
    path = manifest_path(project)
    path.write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    return manifest


def load_baseline(project):
    path = manifest_path(project)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def compare(project, baseline=None):
    """Hash every file in scope and compare it with the baseline."""
    baseline = baseline or load_baseline(project)
    current = collect(project)
    if not baseline:
        return {
            "baseline": False,
            "files_checked": len(current),
            "modified": [], "missing": [], "added": [], "clean": 0,
        }
    old = baseline.get("files") or {}
    modified, missing, added, clean = [], [], [], 0
    for rel, digest in sorted(current.items()):
        if rel not in old:
            added.append(rel)
        elif old[rel] != digest:
            modified.append(rel)
        else:
            clean += 1
    for rel in sorted(old):
        if rel not in current:
            missing.append(rel)
    return {
        "baseline": True,
        "baseline_generated": baseline.get("generated", ""),
        "files_checked": len(current),
        "modified": modified,
        "missing": missing,
        "added": added,
        "clean": clean,
    }


def scan_patterns(project):
    """Look for the fingerprints of injected code in the app's text files.

    Two exclusions, both deliberate:
      - the vendored libraries: third-party minified bundles trip half these rules
        harmlessly, and their integrity is covered by the hash check instead;
      - this file: it contains every pattern by definition, so it reported itself on
        the first line of its own rule table. It is hash-checked like everything else,
        so an edit to it still stands out.
    """
    project = Path(project)
    self_rel = "py/tools/integrity.py"
    hits = []
    for rel in sorted(collect(project)):
        if rel.startswith("static/vendor/") or rel == self_rel:
            continue
        if not rel.lower().endswith(TEXT_EXTS):
            continue
        full = project / rel
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for pattern, why in SUSPICIOUS_PATTERNS:
                if re.search(pattern, line):
                    hits.append({
                        "file": rel,
                        "line": lineno,
                        "why": why,
                        "text": line.strip()[:160],
                    })
                    break
    return hits


def scan_binaries(project):
    """Hash the executables this app ships, and look for markup stashed inside them."""
    project = Path(project)
    results = []
    candidates = []
    for rel in sorted(collect(project)):
        if rel.lower().endswith(EXECUTABLE_EXTS):
            candidates.append(rel)
    # The interpreter the app launches through, if it is present.
    for extra in ("Scripts/TrioForge.exe", "Scripts/pythonw.exe", "Scripts/python.exe"):
        for prefix in (".venv", ".venv-linux"):
            rel = prefix + "/" + extra
            if (project / rel).is_file() and rel not in candidates:
                candidates.append(rel)
    for rel in candidates:
        full = project / rel
        entry = {"file": rel, "size": 0, "sha256": "", "markers": []}
        try:
            entry["size"] = full.stat().st_size
            entry["sha256"] = _sha256(full)
            data = full.read_bytes()
        except OSError as exc:
            entry["error"] = str(exc)
            results.append(entry)
            continue
        for marker in BINARY_MARKERS:
            if marker in data:
                entry["markers"].append(marker.decode("ascii", "replace"))
        results.append(entry)
    return results


def check(project):
    """The whole report the UI and --verify both render."""
    hashes = compare(project)
    patterns = scan_patterns(project)
    binaries = scan_binaries(project)
    marked = [b["file"] for b in binaries if b.get("markers")]
    had_baseline = hashes["baseline"]

    # A single number for the radar. Clean is 0. Each finding adds weight, and the
    # score is capped so a large dump still reads as "look at this", not 9000%.
    score = 0
    score += 35 * len(marked)                       # markup inside a binary: serious
    score += 12 * len(hashes["modified"])
    score += 12 * len(hashes["missing"])
    score += 6 * len(hashes["added"])
    score += 4 * len(patterns)
    if not had_baseline:
        score = max(score, 0)
    risk = min(100, score)

    return {
        "ok": risk == 0,
        "risk_pct": risk,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "has_baseline": had_baseline,
        "baseline_generated": hashes.get("baseline_generated", ""),
        "files_checked": hashes["files_checked"],
        "clean_files": hashes["clean"],
        "modified": hashes["modified"],
        "missing": hashes["missing"],
        "added": hashes["added"],
        "patterns": patterns,
        "binaries": binaries,
        "binaries_with_markers": marked,
    }
