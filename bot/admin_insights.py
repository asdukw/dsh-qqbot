"""Read-only analytics for the admin UI: token ledger, rankings, word cloud."""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from usage import UsageStore, conversation_label

try:
    import jieba
except ImportError:  # pragma: no cover - optional word segmenter
    jieba = None

TZ = ZoneInfo("Asia/Shanghai")
STOPWORDS = {
    "这个", "那个", "什么", "怎么", "就是", "还是", "然后", "因为", "所以",
    "但是", "如果", "已经", "可以", "没有", "不是", "我们", "你们", "他们",
    "她们", "自己", "现在", "今天", "刚才", "真的", "感觉", "一个", "一下",
    "有点", "可能", "应该", "知道", "觉得", "时候", "东西", "这里", "那里",
    "这样", "那样", "这么", "那么", "哈哈", "哈哈哈", "图片", "表情包",
    "语音", "视频", "文件", "消息", "不会", "还有", "只是", "而且",
}
WORD_RE = re.compile(r"[一-鿿]{2,6}|[A-Za-z]{2,}")


def clean_conversation(value: Any) -> str:
    value = str(value or "").strip()[:160]
    if value.startswith(("group:", "private:", "task:")):
        return value
    return ""


def empty_usage(days: int, conversation_id: str) -> dict[str, Any]:
    return {
        "days": days,
        "conversation_id": conversation_id,
        "totals": {
            "input": 0, "cache_read": 0, "output": 0,
            "total": 0, "events": 0, "cache_hit_rate": 0.0,
        },
        "by_day": [],
        "by_purpose": [],
        "by_session": [],
        "conversations": [],
        "recent": [],
    }


def usage_data(
    config: dict[str, Any], days: int = 7, conversation_id: str = ""
) -> dict[str, Any]:
    days = max(1, min(int(days), 90))
    path = str(
        (config.get("usage") or {}).get("database", "/var/lib/qqbot/usage.db")
    )
    if not Path(path).is_file():
        return empty_usage(days, conversation_id)
    try:
        store = UsageStore(path, readonly=True)
        try:
            return store.summary(days, conversation_id)
        finally:
            store.close()
    except (OSError, sqlite3.Error):
        return empty_usage(days, conversation_id)


def parse_day(value: Any) -> datetime | None:
    """Parse a YYYY-MM-DD parameter as midnight in Asia/Shanghai."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=TZ)
    except ValueError:
        return None


def _range_days(start_day: datetime, end_day: datetime) -> int:
    return (end_day.date() - start_day.date()).days + 1


def empty_fun(
    conversation_id: str,
    start_day: datetime | None = None,
    end_day: datetime | None = None,
) -> dict[str, Any]:
    now = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    start_day = start_day or now
    end_day = end_day or now
    return {
        "date": end_day.strftime("%Y-%m-%d"),
        "start": start_day.strftime("%Y-%m-%d"),
        "end": end_day.strftime("%Y-%m-%d"),
        "days": _range_days(start_day, end_day),
        "conversation_id": conversation_id,
        "messages": 0,
        "ranking": [],
        "words": [],
        "conversations": [],
        "conversation_total": 0,
    }


def _tokens(text: str) -> list[str]:
    if jieba is not None:
        return list(jieba.cut(text))
    return WORD_RE.findall(text)


def fun_data(
    config: dict[str, Any],
    conversation_id: str = "",
    start: Any = None,
    end: Any = None,
) -> dict[str, Any]:
    """Message ranking and word cloud for an inclusive Asia/Shanghai date range."""
    now = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    start_day = parse_day(start) or now
    end_day = parse_day(end) or now
    # There is no data in the future, and adding a day to 9999-12-31 would
    # overflow datetime.max.
    if end_day > now:
        end_day = now
    if start_day > now:
        start_day = now
    if end_day < start_day:
        start_day, end_day = end_day, start_day
    start_ts = int(start_day.timestamp())
    end_ts = int((end_day + timedelta(days=1)).timestamp())
    path = str(
        (config.get("paths") or {}).get("database", "/var/lib/qqbot/memory.db")
    )
    if not Path(path).is_file():
        return empty_fun(conversation_id, start_day, end_day)
    where = "created_at >= ? AND created_at < ?"
    params: list[Any] = [start_ts, end_ts]
    if conversation_id:
        where += " AND conversation_id = ?"
        params.append(conversation_id)
    try:
        conn = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, timeout=5
        )
    except sqlite3.Error:
        return empty_fun(conversation_id, start_day, end_day)
    try:
        conn.row_factory = sqlite3.Row
        total = int(
            conn.execute(
                f"SELECT COUNT(*) FROM chat_events WHERE {where}", params
            ).fetchone()[0]
        )
        conversation_total = int(
            conn.execute(
                "SELECT COUNT(DISTINCT conversation_id) FROM chat_events "
                "WHERE created_at >= ? AND created_at < ?",
                (start_ts, end_ts),
            ).fetchone()[0]
        )
        ranking = [
            {
                "user_id": int(row["user_id"]),
                "name": str(row["name"] or row["user_id"]),
                "messages": int(row["messages"]),
            }
            for row in conn.execute(
                "SELECT user_id, "
                "COALESCE(NULLIF(MAX(sender_name),''), CAST(user_id AS TEXT)) AS name, "
                "COUNT(*) AS messages "
                f"FROM chat_events WHERE {where} "
                "GROUP BY user_id ORDER BY messages DESC LIMIT 10",
                params,
            ).fetchall()
        ]
        conversations = [
            {
                "id": str(row["conversation_id"]),
                "label": conversation_label(str(row["conversation_id"])),
                "messages": int(row["messages"]),
            }
            for row in conn.execute(
                "SELECT conversation_id, COUNT(*) AS messages "
                "FROM chat_events WHERE created_at >= ? AND created_at < ? "
                "GROUP BY conversation_id ORDER BY messages DESC LIMIT 200",
                (start_ts, end_ts),
            ).fetchall()
        ]
        texts = [
            str(row[0] or "")
            for row in conn.execute(
                f"SELECT text FROM chat_events WHERE {where} "
                "ORDER BY created_at DESC LIMIT 2000",
                params,
            ).fetchall()
        ]
    except sqlite3.Error:
        return empty_fun(conversation_id, start_day, end_day)
    finally:
        conn.close()

    counts: Counter[str] = Counter()
    for text in texts:
        if not text or text.startswith("/"):
            continue
        for raw in _tokens(text):
            token = str(raw).strip().lower()
            if len(token) < 2 or len(token) > 12:
                continue
            if token in STOPWORDS or token.isdigit():
                continue
            if token.startswith(("http://", "https://", "www.")):
                continue
            if re.fullmatch(r"[^\w一-鿿]+", token):
                continue
            counts[token] += 1
    words = [
        {"text": token, "count": count}
        for token, count in counts.most_common(60)
    ]
    return {
        "date": end_day.strftime("%Y-%m-%d"),
        "start": start_day.strftime("%Y-%m-%d"),
        "end": end_day.strftime("%Y-%m-%d"),
        "days": _range_days(start_day, end_day),
        "conversation_id": conversation_id,
        "messages": total,
        "ranking": ranking,
        "words": words,
        "conversations": conversations,
        "conversation_total": conversation_total,
    }
