"""Drive the actual bot CLI over local OneBot/LLM sockets; no QQ credentials."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import site
import sqlite3
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import websockets

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bot'))
sys.path.insert(0, str(ROOT / 'tests'))

from models import ModelRegistry  # noqa: E402
from test_story import card, definition  # pyright: ignore[reportMissingImports]  # noqa: E402

from cards import CardRegistry  # noqa: E402

REQUESTS: list[dict] = []


class ModelHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        REQUESTS.append(body)
        user = json.loads(body['messages'][1]['content'])
        full = definition().model_dump()
        if 'card' in user:
            content = json.dumps({key: value for key, value in full.items() if key not in ('start', 'nodes', 'version')}, ensure_ascii=False)
        elif 'brief' in user:
            content = json.dumps({key: full[key] for key in ('start', 'nodes')}, ensure_ascii=False)
        else:
            content = json.dumps({'text': '雨落在书店门前，角色与你商量下一次见面。'}, ensure_ascii=False)
        result = json.dumps({'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}],
                             'usage': {'prompt_tokens': 50, 'completion_tokens': 50}}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(result)))
        self.end_headers()
        self.wfile.write(result)


async def smoke(root: Path, model_port: int) -> None:
    sends: asyncio.Queue[str] = asyncio.Queue()
    connected = asyncio.Event()
    peer: Any = None
    receipt = 0

    async def onebot(ws):
        nonlocal peer, receipt
        peer = ws
        connected.set()
        async for raw in ws:
            action = json.loads(raw)
            receipt += 1
            if action['action'] in ('send_group_msg', 'send_private_msg'):
                text = ''.join(segment['data'].get('text', '') for segment in action['params']['message'])
                await sends.put(text)
            await ws.send(json.dumps({'echo': action['echo'], 'status': 'ok', 'retcode': 0,
                                      'data': {'message_id': receipt, 'user_id': 999}}))

    async with websockets.serve(onebot, '127.0.0.1', 0) as server:
        port = server.sockets[0].getsockname()[1]
        cards = CardRegistry(root / 'cards.db')
        a, _ = cards.install_payload(card('Alice'))
        b, _ = cards.install_payload(card('Bob'))
        cards.use(a.id)
        cards.close()
        registry = ModelRegistry(root / 'models.db')
        registry.upsert('fake', 'story-smoke', {'context_window': '64000', 'base_url': f'http://127.0.0.1:{model_port}/v1',
                                              'api_key_env': 'FAKE_KEY', 'enabled': 'true'})
        registry.use('fake', 'story-smoke')
        registry.close()
        def path(name: str) -> str:
            return json.dumps((root / name).as_posix())

        config = root / 'config.toml'
        config.write_text(f'''[onebot]
url = "ws://127.0.0.1:{port}"
self_id = 999
[dsh]
enabled = false
workspace = {path('workspace')}
[models]
database = {path('models.db')}
patch = {path('models.patch.json')}
[cards]
database = {path('cards.db')}
bootstrap = {path('missing.json')}
[paths]
database = {path('memory.db')}
profile_db = {path('profile.db')}
[stickers]
database = {path('stickers.db')}
directory = {path('stickers')}
send = false
[usage]
database = {path('usage.db')}
[tasks]
enabled = false
[feeds]
enabled = false
[daily]
enabled = false
[approvals]
enabled = false
[voice]
enabled = false
[story]
enabled = true
[bot]
admins = [1]
reply_group_at = true
''', encoding='utf-8')
        log = root / 'bot.log'
        with log.open('w', encoding='utf-8') as output:
            # Windows venv python.exe is a redirector. Launch its real interpreter
            # with the venv packages so terminate()/wait() own the actual process.
            executable = getattr(sys, '_base_executable', sys.executable) if os.name == 'nt' else sys.executable
            env = {**os.environ, 'FAKE_KEY': 'local-fake'}
            if os.name == 'nt':
                env['PYTHONPATH'] = os.pathsep.join(site.getsitepackages())
            process = subprocess.Popen([executable, str(ROOT / 'bot/main.py'), '--config', str(config)],
                                       cwd=ROOT, env=env, stdout=output, stderr=output)
            try:
                await asyncio.wait_for(connected.wait(), 15)
                message = 0

                async def say(user: int, text: str):
                    nonlocal message
                    assert peer is not None
                    message += 1
                    await peer.send(json.dumps({'post_type': 'message', 'message_type': 'group', 'group_id': 42,
                                                'user_id': user, 'message_id': message, 'sender': {'nickname': f'player-{user}'},
                                                'message': [{'type': 'text', 'data': {'text': text}}]}))

                async def until(pattern: str) -> str:
                    async with asyncio.timeout(20):
                        while True:
                            text = await sends.get()
                            if pattern in text:
                                return text

                await say(1, '/story setup')
                await until('序幕')
                await say(1, '/story setup')
                await until('已经发送')
                await say(1, f'/card use {b.id}')
                await until('序幕')
                await say(10, '/story start')
                menu_a = await until('/choose ')
                await say(11, '/story start')
                menu_b = await until('/choose ')
                code = re.search(r'/choose ([a-f0-9]+)', menu_a)
                assert code is not None and code.group(1) not in menu_b
                await say(10, f'/choose {code.group(1)} 1')
                await until('信任 +2')
                await until('/choose ')
                with contextlib.closing(sqlite3.connect(root / 'profile.db')) as db:
                    rows = db.execute('SELECT user,data FROM story_relations ORDER BY user').fetchall()
                    trust = {row[0]: json.loads(row[1])['trust'] for row in rows}
                    assert trust['10'] == 12 and trust['11'] == 10, trust
                await say(1, f'/card use {a.id}')
                await until('序幕')
                await say(1, f'/card use {b.id}')
                await until('返回')
                await say(10, '/story continue')
                await until('/choose ')
                assert all('tools' not in request for request in REQUESTS)
                print('PASS: actual CLI, local OneBot+model, automatic switch prologue, two independent players, choice feedback, A→B→A resume')
            except BaseException:
                print(log.read_text(encoding='utf-8')[-8000:])
                raise
            finally:
                if peer is not None:
                    await peer.close()
                process.terminate()
                await asyncio.to_thread(process.wait, 10)


def main() -> None:
    server = ThreadingHTTPServer(('127.0.0.1', 0), ModelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='qqbot-story-smoke-') as tmp:
            asyncio.run(smoke(Path(tmp), server.server_port))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
