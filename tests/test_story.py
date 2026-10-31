from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from commands import CommandRouter  # noqa: E402
from content_policy import ContentPermission, adult_allowed, require_safe  # noqa: E402
from llm import ChatResult  # noqa: E402
from onebot import MessageEvent, OneBotError  # noqa: E402
from personas import PersonaStore  # noqa: E402
from relationship_rules import ChatProposal, Relationship, parse_chat_proposal  # noqa: E402
from story_generation import StoryGenerator  # noqa: E402
from story_models import (  # noqa: E402
    Choice,
    Effect,
    RunState,
    StoryDefinition,
    StoryGraphDraft,
    StorySettings,
    WorldBrief,
    advance,
)
from story_runtime import StoryRuntime  # noqa: E402
from story_store import StoryStore  # noqa: E402

from cards import CardRegistry, normalise  # noqa: E402


def definition() -> StoryDefinition:
    return StoryDefinition.model_validate({
        'title': '雨中的约定', 'background': '一座下雨的小城，角色经营着书店。',
        'synopsis': '从送书开始，选择自己的相处方式。', 'motivation': '重视可靠与坦诚。', 'character': 'Alice，认真可靠，语气温和。', 'public_character': 'Alice，小城书店的店主，认真温和。', 'start': 'opening',
        'behavior': {'初识': '克制礼貌', '亲近': '自然关心但不失去自主意愿'},
        'nodes': [
            {'id': 'opening', 'chapter': '第一幕', 'beat': '在门口遇见角色', 'choices': [
                {'id': 'help', 'text': '帮忙搬书', 'target': 'promise',
                 'effect': {'trust': 2, 'affinity': 1, 'milestone': 'shared_experience', 'reason': '行动可靠'},
                 'delayed': [{'id': 'remembered', 'after_turns': 1, 'effect': {'trust': 1, 'milestone': 'promise', 'reason': '上次的约定被兑现'}}]},
                {'id': 'space', 'text': '礼貌告别，留出空间', 'target': 'promise', 'effect': {'intent': 'boundary'}},
            ]},
            {'id': 'promise', 'chapter': '第二幕', 'beat': '讨论下一次相见', 'choices': [
                {'id': 'again', 'text': '下次再见', 'target': 'friend', 'effect': {'affinity': 1, 'intent': 'friendship'}},
                {'id': 'leave', 'text': '选择各自的旅程', 'target': 'farewell', 'effect': {'intent': 'decline'}},
            ]},
            {'id': 'friend', 'chapter': '尾声', 'beat': '彼此成为可靠的朋友', 'ending': '书店里的朋友'},
            {'id': 'farewell', 'chapter': '尾声', 'beat': '互道珍重', 'ending': '新的旅程'},
        ],
    })


def long_definition() -> StoryDefinition:
    nodes: list[dict] = []
    for index in range(9):
        nodes.append({
            'id': f'n{index}', 'chapter': f'第{index + 1}幕', 'beat': f'节点 {index}',
            'choices': [
                {'id': f'go{index}', 'text': '继续',
                 'target': f'n{index + 1}' if index < 8 else 'final'},
                {'id': f'final{index}', 'text': '直接结束', 'target': 'final'},
            ],
        })
    nodes.append({'id': 'final', 'chapter': '终章', 'beat': '故事结束', 'ending': '终章'})
    return StoryDefinition.model_validate({
        'title': '长链故事', 'background': '一条用于测试的长链。',
        'synopsis': '验证剧情树不走分页。', 'motivation': '测试。',
        'character': 'Alice。', 'public_character': 'Alice。', 'start': 'n0',
        'behavior': {}, 'nodes': nodes,
    })


def card(name: str) -> dict:
    return normalise({'name': name, 'description': name + ' world', 'first_mes': name + ' first meeting'})


