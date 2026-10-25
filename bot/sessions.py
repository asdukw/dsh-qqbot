"""DSH session rotation metadata.

The DSH session id is derived from the QQ conversation plus a generation
counter. Rotation only changes which JSONL the runtime resumes; message
history in ``memory.db`` and FTS recall stay keyed by the conversation.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_state (
    conversation_id TEXT PRIMARY KEY,
    generation      INTEGER NOT NULL DEFAULT 0,
    started_at      INTEGER NOT NULL,
    last_active     INTEGER NOT NULL,
    messages        INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True)
class SessionState:
    conversation_id: str
    generation: int
    session_id: str
    started_at: int
    last_active: int
    messages: int


class SessionStore:
    """Persist per-conversation generation and decide when to rotate.

    A generation of 0 keeps the historical session id (``group-123``);
    generation N>0 uses ``group-123-gN``. Old DSH JSONL files are never
    deleted by this store.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        idle_hours: float = 24.0,
        idle_hours_private: float = 168.0,
        max_hours: float = 0.0,
        max_messages: int = 0,
        daily_hour: int = 4,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.idle_hours = max(0.0, float(idle_hours))
        self.idle_hours_private = max(0.0, float(idle_hours_private))
        self.max_hours = max(0.0, float(max_hours))
        self.max_messages = max(0, int(max_messages))
        # Groups rotate at the first message after this local hour when the
        # session was started on an earlier day. -1 disables daily rotation.
        self.daily_hour = int(daily_hour) if 0 <= int(daily_hour) <= 23 else -1
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        columns = {row[1] for row in self._db.execute('PRAGMA table_info(session_state)')}
        for name, definition in (("persona_token", "TEXT NOT NULL DEFAULT ''"),
                                 ("persona_cutoff", "INTEGER NOT NULL DEFAULT 0")):
            if name not in columns:
                self._db.execute(f'ALTER TABLE session_state ADD COLUMN {name} {definition}')
        self._db.commit()

    @staticmethod
    def base_session_id(conversation_id: str) -> str:
        return conversation_id.replace(":", "-")

    @classmethod
    def session_id_for(cls, conversation_id: str, generation: int) -> str:
        base = cls.base_session_id(conversation_id)
        return base if generation <= 0 else f"{base}-g{generation}"

    def _state(self, row: sqlite3.Row) -> SessionState:
        conversation_id = str(row["conversation_id"])
        generation = int(row["generation"])
        return SessionState(
            conversation_id=conversation_id,
            generation=generation,
            session_id=self.session_id_for(conversation_id, generation),
            started_at=int(row["started_at"]),
            last_active=int(row["last_active"]),
            messages=int(row["messages"]),
        )

    def _row(self, conversation_id: str) -> sqlite3.Row | None:
        return self._db.execute(
            "SELECT conversation_id, generation, started_at, last_active, messages "
            "FROM session_state WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()

    def _ensure(self, conversation_id: str, now: int) -> sqlite3.Row:
        row = self._row(conversation_id)
        if row is not None:
            return row
        self._db.execute(
            "INSERT INTO session_state "
            "(conversation_id, generation, started_at, last_active, messages) "
            "VALUES (?, 0, ?, ?, 0)",
            (conversation_id, now, now),
        )
        self._db.commit()
        row = self._row(conversation_id)
        assert row is not None
        return row

    def _rotate_locked(self, state: SessionState, now: int) -> SessionState:
        generation = state.generation + 1
        self._db.execute(
            "UPDATE session_state SET generation = ?, started_at = ?, "
            "last_active = ?, messages = 0 WHERE conversation_id = ?",
            (generation, now, now, state.conversation_id),
        )
        self._db.commit()
        return SessionState(
            conversation_id=state.conversation_id,
            generation=generation,
            session_id=self.session_id_for(state.conversation_id, generation),
            started_at=now,
            last_active=now,
            messages=0,
        )

    def seed_existing(
        self,
        conversation_id: str,
        *,
        started_at: int,
        last_active: int,
        messages: int = 0,
    ) -> bool:
        """Record an already-existing DSH session without rotating it.

        Startup uses this so an old ``group-123`` session is judged by its
        real start/last-active timestamps instead of looking brand new. A
        generation-0 row with no completed turns is treated as unseeded and
        refreshed; anything else is left alone.
        """
        started = int(started_at)
        active = int(last_active)
        count = max(0, int(messages))
        with self._lock:
            row = self._row(conversation_id)
            if row is None:
                self._db.execute(
                    "INSERT INTO session_state "
                    "(conversation_id, generation, started_at, last_active, messages) "
                    "VALUES (?, 0, ?, ?, ?)",
                    (conversation_id, started, active, count),
                )
                self._db.commit()
                return True
            if int(row["generation"]) != 0 or int(row["messages"]) != 0:
                return False
            self._db.execute(
                "UPDATE session_state SET started_at = ?, last_active = ?, "
                "messages = ? WHERE conversation_id = ?",
                (started, active, count, conversation_id),
            )
            self._db.commit()
            return True

    def _should_rotate(self, state: SessionState, now: int, is_group: bool) -> bool:
        idle_hours = self.idle_hours if is_group else self.idle_hours_private
        if idle_hours and now - state.last_active >= idle_hours * 3600:
            return True
        if self.max_hours and now - state.started_at >= self.max_hours * 3600:
            return True
        if self.max_messages and state.messages >= self.max_messages:
            return True
        if is_group and self.daily_hour >= 0:
            current = datetime.fromtimestamp(now, TZ)
            started = datetime.fromtimestamp(state.started_at, TZ)
            if current.hour >= self.daily_hour and current.date() > started.date():
                return True
        return False

    def current(
        self, conversation_id: str, *, is_group: bool, now: int | None = None
    ) -> tuple[SessionState, bool]:
        """Return the active session, rotating it first if a policy says so."""
        stamp = int(now if now is not None else time.time())
        with self._lock:
            state = self._state(self._ensure(conversation_id, stamp))
            if self._should_rotate(state, stamp, is_group):
                return self._rotate_locked(state, stamp), True
            return state, False

    def touch(self, conversation_id: str, *, now: int | None = None) -> None:
        """Count one completed turn and refresh the idle clock."""
        stamp = int(now if now is not None else time.time())
        with self._lock:
            self._ensure(conversation_id, stamp)
            self._db.execute(
                "UPDATE session_state SET messages = messages + 1, last_active = ? "
                "WHERE conversation_id = ?",
                (stamp, conversation_id),
            )
            self._db.commit()

    def reset(self, conversation_id: str, *, now: int | None = None) -> SessionState:
        """Force a new generation (manual /reset)."""
        stamp = int(now if now is not None else time.time())
        with self._lock:
            state = self._state(self._ensure(conversation_id, stamp))
            return self._rotate_locked(state, stamp)

    def info(self, conversation_id: str) -> SessionState | None:
        with self._lock:
            row = self._row(conversation_id)
        return self._state(row) if row is not None else None

    def reconcile_persona(self, conversation_id: str, token: str, cutoff: int,
                          *, explicit: bool = False) -> SessionState:
        """Rotate once on a new persona, but adopt legacy default sessions intact."""
        stamp = int(time.time())
        with self._lock:
            state = self._state(self._ensure(conversation_id, stamp))
            row = self._db.execute(
                'SELECT persona_token FROM session_state WHERE conversation_id = ?',
                (conversation_id,),
            ).fetchone()
            previous = str(row[0])
            if previous != token:
                if previous or explicit:
                    # Keep the generation and persona token in one commit.
                    self._db.execute(
                        'UPDATE session_state SET generation=generation+1, started_at=?, '
                        'last_active=?, messages=0 WHERE conversation_id=?',
                        (stamp, stamp, conversation_id),
                    )
                self._db.execute(
                    'UPDATE session_state SET persona_token=?, persona_cutoff=? WHERE conversation_id=?',
                    (token, cutoff, conversation_id),
                )
                self._db.commit()
                row = self._row(conversation_id)
                assert row is not None
                state = self._state(row)
            return state

    def close(self) -> None:
        with self._lock:
            self._db.close()
