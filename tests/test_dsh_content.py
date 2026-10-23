from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from dsh_client import DSHClient  # noqa: E402


class DSHContentTests(unittest.TestCase):
    def test_text_only_keeps_the_cheap_string_form(self) -> None:
        self.assertEqual(DSHClient._content("你好"), "你好")

    def test_images_become_acp_blocks(self) -> None:
        blocks = DSHClient._content(
            "这是什么",
            ["data:image/png;base64,QUJD", "data:image/webp;base64,REVG"],
        )
        self.assertEqual(blocks[0], {"type": "text", "text": "这是什么"})
        self.assertEqual(
            blocks[1], {"type": "image", "data": "QUJD", "mimeType": "image/png"}
        )
        self.assertEqual(blocks[2]["mimeType"], "image/webp")

    def test_non_data_uris_are_skipped(self) -> None:
        blocks = DSHClient._content("hi", ["https://example.com/a.png"])
        self.assertEqual(blocks, [{"type": "text", "text": "hi"}])


if __name__ == "__main__":
    unittest.main()
