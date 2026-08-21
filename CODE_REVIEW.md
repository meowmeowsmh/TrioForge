# TrioForge — Code Review

A prioritized review of the current codebase. Line numbers refer to the code as of
this review (before the accompanying fixes). Each item is tagged with its impact and
the recommended, "small & safe" fix.

> Scale: `app.py` ~6,076 lines, `notes.py` ~4,645, `cork_board.py` ~5,025,
> `llm_providers.py` ~915, plus small helper scripts.

## Progress

- ✅ **P0 fixed** — items 1–6 below are done and verified (`python -m py_compile` on all modules, full `import app` succeeds):
  1. Image viewer now reads real messages via `get_messages`.
  2. Dead `ts` parameter removed from `add_message` and all callers.
  3. `log_message_to_sqlite` removed.
  4. `strip_c_comments` (app.py copy) removed.
  5. `import yaml` removed from notes.py.
  6. `import random` removed from notes.py.
- ✅ **Exception hygiene (P1 #6)** — all 15 bare `except:` clauses converted to `except Exception:` across `app.py` and `notes.py` (verified: zero remaining, all modules compile).
- ✅ **Logging (P3 #1)** — all 63 `print()` diagnostics replaced with `logging` (`INFO`/`WARNING`/`ERROR` levels, timestamps, module names, ASCII-safe messages). `logging.basicConfig` added in `app.py` and `gunicorn_conf.py`; every module now uses `logger = logging.getLogger(__name__)`. Verified: zero `print(` remain, full `import app` logs cleanly. Also normalized line endings to LF across all Python files.
- ✅ **Security/robustness (P1 #1, #2, #5, #8)**:
  - **#1** — `MAX_CONTENT_LENGTH` (25 MB) caps every request body, plus a hardened `_write_attachment` with a 25 MB decode cap and error handling.
  - **#2** — rate limiter prunes empty IP buckets once the map exceeds 10,000 entries.
  - **#5** — added a public `DeepSeekProvider.get_status()`; `deepseek_status` no longer touches private `_default_key`/`_get_headers`.
  - **#8** — centralized the Ollama URL in one `OLLAMA_BASE_URL` constant (env-overridable); replaced 13 hardcoded `127.0.0.1:11434` call sites; `OllamaProvider` now resolves the env var itself.
- ✅ **Dedupe (P2 #2)** — extracted shared JSON/`get_conn`/embedding helpers into [`common.py`](D:\TrioForge\common.py); `notes.py` and `cork_board.py` now import `json_dumps`/`json_loads`, a thread-local `get_conn`, and the lazy embedding model from it instead of duplicating them.
- ✅ **Monolith split — HTML (P2 #1)** — moved the ~4,500-line inline HTML/JS/CSS out of `app.py` into [`templates/index.html`](D:\TrioForge\templates\index.html); `build_html()` now reads the file. `app.py` shrank from ~6,069 to ~1,532 lines. Verified byte-for-byte: `build_html()` output MD5 matches the pre-split baseline (`69f76a4b…`), and all 55 routes still register.
- ✅ **Type hints (P3 #2)** — added type hints to [`common.py`](D:\TrioForge\common.py) (`json_dumps`/`json_loads`/`get_conn`/`get_embedder`/`embed_text`) and to `app.py`'s conversation storage layer (`load_conversations`, `get_sorted_conversations`, `save_conversations_async`, `create_conversation`, `get_conversation`, `get_messages`, `add_message`, `delete_conversation`, `clear_conversation_messages`). `llm_providers.py` and parts of `notes.py` already carried hints.
- ✅ **Further de-monolith (P2 #4, #5, #7)** — extracted shared route helpers in `app.py`: `_run_web_search`, `_build_final_prompt`, `_build_messages` (now used by both `chat` and `chat_stream`), and `_build_log_filters` (now shared by `get_logs` and `export_logs_csv`). Moved `import csv` / `StringIO` to the top of the module.
- ✅ **Provider exceptions (P3 #3)** — added `ProviderError(Exception)` in `llm_providers.py` and converted all 27 generic `raise Exception(...)` calls to `raise ProviderError(...)` (callers still catch `Exception`, so behavior is unchanged).

### Remaining (optional follow-ups, not required for the core cleanup)

- **CSP / CSRF headers (P1 #3, #4)** — deferred because they need frontend testing to avoid breaking the UI (the page uses inline scripts/styles and remote CDNs).
- **`handle_ollama_command_stream` vs `execute_ollama_command_sync` (P2 #6)** — still share command parsing that could be consolidated; lower value.
- **Tests (P3 #4)** — no test suite yet; recommend `pytest` smoke tests for `common` and the storage layer.

---

## P0 — Bugs & dead code (fix first)

| # | File:Line | Issue | Impact | Fix |
|---|-----------|-------|--------|-----|
| 1 | `zoompicleftandright.py:23` | `get_conversation_images()` reads `conv.get('messages')`, but messages were migrated **out** of `conversations.json` into SQLite and the `messages` key is stripped. The image viewer always returns `[]`. | Viewer is broken for every conversation | Pass `get_messages` into `setup_viewer()` and iterate real messages |
| 2 | `app.py:407` | `add_message(..., ts=None)` accepts `ts`, computes a default, but **never writes it** — the INSERT hardcodes `datetime.now().isoformat()`. Every caller builds a `ts` string pointlessly. | Dead parameter; misleading | Remove the `ts` param and the `ts = datetime.now().strftime("%H:%M")` lines at call sites |
| 3 | `app.py:195` | `log_message_to_sqlite()` is never called anywhere. | Dead code | Remove |
| 4 | `app.py:467` | `strip_c_comments()` is never called — it duplicates `_strip_c_comments_generic()` in `llm_providers.py:149`. | Dead code | Remove; keep the `llm_providers` version |
| 5 | `notes.py:54` | `import yaml` is unused (frontmatter does the actual YAML/frontmatter parsing). | Unused import | Remove |
| 6 | `notes.py:13` | `import random` is unused. | Unused import | Remove |

---

## P1 — Security & robustness

| # | File:Line | Issue | Fix |
|---|-----------|-------|-----|
| 1 | `app.py:271` | `_save_attachment_to_disk_async()` writes `base64.b64decode(b64_data)` to disk with **no size cap**. A huge paste/upload fills the disk. | Enforce a max attachment size (e.g. 25 MB) and reject/truncate beyond it |
| 2 | `app.py:70` | Rate limiter trusts `X-Forwarded-For` and `_rate_limit_buckets` **never prunes old IPs** — the dict grows forever and the header is spoofable when not behind a trusted proxy. | Prune stale buckets; only trust `X-Forwarded-For` if explicitly enabled |
| 3 | `app.py:92` | Security headers set `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` but **no Content-Security-Policy**. User Markdown is rendered to HTML (DOMPurify client-side); CSP is missing defense-in-depth. | Add a CSP that allows the app's inline + CDN assets |
| 4 | app-wide | State-changing POST/PUT/DELETE routes have **no CSRF protection**; app binds `0.0.0.0`. Lower risk for a local tool, higher once exposed on a LAN. | Document, or add a same-site check / CSRF token if it will be shared |
| 5 | `app.py:5407` | `deepseek_status()` reaches into private `provider._default_key` / `provider._get_headers()`. | Add a public `get_status()` method on the provider |
| 6 | 15 sites | Bare `except:` silently swallows every exception, hiding real bugs (e.g. `app.py:62,155,256,402,523,5250,5288,5299,5326,5339,5366,5382,5416`; `notes.py:359,468`). | Replace with `except Exception:` (and log) or the specific exception type |
| 7 | `llm_providers.py:82` | `_clean_api_key()` raises a bare `Exception`. | Raise a specific `ValueError`/custom error |
| 8 | `app.py:5255` | `OLLAMA_BASE_URL` is read but the literal `http://127.0.0.1:11434` is hardcoded in ~8 places (`describe_image_with_llava`, `execute_ollama_command_sync`, `unload_model`, `set_model`, `cached_vision_check`, `prewarm_vision_cache`, `chat_stream`). Setting the env var only half-works. | Centralize the Ollama URL in one constant and use it everywhere |

---

## P2 — Duplication & structure

| # | Location | Issue | Fix |
|---|----------|-------|-----|
| 1 | `app.py:699-5240` | `build_html()` is a **~4,500-line inline HTML/CSS/JS string** inside Python. | Move to `templates/index.html` (Jinja) or a static file; the function becomes `render_template(...)` |
| 2 | `notes.py` vs `cork_board.py` | Large copy-pasted blocks: `get_conn()` (`notes.py:76` / `cork_board.py:65`), the orjson `json_dumps`/`json_loads` shim (`notes.py:20` / `cork_board.py:20` / `app.py:24`), the `EMBED_AVAILABLE` setup (`notes.py:33` / `cork_board.py:33`), `get_embedder()` (`notes.py:376` / `cork_board.py:296`), `embed_note`/`embed_pin` (`notes.py:385` / `cork_board.py:306`), `DB_DIR`/`DB_PATH` setup (`notes.py:61` / `cork_board.py:57`), the `update_*_fields` partial-update pattern. | Extract a shared `common.py` (json helpers + sqlite helpers + embedding loader) |
| 3 | `app.py:118` vs `notes.py:76` | **Two different SQLite strategies**: `app.py` uses one shared connection + a lock (`check_same_thread=False`), while notes/corkboard use per-thread connections. Inconsistent and the shared-connection approach is fragile under concurrency. | Standardize on the thread-local connection helper |
| 4 | `app.py:5667` & `5795` | `chat()` and `chat_stream()` duplicate ~100 lines of prompt building (web search, file extraction, history assembly). | Extract a `build_messages(...)` helper |
| 5 | `app.py:5586` & `5634` | `get_logs()` and `export_logs_csv()` duplicate the same query-building. | Extract `_build_log_query(...)` |
| 6 | `app.py:537` & `613` | `execute_ollama_command_sync()` and `handle_ollama_command_stream()` duplicate the command dispatch. | Share a single command parser |
| 7 | `app.py:5655` | `import csv` and `from io import StringIO` inside a route function. | Move imports to the top of the module |
| 8 | `notes.py:364` | `save_notes_async`/`save_notes_sync` are "legacy" no-op wrappers. | Remove and update callers |

---

## P3 — Style & polish

| # | Issue | Fix |
|---|-------|-----|
| 1 | ~70 `print()` calls are used as logging across all modules. No levels, timestamps, or redirect. | Add `logging.getLogger(__name__)` and a central `basicConfig`; replace prints |
| 2 | Type hints exist in `llm_providers.py` and parts of `notes.py` but **none in `app.py`**. | Add hints to public functions incrementally |
| 3 | Generic `raise Exception(...)` in `llm_providers.py` for provider errors. | Define `ProviderError(Exception)` subclasses |
| 4 | No tests at all. | Add `pytest` smoke tests for the storage + provider layers first |
| 5 | `requirements.txt` pins only `waitress`/`gunicorn`; everything else floats. | Pin or use `>=`/`<` ranges consistently |

---

## Recommended execution order ("small & safe")

1. **P0 fixes** — one small verified batch each; behavior-preserving.
2. **P1 #6/#7 (exception hygiene)** — narrowest, most mechanical robustness win.
3. **P3 #1 (logging)** — replace `print()` after standing up a shared logger.
4. **P2 #2 (dedupe)** — extract `common.py`; rewire notes + corkboard, verify both pages.
5. **P2 #1 + #4-#6 (de-monolith)** — move HTML to a template, then split `app.py` into `config`, `storage`, and route modules.
6. **P1 #1-#5, #8 (security)** — size caps, CSP, URL centralization, provider status API.

Each step keeps the app importable/runnable and is verified before the next begins.
