# Changelog

All notable changes to TrioForge, newest first. This file is also the body of each
GitHub release (a workflow publishes it whenever a `v*` tag is pushed).

## [1.5.6] — the final release

**This is the final TrioForge release and the one to install.** It supersedes
1.5.5: that tag was cut before the security, terminal-client and performance work
below, so it carries the same version number on a build that is missing all of it.

### Security

- **CSRF protection.** A per-session token is issued at `GET /api/csrf` and required
  on every state-changing request, compared with `secrets.compare_digest`. This
  matters because host mode (`TRIOFORGE_PASSWORD`) puts the app on a network, where
  another site could otherwise POST with the browser's cookie. Exempt on purpose:
  `/v1/` (API-key callers have no session), `/login` and `/logout` (no session
  exists yet), `/api/ping` and `/api/csrf`. `index.html` wraps `window.fetch` once,
  so all ~58 call sites carry the token without being edited — and `sendBeacon`,
  which cannot set a header, sends it in its form body instead.
- **Dependency pinning.** Every floor is now the release this project is tested
  against and carries an upper bound (`flask>=3.1.3,<4.0.0`), so a fresh install
  cannot resolve to a major version nobody has run. `waitress`/`gunicorn` stay
  exactly pinned.
- **Eleven vulnerability alerts cleared.** `uv.lock` still resolved `urllib3` 2.7.0,
  `pypdf` 6.17.0 and `werkzeug` 3.1.8 — three packages carrying 11 Dependabot
  advisories (9 high, 2 moderate) — while the venv already ran fixed versions, so
  the lock was the only thing stale. It now resolves `urllib3` 2.8.0, `pypdf` 6.20.1
  and `werkzeug` 3.1.9, and `requirements.txt` states explicit floors for the two
  transitive ones so a fresh install cannot drift back below the patched releases.
- **Dead code.** `save_notes_async` / `save_notes_sync` had no callers for several
  releases and are gone; the backup functions they forwarded to are still used.

### The terminal client

- **`/roles` — a working team instead of a crowd.** A senior/junior pair runs one
  task as a pipeline: the **senior** plans and reviews (`planner`, `reviewer`), the
  **junior** builds and fixes (`coder`, `fixer`), and each job is handed the
  previous one's output as an artefact. The reviewer ships (`SHIP`) or lists
  concrete `ISSUES:`, and the fixer only runs when there are issues to fix. The
  senior takes the strongest model in the pool, the junior the fast local one.
  `/team` remains the other shape: every model on the same task, first answer ships.
- **Slash autocomplete.** Typing a bare `/` drops a filtered command list above the
  prompt — command on the left, what it does on the right. `↑`/`↓` pick, `tab`
  fills the name in, `enter` runs the highlighted command, `esc` closes it for that
  text. A finished command keeps its row, selected: hiding it made the list vanish
  at the moment you finished typing, and `/set` is a prefix of `/setup`, so the row
  you typed is selected explicitly rather than trusting list order.
- **Typing no longer stalls.** The sidebar's GPU reads were freezing the UI thread.
  `gpu_live()` re-initialised NVML on every read (~0.7 s warm, 2.5 s cold on
  Windows) and fell back to spawning `nvidia-smi` (~750 ms) whenever the driver
  refused the utilisation query; `_probe_gpu()` (which spawns
  `llama-server --list-devices`, 1.4–3.3 s) was called inline every 5 s by a panel
  that redraws every 2 s. NVML now stays initialised for the process, a failed
  utilisation read keeps the memory figures, and a stale GPU cache is returned
  as-is while a worker thread re-probes. Worst-case keystroke latency over 14 s of
  continuous typing went from **1655 ms to 159 ms**.
- **`/help` and the palette read one table**, so `/team`, `/auto`, `/route` and
  `/roles` cannot exist without being listed.
- **The sidebar fits its width, and steps aside when there is none.** The panel is a
  fixed 36 columns, so a long `.gguf` name used to WRAP mid-word —
  `gemma-4-E4B-it-ultra-uncensored-heretic` drew as `…ultra-uncensored-h` / `eretic`
  and pushed its own `5.4 GB` row out of line. Every value is truncated to the
  panel's 34 text columns now, measured in terminal **cells** rather than
  characters, because an emoji like 💻 occupies two columns and how wide one draws
  varies by terminal. `_short_model()` also splits on `\` as well as `/`: on Windows
  a model is `D:\…\name.gguf`, and splitting on `/` alone left the whole path on one
  row. Below 80 columns the panel hides so the transcript gets the full width — at
  60 it used to keep its 36 and leave the chat 24, at 50 it left 14. The resize
  handler reads the event's size rather than the widget's, which was still the old
  one, so the panel used to change one resize late and never come back.
- **The start-up timer no longer races the first paint.** `_tick_clock` runs 250 ms
  after mount and Textual mounts a screen's children asynchronously, so on a slow
  machine the first tick could land before `#face` / `#activity` / `#sidebody`
  existed — and `_paint_face`'s unguarded `query_one` raised `NoMatches` out of the
  timer, which surfaces as an app error at start-up. All three timer-driven paints
  now go through one guarded helper that simply retries on the next tick, and only
  records what it drew once the paint actually landed.

### Image quality: why local images looked blurry

- **The workflow's own negative prompt survives.** FHDR-Setup ships a positive
  (`nude women`) and a negative (`sex in bed`); the app replaced the positive and
  also blanked the negative, so the same workflow produced a different picture
  here than in ComfyUI, where the authored negative still applies. Nearly
  invisible on a cfg=1 Flux workflow, but it changes the output of anything with
  a real negative. An explicit `negative=` still overrides, and `""` still clears.
