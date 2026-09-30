"""The agent loop: model -> tools -> model -> ... until the task is done.

This is the idea taken from Crush's ``internal/agent``: instead of one request
and one reply, a turn keeps going while the model asks for tools. Each tool
result is fed back as a message, so the model can read a file, edit it, run the
tests and report - the thing that makes it an agent rather than a chatbot.

Two things are deliberately different from Crush, both because of what your
machine can actually run:

* **The text protocol is first class.** Crush relies on native ``tool_calls``.
  gemma-3-12b ignores the ``tools`` parameter completely, so a fenced-block
  protocol is used instead when the model does not support native calling. Both
  are parsed, so a hosted model gets the better path and a local one still works.
* **Permissions are asked in the terminal, not over a protocol.** Crush has a
  client/server split; this is one process, so approval is a callback.

Every dangerous tool (write, edit, bash) asks before it runs, and the loop has a
hard iteration cap so a confused model cannot spin forever.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from . import tools as TL
from .backend import BackendError

MAX_STEPS = 12          # tool-calling rounds in one turn
APPROVE_ALL = "all"     # the user chose "yes, and stop asking"

# Adapted from Crush's internal/agent/templates/coder.md.tpl. The rules are kept
# because they are what makes an agent behave; the parts about MCP, LSP, skills
# and git attribution are dropped because forge has none of those.
SYSTEM_PROMPT = """\
You are TrioForge, an AI coding assistant running in the user's terminal, in the
directory {cwd}.

<critical_rules>
These override everything else.

1. READ BEFORE YOU EDIT. Never edit a file you have not read in this
   conversation. Match text EXACTLY, including indentation and line breaks.
2. ACT ONLY ON TASKS. "Autonomous" applies when the user gives you a coding
   task (edit, fix, write, debug, add a feature, investigate, ...): then search,
   read, decide, act, and try another approach before declaring yourself stuck.
   For a greeting, thanks, small talk, or a question that is not a task, just
   answer normally and briefly - do NOT explore files, do NOT call tools, and do
   NOT narrate a plan. "hi" gets "hi", not a file listing.
3. TEST AFTER CHANGES. Run the tests straight after modifying something.
4. BE CONCISE. Under 4 lines unless the change genuinely needs explaining.
   Conciseness applies to your prose, not to how thoroughly you work. Do not
   narrate your internal reasoning or announce what you are about to do
   ("I will now...", "I should...") - just answer, or just do the work.
5. NEVER COMMIT OR PUSH unless the user explicitly asks.
6. NEVER ADD COMMENTS unless asked. Say *why*, not *what*.
7. USE THE TOOLS. Never guess at a file's contents, and never claim to have run
   something you did not run.
8. When you mention code, cite it as `path/to/file.py:123` so it can be found.
</critical_rules>

