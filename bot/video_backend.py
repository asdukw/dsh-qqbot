"""Cancellable, output-bounded extractor process and metadata-only cache."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import time
from collections import OrderedDict
from pathlib import Path

from video_platforms import VideoError, parse_source
from video_resolver import VideoSettings


async def bounded_read(reader: asyncio.StreamReader, limit: int) -> bytes:
    chunks = []
    size = 0
    while chunk := await reader.read(4096):
        size += len(chunk)
        if size > limit:
            raise VideoError("LIMIT_EXCEEDED", "Video worker output exceeded its limit")
        chunks.append(chunk)
    return b"".join(chunks)


async def stop_process(process):
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), 0.5)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    await process.wait()


class VideoBackend:
    def __init__(self, settings: VideoSettings):
        self.settings = settings
        self._slots = asyncio.Semaphore(max(1, min(4, settings.concurrency)))
        self._cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
        self._active: set[asyncio.Task] = set()
        self._closed = False
        self._flights: dict[tuple, list] = {}
        self._versions: dict = {}
        self._waiters = 0
        self._cooldowns: dict[tuple, tuple[float, dict]] = {}

    async def close(self):
        self._closed = True
        tasks = tuple(self._active)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._cache.clear()
        self._flights.clear()
        self._cooldowns.clear()

    def _cooldown(self, platform, version):
        row = self._cooldowns.get((platform, version))
        if row and row[0] > time.monotonic():
            result = copy.deepcopy(row[1])
            result['error']['retry_after_seconds'] = max(1, int(row[0] - time.monotonic() + 1))
            return result
        self._cooldowns.pop((platform, version), None)
        return None

    def _record_failure(self, platform, version, result):
        if result.get('status') == 'error' and result.get('error', {}).get('code') in ('UPSTREAM_BLOCKED', 'RATE_LIMITED'):
            self._cooldowns[(platform, version)] = (time.monotonic() + 60, copy.deepcopy(result))
            while len(self._cooldowns) > 16:
                self._cooldowns.pop(next(iter(self._cooldowns)))

    async def _run(self, source, part, platform, cookie_file, version, media=None):
        try:
            async with asyncio.timeout(max(1, min(60, self.settings.timeout)) if media is None else 55), self._slots:
                cooldown = self._cooldown(platform, version)
                if cooldown:
                    return cooldown
                self._versions[asyncio.current_task()] = version
                try:
                    result = (await self._extract(source, part, cookie_file) if media is None else
                              await self._extract(source, part, cookie_file, media=media))
                except VideoError as exc:
                    result = exc.result()
                finally:
                    self._versions.pop(asyncio.current_task(), None)
                self._record_failure(platform, version, result)
                return result
        except TimeoutError:
            raise VideoError('TIMEOUT', 'Video resolution timed out') from None

    async def resolve(self, source: str, part: int | None = None) -> dict:
        if self._closed:
            raise VideoError('UNAVAILABLE', 'Video service is closed')
        target = parse_source(source, part)
        settings = self.settings
        if not settings.enabled or not getattr(settings, target.platform + '_enabled'):
            raise VideoError('UNSUPPORTED_SOURCE', 'This video platform is disabled')
        cookie_file, version = settings.credentials(target.platform)
        key = (target.platform, target.url if target.short else target.content_id, part if target.short else target.part, version)
        cached = self._cache.get(key)
        if cached and cached[0] > time.monotonic():
            self._cache.move_to_end(key)
            return copy.deepcopy(cached[1])
        cooldown = self._cooldown(target.platform, version)
        if cooldown:
            return cooldown
        if self._waiters >= 32:
            raise VideoError('BUSY', 'Video resolver waiter limit reached')
        flight = self._flights.get(key)
        if flight is None:
            if len(self._active) >= 8:
                raise VideoError('BUSY', 'Video resolver queue is full')
            task = asyncio.create_task(self._resolve_owner(key, source, part, target.platform, cookie_file, version))
            flight = [task, 0]
            self._flights[key] = flight
            self._active.add(task)
            task.add_done_callback(self._active.discard)
        task = flight[0]
        flight[1] += 1
        self._waiters += 1
        try:
            async with asyncio.timeout(max(1, min(60, settings.timeout))):
                return copy.deepcopy(await asyncio.shield(task))
        except TimeoutError:
            raise VideoError('TIMEOUT', 'Video resolution timed out') from None
        finally:
            self._waiters -= 1
            flight[1] -= 1
            if flight[1] == 0:
                if self._flights.get(key) is flight:
                    self._flights.pop(key, None)
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _resolve_owner(self, key, source, part, platform, cookie_file, version):
        result = await self._run(source, part, platform, cookie_file, version)
        if result.get('status') == 'ok' and self.settings.cache_entries > 0:
            # Do not publish an old credential generation into a newly selected partition.
            if self.settings.credentials(platform)[1] == version:
                expiry = time.monotonic() + max(0, min(600, self.settings.cache_seconds))
                self._cache[key] = (expiry, copy.deepcopy(result))
                if result.get('canonical_url'):
                    canonical = parse_source(result['canonical_url'], result.get('selected_part'))
                    canonical_key = (platform, canonical.content_id, canonical.part, version)
                    self._cache[canonical_key] = (expiry, copy.deepcopy(result))
                while len(self._cache) > min(256, self.settings.cache_entries):
                    self._cache.popitem(last=False)
        return result

    async def materialize(self, source, part, *, media):
        if self._closed:
            raise VideoError('UNAVAILABLE', 'Video service is closed')
        target = parse_source(source, part)
        if not self.settings.enabled or not getattr(self.settings, target.platform + '_enabled'):
            raise VideoError('UNSUPPORTED_SOURCE', 'This video platform is disabled')
        cookie_file, version = self.settings.credentials(target.platform)
        if len(self._active) >= 8:
            raise VideoError('BUSY', 'Video resolver queue is full')
        task = asyncio.create_task(self._run(source, part, target.platform, cookie_file, version, media))
        self._active.add(task)
        try:
            return await task
        finally:
            self._active.discard(task)

    async def _extract(self, source, part, cookie_file, *, media: dict | None = None):
        worker = Path(__file__).with_name("video_worker.py")
        # -I prevents ambient Python paths; only the trusted bot source is added explicitly.
        bootstrap = 'import runpy,sys; sys.path.insert(0,sys.argv[1]); runpy.run_path(sys.argv[2],run_name="__main__")'
        env = {
            key: os.environ[key]
            for key in ("SystemRoot", "WINDIR", "TEMP", "TMP")
            if key in os.environ
        }
        env.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"})
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-B",
                "-c",
                bootstrap,
                str(worker.parent),
                str(worker),
                cwd=str(worker.parent),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise VideoError("BACKEND_UNAVAILABLE", "Cannot start video worker") from exc
        readers = []
        try:
            if process.stdin is None or process.stdout is None or process.stderr is None:
                raise VideoError("UNAVAILABLE", "Video worker pipes unavailable")
            version = self._versions.get(asyncio.current_task(), ())
            expected = ({'credential_generation': version[1]} if version and version[0] == 'managed' else
                        {'credential_stat': list(version[2:])} if version and version[0] == 'legacy' else {})
            payload = json.dumps(
                {"source": source, "part": part, "cookie_file": cookie_file, **expected, **(media or {})}, ensure_ascii=False
            ).encode()
            if len(payload) > 8192:
                raise VideoError("INVALID_SOURCE", "Worker request too large")
            process.stdin.write(payload)
            await process.stdin.drain()
            process.stdin.close()
            readers = [
                asyncio.create_task(bounded_read(process.stdout, 16384)),
                asyncio.create_task(bounded_read(process.stderr, 4096)),
            ]
            stdout, _ = await asyncio.gather(*readers)
            await process.wait()
            if process.returncode != 0:
                raise VideoError("UPSTREAM_ERROR", "Video worker exited unexpectedly")
            result = json.loads(stdout)
            if not isinstance(result, dict) or result.get("status") not in ("ok", "error"):
                raise VideoError("UPSTREAM_ERROR", "Invalid video worker response")
            return result
        except (OSError, ValueError) as exc:
            raise VideoError("UPSTREAM_ERROR", "Video backend failed") from exc
        finally:
            cleanup = asyncio.create_task(stop_process(process))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await asyncio.shield(cleanup)
                raise
            finally:
                for reader in readers:
                    reader.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