- **Reproducible seeds are now a choice, because the memory fix costs them.**
  Measured: two byte-identical requests (same prompt, seed, steps, size) to a
  server running with `--cache-none` gave two *different* images, while the same
  two requests to a server without it gave byte-identical files — unloading the
  models between runs is what breaks it. `--cache-none` is what fixed the "RAM
  stays stuck after a prompt" report, so it stays the default;
  `TRIOFORGE_COMFYUI_CACHE=ram` keeps models loaded instead and gets
  reproducibility back, at the cost of that RAM.
- **Auto-detect runs the workflow you saved, not the fastest shipped one.** The
  default was Z-Image-Turbo purely because its name says "turbo", so a workflow
  the user saved themselves — here FHDR-Setup, the uncensored fine-tune — was
  never selected and its pictures never appeared, which reads as "the app ignores
  my prompt". A saved workflow is an explicit choice; a blueprint is only what
  ComfyUI happened to ship, so the user's own wins, with audio/video workflows
  skipped so an image request cannot land on one. The same rule now picks the
  user's Stable Audio workflow ahead of the bundled ACE-Step for audio. An
  explicit pick in either panel still overrides everything.
- **The image dropdown offers the whole install, not two entries.** It held only
  Z-Image-Turbo and FHDR while the ComfyUI install it drives also has
  Z-Image-Base, Qwen-Image, Flux.1/2 Dev, Krea, Anima, Ernie and more — so the app
  looked like it had fewer models than ComfyUI itself. Every ready
  `Text to Image …` workflow is listed now (19 here), fast default first, and one
  whose model files are missing is still left out so picking it cannot fail.
- **The seed is named under every image, and can be set.** The app re-rolled the
  seed silently on every run, so a picture you liked could never be asked for
  again and there was no way to hold one variable still while changing the prompt.
  The caption now reads `… · Text to Image (Z-Image-Base) · seed 802384471`, and
  the panel has a Seed box (blank = new random seed each time). Exact
  reproduction additionally needs `TRIOFORGE_COMFYUI_CACHE=ram`, because a seed
  only repeats while the models stay loaded.
- **The Steps box no longer overrides the workflow's own step count.** The image
  panel sent its own default (4) for *every* workflow, and 4 steps is
  under-denoised for anything that is not a 4-step distilled model. Measured with
  the same prompt, seed and 1024×1024 size, FHDR-Setup — which is tuned for 20
  steps — produced a soft, featureless mush at 4 and a crisp, detailed portrait at
  20, reproduced on two seeds; Z-Image-Turbo is tuned for 8. Nothing about this was
  visible in ComfyUI, which uses each workflow's own value, so the same workflow
  looked right there and wrong here. An empty Steps box now means "use the
  workflow's", and only a number you type overrides it (video steps likewise).
- **Generated images are shown at their real size.** `.msg img` caps
  `max-height` at 240px for attachment thumbnails, and the inline
  `max-width:100%` on a generated image could not undo it — so a 1024×1024 render
  was *displayed* 240×240 and read as blurry beside the identical file shown full
  size in ComfyUI. Generated images now get their own rule (up to 78vh) and stay
  click-to-open-full-size.

### The 🗄️ restore list can be cleared in one go

- **Every delete archives a copy**, so trying one prompt repeatedly leaves a wall of
  near-identical rows — 129 of them on the machine this was written on, 107 titled
  the same, 56 deleted in a single day. One row at a time is not a usable clean-up.
  Each row now has a checkbox, each section (Conversations / Notes / Pins) has its
  own select-all, and there is a live "N selected" count with **Restore selected** /
  **Delete selected** buttons that stay disabled until something is picked. The bulk
  action asks first and names how many items it will affect, and it runs the writes
  **sequentially** — firing dozens at once is how SQLite starts refusing them. The
  per-row Restore/✕ still work exactly as before.
- Verified in real headless Chrome against a live app, not just in source: 129 rows,
  select-all ticked every one, the count read "129 selected", both buttons armed, one
  row read "1 selected", a destructive click asked first, and refusing the confirm
  deleted nothing. `tools/restore-bulk-check.js` repeats that check and cancels every
  confirm, so it can be run any time without touching real archives.

### Local model: a hard deadline, so it can never hang for half an hour

- **Requests to the local model now have a real wall-clock budget.** `requests`'
  timeout is *per socket read*, not a total: a server that trickles the body — or a
  machine that starts swapping mid-answer with a 32K context — resets it forever, so
  a generation could sit "half way" for half an hour while every individual gap
  looked healthy. The call now runs in a worker and the caller waits at most
  `TRIOFORGE_LLAMA_TIMEOUT` (default 300 s), then reports a plain sentence. Two
  things had to be right: checking the clock between chunks is useless because the
  read blocks, and closing the response from another thread does not unblock a
  Windows socket — while closing it on the caller's path *blocks until the body
  finishes*, which put the wait straight back. The hang-up therefore happens on its
  own thread.
- **"Lost the connection" says what it means.** A stream that dies mid-answer used
  to surface as a raw `ChunkedEncodingError` traceback; it now reads as a lost
  connection with the likely cause (the server stopped, or ran out of memory).

### Local model: "timed out" was a long answer, not a broken server

- **The reply is now capped so it can finish, and the wait is tunable.** The local
  provider allows 4096 output tokens, but a 12B model split across an 8 GB card and
  the CPU writes roughly 5 tokens a second — only ~1500 tokens fit in the 300 s
  request timeout, so anything longer died as *"llama.cpp server timed out. Try
  reducing context size or use a smaller model."* That blamed the context and said
  nothing about the actual cause. `TRIOFORGE_MAX_TOKENS` caps the reply (unset =
  unchanged) and `TRIOFORGE_LLAMA_TIMEOUT` sets the wait; the error now names both,
  plus the context lever. A junk or non-positive timeout falls back to 300 s —
  a negative one fails every request instantly and looks exactly like a dead server.

