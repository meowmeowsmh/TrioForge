"""Tools the agent can call.

The descriptions are adapted from Crush's own ``internal/agent/tools/*.md``, so
the model is given the same guidance the real thing gives - particularly
"read before you edit", prefer grep/glob over shelling out to find/grep, and
match whitespace exactly when editing.

Two calling conventions are supported, because your local model needs the second
one:

* **native** - the provider returns ``tool_calls`` on the message. DeepSeek,
  Claude, Gemini, Groq and most hosted models do this. gemma-3-12b does NOT: it
  ignores the ``tools`` parameter entirely and writes a command in prose.
* **text** - the model replies with a fenced ``tool`` block. Verified working
  with gemma-3-12b locally, which is why it exists at all.

``parse_calls`` understands either, so the agent loop does not care which one a
provider used.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# ============================================================ implementation

MAX_OUTPUT = 3_000           # characters of tool output kept
# 12_000 chars is ~3k tokens per tool result, which a local model then has to
# re-read on every round. A "hello" that made the agent run ls + glob over $HOME
# (three near-12k results) is what turned a greeting into a 200 s turn on the
# 5.4 GB local model. Keep the ceiling low so tool output is a summary, not a
# directory dump.
DEFAULT_READ_LIMIT = 400     # lines, like Crush's default read limit


def _truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    """Keep tool output bounded - a runaway `cat` must not blow the context."""
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return (f"{head}\n\n... [{len(text) - limit} characters omitted] ...\n\n{tail}")


def _resolve(path: str) -> Path:
    """Relative paths resolve against the working directory, `~` is expanded."""
    p = Path(os.path.expanduser(path or "."))
    return p if p.is_absolute() else (Path.cwd() / p)


# ------------------------------------------------------------------- the tools


def t_ls(path: str = ".", **_kw) -> str:
    p = _resolve(path)
    if not p.is_dir():
        return f"error: not a directory: {p}"
    entries = []
    try:
        for e in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if e.name.startswith("."):
                continue
            if e.is_dir():
                entries.append(f"{e.name}/")
            else:
                try:
                    entries.append(f"{e.name}  ({e.stat().st_size:,} bytes)")
                except OSError:
                    entries.append(e.name)
    except OSError as exc:
        return f"error: {exc}"
    if len(entries) > 80:
        entries = entries[:80] + [f"... and {len(entries) - 80} more entries"]
    return _truncate("\n".join(entries) or "(empty)")


def t_view(file_path: str = "", offset: int = 1, limit: int = DEFAULT_READ_LIMIT,
           **_kw) -> str:
    p = _resolve(file_path)
    if not p.is_file():
        return f"error: no such file: {p}"
    try:
        size = p.stat().st_size
        if size > 2_000_000:
            return (f"error: {p} is {size / 1e6:.1f} MB - too large to read whole. "
                    f"Use grep to find the lines you need, then view with "
                    f"offset/limit.")
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"error: {exc}"
    start = max(1, int(offset or 1))
    end = min(len(lines), start - 1 + max(1, int(limit or DEFAULT_READ_LIMIT)))
    width = len(str(end))
    body = "\n".join(f"{i:>{width}}  {lines[i - 1]}" for i in range(start, end + 1))
    more = "" if end >= len(lines) else f"\n... ({len(lines) - end} more lines)"
    return _truncate(f"# {p}  ({len(lines)} lines)\n{body}{more}")


def t_write(file_path: str = "", content: str = "", **_kw) -> str:
    p = _resolve(file_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        existed = p.is_file()
        p.write_text(content, encoding="utf-8")
    except OSError as exc:
        return f"error: {exc}"
    verb = "overwrote" if existed else "created"
    return f"{verb} {p}  ({len(content.splitlines())} lines)"


def t_edit(file_path: str = "", old_string: str = "", new_string: str = "",
           replace_all: bool = False, **_kw) -> str:
    """Exact-match replacement.

    Crush insists on exact whitespace, and so does this - a fuzzy match would
    silently edit the wrong place, which is worse than failing.
    """
    p = _resolve(file_path)
    if not p.is_file():
        return f"error: no such file: {p} (view it first)"
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        return f"error: {exc}"

    if old_string == new_string:
        return "error: old_string and new_string are identical"
    count = text.count(old_string)
    if count == 0:
        return (f"error: old_string not found in {p}. It must match EXACTLY, "
                f"including indentation and line breaks. View the file and copy "
                f"the text verbatim.")
    if count > 1 and not replace_all:
        return (f"error: old_string appears {count} times in {p}. Add more "
                f"surrounding context to make it unique, or set replace_all.")
    text = text.replace(old_string, new_string) if replace_all \
        else text.replace(old_string, new_string, 1)
    try:
        p.write_text(text, encoding="utf-8")
    except OSError as exc:
        return f"error: {exc}"
    n = count if replace_all else 1
    return f"edited {p}  ({n} replacement{'s' if n != 1 else ''})"


def _kill_tree(pid: int) -> None:
    """Kill a process and every descendant, so a timeout cannot orphan them."""
    try:
        import psutil
        proc = psutil.Process(pid)
        for child in proc.children(recursive=True):
            try:
                child.kill()
            except Exception:
                pass
        proc.kill()
    except Exception:
        pass


def t_bash(command: str = "", working_dir: str = "", **_kw) -> str:
    if not command.strip():
        return "error: command is empty"
    cwd = _resolve(working_dir) if working_dir else Path.cwd()
    if not cwd.is_dir():
        return f"error: not a directory: {cwd}"
    try:
        # stdin=DEVNULL: a command must not be able to read (or hijack) the TTY
        # the TUI is drawing on. Popen (not run) so a timeout can kill the whole
        # tree: run() only killed the shell and left its children running.
        proc = subprocess.Popen(
            command, shell=True, cwd=cwd, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True)
        out, _ = proc.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        try:
            proc.kill()
        except Exception:
            pass
        return "error: command exceeded the 120s limit (its process tree was killed)"
    except OSError as exc:
        return f"error: {exc}"
    out = (out or "").strip() or "(no output)"
    return _truncate(f"<cwd>{cwd}</cwd>\nexit={proc.returncode}\n{out}")


def t_grep(pattern: str = "", path: str = ".", include: str = "", **_kw) -> str:
    p = _resolve(path)
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return f"error: bad regex: {exc}"
    files = [p] if p.is_file() else [f for f in p.rglob(include or "*") if f.is_file()]
    hits: list[str] = []
    for f in files:
        if any(part.startswith(".") or part in ("__pycache__", "node_modules")
               for part in f.parts):
            continue
        try:
            if f.stat().st_size > 2_000_000:
                continue
            for i, line in enumerate(f.read_text(encoding="utf-8",
                                                 errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{f}:{i}: {line.strip()[:160]}")
                    if len(hits) >= 80:
                        return _truncate("\n".join(hits) + "\n(more matches omitted)")
        except (OSError, ValueError):
            continue
    return _truncate("\n".join(hits) or f"(no matches for {pattern!r} in {p})")


def t_glob(pattern: str = "*", path: str = ".", **_kw) -> str:
    p = _resolve(path)
    try:
        found = sorted(str(f.relative_to(p)) for f in p.rglob(pattern) if f.is_file())
    except OSError as exc:
        return f"error: {exc}"
    if len(found) > 120:
        found = found[:120] + [f"... and {len(found) - 120} more files"]
    return _truncate("\n".join(found) or f"(nothing matching {pattern!r})")


def t_todos(todos: list | None = None, **_kw) -> str:
    """The model's own checklist. Rendering it back keeps a long task on rails."""
    if not todos:
        return "(no todos)"
    lines = []
    for t in todos:
        if isinstance(t, dict):
            mark = {"completed": "[x]", "in_progress": "[~]"}.get(
                str(t.get("status", "")).lower(), "[ ]")
            lines.append(f"{mark} {t.get('content') or t.get('task') or t}")
        else:
            lines.append(f"[ ] {t}")
    return "\n".join(lines)


