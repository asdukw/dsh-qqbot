from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

# onebot imports websockets at module level; the parser does not need it.
_ws = types.ModuleType("websockets")
_ws.exceptions = types.SimpleNamespace(WebSocketException=Exception)
sys.modules.setdefault("websockets", _ws)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from onebot import MessageParser  # noqa: E402


def event(message, **extra):
    payload = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 42,
        "user_id": 7,
        "message_id": 1000,
        "sender": {"nickname": "测试"},
        "message": message,
    }
    payload.update(extra)
    return payload


class MessageParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = MessageParser(self_id=999)

    def test_array_reply_and_at(self) -> None:
        parsed = self.parser.parse(event([
            {"type": "reply", "data": {"id": "555"}},
            {"type": "at", "data": {"qq": "999"}},
            {"type": "text", "data": {"text": " 在吗"}},
        ]))
        assert parsed is not None
        self.assertEqual(parsed.reply_to, 555)
        self.assertTrue(parsed.at_me)
        self.assertIn("在吗", parsed.text)

    def test_string_cq_reply_and_at(self) -> None:
        parsed = self.parser.parse(event(
            "[CQ:reply,id=556][CQ:at,qq=999] 在吗"
        ))
        assert parsed is not None
        self.assertEqual(parsed.reply_to, 556)
        self.assertTrue(parsed.at_me)
        self.assertEqual(parsed.text, "在吗")

    def test_at_all_does_not_count(self) -> None:
        parsed = self.parser.parse(event([
            {"type": "at", "data": {"qq": "all"}},
            {"type": "text", "data": {"text": "大家好"}},
        ]))
        assert parsed is not None
        self.assertFalse(parsed.at_me)
        self.assertIsNone(parsed.reply_to)

    def test_plain_message_has_no_reply_target(self) -> None:
        parsed = self.parser.parse(event("随便聊聊"))
        assert parsed is not None
        self.assertIsNone(parsed.reply_to)
        self.assertFalse(parsed.at_me)

    def test_image_marker_and_url(self) -> None:
        parsed = self.parser.parse(event([
            {"type": "text", "data": {"text": "/card install"}},
            {"type": "image", "data": {"url": "https://example.com/card.png"}},
        ]))
        assert parsed is not None
        self.assertEqual(parsed.text, "/card install[图片]")
        self.assertEqual(parsed.images, ("https://example.com/card.png",))


if __name__ == "__main__":
    unittest.main()
