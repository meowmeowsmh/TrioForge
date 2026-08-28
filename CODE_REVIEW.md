# TrioForge — Code Review

A prioritized review of the codebase, kept up to date as the project evolves.

> **Current scale** (files live under `py/` after the reorganization):
>
> | File | Lines |
> |---|---|
> | `py/features/cork_board.py` | ~5,029 |
> | `py/features/notes.py` | ~4,639 |
> | `py/app.py` (main Flask app) | ~2,180 |
> | `py/providers/llm_providers.py` | ~1,080 |
> | `py/backup_store.py` | ~397 |
> | `py/tools/launcher.py` | ~341 |
> | `py/llamacpp_service.py` | ~271 |
> | `py/tools/voice_agent.py` | ~227 |
> | `py/features/viewer.py` | ~448 |
> | `py/common.py` | ~99 |
> | `py/voice_service.py` | ~61 |
> | `py/https_guni_n_waitress.py` / `py/gunicorn_conf.py` | ~68 / ~44 |
> | `py/paths.py` | ~10 |
> | `templates/index.html` (chat frontend) | ~5,150 |

---

## ✅ Progress (fixed & verified)

The original review's findings have largely been addressed:

- **P0 bugs & dead code** — image viewer reads real messages; dead `ts` param, `log_message_to_sqlite`, `strip_c_comments`, unused `import yaml` / `import random` removed.
- **Exception hygiene** — all bare `except:` converted to `except Exception:`; `ProviderError(Exception)` introduced and all generic `raise Exception(...)` in providers replaced.
- **Logging** — all `print()` diagnostics replaced with `logging` (levels, timestamps, module names); `logger = logging.getLogger(__name__)` everywhere; line endings normalized to LF.
- **Security/robustness** — 25 MB request-body + attachment caps; rate-limiter bucket pruning; `OLLAMA_BASE_URL` centralized; `DeepSeekProvider.get_status()` added.
- **Dedupe** — shared JSON / SQLite / embedding helpers extracted into `py/common.py`; notes + corkboard import from it.
- **De-monolith** — the ~4,500-line inline HTML moved to `templates/index.html`; shared route helpers (`_build_final_prompt`, `_build_messages`, `_build_log_filters`, `_run_web_search`) extracted; `app.py` shrank to ~2,180 lines.
- **Type hints** — added to `py/common.py` and `app.py`'s conversation storage layer.

### 🆕 Recent additions (since the original review)

- **Folder reorganization** — all Python under `py/`, Docker files under `docker/`; `paths.py` provides project-root path helpers.
- **Workspaces** — per-workspace folder + thinking level + dependencies (no API key/provider fields).
- **Automatic backup & restore** — deleted conversations, notes, and pins are archived to `sqlite_data/backup/backup.db` and can be restored via a UI panel.
- **llama.cpp service** — `py/llamacpp_service.py` auto-starts/stops the local `llama-server`, pairs the `mmproj` vision projector by model name, auto-fits GPU layers, and uses KV-cache quantization.
- **Voice agent service** — `py/voice_service.py` starts/stops the voice-to-voice agent; a **🧩 Services** panel toggles llama.cpp / voice.
- **In-app Logs viewer** — 🗒️ shows Server / Chat / Voice logs live.
- **Single-instance guard** — `app.py` refuses to start a second instance (detects port 5001 in use) and opens the browser instead, preventing duplicate-process conflicts.
- **Provider persistence & fallback** — the selected provider is remembered; if Ollama has no models it auto-switches to llama.cpp.
- **llama.cpp chat integration** — chat auto-starts the server, waits for model readiness, and shows the model's reasoning as a fallback.
- **DeepSeek vision** — added `deepseek-v4-flash-vision-exp` with OpenAI-style image input.
- **UI polish** — icon-only top-bar actions, fullscreen consistency across Chat / Notes / Corkboard, default note & pin colours.

---

## 🔭 Open follow-ups (optional, not required for core function)

- **CSP / CSRF headers** — deferred: the page uses inline scripts/styles and remote CDNs; needs frontend testing to avoid breaking the UI.
- **Tests** — no automated suite yet; recommend `pytest` smoke tests for `common`, the storage layer, and the providers.
- **`handle_ollama_command_stream` vs `execute_ollama_command_sync`** — still share command parsing that could be consolidated.
- **`requirements.txt`** — pins only `waitress`/`gunicorn`; the rest float. Consider pinning ranges.

---

## 📋 Original review findings (for reference — files have since moved to `py/`, so old line numbers are historical)

### P0 — Bugs & dead code (addressed)

| # | Issue | Status |
|---|-------|--------|
| 1 | Image viewer read `conv.get('messages')` after messages migrated to SQLite → always empty | ✅ fixed via `get_messages` |
| 2 | `add_message(..., ts=None)` dead parameter | ✅ removed |
| 3 | `log_message_to_sqlite()` never called | ✅ removed |
| 4 | `strip_c_comments()` dead duplicate | ✅ removed |
| 5 | unused `import yaml` | ✅ removed |
| 6 | unused `import random` | ✅ removed |

### P1 — Security & robustness

| # | Issue | Status |
|---|-------|--------|
| 1 | Attachment size cap | ✅ 25 MB cap added |
| 2 | Rate-limiter IP bucket growth / spoofable header | ✅ prunes stale buckets |
| 3 | Missing Content-Security-Policy | ⏸️ deferred (needs frontend testing) |
| 4 | No CSRF on state-changing routes | ⏸️ documented (local tool; low risk) |
| 5 | `deepseek_status()` reached into private internals | ✅ public `get_status()` added |
| 6 | Bare `except:` clauses | ✅ converted to `except Exception:` |
| 7 | `_clean_api_key()` raised bare `Exception` | ✅ `ProviderError` used |
| 8 | Hardcoded Ollama URL in ~8 places | ✅ centralized to `OLLAMA_BASE_URL` |

### P2 — Duplication & structure

| # | Issue | Status |
|---|-------|--------|
| 1 | ~4,500-line inline HTML in `app.py` | ✅ moved to `templates/index.html` |
| 2 | Copy-pasted JSON/SQLite/embedding in notes + corkboard | ✅ extracted to `py/common.py` |
| 3 | Two different SQLite strategies | ✅ standardized on thread-local `get_conn` |
| 4 | `chat()` / `chat_stream()` duplicate prompt building | ✅ `_build_messages` shared |
| 5 | `get_logs()` / `export_logs_csv()` duplicate query logic | ✅ `_build_log_filters` shared |
| 6 | Duplicate Ollama command dispatch | ⏸️ low priority |
| 7 | `import csv`/`StringIO` inside a route | ✅ moved to module top |
| 8 | Legacy no-op `save_notes_async`/`save_notes_sync` | ⏸️ low priority |

### P3 — Style & polish

| # | Issue | Status |
|---|-------|--------|
| 1 | `print()` used as logging | ✅ replaced with `logging` |
| 2 | Type hints in `app.py` | ✅ added to storage layer + common |
| 3 | Generic `raise Exception` in providers | ✅ `ProviderError` |
| 4 | No tests | ⏸️ recommended |
| 5 | Unpinned deps | ⏸️ recommended |
