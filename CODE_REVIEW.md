# TrioForge — Code Review

A prioritized review of the codebase, kept up to date as the project evolves.

> **Current scale — v1.5.0** — 172 tracked files, ~72,700 lines of text on disk
>
> | File | Lines |
> |---|---|
> | `templates/index.html` (chat frontend) | 10,290 |
> | `py/app.py` (main Flask app) | 6,266 |
> | `py/features/notes.py` (page: CSS + HTML + JS in one r-string) | 5,126 |
> | `py/features/cork_board.py` (page: CSS + HTML + JS in one r-string) | 5,119 |
> | `py/providers/llm_providers.py` | 2,824 |
> | `py/tui/textual_app.py` (terminal client) | 2,751 |
> | `tests/regress.py` | 1,810 |
> | `py/tools/launcher.py` | 1,747 |
> | `py/llamacpp_service.py` | 1,464 |
> | `py/tui/commands.py` | 1,293 |
> | `py/tools/app_window.py` | 1,251 |
> | `py/hardware.py` | 961 |
> | `py/comfyui_service.py` | 931 |
>
> **48,114 lines of Python across 79 files.** 185 route decorators across 14 files.
> 8 providers. 20 terminal-client modules. 5 plugins, 6 skills, 3 catalog skills.
> Tracked files per directory: `py/` (73) · `static/` (20) · `tools/` (8) ·
> `skills/` (7) · `plugins/` (6) · `docker/` (4) · `catalog/` (4) · `tests/` (1).

---

## ✅ Verification status

| Check | Result |
|---|---|
| `tests/regress.py` (1,810 lines, no network / GPU / model needed) | **ALL TESTS PASS** |
| CI — `.github/workflows/` | `linux-smoke` · `macos-smoke` · `windows-smoke` · `publish-image` (build + smoke-test the container) · `release` |
| File integrity baseline (`py/tools/integrity.py` + `integrity-manifest.json`) | 113 files hash-verified; CI fails on drift |
| Startup (import → serving) | ~3.4 s → **0.6 s** after deferring blocking I/O (see below) |

---

## ✅ Progress (fixed & verified)

The original review's findings have been addressed:

- **P0 bugs & dead code** — image viewer reads real messages; dead `ts` param, `log_message_to_sqlite`, `strip_c_comments`, unused `import yaml` / `import random` removed.
- **Exception hygiene** — all bare `except:` converted to `except Exception:`; `ProviderError(Exception)` introduced and all generic `raise Exception(...)` in providers replaced.
- **Logging** — `print()` diagnostics replaced with `logging` (levels, timestamps, module names); `logger = logging.getLogger(__name__)` throughout; line endings normalized to LF.
- **Security/robustness** — 25 MB request-body + attachment caps; rate-limiter bucket pruning; `OLLAMA_BASE_URL` centralized; `X-Frame-Options: SAMEORIGIN`; host-mode password gate with an open-path allowlist; `DeepSeekProvider.get_status()` added.
- **Dedupe** — shared JSON / SQLite / embedding helpers extracted into `py/common.py`; notes + corkboard import from it.
- **De-monolith** — the inline HTML moved out of `app.py` into `templates/index.html`; shared route helpers (`_build_final_prompt`, `_build_messages`, `_build_log_filters`, `_run_web_search`) extracted.
- **Type hints** — added to `py/common.py` and `app.py`'s conversation storage layer.
- **Tests** — *(was: "no automated suite")* now a 1,810-line regression suite plus smoke tests on Linux, macOS and Windows.
- **Startup performance** — two blocking calls used to run at import and cost ~2.8 s of every launch:
  - `LlamaCppProvider.__init__` probed a llama-server over HTTP (`/v1/models`) inside the module-level `providers` dict. On Windows a *refused* connection to a closed port costs ~2 s (the TCP stack retries the SYN), so startup waited on a server that usually was not running. The probe now runs in a background thread; the constructor does the local GGUF glob only.
  - `pynvml.nvmlInit()` ran at import (~0.75 s, loading the driver library) — now lazy via `nvml_available()`, paid once on first real VRAM query.

### 🆕 What 1.5 added

