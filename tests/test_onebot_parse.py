from __future__ import annotations

import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

# onebot imports websockets at module level; the parser does not need it.
_ws = types.ModuleType("websockets")
_ws.exceptions = types.SimpleNamespace(WebSocketException=Exception)
sys.modules.setdefault("websockets", _ws)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from onebot import MessageParser, OneBotConfig, OneBotWSClient  # noqa: E402


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

    def test_friend_request(self) -> None:
        parsed = self.parser.parse_request({
            "post_type": "request",
            "request_type": "friend",
            "user_id": 12345,
            "comment": "我是小王",
            "flag": "friend-flag",
        })
        assert parsed is not None
        self.assertEqual(parsed.kind, "friend")
        self.assertEqual(parsed.user_id, 12345)
        self.assertEqual(parsed.comment, "我是小王")
        self.assertEqual(parsed.flag, "friend-flag")
        self.assertIsNone(parsed.group_id)

    def test_group_invite_and_join_request(self) -> None:
        invite = self.parser.parse_request({
            "post_type": "request",
            "request_type": "group",
            "sub_type": "invite",
            "group_id": 42,
            "group_name": "测试群",
            "user_id": 7,
            "comment": "来玩",
            "flag": "invite-flag",
        })
        assert invite is not None
        self.assertEqual(invite.kind, "group_invite")
        self.assertEqual(invite.group_id, 42)
        self.assertEqual(invite.group_name, "测试群")

        join = self.parser.parse_request({
            "post_type": "request",
            "request_type": "group",
            "sub_type": "add",
            "group_id": 43,
            "user_id": 8,
            "flag": "add-flag",
        })
        assert join is not None
        self.assertEqual(join.kind, "group_add")
        self.assertEqual(join.group_id, 43)

    def test_invalid_requests_are_ignored(self) -> None:
        self.assertIsNone(self.parser.parse_request({"post_type": "message"}))
        self.assertIsNone(self.parser.parse_request({
            "post_type": "request", "request_type": "friend", "flag": "",
            "user_id": 1,
        }))
        self.assertIsNone(self.parser.parse_request({
            "post_type": "request", "request_type": "group", "flag": "x",
            "user_id": 1,
        }))
        self.assertIsNone(self.parser.parse_request({
            "post_type": "request", "request_type": "unknown", "flag": "x",
            "user_id": 1,
        }))

    def test_parse_records_receive_time(self) -> None:
        before = time.time()
        parsed = self.parser.parse(event([{"type": "text", "data": {"text": "在吗"}}]))
        assert parsed is not None
        self.assertGreaterEqual(parsed.received_at, before)

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


class SignedMessageParserTests(unittest.TestCase):
    def test_signed_quote_ids_in_array_and_cq(self):
        for message in (
            [{"type": "reply", "data": {"id": "-556"}}, {"type": "text", "data": {"text": "hi"}}],
            "[CQ:reply,id=-556]hi",
            [{"type": "reply", "data": {"id": -556}}, {"type": "text", "data": {"text": "hi"}}],
        ):
            with self.subTest(message=message):
                parsed = MessageParser(999).parse(event(message, message_id=-1000))
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed.message_id, -1000)
                self.assertEqual(parsed.reply_to, -556)
                self.assertEqual(parsed.text, "hi")

    def test_invalid_quote_ids_are_not_authority(self):
        for value in (True, False, 0, 1.5, "0", "-0", "001", "+1", "1.0", "١٢", "all"):
            with self.subTest(value=value):
                parsed = MessageParser(999).parse(event([
                    {"type": "reply", "data": {"id": value}},
                    {"type": "text", "data": {"text": "hi"}},
                ]))
                self.assertIsNotNone(parsed)
                self.assertIsNone(parsed.reply_to)

    def test_signed_ids_are_deduplicated_and_invalid_ids_are_not_real(self):
        parser = MessageParser(999)
        self.assertEqual(parser.parse(event("hi", message_id="-1000")).message_id, -1000)
        self.assertIsNone(parser.parse(event("hi", message_id=-1000)))
        for value in (True, False, 1.5, "+2", "002", "0", "-0"):
            with self.subTest(value=value):
                self.assertEqual(parser.parse(event("hi", message_id=value)).message_id, 0)

    def test_bare_negative_quote_is_not_discarded(self):
        for message in ("[CQ:reply,id=-77]", [{"type": "reply", "data": {"id": "-77"}}]):
            parsed = MessageParser(999).parse(event(message))
            self.assertEqual(parsed.reply_to, -77)
            self.assertEqual(parsed.text, "[空引用]")


class SendInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = OneBotWSClient(OneBotConfig(), AsyncMock())
        self.client.call = AsyncMock(return_value={"message_id": -901})

    async def test_quote_and_two_mentions_use_ordered_segments_and_literal_cq_text(self):
        text = "raw [CQ:at,qq=all][CQ:reply,id=123]"
        result = await self.client.send_interaction("group", 42, text, reply_to=-202,
                                                    user_ids=(8, 9), timeout=3.5)
        self.assertEqual(result, {"message_id": -901})
        self.client.call.assert_awaited_once_with("send_group_msg", {
            "group_id": 42, "message": [
                {"type": "reply", "data": {"id": "-202"}},
                {"type": "at", "data": {"qq": "8"}},
                {"type": "at", "data": {"qq": "9"}},
                {"type": "text", "data": {"text": text}},
            ],
        }, timeout=3.5)

    async def test_private_quote_and_group_mention_without_quote(self):
        await self.client.send_interaction("private", 7, "hi", reply_to=101)
        self.client.call.assert_awaited_with("send_private_msg", {
            "user_id": 7, "message": [
                {"type": "reply", "data": {"id": "101"}},
                {"type": "text", "data": {"text": "hi"}},
            ],
        }, timeout=10)
        await self.client.send_interaction("group", 42, "hello", user_ids=(8,))
        self.client.call.assert_awaited_with("send_group_msg", {
            "group_id": 42, "message": [
                {"type": "at", "data": {"qq": "8"}},
                {"type": "text", "data": {"text": "hello"}},
            ],
        }, timeout=10)

    async def test_rejects_invalid_scope_ids_mentions_text_and_timeouts(self):
        for scope, chat, text, kwargs in (
            ("channel", 42, "hi", {}), ("group", True, "hi", {}),
            ("group", 0, "hi", {}), ("group", "42", "hi", {}),
            ("group", 42, "hi", {"reply_to": True}),
            ("group", 42, "hi", {"reply_to": 0}),
            ("group", 42, "hi", {"reply_to": "-202"}),
            ("group", 42, "hi", {"user_ids": (True,)}),
            ("group", 42, "hi", {"user_ids": (0,)}),
            ("group", 42, "hi", {"user_ids": ("all",)}),
            ("group", 42, "hi", {"user_ids": (7, 8, 9, 10)}),
            ("group", 42, "hi", {"user_ids": (8, 8)}),
            ("group", 42, "hi", {"user_ids": [8]}),
            ("private", 7, "hi", {"user_ids": (8,)}),
            ("group", 42, " ", {}), ("group", 42, None, {}),
            ("group", 42, "hi", {"timeout": True}),
            ("group", 42, "hi", {"timeout": 0}),
            ("group", 42, "hi", {"timeout": float("inf")}),
        ):
            with self.subTest(scope=scope, chat=chat, kwargs=kwargs), self.assertRaises(ValueError):
                await self.client.send_interaction(scope, chat, text, **kwargs)
        self.client.call.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
