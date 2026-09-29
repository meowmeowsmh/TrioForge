"""The guided setup: provider -> model -> API key -> chat.

This exists because remembering ``/provider deepseek`` and ``/key sk-...`` is a
bad way to start. Instead the user is shown a numbered, searchable list at every
step, and can type either the number or the name.

Navigation is a small state machine rather than three straight-line calls, so
**every step can go back**:

    step 1 provider  --back-->  (leaves the wizard)
    step 2 model     --back-->  step 1
    step 3 api key   --back-->  step 2

Type ``back`` (or ``b``, or ``0``) at any prompt to go back one step, and
``cancel`` (or Ctrl-C) to leave without changing anything.
"""

from __future__ import annotations

from . import localmodels, providers, render

BACK = -1          # "go back one step"
STEP_PROVIDER = 1
STEP_MODEL = 2
STEP_KEY = 3
STEP_DONE = 4


class Cancelled(Exception):
    """Raised when the user backs out with Ctrl-C or 'cancel'."""


def _prompt_session(**kw):
    from prompt_toolkit import PromptSession
    from prompt_toolkit.styles import Style

    return PromptSession(
        style=Style.from_dict({"prompt": "bold #7dcfff"}),
        **kw,
    )


def choose(title: str, items: list[tuple[str, str]], default: int = 0,
           allow_back: bool = True) -> int:
    """Show a numbered list and ask. Returns the index, or :data:`BACK`.

    ``items`` is a list of (name, description). The user may type the number,
    the exact name, or part of it - a Tab-completer offers what is available.
    Raises :class:`Cancelled` on Ctrl-C or 'cancel'.
    """
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.history import InMemoryHistory

    if not items:
        raise Cancelled()

    render.blank()
    render.heading(title)
    for i, (name, desc) in enumerate(items, 1):
        render.option(f"{i:>2}", name, desc, current=(i - 1) == default)
    if allow_back:
        render.option(" 0", "back", "return to the previous step", current=False)
    render.blank()

    names = [n for n, _ in items]
    lookup: dict[str, int] = {}
    for i, n in enumerate(names):
        lookup[n.lower()] = i
        lookup[str(i + 1)] = i
    if allow_back:
        for word in ("back", "b", "0"):
            lookup[word] = BACK

    completer = WordCompleter(
        names + [str(i + 1) for i in range(len(items))]
        + (["back"] if allow_back else []),
        ignore_case=True, match_middle=True)

    session = _prompt_session(completer=completer,
                              complete_while_typing=True,
                              history=InMemoryHistory())

    while True:
        try:
            raw = session.prompt("  select › ").strip()
        except (EOFError, KeyboardInterrupt):
            raise Cancelled() from None

        if not raw:
            return default
        if raw.lower() in ("cancel", "quit", "q", "exit"):
            raise Cancelled()
        if raw.lower() in lookup:
            return lookup[raw.lower()]
        # prefix match, so "gem" finds gemini
        hits = [i for i, n in enumerate(names) if n.lower().startswith(raw.lower())]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            render.warn(f"{raw!r} matches {len(hits)}: "
                        + ", ".join(names[i] for i in hits[:6]))
            continue
        hint = ", or 'back' to go back" if allow_back else ""
        render.warn(f"no match for {raw!r} — type a number{hint}")


