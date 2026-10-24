from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from memory import Memory  # noqa: E402
from usage import UsageStore, conversation_label  # noqa: E402


class UsageStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "usage.db"
        self.store = UsageStore(self.path)
        self.now = int(time.time())

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_dsh_usage_and_dedupe(self) -> None:
        events = [
            {
                "type": "assistant/message",
                "seq": 1,
                "time": self.now * 1000,
                "data": {
                    "turn": 1,
                    "step": 1,
                    "usage": {
                        "inputTokens": 100,
                        "outputTokens": 10,
                        "cacheReadTokens": 900,
                    },
                    "message": {"source": {"provider": "p", "model": "m"}},
                },
            },
            {
                "type": "compaction/summary",
                "seq": 2,
                "time": self.now * 1000,
                "data": {
                    "compactionId": "c1",
                    "usage": {"inputTokens": 2000, "outputTokens": 100},
                    "provider": "p",
                    "model": "m",
                },
            },
        ]
        self.store.record_dsh("group:1", "s1", "chat", events)
        self.store.record_dsh("group:1", "s1", "chat", events)  # same seqs
        summary = self.store.summary(days=7)
        self.assertEqual(summary["totals"]["events"], 2)
        self.assertEqual(summary["totals"]["input"], 2100)
        self.assertEqual(summary["totals"]["cache_read"], 900)
        self.assertEqual(summary["totals"]["output"], 110)
        self.assertEqual(summary["totals"]["total"], 3110)
        self.assertEqual(
            {row["purpose"] for row in summary["by_purpose"]},
            {"chat", "compaction"},
        )
        self.assertEqual(summary["conversations"][0]["id"], "group:1")

        filtered = self.store.summary(days=7, conversation_id="group:1")
        self.assertEqual(filtered["totals"]["total"], 3110)
        self.assertEqual(
            self.store.summary(days=7, conversation_id="private:2")["totals"]["total"],
            0,
        )

    def test_backfill_existing_dsh_sessions(self) -> None:
        root = Path(self.tmp.name) / "sessions" / "workspace" / "group-42"
        root.mkdir(parents=True)
        event = {
            "type": "assistant/message",
            "seq": 7,
            "time": self.now * 1000,
            "data": {
                "turn": 2,
                "step": 1,
                "usage": {"inputTokens": 30, "outputTokens": 3, "cacheReadTokens": 70},
                "message": {"source": {"provider": "p", "model": "m"}},
            },
        }
        (root / "session.v4.jsonl").write_text(
            __import__("json").dumps(event) + chr(10), encoding="utf-8"
        )
        self.assertEqual(self.store.backfill_dsh(Path(self.tmp.name) / "sessions"), 1)
        self.assertEqual(
            self.store.summary(days=7, conversation_id="group:42")["totals"]["total"],
            103,
        )

    def test_client_usage_normalizes_cached_prompt(self) -> None:
        self.store.record_client(
            "image_caption",
            "private:7",
            {
                "prompt_tokens": 1000,
                "completion_tokens": 50,
                "prompt_tokens_details": {"cached_tokens": 800},
            },
            provider="opencode-go",
            model="flash",
        )
        summary = self.store.summary(days=7, conversation_id="private:7")
        self.assertEqual(summary["totals"]["input"], 200)
        self.assertEqual(summary["totals"]["cache_read"], 800)
        self.assertEqual(summary["totals"]["output"], 50)
        self.assertEqual(summary["totals"]["cache_hit_rate"], 0.8)
        self.assertEqual(summary["by_purpose"][0]["purpose"], "image_caption")

    def test_readonly_summary(self) -> None:
        self.store.record(
            source="thin",
            purpose="task_parse",
            conversation_id="group:9",
            input_tokens=10,
            output_tokens=2,
        )
        reader = UsageStore(self.path, readonly=True)
        try:
            self.assertEqual(reader.summary(days=7)["totals"]["events"], 1)
        finally:
            reader.close()

    def test_chat_event_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Memory(Path(tmp) / "memory.db")
            try:
                import asyncio
                asyncio.run(memory.add_event(
                    "group:1", "group", 1, 42, "测试", "hello hello", self.now
                ))
                conn = __import__("sqlite3").connect(Path(tmp) / "memory.db")
                rows = conn.execute("SELECT conversation_id, user_id, text FROM chat_events").fetchall()
                conn.close()
                self.assertEqual(rows, [("group:1", 42, "hello hello")])
            finally:
                memory.close()

    def test_conversation_labels(self) -> None:
        self.assertEqual(conversation_label("group:123"), "群 123")
        self.assertEqual(conversation_label("private:456"), "好友 456")
        self.assertEqual(conversation_label("task:7"), "任务 7")
        self.assertEqual(conversation_label(""), "未知会话")


if __name__ == "__main__":
    unittest.main()
