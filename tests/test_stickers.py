from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from stickers import (  # noqa: E402
    StickerError,
    StickerStore,
    dhash_hex,
    extract_sticker_marker,
    hamming_distance,
    parse_tags,
    preview_jpeg,
    sniff_mime,
)

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


class PreviewTests(unittest.TestCase):
    def test_downsizes_a_large_image(self) -> None:
        from io import BytesIO

        from PIL import Image

        out = BytesIO()
        Image.new("RGBA", (1200, 800), (0, 0, 0, 0)).save(out, format="PNG")
        preview = preview_jpeg(out.getvalue())
        self.assertIsNotNone(preview)
        with Image.open(BytesIO(preview)) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertLessEqual(max(image.size), 512)

    def test_corrupt_bytes_return_none(self) -> None:
        self.assertIsNone(preview_jpeg(b"not an image"))


def patterned_image(fmt: str = "PNG", **save: object) -> bytes:
    from io import BytesIO

    from PIL import Image, ImageDraw

    image = Image.new("RGB", (256, 256), (30, 120, 200))
    ImageDraw.Draw(image).rectangle([64, 64, 192, 192], fill=(240, 40, 40))
    out = BytesIO()
    image.save(out, format=fmt, **save)
    return out.getvalue()


def striped_image(vertical: bool = True) -> bytes:
    from io import BytesIO

    from PIL import Image, ImageDraw

    image = Image.new("L", (256, 256), 0)
    draw = ImageDraw.Draw(image)
    for offset in range(0, 256, 32):
        if vertical:
            draw.rectangle([offset, 0, offset + 15, 255], fill=255)
        else:
            draw.rectangle([0, offset, 255, offset + 15], fill=255)
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class DHashTests(unittest.TestCase):
    def test_hash_is_stable_and_64_bits(self) -> None:
        data = patterned_image()
        digest = dhash_hex(data)
        self.assertEqual(len(digest), 16)
        self.assertEqual(digest, dhash_hex(data))
        self.assertEqual(hamming_distance(digest, digest), 0)

    def test_reencoded_image_is_close(self) -> None:
        self.assertLessEqual(
            hamming_distance(
                dhash_hex(patterned_image()),
                dhash_hex(patterned_image("JPEG", quality=50)),
            ),
            4,
        )

    def test_different_structures_are_far(self) -> None:
        self.assertGreater(
            hamming_distance(
                dhash_hex(striped_image(vertical=True)),
                dhash_hex(striped_image(vertical=False)),
            ),
            4,
        )

    def test_corrupt_bytes_return_empty(self) -> None:
        self.assertEqual(dhash_hex(b"not an image"), "")


class MarkerTests(unittest.TestCase):
    def test_extracts_and_strips_the_marker(self) -> None:
        self.assertEqual(
            extract_sticker_marker("哼，才不是。\n[表情: 无语]"),
            ("哼，才不是。", "无语"),
        )
        self.assertEqual(extract_sticker_marker("[表情：开心]"), ("", "开心"))
        self.assertEqual(extract_sticker_marker("只有表情 [表情: 累]"), ("只有表情", "累"))

    def test_plain_replies_are_untouched(self) -> None:
        self.assertEqual(extract_sticker_marker("正常回复"), ("正常回复", None))
        self.assertEqual(extract_sticker_marker("[图片] 这是啥"), ("[图片] 这是啥", None))


