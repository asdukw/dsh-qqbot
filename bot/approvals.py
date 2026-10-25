"""Friend/group request approvals.

NapCat pushes OneBot 11 ``request`` events for friend requests and group
invitations/join requests. The adapter stores them in SQLite; the admin UI
queues an approve/reject action, and the worker here executes it through the
bot's existing OneBot WebSocket connection. Keeping execution in the bot
process means the admin UI never needs its own NapCat credentials or socket.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

FRIEND = "friend"
GROUP_INVITE = "group_invite"
GROUP_ADD = "group_add"
KINDS = frozenset({FRIEND, GROUP_INVITE, GROUP_ADD})
ACTIVE_STATUSES = ("pending", "queued", "executing")
HANDLED_STATUSES = ("approved", "rejected", "failed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_requests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL CHECK (kind IN ('friend', 'group_invite', 'group_add')),
    flag           TEXT NOT NULL,
    user_id        INTEGER NOT NULL,
    group_id       INTEGER,
    group_name     TEXT NOT NULL DEFAULT '',
    requester_name TEXT NOT NULL DEFAULT '',
    comment        TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'queued', 'executing',
                                     'approved', 'rejected', 'failed')),
    action         TEXT NOT NULL DEFAULT ''
                   CHECK (action IN ('', 'approve', 'reject')),
    error          TEXT NOT NULL DEFAULT '',
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL,
    handled_at     INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_flag
    ON approval_requests (kind, flag);
CREATE INDEX IF NOT EXISTS idx_approval_status
    ON approval_requests (status, id);
"""


@dataclass(frozen=True)
class ApprovalRecord:
    id: int
    kind: str
    flag: str
    user_id: int
    group_id: int | None
    group_name: str
    requester_name: str
    comment: str
    status: str
    action: str
    error: str
    created_at: int
    updated_at: int
    handled_at: int


def _record(row: sqlite3.Row) -> ApprovalRecord:
    return ApprovalRecord(
        id=int(row["id"]),
        kind=str(row["kind"]),
        flag=str(row["flag"]),
        user_id=int(row["user_id"]),
        group_id=int(row["group_id"]) if row["group_id"] is not None else None,
        group_name=str(row["group_name"] or ""),
        requester_name=str(row["requester_name"] or ""),
        comment=str(row["comment"] or ""),
        status=str(row["status"]),
        action=str(row["action"] or ""),
        error=str(row["error"] or ""),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
        handled_at=int(row["handled_at"] or 0),
    )


