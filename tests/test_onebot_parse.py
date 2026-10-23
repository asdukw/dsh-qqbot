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

    def test_bare_at_still_produces_an_event(self) -> None:
        parsed = self.parser.parse(event([{"type": "at", "data": {"qq": "999"}}]))
        assert parsed is not None
        self.assertTrue(parsed.at_me)
        self.assertEqual(parsed.text, "[空艾特]")

    def test_bare_cq_at_still_produces_an_event(self) -> None:
        parsed = self.parser.parse(event("[CQ:at,qq=999]"))
        assert parsed is not None
        self.assertEqual(parsed.text, "[空艾特]")

    def test_empty_message_without_at_is_dropped(self) -> None:
        self.assertIsNone(self.parser.parse(event([])))

    def test_bare_quote_still_produces_an_event(self) -> None:
        parsed = self.parser.parse(event([{"type": "reply", "data": {"id": "555"}}]))
        assert parsed is not None
        self.assertEqual(parsed.text, "[空引用]")
        self.assertEqual(parsed.reply_to, 555)
        self.assertFalse(parsed.at_me)

    def test_group_card_and_nickname_are_kept_apart(self) -> None:
        parsed = self.parser.parse(
            event("在吗", sender={"card": "群名片", "nickname": "真名"})
        )
        assert parsed is not None
        self.assertEqual(parsed.sender_name, "群名片")
        self.assertEqual(parsed.sender_nickname, "真名")

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
        self.assertEqual(parsed.stickers, ())

    def test_sticker_segment_is_flagged_and_labelled(self) -> None:
        parsed = self.parser.parse(event([
            {"type": "image", "data": {
                "url": "https://example.com/s.jpg",
                "summary": "[动画表情]",
                "sub_type": 1,
            }},
        ]))
        assert parsed is not None
        self.assertEqual(parsed.text, "[表情包]")
        self.assertEqual(parsed.images, ("https://example.com/s.jpg",))
        self.assertEqual(parsed.stickers, ("https://example.com/s.jpg",))

    def test_sub_type_alone_marks_a_sticker(self) -> None:
        parsed = self.parser.parse(event([
            {"type": "image", "data": {"url": "https://example.com/s.png", "sub_type": "1"}},
        ]))
        assert parsed is not None
        self.assertEqual(parsed.stickers, ("https://example.com/s.png",))

    def test_a_normal_image_is_not_a_sticker(self) -> None:
        parsed = self.parser.parse(event([
            {"type": "image", "data": {
                "url": "https://example.com/p.jpg", "summary": "[图片]", "sub_type": 0,
            }},
        ]))
        assert parsed is not None
        self.assertEqual(parsed.stickers, ())
        self.assertEqual(parsed.text, "[图片]")


if __name__ == "__main__":
    unittest.main()
