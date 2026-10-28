from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from onebot import OneBotError, OneBotRejected, OneBotWSClient  # noqa: E402
from video_backend import VideoBackend  # noqa: E402
from video_delivery import VideoArtifact, VideoMaterializer  # noqa: E402
from video_media import download_tracks, media_url, select_formats  # noqa: E402
from video_platforms import VideoError  # noqa: E402
from video_resolver import VideoSettings  # noqa: E402
from video_sending import send_request  # noqa: E402
from video_service import VideoService  # noqa: E402
from video_store import VideoStore  # noqa: E402

BV = 'BV1xx411c7mD'
URL = f'https://www.bilibili.com/video/{BV}?p=1'
META = {'status': 'ok', 'canonical_url': URL, 'selected_part': 1, 'platform': 'bilibili',
        'content_id': BV, 'title': 'fixture'}


def video(**overrides):
    return {'url': 'https://cn-example.bilivideo.com/video.mp4?signature=secret', 'ext': 'mp4',
            'height': 360, 'vcodec': 'avc1', 'acodec': 'mp4a', 'filesize': 1000, **overrides}


class MediaTests(unittest.TestCase):
    def test_selection_progressive_dash_limits_and_silent_policy(self):
        progressive = video()
        self.assertEqual(select_formats({'duration': 10, 'formats': [progressive]}, 2000, 600), ([progressive], False))
        picture = video(acodec='none')
        audio = video(vcodec='none', acodec='mp4a', ext='m4a', filesize=100)
        self.assertEqual(len(select_formats({'duration': 10, 'formats': [picture, audio]}, 2000, 600)[0]), 2)
        for info in ({'duration': 10, 'formats': [picture]},
                     {'duration': 601, 'formats': [progressive]},
                     {'formats': [progressive]},
                     {'duration': 10, 'formats': [video(vcodec='hevc')]},
                     {'duration': 10, 'formats': [video(height=1080)]},
                     {'duration': 10, 'formats': [video(fragments=[{}])]},
                     {'duration': 10, 'formats': [video(filesize=10000)]}):
            with self.assertRaises(VideoError):
                select_formats(info, 2000, 600)
        self.assertTrue(select_formats({'duration': 10, 'formats': [picture], 'audio_channels': 0}, 2000, 600)[1])
        chosen = select_formats({'duration': 10, 'formats': [video(height=720, filesize=9999), progressive]}, 2000, 600)[0]
        self.assertEqual(chosen, [progressive])

    def test_cdn_url_boundary(self):
        self.assertIn('bilivideo.com', media_url(video()['url'], 'bilibili'))
        for url in ('https://bilivideo.com.evil.test/a', 'https://127.0.0.1/a',
                    'http://cn.bilivideo.com/a', 'https://user:pass@cn.bilivideo.com/a',
                    'https://www.bilibili.com/a', 'https://media.example/a'):
            with self.assertRaises(VideoError):
                media_url(url, 'bilibili')

    def fetch(self, handler, addresses=('8.8.8.8',), limit=2000):
        real_client = httpx.Client
        def client(**settings):
            self.assertFalse(settings['trust_env'])
            self.assertFalse(settings['follow_redirects'])
            def streaming(request):
                self.assertEqual(request.url.host, '8.8.8.8')
                self.assertEqual(request.headers['host'], 'cn-example.bilivideo.com')
                self.assertEqual(request.extensions['sni_hostname'], 'cn-example.bilivideo.com')
                self.assertNotIn('cookie', request.headers)
                self.assertNotIn('authorization', request.headers)
                response = handler(request)
                if response.is_stream_consumed:
                    response.stream = httpx.ByteStream(response.content)
                    response.is_stream_consumed = False
                    response.is_closed = False
                return response
            return real_client(transport=httpx.MockTransport(streaming), **settings)
        dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443)) for ip in addresses]
        with tempfile.TemporaryDirectory() as tmp:
            with patch('httpx.Client', side_effect=client), patch('socket.getaddrinfo', return_value=dns):
                manifest = download_tracks({'duration': 10, 'formats': [video(filesize=0)]}, 'bilibili', tmp, limit, 600)
            self.assertEqual((Path(tmp) / 'input0.mp4').read_bytes(), b'media')
            self.assertEqual(manifest['files'], ['input0.mp4'])

    def test_streaming_boundaries_redirect_dns_and_headers(self):
        self.fetch(lambda r: httpx.Response(200, content=b'media'))
        for handler, addresses, limit in (
            (lambda r: httpx.Response(302, headers={'location': 'https://127.0.0.1/a'}), ('8.8.8.8',), 2000),
            (lambda r: httpx.Response(200, content=b'media'), ('8.8.8.8', '10.0.0.1'), 2000),
            (lambda r: httpx.Response(200, content=b'x' * 100), ('8.8.8.8',), 5),
            (lambda r: httpx.Response(200, headers={'content-encoding': 'gzip'}, content=b'x'), ('8.8.8.8',), 2000),
            (lambda r: httpx.Response(200, headers={'content-type': 'application/vnd.apple.mpegurl'}, content=b'x'), ('8.8.8.8',), 2000),
        ):
            with self.assertRaises(VideoError):
                self.fetch(handler, addresses, limit)

    def test_request_gate(self):
        for text in (f'解析 {URL}', f'解析并发送 {URL}', f'下载视频 {URL}', f'解析 {BV}'):
            self.assertIsNotNone(send_request(text), text)
        for text in (URL, f'总结 {URL}', f'不要解析 {URL}', f'能解析吗 {URL}',
                     f'讨论这个视频 {URL}', f'“解析 {URL}”', f'> 解析 {URL}',
                     f'【解析视频】 {URL}', 'https://b23.tv/resolve', f'only metadata {URL}'):
            self.assertIsNone(send_request(text), text)


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_files_preserved_and_symlinks_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = VideoSettings(media_directory=str(Path(tmp) / 'media'))
            backend = VideoBackend(settings)
            materializer = VideoMaterializer(settings, backend)
            materializer.initialize()
            try:
                unrelated = materializer.outbox / 'unrelated.mp4'
                unrelated.write_bytes(b'keep')
                directory = materializer.staging / ('job-' + 'a' * 32)
                directory.mkdir()
                (directory / 'unexpected.txt').write_text('keep')
                materializer.sweep(abandoned=True)
                self.assertTrue(unrelated.exists())
                self.assertTrue((directory / 'unexpected.txt').exists())
                external = Path(tmp) / 'external'
                external.write_bytes(b'private')
                try:
                    (materializer.outbox / ('b' * 32 + '.mp4')).symlink_to(external)
                except OSError:
                    return  # Windows without developer-mode symlink privilege.
                with self.assertRaises(VideoError):
                    materializer._usage()
                materializer.sweep()
                self.assertEqual(external.read_bytes(), b'private')
            finally:
                await materializer.close()
                await backend.close()