### Small quality-of-life

- **🧊 Free memory, and the numbers to watch.** The top bar shows live RAM and VRAM
  use, and one button unloads the model and stops ComfyUI — the thing a 15 GB
  machine needs after a heavy session, and everything it frees returns on the next
  request. It reports only what *actually* stopped, never "freed ComfyUI" when
  nothing was running.
- **ComfyUI now unloads itself after idling.** `--cache-none` freed the models but
  the server process stayed resident (~2 GB). It now stops after 5 minutes without a
  generation (`TRIOFORGE_COMFYUI_IDLE_UNLOAD` to retime or disable), and only a
  server TrioForge started — one Comfy Desktop opened itself is never touched.
- **Every message is copyable.** Yours and the bot's both get a copy button; a bot
  message copies its *rendered* text, not the `**markdown**` syntax, and the button
  appears on hover like edit/delete.
- **A half-typed message survives a reload.** The chat draft is saved as you type,
  restored when you return, and cleared the moment you send — so a refresh cannot
  resurrect something you already sent.

### Daily briefing (opt-in plugin)

- **One corkboard pin a day** with the next 24 hours of Google Calendar, your unread
  Gmail, and a few of your own idle pins worth a second look. Sources appear only when
  they are connected, so a fresh install still gets a useful pin, and a model is
  optional — if it is down the plain pin is written anyway. One pin per day, rewritten
  in place (where you moved it stays put), and only briefings you never touched are
  pruned. It ships **disabled**: a leading underscore keeps it from loading, so nothing
  changes until you switch it on.
- **It has a panel now, not just an API.** The plugin shipped with three routes and
  nothing in the UI calling them, so the feature was unreachable without `curl`. The
  ☀️ top-bar button opens a panel that says whether today's pin exists, which sources
  are connected and what the last error was, sets **Auto** and the **hour**, and has
  **Write it now**. Because it ships off, "not enabled" is a normal state there — the
  panel says so and offers a one-click **Enable it** instead of looking broken.

### Discord connector — a card in the app, the bot in its own process

- **The bot does not run inside TrioForge.** Plugins are trusted code in the app
  process, and the app's own docs say a crashing plugin "crashes the app" — so the
  one component you should expect to break (a chat bot: rate limits, dead tokens,
  stuck sockets) is not implemented as one. `plugins/discord.py` is a shim: it
  spawns `bridge.py` as a separate process with a hard timeout and turns every
  failure into a message. A crash or a hang out there cannot take the app down, and
  a test pins that (no network client in-process, the child call is bounded, the
  token is never read, a missing bridge is reported rather than raised).
- **No Discord secret enters the app.** The card asks for the bridge folder only; the
  bot token stays in the bridge's own `.env`, so there is nothing to leak, nothing to
  include in a backup, and nothing for TrioForge to revoke — "Disconnect" says so
  plainly instead of pretending it signed something out.
- The bridge posts only to allow-listed channels, is rate-capped, marks existing
  messages as seen so it never answers your history back at you, and keeps inbound
  **off** unless `ALLOW_INBOUND=1` *and* a user id is whitelisted — the combination
  that otherwise lets anyone who can message the bot drive your model.

### ComfyUI

- **ComfyUI Desktop's models are found without opening ComfyUI.** Desktop does not
  keep models in the install: it keeps them in one shared store
  (`ComfyUI-Shared/models`, 151 GB here) and points every ComfyUI instance at it
  with `--extra-model-paths-config`. TrioForge's own headless start did not pass
  that file, so the server it spawned saw only the install's `models/` folder —
  which holds nothing but `put_*_here` placeholders — and reported **every**
  installed model as missing. It now passes the config through, verified by
  starting a server exactly as the app does and finding all six models, with both
  shipped workflows `ready=True`. This was the real reason the app "needed ComfyUI
  open to work", and why ~30 GB was re-downloaded into a second location beside
  copies that had been installed since June.
- **Missing models are fetched on demand, by ComfyUI's own downloader.** ComfyUI
  Desktop bundles ComfyUI-Manager, and its v4 API is now driven directly: the task
  is queued (`/v2/manager/queue/install_model`), started, and polled to completion,
  so the download, its progress in ComfyUI and the final placement are all
  ComfyUI's. Only the files the **selected** workflow names are fetched, and only
  when it is used — nothing is downloaded ahead of time. Verified live: 234.7 MB
  `clip_l.safetensors` fetched in 10.1 s, `result: success`. The server TrioForge
  starts itself now passes `--enable-manager` too, so those endpoints exist even
  when the app is the one that started the engine — without the manager package
  ComfyUI logs a warning and starts anyway, so nothing breaks for an install that
  has none.
- **The URL comes from the workflow when it has one.** A node may carry
  `properties.models = [{name, url, directory}]`; that is the source ComfyUI's
  frontend uses for its own "download missing models", and it is now read first
  (nested subgraphs included), with `MODEL_SOURCES` as the fallback for workflows
  that declare nothing — `FHDR-Setup.json` declares none of its four, which is
  exactly why nothing ever offered to fetch them. A file with no source at all is
  reported, never guessed at.
- **A missing model says so, by name.** `fetch_needed_models()` reports which file
  is missing, who tried to fetch it and what they answered, instead of letting the
  run fail later with ComfyUI's opaque `400 Prompt outputs failed validation`.
- **A download is not duplicated.** Model destinations prefer Desktop's shared
  store, and the completion check looks in every folder ComfyUI reads
  (install `models/`, the shared store, each Desktop base path) — checking one of
  them reported a finished download as a failure, which invited a second copy.
  Without a running Manager, direct fetching still needs an explicit
  `TRIOFORGE_AUTO_MODELS=1`, so a bare install can never silently eat tens of GB.

