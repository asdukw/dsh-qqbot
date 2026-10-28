"""Turn-scoped video bridge. Authority is separate from administrator card tools."""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from video_backend import VideoBackend
from video_delivery import VideoMaterializer
from video_platforms import VideoError, VideoTarget
from video_resolver import VideoSettings
from video_sending import send_request, send_video
from video_store import VideoStore


@dataclass(eq=False)
class VideoLease:
    nonce: str
    session: str
    expires: float
    actor: int
    conversation: str
    message_id: int = 0
    send_source: VideoTarget | None = None
    deadline: float = 0
    calls: int = 0
    tasks: set[asyncio.Task] = field(default_factory=set)
    expiry: asyncio.TimerHandle | None = None


class VideoService:
    def __init__(self, settings: VideoSettings, sender=None):
        self.settings = settings
        self.backend = VideoBackend(settings)
        self.materializer = VideoMaterializer(settings, self.backend)
        self.sender = sender
        self.store: VideoStore | None = None
        self.secret = secrets.token_urlsafe(32)
        self.url = ""
        self.leases: dict[str, VideoLease] = {}
        self._server: asyncio.Server | None = None
        self._lock = asyncio.Lock()
        self._connections: set[asyncio.Task] = set()
        self._closed = False

    async def start(self):
        async with self._lock:
            if self._closed:
                raise ValueError("video service is closed")
            if self._server is None:
                self._server = await asyncio.start_server(self._http, "127.0.0.1", 0, limit=8192)
                self.url = f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"

    def open(self, event, conversation: str, session: str, lifetime: float = 120,
             *, text: str = '', deadline: float | None = None) -> VideoLease:
        if (
            self._closed
            or not self.settings.enabled
            or event.message_id <= 0
            or event.user_id <= 0
            or not session
        ):
            raise ValueError("video authority requires a real chat message")
        expected = (
            f"private:{event.user_id}"
            if event.message_type == "private"
            else f"group:{getattr(event, 'group_id', 0)}"
            if event.message_type == "group"
            else ""
        )
        if conversation != expected or expected == "group:0":
            raise ValueError("video conversation mismatch")
        if len(self.leases) >= 16:
            raise ValueError("too many video turns")
        ttl = max(1, min(600, lifetime))
        lease = VideoLease(
            secrets.token_urlsafe(32), session, time.monotonic() + ttl, event.user_id, conversation
        )
        lease.message_id = event.message_id
        lease.deadline = min(lease.expires, deadline if deadline is not None else lease.expires - 10)
        lease.send_source = send_request(text)
        self.leases[lease.nonce] = lease
        lease.expiry = asyncio.get_running_loop().call_later(ttl, self.revoke, lease)
        return lease

    def env(self, lease: VideoLease) -> dict[str, str]:
        return {
            "QQBOT_VIDEO_BRIDGE": self.url,
            "QQBOT_VIDEO_SECRET": self.secret,
            "QQBOT_VIDEO_LEASE": lease.nonce,
        }

    def revoke(self, lease: VideoLease):
        self.leases.pop(lease.nonce, None)
        if lease.expiry:
            lease.expiry.cancel()
        for task in tuple(lease.tasks):
            task.cancel()

    def authorize(self, nonce: str, session: str) -> VideoLease:
        lease = self.leases.get(nonce)
        if (
            self._closed
            or lease is None
            or lease.session != session
            or lease.expires <= time.monotonic()
        ):
            if lease and lease.expires <= time.monotonic():
                self.revoke(lease)
            raise ValueError("video authority expired or invalid")
        return lease

    async def call(self, nonce: str, session: str, args: dict, operation: str = 'resolve') -> dict:
        lease = self.authorize(nonce, session)
        if operation not in ('resolve', 'send'):
            raise ValueError('unsupported video operation')
        if not isinstance(args, dict) or set(args) - {"source", "part"} or "source" not in args:
            raise ValueError("video_resolve accepts source and optional part only")
        lease.calls += 1
        if lease.calls > min(5, max(1, self.settings.calls_per_turn)):
            raise ValueError("video turn call limit reached")
        task = asyncio.current_task()
        if task is None:
            raise ValueError("video call requires active task")
        lease.tasks.add(task)
        try:
            try:
                result = (await send_video(self, lease, args) if operation == "send" else
                          await self.backend.resolve(args["source"], args.get("part")))
            except VideoError as exc:
                result = exc.result()
            self.authorize(nonce, session)
            return result
        finally:
            lease.tasks.discard(task)

    async def close(self):
        self._closed = True
        active = {task for lease in self.leases.values() for task in lease.tasks}
        for lease in tuple(self.leases.values()):
            self.revoke(lease)
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for task in tuple(self._connections):
            task.cancel()
        await asyncio.gather(*tuple(self._connections), *active, return_exceptions=True)
        await self.materializer.close()
        await self.backend.close()
        if self.store is not None:
            self.store.close()
            self.store = None

    async def _http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        task = asyncio.current_task()
        if task is None or self._closed or len(self._connections) >= 16:
            writer.close()
            return
        self._connections.add(task)
        operation: asyncio.Task[Any] | None = None
        disconnect: asyncio.Task[Any] | None = None
        status, result = 400, {"error": "invalid video bridge request"}
        try:
            async with asyncio.timeout(110):
                header = await reader.readuntil(b"\r\n\r\n")
                lines = header.decode("ascii").split("\r\n")
                fields = {}
                for line in lines[1:]:
                    if not line:
                        continue
                    key, separator, value = line.partition(":")
                    key = key.lower()
                    if not separator or key in fields or key.strip() != key:
                        raise ValueError("invalid headers")
                    fields[key] = value.strip()
                if (
                    lines[0] != "POST /video HTTP/1.1"
                    or "origin" in fields
                    or "transfer-encoding" in fields
                    or not hmac.compare_digest(
                        fields.get("authorization", ""), "Bearer " + self.secret
                    )
                ):
                    raise ValueError("invalid authorization")
                length = int(fields.get("content-length", "0"))
                if not 0 < length <= 8192:
                    raise ValueError("invalid size")
                body = json.loads(await reader.readexactly(length))
                if (not isinstance(body, dict) or set(body) - {"lease", "session", "args", "operation"}
                        or not {"lease", "session", "args"} <= set(body)):
                    raise ValueError("invalid envelope")
                if not isinstance(body["lease"], str) or not isinstance(body["session"], str):
                    raise ValueError("invalid authority")
                operation = asyncio.create_task(
                    self.call(body["lease"], body["session"], body["args"], body.get("operation", "resolve"))
                )
                disconnect = asyncio.create_task(reader.read(1))
                done, _ = await asyncio.wait(
                    (operation, disconnect), return_when=asyncio.FIRST_COMPLETED
                )
                if disconnect in done:
                    operation.cancel()
                    return
                result = await operation
                status = 200
        except (
            ValueError,
            OSError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            pass
        finally:
            for pending in (operation, disconnect):
                if pending is not None and not pending.done():
                    pending.cancel()
            await asyncio.gather(
                *(p for p in (operation, disconnect) if p is not None), return_exceptions=True
            )
            try:
                data = json.dumps(result, ensure_ascii=False).encode()
                if len(data) > 16384:
                    data = b'{"error":"video result too large"}'
                    status = 400
                writer.write(
                    f"HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode()
                    + data
                )
                await writer.drain()
            except (OSError, ConnectionError):
                pass
            self._connections.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
