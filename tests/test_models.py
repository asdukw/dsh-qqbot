from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from models import ModelRegistry, parse_key  # noqa: E402


class ModelRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "models.db"
        self.registry = ModelRegistry(self.db_path)
        # A second route for switch tests; the shipped defaults now only seed
        # the paid OpenCode Go model.
        self.registry.upsert("deepseek", "deepseek-flash", {
            "base_url": "https://api.deepseek.com",
            "api_key_env": "DEEPSEEK_API_KEY",
            "context_window": "1000000",
        })

    def tearDown(self) -> None:
        self.registry.close()
        self.tmp.cleanup()

    def reopen(self) -> None:
        self.registry.close()
        self.registry = ModelRegistry(self.db_path)

    def test_seeds_defaults_once_and_keeps_edits(self) -> None:
        keys = [record.key for record in self.registry.list()]
        self.assertIn("opencode-go/deepseek-v4.1-flash", keys)
        self.assertEqual(
            [key for key in keys if key.startswith("opencode-go/")],
            ["opencode-go/deepseek-v4.1-flash"],
        )
        self.registry.upsert("opencode-go", "deepseek-v4.1-flash", {"context_window": "123"})
        self.reopen()
        record = self.registry.get("opencode-go", "deepseek-v4.1-flash")
        assert record is not None
        self.assertEqual(record.context_window, 123)
        self.assertEqual(len(self.registry.list()), len(keys))

    def test_defaults(self) -> None:
        paid = self.registry.get("opencode-go", "deepseek-v4.1-flash")
        assert paid is not None
        self.assertEqual(paid.context_window, 1_000_000)
        self.assertEqual(paid.reasoning_effort, "low")
        self.assertEqual(paid.effective_effort, "low")
        self.assertEqual(paid.max_output_tokens, 2048)
        self.assertIn("image", paid.capabilities.get("input", []))

    def test_exactly_one_active_model(self) -> None:
        active = self.registry.active()
        assert active is not None
        self.assertEqual(active.key, "opencode-go/deepseek-v4.1-flash")
        self.registry.use("deepseek", "deepseek-flash")
        self.reopen()
        actives = [r.key for r in self.registry.list() if r.is_active]
        self.assertEqual(actives, ["deepseek/deepseek-flash"])

    def test_use_rejects_unknown_and_disabled(self) -> None:
        with self.assertRaises(ValueError):
            self.registry.use("opencode-go", "missing")
        self.registry.upsert("deepseek", "deepseek-flash", {"enabled": "false"})
        with self.assertRaises(ValueError):
            self.registry.use("deepseek", "deepseek-flash")

    def test_cannot_disable_active_model(self) -> None:
        with self.assertRaises(ValueError):
            self.registry.upsert("opencode-go", "deepseek-v4.1-flash", {"enabled": "false"})

    def test_upsert_creates_new_model(self) -> None:
        record = self.registry.upsert("deepseek", "deepseek-flash", {
            "base_url": "https://api.deepseek.com",
            "api_key_env": "DEEPSEEK_API_KEY",
            "context_window": "1000000",
            "capabilities": '{"reasoning_efforts": ["low", "high"]}',
            "reasoning_effort": "high",
        })
        self.assertEqual(record.display_name, "deepseek-flash")
        self.assertEqual(record.effective_effort, "high")
        self.assertFalse(record.is_active)

    def test_upsert_requires_core_fields_for_new_rows(self) -> None:
        with self.assertRaises(ValueError):
            self.registry.upsert("deepseek", "missing-model", {"context_window": "1000"})

    def test_effort_must_be_declared(self) -> None:
        with self.assertRaises(ValueError):
            self.registry.upsert("opencode-go", "deepseek-v4.1-flash", {"reasoning_effort": "max"})

    def test_empty_effort_means_model_default(self) -> None:
        record = self.registry.upsert("opencode-go", "deepseek-v4.1-flash", {"reasoning_effort": ""})
        self.assertEqual(record.effective_effort, "")

    def test_unknown_field_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.registry.upsert("opencode-go", "deepseek-v4.1-flash", {"temperature": "1"})

    def test_patch_groups_models_by_provider(self) -> None:
        target = Path(self.tmp.name) / "state" / "models.patch.json"
        self.assertEqual(self.registry.write_dsh_patch(target), target)
        patch = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(patch[0]["id"], "llm-pi-ai")
        opencode = patch[0]["config"]["providers"]["opencode-go"]
        self.assertEqual(opencode["apiKeyEnv"], "OPENCODE_API_KEY")
        # Built-in provider: no adapter override, so DSH keeps its wrapped
        # adapters (the OpenCode session header) and model catalog.
        self.assertNotIn("api", opencode)
        self.assertNotIn("headers", opencode)
        # Non-built-in provider: DSH needs an explicit adapter base URL.
        self.assertIn("api", patch[0]["config"]["providers"]["deepseek"])
        models = {m["id"]: m for m in opencode["models"]}
        self.assertEqual(models["deepseek-v4.1-flash"]["contextWindow"], 1_000_000)
        self.assertEqual(
            models["deepseek-v4.1-flash"]["reasoningEfforts"],
            {"low": "low", "medium": "medium", "high": "high"},
        )
        self.assertEqual(models["deepseek-v4.1-flash"]["input"], ["text", "image"])

    def test_active_signature_tracks_route_and_parameters(self) -> None:
        before = self.registry.active_signature()
        assert before is not None
        self.registry.upsert("opencode-go", "deepseek-v4.1-flash", {"max_output_tokens": "4096"})
        after = self.registry.active_signature()
        assert after is not None
        self.assertNotEqual(after, before)
        self.registry.use("deepseek", "deepseek-flash")
        switched = self.registry.active_signature()
        assert switched is not None
        self.assertIn("deepseek/deepseek-flash", switched)

    def test_disabled_rows_are_excluded(self) -> None:
        self.registry.upsert("deepseek", "deepseek-flash", {"enabled": "0"})
        target = Path(self.tmp.name) / "models.patch.json"
        self.registry.write_dsh_patch(target)
        patch = json.loads(target.read_text(encoding="utf-8"))
        deepseek = patch[0]["config"]["providers"].get("deepseek", {"models": []})
        ids = [m["id"] for m in deepseek["models"]]
        self.assertNotIn("deepseek-flash", ids)

    def test_inconsistent_provider_metadata_is_rejected(self) -> None:
        self.registry.upsert("opencode-go", "other", {
            "base_url": "https://elsewhere.example/v1",
            "context_window": "1000",
        })
        with self.assertRaises(ValueError):
            self.registry.write_dsh_patch(Path(self.tmp.name) / "bad.json")

    def test_migrates_first_release_schema(self) -> None:
        old = Path(self.tmp.name) / "old.db"
        conn = sqlite3.connect(old)
        conn.executescript(
            """
            CREATE TABLE models (
                provider TEXT NOT NULL, model_id TEXT NOT NULL, display_name TEXT NOT NULL,
                api TEXT NOT NULL DEFAULT 'openai-completions', base_url TEXT NOT NULL,
                api_key_env TEXT NOT NULL DEFAULT 'LLM_API_KEY', context_window INTEGER NOT NULL,
                max_output_tokens INTEGER NOT NULL DEFAULT 2048,
                capabilities TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 1,
                updated_at INTEGER NOT NULL, PRIMARY KEY (provider, model_id));
            INSERT INTO models (provider, model_id, display_name, base_url, context_window, updated_at)
            VALUES ('opencode', 'deepseek-v4.1-flash', 'Flash',
                    'https://opencode.ai/inference/openai/v1', 1000000, 0);
            """
        )
        conn.commit()
        conn.close()
        registry = ModelRegistry(old)
        try:
            record = registry.active()
            assert record is not None
            self.assertEqual(record.key, "opencode-go/deepseek-v4.1-flash")
            self.assertEqual(record.effective_effort, "low")
            self.assertIn("image", record.capabilities.get("input", []))
        finally:
            registry.close()

    def test_parse_key(self) -> None:
        self.assertEqual(parse_key("opencode-go/deepseek-v4.1-flash"), ("opencode-go", "deepseek-v4.1-flash"))
        with self.assertRaises(ValueError):
            parse_key("no-slash")


if __name__ == "__main__":
    unittest.main()
