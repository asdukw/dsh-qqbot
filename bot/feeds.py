"""RSS/Atom subscriptions forwarded into chats.

Bilibili blocks this ECS's IP directly (-412), so Bilibili subscriptions go
through a configurable RSSHub mirror; any plain RSS/Atom URL works too. A
poller fetches each feed once per cycle, dedupes by entry id and forwards new
entries to the subscribing chat.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import httpx

log = logging.getLogger(__name__)

DEFAULT_RSSHUB_BASE = "https://rsshub.liumingye.cn"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36 qqbot-feed/1.0"
)
MAX_SEEN = 50
MAX_SUMMARY = 240

# Public RSSHub mirrors answer 503 while their upstream is throttled or a
# browser helper is down; retry these statuses, but fail fast on other 4xx.
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
RETRY_DELAYS = (1.0, 3.0)

_ATOM = "{http://www.w3.org/2005/Atom}"
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


class FeedError(RuntimeError):
    """A feed fetch/parse failure, with the HTTP status when there was one."""

    def __init__(self, message: str, *, status: int | None = None, detail: str = ""):
        super().__init__(message)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class FeedEntry:
    id: str
    title: str
    link: str
    summary: str
    published: str = ""


def strip_html(text: str, limit: int = MAX_SUMMARY) -> str:
    text = _TAG_RE.sub(" ", text or "")
    text = html.unescape(text)
    text = _SPACE_RE.sub(" ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def feed_log_url(url: str) -> str:
    """Keep the route useful for diagnosis without URL credentials or tokens."""
    try:
        parts = urlsplit(url)
        return parts._replace(
            netloc=parts.netloc.rsplit("@", 1)[-1],
            query="[redacted]" if parts.query else "",
            fragment="",
        ).geturl()
    except ValueError:
        return "[invalid feed URL]"


def feed_log_text(text: str, url: str) -> str:
    """Redact URLs and source credentials echoed in upstream error messages."""
    text = html.unescape(text)
    text = text.replace(url, feed_log_url(url))
    try:
        parts = urlsplit(url)
        secrets = {parts.username or "", parts.password or "", parts.fragment}
        secrets.update(value for _, value in parse_qsl(parts.query))
        secrets.update(part.partition("=")[2] for part in parts.query.split("&"))
        secrets.update(unquote(value) for value in secrets.copy())
    except ValueError:
        return "[invalid feed URL]"
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    return re.sub(r"https?://[^\s<>]+", lambda match: feed_log_url(match.group()), text)


def bilibili_feed_url(target: str, base_url: str = DEFAULT_RSSHUB_BASE) -> str:
    """Turn a UID or space URL into an RSSHub dynamics feed URL."""
    target = re.sub(r"(?i)^uid\s*[:：]\s*", "", target.strip())
    if target.startswith("http://") or target.startswith("https://"):
        parts = urlsplit(target)
        if "bilibili.com" not in parts.netloc:
            return target
        match = re.search(r"/(\d+)", parts.path)
        if not match:
            raise FeedError("B 站链接里找不到 UID")
        uid = match.group(1)
    elif target.isdigit():
        uid = target
    else:
        raise FeedError("请给 B 站 UID、空间链接或 RSS 地址")
    return f"{base_url.rstrip('/')}/bilibili/user/dynamic/{uid}"


def _text(node: ET.Element, tag: str) -> str:
    child = node.find(tag)
    return (child.text or "").strip() if child is not None else ""


def parse_feed(data: bytes) -> list[FeedEntry]:
    """Parse RSS 2.0 or Atom into entries (feed order, newest first)."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise FeedError(f"feed 不是合法 XML: {exc}") from exc

    if root.tag == f"{_ATOM}feed" or root.find(f"{_ATOM}entry") is not None:
        entries: list[FeedEntry] = []
        for node in root.findall(f"{_ATOM}entry"):
            link = ""
            for candidate in node.findall(f"{_ATOM}link"):
                if candidate.get("rel") in (None, "alternate"):
                    link = candidate.get("href") or ""
                    break
            entry_id = _text(node, f"{_ATOM}id") or link
            summary = _text(node, f"{_ATOM}summary") or _text(node, f"{_ATOM}content")
            entries.append(
                FeedEntry(
                    id=entry_id,
                    title=strip_html(_text(node, f"{_ATOM}title"), 160),
                    link=link,
                    summary=strip_html(summary),
                    published=_text(node, f"{_ATOM}published")
                    or _text(node, f"{_ATOM}updated"),
                )
            )
        return entries

    entries = []
    for node in root.iter("item"):
        link = _text(node, "link")
        entry_id = _text(node, "guid") or link
        if not entry_id:
            continue
        entries.append(
            FeedEntry(
                id=entry_id,
                title=strip_html(_text(node, "title"), 160),
                link=link,
                summary=strip_html(_text(node, "description")),
                published=_text(node, "pubDate"),
            )
        )
    return entries


