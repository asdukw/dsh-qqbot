from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

import main  # noqa: E402
from main import AppConfig, Bot  # noqa: E402
from voice import MAX_CHARS, extract_voice_marker, synthesize  # noqa: E402


class VoiceMarkerTests(unittest.TestCase):
    def test_extracts_and_strips(self) -> None:
        self.assertEqual(extract_voice_marker("好呀\n[语音]"), ("好呀", True))
        self.assertEqual(extract_voice_marker("普通回复"), ("普通回复", False))
        self.assertEqual(extract_voice_marker("[语音]"), ("", True))


class SynthesizeTests(unittest.TestCase):
    def test_collects_audio_and_truncates(self) -> None:
        captured = {}

        class FakeCommunicate:
            def __init__(self, text, voice, rate, volume):
                captured.update(text=text, voice=voice, rate=rate)

            async def stream(self):
                yield {"type": "audio", "data": b"abc"}
                yield {"type": "WordBoundary", "data": b""}
                yield {"type": "audio", "data": b"def"}

        fake = types.ModuleType("edge_tts")
        fake.Communicate = FakeCommunicate
        original = sys.modules.get("edge_tts")
        sys.modules["edge_tts"] = fake
        try:
            data = asyncio.run(synthesize("你好", voice="v", rate="+10%"))
            self.assertEqual(data, b"abcdef")
            self.assertEqual(captured["voice"], "v")
            asyncio.run(synthesize("长" * (MAX_CHARS + 20)))
            self.assertEqual(len(captured["text"]), MAX_CHARS)
        finally:
            if original is None:
                sys.modules.pop("edge_tts", None)
            else:
                sys.modules["edge_tts"] = original


class SendVoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot = object.__new__(Bot)
        self.bot.config = AppConfig()
        self.bot.config.voice.enabled = True
        self.bot.config.voice.max_chars = 80
        self.bot.config.voice.cooldown_seconds = 30
        self.bot._voice_last = {}
        self.sent = []

        async def send_private_record(user_id, file):
            self.sent.append((user_id, file))
            return {"message_id": 1}

        self.bot.onebot = SimpleNamespace(send_private_record=send_private_record)

        self._original = main.synthesize

        async def fake_synth(text, voice, rate):
            return b"mp3-bytes"

        main.synthesize = fake_synth

    def tearDown(self) -> None:
        main.synthesize = self._original

    def event(self, user_id=7):
        return SimpleNamespace(message_type="private", user_id=user_id, group_id=None)

    def test_sends_once_then_hits_cooldown(self) -> None:
        self.assertTrue(asyncio.run(self.bot._send_voice(self.event(), "你好呀")))
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0][1].startswith("base64://"))
        self.assertFalse(asyncio.run(self.bot._send_voice(self.event(), "再说一句")))
        self.assertEqual(len(self.sent), 1)

    def test_long_text_skips_voice(self) -> None:
        self.assertFalse(asyncio.run(self.bot._send_voice(self.event(), "长" * 200)))
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
