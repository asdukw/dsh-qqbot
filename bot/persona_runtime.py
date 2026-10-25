"""Bot-side persona preparation and serialized switch delivery."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from dsh_client import DSHError
from log_context import get_logger
from onebot import MessageEvent, OneBotError, OneBotRejected
from personas import Persona, target_of
from prompt import build_system_prompt, substitute

from cards import CardRecord, CharacterCard

log = get_logger(__name__)
TZ = ZoneInfo('Asia/Shanghai')


class PersonaRuntime:
    # These services belong to Bot; this mixin keeps cutover orchestration together.
    config: Any
    personas: Any
    sessions: Any
    conversations: Any
    memory: Any
    group_context: Any
    onebot: Any
    proactive: Any
    _locks: dict[str, asyncio.Lock]
    _persona_worker: asyncio.Task | None
    is_admin: Any
    _remember_own_message: Any

    @staticmethod
    def _persona_names(persona: Persona) -> tuple[str, ...]:
        return tuple(dict.fromkeys(str(name).strip() for name in (
            persona.card.data.get('name'), persona.card.data.get('nickname'), *persona.record.aliases,
        ) if name and str(name).strip()))

    def _persona(self, conversation_id: str) -> Persona:
        if self.personas.pending(conversation_id):
            raise ValueError('persona switch incomplete; inspect switch status before replying')
        persona = self.personas.effective(conversation_id)
        self.sessions.reconcile_persona(conversation_id, persona.token, persona.cutoff,
                                        explicit=persona.epoch > 0)
        return persona

    def _prepare_persona(self, conversation_id: str, session_id: str,
                         event: MessageEvent | None, text: str) -> Persona:
        persona = self._persona(conversation_id)
        if self.conversations is None:
            raise DSHError('conversation persona store unavailable')
        scope, chat_id = target_of(conversation_id)
        user_id = event.user_id if event else (chat_id if scope == 'private' else 0)
        user_name = event.sender_name if event else ('大家' if scope == 'group' else '对方')
        self.conversations.record(session_id, scope, user_id,
                                  chat_id if scope == 'group' else None, user_name)
        lore = persona.card.lorebook.select(text, max_chars=self.config.bot.lore_max_chars)
        prompt = build_system_prompt(persona.card.data, persona.card.name, user_name,
                                     datetime.now(TZ), lore)
        self.conversations.prepare_persona(session_id, conversation_id, persona.token, prompt)
        return persona

    def _opening_context(self, conversation_id: str) -> str:
        opening = self.personas.opening(conversation_id)
        return '\n\n[此前已发送的角色开场白，不要再发送]\n' + opening if opening else ''

    async def switch_card(self, event: MessageEvent, record: CardRecord) -> dict:
        if not self.is_admin(event.user_id):
            raise ValueError('only administrators can switch personas')
        if event.message_type == 'group' and event.group_id:
            conversation_id = f'group:{event.group_id}'
        elif event.message_type == 'private':
            conversation_id = f'private:{event.user_id}'
        else:
            raise ValueError('unknown conversation')
        job = self.personas.enqueue(conversation_id, record, actor=str(event.user_id))
        if job['phase'] == 'queued':
            await self._execute_switch(job['id'])
        return self.personas.job(job['id'])

    async def _send_receipt(self, scope: str, chat_id: int, text: str) -> dict:
        if scope == 'group':
            result = await self.onebot.send_group(chat_id, text)
            self._remember_own_message(chat_id, int(result.get('message_id') or 0))
            self.proactive.note_speaking(chat_id, time.time())
        else:
            result = await self.onebot.send_private(chat_id, text)
        if not result.get('message_id'):
            raise OneBotError('send accepted without a receipt; delivery unknown')
        return result

    async def _execute_switch(self, job_id: str) -> None:
        job = self.personas.job(job_id)
        conversation_id = job['conversation_id']
        async with self._locks.setdefault(conversation_id, asyncio.Lock()):
            if not self.personas.claim(job_id):
                return
            try:
                if job['actor'] != 'cli' and (not job['actor'].isdigit()
                                              or not self.is_admin(int(job['actor']))):
                    raise ValueError('switch actor is no longer an administrator')
                current = self.personas.effective(conversation_id)
                already_bound = bool(job['applied_token']) and current.token == job['applied_token']
                if not already_bound and current.token != job['expected_token']:
                    raise ValueError('persona changed while switch was queued')
                scope, chat_id = target_of(conversation_id)
                card = CharacterCard.from_payload(json.loads(job['payload']))
                receipts = json.loads(job['receipts'])
                if not already_bound:
                    notice = f"系统即将切换本{'群' if scope == 'group' else '会话'}人设：{current.card.name} → {card.name}，请稍候。"
                    self.personas.update(job_id, phase='announcing', announcement=notice)
                    if not any(r.get('kind') == 'announcement' for r in receipts):
                        receipt = await self._send_receipt(scope, chat_id, notice)
                        receipts.append({'kind': 'announcement', 'message_id': receipt['message_id']})
                    self.personas.update(job_id, phase='announced', receipts=json.dumps(receipts))
                    current = self.personas.bind(job_id, self.memory.last_id(conversation_id))
                    self.sessions.reconcile_persona(conversation_id, current.token, current.cutoff, explicit=True)
                    if scope == 'group':
                        self.group_context.drain(chat_id)
                elif current.card.payload != card.payload:
                    raise ValueError('switch target no longer matches current persona')
                self.sessions.reconcile_persona(conversation_id, current.token, current.cutoff, explicit=True)
                opening = job['opening'] or substitute(card.data.get('first_mes', ''), card.name,
                                                       '大家' if scope == 'group' else '你', datetime.now(TZ))
                self.personas.update(job_id, phase='opening', opening=opening)
                # Preserve card text exactly; split only at the transport length limit.
                limit = max(1, self.config.bot.max_reply_chars)
                chunks = [opening[i:i + limit] for i in range(0, len(opening), limit)]
                for index, chunk in enumerate(chunks):
                    if any(r.get('kind') == 'opening' and r.get('index') == index for r in receipts):
                        continue
                    receipt = await self._send_receipt(scope, chat_id, chunk)
                    receipts.append({'kind': 'opening', 'index': index, 'message_id': receipt['message_id']})
                    self.personas.update(job_id, receipts=json.dumps(receipts))
                self.personas.consume_opening(conversation_id)
                self.personas.update(job_id, phase='complete', opening_pending=int(bool(opening)), error='')
                log.info('persona switched: conv=%s card=%s job=%s', conversation_id, card.name, job_id)
            except Exception as exc:  # noqa: BLE001 - retain exact delivery boundary
                phase = self.personas.job(job_id)['phase']
                unknown = phase in ('announcing', 'opening') and not isinstance(exc, OneBotRejected)
                bound = bool(self.personas.job(job_id)['applied_token'])
                self.personas.update(job_id, phase='unknown' if unknown else ('paused' if bound or phase == 'announced' else 'failed'),
                                     error=f'{type(exc).__name__}: {str(exc)[:300]}')
                log.exception('persona switch failed: job=%s phase=%s', job_id, phase)

    def start_personas(self) -> None:
        self.personas.recover()
        self._persona_worker = asyncio.create_task(self._persona_loop(), name='qqbot-personas')

    async def _persona_loop(self) -> None:
        while True:
            try:
                if self.onebot.connected:
                    for job in self.personas.queued():
                        await self._execute_switch(job['id'])
            except Exception:  # noqa: BLE001 - one bad request cannot stop the worker
                log.exception('persona worker failed')
            await asyncio.sleep(1)
