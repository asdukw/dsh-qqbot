"""Explicit QQ identity for each DSH session.

The DSH SDK prompt call carries only a session id and content blocks, so the
adapter records who is speaking (and in which group) in a table shared with the
user-profile plugin before every turn. The plugin reads this row instead of
parsing the session id.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

# Keep in sync with dsh/user-profile.mjs.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    session_id  TEXT PRIMARY KEY,
    scope       TEXT NOT NULL CHECK (scope IN ('private', 'group')),
    user_id     TEXT NOT NULL,
    group_id    TEXT,
    sender_name TEXT NOT NULL DEFAULT '',
    updated_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS session_personas (
    session_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    persona_token TEXT NOT NULL,
    prompt TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
"""


class ConversationStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # record() runs from asyncio.to_thread; one locked connection is safe
        # across threads and serialises the two writers (adapter + profile plugin).
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def record(
        self,
        session_id: str,
        scope: str,
        user_id: int | str,
        group_id: int | str | None,
        sender_name: str,
    ) -> None:
        if scope not in ("private", "group"):
            raise ValueError(f"unknown scope {scope!r}")
        if scope == "group" and group_id is None:
            raise ValueError("group conversations need a group_id")
        with self._lock, self._db:
            self._db.execute(
                """
                INSERT INTO conversations (session_id, scope, user_id, group_id, sender_name, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    scope = excluded.scope,
                    user_id = excluded.user_id,
                    group_id = excluded.group_id,
                    sender_name = excluded.sender_name,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id, scope, str(user_id),
                    None if group_id is None else str(group_id),
                    sender_name or "", int(time.time()),
                ),
            )

    def prepare_persona(self, session_id: str, conversation_id: str,
                        persona_token: str, prompt: str) -> None:
        if not prompt.strip():
            raise ValueError('empty persona prompt')
        with self._lock, self._db:
            self._db.execute(
                'INSERT INTO session_personas VALUES (?, ?, ?, ?, ?) '
                'ON CONFLICT(session_id) DO UPDATE SET conversation_id=excluded.conversation_id, '
                'persona_token=excluded.persona_token, prompt=excluded.prompt, updated_at=excluded.updated_at',
                (session_id, conversation_id, persona_token, prompt, int(time.time())),
            )

    def get(self, session_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM conversations WHERE session_id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        with self._lock:
            self._db.close()