class RulesTests(unittest.TestCase):
    def test_score_does_not_grant_romance(self):
        relation = Relationship(affinity=100, trust=100, milestones=['shared_experience'])
        self.assertEqual(relation.stage, '亲近')
        with self.assertRaises(ValueError):
            advance(RunState(node='opening'), relation,
                    Choice(id='accept', text='明确回应', target='ending', effect=Effect(intent='accept_romance')))
        offered = RunState(node='opening', flags=['romance_offered'])
        _, accepted, _ = advance(offered, relation,
                                  Choice(id='accept', text='明确回应', target='ending', effect=Effect(intent='accept_romance')))
        self.assertEqual(accepted.stage, '恋人')
        self.assertEqual(relation.stage, '亲近')

    def test_boundary_no_penalty(self):
        with self.assertRaises(ValueError):
            Effect(intent='decline', affinity=-1)

    def test_schema_rejects_graph_cycle_and_undefined_flag(self):
        raw = definition().model_dump()
        raw['nodes'][0]['choices'][0]['target'] = 'opening'
        with self.assertRaises(ValueError):
            StoryDefinition.model_validate(raw)
        raw = definition().model_dump()
        raw['nodes'][0]['choices'][0]['condition']['flags'] = ['never_defined']
        with self.assertRaises(ValueError):
            StoryDefinition.model_validate(raw)

    def test_delayed_effect_exact_once(self):
        story = definition()
        state, relation, _ = advance(RunState(node='opening'), Relationship(), story.nodes[0].choices[0])
        self.assertEqual(relation.trust, 12)
        state, relation, feedback = advance(state, relation, story.nodes[1].choices[0])
        self.assertEqual(relation.trust, 13)
        self.assertEqual(state.pending, [])
        self.assertIn('约定已记录', feedback)

    def test_proposal_failure_removes_only_metadata(self):
        text, proposal = parse_chat_proposal('你好\n```relation\n{"version":9}')
        self.assertEqual(text, '你好')
        self.assertIsNone(proposal)

    def test_no_explicit_generation_or_age_guessing(self):
        for scope in ('group', 'private'):
            self.assertFalse(adult_allowed(scope, ContentPermission('adult_verified', 'verified', True), character_adult=True))
        with self.assertRaises(ValueError):
            require_safe('性交')
        self.assertEqual(require_safe('grape'), 'grape')


