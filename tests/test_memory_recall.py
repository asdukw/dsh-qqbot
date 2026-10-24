from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from memory import Memory, format_recall, recall_terms  # noqa: E402


class RecallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.tmp.name) / "memory.db")

    def tearDown(self) -> None:
        self.memory.close()
        self.tmp.cleanup()

    def test_finds_chinese_substrings_in_the_same_conversation(self) -> None:
        asyncio.run(self.memory.add("group:1", "user", "今天长春天气不错，适合出门"))
        asyncio.run(self.memory.add("group:1", "assistant", "那就多穿一点"))
        asyncio.run(self.memory.add("group:2", "user", "长春天气别处"))
        rows = asyncio.run(self.memory.recall("group:1", "长春天气怎么样"))
        self.assertTrue(rows)
        self.assertIn("长春天气", rows[0][1])
        self.assertTrue(all("别处" not in row[1] for row in rows))

    def test_short_query_falls_back_to_like(self) -> None:
        asyncio.run(self.memory.add("group:1", "user", "天气不错"))
        rows = asyncio.run(self.memory.recall("group:1", "天气"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "天气不错")

    def test_no_terms_returns_empty(self) -> None:
        self.assertEqual(asyncio.run(self.memory.recall("group:1", " ！？ ")), [])

    def test_terms_are_sampled_from_cjk_runs(self) -> None:
        terms = recall_terms("长春天气怎么样")
        self.assertIn("长春天", terms)
        self.assertIn("春天气", terms)
        self.assertLessEqual(len(terms), 8)
        self.assertEqual(recall_terms("好的"), ["好的"])
        self.assertEqual(recall_terms("a"), [])

    def test_format_labels_roles_and_truncates(self) -> None:
        text = format_recall([
            ("user", "问题" * 200, 0),
            ("assistant", "回答", 0),
        ])
        self.assertIn("- 对方:", text)
        self.assertIn("- 你: 回答", text)
        self.assertLess(len(text), 700)
        self.assertIn("…", text)


if __name__ == "__main__":
    unittest.main()
