from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from main import AppConfig, Bot  # noqa: E402
from voice_catalog import catalog, provider_defaults, validate_voice_id  # noqa: E402
from voice_settings import (  # noqa: E402
    ProbabilityStore,
    VoiceSelection,
    VoiceSettingsError,
    VoiceStore,
    state_path,
)


class CatalogTests(unittest.TestCase):
    def test_catalog_contains_only_the_two_featured_voices(self) -> None:
        rows = catalog()
        self.assertEqual([row["name"] for row in rows], ["菲比", "流萤"])
        self.assertTrue(all(row["provider"] == "fish" for row in rows))
        self.assertTrue(all(len(row["id"]) == 32 for row in rows))

    def test_every_provider_has_connection_defaults(self) -> None:
        for provider in ("edge", "fish", "stepfun"):
            defaults = provider_defaults(provider)
            self.assertEqual(set(defaults), {"model", "api_key_env", "base_url"})
        self.assertEqual(provider_defaults("fish")["base_url"], "https://api.fish.audio")

    def test_voice_id_validation(self) -> None:
        self.assertEqual(validate_voice_id(" zh-CN-XiaoxiaoNeural "), "zh-CN-XiaoxiaoNeural")
        for bad in ("", "a b", "bad/slash", "x" * 200, None):
            with self.assertRaises(ValueError):
                validate_voice_id(bad)


class VoiceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "voice-state.json"
        self.store = VoiceStore(self.path)
        self.selection = VoiceSelection(
            provider="fish",
            voice="b4f70fdef5f943c2bf43db00e80ad680",
            name="高松灯（企鹅）",
            model="s2.1-pro-free",
            api_key_env="FISH_API_KEY",
            base_url="https://api.fish.audio",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_file_means_no_override(self) -> None:
        self.assertIsNone(self.store.active())

    def test_save_and_clear_round_trip(self) -> None:
        saved = self.store.save(self.selection)
        self.assertGreater(saved.updated_at, 0)
        active = self.store.active()
        self.assertEqual(active.provider, "fish")
        self.assertEqual(active.voice, self.selection.voice)
        self.assertEqual(active.name, "高松灯（企鹅）")
        self.assertEqual(active.model, "s2.1-pro-free")
        self.assertEqual(active.base_url, "https://api.fish.audio")
        self.store.clear()
        self.assertIsNone(self.store.active())
        self.store.clear()  # 幂等

    def test_invalid_file_is_rejected(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(VoiceSettingsError):
            self.store.active()

    def test_save_rejects_invalid_provider(self) -> None:
        bad = VoiceSelection(
            provider="nope",
            voice="x",
            name="",
            model="m",
            api_key_env="",
            base_url="https://example.test",
        )
        with self.assertRaises(ValueError):
            self.store.save(bad)
        self.assertFalse(self.path.exists())

    def test_edge_selection_drops_unused_connection_fields(self) -> None:
        saved = self.store.save(
            VoiceSelection(
                provider="edge",
                voice="zh-CN-YunxiNeural",
                name="云希",
                model="ignored",
                api_key_env="IGNORED_KEY",
                base_url="https://ignored.test",
            )
        )
        self.assertEqual(saved.model, "")
        self.assertEqual(saved.api_key_env, "")
        self.assertEqual(saved.base_url, "")

    def test_state_path_prefers_config(self) -> None:
        self.assertEqual(state_path({"state_file": "/tmp/x.json"}), "/tmp/x.json")
        self.assertEqual(state_path({}), "/var/lib/qqbot/voice-state.json")


class BotVoiceSectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.bot = object.__new__(Bot)
        self.bot.config = AppConfig()
        self.bot.config.voice.state_file = str(Path(self.tmp.name) / "voice.json")
        self.bot.config.voice.probability_file = str(
            Path(self.tmp.name) / "voice-probability.json"
        )
        self.assertEqual(self.bot.config.voice.instruction, "")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_config_is_used_without_an_override(self) -> None:
        section = self.bot._voice_section()
        self.assertEqual(section.provider, self.bot.config.voice.provider)
        self.assertEqual(section.voice, self.bot.config.voice.voice)

    def test_override_replaces_voice_and_connection_but_keeps_other_settings(self) -> None:
        self.bot.config.voice.instruction = "傲娇一点"
        self.bot.config.voice.probability = 0.9
        VoiceStore(self.bot.config.voice.state_file).save(
            VoiceSelection(
                provider="fish",
                voice="b4f70fdef5f943c2bf43db00e80ad680",
                name="高松灯（企鹅）",
                model="s2.1-pro-free",
                api_key_env="FISH_API_KEY",
                base_url="https://api.fish.audio",
            )
        )
        section = self.bot._voice_section()
        self.assertEqual(section.provider, "fish")
        self.assertEqual(section.voice, "b4f70fdef5f943c2bf43db00e80ad680")
        self.assertEqual(section.base_url, "https://api.fish.audio")
        self.assertEqual(section.instruction, "傲娇一点")
        self.assertEqual(section.probability, 0.9)

    def test_invalid_override_falls_back_to_config(self) -> None:
        Path(self.bot.config.voice.state_file).write_text("{oops", encoding="utf-8")
        section = self.bot._voice_section()
        self.assertEqual(section.voice, self.bot.config.voice.voice)


class ProbabilityStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "voice-probability.json"
        self.store = ProbabilityStore(self.path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_file_means_no_override(self) -> None:
        self.assertIsNone(self.store.get())

    def test_save_and_clear_round_trip(self) -> None:
        saved = self.store.save(0.25)
        self.assertAlmostEqual(saved.probability, 0.25)
        record = self.store.get()
        self.assertIsNotNone(record)
        assert record is not None
        self.assertAlmostEqual(record.probability, 0.25)
        self.store.clear()
        self.assertIsNone(self.store.get())

    def test_rejects_invalid_probabilities(self) -> None:
        for value in (-0.1, 1.1, True, "0.5", None):
            with self.assertRaises(ValueError):
                self.store.save(value)  # type: ignore[arg-type]

    def test_invalid_state_is_reported(self) -> None:
        self.path.write_text("{oops", encoding="utf-8")
        with self.assertRaises(VoiceSettingsError):
            self.store.get()
        self.path.write_text(
            json.dumps({"schema": 1, "updated_at": 1, "probability": 2}),
            encoding="utf-8",
        )
        with self.assertRaises(VoiceSettingsError):
            self.store.get()


if __name__ == "__main__":
    unittest.main()
