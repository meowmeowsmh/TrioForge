"""TrioForge control panel — the small window the app pops up.

Same idea as Ollama's desktop app: you launch it, a window appears, and it hosts
TrioForge in the background. From here you can open the workspace, watch what the
server is doing, restart or stop it, pull updates, and decide whether TrioForge
starts when you log in.

Only the standard library is used (tkinter), so the Windows exe stays small and
there is nothing extra to install.

    application.exe            -> this window (and the app it hosts)
    application.exe --status   -> plain console output, no window
"""

import os
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
        self.log_line("TrioForge control panel on port {}".format(self.port))
        self.log_line("Log file: {}".format(self._logfile))

    # ── window ───────────────────────────────────────────────────────────────
    def _build(self) -> None:
        self.root = tk.Tk()
        self.root.title("TrioForge")
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
        self.open_btn = self._button(bar, "Open TrioForge", self.open_app, primary=True)
        self.open_btn.pack(side="left")
        self.restart_btn = self._button(bar, "Restart", self.restart_app)
        self.restart_btn.pack(side="left", padx=8)
        self.stop_btn = self._button(bar, "Stop", self.stop_app)
        self.stop_btn.pack(side="left")
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
                no_browser=bool(os.environ.get("TRIOFORGE_NO_BROWSER") == "1"),
                on_output=self.log_line,
            )
            self.log_line("Launching the app...")
            self.supervisor.run()
            self.set_state("stopped", "The server exited. Press Restart to start it again.")

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def open_app(self) -> None:
        url = (self.supervisor.url if self.supervisor and self.supervisor.url else
               "http://localhost:{}".format(self.port))
        self.log_line("Opening {}".format(url))
        try:
            webbrowser.open(url)
        except Exception as exc:
            self.log_line("Could not open a browser: {}".format(exc))

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
        self.root.destroy()

    def toggle_autostart(self) -> None:
        if self.autostart_var.get():
            ok, detail = autostart.install(self.project)
        else:
            ok, detail = autostart.remove()
        self.log_line(("Auto-start: " if ok else "Auto-start failed: ") + detail)
        # reflect the real state rather than what was clicked
        self.autostart_var.set(autostart.state()[0])

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
        try:
            with urllib.request.urlopen("http://127.0.0.1:{}/api/ping".format(self.port),
                                        timeout=2) as resp:
                return b"trioforge" in resp.read(200).lower()
        except (urllib.error.URLError, OSError, ValueError):
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
                        # Auto-start mode (and scripts) must not hijack the browser.
                        if os.environ.get("TRIOFORGE_NO_BROWSER") == "1":
                            self.log_line("Open {} when you're ready.".format(url))
                        else:
                            self.open_app()
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
