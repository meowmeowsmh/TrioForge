"""Slash commands.

A command is a function ``(ctx, arg) -> None`` plus one row in :data:`COMMANDS`.
Adding a command therefore means adding a function and a line of help - nothing
else in the app needs to know about it.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from . import localmodels, providers, render
from .backend import EchoBackend, fetch_models

class Cmd(NamedTuple):
    """One slash command.

    ``usage`` is the grammar exactly as the handler parses it, ``example`` is a
    line you can actually type. ``client`` is "" for both UIs or "tui" for the
    full-screen client only - the scrolling UI filters those out.
    """
    usage: str
    desc: str
    example: str
    client: str = ""


#: Every command, grouped. This is the SINGLE source of truth: /help, /help
#: <command>, the ctrl+p palette and the usage errors all read it, so a command
#: cannot be added without becoming discoverable. (It could before: /team and
#: /auto existed only in the palette, so /help never mentioned them.)
COMMAND_GROUPS: list[tuple[str, list[Cmd]]] = [
    ("getting started", [
        Cmd("/help [command]", "list every command, or explain one in detail",
            "/help /team"),
        Cmd("/setup", "guided setup: provider, then model, then API key", "/setup"),
        Cmd("/status", "server health, model, endpoint and key state", "/status"),
        Cmd("/specs", "the hardware detected, and which models fit it", "/specs"),
    ]),
    ("the model", [
        Cmd("/model [name|auto]", "show or switch model; 'auto' re-detects it",
            "/model auto"),
        Cmd("/models", "list models: local .gguf files, or the endpoint's list",
            "/models"),
        Cmd("/start [name]", "load a local .gguf into llama-server",
            "/start gemma-3-12b-it"),
        Cmd("/search <query>", "search Hugging Face for GGUF models (browse only)",
            "/search qwen 7b"),
        Cmd("/download <query>", "search Hugging Face and download a GGUF model",
            "/download qwen 7b"),
        Cmd("/provider [name]", "list providers, or switch to one",
            "/provider deepseek"),
        Cmd("/base-url [url]", "show or set the endpoint",
            "/base-url http://127.0.0.1:8080/v1"),
        Cmd("/set token <max> [context]", "cap the reply length and set the context window",
            "/set token 4096 8192"),
    ]),
    ("keys", [
        Cmd("/key [value]", "show or set the key for the CURRENT provider",
            "/key sk-abc123"),
        Cmd("/keys", "every provider, and whether it has a key", "/keys"),
        Cmd("/keys <provider> [value]", "detail for one provider, or set its key",
            "/keys groq gsk-abc123"),
        Cmd("/keys clear [all]", "forget saved keys ('all' clears yours too)",
            "/keys clear all"),
    ]),
    ("routing and teamwork", [
        Cmd("/auto", "route every message: local or cloud, decided for you",
            "/auto", "tui"),
        Cmd("/route <prompt>", "send one prompt and show the routing decision",
            "/route explain this error", "tui"),
        Cmd("/team", "every model in the pool works the task; first answer ships",
            "/team", "tui"),
        Cmd("/copy [n]", "copy an answer (n = how many answers back)", "/copy 2"),
    ]),
    ("llama.cpp", [
        Cmd("/llama", "is the bundled llama.cpp runtime installed and running",
            "/llama"),
        Cmd("/llama install", "fetch the right build for this GPU and start it",
            "/llama install"),
    ]),
    ("the conversation", [
        Cmd("/clear", "forget the conversation", "/clear"),
        Cmd("/history", "replay the conversation", "/history"),
        Cmd("/save [path]", "write the transcript to a file", "/save ~/chat.md"),
        Cmd("/system [text]", "show or set the system prompt",
            "/system answer in one line"),
    ]),
    ("memory", [
        Cmd("/memory", "what the offline vault holds, plus the gate stats",
            "/memory"),
        Cmd("/memory list [prefix]", "list stored keys", "/memory list"),
        Cmd("/memory get <key>", "read one value back", "/memory get editor"),
        Cmd("/memory set <key> = <value>", "remember a fact",
            "/memory set editor = neovim"),
        Cmd("/memory recall <sentence>", "search memory by meaning",
            "/memory recall what editor do I use"),
        Cmd("/memory forget <key>", "delete a key", "/memory forget editor"),
    ]),
    ("plugins & connectors", [
        Cmd("/plugins", "everything installed: skills, plugins, MCP servers",
            "/plugins"),
        Cmd("/plugins browse", "the shipped catalog of installable extensions",
            "/plugins browse"),
        Cmd("/plugins install <id|url|path>", "install from the catalog, a Git URL or a folder",
            "/plugins install refactor"),
        Cmd("/plugins enable <kind> <id>", "turn one extension on",
            "/plugins enable skill refactor"),
        Cmd("/plugins disable <kind> <id>", "turn one extension off",
            "/plugins disable skill refactor"),
        Cmd("/plugins remove <kind> <id>", "uninstall one extension",
            "/plugins remove skill refactor"),
        Cmd("/connectors", "services you've signed into, with live status",
            "/connectors"),
        Cmd("/connectors connect <id>", "sign in to a connector (opens your browser)",
            "/connectors connect gmail"),
    ]),
    ("other", [
        Cmd("/echo", "toggle the offline stub (no model needed)", "/echo"),
        Cmd("/quit", "leave (ctrl+q in the terminal client)", "/quit"),
    ]),
]


def _visible(cmd: Cmd, client: str) -> bool:
    """Terminal-client-only commands are hidden from the scrolling UI."""
    return cmd.client != "tui" or client == "tui"


def _iter(client: str = "tui") -> list[Cmd]:
    """Every command the given client can actually run."""
    return [c for _title, rows in COMMAND_GROUPS for c in rows
            if _visible(c, client)]


#: Flat (usage, description) in group order - what the palette searches.
COMMANDS: list[tuple[str, str]] = [(c.usage, c.desc) for c in _iter()]


def lookup(query: str) -> Cmd | None:
    """Find the command a user meant by ``query`` ("/team", "team", "/keys")."""
    q = query.strip().lower().lstrip("/")
    for c in _iter():
        name = c.usage.split()[0].lstrip("/").lower()
        if q == name:
            return c
    return None


def _help(ctx, arg: str) -> None:
    """``/help`` lists everything; ``/help <command>`` explains one."""
    query = (arg or "").strip()
    if query:
        found = lookup(query)
        if found is None:
            render.error(f"no command called {query!r} — /help lists them all")
            return
        render.help_detail(found.usage, found.desc, found.example,
                           found.client == "tui")
        return
    client = "tui" if getattr(ctx, "is_tui", False) else "plain"
    groups = [(title, [c for c in rows if _visible(c, client)])
              for title, rows in COMMAND_GROUPS]
    render.help_panel([(t, r) for t, r in groups if r])


def _setup(ctx, arg: str) -> None:
    """Re-run the guided setup without leaving the session."""
    from . import wizard

    try:
        wizard.setup(ctx.cfg)
    except wizard.Cancelled:
        render.warn("setup cancelled")
        return
    except Exception as exc:  # noqa: BLE001
        render.error(f"setup failed: {exc}")
        return
    ctx.session.model = ctx.cfg.model
    _apply(ctx)
    render.ok(f"now talking to {ctx.cfg.provider} · {ctx.cfg.model}")


# --------------------------------------------------------------------- models


def _model(ctx, arg: str) -> None:
    if not arg:
        render.table("model", [
            ("provider", ctx.cfg.provider),
            ("model", ctx.session.model or "(auto)"),
            ("endpoint", ctx.cfg.base_url),
            ("backend", type(ctx.backend).__name__),
        ])
        return
    if arg == "auto":
        names = fetch_models(ctx.cfg.base_url, ctx.cfg.api_key)
        if not names:
            render.warn("could not detect a model — is the endpoint reachable?")
            return
        arg = names[0]
    ctx.session.model = arg
    ctx.cfg.model = arg
    _apply(ctx)
    providers.save(ctx.cfg)
    render.ok(f"model → {arg}")


def _models(ctx, arg: str) -> None:
    # Local provider -> the .gguf files on disk are the real list; the endpoint
    # is not running yet, so asking it would return nothing useful.
    if "127.0.0.1" in ctx.cfg.base_url or "localhost" in ctx.cfg.base_url:
        rows = localmodels.rows()
        if rows:
            render.table(f"offline models on disk ({len(rows)})",
                         [(n, d + ("  ◀ current" if n == ctx.session.model else ""))
                          for n, d in rows])
            render.info("load one with /start <name>")
            return
    names = fetch_models(ctx.cfg.base_url, ctx.cfg.api_key)
    if not names:
        known = ctx.cfg.current.get("models") or []
        if known:
            render.table(f"models (cached) — {ctx.cfg.provider}",
                         [(n, "◀ current" if n == ctx.session.model else "") for n in known])
        else:
            render.warn("the endpoint listed no models")
            render.info("check /provider and /key, or /status for the error")
        return
    ctx.cfg.set_models(names)
    providers.save(ctx.cfg)
    rows = [(n, "◀ current" if n == ctx.session.model else "") for n in names]
    render.table(f"models — {ctx.cfg.provider} ({len(names)})", rows)
    if not ctx.session.model and names:
        ctx.session.model = names[0]
        ctx.cfg.model = names[0]
        _apply(ctx)
        providers.save(ctx.cfg)
        render.info(f"selected {names[0]}")


# ------------------------------------------------------------------ providers


def _provider(ctx, arg: str) -> None:
    if not arg:
        render.table("providers", providers.describe(ctx.cfg))
        render.info("switch with /provider <name>")
        return
    if arg not in ctx.cfg.providers:
        render.error(f"unknown provider {arg!r}")
        render.info("known: " + ", ".join(sorted(ctx.cfg.providers)))
        render.info("add one with /provider-add <name> <base-url>")
        return
    ctx.cfg.provider = arg
    ctx.session.model = ""
    ctx.cfg.model = ""
    _apply(ctx)
    providers.save(ctx.cfg)
    render.ok(f"provider → {arg}  ({ctx.cfg.base_url})")

    remote = "127.0.0.1" not in ctx.cfg.base_url and "localhost" not in ctx.cfg.base_url
    if remote and not ctx.cfg.has_key():
        raw = ctx.cfg.raw_key
        if providers.is_indirect(raw):
            render.warn(f"no key — export {raw.strip('${}')} or use /key <value>")
        else:
            render.warn("no API key set — use /key <value>")

    names = fetch_models(ctx.cfg.base_url, ctx.cfg.api_key)
    if names:
        ctx.session.model = names[0]
        ctx.cfg.model = names[0]
        _apply(ctx)
        providers.save(ctx.cfg)
        render.info(f"using {names[0]} — {len(names)} models available (/models)")
    else:
        render.warn("could not list models — check /status")


def _provider_add(ctx, arg: str) -> None:
    parts = arg.split()
    if len(parts) < 2:
        render.info("usage: /provider-add <name> <base-url>")
        return
    name, url = parts[0], parts[1]
    ctx.cfg.add_provider(name, url)
    _apply(ctx)
    providers.save(ctx.cfg)
    render.ok(f"added provider {name} → {url} (now current)")


def _keys_clear(ctx, arg: str, everything: bool = False) -> None:
    """Remove saved keys, KEEPING the current provider unless 'all' is given.

    Deliberately asks first and prints exactly what goes and what stays - this
    deletes credentials, so it should never be a surprise.
    """
    cfg = ctx.cfg
    keep = "" if everything else cfg.provider

    doomed = [n for n in sorted(cfg.providers)
              if n != keep and cfg.providers[n].get("api_key", "")]
    spared = [n for n in sorted(cfg.providers) if n == keep]

    if not doomed:
        render.info("nothing to clear — no other provider has a key saved")
        return

    render.blank()
    render.heading(f"clear keys — keeping {keep}" if keep else "clear ALL keys")
    for n in doomed:
        render.error_line(f"remove  {n:12} {providers.key_state(cfg, n)}")
    for n in spared:
        render.ok_line(f"keep    {n:12} {providers.key_state(cfg, n)}")
    render.blank()

    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.styles import Style

    session = PromptSession(history=InMemoryHistory(),
                            style=Style.from_dict({"prompt": "bold #7dcfff"}))
    try:
        ans = session.prompt(
            f"  remove {len(doomed)} key(s), keeping {keep or 'nothing'}? [y/N] › "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        render.info("cancelled")
        return

    if ans not in ("y", "yes"):
        render.info("cancelled — nothing removed")
        return

    for n in doomed:
        cfg.providers[n]["api_key"] = ""
    providers.save(cfg)
    if keep:
        render.ok(f"cleared {len(doomed)} key(s); kept {keep}")
    else:
        render.ok(f"cleared all {len(doomed)} key(s)")
    render.info("/keys to confirm")


def _keys(ctx, arg: str) -> None:
    """The saved-key entry point: list every provider, then optionally change one.

    /keys                       -> table of all providers + key state
    /keys <provider>            -> detail for one provider
    /keys <provider> <value>    -> set that provider's key
    """
    from . import wizard

    parts = arg.split(maxsplit=1)
    target = parts[0] if parts else ""
    value = parts[1].strip() if len(parts) > 1 else ""

    # /keys clear [all] - wipe every key except the current provider
    if target.lower() in ("clear", "reset", "wipe"):
        _keys_clear(ctx, arg, everything=(value.lower() in ("all", "everything")))
        return

    # ---- set directly: /keys gemini sk-... ---------------------------------
    if target and value:
        if target not in ctx.cfg.providers:
            render.error(f"unknown provider {target!r}")
            return
        ctx.cfg.providers[target]["api_key"] = value
        providers.save(ctx.cfg)
        render.ok(f"{target}: key saved ({providers.masked(value)})")
        render.info("/status to check it, or /provider "
                    f"{target} to switch to it")
        return

    # ---- detail for one provider: /keys gemini -----------------------------
    if target:
        if target not in ctx.cfg.providers:
            render.error(f"unknown provider {target!r}")
            render.info("known: " + ", ".join(sorted(ctx.cfg.providers)))
            return
        entry = ctx.cfg.providers[target]
        render.table(f"key — {target}", [
            ("endpoint", entry.get("base_url", "")),
            ("key", providers.masked(entry.get("api_key", ""))),
            ("in use", "yes" if target == ctx.cfg.provider else "no"),
        ])
        render.info(f"change it with: /keys {target} <value>")
        render.info("or run /keys with no arguments to pick from a list")
        return

    # ---- no arguments: the table, then an optional edit --------------------
    render.table("saved keys", [
        (f"{'▶' if n == ctx.cfg.provider else ' '} {n}",
         providers.key_state(ctx.cfg, n))
        for n in sorted(ctx.cfg.providers)
    ])
    render.blank()
    render.info("Enter keeps everything as it is")
    render.info("type a provider name to change its key")
    render.blank()

    names = sorted(ctx.cfg.providers)
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.styles import Style

    session = PromptSession(
        completer=WordCompleter(names, ignore_case=True, match_middle=True),
        complete_while_typing=True, history=InMemoryHistory(),
        style=Style.from_dict({"prompt": "bold #7dcfff"}))
    try:
        pick = session.prompt("  change which? › ").strip()
    except (EOFError, KeyboardInterrupt):
        return

    if not pick:
        return
    matches = [n for n in names if n.lower().startswith(pick.lower())]
    if pick in ctx.cfg.providers:
        chosen = pick
    elif len(matches) == 1:
        chosen = matches[0]
    else:
        render.error(f"no single provider matches {pick!r}")
        return

    try:
        action, newval = wizard.ask_key(
            chosen, ctx.cfg.providers[chosen].get("label") or chosen,
            current=ctx.cfg.raw_key if chosen == ctx.cfg.provider
            else ctx.cfg.providers[chosen].get("api_key", ""))
    except wizard.Cancelled:
        render.warn("cancelled")
        return

    if action == "back":
        return
    if action == "keep":
        render.info("unchanged")
        return
    if action == "clear":
        ctx.cfg.providers[chosen]["api_key"] = ""
        render.warn(f"{chosen}: saved key removed")
    else:
        ctx.cfg.providers[chosen]["api_key"] = newval
        render.ok(f"{chosen}: key saved ({providers.masked(newval)})")

    if chosen == ctx.cfg.provider:
        _apply(ctx)
    providers.save(ctx.cfg)
    render.info("/status to confirm, or /provider "
                f"{chosen} to switch to it")


def _key(ctx, arg: str) -> None:
    """Set the key. Never echoes the secret back."""
    if not arg:
        raw = ctx.cfg.raw_key
        if not raw:
            render.warn(f"no key set for {ctx.cfg.provider}")
        elif providers.is_indirect(raw):
            render.table("api key", [
                ("provider", ctx.cfg.provider),
                ("stored as", raw),
                ("env now", "set" if providers.resolve(raw) else "NOT set"),
            ])
        else:
            render.table("api key", [
                ("provider", ctx.cfg.provider),
                ("stored as", f"literal · {len(raw)} chars · …{raw[-4:]}"),
            ])
        render.info("set with /key <value>, or /key ${ENV_VAR} to read the env")
        return

    ctx.cfg.set_key(arg)
    _apply(ctx)
    path = providers.save(ctx.cfg)
    if providers.is_indirect(arg):
        render.ok(f"key for {ctx.cfg.provider} → {arg}")
    else:
        render.ok(f"key stored for {ctx.cfg.provider} ({len(arg)} chars)")
        render.warn("stored in plain text — prefer /key ${MY_API_KEY}")
    render.info(f"written to {path} (mode 600)")


# --------------------------------------------------------------------- status


def _status(ctx, arg: str) -> None:
    healthy, detail = ctx.backend.health()
    cfg = ctx.cfg
    render.table("status", [
        ("provider", cfg.provider),
        ("endpoint", cfg.base_url),
        ("model", ctx.session.model or "(auto)"),
        ("api key", "set" if cfg.has_key() else "not set"),
        ("health", ("ok — " + detail) if healthy else ("unreachable — " + detail)),
        ("turns", str(ctx.session.turns)),
        ("messages", str(len(ctx.session.messages))),
    ])
    warn = providers.check_permissions()
    if warn:
        render.warn(warn)
    if not healthy:
        render.warn("endpoint not answering; /echo uses the offline stub")


def _base_url(ctx, arg: str) -> None:
    if not arg:
        render.table("endpoint", [("provider", ctx.cfg.provider),
                                  ("base_url", ctx.cfg.base_url)])
        return
    ctx.cfg.set_base_url(arg)
    _apply(ctx)
    providers.save(ctx.cfg)
    render.ok(f"base_url → {arg}")


def _system(ctx, arg: str) -> None:
    if not arg:
        render.table("system prompt", [("text", ctx.session.system or "(none)")])
        return
    ctx.session.system = arg
    render.ok("system prompt set")


# Built-in token defaults, so /set token can name what it falls back to.
_DEFAULT_MAX_TOKENS = 2048
_DEFAULT_CTX_SIZE = 16384


def _set_token(ctx, arg: str) -> None:
    """``/set token <max> [context]`` - cap the reply length and the window.

    ``max`` is the most tokens a single answer may produce (0 for no override).
    ``context`` is the history window passed to llama.cpp on the next load; it is
    optional, and changing it takes effect the next time the local model starts.

    Dispatched as "/set" (so "/set token 4096" arrives with "token" still in the
    argument) and as "/token" directly, so both spellings work.
    """
    arg = arg.strip()
    if arg.lower().startswith("token"):
        arg = arg[len("token"):].strip()
    if not arg:
        max_out = ctx.cfg.max_tokens or _DEFAULT_MAX_TOKENS
        ctx_size = ctx.cfg.ctx_size or _DEFAULT_CTX_SIZE
        render.table("token limits", [
            ("max output", f"{max_out} tokens"
             + ("" if ctx.cfg.max_tokens else " (default)")),
            ("context", f"{ctx_size} tokens"
             + ("" if ctx.cfg.ctx_size else " (default)")),
        ])
        render.info("usage: /set token <max> [context]")
        return
    parts = arg.split()
    try:
        max_out = int(parts[0])
        ctx_size = int(parts[1]) if len(parts) > 1 else (ctx.cfg.ctx_size or 0)
    except ValueError:
        render.error("usage: /set token <max> [context]  — e.g. /set token 4096 8192")
        return
    if max_out < 1:
        render.error("max output tokens must be at least 1")
        return
    ctx.cfg.max_tokens = max_out
    if len(parts) > 1:
        if ctx_size < 1:
            render.error("context must be at least 1")
            return
        ctx.cfg.ctx_size = ctx_size
    providers.save(ctx.cfg)
    _apply(ctx)
    if len(parts) > 1:
        render.ok(f"max output → {max_out} tokens · context → {ctx_size} tokens "
                  "(context applies next model load)")
    else:
        render.ok(f"max output → {max_out} tokens")


def _clear(ctx, arg: str) -> None:
    n = len(ctx.session.messages)
    ctx.session.clear()
    render.clear()
    render.ok(f"forgot {n} messages")


def _history(ctx, arg: str) -> None:
    if not ctx.session.messages:
        render.info("nothing yet")
        return
    for m in ctx.session.messages:
        if m.role == "user":
            render.user(m.content)
        elif m.role == "assistant":
            render.assistant(m.content)


def _save(ctx, arg: str) -> None:
    if not ctx.session.messages:
        render.warn("nothing to save")
        return
    path = Path(arg).expanduser() if arg else Path.home() / "trio-session.txt"
    try:
        path.write_text(ctx.session.transcript(), encoding="utf-8")
        render.ok(f"saved {len(ctx.session.messages)} messages → {path}")
    except OSError as exc:
        render.error(f"could not write {path}: {exc}")


def _start(ctx, arg: str) -> None:
    """Load an offline .gguf through TrioForge's own server manager."""
    models = localmodels.available()
    if not models:
        render.warn("no offline .gguf models found")
        render.info("looked in TrioForge's model roots (see localmodels.py)")
        return

    if arg:
        m = localmodels.find(arg)
        if m is None:
            render.error(f"no offline model matches {arg!r}")
            render.info("available: " + ", ".join(x.name for x in models))
            return
    else:
        from . import wizard
        rows = localmodels.rows()
        try:
            idx = wizard.choose("load which offline model?", rows, default=0)
        except wizard.Cancelled:
            render.warn("cancelled")
            return
        m = models[idx]

    # Say up front when this model cannot live entirely in VRAM, so a slow reply
    # is explained before it happens rather than discovered as "why is it 1 tok/s".
    try:
        import hardware
        verdict = hardware.fit(m.size_gb)
        if verdict == "split":
            render.warn(f"{m.name} is {m.size_gb:.1f} GB — bigger than your VRAM, "
                        "so it will split GPU+CPU and run several times slower")
        elif verdict == "cpu":
            render.warn(f"no VRAM headroom for {m.name} — it will run on CPU only")
        elif verdict == "too_big":
            render.warn(f"{m.name} is too big for your memory — it may not load")
    except Exception:  # noqa: BLE001 - hardware probing must never block a load
        pass

    render.info(f"loading {m.name} ({m.size_gb:.1f} GB) — this takes a while")
    try:
        import llamacpp_service as svc  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        render.error(f"cannot reach TrioForge's server manager: {exc}")
        return
    try:
        res = svc.start(model=m.path, ctx_size=ctx.cfg.ctx_size or None)
    except Exception as exc:  # noqa: BLE001
        render.error(f"failed to start: {exc}")
        return

    if res.get("running"):
        from .backend import wait_ready
        render.info("waiting for the weights to load (the port opens first)")
        wait_ready("http://127.0.0.1:8080/v1", timeout=900)
        render.ok(f"llama-server is up — {res.get('model') or m.name}")
        ctx.session.model = m.name
        ctx.cfg.model = m.path
        if ctx.cfg.base_url and "127.0.0.1" not in ctx.cfg.base_url:
            ctx.cfg.set_base_url("http://127.0.0.1:8080/v1")
        _apply(ctx)
        providers.save(ctx.cfg)
        render.info("now talking to the local server")
    else:
        render.error(f"could not start: {res.get('error', 'unknown error')}")


