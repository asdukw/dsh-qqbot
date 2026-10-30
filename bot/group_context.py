"""Recent group chatter and immutable, budgeted per-turn message targets."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class GroupLine:
    sender: str
    text: str
    images: tuple[str, ...] = ()
    user_id: int = 0
    message_id: int = 0
    reply_to: int | None = None


def render_line(line: GroupLine) -> str:
    who = f"{line.sender or '某人'}({line.user_id})" if line.user_id else line.sender or '某人'
    identity = f"[消息ID:{line.message_id}" if line.message_id else ''
    if identity and line.reply_to:
        identity += f" 引用:{line.reply_to}"
    return (identity + '] ' if identity else '') + f"{who}: {line.text}"


def visible_lines(lines: list[GroupLine] | tuple[GroupLine, ...], max_chars: int) -> tuple[GroupLine, ...]:
    kept: list[GroupLine] = []
    used = 0
    for line in reversed(lines):
        size = len(render_line(line))
        if used + size > max_chars:
            break
        kept.append(line)
        used += size + 1
    return tuple(reversed(kept))


def line_images(lines: list[GroupLine] | tuple[GroupLine, ...], limit: int) -> list[str]:
    if limit <= 0:
        return []
    newest: list[str] = []
    for line in reversed(lines):
        for url in reversed(line.images):
            if url not in newest:
                newest.append(url)
                if len(newest) >= limit:
                    return list(reversed(newest))
    return list(reversed(newest))


@dataclass(frozen=True)
class TurnSnapshot:
    captured: tuple[GroupLine, ...]
    lines: tuple[GroupLine, ...]
    current: GroupLine | None = None

    @property
    def targets(self) -> tuple[GroupLine, ...]:
        lines = (*self.lines, *((self.current,) if self.current else ()))
        return tuple(line for line in lines if line.message_id and line.user_id > 0)

    def images(self, limit: int) -> list[str]:
        own = self.current.images if self.current else ()
        return list(dict.fromkeys((*own, *line_images(self.lines, limit))))


class GroupContextBuffer:
    """Per-group ring buffer; snapshot consumption never removes newer arrivals."""

    def __init__(self, max_messages: int, max_line_chars: int = 200):
        self.max_messages = max(0, max_messages)
        self.max_line_chars = max_line_chars
        self._buffers: dict[int, deque[GroupLine]] = {}

    def add(self, group_id: int, sender: str, text: str, images: tuple[str, ...] = (),
            user_id: int = 0, message_id: int = 0, reply_to: int | None = None) -> None:
        if self.max_messages == 0 or not text.strip():
            return
        buffer = self._buffers.get(group_id)
        if buffer is None:
            buffer = self._buffers[group_id] = deque(maxlen=self.max_messages)
        buffer.append(GroupLine(sender or '某人', text.strip()[:self.max_line_chars],
                                tuple(images), int(user_id or 0), int(message_id or 0), reply_to))

    def drain(self, group_id: int) -> list[GroupLine]:
        buffer = self._buffers.pop(group_id, None)
        return list(buffer) if buffer else []

    def peek(self, group_id: int, limit: int | None = None) -> list[GroupLine]:
        lines = list(self._buffers.get(group_id, ()))
        return lines[-limit:] if limit else lines

    def consume(self, group_id: int, captured: tuple[GroupLine, ...]) -> None:
        buffer = self._buffers.get(group_id)
        if buffer is None:
            return
        identities = {id(line) for line in captured}
        remaining = [line for line in buffer if id(line) not in identities]
        if remaining:
            self._buffers[group_id] = deque(remaining, maxlen=self.max_messages)
        else:
            self._buffers.pop(group_id, None)

    def images(self, group_id: int, limit: int) -> list[str]:
        return line_images(self.peek(group_id), limit)


def format_group_turn(lines: list[GroupLine] | tuple[GroupLine, ...], sender: str, text: str,
                      max_chars: int, user_id: int = 0, message_id: int = 0,
                      reply_to: int | None = None) -> str:
    trigger = render_line(GroupLine(sender, text, user_id=user_id,
                                    message_id=message_id, reply_to=reply_to))
    kept = visible_lines(lines, max_chars)
    if not kept:
        return trigger
    return '[群聊里最近的消息]\n' + '\n'.join(map(render_line, kept)) + '\n\n[有人 @ 你]\n' + trigger


def format_proactive_turn(lines: list[GroupLine] | tuple[GroupLine, ...], instruction: str,
                          max_chars: int) -> str:
    kept = visible_lines(lines, max_chars)
    body = '\n'.join(map(render_line, kept)) if kept else '（群里暂时没有更多消息）'
    return '[群聊里最近的消息]\n' + body + '\n\n' + instruction.strip()