def t_memory(action: str = "", key: str = "", value: str = "",
             query: str = "", tags: str = "", **_kw) -> str:
    """Long-term memory that outlives the session.

    Backed by TrioForge's own DuckDB vault (sqlite_data/memory.duckdb) with a
    Bloom filter gate held in RAM in front of it: a key that is definitely absent
    is answered without touching the disk at all. "recall" takes a plain sentence
    and rewrites it into candidate keys, which is what makes "what was my port
    setting again?" find ``port_setting`` without an embedding model.
    """
    import memory as mem  # noqa: PLC0415 - keeps duckdb an optional import

    if not mem.available():
        return f"error: {mem.missing_reason()}"

    action = (action or "").strip().lower()

    if action in ("remember", "set", "store", "save", "add"):
        if not key:
            return "error: remember needs a key"
        result = mem.remember(key, value, tags)
        if not result.get("ok"):
            return f"error: {result.get('error')}"
        verb = "updated" if result.get("updated") else "saved"
        note = " (truncated)" if result.get("truncated") else ""
        return (f"{verb} {result['key']!r}{note} - "
                f"{result['keys']} key(s) in the vault")

    if action in ("recall", "find", "search", "query", "ask"):
        text = query or key          # models sometimes put the sentence in `key`
        if not text:
            return "error: recall needs a sentence"
        hits = mem.recall(text)
        if not hits:
            return f"nothing remembered matching {text!r}"
        return "\n".join(f"{k}: {v}" for k, v, _score in hits)

    if action in ("lookup", "get", "read", "show"):
        if not key:
            return "error: lookup needs an exact key"
        found = mem.lookup(key)
        if found is None:
            return f"no memory stored under {key!r}"
        return found

    if action in ("forget", "delete", "remove", "rm"):
        if not key:
            return "error: forget needs a key"
        return (f"forgot {key!r}" if mem.forget(key)
                else f"nothing stored under {key!r}")

    if action in ("list", "keys", "all"):
        found = mem.keys(key)
        if not found:
            return "(the memory vault is empty)"
        shown = found[:80]
        more = f"\n... and {len(found) - 80} more" if len(found) > 80 else ""
        return "\n".join(shown) + more

    if action in ("stats", "status"):
        s = mem.stats()
        return (f"{s['keys']} keys - {s['bits']} bits ({s['bytes']} bytes RAM, "
                f"{s['hashes']} hashes, {s['fill']:.1%} full) - "
                f"{s['gate_skips']}/{s['lookups']} lookups skipped in RAM "
                f"({s['saved_pct']:.0f}% with no disk read) - "
                f"vault {s['db_bytes'] / 1024:.1f} KB")

    return ("error: action must be one of remember, recall, lookup, forget, "
            "list, stats")


