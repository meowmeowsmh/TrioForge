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

To run it from any directory, install the command once:

```bash
./install.sh            # Linux / macOS -> `forge` + `trioforge` on your PATH
```

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1   # Windows
```

Then `trioforge` works anywhere. Both names run the same program. The installers
also install `rich`, `prompt_toolkit` and `textual` — they are deliberately not
in `requirements.txt`, which only covers the Flask web app.

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

| Command | |
|---|---|
| `/help` | list everything |
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
| `/echo` | toggle the offline stub |
| `/quit` | leave |

Keys in the UI are only ever shown **masked** — `••••••••a35d` is enough to
recognise *which* key it is, never enough to use it.

### Keybindings (full-screen UI)

`ctrl+p` commands · `ctrl+l` model · `ctrl+n` new session · `tab` focus chat ·
`esc` focus prompt · `ctrl+q` quit

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
- **The input border is the same colour focused and unfocused.** Tying it to
  `:focus` made the frame appear to break every time you tabbed away.
- **The prompt and keybind bar share one docked container.** Docked separately,
  the keybind bar overlapped the input's bottom border.
- **The provider list is TrioForge's list.** An earlier version offered Together
  AI, Mistral, xAI and OpenAI — none of which have a backend here. `openai`
  was a particular trap: `py/features/openai_api.py` is TrioForge *serving* an
  OpenAI-shaped API from local models, the opposite direction. A config written
  against that list is migrated automatically, and says what it changed.

## Hardware and model fit

`/specs` (or `trioforge --specs` before you start) prints what the machine has
and grades model sizes against it:

```
       system    Linux (x86_64) - 32 cores
          ram    5.7 GB free of 14.7 GB
          gpu    NVIDIA GeForce RTX 5060 Laptop GPU
   gpu memory    8.0 GB total, 1.0 GB free
  gpu backend    Vulkan - dedicated memory
  detected by    llama.cpp --list-devices

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

## Requirements

- Python 3.12 (the `.venv-linux` interpreter)
- `httpx` — request transport
- `rich` — rendering for the plain UI
- `prompt_toolkit` — input for the plain UI
- `textual` — the full-screen UI

The full-screen UI needs a real TTY. Anything piped — or `--classic` — falls
back to the scrolling UI automatically, so `forge "question" | ...` still works.
