# forge

A terminal client for TrioForge — a full-screen, Crush-style chat UI for the
models TrioForge already manages.

It is **independent of the Flask web GUI**: no routes, no templates, no static
files. The only things it shares are the model providers, so "what model is
configured" and "which key is set" live in exactly one place.

```
                                  ┌── chat ──────────────────┬── sidebar ─────────┐
  ████████╗██████╗ ██╗ ██████╗    │                          │  ██████  TRIO      │
  ╚══██╔══╝██╔══██╗██║██╔═══██╗   │  ╭─ you ───────────────╮ │                    │
     ██║   ██████╔╝██║██║   ██║   │  │ explain fit_for()    │ │  New Session       │
     ██║   ██╔══██╗██║██║   ██║   │  ╰──────────────────────╯ │  ~/TrioForge       │
     ██║   ██║  ██║██║╚██████╔╝   │  ╭─ trio · gemma · 2.1s ─╮│  ● gemma-3-12b-it  │
     ╚═╝   ╚═╝  ╚═╝╚═╝ ╚═════╝    │  │ It compares the model ││  local · 2 turns   │
                                  │  │ size against VRAM…    ││                    │
                                  │  ╰──────────────────────╯ │  Models            │
                                  │                           │  ───────────       │
                                  │                           │  gemma-3-12b-it    │
                                  │                           │  6.8 GB · text+img │
                                  ├───────────────────────────┴────────────────────┤
                                  │ ▌ ask me anything about this repo…              │
                                  ├─────────────────────────────────────────────────┤
                                  │ esc cancel · tab chat · ctrl+p commands · …     │
                                  └─────────────────────────────────────────────────┘
```

## Running it

```bash
./forge                      # full-screen UI
./forge --echo               # offline demo, no model contacted
./forge --classic            # plain scrolling UI (no alternate screen)
./forge what is a GGUF?      # one-shot: answer, print, exit
./forge --help               # everything else
```

On Windows the launcher is a batch file, because the `trioforge` symlink cannot
survive a checkout there (git writes it out as a plain text file):

```powershell
.\forge.cmd                  # PowerShell, from the repo folder
forge.cmd                    # cmd, or double-click it in Explorer
```

To run it from any directory, install the command once:

```bash
./install.sh            # Linux / macOS -> `forge` + `trioforge` on your PATH
```

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1   # Windows
```

Then `forge` and `trioforge` work anywhere. Both names run the same program, and
`--version` / `--help` print the name you used. The installers also install
`rich`, `prompt_toolkit`, `textual` and `duckdb`: the first three are deliberately
not in `requirements.txt` (that covers the Flask web app), and `duckdb` is shared
with it for the memory vault.

## The setup wizard

Run `forge --setup`, or just `forge` when nothing is configured yet. It walks
three steps, and **every step can go back** (`back` / `b` / `0`), while `cancel`
or Ctrl-C leaves without changing anything.

```
Step 1 of 3 — choose a provider
────────────────────────────────────────────────────────────
  ▶  1  local        Local (llama.cpp) — your own .gguf files, no key
     2  ollama       Ollama — local models, no key
     3  claude       Claude (Anthropic) — paid
     4  deepseek     DeepSeek — cheap, strong at code
     5  gemini       Google Gemini — free tier
     6  groq         Groq — very fast, free tier
     7  huggingface  Hugging Face — free tier
     8  openrouter   OpenRouter — many vendors, one key

  select › 1

Step 2 of 3 — choose a model  (1 on disk)
────────────────────────────────────────────────────────────
  ▶  1  gemma-3-12b-it  6.8 GB · text+image · vision projector paired
     0  back  return to the previous step

  Start gemma-3-12b-it now? (loads it into memory) [Y/n] › y