def _download(ctx, arg: str) -> None:
    """/download <query> — search Hugging Face and download a GGUF model."""
    from . import model_download, wizard

    query = arg.strip()
    if not query:
        render.error("usage: /download <search query>  —  e.g. /download qwen 7b")
        return

    render.info(f"searching Hugging Face for GGUF models matching {query!r}…")
    results = model_download.search(query)
    if not results:
        render.warn("no GGUF models found — try a shorter or different query")
        return

    rows = [(repo, f"{dl:,} downloads" if dl else "") for repo, dl in results[:20]]
    try:
        idx = wizard.choose("pick a model repo", rows, default=0)
    except wizard.Cancelled:
        render.warn("cancelled")
        return
    repo = results[idx][0]

    render.info(f"listing GGUF files in {repo}…")
    repo_files = model_download.files(repo)
    if not repo_files:
        render.warn("no .gguf files in that repo")
        return

    rows = [(name, f"{size:.2f} GB") for name, size in repo_files[:20]]
    try:
        fidx = wizard.choose("pick a file to download", rows, default=0)
    except wizard.Cancelled:
        render.warn("cancelled")
        return
    filename = repo_files[fidx][0]

    render.info(f"downloading {filename}… (this can take a while)")
    try:
        path = model_download.download(repo, filename)
    except Exception as exc:  # noqa: BLE001 - a failed pull is not fatal
        render.error(f"download failed: {exc}")
        return
    render.ok(f"downloaded → {path}")
    render.info("load it with /start <name>, or pick it with ctrl+l")