### Install and packaging

- **Python 3.12 is now the floor** (was 3.10). 3.10 reached end-of-life and 3.11 is
  in security-only maintenance, so neither is supported any more: `pyproject.toml`
  and `uv.lock` declare `requires-python >=3.12`, `run.sh`, `deps.sh` and
  `install.ps1` refuse anything older up front with a message that says so, and the
  CI matrix tests **3.12, 3.13 and 3.14** instead of 3.10/3.12/3.14. 3.12 was
  already what the Docker image shipped.
- **The dead version branches are gone.** Both archive extractors guarded
  `tarfile.extractall(..., filter="data")` behind `sys.version_info >= (3, 12)` and
  fell back to an unfiltered `extractall` below it — now that 3.12 is required, the
  safe extraction is the only path, so the fallback (which allowed absolute paths
  and `..` members) no longer exists. `cork_board.py` drops its
  `value.replace('Z', '+00:00')` shim for pre-3.11 `fromisoformat`, which also
  mangled any string that merely contained a `Z`.
- **`trioforge-web` exists on Linux/macOS**, and `run.sh` resolves symlinks, so the
  one-liner works when it is invoked through a link.
- **The `models/` guidance files are untracked** rather than committed inside a
  gitignored folder, so they stay on disk without showing up as changes.

Still open, deliberately: **Content-Security-Policy** (needs frontend testing
because the pages use inline scripts) and the **Ollama command-dispatch
consolidation** (a pure refactor).

## [1.5.5] — the last release: no console windows, and one command from anywhere

**This is the final TrioForge release.** It is the one to install.

### Windows stops opening console windows

Every child TrioForge starts is a console program, and Windows gives a console program
a brand-new console window when its parent has no console of its own. That was the
"a cmd window pops up twice whenever I open the app" report, and it had several causes
at once:

- **MCP servers.** `cmd /c npx.CMD …` was hidden with `CREATE_NO_WINDOW`, which hands
  the child *no console at all* — so when npm later ran its own
  `cmd /d /s /c <bin>` shim, that cmd had nothing to inherit and allocated a visible
  window of its own. MCP servers now get **one console for the whole tree, created
  hidden**, so every descendant inherits it instead of making its own.
- **The voice agent** started llama-server and the speech-to-speech agent with no
  window flag at all; both are hidden now.
- **The dependency upgrade and the background update check** run with their window
  hidden and their output swallowed. They are background work, not something to watch.
- **Double-clicking the launcher** no longer goes through a PowerShell relaunch that
  flashed on its own: it hands straight to the windowless `pythonw`.

### `--quit`, and a window that opens

- **`trioforge-web --quit`** stops the running server *and* the whole process tree it
  started — llama.cpp, the voice agent, the MCP servers — instead of leaving you to
  hunt through Task Manager.
- **`--window` works again.** The WebView2 window needs `pywebview`, which only the
  control panel ever installed on demand, so `app_window.py` exited with "pywebview is
  not installed" and nothing appeared. The launcher now installs it itself (hidden, one
  time, ~1 MB) before opening the window.

### One command, from anywhere

- The web launcher is now **`TrioForge-Web.bat`**. As `TrioForge.bat` its name was the
  same letters as `trioforge` to a case-insensitive filesystem, so typing `trioforge`
  resolved to the *web* launcher. Renaming it is what lets `trioforge` be a real
  command, and you now get three — **`forge`** and **`trioforge`** (terminal client)
  and **`trioforge-web`** (web app) — from any directory.
- **The one-line installers** (`bootstrap.sh` / `bootstrap.ps1`) download the repository
  archive instead of cloning it, so git is no longer required, and they put those
  commands on your PATH.

### A better first five minutes

- **`forge --help`** now teaches setup: which provider to pick, where each API key comes
  from, the Docker one-liner, and the config file — instead of only listing flags.
- **⌨️ Help & shortcuts** (Ctrl+/) is a themed panel with a short getting-started guide,
  key caps and quitting/troubleshooting notes, and it follows the app's palette.

### Under the hood

- `py/app.py` lost ~270 lines: the plugins/skills/MCP/extensions/connectors routes are a
  blueprint (`py/features/extensions_api.py`) and port picking moved to
  `py/port_util.py`. The route table is unchanged (186 routes, verified identical).
- One shared helper (`py/proc_util.py`) owns console hiding and process-tree cleanup,
  replacing two near-identically named modules plus a private third copy.
- `huggingface-hub` is capped below 2.0, because the optional semantic stack
  (`sentence-transformers`) pins `<2` — without the cap the launch-time auto-upgrade
  would quietly replace it and break embeddings.

## [1.5.4] — lazy imports, declared for Python 3.15

### Lazy imports, declared for the day Python 3.15 lands