class ApprovalStore:
    """SQLite persistence for incoming requests and queued decisions."""

    def __init__(self, database: str | Path, *, readonly: bool = False):
        self.path = Path(database)
        self.readonly = readonly
        self._lock = threading.Lock()
        if readonly:
            self._db = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True, timeout=5
            )
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            self._db.commit()
        self._db.row_factory = sqlite3.Row

    def add(
        self,
        *,
        kind: str,
        flag: str,
        user_id: int,
        group_id: int | None = None,
        group_name: str = "",
        requester_name: str = "",
        comment: str = "",
        created_at: int | None = None,
    ) -> tuple[ApprovalRecord, bool]:
        """Insert a request once; duplicate flags return the existing row."""
        if kind not in KINDS:
            raise ValueError(f"unknown approval kind {kind!r}")
        if not flag:
            raise ValueError("approval flag is empty")
        now = int(created_at if created_at is not None else time.time())
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO approval_requests "
                "(kind, flag, user_id, group_id, group_name, requester_name, "
                "comment, status, action, error, created_at, updated_at, handled_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '', '', ?, ?, 0)",
                (
                    kind,
                    str(flag)[:200],
                    int(user_id),
                    None if group_id is None else int(group_id),
                    str(group_name or "")[:100],
                    str(requester_name or "")[:100],
                    str(comment or "")[:500],
                    now,
                    now,
                ),
            )
            created = cursor.rowcount > 0
            row = self._db.execute(
                "SELECT * FROM approval_requests WHERE kind = ? AND flag = ?",
                (kind, str(flag)[:200]),
            ).fetchone()
        if row is None:
            raise sqlite3.IntegrityError("approval request was not stored")
        return _record(row), created

    def get(self, request_id: int) -> ApprovalRecord | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM approval_requests WHERE id = ?", (int(request_id),)
            ).fetchone()
        return _record(row) if row else None

    def list(self, limit: int = 200) -> list[ApprovalRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM approval_requests "
                "ORDER BY CASE status "
                "  WHEN 'pending' THEN 0 WHEN 'queued' THEN 1 "
                "  WHEN 'executing' THEN 2 ELSE 3 END, "
                "id DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [_record(row) for row in rows]

    def active_count(self) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM approval_requests "
                "WHERE status IN ('pending', 'queued', 'executing')"
            ).fetchone()
        return int(row[0] or 0) if row else 0

    def queue(self, request_id: int, action: str) -> ApprovalRecord:
        """Mark a pending/failed request for the bot worker to execute."""
        action = str(action or "").strip().lower()
        if action not in ("approve", "reject"):
            raise ValueError("action must be approve or reject")
        now = int(time.time())
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT * FROM approval_requests WHERE id = ?", (int(request_id),)
            ).fetchone()
            if row is None:
                raise ValueError("审批请求不存在")
            record = _record(row)
            if record.status in ("queued", "executing"):
                if record.action == action:
                    return record
                raise ValueError("请求已提交，请等待执行结果")
            if record.status in ("approved", "rejected"):
                raise ValueError("请求已经处理过了")
            if record.status not in ("pending", "failed"):
                raise ValueError("当前状态不能审批")
            self._db.execute(
                "UPDATE approval_requests SET status = 'queued', action = ?, "
                "error = '', updated_at = ? WHERE id = ?",
                (action, now, int(request_id)),
            )
            row = self._db.execute(
                "SELECT * FROM approval_requests WHERE id = ?", (int(request_id),)
            ).fetchone()
        return _record(row)

    def claim(self, limit: int = 10) -> list[ApprovalRecord]:
        """Atomically move queued decisions to executing for the worker."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                rows = self._db.execute(
                    "SELECT * FROM approval_requests WHERE status = 'queued' "
                    "ORDER BY id LIMIT ?",
                    (max(1, min(int(limit), 50)),),
                ).fetchall()
                ids = [int(row["id"]) for row in rows]
                if ids:
                    now = int(time.time())
                    self._db.executemany(
                        "UPDATE approval_requests SET status = 'executing', "
                        "updated_at = ? WHERE id = ?",
                        [(now, request_id) for request_id in ids],
                    )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return [replace(_record(row), status="executing") for row in rows]

    def finish(self, request_id: int, status: str, error: str = "") -> None:
        if status not in ("approved", "rejected", "failed"):
            raise ValueError(f"invalid approval status {status!r}")
        now = int(time.time())
        with self._lock, self._db:
            self._db.execute(
                "UPDATE approval_requests SET status = ?, error = ?, "
                "handled_at = ?, updated_at = ? WHERE id = ?",
                (status, str(error or "")[:1000], now, now, int(request_id)),
            )

    def reset_executing(self) -> int:
        """Requeue work interrupted by a process restart."""
        now = int(time.time())
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE approval_requests SET status = 'queued', updated_at = ? "
                "WHERE status = 'executing'",
                (now,),
            )
        return int(cursor.rowcount or 0)

    def close(self) -> None:
        with self._lock:
            self._db.close()


def onebot_action(record: ApprovalRecord) -> tuple[str, dict[str, Any]]:
    """Map a stored request + decision to the OneBot 11 action."""
    approve = record.action == "approve"
    if record.kind == FRIEND:
        return "set_friend_add_request", {
            "flag": record.flag,
            "approve": approve,
        }
    if record.kind in (GROUP_INVITE, GROUP_ADD):
        params: dict[str, Any] = {
            "flag": record.flag,
            "sub_type": "invite" if record.kind == GROUP_INVITE else "add",
            "approve": approve,
        }
        if not approve:
            params["reason"] = "管理员已拒绝"
        return "set_group_add_request", params
    raise ValueError(f"unknown approval kind {record.kind!r}")


class ApprovalWorker:
    """Execute queued decisions through the bot's OneBot client."""

    def __init__(
        self,
        store: ApprovalStore,
        call: Callable[[str, dict[str, Any]], Awaitable[Any]],
        *,
        check_seconds: float = 3.0,
        ready: Callable[[], bool] | None = None,
    ):
        self.store = store
        self._call = call
        self._ready = ready
        self.check_seconds = max(1.0, float(check_seconds))
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is not None:
            return
        reset = self.store.reset_executing()
        if reset:
            log.warning("requeued %d interrupted approval(s)", reset)
        self._task = asyncio.create_task(self._run(), name="qqbot-approvals")

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
            except Exception:  # noqa: BLE001 - the loop must survive one bad row
                log.exception("approval worker tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.check_seconds)
            except TimeoutError:
                continue

    async def tick(self) -> int:
        if self._ready is not None and not self._ready():
            return 0
        records = await asyncio.to_thread(self.store.claim)
        for record in records:
            try:
                action, params = onebot_action(record)
                await self._call(action, params)
            except Exception as exc:  # noqa: BLE001 - surface in the admin page
                log.warning("approval #%s failed: %s", record.id, exc)
                self.store.finish(record.id, "failed", error=str(exc))
                continue
            status = "approved" if record.action == "approve" else "rejected"
            self.store.finish(record.id, status)
            log.info(
                "approval #%s %s (%s user=%s group=%s)",
                record.id,
                status,
                record.kind,
                record.user_id,
                record.group_id or "-",
            )
        return len(records)