def _search(ctx, arg: str) -> None:
    """/search <query> — search Hugging Face for GGUF models (browse, no download)."""
    from . import model_download

    query = arg.strip()
    if not query:
        render.error("usage: /search <query>  —  e.g. /search qwen 7b")
        return
    render.info(f"searching Hugging Face for GGUF models matching {query!r}…")
    results = model_download.search(query)
    if not results:
        render.warn("no GGUF models found — try a shorter or different query")
        return
    render.table(f"GGUF models matching {query!r}",
                 [(repo, f"{dl:,} downloads" if dl else "") for repo, dl in results[:20]])
    render.info("download one with /download <query>")


def _echo(ctx, arg: str) -> None:
    if isinstance(ctx.backend, EchoBackend):
        ctx.backend = ctx.real_backend
        render.ok("offline stub off — talking to the real endpoint")
    else:
        ctx.real_backend = ctx.backend
        ctx.backend = EchoBackend()
        render.warn("offline stub on — nothing leaves the machine")


def _copy(ctx, arg: str) -> None:
    """Copy an answer to the clipboard (the scrolling UI's version of /copy).

    The full-screen UI handles /copy itself: it owns the terminal and can write
    OSC 52, which needs no external tool. Out here the best available route is a
    clipboard utility, and when none is installed the answer is printed so it can
    be selected by hand or piped.
    """
    import shutil
    import subprocess

    which = 1
    if arg.strip().isdigit():
        which = max(1, int(arg.strip()))
    # session.Message carries .role/.content, not .text.
    answers = [m.content for m in getattr(ctx.session, "messages", [])
               if getattr(m, "role", "") == "assistant" and getattr(m, "content", "")]
    if not answers:
        render.warn("nothing to copy yet — ask something first")
        return
    which = min(which, len(answers))
    text = answers[-which]

    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode(), check=True, timeout=10)
                render.ok("copied {} characters via {}".format(len(text), cmd[0]))
                return
            except Exception:
                continue

    render.warn("no clipboard tool found (install xclip or wl-clipboard) — here it is:")
    render.blank()
    render.info(text)


