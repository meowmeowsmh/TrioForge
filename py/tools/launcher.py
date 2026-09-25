#!/usr/bin/env python3
"""
TrioForge — cross-platform launcher.

One entry point that runs on Windows, Linux, macOS, and WSL.
`TrioForge.bat` and `run.sh` are thin wrappers that call this script
and open an interactive menu so the user can pick their environment.

Usage:
    python launcher.py [path] [--menu] [--install] [--no-install]

Options:
    path         optional path to the TrioForge project folder
    --menu       show the interactive environment menu
    --install    force (re)install dependencies
    --no-install skip dependency installation
    --unix       (internal) run natively; used when launched from WSL/bash
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import platform
import threading
import time
from pathlib import Path
from typing import Iterator, List, Optional

try:                                    # same folder; both are stdlib-only
    import updater
    import autostart
except ImportError:                     # pragma: no cover - launched oddly
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import updater
    import autostart

# Core files that must exist together for a folder to be a TrioForge project.
REQUIRED_FILES = (
    "py/app.py",
    "py/common.py",
    "py/providers/llm_providers.py",
    "py/features/notes.py",
    "py/features/cork_board.py",
    "py/features/viewer.py",
)
# The dependency marker (.deps_installed, plus a hash of the manifests) lives in
# py/tools/updater.py — it is what decides whether deps need reinstalling. No
# second copy of the name here on purpose.

BANNER = r""" _____     _       _____                    
|_   _| __(_) ___ |  ___|__  _ __ __ _  ___ 
  | || '__| |/ _ \| |_ / _ \| '__/ _` |/ _ \
  | || |  | | (_) |  _| (_) | | | (_| |  __/
  |_||_|  |_|\___/|_|  \___/|_|  \__, |\___|
                                 |___/      """


def is_project(path: Optional[Path]) -> bool:
    """Return True if `path` contains all the core TrioForge files."""
    return bool(path) and all((path / name).is_file() for name in REQUIRED_FILES)


def _search_roots() -> List[Path]:
    """Candidate folders to search when the project isn't in the obvious spots."""
    roots: List[Path] = [Path.home()]
    if os.name == "nt":
        drive = Path(__file__).resolve().drive
        if drive:
            roots.append(Path(drive + os.sep))
    else:
        roots += [Path("/home"), Path("/opt"), Path("/srv"), Path("/mnt"), Path("/media")]
    return roots


def _bounded_rglob(root: Path, max_depth: int = 5) -> Iterator[Path]:
    """Yield `app.py` files up to `max_depth` levels deep, skipping unreadable dirs."""
    stack = [(root, 0)]
    while stack:
        path, depth = stack.pop()
        if depth > max_depth or not path.is_dir():
            continue
        try:
            entries = sorted(path.iterdir(), key=lambda p: p.name.lower())
        except (PermissionError, OSError):
            continue
        for entry in entries:
            if entry.is_dir():
                stack.append((entry, depth + 1))
            elif entry.name == "app.py":
                yield entry


def find_project(explicit: Optional[str]) -> Optional[Path]:
    """Locate the project: explicit path, current folder, TRIOFORGE_HOME, then a search."""
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if is_project(path):
            return path
        print("Specified path is not a TrioForge project: {}".format(explicit))
        return None

    candidates = [Path.cwd()]
    home = os.environ.get("TRIOFORGE_HOME")
    if home:
        candidates.append(Path(home))
    for candidate in candidates:
        if is_project(candidate):
            return candidate

    print("Project not found in the current folder; searching common locations...")
    for root in _search_roots():
        for app in _bounded_rglob(root):
            # app.py lives at <project>/py/app.py (or <project>/app.py for older layouts).
            for cand in (app.parent.parent, app.parent):
                if is_project(cand):
                    return cand
    return None


def project_venv_python(project: Path) -> Optional[str]:
    """The project's own venv interpreter, if one exists."""
    for rel in ("Scripts/python.exe", "bin/python3", "bin/python"):
        candidate = project / ".venv" / rel
        if candidate.is_file():
            return str(candidate)
    return None


def ensure_project_venv(project: Path) -> Optional[str]:
    """Create <project>/.venv if needed and return its interpreter path.

    Everything is installed into THIS venv and the app is started from it, so the
    install target and the run target can never disagree (installing into one
    interpreter and running with another is how a first run ends up crashing with
    ModuleNotFoundError right after a successful-looking install).
    """
    existing = project_venv_python(project)
    if existing:
        return existing
    uv = shutil.which("uv")
    print("Creating the project virtual environment (.venv)...")
    if uv:
        result = subprocess.run([uv, "venv", ".venv"], cwd=str(project),
                                creationflags=_no_window_flags())
    else:
        result = subprocess.run([sys.executable, "-m", "venv", ".venv"], cwd=str(project),
                                creationflags=_no_window_flags())
    if result.returncode != 0:
        print("Could not create the virtual environment.")
        return None
    return project_venv_python(project)


def install_deps(project: Path) -> None:
    """Install dependencies into the project venv, then write the marker file.

    Deliberately requirements.txt-driven rather than `uv sync`: uv.lock freezes
    exact versions, and an old lock can pin something with no wheel for this
    Python (e.g. pyyaml 5.1) so the install tries to compile it and fails without
    a C++ toolchain. requirements.txt is the file the docs point at and it always
    resolves to installable versions.
    """
    req = project / "requirements.txt"
    if not req.is_file():
        print("No requirements.txt found; skipping dependency install.")
        return

    venv_python = ensure_project_venv(project)
    if venv_python is None:
        print("Dependency installation skipped (no usable virtual environment).")
        return

    uv = shutil.which("uv")
    print("Installing dependencies into .venv (this may take a while on first run)...")
    if uv:
        result = subprocess.run([uv, "pip", "install", "--python", venv_python,
                                 "-r", str(req)], cwd=str(project),
                                creationflags=_no_window_flags())
    else:
        subprocess.run([venv_python, "-m", "pip", "install", "--upgrade", "pip"],
                       cwd=str(project), creationflags=_no_window_flags())
        result = subprocess.run([venv_python, "-m", "pip", "install", "-r", str(req)],
                                cwd=str(project), creationflags=_no_window_flags())
    if result.returncode != 0:
        print("Dependency installation failed.")
        print("Install manually with: {} -m pip install -r requirements.txt".format(venv_python))
        sys.exit(1)
    updater.write_deps_marker(project)
    print("Dependencies installed.")


def _voice_config_path(project: Path) -> Optional[Path]:
    """Return the voice agent config.json path if present, else None."""
    cfg = project / "voiceguide_llama.cpp_guide" / "config.json"
    return cfg if cfg.is_file() else None


def start_voice_agent(project: Path, enabled: bool = True) -> None:
    """Start the voice-to-voice agent (llama.cpp + speech-to-speech) in the background.

    This is best-effort: if the config or llama-server executable are missing, it
    prints a note and the app still starts normally (app-only).
    """
    if not enabled:
        return
    cfg_path = _voice_config_path(project)
    if cfg_path is None:
        print("Voice agent: no config.json found; starting app only.")
        return
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        print("Voice agent: could not read config.json; starting app only.")
        return
    llama = str(cfg.get("llama_server", ""))
    llama_path = Path(llama)
    has_llama = (llama_path.is_absolute() and llama_path.is_file()) or bool(shutil.which(llama))
    if not has_llama:
        print("Voice agent: llama-server not found; starting app only.")
        return
    agent = project / "py" / "tools" / "voice_agent.py"
    print("Voice agent: starting llama.cpp + speech-to-speech in the background ...")
    try:
        subprocess.Popen(
            [sys.executable, str(agent), "--config", str(cfg_path)],
            cwd=str(project),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except Exception as e:
        print("Voice agent: failed to start ({})".format(e))


def host_setup(project: Path, explicit: str = "") -> str:
    """Turn on host mode: pick a password, tell the user what to share.

    Everything the app needs is the TRIOFORGE_PASSWORD environment variable; the
    password is kept in json_configuration/host_password (git-ignored, like all the
    other local data) so the same link keeps working after a restart.
    """
    password = (explicit or os.environ.get("TRIOFORGE_PASSWORD") or "").strip()
    store = project / "json_configuration" / "host_password"
    if not password:
        try:
            if store.is_file():
                password = store.read_text(encoding="utf-8").strip()
        except Exception:
            password = ""
    if not password:
        # Readable but not guessable: 4 groups of 4 hex characters.
        import secrets as _secrets
        password = "-".join(_secrets.token_hex(2) for _ in range(4))
        try:
            store.parent.mkdir(parents=True, exist_ok=True)
            store.write_text(password + "\n", encoding="utf-8")
        except Exception:
            pass

    os.environ["TRIOFORGE_PASSWORD"] = password

    print()
    print("=" * 62)
    print("  HOST MODE ON - other people can use this TrioForge")
    print("=" * 62)
    print("  Password : {}".format(password))
    print("  (kept in json_configuration/host_password - delete it to reset)")
    print()
    print("  Share it depending on how you are hosting:")
    print("    same Wi-Fi / LAN : this machine's IP, e.g.")
    for url in _lan_urls():
        print("                       {}".format(url))
    print("    internet (tunnel): cloudflared tunnel --url http://localhost:{}".format(
        os.environ.get("TRIOFORGE_PORT", "5003")))
    print("    Docker / server  : docker run -p 5002:5001 ... ghcr.io/meowmeowsmh/trioforge:latest")
    print("    one person only  : point them at the repo (git clone + TrioForge.bat) - no password needed")
    print()
    print("  Everyone you share it with sees this workspace, including its notes,")
    print("  pins and conversations on this machine. Use --host-password to choose")
    print("  your own, or --no-host to go back to local-only.")
    print("=" * 62)
    print()
    return password


def _lan_urls() -> List[str]:
    """Best-effort list of the URLs other devices on the network can use."""
    port = os.environ.get("TRIOFORGE_PORT", "5003")
    scheme = "https" if os.environ.get("TRIOFORGE_SSL", "").strip() in ("1", "true", "on") else "http"
    urls: List[str] = []
    try:
        import socket
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127.") or ip in urls:
                continue
            urls.append("{}://{}:{}".format(scheme, ip, port))
    except Exception:
        pass
    if not urls:
        urls.append("{}://<this-machine-ip>:{}".format(scheme, port))
    return urls[:3]


def _app_command(project: Path) -> List[str]:
    """The command that runs the Flask app.

    The project venv wins: that is where install_deps() just put the dependencies.
    Falling back to `uv run` before checking it would start the app in an
    interpreter that may not have them.

    Hidden launches use the venv's pythonw: a console interpreter cannot be kept
    quiet, because .venv/Scripts/python.exe is a shim that starts the real
    interpreter as a child, and CREATE_NO_WINDOW only silences the shim - the child
    allocates a console window of its own. When we DO have a console (someone ran
    TrioForge.bat in a terminal) the console interpreter is used, so the app's
    output stays in the terminal they are watching.
    """
    app_path = project / "py" / "app.py"
    hidden = _no_window_flags() != 0
    if hidden:
        return [venv_pythonw(project), str(app_path)]
    venv_python = project_venv_python(project)
    if venv_python:
        return [venv_python, str(app_path)]
    in_venv = hasattr(sys, "base_prefix") and sys.prefix != sys.base_prefix
    uv = shutil.which("uv")
    if in_venv or not uv:
        return [sys.executable, str(app_path)]
    return [uv, "run", "python", str(app_path)]


def pid_alive(pid: int) -> bool:
    """Is that process still running? (Never signals it: OpenProcess only.)"""
    if not pid or pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return bool(ok) and code.value == STILL_ACTIVE
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def panel_pid_file(project: Path) -> Path:
    """Where the control panel records its own pid (one panel per folder)."""
    return project / "logs" / "control-panel.pid"


def _no_window_flags() -> int:
    """CREATE_NO_WINDOW when we have no console of our own.

    The control panel runs under pythonw (no console). Spawning the app from there
    without this flag makes Windows create a NEW console window for every child -
    which is the cmd window that keeps appearing and never closes when the app is
    started from the desktop app.
    """
    if os.name != "nt":
        return 0
    try:
        if sys.stdout is None or not sys.stdout.isatty():
            return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    except Exception:
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


class AppSupervisor:
    """Runs the app as a child process so an update can restart it.

    `os.execv` (the old behaviour) replaced this process with the app, which made
    "update while running" impossible. Watching is opt-in via --watch-updates, and
    the app is only restarted when an update was actually applied - never on a
    normal exit or Ctrl+C.
    """

    def __init__(self, project: Path, watch_seconds: int = 0,
                 auto_restart: bool = True, no_browser: bool = False,
                 on_output=None):
        self.project = project
        self.watch_seconds = watch_seconds
        self.auto_restart = auto_restart
        self.no_browser = no_browser
        self.on_output = on_output          # GUI hook: called with each output line
        self.child: Optional[subprocess.Popen] = None
        self.pending = threading.Event()
        self.message = ""
        self.url = ""                       # filled in from the app's own output

    def _environment(self) -> dict:
        env = dict(os.environ)
        if self.no_browser:
            env["TRIOFORGE_NO_BROWSER"] = "1"
        return env

    def _watch(self) -> None:
        """Poll GitHub every N seconds; apply + request a restart when newer."""
        while True:
            time.sleep(self.watch_seconds)
            try:
                state = updater.check(self.project)
                if not state.get("update_available"):
                    continue
                print()
                print("[update] newer version found ({} -> {}); pulling it now...".format(
                    state["local"].get("short") or "?", state["remote"].get("short") or "?"))
                result = updater.apply_update(self.project)
                print("[update] {}".format(result.get("message", "")))
                if result.get("updated"):
                    if updater.deps_changed(self.project):
                        print("[update] dependencies changed; installing them before restart...")
                        install_deps(self.project)
                    self.message = result.get("message", "")
                    self.pending.set()
                    if self.child and self.child.poll() is None:
                        self._stop_child()
                    return
            except Exception as exc:                 # pragma: no cover - defensive
                print("[update] watcher error: {}".format(exc))

    def _stop_child(self) -> None:
        child = self.child
        if not child or child.poll() is not None:
            return
        try:
            child.terminate()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
        except Exception:
            pass

    def _emit(self, line: str) -> None:
        """Forward one line of app output (to the GUI, or to this console)."""
        line = line.rstrip("\r\n")
        if not line.strip():
            return
        # The app prints the URL it actually settled on (it may move ports).
        marker = "Open your browser at:"
        if marker in line:
            self.url = line.split(marker, 1)[1].strip()
        if self.on_output:
            try:
                self.on_output(line)
                return
            except Exception:
                pass
        print(line, flush=True)

    def run(self) -> int:
        app_path = self.project / "py" / "app.py"
        print("Project folder: {}".format(self.project))
        print("Starting TrioForge... it will open your browser automatically.")
        if self.watch_seconds:
            print("Watching for updates every {}s (disable with --watch-updates 0).".format(
                self.watch_seconds))
        print()
        os.chdir(str(self.project))
        cmd = _app_command(self.project)

        if self.watch_seconds:
            threading.Thread(target=self._watch, daemon=True).start()

        while True:
            if self.on_output:
                self.child = subprocess.Popen(cmd, cwd=str(self.project), env=self._environment(),
                                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                              stdin=subprocess.DEVNULL, text=True,
                                              encoding="utf-8", errors="replace", bufsize=1,
                                              creationflags=_no_window_flags())
                threading.Thread(target=self._pump, args=(self.child,), daemon=True).start()
            else:
                self.child = subprocess.Popen(cmd, cwd=str(self.project), env=self._environment(),
                                              creationflags=_no_window_flags())
            try:
                code = self.child.wait()
            except KeyboardInterrupt:
                self._stop_child()
                return 0
            if self.pending.is_set() and self.auto_restart:
                self.pending.clear()
                self.no_browser = True          # don't pop a new tab on every restart
                print("[update] restarting TrioForge on the new version "
                      "(refresh your browser tab)...")
                continue
            return code

    def _pump(self, child: subprocess.Popen) -> None:
        """Read the child's output for the GUI (never let this kill the thread)."""
        try:
            if not child.stdout:
                return
            for line in child.stdout:
                self._emit(line)
        except Exception:
            pass


def run_app(project: Path, start_voice: bool = True, watch_seconds: int = 0,
            auto_restart: bool = True, no_browser: bool = False) -> int:
    """Start the Flask app (supervised, so updates can restart it)."""
    if start_voice:
        print("Voice agent is NOT auto-started (it runs on its own port 8082).")
        print("Run py\\tools\\voice_agent.py separately for voice-to-voice.")
    port = os.environ.get("TRIOFORGE_PORT", "5003")
    _ssl = os.environ.get("TRIOFORGE_SSL", "").strip().lower()
    if _ssl in ("1", "true", "on"):
        _scheme = "https"
    elif _ssl in ("0", "false", "off"):
        _scheme = "http"
    else:
        _scheme = "https" if platform.system() == "Windows" else "http"
    print("  requested: {}://localhost:{}  (if that port is busy, the app picks the "
          "next free one and prints the real URL below)".format(_scheme, port))
    return AppSupervisor(project, watch_seconds=watch_seconds, auto_restart=auto_restart,
                         no_browser=no_browser).run()


def do_update(project: Path, force: bool = False) -> int:
    """`--update`: check and apply now, without starting the app."""
    print("Checking {} ({})...".format(updater.GITHUB_REPO,
                                       updater.local_revision(project).get("short") or "no git"))
    state = updater.check(project)
    if not state.get("update_available"):
        print("Already up to date ({}). {}".format(
            state["local"].get("short") or "?", state.get("reason", "")))
        return 0
    print("Update available: {} -> {}".format(
        state["local"].get("short") or "?", state["remote"].get("short") or "?"))
    result = updater.apply_update(project, allow_dirty=force)
    print(result.get("message", ""))
    for line in result.get("changes", []) or []:
        print("   " + line)
    if result.get("updated") and updater.deps_changed(project):
        print("Dependencies changed; installing them...")
        install_deps(project)
    return 0 if result.get("ok") else 1


def show_integrity(project: Path, write_baseline: bool = False) -> int:
    """`--verify` / `--verify-baseline`: check the app's own files for tampering.

    Exits 0 when nothing is off, 1 when something is - so it can be used in a script
    or a cron/Startup job and still mean something.
    """
    try:
        import integrity as mod
    except Exception as exc:
        print("Integrity module unavailable: {}".format(exc))
        return 1

    if write_baseline:
        manifest = mod.write_baseline(project, note="baseline set from --verify-baseline")
        print("Baseline written: {} file(s) recorded in {}.".format(
            len(manifest.get("files") or {}), mod.MANIFEST_NAME))
        print("Everything as it is right now is now considered trusted.")
        return 0

    report = mod.check(project)
    print("TrioForge integrity check")
    print("  files checked    : {}".format(report["files_checked"]))
    print("  baseline         : {}".format(
        "yes, from {}".format(report["baseline_generated"]) if report["has_baseline"]
        else "NONE yet - run --verify-baseline to record one"))
    print("  modified         : {}".format(len(report["modified"])))
    for rel in report["modified"][:20]:
        print("      ~ {}".format(rel))
    print("  missing          : {}".format(len(report["missing"])))
    for rel in report["missing"][:20]:
        print("      - {}".format(rel))
    print("  added            : {}".format(len(report["added"])))
    for rel in report["added"][:20]:
        print("      + {}".format(rel))
    print("  suspicious code  : {}".format(len(report["patterns"])))
    for hit in report["patterns"][:20]:
        print("      {}:{}  {}  |  {}".format(hit["file"], hit["line"], hit["why"],
                                              hit["text"][:70]))
    print("  executables      : {} checked".format(len(report["binaries"])))
    for name in report["binaries_with_markers"]:
        print("      ! markup found inside {}".format(name))
    print()
    print("  RISK             : {}%".format(report["risk_pct"]))
    if report["risk_pct"] == 0 and report["has_baseline"]:
        print("  Nothing changed and nothing suspicious was found.")
    elif not report["has_baseline"]:
        print("  No baseline yet, so 'modified' cannot be judged. Record one with")
        print("  --verify-baseline once you are happy with the current files.")
    else:
        print("  Look at the lines above. If you made those changes yourself, record")
        print("  them with --verify-baseline; if you did not, treat it seriously.")
    print()
    print("  This is a check, not an antivirus: it sees the files in this folder,")
    print("  not a running process or anything outside it.")
    return 0 if report["risk_pct"] == 0 else 1


def show_status(project: Path) -> int:
    """`--status`: everything support needs to know, in one screen."""
    info = updater.status(project)
    print("TrioForge status")
    try:
        sys.path.insert(0, str(project / "py"))
        from version import __version__ as _trio_version
        print("  version        : {}".format(_trio_version))
    except Exception:
        pass
    print("  project        : {}".format(info["project"]))
    print("  python         : {} ({})".format(info["python"], sys.executable))
    print("  git checkout   : {}".format("yes" if info["git_checkout"] else "no (archive updates)"))
    print("  branch/commit  : {} {}".format(info["branch"] or "-", info["commit"] or "-"))
    print("  last commit    : {}".format(info["commit_subject"] or "-"))
    print("  local changes  : {}".format("YES (auto-update paused)" if info["local_changes"] else "none"))
    print("  deps installed : {}{}".format(
        "yes" if info["deps_installed"] else "no",
        " (manifests changed - will reinstall)" if info["deps_changed"] else ""))
    enabled, detail = autostart.state()
    print("  start at login : {}".format(detail))
    try:
        from shortcuts import state as shortcut_state
        for line in shortcut_state(project):
            print("  shortcut       : {}".format(line))
    except Exception:
        pass
    state = updater.check(project)
    print("  upstream       : {}".format(
        "update available ({} -> {})".format(state["local"].get("short") or "?",
                                             state["remote"].get("short") or "?")
        if state.get("update_available") else "up to date" if state.get("remote", {}).get("sha")
        else state.get("reason", "unknown")))
    return 0


def prepare_and_run(project: Path, args) -> int:
    """Optionally update, install deps if needed, then start the app."""
    autostart_mode = bool(getattr(args, "autostart", False))

    if getattr(args, "host", False) or getattr(args, "host_password", ""):
        host_setup(project, explicit=getattr(args, "host_password", ""))

    if not args.no_update and not getattr(args, "background_update", False):
        _startup_update(project, force=getattr(args, "force_update", False))

    if args.install or updater.deps_changed(project):
        if not args.no_install:
            install_deps(project)

    # --window: open TrioForge in its own WebView2 window (no browser, no console).
    # Spawned before the app so it can wait for the server to come up on its own.
    if getattr(args, "window", False):
        _open_app_window(project)

    # Each entry point makes sure ITS OWN shortcut exists, in every location it
    # belongs, and only creates what is actually missing:
    #   (no --window) -> "TrioForge"          (browser)
    #   --window      -> "TrioForge (window)" (WebView2 window)
    # Both point at the same TrioForge.bat and differ only by argument, so the
    # flavour is taken from --window rather than from which file was launched.
    # So launching one never overwrites the other, and a deleted icon comes back the
    # next time that entry point is used. Skippable with TRIOFORGE_NO_SHORTCUT=1.
    if os.environ.get("TRIOFORGE_NO_SHORTCUT", "").strip() not in ("1", "true", "on"):
        # Written to logs/ so a failure is diagnosable even under pythonw + --detach,
        # where print() has nowhere to go. "My friend's laptop didn't make a shortcut"
        # was un-debuggable before because the error vanished with the console.
        try:
            (project / "logs").mkdir(exist_ok=True)
            _sc_log = open(str(project / "logs" / "shortcuts.log"), "a", encoding="utf-8")
        except Exception:
            _sc_log = None

        def _sc_report(msg):
            print(msg)
            if _sc_log is not None:
                try:
                    _sc_log.write(msg + "\n")
                    _sc_log.flush()
                except Exception:
                    pass

        try:
            from shortcuts import (install as shortcut_install, missing as shortcut_missing,
                                   FLAVORS)
            flavor = "window" if getattr(args, "window", False) else "web"
            gone = shortcut_missing(project, flavor)
            if gone:
                _sc_report("Adding the {} shortcut ({}) in {} - so you can double-click it "
                           "next time:".format(flavor, FLAVORS[flavor]["script"],
                                               ", ".join(gone)))
                for label in gone:
                    for line in shortcut_install(
                            project, where=label.lower().replace(" ", ""), flavor=flavor):
                        _sc_report("  " + line)
        except Exception as exc:
            _sc_report("[shortcut] not created: {}".format(exc))
        finally:
            if _sc_log is not None:
                try:
                    _sc_log.close()
                except Exception:
                    pass

    # --detach: start the server in the background with no console and return. The
    # window (above) waits for it; the .bat that calls this returns immediately.
    if getattr(args, "detach", False):
        cmd = [venv_pythonw(project), str(project / "py" / "app.py")]
        flags = 0
        if os.name == "nt":
            flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        # The app opens a browser tab on start-up unless TRIOFORGE_NO_BROWSER is set
        # in its environment. --window means "TrioForge opens in its own window", so
        # the browser must NOT also appear; --no-browser says the same explicitly.
        child_env = dict(os.environ)
        if getattr(args, "no_browser", False) or getattr(args, "window", False):
            child_env["TRIOFORGE_NO_BROWSER"] = "1"
        # Keep the server's own output instead of discarding it: a detached launch
        # has no console, so DEVNULL meant real tracebacks and warnings were lost.
        out = subprocess.DEVNULL
        try:
            log_path = project / "logs" / "server-console.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            out = open(str(log_path), "a", encoding="utf-8", errors="replace")
            out.write("\n===== server started {} =====\n".format(
                time.strftime("%Y-%m-%d %H:%M:%S")))
            out.flush()
        except Exception:
            out = subprocess.DEVNULL
        subprocess.Popen([str(c) for c in cmd], cwd=str(project), creationflags=flags,
                         env=child_env,
                         stdout=out, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL)
        print("[detach] server starting in the background on port {}{}.".format(
            os.environ.get("TRIOFORGE_PORT", "5003"),
            " with no browser tab" if child_env.get("TRIOFORGE_NO_BROWSER") else ""))
        if getattr(args, "background_update", False):
            _spawn_background_update(project)
        return 0

    watch = getattr(args, "watch_updates", -1)
    if watch is None or watch < 0:
        # Unset: a background/autostart instance keeps itself current, an
        # interactive launch just updates at start-up.
        watch = 1800 if autostart_mode else 0
    return run_app(
        project,
        start_voice=not getattr(args, "no_voice", False),
        watch_seconds=int(watch or 0),
        auto_restart=not getattr(args, "no_auto_restart", False),
        no_browser=bool(getattr(args, "no_browser", False) or autostart_mode),
    )


def _base_python_dir(project: Path) -> Optional[Path]:
    """The directory of the real CPython that backs the venv (pythonw.exe lives here).

    pyvenv.cfg's ``home`` line is the authoritative answer for both uv and stdlib
    venvs. The base interpreter's own directory is the fallback.
    """
    cfg = project / ".venv" / "pyvenv.cfg"
    home = None
    try:
        if cfg.is_file():
            for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("home"):
                    home = line.split("=", 1)[1].strip()
                    break
    except Exception:
        home = None
    for d in (home, None):
        if d:
            p = Path(d)
            if (p / "pythonw.exe").is_file():
                return p
    base = getattr(sys, "_base_executable", "") or sys.executable
    d = Path(base).parent
    return d if (d / "pythonw.exe").is_file() else None


def _safe_copy(src: Path, dst: Path) -> None:
    """Copy a file atomically (temp name + replace), so a racing first launch
    cannot leave a half-written executable or DLL behind."""
    tmp = dst.with_name(dst.name + ".tmp.{}".format(os.getpid()))
    shutil.copy2(str(src), str(tmp))
    os.replace(str(tmp), str(dst))


def _needs_refresh(src: Path, dst: Path) -> bool:
    """True when dst is missing or older than src (e.g. after a Python upgrade)."""
    if not dst.is_file():
        return True
    try:
        return src.stat().st_mtime_ns > dst.stat().st_mtime_ns
    except Exception:
        return True


def _window_hang_marker():
    """The file app_window.py checks to decide whether the GPU may be used."""
    return _app_window_pid_file().parent / "window_hung.txt"


def note_window_hang(reason: str) -> None:
    """Record that the window hung, so the NEXT one uses software rendering.

    The in-process hang watchdog writes this too, but it cannot run when the process
    is genuinely frozen - which is exactly the case that matters. Detecting it here,
    from a healthy process at launch, is what breaks the loop of "open the app, it
    freezes, open it again, it freezes again".
    """
    try:
        marker = _window_hang_marker()
        count = 0
        if marker.is_file():
            try:
                count = int(marker.read_text(encoding="utf-8").strip().split()[0])
            except Exception:
                count = 0
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{} {}\n".format(count + 1, reason), encoding="utf-8")
        print("[window] noted the hang - the next window will use software rendering.")
    except Exception:
        pass


def _app_version_tuple(project: Path):
    """TrioForge's version as a 4-tuple, read from py/version.py (0.0.0.0 if unknown)."""
    import re
    try:
        text = (project / "py" / "version.py").read_text(encoding="utf-8", errors="replace")
        m = re.search(r"__version__\s*=\s*[\"']([0-9]+)\.([0-9]+)\.([0-9]+)", text)
        if m:
            return (int(m.group(1)), int(m.group(2)), int(m.group(3)), 0)
    except Exception:
        pass
    return (0, 0, 0, 0)


def _build_version_resource(description: str, product: str, version=(0, 0, 0, 0)) -> bytes:
    """Build a VS_VERSIONINFO resource block (the 'Details' tab of a .exe).

    Task Manager's Name column shows the version resource's FileDescription - NOT
    the file name - so a renamed copy of pythonw.exe still displays as "Python"
    until this resource says otherwise.
    """
    import struct

    def align4(buf):
        return buf + b"\x00" * ((4 - len(buf) % 4) % 4)

    def wstr(s):
        return s.encode("utf-16-le") + b"\x00\x00"

    def string_entry(key, value):
        body = struct.pack("<HHH", 0, len(value) + 1, 1) + wstr(key)
        body = align4(body) + wstr(value)
        body = align4(body)
        return struct.pack("<H", len(body)) + body[2:]

    def block(key, wtype, value_b, wvalue, children=b""):
        body = struct.pack("<HHH", 0, wvalue, wtype) + wstr(key)
        body = align4(body) + value_b
        body = align4(body) + children
        body = align4(body)
        return struct.pack("<H", len(body)) + body[2:]

    vtext = "{}.{}.{}.{}".format(*version)
    ms = (version[0] << 16) | version[1]
    ls = (version[2] << 16) | version[3]
    fixed = struct.pack(
        "<13I",
        0xFEEF04BD, 0x00010000,          # signature, struct version
        ms, ls, ms, ls,                  # file + product version
        0x3F, 0x00,                      # flags mask, flags
        0x00040004,                      # VOS_NT_WINDOWS32
        0x00000001,                      # VFT_APP
        0x00000000, 0x00000000, 0x00000000)

    strings = b"".join([
        string_entry("CompanyName", product),
        string_entry("FileDescription", description),
        string_entry("FileVersion", vtext),
        string_entry("InternalName", product),
        string_entry("OriginalFilename", product + ".exe"),
        string_entry("ProductName", product),
        string_entry("ProductVersion", vtext),
    ])
    string_file_info = block("StringFileInfo", 1, b"", 0,
                             block("040904B0", 1, b"", 0, strings))
    var_file_info = block("VarFileInfo", 1, b"", 0,
                          block("Translation", 0, struct.pack("<I", 0x040904B0), 4, b""))
    return block("VS_VERSION_INFO", 0, fixed, len(fixed), string_file_info + var_file_info)


def set_exe_description(path: Path, description: str, product: str = "",
                        version=(0, 0, 0, 0)) -> bool:
    """Rewrite an .exe's version resource so Task Manager shows `description`."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        data = _build_version_resource(description, product or description, version)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
        k32.BeginUpdateResourceW.restype = wintypes.HANDLE
        k32.UpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                        wintypes.WORD, ctypes.c_void_p, wintypes.DWORD]
        k32.UpdateResourceW.restype = wintypes.BOOL
        k32.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]
        k32.EndUpdateResourceW.restype = wintypes.BOOL

        # MAKEINTRESOURCE(16) = RT_VERSION, MAKEINTRESOURCE(1) = the version resource.
        rt_version = ctypes.cast(ctypes.c_void_p(16), wintypes.LPCWSTR)
        res_id = ctypes.cast(ctypes.c_void_p(1), wintypes.LPCWSTR)
        buf = ctypes.create_string_buffer(data, len(data))

        handle = k32.BeginUpdateResourceW(str(path), False)
        if not handle:
            return False
        ok = bool(k32.UpdateResourceW(handle, rt_version, res_id, 0x0409,
                                      ctypes.cast(buf, ctypes.c_void_p), len(data)))
        k32.EndUpdateResourceW(handle, not ok)      # discard the edit if it failed
        return ok
    except Exception:
        return False


def _read_ico(path: Path):
    """Return [(dir_entry_bytes, image_size, image_bytes), ...] from an .ico file."""
    import struct
    try:
        data = path.read_bytes()
    except Exception:
        return []
    if len(data) < 6:
        return []
    reserved, itype, count = struct.unpack_from("<HHH", data, 0)
    if reserved != 0 or itype != 1 or count <= 0:
        return []
    entries = []
    for i in range(count):
        off = 6 + i * 16
        if off + 16 > len(data):
            break
        w, h, colors, res, planes, bits, size, img_off = struct.unpack_from(
            "<BBBBHHII", data, off)
        if img_off + size > len(data):
            continue
        entries.append((struct.pack("<BBBBHH", w, h, colors, res, planes, bits),
                        size, data[img_off:img_off + size]))
    return entries


def _build_group_icon(entries) -> bytes:
    """GRPICONDIR: the .ico directory with each image offset replaced by its id."""
    import struct
    out = struct.pack("<HHH", 0, 1, len(entries))
    for i, (prefix, size, _img) in enumerate(entries, start=1):
        out += prefix + struct.pack("<IH", size, i)
    return out


def _exe_resource_langs(path: Path, rtype: int):
    """The (id, language) pairs of one resource type inside an .exe.

    Needed before deleting icon resources: a delete of an id that does not exist
    fails and POISONS the BeginUpdateResource session, after which every call
    returns ERROR_INTERNAL_ERROR and the whole edit is silently lost.
    """
    import ctypes
    from ctypes import wintypes

    LOAD_LIBRARY_AS_DATAFILE = 0x00000002
    # lpType/lpName may be integer atoms, so they must never be declared as strings.
    ENUMNAMEPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p,
                                      ctypes.c_void_p, wintypes.LPARAM)
    ENUMLANGPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p,
                                      ctypes.c_void_p, wintypes.WORD, wintypes.LPARAM)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
    k32.LoadLibraryExW.restype = wintypes.HMODULE
    k32.FreeLibrary.argtypes = [wintypes.HMODULE]
    k32.EnumResourceNamesW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, ENUMNAMEPROC,
                                       wintypes.LPARAM]
    k32.EnumResourceNamesW.restype = wintypes.BOOL
    k32.EnumResourceLanguagesW.argtypes = [wintypes.HMODULE, ctypes.c_void_p,
                                           ctypes.c_void_p, ENUMLANGPROC, wintypes.LPARAM]
    k32.EnumResourceLanguagesW.restype = wintypes.BOOL

    pairs = []
    mod = k32.LoadLibraryExW(str(path), None, LOAD_LIBRARY_AS_DATAFILE)
    if not mod:
        return pairs
    try:
        ids = []

        def on_name(h, t, name, lp):
            if name:
                ids.append(int(name))
            return True

        k32.EnumResourceNamesW(mod, ctypes.c_void_p(rtype), ENUMNAMEPROC(on_name), 0)
        for res_id in ids:
            def on_lang(h, t, n, lang, lp):
                pairs.append((res_id, lang))
                return True

            k32.EnumResourceLanguagesW(mod, ctypes.c_void_p(rtype),
                                       ctypes.c_void_p(res_id), ENUMLANGPROC(on_lang), 0)
    finally:
        k32.FreeLibrary(mod)
    return pairs


def set_exe_icon(path: Path, ico_path: Path, default_lang: int = 0x0409) -> bool:
    """Replace an .exe's icon with TrioForge's own.

    The icon is not part of the version resource: it lives in RT_GROUP_ICON +
    RT_ICON, so renaming the exe and fixing the description still leaves Python's
    logo everywhere Windows draws it. The old entries are deleted first - if they
    stayed, Windows would prefer the original language entry and keep showing the
    Python icon - and only entries that actually exist are deleted, because
    deleting a missing one breaks the whole update session.
    """
    if os.name != "nt":
        return False
    entries = _read_ico(ico_path)
    if not entries:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        RT_ICON, RT_GROUP_ICON = 3, 14
        old_icons = _exe_resource_langs(path, RT_ICON)
        old_groups = _exe_resource_langs(path, RT_GROUP_ICON)
        # Write in the language the exe already uses, so Windows has no reason to
        # prefer another entry.
        lang = old_groups[0][1] if old_groups else default_lang

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
        k32.BeginUpdateResourceW.restype = wintypes.HANDLE
        k32.UpdateResourceW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                        wintypes.WORD, ctypes.c_void_p, wintypes.DWORD]
        k32.UpdateResourceW.restype = wintypes.BOOL
        k32.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]
        k32.EndUpdateResourceW.restype = wintypes.BOOL

        handle = k32.BeginUpdateResourceW(str(path), False)
        if not handle:
            return False

        rt_icon = ctypes.c_void_p(RT_ICON)
        rt_group = ctypes.c_void_p(RT_GROUP_ICON)
        # A NULL data pointer deletes an entry - only for ids/languages that exist.
        for res_id, res_lang in old_icons:
            k32.UpdateResourceW(handle, rt_icon, ctypes.c_void_p(res_id), res_lang, None, 0)
        for res_id, res_lang in old_groups:
            k32.UpdateResourceW(handle, rt_group, ctypes.c_void_p(res_id), res_lang, None, 0)

        keep = []                       # the buffers must outlive UpdateResource
        ok = True
        group = _build_group_icon(entries)
        gbuf = ctypes.create_string_buffer(group, len(group))
        keep.append(gbuf)
        if not k32.UpdateResourceW(handle, rt_group, ctypes.c_void_p(1), lang,
                                   ctypes.cast(gbuf, ctypes.c_void_p), len(group)):
            ok = False
        for i, (_prefix, size, img) in enumerate(entries, start=1):
            buf = ctypes.create_string_buffer(img, len(img))
            keep.append(buf)
            if not k32.UpdateResourceW(handle, rt_icon, ctypes.c_void_p(i), lang,
                                       ctypes.cast(buf, ctypes.c_void_p), size):
                ok = False
        k32.EndUpdateResourceW(handle, not ok)
        return ok
    except Exception:
        return False


def ensure_triorforge_exe(project: Path) -> Optional[str]:
    """Create TrioForge.exe so Task Manager says 'TrioForge', not 'python'.

    Windows shows the executable's file name as the process name, and every
    TrioForge process was pythonw.exe - indistinguishable from any other Python
    program. A renamed copy of the real interpreter stub (plus its DLLs so it can
    load) still resolves the venv through ..\\pyvenv.cfg exactly like the shim, but
    its image name is TrioForge.exe. Returns the path, or None on any failure (the
    caller then falls back to the venv pythonw).
    """
    if os.name != "nt":
        return None
    scripts = project / ".venv" / "Scripts"
    target = scripts / "TrioForge.exe"
    base = _base_python_dir(project)
    if base is None:
        return str(target) if target.is_file() else None
    src = base / "pythonw.exe"
    if not src.is_file():
        return str(target) if target.is_file() else None
    try:
        scripts.mkdir(parents=True, exist_ok=True)
        marker = scripts / ".TrioForge.patch"
        refreshed = False
        if _needs_refresh(src, target):
            _safe_copy(src, target)
            refreshed = True          # a fresh copy has Python's resources again
        # The stub loads python3XY.dll / python3.dll and the VC runtime from its own
        # directory, so they must travel with it. Copy whatever the base ships.
        for pattern in ("python3*.dll", "vcruntime*.dll"):
            for dll in base.glob(pattern):
                dst = scripts / dll.name
                if _needs_refresh(dll, dst):
                    _safe_copy(dll, dst)
        # The copy carries Python's name, description AND icon. Windows takes the
        # display name from the version resource's FileDescription and the logo from
        # RT_GROUP_ICON - neither follows the file name - so both are rewritten here.
        # Bump PATCH_REV whenever this patch changes, so existing installs re-apply it.
        PATCH_REV = "3"
        have = ""
        if not refreshed and marker.is_file():
            try:
                have = marker.read_text(encoding="utf-8").strip()
            except Exception:
                have = ""
        if have != PATCH_REV:
            want = "TrioForge"
            described = set_exe_description(target, want, want, _app_version_tuple(project))
            ico = project / "static" / "logo" / "triorforge.ico"
            # A missing .ico must NOT count as done: treating it as success wrote the
            # patch marker and the copy is never refreshed again, so the exe kept
            # Python's icon forever.
            iconed = bool(ico.is_file()) and set_exe_icon(target, ico)
            if described and iconed:
                try:
                    marker.write_text(PATCH_REV + "\n", encoding="utf-8")
                except Exception:
                    pass
            else:
                # Never silent about this again: an unpatched exe still shows as
                # "Python" with Python's logo, and the marker is withheld so the
                # next start retries instead of assuming it is done.
                print("[window] could not finish branding TrioForge.exe "
                      "(name: {}, icon: {}) - will retry next start.".format(described, iconed))
                if described and not iconed:
                    # set_exe_icon deletes the old icon resources BEFORE writing the
                    # new ones, so a failed write leaves an exe with no icon at all.
                    # Put the pristine copy back rather than keep a broken one.
                    try:
                        _safe_copy(src, target)
                        print("[window] restored the unmodified executable after the "
                              "failed icon patch.")
                    except Exception:
                        pass
        return str(target)
    except Exception:
        return str(target) if target.is_file() else None


def venv_pythonw(project: Path) -> str:
    """The project venv's WINDOWED interpreter (renamed to TrioForge on Windows).

    The venv pythonw shim execs the real pythonw (a GUI process - no console) AND
    keeps the venv's site-packages. The base interpreter via sys._base_executable
    has no console but ALSO no venv packages, so app.py would die on `import
    flask`. On Windows we prefer TrioForge.exe - the same interpreter, but its
    process shows up in Task Manager as TrioForge instead of python.
    """
    if os.name == "nt":
        trioforge = ensure_triorforge_exe(project)
        if trioforge:
            return trioforge
        p = project / ".venv" / "Scripts" / "pythonw.exe"
        if p.is_file():
            return str(p)
    base = project_venv_python(project) or sys.executable
    candidate = Path(base).with_name("pythonw.exe")
    return str(candidate if candidate.is_file() else base)


def _app_window_pid_file():
    """Where the app window records its own pid (same place app_window.py uses)."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "TrioForge" / "app_window.pid"


def _pid_is_our_app(pid: int) -> bool:
    """True when `pid` really is a TrioForge/pywebview process, not a reused pid.

    The pid file outlives the window if it was killed, and Windows reuses pids - so
    trusting the number alone risks focusing, or force-killing, an unrelated
    program. Returns True when the check cannot be made, so a failure here never
    stops the app from starting.
    """
    if os.name != "nt" or not pid:
        return True
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, int(pid))     # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value).lower()
                return (name.startswith("triorforge")
                        or name in ("pythonw.exe", "python.exe"))
        finally:
            k32.CloseHandle(handle)
    except Exception:
        pass
    return True


def _app_window_open() -> bool:
    """True if the app window is already showing (so we never open a second)."""
    try:
        path = _app_window_pid_file()
        if not path.is_file():
            return False
        pid = int(path.read_text(encoding="utf-8").strip().split()[0])
    except Exception:
        return False
    if pid_alive(pid) and _pid_is_our_app(pid):
        return True
    try:
        path.unlink()
    except Exception:
        pass
    return False


def focus_app_window(pid: int) -> bool:
    """Bring the running app window to the front.

    A shortcut click should behave like every other app: when TrioForge is already
    open, show it. Doing nothing is indistinguishable from a broken shortcut - which
    is exactly what the user reported.
    """
    if os.name != "nt" or not pid:
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        found = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def callback(hwnd, _lparam):
            owner = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == int(pid) and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
            return True

        user32.EnumWindows(WNDENUMPROC(callback), 0)
        if not found:
            return False
        hwnd = found[0]
        SW_RESTORE, SW_SHOW = 9, 5
        user32.ShowWindow(hwnd, SW_RESTORE)      # un-minimise
        user32.ShowWindow(hwnd, SW_SHOW)
        user32.SetForegroundWindow(hwnd)         # and raise it
        return True
    except Exception as exc:
        print("[window] could not bring it to the front: {}".format(exc))
        return False


def _app_window_pid() -> int:
    """The pid of the running app window, or 0."""
    try:
        path = _app_window_pid_file()
        if not path.is_file():
            return 0
        return int(path.read_text(encoding="utf-8").strip().split()[0])
    except Exception:
        return 0


def window_is_hung(pid: int) -> bool:
    """True when the app window has stopped responding.

    A frozen window cannot be focused (SetForegroundWindow does nothing to it), so a
    shortcut click would appear to do nothing at all. Replace it instead.
    """
    if os.name != "nt" or not pid:
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        found = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def callback(hwnd, _lparam):
            owner = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == int(pid) and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
            return True

        user32.EnumWindows(WNDENUMPROC(callback), 0)
        if not found:
            return False
        return bool(user32.IsHungAppWindow(ctypes.c_void_p(found[0])))
    except Exception:
        return False


def _replace_app_window(pid: int) -> None:
    """Kill a stale or hung window process (and its children) and drop its pid marker.

    /T matters: the server runs as this window's child, so killing only the window
    would leave the server holding the port with nothing to close it.
    """
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=15, creationflags=_no_window_flags())
    except Exception:
        pass
    try:
        _app_window_pid_file().unlink()
    except Exception:
        pass


def _app_window_started_seconds_ago() -> float:
    """Seconds since the window process started (from its pid file), or huge.

    A freshly-spawned window writes its pid file before its visible window exists,
    so a quick second launch must not mistake it for a dead one. A pid file written
    seconds ago means "still starting"; hours ago means the window is gone while its
    process survived (a crash, or closing the window leaving the message loop behind).
    """
    try:
        path = _app_window_pid_file()
        parts = path.read_text(encoding="utf-8").strip().split()
        if len(parts) >= 3:
            age = time.time() - float(parts[2])
            # A stamp in the FUTURE (clock correction, resumed VM) must read as "very
            # old", not as "just started" - otherwise the grace period below would
            # keep treating a dead window as still-starting and the click would do
            # nothing, indefinitely.
            if age < -60 or age > 86400 * 365:
                return 1e9
            return max(0.0, age)
        # Older pid files carried only pid + url; fall back to the file's mtime.
        return max(0.0, time.time() - path.stat().st_mtime)
    except Exception:
        return 1e9


def _open_app_window(project: Path) -> None:
    """Open TrioForge in its own WebView2 window (no browser, no console).

    The window is a separate process; it probes for the server itself and waits, so
    firing it before the app is ready is fine. One window at a time: an existing one is
    brought to the front, and a hung one is replaced (focusing a frozen window is
    indistinguishable from a broken shortcut).

    A third case matters after a crash or sleep: the window host process survives but
    its WebView2 window is gone. The pid is still alive, so a naive "already open"
    check believes the window exists - and then nothing appears at all, because the
    only thing left is a window-less process. A reboot happened to fix it by killing
    that process, which is why the bug looked "random but cured by restarting". We
    detect it here: a live pid with no visible, focusable window is treated as gone
    and re-opened, exactly like the hung case.
    """
    try:
        if _app_window_open():
            pid = _app_window_pid()
            if window_is_hung(pid):
                print("[window] the open window is not responding - replacing it.")
                # Record it BEFORE replacing: a frozen window cannot write this
                # itself, and without it the replacement starts on the same GPU
                # path and freezes the same way.
                note_window_hang("window not responding when the app was opened again")
                _replace_app_window(pid)
            elif focus_app_window(pid):
                print("[window] TrioForge is already open - brought its window to the front.")
                return
            else:
                if _app_window_started_seconds_ago() < 25:
                    # Still initialising (pid written before the native window exists);
                    # leave it alone - it will appear on its own in a moment.
                    print("[window] TrioForge is still opening (its window is coming up)...")
                    return
                print("[window] the app is running but its window is gone - reopening it.")
                _replace_app_window(pid)
        script = project / "py" / "tools" / "app_window.py"
        if not script.is_file():
            print("[window] app_window.py not found; skipping.")
            return
        pythonw = venv_pythonw(project)
        port = os.environ.get("TRIOFORGE_PORT", "5003")
        flags = 0
        if os.name == "nt":
            flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        # The window's own output goes to a log file: a hang or a crash there used to
        # leave no evidence at all (stdout went to DEVNULL).
        window_log = project / "logs" / "app_window.log"
        try:
            window_log.parent.mkdir(parents=True, exist_ok=True)
            wout = open(str(window_log), "a", encoding="utf-8", errors="replace")
        except Exception:
            wout = subprocess.DEVNULL
        subprocess.Popen([pythonw, "-u", str(script), "--port", str(port), "--title", "TrioForge"],
                         cwd=str(project), creationflags=flags,
                         stdout=wout, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL)
        print("[window] opening TrioForge in its own window on port {}.".format(port))
    except Exception as exc:
        print("[window] could not open the window: {}: {}".format(type(exc).__name__, exc))


def _spawn_background_update(project: Path) -> None:
    """Check for updates in the background, hidden, without delaying the start.

    The blocking update at start-up runs git before the app can open, which is
    exactly the "server should be quick, without git interfering" complaint. This
    runs the same update as its own hidden process: no window, no console, and the
    app is already on screen. New code applies to the next launch.
    """
    try:
        script = Path(__file__).resolve()
        log = project / "logs" / "background-update.log"
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        flags = _no_window_flags()
        if os.name == "nt":
            flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        out = open(str(log), "a", encoding="utf-8", errors="replace")
        subprocess.Popen([venv_pythonw(project), str(script), str(project),
                          "--update", "--no-banner"],
                         cwd=str(project), creationflags=flags,
                         stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    except Exception as exc:
        print("[update] background check not started: {}".format(exc))


def _startup_update(project: Path, force: bool = False) -> None:
    """Best-effort update at launch. Never blocks or breaks the start-up."""
    try:
        state = updater.check(project)
    except Exception as exc:
        print("[update] check failed ({}); starting the current version.".format(exc))
        return
    if not state.get("update_available"):
        if state.get("remote", {}).get("sha"):
            print("[update] up to date ({}).".format(state["local"].get("short") or "?"))
        return
    print("[update] newer version available ({} -> {}); updating...".format(
        state["local"].get("short") or "?", state["remote"].get("short") or "?"))
    result = updater.apply_update(project, allow_dirty=force)
    print("[update] {}".format(result.get("message", "")))
    log = result.get("changes") or []
    if log:
        print("[update] what changed:")
        for line in log[:8]:
            print("   " + line)
    if result.get("updated") and updater.deps_changed(project):
        print("[update] dependency manifests changed; installing before start...")
        install_deps(project)


def run_native(args) -> int:
    """Locate the project and run it with this machine's Python."""
    project = find_project(args.path)
    if project is None:
        print("Could not locate the TrioForge project.")
        print("Run from inside the project folder, pass its path as an argument,")
        print("or set the TRIOFORGE_HOME environment variable.")
        return 1
    return prepare_and_run(project, args)


def _win_to_wsl_path(p: str) -> str:
    """Convert a Windows path (C:\\foo\\bar) to a WSL path (/mnt/c/foo/bar)."""
    p = p.replace("\\", "/")
    drive = p[0].lower()
    return "/mnt/{}/{}".format(drive, p[3:])


def _run_via_wsl(project: Path, args) -> None:
    """Best-effort launch of the Linux side through Windows Subsystem for Linux."""
    wsl = shutil.which("wsl")
    if not wsl:
        print("WSL is not installed. Install WSL (https://learn.microsoft.com/wsl),")
        print("or choose 'Run on Windows' instead — the app is the same either way.")
        return
    launcher = Path(__file__).resolve()
    linux_launcher = _win_to_wsl_path(str(launcher))
    linux_cwd = _win_to_wsl_path(str(project))
    print("Launching via WSL... (make sure Python and the project dependencies")
    print("are installed inside WSL, or run ./run.sh there.)")
    subprocess.run(
        [wsl, "bash", "-lc",
         "cd '{}' && python3 '{}' --unix".format(linux_cwd, linux_launcher)],
    )


def run_unix(args) -> int:
    """'Linux / macOS / WSL' mode: native on Unix, or via WSL from native Windows."""
    if os.name == "nt":
        project = find_project(args.path)
        if project is None:
            print("Could not locate the TrioForge project.")
            return 1
        _run_via_wsl(project, args)
        return 0
    return run_native(args)


# ── Banner (your exact block-character art, kept verbatim) ──
BANNER_WIDE_ROWS = [
    "▐▄▄▄▄▄▌▐▄▄▄▄▄▌ ▐▄▄▌ ▐▄▄▄▄▄▌  ▐▄▄▄▄▄▌ ▐▄▄▄▄▄▌ ▐▄▄▄▄▄▄▌  ▐▄▄▄▄▄▌  ▐▄▄▄▄▄▌     ▐▄▄▄▄▄▌▐▄▄▌ ▐▄▄▌▐▄▄▄▄▄▄▌▐▄▄▌ ▐▄▄▌▐▄▄▄▄▄▄▌ ▐▄▄▌ ▐▄▄▄▄▄▄▌▐▄▄▄▄▄▄▌▐▄▄▌ ▐▄▄▄▄▄▌    ▐▄▄▄▄▄▄▌  ▐▄▄▄▄▄▌ ▐▄▄▄▄▄▄▌",
    "  ▐██▌  ▐██▌ ▐██▌▐██▌▐██▌ ▐██▌▐██▌    ▐██▌ ▐██▌▐██▌ ▐██▌▐██▌     ▐██▌        ▐██▌    ▐██▌ ▐██▌  ▐██▌  ▐██▌ ▐██▌▐██▌ ▐██▌▐██▌▐██▌       ▐██▌  ▐██▌▐██▌        ▐██▌ ▐██▌▐██▌ ▐██▌  ▐██▌  ",
    "  ▐██▌  ▐██████▌ ▐██▌▐██▌ ▐██▌▐████▌  ▐██▌ ▐██▌▐██████▌ ▐██▌▐███▌▐████▌      ▐████▌  ▐██▌ ▐██▌  ▐██▌  ▐██▌ ▐██▌▐██████▌ ▐██▌ ▐█████▌   ▐██▌  ▐██▌▐██▌        ▐██████▌ ▐██▌ ▐██▌  ▐██▌  ",
    "  ▐▀▀▌  ▐▀▀▌ ▐▀▀▌▐▀▀▌▐▀▀▌ ▐▀▀ ▐▀▀▌    ▐▀▀▌ ▐▀▀ ▐▀▀▌ ▐▀▀▌▐▀▀▌ ▐▀▀ ▐▀▀▌        ▐▀▀▌    ▐▀▀▌ ▐▀▀   ▐▀▀▌  ▐▀▀▌ ▐▀▀ ▐▀▀▌ ▐▀▀▌▐▀▀▌     ▐▀▀▌  ▐▀▀▌  ▐▀▀▌▐▀▀▌        ▐▀▀▌ ▐▀▀ ▐▀▀▌ ▐▀▀   ▐▀▀▌  ",
    "  ▐▄▄▌  ▐▄▄▌ ▐▄▄▌▐▄▄▌ ▐▄▄▄▄▄▌ ▐▄▄▌     ▐▄▄▄▄▄▌ ▐▄▄▌ ▐▄▄▌ ▐▄▄▄▄▄▄▌ ▐▄▄▄▄▄▌    ▐▄▄▌     ▐▄▄▄▄▄▌   ▐▄▄▌   ▐▄▄▄▄▄▌ ▐▄▄▌ ▐▄▄▌▐▄▄▌▐▄▄▄▄▄▄▌   ▐▄▄▌  ▐▄▄▌ ▐▄▄▄▄▄▌    ▐▄▄▄▄▄▄▌  ▐▄▄▄▄▄▌   ▐▄▄▌  ",
]


def _colorize_banner_wide() -> str:
    """Return the banner with ANSI color (white borders, cyan fills)."""
    border, fill, reset = '\x1b[97m', '\x1b[96m', '\x1b[0m'
    out = []
    for row in BANNER_WIDE_ROWS:
        out.append(''.join(
            (border + ch + reset) if ch in ('▐', '▌')
            else (fill + ch + reset) if ch in ('▄', '█', '▀')
            else ch
            for ch in row
        ))
    return '\n'.join(out)



def _supports_color() -> bool:
    """True when the terminal is interactive and ANSI color is enabled."""
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def menu_loop(args) -> int:
    """Interactive environment menu."""
    if not getattr(args, "no_banner", False):
        if _supports_color():
            print(_colorize_banner_wide())
        else:
            print('\n'.join(BANNER_WIDE_ROWS))
        print()
    while True:
        print("How do you want to run TrioForge?")
        print("  1) Run on Windows (native)")
        print("  2) Run on Linux / macOS / WSL")
        print("  3) Auto-detect environment")
        print("  4) Quit")
        try:
            choice = input("Enter your choice (1-4): ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if choice == "1":
            run_native(args)
        elif choice == "2":
            run_unix(args)
        elif choice == "3":
            run_native(args)
        elif choice == "4":
            break
        else:
            print("Invalid choice. Please enter 1, 2, 3, or 4.")
    return 0


def main() -> int:
    # Claim the TrioForge identity before anything is spawned. Task Manager groups
    # processes by AppUserModelID and a child inherits its parent's, so a process
    # started before this call appears as its own anonymous entry instead of nesting
    # under TrioForge. Silent and idempotent (and a no-op off Windows).
    try:
        from app_window import claim_app_identity
        claim_app_identity()
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="TrioForge cross-platform launcher.")
    parser.add_argument("path", nargs="?", default=None,
                        help="Path to the TrioForge project folder (optional).")
    parser.add_argument("--menu", action="store_true",
                        help="Show the interactive environment menu.")
    parser.add_argument("--no-banner", action="store_true",
                        help="Skip printing the banner (the .bat already shows it).")
    parser.add_argument("--install", action="store_true",
                        help="Force (re)install dependencies.")
    parser.add_argument("--no-install", action="store_true",
                        help="Skip dependency installation.")
    parser.add_argument("--no-voice", action="store_true",
                        help="Don't auto-start the voice agent; run the app only.")
    parser.add_argument("--unix", action="store_true",
                        help="(internal) Run natively; used when launched via WSL/bash.")
    # ── keeping up to date ──
    parser.add_argument("--update", action="store_true",
                        help="Check for a new version, apply it, and exit (no start).")
    parser.add_argument("--no-update", action="store_true",
                        help="Don't update on start-up (run exactly this checkout).")
    parser.add_argument("--force-update", action="store_true",
                        help="Update even if tracked files have local edits.")
    parser.add_argument("--watch-updates", type=int, default=-1, metavar="SECONDS",
                        help="While running, check for updates every SECONDS and "
                             "restart when one lands (0 = off; default: off when "
                             "interactive, 1800 in --autostart mode).")
    parser.add_argument("--no-auto-restart", action="store_true",
                        help="With --watch-updates: pull the new code but don't "
                             "restart the running app.")
    # ── start automatically ──
    parser.add_argument("--install-autostart", action="store_true",
                        help="Start TrioForge automatically when you log in.")
    parser.add_argument("--remove-autostart", action="store_true",
                        help="Stop starting TrioForge automatically at login.")
    parser.add_argument("--autostart", action="store_true",
                        help="(internal) Quiet mode used by the login entry: no "
                             "menu, no browser tab, updates watched in background.")
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't open a browser tab when starting.")
    parser.add_argument("--status", action="store_true",
                        help="Print version, git state, deps and auto-start state.")
    parser.add_argument("--verify", action="store_true",
                        help="Check the app's own files against integrity-manifest.json "
                             "and scan for injected code. Exits 1 if anything looks off.")
    parser.add_argument("--verify-baseline", action="store_true",
                        help="Rewrite integrity-manifest.json from the current files, "
                             "after a deliberate change.")
    # ── host it for other people ──
    parser.add_argument("--host", action="store_true",
                        help="Host mode: ask for a password before anything is served, "
                             "and print the links to share (LAN / tunnel / Docker).")
    parser.add_argument("--host-password", default="",
                        help="Use this password for host mode instead of a generated one.")
    parser.add_argument("--window", action="store_true",
                        help="Open TrioForge in its own WebView2 window instead of a browser.")
    parser.add_argument("--detach", action="store_true",
                        help="Start the server (and window) in the background and return immediately.")
    parser.add_argument("--background-update", action="store_true",
                        help="Do not wait for an update: start now, check quietly in the background.")
    parser.add_argument("--install-shortcut", action="store_true",
                        help="Put a TrioForge shortcut (with its icon) on the Desktop and Start Menu.")
    parser.add_argument("--remove-shortcut", action="store_true",
                        help="Remove those shortcuts again.")
    args = parser.parse_args()

    try:
        if args.install_autostart or args.remove_autostart:
            project = find_project(args.path)
            if project is None:
                print("Could not locate the TrioForge project.")
                return 1
            if args.install_autostart:
                ok, detail = autostart.install(project)
            else:
                ok, detail = autostart.remove()
            print("{}: {}".format("Auto-start enabled" if args.install_autostart
                                  else "Auto-start disabled", detail))
            return 0 if ok else 1
        if args.install_shortcut or args.remove_shortcut:
            project = find_project(args.path)
            if project is None:
                print("Could not locate the TrioForge project.")
                return 1
            try:
                from shortcuts import install as shortcut_install, remove as shortcut_remove
            except Exception as exc:
                print("Shortcut support unavailable: {}".format(exc))
                return 1
            if args.remove_shortcut:
                for line in shortcut_remove(project):
                    print(line)
            else:
                print("Adding BOTH TrioForge shortcuts (Desktop and Start Menu, this user only):")
                print("  'TrioForge'          -> opens in your browser")
                print("  'TrioForge (window)' -> opens in its own window")
                for flavor in ("web", "window"):
                    for line in shortcut_install(project, flavor=flavor):
                        print("  " + line)
            return 0
        if args.status:
            project = find_project(args.path)
            if project is None:
                print("Could not locate the TrioForge project.")
                return 1
            return show_status(project)
        if args.verify or args.verify_baseline:
            project = find_project(args.path)
            if project is None:
                print("Could not locate the TrioForge project.")
                return 1
            return show_integrity(project, write_baseline=args.verify_baseline)
        if args.update:
            project = find_project(args.path)
            if project is None:
                print("Could not locate the TrioForge project.")
                return 1
            return do_update(project, force=args.force_update)
        if args.menu and not args.autostart:
            return menu_loop(args)
        return run_native(args)
    except KeyboardInterrupt:
        print()
        print("Cancelled.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
