from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from commands import CommandRouter  # noqa: E402
from models import ModelRegistry  # noqa: E402


class FakeBot:
    def __init__(self, db: Path):
        self.config = SimpleNamespace(models=SimpleNamespace(database=str(db)))
        self.sent: list[str] = []
        self.reloads = 0

    def is_admin(self, user_id: int) -> bool:
        return user_id == 1

    async def send_text(self, event: SimpleNamespace, text: str) -> None:
        self.sent.append(text)

    async def reload_model(self) -> None:
        self.reloads += 1


class CommandRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "models.db"
        ModelRegistry(self.db).close()
        self.bot = FakeBot(self.db)
        self.router = CommandRouter(self.bot)
        self.event = SimpleNamespace(user_id=1)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_command(self, text: str, user_id: int = 1) -> None:
        event = SimpleNamespace(user_id=user_id)
        asyncio.run(self.router.handle(event, text))

    def active_key(self) -> str:
        registry = ModelRegistry(self.db)
        try:
            record = registry.active()
            assert record is not None
            return record.key
        finally:
            registry.close()

    def test_non_admin_is_ignored(self) -> None:
        self.run_command("/model 2", user_id=2)
        self.assertEqual(self.bot.sent, [])
        self.assertEqual(self.bot.reloads, 0)

    def test_help(self) -> None:
        self.run_command("/help")
        self.assertIn("可用命令", self.bot.sent[0])

    def test_lists_models(self) -> None:
        self.run_command("/model")
        self.assertIn("opencode-go/deepseek-v4.1-flash", self.bot.sent[0])

    def test_switches_by_index(self) -> None:
        self.run_command("/model 3")
        self.assertEqual(self.bot.reloads, 1)
        self.assertEqual(self.active_key(), "opencode-go/deepseek-v4.1-flash-free")

    def test_switches_by_model_id(self) -> None:
        self.run_command("/model deepseek-v4.1-flash-free")
        self.assertEqual(self.active_key(), "opencode-go/deepseek-v4.1-flash-free")

    def test_unknown_model(self) -> None:
        self.run_command("/model nope")
        self.assertIn("找不到模型", self.bot.sent[0])
        self.assertEqual(self.bot.reloads, 0)

    def test_unknown_command(self) -> None:
        self.run_command("/wat")
        self.assertIn("未知命令", self.bot.sent[0])


if __name__ == "__main__":
    unittest.main()
