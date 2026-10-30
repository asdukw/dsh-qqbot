from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from conversations import ConversationStore  # noqa: E402
from group_context import GroupContextBuffer, GroupLine, format_group_turn  # noqa: E402


class ConversationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ConversationStore(Path(self.tmp.name) / "profile.db")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_records_group_identity(self) -> None:
        self.store.record("group-111-222", "group", 222, 111, "阿杜")
        row = self.store.get("group-111-222")
        assert row is not None
        self.assertEqual(row["scope"], "group")
        self.assertEqual(row["user_id"], "222")
        self.assertEqual(row["group_id"], "111")
        self.assertEqual(row["sender_name"], "阿杜")

    def test_private_has_no_group(self) -> None:
        self.store.record("private-222", "private", 222, None, "阿杜")
        row = self.store.get("private-222")
        assert row is not None
        self.assertIsNone(row["group_id"])

    def test_upsert_updates_nickname(self) -> None:
        self.store.record("private-222", "private", 222, None, "旧名")
        self.store.record("private-222", "private", 222, None, "新名")
        row = self.store.get("private-222")
        assert row is not None
        self.assertEqual(row["sender_name"], "新名")

    def test_rejects_bad_input(self) -> None:
        with self.assertRaises(ValueError):
            self.store.record("x", "channel", 1, None, "")
        with self.assertRaises(ValueError):
            self.store.record("x", "group", 1, None, "")


class GroupContextTests(unittest.TestCase):
    def test_buffer_is_per_group_and_bounded(self) -> None:
        buffer = GroupContextBuffer(max_messages=3)
        for i in range(5):
            buffer.add(1, f"u{i}", f"msg{i}")
        buffer.add(2, "other", "elsewhere")
        lines = buffer.drain(1)
        self.assertEqual([line.text for line in lines], ["msg2", "msg3", "msg4"])
        self.assertEqual(buffer.drain(1), [])  # drained once
        self.assertEqual([line.text for line in buffer.drain(2)], ["elsewhere"])

    def test_disabled_buffer_keeps_nothing(self) -> None:
        buffer = GroupContextBuffer(max_messages=0)
        buffer.add(1, "u", "hello")
        self.assertEqual(buffer.drain(1), [])

    def test_format_without_context_is_plain_trigger(self) -> None:
        self.assertEqual(format_group_turn([], "阿杜", "在吗", 1000), "阿杜: 在吗")

    def test_format_keeps_newest_within_budget(self) -> None:
        lines = [GroupLine("a", "x" * 50), GroupLine("b", "y" * 50), GroupLine("c", "最新")]
        out = format_group_turn(lines, "阿杜", "你怎么看", max_chars=60)
        self.assertIn("c: 最新", out)
        self.assertNotIn("x" * 50, out)
        self.assertTrue(out.endswith("阿杜: 你怎么看"))
        self.assertIn("[有人 @ 你]", out)




class TargetSnapshotTests(unittest.TestCase):
    def test_signed_message_identity_and_quote_are_rendered(self):
        line = GroupLine('同名', 'hello', user_id=12, message_id=-123, reply_to=456)
        text = format_group_turn([line], '同名', 'trigger', 1000, user_id=13, message_id=789)
        self.assertIn('[消息ID:-123 引用:456] 同名(12)', text)
        self.assertIn('[消息ID:789] 同名(13)', text)

    def test_capture_consumption_retains_later_identical_messages(self):
        buffer = GroupContextBuffer(10)
        buffer.add(1, 'a', 'same', user_id=1, message_id=4)
        captured = tuple(buffer.peek(1))
        buffer.add(1, 'a', 'same', user_id=1, message_id=4)
        buffer.consume(1, captured)
        self.assertEqual(len(buffer.peek(1)), 1)
        self.assertIsNot(buffer.peek(1)[0], captured[0])

    def test_targets_and_images_are_only_visible_budgeted_lines(self):
        from group_context import TurnSnapshot, visible_lines
        lines = (GroupLine('old', 'x' * 100, ('old.png',), 1, 101),
                 GroupLine('new', 'short', ('new.png',), 2, 102),
                 GroupLine('poke', 'synthetic', (), 3))
        snapshot = TurnSnapshot(lines, visible_lines(lines, 80))
        self.assertEqual([line.message_id for line in snapshot.targets], [102])
        self.assertEqual(snapshot.images(3), ['new.png'])


if __name__ == "__main__":
    unittest.main()
