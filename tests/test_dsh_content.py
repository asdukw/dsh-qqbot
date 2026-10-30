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

class RawInteractionRuntimeTests(unittest.TestCase):
    def run_result(self, result):
        class Harness:
            def __init__(self, **kwargs):
                self.env = kwargs['env']

            def run(self, content, session_id):
                return result

            def close(self):
                pass

        client = DSHClient(DSHConfig())
        with patch('dsh_client.DeepSeekHarness', Harness):
            return client.run_authorized_result('s', 'hello', (), {'QQBOT_INTERACTION_LEASE': 'nonce'})

    def test_empty_raw_result_is_not_a_regular_reply(self):
        result = SimpleNamespace(final_response='', finish_reason='completed', events=[])
        self.assertEqual(self.run_result(result).reply, '')
        with self.assertRaises(DSHError):
            DSHClient._reply_from_result(result)

    def test_model_error_survives_nonempty_tool_announcement(self):
        result = SimpleNamespace(final_response='I will react', finish_reason='error', events=[{
            'type': 'turn/end', 'data': {'reason': {'kind': 'error', 'error': {'code': 'FAILED', 'message': 'fixture'}}},
        }])
        turn = self.run_result(result)
        self.assertIn('FAILED', turn.error)

    def test_tool_call_text_is_not_terminal_final(self):
        turn = self.run_result(SimpleNamespace(final_response='I will reply', finish_reason='completed', events=[{
            'type': 'assistant/message', 'data': {'message': {'content': [
                {'type': 'text', 'text': 'I will reply'}, {'type': 'tool_use', 'name': 'reply_to_others'},
            ]}},
        }]))
        self.assertEqual(turn.reply, '')

    def test_transport_unknown_is_taken_only_from_actual_social_tool_results(self):
        call = {'type': 'tool/call', 'data': {'callId': 'actual', 'name': 'react_to_others'}}
        result = {'type': 'tool/result', 'data': {'message': {'toolCallId': 'actual',
            'content': [{'type': 'text', 'text': '{"status":"unknown"}'}]}}}
        turn = self.run_result(SimpleNamespace(final_response='do not send', finish_reason='completed',
                                              events=[call, result]))
        self.assertTrue(turn.interaction_unknown)
        unrelated = {'type': 'tool/call', 'data': {'callId': 'actual', 'name': 'read'}}
        self.assertFalse(self.run_result(SimpleNamespace(final_response='answer', events=[unrelated, result])).interaction_unknown)
        self.assertFalse(self.run_result(SimpleNamespace(final_response='{"status":"unknown"}', events=[])).interaction_unknown)

    def test_interaction_authority_is_cleared_even_if_parent_env_contains_it(self):
        created = []

        class Harness:
            def __init__(self, **kwargs):
                created.append(kwargs['env'])

            def run(self, content, session_id):
                return SimpleNamespace(final_response='OK', events=[])

            def close(self):
                pass

        client = DSHClient(DSHConfig())
        keys = ('QQBOT_INTERACTION_BRIDGE', 'QQBOT_INTERACTION_SECRET', 'QQBOT_INTERACTION_LEASE')
        original = dict(os.environ)
        with patch.dict(os.environ, dict.fromkeys(keys, 'parent-authority')), patch('dsh_client.DeepSeekHarness', Harness):
            client.run_authorized_result('s', 'react', (), dict.fromkeys(keys, 'turn-authority'))
            client.run('s', 'ordinary')
            self.assertEqual({key: created[0][key] for key in keys}, dict.fromkeys(keys, 'turn-authority'))
            self.assertEqual({key: created[1][key] for key in keys}, dict.fromkeys(keys, ''))
            self.assertTrue(all(os.environ[key] == 'parent-authority' for key in keys))
            client.close()
        self.assertEqual(dict(os.environ), original)


if __name__ == "__main__":
    unittest.main()