def _quit(ctx, arg: str) -> None:
    ctx.running = False


# Sizes worth showing, in GB of .gguf file: a small one, the usual 7-8B at
# Q4_K_M, a 12-14B, then two that only a big machine can hold.
_SPEC_SIZES = (2.0, 4.6, 8.0, 16.0, 32.0)


def _specs(ctx, arg: str) -> None:
    """What hardware TrioForge found, and what that means for models.

    Detection is hardware.py's job - it understands Apple unified memory and
    AMD/Intel parts, not just NVIDIA, on all three platforms.
    """
    import hardware

    spec = hardware.specs()

    rows = [
        ("system", "{} ({}) - {} cores".format(
            spec["os"], spec["machine"], spec["cpu_count"])),
        ("cpu", spec["cpu"] or "unknown"),
        # Deliberately the same shape a task manager uses, and the word
        # "available" rather than "free": Linux counts the page cache as used, so
        # this used to print "10.4 GB free" while `free -h` said 2.4 GiB free and
        # the desktop monitor said something else again.
        ("memory", "{:.1f} GB used of {:.1f} GB ({:.0f}%)".format(
            spec["ram_used_gb"], spec["ram_total_gb"], spec["ram_percent"])),
        ("available", "{:.1f} GB".format(spec["ram_available_gb"])),
    ]
    if spec["gpu_name"]:
        rows += [
            ("gpu", spec["gpu_name"]),
            ("gpu memory", "{:.1f} GB total, {:.1f} GB free{}".format(
                spec["vram_total_gb"], spec["vram_free_gb"],
                "  (estimate)" if spec["gpu_approximate"] else "")),
            ("gpu backend", "{} - {} memory".format(
                spec["gpu_backend"] or "unknown",
                "unified with the CPU" if spec["gpu_unified"] else "dedicated")),
            ("detected by", spec["gpu_source"]),
        ]
    else:
        rows.append(("gpu", "none detected - models run on the CPU"))

    # Every GPU, with the memory each one can actually give right now - not just
    # the one that was picked. With two cards the interesting question is which
    # is in use and why, so that is stated rather than left to be inferred.
    devices = spec.get("gpu_devices") or []
    if len(devices) > 1:
        rows.append(("gpus found", "{}".format(len(devices))))
        for d in devices:
            if d.get("in_use"):
                mark, tag = "->", "IN USE"
            elif not d.get("adds_memory"):
                # Usable by llama.cpp, but offloading to it frees no memory: its
                # "VRAM" is the system RAM already counted above.
                mark, tag = "  ", "usable, but shares system RAM (adds no memory)"
            else:
                mark, tag = "  ", "usable (idle)"
            rows.append(("  {} {}".format(mark, d["label"]),
                         "{:.2f} GB free of {:.2f} GB   {}".format(
                             d["free_gb"], d["total_gb"], tag)))
            rows.append(("", d["name"]))
        pool = spec.get("gpu_usable_count", 0)
        rows.append(("can combine", "yes - {} cards add memory and llama.cpp can "
                                    "split across them".format(pool) if pool >= 2
                                    else "no - only {} card adds memory; the rest "
                                         "share system RAM".format(pool)))
    render.table("this machine", rows)

    for note in spec.get("notes") or []:
        render.info(note)

    render.blank()
    render.heading("what fits")
    render.table("model file size -> verdict", [
        ("{:.1f} GB".format(size),
         "{}   {}".format(hardware.fit(size, spec),
                          hardware.fit_label(hardware.fit(size, spec), spec)))
        for size in _SPEC_SIZES
    ])


