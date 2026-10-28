from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import tempfile
import time
import unittest
import zlib
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from main import load_config  # noqa: E402
from video_backend import VideoBackend, bounded_read, stop_process  # noqa: E402
from video_http import SafeHTTP  # noqa: E402
from video_platforms import VideoError, checked_url, parse_source  # noqa: E402
from video_resolver import VideoSettings, project  # noqa: E402
from video_service import VideoService  # noqa: E402

BV = 'BV1xx411c7mD'
URL = f'https://www.bilibili.com/video/{BV}'
ROOT = Path(__file__).resolve().parents[1]


class VideoPlatformTests(unittest.TestCase):
    def test_ids_share_text_parts_and_canonical_urls(self):
        self.assertEqual(parse_source(BV).content_id, BV)
        self.assertEqual(parse_source('av123', 2).part, 2)
        target = parse_source(f'来看视频 {URL}?p=3&tracking=secret。')
        self.assertEqual(target.url, URL + '?p=3')
        self.assertEqual(target.part, 3)
        self.assertTrue(parse_source('https://b23.tv/abcdef').short)
        self.assertTrue(parse_source('抖音分享 https://v.douyin.com/abcdef/').short)
        self.assertEqual(parse_source('https://www.douyin.com/video/123').content_id, '123')
        self.assertEqual(parse_source(URL + '?p=2', 2).part, 2)

    def test_bad_ambiguous_unsupported_sources_and_conflicting_parts(self):
        for source, part in [(URL + '?p=2', 3), (URL + '?p=0', None),
                             (URL + '?p=1&p=2', None), (URL + '?p=', None),
                             (URL, True), (URL, 1001), (URL + ' ' + URL + '?p=2', None),
                             ('https://www.douyin.com/video/123', 2),
                             ('https://www.douyin.com/note/123', None),
                             ('https://www.bilibili.com/bangumi/play/ep123', None),
                             ('https://b23.tv/', None), ('BVxyz', None),
                             ('https://api.bilibili.com/video/' + BV, None),
                             ('https://www.bilibili.com.evil.test/video/' + BV, None)]:
            with self.subTest(source=source, part=part), self.assertRaises(VideoError):
                parse_source(source, part)

    def test_network_url_policy(self):
        for url in ('file:///etc/passwd', 'http://www.bilibili.com/a',
                    'https://user:password@www.bilibili.com/a',
                    'https://www.bilibili.com:444/a', 'https://127.0.0.1/a',
                    'https://169.254.169.254/a', 'https://[::1]/a',
                    'https://www.bilibili.com./a', 'https://www.bilibili.com\\@evil.test/a',
                    'https://www.douyin.com/video/1'):
            with self.subTest(url=url), self.assertRaises(VideoError):
                checked_url(url, 'bilibili')

    def test_result_projection_drops_secrets_and_bounds_unknowns(self):
        info = {'id': BV, 'title': 't' * 1000, 'description': 'd' * 5000,
                'cookie': 'secret', 'http_headers': {'Authorization': 'secret'},
                'duration': float('nan'), 'thumbnail': 'https://i.hdslb.com/a.jpg?token=secret',
                'formats': [{'url': 'https://cdn.example/media?secret', 'ext': 'mp4',
                             'vcodec': 'h264', 'acodec': 'none', 'width': True}] * 100,
                'subtitles': {'danmaku': [{'url': 'secret'}]}}
        result = project(info, parse_source(BV))
        self.assertEqual(len(result['title']), 300)
        self.assertEqual(len(result['description']), 1200)
        self.assertIsNone(result['duration_seconds'])
        self.assertIsNone(result['cover_url'])
        self.assertIsNone(result['resources'][0]['width'])
        self.assertEqual(len(result['resources']), 1)
        self.assertEqual(result['subtitles']['status'], 'unknown')
        self.assertNotIn('secret', json.dumps(result))
        for variant in ({'entries': []}, {'_type': 'url'}, {'_type': 'multi_video'}):
            with self.assertRaises(VideoError):
                project(variant, parse_source(BV))

    def test_existing_config_and_numeric_clamps_and_shipping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.toml'
            path.write_text('')
            settings = load_config(path).video
            self.assertTrue(settings.enabled)
            self.assertTrue(settings.bilibili_enabled)
            self.assertFalse(settings.douyin_enabled)
            path.write_text('[video]\ntimeout=999\nconcurrency=0\ncalls_per_turn=999\ncache_entries=999\n')
            settings = load_config(path).video
            self.assertEqual((settings.timeout, settings.concurrency, settings.calls_per_turn,
                              settings.cache_entries), (60, 1, 5, 256))
        destination = '/opt/qqbot/dsh/plugins/video-tools.mjs'
        self.assertIn(destination, (ROOT / 'dsh/qqbot.cordis.patch.yml').read_text())
        self.assertIn(destination, (ROOT / 'deploy/install-bot.sh').read_text())