```

The provider list is **exactly** the set TrioForge's own
`py/providers/llm_providers.py` implements. Nothing is offered that cannot work.

For a local model it lists the **actual `.gguf` files on disk**, reusing
TrioForge's own scanner (`_model_roots`, `_is_mmproj`, `find_mmproj`,
`model_capabilities`) so the two front-ends can never disagree — vision
projectors are excluded, and a model with a paired projector says so.

Choosing a local model offers to **load it**, calling TrioForge's own
`llamacpp_service.start()`. It then waits on `/health` until the weights are in
memory, because `start()` returns as soon as the *port* opens and the first
message would otherwise hit a `503 Loading model`.

## Configuration

Everything lives in one JSON file, created mode `0600` because it can hold keys.

```
~/.config/forge/config.json
```

```json
{
  "provider": "local",
  "model": "/home/tc/TrioForge/models/gemma/gemma-3-12b-it-Q4_K_M.gguf",
  "providers": {
    "deepseek":    { "base_url": "https://api.deepseek.com/v1",
                     "api_key": "${DEEPSEEK_API_KEY}" },
    "openrouter":  { "base_url": "https://openrouter.ai/api/v1",
                     "api_key": "${OPENROUTER_API_KEY}" }
  }
}
```

**Keys may be written as `${ENV_VAR}`.** The file then keeps the *variable name*,
not the secret, so a config pasted into a bug report or a commit does not leak
it. A literal key still works, and warns that it is stored in plain text.

Override the location with `FORGE_CONFIG_DIR`.

## Commands

`/help` lists every command **grouped by purpose, each with a runnable example**,
and `/help <command>` explains one in detail:

```
/help /team          ->  ### /team
                         the senior/junior pair: local works, cloud guides
                         example
                         /team
```

`/help`, the `ctrl+p` palette and the usage errors all read the same table
(`COMMAND_GROUPS` in `commands.py`), so a new command cannot be added without
becoming discoverable. `Cmd.client = "tui"` marks the terminal-client-only ones;
the scrolling UI's help hides those rather than advertising something it cannot run.

| Command | |
|---|---|
| `/help [command]` | list everything, or explain one |
| `/setup` | re-run the guided setup |
| `/provider [name]` | list or switch provider |
| `/key` · `/key <value>` | show / change the key for the current provider |
| `/keys` | every saved key, and a picker to change one |
| `/keys clear` | remove every key **except the current one** |
| `/models` | offline `.gguf` files, or the endpoint's model list |
| `/start [name]` | load an offline `.gguf` into llama-server |
| `/model [name]` | show or switch model; `auto` re-detects |
| `/status` | provider, endpoint, model, key state, health |
| `/system [text]` | show or set the system prompt |
| `/clear` · `/history` · `/save [path]` | session handling |
| `/memory [list\|get\|set\|forget\|recall]` | the offline memory vault (DuckDB + Bloom gate) |
| `/echo` | toggle the offline stub |
| `/quit` | leave |

Keys in the UI are only ever shown **masked** — `••••••••a35d` is enough to
recognise *which* key it is, never enough to use it.

### Keybindings (full-screen UI)

`ctrl+p` commands · `ctrl+l` model · `ctrl+n` new session · `tab` focus chat ·
`esc` focus prompt · `ctrl+y` copy the last answer · `ctrl+a` auto-route · `ctrl+q` quit

`PageUp`/`PageDown` scroll the **transcript** — the prompt box keeps keyboard
focus, and `TextArea` binds those keys to cursor-paging, so before this the
transcript could not be scrolled at all while typing.

### Auto-route — local vs cloud, decided for you (`ctrl+a`)

`ctrl+a` has two jobs:

- **with a prompt typed** — sends that message through the router instead of the
  current model;
- **with an empty prompt** — opens the **control panel**, where you scroll and
  pick *which* models are in the pool (select 2, 5, or as many as you like).

| Check | Result |
|---|---|
| cloud API unreachable (offline) | → **local** (llama.cpp), always |
| task is complex (architecture, math, concurrency…) | → **cloud** (a keyed model from your pool) |
| task is simple (boilerplate, rename, formatting…) | → **local** |

In the panel, `space` or a click toggles a model on/off, `a` turns auto-route
itself on/off, and `enter` saves (your choices persist). So llama.cpp does the
small jobs and your API key guides the big ones — cheap, and it keeps working
with no internet. Each send prints the verdict, e.g.
`☁ cloud · complex task: architecture` or `💻 local · simple task — local is plenty`.

```bash
/auto            # toggle: route EVERY message automatically (sidebar shows AUTO-ROUTE ON)
/route <prompt>  # route one prompt without pressing ctrl+a
```

The router is `py/tui/router.py`: `classify()` scores the prompt (word-boundary
hints + code shape + length), `reachable()` is one TCP connect, and `decide()`
applies offline → local, complex → cloud, else local. No network call is made to
choose the model, so routing itself costs nothing and works offline.

### Team mode — the senior/junior pair (`/team`)

Auto-route picks **one** model per message. Team mode is the other thing: they work
together, at once. `/team` turns it on (persisted, shown as `TEAM MODE ON` in the sidebar).

```
junior (local, llama.cpp)  ─┐  both start on the same task
senior (cloud)             ─┘
        │
        ├── senior ships first ......... its answer is the deliverable
        └── junior keeps going offline .. its answer is appended as a note
