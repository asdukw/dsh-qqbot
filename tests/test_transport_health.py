from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

import websockets  # noqa: E402
from transport_health import probe, transport_health  # noqa: E402


class TransportHealthTests(unittest.IsolatedAsyncioTestCase):
    async def check_status(self, data, *, retcode=0):
        actions = []

        async def handler(ws):
            request = json.loads(await ws.recv())
            actions.append(request['action'])
            await ws.send(json.dumps({'post_type': 'meta_event', 'meta_event_type': 'heartbeat'}))
            await ws.send(json.dumps({'echo': request['echo'], 'retcode': retcode, 'data': data}))

        async with websockets.serve(handler, '127.0.0.1', 0) as server:
            port = server.sockets[0].getsockname()[1]
            result = await probe(f'ws://127.0.0.1:{port}', 'local-test-token', timeout=2)
        self.assertEqual(actions, ['get_status'])
        self.assertNotIn('local-test-token', json.dumps(result))
        return result

    async def test_online_status_not_just_open_socket(self):
        self.assertTrue((await self.check_status({'online': True, 'good': True}))['ready'])
        self.assertFalse((await self.check_status({'online': False, 'good': True}))['ready'])
        self.assertFalse((await self.check_status({'online': True, 'good': False}))['ready'])

    async def test_invalid_and_rejected_status_fail_closed(self):
        self.assertFalse((await self.check_status(None))['ready'])
        self.assertFalse((await self.check_status({'online': 'true'}))['ready'])
        self.assertFalse((await self.check_status({'online': True}, retcode=1))['ready'])

    async def test_unreachable_does_not_expose_credentials(self):
        result = await probe('ws://127.0.0.1:1', 'secret-token', timeout=0.2)
        self.assertEqual(result, {'ready': False, 'state': 'unreachable'})

    async def test_no_status_times_out(self):
        async def handler(ws):
            await ws.recv()
            await ws.wait_closed()

        async with websockets.serve(handler, '127.0.0.1', 0) as server:
            port = server.sockets[0].getsockname()[1]
            result = await probe(f'ws://127.0.0.1:{port}', '', timeout=0.1)
        self.assertFalse(result['ready'])


class CacheTests(unittest.TestCase):
    def test_probe_is_cached_but_not_across_targets(self):
        import transport_health as module
        with patch.object(module, '_cached', {}), patch.object(module, '_cached_target', None):
            async def fake_probe(*args, **kwargs):
                return {'ready': True, 'state': 'online'}
            with patch.object(module, 'probe', side_effect=fake_probe) as mocked:
                one = transport_health({'onebot': {'url': 'ws://localhost:3001'}})
                one['ready'] = False
                self.assertTrue(transport_health({'onebot': {'url': 'ws://localhost:3001'}})['ready'])
                self.assertEqual(mocked.call_count, 1)
                transport_health({'onebot': {'url': 'ws://localhost:3002'}})
                self.assertEqual(mocked.call_count, 2)


if __name__ == '__main__':
    unittest.main()
