"""Read-only analytics for the admin UI: token ledger, rankings, word cloud."""

from __future__ import annotations

import re
import sqlite3
import warnings
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from usage import UsageStore, conversation_label

try:
    with warnings.catch_warnings():
        # jieba.posseg uses invalid escape sequences in its own regexes;
        # Python 3.12 warns about them at import time.
        warnings.simplefilter("ignore", SyntaxWarning)
        import jieba.posseg as pseg
except ImportError:  # pragma: no cover - optional word segmenter
    pseg = None

TZ = ZoneInfo("Asia/Shanghai")
STOPWORDS = {
    # Pronouns, particles and generic connectors.
    "这个", "那个", "什么", "怎么", "就是", "还是", "然后", "因为", "所以",
    "但是", "如果", "已经", "可以", "没有", "不是", "我们", "你们", "他们",
    "她们", "它们", "自己", "大家", "别人", "咱们", "现在", "今天", "昨天",
    "明天", "早上", "中午", "晚上", "刚才", "真的", "感觉", "一个", "一下",
    "一点", "一些", "有点", "可能", "应该", "知道", "觉得", "时候", "东西",
    "事情", "时间", "这里", "那里", "哪里", "这样", "那样", "这么", "那么",
    "这种", "那种", "这些", "那些", "一样", "不会", "还有", "只是",
    "而且", "其实", "不过", "或者", "虽然", "于是", "以及", "为什么",
    "是不是", "有没有", "能不能", "怎么样", "怎么办", "有人", "这是",
    "要是", "第一次", "一份", "三点", "一次", "每次", "这次", "那次",
    "这时", "那时", "有时候", "有些", "有的", "没什么", "开始", "结束",
    "继续", "进行", "认为", "希望", "需要", "出来", "出去", "进来",
    "进去", "回来", "回去", "过来", "过去", "起来", "下来", "下去",
    "上来", "上去",
    # Laughter and interjections (the repeated-character rule catches the rest).
    "哈哈", "哈哈哈", "哈哈哈哈", "嘿嘿", "嘻嘻", "呵呵", "呜呜", "啊啊",
    "哦哦", "嗯嗯", "hhh", "hhhh", "233", "2333", "23333", "wwww", "emmm",
    "emm", "lol",
    # OneBot placeholders and bot-specific text.
    "图片", "表情", "表情包", "动画表情", "语音", "视频", "文件", "消息",
    "附件", "链接", "网址", "空艾特", "空引用", "戳了戳", "昵称",
    # English function words that survive segmentation.
    "the", "and", "for", "you", "your", "are", "but", "not", "with", "have",
    "this", "that", "these", "those", "from", "they", "them", "their", "will",
    "would", "could", "should", "what", "when", "where", "which", "who", "why",
    "how", "just", "like", "about", "there", "then", "than", "all", "can",
    "get", "got", "was", "were", "has", "had", "its", "too", "very", "much",
    "some", "any", "our", "out", "one", "two", "new", "now", "see", "use",
    "used", "using", "also", "into", "only", "more", "most", "other", "such",
    "even", "still", "here", "yes", "ok", "okay", "http", "https", "www", "com",
}
# A word made of one repeated syllable ("啊啊啊", "hhhh") carries no topic.
_FILLER_CHARS = set("啊哦嗯呀哈嘿嘻呵呜哇诶唉嗷呃额")
WORD_RE = re.compile(r"[一-鿿]{2,6}|[A-Za-z]{2,}")

# Keep topic-bearing parts of speech only: nouns, verbs, adjectives, idioms,
# abbreviations, set phrases and English. Pronouns (r), particles (u/c/p/d),
# numerals/measure words (m/q), interjections (e/o) and punctuation (w/x) are
# dropped. This is the standard jieba/wordcloud recipe and removes "有人".
_ALLOWED_POS = frozenset({
    "n", "nr", "ns", "nt", "nz", "nrfg", "nrt",
    "v", "vi", "vn", "a", "an", "i", "j", "l", "eng",
})
# Two-letter English words are usually noise ("cn", "vd") unless they are
# established technical terms.
_SHORT_ENGLISH_OK = frozenset({
    "ai", "ar", "vr", "ui", "ux", "ip", "pc", "qq", "os", "js", "ts", "py",
    "go", "3d", "4k", "5g", "8k", "ml", "llm", "api", "gpu", "cpu", "vps",
    "ssh", "vpn", "dns", "cdn", "url",
})


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


