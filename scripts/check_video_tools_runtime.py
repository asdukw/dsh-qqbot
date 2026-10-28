"""Real pinned SDK + fake local model/backend: video tool visibility and invocation.

Run explicitly: python scripts/check_video_tools_runtime.py. No QQ/external APIs.
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
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from deepseek_harness import DeepSeekHarness  # pyright: ignore[reportMissingImports]  # noqa: E402
from video_delivery import VideoArtifact  # noqa: E402
from video_resolver import VideoSettings  # noqa: E402
from video_service import VideoService  # noqa: E402

PHASE = 'video'
REQUESTS = 0
BV = 'BV1xx411c7mD'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        global REQUESTS
        body = json.loads(self.rfile.read(int(self.headers['content-length'])))
        REQUESTS += 1
        names = {tool['name'] for tool in body.get('tools', [])}
        messages = body.get('messages', [])
        blocks = messages[-1].get('content', [])
        results = [b for b in blocks if isinstance(b, dict) and b.get('type') == 'tool_result'] if isinstance(blocks, list) else []
        if PHASE in ('video', 'send'):
            assert {'video_resolve', 'video_send'} <= names, names
            assert 'card_install' not in names, names
        else:
            assert 'video_resolve' not in names and 'video_send' not in names, names
        invoke = PHASE in ('video', 'send') and not results
        if results:
            text = results[-1]['content']
            if not isinstance(text, str):
                text = ''.join(item.get('text', '') for item in text if isinstance(item, dict))
            result = json.loads(text)
            if PHASE == 'send':
                assert result['status'] == 'sent' and result['message_id'] == 123, text
                assert 'file://' not in text and 'signature' not in text, text
            else:
                assert result['title'] == 'LOCAL_FIXTURE_VIDEO', text
        events: list[dict[str, Any]] = [
            {'type': 'message_start', 'message': {'id': f'msg_{REQUESTS}', 'model': 'smoke-model',
             'type': 'message', 'role': 'assistant', 'content': [], 'stop_reason': None,
             'usage': {'input_tokens': 3, 'output_tokens': 0}}},
        ]
        if invoke:
            events += [
                {'type': 'content_block_start', 'index': 0, 'content_block': {
                    'type': 'tool_use', 'id': f'call_{REQUESTS}', 'name': 'video_send' if PHASE == 'send' else 'video_resolve', 'input': {}}},
                {'type': 'content_block_delta', 'index': 0, 'delta': {
                    'type': 'input_json_delta', 'partial_json': json.dumps({'source': BV})}},
            ]
        else:
            events += [
                {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
                {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'VIDEO_TOOLS_OK'}},
            ]
        events += [
            {'type': 'content_block_stop', 'index': 0},
            {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use' if invoke else 'end_turn',
             'stop_sequence': None}, 'usage': {'output_tokens': 3}},
            {'type': 'message_stop'},
        ]
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        for event in events:
            self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
        self.wfile.flush()


async def main():
    global PHASE
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    service = VideoService(VideoSettings())
    service.backend.resolve = AsyncMock(return_value={'status': 'ok', 'title': 'LOCAL_FIXTURE_VIDEO'})
    try:
        await service.start()
        with tempfile.TemporaryDirectory(prefix='qqbot-video-smoke-') as tmp:
            root = Path(tmp)
            service.settings.media_directory = str(root / 'media')
            service.settings.delivery_database = str(root / 'deliveries.db')
            from video_delivery import VideoMaterializer
            service.materializer = VideoMaterializer(service.settings, service.backend)
            service.sender = AsyncMock(return_value={'message_id': 123})
            plugin = (Path(__file__).resolve().parents[1] / 'dsh/video-tools.mjs').as_uri()
            profile = root / 'video.patch.json'
            profile.write_text(json.dumps([
                {'id': 'session-log-deepseek', 'config': {'enabled': False}},
                {'id': 'session-telemetry-otel', 'disabled': True},
                {'insert': [{'id': 'video-tools', 'name': plugin}]},
            ]), encoding='utf-8')
            config: dict[str, Any] = dict(
                provider='deepseek-official', model='smoke-model', profile='sdk-minimal',
                cwd=str(root), dsh_home=str(root / 'home'), patches=(str(profile),),
                api_key='local-fake-key', base_url=f'http://127.0.0.1:{server.server_port}',
                request_timeout_seconds=45,
            )
            base_env = {'DSH_TELEMETRY_DISABLED': '1', 'HOME': str(root), 'USERPROFILE': str(root),
                        'QQBOT_CARD_BRIDGE': '', 'QQBOT_CARD_SECRET': '', 'QQBOT_CARD_LEASE': ''}
            event = SimpleNamespace(user_id=7, message_type='private', message_id=1)
            lease = service.open(event, 'private:7', 'video-session')
            env = {**base_env, **service.env(lease)}

            def run():
                with DeepSeekHarness(**config, env=env) as harness:
                    try:
                        return harness.run('Resolve the supplied video using video_resolve', session_id='video-session')
                    except Exception as exc:
                        if 'already exists' not in str(exc).lower():
                            raise
                        return harness.resume_session('video-session').run('Check available tools')

            try:
                result = await asyncio.to_thread(run)
                assert result.final_response == 'VIDEO_TOOLS_OK', str(result)
                service.backend.resolve.assert_awaited_once_with(BV, None)
            finally:
                service.revoke(lease)
            PHASE = 'send'
            metadata = {'status': 'ok', 'title': 'LOCAL_FIXTURE_VIDEO', 'platform': 'bilibili',
                        'content_id': BV, 'selected_part': 1,
                        'canonical_url': f'https://www.bilibili.com/video/{BV}?p=1'}
            service.backend.resolve = AsyncMock(return_value=metadata)
            service.materializer.initialize()
            artifact = VideoArtifact(service.materializer.outbox / ('a' * 32 + '.mp4'), metadata)
            artifact.path.write_bytes(b'local-fixture')
            service.materializer.materialize = AsyncMock(return_value=artifact)
            event.message_id = 2
            lease = service.open(event, 'private:7', 'video-session', text=f'解析 {BV}')
            env = {**base_env, **service.env(lease)}
            try:
                result = await asyncio.to_thread(run)
                assert result.final_response == 'VIDEO_TOOLS_OK', str(result)
                service.sender.assert_awaited_once()
                assert service.sender.await_args is not None
                assert service.sender.await_args.args[0] == 'private:7'
            finally:
                service.revoke(lease)
            PHASE = 'background'
            env = {**base_env, 'QQBOT_VIDEO_BRIDGE': '', 'QQBOT_VIDEO_SECRET': '', 'QQBOT_VIDEO_LEASE': ''}
            result = await asyncio.to_thread(run)
            assert result.final_response == 'VIDEO_TOOLS_OK', str(result)
            await service.close()
            print(f'PASS: real SDK video registration/invocation, no card privilege, scoped actual send with receipt, later unauthorized tool absence ({REQUESTS} model requests)')
    finally:
        await service.close()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=5)


if __name__ == '__main__':
    asyncio.run(main())
