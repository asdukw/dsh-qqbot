from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from tasks import (  # noqa: E402
    TZ,
    TaskRecord,
    TaskScheduler,
    TaskSpec,
    TaskStore,
    describe_record,
    describe_target,
    extract_task_management,
    extract_task_marker,
    next_run_for,
    parse_task,
    parse_task_json,
    render_task_list,
    resolve_target,
    spec_of,
)

NOW = datetime(2026, 10, 23, 10, 30, tzinfo=TZ)  # Friday


def spec(**overrides) -> TaskSpec:
    base = {"kind": "daily", "message": "该喝水啦", "time": "08:00"}
    base.update(overrides)
    return TaskSpec(**base)


class NextRunTests(unittest.TestCase):
    def test_daily_rolls_to_tomorrow_after_the_time(self) -> None:
        self.assertEqual(
            next_run_for(spec(time="08:00"), NOW),
            datetime(2026, 10, 24, 8, 0, tzinfo=TZ),
        )
        self.assertEqual(
            next_run_for(spec(time="12:00"), NOW),
            datetime(2026, 10, 23, 12, 0, tzinfo=TZ),
        )

    def test_weekly_picks_the_next_matching_weekday(self) -> None:
        # NOW is Friday (weekday 4); Monday is three days later.
        monday = next_run_for(spec(kind="weekly", weekday=0, time="09:00"), NOW)
        self.assertEqual(monday, datetime(2026, 10, 26, 9, 0, tzinfo=TZ))
        # Today's weekday but earlier than now -> next week.
        friday_early = next_run_for(spec(kind="weekly", weekday=4, time="09:00"), NOW)
        self.assertEqual(friday_early, datetime(2026, 10, 30, 9, 0, tzinfo=TZ))

    def test_once_must_be_in_the_future(self) -> None:
        future = NOW + timedelta(hours=2)
        self.assertEqual(
            next_run_for(spec(kind="once", time=None, run_at=future), NOW), future
        )

    def test_once_time_is_normalised_to_beijing(self) -> None:
        utc = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)
        when = next_run_for(spec(kind="once", time=None, run_at=utc), NOW)
        self.assertEqual(when.utcoffset(), timedelta(hours=8))
        self.assertEqual(when, datetime(2026, 10, 25, 8, 0, tzinfo=TZ))
        with self.assertRaises(ValueError):
            next_run_for(spec(kind="once", time=None, run_at=NOW - timedelta(minutes=1)), NOW)

    def test_interval_is_relative(self) -> None:
        self.assertEqual(
            next_run_for(spec(kind="interval", time=None, every_minutes=30), NOW),
            NOW + timedelta(minutes=30),
        )


