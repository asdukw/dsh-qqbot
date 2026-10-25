"""OneBot 11 WebSocket client for NapCat."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import websockets
from log_context import get_logger, request_context

log = get_logger(__name__)


class OneBotError(RuntimeError):
    pass


class OneBotRejected(OneBotError):
    """The server explicitly rejected an action (not an unknown timeout)."""


@dataclass
class OneBotConfig:
    url: str = "ws://127.0.0.1:3001"
    token: str = ""
    self_id: int = 0


@dataclass
class PokeEvent:
    """A QQ "戳一戳" notice (post_type=notice, notice_type=notify)."""

    user_id: int
    target_id: int
    group_id: int | None = None


@dataclass
class RequestEvent:
    """A friend request or group invitation/join request (post_type=request)."""

    kind: str  # friend | group_invite | group_add
    flag: str
    user_id: int
    group_id: int | None = None
    group_name: str = ""
    requester_name: str = ""
    comment: str = ""
    received_at: float = 0.0


@dataclass
class MessageEvent:
    message_type: str
    user_id: int
    message_id: int
    text: str
    at_me: bool
    sender_name: str
    group_id: int | None = None
    images: tuple[str, ...] = ()
    # message_id of the message this one quotes, if any (OneBot reply segment).
    reply_to: int | None = None
    # Subset of images QQ marked as stickers (summary "[动画表情]" / sub_type 1).
    stickers: tuple[str, ...] = ()
    # The sender's QQ nickname; sender_name prefers the group card, so this can
    # differ and tells the model who is actually behind a joke card.
    sender_nickname: str = ""
    # Local receive time, used to report queueing/preparation latency.
    received_at: float = 0.0


_CQ_IMAGE_RE = re.compile(r"\[CQ:image,([^\]]*)\]")


def _is_sticker_segment(data: dict[str, Any]) -> bool:
    """QQ stickers arrive as image segments marked by summary/sub_type."""
    if str(data.get("summary") or "").strip() == "[动画表情]":
        return True
    try:
        return int(data.get("sub_type") or data.get("subType") or 0) == 1
    except (TypeError, ValueError):
        return False


_CQ_REPLY_RE = re.compile(r"\[CQ:reply,([^\]]*)\]")
_MAX_IMAGES = 4

EMPTY_MENTION = "[空艾特]"
EMPTY_REPLY = "[空引用]"


class MessageParser:
    """Turn OneBot message events into MessageEvent, with id dedup."""

    def __init__(self, self_id: int):
        self.self_id = self_id
        self._seen: deque[int] = deque(maxlen=256)
        self._seen_set: set[int] = set()

    def parse(self, event: dict) -> MessageEvent | None:
        if event.get("post_type") != "message":
            return None
        try:
            message_id = int(event.get("message_id") or 0)
            if message_id and message_id in self._seen_set:
                return None
            if message_id:
                if len(self._seen) == self._seen.maxlen and self._seen:
                    self._seen_set.discard(self._seen[0])
                self._seen.append(message_id)
                self._seen_set.add(message_id)

            user_id = int(event.get("user_id") or 0)
            if user_id == self.self_id:
                return None
            text, at_me, images, reply_to, stickers = self._extract_text(
                event.get("message")
            )
            if not text and not images:
                # Bare mentions/quotes carry no text of their own; keep them
                # with an explicit marker so the turn is never a blank line.
                if at_me:
                    text = EMPTY_MENTION
                elif reply_to is not None:
                    text = EMPTY_REPLY
                else:
                    return None
            raw_sender = event.get("sender")
            sender: dict[str, Any] = raw_sender if isinstance(raw_sender, dict) else {}
            sender_name = str(sender.get("card") or sender.get("nickname") or user_id)
            sender_nickname = str(sender.get("nickname") or "")
            group_id = int(event["group_id"]) if event.get("group_id") else None
            message_type = str(event.get("message_type") or ("group" if group_id else "private"))

            return MessageEvent(
                message_type=message_type,
                user_id=user_id,
                message_id=message_id,
                text=text,
                at_me=at_me,
                sender_name=sender_name,
                group_id=group_id,
                images=tuple(images[:_MAX_IMAGES]),
                reply_to=reply_to,
                stickers=tuple(stickers[:_MAX_IMAGES]),
                sender_nickname=sender_nickname,
                received_at=time.time(),
            )
        except (TypeError, ValueError) as exc:
            log.warning("malformed message event: %s", exc)
            return None

    def parse_notice(self, event: dict) -> PokeEvent | None:
        """Recognise poke notices; everything else returns None."""
        if event.get("post_type") != "notice":
            return None
        notice_type = str(event.get("notice_type") or "")
        sub_type = str(event.get("sub_type") or "")
        if notice_type == "notify":
            if sub_type != "poke":
                return None
        elif notice_type not in ("poke", "POKE"):
            return None
        try:
            user_id = int(event.get("user_id") or 0)
            target_id = int(event.get("target_id") or 0)
            group_id = int(event["group_id"]) if event.get("group_id") else None
        except (TypeError, ValueError) as exc:
            log.warning("malformed poke event: %s", exc)
            return None
        if not user_id or not target_id:
            return None
        return PokeEvent(user_id=user_id, target_id=target_id, group_id=group_id)

    def parse_request(self, event: dict) -> RequestEvent | None:
        """Parse OneBot 11 friend/group request events.

        ``request_type=friend`` is a friend request. ``request_type=group``
        with ``sub_type=invite`` means the bot was invited to a group;
        ``sub_type=add`` is someone asking to join a group. NapCat emits the
        standard OneBot 11 shape, so no vendor-specific fields are required.
        """
        if event.get("post_type") != "request":
            return None
        request_type = str(event.get("request_type") or "").strip().lower()
        try:
            user_id = int(event.get("user_id") or 0)
        except (TypeError, ValueError):
            return None
        flag = str(event.get("flag") or "").strip()
        if not user_id or not flag:
            return None
        comment = " ".join(str(event.get("comment") or "").split())[:500]
        raw_sender = event.get("sender")
        sender: dict[str, Any] = raw_sender if isinstance(raw_sender, dict) else {}
        requester_name = " ".join(
            str(
                event.get("nickname")
                or sender.get("nickname")
                or sender.get("card")
                or ""
            ).split()
        )[:100]

        group_id: int | None = None
        group_name = ""
        if request_type in ("friend", "friend_request"):
            kind = "friend"
        elif request_type in ("group", "group_request"):
            try:
                group_id = int(event.get("group_id") or 0)
            except (TypeError, ValueError):
                return None
            if not group_id:
                return None
            sub_type = str(event.get("sub_type") or "add").strip().lower()
            kind = "group_invite" if sub_type == "invite" else "group_add"
            group_name = " ".join(
                str(event.get("group_name") or "").split()
            )[:100]
        else:
            return None

        return RequestEvent(
            kind=kind,
            flag=flag[:200],
            user_id=user_id,
            group_id=group_id,
            group_name=group_name,
            requester_name=requester_name,
            comment=comment,
            received_at=time.time(),
        )

    def _extract_text(
        self, message: Any
    ) -> tuple[str, bool, list[str], int | None, list[str]]:
        at_me = False
        images: list[str] = []
        stickers: list[str] = []
        reply_to: int | None = None
        if isinstance(message, str):
            for match in _CQ_IMAGE_RE.finditer(message):
                params: dict[str, str] = {}
                for item in match.group(1).split(","):
                    key, _, value = item.partition("=")
                    params[key.strip()] = value.strip()
                url = params.get("url") or params.get("file") or ""
                if url.startswith(("http://", "https://", "data:")):
                    images.append(url)
                    if params.get("sub_type", "").strip() == "1":
                        stickers.append(url)
            reply = _CQ_REPLY_RE.search(message)
            if reply:
                for item in reply.group(1).split(","):
                    key, _, value = item.partition("=")
                    if key.strip() == "id" and value.strip().isdigit():
                        reply_to = int(value.strip())
            marker = f"[CQ:at,qq={self.self_id}]"
            at_me = marker in message
            text = message.replace(marker, "")
            while True:
                start = text.find("[CQ:")
                if start < 0:
                    break
                end = text.find("]", start)
                if end < 0:
                    break
                text = text[:start] + text[end + 1:]
            return text.strip(), at_me, images, reply_to, stickers

        if not isinstance(message, list):
            return "", False, [], None, []

        parts: list[str] = []
        for segment in message:
            if not isinstance(segment, dict):
                continue
            kind = segment.get("type")
            raw_data = segment.get("data")
            data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
            if kind == "text":
                parts.append(str(data.get("text", "")))
            elif kind == "at":
                qq = str(data.get("qq", ""))
                if qq == str(self.self_id):
                    at_me = True
                elif qq and qq != "all":
                    parts.append(f"@{data.get('name') or qq}")
            elif kind == "image":
                url = str(data.get("url") or data.get("file") or "")
                sticker = _is_sticker_segment(data)
                if url.startswith(("http://", "https://", "data:")):
                    images.append(url)
                    if sticker:
                        stickers.append(url)
                parts.append("[表情包]" if sticker else "[图片]")
            elif kind == "reply":
                value = str(data.get("id") or "")
                if value.isdigit():
                    reply_to = int(value)
            elif kind == "poke":
                parts.append("[戳一戳]")
            elif kind == "face":
                parts.append("[表情]")
            elif kind == "record":
                parts.append("[语音]")
            elif kind == "video":
                parts.append("[视频]")
            elif kind == "file":
                parts.append("[文件]")
        return "".join(parts).strip(), at_me, images, reply_to, stickers


class OneBotWSClient:
    """Legacy OneBot WebSocket client (kept as a fallback transport)."""

    def __init__(
        self,
        config: OneBotConfig,
        on_message: Callable[[MessageEvent], Awaitable[None]],
        on_notice: Callable[[dict], Awaitable[None]] | None = None,
        on_request: Callable[[dict], Awaitable[None]] | None = None,
    ):
        self.config = config
        self.on_message = on_message
        self.on_notice = on_notice
        self.on_request = on_request
        self._parser = MessageParser(config.self_id)
        self._ws: Any = None
        self._pending: dict[str, asyncio.Future] = {}
        self._echo = 0
        self._closed = False
        self._connected = asyncio.Event()

    async def run(self) -> None:
        backoff = 1.0
        while not self._closed:
            try:
                await self._connect_and_pump()
                backoff = 1.0
                if not self._closed:
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on any transport error
                if self._closed:
                    break
                log.warning("OneBot connection lost: %s; reconnecting in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _connect_and_pump(self) -> None:
        headers = {}
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"
        url = self.config.url
        if self.config.token:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}access_token={quote(self.config.token)}"

        kwargs: dict[str, Any] = {
            "open_timeout": 15,
            "ping_interval": 20,
            "ping_timeout": 20,
            "close_timeout": 5,
            "max_size": 8 * 1024 * 1024,
        }
        try:
            ws = await websockets.connect(url, additional_headers=headers, **kwargs)
        except TypeError:
            ws = await websockets.connect(url, extra_headers=headers, **kwargs)

        self._ws = ws
        self._connected.set()
        log.info("OneBot connected to %s", self.config.url)
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    log.debug("ignoring non-JSON frame: %.200s", raw)
                    continue
                if isinstance(payload, dict) and "echo" in payload:
                    self._resolve(payload)
                elif isinstance(payload, dict):
                    message = self._parser.parse(payload)
                    if message is not None:
                        task = asyncio.create_task(self._run_handler(message))
                        task.add_done_callback(self._log_handler_error)
                    elif self.on_notice is not None and payload.get("post_type") == "notice":
                        task = asyncio.create_task(self._run_notice(payload))
                        task.add_done_callback(self._log_handler_error)
                    elif self.on_request is not None and payload.get("post_type") == "request":
                        task = asyncio.create_task(self._run_request(payload))
                        task.add_done_callback(self._log_handler_error)
        finally:
            self._connected.clear()
            self._ws = None
            self._fail_pending("connection closed")
            log.info(
                "OneBot disconnected (code=%s reason=%s)",
                getattr(ws, "close_code", None),
                getattr(ws, "close_reason", None),
            )

    def _resolve(self, payload: dict) -> None:
        echo = str(payload.get("echo"))
        future = self._pending.pop(echo, None)
        if future is not None and not future.done():
            future.set_result(payload)

    def _fail_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(OneBotError(reason))
                future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        self._pending.clear()

    async def wait_connected(self, timeout: float = 15.0) -> None:
        await asyncio.wait_for(self._connected.wait(), timeout)

    def parse_notice(self, payload: dict) -> PokeEvent | None:
        return self._parser.parse_notice(payload)

    def parse_request(self, payload: dict) -> RequestEvent | None:
        return self._parser.parse_request(payload)

    @request_context
    async def _run_notice(self, payload: dict) -> None:
        if self.on_notice is None:
            return
        try:
            await self.on_notice(payload)
        except Exception:  # noqa: BLE001 - one bad notice must not kill the loop
            log.exception("notice handler failed")

    async def _run_request(self, payload: dict) -> None:
        if self.on_request is None:
            return
        try:
            await self.on_request(payload)
        except Exception:  # noqa: BLE001 - one bad request must not kill the loop
            log.exception("request handler failed")

    @request_context
    async def _run_handler(self, message: MessageEvent) -> None:
        try:
            await self.on_message(message)
        except Exception:  # noqa: BLE001 - one bad message must not kill the loop
            log.exception("message handler failed")

    @staticmethod
    def _log_handler_error(task: asyncio.Task) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            pass

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def call(self, action: str, params: dict, timeout: float = 30.0) -> dict:
        ws = self._ws
        if ws is None:
            raise OneBotError("not connected")
        self._echo += 1
        echo = str(self._echo)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        try:
            await ws.send(json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))
            response = await asyncio.wait_for(future, timeout)
        except TimeoutError as exc:
            raise OneBotError(f"{action} timed out") from exc
        except websockets.exceptions.WebSocketException as exc:
            raise OneBotError(f"{action} connection error: {exc}") from exc
        finally:
            self._pending.pop(echo, None)

        if response.get("retcode") not in (0, None) or response.get("status") == "failed":
            retcode = response.get("retcode")
            if type(retcode) is not int or not -(2**31) <= retcode < 2**31:
                retcode = "unknown"
            raise OneBotRejected(f"{action} failed (retcode={retcode})")
        return response.get("data") or {}

    async def send_private(self, user_id: int, text: str) -> dict:
        return await self.call("send_private_msg", {
            "user_id": user_id,
            "message": [{"type": "text", "data": {"text": text}}],
        })

    async def send_group(self, group_id: int, text: str) -> dict:
        return await self.call("send_group_msg", {
            "group_id": group_id,
            "message": [{"type": "text", "data": {"text": text}}],
        })

    async def send_private_image(self, user_id: int, file: str, timeout: float = 60.0) -> dict:
        """Send an image; `file` is a base64:// URI, http(s) URL or host path."""
        return await self.call("send_private_msg", {
            "user_id": user_id,
            "message": [{"type": "image", "data": {"file": file}}],
        }, timeout=timeout)

    async def send_group_image(self, group_id: int, file: str, timeout: float = 60.0) -> dict:
        return await self.call("send_group_msg", {
            "group_id": group_id,
            "message": [{"type": "image", "data": {"file": file}}],
        }, timeout=timeout)

    async def send_private_record(self, user_id: int, file: str, timeout: float = 60.0) -> dict:
        """Send a voice message; `file` is a base64:// URI (NapCat accepts mp3)."""
        return await self.call("send_private_msg", {
            "user_id": user_id,
            "message": [{"type": "record", "data": {"file": file}}],
        }, timeout=timeout)

    async def send_group_record(self, group_id: int, file: str, timeout: float = 60.0) -> dict:
        return await self.call("send_group_msg", {
            "group_id": group_id,
            "message": [{"type": "record", "data": {"file": file}}],
        }, timeout=timeout)

    async def get_login_info(self) -> dict:
        return await self.call("get_login_info", {})

    async def close(self) -> None:
        self._closed = True
        ws = self._ws
        if ws is not None:
            await ws.close()