# ============================================================ the registry


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    run: Callable[..., str]
    danger: bool = False          # ask the user before running
    summary: Callable[[dict], str] = field(default=lambda a: "")

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }}


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


TOOLS: dict[str, Tool] = {}


def _register(tool: Tool) -> None:
    TOOLS[tool.name] = tool


_register(Tool(
    "ls", "List files in a directory. Use this instead of running 'ls'.",
    _obj({"path": {"type": "string", "description": "directory, default ."}}, []),
    t_ls,
    summary=lambda a: f"ls {a.get('path', '.')}",
))

_register(Tool(
    "todos",
    "Record a short task list so a multi-step job stays on track. Pass the whole "
    "list each time, with status: pending | in_progress | completed.",
    _obj({"todos": {"type": "array", "items": _obj(
        {"content": {"type": "string"},
         "status": {"type": "string",
                    "enum": ["pending", "in_progress", "completed"]}},
        ["content"])}}, ["todos"]),
    t_todos,
    summary=lambda a: f"todos ({len(a.get('todos') or [])} items)",
))

_register(Tool(
    "memory",
    "Long-term memory that survives the session. Use action=remember ONLY when the "
    "user asks you to remember something - never record anything on your own "
    "initiative, however durable it sounds. It takes key+value. "
    "action=recall takes a plain sentence ('what was my port setting again?') and "
    "finds matching keys. action=lookup needs an exact key; action=list and "
    "action=stats inspect the vault. Not for files - use view/write/grep for those.",
    _obj({"action": {"type": "string", "enum": [
            "remember", "recall", "lookup", "forget", "list", "stats"]},
          "key": {"type": "string", "description": "exact key, e.g. port_setting"},
          "value": {"type": "string", "description": "what to store (remember)"},
          "query": {"type": "string", "description": "a plain sentence (recall)"},
          "tags": {"type": "string",
                   "description": "optional extra words recall can match on"}},
         ["action"]),
    t_memory,
    summary=lambda a: "memory {} {}".format(
        a.get("action", ""), a.get("key") or a.get("query") or "").strip(),
))

_register(Tool(
    "view",
    "Read a file with line numbers. Use offset/limit for large files instead of "
    "reading the whole thing. Use ls for directories.",
    _obj({"file_path": {"type": "string"},
          "offset": {"type": "integer", "description": "first line, 1-based"},
          "limit": {"type": "integer", "description": f"lines (default {DEFAULT_READ_LIMIT})"}},
         ["file_path"]),
    t_view,
    summary=lambda a: f"view {a.get('file_path', '?')}",
))

