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


def t_bash(command: str = "", working_dir: str = "", **_kw) -> str:
    if not command.strip():
        return "error: command is empty"
    cwd = _resolve(working_dir) if working_dir else Path.cwd()
    if not cwd.is_dir():
        return f"error: not a directory: {cwd}"
    try:
        r = subprocess.run(command, shell=True, cwd=cwd, capture_output=True,
                           text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return "error: command exceeded the 120s limit"
    except OSError as exc:
        return f"error: {exc}"
    out = ""
    if r.stdout:
        out += r.stdout
    if r.stderr:
        out += ("\n" if out else "") + r.stderr
    out = out.strip() or "(no output)"
    return _truncate(f"<cwd>{cwd}</cwd>\nexit={r.returncode}\n{out}")


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


def execute(name: str, args: dict, on_output: Callable[[str], None] | None = None) -> str:
    """Run one tool. Never raises - a failure is a result the model can react to."""
    tool = TOOLS.get(name)
    if tool is None:
        return (f"error: unknown tool {name!r}. "
                f"Available: {', '.join(sorted(TOOLS))}")
    if not isinstance(args, dict):
        return f"error: args must be an object, got {type(args).__name__}"
    if on_output:
        on_output(tool.summary(args))
    try:
        return tool.run(**args)
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