def _llama(ctx, arg: str) -> None:
    """Show, pre-fetch or re-fetch the llama.cpp runtime.

    Nothing here is required to use TrioForge - llama.cpp is fetched on first
    use - but this makes it explicit, and lets you re-fetch after a driver or
    GPU change so the right backend build is in place.
    """
    import llama_installer as LI

    action = (arg or "").strip().lower()
    gpu = LI._gpu_backend()
    found = LI.find_installed()

    if action in ("", "status", "show"):
        render.table("llama.cpp runtime", [
            ("platform", "{} / {}".format(gpu.get("os", "?"), gpu.get("arch", "?"))),
            ("backend", "{}  ({})".format(gpu.get("backend", "?"), gpu.get("label", ""))),
            ("installed", found or "not yet - it downloads on first use"),
        ])
        if not found:
            render.info("nothing to do: loading a local model fetches it automatically")
            render.info("or run /llama install now")
        return

    if action in ("install", "update", "reinstall", "get", "fetch"):
        render.info("downloading the prebuilt llama.cpp for {} ({})...".format(
            gpu.get("os", "?"), gpu.get("backend", "?")))
        result = LI.install_llamacpp()
        if result.get("ok"):
            render.ok("llama.cpp ready: {}".format(result.get("path")))
            render.info("load a model with /start")
        else:
            render.error(result.get("error") or "install failed")
            render.info("you can also install it yourself and set LLAMA_SERVER=<path>")
        return

    render.info("usage: /llama [status | install]")