_register(Tool(
    "write",
    "Create or overwrite a file. Cannot append. Read the file first to avoid "
    "clobbering changes. For surgical edits use `edit`.",
    _obj({"file_path": {"type": "string"},
          "content": {"type": "string"}}, ["file_path", "content"]),
    t_write, danger=True,
    summary=lambda a: f"write {a.get('file_path', '?')}",
))

_register(Tool(
    "edit",
    "Replace an exact string in a file. old_string must match EXACTLY, including "
    "whitespace and line breaks - view the file first and copy it verbatim.",
    _obj({"file_path": {"type": "string"},
          "old_string": {"type": "string"},
          "new_string": {"type": "string"},
          "replace_all": {"type": "boolean"}},
         ["file_path", "old_string", "new_string"]),
    t_edit, danger=True,
    summary=lambda a: f"edit {a.get('file_path', '?')}",
))

_register(Tool(
    "bash",
    "Run a shell command. Prefer the grep/glob/ls/view tools over shelling out to "
    "find/grep/cat/ls. Each call runs in a fresh shell - no state persists.",
    _obj({"command": {"type": "string"},
          "working_dir": {"type": "string"}}, ["command"]),
    t_bash, danger=True,
    summary=lambda a: f"bash: {str(a.get('command', ''))[:70]}",
))

_register(Tool(
    "grep",
    "Search file contents with a regular expression. Prefer this over running "
    "'grep'.",
    _obj({"pattern": {"type": "string"},
          "path": {"type": "string"},
          "include": {"type": "string", "description": "glob, e.g. '*.py'"}},
         ["pattern"]),
    t_grep,
    summary=lambda a: f"grep {a.get('pattern', '?')!r}",
))

_register(Tool(
    "glob", "Find files by glob pattern, e.g. '**/*.py'. Prefer this over 'find'.",
    _obj({"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern"]),
    t_glob,
    summary=lambda a: f"glob {a.get('pattern', '?')}",
))

_register(Tool(
    "todos",
    "Record a short task list so a multi-step job stays on track. Pass the whole "
    "list each time, with status: pending | in_progress | completed.",
    _obj({"todos": {"type": "array", "items": _obj(
        {"content": {"type": "string"},
         "status": {"type": "string",
                    "enum": ["pending", "in_progress", "completed"]}},
        ["content"])}}, ["todos"]),
    t_todos,
    summary=lambda a: f"todos ({len(a.get('todos') or [])} items)",
))


def schemas(names: list[str] | None = None) -> list[dict]:
    """JSON schemas for native tool calling."""
    chosen = names or list(TOOLS)
    return [TOOLS[n].schema() for n in chosen if n in TOOLS]


# Escape sequences must never leave a tool. A command that prints
# "\x1b[?1000h\x1b[?1006h" turns the user's terminal into mouse-reporting mode,
# and from then on every mouse move arrives as literal text - the UI fills with
# "<35;37;21M<35;26;20M..." and the input line stops working. Colours, cursor
# moves, OSC title/clipboard strings and DCS strings are all stripped too: they
# are meaningless in a transcript, and the model cannot use them.
_ANSI_RE = re.compile(
    r"\x1b(?:"
    r"\[[0-?]*[ -/]*[@-~]"             # CSI - colours, cursor, mouse modes
    #                                     ^ 0x30-0x3F, so SGR mouse (\x1b[<..M)
    #                                       and the private '?' modes match too
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"   # OSC - window title, clipboard
    r"|[PX^_][^\x1b]*\x1b\\"            # DCS / SOS / PM / APC strings
    r"|[ -/][0-~]"                      # nF - charset selection, e.g. ESC ( B
    r"|[0-~]"                           # Fp/Fe/Fs - ESC 7, ESC =, ESC M, ...
    r")"
)
# Control characters except tab and newline, which are real formatting.
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _clean(text):
    """Strip escape sequences and stray control bytes from tool output."""
    if not isinstance(text, str) or not text:
        return text
    return _CTRL_RE.sub("", _ANSI_RE.sub("", text))


_SECRET_RES = [
    re.compile(r"sk-[A-Za-z0-9_-]{12,}", re.I),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9_\-\.]{10,}"),
    re.compile(r"(?i)\b(api[_-]?key|apikey|secret|password|token|authorization)"
               r"\s*[:=]\s*[\"']?[A-Za-z0-9_\-\.]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
               re.S),
]