```

- **Both are real workers**, not a browse-and-describes pair. The only split is
  who writes files: the senior has the write tools (so it creates and runs the
  finished work), while the junior runs **read-only** so two agents never clobber
  the same paths — it puts its full answer inline instead.
- The senior is told to produce the *actual result* — real files with real
  content, run and reported — never an empty folder, a list, a plan, or a
  question handed back. The junior is told to give its complete answer inline.
- It only applies to a **local** turn — a cloud turn is already the senior.
- The exchange is visible in the transcript (`🧑‍🏫 senior … finished` /
  `💻 local … also finished`), and the senior's tool calls are drawn as usual,
  so you can watch them work as a pair.

The directives live in `py/tui/team.py` (`senior_task()`, `junior_opinion()`),
so they can be tested without a model.

### Copying an answer out

The TUI captures the mouse, so dragging a selection is the *app's* selection, not
the terminal's. Three ways to get text out:

| | |
|---|---|
| `ctrl+y` | copy the **whole last answer** — no selecting needed |
| `/copy` · `/copy 2` | same from the command line; `2` walks one answer further back |
| drag-select, then `ctrl+c` | copy just the part you dragged over |

`ctrl+c` is deliberately dual-purpose: Textual binds it to copying a selection and
that binding *skips itself* when nothing is selected, which is what lets the app's
`ctrl+c` = quit still work. `ctrl+y` is the reliable one, because `TextArea` binds
`ctrl+y` to "redo" and would otherwise swallow it — the prompt intercepts it and
hands it to the app.

Copying uses **OSC 52**, the terminal clipboard escape. That is the only route
available here (no `xclip`/`xsel`/`wl-copy` is installed) and it also works over
SSH, but it does require the terminal to permit clipboard writes — kitty does by
default. In the scrolling UI, `/copy` instead shells out to `xclip`/`wl-copy`, and
if neither exists it prints the answer so you can select it.

### Picking from a list (mouse and keyboard)

Every picker — provider, model, and the `ctrl+p` command palette — is searchable,
and you can get to a row however you like:

| | |
|---|---|
| **Click** a row | move the highlight to it |
| **Click it again** (or double-click) | pick it |
| `↑` `↓` `PgUp` `PgDn` | move the highlight |
| type | filter the list down as you type |
| `enter` | pick the highlighted row |
| `esc` | cancel |

The filter box keeps keyboard focus, so `↑`/`↓` are handled at the screen level
rather than by the list — otherwise the only way to choose was to type enough of a
name that the filter left a single row, and press enter. Clicking is timed rather
than chain-based, so a double-click picks you the row even in terminals that do
not report multi-click events.

Tool approval prompts are clickable too: **allow** / **allow all** / **deny** are
buttons as well as `enter` / `a` / `n`.

## Reasoning / thinking

Reasoning models (DeepSeek's reasoner, Claude with thinking, and Ollama's
thinking models) emit their chain of thought on a **separate stream field** from
the answer. The backend keeps the two apart and yields `(kind, text)`, where
`kind` is `"content"` or `"reasoning"`, reading all three spellings found in the
wild:

| Field | Used by |
|---|---|
| `reasoning_content` | DeepSeek, most OpenAI-compatible reasoners |
| `reasoning` | OpenRouter and others |
| `thinking` | Ollama |

The UI renders the reasoning as a **quoted block above the answer**, so it is
always obvious which text is the model thinking and which is the reply:

```
╭─ trio · deepseek-reasoner · 4.2s ─────────────────────────╮
│ > 🧠 thinking                                             │
│ >                                                         │
│ > First I should look at fit_for().                       │
│ > Then check whether the model fits in VRAM.              │
│                                                           │
│ The model fits: it needs 7.2 GB and you have 8 GB.        │
╰───────────────────────────────────────────────────────────╯
```

While a turn is running the sidebar shows live progress, throttled to twice a
second so it never competes with the model for CPU:

```
⠹  3.4s  ·  128 tok  ·  38 tok/s
```

## Memory

The agent keeps facts that outlive a session — "my llama port is 8080", "we keep
notes in DuckDB" — and looks them up later. `py/memory.py` (shared with the web
app) uses two layers:

* **A Bloom filter gate held in RAM.** It answers "is this key *definitely*
  absent?" with no disk access at all. A miss ends the lookup right there.
* **A DuckDB table** holding the values. DuckDB compresses that text column
  itself, so nothing here zips or unzips by hand.

The gate is probabilistic, so its two answers are not symmetric:

```
gate says NO   -> definitely absent  -> skip the query       (exact)
gate says YES  -> probably present   -> query and confirm     (maybe)
```

A false positive therefore costs one wasted query and can never return a wrong
value, because the DuckDB lookup still has to match the key. `/memory` prints how
many lookups the gate answered in RAM, so the saving is measured rather than
claimed.

A filter can only test an exact key, so `recall` first rewrites "what was my port
setting again?" into candidate keys — in RAM, with no embeddings and no model —
and only then offers those to the gate.

Two details worth keeping:

* the filter is **persisted as one BLOB** and read back in a single small query.
  Re-deriving it at boot by scanning every key would cost exactly the disk I/O the
  gate exists to avoid, and get slower as the vault grows.
* it is **rebuilt once it fills**. A saturated Bloom filter answers YES to
  everything, which would silently turn every lookup into a disk read.

DuckDB allows one writing process per file. If the web app already holds the
vault, `forge` reopens it read-only instead of failing, and says so in `/memory`.

| | |
|---|---|
| `/memory` | keys, gate stats, most recent entries |
| `/memory list [prefix]` · `/memory get KEY` | inspect |
| `/memory set KEY = VALUE` · `/memory forget KEY` | edit |
| `/memory recall <sentence>` | sentence in, matching entries out |
| agent tool | `memory(action=remember\|recall\|lookup\|forget\|list\|stats)` |

The vault lives at `sqlite_data/memory.duckdb`, which is gitignored.

## Layout of the code

Everything below the widget layer is UI-agnostic, which is why two completely
different front-ends share it:

| File | Job |
|---|---|
| `backend.py` | model transport — OpenAI-compatible streaming, plus `wait_ready()` and an offline stub |
| `providers.py` | providers, keys, the config file, and migrations |
| `session.py` | conversation state. No I/O, easy to test |
| `localmodels.py` | TrioForge's on-disk `.gguf` files |
| `commands.py` | slash commands — one function plus one line of help |
| `textual_app.py` | the full-screen Crush-style UI |
| `app.py` | the plain scrolling UI, one-shot mode, and the entry point |
| `render.py` · `theme.py` | output and palette for the plain UI |
| `cli.py` | arguments |

## Why some things are the way they are

These are all bugs that were found and fixed; the comments in the code say the
same, so nobody "simplifies" them back:

- **A local model needs `wait_ready`, not just a started server.** llama-server
  binds the port immediately but answers `503 Loading model` until the weights
  are in memory. A `503` mid-session is also retried once rather than failing.
- **Cards are `width: 1fr`, not `auto`.** With `auto` a long line grew wider
  than the pane and was clipped — text appeared to be eaten.
- **`/help` is a Markdown list, not joined lines.** Consecutive lines are one
  paragraph in Markdown, so every command ran together on a single line.
- **One table drives the help, the palette and the errors.** `/team`, `/auto` and
  `/route` used to live only in the palette, so `/help` never mentioned them and
  nobody could discover them. The palette also de-duplicates: `/keys` and
  `/memory` document several forms each and would otherwise repeat.
- **The input border is the same colour focused and unfocused.** Tying it to
  `:focus` made the frame appear to break every time you tabbed away.
- **The prompt and keybind bar share one docked container.** Docked separately,
  the keybind bar overlapped the input's bottom border.
- **The provider list is TrioForge's list.** An earlier version offered Together
  AI, Mistral, xAI and OpenAI — none of which have a backend here. `openai`
  was a particular trap: `py/features/openai_api.py` is TrioForge *serving* an
  OpenAI-shaped API from local models, the opposite direction. A config written
  against that list is migrated automatically, and says what it changed.
- **Tool output is secret-redacted.** The agent can read any file it can see, and
  in team mode the junior's findings are reviewed by a cloud model. `execute()`
  masks `sk-…` keys, bearer tokens, `api_key`/`password`/`secret` assignments and
  private-key blocks before anything reaches a model's context.
- **The model is kept warm during a turn.** The idle watchdog unloads a local
  model after 5 minutes of no request, but a team turn can go minutes without one
  (the cloud senior is thinking, or a tool is running). The tick touches
  llama.cpp while busy, so the unload only ever happens *between* turns — it was
  unloading the junior mid-turn and the next request died with "connection
  refused".
- **A saved model path is a preference, not a requirement.** A path recorded on
  another machine, or a model renamed in place, used to leave llama.cpp
  permanently off ("model not found"). `start()` now auto-scans the model roots
  and runs the best model that fits (fits-in-VRAM > splits GPU+CPU > neither),
  largest within a tier, skipping vision projectors.

## Hardware and model fit

`/specs` (or `trioforge --specs` before you start) prints what the machine has
and grades model sizes against it:

```
       system    Linux (x86_64) - 32 cores
       memory    4.9 GB used of 14.7 GB (34%)
    available    9.8 GB
          gpu    NVIDIA GeForce RTX 5060 Laptop GPU
   gpu memory    8.0 GB total, 1.0 GB free
  gpu backend    Vulkan - dedicated memory
  detected by    llama.cpp --list-devices
    gpus found    2
       Vulkan0    6.70 GB free of 7.83 GB   integrated - shares system RAM
                  AMD Radeon 610M (RADV RAPHAEL_MENDOCINO)
    -> Vulkan1    7.51 GB free of 7.96 GB   IN USE
                  NVIDIA GeForce RTX 5060 Laptop GPU
   can combine    no - only 1 card adds memory; the rest share system RAM

   4.6 GB    gpu       Fast - fits entirely in NVIDIA GeForce RTX 5060 ... 7.96 GB
   8.0 GB    split     OK - runs partly on GPU, partly in RAM (slower)
  16.0 GB    too_big   Too big for your memory right now