def _one_line(text: str, width: int = 70) -> str:
    """Collapse a stored value onto one line for the list views."""
    flat = " ".join((text or "").split())
    return flat[:width] + ("..." if len(flat) > width else "")


def _memory(ctx, arg: str) -> None:
    """The offline memory vault: a DuckDB table behind a Bloom filter gate.

    Every lookup asks the gate first. A key the gate rejects is definitely absent,
    so that lookup ends in RAM and the vault is never touched - which is what the
    stats line counts.
    """
    try:
        import memory as mem
    except Exception as exc:  # noqa: BLE001
        render.error(f"memory vault unavailable: {exc}")
        return

    if not mem.available():
        render.warn(mem.missing_reason())
        return

    verb, _, rest = (arg or "").strip().partition(" ")
    verb = verb.lower()
    rest = rest.strip()

    if verb in ("", "stats", "gate", "status"):
        s = mem.stats()
        render.table("memory vault", [
            ("vault", "{}  ({:.1f} KB)".format(s["db"], s["db_bytes"] / 1024)),
            ("keys", str(s["keys"])),
            ("gate (RAM)", "{} bits · {} hashes · {} bytes · {:.1%} full".format(
                s["bits"], s["hashes"], s["bytes"], s["fill"])),
            ("gate target", "{:.1%} false positives at {} keys".format(
                s["target_fp"], s["capacity"])),
            ("lookups", "{} of {} skipped in RAM — {:.0f}% with no disk read".format(
                s["gate_skips"], s["lookups"], s["saved_pct"])),
            ("false hits", "{} (gate said maybe, the vault said no)".format(
                s["gate_misses"])),
            ("writes", "{}  ·  filter rebuilds {}".format(
                s["writes"], s["rebuilds"])),
        ])
        if not s["writable"]:
            render.warn(s["reason"])
        rows = mem.entries(limit=10)
        if rows:
            render.info("most recently updated:")
            for key, value in rows:
                render.info(f"  {key} = {_one_line(value)}")
        else:
            render.info("nothing stored yet — ask the agent to remember something, "
                        "or use /memory set KEY = VALUE")
        return

    if verb in ("list", "keys"):
        found = mem.keys(rest)
        if not found:
            render.warn("the vault is empty" if not rest
                        else f"no key starts with {rest!r}")
            return
        render.info("{} key(s):".format(len(found)))
        for key in found[:60]:
            render.info(f"  {key}")
        if len(found) > 60:
            render.info("  ... and {} more".format(len(found) - 60))
        return

    if verb in ("get", "lookup", "show"):
        if not rest:
            render.error("usage: /memory get KEY")
            return
        value = mem.lookup(rest)
        if value is None:
            render.warn(f"no memory stored under {rest!r}")
            return
        render.info(f"{rest} =")
        for line in (value.splitlines() or [""]):
            render.info(f"  {line}")
        return

    if verb in ("set", "add", "remember", "save"):
        key, sep, value = rest.partition("=")
        if not sep:                       # no '=': the first word is the key
            key, _, value = rest.partition(" ")
        key, value = key.strip(), value.strip()
        if not key or not value:
            render.error("usage: /memory set KEY = VALUE")
            return
        result = mem.remember(key, value)
        if not result.get("ok"):
            render.error(result.get("error") or "could not store that")
            return
        render.ok("{} {!r} — {} key(s), gate {:.1%} full".format(
            "updated" if result.get("updated") else "saved",
            result["key"], result["keys"], mem.stats()["fill"]))
        return

    if verb in ("forget", "delete", "rm", "remove"):
        if not rest:
            render.error("usage: /memory forget KEY")
            return
        if mem.forget(rest):
            render.ok(f"forgot {rest!r}")
        else:
            render.warn(f"nothing stored under {rest!r}")
        return

    if verb in ("recall", "find", "search"):
        if not rest:
            render.error("usage: /memory recall what was my port setting again?")
            return
        hits = mem.recall(rest)
        if not hits:
            render.warn(f"nothing remembered matching {rest!r}")
            return
        render.info("{} match(es), ranked by the keyword rewriter:".format(len(hits)))
        for key, value, score in hits:
            render.info(f"  {key} (score {score:.1f}) = {_one_line(value, 60)}")
        return

    render.info("usage: /memory [list | get KEY | set KEY = VALUE | forget KEY | "
                "recall SENTENCE | stats]")


