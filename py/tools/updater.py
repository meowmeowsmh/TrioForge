"""TrioForge — self-update.

Pulls the latest code whenever the user starts TrioForge (and optionally while it
runs), so a push from the maintainer's editor reaches everyone without anybody
doing anything.

Two update paths:

* **git checkout** (the normal case — `git clone`) — `git fetch` +
  `git merge --ff-only`, so it can never create a merge commit or rewrite
  history. If the working tree has edits to *tracked* files it refuses and says
  so, instead of quietly destroying somebody's changes.
* **folder without git** (somebody downloaded a ZIP) — downloads the repository
  archive and overlays ONLY code paths from an explicit allowlist.

User data is never touched by either path: conversations, SQLite databases,
models, uploads, certificates and logs are all outside the update set.

Nothing here raises: a failed update must never stop the app from starting.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

GITHUB_REPO = "meowmeowsmh/TrioForge"
DEFAULT_BRANCH = "main"
ARCHIVE_URL = "https://codeload.github.com/{repo}/zip/refs/heads/{branch}"
API_COMMIT_URL = "https://api.github.com/repos/{repo}/commits/{branch}"

DEPS_MARKER = ".deps_installed"
DEPS_FILES = ("requirements.txt", "requirements-ml.txt", "pyproject.toml", "uv.lock")

# Directories/files an archive overlay is allowed to replace. Everything else in
# the project folder is either user data or a local environment.
OVERLAY_PATHS = (
    "py",
    "templates",
    "docker",
    "plugins",
    ".github",
    "static",                 # static/uploads is excluded below — that is user data
    "run.sh",
    "TrioForge.bat",
    "voice_agent.bat",
    "requirements.txt",
    "requirements-ml.txt",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "INSTALL_PYTHON.md",
    "LICENSE",
    "SECURITY.md",
    "Disclaimer.md",
    "CODE_REVIEW.md",
    ".gitignore",
    ".gitattributes",
    ".dockerignore",
)

# Never written to, by either update path.
PROTECTED = (
    ".git",
    ".venv",
    ".venv-linux",
    ".venv-gif",
    "json_configuration",
    "sqlite_data",
    "cert_store",
    "logs",
    "models",
    "video_model",
    "universal_models_to_text",
    "tools/llama.cpp",
    "static/uploads",
    "voiceguide_llama.cpp_guide/config.json",
    "voiceguide_llama.cpp_guide/logs",
    ".env",
    ".deps_installed",
    ".deps_hash",
)

_LOG: List[str] = []


def log(message: str) -> None:
    """Print an update message (kept tiny so callers can reuse the log list)."""
    _LOG.append(message)
    print("[update] {}".format(message))


# ── process helpers ──────────────────────────────────────────────────────────
def _env() -> Dict[str, str]:
    """Environment for git: never prompt, never open an editor or a pager."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"     # fail instead of hanging on a password prompt
    env["GIT_ASKPASS"] = "echo"
    env["GIT_PAGER"] = "cat"
    env["GIT_CONFIG_NOSYSTEM"] = "0"
    return env


def run(cmd: List[str], cwd: Path, timeout: int = 60) -> Tuple[int, str]:
    """Run a command, returning (returncode, combined output). Never raises.

    Git is a console program: without CREATE_NO_WINDOW, a hidden launch (TrioForge.bat,
    pythonw) gets a fresh console window for every git call - the "why is a terminal
    popping up with git/..." complaint.
    """
    try:
        try:
            from procutil import no_window_flags
            flags = no_window_flags()
        except Exception:
            flags = 0
        done = subprocess.run(
            cmd, cwd=str(cwd), env=_env(), timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=flags,
        )
        return done.returncode, done.stdout.decode("utf-8", "replace").strip()
    except subprocess.TimeoutExpired:
        return 124, "timed out after {}s: {}".format(timeout, " ".join(cmd))
    except FileNotFoundError:
        return 127, "not found: {}".format(cmd[0])
    except Exception as exc:                       # pragma: no cover - defensive
        return 1, "{}: {}".format(type(exc).__name__, exc)


def _git(project: Path, *args: str, timeout: int = 60) -> Tuple[int, str]:
    return run(["git"] + list(args), project, timeout=timeout)


def git_available() -> bool:
    return shutil.which("git") is not None


def is_git_checkout(project: Path) -> bool:
    if not (project / ".git").exists():
        return False
    rc, out = _git(project, "rev-parse", "--is-inside-work-tree", timeout=15)
    return rc == 0 and out.strip().lower() == "true"


# ── local / remote state ─────────────────────────────────────────────────────
def local_revision(project: Path) -> Dict[str, str]:
    """Branch, commit and subject of the current checkout (empty when unknown)."""
    info = {"branch": "", "sha": "", "short": "", "subject": "", "dirty": ""}
    if not is_git_checkout(project):
        return info
    rc, out = _git(project, "rev-parse", "--abbrev-ref", "HEAD", timeout=15)
    if rc == 0:
        info["branch"] = out.strip()
    rc, out = _git(project, "rev-parse", "HEAD", timeout=15)
    if rc == 0:
        info["sha"] = out.strip()
        info["short"] = out.strip()[:7]
    rc, out = _git(project, "log", "-1", "--pretty=%s", timeout=15)
    if rc == 0:
        info["subject"] = out.strip()
    # Only TRACKED modifications block an update; untracked junk must not.
    rc, out = _git(project, "status", "--porcelain", "--untracked-files=no", timeout=20)
    if rc == 0 and out.strip():
        info["dirty"] = out.strip()
    return info