@lru_cache(maxsize=4096)
def _tagged_words(text: str) -> tuple[tuple[str, str], ...]:
    """Jieba words + POS flags; empty flags when jieba is unavailable."""
    if pseg is not None:
        return tuple((pair.word, pair.flag) for pair in pseg.cut(text))
    return tuple((word, "") for word in WORD_RE.findall(text))


def _is_noise(token: str) -> bool:
    if len(set(token)) == 1:
        if token[0] in _FILLER_CHARS:
            return True
        if token.isascii() and token.isalpha():
            return True
    return False


def top_words(
    texts: list[str],
    limit: int = 60,
    *,
    exclude: Iterable[str] = (),
    min_count: int | None = None,
) -> list[dict[str, Any]]:
    """Most frequent CJK/latin topics across chat messages.

    Best-practice word-cloud extraction: POS filtering via jieba (nouns,
    verbs, adjectives, idioms and English), stopword removal, member-name
    exclusion, and document frequency as the weight. ``count`` is how many
    messages mention the word, so one person spamming it only counts once.

    ``min_count`` defaults to 2 once the sample reaches 20 messages, which
    drops one-off typos from the cloud; small samples keep singletons.
    """
    excluded = {
        " ".join(str(name or "").split()).strip().casefold()
        for name in exclude
        if str(name or "").strip()
    }
    counts: Counter[str] = Counter()
    for text in texts:
        if not text or text.startswith("/"):
            continue
        seen: set[str] = set()
        for raw, flag in _tagged_words(text):
            token = str(raw).strip().lower()
            if len(token) < 2 or len(token) > 12:
                continue
            if flag and flag not in _ALLOWED_POS:
                continue
            if token in STOPWORDS or token.isdigit():
                continue
            if token in excluded or _is_noise(token):
                continue
            if flag == "eng" and len(token) <= 2 and token not in _SHORT_ENGLISH_OK:
                continue
            if flag == "v" and len(token) == 2 and token[0] == token[1]:
                continue
            if token.startswith(("http://", "https://", "www.")):
                continue
            if re.fullmatch(r"[^\w一-鿿]+", token):
                continue
            if token in seen:
                continue
            seen.add(token)
            counts[token] += 1
    threshold = (
        max(1, int(min_count))
        if min_count is not None
        else (2 if len(texts) >= 20 else 1)
    )
    words = [
        {"text": token, "count": count}
        for token, count in counts.most_common()
        if count >= threshold
    ]
    return words[: max(1, int(limit))]


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
        # Spread the sample over the whole range instead of only its tail, so
        # a busy last day cannot hide the rest of a month. ROW_NUMBER keeps the
        # sample deterministic and bounded for large ranges.
        sample_limit = 2000
        stride = max(1, (total + sample_limit - 1) // sample_limit) if total else 1
        texts = [
            str(row[0] or "")
            for row in conn.execute(
                f"WITH picked AS ("
                f"  SELECT text, ROW_NUMBER() OVER (ORDER BY created_at, id) AS rn "
                f"  FROM chat_events WHERE {where}"
                f") SELECT text FROM picked WHERE (rn - 1) % ? = 0 "
                "ORDER BY rn LIMIT ?",
                (*params, stride, sample_limit),
            ).fetchall()
        ]
        sender_names = [
            str(row[0])
            for row in conn.execute(
                f"SELECT DISTINCT sender_name FROM chat_events WHERE {where} "
                "AND sender_name <> ''",
                params,
            ).fetchall()
        ]
    except sqlite3.Error:
        return empty_fun(conversation_id, start_day, end_day)
    finally:
        conn.close()

    words = top_words(texts, exclude=sender_names)
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