class ParseTagsTests(unittest.TestCase):
    def test_reads_json_with_surrounding_prose(self) -> None:
        desc, tags, safe = parse_tags(
            '好的：{"desc": "一只翻白眼的猫", "tags": ["无语", "猫"], "safe": false}'
        )
        self.assertEqual(desc, "一只翻白眼的猫")
        self.assertEqual(tags, ("无语", "猫"))
        self.assertFalse(safe)

    def test_missing_safe_defaults_to_true(self) -> None:
        _desc, _tags, safe = parse_tags('{"desc": "开心", "tags": ["开心"]}')
        self.assertTrue(safe)

    def test_falls_back_to_plain_text(self) -> None:
        desc, tags, safe = parse_tags("一只翻白眼的猫")
        self.assertEqual(desc, "一只翻白眼的猫")
        self.assertEqual(tags, ())
        self.assertTrue(safe)


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

    def test_near_duplicate_reencoded_image_is_merged(self) -> None:
        png = patterned_image()
        jpg = patterned_image("JPEG", quality=50)
        first, created = self.store.add_bytes(png)
        self.assertTrue(created)
        self.assertEqual(len(first.phash), 16)
        second, created_again = self.store.add_bytes(jpg)
        self.assertFalse(created_again)
        self.assertEqual(second.id, first.id)
        self.assertEqual(self.store.count(), 1)

    def test_different_images_are_kept(self) -> None:
        first, created = self.store.add_bytes(striped_image(vertical=True))
        self.assertTrue(created)
        second, created_again = self.store.add_bytes(striped_image(vertical=False))
        self.assertTrue(created_again)
        self.assertNotEqual(second.id, first.id)
        self.assertEqual(self.store.count(), 2)

    def test_backfill_hashes_existing_rows(self) -> None:
        png = patterned_image()
        jpg = patterned_image("JPEG", quality=50)
        first, created = self.store.add_bytes(png)
        self.assertTrue(created)
        conn = sqlite3.connect(self.store.path)
        conn.execute("UPDATE stickers SET phash = '' WHERE id = ?", (first.id,))
        conn.commit()
        conn.close()
        self.store.close()
        self.store = StickerStore(self.store.path, self.store.directory)
        second, created_again = self.store.add_bytes(jpg)
        self.assertFalse(created_again)
        self.assertEqual(second.id, first.id)

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

    def test_pick_prefers_matching_tags(self) -> None:
        first, _ = self.store.add_bytes(PNG)
        second, _ = self.store.add_bytes(JPEG)
        self.store.describe(first.id, "开心地转圈", ["开心"])
        self.store.describe(second.id, "无语地翻白眼", ["无语"])
        self.assertEqual(self.store.pick("开心").id, first.id)
        self.assertEqual(self.store.pick("无语").id, second.id)
        self.assertIn(self.store.pick("没有这个词").id, (first.id, second.id))

    def test_pick_skips_banned_and_empty(self) -> None:
        self.assertIsNone(self.store.pick("开心"))
        record, _ = self.store.add_bytes(PNG)
        self.store.set_status(record.id, "banned")
        self.assertIsNone(self.store.pick("开心"))

    def test_evict_drops_least_used_first(self) -> None:
        keep, _ = self.store.add_bytes(PNG)
        old_unused, _ = self.store.add_bytes(JPEG)
        middle, _ = self.store.add_bytes(GIF)
        self.store.mark_used(keep.id)
        path = Path(old_unused.path)
        removed = self.store.evict(2)
        self.assertEqual(removed, 1)
        self.assertEqual(self.store.count(), 2)
        self.assertIsNone(self.store.get(old_unused.id))
        self.assertIsNotNone(self.store.get(keep.id))
        self.assertIsNotNone(self.store.get(middle.id))
        self.assertFalse(path.exists())

    def test_evict_is_noop_under_the_cap(self) -> None:
        self.store.add_bytes(PNG)
        self.assertEqual(self.store.evict(10), 0)
        self.assertEqual(self.store.evict(0), 0)
        self.assertEqual(self.store.count(), 1)

    def test_stats(self) -> None:
        self.store.add_bytes(PNG)
        self.store.add_bytes(JPEG)
        stats = self.store.stats()
        self.assertEqual(stats["count"], 2)
        self.assertEqual(stats["bytes"], len(PNG) + len(JPEG))
        self.assertEqual(stats["banned"], 0)


if __name__ == "__main__":
    unittest.main()
