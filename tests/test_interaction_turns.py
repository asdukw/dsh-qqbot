from __future__ import annotations

import asyncio
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from dsh_client import DSHClient, DSHConfig, DSHTurn  # noqa: E402
from group_context import GroupContextBuffer  # noqa: E402
from main import AppConfig, Bot  # noqa: E402
from onebot import MessageEvent, OneBotError, OneBotRejected  # noqa: E402
from proactive import ProactivePolicy  # noqa: E402


class InteractionTurnTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self):
        bot = object.__new__(Bot)
        bot.config = AppConfig()
        bot.config.video.enabled = False
        bot.config.bot.notify_errors = True
        bot.dsh = DSHClient(DSHConfig(request_timeout=5))
        bot.group_context = GroupContextBuffer(30)
        bot.is_admin = Mock(return_value=False)
        bot.ensure_card = AsyncMock(return_value=True)
        bot._persona = Mock()
        bot._session_for = Mock(return_value=('interaction-session', False))
        bot._prepare_persona = Mock()
        bot._recall_block = AsyncMock(return_value='')
        bot._opening_context = Mock(return_value='')
        bot._record_dsh_usage = Mock()
        bot._touch_session = Mock()
        bot._sticker_hint = Mock(return_value='')
        bot._task_hint = Mock(return_value='')
        bot._voice_hint = Mock(return_value='')
        bot._meme_hint = Mock(return_value='')
        bot.memory = SimpleNamespace(add=AsyncMock())
        bot.personas = SimpleNamespace(consume_opening=Mock())
        bot.proactive = ProactivePolicy(bot.config.bot.proactive)
        bot._own_messages = {}
        bot._card_names = ()
        bot._send = AsyncMock()
        bot._send_reply = AsyncMock(return_value=[{'message_id': 333}])
        bot.onebot = SimpleNamespace(
            send_interaction=AsyncMock(return_value={'message_id': -321}),
            set_msg_emoji_like=AsyncMock(return_value={}),
        )
        bot.interaction_service = None
        bot.card_service = None
        bot.video_service = None
        self.bots.append(bot)
        return bot

    async def asyncSetUp(self):
        self.bots = []
        self.event = MessageEvent('group', 7, 123, 'what do you think?', True, '同名', group_id=42)

    async def asyncTearDown(self):
        for bot in self.bots:
            for name in ('interaction_service', 'video_service', 'card_service'):
                service = getattr(bot, name, None)
                if service is not None:
                    await service.close()
            bot.dsh.close()

    def model(self, bot, calls=(), *, reply='', error='', after=None):
        loop = asyncio.get_running_loop()

        def run(session, prompt, images, env):
            self.assertNotIn('QQBOT_CARD_LEASE', env)
            self.assertNotIn('QQBOT_VIDEO_LEASE', env)
            service = bot.interaction_service
            for operation, args in calls:
                result = asyncio.run_coroutine_threadsafe(
                    service.call(env['QQBOT_INTERACTION_LEASE'], session, args, operation), loop,
                ).result(5)
                self.results.append(result)
            if after:
                loop.call_soon_threadsafe(after)
            return DSHTurn(reply, [], 'completed', error)

        self.results = []
        bot.dsh.run_authorized_result = Mock(side_effect=run)

    async def test_quotes_other_person_and_mentions_users_without_final_echo(self):
        bot = self.make_bot()
        bot.group_context.add(42, '同名', 'earlier', user_id=8, message_id=-122)
        self.model(bot, [('reply', {'message_id': '-122', 'text': 'reply to both', 'user_ids': ['7', '8']})],
                   reply='duplicate [定时: 每天提醒]')
        await bot._reply_dsh('group:42', self.event, self.event.text)
        bot.onebot.send_interaction.assert_awaited_once_with(
            'group', 42, 'reply to both', reply_to=-122, user_ids=(7, 8), timeout=ANY)
        bot._send_reply.assert_not_awaited()
        bot._send.assert_not_awaited()
        self.assertIn(-321, bot._own_messages[42])
        assistant = [c.args[2] for c in bot.memory.add.await_args_list if c.args[1] == 'assistant']
        self.assertEqual(assistant, ['reply to both'])
        self.assertIn('[消息ID:-122]', bot.dsh.run_authorized_result.call_args.args[1])
        self.assertEqual(bot.interaction_service.leases, {})

    async def test_reaction_only_empty_final_has_no_error_message(self):
        bot = self.make_bot()
        self.model(bot, [('react', {'message_id': '123', 'emoji_id': '76'})])
        await bot._reply_dsh('group:42', self.event, self.event.text)
        bot._send_reply.assert_not_awaited()
        bot._send.assert_not_awaited()
        self.assertFalse(bot._own_messages)
        self.assertEqual([c.args[1] for c in bot.memory.add.await_args_list], ['user'])

    async def test_reaction_with_final_is_sent_once(self):
        bot = self.make_bot()
        self.model(bot, [('react', {'message_id': '123', 'emoji_id': '124'})], reply='agreed')
        await bot._reply_dsh('group:42', self.event, self.event.text)
        bot._send_reply.assert_awaited_once_with(self.event, 'group:42', 'agreed', allow_silent=True)

    async def test_empty_final_without_confirmed_action_remains_failure(self):
        bot = self.make_bot()
        self.model(bot)
        await bot._reply_dsh('group:42', self.event, self.event.text)
        bot._send.assert_awaited_once_with(self.event, bot.config.bot.error_message)
        bot._send_reply.assert_not_awaited()

    async def test_rejection_can_fall_back_to_final(self):
        bot = self.make_bot()
        bot.onebot.send_interaction.side_effect = OneBotRejected('fixture')
        self.model(bot, [('reply', {'message_id': '123', 'text': 'try'})], reply='fallback')
        await bot._reply_dsh('group:42', self.event, self.event.text)
        self.assertEqual(self.results[0]['status'], 'rejected')
        bot._send_reply.assert_awaited_once_with(self.event, 'group:42', 'fallback')

    async def test_unknown_prevents_final_and_later_action(self):
        bot = self.make_bot()
        bot.onebot.send_interaction.side_effect = OneBotError('timeout')
        self.model(bot, [('reply', {'message_id': '123', 'text': 'maybe sent'}),
                         ('react', {'message_id': '123', 'emoji_id': '76'})], reply='do not send')
        await bot._reply_dsh('group:42', self.event, self.event.text)
        self.assertEqual(self.results[0]['status'], 'unknown')
        bot.onebot.set_msg_emoji_like.assert_not_awaited()
        bot._send_reply.assert_not_awaited()
        bot._send.assert_not_awaited()

    async def test_error_after_text_keeps_only_actual_text(self):
        bot = self.make_bot()
        self.model(bot, [('reply', {'message_id': '123', 'text': 'already sent'})],
                   reply='stale', error='fixture model error')
        await bot._reply_dsh('group:42', self.event, self.event.text)
        bot._send_reply.assert_not_awaited()
        bot._send.assert_not_awaited()
        self.assertEqual(bot.memory.add.await_args.args, ('group:42', 'assistant', 'already sent'))

    async def test_proactive_reaction_consumes_only_snapshot_and_counts_once(self):
        bot = self.make_bot()
        bot.group_context.add(42, '同名', 'old', user_id=8, message_id=122)
        bot.group_context.add(42, '同名', self.event.text, user_id=7, message_id=123)
        self.model(bot, [('react', {'message_id': '122', 'emoji_id': '76'})],
                   after=lambda: bot.group_context.add(42, 'new', 'new arrival', user_id=9, message_id=124))
        await bot._reply_proactive('group:42', self.event)
        self.assertEqual([line.message_id for line in bot.group_context.peek(42)], [124])
        self.assertEqual(bot.proactive.status(42, time.time())['hour_count'], 1)
        bot._send_reply.assert_not_awaited()

    async def test_failed_proactive_without_action_retains_buffer(self):
        bot = self.make_bot()
        bot.group_context.add(42, '同名', self.event.text, user_id=7, message_id=123)
        self.model(bot, error='fixture')
        await bot._reply_proactive('group:42', self.event)
        self.assertEqual(len(bot.group_context.peek(42)), 1)
        self.assertEqual(bot.proactive.status(42, time.time())['hour_count'], 0)

    async def test_private_and_synthetic_permissions_differ(self):
        bot = self.make_bot()
        event = MessageEvent('private', 7, -123, 'hello', False, 'name')
        self.model(bot, [('reply', {'message_id': '-123', 'text': 'private reply'})])
        await bot._reply_dsh('private:7', event, event.text)
        bot.onebot.send_interaction.assert_awaited_once()
        event.message_id = 0
        bot.dsh.run_with_events = Mock(return_value=('poke answer', []))
        await bot._reply_dsh('private:7', event, event.text)
        bot.dsh.run_with_events.assert_called_once()
        self.assertEqual(bot.dsh.run_authorized_result.call_count, 1)

    async def test_cancel_revokes_before_worker_exit(self):
        bot = self.make_bot()
        entered, finish = threading.Event(), threading.Event()

        def run(session, prompt, images, env):
            entered.set()
            finish.wait(5)
            with self.assertRaises(ValueError):
                bot.interaction_service.authorize(env['QQBOT_INTERACTION_LEASE'], session)
            return DSHTurn('late text', [])

        bot.dsh.run_authorized_result = Mock(side_effect=run)
        task = asyncio.create_task(bot._reply_dsh('group:42', self.event, self.event.text))
        try:
            await asyncio.to_thread(entered.wait, 3)
            self.assertTrue(entered.is_set())
            task.cancel()
            await asyncio.sleep(0)
            self.assertEqual(bot.interaction_service.leases, {})
            self.assertFalse(task.done())
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            bot._send_reply.assert_not_awaited()
        finally:
            finish.set()
            if not task.done():
                await asyncio.gather(task, return_exceptions=True)

    async def test_budgeted_prompt_and_authority_have_same_target_set(self):
        bot = self.make_bot()
        bot.config.bot.group_context_chars = 55
        bot.group_context.add(42, 'old', 'x' * 100, images=('hidden.png',), user_id=8, message_id=121)
        bot.group_context.add(42, 'new', 'ok', user_id=9, message_id=122)
        snapshot = bot._turn_snapshot(self.event, self.event.text)
        self.assertEqual({line.message_id for line in snapshot.targets}, {122, 123})
        prompt, _ = bot._turn_prompt(self.event, self.event.text, snapshot=snapshot)
        self.assertIn('[消息ID:122]', prompt)
        self.assertNotIn('[消息ID:121]', prompt)
        self.assertEqual(snapshot.images(4), [])
        self.assertEqual(bot.group_context.peek(42), [])

    async def test_only_hidden_final_after_reaction_does_not_send_ellipsis(self):
        bot = self.make_bot()
        bot.relations = None
        bot._send_reply = Bot._send_reply.__get__(bot, Bot)
        bot._send_voice = AsyncMock(return_value=False)
        bot._send_meme = AsyncMock(return_value=False)
        bot._send_sticker = AsyncMock(return_value=False)
        self.model(bot, [('react', {'message_id': '123', 'emoji_id': '76'})], reply='[好感+1]')
        await bot._reply_dsh('group:42', self.event, self.event.text)
        bot._send.assert_not_awaited()
        self.assertEqual([c.args[1] for c in bot.memory.add.await_args_list], ['user'])

    async def test_valid_negative_own_id_still_triggers_quote_reply(self):
        bot = self.make_bot()
        bot._remember_own_message(42, -987)
        bot.config.bot.reply_group_at = True
        bot._persona_names = Mock(return_value=())
        bot.personas.effective = Mock()
        quoted = MessageEvent('group', 7, 124, 'followup', False, 'name', group_id=42, reply_to=-987)
        self.assertTrue(bot._addresses_bot(quoted, quoted.text))
        bot._remember_own_message(42, 0)
        bot._remember_own_message(42, True)
        self.assertEqual(list(bot._own_messages[42]), [-987])

    async def test_missing_or_bool_receipt_is_unknown_not_confirmed(self):
        for receipt in ({}, {'message_id': True}, {'message_id': '12'}):
            with self.subTest(receipt=receipt):
                bot = self.make_bot()
                bot.onebot.send_interaction.return_value = receipt
                self.model(bot, [('reply', {'message_id': '123', 'text': 'maybe'})], reply='duplicate')
                await bot._reply_dsh('group:42', self.event, self.event.text)
                self.assertEqual(self.results[0]['status'], 'unknown')
                self.assertFalse(bot._own_messages)
                bot._send_reply.assert_not_awaited()

    async def test_partial_model_failure_after_proactive_action_counts_once(self):
        bot = self.make_bot()
        bot.group_context.add(42, 'name', 'old', user_id=8, message_id=122)
        self.model(bot, [('react', {'message_id': '122', 'emoji_id': '76'})], error='fixture')
        await bot._reply_proactive('group:42', self.event)
        self.assertEqual(bot.group_context.peek(42), [])
        self.assertEqual(bot.proactive.status(42, time.time())['hour_count'], 1)
        bot._send_reply.assert_not_awaited()

    async def test_runtime_turn_touch_is_counted_once_after_tool_action(self):
        bot = self.make_bot()
        self.model(bot, [('react', {'message_id': '123', 'emoji_id': '76'})], reply='yes')
        await bot._reply_dsh('group:42', self.event, self.event.text)
        bot._touch_session.assert_called_once_with('group:42')

    def test_plugin_is_registered_and_installed(self):
        root = Path(__file__).resolve().parents[1]
        destination = '/opt/qqbot/dsh/plugins/interaction-tools.mjs'
        self.assertIn(destination, (root / 'dsh/qqbot.cordis.patch.yml').read_text())
        self.assertIn(destination, (root / 'deploy/install-bot.sh').read_text())

    async def test_actual_model_error_after_reaction_is_partial_not_extra_chat(self):
        bot = self.make_bot()
        self.model(bot, [('react', {'message_id': '123', 'emoji_id': '76'})],
                   error='EMPTY_RESPONSE: DeepSeek Messages returned no content')
        with self.assertLogs('qqbot', level='ERROR') as logs:
            await bot._reply_dsh('group:42', self.event, self.event.text)
        self.assertIn('EMPTY_RESPONSE', '\n'.join(logs.output))
        bot._send_reply.assert_not_awaited()
        bot._send.assert_not_awaited()
        bot.onebot.set_msg_emoji_like.assert_awaited_once()
        bot._touch_session.assert_called_once()

    async def test_zero_configured_reply_limit_retains_hard_tool_bound(self):
        bot = self.make_bot()
        bot.config.bot.max_reply_chars = 0
        self.model(bot, [('reply', {'message_id': '123', 'text': 'normal text'})])
        await bot._reply_dsh('group:42', self.event, self.event.text)
        self.assertEqual(self.results[0]['status'], 'sent')
        bot._send.assert_not_awaited()

    async def test_cancel_after_confirmed_text_preserves_memory_without_final(self):
        bot = self.make_bot()
        loop = asyncio.get_running_loop()
        entered, finish = threading.Event(), threading.Event()

        def run(session, prompt, images, env):
            service = bot.interaction_service
            asyncio.run_coroutine_threadsafe(service.call(
                env['QQBOT_INTERACTION_LEASE'], session,
                {'message_id': '123', 'text': 'already delivered'}, 'reply'), loop).result(5)
            entered.set()
            finish.wait(5)
            return DSHTurn('not delivered', [{'type': 'assistant/message', 'data': {'usage': {'outputTokens': 12}}}])

        bot.dsh.run_authorized_result = Mock(side_effect=run)
        task = asyncio.create_task(bot._reply_dsh('group:42', self.event, self.event.text))
        try:
            await asyncio.to_thread(entered.wait, 3)
            self.assertTrue(entered.is_set())
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            bot._send_reply.assert_not_awaited()
            bot._send.assert_not_awaited()
            self.assertEqual(bot.memory.add.await_args.args,
                             ('group:42', 'assistant', 'already delivered'))
            bot._touch_session.assert_called_once()
            bot._record_dsh_usage.assert_called_once()
            self.assertEqual(bot._record_dsh_usage.call_args.args[:3],
                             ('group:42', 'interaction-session', 'chat'))
            self.assertEqual(bot._record_dsh_usage.call_args.args[3][0]['data']['usage']['outputTokens'], 12)
        finally:
            finish.set()
            if not task.done():
                await asyncio.gather(task, return_exceptions=True)

    async def test_uncertain_action_revokes_other_turn_authorities_before_model_continues(self):
        bot = self.make_bot()
        bot.is_admin.return_value = True
        bot.personas.effective = Mock(return_value=SimpleNamespace(token='persona-token'))
        bot.config.video.enabled = True
        loop = asyncio.get_running_loop()
        bot.onebot.send_interaction.side_effect = OneBotError('uncertain fixture')

        def run(session, prompt, images, env):
            service = bot.interaction_service
            result = asyncio.run_coroutine_threadsafe(service.call(
                env['QQBOT_INTERACTION_LEASE'], session,
                {'message_id': '123', 'text': 'uncertain'}, 'reply'), loop).result(5)
            self.assertEqual(result['status'], 'unknown')
            with self.assertRaises(ValueError):
                bot.card_service.authorize(env['QQBOT_CARD_LEASE'], session)
            with self.assertRaises(ValueError):
                bot.video_service.authorize(env['QQBOT_VIDEO_LEASE'], session)
            return DSHTurn('do not send', [])

        bot.dsh.run_authorized_result = Mock(side_effect=run)
        await bot._reply_dsh('group:42', self.event, self.event.text)
        bot._send_reply.assert_not_awaited()
        bot._send.assert_not_awaited()

    async def test_queued_authority_budget_begins_after_real_runtime_lock(self):
        bot = self.make_bot()
        bot.dsh.config.request_timeout = 0.2
        loop = asyncio.get_running_loop()
        created = asyncio.Event()
        original = bot._open_interactions
        captured = []

        async def open_interactions(*args):
            lease = await original(*args)
            captured.append(lease)
            created.set()
            return lease

        bot._open_interactions = open_interactions

        class Harness:
            def __init__(self, **kwargs):
                self.env = kwargs['env']

            def run(self, content, session_id):
                result = asyncio.run_coroutine_threadsafe(bot.interaction_service.call(
                    self.env['QQBOT_INTERACTION_LEASE'], session_id,
                    {'message_id': '123', 'emoji_id': '76'}, 'react'), loop).result(3)
                if result['status'] != 'reacted':
                    raise AssertionError(result)
                return SimpleNamespace(final_response='', finish_reason='completed', events=[])

            def close(self):
                pass

        bot.dsh._lock.acquire()
        task = None
        try:
            with patch('dsh_client.DeepSeekHarness', Harness):
                task = asyncio.create_task(bot._reply_dsh('group:42', self.event, self.event.text))
                await asyncio.wait_for(created.wait(), 2)
                elapsed = asyncio.Event()
                loop.call_later(0.3, elapsed.set)
                await asyncio.wait_for(elapsed.wait(), 2)
                self.assertIn(captured[0].nonce, bot.interaction_service.leases)
                self.assertGreater(captured[0].activation_lifetime, 0)
                bot.dsh._lock.release()
                await asyncio.wait_for(task, 3)
            bot.onebot.set_msg_emoji_like.assert_awaited_once()
            bot._send.assert_not_awaited()
            bot._send_reply.assert_not_awaited()
            self.assertEqual(captured[0].activation_lifetime, 0)
        finally:
            if bot.dsh._lock.locked():
                bot.dsh._lock.release()
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_repeated_cancel_during_final_memory_keeps_all_text_and_proactive_quota(self):
        bot = self.make_bot()
        bot.group_context.add(42, 'name', self.event.text, user_id=7, message_id=123)
        loop = asyncio.get_running_loop()
        generated, finish = threading.Event(), threading.Event()
        writing, release = asyncio.Event(), asyncio.Event()
        recorded = []

        async def add(conversation, role, text):
            if role == 'assistant' and not writing.is_set():
                writing.set()
                await asyncio.wait_for(release.wait(), 3)
            recorded.append((role, text))

        bot.memory.add.side_effect = add

        def run(session, prompt, images, env):
            for text in ('first confirmed', 'second confirmed'):
                asyncio.run_coroutine_threadsafe(bot.interaction_service.call(
                    env['QQBOT_INTERACTION_LEASE'], session,
                    {'message_id': '123', 'text': text}, 'reply'), loop).result(3)
            generated.set()
            finish.wait(5)
            return DSHTurn('', [], 'completed')

        bot.dsh.run_authorized_result = Mock(side_effect=run)
        task = asyncio.create_task(bot._reply_proactive('group:42', self.event))
        try:
            await asyncio.to_thread(generated.wait, 3)
            self.assertTrue(generated.is_set())
            task.cancel()
            finish.set()
            await asyncio.wait_for(writing.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
            self.assertEqual([text for role, text in recorded if role == 'assistant'],
                             ['first confirmed', 'second confirmed'])
            bot._touch_session.assert_called_once()
            self.assertEqual(bot.proactive.status(42, time.time())['hour_count'], 1)
            self.assertEqual(bot.group_context.peek(42), [])
            bot._send_reply.assert_not_awaited()
        finally:
            finish.set()
            release.set()
            if not task.done():
                await asyncio.gather(task, return_exceptions=True)

    async def test_native_tool_unknown_event_suppresses_final_even_without_server_action(self):
        bot = self.make_bot()
        bot.dsh.run_authorized_result = Mock(return_value=DSHTurn(
            'FINAL_AFTER_UNKNOWN', [], 'completed', interaction_unknown=True))
        await bot._reply_dsh('group:42', self.event, self.event.text)
        bot._send.assert_not_awaited()
        bot._send_reply.assert_not_awaited()
        bot.onebot.send_interaction.assert_not_awaited()
