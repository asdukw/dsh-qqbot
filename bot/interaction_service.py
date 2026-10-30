"""Snapshot-authorized QQ interactions and a turn-local, cancellation-safe ledger."""

from __future__ import annotations

import asyncio
import hmac
import json
import math
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from content_policy import require_safe
from group_context import GroupLine
from meme import extract_meme_marker
from onebot import OneBotRejected
from relationship_rules import parse_chat_proposal
from schedule import extract_schedule, render_schedule
from stickers import extract_sticker_marker
from tasks import extract_task_management, extract_task_marker
from voice import extract_voice_marker

MAX_ACTIONS = 6
MAX_TEXT_ACTIONS = 3
MAX_REQUESTS = 24
MAX_USERS = 3
MAX_CONNECTIONS = 16
MAX_LEASES = 16
MAX_HEADER_BYTES = 8192
MAX_REQUEST_BYTES = 65536
MAX_RESPONSE_BYTES = 8192
TEXT_TIMEOUT = 10.0
REACTION_TIMEOUT = 2.0
REQUEST_TIMEOUT = 45.0

Sender = Callable[[str, str, int | None, tuple[int, ...], float], Awaitable[dict]]
Reactor = Callable[..., Awaitable[dict]]
Fingerprint = tuple[str, int | None, tuple[int, ...], str, str]


def _real_id(value: Any, *, positive: bool = False) -> bool:
    return type(value) is int and (value > 0 if positive else value != 0)


def _model_id(value: Any, *, positive: bool = False) -> int:
    pattern = r'[1-9][0-9]{0,63}' if positive else r'-?[1-9][0-9]{0,63}'
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError('IDs must be canonical nonzero decimal strings')
    return int(value)


def _reject_markers(text: str) -> None:
    # Existing pure parsers remove even malformed relation fences. Ignore their
    # whitespace normalization, but reject removed controls rather than run them.
    if re.search(r'\[(?:好感(?:度)?|印象|关系)', text):
        raise ValueError('hidden control markers are not allowed; use ordinary final output')
    compact = ''.join(text.split())
    for parser in (
        parse_chat_proposal, extract_task_marker,
        extract_task_management, extract_voice_marker, extract_sticker_marker,
        extract_meme_marker,
    ):
        visible, _ = parser(text)
        if ''.join(visible.split()) != compact:
            raise ValueError('hidden control markers are not allowed; use ordinary final output')


