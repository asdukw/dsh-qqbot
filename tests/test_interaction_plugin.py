"""Native plugin transport tests; use only loopback and skip without Node."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))


@unittest.skipUnless(shutil.which('node'), 'Node is required for native plugin checks')
class InteractionPluginTests(unittest.TestCase):
    def test_parallel_calls_queue_and_uncertain_errors_freeze(self):
        plugin = (Path(__file__).resolve().parents[1] / 'dsh/interaction-tools.mjs').as_uri()
        script = r'''
import assert from 'node:assert/strict'
import http from 'node:http'
import { apply } from PLUGIN
let active = 0, maximum = 0, requests = 0, mode = 'sent'
const server = http.createServer(async (req, res) => {
  let body = ''
  for await (const chunk of req) body += chunk
  const operation = JSON.parse(body).operation
  if (operation === 'ack' || operation === 'uncertain') {
    if (mode === 'ack-loss' && operation === 'ack') { req.socket.destroy(); return }
    res.writeHead(200); res.end('{}'); return
  }
  requests++; active++; maximum = Math.max(maximum, active)
  await new Promise(resolve => setTimeout(resolve, 25))
  active--
  res.writeHead(['sent', 'ack-loss'].includes(mode) ? 200 : 400, { 'content-type': 'application/json' })
  res.end(JSON.stringify(['sent', 'ack-loss'].includes(mode) ? { status: 'sent', message_id: '1' } :
    mode === 'reject' ? { status: 'rejected', error: 'shorten to 1200 characters' } :
    { error: 'invalid interaction bridge request' }))
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
try {
  process.env.QQBOT_INTERACTION_BRIDGE = `http://127.0.0.1:${server.address().port}`
  process.env.QQBOT_INTERACTION_SECRET = 'secret'.repeat(8)
  process.env.QQBOT_INTERACTION_LEASE = 'lease'.repeat(8)
  const register = () => {
    const tools = new Map()
    apply({ tools: { register: t => tools.set(t.name, t) }, on: () => {} })
    return tools
  }
  const exec = { agent: { id: 'session' } }
  const args = { message_id: '1', text: 'hello' }
  let tools = register()
  const results = await Promise.all([1, 2, 3].map(() => tools.get('reply_to_others').execute(args, exec)))
  assert(results.every(r => JSON.parse(r).status === 'sent'))
  assert.equal(maximum, 1, 'network requests must queue before request deadlines begin')
  mode = 'reject'; tools = register()
  assert.equal(JSON.parse(await tools.get('reply_to_others').execute(args, exec)).status, 'rejected')
  mode = 'sent'
  assert.equal(JSON.parse(await tools.get('reply_to_others').execute(args, exec)).status, 'sent')
  mode = 'ack-loss'; tools = register()
  assert.equal(JSON.parse(await tools.get('reply_to_others').execute(args, exec)).status, 'sent',
    'lost receipt ACK cannot invalidate a validated successful action result')
  mode = 'uncertain'; tools = register()
  assert.equal(JSON.parse(await tools.get('reply_to_others').execute(args, exec)).status, 'unknown')
  const count = requests
  assert.equal(JSON.parse(await tools.get('react_to_others').execute({message_id:'1',emoji_id:'76'},exec)).status,'unknown')
  assert.equal(requests, count, 'uncertain HTTP errors must freeze further I/O')
} finally { await new Promise(resolve => server.close(resolve)) }
'''.replace('PLUGIN', json.dumps(plugin))
        result = subprocess.run(['node', '--input-type=module', '-e', script],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


@unittest.skipUnless(shutil.which('node'), 'Node is required for native bridge fault checks')
class InteractionReceiptFaultTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_plugin_real_bridge_lost_receipts_and_final_arbitration(self):
        from dsh_client import _raw_turn
        from test_interaction_turns import InteractionTurnTests

        plugin = (Path(__file__).resolve().parents[1] / 'dsh/interaction-tools.mjs').as_uri()
        fixture = InteractionTurnTests()
        await fixture.asyncSetUp()
        try:
            for mode in ('normal', 'drop-action', 'drop-ack', 'block-ack', 'block-action'):
                with self.subTest(mode=mode):
                    bot = fixture.make_bot()
                    connections = set()

                    async def proxy(reader, writer, *, mode=mode, bot=bot, connections=connections):
                        task = asyncio.current_task()
                        connections.add(task)
                        upstream = None
                        try:
                            async with asyncio.timeout(5):
                                header = await reader.readuntil(b'\r\n\r\n')
                                length = int(next(line.split(b':', 1)[1] for line in header.split(b'\r\n')
                                                  if line.lower().startswith(b'content-length:')))
                                body = await reader.readexactly(length)
                                operation = json.loads(body)['operation']
                                if operation == 'uncertain' or (mode == 'block-ack' and operation == 'ack'):
                                    return
                                if mode == 'block-action' and operation == 'react':
                                    return
                                target = urlsplit(bot.interaction_service.url)
                                source, upstream = await asyncio.open_connection(target.hostname, target.port)
                                upstream.write(header + body)
                                await upstream.drain()
                                response = await source.read()
                                if ((mode == 'drop-action' and operation == 'react')
                                        or (mode == 'drop-ack' and operation == 'ack')):
                                    return
                                writer.write(response)
                                await writer.drain()
                        finally:
                            if upstream is not None:
                                upstream.close()
                                await upstream.wait_closed()
                            writer.close()
                            await writer.wait_closed()
                            connections.discard(task)

                    server = await asyncio.start_server(proxy, '127.0.0.1', 0)
                    port = server.sockets[0].getsockname()[1]
                    outcome = []

                    def run(session, prompt, images, env, *, port=port, outcome=outcome):
                        code = '''
import { apply } from PLUGIN
const tools = new Map()
apply({ tools: { register: t => tools.set(t.name, t) }, on: () => {} })
const result = await tools.get('react_to_others').execute(
  {message_id:'123',emoji_id:'76'}, {agent:{id:'interaction-session'}})
console.log(result)
'''.replace('PLUGIN', json.dumps(plugin))
                        child_env = {**os.environ, **env, 'QQBOT_INTERACTION_BRIDGE': f'http://127.0.0.1:{port}'}
                        child = subprocess.run(['node', '--input-type=module', '-e', code],
                                               env=child_env, capture_output=True, text=True, timeout=10)
                        self.assertEqual(child.returncode, 0, child.stderr)
                        result = json.loads(child.stdout)
                        outcome.append(result['status'])
                        return _raw_turn(SimpleNamespace(final_response='FINAL_AFTER_RESULT', finish_reason='completed', events=[
                            {'type': 'tool/call', 'data': {'callId': 'real', 'name': 'react_to_others'}},
                            {'type': 'tool/result', 'data': {'message': {'toolCallId': 'real',
                                'content': [{'type': 'text', 'text': child.stdout.strip()}]}}},
                        ]))

                    bot.dsh.run_authorized_result = Mock(side_effect=run)
                    try:
                        await asyncio.wait_for(bot._reply_dsh('group:42', fixture.event, fixture.event.text), 15)
                        if mode in ('drop-action', 'block-ack', 'block-action'):
                            bot._send_reply.assert_not_awaited()
                        else:
                            bot._send_reply.assert_awaited_once()
                        self.assertEqual(outcome, ['unknown' if mode in ('drop-action', 'block-action') else 'reacted'])
                        self.assertEqual(bot.onebot.set_msg_emoji_like.await_count, 0 if mode == 'block-action' else 1)
                        bot._send.assert_not_awaited()
                    finally:
                        server.close()
                        await server.wait_closed()
                        await asyncio.gather(*tuple(connections), return_exceptions=True)
        finally:
            await fixture.asyncTearDown()