`lazy import x` is [PEP 810](https://peps.python.org/pep-0810/) and arrives in
**Python 3.15**. On 3.12–3.14 that statement is a `SyntaxError`, so it cannot be
used yet: putting it in front of the imports would stop the app from starting at
all, on every interpreter the project supports.

The forward-compatible half of the PEP works today and is now used. Seven modules
declare

```python
__lazy_modules__ = ["requests", "psutil"]
```

which is an ordinary, unread module-level name below 3.15 — the imports stay eager
and exactly nothing changes — and turns those imports lazy automatically on 3.15+.

The two named are the ones `-X importtime` shows are heavy (~22 ms for `requests`)
and are **not** needed to build the app object. Flask and the blueprints are used
at import time (`app = Flask(...)`, `register_blueprint(...)`), so they stay
deliberately eager: a lazy import that is consumed immediately buys nothing.

## [1.5.3] — the newest Python, and a faster start

### Startup is quicker again

`pynvml`, `duckduckgo-search` and `frontmatter` are imported when they are first
needed rather than at import time, so launching the app no longer pays for the GPU
library and the search client before it has drawn anything.

### The launcher uses the newest Python you have

`./run.sh` used to take whatever `python3` resolved to — so a machine with 3.14
installed still built its venv on the distro's 3.12 alias, and the newest
interpreter sat unused. It now:

- picks the **newest `python3.X` on PATH** (3.16 → 3.10, listed ahead of time so a
  future release becomes the default the day it appears);
- honours **`TRIOFORGE_PYTHON=python3.12 ./run.sh`** to force a specific one;
- **enforces the declared floor** (`requires-python >=3.10`), failing immediately
  with a message that names Python — instead of a confusing error later, inside a
  dependency;
- and, because an existing venv keeps its own interpreter, prints the one command
  that rebuilds it when a newer one is available.

Verified on 3.14.8: the whole suite passes and the app boots with all 186 routes.

### CI: the Python range is proven, not claimed

A new **Python version matrix** job runs the regression suite on **3.10, 3.12 and
3.14** — so `requires-python >=3.10` is something CI checks rather than a hope.
Every GitHub Action was also moved off the deprecated Node 20 runtime
(`checkout`/`upload-artifact` v7, `docker/*` v4/v7, `action-gh-release` v3).

### Launcher

`--background-update` only worked together with `--detach`; `run.sh` now passes
both, so the flag does what its name says.

## [1.5.2] — the terminal can see images

### The terminal reads images by path

A terminal has no drag-and-drop, so the terminal client takes the other route: **a
path in your message IS the attachment.** Any token that resolves to an image file
is sent as an `image_url` part, and naming a folder attaches the images inside it.

```
what is in ~/Pictures/cat.jpg?
describe ./screenshots
```

Every token is tested as a path, so `~/a.png`, `./a.png`, `shots/a.png`, a bare
`a.png`, a folder, and a quoted `"My Pictures/a.png"` all work. Forge also warns
when the loaded model has no vision projector, instead of letting a text-only GGUF
silently ignore the image and answer confidently about nothing.

Two bugs found while using it, both fixed:

- **an absolute path was eaten as a command.** Pasting
  `/home/tc/Pictures/shot.png` was answered with "unknown command — try /help", so
  the image was never seen. A known command still wins; an unknown word that names
  an existing path is now a message.
- **`view` on an image made the model invent one.** Read as text an image is pixel
  noise, and a model asked to make sense of noise answers anyway — a screenshot
  became "it appears to be an image of a cat". `view` now refuses an image outright
  and says not to guess.

### Gemma rejected whole turns with HTTP 400

```
Jinja Exception: Conversation roles must alternate user/assistant/user/assistant/…
```

llama.cpp's chat template for gemma-3 **requires** strict alternation, and the agent
loop could emit two `user` turns in a row: a round with two tool calls appends two
tool-result user messages, and the anti-stall nudge lands directly after tool
results. The model never saw the request. Consecutive user turns are now merged into
one (content preserved, images stay image *parts*), and a sliding window that begins
mid-pair drops the orphaned answer, so a turn can no longer die on the way out.

## [1.5.1] — a calendar that does things, and a model switch that sticks

### Google Calendar can change your calendar now, not only read it

The connector had one tool (`calendar_list_events`); it has six:

- `calendar_create_event` — create an event (title + start; end defaults to +1 hour)
- `calendar_quick_add` — one natural line, e.g. *"Lunch with Sam tomorrow at noon"*;
  Google parses the time itself
- `calendar_read_event` / `calendar_delete_event` — by id, from a listing
- `calendar_list_events` and `calendar_status` — as before

The scope is now `calendar.events` (read **and** write events), so a connector that
was signed in before this release needs one more sign-in.

### One bad line caused two confusing symptoms

The status check called `calendars/primary`, which the `calendar.events` scope does
**not** permit. That single call produced both:

- a bogus `403 insufficient authentication scopes` on the card, and
- **"this sign-in link was already used"** *after a successful sign-in* — the OAuth
  callback asks the status "are we connected now?", and that check was failing.

It now verifies with an events call, which the scope does allow.

### The Connectors panel grew up

- **🔑 Enable <API>** — one click to the Google Cloud page that turns the API on
- **🌐 OAuth consent screen** — straight to Test users / Publish (private ↔ public)
- **✏️ Change keys** — re-enter the Client ID / Secret at any time, signed in or not.
  Before this the fields vanished once anything was saved, so a typo was unfixable
- **Disconnect** now appears whenever there is something to clear, not only while
  connected — so a connector set up with the wrong keys can be cleared from its card
- **🔄 Reload** — re-check every connector's status without restarting the app
- the stray "back" links are gone

### Terminal: switching the model finally switches it

Two independent bugs made `/model` look broken while auto-route was on:

- `/model`, `/start`, `/provider` and the `ctrl+l` picker never updated the **route
  pool**, and the router reads that pool on *every* send — so a stale entry silently
  reverted the switch, and the sidebar's green "active" marker never moved
- the local server was reused whenever the port was up, and llama-server **ignores**
  the request's `model` field: the old model kept answering (37 s a turn when it did
  not fit in VRAM) while the UI named the new one

Both are fixed: every switch path syncs the pool, and the server is reloaded when it
is serving a different GGUF.

### Local models tell the truth about what they can read

A model now advertises `text+image` **only when a vision projector (mmproj) is
actually paired with it**. Everything under `models/` used to claim image input, so
`qwen2.5-7b-instruct` — no projector, cannot see — looked like a vision model and an
image could be attached to it.

### Faster, and better tested

- startup is much quicker: NVML init and the llama.cpp probe are deferred
  (**3.4 s → 0.6 s**)
- CI now genuinely runs `tests/regress.py` on Linux, macOS and Windows
- added CONTRIBUTING.md and issue templates

