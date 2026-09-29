"""Conversation state.

Deliberately dumb: it holds the message list and knows how to turn it into the
payload a model expects. It does no I/O, so it is easy to test.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str

    def as_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


@dataclass
class Session:
    model: str = ""
    where: str = ""
    system: str = ""
    messages: list[Message] = field(default_factory=list)

    # ---------------------------------------------------------------- writes
    def add_user(self, text: str) -> None:
        self.messages.append(Message("user", text))

    def add_assistant(self, text: str) -> None:
        self.messages.append(Message("assistant", text))

    def drop_last(self) -> None:
        """Remove the trailing message - used when a request fails, so the
        failed turn is not resent on the next attempt."""
        if self.messages:
            self.messages.pop()

    def clear(self) -> None:
        self.messages.clear()

    # ---------------------------------------------------------------- reads
    @property
    def turns(self) -> int:
        """Number of user messages - what a person counts as 'messages'."""
        return sum(1 for m in self.messages if m.role == "user")

    @property
    def last_assistant(self) -> str:
        for m in reversed(self.messages):
            if m.role == "assistant":
                return m.content
        return ""

    def payload(self) -> list[dict]:
        """The message list in OpenAI wire format, system prompt first."""
        out: list[dict] = []
        if self.system:
            out.append({"role": "system", "content": self.system})
        out.extend(m.as_dict() for m in self.messages)
        return out

    def transcript(self) -> str:
        """Plain-text dump, for /save."""
        lines: list[str] = []
        for m in self.messages:
            who = {"user": "you", "assistant": "trio"}.get(m.role, m.role)
            lines.append(f"{who}: {m.content}")
            lines.append("")
        return "\n".join(lines)