class SpecTests(unittest.TestCase):
    def test_requires_kind_specific_fields(self) -> None:
        with self.assertRaises(ValueError):
            TaskSpec(kind="daily", message="x")
        with self.assertRaises(ValueError):
            TaskSpec(kind="weekly", message="x", time="08:00")
        with self.assertRaises(ValueError):
            TaskSpec(kind="interval", message="x", every_minutes=1)
        with self.assertRaises(ValueError):
            TaskSpec(kind="daily", message="", time="08:00")

    def test_time_is_normalised(self) -> None:
        self.assertEqual(spec(time="8:5").time, "08:05")

    def test_irrelevant_fields_are_dropped(self) -> None:
        moved = spec(run_at=datetime(2026, 10, 25, 0, 0, tzinfo=TZ), every_minutes=30)
        self.assertIsNone(moved.run_at)
        self.assertIsNone(moved.every_minutes)

    def test_parse_json_tolerates_prose(self) -> None:
        parsed = parse_task_json(
            '好的：{"kind":"daily","time":"08:00","message":"该喝水啦"}'
        )
        self.assertEqual(parsed.kind, "daily")
        with self.assertRaises(ValueError):
            parse_task_json("not json")

    def test_target_defaults_to_the_current_chat(self) -> None:
        self.assertEqual(spec().target, "here")
        parsed = TaskSpec(
            kind="once",
            message="x",
            run_at=datetime(2026, 10, 25, 9, 0, tzinfo=TZ),
            target="private",
        )
        self.assertEqual(parsed.target, "private")

    def test_unused_placeholder_fields_are_ignored(self) -> None:
        parsed = parse_task_json(
            '{"kind":"once","run_at":"2026-10-25T09:00:00+08:00",'
            '"time":"","every_minutes":0,"weekday":7,"message":"交报告"}'
        )
        self.assertEqual(parsed.kind, "once")
        self.assertIsNone(parsed.time)
        self.assertIsNone(parsed.every_minutes)
        self.assertIsNone(parsed.weekday)

    def test_resolve_target(self) -> None:
        self.assertEqual(resolve_target("here", "group", 1, 42), ("group", 42))
        self.assertEqual(resolve_target("here", "private", 1, None), ("private", 1))
        self.assertEqual(resolve_target("private", "group", 1, 42), ("private", 1))
        self.assertEqual(resolve_target("group", "group", 1, 42), ("group", 42))
        with self.assertRaises(ValueError):
            resolve_target("group", "private", 1, None)
        record = TaskRecord(
            id=1,
            chat_type="group",
            chat_id=42,
            created_by="1",
            instruction="x",
            message="m",
            kind="daily",
            time_of_day="08:00",
            weekday=None,
            run_at=None,
            every_minutes=None,
            mode="fixed",
            enabled=True,
            created_at=0,
            next_run=0,
            last_run=0,
            runs=0,
        )
        self.assertEqual(describe_target(record), "群 42")

    def test_parse_task_uses_the_client(self) -> None:
        class FakeClient:
            async def chat(self, messages, **kwargs):
                return '{"kind":"weekly","weekday":0,"time":"09:00","message":"新的一周"}'

        parsed, when = asyncio.run(parse_task(FakeClient(), "每周一早上9点", NOW))
        self.assertEqual(parsed.kind, "weekly")
        self.assertEqual(when, datetime(2026, 10, 26, 9, 0, tzinfo=TZ))


class TaskMarkerTests(unittest.TestCase):
    def test_extract_and_strip(self) -> None:
        visible, description = extract_task_marker("好呀。\n[定时: 每天早上8点提醒我起床]")
        self.assertEqual(visible, "好呀。")
        self.assertEqual(description, "每天早上8点提醒我起床")
        self.assertEqual(extract_task_marker("正常回复"), ("正常回复", None))


class TaskRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        from main import Bot

        self.bot = object.__new__(Bot)
        self.bot.create_task = self._create
        self.created: list[str] = []
        self.record = TaskRecord(
            id=3,
            chat_type="private",
            chat_id=1,
            created_by="1",
            instruction="每天早上8点提醒我起床",
            message="起床啦",
            kind="daily",
            time_of_day="08:00",
            weekday=None,
            run_at=None,
            every_minutes=None,
            mode="fixed",
            enabled=True,
            created_at=0,
            next_run=0,
            last_run=0,
            runs=0,
        )

    async def _create(self, event, description):
        self.created.append(description)
        return self.record, datetime(2026, 10, 24, 8, 0, tzinfo=TZ)

    def test_admin_request_creates_and_confirms(self) -> None:
        import asyncio
        from types import SimpleNamespace

        self.bot.is_admin = lambda user_id: True
        event = SimpleNamespace(user_id=1)
        out = asyncio.run(
            self.bot._handle_task_request(event, "好的", "每天早上8点提醒我起床")
        )
        self.assertEqual(self.created, ["每天早上8点提醒我起床"])
        self.assertIn("已创建任务 #3", out)
        self.assertIn("每天 08:00", out)
        self.assertIn("起床啦", out)

    def test_non_admin_request_is_ignored(self) -> None:
        import asyncio
        from types import SimpleNamespace

        self.bot.is_admin = lambda user_id: False
        event = SimpleNamespace(user_id=2)
        out = asyncio.run(
            self.bot._handle_task_request(event, "好的", "每天早上8点提醒我起床")
        )
        self.assertEqual(out, "好的")
        self.assertEqual(self.created, [])


