"""Recent group chatter, so an @-mention arrives with the conversation around it."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class GroupLine:
    sender: str
    text: str
    # Image URLs from that message, so an @ or a proactive turn can still see
    # the picture after the message itself has scrolled into the context.
    images: tuple[str, ...] = ()


class GroupContextBuffer:
    """Per-group ring buffer of messages the bot was not addressed in.

    Lines stay in memory only and are drained when the bot is @-mentioned in
    that group, so each line reaches the model at most once and never leaves
    its own group.
    """

    def __init__(self, max_messages: int, max_line_chars: int = 200):
        self.max_messages = max(0, max_messages)
        self.max_line_chars = max_line_chars
        self._buffers: dict[int, deque[GroupLine]] = {}

    def add(
        self, group_id: int, sender: str, text: str, images: tuple[str, ...] = ()
    ) -> None:
        if self.max_messages == 0 or not text.strip():
            return
        buffer = self._buffers.get(group_id)
        if buffer is None:
            buffer = self._buffers[group_id] = deque(maxlen=self.max_messages)
        buffer.append(
            GroupLine(
                sender or "某人",
                text.strip()[: self.max_line_chars],
                tuple(images),
            )
        )

    def drain(self, group_id: int) -> list[GroupLine]:
        buffer = self._buffers.pop(group_id, None)
        return list(buffer) if buffer else []

    def peek(self, group_id: int, limit: int | None = None) -> list[GroupLine]:
        buffer = self._buffers.get(group_id)
        if not buffer:
            return []
        lines = list(buffer)
        return lines[-limit:] if limit else lines

    def images(self, group_id: int, limit: int) -> list[str]:
        """The newest buffered image URLs, oldest first, deduplicated."""
        if limit <= 0:
            return []
        newest: list[str] = []
        for line in reversed(self._buffers.get(group_id, ())):
            for url in reversed(line.images):
                if url in newest:
                    continue
                newest.append(url)
                if len(newest) >= limit:
                    return list(reversed(newest))
        return list(reversed(newest))


def format_group_turn(lines: list[GroupLine], sender: str, text: str, max_chars: int) -> str:
    """Render recent group lines plus the @-message as one user turn."""
    trigger = f"{sender or '某人'}: {text}"
    kept: list[str] = []
    used = 0
    for line in reversed(lines):  # keep the newest lines within the budget
        rendered = f"{line.sender}: {line.text}"
        if used + len(rendered) > max_chars:
            break
        kept.append(rendered)
        used += len(rendered) + 1
    if not kept:
        return trigger
    kept.reverse()
    return "[群聊里最近的消息]\n" + "\n".join(kept) + "\n\n[有人 @ 你]\n" + trigger


def format_proactive_turn(lines: list[GroupLine], instruction: str, max_chars: int) -> str:
    """Render recent group lines for a turn the bot joins on its own."""
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        rendered = f"{line.sender}: {line.text}"
        if used + len(rendered) > max_chars:
            break
        kept.append(rendered)
        used += len(rendered) + 1
    kept.reverse()
    body = "\n".join(kept) if kept else "（群里暂时没有更多消息）"
    return "[群聊里最近的消息]\n" + body + "\n\n" + instruction.strip()
