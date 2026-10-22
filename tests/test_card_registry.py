from __future__ import annotations

import base64
import json
import os
import struct
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from cards import CardError, CardRegistry, normalise  # noqa: E402


def payload(name: str, **data) -> dict:
    body = {"name": name, "character_book": {"entries": []}}
    body.update(data)
    return normalise({"spec": "chara_card_v2", "spec_version": "2.0", "data": body})


def chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def card_png(name: str) -> bytes:
    card = {"spec": "chara_card_v3", "spec_version": "3.0", "data": {"name": name}}
    encoded = base64.b64encode(json.dumps(card, ensure_ascii=False).encode("utf-8"))
    out = b"\x89PNG\r\n\x1a\n"
    out += chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    out += chunk(b"tEXt", b"ccv3\x00" + encoded)
    return out + chunk(b"IEND", b"")


class CardRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "cards.db"
        self.registry = CardRegistry(self.db_path)

    def tearDown(self) -> None:
        self.registry.close()
        self.tmp.cleanup()

    def reopen(self) -> None:
        self.registry.close()
        self.registry = CardRegistry(self.db_path)

    def install(self, name: str, **data):
        return self.registry.install_payload(payload(name, **data), installed_by="test")

    def test_install_list_and_active(self) -> None:
        first, created = self.install("阿杜", description="图书管理员")
        self.assertTrue(created)
        self.assertFalse(first.is_active)
        self.assertIsNone(self.registry.active())

        self.registry.ensure_active()
        active = self.registry.active()
        assert active is not None
        self.assertEqual(active.id, first.id)
        self.assertEqual(active.display_name, "阿杜")
        self.assertEqual(self.registry.count(), 1)

        second, _ = self.install("小林")
        self.registry.use(second.id)
        self.assertEqual(self.registry.active().id, second.id)  # type: ignore[union-attr]
        self.assertEqual(len(self.registry.list()), 2)

    def test_install_is_idempotent_by_fingerprint(self) -> None:
        first, created = self.install("阿杜", description="同一个卡")
        again, created_again = self.install("阿杜", description="同一个卡")
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.id, again.id)
        self.assertEqual(self.registry.count(), 1)

    def test_resolve_by_id_name_and_unique_substring(self) -> None:
        a, _ = self.install("阿杜", nickname="小杜")
        b, _ = self.install("图书管理员")
        self.install("另一个角色")
        self.install("角色二号")
        self.assertEqual(self.registry.resolve(str(a.id)).id, a.id)  # type: ignore[union-attr]
        self.assertEqual(self.registry.resolve("阿杜").id, a.id)  # type: ignore[union-attr]
        self.assertEqual(self.registry.resolve("小杜").id, a.id)  # type: ignore[union-attr]
        self.assertEqual(self.registry.resolve("图书").id, b.id)  # type: ignore[union-attr]
        self.assertIsNone(self.registry.resolve("角色"))  # ambiguous
        self.assertIsNone(self.registry.resolve("nope"))

    def test_search_matches_description_personality_and_tags(self) -> None:
        self.install("阿杜", description="温柔的图书管理员", tags=["日常"])
        self.install("小林", personality="喜欢爬山和摄影", tags=["户外"])
        self.install("阿万", description="图书管理员，脾气很差")

        self.assertEqual([r.name for r in self.registry.search("图书")], ["阿杜", "阿万"])
        self.assertEqual([r.name for r in self.registry.search("温柔")], ["阿杜"])
        self.assertEqual([r.name for r in self.registry.search("户外")], ["小林"])
        self.assertEqual([r.name for r in self.registry.search("爬山 摄影")], ["小林"])
        self.assertEqual(self.registry.search("爬山 图书"), [])

    def test_search_escapes_like_wildcards(self) -> None:
        self.install("百分号", description="100% 纯手工")
        self.install("下划线", description="a_b")
        self.assertEqual([r.name for r in self.registry.search("100%")], ["百分号"])
        self.assertEqual([r.name for r in self.registry.search("a_b")], ["下划线"])
        self.assertEqual([r.name for r in self.registry.search("%")], ["百分号"])

    def test_search_ranks_name_matches_first(self) -> None:
        self.install("阿杜", description="喜欢摄影")
        self.install("摄影社", description="社团")
        self.assertEqual([r.name for r in self.registry.search("摄影")], ["摄影社", "阿杜"])

    def test_only_one_active_card(self) -> None:
        a, _ = self.install("A")
        b, _ = self.install("B")
        self.registry.use(a.id)
        self.registry.use(b.id)
        self.reopen()
        actives = [r.id for r in self.registry.list() if r.is_active]
        self.assertEqual(actives, [b.id])

    def test_delete(self) -> None:
        a, _ = self.install("A")
        b, _ = self.install("B")
        self.registry.use(a.id)
        with self.assertRaises(ValueError):
            self.registry.delete(a.id)
        self.assertTrue(self.registry.delete(b.id))
        self.assertFalse(self.registry.delete(b.id))
        self.assertEqual(self.registry.count(), 1)

    def test_persists_across_reopen(self) -> None:
        record, _ = self.install("持久卡", description="写入 SQLite")
        self.registry.use(record.id)
        self.reopen()
        loaded = self.registry.active()
        assert loaded is not None
        self.assertEqual(loaded.id, record.id)
        self.assertEqual(loaded.description, "写入 SQLite")
        self.assertEqual(loaded.card.name, "持久卡")

    def test_install_from_json_and_png_bytes(self) -> None:
        record, created = self.registry.install_bytes(
            json.dumps(payload("字节卡")).encode("utf-8"), source="memory.json",
        )
        self.assertTrue(created)
        self.assertEqual(record.name, "字节卡")

        png_record, created = self.registry.install_bytes(card_png("PNG 卡"), source="card.png")
        self.assertTrue(created)
        self.assertEqual(png_record.name, "PNG 卡")
        self.assertEqual(png_record.source_kind, "url")

        with self.assertRaises(CardError):
            self.registry.install_bytes(b"not a card", source="bad.bin")

    def test_bootstrap_imports_legacy_file_once(self) -> None:
        path = self.root / "character.json"
        path.write_text(json.dumps(payload("引导卡", description="v1")), encoding="utf-8")

        record = self.registry.bootstrap(path)
        assert record is not None
        self.assertEqual(record.name, "引导卡")
        self.assertEqual(record.source_kind, "bootstrap")
        self.assertTrue(record.is_active)

        # A second bootstrap call does not import it again.
        self.registry.bootstrap(path)
        self.assertEqual(self.registry.count(), 1)

    def test_sync_bootstrap_reloads_changed_file(self) -> None:
        path = self.root / "character.json"
        path.write_text(json.dumps(payload("引导卡", description="v1")), encoding="utf-8")
        record = self.registry.bootstrap(path)
        assert record is not None
        self.assertFalse(self.registry.sync_bootstrap(path))

        path.write_text(json.dumps(payload("引导卡", description="v2")), encoding="utf-8")
        future = time.time() + 5
        os.utime(path, (future, future))
        self.assertTrue(self.registry.sync_bootstrap(path))
        active = self.registry.active()
        assert active is not None
        self.assertEqual(active.description, "v2")
        self.assertEqual(active.id, record.id)
        self.assertFalse(self.registry.sync_bootstrap(path))

    def test_sync_bootstrap_ignores_inactive_or_other_cards(self) -> None:
        path = self.root / "character.json"
        path.write_text(json.dumps(payload("引导卡")), encoding="utf-8")
        self.registry.bootstrap(path)
        other, _ = self.install("普通卡")
        self.registry.use(other.id)

        path.write_text(json.dumps(payload("引导卡", description="改了")), encoding="utf-8")
        self.assertFalse(self.registry.sync_bootstrap(path))

    def test_fingerprint_index_is_unique(self) -> None:
        indexes = self.registry._db.execute(
            "SELECT name, \"unique\" FROM pragma_index_list('cards')"
        ).fetchall()
        unique = {row[0]: bool(row[1]) for row in indexes}
        self.assertTrue(unique.get("idx_cards_fingerprint"))

    def test_sync_bootstrap_dedupes_copy_installed_elsewhere(self) -> None:
        path = self.root / "character.json"
        path.write_text(json.dumps(payload("引导卡", description="v1")), encoding="utf-8")
        boot = self.registry.bootstrap(path)
        assert boot is not None
        extra, _ = self.registry.install_payload(payload("引导卡", description="v2"))

        path.write_text(json.dumps(payload("引导卡", description="v2")), encoding="utf-8")
        os.utime(path, (time.time() + 5, time.time() + 5))
        self.assertTrue(self.registry.sync_bootstrap(path))

        active = self.registry.active()
        assert active is not None
        self.assertEqual(active.id, boot.id)
        self.assertEqual(active.description, "v2")
        self.assertIsNone(self.registry.get(extra.id))
        self.assertEqual(self.registry.count(), 1)

    def test_active_signature_changes_on_switch(self) -> None:
        a, _ = self.install("A")
        b, _ = self.install("B")
        self.registry.use(a.id)
        first = self.registry.active_signature()
        self.registry.use(b.id)
        self.assertNotEqual(first, self.registry.active_signature())
        self.registry.use(b.id)
        self.assertIsNotNone(self.registry.active_signature())


if __name__ == "__main__":
    unittest.main()
