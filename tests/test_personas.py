from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from commands import CommandRouter  # noqa: E402
from conversations import ConversationStore  # noqa: E402
from dsh_client import DSHError  # noqa: E402
from group_context import GroupContextBuffer  # noqa: E402
from main import AppConfig, Bot  # noqa: E402
from memory import Memory  # noqa: E402
from onebot import MessageEvent, OneBotError, OneBotRejected  # noqa: E402
from personas import PersonaStore, target_of  # noqa: E402
from sessions import SessionStore  # noqa: E402

from cards import CardRegistry, normalise  # noqa: E402


def payload(name: str, opening: str = '') -> dict:
    return normalise({'spec': 'chara_card_v3', 'spec_version': '3.0', 'data': {
        'name': name, 'description': f'Only {name} persona', 'first_mes': opening,
        'character_book': {'entries': [{
            'id': 0, 'keys': ['secret'], 'content': f'{name} world secret',
            'enabled': True, 'insertion_order': 0,
        }]},
    }})


class PersonaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cards = CardRegistry(root / 'cards.db')
        self.a, _ = self.cards.install_payload(payload('Default'))
        self.b, _ = self.cards.install_payload(payload('Alice', 'Get out.\r\n*bumps* [定时: do not execute]'))
        self.c, _ = self.cards.install_payload(payload('Third', 'Hello {{user}} from {{char}}'))
        self.cards.use(self.a.id)
        self.store = PersonaStore(root / 'cards.db', self.cards)
        self.memory = Memory(root / 'memory.db')
        self.sessions = SessionStore(root / 'memory.db', daily_hour=-1)
        self.conversations = ConversationStore(root / 'profile.db')
        self.bot = object.__new__(Bot)
        self.bot.config = AppConfig()
        self.bot.config.bot.admin_qq_ids = (1,)
        self.bot.config.bot.max_reply_chars = 1200
        self.bot.personas = self.store
        self.bot.cards = self.cards
        self.bot.memory = self.memory
        self.bot.sessions = self.sessions
        self.bot.conversations = self.conversations
        self.bot.group_context = GroupContextBuffer(10)
        self.bot._locks = {}
        self.bot._global = asyncio.Semaphore(3)
        self.bot.proactive = SimpleNamespace(note_speaking=Mock())
        self.bot._remember_own_message = Mock()
        self.bot.dsh = SimpleNamespace(set_system_prompt=Mock())
        self.sends: list[tuple[str, int, str]] = []

        async def group(chat_id: int, text: str) -> dict:
            self.sends.append(('group', chat_id, text))
            return {'message_id': len(self.sends)}

        async def private(chat_id: int, text: str) -> dict:
            self.sends.append(('private', chat_id, text))
            return {'message_id': len(self.sends)}

        self.bot.onebot = SimpleNamespace(send_group=group, send_private=private)

    async def asyncTearDown(self) -> None:
        interaction = getattr(self.bot, 'interaction_service', None)
        if interaction is not None:
            await interaction.close()
        video = getattr(self.bot, 'video_service', None)
        if video is not None:
            await video.close()
        self.conversations.close()
        self.sessions.close()
        self.memory.close()
        self.store.close()
        self.cards.close()
        self.tmp.cleanup()

    def event(self, group: int = 42, user: int = 1) -> MessageEvent:
        return MessageEvent('group', user, 1, 'hello', True, 'member', group_id=group)

    async def test_switch_order_original_text_and_other_chats_unchanged(self) -> None:
        await self.memory.add('group:42', 'assistant', 'old secret identity')
        before = self.bot._persona('group:42')
        other = self.bot._persona('group:43')
        old_session = self.sessions.info('group:42')
        self.bot.group_context.add(42, 'someone', 'old buffered text')
        self.bot.group_context.add(43, 'someone', 'other buffered text')
        job = await self.bot.switch_card(self.event(), self.b)
        self.assertEqual(job['phase'], 'complete')
        self.assertEqual([x[1] for x in self.sends], [42, 42])
        self.assertIn('即将切换', self.sends[0][2])
        self.assertEqual(self.sends[1][2], self.b.card.data['first_mes'])
        self.assertEqual(len(json.loads(job['receipts'])), 2)
        self.assertEqual(self.cards.active().id, self.a.id)
        self.assertEqual(self.bot._persona('group:43').token, other.token)
        self.assertEqual(self.bot._persona('private:1').record.id, self.a.id)
        current = self.bot._persona('group:42')
        self.assertNotEqual(current.token, before.token)
        self.assertEqual(self.sessions.info('group:42').generation, old_session.generation + 1)
        self.assertFalse(self.bot.group_context.peek(42))
        self.assertTrue(self.bot.group_context.peek(43))
        self.bot.dsh.set_system_prompt.assert_not_called()
        self.assertEqual(await self.memory.history('group:42', 10, after_id=current.cutoff), [])
        self.assertEqual(await self.memory.recall('group:42', 'secret', after_id=current.cutoff), [])
        self.assertEqual(await self.memory.count('group:42'), 1)

    async def test_story_prepared_before_binding_and_public_synopsis(self) -> None:
        from story_models import StorySettings
        from story_runtime import StoryRuntime
        from story_store import StoryStore
        from test_story import definition
        store = StoryStore(Path(self.tmp.name) / 'story.db')
        self.bot.client = SimpleNamespace()
        self.bot.story = StoryRuntime(self.bot, store, StorySettings(enabled=True))
        generator = SimpleNamespace(client=self.bot.client, settings=self.bot.story.settings,
                                    outline=AsyncMock(side_effect=ValueError('invalid JSON')))
        self.bot.story._generator = generator
        try:
            failed = await self.bot.switch_card(self.event(), self.b)
            self.assertEqual(failed['phase'], 'failed')
            self.assertEqual(self.store.effective('group:42').record.id, self.a.id)
            self.assertEqual(self.sends, [])
            generator.outline.side_effect = None
            generator.outline.return_value = definition()
            completed = await self.bot.switch_card(self.event(), self.b)
            self.assertEqual(completed['phase'], 'complete')
            self.assertEqual(self.sends[1][2], self.b.card.data['first_mes'])
            self.assertIn('序幕', self.sends[2][2])
            self.assertIn('/story start', self.sends[2][2])
            self.assertTrue(completed['story_prepared'])
            await self.bot.switch_card(self.event(), self.c)
            self.sends.clear()
            await self.bot.switch_card(self.event(), self.b)
            self.assertEqual(len(self.sends), 2)
            self.assertIn('返回', self.sends[-1][2])
        finally:
            store.close()

    async def test_same_card_noop_and_repeat_switch_does_not_rotate(self) -> None:
        job = await self.bot.switch_card(self.event(), self.a)
        self.assertEqual(job['phase'], 'complete')
        self.assertEqual(self.sends, [])
        await self.bot.switch_card(self.event(), self.b)
        generation = self.sessions.info('group:42').generation
        await self.bot.switch_card(self.event(), self.b)
        self.assertEqual(len(self.sends), 2)
        self.assertEqual(self.sessions.info('group:42').generation, generation)

    async def test_waits_for_complete_old_reply(self) -> None:
        guard = self.bot._locks.setdefault('group:42', asyncio.Lock())
        await guard.acquire()
        task = asyncio.create_task(self.bot.switch_card(self.event(), self.b))
        await asyncio.sleep(0)
        self.assertFalse(self.sends)
        self.sends.append(('group', 42, 'last old reply'))
        guard.release()
        await task
        self.assertEqual(self.sends[0][2], 'last old reply')
        self.assertIn('即将切换', self.sends[1][2])

    async def test_prompt_and_world_are_scoped_and_missing_identity_fails(self) -> None:
        await self.bot.switch_card(self.event(), self.b)
        self.bot._prepare_persona('group:42', 'group-42-g1', self.event(), 'secret')
        self.bot._prepare_persona('group:43', 'group-43', self.event(43), 'secret')
        conn = sqlite3.connect(Path(self.tmp.name) / 'profile.db')
        try:
            prompts = dict(conn.execute('SELECT session_id, prompt FROM session_personas'))
        finally:
            conn.close()
        self.assertIn('Alice world secret', prompts['group-42-g1'])
        self.assertNotIn('Default world secret', prompts['group-42-g1'])
        self.assertIn('Default world secret', prompts['group-43'])
        self.assertNotIn('Alice world secret', prompts['group-43'])
        self.bot.conversations = None
        with self.assertRaises(DSHError):
            self.bot._prepare_persona('group:42', 'group-42-g1', self.event(), 'secret')

    async def test_unknown_announcement_never_binds_or_auto_replays(self) -> None:
        self.bot.onebot.send_group = AsyncMock(side_effect=OneBotError('timeout'))
        job = await self.bot.switch_card(self.event(), self.b)
        self.assertEqual(job['phase'], 'unknown')
        self.assertEqual(self.store.effective('group:42').record.id, self.a.id)
        self.store.recover()
        self.assertEqual(self.store.queued(), [])
        with self.assertRaises(ValueError):
            self.bot._persona('group:42')
        self.assertEqual(self.bot._persona('group:43').record.id, self.a.id)

    async def test_explicit_announcement_rejection_is_failed_not_unknown(self) -> None:
        self.bot.onebot.send_group = AsyncMock(side_effect=OneBotRejected('rejected'))
        job = await self.bot.switch_card(self.event(), self.b)
        self.assertEqual(job['phase'], 'failed')
        self.assertEqual(self.store.effective('group:42').record.id, self.a.id)
        self.assertIsNone(self.store.pending('group:42'))

    async def test_failed_opening_keeps_binding_then_explicit_resume(self) -> None:
        self.bot.onebot.send_group = AsyncMock(side_effect=[{'message_id': 7}, OneBotRejected('rejected')])
        job = await self.bot.switch_card(self.event(), self.b)
        self.assertEqual(job['phase'], 'paused')
        self.assertEqual(self.store.effective('group:42').record.id, self.b.id)
        self.store.resume(job['id'])
        with self.assertRaises(ValueError):
            self.bot._persona('group:42')
        self.bot.onebot.send_group = AsyncMock(return_value={'message_id': 8})
        await self.bot._execute_switch(job['id'])
        self.bot.onebot.send_group.assert_awaited_once_with(42, self.b.card.data['first_mes'])
        self.assertEqual(self.store.job(job['id'])['phase'], 'complete')
        self.assertEqual(self.sessions.info('group:42').generation, 1)

    async def test_receiptless_acceptance_is_unknown(self) -> None:
        self.bot.onebot.send_group = AsyncMock(return_value={})
        job = await self.bot.switch_card(self.event(), self.b)
        self.assertEqual(job['phase'], 'unknown')
        self.assertEqual(self.store.effective('group:42').record.id, self.a.id)

    async def test_opening_context_one_time_and_reset_keeps_binding(self) -> None:
        await self.bot.switch_card(self.event(), self.c)
        self.assertIn('Hello 大家 from Third', self.bot._opening_context('group:42'))
        before = self.bot._persona('group:42')
        self.sessions.reset('group:42')
        self.assertEqual(self.bot._persona('group:42').token, before.token)
        self.assertEqual(self.bot._persona('group:42').cutoff, before.cutoff)
        self.store.consume_opening('group:42')
        self.assertEqual(self.bot._opening_context('group:42'), '')

    async def test_chunks_are_recorded_and_not_resent(self) -> None:
        self.bot.config.bot.max_reply_chars = 10
        responses = [{'message_id': 1}, {'message_id': 2}, OneBotError('timeout')]
        self.bot.onebot.send_group = AsyncMock(side_effect=responses)
        job = await self.bot.switch_card(self.event(), self.b)
        self.assertEqual(job['phase'], 'unknown')
        receipts = json.loads(job['receipts'])
        self.assertEqual([r.get('index') for r in receipts if r['kind'] == 'opening'], [0])
        self.store.resume(job['id'])
        self.bot.onebot.send_group = AsyncMock(return_value={'message_id': 10})
        await self.bot._execute_switch(job['id'])
        texts = [call.args[1] for call in self.bot.onebot.send_group.await_args_list]
        self.assertEqual(''.join(texts), self.b.card.data['first_mes'][10:])

    async def test_queue_dedupe_and_referenced_card_deletion(self) -> None:
        one = self.store.enqueue('group:42', self.b, actor='cli', request_id='test-id')
        two = self.store.enqueue('group:42', self.b, actor='cli', request_id='test-id')
        self.assertEqual(one['id'], two['id'])
        with self.assertRaises(ValueError):
            self.store.enqueue('group:42', self.c, actor='cli')
        with self.assertRaises(ValueError):
            self.cards.delete(self.b.id)
        with self.assertRaises(ValueError):
            self.store.enqueue('group:43', self.b, actor='cli', request_id='test-id')

    async def test_reconcile_is_idempotent_and_legacy_adoption_keeps_generation(self) -> None:
        state, _ = self.sessions.current('group:42', is_group=True)
        self.bot._persona('group:42')
        self.assertEqual(self.sessions.info('group:42').generation, state.generation)
        await self.bot.switch_card(self.event(), self.b)
        after = self.sessions.info('group:42')
        for _ in range(3):
            self.bot._persona('group:42')
        self.assertEqual(self.sessions.info('group:42'), after)

    async def test_memory_fts_and_like_boundaries(self) -> None:
        await self.memory.add('group:42', 'assistant', 'old abc 中文')
        cutoff = self.memory.last_id('group:42')
        await self.memory.add('group:42', 'assistant', 'new abc 中文')
        self.assertEqual(len(await self.memory.recall('group:42', 'abc', after_id=cutoff)), 1)
        self.assertEqual(len(await self.memory.recall('group:42', '中文', after_id=cutoff)), 1)
        history = await self.memory.history('group:42', 10, after_id=cutoff)
        self.assertEqual([item['content'] for item in history], ['new abc 中文'])

    async def test_non_admin_cannot_request_group_cutover(self) -> None:
        with self.assertRaises(ValueError):
            await self.bot.switch_card(self.event(user=9), self.b)
        self.assertFalse(self.sends)
        self.assertEqual(self.store.queued(), [])

    def private_event(self, user: int = 9) -> MessageEvent:
        return MessageEvent('private', user, 1, 'hello', False, 'player')

    async def test_private_player_switches_via_router_and_isolates_history(self) -> None:
        self.bot.send_text = AsyncMock()
        router = CommandRouter(self.bot)
        await self.memory.add('private:9', 'assistant', 'old secret identity')
        old = self.bot._persona('private:9')
        generation = self.sessions.info('private:9').generation
        others = {key: self.bot._persona(key).token for key in ('private:10', 'group:42')}
        await router.handle(self.private_event(), f'/card use {self.b.id}')
        current = self.bot._persona('private:9')
        self.assertEqual(current.record.id, self.b.id)
        self.assertNotEqual(current.token, old.token)
        self.assertEqual([(scope, chat) for scope, chat, _ in self.sends], [('private', 9), ('private', 9)])
        self.assertIn('即将切换', self.sends[0][2])
        self.assertEqual(self.sends[1][2], self.b.card.data['first_mes'])
        self.assertEqual(self.sessions.info('private:9').generation, generation + 1)
        self.assertEqual(self.cards.active().id, self.a.id)
        self.assertEqual({key: self.bot._persona(key).token for key in others}, others)
        self.assertEqual(await self.memory.history('private:9', 10, after_id=current.cutoff), [])
        self.assertEqual(await self.memory.recall('private:9', 'secret', after_id=current.cutoff), [])
        self.bot._prepare_persona('private:9', 'private-9-test', self.private_event(), 'secret')
        self.bot._prepare_persona('private:10', 'private-10-test', self.private_event(10), 'secret')
        conn = sqlite3.connect(Path(self.tmp.name) / 'profile.db')
        try:
            prompts = dict(conn.execute('SELECT session_id, prompt FROM session_personas'))
        finally:
            conn.close()
        self.assertIn('Alice world secret', prompts['private-9-test'])
        self.assertNotIn('Default world secret', prompts['private-9-test'])
        self.assertIn('Default world secret', prompts['private-10-test'])
        await router.handle(self.private_event(), f'/cards switch {self.b.id}')
        self.assertEqual(len(self.sends), 2)
        self.assertEqual(self.sessions.info('private:9').generation, generation + 1)
        self.bot.send_text.assert_awaited_once()

    async def test_switch_rejects_missing_or_unknown_event_type_before_enqueue(self) -> None:
        for user in (1, 9):
            for kind in (None, 'unknown', ''):
                with self.subTest(user=user, kind=kind):
                    event = SimpleNamespace(user_id=user)
                    if kind is not None:
                        event.message_type = kind
                    with self.assertRaises(ValueError):
                        await self.bot.switch_card(event, self.b)
        self.assertEqual(self.store.queued(), [])
        self.assertEqual(self.sends, [])

    async def test_worker_allows_self_private_and_cli_tasks(self) -> None:
        for conversation, actor in (('private:9', '9'), ('group:42', 'cli')):
            with self.subTest(conversation=conversation, actor=actor):
                job = self.store.enqueue(conversation, self.b, actor=actor)
                await self.bot._execute_switch(job['id'])
                self.assertEqual(self.store.job(job['id'])['phase'], 'complete')
                self.assertEqual(self.store.effective(conversation).record.id, self.b.id)
        self.assertEqual([(scope, chat) for scope, chat, _ in self.sends],
                         [('private', 9), ('private', 9), ('group', 42), ('group', 42)])

    async def test_worker_rejects_cross_user_group_and_invalid_actor_without_side_effects(self) -> None:
        self.bot.story = SimpleNamespace(prepare_switch=AsyncMock())
        for conversation, actor in (('private:10', '9'), ('group:42', '9'),
                                    ('private:9', 'invalid'), ('private:9', ''), ('private:9', '0')):
            with self.subTest(conversation=conversation, actor=actor):
                job = self.store.enqueue(conversation, self.b, actor=actor)
                await self.bot._execute_switch(job['id'])
                self.assertEqual(self.store.job(job['id'])['phase'], 'failed')
                self.assertEqual(self.store.effective(conversation).record.id, self.a.id)
        self.bot.story.prepare_switch.assert_not_awaited()
        self.assertEqual(self.sends, [])

    async def test_worker_rechecks_group_admin_after_revocation(self) -> None:
        job = self.store.enqueue('group:42', self.b, actor='1')
        self.bot.config.bot.admin_qq_ids = ()
        await self.bot._execute_switch(job['id'])
        self.assertEqual(self.store.job(job['id'])['phase'], 'failed')
        self.assertEqual(self.store.effective('group:42').record.id, self.a.id)
        self.assertEqual(self.sends, [])

    async def test_private_player_story_prepares_before_binding_and_resumes_own_save(self) -> None:
        from story_models import StorySettings
        from story_runtime import StoryRuntime
        from story_store import StoryStore
        from test_story import definition
        store = StoryStore(Path(self.tmp.name) / 'story.db')
        self.bot.client = SimpleNamespace()
        self.bot.story = StoryRuntime(self.bot, store, StorySettings(enabled=True))
        generator = SimpleNamespace(client=self.bot.client, settings=self.bot.story.settings,
                                    outline=AsyncMock(side_effect=ValueError('invalid JSON')),
                                    scene=AsyncMock(return_value='雨落在书店门前。'))
        self.bot.story._generator = generator
        try:
            failed = await self.bot.switch_card(self.private_event(), self.b)
            self.assertEqual(failed['phase'], 'failed')
            self.assertEqual(self.store.effective('private:9').record.id, self.a.id)
            self.assertEqual(self.sends, [])
            generator.outline.side_effect = None
            generator.outline.return_value = definition()
            completed = await self.bot.switch_card(self.private_event(), self.b)
            self.assertEqual(completed['phase'], 'complete')
            self.assertIn('序幕', self.sends[-1][2])
            await self.bot.story.enter(self.private_event())
            relation = store.relation('private:9', self.b.character_id, '9')
            run = store.run(relation['id'])
            assert run is not None
            menu = store.current_menu(run['id'])
            assert menu is not None
            await self.bot.story.choose(self.private_event(), menu['id'], 1)
            saved = store.snapshot(run['id'])
            other_before = self.store.effective('private:10').token
            await self.bot.switch_card(self.private_event(), self.c)
            await self.bot.story.enter(self.private_event())
            other_character = store.relation('private:9', self.c.character_id, '9')
            other_run = store.run(other_character['id'])
            assert other_run is not None
            self.assertNotEqual(other_run['id'], run['id'])
            self.sends.clear()
            await self.bot.switch_card(self.private_event(), self.b)
            self.assertEqual(len(self.sends), 2)
            self.assertIn('返回', self.sends[-1][2])
            await self.bot.story.enter(self.private_event())
            restored = store.snapshot(run['id'])
            resumed_run = store.run(relation['id'])
            assert resumed_run is not None
            self.assertEqual(resumed_run['id'], run['id'])
            self.assertEqual(restored['relation'], saved['relation'])
            self.assertEqual(restored['state_model'], saved['state_model'])
            resumed_menu = store.current_menu(run['id'])
            assert resumed_menu is not None
            self.assertNotEqual(resumed_menu['id'], menu['id'])
            with self.assertRaises(ValueError):
                await self.bot.story.choose(self.private_event(), menu['id'], 1)
            self.assertTrue(all(scope == 'private' and chat == 9 for scope, chat, _ in self.sends))
            self.assertEqual(self.store.effective('private:10').token, other_before)
        finally:
            store.close()

    async def test_private_player_unknown_delivery_requires_explicit_resume(self) -> None:
        self.bot.onebot.send_private = AsyncMock(side_effect=OneBotError('timeout'))
        job = await self.bot.switch_card(self.private_event(), self.b)
        self.assertEqual(job['actor'], '9')
        self.assertEqual(job['phase'], 'unknown')
        self.assertEqual(self.store.effective('private:9').record.id, self.a.id)
        self.store.recover()
        self.assertEqual(self.store.queued(), [])
        self.bot.onebot.send_private.assert_awaited_once()
        self.store.resume(job['id'])
        self.bot.onebot.send_private = AsyncMock(return_value={'message_id': 9})
        await self.bot._execute_switch(job['id'])
        self.assertEqual(self.store.job(job['id'])['phase'], 'complete')
        self.assertEqual(self.bot.onebot.send_private.await_count, 2)
        self.assertTrue(all(call.args[0] == 9 for call in self.bot.onebot.send_private.await_args_list))
        self.assertEqual(self.store.effective('private:9').record.id, self.b.id)

    async def test_tool_switch_finishes_old_reply_before_publication_without_deadlock(self) -> None:
        from card_service import CardService
        from commands import CommandRouter
        event = self.private_event(1)
        event.text = '切换角色'
        self.bot.commands = CommandRouter(self.bot)
        self.bot._names = {}
        self.bot.relations = None
        self.bot._log_chat_event = Mock()
        self.bot._maybe_reload_model = AsyncMock()
        service = self.bot.card_service = CardService(self.bot)
        lease = service.open(event, 'private:1', 's1', event.text, self.bot._persona('private:1').token)

        async def reply(conversation, original, text):
            self.assertTrue(self.bot._locks[conversation].locked())
            service.library(lease, {'action': 'use', 'query': str(self.b.id)})
            self.assertEqual(self.store.queued(), [])
            self.assertIsNone(self.store.pending(conversation))
            await self.bot._send_reply(original, conversation, '旧角色回复完毕', strict_receipts=True)
            service.revoke(lease)
            return lease.deferred

        self.bot._reply = reply
        try:
            await asyncio.wait_for(self.bot.handle(event), timeout=3)
            self.assertEqual(len(self.sends), 1)
            self.assertEqual(self.store.effective('private:1').record.id, self.a.id)
            queued = self.store.queued()
            self.assertEqual(len(queued), 1)
            await self.bot._execute_switch(queued[0]['id'])
            self.assertEqual(self.sends[0][2], '旧角色回复完毕')
            self.assertIn('即将切换', self.sends[1][2])
            self.assertEqual(self.store.effective('private:1').record.id, self.b.id)
        finally:
            await service.close()

    async def test_strict_tool_reply_requires_every_text_receipt_and_never_uses_voice(self) -> None:
        self.bot.relations = None
        self.bot.config.bot.max_reply_chars = 8
        self.bot._send_voice = AsyncMock(return_value=True)
        for failure in (OneBotError('timeout'), {}):
            self.bot.onebot.send_private = AsyncMock(side_effect=[{'message_id': 1}, failure])
            with self.subTest(failure=failure), self.assertRaises(OneBotError):
                await self.bot._send_reply(self.private_event(1), 'private:1',
                                           '旧角色还有很长很长的回复不能截断[语音]', strict_receipts=True)
            self.assertEqual(self.bot.onebot.send_private.await_count, 2)
        self.bot._send_voice.assert_not_awaited()
        self.assertEqual(self.store.queued(), [])

    async def test_dsh_send_failure_discards_tool_deferred_and_revokes_lease(self) -> None:
        from card_service import CardService
        from dsh_client import DSHClient, DSHConfig, DSHTurn
        self.bot.card_service = service = CardService(self.bot)
        self.bot.dsh = DSHClient(DSHConfig())
        self.bot.ensure_card = AsyncMock(return_value=True)
        self.bot._session_for = Mock(return_value=('s1', False))
        self.bot._prepare_persona = Mock()
        self.bot._turn_images = Mock(return_value=[])
        self.bot._turn_prompt = Mock(return_value=('switch please', 0))
        self.bot._recall_block = AsyncMock(return_value='')
        self.bot._record_dsh_usage = Mock()
        self.bot._touch_session = Mock()
        self.bot._send_reply = AsyncMock(side_effect=OneBotError('unknown delivery'))

        def run(session, text, images, env):
            lease = service.authorize(env['QQBOT_CARD_LEASE'], session)
            service.library(lease, {'action': 'use', 'query': str(self.b.id)})
            return DSHTurn('已受理，回复后切换', [], 'completed')

        self.bot.dsh.run_authorized_result = Mock(side_effect=run)
        try:
            deferred = await self.bot._reply_dsh('private:1', self.private_event(1), '切换角色')
            self.assertIsNone(deferred)
            self.assertEqual(service.leases, {})
            self.assertEqual(self.store.queued(), [])
            self.bot._send_reply.assert_awaited_once()
            self.assertTrue(self.bot._send_reply.await_args.kwargs['strict_receipts'])
        finally:
            await service.close()

    async def test_cancelled_dsh_revokes_lease_before_worker_finishes(self) -> None:
        import threading

        from card_service import CardService
        from dsh_client import DSHClient, DSHConfig, DSHTurn
        self.bot.card_service = service = CardService(self.bot)
        self.bot.dsh = DSHClient(DSHConfig())
        self.bot.ensure_card = AsyncMock(return_value=True)
        self.bot._session_for = Mock(return_value=('s1', False))
        self.bot._prepare_persona = Mock()
        self.bot._turn_images = Mock(return_value=[])
        self.bot._turn_prompt = Mock(return_value=('switch', 0))
        self.bot._recall_block = AsyncMock(return_value='')
        entered, finish = threading.Event(), threading.Event()
        captured = {}

        def run(session, text, images, env):
            captured.update(env)
            entered.set()
            finish.wait(5)
            with self.assertRaises(ValueError):
                service.authorize(env['QQBOT_CARD_LEASE'], session)
            return DSHTurn('late reply', [])

        self.bot.dsh.run_authorized_result = Mock(side_effect=run)
        task = asyncio.create_task(self.bot._reply_dsh('private:1', self.private_event(1), '切换角色'))
        try:
            await asyncio.to_thread(entered.wait, 3)
            self.assertTrue(entered.is_set())
            task.cancel()
            await asyncio.sleep(0)
            self.assertEqual(service.leases, {})
            self.assertFalse(task.done())
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(self.store.queued(), [])
        finally:
            finish.set()
            await service.close()

    async def test_video_tools_ordinary_admin_and_synthetic_turn_authority(self) -> None:
        from dsh_client import DSHClient, DSHConfig, DSHTurn
        self.bot.dsh = DSHClient(DSHConfig())
        self.bot.ensure_card = AsyncMock(return_value=True)
        self.bot._session_for = Mock(return_value=('video-session', False))
        self.bot._prepare_persona = Mock()
        self.bot._turn_images = Mock(return_value=[])
        self.bot._turn_prompt = Mock(return_value=('video please', 0))
        self.bot._recall_block = AsyncMock(return_value='')
        self.bot._record_dsh_usage = Mock()
        self.bot._touch_session = Mock()
        self.bot._send_reply = AsyncMock()
        captured = []

        def authorized(session, text, images, env):
            captured.append(env)
            self.bot.video_service.authorize(env['QQBOT_VIDEO_LEASE'], session)
            return DSHTurn('metadata only', [])

        self.bot.dsh.run_authorized_result = Mock(side_effect=authorized)
        self.bot.dsh.run_with_events = Mock(return_value=('ordinary', []))
        try:
            await self.bot._reply_dsh('private:2', self.private_event(2), '解析视频')
            self.assertIn('QQBOT_VIDEO_LEASE', captured[-1])
            self.assertNotIn('QQBOT_CARD_LEASE', captured[-1])
            self.assertEqual(self.bot.video_service.leases, {})
            await self.bot._reply_dsh('private:1', self.private_event(1), '解析视频')
            self.assertIn('QQBOT_VIDEO_LEASE', captured[-1])
            self.assertIn('QQBOT_CARD_LEASE', captured[-1])
            synthetic = self.private_event(2)
            synthetic.message_id = 0
            await self.bot._reply_dsh('private:2', synthetic, 'poke')
            self.assertEqual(len(captured), 2)
            self.bot.dsh.run_with_events.assert_called_once()
        finally:
            cards = getattr(self.bot, 'card_service', None)
            if cards is not None:
                await cards.close()

    async def test_video_receipt_updates_group_state_and_rejects_missing_receipt(self) -> None:
        self.bot.onebot.send_private_video = AsyncMock(return_value={'message_id': 11})
        self.bot.onebot.send_group_video = AsyncMock(return_value={'message_id': 12})
        result = await self.bot._send_video_receipt('private:7', 'file:///app/qqbot-video/a.mp4', 9)
        self.assertEqual(result['message_id'], 11)
        self.bot.onebot.send_private_video.assert_awaited_once_with(7, 'file:///app/qqbot-video/a.mp4', timeout=9)
        await self.bot._send_video_receipt('group:42', 'file:///app/qqbot-video/b.mp4', 8)
        self.bot._remember_own_message.assert_called_with(42, 12)
        self.bot.proactive.note_speaking.assert_called_once()
        self.bot.onebot.send_group_video.return_value = {}
        with self.assertRaises(OneBotError):
            await self.bot._send_video_receipt('group:42', 'file:///app/qqbot-video/c.mp4', 8)
        self.bot._remember_own_message.assert_called_once()

    async def test_interaction_text_receipts_can_ack_deferred_switch_but_reaction_cannot(self):
        from card_service import CardService
        from dsh_client import DSHClient, DSHConfig, DSHTurn
        self.bot.card_service = service = CardService(self.bot)
        self.bot.config.video.enabled = False
        self.bot.dsh = DSHClient(DSHConfig())
        self.bot.ensure_card = AsyncMock(return_value=True)
        self.bot._session_for = Mock(return_value=('social-switch', False))
        self.bot._turn_prompt = Mock(return_value=('switch', 0))
        self.bot._prepare_persona = Mock()
        self.bot._recall_block = AsyncMock(return_value='')
        self.bot._record_dsh_usage = Mock()
        self.bot._touch_session = Mock()
        self.bot._send_reply = AsyncMock(return_value=[])
        self.bot._send = AsyncMock()
        self.bot.onebot.send_interaction = AsyncMock(return_value={'message_id': -99})
        self.bot.onebot.set_msg_emoji_like = AsyncMock(return_value={})
        loop = asyncio.get_running_loop()
        operation = 'reply'
        final = ''
        termination = 'completed'

        def run(session, prompt, images, env):
            card = service.authorize(env['QQBOT_CARD_LEASE'], session)
            service.library(card, {'action': 'use', 'query': str(self.b.id)})
            interactions = self.bot.interaction_service
            args = {'message_id': '1', 'text': 'switch acknowledged'} if operation == 'reply' else {
                'message_id': '1', 'emoji_id': '76'}
            asyncio.run_coroutine_threadsafe(interactions.call(
                env['QQBOT_INTERACTION_LEASE'], session, args, operation), loop).result(5)
            return DSHTurn(final, [], termination)

        self.bot.dsh.run_authorized_result = Mock(side_effect=run)
        try:
            event = self.private_event(1)
            deferred = await self.bot._reply_dsh('private:1', event, '切换角色')
            self.assertIsNotNone(deferred)
            self.assertEqual(self.store.queued(), [])
            self.bot._send_reply.assert_not_awaited()
            termination = 'max-tokens'
            deferred = await self.bot._reply_dsh('private:1', event, '切换角色')
            self.assertIsNone(deferred)
            termination = 'completed'
            operation = 'react'
            deferred = await self.bot._reply_dsh('private:1', event, '切换角色')
            self.assertIsNone(deferred)
            self.bot._send_reply.assert_not_awaited()
            final = '[好感+1]'
            deferred = await self.bot._reply_dsh('private:1', event, '切换角色')
            self.assertIsNone(deferred)
            self.assertTrue(self.bot._send_reply.await_args.kwargs['allow_silent'])
            self.assertTrue(self.bot._send_reply.await_args.kwargs['strict_receipts'])
        finally:
            await service.close()

    async def test_fallback_card_request_reports_unavailable_instead_of_model_success(self) -> None:
        self.bot._send = AsyncMock()
        self.bot.client = SimpleNamespace(chat=AsyncMock())
        await self.bot._reply_llm('private:1', self.private_event(1), '找角色卡并下载')
        self.bot.client.chat.assert_not_awaited()
        self.assertIn('语义角色工具不可用', self.bot._send.await_args.args[1])
        self.assertEqual(self.store.queued(), [])

    async def test_no_opening_finishes_after_announcement(self) -> None:
        card, _ = self.cards.install_payload(payload('Silent'))
        job = await self.bot.switch_card(self.event(), card)
        self.assertEqual(job['phase'], 'complete')
        self.assertEqual(len(self.sends), 1)
        self.assertFalse(job['opening_pending'])

    def test_invalid_target_rejected(self) -> None:
        for value in ('group:0', 'group:-1', 'group:all', 'private:abc', 'group:42:43'):
            with self.assertRaises(ValueError):
                target_of(value)


if __name__ == '__main__':
    unittest.main()
