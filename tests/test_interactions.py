from __future__ import annotations

import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from group_context import GroupLine  # noqa: E402
from interaction_service import MAX_REQUESTS, InteractionService  # noqa: E402
from onebot import MessageEvent, OneBotError, OneBotRejected  # noqa: E402


def event(**overrides):
    values = dict(message_type='group', group_id=42, user_id=7, message_id=101,
                  text='hello', at_me=True, sender_name='same name')
    values.update(overrides)
    return MessageEvent(**values)


def targets():
    return (
        GroupLine('same name', 'hello', user_id=7, message_id=101),
        GroupLine('same name', 'other participant', user_id=8, message_id=-202, reply_to=909),
        GroupLine('third', 'third participant', user_id=9, message_id=303),
        GroupLine('fourth', 'fourth participant', user_id=10, message_id=404),
    )


class InteractionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sender = AsyncMock(return_value={'message_id': -501})
        self.reactor = AsyncMock(return_value={})
        self.service = InteractionService(self.sender, self.reactor)
        await self.service.start()
        self.lease = self.service.open(event(), 'group:42', 'session-A', targets())

    async def asyncTearDown(self):
        await asyncio.wait_for(self.service.close(), 3)

    async def call(self, args, operation='reply', lease=None):
        lease = lease or self.lease
        return await self.service.call(lease.nonce, lease.session, args, operation)

    async def test_reply_to_other_signed_message_plus_multiple_distinct_users(self):
        result = await self.call({'message_id': '-202', 'text': '  agreed  ',
                                  'user_ids': ['8', '9']})
        self.assertEqual(result['status'], 'sent')
        self.assertEqual(result['message_id'], '-501')
        self.sender.assert_awaited_once_with('group:42', 'agreed', -202, (8, 9), 10.0)
        self.assertTrue(self.lease.has_confirmed)
        self.assertTrue(self.lease.has_text)
        self.assertFalse(self.lease.has_unknown)
        self.assertEqual(self.lease.sent_texts, ['agreed'])
        self.assertEqual(self.lease.receipts, [{'message_id': -501}])
        receipts = self.lease.receipts
        receipts[0]['message_id'] = 99
        self.assertEqual(self.lease.receipts[0]['message_id'], -501)

    async def test_mention_multiple_people_without_quoting(self):
        result = await self.call({'user_ids': ['8', '9', '10'], 'text': 'everyone'}, 'mention')
        self.assertEqual(result['status'], 'sent')
        self.sender.assert_awaited_once_with('group:42', 'everyone', None, (8, 9, 10), 10.0)

    async def test_reaction_only_is_not_a_text_receipt(self):
        result = await self.call({'message_id': '303', 'emoji_id': '76'}, 'react')
        self.assertEqual(result['status'], 'reacted')
        self.reactor.assert_awaited_once_with(303, '76', timeout=2.0)
        self.sender.assert_not_awaited()
        self.assertTrue(self.lease.has_confirmed)
        self.assertFalse(self.lease.has_text)
        self.assertEqual(self.lease.sent_texts, [])
        self.assertEqual(self.lease.receipts, [])

    async def test_message_and_user_targets_are_snapshot_only_not_quote_or_text(self):
        for args, operation in (
            ({'message_id': '909', 'text': 'quoted but invisible'}, 'reply'),
            ({'message_id': '500', 'text': 'forged'}, 'reply'),
            ({'user_ids': ['42'], 'text': 'not a participant'}, 'mention'),
            ({'message_id': '909', 'emoji_id': '76'}, 'react'),
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                await self.call(args, operation)
        self.sender.assert_not_awaited()
        self.reactor.assert_not_awaited()
        self.assertFalse(self.lease.actions)

    async def test_all_id_parameters_are_canonical_decimal_strings(self):
        for bad in (101, True, None, 0, 1.0, '', '0', '-0', '0101', '+101',
                    ' 101', '101 ', '١٠١', '1e2', '1.0', '1' * 65):
            with self.subTest(message_id=bad), self.assertRaises(ValueError):
                await self.call({'message_id': bad, 'text': 'no'})
        self.lease.calls = 0
        for bad in (8, True, '', 'all', '0', '-8', '+8', '08', ' 8', '８'):
            with self.subTest(user_id=bad), self.assertRaises(ValueError):
                await self.call({'user_ids': [bad], 'text': 'no'}, 'mention')
        self.sender.assert_not_awaited()

    async def test_parameter_schema_rejects_extras_and_wrong_types(self):
        for args, op in (
            ([], 'reply'), ({}, 'reply'),
            ({'message_id': '101', 'text': 'hi', 'conversation': 'group:43'}, 'reply'),
            ({'user_ids': [], 'text': 'hi'}, 'mention'),
            ({'user_ids': ('8',), 'text': 'hi'}, 'mention'),
            ({'user_ids': ['7', '8', '9', '10'], 'text': 'hi'}, 'mention'),
            ({'message_id': '101', 'emoji_id': 76}, 'react'),
            ({'message_id': '101', 'emoji_id': '076'}, 'react'),
            ({'message_id': '101', 'emoji_id': '76', 'text': 'extra'}, 'react'),
            ({'message_id': '101', 'text': 'hi'}, 'send'),
            ({'message_id': '101', 'text': 'hi'}, []),
        ):
            with self.subTest(args=args, op=op), self.assertRaises(ValueError):
                await self.call(args, op)
        self.sender.assert_not_awaited()

    async def test_negative_reaction_target_is_explicitly_unsupported(self):
        with self.assertRaisesRegex(ValueError, 'negative'):
            await self.call({'message_id': '-202', 'emoji_id': '76'}, 'react')
        self.reactor.assert_not_awaited()

    async def test_private_can_quote_peer_but_cannot_mention(self):
        private = self.service.open(event(message_type='private', group_id=None), 'private:7',
                                    'private-session', targets()[:1])
        result = await self.call({'message_id': '101', 'text': 'private'}, lease=private)
        self.assertEqual(result['status'], 'sent')
        self.sender.assert_awaited_once_with('private:7', 'private', 101, (), 10.0)
        for args, op in (({'user_ids': ['7'], 'text': 'no'}, 'mention'),
                         ({'message_id': '101', 'user_ids': ['7'], 'text': 'no'}, 'reply')):
            with self.assertRaises(ValueError):
                await self.call(args, op, private)

    async def test_open_rejects_synthetic_or_mismatched_authority(self):
        for incoming, conversation, lines in (
            (event(message_id=0), 'group:42', targets()),
            (event(message_id=True), 'group:42', targets()),
            (event(message_id='101'), 'group:42', targets()),
            (event(user_id=True), 'group:42', targets()),
            (event(group_id=True), 'group:True', targets()),
            (event(), 'group:43', targets()),
            (event(), 'private:7', targets()),
            (event(message_type='notice'), 'group:42', targets()),
            (event(), 'group:42', [*targets()]),
            (event(), 'group:42', ()),
            (event(), 'group:42', (GroupLine('fake', 'x', user_id=0, message_id=100),)),
            (event(), 'group:42', (GroupLine('fake', 'x', user_id=8),)),
            (event(), 'group:42', (GroupLine('fake', 'x', user_id=8, message_id=True),)),
            (event(message_type='private'), 'private:7', targets()),
        ):
            with self.subTest(conversation=conversation, lines=lines), self.assertRaises(ValueError):
                self.service.open(incoming, conversation, 'session', lines)
        signed = self.service.open(event(message_id=-101), 'group:42', 'signed', targets())
        self.assertIs(self.service.authorize(signed.nonce, 'signed'), signed)

    async def test_open_requires_positive_finite_bounds(self):
        for kwargs in ({'max_text_chars': 0}, {'max_text_chars': True},
                       {'lifetime': 0}, {'lifetime': True}, {'lifetime': float('nan')},
                       {'lifetime': float('inf')}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.service.open(event(), 'group:42', 'session', targets(), **kwargs)
        hard = self.service.open(event(), 'group:42', 'hard', targets(), max_text_chars=9000)
        self.assertEqual(hard.max_text_chars, 4000)

    async def test_authority_and_bridge_secrets_are_independent_and_turn_local(self):
        env = self.service.env(self.lease)
        self.assertEqual(set(env), {'QQBOT_INTERACTION_BRIDGE', 'QQBOT_INTERACTION_SECRET',
                                    'QQBOT_INTERACTION_LEASE'})
        self.assertTrue(env['QQBOT_INTERACTION_BRIDGE'].startswith('http://127.0.0.1:'))
        for nonce, session in ((self.lease.nonce, 'session-B'), ('forged', self.lease.session),
                               (None, self.lease.session), (self.lease.nonce, 123)):
            with self.assertRaises(ValueError):
                await self.service.call(nonce, session, {'message_id': '101', 'text': 'no'})
        second = InteractionService(self.sender, self.reactor)
        try:
            self.assertNotEqual(second.secret, self.service.secret)
            with self.assertRaises(ValueError):
                second.authorize(self.lease.nonce, self.lease.session)
        finally:
            await second.close()

    async def test_text_safety_and_every_hidden_control_parser(self):
        for marker in ('[好感+1]', '[印象: 新印象]', '[关系: 朋友]',
                       '```relation\nnot json\n```', '[定时: 喝水]', '[定时列表]',
                       '[定时删除: 喝水]', '[语音]', '[表情: ]', '[梗图: 文字]'):
            with self.subTest(marker=marker), self.assertRaisesRegex(ValueError, 'control markers'):
                await self.call({'message_id': '101', 'text': 'hello' + marker})
        with self.assertRaises(ValueError):
            await self.call({'message_id': '101', 'text': '性交'})
        self.sender.assert_not_awaited()
        self.assertFalse(self.lease.has_confirmed)

    async def test_oversize_text_is_rejected_before_control_parsers(self):
        with patch('interaction_service._reject_markers') as parse:
            with self.assertRaisesRegex(ValueError, 'shorten'):
                await self.call({'message_id': '101', 'text': '[好感' + ' ' * 8000 + 'x'})
        parse.assert_not_called()
        self.sender.assert_not_awaited()

    async def test_malformed_relation_prefix_does_not_enter_backtracking_parser(self):
        with self.assertRaisesRegex(ValueError, 'control markers'):
            await self.call({'message_id': '101', 'text': '[好感' + ' ' * 1000 + 'x'})
        self.sender.assert_not_awaited()

    async def test_unknown_notifies_other_authorities_once(self):
        revoked = []
        self.lease.on_unknown = lambda: revoked.append('external authorities revoked')
        self.sender.side_effect = OneBotError('timeout')
        result = await self.call({'message_id': '101', 'text': 'uncertain'})
        self.assertEqual(result['status'], 'unknown')
        await self.call({'message_id': '101', 'text': 'uncertain'})
        self.service.revoke(self.lease)
        self.assertEqual(revoked, ['external authorities revoked'])

    async def test_text_limits_reject_without_splitting_or_truncating(self):
        bounded = self.service.open(event(), 'group:42', 'short', targets(), max_text_chars=5)
        for text in ('', '   ', None, True, '123456'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                await self.call({'message_id': '101', 'text': text}, lease=bounded)
        self.sender.assert_not_awaited()
        result = await self.call({'message_id': '101', 'text': '12345'}, lease=bounded)
        self.assertEqual(result['status'], 'sent')
        self.assertEqual(bounded.sent_texts, ['12345'])

    async def test_schedule_is_purely_rendered_and_cq_stays_visible_text(self):
        result = await self.call({'message_id': '101', 'text':
            '```schedule\n{"event":"Cup","matches":[{"start_time":"2026-11-01T12:00:00+08:00",'
            '"team_a":"A","team_b":"B"}]}\n```'})
        self.assertEqual(result['status'], 'sent')
        self.assertIn('A vs B', self.lease.sent_texts[0])
        self.assertNotIn('```', self.lease.sent_texts[0])
        cq = '[CQ:at,qq=all][CQ:reply,id=999]'
        await self.call({'message_id': '101', 'text': cq})
        self.assertEqual(self.lease.sent_texts[-1], cq)

    async def test_three_text_and_six_total_actions_are_claimed(self):
        for i in range(3):
            await self.call({'message_id': '101', 'text': f'text-{i}'})
        with self.assertRaisesRegex(ValueError, 'text action limit'):
            await self.call({'message_id': '101', 'text': 'fourth'})
        for i in range(3):
            await self.call({'message_id': '101', 'emoji_id': str(i + 1)}, 'react')
        with self.assertRaisesRegex(ValueError, 'action limit'):
            await self.call({'message_id': '101', 'emoji_id': '100'}, 'react')
        self.assertEqual(len(self.lease.actions), 6)
        self.assertEqual(self.sender.await_count, 3)
        self.assertEqual(self.reactor.await_count, 3)
        self.assertEqual(self.lease.sent_texts, ['text-0', 'text-1', 'text-2'])
        self.assertEqual(len(self.lease.receipts), 3)

    async def test_total_request_limit_even_for_cached_actions(self):
        args = {'message_id': '101', 'text': 'one'}
        for _ in range(MAX_REQUESTS):
            await self.call(args)
        with self.assertRaisesRegex(ValueError, 'request limit'):
            await self.call(args)
        self.sender.assert_awaited_once()
        self.assertEqual(len(self.lease.actions), 1)

    async def test_concurrent_duplicates_share_one_send_and_cached_result(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(*_args):
            entered.set()
            await asyncio.wait_for(release.wait(), 2)
            return {'message_id': 601}

        self.sender.side_effect = send
        args = {'message_id': '-202', 'text': 'hello', 'user_ids': ['8', '9']}
        a = asyncio.create_task(self.call(args))
        await asyncio.wait_for(entered.wait(), 1)
        b = asyncio.create_task(self.call({**args, 'user_ids': ['9', '8', '8']}))
        await asyncio.sleep(0)
        self.assertEqual(len(self.lease.actions), 1)
        release.set()
        first, second = await asyncio.gather(a, b)
        self.assertEqual(first, second)
        self.assertEqual(first, await self.call(args))
        first['status'] = 'tampered'
        self.assertEqual((await self.call(args))['status'], 'sent')
        self.sender.assert_awaited_once()
        self.assertEqual(self.lease.sent_texts, ['hello'])

    async def test_distinct_concurrent_actions_serialize_and_unknown_freezes_queue(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(*_args):
            entered.set()
            await asyncio.wait_for(release.wait(), 2)
            raise OneBotError('connection lost')

        self.sender.side_effect = send
        first = asyncio.create_task(self.call({'message_id': '101', 'text': 'first'}))
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(self.call({'message_id': '-202', 'text': 'second'}))
        third = asyncio.create_task(self.call({'message_id': '303', 'emoji_id': '76'}, 'react'))
        await asyncio.sleep(0)
        self.assertEqual(self.sender.await_count, 1)
        release.set()
        results = await asyncio.gather(first, second, third)
        self.assertEqual([r['status'] for r in results], ['unknown', 'not_sent', 'not_sent'])
        self.sender.assert_awaited_once()
        self.reactor.assert_not_awaited()
        self.assertTrue(self.lease.has_unknown)
        self.assertFalse(self.lease.has_text)
        again = await self.call({'message_id': '101', 'text': 'first'})
        self.assertEqual(again['status'], 'unknown')
        later = await self.call({'message_id': '101', 'text': 'later'})
        self.assertEqual(later['status'], 'not_sent')

    async def test_only_explicit_rejection_is_safe_to_continue_and_text_complete_is_strict(self):
        self.sender.side_effect = [OneBotRejected('no'), {'message_id': 10}]
        args = {'message_id': '101', 'text': 'rejected'}
        self.assertEqual((await self.call(args))['status'], 'rejected')
        self.assertEqual((await self.call(args))['status'], 'rejected')
        self.sender.assert_awaited_once()
        self.assertEqual((await self.call({'message_id': '101', 'text': 'sent'}))['status'], 'sent')
        self.assertTrue(self.lease.has_text)
        self.assertFalse(self.lease.has_unknown)
        self.assertFalse(self.lease.text_complete)
        self.assertEqual(self.lease.sent_texts, ['sent'])

    async def test_invalid_receipt_never_reports_success(self):
        for data in ({}, {'message_id': True}, {'message_id': False}, {'message_id': '123'},
                     {'message_id': 0}, {'message_id': 1.0}, [], None):
            lease = self.service.open(event(), 'group:42', 'bad-receipt', targets())
            self.sender.return_value = data
            with self.subTest(data=data):
                result = await self.call({'message_id': '101', 'text': 'no false success'}, lease=lease)
                self.assertEqual(result['status'], 'unknown')
                self.assertTrue(lease.has_unknown)
                self.assertFalse(lease.has_confirmed)
                self.assertFalse(lease.has_text)
                self.assertEqual(lease.receipts, [])
                self.assertFalse(lease.text_complete)
            self.service.revoke(lease)


    async def test_reaction_transport_failure_freezes_text_too(self):
        self.reactor.side_effect = TimeoutError('no ack')
        result = await self.call({'message_id': '303', 'emoji_id': '124'}, 'react')
        self.assertEqual(result['status'], 'unknown')
        result = await self.call({'message_id': '101', 'text': 'must not send'})
        self.assertEqual(result['status'], 'not_sent')
        self.sender.assert_not_awaited()

    async def test_own_timeout_cancels_transport_and_records_unknown(self):
        cancelled = asyncio.Event()

        async def send(*_args):
            try:
                await asyncio.wait_for(asyncio.Future(), 5)
            finally:
                cancelled.set()

        self.sender.side_effect = send
        with patch('interaction_service.TEXT_TIMEOUT', 0.01):
            result = await self.call({'message_id': '101', 'text': 'timeout'})
        self.assertEqual(result['status'], 'unknown')
        self.assertTrue(cancelled.is_set())
        self.assertTrue(self.lease.has_unknown)
        self.assertEqual(self.lease.sent_texts, [])

    async def test_caller_cancellation_is_unknown_and_settle_waits_cleanup(self):
        entered, cleanup_started, cleanup_release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def send(*_args):
            entered.set()
            try:
                await asyncio.wait_for(asyncio.Future(), 5)
            finally:
                cleanup_started.set()
                await asyncio.wait_for(cleanup_release.wait(), 2)

        self.sender.side_effect = send
        pending = asyncio.create_task(self.call({'message_id': '101', 'text': 'cancel'}))
        await asyncio.wait_for(entered.wait(), 1)
        pending.cancel()
        await asyncio.wait_for(cleanup_started.wait(), 2)
        self.assertTrue(self.lease.has_unknown)
        settled = asyncio.create_task(self.service.settle(self.lease))
        await asyncio.sleep(0)
        self.assertFalse(settled.done())
        cleanup_release.set()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        await settled
        self.assertEqual(self.lease.sent_texts, [])

    async def test_revoke_cancels_started_and_queued_tasks_and_preserves_prior_receipt(self):
        await self.call({'message_id': '101', 'text': 'confirmed'})
        entered = asyncio.Event()

        async def send(*_args):
            entered.set()
            await asyncio.wait_for(asyncio.Future(), 5)

        self.sender.side_effect = send
        first = asyncio.create_task(self.call({'message_id': '101', 'text': 'in flight'}))
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(self.call({'message_id': '-202', 'text': 'queued'}))
        await asyncio.sleep(0)
        self.service.revoke(self.lease)
        self.assertTrue(self.lease.has_unknown)
        await asyncio.wait_for(self.service.settle(self.lease), 3)
        await asyncio.gather(first, second, return_exceptions=True)
        self.assertEqual(self.lease.sent_texts, ['confirmed'])
        self.assertEqual(self.lease.receipts, [{'message_id': -501}])
        outcomes = [a.result for a in self.lease.actions.values()]
        assert all(result is not None for result in outcomes)
        self.assertEqual([result['status'] for result in outcomes if result is not None],
                         ['sent', 'unknown', 'not_sent'])
        with self.assertRaises(ValueError):
            await self.call({'message_id': '101', 'text': 'expired'})


    async def test_cancelling_caller_cancels_action_and_duplicate_caches_uncertainty(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def send(*args):
            started.set()
            try:
                await asyncio.wait_for(asyncio.Event().wait(), 5)
            finally:
                cancelled.set()
        self.sender.side_effect = send
        args = {'message_id': '101', 'text': 'cancel'}
        task = asyncio.create_task(self.call(args))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(self.service.settle(self.lease), 3)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(self.lease.has_unknown)
        self.assertEqual((await self.call(args))['status'], 'unknown')
        self.sender.assert_awaited_once()

    async def test_close_collects_already_revoked_inflight_tasks(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def send(*args):
            started.set()
            try:
                await asyncio.wait_for(asyncio.Event().wait(), 5)
            finally:
                cancelled.set()
        self.sender.side_effect = send
        task = asyncio.create_task(self.call({'message_id': '101', 'text': 'closing'}))
        await asyncio.wait_for(started.wait(), 1)
        self.service.revoke(self.lease)
        await asyncio.wait_for(self.service.close(), 3)
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(self.lease.has_unknown)
        self.assertFalse(self.service._tasks)


    async def test_expired_authority_rejects_calls_but_preserves_confirmed_facts(self):
        await self.call({'message_id': '101', 'text': 'delivered'})
        self.lease.expires = time.monotonic() - 1
        with self.assertRaises(ValueError):
            await self.call({'message_id': '101', 'text': 'too late'})
        self.assertTrue(self.lease.has_confirmed)
        self.assertTrue(self.lease.text_complete)
        self.assertEqual(self.lease.sent_texts, ['delivered'])
        self.assertNotIn(self.lease.nonce, self.service.leases)

    async def test_expiry_timer_cancels_inflight_send_and_close_collects_it(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def send(*_args):
            entered.set()
            try:
                await asyncio.wait_for(asyncio.Future(), 5)
            finally:
                cancelled.set()

        self.sender.side_effect = send
        lease = self.service.open(event(), 'group:42', 'expiring', targets(), lifetime=30)
        pending = asyncio.create_task(self.call({'message_id': '101', 'text': 'flight'}, lease=lease))
        await asyncio.wait_for(entered.wait(), 1)
        assert lease.expiry is not None
        lease.expiry.cancel()
        lease.expires = time.monotonic() + 0.02
        lease.expiry = asyncio.get_running_loop().call_later(0.02, self.service.revoke, lease)
        await asyncio.wait_for(cancelled.wait(), 1)
        await asyncio.gather(pending, return_exceptions=True)
        await asyncio.wait_for(self.service.settle(lease), 3)
        self.assertTrue(lease.has_unknown)
        self.assertNotIn(lease.nonce, self.service.leases)
        await asyncio.wait_for(self.service.close(), 3)
        self.assertFalse(self.service._tasks)
        with self.assertRaises(ValueError):
            await self.service.start()

    async def test_receipt_is_committed_without_post_send_authority_check(self):
        async def send(*_args):
            self.service.revoke(self.lease)
            return {'message_id': -777}

        self.sender.side_effect = send
        await asyncio.gather(self.call({'message_id': '101', 'text': 'sent at expiry'}),
                             return_exceptions=True)
        self.assertTrue(self.lease.has_confirmed)
        self.assertFalse(self.lease.has_unknown)
        self.assertEqual(self.lease.sent_texts, ['sent at expiry'])
        self.assertEqual(self.lease.receipts, [{'message_id': -777}])

    async def request(self, body=None, *, path='/interaction', secret=None, extra='', raw=None):
        parsed = urlsplit(self.service.url)
        reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
        if body is None:
            body = {'lease': self.lease.nonce, 'session': self.lease.session,
                    'args': {'message_id': '101', 'text': 'http'}}
        payload = json.dumps(body).encode() if raw is None else raw
        secret = self.service.secret if secret is None else secret
        writer.write((f'POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {secret}\r\n'
                      f'Content-Length: {len(payload)}\r\n{extra}\r\n').encode() + payload)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 2)
        writer.close()
        await writer.wait_closed()
        header, _, data = response.partition(b'\r\n\r\n')
        return int(header.split()[1]), json.loads(data)

    async def test_pending_receipt_without_ack_suppresses_final_on_revoke(self):
        status, result = await self.request({'lease': self.lease.nonce, 'session': self.lease.session,
            'operation': 'react', 'args': {'message_id': '303', 'emoji_id': '76'}, 'receipt_ack': True})
        self.assertEqual((status, result['status']), (200, 'reacted'))
        self.assertTrue(self.lease.pending_receipt)
        self.assertFalse(self.lease.has_unknown)
        self.service.revoke(self.lease)
        self.assertTrue(self.lease.has_unknown)
        self.assertTrue(self.lease.has_confirmed)
        self.reactor.assert_awaited_once()

    async def test_ack_clears_only_delivery_guard_not_existing_uncertainty(self):
        await self.request({'lease': self.lease.nonce, 'session': self.lease.session,
            'operation': 'react', 'args': {'message_id': '303', 'emoji_id': '76'}, 'receipt_ack': True})
        status, result = await self.request({'lease': self.lease.nonce, 'session': self.lease.session,
                                           'operation': 'ack', 'args': {}})
        self.assertEqual((status, result['status']), (200, 'acknowledged'))
        self.assertFalse(self.lease.pending_receipt)
        await self.request({'lease': self.lease.nonce, 'session': self.lease.session,
                            'operation': 'uncertain', 'args': {}})
        await self.request({'lease': self.lease.nonce, 'session': self.lease.session,
                            'operation': 'ack', 'args': {}})
        self.assertTrue(self.lease.has_unknown)
        result = await self.call({'message_id': '101', 'text': 'not allowed'})
        self.assertEqual(result['status'], 'not_sent')
        self.sender.assert_not_awaited()

    async def test_http_direct_reply_mention_and_reaction(self):
        self.assertEqual((await self.request())[1]['status'], 'sent')
        status, result = await self.request({'lease': self.lease.nonce, 'session': self.lease.session,
            'operation': 'mention', 'args': {'user_ids': ['8', '9'], 'text': 'http mention'}})
        self.assertEqual((status, result['status']), (200, 'sent'))
        status, result = await self.request({'lease': self.lease.nonce, 'session': self.lease.session,
            'operation': 'react', 'args': {'message_id': '303', 'emoji_id': '76'}})
        self.assertEqual((status, result['status']), (200, 'reacted'))
        self.assertEqual(self.lease.sent_texts, ['http', 'http mention'])

    async def test_http_independent_secret_envelope_and_origin_guards(self):
        generic = {'error': 'invalid interaction bridge request'}
        for kwargs in ({'secret': 'video-or-card-secret'}, {'path': '/video'},
                       {'extra': 'Origin: http://127.0.0.1\r\n'},
                       {'extra': 'Transfer-Encoding: chunked\r\n'},
                       {'extra': 'authorization: duplicate\r\n'},
                       {'body': {'lease': self.lease.nonce, 'session': 'wrong', 'args': {}}},
                       {'body': {'lease': 'forged', 'session': self.lease.session, 'args': {}}},
                       {'body': {'lease': True, 'session': self.lease.session, 'args': {}}},
                       {'body': {'lease': self.lease.nonce, 'session': self.lease.session,
                                 'args': {}, 'recipient': 'group:43'}},
                       {'raw': b'{"lease":"first","lease":"duplicate"}'},
                       {'raw': b'{"args":NaN}'}, {'raw': b'not JSON'}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(await self.request(**kwargs), (400, generic))
        self.sender.assert_not_awaited()
        self.reactor.assert_not_awaited()

    async def test_http_authenticated_validation_errors_are_safe_and_actionable(self):
        body = {'lease': self.lease.nonce, 'session': self.lease.session,
                'args': {'message_id': '101', 'text': 'x' * 1201}}
        status, result = await self.request(body)
        self.assertEqual(status, 400)
        self.assertIn('1200', result['error'])
        self.assertIn('shorten', result['error'])
        self.assertLessEqual(len(result['error']), 300)
        self.assertNotIn('x' * 10, result['error'])
        body['session'] = 'wrong-session'
        self.assertEqual((await self.request(body))[1], {'error': 'invalid interaction bridge request'})
        private = self.service.open(event(message_type='private'), 'private:7', 'private', targets()[:1])
        status, result = await self.request({'lease': private.nonce, 'session': private.session,
            'operation': 'mention', 'args': {'user_ids': ['7'], 'text': 'no'}})
        self.assertEqual(status, 400)
        self.assertIn('current group', result['error'])
        self.sender.assert_not_awaited()

    async def test_http_disconnect_cancels_transport_unknown_and_no_retry(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def send(*_args):
            entered.set()
            try:
                await asyncio.wait_for(asyncio.Future(), 5)
            finally:
                cancelled.set()

        self.sender.side_effect = send
        parsed = urlsplit(self.service.url)
        _reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
        body = json.dumps({'lease': self.lease.nonce, 'session': self.lease.session,
                           'args': {'message_id': '101', 'text': 'disconnect'}}).encode()
        writer.write((f'POST /interaction HTTP/1.1\r\nAuthorization: Bearer {self.service.secret}\r\n'
                      f'Content-Length: {len(body)}\r\n\r\n').encode() + body)
        await writer.drain()
        await asyncio.wait_for(entered.wait(), 1)
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(cancelled.wait(), 1)
        await asyncio.wait_for(self.service.settle(self.lease), 3)
        self.assertTrue(self.lease.has_unknown)
        self.assertFalse(self.lease.has_confirmed)
        result = await self.call({'message_id': '101', 'text': 'disconnect'})
        self.assertEqual(result['status'], 'unknown')
        self.sender.assert_awaited_once()


    async def test_non_rejection_send_errors_are_unknown(self):
        for error in (OneBotError('offline'), OSError('socket'), ValueError('adapter failed')):
            lease = self.service.open(event(), 'group:42', 'transport-error', targets())
            self.sender.side_effect = error
            result = await self.call({'message_id': '101', 'text': 'uncertain'}, lease=lease)
            self.assertEqual(result['status'], 'unknown')
            self.assertTrue(lease.has_unknown)
            self.assertFalse(lease.has_confirmed)
            self.service.revoke(lease)

    async def test_http_rejects_duplicate_headers_large_deep_and_nonfinite_json(self):
        for extra in ('Content-Length: 1', 'Expect: 100-continue', 'Bad Header: value'):
            self.assertEqual((await self.request(extra=extra + chr(13) + chr(10)))[0], 400)
        for raw in (b'{}' * 40000, b'[' * 2000 + b']' * 2000, b'{"x":NaN}'):
            self.assertEqual((await self.request(raw=raw))[0], 400)
        self.sender.assert_not_awaited()

    async def test_http_started_action_expiry_returns_unknown_not_rejected(self):
        entered = asyncio.Event()

        async def send(*_args):
            entered.set()
            await asyncio.wait_for(asyncio.Future(), 5)

        self.sender.side_effect = send
        request = asyncio.create_task(self.request())
        await asyncio.wait_for(entered.wait(), 2)
        self.service.revoke(self.lease)
        status, result = await asyncio.wait_for(request, 2)
        self.assertEqual(status, 200)
        self.assertEqual(result['status'], 'unknown')
        self.assertTrue(self.lease.has_unknown)
        self.sender.assert_awaited_once()

    async def test_http_execution_timeout_cancels_inflight_action(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def send(*_args):
            entered.set()
            try:
                await asyncio.wait_for(asyncio.Future(), 5)
            finally:
                cancelled.set()

        self.sender.side_effect = send
        with patch('interaction_service.REQUEST_TIMEOUT', 0.5):
            status, result = await self.request()
        self.assertEqual(status, 200)
        self.assertEqual(result['status'], 'unknown')
        self.assertTrue(entered.is_set())
        self.assertTrue(cancelled.is_set())
        self.assertTrue(self.lease.has_unknown)

    async def test_open_enforces_total_lease_bound(self):
        with patch('interaction_service.MAX_LEASES', 1), self.assertRaises(ValueError):
            self.service.open(event(), 'group:42', 'overflow', targets())
        self.service.revoke(self.lease)
        with patch('interaction_service.MAX_LEASES', 1):
            self.service.open(event(), 'group:42', 'replacement', targets())


if __name__ == '__main__':
    unittest.main()
