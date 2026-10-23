"""SQLite-backed conversation history."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='id',
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
END;
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages (conversation_id, id);
CREATE TABLE IF NOT EXISTS image_captions (
    hash TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    mime TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
"""


def recall_terms(text: str, limit: int = 8) -> list[str]:
    """Search terms for the trigram index, sampled from CJK/latin chunks."""
    import re

    terms: list[str] = []
    for chunk in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9]{2,}", text or ""):
        if len(chunk) <= 3:
            candidates = [chunk] if len(chunk) >= 2 else []
        else:
            step = 1 if len(chunk) <= 12 else 2
            candidates = [chunk[i : i + 3] for i in range(0, len(chunk) - 2, step)]
        for candidate in candidates:
            if candidate not in terms:
                terms.append(candidate)
            if len(terms) >= limit:
                return terms
    return terms


def format_recall(rows: list[tuple[str, str, int]], max_chars: int = 600) -> str:
    """Render recalled messages for the prompt."""
    lines: list[str] = []
    used = 0
    for role, content, _created in rows:
        who = "你" if role == "assistant" else "对方"
        text = " ".join(str(content).split())
        if len(text) > 180:
            text = text[:180] + "…"
        line = f"- {who}: {text}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


class Memory:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        existed = self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'messages_fts'"
        ).fetchone()
        self._db.executescript(_FTS_SCHEMA)
        if existed is None:
            # Index whatever was stored before FTS was introduced.
            self._db.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        self._db.commit()

    def _add_sync(self, conversation_id: str, role: str, content: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (conversation_id, role, content, int(time.time())),
            )
            self._db.commit()

    async def add(self, conversation_id: str, role: str, content: str) -> None:
        await asyncio.to_thread(self._add_sync, conversation_id, role, content)

    def _history_sync(self, conversation_id: str, limit: int) -> list[dict[str, str]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT role, content FROM messages WHERE conversation_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
        rows.reverse()
        return [{"role": role, "content": content} for role, content in rows]

    async def history(self, conversation_id: str, limit: int) -> list[dict[str, str]]:
        return await asyncio.to_thread(self._history_sync, conversation_id, limit)

    def _caption_get_sync(self, digest: str) -> str:
        digest = str(digest or "").strip()
        if not digest:
            return ""
        with self._lock:
            row = self._db.execute(
                "SELECT description FROM image_captions WHERE hash = ?",
                (digest,),
            ).fetchone()
        return str(row[0]).strip() if row else ""

    async def caption_get(self, digest: str) -> str:
        """Return a cached image description, or '' when it has not been built."""
        return await asyncio.to_thread(self._caption_get_sync, digest)

    def _caption_set_sync(
        self, digest: str, description: str, model: str, path: str, mime: str
    ) -> None:
        digest = str(digest or "").strip()
        description = str(description or "").strip()
        if not digest or not description:
            return
        with self._lock:
            self._db.execute(
                "INSERT INTO image_captions "
                "(hash, description, model, path, mime, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(hash) DO UPDATE SET "
                "description = excluded.description, model = excluded.model, "
                "path = excluded.path, mime = excluded.mime",
                (digest, description[:1000], str(model or ""), str(path or ""), str(mime or ""), int(time.time())),
            )
            self._db.commit()

    async def caption_set(
        self, digest: str, description: str, model: str = "", path: str = "", mime: str = ""
    ) -> None:
        await asyncio.to_thread(
            self._caption_set_sync, digest, description, model, path, mime
        )

    def _recall_sync(
        self, conversation_id: str, query: str, limit: int
    ) -> list[tuple[str, str, int]]:
        # The trigram tokenizer only indexes 3-character sequences.
        terms = [term for term in recall_terms(query) if len(term) >= 3]
        if not terms:
            return self._like_sync(conversation_id, query, limit)
        match = " OR ".join(f'"{term}"' for term in terms)
        rows = None
        with self._lock:
            try:
                rows = self._db.execute(
                    "SELECT m.role, m.content, m.created_at "
                    "FROM messages_fts f JOIN messages m ON m.id = f.rowid "
                    "WHERE messages_fts MATCH ? AND m.conversation_id = ? "
                    "ORDER BY rank LIMIT ?",
                    (match, conversation_id, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = None
        if rows is None:
            return self._like_sync(conversation_id, query, limit)
        return [(row[0], row[1], int(row[2])) for row in rows]

    def _like_sync(
        self, conversation_id: str, query: str, limit: int
    ) -> list[tuple[str, str, int]]:
        needle = f"%{query.strip()[:40]}%"
        if needle == "%%":
            return []
        with self._lock:
            rows = self._db.execute(
                "SELECT role, content, created_at FROM messages "
                "WHERE conversation_id = ? AND content LIKE ? "
                "ORDER BY id DESC LIMIT ?",
                (conversation_id, needle, limit),
            ).fetchall()
        return [(row[0], row[1], int(row[2])) for row in rows]

    async def recall(
        self, conversation_id: str, query: str, limit: int = 3
    ) -> list[tuple[str, str, int]]:
        return await asyncio.to_thread(
            self._recall_sync, conversation_id, query, max(1, min(limit, 10))
        )

    def _count_sync(self, conversation_id: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    async def count(self, conversation_id: str) -> int:
        return await asyncio.to_thread(self._count_sync, conversation_id)

    def close(self) -> None:
        with self._lock:
            self._db.close()
