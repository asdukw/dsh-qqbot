from __future__ import annotations

import base64
import json
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from cards import CharacterCard, Lorebook, normalise  # noqa: E402


def chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def make_png(text_chunks: list[tuple[bytes, bytes, bytes]]) -> bytes:
    """text_chunks: (keyword, payload, b'tEXt'|b'iTXt')."""
    out = b"\x89PNG\r\n\x1a\n"
    out += chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    for keyword, payload, kind in text_chunks:
        if kind == b"tEXt":
            out += chunk(b"tEXt", keyword + b"\x00" + payload)
        else:
            out += chunk(b"iTXt", keyword + b"\x00\x00\x00\x00\x00" + payload)
    return out + chunk(b"IEND", b"")


def b64(obj: dict) -> bytes:
    return base64.b64encode(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


class CardNormalisationTests(unittest.TestCase):
    def test_v1_flat_fields(self) -> None:
        raw = {
            "char_name": "旧卡",
            "char_persona": "旧设定",
            "char_greeting": "你好",
            "world_scenario": "旧场景",
            "example_dialogue": "示例",
        }
        payload = normalise(raw)
        data = payload["data"]
        self.assertEqual(payload["spec"], "chara_card_v3")
        self.assertEqual(data["name"], "旧卡")
        self.assertEqual(data["description"], "旧设定")
        self.assertEqual(data["first_mes"], "你好")
        self.assertEqual(data["scenario"], "旧场景")
        self.assertEqual(data["mes_example"], "示例")
        self.assertEqual(payload["_import"]["spec"], "v1")

    def test_v2_is_upgraded_to_v3(self) -> None:
        raw = {
            "spec": "chara_card_v2",
            "spec_version": "2.0",
            "data": {
                "name": "阿杜",
                "description": "描述",
                "personality": "温和",
                "first_mes": "嗨",
                "character_book": {"entries": []},
            },
        }
        payload = normalise(raw)
        self.assertEqual(payload["spec"], "chara_card_v3")
        self.assertEqual(payload["data"]["name"], "阿杜")
        self.assertEqual(payload["data"]["group_only_greetings"], [])
        self.assertEqual(payload["_import"]["spec"], "chara_card_v2")

    def test_v3_keeps_extensions(self) -> None:
        raw = {
            "spec": "chara_card_v3",
            "spec_version": "3.0",
            "data": {
                "name": "V3",
                "nickname": "小V",
                "group_only_greetings": ["群欢迎"],
                "extensions": {"depth_prompt": {"prompt": "x", "depth": 2, "role": "system"}},
            },
        }
        payload = normalise(raw)
        self.assertEqual(payload["data"]["nickname"], "小V")
        self.assertEqual(payload["data"]["group_only_greetings"], ["群欢迎"])
        self.assertIn("depth_prompt", payload["data"]["extensions"])


class PngCardTests(unittest.TestCase):
    def test_text_chunk_prefers_ccv3(self) -> None:
        v2 = {"spec": "chara_card_v2", "spec_version": "2.0", "data": {"name": "旧"}}
        v3 = {"spec": "chara_card_v3", "spec_version": "3.0", "data": {"name": "新"}}
        png = make_png([(b"chara", b64(v2), b"tEXt"), (b"ccv3", b64(v3), b"tEXt")])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "card.png"
            path.write_bytes(png)
            card = CharacterCard.load(path)
        self.assertEqual(card.data["name"], "新")

    def test_itxt_compressed_payload(self) -> None:
        card = {"spec": "chara_card_v3", "spec_version": "3.0", "data": {"name": "压缩卡"}}
        payload = zlib.compress(b64(card))
        png = make_png([(b"ccv3", payload, b"iTXt")])
        # Replace the five zero control bytes with flag=1, method=0, empty lang/translated.
        png = png.replace(b"ccv3\x00\x00\x00\x00\x00", b"ccv3\x00\x01\x00\x00\x00")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "compressed.png"
            path.write_bytes(png)
            loaded = CharacterCard.load(path)
        self.assertEqual(loaded.data["name"], "压缩卡")


class LorebookTests(unittest.TestCase):
    def test_selects_constant_keyword_and_regex(self) -> None:
        book = Lorebook.from_card({
            "entries": [
                {"keys": [], "content": "常驻设定", "constant": True, "insertion_order": 0},
                {"keys": ["杭州"], "content": "杭州设定", "insertion_order": 1},
                {"keys": ["北京"], "content": "北京设定", "enabled": False},
                {"keys": ["harbin"], "content": "正则设定", "use_regex": True},
            ]
        })
        selected = book.select("我住在杭州，也去过 HARBIN")
        self.assertIn("常驻设定", selected)
        self.assertIn("杭州设定", selected)
        self.assertIn("正则设定", selected)
        self.assertNotIn("北京设定", selected)

    def test_secondary_keys_require_both(self) -> None:
        book = Lorebook.from_card({
            "entries": [
                {
                    "keys": ["旅行"],
                    "secondary_keys": ["日本"],
                    "selective": True,
                    "content": "日本旅行设定",
                },
            ]
        })
        self.assertEqual(book.select("我想去旅行"), [])
        self.assertEqual(book.select("我想去日本旅行"), ["日本旅行设定"])


if __name__ == "__main__":
    unittest.main()
