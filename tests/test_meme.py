from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

import meme  # noqa: E402
from main import AppConfig, Bot  # noqa: E402
from meme import extract_meme_marker, render_meme  # noqa: E402


class MemeMarkerTests(unittest.TestCase):
    def test_extracts_and_strips(self) -> None:
        self.assertEqual(
            extract_meme_marker("笑死\n[梗图: 我不理解]"),
            ("笑死", "我不理解"),
        )
        self.assertEqual(extract_meme_marker("普通回复"), ("普通回复", None))


class RenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def test_gradient_card(self) -> None:
        path = render_meme("我不理解", output_dir=self.out)
        self.assertTrue(path.is_file())
        from PIL import Image

        with Image.open(path) as image:
            self.assertEqual(image.size, (meme.CANVAS, meme.CANVAS))
        self.assertFalse(path.exists() and path.stat().st_size == 0)

    def test_uses_the_sticker_as_base(self) -> None:
        from PIL import Image

        base = self.out / "base.png"
        Image.new("RGB", (300, 200), (200, 40, 40)).save(base)
        path = render_meme("无语", base_image=base, output_dir=self.out)
        with Image.open(path) as image:
            self.assertEqual(image.size, (meme.CANVAS, meme.CANVAS))
            self.assertNotEqual(image.convert("RGB").getpixel((5, 5)), (200, 40, 40))

    def test_long_text_is_truncated(self) -> None:
        path = render_meme("长" * 100, output_dir=self.out)
        self.assertTrue(path.is_file())

    def test_empty_text_is_rejected(self) -> None:
        with self.assertRaises(meme.MemeError):
            render_meme("   ", output_dir=self.out)


class SendMemeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bot = object.__new__(Bot)
        self.bot.config = AppConfig()
        self.bot.config.meme.directory = self.tmp.name
        self.bot.config.meme.cooldown_seconds = 30
        self.bot._meme_last = {}
        self.bot.stickers = None
        self.sent = []

        async def send_private_image(user_id, file):
            self.sent.append((user_id, file))
            return {"message_id": 1}

        self.bot.onebot = SimpleNamespace(send_private_image=send_private_image)

    def event(self):
        return SimpleNamespace(message_type="private", user_id=7, group_id=None)

    def test_sends_once_then_cooldown(self) -> None:
        self.assertTrue(asyncio.run(self.bot._send_meme(self.event(), "我不理解")))
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0][1].startswith("base64://"))
        self.assertFalse(asyncio.run(self.bot._send_meme(self.event(), "再说一次")))
        self.assertEqual(len(self.sent), 1)


if __name__ == "__main__":
    unittest.main()
