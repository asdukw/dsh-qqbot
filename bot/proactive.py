"""Heuristic policy for proactive group replies.

All decisions are made from explicit inputs (clock, context size, RNG) so the
policy can be unit-tested without sleeping or touching the network.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable

DEFAULT_PROMPT = (
    "你正在群里和大家聊天。结合上面的最近消息，简短、自然地接一句；"
    "不要复述别人的话，不要长篇大论，不要提自己是 AI。"
)


@dataclass
class ProactiveSettings:
    enabled: bool = False
    groups: tuple[int, ...] = ()
    probability: float = 0.04
    cooldown_seconds: int = 600
    max_per_hour: int = 4
    max_per_day: int = 20
    min_context_messages: int = 2
    keywords: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    quiet_hours: tuple[int, int] = (1, 7)
    prompt: str = DEFAULT_PROMPT


class ProactivePolicy:
    def __init__(self, settings: ProactiveSettings):
        self.settings = settings
        self._last: dict[int, float] = {}
        self._hour: dict[int, list[float]] = defaultdict(list)
        self._day: dict[int, list[float]] = defaultdict(list)

    def _prune(self, group_id: int, now: float) -> None:
        self._hour[group_id] = [t for t in self._hour[group_id] if now - t < 3600]
        self._day[group_id] = [t for t in self._day[group_id] if now - t < 86400]

    def _quiet(self, hour: int) -> bool:
        start, end = self.settings.quiet_hours
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def forced(self, text: str) -> bool:
        needle = text.lower()
        terms = (*self.settings.aliases, *self.settings.keywords)
        return any(term and term.lower() in needle for term in terms)

    def should_reply(
        self,
        group_id: int,
        text: str,
        context_count: int,
        now: float,
        hour: int,
        *,
        locked: bool = False,
        rng: Callable[[], float] = random.random,
    ) -> bool:
        s = self.settings
        if not s.enabled or locked:
            return False
        if s.groups and group_id not in s.groups:
            return False
        if self._quiet(hour):
            return False
        if context_count < s.min_context_messages:
            return False
        self._prune(group_id, now)
        if now - self._last.get(group_id, 0.0) < s.cooldown_seconds:
            return False
        if len(self._hour[group_id]) >= s.max_per_hour:
            return False
        if len(self._day[group_id]) >= s.max_per_day:
            return False
        return self.forced(text) or rng() < s.probability

    def record(self, group_id: int, now: float) -> None:
        self._last[group_id] = now
        self._hour[group_id].append(now)
        self._day[group_id].append(now)

    def status(self, group_id: int, now: float) -> dict:
        self._prune(group_id, now)
        last = self._last.get(group_id)
        return {
            "enabled": self.settings.enabled,
            "probability": self.settings.probability,
            "cooldown_seconds": self.settings.cooldown_seconds,
            "last_reply_ago": None if last is None else max(0, int(now - last)),
            "hour_count": len(self._hour[group_id]),
            "day_count": len(self._day[group_id]),
            "max_per_hour": self.settings.max_per_hour,
            "max_per_day": self.settings.max_per_day,
        }
