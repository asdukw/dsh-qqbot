"""Exercise persona.mjs on the installed DSH SDK with a local fake model.

Run explicitly: python scripts/check_persona_runtime.py. No QQ/API credentials used.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from conversations import ConversationStore  # noqa: E402
from deepseek_harness import DeepSeekHarness  # pyright: ignore[reportMissingImports]  # noqa: E402

REQUESTS: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('content-length', 0))))
        REQUESTS.append(body)
        events = [
            {'type': 'message_start', 'message': {'id': 'msg_persona', 'model': 'smoke-model',
             'type': 'message', 'role': 'assistant', 'content': [], 'stop_reason': None,
             'usage': {'input_tokens': 3, 'output_tokens': 0}}},
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'PERSONA_OK'}},
            {'type': 'content_block_stop', 'index': 0},
            {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None},
             'usage': {'output_tokens': 3}},
            {'type': 'message_stop'},
        ]
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        for event in events:
            self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
        self.wfile.flush()


def system_text(body: dict) -> str:
    return json.dumps(body.get('system', ''), ensure_ascii=False)


def main() -> None:
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='qqbot-persona-smoke-') as tmp:
            root = Path(tmp)
            store = ConversationStore(root / 'profile.db')
            plugin = (Path(__file__).resolve().parents[1] / 'dsh' / 'persona.mjs').as_uri()
            profile_plugin = (Path(__file__).resolve().parents[1] / 'dsh' / 'user-profile.mjs').as_uri()
            patch = root / 'persona.patch.json'
            patch.write_text(json.dumps([
                {'id': 'session-log-deepseek', 'config': {'enabled': False}},
                {'id': 'session-telemetry-otel', 'disabled': True},
                {'insert': [{'id': 'conversation-persona', 'name': plugin}, {'id': 'scoped-profile', 'name': profile_plugin}]},
            ]), encoding='utf-8')
            config: dict[str, Any] = dict(
                provider='deepseek-official', model='smoke-model', profile='sdk-minimal',
                cwd=str(root), dsh_home=str(root / 'home'), patches=(str(patch),),
                api_key='local-fake-key', base_url=f'http://127.0.0.1:{server.server_port}',
                request_timeout_seconds=30,
                env={'USER_PROFILE_DB': str(root / 'profile.db'), 'DSH_SYSTEM_PROMPT': 'neutral operational rules',
                     'QQBOT_RELATION_ENABLED': '0', 'DSH_TELEMETRY_DISABLED': '1', 'HOME': str(root), 'USERPROFILE': str(root)},
            )
            try:
                store.record('group-42', 'group', 1, 42, 'A')
                store.record('group-43', 'group', 2, 43, 'B')
                store.prepare_persona('group-42', 'group:42', '1:a', 'ALICE_SENTINEL WORLD_A {{unsupported_literal}}', 'RELATION_A_SENTINEL')
                store.prepare_persona('group-43', 'group:43', '1:b', 'FISH_SENTINEL WORLD_B', 'RELATION_B_SENTINEL')
                with DeepSeekHarness(**config) as harness:
                    for session, expected, forbidden in [
                        ('group-42', 'ALICE_SENTINEL', 'FISH_SENTINEL'),
                        ('group-43', 'FISH_SENTINEL', 'ALICE_SENTINEL'),
                        ('group-42', 'ALICE_SENTINEL', 'FISH_SENTINEL'),
                    ]:
                        result = harness.run('Reply PERSONA_OK', session_id=session)
                        assert result.final_response == 'PERSONA_OK', str(result)
                        prompt = system_text(REQUESTS[-1])
                        assert expected in prompt and forbidden not in prompt, prompt
                        relation = 'RELATION_A_SENTINEL' if session == 'group-42' else 'RELATION_B_SENTINEL'
                        assert relation in prompt, prompt
                        assert '[好感+2]' not in prompt, prompt
                    store.prepare_persona('group-42', 'group:42', '1:a', 'ALICE_UPDATED WORLD_A', 'RELATION_UPDATED_SENTINEL')
                    result = harness.run('Reply PERSONA_OK', session_id='group-42')
                    assert result.final_response == 'PERSONA_OK', str(result)
                    assert 'ALICE_UPDATED' in system_text(REQUESTS[-1])
                    assert 'RELATION_UPDATED_SENTINEL' in system_text(REQUESTS[-1])
                    before = len(REQUESTS)
                    try:
                        result = harness.run('No persona should fail', session_id='unprepared')
                    except Exception:
                        pass
                    else:
                        assert result.finish_reason == 'error' or not result.final_response, str(result)
                    assert len(REQUESTS) == before, 'unprepared session reached model'
                with DeepSeekHarness(**config) as harness:
                    session = harness.resume_session('group-43')
                    result = session.run('Reply PERSONA_OK')
                    assert result.final_response == 'PERSONA_OK', str(result)
                    assert 'FISH_SENTINEL' in system_text(REQUESTS[-1])
                    assert 'ALICE_UPDATED' not in system_text(REQUESTS[-1])
                print(f'PASS: pinned SDK isolated persona/lore, dynamic refresh, fail-closed and resume ({len(REQUESTS)} model requests)')
            finally:
                store.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == '__main__':
    main()
