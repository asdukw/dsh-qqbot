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

    async def test_non_admin_cannot_request_cutover(self) -> None:
        with self.assertRaises(ValueError):
            await self.bot.switch_card(self.event(user=9), self.b)
        self.assertFalse(self.sends)

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
