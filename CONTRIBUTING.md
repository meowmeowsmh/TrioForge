# Contributing to TrioForge

Thanks for taking the time. This document is deliberately short and specific — it only
covers the things that will actually get a change merged, or that will bite you if you
don't know them.

---

## Ways to help

| | |
|---|---|
| **Report a bug** | Open an issue with the bug template. Include the Logs panel output — it usually contains the answer. |
| **Request a feature** | Open an issue. "What are you trying to do?" matters more than the proposed implementation. |
| **Fix something** | Good first issues are anything in [CODE_REVIEW.md](CODE_REVIEW.md)'s "Open follow-ups". |
| **Add a plugin or skill** | See [plugins/README.md](plugins/README.md) and [skills/README.md](skills/README.md). A plugin is one `.py` file; a skill is one Markdown file. No core changes needed. |
| **Improve the docs** | The README is long; a clearer paragraph is a real contribution. |

---

## Getting it running

```bash
git clone https://github.com/meowmeowsmh/TrioForge.git
cd TrioForge
./forge                 # installs Python-if-needed, the venv, deps, then starts
```

Windows: double-click **`TrioForge.bat`**. Docker: the one-liner in the README.
`./setup.sh` is only needed if you also want the `forge` terminal client on your `PATH`.

You do **not** need a GPU, a model, or an API key to work on the app — it starts fine
without any of them, and the test suite needs none of them either.

---

## Running the tests

```bash
.venv-linux/bin/python tests/regress.py     # Linux / macOS / WSL
.venv\Scripts\python.exe tests\regress.py   # Windows
```

It needs **no network, no GPU and no model** — every test drives the code with fakes. It
exits non-zero if anything fails, and CI runs it on Linux, macOS and Windows, so you can
also just open a pull request and let CI tell you.

Run it before you push. If you fixed a bug, add a test that would have caught it — the
suite exists because these things broke before.

---

## ⚠️ The integrity baseline — read this one

`integrity-manifest.json` is a SHA-256 of every file that counts as "the app's own
code". CI verifies it **on a fresh clone** and fails if it doesn't match. This trips up
almost every new contributor once, so:

**After adding, removing or editing an in-scope file, run:**

```bash
./refresh-integrity.sh
git add integrity-manifest.json
# commit it together with the change it describes
```

**In scope:** `py/`, `templates/`, `static/`, `docker/`, `catalog/`, plus the launcher
scripts and packaging files listed in `py/tools/integrity.py` (`TrioForge.bat`,
`run.sh`, `install.sh`, `install.ps1`, `forge`, `forge.cmd`, `requirements*.txt`,
`pyproject.toml`, `.gitattributes`).

**Out of scope** — no refresh needed: `README.md`, `CONTRIBUTING.md`, `CODE_REVIEW.md`,
`tests/`, `tools/`, `plugins/`, `skills/`, `.github/`, and anything in `models/`, `logs/`,
`static/uploads/` or `json_configuration/`.

Two things worth knowing:

- **Removing a file counts too.** Deleting an unlisted asset and committing it without
  refreshing leaves the baseline expecting a file that no longer exists.
- Line endings are normalised before hashing (CRLF and CR both fold to LF), so a Windows
  and a Linux checkout produce the same hashes. You don't need to worry about that part.

---

## Code conventions

These are enforced by review, not by a linter. They are all drawn from the existing code.

- **`logging`, never `print`.** Every module has `logger = logging.getLogger(__name__)`.
  The one exception is user-facing output in the launcher and terminal client.
- **`except Exception:`, never a bare `except:`.** A bare except swallows
  `KeyboardInterrupt` and hides real failures.
- **`ProviderError` for provider failures**, not a generic `raise Exception(...)`.
- **Comment the *why*, not the *what*.** The existing code is full of comments explaining
  which specific bug a line prevents and how it showed up. Match that.
- **Cross-platform is not optional.** Windows, macOS, Linux and Docker are all supported
  and all covered by CI. Don't add a POSIX-only path without a Windows branch.
- **Type hints** where they already exist (`py/common.py`, the storage layer).
- **Bumping the version:** keep `py/version.py` and `pyproject.toml` in sync — one is read
  at runtime, the other by packaging tools, and `--status` reports the first.

---

## Pull requests

1. One concern per PR. A bug fix plus a refactor is two PRs.
2. Say **what was broken and how you verified the fix** — the "how" is what makes a PR
   reviewable without a debate.
3. New behaviour needs a test in `tests/regress.py`.
4. Run `tests/regress.py` and `refresh-integrity.sh` if you touched in-scope files.
5. CI runs on Linux, macOS and Windows. Red CI is not "flake" until you've read the log.

A short, honest PR beats a long, confident one. "I'm not sure this is the right layer for
this" is a perfectly good thing to write in a description.

---

## Reporting a security issue

Please **don't** open a public issue. See [SECURITY.md](SECURITY.md).

---

## What this project is not

Worth knowing before you invest time:

- It is **not** trying to be a hosted service. Everything runs on the user's machine.
- It does **not** ship or download model weights for you — you point it at your own.
- It has **no telemetry** and no account system. Please don't add analytics.
