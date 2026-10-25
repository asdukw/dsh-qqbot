from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

import admin_insights  # noqa: E402
from admin_insights import TZ, fun_data, top_words  # noqa: E402


def fake_tagged(text: str) -> list[tuple[str, str]]:
    return [(word, "n") for word in text.split()]


class TopWordsTests(unittest.TestCase):
    def test_counts_messages_not_raw_occurrences(self) -> None:
        with patch.object(admin_insights, "_tagged_words", side_effect=fake_tagged):
            words = top_words(["奶茶 奶茶 奶茶", "奶茶 好喝"])
        self.assertEqual(words[0], {"text": "奶茶", "count": 2})

    def test_excludes_member_names(self) -> None:
        with patch.object(admin_insights, "_tagged_words", side_effect=fake_tagged):
            words = top_words(
                ["小明 原神 原神", "小明 原神 好玩"], exclude=["小明"]
            )
        counts = {word["text"]: word["count"] for word in words}
        self.assertEqual(counts.get("原神"), 2)
        self.assertNotIn("小明", counts)

    def test_drops_stopwords_and_filler(self) -> None:
        with patch.object(admin_insights, "_tagged_words", side_effect=fake_tagged):
            words = top_words(["the 啊啊啊 hhh 哈哈哈哈 谢谢"], limit=20)
        self.assertEqual([word["text"] for word in words], ["谢谢"])

    def test_casefolds_latin_words(self) -> None:
        with patch.object(admin_insights, "_tagged_words", side_effect=fake_tagged):
            words = top_words(["Django django DJANGO"])
        self.assertEqual(words, [{"text": "django", "count": 1}])

    def test_keeps_only_topic_parts_of_speech(self) -> None:
        def tagged(_text: str) -> list[tuple[str, str]]:
            return [
                ("有人", "r"),
                ("要是", "c"),
                ("第一次", "m"),
                ("原神", "n"),
                ("部署", "vn"),
            ]

        with patch.object(admin_insights, "_tagged_words", side_effect=tagged):
            words = top_words(["ignored"])
        self.assertEqual({word["text"] for word in words}, {"原神", "部署"})

    def test_drops_short_english_noise(self) -> None:
        def tagged(_text: str) -> list[tuple[str, str]]:
            return [("cn", "eng"), ("vd", "eng"), ("ai", "eng"), ("bilibili", "eng")]

        with patch.object(admin_insights, "_tagged_words", side_effect=tagged):
            words = top_words(["ignored"])
        self.assertEqual({word["text"] for word in words}, {"ai", "bilibili"})

    def test_drops_reduplicated_verbs(self) -> None:
        def tagged(_text: str) -> list[tuple[str, str]]:
            return [("看看", "v"), ("谢谢", "v"), ("道歉", "v"), ("部署", "vn")]

        with patch.object(admin_insights, "_tagged_words", side_effect=tagged):
            words = top_words(["ignored"])
        self.assertEqual({word["text"] for word in words}, {"道歉", "部署"})

    def test_min_count_drops_one_off_words_in_large_samples(self) -> None:
        texts = ["常见 独特", "常见 别的"] + [
            f"填充{index}" for index in range(18)
        ]
        with patch.object(admin_insights, "_tagged_words", side_effect=fake_tagged):
            words = top_words(texts)
        self.assertEqual(words, [{"text": "常见", "count": 2}])


class FunSamplingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "memory.db"
        conn = sqlite3.connect(self.db)
        conn.executescript(
            """
            CREATE TABLE chat_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                chat_type TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                sender_name TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL
            );
            """
        )
        self.conn = conn

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def noon(self, day: str) -> int:
        value = datetime.strptime(day, "%Y-%m-%d").replace(hour=12, tzinfo=TZ)
        return int(value.timestamp())

    def test_recovers_the_older_part_of_a_long_range(self) -> None:
        today = datetime.now(TZ).date()
        start_day = (today - timedelta(days=30)).isoformat()
        end_day = (today - timedelta(days=1)).isoformat()
        early_ts = self.noon(start_day)
        late_ts = self.noon(end_day)
        self.conn.executemany(
            "INSERT INTO chat_events "
            "(conversation_id, chat_type, chat_id, user_id, sender_name, text, created_at) "
            "VALUES (?, 'group', 42, ?, ?, ?, ?)",
            [
                ("group:42", 1, "旧人", f"earlyword {index}", early_ts + index)
                for index in range(3000)
            ]
            + [
                ("group:42", 2, "新人", f"lateword {index}", late_ts + index)
                for index in range(3000)
            ],
        )
        self.conn.commit()

        captured: dict[str, object] = {}

        def fake_top_words(texts, **kwargs):
            captured["texts"] = list(texts)
            captured["exclude"] = set(kwargs.get("exclude", ()))
            return []

        with patch.object(admin_insights, "top_words", side_effect=fake_top_words):
            fun_data(
                {"paths": {"database": str(self.db)}},
                start=start_day,
                end=end_day,
            )

        texts = captured["texts"]
        assert isinstance(texts, list)
        self.assertTrue(any("earlyword" in text for text in texts))
        self.assertTrue(any("lateword" in text for text in texts))
        self.assertEqual(captured["exclude"], {"旧人", "新人"})
        self.assertLessEqual(len(texts), 2000)

    def test_fun_data_filters_names_and_uses_document_frequency(self) -> None:
        today = datetime.now(TZ).date().isoformat()
        now = self.noon(today)
        self.conn.executemany(
            "INSERT INTO chat_events "
            "(conversation_id, chat_type, chat_id, user_id, sender_name, text, created_at) "
            "VALUES ('group:42', 'group', 42, ?, ?, ?, ?)",
            [
                (1, "小明", "小明 原神 原神 原神", now),
                (2, "小红", "小红 原神 好喝", now + 1),
            ],
        )
        self.conn.commit()

        with patch.object(admin_insights, "_tagged_words", side_effect=fake_tagged):
            payload = fun_data(
                {"paths": {"database": str(self.db)}},
                start=today,
                end=today,
            )

        words = {word["text"]: word["count"] for word in payload["words"]}
        self.assertEqual(words.get("原神"), 2)
        self.assertNotIn("小明", words)
        self.assertNotIn("小红", words)


if __name__ == "__main__":
    unittest.main()