## [1.5.0] — plugins, connectors & DeepSeek Harness

### Plugins, skills & MCP — one inventory

A full extension system, gathered in one 🧩 **Plugins** panel (and `/plugins` in the terminal):

- **plugins** — a single `.py` file in `plugins/` exposes agent tools (`TOOLS` + a
  `dispatch(name, args)` callable), plus an optional `register(app)` for HTTP routes.
- **skills** — Markdown instruction packs in `skills/`; only their name/description is
  advertised until the model opts in with `use_skill`.
- **MCP** — external servers are borrowed as tools (`mcp__server__tool`), with enable/disable.
- A browsable **catalog**, install-by-Git-URL/folder, and a failed extension is shown as
  *broken*, not silently missing.

### Connectors — Gmail, Obsidian, Google Calendar

- **Gmail** — `gmail_list_inbox` / `gmail_read_message` / `gmail_status`, through real
  "Sign in with Google" OAuth (with an App Password over IMAP fallback). Credentials stay
  local-only; disconnecting keeps the one-time Google app.
- **Obsidian** — read, search and write notes in a local vault (fully offline — just a folder path).
- **Google Calendar** — `calendar_list_events` / `calendar_status`, its own OAuth app,
  kept deliberately separate from Gmail.

### DeepSeek Harness plugin

`dsh_run` / `dsh_status` hand a whole coding task to DeepSeek Harness (`dsh headless`) and
read back the finished result. It ships **disabled by default** (enable it in the 🧩 Plugins
panel to use) and is detected in the 🚀 Setup panel — which also ignores the unrelated
Debian program that happens to be called `dsh`.

### Docker launcher: backup, restore, download, update

`docker/application.sh` gained `--backup`, `--restore <tar.gz>`, `--download "<query>"` (they
work even without Docker), and `--update` now does `git pull --ff-only` + rebuild + `up -d`.

### Local models can reach plugins — no workspace needed, no tool template needed

Two things had locked offline (llama.cpp) models out of plugins and connectors:

- the agent only ran when a **workspace folder** was set, so Gmail/Obsidian/skills/MCP
  — which don't need a folder — were unreachable until you created one. Workspace
  file tools still require a folder, but plugin/connector/skill/MCP tools are now
  offered on their own.
- local models that ignore the native `tools` parameter (gemma-3 and friends) could
  never call a tool. The web app now offers them the same fenced ```tool {…}```
  text protocol the terminal client already uses, so any GGUF can reach the
  connectors, not just models with a function-calling template.

### Links that are really controls keep their own colour

The theme stylesheet coloured every `<a>` with the theme accent (`!important`), which
also caught links used as UI controls, repainting their own colours bright pink (very
obvious in the Galaxy theme):

- link-buttons — the Setup modal's **"⬇ Download"** and the Design Studio's
  **"⬇ HTML"** (white/light labels on a blue or near-black button)
- the **Chat / Notes / Cork Board** pills — Chat is a `<button>` but Notes and Cork
  Board are `<a>`, so only those two were recoloured and the row looked inconsistent

The `a` rule now skips any link that carries its own background, or that is a
`.tab-btn`; plain text links stay accented.

### Your name above a message is white again

