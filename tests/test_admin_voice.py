from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from admin_voice import apply_voice_action, preview_audio, voice_data  # noqa: E402
from voice_settings import VoiceSettingsError  # noqa: E402

FISH_VOICE = "8e80c3dcdf894213a10b48e9b08ad8c6"


class AdminVoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = str(Path(self.tmp.name) / "voice-state.json")
        self.config: dict[str, Any] = {
            "voice": {
                "enabled": True,
                "provider": "edge",
                "voice": "zh-CN-XiaoxiaoNeural",
                "rate": "+5%",
                "model": "stepaudio-3-tts",
                "api_key_env": "STEPFUN_API_KEY",
                "base_url": "https://api.stepfun.com",
                "instruction": "",
                "speed": 1.0,
                "state_file": self.state,
            }
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_config_is_the_default_source(self) -> None:
        data = voice_data(self.config)
        self.assertEqual(data["source"], "config")
        self.assertEqual(data["active"]["provider"], "edge")
        self.assertEqual(data["active"]["voice"], "zh-CN-XiaoxiaoNeural")
        # config 里的音色不在上架目录时没有显示名，面板回退到 voice id。
        self.assertEqual(data["active"]["name"], "")
        self.assertEqual(data["active"]["model"], "stepaudio-3-tts")
        self.assertTrue(data["active"]["key_ready"])
        self.assertEqual([row["name"] for row in data["catalog"]], ["菲比", "流萤"])

    def test_switching_across_providers_uses_provider_defaults(self) -> None:
        data = apply_voice_action(
            self.config, {"provider": "fish", "voice": FISH_VOICE, "name": "菲比"}
        )
        self.assertEqual(data["source"], "panel")
        self.assertEqual(data["active"]["provider"], "fish")
        self.assertEqual(data["active"]["model"], "s2.1-pro-free")
        self.assertEqual(data["active"]["api_key_env"], "FISH_API_KEY")
        self.assertEqual(data["active"]["base_url"], "https://api.fish.audio")
        self.assertIsNotNone(data["updated_at"])
        self.assertTrue(Path(self.state).is_file())

    def test_switching_within_a_provider_keeps_config_connection(self) -> None:
        self.config["voice"].update(
            provider="fish",
            voice=FISH_VOICE,
            model="custom-model",
            api_key_env="MY_FISH_KEY",
            base_url="https://proxy.example/fish",
        )
        data = apply_voice_action(
            self.config, {"provider": "fish", "voice": "79b9954c15f24deeab2319ec5f44f3b0"}
        )
        self.assertEqual(data["active"]["model"], "custom-model")
        self.assertEqual(data["active"]["api_key_env"], "MY_FISH_KEY")
        self.assertEqual(data["active"]["base_url"], "https://proxy.example/fish")
        # 目录里的显示名自动补上。
        self.assertEqual(data["active"]["name"], "流萤")

    def test_clear_restores_config_default(self) -> None:
        apply_voice_action(self.config, {"provider": "fish", "voice": FISH_VOICE})
        data = apply_voice_action(self.config, {"clear": True})
        self.assertEqual(data["source"], "config")
        self.assertFalse(Path(self.state).exists())

    def test_invalid_actions_are_rejected(self) -> None:
        for payload in (
            {"provider": "nope", "voice": FISH_VOICE},
            {"provider": "fish", "voice": "bad id"},
            {"provider": "fish", "voice": FISH_VOICE, "extra": 1},
            {"clear": "yes"},
            {"provider": "fish"},
        ):
            with self.assertRaises(ValueError):
                apply_voice_action(self.config, payload)
        self.assertFalse(Path(self.state).exists())

    def test_invalid_state_file_is_reported(self) -> None:
        Path(self.state).write_text("{oops", encoding="utf-8")
        with self.assertRaises(VoiceSettingsError):
            voice_data(self.config)

    def test_preview_uses_the_requested_provider(self) -> None:
        captured: dict[str, Any] = {}

        async def fake_fish(text, **kwargs):
            captured.update(text=text, **kwargs)
            return b"mp3"

        old = os.environ.get("FISH_API_KEY")
        os.environ["FISH_API_KEY"] = "secret"
        try:
            with patch("admin_voice.synthesize_fish", new=fake_fish):
                audio = preview_audio(self.config, {"provider": "fish", "voice": FISH_VOICE})
        finally:
            if old is None:
                os.environ.pop("FISH_API_KEY", None)
            else:
                os.environ["FISH_API_KEY"] = old
        self.assertEqual(audio, b"mp3")
        self.assertEqual(captured["reference_id"], FISH_VOICE)
        self.assertEqual(captured["model"], "s2.1-pro-free")
        self.assertEqual(captured["base_url"], "https://api.fish.audio")

    def test_preview_reports_a_missing_key(self) -> None:
        old = os.environ.pop("FISH_API_KEY", None)
        try:
            with self.assertRaisesRegex(ValueError, "FISH_API_KEY"):
                preview_audio(self.config, {"provider": "fish", "voice": FISH_VOICE})
        finally:
            if old is not None:
                os.environ["FISH_API_KEY"] = old

    def test_preview_of_the_active_edge_voice_uses_config_rate(self) -> None:
        captured: dict[str, Any] = {}

        async def fake_edge(text, **kwargs):
            captured.update(text=text, **kwargs)
            return b"mp3"

        with patch("admin_voice.synthesize", new=fake_edge):
            audio = preview_audio(self.config, {})
        self.assertEqual(audio, b"mp3")
        self.assertEqual(captured["voice"], "zh-CN-XiaoxiaoNeural")
        self.assertEqual(captured["rate"], "+5%")

    def test_preview_rejects_unknown_fields(self) -> None:
        with self.assertRaises(ValueError):
            preview_audio(self.config, {"voice": FISH_VOICE, "text": "hi"})


if __name__ == "__main__":
    unittest.main()
