from __future__ import annotations

import asyncio
import base64
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from group_context import GroupContextBuffer  # noqa: E402
from images import ImageStore  # noqa: E402
from main import AppConfig, Bot  # noqa: E402
from memory import Memory  # noqa: E402
from onebot import MessageEvent  # noqa: E402

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PNG_URI = "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")


class FakeClient:
    def __init__(self, reply: str = "一只橘猫趴在键盘上"):
        self.reply = reply
        self.config = SimpleNamespace(model="fake-vision")
        self.calls: list[list[dict]] = []

    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        return self.reply


class ImageStoreTests(unittest.TestCase):
    def test_save_is_content_addressed_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ImageStore(tmp, "images", keep_days=0, max_files=0)
            first = store.save(PNG, "image/png")
            second = store.save(PNG, "image/png")
            self.assertEqual(first.sha1, second.sha1)
            self.assertEqual(first.path, second.path)
            self.assertTrue(first.path.exists())
            self.assertEqual(first.relative, f"images/{first.path.name}")
            self.assertEqual(first.path.read_bytes(), PNG)

    def test_prune_drops_old_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ImageStore(tmp, "images", keep_days=1, max_files=0)
            saved = store.save(PNG, "image/png")
            old = time.time() - 3 * 86400
            os.utime(saved.path, (old, old))
            store.prune()
            self.assertFalse(saved.path.exists())


class CaptionCacheTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = Memory(Path(tmp) / "memory.db")
            try:
                self.assertEqual(asyncio.run(memory.caption_get("abc")), "")
                asyncio.run(memory.caption_set("abc", "一只猫", model="m", path="images/x.png", mime="image/png"))
                self.assertEqual(asyncio.run(memory.caption_get("abc")), "一只猫")
            finally:
                memory.close()


def make_bot(tmp: str) -> Bot:
    bot = object.__new__(Bot)
    bot.config = AppConfig()
    bot.config.dsh.workspace = tmp
    bot.config.dsh.image_dir = "images"
    bot.config.dsh.image_caption = True
    bot.config.dsh.image_mode = "placeholder"
    bot.memory = Memory(Path(tmp) / "memory.db")
    bot.client = FakeClient()
    bot.image_store = None
    bot.conversations = None
    bot._persona = Mock()
    bot._prepare_persona = Mock()
    bot._opening_context = Mock(return_value="")
    bot.personas = SimpleNamespace(consume_opening=Mock())
    bot.group_context = GroupContextBuffer(10)
    return bot


class PrepareImageTests(unittest.TestCase):
    def test_store_caption_and_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp)
            try:
                prepared = asyncio.run(bot._prepare_images([PNG_URI]))
                self.assertEqual(len(prepared), 1)
                image = prepared[0]
                self.assertEqual(image.description, "一只橘猫趴在键盘上")
                self.assertTrue((Path(tmp) / image.relative).exists())
                self.assertEqual(len(bot.client.calls), 1)

                # A second turn reuses the caption cache and the same file.
                again = asyncio.run(bot._prepare_images([PNG_URI]))
                self.assertEqual(again[0].sha1, image.sha1)
                self.assertEqual(len(bot.client.calls), 1)

                hint = bot._image_hint(prepared)
                self.assertIn("一只橘猫趴在键盘上", hint)
                self.assertIn(image.relative, hint)
                self.assertNotIn("data:", hint)
            finally:
                bot.memory.close()

    def test_reply_routes_images_to_dsh(self) -> None:
        bot = object.__new__(Bot)
        bot.dsh = object()
        called = []

        async def fake_dsh(conversation_id, event, text):
            called.append(("dsh", conversation_id))

        async def fake_llm(conversation_id, event, text):
            called.append(("llm", conversation_id))

        bot._reply_dsh = fake_dsh
        bot._reply_llm = fake_llm
        event = MessageEvent(
            message_type="group", user_id=1, message_id=1, text="[图片] 这是啥",
            at_me=True, sender_name="乙", group_id=7, images=(PNG_URI,),
        )
        asyncio.run(bot._reply("group:7", event, "[图片] 这是啥"))
        self.assertEqual(called, [("dsh", "group:7")])


class DshPlaceholderTests(unittest.TestCase):
    def test_dsh_receives_text_not_image_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp)
            captured = {}

            class FakeDsh:
                def run(self, session_id, prompt, images=()):
                    captured["session"] = session_id
                    captured["prompt"] = prompt
                    captured["images"] = list(images)
                    return "收到"

            bot.dsh = FakeDsh()

            async def ensure_card():
                return True

            def turn_prompt(event, text):
                return "用户: 这是啥", 0

            async def recall(event, text):
                return ""

            async def send_reply(event, conversation_id, reply):
                captured["reply"] = reply

            bot.ensure_card = ensure_card
            bot._turn_prompt = turn_prompt
            bot._recall_block = recall
            bot._send_reply = send_reply
            event = MessageEvent(
                message_type="group", user_id=1, message_id=1, text="这是啥",
                at_me=True, sender_name="乙", group_id=7, images=(PNG_URI,),
            )
            try:
                asyncio.run(bot._reply_dsh("group:7", event, "这是啥"))
                self.assertEqual(captured["images"], [])
                self.assertIn("[图片附件]", captured["prompt"])
                self.assertIn("一只橘猫趴在键盘上", captured["prompt"])
                self.assertNotIn("data:", captured["prompt"])
                self.assertEqual(captured["reply"], "收到")
            finally:
                bot.memory.close()


if __name__ == "__main__":
    unittest.main()
