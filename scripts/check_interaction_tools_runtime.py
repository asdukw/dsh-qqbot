"""Pinned real SDK + local fake model + mock OneBot, without QQ/external model calls.

Run explicitly: python scripts/check_interaction_tools_runtime.py.
All SDK state is temporary; the repository plugin is loaded from its absolute URI.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bot'))

from deepseek_harness import DeepSeekHarness  # pyright: ignore[reportMissingImports]  # noqa: E402
from dsh_client import DSHClient, DSHConfig, DSHTurn  # noqa: E402
from group_context import GroupLine, render_line  # noqa: E402
from interaction_service import InteractionService  # noqa: E402
from onebot import OneBotConfig, OneBotWSClient  # noqa: E402

TOOLS = {'reply_to_others', 'mention_others', 'react_to_others'}
UNRELATED = {'card_library', 'card_online_search', 'card_install', 'video_resolve', 'video_send'}
USER_A, USER_B = '9007199254740993', '9007199254740995'
PHASE = 'group'
STEP = 0
REQUESTS = 0
FAILURES: list[str] = []
CALLS: dict[str, tuple[tuple[str, dict[str, Any], str], ...]] = {
    'group': (
        ('reply_to_others', {'message_id': '-12', 'text': 'QUOTE_AND_MENTION', 'user_ids': [USER_A, USER_B]}, 'sent'),
        ('reply_to_others', {'message_id': '34', 'text': 'SECOND_REPLY'}, 'sent'),
        ('mention_others', {'user_ids': [USER_B, USER_A], 'text': 'MULTIPLE_USERS'}, 'sent'),
        ('react_to_others', {'message_id': '34', 'emoji_id': '76'}, 'reacted'),
    ),
    'proactive': (
        ('mention_others', {'user_ids': [USER_A], 'text': 'PROACTIVE_INTERACTION'}, 'sent'),
    ),
    'private': (
        ('reply_to_others', {'message_id': '56', 'text': 'PRIVATE_QUOTE'}, 'sent'),
        ('mention_others', {'user_ids': ['7'], 'text': 'MUST_NOT_SEND'}, 'rejected'),
        ('reply_to_others', {'message_id': '56', 'text': 'x' * 1201}, 'rejected'),
    ),
    'reaction-empty': (
        ('react_to_others', {'message_id': '56', 'emoji_id': '124'}, 'reacted'),
    ),
    'reaction-whitespace': (
        ('react_to_others', {'message_id': '56', 'emoji_id': '76'}, 'reacted'),
    ),
    'reaction-no-block': (
        ('react_to_others', {'message_id': '56', 'emoji_id': '124'}, 'reacted'),
    ),
    'unauthorized': (),
}


def check_schemas(schemas: dict[str, dict[str, Any]]) -> None:
    """Check the schemas actually sent to the model, not merely plugin source."""
    expected = {
        'reply_to_others': ({'message_id', 'text'}, {'message_id', 'text', 'user_ids'}),
        'mention_others': ({'user_ids', 'text'}, {'user_ids', 'text'}),
        'react_to_others': ({'message_id', 'emoji_id'}, {'message_id', 'emoji_id'}),
    }
    for name, (required, properties) in expected.items():
        schema = schemas[name]['input_schema']
        assert schema['type'] == 'object' and schema['additionalProperties'] is False, schema
        assert set(schema['required']) == required, schema
        assert set(schema['properties']) == properties, schema
        props = schema['properties']
        if 'message_id' in props:
            spec = props['message_id']
            assert spec['type'] == 'string', spec
            for value in ('-12', '34', '9007199254740993'):
                assert re.fullmatch(spec['pattern'], value), spec
            for value in ('0', '-0', '01', '+1', '1.2', 'all'):
                assert not re.fullmatch(spec['pattern'], value), spec
        if 'user_ids' in props:
            spec = props['user_ids']
            assert spec['type'] == 'array' and spec['maxItems'] == 3 and spec['uniqueItems'], spec
            assert spec['items']['type'] == 'string', spec
            if name == 'mention_others':
                assert spec['minItems'] == 1, spec
            for value in ('all', '0', '-1', '01'):
                assert not re.fullmatch(spec['items']['pattern'], value), spec
            assert re.fullmatch(spec['items']['pattern'], USER_A), spec
        if 'text' in props:
            assert props['text']['type'] == 'string' and props['text']['maxLength'] == 4000, props
        if 'emoji_id' in props:
            assert props['emoji_id']['type'] == 'string', props
            assert re.fullmatch(props['emoji_id']['pattern'], '76'), props


def model_reply(body: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    global STEP
    schemas = {tool['name']: tool for tool in body.get('tools', [])}
    assert not UNRELATED.intersection(schemas), schemas.keys()
    if PHASE == 'unauthorized':
        assert not TOOLS.intersection(schemas), schemas.keys()
        return '', {}, 'INTERACTION_TOOLS_OK'
    assert TOOLS <= schemas.keys(), schemas.keys()
    check_schemas(schemas)
    system = json.dumps(body.get('system', ''), ensure_ascii=False)
    assert '本轮 QQ 自主互动工具可用' in system, system
    assert 'max_reply_chars' in system and 'unknown' in system, system
    blocks = body['messages'][-1].get('content', [])
    results = [block for block in blocks if isinstance(block, dict) and block.get('type') == 'tool_result'] if isinstance(blocks, list) else []
    # A preceding failed no-block turn leaves a tool-result as the final
    # history item. It is not a result for this new phase's first request.
    if results and STEP > 0:
        assert len(results) == 1, results
        content = results[0]['content']
        if not isinstance(content, str):
            content = ''.join(item.get('text', '') for item in content if isinstance(item, dict))
        assert not results[0].get('is_error'), results
        result = json.loads(content)
        expected = CALLS[PHASE][STEP - 1][2]
        assert result.get('status') == expected, (PHASE, expected, result)
        if PHASE == 'private' and STEP == 3:
            assert '1200' in result.get('error', ''), content
    else:
        assert STEP == 0, (PHASE, STEP, blocks)
    if STEP < len(CALLS[PHASE]):
        name, args, _status = CALLS[PHASE][STEP]
        STEP += 1
        return name, args, ''
    if PHASE == 'reaction-whitespace':
        return '', {}, '  '
    return '', {}, '' if PHASE in ('reaction-empty', 'reaction-no-block') else 'INTERACTION_TOOLS_OK'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        global REQUESTS
        self.connection.settimeout(5)
        REQUESTS += 1
        try:
            size = int(self.headers.get('content-length', '0'))
            if not 0 < size <= 2 * 1024 * 1024:
                raise ValueError('invalid local fixture request size')
            name, args, text = model_reply(json.loads(self.rfile.read(size)))
        except Exception as exc:  # Send a terminal response even when validation fails.
            FAILURES.append(f'{PHASE} model request {REQUESTS}: {type(exc).__name__}: {exc}')
            name, args, text = '', {}, 'SMOKE_FAILURE'
        events: list[dict[str, Any]] = [
            {'type': 'message_start', 'message': {
                'id': f'msg_{REQUESTS}', 'model': 'smoke-model', 'type': 'message',
                'role': 'assistant', 'content': [], 'stop_reason': None,
                'usage': {'input_tokens': 3, 'output_tokens': 0},
            }},
        ]
        if name:
            events += [
                {'type': 'content_block_start', 'index': 0, 'content_block': {
                    'type': 'tool_use', 'id': f'call_{REQUESTS}', 'name': name, 'input': {}}},
                {'type': 'content_block_delta', 'index': 0, 'delta': {
                    'type': 'input_json_delta', 'partial_json': json.dumps(args)}},
                {'type': 'content_block_stop', 'index': 0},
            ]
        elif text:
            events += [
                {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
                {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text}},
                {'type': 'content_block_stop', 'index': 0},
            ]
        elif PHASE == 'reaction-empty':
            # The pinned provider rejects zero content as EMPTY_RESPONSE. A
            # thinking-only terminal is valid content with truly empty visible text.
            events += [
                {'type': 'content_block_start', 'index': 0, 'content_block': {
                    'type': 'thinking', 'thinking': ''}},
                {'type': 'content_block_delta', 'index': 0, 'delta': {
                    'type': 'thinking_delta', 'thinking': 'The reaction completed; no visible text.'}},
                {'type': 'content_block_stop', 'index': 0},
            ]
        # reaction-no-block deliberately emits no content blocks at all. The
        # pinned provider must preserve its EMPTY_RESPONSE error, not success.
        events += [
            {'type': 'message_delta', 'delta': {
                'stop_reason': 'tool_use' if name else 'end_turn', 'stop_sequence': None},
                'usage': {'output_tokens': 3}},
            {'type': 'message_stop'},
        ]
        payload = ''.join('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n' for event in events).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Connection', 'close')
        self.end_headers()
        try:
            self.wfile.write(payload)
            self.wfile.flush()
        except (OSError, TimeoutError) as exc:
            FAILURES.append(f'{PHASE} local fixture response failed: {exc}')
        self.close_connection = True


async def main() -> None:
    global PHASE, STEP
    assert version('deepseek-harness-sdk') == '0.2.1rc2', 'requires fixed SDK 0.2.1rc2'
    destination = '/opt/qqbot/dsh/plugins/interaction-tools.mjs'
    for path in ('dsh/qqbot.cordis.patch.yml', 'deploy/install-bot.sh'):
        assert destination in (ROOT / path).read_text(encoding='utf-8'), path
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    onebot = OneBotWSClient(OneBotConfig(), AsyncMock())
    onebot_calls: list[tuple[str, dict[str, Any], float]] = []

    async def mock_call(action: str, params: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        onebot_calls.append((action, params, timeout))
        assert action in ('send_group_msg', 'send_private_msg', 'set_msg_emoji_like'), action
        return {} if action == 'set_msg_emoji_like' else {'message_id': 8000 + len(onebot_calls)}

    onebot.call = mock_call  # type: ignore[method-assign]

    async def sender(conversation: str, text: str, reply_to: int | None,
                     user_ids: tuple[int, ...], timeout: float) -> dict[str, Any]:
        scope, _, chat_id = conversation.partition(':')
        return await onebot.send_interaction(scope, int(chat_id), text, reply_to=reply_to,
                                             user_ids=user_ids, timeout=timeout)

    service = InteractionService(sender, onebot.set_msg_emoji_like)
    try:
        await service.start()
        with tempfile.TemporaryDirectory(prefix='qqbot-interaction-smoke-') as tmp:
            root = Path(tmp)
            profile = root / 'interaction.patch.json'
            profile.write_text(json.dumps([
                {'id': 'session-log-deepseek', 'config': {'enabled': False}},
                {'id': 'session-telemetry-otel', 'disabled': True},
                {'insert': [
                    {'id': 'interaction-tools', 'name': (ROOT / 'dsh/interaction-tools.mjs').as_uri()},
                    {'id': 'video-tools', 'name': (ROOT / 'dsh/video-tools.mjs').as_uri()},
                    {'id': 'web', 'name': '@deepseek-ai/dsh-web',
                     'config': {'searchProvider': 'unused-local-fixture'}},
                    {'id': 'card-tools', 'name': (ROOT / 'dsh/card-tools.mjs').as_uri()},
                ]},
            ]), encoding='utf-8')
            client = DSHClient(DSHConfig(
                provider='deepseek-official', model='smoke-model', profile='sdk-minimal',
                workspace=str(root), home=str(root / 'home'), patches=(str(profile),),
                request_timeout=30,
            ))
            base_env = {'DSH_TELEMETRY_DISABLED': '1', 'OTEL_SDK_DISABLED': 'true',
                        'HOME': str(root), 'USERPROFILE': str(root), 'NO_PROXY': '*'}
            for family in ('CARD', 'VIDEO', 'INTERACTION'):
                for suffix in ('BRIDGE', 'SECRET', 'LEASE'):
                    base_env[f'QQBOT_{family}_{suffix}'] = ''
            group_event = SimpleNamespace(message_type='group', user_id=7, message_id=99, group_id=123)
            private_event = SimpleNamespace(message_type='private', user_id=7, message_id=56, group_id=None)
            group_targets = (
                GroupLine('同名', '第一位参与者', user_id=int(USER_A), message_id=-12),
                GroupLine('同名', '第二位参与者', user_id=int(USER_B), message_id=34),
                GroupLine('触发者', '聊聊吧', user_id=7, message_id=99),
            )
            private_targets = (GroupLine('用户', '当前私聊', user_id=7, message_id=56),)
            session = 'interaction-session'
            # Recreate runtimes while keeping this session's actual on-disk history.
            for phase in CALLS:
                PHASE, STEP = phase, 0
                event = private_event if phase == 'private' or phase.startswith('reaction-') else group_event
                conversation = 'private:7' if event is private_event else 'group:123'
                targets = private_targets if event is private_event else group_targets
                lease = None if phase == 'unauthorized' else service.open(
                    event, conversation, session, targets, lifetime=120, max_text_chars=1200)
                env = service.env(lease) if lease else {}
                before = len(onebot_calls)

                def make_harness(**kwargs: Any) -> Any:
                    # Simulate inherited tool credentials without changing os.environ.
                    # DSHClient's explicit per-runtime clear list must override them.
                    ambient = {f'QQBOT_{family}_{key}': value
                               for family in ('CARD', 'VIDEO', 'INTERACTION')
                               for key, value in (('BRIDGE', 'http://127.0.0.1:1'),
                                                  ('SECRET', 'ambient_secret_123456'),
                                                  ('LEASE', 'ambient_lease_1234567'))}
                    runtime_env = {**base_env, **ambient, **kwargs.pop('env')}
                    return DeepSeekHarness(**kwargs, env=runtime_env, api_key='local-fake-key',
                                           base_url=f'http://127.0.0.1:{server.server_port}')

                def run(env: dict[str, str] = env, phase: str = phase,
                        targets: tuple[GroupLine, ...] = targets) -> DSHTurn:
                    prompt = f'LOCAL_FIXTURE_PHASE {phase}' + chr(10) + chr(10).join(map(render_line, targets))
                    if phase == 'unauthorized':
                        try:
                            reply, events = client.run_with_events(session, prompt)
                            return DSHTurn(reply, events)
                        finally:
                            client.close()
                    return client.run_authorized_result(session, prompt, (), env)

                try:
                    with patch('dsh_client.DeepSeekHarness', side_effect=make_harness):
                        result = await asyncio.to_thread(run)
                    assert not FAILURES, '\n'.join(FAILURES)
                    assert STEP == len(CALLS[phase]), (phase, STEP)
                    expected_final = '' if phase.startswith('reaction-') else 'INTERACTION_TOOLS_OK'
                    assert result.reply == expected_final, result
                    if phase == 'reaction-no-block':
                        assert result.finish_reason == 'error' and result.error.startswith('EMPTY_RESPONSE:'), result
                        ending = [event for event in result.events if event.get('type') == 'turn/end'][-1]
                        assert ending['data']['reason']['error']['code'] == 'EMPTY_RESPONSE', ending
                        assert any(event.get('type') == 'llm/retry' for event in result.events), result.events
                    else:
                        assert not result.error, result.error
                        if phase.startswith('reaction-'):
                            assert result.finish_reason == 'completed', result.finish_reason
                    if phase == 'reaction-empty':
                        assert result.finish_reason == 'completed', result.finish_reason
                        messages = [event['data']['message'] for event in result.events
                                    if event.get('type') == 'assistant/message']
                        terminal = messages[-1]['content']
                        assert terminal and all(block['type'] == 'reasoning' for block in terminal), terminal
                    if lease:
                        await service.settle(lease)
                        assert lease.has_confirmed and not lease.has_unknown, lease.receipts
                        assert lease.has_text == (not phase.startswith('reaction-')), lease.receipts
                    if phase == 'group':
                        calls = onebot_calls[before:]
                        assert len(calls) == 4, calls
                        assert calls[0][0] == 'send_group_msg' and calls[0][1]['group_id'] == 123, calls
                        segments = calls[0][1]['message']
                        assert [segment['type'] for segment in segments] == ['reply', 'at', 'at', 'text'], segments
                        assert segments[0]['data']['id'] == '-12', segments
                        assert [segment['data']['qq'] for segment in segments[1:3]] == [USER_A, USER_B], segments
                        assert calls[1][1]['message'][-1]['data']['text'] == 'SECOND_REPLY', calls
                        assert calls[2][1]['message'][-1]['data']['text'] == 'MULTIPLE_USERS', calls
                        assert calls[3][0] == 'set_msg_emoji_like' and calls[3][2] <= 2, calls
                        assert lease is not None and len(lease.sent_texts) == 3, lease
                    elif phase == 'private':
                        calls = onebot_calls[before:]
                        assert len(calls) == 1 and calls[0][0] == 'send_private_msg', calls
                        assert calls[0][1]['user_id'] == 7, calls
                        assert [item['type'] for item in calls[0][1]['message']] == ['reply', 'text'], calls
                    elif phase.startswith('reaction-'):
                        assert not lease or not lease.sent_texts, lease
                        assert len(onebot_calls) == before + 1, onebot_calls[before:]
                        assert onebot_calls[-1][0] == 'set_msg_emoji_like', onebot_calls[-1]
                    elif phase == 'unauthorized':
                        assert len(onebot_calls) == before, onebot_calls[before:]
                    else:
                        assert len(onebot_calls) == before + 1, onebot_calls[before:]
                        assert onebot_calls[-1][0] == 'send_group_msg', onebot_calls[-1]
                finally:
                    if lease:
                        service.revoke(lease)
                        await service.settle(lease)
            await asyncio.to_thread(client.close)
            print('PASS: SDK 0.2.1rc2 actual schemas + all 3 dispatches; signed quote + multi-user mention; '
                  'normal/proactive/private scopes; private mention and oversize text rejected; '
                  'reaction-only thinking/whitespace empty-visible completion; true no-block EMPTY_RESPONSE preserved; '
                  f'later unauthorized tools absent; no inherited card/video privilege ({REQUESTS} local model requests)')
    finally:
        await service.close()
        await onebot.close()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=5)


if __name__ == '__main__':
    asyncio.run(main())