{tools}
"""


@dataclass
class Step:
    """One tool call and what it produced, for the transcript."""
    name: str
    args: dict
    output: str = ""
    approved: bool = False


@dataclass
class Turn:
    text: str = ""
    reasoning: str = ""
    steps: list[Step] = field(default_factory=list)


class Agent:
    """Runs one conversational turn, including any tool calls it needs."""

    def __init__(self, backend, session, *, use_tools: bool = True,
                 native_tools: bool = False,
                 approve: Callable[[str, dict, str], bool] | None = None,
                 persist=None):
        self.backend = backend
        self.session = session
        self.use_tools = use_tools
        # Whether this provider can do native tool calling. Determined once by
        # the harness: gemma cannot, DeepSeek/Claude/Gemini/Groq can.
        self.native_tools = native_tools
        self.approve = approve
        self.persist = persist
        self._allow_all = False

    # ------------------------------------------------------------------ prompt
    def system_prompt(self) -> str:
        import os
        base = SYSTEM_PROMPT.format(
            cwd=os.getcwd(),
            tools=TL.protocol_text() if self.use_tools else
            "You have no tools. Answer from what you already know.",
        )
        return (self.session.system + "\n\n" + base) if self.session.system else base

    def _messages(self) -> list[dict]:
        msgs = [{"role": "system", "content": self.system_prompt()}]
        msgs.extend(m.as_dict() for m in self.session.messages)
        return msgs

    # -------------------------------------------------------------------- gate
    def _permitted(self, name: str, args: dict) -> bool:
        tool = TL.TOOLS.get(name)
        if tool is None or not tool.danger:
            return True
        if self._allow_all or self.approve is None:
            return True
        verdict = self.approve(name, args, tool.summary(args))
        if verdict == APPROVE_ALL:
            self._allow_all = True
            return True
        return bool(verdict)

    # -------------------------------------------------------------------- loop
    def turn(self, on_event: Callable[[str, dict], None]) -> Turn:
        """Run the turn. ``on_event(kind, payload)`` drives the UI.

        kinds: reasoning · content · tool_start · tool_end · approval · done
        """
        result = Turn()
        messages = self._messages()

        for step_no in range(MAX_STEPS):
            content, reasoning, calls = self._one_request(messages, on_event)
            result.reasoning += reasoning
            result.text += content

            if not self.use_tools or not calls:
                break

            # Record the assistant's tool request so the model sees its own
            # reasoning on the next round.
            messages.append(self._assistant_message(content, calls))

            for name, args in calls:
                summary = TL.TOOLS[name].summary(args) if name in TL.TOOLS else name
                on_event("tool_start", {"name": name, "args": args,
                                        "summary": summary})
                if not self._permitted(name, args):
                    output = "error: the user denied permission for this tool"
                    on_event("tool_end", {"name": name, "output": output,
                                          "denied": True})
                else:
                    output = TL.execute(name, args)
                    on_event("tool_end", {"name": name, "output": output})
                result.steps.append(Step(name, args, output,
                                         approved="denied" not in output.lower()))
                messages.append(self._tool_message(name, output, len(messages)))

            if self.persist:
                self.persist()

        on_event("done", {"steps": len(result.steps)})
        return result

    # --------------------------------------------------------------- one round
    def _one_request(self, messages: list[dict],
                     on_event) -> tuple[str, str, list[tuple[str, dict]]]:
        """One model call. Returns (content, reasoning, tool calls)."""
        content: list[str] = []
        reasoning: list[str] = []
        partial: dict[int, dict] = {}

        self.backend.tools = TL.schemas() if (self.use_tools and self.native_tools) else []
        try:
            for kind, payload in self.backend.stream(messages):
                if kind == "reasoning":
                    reasoning.append(payload)
                    on_event("reasoning", {"text": payload})
                elif kind == "content":
                    content.append(payload)
                    on_event("content", {"text": payload})
                elif kind == "tool_call":
                    # Fragments arrive per index; name once, arguments in pieces.
                    idx = payload.get("index", 0)
                    slot = partial.setdefault(idx, {"name": "", "arguments": ""})
                    if payload.get("name"):
                        slot["name"] = payload["name"]
                    if payload.get("arguments"):
                        slot["arguments"] += payload["arguments"]
        except BackendError as exc:
            on_event("error", {"message": str(exc)})
            return "".join(content), "".join(reasoning), []

        text = "".join(content)
        calls: list[tuple[str, dict]] = []

        # Native calls, assembled from the fragments.
        for idx in sorted(partial):
            slot = partial[idx]
            name = slot["name"]
            if not name:
                continue
            try:
                args = json.loads(slot["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append((name, args if isinstance(args, dict) else {}))

        # Otherwise the text protocol.
        if not calls and self.use_tools:
            calls = TL.parse_text_calls(text)
            if calls:
                text = TL.strip_text_calls(text)

        return text, "".join(reasoning), calls

    # ------------------------------------------------------------- wire format
    def _assistant_message(self, content: str, calls: list[tuple[str, dict]]) -> dict:
        """How the model's tool request is echoed back to it."""
        if self.native_tools:
            return {
                "role": "assistant",
                "content": content or "",
                "tool_calls": [
                    {"id": f"call_{i}", "type": "function", "function": {
                        "name": n, "arguments": json.dumps(a)}}
                    for i, (n, a) in enumerate(calls)
                ],
            }
        # Text protocol: the blocks ARE the assistant message.
        blocks = "\n".join(
            "```tool\n" + json.dumps({"name": n, "args": a}) + "\n```"
            for n, a in calls)
        return {"role": "assistant", "content": (content + "\n" + blocks).strip()}

    def _tool_message(self, name: str, output: str, n: int) -> dict:
        """How a tool result is fed back."""
        if self.native_tools:
            return {"role": "tool", "tool_call_id": f"call_{n}",
                    "name": name, "content": output}
        # Text protocol: the result is a user message describing the output.
        return {"role": "user",
                "content": f"<tool_result name=\"{name}\">\n{output}\n</tool_result>"}


def supports_native_tools(backend) -> bool:
    """Whether to offer native tool calling for this endpoint.

    Local llama-server is assumed NOT to, because the local model here (gemma)
    ignores the parameter and would silently never call a tool. Hosted endpoints
    are assumed to, since they generally do. Overridable from the CLI.
    """
    url = getattr(backend, "where", "") or ""
    return "127.0.0.1" not in url and "localhost" not in url
