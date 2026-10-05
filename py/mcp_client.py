"""MCP (Model Context Protocol) client for TrioForge.

This is the leverage move behind the plugin system. The alternative to speaking
MCP is writing one TrioForge connector per service, which means re-implementing
Playwright, GitHub, Context7, Figma, Postgres, Sentry and the rest by hand. MCP
is an open JSON-RPC protocol, so a server someone else already wrote works here
for free - the same servers Claude, Cursor and VS Code drive.

A server is configured in ``json_configuration/mcp_servers.json``::

    {
      "servers": {
        "playwright": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]},
        "context7":   {"url": "https://mcp.context7.com/mcp",
                       "headers": {"CONTEXT7_API_KEY": "..."}}
      }
    }

Two transports are supported, both with no extra dependency:

* **stdio** - a local subprocess speaking newline-delimited JSON-RPC. This is how
  npx/uvx/pipx servers run.
* **HTTP** - a POST of JSON-RPC to a remote URL, answering with either JSON or an
  SSE stream. This is how hosted servers run.

Tools are published into the agent under ``mcp__<server>__<tool>``. The prefix is
not decoration: without it, two servers that both expose a ``search`` tool would
silently overwrite each other, and the model would call the wrong one.

Connections are made in a background thread at startup, because a first run of
``npx -y`` downloads a package and blocking the web server on that would make
TrioForge look broken. A server that fails to start is reported and skipped, with
its error kept for the UI - exactly like the plugin loader.
"""

import atexit
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from typing import Dict, List

import requests

from paths import root_path

logger = logging.getLogger(__name__)

CONFIG_PATH = root_path("json_configuration", "mcp_servers.json")

#: MCP revision we speak. Chosen for the widest server compatibility; the
#: handshake negotiates down if a server only knows an older one.
PROTOCOL_VERSION = "2024-11-05"

CONNECT_TIMEOUT = 45          # seconds: generous, because `npx -y` may download
CALL_TIMEOUT = 180
BACKOFF_AFTER_FAILURE = 30    # don't retry a dead server on every single turn

#: Cap on a single tool result. A browser server will happily return a whole
#: page; unbounded, that evicts the conversation from the context window.
MAX_RESULT_CHARS = 30000

TOOL_PREFIX = "mcp__"

STATUS_DISABLED = "disabled"
STATUS_CONNECTING = "connecting"
STATUS_READY = "ready"
STATUS_ERROR = "error"

_servers: Dict[str, "McpServer"] = {}
#: prefixed tool name -> (server id, the tool's real name). A reverse map rather
#: than parsing the name back apart, so a server id or tool name containing "_"
#: cannot make two different tools collide.
_tool_index: Dict[str, tuple] = {}
_index_lock = threading.Lock()


# ── configuration ───────────────────────────────────────────────────────────

def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    servers = data.get("servers") if isinstance(data, dict) else None
    return servers if isinstance(servers, dict) else {}


def save_config(servers: dict) -> None:
    """Write the server map.

    The on-disk file is ``{"servers": {...}}`` while ``load_config`` returns the
    inner map, so a caller holding the whole object would otherwise write a
    nested ``servers.servers`` and end up with a server named "servers". Accept
    either shape rather than relying on the caller to remember which is which.
    """
    if isinstance(servers, dict) and isinstance(servers.get("servers"), dict) \
            and not any(k in servers for k in ("command", "url")):
        servers = servers["servers"]
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"servers": servers}, fh, indent=2)
    os.replace(tmp, CONFIG_PATH)


def config_path() -> str:
    return CONFIG_PATH


# ── transports ──────────────────────────────────────────────────────────────

def _spawn_command(command: str, args: List[str]) -> List[str]:
    """Build an argv that CreateProcess can actually start on Windows.

    ``npx`` and ``uvx`` resolve to ``.cmd`` shims there, and CreateProcess cannot
    execute a batch file - only cmd.exe can. Handing it the bare name fails with
    FileNotFoundError, which reads like "MCP is broken" rather than "wrap it".
    """
    found = shutil.which(command)
    if not found:
        return [command] + list(args)
    if os.name == "nt" and found.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", found] + list(args)
    return [found] + list(args)


