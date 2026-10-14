# Upgrading the Python floor

TrioForge declares `requires-python >=3.12` in `pyproject.toml`. Python ships a new
release every October and supports each one for ~5 years, so roughly **every two
years the floor must move up before the current one reaches end-of-life**.

| Version | Security support ends | Status when this was written |
|---|---|---|
| 3.10 | October 2026 (EOL) | dropped |
| 3.11 | October 2027 | not supported (security-only) |
| **3.12** | **October 2028** | **the current floor** |
| 3.13 | October 2029 | tested |
| 3.14 | October 2030 | tested |

**When 3.12 is ~6 months from its end-of-life (spring 2028), run this recipe to move
the floor up.** It is mechanical — the 3.10 → 3.12 bump followed exactly these steps
and nothing was left behind.

---

## 1. Pick the new floor

Move to the oldest release that is still in its security-fix window. From 3.12 that
is **3.13**, then **3.14**. Never keep a version past EOL — the reason it was dropped
is not "old", it's "unpatched".

## 2. Declare it

- `pyproject.toml` → `requires-python = ">=3.XX"`.
- Regenerate the lock: `uv lock`. This drops the old resolution markers and collapses
  the duplicate `numpy` / `scipy` / `scikit-learn` / `scipy` variants into one each
  (that is the big `uv.lock` diff you will see).

## 3. Enforce it in every installer

Each of these has its **own copy** of the floor check — update all three:

- **`run.sh`** — the `pick_python()` candidate list (drop the old floor's name),
  the `sys.version_info >= (3, X)` check, and the "needs Python 3.X or newer" message.
- **`deps.sh`** — `forge_find_python()`'s `sys.version_info >= (3, X)` check, plus
  three messages: "python 3.X+ not found", "Install Python 3.X+", and
  "no usable python3 (3.X+) found".
- **`install.ps1`** — the floor-check block right after `$py` is chosen, and its
  message. ⚠️ Keep this file **ASCII-only**: a single em-dash or smart quote breaks
  PowerShell 5.1 parsing and the whole installer stops working.

## 4. CI and Docker

- `.github/workflows/python-matrix.yml` — the `python:` list. Keep three entries:
  **floor, one between, newest** (it went 3.10/3.12/3.14 → 3.12/3.13/3.14).
- `docker/Dockerfile` — `FROM python:X.Y-slim` must be >= the floor. It is
  `3.12-slim` today; bump it the same day as the floor.

## 5. Docs

- `README.md` — the badge and the "Requires Python 3.X+" paragraph.
- `CHANGELOG.md` — one bullet under the current release.

## 6. Remove now-dead version branches and shims

Grep for the old floor and the old floor's features:

```bash
grep -rn "sys.version_info\|pre-3\.\|3\.XX+\|3\.YY+" py/
```

- Anything guarded by the **old** floor becomes unconditional.
- From the 3.10 → 3.12 bump, that meant: `tarfile.extractall(..., filter="data")`
  lost its unsafe fallback, the `value.replace('Z', '+00:00')` shim was deleted, and
  the now-unused `import sys` was removed.
- Substitute newer stdlib where a workaround existed: `datetime.fromisoformat`
  parses a trailing `Z` natively on 3.11+, so the shim went away.

## 7. Verify

```bash
# on the dev venv
.venv\Scripts\python.exe tests\regress.py        # Windows
.venv-linux/bin/python tests/regress.py          # Linux / macOS / WSL

# and on the NEW floor, for real
uv venv --python 3.XX "$TEMP\tf3XX"
uv pip install --python "$TEMP\tf3XX\Scripts\python.exe" -r requirements.txt
"$TEMP\tf3XX\Scripts\python.exe" tests\regress.py
```

Then refresh the integrity baseline (see `CONTRIBUTING.md`), commit, push, and
re-tag the release (or cut a new one).

## Things NOT to touch

- `gunicorn` and `waitress` are **servers**, not imports — they stay.
- `werkzeug` and `urllib3` are **transitive security floors** pinned deliberately
  (they ride in via flask/requests; the direct pins only stop a re-resolve drifting
  below the CVE-fixed versions). They stay, even though nothing `import`s them.
