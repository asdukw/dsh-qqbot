from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from prompt import build_messages, build_system_prompt, substitute, trim_history  # noqa: E402


class PromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 10, 22, 21, 0)
        self.data = {
            "system_prompt": "你是{{char}}，和{{user}}聊天。",
            "description": "一个角色。",
            "personality": "温和。",
            "scenario": "在 QQ 上。",
            "mes_example": "<START>\n{{char}}: 你好",
            "post_history_instructions": "保持简短。",
            "extensions": {
                "depth_prompt": {"prompt": "记住 {{user}} 的名字。", "depth": 1, "role": "system"}
            },
        }

    def test_substitute_macros(self) -> None:
        text = substitute("{{char}} / {{user}} / {{date}}", "阿杜", "小明", self.now)
        self.assertIn("阿杜", text)
        self.assertIn("小明", text)
        self.assertIn("2026-10-22", text)

    def test_system_prompt_contains_character_sections(self) -> None:
        prompt = build_system_prompt(self.data, "阿杜", "用户", self.now, ["世界设定"])
        self.assertIn("一个角色。", prompt)
        self.assertIn("温和。", prompt)
        self.assertIn("世界设定", prompt)
        self.assertIn("你是阿杜", prompt)

    def test_build_messages_order_and_depth(self) -> None:
        history = [
            {"role": "user", "content": "早"},
            {"role": "assistant", "content": "早呀"},
        ]
        messages = build_messages(
            data=self.data,
            history=history,
            user_text="今天做什么？",
            char_name="阿杜",
            user_name="用户",
            now=self.now,
            lore_chunks=[],
        )
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[-1]["role"], "system")  # post-history instructions
        self.assertIn("保持简短。", messages[-1]["content"])
        contents = "\n".join(m["content"] for m in messages)
        self.assertIn("记住 用户 的名字。", contents)
        self.assertIn("今天做什么？", contents)

    def test_trim_history_drops_oldest(self) -> None:
        history = [
            {"role": "user", "content": "a" * 10},
            {"role": "assistant", "content": "b" * 10},
            {"role": "user", "content": "c" * 10},
        ]
        trimmed = trim_history(history, max_chars=15)
        self.assertEqual(len(trimmed), 1)
        self.assertEqual(trimmed[0]["content"], "c" * 10)

    def test_trim_history_zero_budget(self) -> None:
        self.assertEqual(trim_history([{"role": "user", "content": "x"}], 0), [])


if __name__ == "__main__":
    unittest.main()