class TaskManagementTests(unittest.TestCase):
    def test_extract_management_markers(self) -> None:
        self.assertEqual(
            extract_task_management("好。\n[定时列表]"),
            ("好。", ("列表", "")),
        )
        self.assertEqual(
            extract_task_management("[定时删除: 喝水]"),
            ("", ("删除", "喝水")),
        )
        self.assertEqual(
            extract_task_management("[定时暂停：3]"), ("", ("暂停", "3"))
        )
        self.assertEqual(extract_task_management("普通回复"), ("普通回复", None))

    def test_spec_mode(self) -> None:
        self.assertEqual(spec().mode, "fixed")
        parsed = parse_task_json(
            '{"kind":"daily","time":"08:00","message":"x","mode":"generate"}'
        )
        self.assertEqual(parsed.mode, "generate")
        with self.assertRaises(ValueError):
            parse_task_json('{"kind":"daily","time":"08:00","message":"x","mode":"no"}')

    def test_render_lists_target_and_mode(self) -> None:
        record = TaskRecord(
            id=5,
            chat_type="group",
            chat_id=42,
            created_by="1",
            instruction="每天喝水",
            message="该喝水啦",
            kind="daily",
            time_of_day="08:00",
            weekday=None,
            run_at=None,
            every_minutes=None,
            mode="generate",
            enabled=True,
            created_at=0,
            next_run=int(NOW.timestamp()),
            last_run=0,
            runs=0,
        )
        text = render_task_list([record])
        self.assertIn("群 42", text)
        self.assertIn("生成", text)
        self.assertIn("该喝水啦", text)


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TaskStore(str(Path(self.tmp.name) / "tasks.db"))

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def add(self, **overrides) -> TaskRecord:
        base = dict(
            chat_type="group",
            chat_id=42,
            created_by="1",
            instruction="每天早上8点提醒大家喝水",
            spec=spec(),
            next_run=NOW + timedelta(hours=1),
        )
        base.update(overrides)
        return self.store.add(**base)

    def test_add_get_list_delete(self) -> None:
        record = self.add()
        self.assertEqual(record.kind, "daily")
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.store.get(record.id).message, "该喝水啦")
        self.assertEqual(describe_record(record), "每天 08:00")
        self.assertTrue(self.store.delete(record.id))
        self.assertEqual(self.store.count(), 0)

    def test_count_for_chat(self) -> None:
        self.add()
        self.add(chat_id=43)
        self.assertEqual(self.store.count_for("group", 42), 1)
        self.assertEqual(self.store.count_for("group", 99), 0)

    def test_due_only_returns_enabled_tasks_that_are_due(self) -> None:
        due = self.add(next_run=NOW - timedelta(minutes=1))
        self.add(next_run=NOW + timedelta(hours=1))
        paused = self.add(next_run=NOW - timedelta(minutes=1))
        self.store.set_enabled(paused.id, False, None)
        self.assertEqual([task.id for task in self.store.due(NOW)], [due.id])

    def test_mark_run_advances_or_disables(self) -> None:
        record = self.add()
        later = NOW + timedelta(days=1)
        self.store.mark_run(record.id, next_run=later, ran_at=NOW)
        updated = self.store.get(record.id)
        self.assertEqual(updated.next_run, int(later.timestamp()))
        self.assertEqual(updated.runs, 1)

        self.store.mark_run(record.id, next_run=None, ran_at=NOW)
        updated = self.store.get(record.id)
        self.assertFalse(updated.enabled)
        self.assertEqual(updated.next_run, 0)


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TaskStore(str(Path(self.tmp.name) / "tasks.db"))
        self.sent: list[tuple[str, int, str]] = []

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    async def send(self, task) -> None:
        self.sent.append((task.chat_type, task.chat_id, task.message))

    def test_tick_fires_and_reschedules(self) -> None:
        record = self.store.add(
            chat_type="group",
            chat_id=42,
            created_by="1",
            instruction="每天早上8点",
            spec=spec(time="08:00"),
            next_run=NOW - timedelta(minutes=5),
        )
        scheduler = TaskScheduler(self.store, self.send)
        fired = asyncio.run(scheduler.tick(NOW))
        self.assertEqual(fired, 1)
        self.assertEqual(self.sent, [("group", 42, "该喝水啦")])
        updated = self.store.get(record.id)
        self.assertEqual(updated.runs, 1)
        self.assertEqual(updated.next_run, int(datetime(2026, 10, 24, 8, 0, tzinfo=TZ).timestamp()))

    def test_once_task_is_disabled_after_firing(self) -> None:
        when = NOW - timedelta(minutes=1)
        record = self.store.add(
            chat_type="private",
            chat_id=7,
            created_by="1",
            instruction="一分钟后提醒我",
            spec=spec(kind="once", time=None, run_at=when),
            next_run=when,
        )
        scheduler = TaskScheduler(self.store, self.send)
        self.assertEqual(asyncio.run(scheduler.tick(NOW)), 1)
        updated = self.store.get(record.id)
        self.assertFalse(updated.enabled)
        self.assertEqual(self.store.due(NOW + timedelta(days=1)), [])

    def test_stale_one_shot_is_dropped(self) -> None:
        when = NOW - timedelta(hours=13)
        record = self.store.add(
            chat_type="private",
            chat_id=7,
            created_by="1",
            instruction="很久以前的一次性提醒",
            spec=spec(kind="once", time=None, run_at=when),
            next_run=when,
        )
        scheduler = TaskScheduler(self.store, self.send)
        self.assertEqual(asyncio.run(scheduler.tick(NOW)), 0)
        updated = self.store.get(record.id)
        self.assertFalse(updated.enabled)
        self.assertEqual(self.sent, [])

    def test_send_failure_still_advances_the_task(self) -> None:
        record = self.store.add(
            chat_type="group",
            chat_id=42,
            created_by="1",
            instruction="每天早上8点",
            spec=spec(time="08:00"),
            next_run=NOW - timedelta(minutes=5),
        )

        async def boom(task):
            raise RuntimeError("network down")

        scheduler = TaskScheduler(self.store, boom)
        self.assertEqual(asyncio.run(scheduler.tick(NOW)), 0)
        updated = self.store.get(record.id)
        self.assertEqual(updated.runs, 1)
        self.assertGreater(updated.next_run, int(NOW.timestamp()))

    def test_tick_is_gated_on_connectivity(self) -> None:
        record = self.store.add(
            chat_type="group",
            chat_id=42,
            created_by="1",
            instruction="每天早上8点",
            spec=spec(time="08:00"),
            next_run=NOW - timedelta(minutes=5),
        )
        scheduler = TaskScheduler(self.store, self.send, ready=lambda: False)
        self.assertEqual(asyncio.run(scheduler.tick(NOW)), 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store.get(record.id).runs, 0)

    def test_paused_task_is_skipped_even_if_snapshotted(self) -> None:
        record = self.store.add(
            chat_type="group",
            chat_id=42,
            created_by="1",
            instruction="每天早上8点",
            spec=spec(time="08:00"),
            next_run=NOW - timedelta(minutes=5),
        )
        snapshot = [record]
        self.store.due = lambda now: snapshot  # stale snapshot from a concurrent tick
        self.store.set_enabled(record.id, False, None)
        scheduler = TaskScheduler(self.store, self.send)
        self.assertEqual(asyncio.run(scheduler.tick(NOW)), 0)
        self.assertEqual(self.sent, [])

    def test_spec_round_trip(self) -> None:
        record = self.store.add(
            chat_type="group",
            chat_id=42,
            created_by="1",
            instruction="每 30 分钟",
            spec=spec(kind="interval", time=None, every_minutes=30),
            next_run=NOW,
        )
        rebuilt = spec_of(record)
        self.assertEqual(rebuilt.kind, "interval")
        self.assertEqual(rebuilt.every_minutes, 30)


if __name__ == "__main__":
    unittest.main()
