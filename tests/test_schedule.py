from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from schedule import extract_schedule, render_schedule  # noqa: E402


def fence(payload: str, lang: str = "schedule") -> str:
    return "```" + lang + "\n" + payload + "\n```"


VALID = (
    '{"event":"S16","matches":['
    '{"stage":"瑞士轮","start_time":"2026-10-24T02:00:00+08:00",'
    '"team_a":"HLE","team_b":"TES","best_of":1},'
    '{"start_time":"2026-10-24T01:00:00+08:00","team_a":"GEN","team_b":"IG"}'
    "]}"
)


class ScheduleTests(unittest.TestCase):
    def test_valid_block_is_validated_rendered_and_removed(self) -> None:
        outer, schedule = extract_schedule("提醒一句。\n" + fence(VALID))
        self.assertIsNotNone(schedule)
        self.assertEqual(outer, "提醒一句。")
        rendered = render_schedule(schedule)
        lines = rendered.splitlines()
        self.assertEqual(lines[0], "【S16】")
        self.assertIn("10-24 01:00 ｜ GEN vs IG", lines[1])
        self.assertIn("10-24 02:00 ｜ HLE vs TES ｜ BO1", lines[2])
        self.assertNotIn("http", rendered)
        self.assertNotIn("```", rendered)

    def test_naive_times_are_read_as_beijing(self) -> None:
        payload = (
            '{"event":"测试杯","matches":[{"start_time":"2026-10-24T01:00:00",'
            '"team_a":"A","team_b":"B"}]}'
        )
        _outer, schedule = extract_schedule(fence(payload))
        assert schedule is not None
        self.assertIn("10-24 01:00", render_schedule(schedule))
        self.assertNotIn("BO", render_schedule(schedule))

    def test_invalid_block_never_reaches_the_chat(self) -> None:
        bad = '{"event":"S16","matches":[{"start_time":"2026-10-24T01:00:00","team_a":"","team_b":"B"}]}'
        outer, schedule = extract_schedule("看这里\n" + fence(bad))
        self.assertIsNone(schedule)
        self.assertEqual(outer, "看这里")

        outer, schedule = extract_schedule(fence('{"matches": [oops'))
        self.assertIsNone(schedule)
        self.assertEqual(outer, "赛程解析失败，稍后再试。")

    def test_plain_replies_are_untouched(self) -> None:
        text = "今晚 1 点开打，记得早点睡。"
        self.assertEqual(extract_schedule(text), (text, None))

    def test_json_fence_without_matches_is_left_alone(self) -> None:
        text = fence('{"ok": true}', lang="json")
        self.assertEqual(extract_schedule(text), (text, None))


if __name__ == "__main__":
    unittest.main()
