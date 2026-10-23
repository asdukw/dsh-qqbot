"""Admin-created scheduled messages.

An admin describes a task in natural language ("每天早上 8 点提醒大家喝水");
one thin-client call turns it into a validated TaskSpec, and the scheduler
fires it at the computed next run. Only admins reach this code: creation goes
through the admin-only command router.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator

log = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Shanghai")
MIN_INTERVAL_MINUTES = 5
MAX_MESSAGE_CHARS = 300
# A one-shot reminder more than this late is dropped instead of fired at boot.
ONCE_MAX_LATE_SECONDS = 12 * 3600

# Natural-language task management: "[定时列表]", "[定时删除: 喝水]" and so on.
_MANAGE_RE = re.compile(r"\[定时(列表|删除|暂停|恢复)[:：]?\s*([^\]\n]{0,100})\]")


def extract_task_management(text: str) -> tuple[str, tuple[str, str] | None]:
    """Split a management marker out of a reply; return (visible, (action, arg))."""
    match = _MANAGE_RE.search(text or "")
    if match is None:
        return text, None
    visible = _MANAGE_RE.sub("", text, count=1).strip()
    return visible, (match.group(1), match.group(2).strip())


# Natural-language task requests: the model emits "[定时: ...]" and the
# adapter validates + creates it. Only admins reach the creation branch.
_TASK_MARKER_RE = re.compile(r"\[定时[:：]\s*([^\]\n]{1,200})\]")


def extract_task_marker(text: str) -> tuple[str, str | None]:
    """Split a "[定时: 描述]" marker out of a reply; return (visible, description)."""
    match = _TASK_MARKER_RE.search(text or "")
    if match is None:
        return text, None
    return _TASK_MARKER_RE.sub("", text, count=1).strip(), match.group(1).strip()


_TASK_PROMPT = """把管理员这句话解析成定时任务，只返回 JSON，不要解释。
当前时间：{now}（{weekday}，时区 Asia/Shanghai）。
字段：
- kind: "daily" 每天 | "weekly" 每周 | "once" 一次性 | "interval" 每隔多久
- time: "HH:MM"（daily/weekly 必填，24 小时制）
- weekday: 0=周一 ... 6=周日（weekly 必填）
- run_at: ISO 8601 时间，带 +08:00（once 必填，必须晚于当前时间）
- every_minutes: 间隔分钟数，最小 {min_interval}（interval 必填）
- target: "here" 就在当前会话 | "private" 私聊管理员 | "group" 当前群（私聊里不能选 group）；默认 here
- mode: "fixed" 按 message 原样发送 | "generate" 到点按人设现写一句；默认 fixed
- message: 到点要发送的内容，一句话，保留原意，不要包含时间
例子：{{"kind":"daily","time":"08:00","message":"该喝水啦，别一直坐着。"}}
管理员的话：{description}"""


class TaskSpec(BaseModel):
    """A validated schedule + the message to send."""

    kind: Literal["daily", "weekly", "once", "interval"]
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    target: Literal["here", "private", "group"] = "here"
    mode: Literal["fixed", "generate"] = "fixed"
    time: str | None = None
    weekday: int | None = Field(default=None, ge=0, le=6)
    run_at: datetime | None = None
    every_minutes: int | None = Field(
        default=None, ge=MIN_INTERVAL_MINUTES, le=7 * 24 * 60
    )

    @field_validator("message")
    @classmethod
    def _clean_message(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("message is empty")
        return value

    @field_validator("run_at")
    @classmethod
    def _aware_run_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.astimezone(TZ) if value.tzinfo is not None else value.replace(tzinfo=TZ)

    @field_validator("time")
    @classmethod
    def _clean_time(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = value.strip().split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ValueError("time must be HH:MM")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("time must be HH:MM")
        return f"{hour:02d}:{minute:02d}"

    @model_validator(mode="before")
    @classmethod
    def _drop_irrelevant(cls, data: Any) -> Any:
        """Ignore placeholder values in fields this kind does not use."""
        if not isinstance(data, dict):
            return data
        kind = data.get("kind")
        keep = {"kind", "message", "target", "mode"}
        if kind in ("daily", "weekly"):
            keep.add("time")
            if kind == "weekly":
                keep.add("weekday")
        elif kind == "once":
            keep.add("run_at")
        elif kind == "interval":
            keep.add("every_minutes")
        return {key: value for key, value in data.items() if key in keep}

    @model_validator(mode="after")
    def _kind_fields(self) -> TaskSpec:
        if self.kind in ("daily", "weekly") and not self.time:
            raise ValueError("time is required for daily/weekly")
        if self.kind == "weekly" and self.weekday is None:
            raise ValueError("weekday is required for weekly")
        if self.kind == "once" and self.run_at is None:
            raise ValueError("run_at is required for once")
        if self.kind == "interval" and self.every_minutes is None:
            raise ValueError("every_minutes is required for interval")
        return self

    def to_row(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "message": self.message,
            "time_of_day": self.time,
            "weekday": self.weekday,
            "run_at": int(self.run_at.timestamp()) if self.run_at else None,
            "every_minutes": self.every_minutes,
        }


def spec_from_row(row: sqlite3.Row) -> TaskSpec:
    return TaskSpec(
        kind=row["kind"],
        message=row["message"],
        time=row["time_of_day"],
        weekday=row["weekday"],
        run_at=(
            datetime.fromtimestamp(int(row["run_at"]), TZ) if row["run_at"] else None
        ),
        every_minutes=row["every_minutes"],
    )


def next_run_for(spec: TaskSpec, now: datetime) -> datetime:
    """First fire time strictly after `now` (raises for past one-shot times)."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    now = now.astimezone(TZ)

    if spec.kind == "interval":
        return now + timedelta(minutes=int(spec.every_minutes or 0))

    if spec.kind == "once":
        run_at = spec.run_at
        assert run_at is not None
        run_at = run_at.astimezone(TZ) if run_at.tzinfo else run_at.replace(tzinfo=TZ)
        if run_at <= now:
            raise ValueError("once time must be in the future")
        return run_at

    hour, minute = (int(part) for part in str(spec.time).split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if spec.kind == "daily":
        if target <= now:
            target += timedelta(days=1)
        return target
    # weekly
    days = (int(spec.weekday) - now.weekday()) % 7
    target += timedelta(days=days)
    if target <= now:
        target += timedelta(days=7)
    return target


def resolve_target(
    target: str, chat_type: str, user_id: int, group_id: int | None
) -> tuple[str, int]:
    """Turn the model's target choice into (chat_type, chat_id)."""
    if target == "private":
        return "private", int(user_id)
    if target == "group":
        if chat_type != "group" or group_id is None:
            raise ValueError("私聊里创建的任务不能发到群里")
        return "group", int(group_id)
    if chat_type == "group" and group_id is not None:
        return "group", int(group_id)
    return "private", int(user_id)


def render_task_list(rows: list[TaskRecord], limit: int = 20) -> str:
    """Shared rendering for /task list and the natural-language list marker."""
    if not rows:
        return "还没有定时任务。"
    lines = [f"定时任务（共 {len(rows)} 个）:"]
    for record in rows[:limit]:
        mark = "*" if record.enabled else "-"
        mode = " 生成" if record.mode == "generate" else ""
        when = (
            datetime.fromtimestamp(record.next_run, TZ).strftime("%m-%d %H:%M")
            if record.enabled and record.next_run
            else "暂停"
        )
        lines.append(
            f"{mark} #{record.id} {describe_record(record)} → {describe_target(record)}"
            f"｜ 下次 {when}{mode} ｜ {record.message}"
        )
    if len(rows) > limit:
        lines.append(f"…还有 {len(rows) - limit} 个。")
    return "\n".join(lines)


def describe_target(record: TaskRecord) -> str:
    kind = "群" if record.chat_type == "group" else "私聊"
    return f"{kind} {record.chat_id}"


def spec_of(record: TaskRecord) -> TaskSpec:
    """Rebuild a validated spec from a stored row."""
    return TaskSpec(
        kind=record.kind,
        message=record.message,
        time=record.time_of_day,
        weekday=record.weekday,
        run_at=(
            datetime.fromtimestamp(record.run_at, TZ) if record.run_at else None
        ),
        every_minutes=record.every_minutes,
    )


def describe_record(record: TaskRecord) -> str:
    """Human-readable schedule summary for /task list."""
    if record.kind == "daily":
        return f"每天 {record.time_of_day}"
    if record.kind == "weekly":
        return f"每周{_WEEKDAYS[int(record.weekday or 0)]} {record.time_of_day}"
    if record.kind == "interval":
        return f"每 {record.every_minutes} 分钟"
    if record.kind == "once" and record.run_at:
        return datetime.fromtimestamp(record.run_at, TZ).strftime("%m-%d %H:%M 一次")
    return record.kind


def parse_task_json(raw: str) -> TaskSpec:
    """Pull the JSON object out of a model reply and validate it."""
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("model returned no JSON object")
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"model returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("model returned JSON that is not an object")
    return TaskSpec.model_validate(data)


_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


async def parse_task(client: Any, description: str, now: datetime) -> tuple[TaskSpec, datetime]:
    """Ask the thin client to structure a natural-language task."""
    prompt = _TASK_PROMPT.format(
        now=now.astimezone(TZ).strftime("%Y-%m-%d %H:%M"),
        weekday=_WEEKDAYS[now.weekday()],
        min_interval=MIN_INTERVAL_MINUTES,
        description=description.strip(),
    )
    raw = await client.chat([
        {
            "role": "system",
            "content": "你是一个把自然语言转成定时任务的解析器，只输出 JSON。",
        },
        {"role": "user", "content": prompt},
    ])
    spec = parse_task_json(raw)
    return spec, next_run_for(spec, now)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_type     TEXT NOT NULL CHECK (chat_type IN ('group', 'private')),
    chat_id       INTEGER NOT NULL,
    created_by    TEXT NOT NULL,
    instruction   TEXT NOT NULL,
    message       TEXT NOT NULL,
    kind          TEXT NOT NULL,
    time_of_day   TEXT,
    weekday       INTEGER,
    run_at        INTEGER,
    every_minutes INTEGER,
    mode          TEXT NOT NULL DEFAULT 'fixed',
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    INTEGER NOT NULL,
    next_run      INTEGER NOT NULL,
    last_run      INTEGER NOT NULL DEFAULT 0,
    runs          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_next ON tasks (enabled, next_run);
"""


@dataclass(frozen=True)
class TaskRecord:
    id: int
    chat_type: str
    chat_id: int
    created_by: str
    instruction: str
    message: str
    kind: str
    time_of_day: str | None
    weekday: int | None
    run_at: int | None
    every_minutes: int | None
    mode: str
    enabled: bool
    created_at: int
    next_run: int
    last_run: int
    runs: int


def _record(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        id=int(row["id"]),
        chat_type=row["chat_type"],
        chat_id=int(row["chat_id"]),
        created_by=row["created_by"] or "",
        instruction=row["instruction"] or "",
        message=row["message"] or "",
        kind=row["kind"] or "",
        time_of_day=row["time_of_day"],
        weekday=row["weekday"],
        run_at=row["run_at"],
        every_minutes=row["every_minutes"],
        mode=row["mode"] or "fixed",
        enabled=bool(row["enabled"]),
        created_at=int(row["created_at"]),
        next_run=int(row["next_run"]),
        last_run=int(row["last_run"] or 0),
        runs=int(row["runs"] or 0),
    )


class TaskStore:
    """SQLite persistence for scheduled tasks."""

    def __init__(self, database: str):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(database, timeout=5, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(tasks)")}
        if "mode" not in columns:
            self._db.execute(
                "ALTER TABLE tasks ADD COLUMN mode TEXT NOT NULL DEFAULT 'fixed'"
            )
        self._db.commit()

    def add(
        self,
        *,
        chat_type: str,
        chat_id: int,
        created_by: str,
        instruction: str,
        spec: TaskSpec,
        next_run: datetime,
    ) -> TaskRecord:
        now = int(time.time())
        with self._lock, self._db:
            cursor = self._db.execute(
                """
                INSERT INTO tasks
                    (chat_type, chat_id, created_by, instruction, message, kind,
                     time_of_day, weekday, run_at, every_minutes, mode, enabled,
                     created_at, next_run)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    chat_type,
                    int(chat_id),
                    str(created_by)[:40],
                    instruction.strip()[:500],
                    spec.message,
                    spec.kind,
                    spec.time,
                    spec.weekday,
                    int(spec.run_at.timestamp()) if spec.run_at else None,
                    spec.every_minutes,
                    spec.mode,
                    now,
                    int(next_run.timestamp()),
                ),
            )
            row = self._db.execute(
                "SELECT * FROM tasks WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return _record(row)

    def get(self, task_id: int) -> TaskRecord | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return _record(row) if row else None

    def list(self, limit: int = 50) -> list[TaskRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM tasks ORDER BY enabled DESC, next_run, id LIMIT ?",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [_record(row) for row in rows]

    def count_for(self, chat_type: str, chat_id: int) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM tasks WHERE chat_type = ? AND chat_id = ?",
                (chat_type, int(chat_id)),
            ).fetchone()
        return int(row[0]) if row else 0

    def count(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT COUNT(*) FROM tasks").fetchone()
        return int(row[0]) if row else 0

    def due(self, now: datetime) -> list[TaskRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM tasks WHERE enabled = 1 AND next_run <= ? "
                "ORDER BY next_run, id",
                (int(now.timestamp()),),
            ).fetchall()
        return [_record(row) for row in rows]

    def mark_run(
        self, task_id: int, *, next_run: datetime | None, ran_at: datetime
    ) -> None:
        """Advance a task before its message is sent (at-most-once delivery)."""
        with self._lock, self._db:
            if next_run is None:
                self._db.execute(
                    "UPDATE tasks SET enabled = 0, next_run = 0, last_run = ?, "
                    "runs = runs + 1 WHERE id = ?",
                    (int(ran_at.timestamp()), task_id),
                )
            else:
                self._db.execute(
                    "UPDATE tasks SET next_run = ?, last_run = ?, runs = runs + 1 "
                    "WHERE id = ?",
                    (int(next_run.timestamp()), int(ran_at.timestamp()), task_id),
                )

    def set_enabled(self, task_id: int, enabled: bool, next_run: datetime | None) -> TaskRecord:
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE tasks SET enabled = ?, next_run = ? WHERE id = ?",
                (
                    1 if enabled else 0,
                    int(next_run.timestamp()) if next_run else 0,
                    task_id,
                ),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"task #{task_id} does not exist")
            row = self._db.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return _record(row)

    def delete(self, task_id: int) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cursor.rowcount > 0

    def close(self) -> None:
        self._db.close()


class TaskScheduler:
    """Poll due tasks and hand them to the adapter's sender."""

    def __init__(
        self,
        store: TaskStore,
        send: Any,
        *,
        check_seconds: float = 20.0,
        ready: Any = None,
    ):
        self.store = store
        self._send = send
        # `ready` gates ticks until the transport is connected: marking a task
        # run while the websocket is down would silently lose the occurrence.
        self._ready = ready
        self.check_seconds = max(5.0, float(check_seconds))
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="qqbot-tasks")

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
                await self.tick(datetime.now(TZ))
            except Exception:  # noqa: BLE001 - the loop must survive one bad task
                log.exception("task scheduler tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.check_seconds)
            except asyncio.TimeoutError:
                continue

    async def tick(self, now: datetime) -> int:
        """Fire every due task; returns how many fired (also useful in tests)."""
        if self._ready is not None and not self._ready():
            return 0
        fired = 0
        for task in self.store.due(now):
            # A concurrent pause/delete must win over a tick that already
            # snapshotted the row.
            current = self.store.get(task.id)
            if (
                current is None
                or not current.enabled
                or current.next_run != task.next_run
            ):
                continue
            if (
                task.kind == "once"
                and now.timestamp() - task.next_run > ONCE_MAX_LATE_SECONDS
            ):
                log.info(
                    "task #%s is more than %.0fh late; dropping instead of firing",
                    task.id,
                    ONCE_MAX_LATE_SECONDS / 3600,
                )
                self.store.mark_run(task.id, next_run=None, ran_at=now)
                continue
            try:
                spec = spec_of(task)
            except Exception as exc:  # noqa: BLE001 - bad row must not wedge the loop
                log.warning("task #%s has an invalid schedule: %s; disabling", task.id, exc)
                self.store.set_enabled(task.id, False, None)
                continue
            next_run: datetime | None
            try:
                next_run = None if task.kind == "once" else next_run_for(spec, now)
            except ValueError as exc:
                log.warning("task #%s cannot be rescheduled: %s", task.id, exc)
                next_run = None
            self.store.mark_run(task.id, next_run=next_run, ran_at=now)
            try:
                await self._send(task)
            except Exception:  # noqa: BLE001 - one failed send must not stop the loop
                log.exception("task #%s failed to send", task.id)
            else:
                fired += 1
                log.info(
                    "task #%s fired to %s:%s (next=%s)",
                    task.id,
                    task.chat_type,
                    task.chat_id,
                    next_run.isoformat() if next_run else "-",
                )
        return fired