def _redact(text: str) -> str:
    """Mask obvious secrets in tool output before it reaches the model.

    The agent can read any file it can see, including a config that holds API
    keys. Redacting here means a key the model happened to view can never be
    echoed back into its context - and in team mode, sent on to the cloud senior.
    """
    if not isinstance(text, str) or not text:
        return text
    out = _SECRET_RES[0].sub("sk-[REDACTED]", text)
    out = _SECRET_RES[1].sub(r"\1[REDACTED]", out)
    out = _SECRET_RES[2].sub(r"\1=[REDACTED]", out)
    out = _SECRET_RES[3].sub("[REDACTED PRIVATE KEY]", out)
    return out


def execute(name: str, args: dict, on_output: Callable[[str], None] | None = None) -> str:
    """Run one tool. Never raises - a result the model can react to.

    Every result passes through ``_clean`` so no escape sequence can reach the
    terminal, the transcript or the model.
    """
    tool = TOOLS.get(name)
    if tool is None:
        return (f"error: unknown tool {name!r}. "
                f"Available: {', '.join(sorted(TOOLS))}")
    if not isinstance(args, dict):
        return f"error: args must be an object, got {type(args).__name__}"
    if on_output:
        on_output(tool.summary(args))
    try:
        return _redact(_clean(tool.run(**args)))
    except TypeError as exc:
        return f"error: bad arguments for {name}: {exc}"
    except Exception as exc:  # noqa: BLE001 - a tool must never crash the agent
        return f"error: {type(exc).__name__}: {exc}"


# ============================================================ calling formats

# The text protocol. Deliberately plain so a small local model can follow it.
PROTOCOL = """\
## Calling tools

You have tools. Use them - never guess at file contents or claim to have run
something you did not run.

To call a tool, reply with ONLY a fenced block, nothing before or after:

```tool
{"name": "view", "args": {"file_path": "py/tui/backend.py", "limit": 60}}
```

To call several in one turn, emit several blocks. When you have everything you
need and the task is done, reply with normal prose instead - that ends the turn.

If the provider supports native tool calling, use that instead; both are accepted.

### Available tools

"""


def protocol_text() -> str:
    """The protocol plus a plain-language list of the tools."""
    out = [PROTOCOL]
    for name, t in TOOLS.items():
        props = t.parameters.get("properties", {})
        args = ", ".join(props) or "no arguments"
        danger = "  (asks permission first)" if t.danger else ""
        out.append(f"- `{name}({args})` - {t.description}{danger}\n")
    return "".join(out)


_CALL_RE = re.compile(r"```tool\s*(\{.*?\})\s*```", re.S)


def parse_text_calls(text: str) -> list[tuple[str, dict]]:
    """Pull ``{"name": ..., "args": {...}}`` calls out of a reply."""
    calls: list[tuple[str, dict]] = []
    for m in _CALL_RE.finditer(text or ""):
        try:
            obj = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        name = obj.get("name") or obj.get("tool")
        args = obj.get("args") or obj.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if not args and isinstance(obj, dict):
            # Small models routinely put the arguments at the TOP level:
            #   {"name": "memory", "action": "remember", "key": "user_name"}
            # That arrived here as an empty args dict, so the tool rejected
            # well-formed input ("action must be one of ...") and the model had
            # to burn a whole round trip working out the nesting. Take the rest
            # of the object as the arguments instead.
            flat = {k: v for k, v in obj.items()
                    if k not in ("name", "tool", "args", "arguments")}
            if flat:
                args = flat
        if name:
            calls.append((name, args if isinstance(args, dict) else {}))
    return calls


def parse_calls(message: dict) -> list[tuple[str, dict]]:
    """Native ``tool_calls`` if present, else the text protocol."""
    calls: list[tuple[str, dict]] = []
    for c in message.get("tool_calls") or []:
        fn = c.get("function") or {}
        name = fn.get("name")
        raw = fn.get("arguments") or "{}"
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = {}
        if name:
            calls.append((name, raw if isinstance(raw, dict) else {}))
    if calls:
        return calls
    return parse_text_calls(message.get("content") or "")


def strip_text_calls(text: str) -> str:
    """The prose around the tool blocks - usually nothing, occasionally a note."""
    return _CALL_RE.sub("", text or "").strip()
