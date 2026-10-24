from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from commands import (  # noqa: E402
    CommandRouter,
    strip_leading_mention,
    strip_message_markers,
)
from models import ModelRegistry  # noqa: E402
from relations import RelationMarkers, RelationStore  # noqa: E402

from cards import CardRegistry, normalise  # noqa: E402


def card_payload(name: str, **data) -> dict:
    body = {"name": name, "character_book": {"entries": []}}
    body.update(data)
    return normalise({"spec": "chara_card_v2", "spec_version": "2.0", "data": body})


class FakeBot:
    def __init__(self, db: Path, cards_db: Path):
        self.config = SimpleNamespace(models=SimpleNamespace(database=str(db)))
        self.cards = CardRegistry(cards_db)
        self.stickers = None
        self.relations = None
        self.sent: list[str] = []
        self.reloads = 0
        self.card_reloads = 0

    def is_admin(self, user_id: int) -> bool:
        return user_id == 1

    async def send_text(self, event: SimpleNamespace, text: str) -> None:
        self.sent.append(text)

    async def reload_model(self) -> None:
        self.reloads += 1

    async def reload_card(self) -> bool:
        self.card_reloads += 1
        return True


class StripMentionTests(unittest.TestCase):
    def test_typed_at_before_a_command_is_removed(self) -> None:
        self.assertEqual(
            strip_leading_mention("@嘻嘻 /sub add 456664753"), "/sub add 456664753"
        )
        self.assertEqual(strip_leading_mention("@bot   /model"), "/model")

    def test_chatter_is_untouched(self) -> None:
        self.assertEqual(strip_leading_mention("@嘻嘻 你好呀"), "@嘻嘻 你好呀")
        self.assertEqual(strip_leading_mention("你好 /sub"), "你好 /sub")


class RelationCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = root / "models.db"
        self.cards_db = root / "cards.db"
        self.profile_db = root / "profile.db"
        ModelRegistry(self.db).close()
        self.bot = FakeBot(self.db, self.cards_db)
        self.store = RelationStore(self.profile_db)
        self.bot.relations = self.store
        self.router = CommandRouter(self.bot)

    def tearDown(self) -> None:
        self.store.close()
        self.bot.cards.close()
        self.tmp.cleanup()

    def test_relation_is_public_for_self(self) -> None:
        self.store.touch(2, "小明")
        self.store.apply(
            2,
            RelationMarkers(delta=2, impression="有礼貌", relationship="损友"),
            now=1000,
        )
        event = SimpleNamespace(user_id=2, message_type="private", sender_name="小明")
        asyncio.run(self.router.handle(event, "/relation"))
        text = self.bot.sent[-1]
        self.assertIn("好感度 22/100", text)
        self.assertIn("有礼貌", text)
        self.assertIn("损友", text)

    def test_self_in_group_does_not_show_private_or_other_group_facts(self) -> None:
        self.store.touch(2, "小明")
        self.store.apply(
            2,
            RelationMarkers(impression="公开印象", relationship="损友"),
            now=1000,
            scope="group",
        )
        self.store.apply(2, RelationMarkers(impression="私下印象"), now=1100)
        conn = sqlite3.connect(self.profile_db)
        conn.execute(
            "CREATE TABLE user_facts (user_id TEXT, group_id TEXT NOT NULL DEFAULT '', "
            "key TEXT, value TEXT, visibility TEXT, updated_at INTEGER, "
            "PRIMARY KEY (user_id, group_id, key))"
        )
        conn.executemany(
            "INSERT INTO user_facts VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("2", "42", "city", "长春", "public", 3),
                ("2", "43", "city", "北京", "public", 4),
                ("2", "", "secret", "怕黑", "private", 1),
            ],
        )
        conn.commit()
        conn.close()
        event = SimpleNamespace(
            user_id=2, message_type="group", group_id=42, sender_name="小明"
        )
        asyncio.run(self.router.handle(event, "/relation"))
        text = self.bot.sent[-1]
        self.assertIn("长春", text)
        self.assertNotIn("北京", text)
        self.assertNotIn("怕黑", text)
        self.assertIn("公开印象", text)
        self.assertNotIn("私下印象", text)
        self.assertNotIn("损友", text)

    def test_others_only_see_public_fields(self) -> None:
        self.store.touch(2, "小明")
        self.store.apply(
            2, RelationMarkers(delta=2, impression="私下的印象"), now=1000
        )
        conn = sqlite3.connect(self.profile_db)
        conn.execute(
            "CREATE TABLE user_facts (user_id TEXT, group_id TEXT NOT NULL DEFAULT '', "
            "key TEXT, value TEXT, visibility TEXT, updated_at INTEGER, "
            "PRIMARY KEY (user_id, group_id, key))"
        )
        conn.execute(
            "INSERT INTO user_facts VALUES ('2', '42', 'city', '杭州', 'public', 1)"
        )
        conn.commit()
        conn.close()
        event = SimpleNamespace(
            user_id=3, message_type="group", group_id=42, sender_name="小红"
        )
        asyncio.run(self.router.handle(event, "/relation 2"))
        text = self.bot.sent[-1]
        self.assertIn("好感度 22/100", text)
        self.assertIn("杭州", text)
        self.assertNotIn("私下的印象", text)

    def test_admin_can_set_and_reset(self) -> None:
        event = SimpleNamespace(user_id=1, message_type="private", sender_name="admin")
        asyncio.run(self.router.handle(event, "/relation set 2 42"))
        self.assertIn("42/100", self.bot.sent[-1])
        record = self.store.get(2)
        assert record is not None
        self.assertEqual(record.affinity, 42)
        asyncio.run(self.router.handle(event, "/relation reset 2"))
        self.assertIsNone(self.store.get(2))

    def test_non_admin_cannot_set(self) -> None:
        event = SimpleNamespace(user_id=2, message_type="private", sender_name="小明")
        asyncio.run(self.router.handle(event, "/relation set 3 42"))
        self.assertIn("只有管理员", self.bot.sent[-1])


class CommandRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = root / "models.db"
        self.cards_db = root / "cards.db"
        registry = ModelRegistry(self.db)
        registry.upsert("deepseek", "deepseek-flash", {
            "base_url": "https://api.deepseek.com",
            "api_key_env": "DEEPSEEK_API_KEY",
            "context_window": "1000000",
        })
        registry.close()
        self.bot = FakeBot(self.db, self.cards_db)
        self.a, _ = self.bot.cards.install_payload(
            card_payload("阿杜", description="温柔的图书管理员", tags=["日常"]),
            installed_by="test",
        )
        self.b, _ = self.bot.cards.install_payload(
            card_payload("小林", personality="喜欢爬山", tags=["户外"]),
            installed_by="test",
        )
        self.bot.cards.use(self.a.id)
        self.router = CommandRouter(self.bot)
        self.event = SimpleNamespace(user_id=1)

    def tearDown(self) -> None:
        if self.bot.stickers is not None:
            self.bot.stickers.close()
        self.bot.cards.close()
        self.tmp.cleanup()

    def run_command(self, text: str, user_id: int = 1, images: tuple[str, ...] = ()) -> None:
        event = SimpleNamespace(user_id=1 if user_id == 1 else user_id, images=images)
        asyncio.run(self.router.handle(event, text))

    def active_id(self) -> int | None:
        active = self.bot.cards.active()
        return active.id if active else None

    def test_non_admin_is_ignored(self) -> None:
        self.run_command("/model 2", user_id=2)
        self.run_command("/card use 2", user_id=2)
        self.assertEqual(self.bot.sent, [])
        self.assertEqual(self.bot.reloads, 0)
        self.assertEqual(self.bot.card_reloads, 0)

    def test_help(self) -> None:
        self.run_command("/help")
        self.assertIn("可用命令", self.bot.sent[0])
        self.assertIn("/card", self.bot.sent[0])

    def test_lists_models(self) -> None:
        self.run_command("/model")
        self.assertIn("opencode-go/deepseek-v4.1-flash", self.bot.sent[0])

    def test_switches_by_index(self) -> None:
        self.run_command("/model 2")
        self.assertEqual(self.bot.reloads, 1)

    def test_switches_by_model_id(self) -> None:
        self.run_command("/model deepseek/deepseek-flash")
        self.assertIn("已切换模型", self.bot.sent[0])

    def test_unknown_model(self) -> None:
        self.run_command("/model nope")
        self.assertIn("找不到模型", self.bot.sent[0])
        self.assertEqual(self.bot.reloads, 0)

    def test_unknown_command(self) -> None:
        self.run_command("/wat")
        self.assertIn("未知命令", self.bot.sent[0])

    # ---------------------------------------------------------------- cards

    def test_card_alias_add_list_remove(self) -> None:
        self.run_command("/card alias add 小杜")
        self.assertIn("添加别名", self.bot.sent[-1])
        self.assertEqual(self.bot.cards.active().aliases, ("小杜",))
        self.run_command("/card alias")
        self.assertIn("别名: 小杜", self.bot.sent[-1])
        self.run_command("/card alias remove 小杜")
        self.assertIn("已删除别名", self.bot.sent[-1])
        self.assertEqual(self.bot.cards.active().aliases, ())

    def test_card_alias_rejects_short_names(self) -> None:
        self.run_command("/card alias add 杜")
        self.assertIn("添加别名失败", self.bot.sent[-1])
        self.assertEqual(self.bot.cards.active().aliases, ())

    def test_card_show_resolves_by_alias(self) -> None:
        self.bot.cards.add_alias(self.a.id, "小杜")
        self.run_command("/card show 小杜")
        self.assertIn("阿杜", self.bot.sent[-1])

    def test_sticker_add_and_list(self) -> None:
        from stickers import StickerStore

        root = Path(self.tmp.name)
        self.bot.stickers = StickerStore(root / "stickers.db", root / "sticker-files")
        self.bot.config.stickers = SimpleNamespace(
            max_bytes=8 * 1024 * 1024, max_stored=500
        )
        png = bytes.fromhex("89504e470d0a1a0a") + b"fake"
        uri = "data:image/png;base64," + base64.b64encode(png).decode()
        self.run_command("/sticker add", images=(uri,))
        self.assertIn("已收藏", self.bot.sent[-1])
        self.run_command("/sticker list")
        self.assertIn("共 1 张", self.bot.sent[-1])
        self.run_command("/sticker stats")
        self.assertIn("表情库: 1 张", self.bot.sent[-1])

    def test_sticker_without_library(self) -> None:
        self.run_command("/sticker")
        self.assertIn("表情库没有启用", self.bot.sent[-1])

    def test_card_list_marks_active(self) -> None:
        self.run_command("/card")
        text = self.bot.sent[0]
        self.assertIn("阿杜", text)
        self.assertIn("小林", text)
        self.assertIn(f"* #{self.a.id} 阿杜", text)

    def test_card_use_by_id_reloads(self) -> None:
        self.run_command(f"/card use {self.b.id}")
        self.assertEqual(self.active_id(), self.b.id)
        self.assertEqual(self.bot.card_reloads, 1)
        self.assertIn("已切换角色卡", self.bot.sent[0])

    def test_card_use_by_name(self) -> None:
        self.run_command("/card use 小林")
        self.assertEqual(self.active_id(), self.b.id)

    def test_card_use_already_active(self) -> None:
        self.run_command(f"/card use {self.a.id}")
        self.assertIn("当前已经是", self.bot.sent[0])
        self.assertEqual(self.bot.card_reloads, 0)

    def test_card_use_unknown_lists_candidates(self) -> None:
        self.run_command("/card use 不存在")
        self.assertIn("找不到角色卡", self.bot.sent[0])

    def test_card_search(self) -> None:
        self.run_command("/card search 图书")
        self.assertIn("阿杜", self.bot.sent[0])
        self.assertNotIn("小林", self.bot.sent[0])

    def test_card_show(self) -> None:
        self.run_command(f"/card show {self.a.id}")
        text = self.bot.sent[0]
        self.assertIn("阿杜", text)
        self.assertIn("日常", text)
        self.assertIn("温柔的图书管理员", text)

    def test_card_install_from_path(self) -> None:
        path = Path(self.tmp.name) / "new.json"
        path.write_text(json.dumps(card_payload("新卡", description="路径安装")), encoding="utf-8")
        self.run_command(f"/card install {path}")
        self.assertIn("已安装", self.bot.sent[0])
        self.assertIsNotNone(self.bot.cards.resolve("新卡"))

    def test_card_install_dedupes(self) -> None:
        path = Path(self.tmp.name) / "dup.json"
        path.write_text(json.dumps(card_payload("阿杜", description="温柔的图书管理员", tags=["日常"])), encoding="utf-8")
        self.run_command(f"/card install {path}")
        self.assertIn("已经在库里", self.bot.sent[0])
        self.assertEqual(self.bot.cards.count(), 2)

    def test_card_install_from_attached_image(self) -> None:
        card = {"spec": "chara_card_v3", "spec_version": "3.0", "data": {"name": "图片卡"}}
        uri = "data:image/png;base64," + base64.b64encode(json.dumps(card).encode()).decode()
        self.run_command("/card install", images=(uri,))
        self.assertIn("已安装", self.bot.sent[0])
        self.assertIsNotNone(self.bot.cards.resolve("图片卡"))

    def test_strip_message_markers(self) -> None:
        self.assertEqual(strip_message_markers("/card install[图片]"), "/card install")
        self.assertEqual(strip_message_markers("[图片]/card install"), "/card install")
        self.assertEqual(strip_message_markers("/card[图片]"), "/card")
        self.assertEqual(strip_message_markers("普通消息 [表情]"), "普通消息")

    def test_card_install_with_parser_placeholder(self) -> None:
        card = {"spec": "chara_card_v3", "spec_version": "3.0", "data": {"name": "带图卡"}}
        uri = "data:image/png;base64," + base64.b64encode(json.dumps(card).encode()).decode()
        self.run_command("/card install[图片]", images=(uri,))
        self.assertIn("已安装", self.bot.sent[0])
        record = self.bot.cards.resolve("带图卡")
        assert record is not None
        self.assertEqual(record.source, "data:image/png;base64")
        self.assertLess(len(record.source), 100)

    def test_card_install_requires_source(self) -> None:
        self.run_command("/card install")
        self.assertIn("用法", self.bot.sent[0])

    def test_card_install_bad_file(self) -> None:
        path = Path(self.tmp.name) / "bad.json"
        path.write_text("not a card", encoding="utf-8")
        self.run_command(f"/card install {path}")
        self.assertIn("解析角色卡失败", self.bot.sent[0])

    def test_card_delete(self) -> None:
        self.run_command(f"/card delete {self.b.id}")
        self.assertIn("已删除", self.bot.sent[0])
        self.assertIsNone(self.bot.cards.resolve("小林"))

    def test_card_delete_active_is_rejected(self) -> None:
        self.run_command(f"/card delete {self.a.id}")
        self.assertIn("cannot delete the active card", self.bot.sent[0])
        self.assertEqual(self.bot.cards.count(), 2)

    def test_card_unknown_subcommand(self) -> None:
        self.run_command("/card wat")
        self.assertIn("未知角色卡命令", self.bot.sent[0])


if __name__ == "__main__":
    unittest.main()