def ask_key(provider_name: str, label: str, current: str = "") -> tuple[str, str]:
    """Ask about the API key.

    Returns ``(action, value)`` where action is one of:

    * ``"keep"``  - leave the saved key alone
    * ``"set"``   - store ``value`` (a literal key)
    * ``"env"``   - store ``value`` (an ``${ENV_VAR}`` reference)
    * ``"clear"`` - remove the saved key
    * ``"back"``  - go back a step
    """
    from prompt_toolkit.history import InMemoryHistory

    render.blank()
    render.heading(f"Step 3 of 3 — API key for {label}")

    have = bool(current)
    if have:
        render.table("saved", [
            ("provider", provider_name),
            ("key", providers.masked(current)),
        ])
        render.blank()
        render.info("Enter          keep this key")
        render.info("change         replace it with a different one")
        render.info("remove         forget it (fall back to no key)")
        render.info("back           pick a different model")
    else:
        render.info("paste the key and press Enter — it is hidden as you type")
        render.info("Enter alone    store ${ENV_VAR} and read it from the environment")
        render.info("back           pick a different model")
    render.blank()

    session = _prompt_session(history=InMemoryHistory())
    prompt = "  change › " if have else "  key › "
    try:
        raw = session.prompt(prompt, is_password=not have).strip()
    except (EOFError, KeyboardInterrupt):
        raise Cancelled() from None

    low = raw.lower()
    if low in ("back", "b"):
        return "back", ""
    if have:
        if not raw:
            return "keep", ""
        if low in ("change", "c"):
            return _ask_new_key(provider_name, label)
        if low in ("remove", "delete", "clear", "none"):
            return "clear", ""
        # anything else at the change prompt is treated as the new key itself
        return ("env", raw) if raw.startswith("${") else ("set", raw)

    if not raw:
        return "env", f"${{{provider_name.upper()}_API_KEY}}"
    return ("env", raw) if raw.startswith("${") else ("set", raw)


def _ask_new_key(provider_name: str, label: str) -> tuple[str, str]:
    """Second prompt when the user chose 'change'."""
    from prompt_toolkit.history import InMemoryHistory

    render.blank()
    render.info("new key — hidden as you type; Enter alone reads ${ENV_VAR}")
    render.info("back           cancel the change")
    session = _prompt_session(history=InMemoryHistory())
    try:
        raw = session.prompt("  new key › ", is_password=True).strip()
    except (EOFError, KeyboardInterrupt):
        raise Cancelled() from None
    if raw.lower() in ("back", "b"):
        return "back", ""
    if not raw:
        return "env", f"${{{provider_name.upper()}_API_KEY}}"
    return ("env", raw) if raw.startswith("${") else ("set", raw)


