"""Token usage ledger for main and auxiliary model calls.

Rows are normalized so the admin UI can compare DSH turns with thin-client
calls: ``input_tokens`` is non-cached prompt input, ``cache_read_tokens`` is
the cached prefix, and ``total_tokens`` is their sum plus output.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        INTEGER NOT NULL,
    source            TEXT NOT NULL DEFAULT '',
    purpose           TEXT NOT NULL DEFAULT '',
    conversation_id   TEXT NOT NULL DEFAULT '',
    session_id        TEXT NOT NULL DEFAULT '',
    provider          TEXT NOT NULL DEFAULT '',
    model             TEXT NOT NULL DEFAULT '',
    input_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    turn              INTEGER,
    step              INTEGER,
    dedupe_key        TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_events(created_at);
CREATE INDEX IF NOT EXISTS idx_usage_conversation
    ON usage_events(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_purpose
    ON usage_events(purpose, created_at);
"""


def conversation_label(conversation_id: str) -> str:
    if conversation_id.startswith("group:"):
        return f"群 {conversation_id[6:]}"
    if conversation_id.startswith("private:"):
        return f"好友 {conversation_id[8:]}"
    if conversation_id.startswith("task:"):
        return f"任务 {conversation_id[5:]}"
    return conversation_id or "未知会话"


def _int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _event_get(event: Any, key: str, default: Any = None) -> Any:
    if isinstance(event, dict):
        return event.get(key, default)
    return getattr(event, key, default)