class _StdioTransport:
    """A local MCP server subprocess, newline-delimited JSON-RPC on stdio."""

    def __init__(self, spec: dict):
        self.argv = _spawn_command(spec.get("command", ""), spec.get("args") or [])
        self.env_extra = spec.get("env") or {}
        self.cwd = spec.get("cwd") or None
        self.proc = None
        self._pending: Dict[object, queue.Queue] = {}
        self._lock = threading.Lock()
        self._stderr_tail: List[str] = []

    def start(self) -> None:
        env = os.environ.copy()
        for key, value in (self.env_extra or {}).items():
            env[str(key)] = str(value)
        kwargs = {}
        if os.name == "nt":
            # Without this a console window flashes up for every server.
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.proc = subprocess.Popen(
                self.argv,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, cwd=self.cwd, text=True, encoding="utf-8", errors="replace",
                bufsize=1, **kwargs
            )
        except FileNotFoundError:
            # By far the most common setup failure, so name it plainly instead of
            # surfacing WinError 2, which reads like a TrioForge bug.
            raise RuntimeError(
                "'{}' was not found. Install it and make sure it is on PATH.".format(self.argv[0]))
        except PermissionError:
            raise RuntimeError("'{}' is not executable.".format(self.argv[0]))
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._stderr_loop, daemon=True).start()

    def _read_loop(self) -> None:
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    # Servers are allowed to print banners; they are not protocol.
                    continue
                mid = msg.get("id")
                if mid is None:
                    continue
                with self._lock:
                    box = self._pending.get(mid)
                if box is not None:
                    box.put(msg)
        except Exception:
            pass
        finally:
            # The pipe closed: fail every waiter instead of letting them time out.
            self._fail_waiters("the server exited")

    def _stderr_loop(self) -> None:
        try:
            for line in self.proc.stderr:
                line = line.rstrip()
                if line:
                    self._stderr_tail.append(line)
                    del self._stderr_tail[:-12]
        except Exception:
            pass

    def _fail_waiters(self, reason: str) -> None:
        with self._lock:
            boxes = list(self._pending.values())
        for box in boxes:
            box.put({"__transport_error__": reason})

    def request(self, payload: dict, timeout: float) -> dict:
        if self.proc is None or self.proc.poll() is not None:
            raise RuntimeError(self.diagnostics() or "the server is not running")
        box: queue.Queue = queue.Queue()
        with self._lock:
            self._pending[payload["id"]] = box
        try:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()
        except Exception as e:
            with self._lock:
                self._pending.pop(payload["id"], None)
            raise RuntimeError("could not write to the server: {}".format(e))
        try:
            msg = box.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("no reply within {}s{}".format(
                int(timeout), ": " + self.diagnostics() if self.diagnostics() else ""))
        finally:
            with self._lock:
                self._pending.pop(payload["id"], None)
        if "__transport_error__" in msg:
            raise RuntimeError(msg["__transport_error__"])
        return msg

    def notify(self, payload: dict) -> None:
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.stdin.write(json.dumps(payload) + "\n")
                self.proc.stdin.flush()
        except Exception:
            pass

    def diagnostics(self) -> str:
        return " | ".join(self._stderr_tail[-3:]) if self._stderr_tail else ""

    def close(self) -> None:
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass


def _parse_sse(text: str, want_id):
    """Pull the JSON-RPC message out of an SSE stream body."""
    for block in text.split("\n\n"):
        data_lines = [ln[5:].strip() for ln in block.splitlines() if ln.startswith("data:")]
        if not data_lines:
            continue
        try:
            msg = json.loads("\n".join(data_lines))
        except ValueError:
            continue
        if want_id is None or msg.get("id") == want_id:
            return msg
    return None


class _HttpTransport:
    """A remote MCP server reached over streamable HTTP."""

    def __init__(self, spec: dict):
        self.url = spec.get("url", "")
        self.headers = {str(k): str(v) for k, v in (spec.get("headers") or {}).items()}
        token = spec.get("token")
        if token and "Authorization" not in self.headers:
            self.headers["Authorization"] = "Bearer {}".format(token)
        self._session_id = None

    def start(self) -> None:
        if not self.url:
            raise RuntimeError("no url configured")

    def request(self, payload: dict, timeout: float) -> dict:
        headers = dict(self.headers)
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json, text/event-stream"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            resp = requests.post(self.url, json=payload, headers=headers, timeout=timeout)
        except requests.exceptions.RequestException as e:
            raise RuntimeError("cannot reach {}: {}".format(self.url, e))
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self._session_id = sid
        if resp.status_code >= 400:
            raise RuntimeError("HTTP {} from {}: {}".format(
                resp.status_code, self.url, resp.text[:200]))
        if "text/event-stream" in (resp.headers.get("Content-Type") or ""):
            msg = _parse_sse(resp.text, payload.get("id"))
            if msg is None:
                raise RuntimeError("the SSE reply carried no JSON-RPC message")
            return msg
        try:
            return resp.json()
        except ValueError:
            raise RuntimeError("the reply was not JSON: {}".format(resp.text[:200]))

    def notify(self, payload: dict) -> None:
        try:
            self.request(payload, 20)
        except Exception:
            pass

    def diagnostics(self) -> str:
        return ""

    def close(self) -> None:
        pass


