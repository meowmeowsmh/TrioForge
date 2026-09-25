# 🐍 Installing Python for TrioForge (no manual downloads)

You **don't need Python or Docker already installed** to run TrioForge. The launcher
(`TrioForge.bat` on Windows, `run.sh` on Linux/macOS) detects a missing Python,
installs the **latest version**, and shows a **Launch** prompt.

Just double-click **`TrioForge.bat`** (Windows) or run **`./run.sh`** (Linux/macOS),
then press **Enter** when it says "Launch".

---

## 🪟 Windows

1. Double-click **`TrioForge.bat`**.
2. It finds the **latest installed Python 3** automatically (`py -3`) — no version
   number is hard-coded.
3. If Python isn't found, it installs the latest via **winget** (built into Windows 10/11).
4. A **"Press Launch (Enter)"** prompt appears — press Enter and TrioForge opens
   at **https://localhost:5001**.

**If winget is missing (older Windows):** the launcher opens the Microsoft Store
page for Python (latest) so you click **Get** — still no manual file download.

---

## 🐧 Linux (Debian / Ubuntu / Fedora / Arch / WSL)

1. Open a terminal in the TrioForge folder and run:
   ```bash
   ./run.sh
   ```
2. It uses the **latest `python3`** on your system. If none is installed, it
   auto-installs it with your package manager:
   - **Debian / Ubuntu / WSL** → `sudo apt-get install python3 python3-pip`
   - **Fedora / RHEL** → `sudo dnf install python3 python3-pip`
   - **Arch / Manjaro** → `sudo pacman -S python python-pip`
3. Then it starts TrioForge at **https://localhost:5001**.

> 🔐 The `sudo` step may ask for your password — that's normal (installing a
> system package). Everything else runs without sudo.

---

## 🍎 macOS

1. Open a terminal in the TrioForge folder and run:
   ```bash
   ./run.sh
   ```
2. It uses the latest `python3`. If you have **Homebrew**, missing Python is
   installed with `brew install python` automatically.
3. If you don't have Homebrew, install it once:
   ```bash
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```
   …then run `./run.sh` again.

---

## ❓ What if it still can't find Python?

After an automatic install, the **current terminal window** sometimes can't see the
new Python until you open a fresh one.

- **Windows:** close the window, open a new Command Prompt, run `TrioForge.bat` again.
- **Linux/macOS:** open a new terminal, run `./run.sh` again.

---

## ✅ Manual install (only if you prefer)

| OS | Install the latest Python from |
|---|---|
| Windows | https://apps.microsoft.com/detail/9NRWMJP3717K (Store) |
| macOS | https://www.python.org/downloads/ |
| Linux | `sudo apt install python3` / `sudo dnf install python3` / `sudo pacman -S python` |

> ⚠️ These links are only a fallback — the launcher normally does this for you.