def remote_revision(project: Path, branch: str = "", timeout: int = 20) -> Dict[str, str]:
    """Latest commit on the remote branch, via git ls-remote (then the API)."""
    branch = branch or DEFAULT_BRANCH
    if git_available() and is_git_checkout(project):
        rc, out = _git(project, "ls-remote", "origin", "refs/heads/{}".format(branch),
                       timeout=timeout)
        if rc == 0 and out.strip():
            sha = out.split()[0].strip()
            return {"sha": sha, "short": sha[:7], "source": "git"}
        # fall through to the API (e.g. no `origin`, or a partial clone)
    try:
        url = API_COMMIT_URL.format(repo=GITHUB_REPO, branch=branch)
        req = urllib.request.Request(url, headers={"User-Agent": "TrioForge-updater"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        sha = data.get("sha", "")
        return {"sha": sha, "short": sha[:7], "source": "api"}
    except Exception as exc:
        return {"sha": "", "short": "", "source": "", "error": "{}: {}".format(type(exc).__name__, exc)}


def check(project: Path, branch: str = "") -> Dict[str, object]:
    """Is there something newer upstream? (Never touches the working tree.)"""
    local = local_revision(project)
    branch = branch or local.get("branch") or DEFAULT_BRANCH
    remote = remote_revision(project, branch)
    result: Dict[str, object] = {
        "checked_at": time.time(),
        "branch": branch,
        "local": local,
        "remote": remote,
        "update_available": False,
        "reason": "",
    }
    if not remote.get("sha"):
        result["reason"] = "could not reach GitHub ({})".format(remote.get("error", "offline?"))
        return result
    if not local.get("sha"):
        result["reason"] = "not a git checkout - archive updates only"
        result["update_available"] = True          # an archive can still be replaced
        return result
    result["update_available"] = remote["sha"] != local["sha"]
    if not result["update_available"]:
        result["reason"] = "already up to date"
    return result


def changelog(project: Path, since_sha: str, limit: int = 8) -> List[str]:
    """One-line subjects between the current HEAD and `since_sha`."""
    if not since_sha or not is_git_checkout(project):
        return []
    rc, out = _git(project, "log", "--oneline", "--no-decorate",
                   "{}..FETCH_HEAD".format(since_sha), timeout=20)
    if rc != 0 or not out.strip():
        return []
    return [line.strip() for line in out.splitlines()[:limit]]


# ── dependency fingerprinting ────────────────────────────────────────────────
def deps_fingerprint(project: Path) -> str:
    """Hash of the dependency manifests — changes only when deps really change."""
    sha = hashlib.sha256()
    for name in DEPS_FILES:
        path = project / name
        sha.update(name.encode())
        if path.is_file():
            sha.update(path.read_bytes())
        sha.update(b"\0")
    return sha.hexdigest()


def marker_fingerprint(project: Path) -> str:
    """The fingerprint recorded in .deps_installed ('' when missing/blank)."""
    marker = project / DEPS_MARKER
    try:
        text = marker.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return ""
    return text.split("sha256:", 1)[1].strip() if "sha256:" in text else ""


def write_deps_marker(project: Path) -> None:
    try:
        (project / DEPS_MARKER).write_text(
            "sha256:{}\n".format(deps_fingerprint(project)), encoding="utf-8")
    except Exception:
        pass


def deps_changed(project: Path) -> bool:
    """True when requirements/lock files changed since the last install.

    An EMPTY or missing marker both mean "we cannot prove the deps are installed",
    and the safe answer is to install: an empty marker is exactly what a fresh
    clone used to carry (it was committed by mistake), and treating it as
    "already installed" is how a first run ends up starting the app in an
    interpreter with no Flask - which crashes with the console closing before
    anybody can read why.
    """
    marker = project / DEPS_MARKER
    if not marker.exists():
        return True
    recorded = marker_fingerprint(project)
    if not recorded:
        return True
    return recorded != deps_fingerprint(project)


# ── applying the update ──────────────────────────────────────────────────────
def _is_protected(rel: str) -> bool:
    rel = rel.replace("\\", "/").lstrip("./")
    return any(rel == p or rel.startswith(p + "/") for p in PROTECTED)


def _apply_git(project: Path, branch: str, allow_dirty: bool = False) -> Dict[str, object]:
    state = local_revision(project)
    dirty = bool(state.get("dirty"))
    if dirty and not allow_dirty:
        return {"ok": False, "updated": False,
                "message": "local edits to tracked files - skipping the automatic update "
                           "(commit/stash them, or run with --force-update)"}

    before = state.get("sha", "")
    # --force-update: park the edits in a stash so git will fast-forward, then put
    # them back. The edits are never deleted: if they cannot be re-applied they
    # stay in `git stash` and we say exactly how to get them back.
    stashed = False
    if dirty:
        rc, out = _git(project, "stash", "push", "--quiet", "--message",
                       "trioforge auto-update", timeout=60)
        if rc != 0:
            return {"ok": False, "updated": False,
                    "message": "could not stash the local edits: {}".format(out)}
        stashed = True
        log("parked your local edits in a git stash while updating")

    rc, out = _git(project, "fetch", "--quiet", "--prune", "origin", branch, timeout=180)
    if rc != 0:
        if stashed:
            _git(project, "stash", "pop", "--quiet", timeout=60)
        return {"ok": False, "updated": False, "message": "git fetch failed: {}".format(out)}
    rc, out = _git(project, "rev-parse", "FETCH_HEAD", timeout=20)
    target = out.strip() if rc == 0 else ""
    if target and before and target == before:
        if stashed:
            _git(project, "stash", "pop", "--quiet", timeout=60)
        return {"ok": True, "updated": False, "message": "already up to date",
                "from": before[:7], "to": before[:7]}

    changes = changelog(project, before)
    rc, out = _git(project, "merge", "--ff-only", "--quiet", "FETCH_HEAD", timeout=120)
    if rc != 0:
        if stashed:
            _git(project, "stash", "pop", "--quiet", timeout=60)
        return {"ok": False, "updated": False, "from": before[:7], "to": target[:7],
                "message": "fast-forward failed (local commits or diverged history): {}".format(out)}

    after = local_revision(project)
    result: Dict[str, object] = {
        "ok": True, "updated": bool(after.get("sha") != before),
        "from": before[:7], "to": after.get("short", ""), "changes": changes,
        "message": "updated {} -> {}".format(before[:7] or "?", after.get("short", "?")),
    }
    if stashed:
        rc, out = _git(project, "stash", "pop", "--quiet", timeout=60)
        if rc == 0:
            result["message"] = str(result["message"]) + " (your local edits were re-applied)"
        else:
            result["message"] = ("{} - your local edits could not be re-applied automatically "
                                 "and are safe in `git stash` (list: git stash list, "
                                 "restore: git stash pop)".format(result["message"]))
    return result


def _apply_archive(project: Path, branch: str) -> Dict[str, object]:
    """Overlay the GitHub archive — for a folder that is not a git checkout."""
    url = ARCHIVE_URL.format(repo=GITHUB_REPO, branch=branch)
    log("no git checkout here; downloading the release archive instead")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "TrioForge-updater"})
        with urllib.request.urlopen(req, timeout=90) as resp:
            blob = resp.read()
    except Exception as exc:
        return {"ok": False, "updated": False,
                "message": "could not download the archive: {}: {}".format(type(exc).__name__, exc)}

    tmp = Path(tempfile.mkdtemp(prefix="trioforge-update-"))
    copied = 0
    try:
        archive = tmp / "src.zip"
        archive.write_bytes(blob)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(tmp / "src")
        roots = [p for p in (tmp / "src").iterdir() if p.is_dir()]
        if not roots:
            return {"ok": False, "updated": False, "message": "archive had no top-level folder"}
        src = roots[0]

        for rel in OVERLAY_PATHS:
            source = src / rel
            if not source.exists() or _is_protected(rel):
                continue
            target = project / rel
            if source.is_dir():
                for item in source.rglob("*"):
                    inner = item.relative_to(src).as_posix()
                    if _is_protected(inner) or item.is_dir():
                        continue
                    dest = project / inner
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, dest)
                    copied += 1
            else:
                shutil.copy2(source, target)
                copied += 1
    except Exception as exc:
        return {"ok": False, "updated": False,
                "message": "archive update failed: {}: {}".format(type(exc).__name__, exc)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return {"ok": True, "updated": copied > 0, "to": "archive",
            "message": "replaced {} file(s) from the latest archive".format(copied)}


def apply_update(project: Path, branch: str = "", allow_dirty: bool = False) -> Dict[str, object]:
    """Check and apply. Returns a dict; never raises, never blocks startup."""
    branch = branch or local_revision(project).get("branch") or DEFAULT_BRANCH
    try:
        if is_git_checkout(project):
            return _apply_git(project, branch, allow_dirty=allow_dirty)
        return _apply_archive(project, branch)
    except Exception as exc:                       # pragma: no cover - defensive
        return {"ok": False, "updated": False,
                "message": "update failed: {}: {}".format(type(exc).__name__, exc)}


def status(project: Path) -> Dict[str, object]:
    """Everything the launcher prints for `--status`."""
    local = local_revision(project)
    return {
        "project": str(project),
        "python": sys.version.split()[0],
        "git_checkout": is_git_checkout(project),
        "branch": local.get("branch", ""),
        "commit": local.get("short", ""),
        "commit_subject": local.get("subject", ""),
        "local_changes": bool(local.get("dirty")),
        "deps_installed": (project / DEPS_MARKER).exists(),
        "deps_changed": deps_changed(project),
    }
