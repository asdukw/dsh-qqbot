from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
import unittest
from http.cookiejar import CookieJar
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from video_backend import VideoBackend  # noqa: E402
from video_credentials import CredentialError, effective, load_jar, save, status  # noqa: E402
from video_platforms import VideoError  # noqa: E402
from video_resolver import VideoSettings  # noqa: E402

BV = 'BV1xx411c7mD'
SECRET = 'credential-test-value=='


class CredentialTests(unittest.TestCase):
    def test_header_and_netscape_scope_and_no_echo(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = save(tmp, 'bilibili', 'header', 'Cookie: SESSDATA=' + SECRET + '; other=x')
            self.assertNotIn(SECRET, json.dumps(result))
            path, version = effective(tmp, 'bilibili')
            jar = CookieJar()
            load_jar(path, jar, 'bilibili')
            for host in ('www.bilibili.com', 'api.bilibili.com'):
                request = Request('https://' + host + '/')
                jar.add_cookie_header(request)
                self.assertIn('SESSDATA=' + SECRET, request.get_header('Cookie'))
            for host in ('b23.tv', 'www.douyin.com', 'a.bilivideo.com', 'child.www.bilibili.com'):
                request = Request('https://' + host + '/')
                jar.add_cookie_header(request)
                self.assertIsNone(request.get_header('Cookie'))
            self.assertTrue(all(not cookie.domain_specified for cookie in jar))
            save(tmp, 'bilibili', 'netscape', '# Netscape HTTP Cookie File\n#HttpOnly_.bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tnew==\n')
            self.assertNotEqual(version, effective(tmp, 'bilibili')[1])
            if os.name != 'nt':
                self.assertEqual(stat.S_IMODE(Path(path).stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(Path(tmp).stat().st_mode), 0o700)

    def test_invalid_inputs_are_generic_and_leave_prior_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            save(tmp, 'douyin', 'header', 'session=' + SECRET)
            prior = effective(tmp, 'douyin')[1]
            cases = [('header', ''), ('header', 'a=1\r\nAuthorization: ' + SECRET),
                     ('header', 'a=1; a=2'), ('header', 'https://example.com/'),
                     ('header', 'x=' + 'a' * 65536), ('json', '{}'),
                     ('netscape', '# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tTRUE\t0\tx\t' + SECRET),
                     ('netscape', '# Netscape HTTP Cookie File\n.douyin.com\tFALSE\t/\tTRUE\t0\tx\ty'),
                     ('netscape', '# Netscape HTTP Cookie File\n.douyin.com\tTRUE\t/\tTRUE\tx\tx\ty')]
            for format, content in cases:
                with self.subTest(format=format), self.assertRaises(CredentialError) as error:
                    save(tmp, 'douyin', format, content)
                self.assertNotIn(SECRET, str(error.exception))
                self.assertEqual(prior, effective(tmp, 'douyin')[1])
            self.assertFalse(list(Path(tmp).glob('.cookies-*')))

    def test_clear_masks_legacy_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = str(Path(tmp) / 'managed')
            legacy = Path(tmp) / 'legacy.txt'
            legacy.write_text('# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tTRUE\t0\tx\tlegacy\n')
            self.assertEqual(effective(directory, 'bilibili', str(legacy))[0], str(legacy))
            save(directory, 'bilibili', 'header', 'x=' + SECRET)
            self.assertNotEqual(effective(directory, 'bilibili', str(legacy))[0], str(legacy))
            result = save(directory, 'bilibili', clear=True)
            self.assertEqual((result['source'], result['configured']), ('cleared', False))
            self.assertEqual(effective(directory, 'bilibili', str(legacy))[0], '')
            self.assertIn('legacy', legacy.read_text())
            self.assertFalse(status(directory, 'bilibili', str(legacy))['configured'])

    def test_record_limit_and_parallel_atomic_replace(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CredentialError):
                save(tmp, 'douyin', 'header', '; '.join(f'x{i}=value' for i in range(257)))
            save(tmp, 'bilibili', 'header', 'x=first')
            def replace(index):
                save(tmp, 'bilibili', 'header', f'x=value{index}')
                path, _ = effective(tmp, 'bilibili')
                jar = CookieJar()
                load_jar(path, jar, 'bilibili')
                values = {cookie.value for cookie in jar}
                self.assertEqual(len(values), 1)
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(replace, range(20)))
            self.assertFalse(list(Path(tmp).glob('.cookies-*')))

    def test_generation_mismatch_and_cross_platform_state_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            save(tmp, 'bilibili', 'header', 'x=first')
            path, generation = effective(tmp, 'bilibili')
            save(tmp, 'bilibili', 'header', 'x=second')
            with self.assertRaises(CredentialError):
                load_jar(path, CookieJar(), 'bilibili', generation[1])
            with self.assertRaises(CredentialError):
                load_jar(path, CookieJar(), 'douyin')

    def test_symlink_target_rejected_without_touching_other_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            external = Path(tmp) / 'private'
            external.write_text(SECRET)
            root = Path(tmp) / 'credentials'
            root.mkdir()
            try:
                (root / 'bilibili.json').symlink_to(external)
            except OSError:
                self.skipTest('symlink privilege unavailable')
            with self.assertRaises(CredentialError):
                save(str(root), 'bilibili', 'header', 'x=bad')
            self.assertEqual(external.read_text(), SECRET)


class SharedBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_singleflight_one_cancel_does_not_cancel_other(self):
        backend = VideoBackend(VideoSettings(cache_entries=0))
        entered, release = asyncio.Event(), asyncio.Event()
        async def extract(*args):
            entered.set()
            await release.wait()
            return {'status': 'ok', 'title': 'result'}
        backend._extract = AsyncMock(side_effect=extract)
        first = asyncio.create_task(backend.resolve(BV))
        await entered.wait()
        second = asyncio.create_task(backend.resolve(BV))
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        release.set()
        self.assertEqual((await second)['title'], 'result')
        self.assertEqual(backend._extract.await_count, 1)
        await backend.close()
        self.assertFalse(backend._active)

    async def test_last_cancel_reaps_owner_and_cooldown_is_credential_partitioned(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = VideoSettings(credentials_directory=tmp)
            backend = VideoBackend(settings)
            entered, stopped = asyncio.Event(), asyncio.Event()
            async def extract(*args):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
            backend._extract = AsyncMock(side_effect=extract)
            waiter = asyncio.create_task(backend.resolve(BV))
            await entered.wait()
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            self.assertTrue(stopped.is_set())
            backend._extract.side_effect = None
            backend._extract.return_value = VideoError('UPSTREAM_BLOCKED', 'blocked', http_status=412).result()
            self.assertEqual((await backend.resolve(BV))['error']['code'], 'UPSTREAM_BLOCKED')
            before = backend._extract.await_count
            self.assertGreater((await backend.resolve('av123'))['error']['retry_after_seconds'], 0)
            await backend.materialize(BV, None, media={})
            self.assertEqual(backend._extract.await_count, before)
            save(tmp, 'bilibili', 'header', 'SESSDATA=' + SECRET)
            await backend.resolve(BV)
            self.assertEqual(backend._extract.await_count, before + 1)
            save(tmp, 'bilibili', clear=True)
            await backend.resolve(BV)
            self.assertEqual(backend._extract.await_count, before + 2)
            await backend.close()

    async def test_queue_rechecks_cooldown_and_alias_cache_avoids_repeat_shortlink(self):
        backend = VideoBackend(VideoSettings(concurrency=1))
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked(*args):
            entered.set()
            await release.wait()
            return VideoError('RATE_LIMITED', 'rate limit').result()
        backend._extract = AsyncMock(side_effect=blocked)
        first = asyncio.create_task(backend.resolve(BV))
        await entered.wait()
        second = asyncio.create_task(backend.resolve('av123'))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(backend._extract.await_count, 1)
        await backend.close()
        backend = VideoBackend(VideoSettings())
        backend._extract = AsyncMock(return_value={'status': 'ok', 'canonical_url':
            f'https://www.bilibili.com/video/{BV}?p=2', 'selected_part': 2})
        await backend.resolve('https://b23.tv/test')
        await backend.resolve(BV, 2)
        self.assertEqual(backend._extract.await_count, 1)
        await backend.resolve('https://b23.tv/test', 1)
        self.assertEqual(backend._extract.await_count, 2)
        await backend.close()

    async def test_shared_waiter_limits_and_generation_change_dont_publish_old_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = VideoBackend(VideoSettings(credentials_directory=tmp))
            entered, release = asyncio.Event(), asyncio.Event()
            async def delayed(*args):
                entered.set()
                await release.wait()
                return {'status': 'ok', 'title': 'old'}
            backend._extract = AsyncMock(side_effect=delayed)
            first = asyncio.create_task(backend.resolve(BV))
            await entered.wait()
            save(tmp, 'bilibili', 'header', 'x=new')
            release.set()
            await first
            self.assertFalse(backend._cache)
            await backend.resolve(BV)
            self.assertEqual(backend._extract.await_count, 2)
            await backend.close()

    async def test_cooldown_expiry_and_waiter_limit_are_bounded(self):
        backend = VideoBackend(VideoSettings(cache_entries=0))
        backend._extract = AsyncMock(return_value=VideoError('UPSTREAM_BLOCKED', 'blocked').result())
        await backend.resolve(BV)
        version = backend.settings.credentials('bilibili')[1]
        row = backend._cooldowns[('bilibili', version)]
        backend._cooldowns[('bilibili', version)] = (0, row[1])
        await backend.resolve(BV)
        self.assertEqual(backend._extract.await_count, 2)
        backend._cooldowns.clear()
        backend._waiters = 32
        with self.assertRaises(VideoError) as error:
            await backend.resolve(BV)
        self.assertEqual(error.exception.code, 'BUSY')
        backend._waiters = 0
        await backend.close()

    async def test_hot_save_replace_clear_without_restarting_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = VideoSettings(credentials_directory=tmp)
            backend = VideoBackend(settings)
            backend._extract = AsyncMock(return_value={'status': 'ok', 'title': 'safe'})
            await backend.resolve(BV)
            await backend.resolve(BV)
            self.assertEqual(backend._extract.await_count, 1)
            for content in ('SESSDATA=first', 'SESSDATA=second'):
                save(tmp, 'bilibili', 'header', content)
                await backend.resolve(BV)
            save(tmp, 'bilibili', clear=True)
            await backend.resolve(BV)
            self.assertEqual(backend._extract.await_count, 4)
            self.assertEqual(backend._extract.await_args.args[2], '')
            await backend.close()


if __name__ == '__main__':
    unittest.main()
