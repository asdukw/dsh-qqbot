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

    async def close(self):
        self._closed = True
        tasks = tuple(self._active)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._cache.clear()

    async def resolve(self, source: str, part: int | None = None) -> dict:
        if self._closed:
            raise VideoError("UNAVAILABLE", "Video service is closed")
        target = parse_source(source, part)
        settings = self.settings
        if not settings.enabled or not getattr(settings, target.platform + "_enabled"):
            raise VideoError("UNSUPPORTED_SOURCE", "This video platform is disabled")
        cookie_file = settings.cookie_file(target.platform)
        credential_version = None
        if cookie_file:
            try:
                stat = Path(cookie_file).stat()
                credential_version = (cookie_file, stat.st_mtime_ns, stat.st_size)
            except OSError:
                raise VideoError(
                    "CREDENTIALS_INVALID", "Configured platform cookie file is unavailable"
                ) from None
        key = (target.platform, target.content_id, target.part, credential_version)
        cached = self._cache.get(key) if not target.short else None
        if cached and cached[0] > time.monotonic():
            self._cache.move_to_end(key)
            return copy.deepcopy(cached[1])
        task = asyncio.current_task()
        if task is None or len(self._active) >= 8:
            raise VideoError("BUSY", "Video resolver queue is full")
        self._active.add(task)
        try:
            async with asyncio.timeout(max(1, min(60, settings.timeout))), self._slots:
                result = await self._extract(source, part, cookie_file)
            if result.get("status") == "ok" and not target.short and settings.cache_entries > 0:
                self._cache[key] = (
                    time.monotonic() + max(0, min(600, settings.cache_seconds)),
                    copy.deepcopy(result),
                )
                self._cache.move_to_end(key)
                while len(self._cache) > min(256, settings.cache_entries):
                    self._cache.popitem(last=False)
            return result
        except TimeoutError:
            raise VideoError("TIMEOUT", "Video resolution timed out") from None
        finally:
            self._active.discard(task)

    async def _extract(self, source, part, cookie_file):
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
            payload = json.dumps(
                {"source": source, "part": part, "cookie_file": cookie_file}, ensure_ascii=False
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
