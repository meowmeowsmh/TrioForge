#!/usr/bin/env python3
"""Small "which browser?" chooser for the TrioForge shortcut.

Click the shortcut -> this little window appears -> pick Chrome / Firefox / Safari
(or whatever is installed) -> TrioForge opens there.

It deliberately starts the app with TRIOFORGE_NO_BROWSER=1 so the app never picks a
browser itself; this dialog decides. The choice is also saved, so the app's own
"open in" setting agrees next time.
"""

import os
import sys
import json
import shutil
import socket
import platform
import subprocess
import time

PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PREF_FILE = os.path.join(PROJECT, "json_configuration", "browser.json")
DEFAULT_PORT = 5003

# id, label, candidate executables
BROWSERS = (
    ("firefox", "Firefox",         ("firefox",)),
    ("chrome",   "Google Chrome",  ("google-chrome", "google-chrome-stable")),
    ("chromium", "Chromium",       ("chromium", "chromium-browser")),
    ("safari",  "Safari",          ()),
    ("edge",    "Microsoft Edge",  ("microsoft-edge", "microsoft-edge-stable")),
    ("brave",   "Brave",           ("brave-browser",)),
    ("opera",   "Opera",           ("opera",)),
    ("vivaldi", "Vivaldi",         ("vivaldi",)),
)


def detect():
    found = []
    for bid, label, commands in BROWSERS:
        path = ""
        for cmd in commands:
            located = shutil.which(cmd)
            if located:
                path = located
                break
        if not path and bid == "safari" and platform.system() == "Darwin":
            if os.path.isdir("/Applications/Safari.app"):
                path = "/Applications/Safari.app"
        if path:
            found.append((bid, label, path))
    return found


def read_pref():
    try:
        with open(PREF_FILE, encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("browser") or ""
    except Exception:
        return ""


def save_pref(browser_id):
    try:
        os.makedirs(os.path.dirname(PREF_FILE), exist_ok=True)
        with open(PREF_FILE, "w", encoding="utf-8") as fh:
            json.dump({"browser": browser_id}, fh, indent=2)
    except Exception:
        pass


def start_trio():
    """Start TrioForge detached, WITHOUT letting it open a browser itself."""
    python = os.path.join(PROJECT, ".venv-linux", "bin", "python")
    if not os.path.isfile(python):
        python = sys.executable
    launcher = os.path.join(PROJECT, "py", "tools", "launcher.py")
    env = dict(os.environ)
    env["TRIOFORGE_NO_BROWSER"] = "1"
    try:
        subprocess.Popen([python, launcher, PROJECT, "--detach"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception as exc:
        print("could not start TrioForge: %s" % exc, file=sys.stderr)


def wait_for_port(port=DEFAULT_PORT, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def open_browser(bid, path, port=DEFAULT_PORT):
    url = "http://localhost:%d/" % port
    try:
        if bid == "safari":
            subprocess.Popen(["open", "-a", "Safari", url])
        else:
            subprocess.Popen([path, url], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                             start_new_session=True)
        return True
    except Exception as exc:
        print("could not open %s: %s" % (bid, exc), file=sys.stderr)
        return False


# ----------------------------------------------------------------- the dialog
def run_gui(options):
    import tkinter as tk

    current = read_pref()
    root = tk.Tk()
    root.title("Open TrioForge in…")
    root.configure(bg="#14141c")
    root.resizable(False, False)

    tk.Label(root, text="Open TrioForge in which browser?",
             bg="#14141c", fg="#e1e4e8",
             font=("Sans", 12, "bold")).pack(padx=18, pady=(16, 8), anchor="w")

    choice = tk.StringVar(value=current if current in [o[0] for o in options] else options[0][0])
    for bid, label, path in options:
        tk.Radiobutton(root, text=label, variable=choice, value=bid,
                       bg="#14141c", fg="#c9d1d9", selectcolor="#1c1c26",
                       activebackground="#14141c", activeforeground="#ffffff",
                       font=("Sans", 11), anchor="w").pack(fill="x", padx=22, pady=1)

    remember = tk.BooleanVar(value=True)
    tk.Checkbutton(root, text="Remember this choice", variable=remember,
                   bg="#14141c", fg="#8b949e", selectcolor="#1c1c26",
                   activebackground="#14141c", font=("Sans", 9)).pack(padx=20, pady=(8, 0), anchor="w")

    status = tk.Label(root, text="", bg="#14141c", fg="#8b949e", font=("Sans", 9))
    status.pack(padx=18, pady=(4, 0), anchor="w")

    picked = {"done": False}

    def go():
        bid = choice.get()
        path = next(p for b, l, p in options if b == bid)
        if remember.get():
            save_pref(bid)
            status.config(text="Saved. Opening %s…" % bid)
        else:
            status.config(text="Opening %s…" % bid)
        root.update_idletasks()
        start_trio()
        status.config(text="Starting TrioForge…")
        root.update_idletasks()
        wait_for_port()
        open_browser(bid, path)
        picked["done"] = True
        root.destroy()

    bar = tk.Frame(root, bg="#14141c")
    bar.pack(fill="x", padx=18, pady=(12, 16))
    tk.Button(bar, text="Open TrioForge", command=go, bg="#1c2b1c", fg="#7ee787",
              activebackground="#243b24", relief="flat", padx=14, pady=6).pack(side="left")
    tk.Button(bar, text="Cancel", command=root.destroy, bg="#1c1c26", fg="#c9d1d9",
              activebackground="#26263a", relief="flat", padx=14, pady=6).pack(side="left", padx=8)

    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 3
    root.geometry("+%d+%d" % (x, y))
    root.attributes("-topmost", True)
    root.mainloop()
    return picked["done"]


def run_zenity(options):
    args = ["zenity", "--list", "--radiolist", "--title=Open TrioForge in…",
            "--text=Which browser should TrioForge open in?", "--column=", "--column=Browser"]
    for i, (bid, label, _p) in enumerate(options):
        args.append("TRUE" if i == 0 else "FALSE")
        args.append(label)
    try:
        out = subprocess.run(args, capture_output=True, text=True)
    except Exception:
        return False
    label = (out.stdout or "").strip()
    if not label:
        return False
    bid, path = next(((b, p) for b, l, p in options if l == label), (None, None))
    if not bid:
        return False
    save_pref(bid)
    start_trio()
    wait_for_port()
    return open_browser(bid, path)


def main():
    options = detect()
    if not options:
        print("No browsers found.", file=sys.stderr)
        # Still start the app so the shortcut is not dead.
        start_trio()
        return 1
    try:
        run_gui(options)
        return 0
    except Exception as exc:
        print("GUI unavailable (%s); falling back to zenity." % exc, file=sys.stderr)
        run_zenity(options)
        return 0


if __name__ == "__main__":
    sys.exit(main())
