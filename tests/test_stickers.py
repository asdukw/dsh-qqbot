from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from stickers import StickerError, StickerStore, sniff_mime  # noqa: E402

PNG = bytes.fromhex("89504e470d0a1a0a") + b"fake-png-body"
JPEG = bytes.fromhex("ffd8ff") + b"fake-jpeg-body"
GIF = bytes.fromhex("474946383961") + b"fake-gif-body"
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"body"


class SniffTests(unittest.TestCase):
    def test_supported_formats(self) -> None:
        self.assertEqual(sniff_mime(PNG), ("image/png", ".png"))
        self.assertEqual(sniff_mime(JPEG), ("image/jpeg", ".jpg"))
        self.assertEqual(sniff_mime(GIF), ("image/gif", ".gif"))
        self.assertEqual(sniff_mime(WEBP), ("image/webp", ".webp"))

    def test_unknown_format(self) -> None:
        self.assertEqual(sniff_mime(b"not an image"), ("", ""))


class StickerStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = StickerStore(root / "stickers.db", root / "files")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_add_and_dedupe_by_content(self) -> None:
        record, created = self.store.add_bytes(PNG, source="group:1", source_kind="chat")
        self.assertTrue(created)
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(record.mime, "image/png")
        self.assertTrue(Path(record.path).is_file())

        again, created_again = self.store.add_bytes(PNG, source="group:2")
        self.assertFalse(created_again)
        self.assertEqual(again.id, record.id)
        self.assertEqual(self.store.count(), 1)

    def test_rejects_unknown_and_oversized(self) -> None:
        with self.assertRaises(StickerError):
            self.store.add_bytes(b"plain text")
        with self.assertRaises(StickerError):
            self.store.add_bytes(PNG, max_bytes=4)

    def test_describe_and_search(self) -> None:
        record, _ = self.store.add_bytes(PNG, source="group:9", source_kind="chat")
        self.store.describe(record.id, "一只摊在键盘上的橘猫", ["开心", "摸鱼"])
        hits = self.store.search("橘猫")
        self.assertEqual([hit.id for hit in hits], [record.id])
        hits = self.store.search("摸鱼")
        self.assertEqual([hit.id for hit in hits], [record.id])
        self.assertEqual(self.store.search("不存在"), [])

    def test_ban_hides_from_ok_list_but_keeps_row(self) -> None:
        record, _ = self.store.add_bytes(PNG)
        self.store.set_status(record.id, "banned")
        self.assertEqual(self.store.list(status="ok"), [])
        self.assertEqual(self.store.list(status="banned")[0].id, record.id)
        self.store.set_status(record.id, "ok")
        self.assertEqual(self.store.list(status="ok")[0].id, record.id)
        with self.assertRaises(ValueError):
            self.store.set_status(record.id, "nonsense")

    def test_delete_removes_file_and_row(self) -> None:
        record, _ = self.store.add_bytes(PNG)
        path = Path(record.path)
        self.assertTrue(path.is_file())
        self.assertTrue(self.store.delete(record.id))
        self.assertFalse(path.exists())
        self.assertEqual(self.store.count(), 0)
        self.assertFalse(self.store.delete(record.id))

    def test_stats(self) -> None:
        self.store.add_bytes(PNG)
        self.store.add_bytes(JPEG)
        stats = self.store.stats()
        self.assertEqual(stats["count"], 2)
        self.assertEqual(stats["bytes"], len(PNG) + len(JPEG))
        self.assertEqual(stats["banned"], 0)


if __name__ == "__main__":
    unittest.main()
