"""TrioForge control panel — an optional host window.

The everyday way to run TrioForge is the app itself: double-click ``TrioForge.bat``
(Windows) or run ``./run.sh`` and open the web interface. This panel is for people
who want a small window to manage it instead: it starts and supervises the server,
shows what it is doing, lets you restart or stop it, pull updates, turn on hosting,
and decide whether TrioForge starts when you log in.

Only the standard library is used (tkinter), so there is nothing extra to install.

    python py/tools/launcher_gui.py <project>   -> this window (the host)
    python py/tools/launcher.py <project>       -> plain console, no window
"""

import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import font as tkfont
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import launcher          # noqa: E402  (same folder)
import updater           # noqa: E402
import autostart         # noqa: E402

BG = "#0b0d12"
PANEL = "#12151d"
PANEL_2 = "#171b25"
LINE = "#242a36"
TEXT = "#e6edf3"
MUTED = "#9aa4b2"
DIM = "#6e7784"
ACCENT = "#ff7a2f"
GREEN = "#3fb950"
RED = "#f85149"
BLUE = "#58a6ff"

IS_WINDOWS = os.name == "nt"


class ControlPanel:
    def __init__(self, project: Path, port: str = "") -> None:
        self.project = project
        self.port = port or os.environ.get("TRIOFORGE_PORT", "5003")
        self.supervisor: Optional[launcher.AppSupervisor] = None
        self.worker: Optional[threading.Thread] = None
        self.state = "starting"          # starting | running | stopped | working
        self.detail = "Preparing..."
        self.lines: list = []
        logs = project / "logs"
        try:
            logs.mkdir(parents=True, exist_ok=True)
            self._logfile = logs / "control-panel.log"
        except Exception:
            self._logfile = Path(os.environ.get("TEMP", ".")) / "trioforge-control-panel.log"
        self._build()
        self._start_app()
        self.root.after(400, self._tick)
        self._write_pid_file()
        self.log_line("TrioForge control panel on port {}".format(self.port))
        self.log_line("Log file: {}".format(self._logfile))

    # ── one panel per folder ─────────────────────────────────────────────────
    def _write_pid_file(self) -> None:
        """Record this panel so a second launch attaches instead of duplicating."""
        try:
            path = launcher.panel_pid_file(self.project)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{} {}\n".format(os.getpid(), self.port), encoding="utf-8")
            self._pid_file = path
        except Exception:
            self._pid_file = None

    # ── window ───────────────────────────────────────────────────────────────
    def _build(self) -> None:
        self.root = tk.Tk()
        # NOT "TrioForge": that is the app window's title, and two identically named
        # windows in the taskbar are impossible to tell apart.
        self.root.title("TrioForge control panel")
        self.root.configure(bg=BG)
        self.root.geometry("560x460")
        self.root.minsize(480, 380)
        try:
            icon = self.project / "static" / "logo" / "favicon.png"
            if icon.is_file():
                self._icon = tk.PhotoImage(file=str(icon))
                self.root.iconphoto(True, self._icon)
        except Exception:
            pass

        mono = "Consolas" if IS_WINDOWS else "Menlo"
        ui = "Segoe UI" if IS_WINDOWS else "Helvetica"

        # header: the wordmark, drawn from the app's own logo
        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=18, pady=(16, 8))
        try:
            wordmark = self.project / "static" / "logo" / "wordmark.png"
            if wordmark.is_file():
                self._word = tk.PhotoImage(file=str(wordmark))
                shrink = max(1, self._word.height() // 26)
                self._word = self._word.subsample(shrink, shrink)
                tk.Label(head, image=self._word, bg=BG).pack(side="left")
            else:
                raise RuntimeError
        except Exception:
            tk.Label(head, text="TrioForge", bg=BG, fg=ACCENT,
                     font=(ui, 18, "bold")).pack(side="left")
        tk.Label(head, text="control panel", bg=BG, fg=DIM,
                 font=(ui, 10)).pack(side="left", padx=(10, 0), pady=(6, 0))

        # status card
        card = tk.Frame(self.root, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        card.pack(fill="x", padx=18, pady=(4, 10))
        row = tk.Frame(card, bg=PANEL)
        row.pack(fill="x", padx=14, pady=(12, 6))
        self.dot = tk.Label(row, text="●", bg=PANEL, fg=MUTED, font=(ui, 15))
        self.dot.pack(side="left")
        self.status = tk.Label(row, text="Starting...", bg=PANEL, fg=TEXT,
                               font=(ui, 14, "bold"), anchor="w")
        self.status.pack(side="left", padx=(8, 0))
        self.detail_lbl = tk.Label(card, text="", bg=PANEL, fg=MUTED, font=(ui, 10),
                                   anchor="w", justify="left", wraplength=470)
        self.detail_lbl.pack(fill="x", padx=14, pady=(0, 12))

        # buttons
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill="x", padx=18, pady=(0, 10))
        self.open_btn = self._button(bar, "Open app window", self.open_app_window, primary=True)
        self.open_btn.pack(side="left")
        self.browser_btn = self._button(bar, "Browser", self.open_app)
        self.browser_btn.pack(side="left", padx=8)
        self.restart_btn = self._button(bar, "Restart", self.restart_app)
        self.restart_btn.pack(side="left")
        self.stop_btn = self._button(bar, "Stop", self.stop_app)
        self.stop_btn.pack(side="left", padx=8)
        self.update_btn = self._button(bar, "Check updates", self.check_updates)
        self.update_btn.pack(side="right")

        # options
        opts = tk.Frame(self.root, bg=BG)
        opts.pack(fill="x", padx=18, pady=(0, 8))
        self.autostart_var = tk.BooleanVar(value=autostart.state()[0])
        tk.Checkbutton(opts, text="Start TrioForge when I log in", variable=self.autostart_var,
                       command=self.toggle_autostart, bg=BG, fg=MUTED, selectcolor=PANEL_2,
                       activebackground=BG, activeforeground=TEXT, font=(ui, 10),
                       highlightthickness=0, borderwidth=0).pack(side="left")
        tk.Label(opts, text="port " + self.port, bg=BG, fg=DIM, font=(ui, 10)).pack(side="right")

        # Hosting: let other people use this instance (LAN / tunnel / a server).
        host_row = tk.Frame(self.root, bg=BG)
        host_row.pack(fill="x", padx=18, pady=(0, 8))
        self.host_var = tk.BooleanVar(value=self._host_enabled())
        tk.Checkbutton(host_row, text="Let other people use this (ask them for a password)",
                       variable=self.host_var, command=self.toggle_host, bg=BG, fg=MUTED,
                       selectcolor=PANEL_2, activebackground=BG, activeforeground=TEXT,
                       font=(ui, 10), highlightthickness=0, borderwidth=0).pack(side="left")
        self.host_lbl = tk.Label(host_row, text="", bg=BG, fg=DIM, font=(ui, 9))
        self.host_lbl.pack(side="right")
        self._show_host_password()

        # log
        wrap = tk.Frame(self.root, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        wrap.pack(fill="both", expand=True, padx=18, pady=(4, 8))
        tk.Label(wrap, text="Activity", bg=PANEL, fg=DIM, font=(ui, 10)).pack(anchor="w",
                                                                             padx=12, pady=(8, 0))
        self.log = tk.Text(wrap, height=8, bg=PANEL, fg=MUTED, bd=0, highlightthickness=0,
                           font=(mono, 9), wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True, padx=12, pady=(4, 10))

        foot = tk.Frame(self.root, bg=BG)
        foot.pack(fill="x", padx=18, pady=(0, 14))
        self.foot_lbl = tk.Label(foot, text="", bg=BG, fg=DIM, font=(ui, 9))
        self.foot_lbl.pack(side="left")
        self._button(foot, "Quit", self.quit_app).pack(side="right")

        self.root.protocol("WM_DELETE_WINDOW", self.quit_app)

    def _button(self, parent, text, command, primary=False):
        ui = "Segoe UI" if IS_WINDOWS else "Helvetica"
        return tk.Button(parent, text=text, command=command, font=(ui, 10, "bold" if primary else "normal"),
                         bg=ACCENT if primary else PANEL_2, fg="#1a0e05" if primary else TEXT,
                         activebackground=ACCENT if primary else LINE,
                         activeforeground="#1a0e05" if primary else TEXT,
                         relief="flat", bd=0, padx=14, pady=7, cursor="hand2",
                         highlightthickness=0)

    # ── logging helpers ──────────────────────────────────────────────────────
    def log_line(self, text: str) -> None:
        line = str(text).rstrip()
        self.lines.append(line)
        self.lines = self.lines[-400:]
        if hasattr(self, "log"):
            self.log.configure(state="normal")
            self.log.insert("end", line + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        # keep a file copy too: the window can be closed, the reason should survive
        try:
            with self._logfile.open("a", encoding="utf-8", errors="replace") as fh:
                fh.write(line + "\n")
        except Exception:
            pass

    def set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        self.detail = detail
        colours = {"running": GREEN, "stopped": RED, "starting": MUTED, "working": ACCENT}
        labels = {"running": "Running", "stopped": "Stopped", "starting": "Starting...",
                  "working": "Working..."}
        self.dot.configure(fg=colours.get(state, MUTED))
        self.status.configure(text=labels.get(state, state))
        self.detail_lbl.configure(text=detail)

    # ── the hosted app ───────────────────────────────────────────────────────
    def _start_app(self) -> None:
        # Already serving? Attach to it instead of starting a second copy: two
        # copies means two ports, two consoles and two firewall prompts.
        if self._is_up():
            self.set_state("running", "Already running on http://localhost:{}".format(self.port))
            self.log_line("TrioForge is already running on port {} - attaching.".format(self.port))
            self._opened = True
            return

        self.set_state("starting", "Setting up the environment and starting the server...")
        self.log_line("Starting TrioForge from {}".format(self.project))

        def work():
            try:
                if updater.deps_changed(self.project):
                    self.log_line("Installing dependencies (first run)...")
                    launcher.install_deps(self.project)
                self.log_line("Dependencies ready.")
            except SystemExit as exc:                        # install_deps exits on failure
                self.log_line("Dependency install failed ({}).".format(exc))
                self.set_state("stopped", "Dependency install failed - see Activity.")
                return
            except Exception as exc:
                self.log_line("Setup problem: {}".format(exc))

            self.supervisor = launcher.AppSupervisor(
                self.project,
                watch_seconds=int(os.environ.get("TRIOFORGE_WATCH", "1800")),
                auto_restart=True,
                # The app must NOT open a browser on its own: the panel is the host
                # and it opens TrioForge's window (and a browser only when you press
                # the Browser button). Without this, every start threw open a browser
                # tab AND the app window - "the app keeps opening".
                no_browser=True,
                on_output=self.log_line,
            )
            self.log_line("Launching the app...")
            self.supervisor.run()
            self.set_state("stopped", "The server exited. Press Restart to start it again.")

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def open_app(self) -> None:
        """The web interface, in a browser (handy on a phone or for debugging)."""
        url = (self.supervisor.url if self.supervisor and self.supervisor.url else
               "http://localhost:{}".format(self.port))
        self.log_line("Opening {} in your browser".format(url))
        try:
            webbrowser.open(url)
        except Exception as exc:
            self.log_line("Could not open a browser: {}".format(exc))

    # ── the app window (the desktop edition's own window) ────────────────────
    def _pythonw(self) -> str:
        exe = launcher.project_venv_python(self.project) or sys.executable
        candidate = Path(exe).with_name("pythonw.exe")
        return str(candidate if candidate.is_file() else exe)

    def _webview_installed(self) -> bool:
        try:
            import webview  # noqa: F401
            return True
        except Exception:
            return False

    def ensure_webview(self) -> bool:
        """Install pywebview into the project venv the first time it is needed.

        The app window renders through the WebView2 engine Windows already has;
        pywebview is only the ~1 MB glue. Installing it on demand keeps it out of
        the core requirements (Docker and Linux servers do not need it at all).
        """
        if self._webview_installed():
            return True
        venv_python = launcher.project_venv_python(self.project)
        if not venv_python:
            self.log_line("No project venv found; cannot install the window engine.")
            return False
        uv = shutil.which("uv")
        cmd = ([uv, "pip", "install", "--python", venv_python, "pywebview"] if uv
               else [venv_python, "-m", "pip", "install", "pywebview"])
        self.log_line("Installing the app window engine (one time, ~1 MB)...")
        try:
            result = subprocess.run(cmd, cwd=str(self.project), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                    errors="replace", timeout=300,
                                    creationflags=launcher._no_window_flags())
        except Exception as exc:
            self.log_line("Could not install it: {}".format(exc))
            return False
        if result.returncode != 0:
            self.log_line("Install failed: " + (result.stdout or "").strip().splitlines()[-1:] and
                          (result.stdout or "").strip().splitlines()[-1] or "unknown error")
            return False
        self.log_line("Window engine installed.")
        # The panel's own interpreter already tried and failed to import it; the
        # window runs in a separate process, so that is fine.
        return True

    def _app_window_open(self) -> bool:
        """True if an app window is already showing.

        Checked through the pid file the window itself writes, not a pid we stored
        when launching it: the venv's pythonw.exe is a shim that exits right after
        starting the real interpreter, so a stored pid dies immediately and we
        would open another window on every click.
        """
        try:
            from app_window import window_pid_file     # same folder
        except Exception:
            return False
        try:
            path = window_pid_file()
            if not path.is_file():
                return False
            pid = int(path.read_text(encoding="utf-8").strip().split()[0])
        except Exception:
            return False
        if launcher.pid_alive(pid):
            return True
        try:
            path.unlink()                 # stale file from a closed/crashed window
        except Exception:
            pass
        return False

    def open_app_window(self) -> None:
        """TrioForge in its own window: no browser, no tabs, no address bar."""
        url = (self.supervisor.url if self.supervisor and self.supervisor.url else
               "http://localhost:{}".format(self.port))
        script = self.project / "py" / "tools" / "app_window.py"
        if not script.is_file():
            self.log_line("app_window.py is missing (update TrioForge); using the browser.")
            return self.open_app()
        if self._app_window_open():
            self.log_line("The app window is already open.")
            return

        def work():
            if not self.ensure_webview():
                self.log_line("Falling back to the browser.")
                self.root.after(0, self.open_app)
                return
            cmd = [self._pythonw(), str(script), "--url", url, "--title", "TrioForge"]
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | launcher._no_window_flags()
            try:
                proc = subprocess.Popen(cmd, cwd=str(self.project), creationflags=flags,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        stdin=subprocess.DEVNULL)
                self._window_pid = proc.pid
                self.log_line("App window opened ({})".format(url))
            except Exception as exc:
                self.log_line("Could not open the app window: {}".format(exc))

        threading.Thread(target=work, daemon=True).start()

    def stop_app(self) -> None:
        if self.supervisor:
            self.log_line("Stopping the server...")
            self.supervisor._stop_child()
        self.set_state("stopped", "Stopped by you. Press Restart to bring it back.")

    def restart_app(self) -> None:
        self.log_line("Restarting...")
        self.stop_app()
        self.root.after(1200, self._start_app)

    def quit_app(self) -> None:
        if self.supervisor:
            self.supervisor._stop_child()
        # Close the app window too: leaving a window pointing at a dead server is
        # worse than closing it.
        if self._app_window_open():
            try:
                from app_window import window_pid_file
                pid = int(window_pid_file().read_text(encoding="utf-8").strip().split()[0])
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                               creationflags=launcher._no_window_flags())
            except Exception:
                pass
        try:
            if getattr(self, "_pid_file", None):
                self._pid_file.unlink()
        except Exception:
            pass
        self.root.destroy()

    def toggle_autostart(self) -> None:
        if self.autostart_var.get():
            ok, detail = autostart.install(self.project)
        else:
            ok, detail = autostart.remove()
        self.log_line(("Auto-start: " if ok else "Auto-start failed: ") + detail)
        # reflect the real state rather than what was clicked
        self.autostart_var.set(autostart.state()[0])

    # ── hosting for other people ─────────────────────────────────────────────
    def _host_store(self):
        return self.project / "json_configuration" / "host_password"

    def _host_enabled(self) -> bool:
        store = self._host_store()
        try:
            return store.is_file() and bool(store.read_text(encoding="utf-8").strip())
        except Exception:
            return False

    def _show_host_password(self) -> None:
        """Show the password next to the checkbox so it can be passed on."""
        if not self._host_enabled():
            self.host_lbl.configure(text="local only")
            return
        try:
            pw = self._host_store().read_text(encoding="utf-8").strip()
        except Exception:
            pw = "(see json_configuration/host_password)"
        self.host_lbl.configure(text="password: {}".format(pw))

    def toggle_host(self) -> None:
        """Turn host mode on/off and restart the app so it takes effect."""
        store = self._host_store()
        if self.host_var.get():
            # host_setup() creates the password (and keeps it across restarts)
            launcher.host_setup(self.project)
            self.log_line("Hosting enabled - share the link and the password below.")
            for url in launcher._lan_urls():
                self.log_line("   on your network: " + url)
            self.log_line("   internet: cloudflared tunnel --url http://localhost:{}".format(self.port))
            self.log_line("   Everyone you share it with can see this workspace and its data.")
        else:
            os.environ.pop("TRIOFORGE_PASSWORD", None)
            try:
                store.unlink()
            except Exception:
                pass
            self.log_line("Hosting disabled - back to local only.")
        self._show_host_password()
        self.restart_app()

    def check_updates(self) -> None:
        self.set_state("working", "Checking for a new version...")

        def work():
            state = updater.check(self.project)
            if not state.get("update_available"):
                self.root.after(0, lambda: self.set_state("running" if self._is_up() else "stopped",
                                                          "Already up to date ({}).".format(
                                                              state["local"].get("short") or "?")))
                return
            self.log_line("Update found: {} -> {}".format(
                state["local"].get("short") or "?", state["remote"].get("short") or "?"))
            result = updater.apply_update(self.project)
            self.log_line(result.get("message", ""))
            for line in result.get("changes", []) or []:
                self.log_line("   " + line)
            if result.get("updated") and updater.deps_changed(self.project):
                launcher.install_deps(self.project)
            self.root.after(0, lambda: self.restart_app() if result.get("updated") else None)
            self.root.after(0, lambda: self.set_state("running" if self._is_up() else "stopped",
                                                      result.get("message", "")))

        threading.Thread(target=work, daemon=True).start()

    # ── polling ──────────────────────────────────────────────────────────────
    def _is_up(self) -> bool:
        """Is the hosted app answering? Tries https too - Windows serves HTTPS."""
        import ssl as _ssl
        ctx = _ssl._create_unverified_context()
        for scheme in ("http", "https"):
            try:
                with urllib.request.urlopen("{}://127.0.0.1:{}/api/ping".format(scheme, self.port),
                                            timeout=2, context=ctx) as resp:
                    if b"trioforge" in resp.read(200).lower():
                        return True
            except (urllib.error.URLError, OSError, ValueError, _ssl.SSLError):
                continue
        return False

    def _tick(self) -> None:
        if self.state in ("starting", "working") or self.state == "running":
            if self._is_up():
                if self.state != "running":
                    url = (self.supervisor.url if self.supervisor and self.supervisor.url
                           else "http://localhost:{}".format(self.port))
                    self.set_state("running", "Listening on {}".format(url))
                    self.log_line("Server is up: {}".format(url))
                    if not getattr(self, "_opened", False):
                        self._opened = True
                        # Open TrioForge's OWN window. This is the desktop app: it
                        # must not throw a web page at you every time it starts.
                        if os.environ.get("TRIOFORGE_NO_BROWSER") == "1":
                            self.log_line("Press Open app window when you are ready.")
                        else:
                            self.open_app_window()
            elif self.state == "running":
                self.set_state("starting", "The server is not answering; waiting...")
        try:
            info = updater.status(self.project)
            self.foot_lbl.configure(text="{} · {} · python {}".format(
                info.get("commit") or "no git", info.get("branch") or "-", info.get("python")))
        except Exception:
            pass
        self.root.after(1200, self._tick)

    def run(self) -> int:
        self.root.mainloop()
        return 0


def main() -> int:
    # Claim the TrioForge identity before the panel starts the server or the app window.
    # Task Manager groups processes by AppUserModelID, and a child inherits its parent's:
    # without this, the server and window it spawns appear as separate entries instead of
    # nesting under TrioForge. Silent and idempotent.
    try:
        from app_window import claim_app_identity
        claim_app_identity()
    except Exception:
        pass

    import argparse
    parser = argparse.ArgumentParser(description="TrioForge control panel")
    parser.add_argument("path", nargs="?", default=None)
    args = parser.parse_args()
    project = launcher.find_project(args.path)
    if project is None:
        print("Could not locate the TrioForge project.")
        return 1
    return ControlPanel(project).run()


if __name__ == "__main__":
    sys.exit(main())
