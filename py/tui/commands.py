"""Slash commands.

A command is a function ``(ctx, arg) -> None`` plus one row in :data:`COMMANDS`.
Adding a command therefore means adding a function and a line of help - nothing
else in the app needs to know about it.
"""

from __future__ import annotations

from pathlib import Path

from . import localmodels, providers, render
from .backend import EchoBackend, fetch_models

# (usage, description) - also what /help prints, in this order.
COMMANDS: list[tuple[str, str]] = [
    ("/help", "show this list"),
    ("/setup", "guided setup: provider → model → API key"),
    ("/provider [name]", "list or switch provider (local, deepseek, groq, ...)"),
    ("/key [value]", "show/change the key for the CURRENT provider"),
    ("/keys [provider] [value]", "list every saved key, or change one"),
    ("/keys clear [all]", "remove every key EXCEPT the current one"),
    ("/models", "list models (offline .gguf files, or the endpoint's list)"),
    ("/specs", "what hardware was detected (GPU, memory) and what fits"),
    ("/copy [n]", "copy an answer to the clipboard (n = how many back)"),
    ("/llama [install|status]", "the bundled llama.cpp runtime (auto-installed)"),
    ("/start [name]", "load an offline .gguf into llama-server"),
    ("/model [name]", "show or switch model; 'auto' re-detects it"),
    ("/status", "server health, model, endpoint and key state"),
    ("/base-url [url]", "show or set the endpoint"),
    ("/system [text]", "show or set the system prompt"),
    ("/clear", "forget the conversation"),
    ("/history", "replay the conversation"),
    ("/save [path]", "write the transcript to a file"),
    ("/echo", "toggle the offline stub"),
    ("/quit", "leave (Ctrl-D also works)"),
]


def _help(ctx, arg: str) -> None:
    render.help_panel(COMMANDS)


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
                         [(n, d + ("  ◀ current" if n in (ctx.session.model or "") else ""))
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
        rows = [(x.name, f"{x.size_gb:.1f} GB  ·  {x.caps_label}") for x in models]
        try:
            idx = wizard.choose("load which offline model?", rows, default=0)
        except wizard.Cancelled:
            render.warn("cancelled")
            return
        m = models[idx]

    render.info(f"loading {m.name} ({m.size_gb:.1f} GB) — this takes a while")
    try:
        import llamacpp_service as svc  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        render.error(f"cannot reach TrioForge's server manager: {exc}")
        return
    try:
        res = svc.start(model=m.path)
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
            elif d.get("integrated"):
                mark, tag = "  ", "integrated - shares system RAM"
            else:
                mark, tag = "  ", "idle"
            rows.append(("  {} {}".format(mark, d["label"]),
                         "{:.2f} GB free of {:.2f} GB   {}".format(
                             d["free_gb"], d["total_gb"], tag)))
            rows.append(("", d["name"]))
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


_TABLE = {
    "/help": _help, "/?": _help, "/setup": _setup,
    "/model": _model, "/models": _models, "/specs": _specs, "/llama": _llama,
    "/copy": _copy, "/yank": _copy,
    "/provider": _provider, "/provider-add": _provider_add,
    "/key": _key, "/keys": _keys, "/status": _status, "/base-url": _base_url,
    "/system": _system, "/clear": _clear, "/history": _history,
    "/save": _save, "/echo": _echo, "/start": _start,
    "/quit": _quit, "/exit": _quit, "/q": _quit,
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
    )
    ctx.real_backend = ctx.backend


def handle(line: str, ctx) -> bool:
    """Run a slash command. Returns True if the line was consumed."""
    name, _, arg = line.strip().partition(" ")
    fn = _TABLE.get(name)
    if fn is None:
        render.error(f"unknown command {name} — try /help")
        return True
    fn(ctx, arg.strip())
    return True
