"""Conversation state.

Deliberately dumb: it holds the message list and knows how to turn it into the
payload a model expects. It does no I/O, so it is easy to test.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_limit(name: str, default: int, floor: int) -> int:
    """Read an int env var, clamped so a typo cannot disable the window."""
    try:
        return max(floor, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


def window(messages, max_messages=None, max_chars=None):
    """The recent slice of a conversation the model should actually see.

    A sliding window over the history: keep the newest messages and drop the
    oldest, so a long session does not feed the model its own stale turns and
    send it drifting off-task. Two limits, in this order:

    1. message count - at most ``max_messages`` of the newest are kept;
    2. character budget - oldest-first messages are trimmed until the total fits
       ``max_chars`` (about 4 chars per token), so one giant pasted file cannot
       crowd out the current turn.

    The newest message is never dropped, and the caller prepends the system
    prompt - the window only ever applies to the conversation, never to it.
    ``messages`` is read, not mutated.

    Tune with TRIOFORGE_CONTEXT_MESSAGES / TRIOFORGE_CONTEXT_CHARS.
    """
    if max_messages is None:
        max_messages = _env_limit("TRIOFORGE_CONTEXT_MESSAGES", 12, 4)
    if max_chars is None:
        max_chars = _env_limit("TRIOFORGE_CONTEXT_CHARS", 24000, 4000)
    if not messages:
        return list(messages)
    recent = list(messages[-max_messages:])
    total = sum(len(m.content) for m in recent)
    drop = 0
    while total > max_chars and drop < len(recent) - 1:
        total -= len(recent[drop].content)
        drop += 1
    return recent[drop:]


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
    #: Optional callback fired after add_user / add_assistant / clear, receiving
    #: ``self``. The full-screen UI sets it to persist the transcript to disk;
    #: the throwaway team sessions leave it None so they never write.
    on_change: object | None = field(default=None, repr=False)

    # ---------------------------------------------------------------- writes
    def add_user(self, text: str) -> None:
        self.messages.append(Message("user", text))
        self._changed()

    def add_assistant(self, text: str) -> None:
        self.messages.append(Message("assistant", text))
        self._changed()

    def drop_last(self) -> None:
        """Remove the trailing message - used when a request fails, so the
        failed turn is not resent on the next attempt."""
        if self.messages:
            self.messages.pop()
            self._changed()

    def clear(self) -> None:
        self.messages.clear()
        self._changed()

    def _changed(self) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change(self)
        except Exception:  # noqa: BLE001 - persistence must never break a turn
            pass

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
        """The message list in OpenAI wire format, system prompt first.

        The conversation is passed through :func:`window`, so only the recent
        history reaches the model - old turns are sliced out and the model stays
        on the current task instead of drifting off its own stale answers.
        """
        out: list[dict] = []
        if self.system:
            out.append({"role": "system", "content": self.system})
        out.extend(m.as_dict() for m in window(self.messages))
        return out

    def transcript(self) -> str:
        """Plain-text dump, for /save."""
        lines: list[str] = []
        for m in self.messages:
            who = {"user": "you", "assistant": "trio"}.get(m.role, m.role)
            lines.append(f"{who}: {m.content}")
            lines.append("")
        return "\n".join(lines)
