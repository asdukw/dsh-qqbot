from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from dsh_client import DSHClient, DSHConfig, DSHError  # noqa: E402


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


class AuthorizedRuntimeTests(unittest.TestCase):
    def test_private_runtime_nonce_does_not_leak_to_later_ordinary_turn(self):
        created = []

        class Harness:
            def __init__(self, **kwargs):
                self.env = kwargs['env']
                self.closed = False
                created.append(self)

            def run(self, content, session_id):
                return SimpleNamespace(final_response='OK', events=[])

            def close(self):
                self.closed = True

        old_env = dict(os.environ)
        client = DSHClient(DSHConfig())
        with patch('dsh_client.DeepSeekHarness', Harness):
            client.run_authorized('s1', 'install', (), {'QQBOT_CARD_LEASE': 'old-nonce', 'QQBOT_CARD_SECRET': 'secret',
                                                               'QQBOT_VIDEO_LEASE': 'video-nonce', 'QQBOT_VIDEO_SECRET': 'video-secret'})
            self.assertTrue(created[0].closed)
            client.run('s1', 'ordinary')
            self.assertEqual(created[1].env['QQBOT_CARD_LEASE'], '')
            self.assertEqual(created[1].env['QQBOT_CARD_SECRET'], '')
            self.assertEqual(created[1].env['QQBOT_VIDEO_LEASE'], '')
            self.assertEqual(created[1].env['QQBOT_VIDEO_SECRET'], '')
            self.assertEqual(created[1].env['QQBOT_VIDEO_BRIDGE'], '')
            self.assertEqual(os.environ, old_env)
            client.close()

    def test_failed_authorized_runtime_is_closed_and_lock_released(self):
        class Harness:
            closed = False

            def __init__(self, **kwargs):
                pass

            def run(self, content, session_id):
                raise RuntimeError('failure')

            def close(self):
                Harness.closed = True

        client = DSHClient(DSHConfig())
        with patch('dsh_client.DeepSeekHarness', Harness):
            with self.assertRaises(DSHError):
                client.run_authorized('s1', 'install', (), {'QQBOT_CARD_LEASE': 'nonce'})
        self.assertTrue(Harness.closed)
        self.assertIsNone(client._harness)
        self.assertFalse(client._lock.locked())


if __name__ == "__main__":
    unittest.main()