# ── one server ──────────────────────────────────────────────────────────────

class McpServer:
    def __init__(self, sid: str, spec: dict):
        self.id = sid
        self.spec = spec or {}
        self.status = STATUS_DISABLED if self.spec.get("enabled") is False else STATUS_CONNECTING
        self.error = ""
        self.tools: List[dict] = []
        self.transport = None
        self._next_id = 1
        self._failed_at = 0.0
        self._lock = threading.Lock()

    # -- protocol ----------------------------------------------------------

    def _rpc(self, method: str, params: dict, timeout: float) -> dict:
        with self._lock:
            rid = self._next_id
            self._next_id += 1
        msg = self.transport.request(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}, timeout)
        if "error" in msg:
            err = msg["error"] or {}
            raise RuntimeError("{} (code {})".format(
                err.get("message", "protocol error"), err.get("code")))
        return msg.get("result") or {}

    def connect(self) -> bool:
        """Start the transport and complete the MCP handshake."""
        if self.spec.get("enabled") is False:
            self.status = STATUS_DISABLED
            return False
        self.status = STATUS_CONNECTING
        self.error = ""
        self.tools = []
        try:
            if self.spec.get("url"):
                self.transport = _HttpTransport(self.spec)
            elif self.spec.get("command"):
                self.transport = _StdioTransport(self.spec)
            else:
                raise RuntimeError("needs either a command or a url")
            self.transport.start()
            result = self._rpc("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "TrioForge", "version": "1.0"},
            }, CONNECT_TIMEOUT)
            self.transport.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})
            info = result.get("serverInfo") or {}
            self.server_name = info.get("name") or self.id
            self._fetch_tools()
            self.status = STATUS_READY
            logger.info("MCP server '%s' ready with %d tool(s)", self.id, len(self.tools))
            return True
        except Exception as e:
            self.status = STATUS_ERROR
            self.error = _brief(e) or type(e).__name__
            self._failed_at = time.time()
            logger.warning("MCP server '%s' failed: %s", self.id, self.error)
            self._teardown()
            return False

    def _fetch_tools(self) -> None:
        result = self._rpc("tools/list", {}, CONNECT_TIMEOUT)
        self.tools = [t for t in (result.get("tools") or []) if t.get("name")]

    def ensure_connected(self) -> bool:
        if self.status == STATUS_READY:
            return True
        if self.status == STATUS_DISABLED:
            return False
        # Don't hammer a server that just died; the model may call a stale tool.
        if self.status == STATUS_ERROR and (time.time() - self._failed_at) < BACKOFF_AFTER_FAILURE:
            return False
        return self.connect()

    def call(self, tool_name: str, args: dict) -> dict:
        try:
            result = self._rpc("tools/call",
                               {"name": tool_name, "arguments": args or {}}, CALL_TIMEOUT)
        except Exception as e:
            return {"error": "{} ({}): {}".format(self.id, tool_name, _brief(e) or type(e).__name__)}
        text = _flatten(result)
        if result.get("isError"):
            return {"error": text or "the tool reported an error"}
        return {"result": text}

    def _teardown(self) -> None:
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:
                pass
            self.transport = None

    def close(self) -> None:
        self._teardown()

    # -- exposure ----------------------------------------------------------

    def openai_tools(self) -> List[dict]:
        out = []
        for tool in self.tools:
            out.append({
                "type": "function",
                "function": {
                    "name": prefixed(self.id, tool["name"]),
                    # Naming the server in the description is what lets the model
                    # choose between two similar tools from different servers.
                    "description": "[{}] {}".format(
                        self.id, tool.get("description") or tool["name"])[:1024],
                    "parameters": tool.get("inputSchema")
                                  or {"type": "object", "properties": {}},
                },
            })
        return out

    def describe(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "error": self.error,
            "transport": "http" if self.spec.get("url") else "stdio",
            "command": self.spec.get("command", ""),
            "url": self.spec.get("url", ""),
            "enabled": self.spec.get("enabled") is not False,
            "tool_count": len(self.tools),
            "tool_names": [prefixed(self.id, t["name"]) for t in self.tools],
        }


# ── helpers ─────────────────────────────────────────────────────────────────

def prefixed(sid: str, tool_name: str) -> str:
    return "{}{}__{}".format(TOOL_PREFIX, sid, tool_name)


def _brief(e: Exception) -> str:
    text = str(e).strip()
    return text[:300]


