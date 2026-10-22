from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from group_context import GroupLine, format_proactive_turn  # noqa: E402
from proactive import ProactivePolicy, ProactiveSettings  # noqa: E402


def settings(**overrides) -> ProactiveSettings:
    base = dict(
        enabled=True, probability=0.5, cooldown_seconds=600,
        max_per_hour=2, max_per_day=3, min_context_messages=2,
        quiet_hours=(1, 7),
    )
    base.update(overrides)
    return ProactiveSettings(**base)


class ProactivePolicyTests(unittest.TestCase):
    def test_disabled_never_replies(self) -> None:
        policy = ProactivePolicy(settings(enabled=False))
        self.assertFalse(policy.should_reply(1, "hi", 5, 1000, hour=12))

    def test_group_whitelist(self) -> None:
        policy = ProactivePolicy(settings(groups=(10,)))
        self.assertFalse(policy.should_reply(1, "hi", 5, 1000, hour=12, rng=lambda: 0.0))
        self.assertTrue(policy.should_reply(10, "hi", 5, 1000, hour=12, rng=lambda: 0.0))

    def test_needs_enough_context(self) -> None:
        policy = ProactivePolicy(settings())
        self.assertFalse(policy.should_reply(1, "hi", 1, 1000, hour=12, rng=lambda: 0.0))

    def test_quiet_hours_wrap(self) -> None:
        policy = ProactivePolicy(settings(quiet_hours=(22, 7)))
        self.assertFalse(policy.should_reply(1, "hi", 5, 1000, hour=23, rng=lambda: 0.0))
        self.assertFalse(policy.should_reply(1, "hi", 5, 1000, hour=3, rng=lambda: 0.0))
        self.assertTrue(policy.should_reply(1, "hi", 5, 1000, hour=12, rng=lambda: 0.0))

    def test_probability_gate(self) -> None:
        policy = ProactivePolicy(settings(probability=0.1))
        self.assertFalse(policy.should_reply(1, "hi", 5, 1000, hour=12, rng=lambda: 0.9))
        self.assertTrue(policy.should_reply(1, "hi", 5, 1000, hour=12, rng=lambda: 0.05))

    def test_keyword_forces_reply(self) -> None:
        policy = ProactivePolicy(settings(probability=0.0, keywords=("显卡",)))
        self.assertTrue(policy.should_reply(1, "这显卡怎么样", 5, 1000, hour=12))
        self.assertFalse(policy.should_reply(1, "在吗", 5, 1000, hour=12))

    def test_alias_is_a_name_address_not_a_random_try(self) -> None:
        policy = ProactivePolicy(settings(probability=0.0, aliases=("小助手",)))
        self.assertTrue(policy.named("小助手在吗"))
        self.assertFalse(policy.named("在吗"))
        # Names are handled as a direct reply by the bot, not by the policy.
        self.assertFalse(policy.should_reply(1, "小助手在吗", 5, 1000, hour=12))

    def test_card_names_extend_the_address_aliases(self) -> None:
        policy = ProactivePolicy(settings(aliases=()))
        self.assertFalse(policy.named("大肥鱼在吗"))
        self.assertTrue(policy.named("大肥鱼在吗", ("大肥鱼", "小杜")))
        self.assertTrue(policy.named("小杜你好", ("大肥鱼", "小杜")))
        # Single-character names are ignored to avoid false positives.
        self.assertFalse(policy.named("鱼在吗", ("鱼",)))

    def test_followup_multiplier(self) -> None:
        policy = ProactivePolicy(settings(
            probability=0.1, cooldown_seconds=0,
            followup_seconds=300, followup_multiplier=2.0,
        ))
        policy.note_speaking(1, 1000)
        self.assertTrue(policy.should_reply(1, "hi", 5, 1100, hour=12, rng=lambda: 0.15))
        self.assertFalse(policy.should_reply(1, "hi", 5, 1400, hour=12, rng=lambda: 0.15))

    def test_cooldown_and_hourly_cap(self) -> None:
        policy = ProactivePolicy(settings(cooldown_seconds=600, max_per_hour=2))
        self.assertTrue(policy.should_reply(1, "hi", 5, 1000, hour=12, rng=lambda: 0.0))
        policy.record(1, 1000)
        self.assertFalse(policy.should_reply(1, "hi", 5, 1100, hour=12, rng=lambda: 0.0))
        self.assertTrue(policy.should_reply(1, "hi", 5, 1700, hour=12, rng=lambda: 0.0))
        policy.record(1, 1700)
        self.assertFalse(policy.should_reply(1, "hi", 5, 2400, hour=12, rng=lambda: 0.0))

    def test_locked_group_skips(self) -> None:
        policy = ProactivePolicy(settings())
        self.assertFalse(
            policy.should_reply(1, "hi", 5, 1000, hour=12, locked=True, rng=lambda: 0.0)
        )


class ProactiveFormatTests(unittest.TestCase):
    def test_renders_recent_lines_and_instruction(self) -> None:
        lines = [GroupLine("a", "first"), GroupLine("b", "second")]
        out = format_proactive_turn(lines, "接一句", 1000)
        self.assertIn("a: first", out)
        self.assertIn("b: second", out)
        self.assertTrue(out.endswith("接一句"))

    def test_budget_keeps_newest(self) -> None:
        lines = [GroupLine("a", "x" * 50), GroupLine("b", "最新")]
        out = format_proactive_turn(lines, "接一句", 20)
        self.assertIn("b: 最新", out)
        self.assertNotIn("x" * 50, out)


if __name__ == "__main__":
    unittest.main()