class VideoHTTPTests(unittest.TestCase):
    def fetch(self, handler, url=URL, addresses=('8.8.8.8',), transport=None, **kwargs):
        real_client = httpx.Client
        options = []

        def client(**settings):
            options.append(settings)
            def streaming(request):
                response = handler(request)
                if response.is_stream_consumed:
                    response.stream = httpx.ByteStream(response.content)
                    response.is_stream_consumed = False
                    response.is_closed = False
                return response
            return real_client(transport=httpx.MockTransport(streaming), **settings)

        dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443)) for ip in addresses]
        with patch('video_http.httpx.Client', side_effect=client), patch('socket.getaddrinfo', return_value=dns):
            result = (transport or SafeHTTP('bilibili')).fetch(url, **kwargs)
        self.assertFalse(options[0]['trust_env'])
        self.assertFalse(options[0]['follow_redirects'])
        return result

    def test_pinned_connection_host_sni_headers_and_no_credential_forwarding(self):
        def handler(request):
            self.assertEqual(request.url.host, '8.8.8.8')
            self.assertEqual(request.headers['host'], 'www.bilibili.com')
            self.assertEqual(request.extensions['sni_hostname'], 'www.bilibili.com')
            self.assertNotIn('authorization', request.headers)
            self.assertNotIn('cookie', request.headers)
            self.assertEqual(request.headers['referer'], 'https://www.bilibili.com/')
            return httpx.Response(200, content=b'ok')
        self.assertEqual(self.fetch(handler, headers={'Authorization': 'secret', 'Cookie': 'secret'})[0], b'ok')

    def test_mixed_dns_and_unsafe_redirects_fail_before_connection(self):
        handler = Mock(return_value=httpx.Response(200))
        with self.assertRaises(VideoError):
            self.fetch(handler, addresses=('8.8.8.8', '127.0.0.1'))
        handler.assert_not_called()
        for destination in ('https://127.0.0.1/', 'https://evil.test/',
                            'https://www.douyin.com/video/1', 'http://www.bilibili.com/a'):
            handler = Mock(return_value=httpx.Response(302, headers={'location': destination}))
            with self.subTest(destination=destination), self.assertRaises(VideoError):
                self.fetch(handler)
            self.assertEqual(handler.call_count, 1)

    def test_redirects_cookie_domain_and_response_cookie_updates(self):
        jar = CookieJar()
        jar.set_cookie(Cookie(0, 'session', 'secret', None, False, 'www.bilibili.com',
                              True, False, '/', True, True, None, True, None, None, {}))
        calls = []
        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                self.assertIn('session=secret', request.headers['cookie'])
                return httpx.Response(302, headers={'location': 'https://api.bilibili.com/x',
                                                   'set-cookie': 'new=hidden; Path=/; Secure'})
            self.assertNotIn('cookie', request.headers)
            return httpx.Response(200, content=b'ok')
        self.fetch(handler, cookiejar=jar)
        self.assertTrue(any(cookie.name == 'new' for cookie in jar))

    def test_compression_raw_decoded_total_and_request_budgets(self):
        payload = zlib.compress(b'body')
        self.assertEqual(self.fetch(lambda r: httpx.Response(200, headers={'content-encoding': 'deflate'},
                                                             stream=httpx.ByteStream(payload)))[0], b'body')
        for response in (httpx.Response(200, content=b'12345'),
                         httpx.Response(200, headers={'content-encoding': 'deflate'},
                                        stream=httpx.ByteStream(zlib.compress(b'x' * 100))),
                         httpx.Response(200, headers={'content-encoding': 'br'}, content=b'x')):
            with patch('video_http.MAX_RESPONSE', 4), self.assertRaises(VideoError):
                self.fetch(lambda r, response=response: response)
        with patch('video_http.MAX_TOTAL', 2), self.assertRaises(VideoError):
            self.fetch(lambda r: httpx.Response(200, content=b'123'))
        transport = SafeHTTP('bilibili')
        transport.requests = 24
        with self.assertRaises(VideoError):
            self.fetch(lambda r: httpx.Response(200), transport=transport)
        with self.assertRaises(VideoError):
            self.fetch(lambda r: httpx.Response(302, headers={'location': URL}))


class VideoServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = VideoService(VideoSettings())
        self.event = SimpleNamespace(message_id=1, user_id=7, message_type='private')
        self.lease = self.service.open(self.event, 'private:7', 's1')
        self.service.backend.resolve = AsyncMock(return_value={'status': 'ok'})

    async def asyncTearDown(self):
        await self.service.close()

    async def test_ordinary_authority_scope_expiry_quota_and_argument_validation(self):
        with self.assertRaises(ValueError):
            self.service.open(self.event, 'private:8', 's1')
        self.event.message_id = 0
        with self.assertRaises(ValueError):
            self.service.open(self.event, 'private:7', 's1')
        for nonce, session in (('invented', 's1'), (self.lease.nonce, 'other')):
            with self.assertRaises(ValueError):
                await self.service.call(nonce, session, {'source': BV})
        with self.assertRaises(ValueError):
            await self.service.call(self.lease.nonce, 's1', {'source': BV, 'cookie_file': 'secret'})
        for _ in range(3):
            self.assertEqual(await self.service.call(self.lease.nonce, 's1', {'source': BV}), {'status': 'ok'})
        with self.assertRaises(ValueError):
            await self.service.call(self.lease.nonce, 's1', {'source': BV})
        self.lease.expires = time.monotonic() - 1
        with self.assertRaises(ValueError):
            self.service.authorize(self.lease.nonce, 's1')
        self.assertEqual(self.service.leases, {})

    async def test_revocation_cancels_inflight_and_expiry_actively_revokes(self):
        entered = asyncio.Event()
        finished = asyncio.Event()
        async def slow(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()
        self.service.backend.resolve.side_effect = slow
        task = asyncio.create_task(self.service.call(self.lease.nonce, 's1', {'source': BV}))
        await entered.wait()
        self.service.revoke(self.lease)
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())
        with self.assertRaises(ValueError):
            self.service.authorize(self.lease.nonce, 's1')
        expired = self.service.open(SimpleNamespace(message_id=1, user_id=7, message_type='private'), 'private:7', 's2')
        expired.expires = 0
        with self.assertRaises(ValueError):
            self.service.authorize(expired.nonce, 's2')
        self.assertNotIn(expired.nonce, self.service.leases)

    async def test_real_http_auth_duplicate_headers_origin_and_disconnect(self):
        await self.service.start()
        async with httpx.AsyncClient(trust_env=False) as client:
            data = {'lease': self.lease.nonce, 'session': 's1', 'args': {'source': BV}}
            self.assertEqual((await client.post(self.service.url + '/video', json=data)).status_code, 400)
            headers = {'authorization': 'Bearer ' + self.service.secret}
            self.assertEqual((await client.post(self.service.url + '/video', headers=headers, json=data)).status_code, 200)
            self.assertEqual((await client.post(self.service.url + '/video', headers={**headers, 'origin': 'null'}, json=data)).status_code, 400)
        port = int(self.service.url.rsplit(':', 1)[1])
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        body = json.dumps(data).encode()
        writer.write(f'POST /video HTTP/1.1\r\nAuthorization: Bearer {self.service.secret}\r\nContent-Length: {len(body)}\r\nContent-Length: {len(body)}\r\n\r\n'.encode() + body)
        await writer.drain()
        self.assertIn(b'400', await reader.read())
        writer.close()
        await writer.wait_closed()
        entered, stopped = asyncio.Event(), asyncio.Event()
        async def slow(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        self.service.backend.resolve.side_effect = slow
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        writer.write(f'POST /video HTTP/1.1\r\nAuthorization: Bearer {self.service.secret}\r\nContent-Length: {len(body)}\r\n\r\n'.encode() + body)
        await writer.drain()
        await asyncio.wait_for(entered.wait(), 2)
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(stopped.wait(), 2)

    async def test_closed_cannot_restart_or_open(self):
        await self.service.close()
        with self.assertRaises(ValueError):
            await self.service.start()
        with self.assertRaises(ValueError):
            self.service.open(self.event, 'private:7', 's1')


class VideoBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_projection_cache_deepcopy_parts_credentials_and_disabled_douyin(self):
        settings = VideoSettings()
        backend = VideoBackend(settings)
        backend._extract = AsyncMock(return_value={'status': 'ok', 'title': 'original'})
        try:
            result = await backend.resolve(BV)
            result['title'] = 'mutated'
            self.assertEqual((await backend.resolve(BV))['title'], 'original')
            self.assertEqual(backend._extract.await_count, 1)
            await backend.resolve(BV, 2)
            self.assertEqual(backend._extract.await_count, 2)
            with tempfile.TemporaryDirectory() as tmp:
                cookie = Path(tmp) / 'cookies'
                cookie.write_text('version 1')
                settings.bilibili_cookies = str(cookie)
                await backend.resolve(BV)
                cookie.write_text('new version longer')
                await backend.resolve(BV)
                self.assertEqual(backend._extract.await_count, 4)
            with self.assertRaises(VideoError) as error:
                await backend.resolve('https://www.douyin.com/video/123')
            self.assertEqual(error.exception.code, 'UNSUPPORTED_SOURCE')
        finally:
            await backend.close()

    async def test_cache_bound_errors_not_cached_and_revoke_queue(self):
        backend = VideoBackend(VideoSettings(cache_entries=1))
        backend._extract = AsyncMock(return_value={'status': 'error'})
        await backend.resolve(BV)
        await backend.resolve(BV)
        self.assertEqual(backend._extract.await_count, 2)
        backend._extract.return_value = {'status': 'ok'}
        await backend.resolve(BV)
        await backend.resolve('av123')
        self.assertEqual(len(backend._cache), 1)
        backend._slots = asyncio.Semaphore(0)
        task = asyncio.create_task(backend.resolve('av456'))
        await asyncio.sleep(0)
        await backend.close()
        self.assertTrue(task.cancelled())
        self.assertEqual(backend._active, set())

    async def test_output_reader_and_terminate_kill_reap(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b'12345')
        reader.feed_eof()
        with self.assertRaises(VideoError):
            await bounded_read(reader, 4)
        process = SimpleNamespace(returncode=None, terminate=Mock(), kill=Mock(),
                                  wait=AsyncMock(side_effect=[TimeoutError(), None]))
        await stop_process(process)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.await_count, 2)

    async def test_real_worker_protocol_and_cancelled_child_is_reaped(self):
        backend = VideoBackend(VideoSettings(cache_entries=0))
        # Unsupported source is rejected in the actual worker without a network request.
        result = await backend._extract('https://example.com/video', None, '')
        self.assertEqual(result['status'], 'error')
        self.assertIn(result['error']['code'], ('UNSUPPORTED_SOURCE', 'BACKEND_UNAVAILABLE'))
        real_spawn = asyncio.create_subprocess_exec
        process = None
        entered = asyncio.Event()

        async def spawn(*args, **kwargs):
            nonlocal process
            process = await real_spawn(sys.executable, '-I', '-c',
                                       'import sys; sys.stdin.buffer.read(); import time; time.sleep(60)',
                                       **kwargs)
            entered.set()
            return process

        with patch('video_backend.asyncio.create_subprocess_exec', side_effect=spawn):
            task = asyncio.create_task(backend.resolve(BV))
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        self.assertIsNotNone(process.returncode)
        self.assertEqual(backend._active, set())
        await backend.close()

    async def test_subprocess_minimal_environment_output_bounds_and_cleanup(self):
        process = SimpleNamespace(returncode=None, terminate=Mock(), kill=Mock(), wait=AsyncMock(return_value=0),
                                  stdin=SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock()),
                                  stdout=asyncio.StreamReader(), stderr=asyncio.StreamReader())
        process.stdout.feed_data(b'x' * 17000)
        process.stdout.feed_eof()
        process.stderr.feed_eof()
        backend = VideoBackend(VideoSettings())
        with (patch('asyncio.create_subprocess_exec', AsyncMock(return_value=process)) as spawn,
              patch.dict(os.environ, {'MODEL_API_KEY': 'secret', 'HTTPS_PROXY': 'secret'})):
            with self.assertRaises(VideoError):
                await backend.resolve(BV)
        process.terminate.assert_called_once()
        self.assertNotIn('MODEL_API_KEY', spawn.call_args.kwargs['env'])
        self.assertNotIn('HTTPS_PROXY', spawn.call_args.kwargs['env'])
        self.assertIn('-I', spawn.call_args.args)
        await backend.close()


if __name__ == '__main__':
    unittest.main()