The theme stylesheet painted `.msg-name` with the accent, so the name you set in your
profile showed up bright pink instead of the white the message component intends (the
bot's name stays its own blue). The override is gone, and the message timestamp below
it is now readable white rather than the faint half-dim it used to be.

### Timestamps & the desktop window

- History timestamps that came back as raw ISO (`2026-09-28T00:56:07.893032`) now render
  as `Sep 28, 2026 · 12:56 AM`.
- A one-character typo had disabled the Windows single-instance guard, so a second launch
  fought the first over the port and neither painted — fixed, and covered by a regression test.
- `--window` on Linux now checks for the WebKitGTK engine up front and prints exactly what
  to install instead of dying with an opaque GTK error.

## [1.4.9] — search & download models, and Docker on Apple Silicon

### Search and download GGUF models from the terminal

- `/search <query>` lists matching Hugging Face GGUF repos and their download
  counts — browsing only, nothing is pulled.
- `/download <query>` goes one step further: pick a repo, see its `.gguf` files
  with sizes (model first, then any `mmproj` projector), and pull the one you
  choose into `models/` — the same folder the web UI's download uses.

### Docker builds on Apple Silicon and Linux x86_64

The image moved to `python:3.12-slim` (multi-arch, so one `Dockerfile` serves
arm64 and amd64), gained a healthcheck on `TRIOFORGE_PORT`, and `.dockerignore`
now keeps the host's plugins, backups, tests and screenshots out of the image.
The Docker launcher scripts are executable out of the box, the installer message
names the correct Ubuntu/Mint package (`docker-compose-v2`), and the README walks
through the whole first-time setup (including removing a stale `docker.com` repo).

## [1.4.8] — the model picker tells you what will actually fit

### The picker shows the fit before you load it

`ctrl+l`, `/models` and `/start` now badge every offline model with its fit verdict,
computed from the live hardware spec — so a model that cannot live entirely in VRAM
is flagged *before* it is loaded instead of discovered as "why is this 1 tok/s":

- `fits in VRAM` — the whole model and its cache sit on the GPU
- `split GPU+CPU (slower)` — bigger than the card, so part runs on the CPU
- `CPU only` / `too big` — no offload, or it will not fit at all

`/start` also prints a one-line warning naming the model and the reason when it is
going to split or run on CPU, so a slow reply is explained up front.

## [1.4.7] — the design studio: live preview, files, and a database that remembers

### A brief becomes a working prototype, live

The new **Design Studio** turns a one-line brief into a project and renders it live:

- **live preview + code side by side**, with the page no longer truncated mid-CSS
  (the generation budget lifted from 8k to 65k tokens)
- a **Files tab** beside Preview, showing projects as folders (`index.html` +
  `style.css` / `app.js`) instead of a flat pile of html files
- **Python projects**: a brief can produce `main.py` + modules + `requirements.txt`,
  and a **live terminal** runs them — and speaks the project's language

### Designs survive a reload (database)

Design briefs and their generated files are now written to the database, so closing
the chat no longer loses the whole design — a stored design re-renders its live
preview from history.

### Token counter and window

- the token counter shows **total tokens consumed**, and "thinking" during reasoning
  instead of a misleading `0.0 tok/s`
- the desktop window restores itself when WebView2 collapses it to a sliver

## [1.4.6] — the desktop app actually opens, and a real DeepSeek model picker

### The desktop app opens one window that shows up

Double-clicking the app used to leave nothing on screen while TrioForge processes
piled up in Task Manager. Two faults combined:

- the "a window is already open" marker was written *after* the server was spawned
  and the window created — and with a plain write — so every impatient click started
  another full window process, six servers racing for one port and six WebView2
  engines fighting over one locked profile folder
- "the window is ready" meant *the form object exists*, not *a visible window is on
  screen*; pywebview only shows the form after WebView2 finishes initialising, so
  when that wedged, every watchdog concluded the app was healthy while you stared at
  an empty desktop

The window slot is now claimed atomically at the top of start-up, readiness means a
real visible window, a lingering hidden form is asked to show itself, and a window
that never appears retries once with a fresh profile and then hands you the app in
your browser instead of lingering invisibly. The recorded hang also expires after a
day and clears when a window paints, so one bad night no longer docks the GPU
forever.

### The DeepSeek dropdown shows the actual models

The DeepSeek provider now presents its harness-style catalogue instead of raw ids:

- **DeepSeek-V41-Flash** — text + image, 1M-token context
- **DeepSeek-V4-Pro** — text, 1M-token context, with an Off / Low / High / Max
  reasoning-effort selector that rides to the API as `reasoning_effort`

An info line shows the selected model's name, context window and input modalities,
the 👁 vision badge and image routing come from the catalogue's capabilities, and a
new `/providers/model_catalog` endpoint serves the metadata.

## [1.4.5] — memory, hybrid-GPU fixes, and /set token

### The sidebar shows the GPU working in real time

The right-hand "Machine" panel no longer prints a frozen `1.1/8.0GB free` that
looked broken while a turn ran. The primary GPU line now reads live through NVML
(in-process, with an `nvidia-smi` fallback) and shows `6.9/8.0GB 98%` — memory
used plus utilisation — refreshing on every sidebar redraw. An integrated GPU is
labelled `shares RAM` instead of a misleading shared-memory size.

### /set token caps the reply length and the context window

`/set token <max> [context]` sets how many tokens one answer may produce and the
context window handed to llama.cpp (applied on the next model load). Both values
persist in `~/.config/forge/config.json`; `/set token` with no argument shows the
current limits. Defaults: 2048 output / 16384 context.

### Hybrid laptops report every GPU again, not just the integrated one

On Linux, `py/hardware.py` only enumerated cards that publish an `amdgpu`/`i915`
`mem_info_vram_total` node, so a hybrid NVIDIA + AMD-APU machine showed just the
integrated Radeon (or nothing) and hid the discrete card entirely. Detection now
builds the inventory from `/sys/class/drm/card*` directly:

- **NVIDIA** VRAM is read from the card's 64-bit prefetchable PCIe BAR (there is
  no `mem_info_vram_*` node), and its marketing name from
  `/proc/driver/nvidia/gpus/*/information` — both stay readable even when the
  driver is version-skewed and `nvidia-smi`/NVML fail with `DriverNotLoaded`
- **AMD/Intel** keep reading `mem_info_vram_*`, and an APU iGPU (GTT far larger
  than its visible VRAM window) is correctly marked as sharing system RAM
- the loader's `--list-devices` answer and NVML are overlaid where they can see a
  card, so a Vulkan build that only lists the iGPU no longer makes the RTX card
  vanish — the sidebar and `/specs` now say "2 GPUs" with the discrete card as
  primary

### The agent has a memory — DuckDB behind a Bloom filter

