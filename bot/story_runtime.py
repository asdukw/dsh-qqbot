"""Player-scoped story orchestration. Model calls never hold a SQL transaction."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from content_policy import require_safe
from onebot import OneBotError, OneBotRejected
from personas import target_of
from relationship_rules import Relationship, relation_context
from story_generation import StoryGenerator
from story_models import Choice, RunState, StoryDefinition, StorySettings, advance, available
from story_store import StoryStore, uid

STORY_HELP = '''/story setup  管理员补发当前角色的公开设定（不重开）
/story setup retry  明确重试投递未知的设定介绍
/story start|continue  开始/继续自己的剧情
/story status|history|pause  状态/经历/暂停
/story refresh  按当前关系显式刷新可用选项（不改效果）
/choose <菜单码> <编号>  选择；也可以引用菜单回复数字
/story restart  确认后重开本角色关系与周目
/story forget  确认后遗忘本角色本会话的活动及归档剧情
/story boundaries  内容偏好（本期仅安全内容）
群内剧情公开可见，但每位玩家独立存档。'''


def conversation_of(event: Any) -> str:
    if event.message_type == 'group' and event.group_id:
        return f'group:{event.group_id}'
    if event.message_type == 'private':
        return f'private:{event.user_id}'
    raise ValueError('未知会话')


def render_scene(definition: StoryDefinition, state: RunState, relation: Relationship,
                 text: str, choices: list[Choice], menu: str, user: str,
                 feedback: list[str], limit: int) -> list[str]:
    node = definition.node(state.node)
    header = f'【{definition.title} · {node.chapter}】\n玩家：{user}\n'
    if feedback:
        header += '〔' + '；'.join(feedback)[:600] + '〕\n'
    prose = require_safe(header + '\n' + text)
    if node.ending:
        footer = f'【结局：{node.ending}】\n关系：{relation.stage}｜好感 {relation.affinity}｜信任 {relation.trust}\n/story restart 可确认重开。'
    else:
        footer = f'【{user} 的选项 · {menu}】\n' + '\n'.join(
            f'{index}. {choice.text}' for index, choice in enumerate(choices, 1)
        ) + f'\n/choose {menu} <编号>，或引用此消息回复数字。'
    # Keep the actionable menu together. Narrative may span transport chunks.
    limit = max(1, limit)
    if len(footer) > limit:
        raise ValueError('菜单超过消息长度限制，请调高 max_reply_chars')
    return [prose[index:index + limit] for index in range(0, len(prose), limit)] + [require_safe(footer)]


class StoryRuntime:
    def __init__(self, bot: Any, store: StoryStore, defaults: StorySettings):
        self.bot = bot
        self.store = store
        self.defaults = defaults
        self._locks: dict[str, asyncio.Lock] = {}
        self._world_locks: dict[str, asyncio.Lock] = {}
        self._generator = StoryGenerator(bot.client, defaults)
        self._generator.telemetry = store.generation_log
        self._generator.diagnostics = store.generation_attempt
        self._generator.checkpoints = store

    @property
    def settings(self) -> StorySettings:
        return self.store.settings(self.defaults)

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    def generator(self) -> StoryGenerator:
        settings = self.settings
        if self._generator.client is not self.bot.client or self._generator.settings != settings:
            self._generator = StoryGenerator(self.bot.client, settings)
            self._generator.telemetry = self.store.generation_log
            self._generator.diagnostics = self.store.generation_attempt
            self._generator.checkpoints = self.store
        return self._generator

    async def prepare_switch(self, conversation: str, record: Any) -> dict | None:
        if not self.enabled:
            return None
        extension = record.payload.get('data', {}).get('extensions', {}).get('qqbot', {})
        preferences = extension.get('galgame', {}) if isinstance(extension, dict) else {}
        if not isinstance(preferences, dict):
            preferences = {}
        preferences = {**preferences, **self.settings.character_preferences.get(record.character_id, {})}
        if preferences.get('enabled') is False:
            return None
        key = f'{conversation}:{record.character_id}:{record.fingerprint}'
        async with self._world_locks.setdefault(key, asyncio.Lock()):
            existing = self.store.world(conversation, record.character_id, record.fingerprint)
            if existing:
                return {'world': existing, 'returning': True}
            # Mark first: conservatively protect the card even if generation fails.
            self.bot.cards.mark_story(record.id)
            definition = await self.generator().outline(record.payload, conversation, preferences,
                                                        character_id=record.character_id, fingerprint=record.fingerprint)
            world = self.store.save_world(conversation, record.character_id, record.fingerprint, definition)
            return {'world': world, 'returning': False}

    @staticmethod
    def public_opening(prepared: dict) -> str:
        definition = StoryDefinition.model_validate_json(prepared['world']['definition'])
        if prepared['returning']:
            return require_safe(f'【{definition.title} · 返回】\n{definition.synopsis}\n原存档保留。每人 /story continue 恢复自己的剧情。')
        return require_safe(f'【{definition.title} · 序幕】' + chr(10) + definition.public_character + chr(10) * 2 + definition.background + chr(10) * 2 + definition.synopsis + chr(10) +
                            '每位玩家 /story start 开始独立剧情。群内剧情公开可见，不是共同投票。')

    async def setup(self, event: Any, *, retry: bool = False) -> str:
        if not self.bot.is_admin(event.user_id):
            raise ValueError('只有管理员可以向群里发布设定')
        if not self.enabled:
            raise ValueError('剧情系统未启用')
        conversation = conversation_of(event)
        persona = self.bot._persona(conversation)
        prepared = await self.prepare_switch(conversation, persona.record)
        if prepared is None:
            raise ValueError('当前角色的剧情未启用')
        prepared = {**prepared, 'returning': False}
        text = self.public_opening(prepared)
        limit = max(1, self.bot.config.bot.max_reply_chars)
        chunks = [text[index:index + limit] for index in range(0, len(text), limit)]
        world_id = prepared['world']['id']
        async with self.bot._locks.setdefault(conversation, asyncio.Lock()):
            self._check_persona(conversation, persona.token)
            announcement = self.store.prepare_announcement(world_id, persona.token, chunks)
            if announcement['status'] == 'sent':
                return '本角色的公开人物/剧情设定已经发送，不重复刷屏；个人 /story start 或 /story continue。'
            if announcement['status'] in ('unknown', 'sending') and not retry:
                return '设定介绍投递未知，不自动重发；检查群消息后 /story setup retry 明确重试（可能重复展示）。'
            receipts = json.loads(announcement['receipts'])
            chunks = json.loads(announcement['chunks'])
            for chunk in chunks:
                require_safe(chunk)
            scope, chat_id = target_of(conversation)
            for index in range(len(receipts), len(chunks)):
                self.store.update_announcement(world_id, 'sending', receipts, token=persona.token)
                try:
                    result = await self.bot._send_receipt(scope, chat_id, chunks[index])
                    receipts.append({'index': index, 'message_id': result['message_id']})
                    self.store.update_announcement(world_id, 'pending', receipts, token=persona.token)
                except BaseException as exc:
                    self.store.update_announcement(world_id, 'rejected' if isinstance(exc, OneBotRejected) else 'unknown',
                                                   receipts, token=persona.token, error='delivery incomplete')
                    raise
            self.store.update_announcement(world_id, 'sent', receipts, token=persona.token)
        return ''

    def context(self, conversation: str, persona: Any, user: str) -> str:
        self.bot.cards.mark_story(persona.record.id)
        row = self.store.relation(conversation, persona.record.character_id, user)
        relation = Relationship.model_validate_json(row['data'])
        world = self.store.world(conversation, persona.record.character_id, persona.record.fingerprint)
        behavior = ''
        if world:
            behavior = StoryDefinition.model_validate_json(world['definition']).behavior.get(relation.stage, '')
        return relation_context(relation, behavior=behavior)

    def _menu(self, definition: StoryDefinition, state: RunState, relation: Relationship) -> list[Choice]:
        choices = available(definition.node(state.node), state, relation)
        # In addition to declarative gates, romance effects always pass the reducer's hard gate.
        valid = []
        for choice in choices:
            try:
                advance(state, relation, choice)
            except ValueError:
                continue
            valid.append(choice)
        if not definition.node(state.node).ending and not valid:
            raise ValueError('当前节点没有合法选项，剧本需要修复；进度未改变')
        return valid

    def _render(self, snapshot: dict, definition: StoryDefinition, state: RunState,
                relation: Relationship, text: str, feedback: list[str]) -> tuple[str, list[dict], list[str]]:
        menu_id = uid()[:12]
        choices = self._menu(definition, state, relation)
        chunks = render_scene(definition, state, relation, text, choices, menu_id,
                              snapshot['user'], feedback, self.bot.config.bot.max_reply_chars)
        return menu_id, [choice.model_dump() for choice in choices], chunks

    def _resume_payload(self, snapshot: dict, definition: StoryDefinition, feedback: list[str]) -> tuple[str, list[dict], list[str]]:
        menu_id = uid()[:12]
        old_menu = self.store.current_menu(snapshot['id'])
        choices = ([Choice.model_validate(item) for item in json.loads(old_menu['choices'])]
                   if old_menu else self._menu(definition, snapshot['state_model'], snapshot['relation']))
        chunks = render_scene(definition, snapshot['state_model'], snapshot['relation'], snapshot['scene'],
                              choices, menu_id, snapshot['user'], feedback, self.bot.config.bot.max_reply_chars)
        return menu_id, [choice.model_dump() for choice in choices], chunks

    async def enter(self, event: Any) -> None:
        conversation, user = conversation_of(event), str(event.user_id)
        persona = self.bot._persona(conversation)
        key = f'{conversation}:{persona.record.character_id}:{user}'
        async with self._locks.setdefault(key, asyncio.Lock()):
            relation_row = self.store.relation(conversation, persona.record.character_id, user)
            run = self.store.run(relation_row['id'])
            if run:
                snapshot = self.store.snapshot(run['id'])
                definition = StoryDefinition.model_validate_json(snapshot['world']['definition'])
                menu_id, choices, chunks = self._resume_payload(snapshot, definition, ['恢复既有场景，不重复奖励'])
                async with self.bot._locks.setdefault(conversation, asyncio.Lock()):
                    self._check_persona(conversation, persona.token)
                    self.store.replay(snapshot, persona.token, menu_id, choices, chunks,
                                      self._expires(), self.store.policy_revision(conversation, user))
                await self.deliver(run['id'])
                return
            prepared = await self.prepare_switch(conversation, persona.record)
            if prepared is None:
                raise ValueError('当前角色的剧情未启用')
            definition = StoryDefinition.model_validate_json(prepared['world']['definition'])
            state = RunState(node=definition.start)
            relation = Relationship.model_validate_json(relation_row['data'])
            policy = self.store.policy_revision(conversation, user)
            scene = await self.generator().scene(definition, state, relation, [], conversation)
            menu_id, choices, chunks = self._render({'user': user}, definition, state, relation, scene, [])
            async with self.bot._locks.setdefault(conversation, asyncio.Lock()):
                self._check_persona(conversation, persona.token)
                if policy != self.store.policy_revision(conversation, user):
                    raise ValueError('内容偏好已变化，请重试')
                run_id = self.store.start(relation_row, prepared['world'], state, scene, persona.token,
                                          menu_id, choices, chunks, self._expires(), policy)
            await self.deliver(run_id)

    async def refresh(self, event: Any) -> str:
        conversation, user = conversation_of(event), str(event.user_id)
        persona = self.bot._persona(conversation)
        key = f'{conversation}:{persona.record.character_id}:{user}'
        async with self._locks.setdefault(key, asyncio.Lock()):
            relation = self.store.relation(conversation, persona.record.character_id, user)
            run = self.store.run(relation['id'])
            if not run:
                return '尚未开始剧情，请 /story start。'
            snapshot = self.store.snapshot(run['id'])
            definition = StoryDefinition.model_validate_json(snapshot['world']['definition'])
            menu, choices, chunks = self._render(snapshot, definition, snapshot['state_model'],
                                                snapshot['relation'], snapshot['scene'], ['按当前关系刷新可用选项，旧菜单失效'])
            async with self.bot._locks.setdefault(conversation, asyncio.Lock()):
                self._check_persona(conversation, persona.token)
                self.store.replay(snapshot, persona.token, menu, choices, chunks,
                                  self._expires(), self.store.policy_revision(conversation, user))
            await self.deliver(run['id'])
        return ''

    def _check_persona(self, conversation: str, token: str) -> None:
        if self.bot._persona(conversation).token != token:
            raise ValueError('角色已切换，请 /story continue')

    def _expires(self) -> int:
        return int(time.time()) + self.settings.menu_hours * 3600

    async def choose(self, event: Any, menu_id: str, number: int) -> None:
        conversation, user = conversation_of(event), str(event.user_id)
        menu = self.store.menu(menu_id)
        if menu is None:
            raise ValueError('菜单不存在，请 /story continue')
        snapshot = self.store.snapshot(menu['run_id'])
        if snapshot['conversation'] != conversation or snapshot['user'] != user:
            raise ValueError('只能选择本会话中属于自己的菜单')
        key = f"{conversation}:{snapshot['character']}:{user}"
        async with self._locks.setdefault(key, asyncio.Lock()):
            snapshot = self.store.snapshot(menu['run_id'])
            menu = self.store.menu(menu_id)
            assert menu is not None
            if (not self.enabled or snapshot['status'] != 'active' or menu['status'] != 'sent'
                    or menu['expires'] < time.time() or menu['revision'] != snapshot['revision']):
                raise ValueError('菜单尚未送达、已选择、过期或暂停；请 /story continue')
            self._check_persona(conversation, menu['token'])
            choices = [Choice.model_validate(item) for item in json.loads(menu['choices'])]
            if not 1 <= number <= len(choices):
                raise ValueError('选项编号无效')
            choice = choices[number - 1]
            if not choice.condition.accepts(snapshot['state_model'], snapshot['relation']):
                raise ValueError('关系已变化，这个选项暂不可用，请继续当前存档')
            # The durable menu itself prevents repeat rewards even without a message id.
            message_id = getattr(event, 'message_id', 0)
            action = f'{conversation}:{user}:msg:{message_id}' if message_id else f'{snapshot["id"]}:{menu_id}'
            if not self.store.reserve(action, snapshot['id']):
                raise ValueError('此选择已处理或正在处理，请 /story status')
            try:
                state, relation, feedback = advance(snapshot['state_model'], snapshot['relation'], choice)
                definition = StoryDefinition.model_validate_json(snapshot['world']['definition'])
                policy = self.store.policy_revision(conversation, user)
                events = self.store.events(snapshot['relation_id'], canonical=True)
                events.append({'kind': 'selected', 'summary': choice.text + '；' + '；'.join(feedback)})
                scene = await self.generator().scene(definition, state, relation, events, conversation)
                next_id, next_choices, chunks = self._render(snapshot, definition, state, relation, scene, feedback)
                async with self.bot._locks.setdefault(conversation, asyncio.Lock()):
                    self._check_persona(conversation, menu['token'])
                    self.store.commit_choice(snapshot, menu, action, state, relation, scene,
                                             choice.text + '；' + '；'.join(feedback), menu['token'],
                                             next_id, next_choices, chunks, self._expires(), policy,
                                             bool(definition.node(state.node).ending))
            except BaseException:
                self.store.fail_action(action)
                raise
            await self.deliver(snapshot['id'])

    async def deliver(self, run_id: str) -> None:
        delivery = self.store.outbox(run_id)
        if delivery is None or delivery['status'] != 'pending':
            return
        snapshot = self.store.snapshot(run_id)
        conversation = snapshot['conversation']
        async with self.bot._locks.setdefault(conversation, asyncio.Lock()):
            try:
                delivery = self.store.outbox(run_id)
                if delivery is None or delivery['status'] != 'pending':
                    return
                snapshot = self.store.snapshot(run_id)
                if snapshot['status'] not in ('active', 'ended') or snapshot['revision'] != delivery['revision']:
                    return
                self._check_persona(conversation, delivery['token'])
                if not self.enabled or delivery['policy'] != self.store.policy_revision(conversation, snapshot['user']):
                    self.store.pause(run_id)
                    return
                scope, chat_id = target_of(conversation)
                receipts = json.loads(delivery['receipts'])
                chunks = json.loads(delivery['chunks'])
                for chunk in chunks:
                    require_safe(chunk)
                for index in range(len(receipts), len(chunks)):
                    if delivery['policy'] != self.store.policy_revision(conversation, snapshot['user']):
                        self.store.pause(run_id)
                        return
                    require_safe(chunks[index])
                    if not self.store.sending(delivery['id']):
                        return
                    try:
                        receipt = await self.bot._send_receipt(scope, chat_id, chunks[index])
                        receipts.append({'index': index, 'message_id': receipt['message_id']})
                        self.store.receipt(delivery['id'], receipts)
                    except BaseException as exc:
                        self.store.delivery_failed(delivery, unknown=not isinstance(exc, OneBotRejected))
                        raise
                self.store.delivered(delivery)
            except ValueError:
                self.store.pause(run_id)
                return

    async def maybe_choice(self, event: Any, text: str) -> bool:
        if not self.enabled or not text.strip().isdigit():
            return False
        conversation, user = conversation_of(event), str(event.user_id)
        menu = None
        if getattr(event, 'reply_to', None):
            menu = self.store.quoted_menu(conversation, user, event.reply_to)
        elif event.message_type == 'private':
            persona = self.bot._persona(conversation)
            relation = self.store.relation(conversation, persona.record.character_id, user)
            run = self.store.run(relation['id'])
            menu = self.store.current_menu(run['id']) if run else None
        if menu is None:
            return False
        await self.choose(event, menu['id'], int(text.strip()))
        return True

    async def handle_command(self, event: Any, name: str, args: list[str]) -> str:
        if name in ('/choose', '/选择'):
            if len(args) != 2 or not args[1].isdigit():
                raise ValueError('用法：/choose <菜单码> <编号>')
            if not self.enabled:
                raise ValueError('剧情系统未启用')
            await self.choose(event, args[0], int(args[1]))
            return ''
        return await self.command(event, args)

    async def command(self, event: Any, args: list[str]) -> str:
        action = args[0].lower() if args else 'continue'
        action = {'开始': 'start', '继续': 'continue', '状态': 'status', '经历': 'history', '暂停': 'pause', '重开': 'restart', '遗忘': 'forget', '边界': 'boundaries', '设定': 'setup', '刷新': 'refresh'}.get(action, action)
        conversation, user = conversation_of(event), str(event.user_id)
        if action in ('help', '?'):
            return STORY_HELP
        if action == 'boundaries':
            if event.message_type != 'private':
                return '内容偏好属于私人设置，请私聊使用 /story boundaries；群聊始终安全模式。'
            mode = args[1] if len(args) > 1 else ''
            if mode in ('unknown', 'adult_declared', 'minor'):
                permission = self.store.permission(conversation, user, age=mode)
            elif mode in ('off', 'on'):
                permission = self.store.permission(conversation, user, opt_in=mode == 'on')
            else:
                permission = self.store.permission(conversation, user)
            return (f"年龄声明：{permission['age']}（自我声明不是身份核验）\n"
                    f"偏好记录：{'on' if permission['opted_in'] else 'off'}；本期成人生成始终关闭。\n"
                    '/story boundaries unknown|adult_declared|minor|off|on；off 可随时撤回。')
        if not self.enabled:
            return '剧情系统未启用，管理员可在面板开启。'
        if action == 'setup':
            return await self.setup(event, retry=len(args) == 2 and args[1] == 'retry')
        if action == 'refresh':
            return await self.refresh(event)
        if action in ('start', 'continue', 'resume'):
            await self.enter(event)
            return ''
        persona = self.bot._persona(conversation)
        self.bot.cards.mark_story(persona.record.id)
        relation = self.store.relation(conversation, persona.record.character_id, user)
        if action == 'status':
            run = self.store.run(relation['id'])
            model = Relationship.model_validate_json(relation['data'])
            return (f'{persona.card.name} × {user}｜{model.stage}\n好感 {model.affinity}/100｜信任 {model.trust}/100\n'
                    + (f"周目 {run['id'][:12]} · {run['status']}\n/story continue 恢复" if run else '/story start 开始剧情') + chr(10) + '最近变化：' + '；'.join(item['summary'] for item in self.store.events(relation['id'], 3)))
        if action == 'history':
            return '\n'.join(item['summary'] for item in self.store.events(relation['id'])) or '暂无已确认经历。'
        if action == 'pause':
            run = self.store.run(relation['id'])
            if run:
                key = f'{conversation}:{persona.record.character_id}:{user}'
                async with self._locks.setdefault(key, asyncio.Lock()):
                    async with self.bot._locks.setdefault(conversation, asyncio.Lock()):
                        self.store.pause(run['id'])
            return '已暂停自己的剧情；/story continue 恢复。'
        if action in ('restart', 'forget'):
            if len(args) < 2:
                code = self.store.confirmation(relation, action)
                return (f'确认{ "重开" if action == "restart" else "遗忘" }本会话 {persona.card.name} × {user}？'
                        '关系和剧情将重置，真实用户 facts 不变。\n'
                        f'/story {action} {code}（5分钟有效）\n'
                        '已发消息/第三方副本无法撤回，本地备份按保留期过期。')
            key = f'{conversation}:{persona.record.character_id}:{user}'
            async with self._locks.setdefault(key, asyncio.Lock()):
                async with self.bot._locks.setdefault(conversation, asyncio.Lock()):
                    self.store.reset(relation, args[1], action)
            return '已处理，仅影响这个角色/会话/玩家。/story start 新开局。'
        return STORY_HELP

    async def process_operations(self) -> None:
        for operation in self.store.queued():
            if not self.store.claim_operation(operation['id']):
                continue
            try:
                payload = json.loads(operation['payload'])
                if operation['kind'] == 'settings':
                    self.store.save_settings(payload, self.defaults)
                elif operation['kind'] == 'regenerate':
                    if not self.enabled:
                        raise ValueError('enable story before preparing a world')
                    record = self.bot.cards.get(int(payload['card_id']))
                    if record is None:
                        raise ValueError('card missing')
                    # Reuse an immutable accepted definition; create only missing packages.
                    await self.prepare_switch(str(payload['conversation']), record)
                elif operation['kind'] == 'link':
                    self.bot.cards.link_character(int(payload['card_id']), int(payload['original_id']))
                else:
                    snapshot = self.store.snapshot(str(payload['run_id']))
                    conversation = snapshot['conversation']
                    async with self.bot._locks.setdefault(conversation, asyncio.Lock()):
                        snapshot = self.store.snapshot(str(payload['run_id']))
                        if (int(payload['revision']) != snapshot['revision'] or any(payload.get(key) != snapshot[key] for key in ('conversation', 'character', 'user'))):
                            raise ValueError('stale operation')
                        if operation['kind'] == 'restart':
                            if payload.get('confirmed') != snapshot['user']:
                                raise ValueError('confirmation missing')
                            relation = self.store.relation(conversation, snapshot['character'], snapshot['user'])
                            code = self.store.confirmation(relation, 'restart')
                            self.store.reset(relation, code)
                        elif operation['kind'] == 'import_legacy':
                            self.store.import_legacy(snapshot, str(payload.get('confirmed', '')))
                        elif operation['kind'] == 'retry':
                            persona = self.bot._persona(conversation)
                            if persona.record.character_id != snapshot['character']:
                                raise ValueError('character changed')
                            definition = StoryDefinition.model_validate_json(snapshot['world']['definition'])
                            menu_id, choices, chunks = self._resume_payload(snapshot, definition, ['管理员明确恢复'])
                            self.store.replay(snapshot, persona.token, menu_id, choices, chunks,
                                              self._expires(), self.store.policy_revision(conversation, snapshot['user']))
                    if operation['kind'] == 'retry':
                        await self.deliver(snapshot['id'])
                self.store.finish_operation(operation['id'], True)
            except Exception:
                self.store.finish_operation(operation['id'], False)
                from log_context import get_logger
                get_logger(__name__).exception('story operation failed: id=%s', operation['id'])

    async def tick(self) -> None:
        await self.process_operations()
        for run_id in self.store.pending_runs():
            try:
                await self.deliver(run_id)
            except (ValueError, OneBotError):
                pass
