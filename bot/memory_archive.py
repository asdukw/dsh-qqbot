"""Long-term memory consolidation for QQ conversations.

Raw messages stay in ``memory.db`` (and the DSH session JSONL files). This
module adds two consolidated layers on top:

* ``memory_days`` — one dated summary per conversation, so the bot can answer
  "what did we talk about a month ago" without replaying raw logs.
* ``memory_items`` — user/group semantic memories with provenance, importance,
  visibility and recency-aware invalidation.

Extraction runs asynchronously when a DSH session generation rotates and as a
daily fallback. Retrieval is exposed to the model through the ``memory-archive``
DSH plugin; this module only provides the storage/query primitives.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Shanghai")
MAX_DAY_SUMMARY = 2000
MAX_INPUT_CHARS = 12000
MAX_ITEMS_PER_RUN = 40
MAX_TRANSCRIPT_MESSAGES = 200

MEMORY_SYSTEM = (
    "你是长期记忆整理器。只根据聊天记录提取明确出现的信息，不推测、不补全。"
    "输出必须是一个 JSON 对象，不要代码块或解释。"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_days (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    day             TEXT NOT NULL,
    summary         TEXT NOT NULL,
    topics          TEXT NOT NULL DEFAULT '[]',
    open_loops      TEXT NOT NULL DEFAULT '[]',
    message_count   INTEGER NOT NULL DEFAULT 0,
    session_ids     TEXT NOT NULL DEFAULT '[]',
    generation      INTEGER NOT NULL DEFAULT 0,
    model           TEXT NOT NULL DEFAULT '',
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    UNIQUE(conversation_id, day)
);
CREATE INDEX IF NOT EXISTS idx_memory_days_conversation
    ON memory_days (conversation_id, day DESC);
CREATE TABLE IF NOT EXISTS memory_items (
    id               TEXT PRIMARY KEY,
    scope            TEXT NOT NULL CHECK (scope IN ('user','group')),
    conversation_id  TEXT NOT NULL DEFAULT '',
    group_id         TEXT NOT NULL DEFAULT '',
    user_id          TEXT NOT NULL DEFAULT '',
    kind             TEXT NOT NULL DEFAULT 'fact',
    key              TEXT NOT NULL,
    value            TEXT NOT NULL,
    importance       INTEGER NOT NULL DEFAULT 3,
    confidence       REAL NOT NULL DEFAULT 1.0,
    visibility       TEXT NOT NULL DEFAULT 'private'
                     CHECK (visibility IN ('private','public')),
    occurred_at      INTEGER NOT NULL,
    source_session   TEXT NOT NULL DEFAULT '',
    source_message_ids TEXT NOT NULL DEFAULT '[]',
    supersedes_id    TEXT NOT NULL DEFAULT '',
    invalid_at       INTEGER,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_item_current
    ON memory_items (scope, group_id, user_id, key) WHERE invalid_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_memory_items_user
    ON memory_items (user_id, occurred_at DESC);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    id UNINDEXED,
    text,
    tokenize='trigram'
);
"""


@dataclass(frozen=True)
class MemoryArchiveSettings:
    enabled: bool = True
    database: str = ""
    archive_on_rotation: bool = True
    daily_hour: int = 4
    check_seconds: int = 300
    backfill_days: int = 30
    retention_days: int = 365
    sample_messages: int = 200
    max_input_chars: int = MAX_INPUT_CHARS
    max_output_tokens: int = 1600


def day_of(stamp: int | float) -> str:
    return datetime.fromtimestamp(int(stamp), TZ).date().isoformat()


def day_bounds(day: str) -> tuple[int, int]:
    start = datetime.fromisoformat(day).replace(tzinfo=TZ)
    start_ts = int(start.timestamp())
    # Asia/Shanghai has no DST; avoid datetime overflow for the 9999 sentinel.
    return start_ts, start_ts + 86400


def _terms(query: str) -> list[str]:
    """Use the same CJK-friendly trigram sampling as the chat recall index."""
    terms: list[str] = []
    for chunk in re.findall(r"[一-鿿]+|[A-Za-z0-9]{2,}", query or ""):
        if len(chunk) <= 2:
            continue
        step = 1 if len(chunk) <= 12 else 2
        for index in range(0, len(chunk) - 2, step):
            term = chunk[index:index + 3]
            if term not in terms:
                terms.append(term)
            if len(terms) >= 8:
                return terms
    return terms