`py/memory.py` gives the agent facts that outlive a session ("my llama port is
8080") and finds them again later:

- a **Bloom filter held in RAM** answers "definitely absent" without touching the
  disk, so a miss ends the lookup there; only a possible hit reads the vault
- the values live in a **DuckDB** table, which compresses the text column itself,
  so nothing zips or unzips by hand
- `recall` rewrites a plain sentence into candidate keys in RAM first, which is
  what makes "what was my port setting again?" find `port_setting` with no
  embedding model and no network
- `/memory` prints how many lookups the gate answered in RAM, so the saving is
  measured rather than claimed
- the filter is **persisted as one small blob** and **rebuilt when it fills** -
  not rescanned at boot (which would cost the very I/O the gate exists to avoid),
  and not left to saturate (a full Bloom filter answers YES to everything)

The agent gains a ninth tool, `memory` (`remember` / `recall` / `lookup` /
`forget` / `list` / `stats`), and `/memory` works in both the full-screen client
and the plain UI through the shared command table. `duckdb` is imported lazily:
without it, everything else still runs and only memory reports how to install it.

### Local model: stop leaking, load what fits, and auto-scan

The local-model path had three failure modes that each looked like "llama.cpp
won't run":

- `stop()` only killed the child *that process* started, so every restart leaked
  a llama-server holding ~6 GB of RAM and VRAM — which is what later made loads
  "fail" for lack of memory. It now kills whatever holds the port (after
  confirming it is llama-server) and verifies the port is free before saying so.
- the RAM guard judged the whole file size against free RAM, refusing a model
  that fits across **both** GPU and CPU. A tested `_plan_load` now decides it:
  fits-in-VRAM, splits GPU+CPU, or genuinely refuses. Split loads quantise the KV
  cache to `q8_0` by default.
- a saved model path that went stale (or was recorded on another machine) left
  the server permanently off. `start()` now auto-scans the model roots and runs
  the best model that fits, so the app works per-machine with no hand-edited
  config.

### Security and correctness (audited, each with a test)

- `bash` timeout no longer orphans the command's child processes; the cap is
  tunable via `TRIOFORGE_BASH_TIMEOUT`.
- dangerous tools are denied when no approval callback is wired.
- memory writes are atomic against a crash (row + filter), and over-long keys are
  handled consistently.
- tool output is secret-redacted before it reaches a model — and in team mode,
  before it is sent to the cloud senior.

### TUI polish

- the mood face no longer clips off-screen, the "Thinking…" title no longer keeps
  its focus highlight, and streamed answers re-render at a throttled rate instead
  of flickering per token.
- typing `/q` pleads on Enter only, holds 2.5 s, and a second `ctrl+q` leaves at
  once.

## [1.4.0] — cross-platform, and the terminal client (`forge`)

**The headline: local AI now works on whichever machine you sit down at** — Apple,
Windows or Linux — with nothing to download and nothing to compile.

### The terminal client — `forge` / `trioforge`

A full-screen terminal chat (Crush-style) that shares TrioForge's models and saved
API keys, plus a coding agent with real file tools — `ls`, `view`, `write`, `edit`,
`bash`, `grep`, `glob`, `todos` — each tool call drawn as its own card in the
transcript.

```bash
./install.sh                    # Apple/Linux: puts forge + trioforge on your PATH
powershell -ExecutionPolicy Bypass -File .\install.ps1   # Windows

trioforge --version             # -> trioforge 1.4.0
trioforge --echo                # offline demo
trioforge                       # a real session
trioforge "explain this repo"   # one-shot
trioforge --specs               # hardware + what fits
```

- **You never install llama.cpp or pick a build.** The prebuilt binary for *your*
  machine — Metal on Apple Silicon, CUDA/ROCm/Vulkan on Linux and Windows, CPU with
  no GPU — is fetched once and reused. `trioforge --install-llama` / `/llama` control it.
- **The first message on a local model starts the server for you** — no more
  "Connection refused" for forgetting `/start`. If it can't load, the reason appears
  in the chat, not a raw socket error.
- **Pick anything with the mouse.** Every picker (model, provider, the `ctrl+p`
  command palette) is searchable, and you can click a row to move to it and click it
  again to pick it — or just use `↑`/`↓`/`PgUp`/`PgDn` + `enter`.
- **Copy an answer out with `ctrl+y`** (or `/copy`, `/copy 2` for an earlier one) —
  via the terminal clipboard, so it works over SSH too.
- **Multi-line input** with `ctrl+j` (Shift+Enter cannot be told apart from Enter).
- **`/specs` reports the machine** the way a task manager does — `5.0 GB used of
  14.7 GB (34%)`, `9.7 GB available` — and lists **every GPU with its free VRAM and a
  verdict**:

```
    gpus found    2
       Vulkan0    6.70 GB free of 7.83 GB   usable, but shares system RAM
                  AMD Radeon 610M (RADV RAPHAEL_MENDOCINO)
    -> Vulkan1    7.51 GB free of 7.96 GB   IN USE
                  NVIDIA GeForce RTX 5060 Laptop GPU
   can combine    no - only 1 card adds memory; the rest share system RAM
```

Key bindings: `enter` send · `ctrl+j` newline · `ctrl+p` commands · `ctrl+l` model ·
`ctrl+o` provider · `ctrl+y` copy · `tab` chat · `ctrl+n` new · `ctrl+q` quit.

### `py/llama_cpp.py` — a stdlib-only `llama_cpp`

For people who would otherwise `pip install llama-cpp-python` and watch it try to
compile. Drives the same prebuilt `llama-server` over HTTP, so there is nothing to
build, and nothing to type either:

```bash
python py/llama_cpp.py -p "2+2?" -n 40     # model, GPU layers and a free port auto-detected
python py/llama_cpp.py --chat
```
```python
import llama_cpp            # PYTHONPATH=<project>/py
llm = llama_cpp.Llama()     # auto-finds the model in models/
print(llm("Q: 2+2? A:", max_tokens=32)["choices"][0]["text"])
```

### Hardware detection, fixed

- Detects Apple Silicon (Metal/unified), NVIDIA (CUDA/Vulkan), AMD (ROCm/Vulkan) and
  Intel (SYCL/Vulkan) — via `llama-server --list-devices` first, then vendor APIs.
- No longer **sums** an iGPU's shared RAM with the dGPU's VRAM (which once reported
  15.8 GB and would have called a 12 GB model "fits in your GPU").
- RAM is reported as **used/total/% + available** (previously `available` was printed
  under the name "free", disagreeing with `free -h` and the desktop monitor).
- Explains why a "16 GB" machine shows 14.7 GB usable (firmware + iGPU frame buffer).

### Verified on real Windows and Apple Silicon

CI now runs the platform logic *and* `llama_cpp.py`'s lifecycle on real
`windows-latest` and `macos-14` (Apple Silicon) runners — so "works on Windows/macOS"
is executed on every push, not just claimed.

## [1.0.3] — the stability release

Large attachments now stream to disk (a 563 MB recording uploads in seconds), the app
bundles its own current ffmpeg, audio transcription runs four chunks at a time, the
idle timer no longer unloads a model mid-job, and the interface is a single dark
7-theme system (plus custom accents). The optional desktop window samples its own
pixels and falls back to the browser when a GPU can't draw it.