```

The verdicts come from `py/hardware.py`, which is deliberately multi-vendor
because there is no portable API for "how much GPU memory is there":

| Machine | How GPU memory is found |
| --- | --- |
| Any, with llama.cpp installed | `llama-server --list-devices` — sees Metal, Vulkan, ROCm, SYCL and CUDA at once, with free MiB per device |
| Apple Silicon | unified memory: the GPU's wired budget, or ~75% of RAM when the kernel publishes no limit |
| Intel Mac with a dGPU | `system_profiler` |
| NVIDIA (any OS) | NVML |
| AMD / Intel on Linux | the DRM sysfs nodes (`mem_info_vram_total` / `_used`) |
| AMD / Intel on Windows | WMI, treated as an estimate |

### RAM is reported the way a task manager reports it

`memory  4.9 GB used of 14.7 GB (34%)` plus `available  9.8 GB` — deliberately
**not** `free`. Linux counts the page cache as used, so `free`'s free column is
nearly always tiny ("2.4 GB free of 14.7 GB" on a healthy idle desktop, because
~8 GB is reclaimable cache). An earlier version published `available` under the
name "free", so TrioForge printed 10.4 GB free while `free -h` printed 2.4 GiB
and the desktop monitor printed a third number — all correct, none agreeing.
`ram_free_gb` now means free, and `ram_available_gb` means available.

`--specs` also explains why a 16 GB machine reports 14.7 GB: Linux exposes the
firmware's memory map in `/sys/firmware/memmap`, and its System RAM total (15.05
GB here) is larger than `MemTotal` because the kernel, ACPI tables and the
integrated GPU's frame buffer are carved out of it. Windows Task Manager shows
the *installed* size, so it reads higher. Neither number is wrong.

Two things it deliberately gets right, because both were getting it wrong:

- **An integrated GPU's memory is system RAM.** On a hybrid laptop the iGPU and
  the dGPU were being *summed* — an AMD 610M's 7.8 GB plus an RTX 5060's 8 GB
  was reported as 15.8 GB, which calls a 12 GB model "fits in VRAM". The fit is
  now judged on one device (the dedicated card when there is one), and an
  APU/unified machine is treated as a single pool instead of VRAM + RAM.
- **Non-NVIDIA hardware is not "no GPU".** The old probe was NVML-only, so on
  Apple, AMD and Intel every model was graded as CPU-only — even with plenty of
  GPU memory — and the llama.cpp layer-offload decision silently stopped
  offloading. Both now use the same detection.

## llama.cpp is fetched for you

You never install llama.cpp, and you never pick a build. The first time a local
model is loaded, the prebuilt binary for **this** machine is downloaded once into
`tools/llama.cpp/` and reused from then on:

- **Apple Silicon / Intel Mac** → the macOS build (Metal)
- **NVIDIA** → the CUDA build when the driver answers, Vulkan otherwise
- **AMD** → ROCm when it is really installed, Vulkan otherwise
- **Intel** → Vulkan
- **no GPU** → the CPU build

The backend is detected from the PCI ids on Linux and WMI on Windows, so it does
**not** depend on `vulkaninfo` being installed — requiring it meant AMD and Intel
machines with a working Vulkan driver were quietly given the CPU build.

```bash
trioforge --install-llama     # fetch it now instead of on first use
```
```bash
/llama status                 # what backend was detected, what is installed
/llama install                # re-fetch (e.g. after a GPU or driver change)
```

Set `TRIOFORGE_NO_AUTO_INSTALL=1` to stop anything being downloaded; loading a
local model then reports plainly that llama-server is missing, and
`LLAMA_SERVER=<path>` still points at your own build.

The first message you send on the `local` provider **starts the model server
for you** and waits for it to be ready, so you do not get "Connection refused"
just because nothing had run `/start` yet. If the model is missing or cannot
load, the error is shown in the chat instead of a raw socket error.

## Requirements

- Python 3.12 (the `.venv-linux` interpreter)
- `httpx` — request transport
- `rich` — rendering for the plain UI
- `prompt_toolkit` — input for the plain UI
- `textual` — the full-screen UI
- `duckdb` — the memory vault (`/memory` and the agent's `memory` tool). Imported
  lazily, so without it everything else still runs and only memory reports that
  it is missing.

The full-screen UI needs a real TTY. Anything piped — or `--classic` — falls
back to the scrolling UI automatically, so `forge "question" | ...` still works.