def setup(cfg: providers.Config, backend=None) -> providers.Config:
    """Run the whole flow. Returns the updated config (already saved)."""
    from .backend import fetch_models, wait_ready

    step = STEP_PROVIDER
    name = cfg.provider
    model = ""
    local_model = None

    while step != STEP_DONE:
        # ------------------------------------------------------------ provider
        if step == STEP_PROVIDER:
            rows = []
            for pname, entry in sorted(cfg.providers.items()):
                label = entry.get("label") or entry.get("base_url", "")
                note = entry.get("note", "")
                rows.append((pname, f"{label} — {note}" if note else label))
            rows.sort(key=lambda r: (0 if _is_local(cfg, r[0]) else 1, r[0]))

            try:
                idx = choose("Step 1 of 3 — choose a provider", rows,
                             default=0, allow_back=False)
            except Cancelled:
                render.warn("setup cancelled — nothing changed")
                raise

            name = rows[idx][0]
            cfg.provider = name

            # Pre-load whatever step 2 will need.
            local_model = None
            offline = localmodels.available() if _is_local(cfg, name) else []
            if offline:
                rows2 = [(m.name, f"{m.size_gb:.1f} GB  ·  {m.caps_label}"
                          + ("  ·  vision projector paired" if m.projector else ""))
                         for m in offline]
            else:
                names = fetch_models(cfg.base_url, cfg.api_key) \
                    or list(cfg.current.get("models") or [])
                rows2 = [(n, "suggested") for n in names]
            step = STEP_MODEL

        # --------------------------------------------------------------- model
        elif step == STEP_MODEL:
            if not rows2:
                render.blank()
                render.warn(f"{name} did not list any models")
                render.info("the API key may be missing or wrong — "
                            "you can go back and pick another provider")
                rows2 = [("(type the model name yourself)", "manual entry")]

            try:
                midx = choose(f"Step 2 of 3 — choose a model  ({name})", rows2,
                              default=0, allow_back=True)
            except Cancelled:
                render.warn("setup cancelled — nothing changed")
                raise

            if midx == BACK:
                step = STEP_PROVIDER
                continue

            if offline:
                local_model = offline[midx]
                model = local_model.path
            else:
                model = rows2[midx][0]
                if model.startswith("("):
                    session = _prompt_session()
                    try:
                        model = session.prompt("  model name › ").strip()
                    except (EOFError, KeyboardInterrupt):
                        raise Cancelled() from None
                    if model.lower() == "back":
                        continue
                    if not model:
                        continue
            step = STEP_KEY if not _is_local(cfg, name) else STEP_DONE

        # ----------------------------------------------------------------- key
        elif step == STEP_KEY:
            try:
                action, value = ask_key(name, cfg.providers[name].get("label") or name,
                                        current=cfg.raw_key)
            except Cancelled:
                render.warn("setup cancelled — nothing changed")
                raise

            if action == "back":
                step = STEP_MODEL
                continue
            if action == "keep":
                render.info(f"keeping the saved key for {name}")
            elif action == "clear":
                cfg.set_key("")
                render.warn(f"removed the saved key for {name}")
            elif action == "env":
                cfg.set_key(value)
                render.ok(f"{name}: stored {value} (read from the environment)")
            else:
                cfg.set_key(value)
                render.ok(f"{name}: key saved ({providers.masked(value)})")
                render.info("stored in plain text in the config — "
                            "use ${ENV_VAR} if you prefer")
            step = STEP_DONE

    # ------------------------------------------------------------------- save
    cfg.model = model
    path = providers.save(cfg)

    if _is_local(cfg, name) and local_model is not None:
        render.blank()
        try:
            if _confirm(f"Start {local_model.name} now? (loads it into memory)",
                        True):
                _start_local(local_model)
        except Cancelled:
            render.warn("not started — run /start later to load it")

    render.blank()
    shown = model.rsplit("/", 1)[-1][:-5] if model.endswith(".gguf") else model
    render.ok(f"ready — {name} · {shown}")
    render.info(f"saved to {path} (mode 600)")
    return cfg


def _confirm(question: str, default: bool = True) -> bool:
    """A yes/no prompt. Blank takes the default."""
    suffix = "Y/n" if default else "y/N"
    session = _prompt_session()
    while True:
        try:
            raw = session.prompt(f"  {question} [{suffix}] › ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            raise Cancelled() from None
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        render.warn("please answer y or n")


def _start_local(m) -> bool:
    """Load an offline .gguf through TrioForge's own server manager."""
    try:
        import llamacpp_service as svc  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        render.error(f"cannot reach TrioForge's server manager: {exc}")
        return False

    render.info(f"loading {m.name} — this can take a while ({m.size_gb:.0f}+ GB)")
    try:
        res = svc.start(model=m.path)
    except Exception as exc:  # noqa: BLE001
        render.error(f"failed to start: {exc}")
        return False

    if not res.get("running"):
        render.error(f"could not start: {res.get('error', 'unknown error')}")
        return False

    # The port opens immediately but the weights are still loading - llama-server
    # answers /health with 503 until they are in memory. Wait for it, so the very
    # first message does not fail.
    from .backend import wait_ready

    render.info("loading weights into memory — the first message will not work "
                "until this finishes")

    def tick(secs: float) -> None:
        if int(secs) % 5 == 0:
            render.info(f"  … {int(secs)}s")

    if wait_ready("http://127.0.0.1:8080/v1", timeout=900, on_tick=tick):
        render.ok(f"{m.name} is loaded and ready")
        return True
    render.warn("still loading after 15 minutes — it may need more RAM/VRAM")
    return False


def _is_local(cfg: providers.Config, name: str) -> bool:
    url = (cfg.providers.get(name) or {}).get("base_url", "")
    return "127.0.0.1" in url or "localhost" in url