class UsageStore:
    def __init__(
        self, path: str | Path, *, retention_days: int = 90, readonly: bool = False
    ):
        self.path = Path(path)
        self.readonly = readonly
        self.retention_days = max(0, int(retention_days))
        self._lock = threading.Lock()
        if readonly:
            self._db = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True, timeout=5
            )
            self._db.row_factory = sqlite3.Row
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._prune_locked()

    def _prune_locked(self) -> None:
        if not self.retention_days:
            return
        cutoff = int(time.time()) - self.retention_days * 86400
        self._db.execute("DELETE FROM usage_events WHERE created_at < ?", (cutoff,))
        self._db.commit()

    def record(
        self,
        *,
        source: str,
        purpose: str,
        conversation_id: str = "",
        session_id: str = "",
        provider: str = "",
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        created_at: int | None = None,
        turn: int | None = None,
        step: int | None = None,
        dedupe_key: str = "",
    ) -> None:
        input_tokens = _int(input_tokens)
        output_tokens = _int(output_tokens)
        cache_read_tokens = _int(cache_read_tokens)
        total = input_tokens + output_tokens + cache_read_tokens
        if total <= 0:
            return
        stamp = int(created_at if created_at is not None else time.time())
        key = dedupe_key or f"client:{time.time_ns()}:{uuid.uuid4().hex}"
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO usage_events "
                "(created_at, source, purpose, conversation_id, session_id, "
                "provider, model, input_tokens, output_tokens, cache_read_tokens, "
                "total_tokens, turn, step, dedupe_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stamp, str(source), str(purpose), str(conversation_id),
                    str(session_id), str(provider), str(model), input_tokens,
                    output_tokens, cache_read_tokens, total, turn, step, key,
                ),
            )
            self._db.commit()

    def record_client(
        self,
        purpose: str,
        conversation_id: str,
        usage: dict[str, Any],
        *,
        provider: str = "",
        model: str = "",
    ) -> None:
        """Normalize OpenAI-style or DSH-style usage from a thin-client call."""
        usage = usage or {}
        prompt = _int(usage.get("prompt_tokens") or usage.get("input_tokens"))
        cached = 0
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            cached = _int(details.get("cached_tokens"))
        if not cached:
            cached = _int(
                usage.get("prompt_cache_hit_tokens")
                or usage.get("cache_read_input_tokens")
                or usage.get("cache_read_tokens")
            )
        completion = _int(usage.get("completion_tokens") or usage.get("output_tokens"))
        if "prompt_tokens" in usage:
            non_cached = max(0, prompt - cached)
        else:
            non_cached = prompt
        self.record(
            source="thin",
            purpose=purpose,
            conversation_id=conversation_id,
            provider=provider,
            model=model,
            input_tokens=non_cached,
            output_tokens=completion,
            cache_read_tokens=cached,
            created_at=int(time.time()),
        )

    def record_dsh(
        self,
        conversation_id: str,
        session_id: str,
        purpose: str,
        events: Any,
    ) -> None:
        """Record assistant-step and compaction usage from one DSH turn."""
        for event in events or []:
            event_type = _event_get(event, "type")
            data = _event_get(event, "data") or {}
            if not isinstance(data, dict):
                continue
            usage = data.get("usage")
            if not isinstance(usage, dict) or not usage:
                continue
            stamp = int((_event_get(event, "time") or 0) / 1000) or int(time.time())
            seq = _event_get(event, "seq")
            if event_type == "assistant/message":
                message = data.get("message") or {}
                source = message.get("source") or {}
                key = f"dsh:{session_id}:{seq}:assistant"
                self.record(
                    source="dsh",
                    purpose=purpose,
                    conversation_id=conversation_id,
                    session_id=session_id,
                    provider=str(source.get("provider") or ""),
                    model=str(source.get("model") or ""),
                    input_tokens=_int(usage.get("inputTokens")),
                    output_tokens=_int(usage.get("outputTokens")),
                    cache_read_tokens=_int(usage.get("cacheReadTokens")),
                    created_at=stamp,
                    turn=data.get("turn"),
                    step=data.get("step"),
                    dedupe_key=key,
                )
            elif event_type == "compaction/summary":
                compaction_id = str(data.get("compactionId") or seq)
                key = f"dsh:{session_id}:{compaction_id}:compaction"
                self.record(
                    source="dsh",
                    purpose="compaction",
                    conversation_id=conversation_id,
                    session_id=session_id,
                    provider=str(data.get("provider") or ""),
                    model=str(data.get("model") or ""),
                    input_tokens=_int(usage.get("inputTokens")),
                    output_tokens=_int(usage.get("outputTokens")),
                    cache_read_tokens=_int(usage.get("cacheReadTokens")),
                    created_at=stamp,
                    turn=data.get("turn"),
                    dedupe_key=key,
                )

    def backfill_dsh(self, sessions_root: str | Path) -> int:
        """Import usage already present in DSH JSONL files.

        Existing installs predate the ledger. Replaying assistant-step and
        compaction usage is idempotent because record_dsh dedupes by event seq.
        """
        root = Path(sessions_root)
        if not root.is_dir():
            return 0
        processed = 0
        for path in root.rglob("session.v4.jsonl"):
            session_id = path.parent.name
            match = re.fullmatch(r"(group|private)-(\d+)(?:-g\d+)?", session_id)
            if match is not None:
                conversation_id = f"{match.group(1)}:{match.group(2)}"
                purpose = "chat"
            elif session_id.startswith("task-"):
                conversation_id = f"task:{session_id[5:]}"
                purpose = "task_generate"
            else:
                continue
            try:
                with path.open(encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            event = json.loads(line)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            continue
                        if not isinstance(event, dict):
                            continue
                        if event.get("type") not in (
                            "assistant/message", "compaction/summary"
                        ):
                            continue
                        before = self._db.total_changes
                        self.record_dsh(
                            conversation_id, session_id, purpose, [event]
                        )
                        if self._db.total_changes > before:
                            processed += 1
            except OSError:
                continue
        return processed

    def summary(self, days: int = 7, conversation_id: str = "") -> dict[str, Any]:
        days = max(1, min(int(days), 90))
        cutoff = int(time.time()) - days * 86400
        where = "created_at >= ?"
        params: list[Any] = [cutoff]
        if conversation_id:
            where += " AND conversation_id = ?"
            params.append(conversation_id)
        with self._lock:
            totals = self._db.execute(
                "SELECT COALESCE(SUM(input_tokens),0), "
                "COALESCE(SUM(cache_read_tokens),0), "
                "COALESCE(SUM(output_tokens),0), COUNT(*) "
                f"FROM usage_events WHERE {where}",
                params,
            ).fetchone()
            by_day = self._db.execute(
                "SELECT date(created_at, 'unixepoch', '+8 hours') AS day, "
                "COALESCE(SUM(input_tokens),0), COALESCE(SUM(cache_read_tokens),0), "
                "COALESCE(SUM(output_tokens),0), COUNT(*) "
                f"FROM usage_events WHERE {where} "
                "GROUP BY day ORDER BY day",
                params,
            ).fetchall()
            by_purpose = self._db.execute(
                "SELECT purpose, COALESCE(SUM(input_tokens),0), "
                "COALESCE(SUM(cache_read_tokens),0), COALESCE(SUM(output_tokens),0), COUNT(*) "
                f"FROM usage_events WHERE {where} "
                "GROUP BY purpose ORDER BY 2 + 3 + 4 DESC",
                params,
            ).fetchall()
            by_session = self._db.execute(
                "SELECT conversation_id, session_id, COALESCE(SUM(input_tokens),0), "
                "COALESCE(SUM(cache_read_tokens),0), COALESCE(SUM(output_tokens),0), COUNT(*) "
                f"FROM usage_events WHERE {where} "
                "GROUP BY conversation_id, session_id "
                "ORDER BY 3 + 4 + 5 DESC LIMIT 20",
                params,
            ).fetchall()
            conversations = self._db.execute(
                "SELECT conversation_id, COALESCE(SUM(input_tokens),0), "
                "COALESCE(SUM(cache_read_tokens),0), COALESCE(SUM(output_tokens),0) "
                "FROM usage_events WHERE created_at >= ? "
                "GROUP BY conversation_id ORDER BY 2 + 3 + 4 DESC LIMIT 50",
                (cutoff,),
            ).fetchall()
            recent = self._db.execute(
                "SELECT created_at, source, purpose, conversation_id, session_id, "
                "provider, model, input_tokens, cache_read_tokens, output_tokens "
                f"FROM usage_events WHERE {where} ORDER BY id DESC LIMIT 20",
                params,
            ).fetchall()

        input_tokens = int(totals[0] or 0)
        cache_tokens = int(totals[1] or 0)
        output_tokens = int(totals[2] or 0)
        prompt_total = input_tokens + cache_tokens
        cache_hit_rate = cache_tokens / prompt_total if prompt_total else 0.0

        return {
            "days": days,
            "conversation_id": conversation_id,
            "totals": {
                "input": input_tokens,
                "cache_read": cache_tokens,
                "output": output_tokens,
                "total": input_tokens + cache_tokens + output_tokens,
                "events": int(totals[3] or 0),
                "cache_hit_rate": round(cache_hit_rate, 4),
            },
            "by_day": [
                {
                    "day": str(row[0]),
                    "input": int(row[1]),
                    "cache_read": int(row[2]),
                    "output": int(row[3]),
                    "total": int(row[1]) + int(row[2]) + int(row[3]),
                    "events": int(row[4]),
                }
                for row in by_day
            ],
            "by_purpose": [
                {
                    "purpose": str(row[0]),
                    "input": int(row[1]),
                    "cache_read": int(row[2]),
                    "output": int(row[3]),
                    "total": int(row[1]) + int(row[2]) + int(row[3]),
                    "events": int(row[4]),
                }
                for row in by_purpose
            ],
            "by_session": [
                {
                    "conversation_id": str(row[0]),
                    "session_id": str(row[1]),
                    "input": int(row[2]),
                    "cache_read": int(row[3]),
                    "output": int(row[4]),
                    "total": int(row[2]) + int(row[3]) + int(row[4]),
                    "events": int(row[5]),
                }
                for row in by_session
            ],
            "conversations": [
                {
                    "id": str(row[0]),
                    "label": conversation_label(str(row[0])),
                    "input": int(row[1]),
                    "cache_read": int(row[2]),
                    "output": int(row[3]),
                    "total": int(row[1]) + int(row[2]) + int(row[3]),
                }
                for row in conversations
            ],
            "recent": [
                {
                    "created_at": int(row[0]),
                    "source": str(row[1]),
                    "purpose": str(row[2]),
                    "conversation_id": str(row[3]),
                    "session_id": str(row[4]),
                    "provider": str(row[5]),
                    "model": str(row[6]),
                    "input": int(row[7]),
                    "cache_read": int(row[8]),
                    "output": int(row[9]),
                    "total": int(row[7]) + int(row[8]) + int(row[9]),
                }
                for row in recent
            ],
        }

    def close(self) -> None:
        with self._lock:
            self._db.close()
