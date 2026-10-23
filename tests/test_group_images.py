from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from group_context import GroupContextBuffer, format_group_turn  # noqa: E402
from llm import LLMError  # noqa: E402
from main import AppConfig, Bot  # noqa: E402
from onebot import MessageEvent  # noqa: E402

PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
PNG2 = PNG[:-4] + "AAAA"


def make_bot(images: int = 2) -> Bot:
    bot = object.__new__(Bot)
    bot.config = AppConfig()
    bot.config.bot.group_context_images = images
    bot.group_context = GroupContextBuffer(10)
    return bot


class GroupContextImageTests(unittest.TestCase):
    def test_images_are_kept_with_their_line(self) -> None:
        buf = GroupContextBuffer(10)
        buf.add(1, "甲", "[图片]", images=("https://a/1.jpg",))
        buf.add(1, "乙", "看这个", images=("https://a/2.jpg",))
        self.assertEqual(buf.images(1, 2), ["https://a/1.jpg", "https://a/2.jpg"])

    def test_newest_images_win_and_are_deduplicated(self) -> None:
        buf = GroupContextBuffer(10)
        buf.add(1, "甲", "[图片]", images=("https://a/1.jpg", "https://a/2.jpg"))
        buf.add(1, "乙", "又发一次", images=("https://a/2.jpg",))
        # The two newest, rendered in the order they were posted.
        self.assertEqual(buf.images(1, 2), ["https://a/1.jpg", "https://a/2.jpg"])
        self.assertEqual(buf.images(1, 1), ["https://a/2.jpg"])

    def test_zero_limit_disabled(self) -> None:
        buf = GroupContextBuffer(10)
        buf.add(1, "甲", "[图片]", images=("https://a/1.jpg",))
        self.assertEqual(buf.images(1, 0), [])

    def test_images_do_not_leak_into_the_text_turn(self) -> None:
        buf = GroupContextBuffer(10)
        buf.add(1, "甲", "[图片]", images=("https://a/1.jpg",))
        rendered = format_group_turn(buf.drain(1), "乙", "这是啥", 3000)
        self.assertNotIn("https://", rendered)
        self.assertIn("甲: [图片]", rendered)


class TurnImageTests(unittest.TestCase):
    def group_event(self, **overrides) -> MessageEvent:
        base = dict(
            message_type="group", user_id=1, message_id=1, text="[图片] 这是啥",
            at_me=True, sender_name="乙", group_id=7,
        )
        base.update(overrides)
        return MessageEvent(**base)

    def test_own_image_comes_first_then_recent_context(self) -> None:
        bot = make_bot(images=2)
        bot.group_context.add(7, "甲", "[图片]", images=("https://a/old.jpg",))
        bot.group_context.add(7, "甲", "[图片]", images=("https://a/new.jpg",))
        event = self.group_event(images=("https://a/own.jpg",))
        self.assertEqual(
            bot._turn_images(event),
            ["https://a/own.jpg", "https://a/old.jpg", "https://a/new.jpg"],
        )

    def test_duplicates_are_collapsed(self) -> None:
        bot = make_bot(images=2)
        bot.group_context.add(7, "甲", "[图片]", images=("https://a/own.jpg",))
        event = self.group_event(images=("https://a/own.jpg",))
        self.assertEqual(bot._turn_images(event), ["https://a/own.jpg"])

    def test_private_turns_never_pull_group_images(self) -> None:
        bot = make_bot()
        bot.group_context.add(7, "甲", "[图片]", images=("https://a/own.jpg",))
        event = self.group_event(message_type="private", group_id=None, images=())
        self.assertEqual(bot._turn_images(event), [])


class FakeClient:
    def __init__(self, reply: str = "一张蓝色的图片", error: Exception | None = None):
        self.reply = reply
        self.error = error
        self.calls: list[list[dict]] = []

    async def chat(self, messages):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return self.reply


class ImageCaptionTests(unittest.TestCase):
    def test_captions_send_the_image_and_collect_text(self) -> None:
        bot = make_bot()
        bot.client = FakeClient("一只橘猫趴在键盘上")
        captions = asyncio.run(bot._image_captions([PNG]))
        self.assertEqual(captions, ["一只橘猫趴在键盘上"])
        parts = bot.client.calls[0][-1]["content"]
        self.assertEqual([part["type"] for part in parts], ["text", "image_url"])

    def test_a_failing_caption_does_not_break_the_turn(self) -> None:
        bot = make_bot()
        bot.client = FakeClient(error=LLMError("boom"))
        self.assertEqual(asyncio.run(bot._image_captions([PNG])), [])


if __name__ == "__main__":
    unittest.main()