def feed_title(data: bytes) -> str:
    """Best-effort feed title for display and /sub list."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return ""
    if root.tag == f"{_ATOM}feed":
        return strip_html(_text(root, f"{_ATOM}title"), 80)
    channel = root.find("channel")
    if channel is not None:
        return strip_html(_text(channel, "title"), 80)
    return ""


def _error_detail(response: httpx.Response, url: str) -> str:
    """Pull RSSHub's "Error Message" out of its HTML error page, else a prefix."""
    body = re.sub(
        r"<(script|style)\b[^>]*>.*?</\1\s*>", " ", response.text or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    body = _SPACE_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", body))).strip()
    match = re.search(
        r"Error Message\s*:\s*(.*?)(?=\s+(?:Full Route|Route|Node Version|Git Hash|Report)\s*:|$)",
        body, re.IGNORECASE,
    )
    detail = match.group(1).strip() if match else ""
    return feed_log_text(detail or body, url)[:300]


async def fetch_feed(
    url: str,
    timeout: float = 25.0,
    user_agent: str = USER_AGENT,
    retries: int = 0,
) -> bytes:
    """Fetch a feed, retrying transient RSSHub/mirror failures.

    A public mirror answers 503 when its upstream is rate-limited (Bilibili
    -412) or its browser helper is down. A bounded retry can recover from a
    transient failure; 404/403 fail on the first attempt.
    """
    attempts = max(1, int(retries) + 1)
    last_error: FeedError | None = None
    for attempt in range(attempts):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
                follow_redirects=True,
                headers={"User-Agent": user_agent, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"},
            ) as client:
                response = await client.get(url)
        except httpx.HTTPError as exc:
            last_error = FeedError(
                f"feed 请求失败: {type(exc).__name__} {exc}".strip()
            )
        else:
            if response.status_code == 200:
                content_type = (response.headers.get("content-type") or "").lower()
                if "html" in content_type:
                    raise FeedError("feed 返回了 HTML（镜像可能不可用或触发了风控）")
                return response.content
            last_error = FeedError(
                f"feed HTTP {response.status_code}",
                status=response.status_code,
                detail=_error_detail(response, url),
            )
            if response.status_code not in RETRY_STATUSES:
                raise last_error
        if attempt + 1 < attempts:
            delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
            log.warning(
                "feed %s attempt %d failed (%s); retrying in %.0fs",
                feed_log_url(url),
                attempt + 1,
                feed_log_text(str(last_error), url),
                delay,
            )
            await asyncio.sleep(delay)
    assert last_error is not None
    raise last_error


def format_entry(title: str, entry: FeedEntry) -> str:
    lines = [f"【{title or '订阅更新'}】"]
    if entry.title:
        lines.append(entry.title)
    if entry.summary:
        lines.append(entry.summary)
    if entry.link:
        lines.append(entry.link)
    return "\n".join(lines)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_type   TEXT NOT NULL CHECK (chat_type IN ('group', 'private')),
    chat_id     INTEGER NOT NULL,
    feed_url    TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'rss',
    seen        TEXT NOT NULL DEFAULT '[]',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  INTEGER NOT NULL,
    last_check  INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_feeds_unique
    ON feeds (chat_type, chat_id, feed_url);
"""


@dataclass(frozen=True)
class FeedRecord:
    id: int
    chat_type: str
    chat_id: int
    feed_url: str
    title: str
    source: str
    seen: tuple[str, ...]
    enabled: bool
    created_at: int
    last_check: int
    last_error: str


def _record(row: sqlite3.Row) -> FeedRecord:
    try:
        seen = json.loads(row["seen"] or "[]")
    except (TypeError, json.JSONDecodeError):
        seen = []
    return FeedRecord(
        id=int(row["id"]),
        chat_type=row["chat_type"],
        chat_id=int(row["chat_id"]),
        feed_url=row["feed_url"],
        title=row["title"] or "",
        source=row["source"] or "rss",
        seen=tuple(str(item) for item in seen) if isinstance(seen, list) else (),
        enabled=bool(row["enabled"]),
        created_at=int(row["created_at"]),
        last_check=int(row["last_check"] or 0),
        last_error=row["last_error"] or "",
    )


class FeedStore:
    def __init__(self, database: str):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(database, timeout=5, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def add(
        self,
        *,
        chat_type: str,
        chat_id: int,
        feed_url: str,
        title: str = "",
        source: str = "rss",
        seen: tuple[str, ...] = (),
    ) -> tuple[FeedRecord, bool]:
        with self._lock, self._db:
            cursor = self._db.execute(
                """
                INSERT INTO feeds
                    (chat_type, chat_id, feed_url, title, source, seen, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_type, chat_id, feed_url) DO NOTHING
                """,
                (
                    chat_type,
                    int(chat_id),
                    feed_url,
                    title[:120],
                    source[:20],
                    json.dumps(list(seen[:MAX_SEEN])),
                    int(time.time()),
                ),
            )
            row = self._db.execute(
                "SELECT * FROM feeds WHERE chat_type = ? AND chat_id = ? AND feed_url = ?",
                (chat_type, int(chat_id), feed_url),
            ).fetchone()
        return _record(row), cursor.rowcount > 0

    def get(self, feed_id: int) -> FeedRecord | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        return _record(row) if row else None

    def list_for(self, chat_type: str, chat_id: int) -> list[FeedRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM feeds WHERE chat_type = ? AND chat_id = ? ORDER BY id",
                (chat_type, int(chat_id)),
            ).fetchall()
        return [_record(row) for row in rows]

    def enabled(self) -> list[FeedRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM feeds WHERE enabled = 1 ORDER BY id"
            ).fetchall()
        return [_record(row) for row in rows]

    def count(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT COUNT(*) FROM feeds").fetchone()
        return int(row[0]) if row else 0

    def delete(self, feed_id: int) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
        return cursor.rowcount > 0

    def mark_check(self, feed_id: int, error: str = "") -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE feeds SET last_check = ?, last_error = ? WHERE id = ?",
                (int(time.time()), error[:200], feed_id),
            )

    def update_seen(self, feed_id: int, ids: list[str]) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE feeds SET seen = ? WHERE id = ?",
                (json.dumps(ids[:MAX_SEEN]), feed_id),
            )

    def close(self) -> None:
        self._db.close()


class FeedPoller:
    """Poll subscribed feeds and forward new entries to their chats."""

    def __init__(
        self,
        store: FeedStore,
        send: Callable[[FeedRecord, FeedEntry], Any],
        *,
        check_seconds: float = 300.0,
        max_new: int = 5,
        fetcher: Callable[[str], Any] | None = None,
    ):
        self.store = store
        self._send = send
        self.check_seconds = max(60.0, float(check_seconds))
        self.max_new = max(1, min(int(max_new), 20))
        self._fetcher = fetcher or fetch_feed
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="qqbot-feeds")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - the loop must survive bad feeds
                log.exception("feed poller tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.check_seconds)
            except TimeoutError:
                continue

    async def tick(self) -> int:
        sent = 0
        by_url: dict[str, list[FeedRecord]] = {}
        for record in self.store.enabled():
            by_url.setdefault(record.feed_url, []).append(record)
        for url, records in by_url.items():
            try:
                data = await self._fetcher(url)
                entries = parse_feed(data)
            except FeedError as exc:
                log.warning("feed %s failed: %s", feed_log_url(url), feed_log_text(str(exc), url))
                for record in records:
                    self.store.mark_check(record.id, str(exc))
                continue
            if not entries:
                continue
            current_ids = [entry.id for entry in entries]
            for record in records:
                seen = set(record.seen)
                if not seen:
                    # First poll: baseline without flooding the chat.
                    self.store.update_seen(record.id, current_ids)
                    self.store.mark_check(record.id)
                    log.info("feed #%s baselined with %d entries", record.id, len(entries))
                    continue
                fresh = [entry for entry in reversed(entries) if entry.id not in seen]
                for entry in fresh[-self.max_new:]:
                    try:
                        await self._send(record, entry)
                    except Exception:  # noqa: BLE001 - keep other entries going
                        log.exception("feed #%s send failed", record.id)
                    else:
                        sent += 1
                self.store.update_seen(
                    record.id, list(dict.fromkeys(current_ids + list(record.seen)))
                )
                self.store.mark_check(record.id)
        return sent
