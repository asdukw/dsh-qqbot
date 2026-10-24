"""Prompt construction: character card -> chat messages."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

DEFAULT_SYSTEM = (
    "你正在扮演 {{char}}，与 {{user}} 聊天。\n"
    "始终以 {{char}} 的身份、语气和性格回应，不要跳出角色，"
    "不要提及自己是 AI、语言模型或程序。\n"
    "回复自然、口语化，长度适中，符合角色设定。"
)

_MACRO_RE = re.compile(r"\{\{\s*(char|user|time|date|weekday|isotime|original)\s*\}\}", re.IGNORECASE)


def substitute(text: str, char_name: str, user_name: str, now: datetime) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1).lower()
        if key == "char":
            return char_name
        if key == "user":
            return user_name
        if key == "time":
            return now.strftime("%H:%M")
        if key == "date":
            return now.strftime("%Y-%m-%d")
        if key == "weekday":
            return now.strftime("%A")
        if key == "isotime":
            return now.strftime("%Y-%m-%dT%H:%M:%S")
        return DEFAULT_SYSTEM

    text = _MACRO_RE.sub(repl, text)
    return text.replace("<BOT>", char_name).replace("<USER>", user_name)


def _section(title: str, body: str) -> str:
    body = body.strip()
    return f"[{title}]\n{body}" if body else ""


def build_system_prompt(
    data: dict[str, Any],
    char_name: str,
    user_name: str,
    now: datetime,
    lore_chunks: list[str],
) -> str:
    base = (data.get("system_prompt") or DEFAULT_SYSTEM).strip()
    parts = [substitute(base, char_name, user_name, now)]

    parts.append(_section("角色设定", substitute(data.get("description", ""), char_name, user_name, now)))
    parts.append(_section("性格", substitute(data.get("personality", ""), char_name, user_name, now)))
    parts.append(_section("场景", substitute(data.get("scenario", ""), char_name, user_name, now)))

    examples = data.get("mes_example", "").strip()
    if examples:
        parts.append("[对话示例]\n" + substitute(examples, char_name, user_name, now))

    if lore_chunks:
        lore = "\n".join(chunk.strip() for chunk in lore_chunks if chunk.strip())
        parts.append("[世界设定（仅在相关时参考）]\n" + substitute(lore, char_name, user_name, now))

    return "\n\n".join(part for part in parts if part)


def _depth_prompt(data: dict[str, Any]) -> dict[str, Any] | None:
    extensions = data.get("extensions")
    if not isinstance(extensions, dict):
        return None
    depth = extensions.get("depth_prompt")
    if isinstance(depth, str):
        try:
            depth = json.loads(depth)
        except json.JSONDecodeError:
            return None
    if not isinstance(depth, dict) or not str(depth.get("prompt", "")).strip():
        return None
    return depth


def trim_history(history: list[dict[str, str]], max_chars: int) -> list[dict[str, str]]:
    if max_chars <= 0:
        return []
    total = sum(len(item.get("content", "")) for item in history)
    start = 0
    while start < len(history) and total > max_chars:
        total -= len(history[start].get("content", ""))
        start += 1
    return history[start:]


def build_messages(
    data: dict[str, Any],
    history: list[dict[str, str]],
    user_text: str,
    char_name: str,
    user_name: str,
    now: datetime,
    lore_chunks: list[str],
    max_history_chars: int = 12000,
) -> list[dict[str, Any]]:
    system = build_system_prompt(data, char_name, user_name, now, lore_chunks)
    trimmed = trim_history(history, max_history_chars)

    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    messages.extend(trimmed)
    messages.append({"role": "user", "content": user_text})

    depth = _depth_prompt(data)
    if depth is not None:
        content = substitute(str(depth["prompt"]), char_name, user_name, now)
        role = str(depth.get("role") or "system").lower()
        if role not in ("system", "user", "assistant"):
            role = "system"
        try:
            offset = int(depth.get("depth", 4))
        except (TypeError, ValueError):
            offset = 4
        index = max(1, len(messages) - max(0, offset))
        messages.insert(index, {"role": role, "content": content})

    post = data.get("post_history_instructions", "").strip()
    if post:
        messages.append({
            "role": "system",
            "content": substitute(post, char_name, user_name, now),
        })

    return messages
