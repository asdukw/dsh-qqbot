from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from commands import (  # noqa: E402
    CommandRouter,
    strip_leading_mention,
    strip_message_markers,
)
from models import ModelRegistry  # noqa: E402
from personas import PersonaStore  # noqa: E402
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
        self.personas = PersonaStore(cards_db, self.cards)
        self.stickers = None
        self.relations = None
        self.sent: list[str] = []
        self.reloads = 0
        self.card_reloads = 0

    async def switch_card(self, event, record) -> dict:
        scope = getattr(event, 'message_type', None)
        group_id = getattr(event, 'group_id', None)
        if scope == 'group' and group_id:
            conversation = f'group:{group_id}'
        elif scope == 'private':
            conversation = f'private:{event.user_id}'
        else:
            raise ValueError('unknown conversation')
        job = self.personas.enqueue(conversation, record, actor=str(event.user_id))
        if job['phase'] == 'queued':
            self.personas.bind(job['id'], 0)
            self.personas.update(job['id'], phase='complete', announcement='notice')
        return self.personas.job(job['id'])

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
        self.bot.personas.close()
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
        self.bot.personas.close()
        self.bot.cards.close()
        self.tmp.cleanup()

    def run_command(self, text: str, user_id: int = 1, images: tuple[str, ...] = (),
                    message_type: str | None = "private", group_id: int | None = None) -> None:
        event = SimpleNamespace(user_id=user_id, images=images, group_id=group_id)
        if message_type is not None:
            event.message_type = message_type
        asyncio.run(self.router.handle(event, text))

    def active_id(self) -> int | None:
        active = self.bot.cards.active()
        return active.id if active else None

    def test_non_admin_management_commands_are_ignored(self) -> None:
        for text in ("/model 2", "/reset", "/help", "/task list", "/sub list"):
            self.run_command(text, user_id=2)
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

    def test_daily_passes_the_requested_date(self) -> None:
        calls = []

        async def send_daily_report(event, day):
            calls.append(day)
            return True

        self.bot.send_daily_report = send_daily_report
        self.run_command("/daily 2026-10-24")
        self.assertEqual(calls, [date(2026, 10, 24)])
        self.assertEqual(self.bot.sent, [])

    def test_daily_without_data_says_so(self) -> None:
        async def send_daily_report(event, day):
            return False

        self.bot.send_daily_report = send_daily_report
        self.run_command("/daily")
        self.assertIn("没有可汇总", self.bot.sent[0])

    # ---------------------------------------------------------------- cards

    def assert_public_card_response(self, text: str) -> None:
        for command in ("/card install", "/card delete", "/card alias"):
            self.assertNotIn(command, text)

    def test_private_player_card_browsing_aliases(self) -> None:
        for prefix in ("/card", "/cards", "/CaRdS"):
            for action in ("", "list", "ls", "LIST", "search 图书", "find 图书",
                           f"show {self.a.id}", f"info {self.a.id}", "help", "?"):
                with self.subTest(prefix=prefix, action=action):
                    self.bot.sent.clear()
                    self.run_command(f"{prefix} {action}", user_id=2)
                    self.assertEqual(len(self.bot.sent), 1)
                    self.assert_public_card_response(self.bot.sent[0])
                    if action not in ("help", "?"):
                        self.assertIn("阿杜", self.bot.sent[0])

    def test_private_player_switches_by_id_name_and_alias_only_self(self) -> None:
        self.bot.cards.add_alias(self.b.id, "登山伙伴")
        before = {key: self.bot.personas.effective(key).token
                  for key in ("private:1", "private:3", "group:42")}
        for prefix in ("/card", "/cards"):
            for action in ("use", "switch", "USE"):
                for target in (str(self.b.id), "小林", "登山伙伴"):
                    with self.subTest(prefix=prefix, action=action, target=target):
                        self.run_command(f"/card use {self.a.id}", user_id=2)
                        self.run_command(f"{prefix} {action} {target}", user_id=2)
                        self.assertEqual(self.bot.personas.effective("private:2").record.id, self.b.id)
                        self.assertEqual(self.active_id(), self.a.id)
                        self.assertEqual({key: self.bot.personas.effective(key).token for key in before}, before)
        self.run_command("/card", user_id=2)
        self.assertIn("本会话: 小林", self.bot.sent[-1])
        self.assertEqual(self.bot.card_reloads, 0)

    def test_non_admin_card_commands_require_explicit_private_event(self) -> None:
        with patch.object(self.bot, "switch_card", new_callable=AsyncMock) as switch:
            for scope in ("group", None, "unknown", ""):
                for command in ("/card", "/cards search 图书", "/card show 1", "/card help", "/card use 2"):
                    with self.subTest(scope=scope, command=command):
                        self.run_command(command, user_id=2, message_type=scope, group_id=42)
            switch.assert_not_awaited()
        self.assertEqual(self.bot.sent, [])
        self.assertEqual(self.bot.personas.queued(), [])

    def test_private_player_cannot_access_management_actions(self) -> None:
        self.bot.cards.add_alias(self.a.id, "保留别名")
        before = self.bot.cards.list()
        commands = [f"{action} {source}" for action in ("install", "add", "INSTALL")
                    for source in ("", "https://example.com/card.png", "/server/private.json")]
        commands += [f"{action} {self.b.id}" for action in ("delete", "rm", "remove")]
        commands += [f"{action} {operation} 保留别名" for action in ("alias", "aliases", "nick")
                     for operation in ("", "list", "ls", "add", "+", "remove", "rm", "del", "delete", "-")]
        with (patch("commands.fetch_card_bytes", new_callable=AsyncMock) as fetch,
              patch("commands.Path.read_bytes") as read,
              patch.object(self.router, "_install_card", new_callable=AsyncMock) as install,
              patch.object(self.router, "_delete_card", new_callable=AsyncMock) as delete,
              patch.object(self.router, "_card_alias", new_callable=AsyncMock) as alias):
            for prefix in ("/card", "/cards"):
                for command in commands:
                    with self.subTest(prefix=prefix, command=command):
                        self.run_command(f"{prefix} {command}", user_id=2, images=("https://example.com/image.png",))
                        self.assertIn("仅管理员", self.bot.sent[-1])
                        self.assert_public_card_response(self.bot.sent[-1])
            fetch.assert_not_awaited()
            read.assert_not_called()
            install.assert_not_awaited()
            delete.assert_not_awaited()
            alias.assert_not_awaited()
        self.assertEqual(self.bot.cards.list(), before)
        self.assertEqual(self.bot.personas.queued(), [])

    def test_private_player_help_missing_targets_and_same_card(self) -> None:
        for command in ("/card help", "/card ?", "/card wat", "/card use 不存在", "/card show 不存在",
                        "/card use", "/card show", "/card search", "/card search 不存在"):
            with self.subTest(command=command):
                self.run_command(command, user_id=2)
                self.assert_public_card_response(self.bot.sent[-1])
        self.run_command(f"/card use {self.a.id}", user_id=2)
        self.assertIn("当前会话已经是", self.bot.sent[-1])

    def test_private_player_ambiguous_card_uses_safe_candidates(self) -> None:
        self.bot.cards.install_payload(card_payload("小林同伴"))
        self.run_command("/card use 小", user_id=2)
        self.assertIn("多个匹配", self.bot.sent[-1])
        self.assert_public_card_response(self.bot.sent[-1])
        self.assertEqual(self.bot.personas.effective("private:2").record.id, self.a.id)

    def test_empty_card_library_has_safe_private_player_hint(self) -> None:
        with (patch.object(self.bot.cards, "list", return_value=[]),
              patch.object(self.router, "_current_card", side_effect=AssertionError("no effective card"))):
            self.run_command("/card", user_id=2)
            self.assertIn("暂无可用角色", self.bot.sent[-1])
            self.assert_public_card_response(self.bot.sent[-1])
            self.run_command("/card", user_id=1)
            self.assertIn("/card install", self.bot.sent[-1])

    def test_private_player_card_details_hide_operational_metadata(self) -> None:
        record, _ = self.bot.cards.install_payload(
            card_payload("来源测试", description="公开简介", creator="作者", character_version="v1"),
            source="/srv/secret-cards/private.json", source_kind="internal-file", installed_by="99887766",
        )
        self.run_command(f"/card show {record.id}", user_id=2)
        public = self.bot.sent[-1]
        for marker in (record.source, record.source_kind, record.installed_by, "来源:", "安装:"):
            self.assertNotIn(marker, public)
        for marker in ("来源测试", "公开简介", "作者", "v1"):
            self.assertIn(marker, public)
        self.run_command(f"/card show {record.id}")
        for marker in (record.source, record.source_kind, record.installed_by):
            self.assertIn(marker, self.bot.sent[-1])

    def test_private_player_switch_errors_hide_internal_details(self) -> None:
        with patch.object(self.bot, "switch_card", new_callable=AsyncMock) as switch:
            switch.side_effect = sqlite3.OperationalError("/srv/private/database.db")
            self.run_command(f"/card use {self.b.id}", user_id=2)
            self.assertNotIn("/srv/private", self.bot.sent[-1])
            switch.side_effect = None
            switch.return_value = {"id": "failed-job", "phase": "failed", "error": "/srv/private/model-key"}
            self.run_command(f"/card use {self.b.id}", user_id=2)
            self.assertNotIn("/srv/private", self.bot.sent[-1])
            self.assertIn("failed-job", self.bot.sent[-1])

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

    def test_card_use_by_id_is_scoped_without_reload(self) -> None:
        self.run_command(f"/card use {self.b.id}")
        self.assertEqual(self.active_id(), self.a.id)
        self.assertEqual(self.bot.personas.effective('private:1').record.id, self.b.id)
        self.assertEqual(self.bot.personas.effective('group:42').record.id, self.a.id)
        self.assertEqual(self.bot.card_reloads, 0)

    def test_card_use_by_name(self) -> None:
        self.run_command("/card use 小林")
        self.assertEqual(self.bot.personas.effective('private:1').record.id, self.b.id)
        self.assertEqual(self.active_id(), self.a.id)

    def test_card_use_already_active(self) -> None:
        self.run_command(f"/card use {self.a.id}")
        self.assertIn("当前会话已经是", self.bot.sent[0])
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
