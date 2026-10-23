from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from group_context import GroupContextBuffer  # noqa: E402
from main import AppConfig, Bot  # noqa: E402
from onebot import MessageParser  # noqa: E402


def notice(
    user: int = 7,
    target: int = 999,
    group: int | None = 42,
    notice_type: str = "notify",
    sub_type: str = "poke",
) -> dict:
    payload = {
        "post_type": "notice",
        "notice_type": notice_type,
        "sub_type": sub_type,
        "user_id": user,
        "target_id": target,
    }
    if group is not None:
        payload["group_id"] = group
    return payload


class PokeNoticeParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = MessageParser(self_id=999)

    def test_group_poke(self) -> None:
        poke = self.parser.parse_notice(notice())
        assert poke is not None
        self.assertEqual((poke.user_id, poke.target_id, poke.group_id), (7, 999, 42))

    def test_private_poke(self) -> None:
        poke = self.parser.parse_notice(notice(group=None))
        assert poke is not None
        self.assertIsNone(poke.group_id)

    def test_other_notices_are_ignored(self) -> None:
        self.assertIsNone(self.parser.parse_notice(notice(sub_type="input_status")))
        self.assertIsNone(
            self.parser.parse_notice(notice(notice_type="group_increase", sub_type="approve"))
        )
        self.assertIsNone(self.parser.parse_notice({"post_type": "message"}))

    def test_poke_message_segment_gets_a_marker(self) -> None:
        parsed = self.parser.parse({
            "post_type": "message",
            "message_type": "private",
            "user_id": 7,
            "message_id": 1,
            "sender": {"nickname": "甲"},
            "message": [{"type": "poke", "data": {"type": "1", "id": "1"}}],
        })
        assert parsed is not None
        self.assertEqual(parsed.text, "[戳一戳]")


class PokeHandlingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot = object.__new__(Bot)
        self.bot.config = AppConfig()
        self.bot.config.onebot.self_id = 999
        self.bot.group_context = GroupContextBuffer(10)
        self.bot._names = {7: "甲", 8: "乙"}
        self.bot._poke_last = {}
        self.bot._poke_cooldown = 8.0
        parser = MessageParser(self_id=999)
        self.bot.onebot = SimpleNamespace(parse_notice=parser.parse_notice)
        self.handled = []

        async def handle(event) -> None:
            self.handled.append(event)

        self.bot.handle = handle

    def test_poking_the_bot_triggers_one_reply(self) -> None:
        asyncio.run(self.bot.handle_notice(notice()))
        self.assertEqual(len(self.handled), 1)
        event = self.handled[0]
        self.assertEqual(event.message_type, "group")
        self.assertEqual(event.group_id, 42)
        self.assertTrue(event.at_me)
        self.assertEqual(event.sender_name, "甲")
        self.assertEqual(event.text, "[有人戳了戳你]")

        asyncio.run(self.bot.handle_notice(notice()))  # within cooldown
        self.assertEqual(len(self.handled), 1)

    def test_private_poke_is_answered(self) -> None:
        asyncio.run(self.bot.handle_notice(notice(group=None)))
        self.assertEqual(len(self.handled), 1)
        self.assertEqual(self.handled[0].message_type, "private")
        self.assertFalse(self.handled[0].at_me)

    def test_poking_someone_else_becomes_context(self) -> None:
        asyncio.run(self.bot.handle_notice(notice(target=8)))
        self.assertEqual(self.handled, [])
        lines = self.bot.group_context.peek(42)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].sender, "甲")
        self.assertEqual(lines[0].text, "[戳了戳 乙]")
        self.assertEqual(lines[0].user_id, 7)

    def test_bot_poking_itself_is_ignored(self) -> None:
        asyncio.run(self.bot.handle_notice(notice(user=999)))
        self.assertEqual(self.handled, [])


if __name__ == "__main__":
    unittest.main()