def _flatten(result: dict) -> str:
    """Turn an MCP tool result into text for the model."""
    content = result.get("content")
    parts = []
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                parts.append(str(item))
            elif item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif item.get("type") == "image":
                parts.append("[image: {} bytes]".format(
                    len(item.get("data") or "")))
            elif item.get("type") == "resource":
                res = item.get("resource") or {}
                parts.append(res.get("text") or json.dumps(res)[:2000])
            else:
                parts.append(json.dumps(item)[:2000])
    elif content is not None:
        parts.append(str(content))
    if not parts and result:
        parts.append(json.dumps(result)[:2000])
    text = "\n".join(p for p in parts if p).strip()
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "\n…[truncated, {} chars total]".format(len(text))
    return text


def _reindex() -> None:
    index = {}
    for srv in _servers.values():
        if srv.status == STATUS_READY:
            for tool in srv.tools:
                index[prefixed(srv.id, tool["name"])] = (srv.id, tool["name"])
    with _index_lock:
        _tool_index.clear()
        _tool_index.update(index)


# ── lifecycle ───────────────────────────────────────────────────────────────

def load_all() -> List[dict]:
    """Read the config and connect to every enabled server in the background.

    Never blocks: a first ``npx -y`` can take a minute, and the web server must
    not wait on it. Tools appear as their server becomes ready.
    """
    _servers.clear()
    with _index_lock:
        _tool_index.clear()
    cfgs = load_config()
    for sid, spec in cfgs.items():
        if not isinstance(spec, dict):
            continue
        srv = McpServer(sid, spec)
        _servers[sid] = srv
        if srv.status == STATUS_DISABLED:
            continue
        threading.Thread(target=_connect_then_index, args=(srv,), daemon=True).start()
    return list_servers()


def _connect_then_index(srv: "McpServer") -> None:
    srv.connect()
    _reindex()


def add_server(sid: str, spec: dict, connect: bool = True) -> dict:
    """Save a server and start connecting to it in the background.

    Deliberately non-blocking, like ``load_all``: this is called from an HTTP
    route, and a first ``npx -y`` would otherwise hold the request open for a
    minute. The caller polls ``list_servers`` for the status.
    """
    cfgs = load_config()
    cfgs[sid] = spec
    save_config(cfgs)
    old = _servers.get(sid)
    if old is not None:
        old.close()
    srv = McpServer(sid, spec)
    _servers[sid] = srv
    _reindex()
    if connect and srv.status != STATUS_DISABLED:
        threading.Thread(target=_connect_then_index, args=(srv,), daemon=True).start()
    return srv.describe()


def remove_server(sid: str) -> bool:
    cfgs = load_config()
    existed = sid in cfgs or sid in _servers
    cfgs.pop(sid, None)
    save_config(cfgs)
    srv = _servers.pop(sid, None)
    if srv is not None:
        srv.close()
    _reindex()
    return existed


def reconnect_all() -> List[dict]:
    for srv in _servers.values():
        threading.Thread(target=_connect_then_index, args=(srv,), daemon=True).start()
    return list_servers()


def list_servers() -> List[dict]:
    return [srv.describe() for srv in _servers.values()]


def shutdown() -> None:
    for srv in list(_servers.values()):
        srv.close()


atexit.register(shutdown)


# ── agent-facing surface (same shape as plugin_loader / skills_loader) ──────

def collect_tools() -> List[dict]:
    """Every tool from every ready server."""
    out = []
    for srv in _servers.values():
        if srv.status == STATUS_READY:
            out.extend(srv.openai_tools())
    return out


def execute_tool(name: str, args: dict):
    """Run an MCP tool. Returns None when the name is not an MCP tool."""
    if not name.startswith(TOOL_PREFIX):
        return None
    with _index_lock:
        owner = _tool_index.get(name)
    if owner is None:
        # The server may still have been connecting when the tools were listed;
        # give it one synchronous chance before reporting the name as unknown.
        _connect_pending()
        with _index_lock:
            owner = _tool_index.get(name)
    if owner is None:
        return {"error": "No MCP tool named '{}'. Its server may be disconnected - "
                         "check the MCP panel.".format(name)}
    sid, tool_name = owner
    srv = _servers.get(sid)
    if srv is None:
        return {"error": "The MCP server '{}' is no longer configured.".format(sid)}
    if not srv.ensure_connected():
        return {"error": "The MCP server '{}' is not available: {}".format(
            sid, srv.error or srv.status)}
    return srv.call(tool_name, args)


def _connect_pending() -> None:
    for srv in _servers.values():
        if srv.status == STATUS_CONNECTING:
            srv.connect()
    _reindex()


def wait_ready(timeout: float = 30.0) -> bool:
    """Block until no server is still connecting. Used by tests and the UI."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(s.status == STATUS_CONNECTING for s in _servers.values()):
            _reindex()
            return True
        time.sleep(0.05)
    _reindex()
    return False