_ext_ready = False


def _ext():
    """Load skills, plugins and MCP once, then hand back the extensions module.

    The web app loads these at startup; the TUI is a separate process, so it does
    the same, lazily, the first time a plugin command is used. Plugins are given a
    bare Flask app to register routes on - harmless here, and it keeps gmail
    loading cleanly instead of failing its register(None) call.
    """
    global _ext_ready
    import plugin_loader, skills_loader, mcp_client, extensions
    if not _ext_ready:
        try:
            skills_loader.load_all()
        except Exception as e:  # noqa: BLE001
            render.warn(f"skills failed to load: {e}")
        try:
            mcp_client.load_all()
        except Exception as e:  # noqa: BLE001
            render.warn(f"mcp failed to load: {e}")
        try:
            from flask import Flask
            plugin_loader.load_all(Flask("trioforge-tui"))
        except Exception:  # noqa: BLE001 - a plugin's register() may not like Flask
            try:
                plugin_loader.load_all(None)
            except Exception as e:  # noqa: BLE001
                render.warn(f"plugins failed to load: {e}")
        _ext_ready = True
    return extensions


def _plugins(ctx, arg: str) -> None:
    """The 🧩 Plugins panel as a command: list, browse, install, toggle, remove."""
    ext = _ext()
    parts = (arg or "").strip().split()
    verb = parts[0].lower() if parts else ""

    if verb == "browse":
        rows = []
        for c in ext.catalog():
            state = "installed" if c["installed"] else "install"
            rows.append(("{} · {}".format(state, c["kind"]),
                         "{} — {}".format(c["title"], c["description"])))
        render.table("catalog", rows)
        render.info("install one with /plugins install <id>")
        return

    if verb == "install":
        if len(parts) < 2:
            render.error("usage: /plugins install <catalog-id | git-url | folder-path>")
            return
        src = parts[1]
        known = {c["id"] for c in ext.catalog()}
        if src in known and "://" not in src and not Path(src).exists():
            src = "catalog:" + src
        res = ext.install(src)
        if res.get("error"):
            render.error(res["error"])
            return
        lines = []
        for s in res.get("skills") or []:
            lines.append("  skill  " + s)
        for p in res.get("plugins") or []:
            lines.append("  plugin " + p)
        render.ok("installed:")
        for line in lines:
            render.info(line)
        if res.get("restart"):
            render.warn("plugins import at startup — restart the TUI to load them")
        return

    if verb in ("enable", "disable", "on", "off"):
        enabled = verb in ("enable", "on")
        if len(parts) < 3:
            render.error(f"usage: /plugins {verb} <skill|plugin|mcp> <id>")
            return
        res = ext.set_enabled(parts[1].lower(), parts[2], enabled)
        if res.get("error"):
            render.error(res["error"])
            return
        render.ok(f"{parts[2]} {'enabled' if enabled else 'disabled'}")
        if res.get("restart"):
            render.warn("plugins import at startup — restart the TUI for this to take effect")
        return

    if verb in ("remove", "uninstall", "rm", "delete"):
        if len(parts) < 3:
            render.error("usage: /plugins remove <skill|plugin|mcp> <id>")
            return
        res = ext.remove(parts[1].lower(), parts[2])
        if res.get("error"):
            render.error(res["error"])
            return
        render.ok(f"removed {parts[2]}")
        if res.get("restart"):
            render.warn("its routes stay loaded until the TUI restarts")
        return

    if verb:
        render.info("usage: /plugins [browse | install <id|url|path> | enable <kind> <id> | "
                    "disable <kind> <id> | remove <kind> <id>]")
        return

    # no verb -> the inventory
    rows = []
    for e in ext.inventory():
        state = "on" if e["enabled"] else "OFF"
        kind = e["kind"]
        if e.get("connector"):
            kind += " · connector"
        if e.get("status"):
            state += " " + e["status"]
        rows.append(("{} · {}".format(state, kind),
                     "{} ({}) — {}".format(e["title"], e["id"], e["description"])))
    render.table("plugins", rows)
    render.info("manage them with: /plugins browse | install | enable | disable | remove")