class StoryGraphDetailTests(unittest.TestCase):
    def test_graph_detail_returns_all_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StoryStore(Path(tmp) / 'profile.db')
            try:
                store.save_world('group:1', 'character', 'fingerprint', definition())
                world = store.admin()['worlds'][0]
                graph = store.detail(world['id'], kind='graph')
                self.assertEqual(graph['total'], 4)
                self.assertEqual([node['id'] for node in graph['nodes']],
                                 ['opening', 'promise', 'friend', 'farewell'])
                self.assertEqual(graph['definition']['start'], 'opening')
                page = store.detail(world['id'], kind='world')
                self.assertEqual(len(page['nodes']), 4)
                self.assertEqual(page['total'], 4)
            finally:
                store.close()

    def test_graph_detail_is_not_limited_to_the_admin_page_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StoryStore(Path(tmp) / 'profile.db')
            try:
                store.save_world('group:1', 'character', 'fingerprint', long_definition())
                world = store.admin()['worlds'][0]
                graph = store.detail(world['id'], kind='graph')
                self.assertEqual(graph['total'], 10)
                self.assertEqual(len(graph['nodes']), 10)
                page = store.detail(world['id'], kind='world')
                self.assertEqual(page['total'], 10)
                self.assertEqual(len(page['nodes']), 8)
            finally:
                store.close()


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.profile = root / 'profile.db'
        self.store = StoryStore(self.profile)
        self.cards = CardRegistry(root / 'cards.db')
        self.a, _ = self.cards.install_payload(card('Alice'))
        self.b, _ = self.cards.install_payload(card('Bob'))
        self.cards.use(self.a.id)
        self.personas = PersonaStore(root / 'cards.db', self.cards)
        self.sent = []

        async def send(scope, chat, text):
            self.sent.append((scope, chat, text))
            return {'message_id': len(self.sent)}

        self.bot = SimpleNamespace(
            client=SimpleNamespace(), cards=self.cards, personas=self.personas, _locks={},
            config=SimpleNamespace(bot=SimpleNamespace(max_reply_chars=1200)),
            _send_receipt=send,
            _persona=lambda conversation: self.personas.effective(conversation),
            is_admin=lambda user: user == 1,
        )
        self.story = StoryRuntime(self.bot, self.store, StorySettings(enabled=True))
        self.generator = SimpleNamespace(client=self.bot.client, settings=self.story.settings,
                                         outline=AsyncMock(return_value=definition()),
                                         scene=AsyncMock(return_value='雨落在书店门前。角色抬头打了个招呼。'))
        self.story._generator = self.generator
        self.bot.story = self.story
        self.bot.send_text = AsyncMock()

    async def asyncTearDown(self):
        self.personas.close()
        self.cards.close()
        self.store.close()
        self.tmp.cleanup()

    def event(self, user=10, group=42, message=100, reply=None):
        return MessageEvent('group', user, message, '1', True, 'player', group_id=group, reply_to=reply)

    def snapshot(self, user=10, conversation='group:42', character=None):
        relation = self.store.relation(conversation, character or self.a.character_id, str(user))
        run = self.store.run(relation['id'])
        assert run is not None
        return self.store.snapshot(run['id'])

    def menu(self, run_id: str) -> dict:
        result = self.store.current_menu(run_id)
        assert result is not None
        return result

    async def test_two_players_own_choices_and_delayed_endings(self):
        await self.story.enter(self.event())
        await self.story.enter(self.event(11))
        one, two = self.snapshot(), self.snapshot(11)
        menu = self.menu(one['id'])
        with self.assertRaises(ValueError):
            await self.story.choose(self.event(11), menu['id'], 1)
        with self.assertRaises(ValueError):
            await self.story.choose(self.event(group=43), menu['id'], 1)
        await self.story.choose(self.event(), menu['id'], 1)
        self.assertEqual(self.snapshot()['relation'].trust, 12)
        self.assertEqual(self.snapshot(11)['relation'].trust, 10)
        with self.assertRaises(ValueError):
            await self.story.choose(self.event(), menu['id'], 1)
        next_menu = self.menu(one['id'])
        await self.story.choose(self.event(message=101), next_menu['id'], 1)
        self.assertEqual(self.snapshot()['status'], 'ended')
        self.assertEqual(self.snapshot()['relation'].trust, 13)
        self.assertIn('结局', self.sent[-1][2])
        self.assertEqual(two['id'], self.snapshot(11)['id'])

    async def test_failed_generation_never_consumes_choice(self):
        await self.story.enter(self.event())
        snapshot = self.snapshot()
        menu = self.menu(snapshot['id'])
        self.generator.scene.side_effect = ValueError('bad json')
        with self.assertRaises(ValueError):
            await self.story.choose(self.event(), menu['id'], 1)
        self.assertEqual(self.snapshot()['revision'], 0)
        saved_menu = self.store.menu(menu['id'])
        assert saved_menu is not None
        self.assertEqual(saved_menu['status'], 'sent')
        self.assertEqual(self.snapshot()['relation'].trust, 10)
        self.generator.scene.side_effect = None
        await self.story.choose(self.event(), menu['id'], 1)
        self.assertEqual(self.snapshot()['revision'], 1)

    async def test_unknown_delivery_restart_and_explicit_resume(self):
        await self.story.enter(self.event())
        menu = self.menu(self.snapshot()['id'])
        self.bot._send_receipt = AsyncMock(side_effect=OneBotError('timeout'))
        with self.assertRaises(OneBotError):
            await self.story.choose(self.event(), menu['id'], 1)
        self.assertEqual(self.snapshot()['status'], 'unknown')
        self.assertEqual(self.snapshot()['relation'].trust, 12)
        calls = self.bot._send_receipt.await_count
        self.store.recover()
        await self.story.tick()
        self.assertEqual(calls, self.bot._send_receipt.await_count)
        self.bot._send_receipt = AsyncMock(return_value={'message_id': 999})
        await self.story.enter(self.event())
        self.assertEqual(self.snapshot()['status'], 'active')
        self.assertEqual(self.snapshot()['relation'].trust, 12)
        self.assertEqual(len(self.store.events(self.snapshot()['relation_id'])), 1)

    async def test_switch_back_resumes_original_save_new_menu_token(self):
        await self.story.enter(self.event())
        old = self.snapshot()
        old_menu = self.menu(old['id'])
        job = self.personas.enqueue('group:42', self.b, actor='1')
        self.personas.bind(job['id'], 0)
        self.personas.update(job['id'], phase='complete')
        await self.story.enter(self.event())
        self.assertNotEqual(self.snapshot(character=self.b.character_id)['id'], old['id'])
        job = self.personas.enqueue('group:42', self.a, actor='1')
        self.personas.bind(job['id'], 0)
        self.personas.update(job['id'], phase='complete')
        await self.story.enter(self.event())
        self.assertEqual(self.snapshot()['id'], old['id'])
        self.assertNotEqual(self.menu(old['id'])['id'], old_menu['id'])
        with self.assertRaises(ValueError):
            await self.story.choose(self.event(), old_menu['id'], 1)

    async def test_confirmed_restart_and_forget_are_scoped(self):
        await self.story.enter(self.event())
        await self.story.enter(self.event(11))
        old, other = self.snapshot(), self.snapshot(11)
        response = await self.story.command(self.event(), ['restart'])
        code = response.split('/story restart ')[1].split('（')[0]
        await self.story.command(self.event(), ['restart', code])
        await self.story.enter(self.event())
        self.assertNotEqual(old['id'], self.snapshot()['id'])
        self.assertEqual(other['id'], self.snapshot(11)['id'])
        response = await self.story.command(self.event(), ['forget'])
        code = response.split('/story forget ')[1].split('（')[0]
        await self.story.command(self.event(), ['forget', code])
        with closing(sqlite3.connect(self.profile)) as db:
            scenes = db.execute('SELECT r.scene FROM story_runs r JOIN story_relations p ON p.id=r.relation_id WHERE p.user=\'10\'').fetchall()
        self.assertTrue(all(not row[0] for row in scenes))

    async def test_group_bare_digit_and_quote_routing(self):
        await self.story.enter(self.event())
        self.assertFalse(await self.story.maybe_choice(self.event(), '1'))
        quote = self.event(reply=len(self.sent))
        self.assertTrue(await self.story.maybe_choice(quote, '1'))
        self.assertEqual(self.snapshot()['revision'], 1)

    async def test_choice_race_only_commits_once(self):
        await self.story.enter(self.event())
        menu = self.menu(self.snapshot()['id'])
        results = await asyncio.gather(self.story.choose(self.event(), menu['id'], 1),
                                       self.story.choose(self.event(message=101), menu['id'], 2), return_exceptions=True)
        self.assertEqual(sum(isinstance(item, ValueError) for item in results), 1)
        self.assertEqual(self.snapshot()['revision'], 1)

    async def test_chat_limits_do_not_swallow_conflict_or_impression(self):
        relation = self.store.relation('group:42', self.a.character_id, '10')
        settings = StorySettings(daily_chat_gain=1, chat_cooldown=60)
        kind = ChatProposal(kind='kindness', evidence='帮忙', reason='友善')
        self.store.chat(relation, kind, 'a', settings, 1000)
        self.store.chat(relation, kind, 'b', settings, 1001)
        result = self.store.chat(relation, ChatProposal(kind='conflict', evidence='明确争执', reason='分歧', impression='需要空间'), 'c', settings, 1002)
        self.assertEqual(result.affinity, 20)
        self.assertEqual(result.impression, '需要空间')
        self.assertEqual(self.store.chat(relation, kind, 'a', settings, 1003).affinity, 20)

    async def test_policy_change_cancels_inflight_commit(self):
        await self.story.enter(self.event())
        menu = self.menu(self.snapshot()['id'])

        async def changed(*args):
            self.store.permission('group:42', '10', opt_in=False)
            return '安全场景'

        self.generator.scene.side_effect = changed
        with self.assertRaises(ValueError):
            await self.story.choose(self.event(), menu['id'], 1)
        self.assertEqual(self.snapshot()['revision'], 0)

    async def test_switch_race_discards_stale_generation(self):
        await self.story.enter(self.event())
        menu = self.menu(self.snapshot()['id'])

        async def switched(*args):
            job = self.personas.enqueue('group:42', self.b, actor='1')
            self.personas.bind(job['id'], 0)
            self.personas.update(job['id'], phase='complete')
            return '旧角色场景'

        self.generator.scene.side_effect = switched
        with self.assertRaises(ValueError):
            await self.story.choose(self.event(), menu['id'], 1)
        self.assertEqual(self.snapshot()['revision'], 0)

    async def test_setup_admin_only_once_and_public_fields_only(self):
        with self.assertRaises(ValueError):
            await self.story.command(self.event(), ['setup'])
        response = await self.story.command(self.event(user=1), ['setup'])
        self.assertEqual(response, '')
        posted = ''.join(item[2] for item in self.sent)
        self.assertIn('Alice，小城书店的店主', posted)
        self.assertIn('一座下雨的小城', posted)
        self.assertNotIn('重视可靠与坦诚', posted)
        count = len(self.sent)
        response = await self.story.command(self.event(user=1), ['setup'])
        self.assertIn('已经发送', response)
        self.assertEqual(len(self.sent), count)
        self.generator.outline.assert_awaited_once()

    async def test_setup_unknown_requires_explicit_retry(self):
        self.bot._send_receipt = AsyncMock(side_effect=OneBotError('timeout'))
        with self.assertRaises(OneBotError):
            await self.story.command(self.event(user=1), ['setup'])
        self.store.recover()
        calls = self.bot._send_receipt.await_count
        response = await self.story.command(self.event(user=1), ['setup'])
        self.assertIn('投递未知', response)
        self.assertEqual(self.bot._send_receipt.await_count, calls)
        self.bot._send_receipt = AsyncMock(return_value={'message_id': 998})
        await self.story.command(self.event(user=1), ['setup', 'retry'])
        self.assertEqual(self.bot._send_receipt.await_count, 1)

    async def test_explicit_refresh_reissues_menu_without_changing_effects(self):
        await self.story.enter(self.event())
        snapshot = self.snapshot()
        previous = self.menu(snapshot['id'])
        await self.story.command(self.event(), ['refresh'])
        current = self.menu(snapshot['id'])
        self.assertNotEqual(previous['id'], current['id'])
        self.assertEqual(previous['choices'], current['choices'])
        self.assertEqual(self.snapshot()['revision'], 0)
        old = self.store.menu(previous['id'])
        assert old is not None
        self.assertEqual(old['status'], 'stale')
        self.generator.scene.assert_awaited_once()

    async def test_public_commands_are_not_admin_only(self):
        await CommandRouter(self.bot).handle(self.event(), '/story status')
        self.bot.send_text.assert_awaited()
        self.assertIn('Alice', self.bot.send_text.await_args.args[1])

    async def test_private_and_group_saves_and_permission_revocation(self):
        await self.story.enter(self.event())
        private = MessageEvent('private', 10, 200, 'start', False, 'player')
        await self.story.enter(private)
        private_snapshot = self.snapshot(conversation='private:10')
        self.assertNotEqual(private_snapshot['id'], self.snapshot()['id'])
        self.store.permission('private:10', '10', age='adult_declared')
        enabled = self.store.permission('private:10', '10', opt_in=True)
        self.assertTrue(enabled['opted_in'])
        self.store.permission('private:10', '10', opt_in=False)
        self.assertEqual(self.snapshot(conversation='private:10')['status'], 'paused')
        self.assertEqual(self.snapshot()['status'], 'active')
        await self.story.enter(private)
        self.assertEqual(self.snapshot(conversation='private:10')['status'], 'active')

    async def test_expired_menu_and_restart_after_reopening_database(self):
        await self.story.enter(self.event())
        old = self.snapshot()
        menu = self.menu(old['id'])
        with self.store.transaction() as db:
            db.execute('UPDATE story_menus SET expires=0 WHERE id=?', (menu['id'],))
        with self.assertRaises(ValueError):
            await self.story.choose(self.event(), menu['id'], 1)
        self.store.close()
        self.store = StoryStore(self.profile)
        self.store.recover()
        self.story.store = self.store
        await self.story.enter(self.event())
        self.assertEqual(self.snapshot()['id'], old['id'])
        self.assertNotEqual(self.menu(old['id'])['id'], menu['id'])

    async def test_admin_queue_validates_target_and_executes_settings(self):
        from admin_story import queue_story
        await self.story.enter(self.event())
        snapshot = self.snapshot()
        payload = {'kind': 'retry', 'data': {'run_id': snapshot['id'], 'revision': 0,
                   'conversation': 'group:42', 'character': self.a.character_id, 'user': '999'}}
        with self.assertRaises(ValueError):
            queue_story(str(self.profile), payload, self.story.defaults.model_dump())
        queued = queue_story(str(self.profile), {'kind': 'settings', 'data': {'daily_chat_gain': 3}}, self.story.defaults.model_dump())
        await self.story.process_operations()
        self.assertEqual(self.story.settings.daily_chat_gain, 3)
        result = next(row for row in self.store.admin()['operations'] if row['id'] == queued['id'])
        self.assertEqual(result['status'], 'complete')

    async def test_stable_identity_linking_is_explicit_and_references_protected(self):
        version, _ = self.cards.install_payload(card('Alice v2'))
        self.assertNotEqual(version.character_id, self.a.character_id)
        linked = self.cards.link_character(version.id, self.a.id)
        self.assertEqual(linked.character_id, self.a.character_id)
        await self.story.enter(self.event())
        with self.assertRaises(ValueError):
            self.cards.delete(self.a.id)
        with self.assertRaises(ValueError):
            self.cards.link_character(self.a.id, self.b.id)


class GenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_fenced_json_repair_and_no_tools_or_shared_history(self):
        raw = definition().model_dump()
        brief = WorldBrief.model_validate({key: raw[key] for key in WorldBrief.model_fields})
        graph = StoryGraphDraft.model_validate({key: raw[key] for key in StoryGraphDraft.model_fields})
        client = SimpleNamespace(complete=AsyncMock(side_effect=[ChatResult('bad', 'stop', {}),
                               ChatResult(brief.model_dump_json(), 'stop', {}), ChatResult(graph.model_dump_json(), 'stop', {})]))
        generator = StoryGenerator(client, StorySettings())
        result = await generator.outline(card('Alice'), 'group:42')
        self.assertEqual(result.title, definition().title)
        self.assertEqual(client.complete.await_count, 3)
        kwargs = client.complete.await_args.kwargs
        self.assertEqual(kwargs['attempts'], 1)
        self.assertEqual(kwargs['purpose'], 'story_graph')
        self.assertNotIn('tools', kwargs)
        self.assertNotIn('session', kwargs)

    async def test_truncated_and_unsafe_response_rejected(self):
        for result in [ChatResult(definition().model_dump_json(), 'length', {}), ChatResult('性交', 'stop', {})]:
            client = SimpleNamespace(complete=AsyncMock(return_value=result))
            with self.assertRaises(ValueError):
                await StoryGenerator(client, StorySettings()).outline(card('Alice'), 'group:42')


if __name__ == '__main__':
    unittest.main()
