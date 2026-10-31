"""容器首次启动配置的回归测试。"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "init_napcat", Path(__file__).resolve().parents[1] / "deploy/init-napcat.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class NapcatInitTests(unittest.TestCase):
    def test_initialization_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            MODULE.initialize(root, "dummy-token")
            path = root / "onebot11.json"
            original = path.read_bytes()
            MODULE.initialize(root, "dummy-token")
            self.assertEqual(path.read_bytes(), original)
            data = json.loads(original)
            self.assertEqual(data["network"]["websocketServers"][0]["token"], "dummy-token")

    def test_mismatch_preserves_existing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            MODULE.initialize(root, "old-token")
            path = root / "onebot11.json"
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                MODULE.initialize(root, "new-token")
            self.assertEqual(path.read_bytes(), original)

    def test_empty_token_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                MODULE.initialize(Path(directory), "")


BOOTSTRAP_SPEC = importlib.util.spec_from_file_location(
    "container_bootstrap",
    Path(__file__).resolve().parents[1] / "deploy/container_bootstrap.py",
)


class ContainerBootstrapTests(unittest.TestCase):
    def setUp(self):
        assert BOOTSTRAP_SPEC is not None and BOOTSTRAP_SPEC.loader is not None
        self.module = importlib.util.module_from_spec(BOOTSTRAP_SPEC)
        BOOTSTRAP_SPEC.loader.exec_module(self.module)

    def test_configuration_is_container_specific_and_preserved(self):
        source = Path(__file__).resolve().parents[1] / "bot"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.module.initialize_config(root, source)
            config = root / "config.toml"
            original = config.read_text(encoding="utf-8")
            self.assertIn('url = "ws://napcat:3001"', original)
            self.assertIn('host = "0.0.0.0"', original)
            config.write_text("# custom configuration\n", encoding="utf-8")
            self.module.initialize_config(root, source)
            self.assertEqual(config.read_text(encoding="utf-8"), "# custom configuration\n")
            self.assertEqual((root / "character.json").read_bytes(),
                             (source / "character.example.json").read_bytes())

    def test_model_readiness_requires_active_model_and_key(self):
        from models import ModelRegistry

        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "models.db")
            self.assertFalse(self.module.model_ready(database))
            registry = ModelRegistry(database)
            try:
                registry.upsert("smoke", "fake", {"base_url": "http://fake:8765/v1", "context_window": "32000"})
                self.assertFalse(self.module.model_ready(database))
                registry.use("smoke", "fake")
                self.assertFalse(self.module.model_ready(database))
                registry.upsert_provider("smoke", {"api_key": "dummy-key"})
                self.assertTrue(self.module.model_ready(database))
            finally:
                registry.close()