class StoreTests(unittest.TestCase):
    def test_claim_recovery_unknown_and_scoping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'deliveries.db')
            store = VideoStore(path)
            self.assertIsNone(store.claim('private:7', 1, 'bili:1'))
            prior = store.claim('private:7', 1, 'bili:2')
            assert prior is not None
            self.assertEqual(prior['status'], 'preparing')
            store.set('private:7', 1, 'sending')
            store.close()
            store = VideoStore(path)
            prior = store.claim('private:7', 1, 'bili:1')
            assert prior is not None
            self.assertEqual(prior['status'], 'unknown')
            self.assertIsNone(store.claim('group:7', 1, 'bili:1'))
            store.close()


class SendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        settings = VideoSettings(media_directory=str(root / 'media'), delivery_database=str(root / 'ledger.db'))
        self.sender = AsyncMock(return_value={'message_id': 456})
        self.service = VideoService(settings, self.sender)
        self.service.backend.resolve = AsyncMock(return_value=META)
        self.service.materializer.initialize()
        artifact = VideoArtifact(self.service.materializer.outbox / ('a' * 32 + '.mp4'), META)
        artifact.path.write_bytes(b'fixture')
        self.service.materializer.materialize = AsyncMock(return_value=artifact)
        self.event = SimpleNamespace(user_id=7, message_id=1, message_type='private')
        self.lease = self.service.open(self.event, 'private:7', 's1', text='解析 ' + URL)

    async def asyncTearDown(self):
        await self.service.close()
        self.tmp.cleanup()

    async def send(self):
        return await self.service.call(self.lease.nonce, 's1', {'source': URL}, 'send')

    async def test_sent_current_target_sanitized_and_deduped(self):
        result = await self.send()
        self.assertEqual(result['status'], 'sent')
        self.assertNotIn(str(self.tmp.name), json.dumps(result))
        target, uri, timeout = self.sender.await_args.args
        self.assertEqual(target, 'private:7')
        self.assertTrue(uri.startswith('file:///app/qqbot-video/'))
        self.assertLessEqual(timeout, 25)
        self.assertEqual((await self.send())['status'], 'sent')
        self.assertTrue((await self.send())['duplicate'])
        self.sender.assert_awaited_once()

    async def test_unknown_rejected_missing_receipt_and_no_auto_retry(self):
        for outcome, expected in ((OneBotError('timeout'), 'unknown'), (OneBotRejected('denied'), 'rejected'), ({}, 'unknown')):
            self.event.message_id += 1
            self.lease = self.service.open(self.event, 'private:7', 's1', text='解析 ' + URL)
            self.sender.side_effect = outcome if isinstance(outcome, Exception) else None
            self.sender.return_value = outcome if isinstance(outcome, dict) else None
            self.assertEqual((await self.send())['status'], expected)
            self.assertEqual((await self.send())['status'], expected)
        self.assertEqual(self.sender.await_count, 3)

    async def test_authority_source_and_plain_link_discussion(self):
        result = await self.service.call(self.lease.nonce, 's1', {'source': 'av123'}, 'send')
        self.assertEqual(result['error']['code'], 'INVALID_SOURCE')
        for text in (URL, '总结 ' + URL):
            lease = self.service.open(self.event, 'private:7', 's2', text=text)
            result = await self.service.call(lease.nonce, 's2', {'source': URL}, 'send')
            self.assertEqual(result['status'], 'confirmation_required')
        self.sender.assert_not_awaited()
        self.service.backend.resolve.assert_not_awaited()

    async def test_cancelled_send_becomes_unknown_and_artifact_retained(self):
        entered = asyncio.Event()
        async def hang(*args):
            entered.set()
            await asyncio.Event().wait()
        self.sender.side_effect = hang
        task = asyncio.create_task(self.send())
        await asyncio.wait_for(entered.wait(), 3)
        self.service.revoke(self.lease)
        with self.assertRaises(asyncio.CancelledError):
            await task
        prior = self.service.store.claim('private:7', 1, 'same')
        assert prior is not None
        self.assertEqual(prior['status'], 'unknown')
        self.assertTrue(self.service.materializer.materialize.return_value.path.exists())

    async def test_acquisition_failure_never_attempts_qq_send(self):
        self.service.materializer.materialize.side_effect = VideoError('LIMIT_EXCEEDED', 'too large')
        self.assertEqual((await self.send())['error']['code'], 'LIMIT_EXCEEDED')
        self.assertEqual((await self.send())['status'], 'acquisition_failed')
        self.sender.assert_not_awaited()

    async def test_metadata_rejection_details_reach_tool_without_downloading(self):
        failure = {'schema_version': 1, 'status': 'error', 'error': {
            'code': 'UPSTREAM_BLOCKED', 'stage': 'metadata', 'http_status': 412,
            'message': 'Platform rejected the server request'}}
        self.service.backend.resolve.return_value = failure
        self.assertEqual(await self.send(), failure)
        self.service.materializer.materialize.assert_not_awaited()
        self.sender.assert_not_awaited()

    async def test_group_destination_deadline_and_revocation_before_send(self):
        self.event.message_type = 'group'
        self.event.group_id = 42
        lease = self.service.open(self.event, 'group:42', 'group-session', text='解析 ' + URL)
        result = await self.service.call(lease.nonce, 'group-session', {'source': URL}, 'send')
        self.assertEqual(result['status'], 'sent')
        self.assertEqual(self.sender.await_args.args[0], 'group:42')
        self.event.message_id = 99
        lease = self.service.open(self.event, 'group:42', 'late', text='解析 ' + URL,
                                  deadline=time.monotonic() + 1)
        result = await self.service.call(lease.nonce, 'late', {'source': URL}, 'send')
        self.assertEqual(result['error']['code'], 'TIMEOUT')
        self.sender.assert_awaited_once()
        self.event.message_id = 100
        lease = self.service.open(self.event, 'group:42', 'revoked', text='解析 ' + URL)
        async def revoke(*args):
            self.service.revoke(lease)
            await asyncio.sleep(0)
        self.service.materializer.materialize.side_effect = revoke
        with self.assertRaises(asyncio.CancelledError):
            await self.service.call(lease.nonce, 'revoked', {'source': URL}, 'send')
        self.sender.assert_awaited_once()

    async def test_onebot_video_helpers(self):
        client = object.__new__(OneBotWSClient)
        client.call = AsyncMock(return_value={'message_id': 1})
        await client.send_private_video(7, 'file:///app/qqbot-video/a.mp4', timeout=12)
        self.assertEqual(client.call.await_args.args, ('send_private_msg', {
            'user_id': 7, 'message': [{'type': 'video', 'data': {'file': 'file:///app/qqbot-video/a.mp4'}}]}))
        self.assertEqual(client.call.await_args.kwargs, {'timeout': 12})
        await client.send_group_video(42, 'file:///app/qqbot-video/a.mp4')
        self.assertEqual(client.call.await_args.args[1]['group_id'], 42)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg/ffprobe required')
class AssemblyTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_dash_merge_probe_and_media_lifetime(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = VideoSettings(media_directory=str(Path(tmp) / 'media'),
                                     ffmpeg=str(Path(shutil.which('ffmpeg')).resolve()),
                                     ffprobe=str(Path(shutil.which('ffprobe')).resolve()))
            backend = VideoBackend(settings)
            materializer = VideoMaterializer(settings, backend)
            materializer.initialize()
            directory = materializer.staging / ('job-' + 'b' * 32)
            directory.mkdir()
            try:
                for args in ([ '-f', 'lavfi', '-i', 'color=c=black:s=160x90:r=10', '-t', '0.5',
                                '-c:v', 'libx264', '-an', str(directory / 'input0.mp4')],
                             ['-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=mono', '-t', '0.5',
                              '-c:a', 'aac', '-vn', str(directory / 'input1.mp4')]):
                    subprocess.run([settings.ffmpeg, '-nostdin', '-v', 'error', *args], check=True, capture_output=True)
                await materializer.assemble(directory, ['input0.mp4', 'input1.mp4'], False,
                                            directory / 'output.mp4', time.monotonic() + 15, 1_000_000)
                self.assertTrue((directory / 'output.mp4').is_file())
                inputs = {name: (directory / name).read_bytes() for name in ('input0.mp4', 'input1.mp4')}
                async def download(source, part, cookie, *, media):
                    for name, data in inputs.items():
                        (Path(media['directory']) / name).write_bytes(data)
                    return {**META, 'duration_seconds': 0.5,
                            'artifacts': {'files': list(inputs), 'silent': False}}
                backend._extract = AsyncMock(side_effect=download)
                artifact = await materializer.materialize(URL, 1, META, time.monotonic() + 15)
                self.assertTrue(artifact.path.is_file())
                self.assertNotIn('artifacts', artifact.metadata)
                # Retained files count against reservations and cannot be evicted early.
                with patch('video_delivery.DISK_LIMIT', 1), self.assertRaises(VideoError) as error:
                    await materializer.materialize(URL, 1, META, time.monotonic() + 15)
                self.assertEqual(error.exception.code, 'STORAGE_LIMIT')
                self.assertEqual(materializer._reserved, 0)
                self.assertTrue(artifact.path.exists())
                backend._extract.side_effect = None
                backend._extract.return_value = {'status': 'error', 'error': {
                    'code': 'UPSTREAM_BLOCKED', 'stage': 'extractor', 'http_status': 412,
                    'message': 'Platform rejected the server request'}}
                with self.assertRaises(VideoError) as blocked:
                    await materializer.materialize(URL, 1, META, time.monotonic() + 15)
                self.assertEqual(blocked.exception.result()['error']['http_status'], 412)
                self.assertEqual(blocked.exception.stage, 'extractor')
                async def unexpected(source, part, cookie, *, media):
                    (Path(media['directory']) / 'input0.mp4').write_bytes(inputs['input0.mp4'])
                    return {**META, 'artifacts': {'files': ['../../private'], 'silent': False}}
                backend._extract.side_effect = unexpected
                with self.assertRaises(VideoError):
                    await materializer.materialize(URL, 1, META, time.monotonic() + 15)
                self.assertEqual(materializer._reserved, 0)
                path = materializer.outbox / ('c' * 32 + '.mp4')
                (directory / 'output.mp4').replace(path)
                materializer.sweep()
                self.assertTrue(path.exists())
                import os
                os.utime(path, (time.time() - 700, time.time() - 700))
                materializer.sweep()
                self.assertFalse(path.exists())
            finally:
                await materializer.close()
                await backend.close()


if __name__ == '__main__':
    unittest.main()
