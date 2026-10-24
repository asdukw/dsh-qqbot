"""Heuristic policy for proactive group replies.

All decisions are made from explicit inputs (clock, context size, RNG) so the
policy can be unit-tested without sleeping or touching the network.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

DEFAULT_PROMPT = (
    "你正在群里和大家聊天。结合上面的最近消息，简短、自然地接一句；"
    "不要复述别人的话，不要长篇大论，不要提自己是 AI。"
)


@dataclass
class ProactiveSettings:
    enabled: bool = False
    groups: tuple[int, ...] = ()
    # ~3x the old defaults: MaiBot's normal talk_value and the dafeiyu bot's
    # 60s decision gap both sit well above a 5% / 10-minute policy.
    probability: float = 0.15
    cooldown_seconds: int = 90
    max_per_hour: int = 12
    max_per_day: int = 60
    min_context_messages: int = 2
    # Interest words: a hit forces a proactive attempt (quota still applies).
    keywords: tuple[str, ...] = ()
    # Names that address the bot: treated like an @-mention, not a random try.
    aliases: tuple[str, ...] = ()
    quiet_hours: tuple[int, int] = (1, 7)
    # After the bot speaks in a group, keep joining nearby messages more eagerly.
    followup_seconds: int = 300
    followup_multiplier: float = 2.0
    prompt: str = DEFAULT_PROMPT


class ProactivePolicy:
    def __init__(self, settings: ProactiveSettings):
        self.settings = settings
        self._last: dict[int, float] = {}
        self._last_speak: dict[int, float] = {}
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

    def named(self, text: str, extra_names: tuple[str, ...] = ()) -> bool:
        """The message addresses the bot by name; reply directly, like an @.

        Extra names carry the active character card's name/nickname, so the
        trigger follows the card instead of a hard-coded config list.
        """
        needle = text.lower()
        names = (*self.settings.aliases, *extra_names)
        return any(len(name) >= 2 and name.lower() in needle for name in names if name)

    def forced(self, text: str) -> bool:
        """An interest keyword forces a proactive attempt (still quota-gated)."""
        needle = text.lower()
        return any(word and word.lower() in needle for word in self.settings.keywords)

    def note_speaking(self, group_id: int, now: float) -> None:
        """Remember that the bot just spoke here (direct reply or proactive)."""
        self._last_speak[group_id] = now

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
        probability = s.probability
        since_spoke = now - self._last_speak.get(group_id, 0.0)
        if s.followup_seconds > 0 and since_spoke <= s.followup_seconds:
            probability = min(1.0, probability * max(1.0, s.followup_multiplier))
        return self.forced(text) or rng() < probability

    def record(self, group_id: int, now: float) -> None:
        self._last[group_id] = now
        self._last_speak[group_id] = now
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
