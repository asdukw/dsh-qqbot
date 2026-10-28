"""Local video assembly and private artifact lifecycle, outside the DSH workspace."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from video_backend import VideoBackend, bounded_read, stop_process
from video_platforms import VideoError
from video_resolver import VideoSettings, number

DISK_LIMIT = 256 * 1024 * 1024
RETENTION = 600


@dataclass
class VideoArtifact:
    path: Path
    metadata: dict


async def command(executable: str, args: list[str], timeout: float) -> bytes:
    if not Path(executable).is_absolute() or not Path(executable).is_file():
        raise VideoError('DEPENDENCY_UNAVAILABLE', 'Configured ffmpeg/ffprobe executable is unavailable')
    process = await asyncio.create_subprocess_exec(
        executable, *args, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={k: v for k, v in os.environ.items() if k in ('SystemRoot', 'WINDIR', 'TEMP', 'TMP')})
    tasks = []
    try:
        if process.stdout is None or process.stderr is None:
            raise VideoError('UNAVAILABLE', 'Media process pipes unavailable')
        tasks = [asyncio.create_task(bounded_read(process.stdout, 16384)),
                 asyncio.create_task(bounded_read(process.stderr, 16384))]
        async with asyncio.timeout(max(0.01, timeout)):
            stdout, _ = await asyncio.gather(*tasks)
            await process.wait()
        if process.returncode != 0:
            raise VideoError('UNSUPPORTED_MEDIA', 'Local media validation or assembly failed')
        return stdout
    finally:
        cleanup = asyncio.create_task(stop_process(process))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await asyncio.shield(cleanup)
            raise
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


class VideoMaterializer:
    def __init__(self, settings: VideoSettings, backend: VideoBackend):
        self.settings, self.backend = settings, backend
        self.root = Path(settings.media_directory)
        self.staging, self.outbox = self.root / 'staging', self.root / 'outbox'
        self._active: set[asyncio.Task] = set()
        self._reserved = 0
        self._slots = asyncio.Semaphore(1)
        self._initialized = False
        self._sweeper: asyncio.Task | None = None
        self._closed = False

    def initialize(self):
        if self._initialized:
            return
        for directory in (self.root, self.staging, self.outbox):
            if directory.is_symlink():
                raise VideoError('UNAVAILABLE', 'Unsafe video storage directory')
            directory.mkdir(parents=True, exist_ok=True, mode=0o750)
        # Only our strict random job names are eligible for startup cleanup.
        self.sweep(abandoned=True)
        self._initialized = True
        self._sweeper = asyncio.create_task(self._sweep_loop())

    def sweep(self, *, abandoned=False):
        for path in self.outbox.iterdir():
            if re.fullmatch(r'[0-9a-f]{32}\.mp4', path.name) and not path.is_symlink():
                if path.is_file() and time.time() - path.stat().st_mtime >= RETENTION:
                    path.unlink()
        if abandoned:
            for path in self.staging.iterdir():
                if re.fullmatch(r'job-[0-9a-f]{32}', path.name) and path.is_dir() and not path.is_symlink():
                    self._remove_job(path)

    async def _sweep_loop(self):
        while True:
            await asyncio.sleep(60)
            self.sweep()

    def _remove_job(self, directory: Path):
        # Never follow a symlink or recursively delete unexpected directory trees.
        for path in directory.iterdir():
            if path.name not in ('input0.mp4', 'input1.mp4', 'output.mp4') or not path.is_file() or path.is_symlink():
                return
        for path in directory.iterdir():
            path.unlink()
        directory.rmdir()

    def _usage(self) -> int:
        total = 0
        for directory in (self.staging, self.outbox):
            for path in directory.rglob('*'):
                if path.is_symlink():
                    raise VideoError('UNAVAILABLE', 'Unsafe video storage contents')
                if path.is_file():
                    total += path.stat().st_size
        return total

    async def materialize(self, source: str, part: int | None, metadata: dict,
                          deadline: float) -> VideoArtifact:
        if self._closed:
            raise VideoError('UNAVAILABLE', 'Video sender is closed')
        for executable in (self.settings.ffmpeg, self.settings.ffprobe):
            if not Path(executable).is_absolute() or not Path(executable).is_file():
                raise VideoError('DEPENDENCY_UNAVAILABLE', 'Install/configure ffmpeg and ffprobe before sending')
        self.initialize()
        task = asyncio.current_task()
        if task is None or len(self._active) >= 3:
            raise VideoError('BUSY', 'Video sending queue is full')
        max_bytes = min(64 * 1024 * 1024, max(1024, self.settings.max_send_bytes))
        reservation = 3 * max_bytes
        self._active.add(task)
        directory = None
        reserved = False
        try:
            async with asyncio.timeout_at(deadline), self._slots:
                self.sweep()
                if self._usage() + self._reserved + reservation > DISK_LIMIT or shutil.disk_usage(self.root).free < reservation:
                    raise VideoError('STORAGE_LIMIT', 'Video storage is busy; retained uploads cannot be removed yet')
                self._reserved += reservation
                reserved = True
                directory = self.staging / ('job-' + secrets.token_hex(16))
                directory.mkdir(mode=0o700)
                # Metadata cache is not a downloadable artifact; always get fresh resources.
                result = await self.backend.materialize(metadata['canonical_url'], metadata['selected_part'], media={
                        'materialize': True, 'directory': str(directory.resolve()),
                        'max_bytes': max_bytes, 'max_duration': min(600, self.settings.max_send_duration),
                        'download_timeout': min(35, max(0.01, deadline - time.monotonic() - 10))})
                if result.get('status') != 'ok':
                    error = result.get('error', {})
                    raise VideoError(error.get('code', 'UPSTREAM_ERROR'), error.get('message', 'Video acquisition failed'),
                                     stage=error.get('stage'), http_status=error.get('http_status'))
                if (result.get('content_id') != metadata.get('content_id')
                        or result.get('selected_part') != metadata.get('selected_part')
                        or result.get('platform') != metadata.get('platform')):
                    raise VideoError('UNSUPPORTED_MEDIA', 'Downloaded media identity changed')
                manifest = result.pop('artifacts', {})
                files = manifest.get('files')
                if files not in (['input0.mp4'], ['input0.mp4', 'input1.mp4']) or type(manifest.get('silent')) is not bool:
                    raise VideoError('UNSUPPORTED_MEDIA', 'Invalid media artifact manifest')
                if {p.name for p in directory.iterdir()} != set(files):
                    raise VideoError('UNSUPPORTED_MEDIA', 'Unexpected media artifacts')
                for name in files:
                    path = directory / name
                    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                        raise VideoError('UNSUPPORTED_MEDIA', 'Invalid media artifact')
                if sum((directory / name).stat().st_size for name in files) > max_bytes:
                    raise VideoError('LIMIT_EXCEEDED', 'Media inputs exceed the size limit')
                output = directory / 'output.mp4'
                await self.assemble(directory, files, manifest['silent'], output,
                                    min(deadline, time.monotonic() + 10), max_bytes, result.get('duration_seconds'))
                destination = self.outbox / (secrets.token_hex(16) + '.mp4')
                output.chmod(0o640)
                output.replace(destination)
                os.utime(destination, None)
                return VideoArtifact(destination, result)
        except TimeoutError:
            raise VideoError('TIMEOUT', 'Video acquisition timed out') from None
        finally:
            if directory is not None:
                self._remove_job(directory)
            if reserved:
                self._reserved -= reservation
            self._active.discard(task)

    async def assemble(self, directory, files, silent, output, deadline, max_bytes, expected_duration=None):
        args = ['-nostdin', '-v', 'error', '-n']
        for name in files:
            args += ['-protocol_whitelist', 'file', '-f', 'mov', '-i', str((directory / name).resolve())]
        args += ['-map', '0:v:0']
        if not silent:
            args += ['-map', '1:a:0' if len(files) == 2 else '0:a:0']
        args += ['-c', 'copy', '-movflags', '+faststart', '-fs', str(max_bytes + 1), '-f', 'mp4', str(output.resolve())]
        await command(self.settings.ffmpeg, args, min(10, deadline - time.monotonic()))
        if not output.is_file() or output.is_symlink() or not 0 < output.stat().st_size <= max_bytes:
            raise VideoError('LIMIT_EXCEEDED', 'Final video exceeds the size limit')
        raw = await command(self.settings.ffprobe, ['-v', 'error', '-protocol_whitelist', 'file', '-f', 'mov',
                            '-show_entries', 'stream=codec_type,codec_name,height:format=duration,format_name',
                            '-of', 'json', str(output.resolve())], min(5, deadline - time.monotonic()))
        try:
            probe = json.loads(raw)
            streams = probe['streams']
            videos = [s for s in streams if s.get('codec_type') == 'video']
            audios = [s for s in streams if s.get('codec_type') == 'audio']
            duration = float(probe['format']['duration'])
            if (len(videos) != 1 or videos[0].get('codec_name') != 'h264'
                    or not 0 < videos[0].get('height', 0) <= 720
                    or (not silent and (len(audios) != 1 or audios[0].get('codec_name') != 'aac'))
                    or (silent and audios) or number(duration) is None
                    or not 0 < duration <= min(600, self.settings.max_send_duration)
                    or (expected_duration is not None and (number(expected_duration) is None
                        or abs(duration - expected_duration) > max(2, expected_duration * 0.02)))):
                raise ValueError('invalid streams')
        except (ValueError, KeyError, TypeError) as exc:
            raise VideoError('UNSUPPORTED_MEDIA', 'Final video is not compatible H.264/AAC MP4') from exc

    def retain(self, artifact: VideoArtifact):
        if artifact.path.parent == self.outbox and not artifact.path.is_symlink():
            os.utime(artifact.path, None)

    def uri(self, artifact: VideoArtifact) -> str:
        if artifact.path.parent != self.outbox or not re.fullmatch(r'[0-9a-f]{32}\.mp4', artifact.path.name):
            raise VideoError('UNAVAILABLE', 'Invalid published video artifact')
        base = self.settings.container_directory
        if not base.startswith('/') or '..' in Path(base).parts:
            raise VideoError('UNAVAILABLE', 'Invalid container video directory')
        from urllib.parse import quote
        return 'file://' + quote(base.rstrip('/') + '/' + artifact.path.name, safe='/')

    async def close(self):
        self._closed = True
        if self._sweeper:
            self._sweeper.cancel()
            await asyncio.gather(self._sweeper, return_exceptions=True)
        for task in tuple(self._active):
            task.cancel()
        await asyncio.gather(*tuple(self._active), return_exceptions=True)