def _connector_status(pid: str) -> dict:
    """A connector's live state, via its <id>_status tool when it has one."""
    import plugin_loader
    names = []
    for p in plugin_loader.list_loaded():
        if p["id"] == pid:
            names = p.get("tool_names") or []
    status_tool = next((n for n in names if n.endswith("_status")), None)
    if not status_tool:
        return {"error": "no status tool"}
    return plugin_loader.execute_tool(status_tool, {}) or {}


def _connectors(ctx, arg: str) -> None:
    """The 🔌 Connectors panel as a command: list, detail, and sign in."""
    import plugin_loader
    ext = _ext()
    conns = [e for e in ext.inventory() if e["kind"] == "plugin" and e.get("connector")]
    if not conns:
        render.warn("no connectors installed — services you sign into appear here")
        return

    parts = (arg or "").strip().split()
    if parts and parts[0].lower() == "connect":
        if len(parts) < 2:
            render.error("usage: /connectors connect <id>")
            return
        pid = parts[1]
        info = plugin_loader.connect_info(pid)
        if "error" in info:
            render.error(info["error"])
            return
        if info.get("connected"):
            render.ok(f"already connected as {info.get('account')}")
            return
        url = info.get("url")
        if not url:
            render.warn("this connector has no terminal sign-in flow")
            return
        render.info("open this in your browser to sign in:")
        render.info(url)
        if info.get("note"):
            render.warn(info["note"])
        try:
            import webbrowser
            webbrowser.open(url)
            render.info("opened in your browser — when it says 'connected', run /connectors to confirm")
        except Exception:  # noqa: BLE001 - no browser is fine, the URL is printed
            render.info("could not open a browser automatically — open the URL above, "
                        "then run /connectors")
        return

    # detail for one connector
    if parts:
        pid = parts[0]
        if pid not in [c["id"] for c in conns]:
            render.error(f"no connector named {pid!r}")
            render.info("installed: " + ", ".join(c["id"] for c in conns))
            return
        st = _connector_status(pid)
        rows = [("id", pid)]
        if st.get("connected"):
            rows += [("state", "connected"), ("account", st.get("account") or ""),
                     ("method", st.get("method") or "")]
        else:
            rows += [("state", "not connected"),
                     ("configured", "yes" if st.get("configured") else "no")]
            if st.get("error"):
                rows.append(("error", st["error"]))
        render.table(f"connector — {pid}", rows)
        render.info("sign in with /connectors connect " + pid)
        return

    # list with live status
    rows = []
    for c in conns:
        st = _connector_status(c["id"])
        if st.get("connected"):
            state = f"connected · {st.get('account')}"
        elif st.get("configured"):
            state = "not connected"
        else:
            state = "setup needed"
        rows.append((state, "{} — {}".format(c["title"], c["description"])))
    render.table("connectors", rows)
    render.info("detail with /connectors <id> · sign in with /connectors connect <id>")


_TABLE = {
    "/help": _help, "/?": _help, "/setup": _setup,
    "/model": _model, "/models": _models, "/specs": _specs, "/llama": _llama,
    "/copy": _copy, "/yank": _copy,
    "/provider": _provider, "/provider-add": _provider_add,
    "/key": _key, "/keys": _keys, "/status": _status, "/base-url": _base_url,
    "/set": _set_token, "/token": _set_token,
    "/system": _system, "/clear": _clear, "/history": _history,
    "/save": _save, "/echo": _echo, "/start": _start,
    "/download": _download, "/pull": _download,
    "/search": _search, "/find": _search,
    "/quit": _quit, "/exit": _quit, "/q": _quit,
    "/memory": _memory, "/mem": _memory,
    "/plugins": _plugins, "/connectors": _connectors,
}


def _apply(ctx) -> None:
    """Rebuild the live backend from the current provider + model."""
    from .backend import OpenAICompatBackend

    if isinstance(ctx.backend, EchoBackend):
        return
    ctx.backend = OpenAICompatBackend(
        base_url=ctx.cfg.base_url,
        model=ctx.session.model,
        api_key=ctx.cfg.api_key,
        timeout=getattr(ctx.args, "timeout", 120.0),
        temperature=getattr(ctx.args, "temperature", 0.7),
        max_tokens=ctx.cfg.max_tokens,
    )
    ctx.real_backend = ctx.backend


def is_command(line: str) -> bool:
    """True when the first word of ``line`` is a command this build knows.

    Needed because a PATH also begins with ``/``: pasting
    ``/home/tc/Pictures/shot.png`` used to be answered with "unknown command", and
    the image was never seen. The caller can ask this first and treat an unknown
    word that names a real path as a message instead.
    """
    return line.strip().partition(" ")[0] in _TABLE


def handle(line: str, ctx) -> bool:
    """Run a slash command. Returns True if the line was consumed."""
    name, _, arg = line.strip().partition(" ")
    fn = _TABLE.get(name)
    if fn is None:
        render.error(f"unknown command {name} — try /help")
        return True
    fn(ctx, arg.strip())
    return True