def _clean_summary(value: Any, limit: int = MAX_DAY_SUMMARY) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _string_list(value: Any, limit: int = 12, size: int = 80) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = _clean_summary(item, size)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _json_object(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("memory extractor returned no JSON object")
    value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("memory extractor returned a non-object")
    return value


class MemoryArchive:
    def __init__(
        self,
        path: str | Path,
        settings: MemoryArchiveSettings | None = None,
        *,
        generate: Callable[[str, str], Awaitable[str]] | None = None,
    ):
        self.settings = settings or MemoryArchiveSettings()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._generate = generate
        self._closed = False

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._db.close()

    # ── extraction ───────────────────────────────────────────────

    def _transcript(
        self,
        conversation_id: str,
        start_at: int,
        end_at: int,
        *,
        limit: int = MAX_TRANSCRIPT_MESSAGES,
        max_chars: int = MAX_INPUT_CHARS,
    ) -> tuple[str, set[str], int]:
        """Return (transcript, known user ids, message count) for a time window."""
        with self._lock:
            events = self._db.execute(
                "SELECT created_at, user_id, sender_name, text FROM chat_events "
                "WHERE conversation_id=? AND created_at>=? AND created_at<? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (conversation_id, start_at, end_at, limit),
            ).fetchall()
            replies = self._db.execute(
                "SELECT created_at, content FROM messages "
                "WHERE conversation_id=? AND created_at>=? AND created_at<? "
                "AND role='assistant' ORDER BY created_at DESC, id DESC LIMIT ?",
                (conversation_id, start_at, end_at, limit),
            ).fetchall()
        rows: list[tuple[int, str, str]] = []
        users: set[str] = set()
        for row in events:
            stamp = int(row["created_at"])
            user_id = str(row["user_id"])
            users.add(user_id)
            name = str(row["sender_name"] or "")[:24]
            text = " ".join(str(row["text"] or "").split())[:700]
            if text:
                rows.append((stamp, user_id, f"[{datetime.fromtimestamp(stamp, TZ):%Y-%m-%d %H:%M}] QQ{user_id} {name}: {text}"))
        for row in replies:
            stamp = int(row["created_at"])
            text = " ".join(str(row["content"] or "").split())[:700]
            if text:
                rows.append((stamp, "", f"[{datetime.fromtimestamp(stamp, TZ):%Y-%m-%d %H:%M}] 机器人: {text}"))
        rows.sort(key=lambda item: item[0])
        lines = [item[2] for item in rows]
        while len(lines) > 1 and sum(len(line) + 1 for line in lines) > max(500, max_chars):
            lines.pop(0)
        return "\n".join(lines), users, len(rows)

    @staticmethod
    def _prompt(conversation_id: str, transcript: str, *, is_group: bool, group_id: str) -> str:
        scope = f"群聊 group:{group_id}" if is_group else "私聊"
        return (
            f"{MEMORY_SYSTEM}\n\n会话：{conversation_id}（{scope}）\n"
            "任务：整理这段时间的聊天，输出 JSON，字段如下：\n"
            '{"summary":"150-300字，按时间顺序概括发生了什么、重要决定、情绪和未完成的事",'
            '"topics":["主题"],"open_loops":["未完成/约定的事"],'
            '"user_facts":[{"user_id":"聊天记录里的QQ号","name":"昵称","key":"简短稳定键",'
            '"value":"事实","kind":"fact|preference|event|relationship|plan","importance":1-5,'
            '"visibility":"private|public"}],'
            '"group_facts":[{"key":"简短稳定键","value":"群信息","importance":1-5}]}\n'
            "规则：只写记录中明确出现的事实；user_id 必须来自记录；私聊事实一律 private；"
            "群聊用户事实一律 public 且只属于本群；group_facts 只用于群聊；不要复述整段聊天；"
            "没有内容就给空数组。\n\n聊天记录：\n"
            f"{transcript}"
        )

    def _store_extraction(
        self,
        conversation_id: str,
        day: str,
        result: dict[str, Any],
        *,
        known_users: set[str],
        message_count: int,
        session_id: str,
        generation: int,
        is_group: bool,
        group_id: str,
        now: int,
    ) -> None:
        summary = _clean_summary(result.get("summary"))
        topics = _string_list(result.get("topics"))
        loops = _string_list(result.get("open_loops"))
        if not summary and not topics:
            return
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT * FROM memory_days WHERE conversation_id=? AND day=?",
                (conversation_id, day),
            ).fetchone()
            sessions = json.loads(row["session_ids"]) if row else []
            if session_id and session_id in sessions:
                return
            if row is None:
                day_id = uuid.uuid4().hex
                self._db.execute(
                    "INSERT INTO memory_days "
                    "(id,conversation_id,day,summary,topics,open_loops,message_count,"
                    "session_ids,generation,model,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,'',?,?)",
                    (
                        day_id, conversation_id, day, summary,
                        json.dumps(topics, ensure_ascii=False),
                        json.dumps(loops, ensure_ascii=False),
                        message_count,
                        json.dumps([session_id] if session_id else [], ensure_ascii=False),
                        generation, now, now,
                    ),
                )
                day_text = " ".join([summary, *topics, *loops])
            else:
                merged = row["summary"]
                if summary and summary not in merged:
                    merged = (merged + "\n\n" + summary)[-MAX_DAY_SUMMARY:]
                merged_topics = _string_list(json.loads(row["topics"]) + topics, limit=20)
                merged_loops = _string_list(json.loads(row["open_loops"]) + loops, limit=20)
                sessions.append(session_id)
                self._db.execute(
                    "UPDATE memory_days SET summary=?, topics=?, open_loops=?, "
                    "message_count=message_count+?, session_ids=?, generation=?, updated_at=? "
                    "WHERE id=?",
                    (
                        merged, json.dumps(merged_topics, ensure_ascii=False),
                        json.dumps(merged_loops, ensure_ascii=False),
                        message_count, json.dumps(sessions, ensure_ascii=False),
                        generation, now, row["id"],
                    ),
                )
                day_id = row["id"]
                day_text = " ".join([merged, *merged_topics, *merged_loops])
            self._fts_set(f"day:{day_id}", day_text)
            for item in (result.get("user_facts") or [])[:20]:
                if not isinstance(item, dict):
                    continue
                user_id = str(item.get("user_id") or "").strip()
                key = _clean_summary(item.get("key"), 60)
                value = _clean_summary(item.get("value"), 400)
                if not user_id or not key or not value:
                    continue
                if not is_group and conversation_id.startswith("private:"):
                    user_id = conversation_id.split(":", 1)[1]
                if user_id not in known_users and not (
                    not is_group and conversation_id.startswith("private:")
                ):
                    continue
                visibility = "public" if is_group else "private"
                self._write_item(
                    scope="user",
                    conversation_id=conversation_id,
                    group_id=group_id if is_group else "",
                    user_id=user_id,
                    kind=_clean_summary(item.get("kind"), 24) or "fact",
                    key=key,
                    value=value,
                    importance=_importance(item.get("importance")),
                    visibility=visibility,
                    occurred_at=now,
                    source_session=session_id,
                )
            if is_group:
                for item in (result.get("group_facts") or [])[:8]:
                    if not isinstance(item, dict):
                        continue
                    key = _clean_summary(item.get("key"), 60)
                    value = _clean_summary(item.get("value"), 400)
                    if key and value:
                        self._write_item(
                            scope="group",
                            conversation_id=conversation_id,
                            group_id=group_id,
                            user_id="",
                            kind="group_fact",
                            key=key,
                            value=value,
                            importance=_importance(item.get("importance")),
                            visibility="public",
                            occurred_at=now,
                            source_session=session_id,
                        )

    def _fts_set(self, key: str, text: str) -> None:
        self._db.execute("DELETE FROM memory_fts WHERE id=?", (key,))
        if text.strip():
            self._db.execute("INSERT INTO memory_fts (id, text) VALUES (?, ?)", (key, text))

    def _write_item(
        self,
        *,
        scope: str,
        conversation_id: str,
        group_id: str,
        user_id: str,
        kind: str,
        key: str,
        value: str,
        importance: int,
        visibility: str,
        occurred_at: int,
        source_session: str,
    ) -> None:
        now = int(time.time())
        row = self._db.execute(
            "SELECT id,value FROM memory_items WHERE scope=? AND group_id=? "
            "AND user_id=? AND key=? AND invalid_at IS NULL",
            (scope, group_id, user_id, key),
        ).fetchone()
        if row is not None and str(row["value"]) == value:
            self._db.execute(
                "UPDATE memory_items SET importance=?, occurred_at=?, "
                "source_session=?, updated_at=? WHERE id=?",
                (importance, occurred_at, source_session, now, row["id"]),
            )
            self._fts_set(f"item:{row['id']}", f"{key}: {value}")
            return
        if row is not None:
            self._db.execute(
                "UPDATE memory_items SET invalid_at=?, updated_at=? WHERE id=?",
                (now, now, row["id"]),
            )
        item_id = uuid.uuid4().hex
        self._db.execute(
            "INSERT INTO memory_items "
            "(id,scope,conversation_id,group_id,user_id,kind,key,value,importance,"
            "confidence,visibility,occurred_at,source_session,source_message_ids,"
            "supersedes_id,invalid_at,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,1.0,?,?,?, '', ?,NULL,?,?)",
            (
                item_id, scope, conversation_id, group_id, user_id, kind, key,
                value, importance, visibility, occurred_at, source_session,
                row["id"] if row is not None else "", now, now,
            ),
        )
        self._fts_set(f"item:{item_id}", f"{key}: {value}")

    async def archive_window(
        self,
        conversation_id: str,
        start_at: int,
        end_at: int,
        *,
        session_id: str = "",
        generation: int = 0,
        skip_existing_day: bool = False,
        day: str | None = None,
    ) -> bool:
        if not self.settings.enabled or self._generate is None:
            return False
        day = day or day_of(end_at)
        if skip_existing_day:
            with self._lock:
                exists = self._db.execute(
                    "SELECT 1 FROM memory_days WHERE conversation_id=? AND day=?",
                    (conversation_id, day),
                ).fetchone()
            if exists:
                return False
        if session_id:
            with self._lock:
                row = self._db.execute(
                    "SELECT session_ids FROM memory_days WHERE conversation_id=? AND day=?",
                    (conversation_id, day),
                ).fetchone()
            if row is not None and session_id in json.loads(row["session_ids"]):
                return False
        transcript, users, count = self._transcript(
            conversation_id, start_at, end_at,
            limit=self.settings.sample_messages,
            max_chars=self.settings.max_input_chars,
        )
        if not transcript.strip():
            return False
        is_group = conversation_id.startswith("group:")
        group_id = conversation_id.split(":", 1)[1] if is_group else ""
        prompt = self._prompt(conversation_id, transcript, is_group=is_group, group_id=group_id)
        try:
            raw = await self._generate(conversation_id, prompt)
            result = _json_object(raw)
        except Exception as exc:  # noqa: BLE001 - extraction must not break chat
            log.warning("memory extraction failed for %s: %s", conversation_id, exc)
            return False
        try:
            self._store_extraction(
                conversation_id, day, result,
                known_users=users,
                message_count=count,
                session_id=session_id,
                generation=generation,
                is_group=is_group,
                group_id=group_id,
                now=int(end_at),
            )
        except (sqlite3.Error, ValueError, TypeError) as exc:
            log.warning("memory store failed for %s: %s", conversation_id, exc)
            return False
        return True

    async def archive_day(self, conversation_id: str, day: str) -> bool:
        start, end = day_bounds(day)
        return await self.archive_window(
            conversation_id, start, end,
            session_id=f"day:{day}", skip_existing_day=True, day=day,
        )

    def active_conversations(self, day: str) -> list[str]:
        start, end = day_bounds(day)
        with self._lock:
            rows = self._db.execute(
                "SELECT DISTINCT conversation_id FROM chat_events "
                "WHERE created_at>=? AND created_at<?",
                (start, end),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def missing_days(self, days: int) -> list[tuple[str, str]]:
        """(conversation_id, day) pairs without a daily summary."""
        days = max(0, min(90, int(days)))
        today = datetime.now(TZ).date()
        result: list[tuple[str, str]] = []
        for offset in range(1, days + 1):
            day = (today - timedelta(days=offset)).isoformat()
            for conversation_id in self.active_conversations(day):
                with self._lock:
                    row = self._db.execute(
                        "SELECT 1 FROM memory_days WHERE conversation_id=? AND day=?",
                        (conversation_id, day),
                    ).fetchone()
                if row is None:
                    result.append((conversation_id, day))
        return result

    # ── retrieval ─────────────────────────────────────────────────

    def _matching_ids(self, query: str) -> set[str]:
        terms = _terms(query)
        with self._lock:
            if not terms:
                rows = self._db.execute(
                    "SELECT id FROM memory_fts WHERE text LIKE ? LIMIT 200",
                    (f"%{query.strip()}%",),
                ).fetchall()
                return {str(row[0]) for row in rows}
            match = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
            try:
                rows = self._db.execute(
                    "SELECT id FROM memory_fts WHERE memory_fts MATCH ? LIMIT 200",
                    (match,),
                ).fetchall()
                return {str(row[0]) for row in rows}
            except sqlite3.Error:
                rows = self._db.execute(
                    "SELECT id FROM memory_fts WHERE text LIKE ? LIMIT 200",
                    (f"%{query.strip()}%",),
                ).fetchall()
                return {str(row[0]) for row in rows}

    def search(
        self,
        conversation_id: str,
        *,
        query: str = "",
        user_id: str = "",
        is_group: bool = False,
        group_id: str = "",
        since: str = "",
        until: str = "",
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(20, int(limit)))
        since = since or "1970-01-01"
        until = until or "9999-12-31"
        try:
            start_ts, _ = day_bounds(since)
            _, end_ts = day_bounds(until)
        except ValueError:
            start_ts, end_ts = 0, 2**62
        matched = self._matching_ids(query) if query.strip() else None
        rows: list[dict[str, Any]] = []
        with self._lock:
            days = self._db.execute(
                "SELECT * FROM memory_days WHERE conversation_id=? AND day>=? AND day<=? "
                "ORDER BY day DESC LIMIT 60",
                (conversation_id, since, until),
            ).fetchall()
            if is_group:
                items = self._db.execute(
                    "SELECT * FROM memory_items WHERE invalid_at IS NULL AND ("
                    "(scope='group' AND group_id=?) OR "
                    "(scope='user' AND visibility='public' AND group_id IN ('',?))) "
                    "ORDER BY occurred_at DESC LIMIT 80",
                    (group_id, group_id),
                ).fetchall()
            else:
                items = self._db.execute(
                    "SELECT * FROM memory_items WHERE invalid_at IS NULL AND "
                    "scope='user' AND user_id=? AND group_id='' "
                    "ORDER BY occurred_at DESC LIMIT 80",
                    (user_id,),
                ).fetchall()
        for row in days:
            if matched is not None and f"day:{row['id']}" not in matched:
                continue
            rows.append({
                "kind": "day", "id": row["id"], "date": row["day"],
                "summary": row["summary"],
                "topics": json.loads(row["topics"]),
                "open_loops": json.loads(row["open_loops"]),
                "importance": 3,
            })
        for row in items:
            occurred = int(row["occurred_at"])
            if occurred < start_ts or occurred > end_ts:
                continue
            if matched is not None and f"item:{row['id']}" not in matched:
                continue
            rows.append({
                "kind": "item", "id": row["id"],
                "date": datetime.fromtimestamp(int(row["occurred_at"]), TZ).date().isoformat(),
                "key": row["key"], "value": row["value"],
                "scope": row["scope"], "kind_name": row["kind"],
                "importance": int(row["importance"]),
            })
        rows.sort(key=lambda item: (item["importance"], item["date"]), reverse=True)
        return rows[:limit]

    def timeline(
        self,
        conversation_id: str,
        *,
        since: str = "",
        until: str = "",
        limit: int = 14,
    ) -> list[dict[str, Any]]:
        return [
            row for row in self.search(
                conversation_id, since=since, until=until, limit=limit + 20
            ) if row["kind"] == "day"
        ][:max(1, min(30, limit))]

    def user_items(
        self,
        user_id: str,
        *,
        group_id: str = "",
        is_group: bool = False,
        query: str = "",
        since: str = "",
        until: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return [
            row for row in self.search(
                f"group:{group_id}" if is_group else f"private:{user_id}",
                query=query, user_id=user_id, is_group=is_group,
                group_id=group_id, since=since, until=until, limit=limit + 20,
            ) if row["kind"] == "item" and row.get("scope") == "user"
        ][:max(1, min(20, limit))]

    def recent_context(
        self,
        conversation_id: str,
        *,
        user_id: str,
        is_group: bool,
        group_id: str,
        days: int = 7,
    ) -> str:
        since = (datetime.now(TZ).date() - timedelta(days=max(1, days))).isoformat()
        entries = self.search(
            conversation_id, user_id=user_id, is_group=is_group,
            group_id=group_id, since=since, limit=8,
        )
        if not entries:
            return ""
        lines = [f"近 {days} 天记忆（按需使用，不要生硬复述）："]
        for entry in entries:
            if entry["kind"] == "day":
                lines.append(f"- {entry['date']}：{entry['summary']}")
            else:
                lines.append(f"- {entry['date']} {entry['key']}：{entry['value']}")
        return "\n".join(lines)


def _importance(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 3
    return max(1, min(5, number))