- **Plugins · skills · MCP** — `plugin_loader.py`, `skills_loader.py`, `mcp_client.py`, `extensions.py`. A plugin is one `.py` file exposing tools; a skill is a Markdown instruction pack; an MCP server is borrowed as tools. One inventory panel, install by Git URL or folder.
- **Connectors** — Gmail (`plugins/gmail.py`), Google Calendar (`plugins/calendar.py`), and a fully offline Obsidian vault connector (`plugins/obsidian.py`).
- **Terminal client** (`py/tui/`, 20 modules; `forge` / `trioforge`) — full-screen Textual UI with its own agent, file tools, model pickers, router and team mode.
- **Hardware layer** (`py/hardware.py`) — per-vendor GPU detection (CUDA / Vulkan / Metal / ROCm / SYCL) via `llama-server --list-devices`, then vendor APIs; no iGPU/dGPU RAM summing.
- **llama.cpp lifecycle** (`py/llamacpp_service.py`, `py/llama_cpp.py`, `py/llama_installer.py`) — auto-start/stop, GPU-layer auto-fit, `mmproj` pairing, KV-cache quantization, UI-controlled context size, GPU-OOM step-down, stdlib-only client.
- **Bundled installers** — llama.cpp and ffmpeg fetched for the detected OS + GPU.
- **Memory** (`py/memory.py`), **workspaces**, **automatic backup & restore** (`py/backup_store.py`), **in-app log viewer**, **voice agent** (`py/voice_service.py`), **PWA**, **Docker + GHCR publishing**.
- **Integrity system** (`py/tools/integrity.py`) — SHA-256 baseline of app files, checked in CI on a fresh clone.

---

## 🔭 Open follow-ups (optional, not required for core function)

- **Content-Security-Policy** — still absent (only `X-Frame-Options`). The pages use inline scripts/styles and, historically, remote CDNs; adding CSP needs frontend testing so the UI does not break. Worth revisiting now that vendored JS lives under `static/vendor/`.
- **CSRF on state-changing routes** — none. Historically low risk for a local-only tool, but it matters more since host mode (`TRIOFORGE_PASSWORD`) exposes the app beyond localhost.
- **Ollama command dispatch** — `is_ollama_command`, `execute_ollama_command_sync` and `handle_ollama_command_stream` still share command parsing that could be consolidated into one parser.
- **`requirements.txt` pinning** — only `waitress==3.0.2` and `gunicorn==26.0.0` are exactly pinned; the rest use `>=` floors. Consider pinning ranges for reproducible installs.

---

## 📋 Original review findings (historical record)

Line numbers are historical — the files have since moved under `py/` and grown considerably.

### P0 — Bugs & dead code

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
| 3 | Missing Content-Security-Policy | ⏸️ still open (see above) |
| 4 | No CSRF on state-changing routes | ⏸️ still open (see above) |
| 5 | `deepseek_status()` reached into private internals | ✅ public `get_status()` added |
| 6 | Bare `except:` clauses | ✅ converted to `except Exception:` |
| 7 | `_clean_api_key()` raised bare `Exception` | ✅ `ProviderError` used |
| 8 | Hardcoded Ollama URL in ~8 places | ✅ centralized to `OLLAMA_BASE_URL` |

### P2 — Duplication & structure

| # | Issue | Status |
|---|-------|--------|
| 1 | Large inline HTML inside `app.py` | ✅ moved to `templates/index.html` |
| 2 | Copy-pasted JSON/SQLite/embedding in notes + corkboard | ✅ extracted to `py/common.py` |
| 3 | Two different SQLite strategies | ✅ standardized on thread-local `get_conn` |
| 4 | `chat()` / `chat_stream()` duplicate prompt building | ✅ `_build_messages` shared |
| 5 | `get_logs()` / `export_logs_csv()` duplicate query logic | ✅ `_build_log_filters` shared |
| 6 | Duplicate Ollama command dispatch | ⏸️ still open |
| 7 | `import csv`/`StringIO` inside a route | ✅ moved to module top |
| 8 | Legacy no-op `save_notes_async`/`save_notes_sync` | ⏸️ low priority |

### P3 — Style & polish

| # | Issue | Status |
|---|-------|--------|
| 1 | `print()` used as logging | ✅ replaced with `logging` |
| 2 | Type hints in `app.py` | ✅ added to storage layer + common |
| 3 | Generic `raise Exception` in providers | ✅ `ProviderError` |
| 4 | No tests | ✅ 1,810-line regression suite + CI on 3 platforms |
| 5 | Unpinned deps | ⏸️ partially pinned (see above) |
