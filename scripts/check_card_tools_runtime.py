"""Exercise real pinned DSH tools against a local fake model/search and temporary library.

Run explicitly: python scripts/check_card_tools_runtime.py. No QQ or external API calls.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from card_fetch import RemoteCardContent  # noqa: E402
from card_service import CardService  # noqa: E402
from deepseek_harness import DeepSeekHarness  # pyright: ignore[reportMissingImports]  # noqa: E402
from personas import PersonaStore  # noqa: E402

from cards import CardRegistry, normalise  # noqa: E402

REQUESTS: list[dict] = []
PHASE = 'search'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['content-length'])))
        REQUESTS.append(body)
        names = {tool['name'] for tool in body.get('tools', [])}
        blocks = body.get('messages', [])[-1].get('content', [])
        results = [block for block in blocks if isinstance(block, dict) and block.get('type') == 'tool_result'] if isinstance(blocks, list) else []
        name, args = '', {}
        if PHASE == 'ordinary':
            assert 'card_install' not in names and 'card_online_search' not in names, names
        else:
            assert {'card_library', 'card_online_search', 'card_install'} <= names, names
            if not results:
                if PHASE == 'switch':
                    name, args = 'card_library', {'action': 'use', 'query': 'last_installed'}
                else:
                    name, args = ('card_online_search', {'query': 'friendly librarian'}) if PHASE == 'search' else ('card_library', {'action': 'candidates'})
            else:
                text = results[-1]['content']
                if not isinstance(text, str):
                    text = ''.join(item.get('text', '') for item in text if isinstance(item, dict))
                result = json.loads(text)
                if 'candidates' in result:
                    assert result['candidates'], result
                    name, args = 'card_install', {'candidate_id': result['candidates'][0]['candidate_id']}
                else:
                    assert result['status'] in ('installed', 'already_exists', 'accepted'), result
        events: list[dict[str, Any]] = [
            {'type': 'message_start', 'message': {'id': 'msg_card', 'model': 'smoke-model',
             'type': 'message', 'role': 'assistant', 'content': [], 'stop_reason': None,
             'usage': {'input_tokens': 3, 'output_tokens': 0}}},
        ]
        if name:
            events += [
                {'type': 'content_block_start', 'index': 0, 'content_block': {
                    'type': 'tool_use', 'id': f'call_{len(REQUESTS)}', 'name': name, 'input': {}}},
                {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(args)}},
            ]
        else:
            events += [
                {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
                {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'CARD_TOOLS_OK'}},
            ]
        events += [
            {'type': 'content_block_stop', 'index': 0},
            {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use' if name else 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': 3}},
            {'type': 'message_stop'},
        ]
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        for event in events:
            self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
        self.wfile.flush()


async def main() -> None:
    global PHASE
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='qqbot-card-smoke-') as tmp:
            root = Path(tmp)
            registry = CardRegistry(root / 'cards.db')
            default, _ = registry.install_payload(normalise({'name': 'Default'}))
            registry.use(default.id)
            personas = PersonaStore(root / 'cards.db', registry)
            bot = SimpleNamespace(cards=registry, personas=personas, is_admin=lambda user: user == 1)
            service = CardService(bot)
            await service.start()
            # This provider exercises the real ctx.web seam without touching the Internet.
            provider = root / 'fixture-web.mjs'
            provider.write_text("""export const name='card-smoke-web'; export const inject=['web'];
export function apply(ctx) { ctx.web.registerSearchProvider({id:'fixture',available:()=>true,
async search(request) { if (!request.query.includes('librarian')) throw Error('bad query');
return {sources:[{url:'https://cards.example/card.json',title:'Librarian'}],truncated:false} }}) }
""", encoding='utf-8')
            plugin = (Path(__file__).resolve().parents[1] / 'dsh/card-tools.mjs').as_uri()
            profile = root / 'card.patch.json'
            profile.write_text(json.dumps([
                {'id': 'session-log-deepseek', 'config': {'enabled': False}},
                {'id': 'session-telemetry-otel', 'disabled': True},
                {'insert': [
                    {'id': 'web', 'name': '@deepseek-ai/dsh-web', 'config': {'searchProvider': 'fixture'}},
                    {'id': 'card-smoke-web', 'name': provider.as_uri()},
                    {'id': 'card-tools', 'name': plugin},
                ]},
            ]), encoding='utf-8')
            card = json.dumps({'spec': 'chara_card_v3', 'data': {'name': 'Librarian', 'description': 'A friendly librarian'}}).encode()

            async def fetch(url, **kwargs):
                return RemoteCardContent(card, url, 'application/json')

            config: dict[str, Any] = dict(
                provider='deepseek-official', model='smoke-model', profile='sdk-minimal',
                cwd=str(root), dsh_home=str(root / 'home'), patches=(str(profile),),
                api_key='local-fake-key', base_url=f'http://127.0.0.1:{server.server_port}',
                request_timeout_seconds=45,
            )
            event = SimpleNamespace(user_id=1, message_type='private', message_id=1)
            try:
                with patch('card_service.fetch_public_card_content', side_effect=fetch):
                    for phase, text in [('search', '找一张友善图书管理员角色卡并下载'), ('choose', '下载第 1 张'), ('switch', '切换到刚安装的角色')]:
                        PHASE = phase
                        lease = service.open(event, 'private:1', 'card-session', text, personas.effective('private:1').token)
                        env = {**service.env(lease), 'DSH_TELEMETRY_DISABLED': '1', 'HOME': str(root), 'USERPROFILE': str(root)}

                        def run(env=env, text=text):
                            with DeepSeekHarness(**config, env=env) as harness:
                                try:
                                    return harness.run(text, session_id='card-session')
                                except Exception as exc:
                                    if 'already exists' not in str(exc).lower():
                                        raise
                                    return harness.resume_session('card-session').run(text)
                        try:
                            result = await asyncio.to_thread(run)
                            assert result.final_response == 'CARD_TOOLS_OK', str(result)
                            if phase == 'switch':
                                assert lease.deferred is not None
                                assert personas.queued() == [], 'switch published before reply finished'
                        finally:
                            service.revoke(lease)
                    assert registry.count() == 2
                    active = registry.active()
                    assert active is not None and active.id == default.id
                    assert personas.queued() == []
                    PHASE = 'ordinary'

                    def ordinary():
                        with DeepSeekHarness(**config, env={'DSH_TELEMETRY_DISABLED': '1', 'HOME': str(root), 'USERPROFILE': str(root),
                                                          'QQBOT_CARD_LEASE': '', 'QQBOT_CARD_SECRET': '', 'QQBOT_CARD_BRIDGE': ''}) as harness:
                            return harness.run('hello', session_id='ordinary')
                    assert (await asyncio.to_thread(ordinary)).final_response == 'CARD_TOOLS_OK'
                print(f'PASS: real SDK search/candidate/install tools, next-turn selection, dedupe and ordinary tool absence ({len(REQUESTS)} model requests)')
            finally:
                await service.close()
                personas.close()
                registry.close()
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=5)


if __name__ == '__main__':
    asyncio.run(main())
