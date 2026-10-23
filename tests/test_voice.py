from __future__ import annotations

import asyncio
import os
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

import main  # noqa: E402
from main import AppConfig, Bot  # noqa: E402
from voice import (  # noqa: E402
    MAX_CHARS,
    VoiceError,
    extract_voice_marker,
    synthesize,
    synthesize_fish,
    synthesize_stepfun,
    spoken_text,
)


class VoiceMarkerTests(unittest.TestCase):
    def test_extracts_and_strips(self) -> None:
        self.assertEqual(extract_voice_marker("好呀\n[语音]"), ("好呀", True))
        self.assertEqual(extract_voice_marker("普通回复"), ("普通回复", False))
        self.assertEqual(extract_voice_marker("[语音]"), ("", True))


class SpokenTextTests(unittest.TestCase):
    def test_stage_directions_are_removed(self) -> None:
        self.assertEqual(
            spoken_text("哼，才没有！*（脸红）* 真的（小声）"),
            "哼，才没有！ 真的",
        )

    def test_plain_text_is_unchanged(self) -> None:
        self.assertEqual(spoken_text("晚安呀"), "晚安呀")


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


class FishSynthTests(unittest.TestCase):
    def test_missing_key_raises(self) -> None:
        with self.assertRaises(VoiceError):
            asyncio.run(
                synthesize_fish("你好", reference_id="rid", api_key_env="MISSING_FISH_KEY")
            )

    def test_posts_expected_payload(self) -> None:
        import voice

        captured = {}

        class FakeClient:
            def __init__(self, **kwargs):
                captured["headers"] = kwargs.get("headers")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, path, json):
                captured["path"] = path
                captured["json"] = json
                return SimpleNamespace(status_code=200, content=b"mp3", text="")

        original = voice.httpx.AsyncClient
        original_env = os.environ.get("TEST_FISH_KEY")
        os.environ["TEST_FISH_KEY"] = "secret"
        voice.httpx.AsyncClient = FakeClient
        try:
            data = asyncio.run(
                synthesize_fish(
                    "你好呀",
                    reference_id="rid",
                    model="s2.1-pro-free",
                    api_key_env="TEST_FISH_KEY",
                )
            )
        finally:
            voice.httpx.AsyncClient = original
            if original_env is None:
                os.environ.pop("TEST_FISH_KEY", None)
            else:
                os.environ["TEST_FISH_KEY"] = original_env
        self.assertEqual(data, b"mp3")
        self.assertEqual(captured["path"], "/v1/tts")
        self.assertEqual(captured["json"]["reference_id"], "rid")
        self.assertEqual(captured["json"]["format"], "mp3")
        self.assertEqual(captured["headers"]["model"], "s2.1-pro-free")


class StepFunSynthTests(unittest.TestCase):
    def _run(self, **kwargs):
        import voice

        captured = {}
        original = voice.httpx.AsyncClient

        class Bound:
            def __init__(inner, **kw):
                captured["headers"] = kw.get("headers")

            async def __aenter__(inner):
                return inner

            async def __aexit__(inner, *args):
                return False

            async def post(inner, path, json):
                captured["path"] = path
                captured["json"] = json
                return SimpleNamespace(status_code=200, content=b"mp3", text="")

        voice.httpx.AsyncClient = Bound
        os.environ["TEST_STEPFUN_KEY"] = "secret"
        try:
            data = asyncio.run(
                synthesize_stepfun(
                    "你好呀",
                    voice="ruanmengnvsheng",
                    api_key_env="TEST_STEPFUN_KEY",
                    **kwargs,
                )
            )
        finally:
            voice.httpx.AsyncClient = original
            os.environ.pop("TEST_STEPFUN_KEY", None)
        return data, captured

    def test_instruction_is_sent_for_25_tts(self) -> None:
        data, captured = self._run(model="stepaudio-2.5-tts", instruction="傲娇一点")
        self.assertEqual(data, b"mp3")
        self.assertEqual(captured["path"], "/v1/audio/speech")
        self.assertEqual(captured["json"]["voice"], "ruanmengnvsheng")
        self.assertEqual(captured["json"]["instruction"], "傲娇一点")

    def test_instruction_is_omitted_for_mini(self) -> None:
        _data, captured = self._run(model="step-tts-mini", instruction="傲娇一点")
        self.assertNotIn("instruction", captured["json"])

    def test_missing_key_raises(self) -> None:
        with self.assertRaises(VoiceError):
            asyncio.run(
                synthesize_stepfun("你好", voice="x", api_key_env="MISSING_STEPFUN_KEY")
            )


class VoiceProbabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot = object.__new__(Bot)
        self.bot.config = AppConfig()
        self.bot.config.voice.enabled = True
        self.bot.config.voice.probability = 0.5
        self.bot.config.voice.max_chars = 80

    def test_roll_follows_the_configured_probability(self) -> None:
        from unittest.mock import patch

        with patch("main.random.random", return_value=0.4):
            self.assertTrue(self.bot._voice_roll("晚安呀"))
        with patch("main.random.random", return_value=0.6):
            self.assertFalse(self.bot._voice_roll("晚安呀"))

    def test_long_text_never_rolls(self) -> None:
        from unittest.mock import patch

        with patch("main.random.random", return_value=0.0):
            self.assertFalse(self.bot._voice_roll("长" * 200))

    def test_disabled_or_zero_probability_never_rolls(self) -> None:
        from unittest.mock import patch

        self.bot.config.voice.probability = 0
        with patch("main.random.random", return_value=0.0):
            self.assertFalse(self.bot._voice_roll("晚安"))
        self.bot.config.voice.probability = 0.5
        self.bot.config.voice.enabled = False
        with patch("main.random.random", return_value=0.0):
            self.assertFalse(self.bot._voice_roll("晚安"))


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