def _safe_text(value: Any, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_REQUEST_BYTES:
        raise ValueError('text must be a nonempty bounded string')
    if len(value) > max_chars:
        raise ValueError(f'text must contain 1..{max_chars} characters; shorten it and try again')
    text = require_safe(value.strip())
    _reject_markers(text)
    outer, schedule = extract_schedule(text)
    if schedule is not None:
        text = '\n\n'.join(part for part in (outer, render_schedule(schedule)) if part)
    elif outer != text:
        raise ValueError('invalid schedule; use ordinary visible text')
    require_safe(text)
    _reject_markers(text)
    if not text.strip() or len(text) > max_chars:
        raise ValueError(f'text must contain 1..{max_chars} characters; shorten it and try again')
    return text


@dataclass(eq=False)
class InteractionAction:
    operation: str
    message_id: int | None
    user_ids: tuple[int, ...]
    text: str = ''
    emoji_id: str = ''
    started: bool = False
    result: dict | None = None
    receipt: dict | None = None
    task: asyncio.Task | None = None


@dataclass(eq=False)
class InteractionLease:
    nonce: str
    session: str
    expires: float
    conversation: str
    actor: int
    targets: tuple[GroupLine, ...]
    message_ids: frozenset[int]
    user_ids: frozenset[int]
    max_text_chars: int
    calls: int = 0
    actions: dict[Fingerprint, InteractionAction] = field(default_factory=dict)
    tasks: set[asyncio.Task] = field(default_factory=set)
    expiry: asyncio.TimerHandle | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    on_unknown: Callable[[], None] | None = None
    unknown_notified: bool = False
    transport_unknown: bool = False
    pending_receipt: bool = False
    activation_lifetime: float = 0

    def freeze(self) -> None:
        if self.has_unknown and not self.unknown_notified:
            self.unknown_notified = True
            if self.on_unknown is not None:
                self.on_unknown()

    @property
    def has_confirmed(self) -> bool:
        return any(a.result and a.result['status'] in ('sent', 'reacted')
                   for a in self.actions.values())

    @property
    def has_unknown(self) -> bool:
        return self.transport_unknown or any(a.result and a.result['status'] == 'unknown' for a in self.actions.values())

    @property
    def sent_texts(self) -> list[str]:
        return [a.text for a in self.actions.values()
                if a.result is not None and a.result['status'] == 'sent']

    @property
    def has_text(self) -> bool:
        return bool(self.sent_texts)

    @property
    def text_complete(self) -> bool:
        text_actions = [a for a in self.actions.values() if a.operation != 'react']
        return bool(text_actions) and not self.has_unknown and all(
            a.result is not None and a.result['status'] == 'sent' for a in text_actions
        )

    @property
    def receipts(self) -> list[dict]:
        return [dict(a.receipt) for a in self.actions.values()
                if a.receipt is not None and a.result is not None
                and a.result['status'] == 'sent']


class InteractionValidationError(ValueError):
    """Safe post-authorization feedback for the tool, never transport details."""


class InteractionService:
    def __init__(self, sender: Sender, reactor: Reactor):
        self.sender = sender
        self.reactor = reactor
        self.secret = secrets.token_urlsafe(32)
        self.url = ''
        self.leases: dict[str, InteractionLease] = {}
        self._server: asyncio.Server | None = None
        self._lock = asyncio.Lock()
        self._connections: set[asyncio.Task] = set()
        # Revoked leases leave authorization immediately, not cleanup.
        self._tasks: set[asyncio.Task] = set()
        self._closed = False

    async def start(self) -> None:
        async with self._lock:
            if self._closed:
                raise ValueError('interaction service is closed')
            if self._server is None:
                self._server = await asyncio.start_server(
                    self._http, '127.0.0.1', 0, limit=MAX_HEADER_BYTES,
                )
                self.url = f'http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}'

    def open(
        self, event, conversation: str, session: str, targets: tuple[GroupLine, ...], *,
        lifetime: float = 120, max_text_chars: int = 1200, deferred: bool = False,
    ) -> InteractionLease:
        if (self._closed or not _real_id(event.message_id)
                or not _real_id(event.user_id, positive=True)
                or not isinstance(session, str) or not session or len(session) > 512):
            raise ValueError('interaction authority requires a real chat message and session')
        scope = event.message_type
        chat_id = event.user_id if scope == 'private' else getattr(event, 'group_id', None)
        if (scope not in ('group', 'private') or not _real_id(chat_id, positive=True)
                or conversation != f'{scope}:{chat_id}'):
            raise ValueError('interaction conversation mismatch')
        if (not isinstance(targets, tuple) or not targets
                or any(not isinstance(line, GroupLine)
                       or not _real_id(line.user_id, positive=True)
                       or not _real_id(line.message_id)
                       for line in targets)):
            raise ValueError('interaction targets must be real immutable visible messages')
        if scope == 'private' and any(line.user_id != event.user_id for line in targets):
            raise ValueError('private targets must belong to the actual peer')
        if type(max_text_chars) is not int or max_text_chars <= 0:
            raise ValueError('max_text_chars must be positive')
        if type(lifetime) not in (int, float) or not math.isfinite(lifetime) or lifetime <= 0:
            raise ValueError('interaction lifetime must be positive and finite')
        if len(self.leases) >= MAX_LEASES:
            raise ValueError('too many interaction turns')
        ttl = min(600.0, lifetime)
        lease = InteractionLease(
            nonce=secrets.token_urlsafe(32), session=session, expires=time.monotonic() + ttl,
            conversation=conversation, actor=event.user_id, targets=targets,
            message_ids=frozenset(line.message_id for line in targets),
            user_ids=frozenset(line.user_id for line in targets),
            max_text_chars=min(max_text_chars, 4000),
        )
        self.leases[lease.nonce] = lease
        if deferred:
            lease.activation_lifetime = ttl
            # Queued authority has no execution budget yet. Bound queue life
            # separately and never re-activate a revoked lease.
            lease.expires = time.monotonic() + 3600
            lease.expiry = asyncio.get_running_loop().call_later(3600, self.revoke, lease)
        else:
            lease.expiry = asyncio.get_running_loop().call_later(ttl, self.revoke, lease)
        return lease

    def activate(self, lease: InteractionLease) -> None:
        self.authorize(lease.nonce, lease.session)
        if lease.activation_lifetime:
            if lease.expiry is not None:
                lease.expiry.cancel()
            ttl, lease.activation_lifetime = lease.activation_lifetime, 0
            lease.expires = time.monotonic() + ttl
            lease.expiry = asyncio.get_running_loop().call_later(ttl, self.revoke, lease)

    def env(self, lease: InteractionLease) -> dict[str, str]:
        if not self.url or self.authorize(lease.nonce, lease.session) is not lease:
            raise ValueError('interaction service is not started or lease is foreign')
        return {
            'QQBOT_INTERACTION_BRIDGE': self.url,
            'QQBOT_INTERACTION_SECRET': self.secret,
            'QQBOT_INTERACTION_LEASE': lease.nonce,
        }

    @staticmethod
    def _cancel(lease: InteractionLease, action: InteractionAction, reason: str) -> None:
        if action.task is not None and not action.task.done():
            if action.result is None:
                action.result = {
                    'status': 'unknown' if action.started else 'not_sent',
                    'operation': action.operation, 'error': reason,
                }
            action.task.cancel()
            lease.freeze()

    def revoke(self, lease: InteractionLease) -> None:
        if lease.pending_receipt:
            lease.transport_unknown = True
            lease.pending_receipt = False
            lease.freeze()
        if self.leases.get(lease.nonce) is lease:
            self.leases.pop(lease.nonce)
        if lease.expiry is not None:
            lease.expiry.cancel()
        for action in lease.actions.values():
            self._cancel(lease, action, 'interaction authority revoked; do not retry started actions')

    async def settle(self, lease: InteractionLease) -> None:
        await asyncio.gather(
            *(a.task for a in lease.actions.values() if a.task is not None),
            return_exceptions=True,
        )

    def authorize(self, nonce: str, session: str) -> InteractionLease:
        if not isinstance(nonce, str) or not isinstance(session, str):
            raise ValueError('invalid interaction authority')
        lease = self.leases.get(nonce)
        if (self._closed or lease is None or lease.session != session
                or lease.expires <= time.monotonic()):
            if lease is not None and lease.expires <= time.monotonic():
                self.revoke(lease)
            raise ValueError('interaction authority expired or invalid')
        return lease

    def _normalize(self, lease: InteractionLease, args: dict, operation: str) -> InteractionAction:
        fields = {
            'reply': ({'message_id', 'text'}, {'user_ids'}),
            'mention': ({'user_ids', 'text'}, set()),
            'react': ({'message_id', 'emoji_id'}, set()),
        }
        if not isinstance(operation, str) or operation not in fields:
            raise ValueError('unsupported interaction operation')
        required, optional = fields[operation]
        if not isinstance(args, dict) or not required <= set(args) or set(args) - required - optional:
            raise ValueError(f'invalid {operation} parameters')
        message_id = None
        if operation in ('reply', 'react'):
            message_id = _model_id(args['message_id'])
            if message_id not in lease.message_ids:
                raise ValueError('message is not in this turn visible snapshot')
        raw_users = args.get('user_ids', [])
        if not isinstance(raw_users, list) or len(raw_users) > MAX_USERS:
            raise ValueError('user_ids must be a list of at most three visible users')
        users = tuple(dict.fromkeys(_model_id(value, positive=True) for value in raw_users))
        if any(user not in lease.user_ids for user in users):
            raise ValueError('user is not in this turn visible snapshot')
        if (operation == 'mention' or users) and not lease.conversation.startswith('group:'):
            raise ValueError('mentions are only allowed in the current group')
        if operation == 'mention' and not users:
            raise ValueError('mention requires at least one visible user')
        action = InteractionAction(operation, message_id, users)
        if operation == 'react':
            if message_id is None or message_id <= 0:
                raise ValueError('reaction does not support negative message IDs')
            emoji = args['emoji_id']
            if not isinstance(emoji, str) or re.fullmatch(r'[1-9][0-9]{0,9}', emoji) is None:
                raise ValueError('emoji_id must be a numeric emoji ID string')
            action.emoji_id = emoji
        else:
            action.text = _safe_text(args['text'], lease.max_text_chars)
        return action

    async def call(
        self, nonce: str, session: str, args: dict, operation: str = 'reply',
    ) -> dict:
        lease = self.authorize(nonce, session)
        lease.calls += 1
        if lease.calls > MAX_REQUESTS:
            raise InteractionValidationError('interaction request limit reached')
        try:
            action = self._normalize(lease, args, operation)
        except ValueError as exc:
            raise InteractionValidationError(str(exc)) from None
        key = (operation, action.message_id, tuple(sorted(action.user_ids)),
               action.text, action.emoji_id)
        previous = lease.actions.get(key)
        if previous is not None:
            action = previous
        else:
            if lease.has_unknown:
                return {'status': 'not_sent', 'operation': operation,
                        'error': 'previous action is uncertain; no further actions or retries'}
            if len(lease.actions) >= MAX_ACTIONS:
                raise InteractionValidationError('interaction action limit reached')
            if operation != 'react' and sum(a.operation != 'react' for a in lease.actions.values()) >= MAX_TEXT_ACTIONS:
                raise InteractionValidationError('interaction text action limit reached')
            # Claim both fingerprint and budget before the first await.
            lease.actions[key] = action
            action.task = asyncio.create_task(self._execute(lease, action))
            lease.tasks.add(action.task)
            self._tasks.add(action.task)
            action.task.add_done_callback(lambda task: self._finished(lease, task))
        if action.result is not None:
            return dict(action.result)
        assert action.task is not None
        try:
            return dict(await asyncio.shield(action.task))
        except asyncio.CancelledError:
            # Disconnect cancels the actual action, not merely its waiter.
            self._cancel(lease, action, 'interaction request cancelled; do not retry started actions')
            await asyncio.gather(action.task, return_exceptions=True)
            raise

    def _finished(self, lease: InteractionLease, task: asyncio.Task) -> None:
        lease.tasks.discard(task)
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _execute(self, lease: InteractionLease, action: InteractionAction) -> dict:
        try:
            async with lease.lock:
                self.authorize(lease.nonce, lease.session)
                if lease.has_unknown:
                    action.result = {'status': 'not_sent', 'operation': action.operation,
                                     'error': 'previous action is uncertain; no further actions or retries'}
                    return action.result
                timeout = min(REACTION_TIMEOUT if action.operation == 'react' else TEXT_TIMEOUT,
                              lease.expires - time.monotonic())
                if timeout <= 0:
                    raise ValueError('interaction authority expired')
                action.started = True
                # No await between a confirmed return and its ledger assignment.
                async with asyncio.timeout(timeout):
                    if action.operation == 'react':
                        data = await self.reactor(action.message_id, action.emoji_id, timeout=timeout)
                    else:
                        data = await self.sender(
                            lease.conversation, action.text, action.message_id,
                            action.user_ids, timeout,
                        )
                if not isinstance(data, dict):
                    raise RuntimeError('invalid OneBot receipt')
                if action.operation == 'react':
                    action.result = {'status': 'reacted', 'operation': 'react',
                                     'message_id': str(action.message_id), 'emoji_id': action.emoji_id}
                elif _real_id(data.get('message_id')):
                    action.receipt = dict(data)
                    action.result = {'status': 'sent', 'operation': action.operation,
                                     'message_id': str(data['message_id'])}
                else:
                    raise RuntimeError('missing valid OneBot message receipt')
        except asyncio.CancelledError:
            if action.result is None:
                action.result = {'status': 'unknown' if action.started else 'not_sent',
                                 'operation': action.operation,
                                 'error': 'interaction cancelled; do not retry started actions'}
            lease.freeze()
            raise
        except OneBotRejected:
            action.result = {'status': 'rejected', 'operation': action.operation,
                             'error': 'OneBot explicitly rejected the action'}
        except Exception:  # noqa: BLE001 - transport failure may have taken effect
            action.result = {'status': 'unknown' if action.started else 'not_sent',
                             'operation': action.operation,
                             'error': ('OneBot result is uncertain; do not retry' if action.started
                                       else 'interaction authority expired or invalid')}
        lease.freeze()
        assert action.result is not None
        return action.result

    async def close(self) -> None:
        self._closed = True
        for lease in tuple(self.leases.values()):
            self.revoke(lease)
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        connections = tuple(self._connections)
        actions = tuple(self._tasks)
        for task in connections:
            task.cancel()
        await asyncio.gather(*connections, *actions, return_exceptions=True)

    async def _http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is None or self._closed or len(self._connections) >= MAX_CONNECTIONS:
            writer.close()
            return
        self._connections.add(task)
        operation: asyncio.Task | None = None
        disconnect: asyncio.Task | None = None
        disconnected = False
        status, result = 400, {'error': 'invalid interaction bridge request'}
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with asyncio.timeout(5):
                    header = await reader.readuntil(b'\r\n\r\n')
                    if len(header) > MAX_HEADER_BYTES:
                        raise ValueError('invalid headers')
                    lines = header.decode('ascii').split('\r\n')
                    fields: dict[str, str] = {}
                    for line in lines[1:]:
                        if not line:
                            continue
                        key, separator, value = line.partition(':')
                        key = key.lower()
                        if (not separator or re.fullmatch(r'[a-z0-9-]+', key) is None
                                or key in fields):
                            raise ValueError('invalid headers')
                        fields[key] = value.strip()
                    if (lines[0] != 'POST /interaction HTTP/1.1'
                            or {'origin', 'transfer-encoding', 'expect'} & fields.keys()
                            or not hmac.compare_digest(fields.get('authorization', ''), 'Bearer ' + self.secret)):
                        raise ValueError('invalid authorization')
                    length = fields.get('content-length', '')
                    if re.fullmatch(r'[1-9][0-9]{0,5}', length) is None or int(length) > MAX_REQUEST_BYTES:
                        raise ValueError('invalid size')
                    body = json.loads(await reader.readexactly(int(length)),
                                      object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
                if (not isinstance(body, dict)
                        or set(body) - {'lease', 'session', 'args', 'operation', 'receipt_ack'}
                        or not {'lease', 'session', 'args'} <= set(body)
                        or not isinstance(body['lease'], str) or not isinstance(body['session'], str)):
                    raise ValueError('invalid envelope')
                lease = self.authorize(body['lease'], body['session'])
                internal = body.get('operation')
                if internal in ('ack', 'uncertain'):
                    if body.get('args') != {}:
                        raise ValueError('invalid internal receipt')
                    if internal == 'uncertain':
                        lease.transport_unknown = True
                        lease.freeze()
                        result = {'status': 'unknown'}
                    else:
                        lease.pending_receipt = False
                        result = {'status': 'acknowledged'}
                    status = 200
                    return
                if 'receipt_ack' in body and body['receipt_ack'] is not True:
                    raise ValueError('invalid receipt mode')
                operation = asyncio.create_task(self.call(
                    body['lease'], body['session'], body['args'], body.get('operation', 'reply'),
                ))
                disconnect = asyncio.create_task(reader.read(1))
                done, _ = await asyncio.wait((operation, disconnect), return_when=asyncio.FIRST_COMPLETED)
                if disconnect in done:
                    disconnected = True
                    return
                result = await operation
                if body.get('receipt_ack') is True and result.get('status') in ('sent', 'reacted'):
                    # The plugin acknowledges receipt separately. If its response
                    # is lost, runtime revocation preserves transport uncertainty.
                    lease.pending_receipt = True
                status = 200
        except InteractionValidationError as exc:
            result = {'status': 'rejected', 'error': str(exc)[:300]}
        except (ValueError, OSError, TimeoutError, UnicodeError, RecursionError,
                asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass

        finally:
            for pending in (operation, disconnect):
                if pending is not None and not pending.done():
                    pending.cancel()
            await asyncio.gather(*(p for p in (operation, disconnect) if p is not None),
                                 return_exceptions=True)
            if operation is not None and status != 200 and result.get('status') != 'rejected':
                # Dispatch may have reached OneBot before cancellation/expiry.
                # A generic HTTP 400 must never claim it definitely did not.
                status, result = 200, {'status': 'unknown', 'error': 'Interaction outcome is uncertain; do not retry'}
            try:
                if not disconnected:
                    data = json.dumps(result, ensure_ascii=False).encode()
                    if len(data) > MAX_RESPONSE_BYTES:
                        data, status = b'{"status":"unknown","error":"interaction result too large; do not retry"}', 200
                    writer.write(
                        f'HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n'.encode() + data,
                    )
                    async with asyncio.timeout(2):
                        await writer.drain()
            except (OSError, TimeoutError):
                pass
            finally:
                self._connections.discard(task)
                writer.close()
                try:
                    async with asyncio.timeout(2):
                        await writer.wait_closed()
                except (OSError, TimeoutError):
                    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    value: dict = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate JSON key')
        value[key] = item
    return value


def _invalid_constant(value: str) -> None:
    raise ValueError('invalid JSON constant')
