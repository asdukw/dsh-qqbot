from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from memory import Memory  # noqa: E402
from memory_archive import MemoryArchive, MemoryArchiveSettings, day_bounds, day_of  # noqa: E402
from sessions import SessionStore  # noqa: E402


def extraction() -> str:
    return json.dumps(
        {
            'summary': '聊了咖啡和周末计划。',
            'topics': ['咖啡', '周末'],
            'open_loops': ['约好下次一起去咖啡店'],
            'user_facts': [
                {
                    'user_id': '7',
                    'name': '小明',
                    'key': '喜好',
                    'value': '喜欢手冲咖啡',
                    'kind': 'preference',
                    'importance': 4,
                    'visibility': 'private',
                }
            ],
            'group_facts': [
                {'key': '群主题', 'value': '咖啡店', 'importance': 2}
            ],
        },
        ensure_ascii=False,
    )


class MemoryArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'memory.db'
        self.memory = Memory(self.path)
        self.calls: list[tuple[str, str]] = []

        async def generate(conversation_id: str, prompt: str) -> str:
            self.calls.append((conversation_id, prompt))
            return extraction()

        self.archive = MemoryArchive(
            self.path,
            MemoryArchiveSettings(sample_messages=50, max_input_chars=4000),
            generate=generate,
        )
        now = int(time.time())
        await self.memory.add_event('group:1', 'group', 1, 7, '小明', '我最近喜欢手冲咖啡', now - 60)
        await self.memory.add_event('group:1', 'group', 1, 8, '小红', '周末去咖啡店吗', now - 30)

    async def asyncTearDown(self) -> None:
        self.archive.close()
        self.memory.close()
        self.tmp.cleanup()

    def day(self) -> str:
        return day_of(time.time())

    async def test_archive_window_writes_day_and_items(self) -> None:
        now = int(time.time())
        self.assertTrue(
            await self.archive.archive_window('group:1', now - 3600, now, session_id='s1')
        )
        with closing(sqlite3.connect(self.path)) as db:
            day = db.execute('SELECT summary FROM memory_days').fetchone()
            items = db.execute(
                'SELECT scope,user_id,key,value,visibility FROM memory_items ORDER BY scope'
            ).fetchall()
        self.assertIn('咖啡', day[0])
        self.assertEqual([row[0] for row in items], ['group', 'user'])
        self.assertEqual(items[0][2], '群主题')
        self.assertEqual(items[1][1], '7')
        self.assertEqual(items[1][4], 'public')  # group facts are always public

    async def test_search_and_person_scope(self) -> None:
        now = int(time.time())
        await self.archive.archive_window('group:1', now - 3600, now, session_id='s1')
        group = self.archive.search(
            'group:1', query='咖啡', user_id='7', is_group=True, group_id='1'
        )
        self.assertTrue(any(row['kind'] == 'day' for row in group))
        self.assertTrue(any(row.get('key') == '群主题' for row in group))
        person = self.archive.user_items(
            '7', group_id='1', is_group=True, query='手冲'
        )
        self.assertEqual(len(person), 1)
        self.assertEqual(person[0]['value'], '喜欢手冲咖啡')
        private = self.archive.search(
            'private:7', user_id='7', is_group=False
        )
        self.assertEqual(private, [])

    async def test_same_session_is_not_duplicated(self) -> None:
        now = int(time.time())
        await self.archive.archive_window('group:1', now - 3600, now, session_id='s1')
        await self.archive.archive_window('group:1', now - 3600, now, session_id='s1')
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM memory_days').fetchone()[0], 1)
        self.assertEqual(len(self.calls), 1)

    async def test_changed_fact_invalidates_old_value(self) -> None:
        now = int(time.time())
        await self.archive.archive_window('group:1', now - 3600, now, session_id='s1')
        await self.memory.add_event('group:1', 'group', 1, 7, '小明', '现在改喝美式了', now)
        original = json.loads(extraction())
        original['user_facts'][0]['value'] = '改喝美式'

        async def changed(_conversation: str, _prompt: str) -> str:
            return json.dumps(original, ensure_ascii=False)

        self.archive._generate = changed
        await self.archive.archive_window('group:1', now, now + 1, session_id='s2')
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute(
                "SELECT value,invalid_at FROM memory_items WHERE scope='user' ORDER BY created_at"
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], '喜欢手冲咖啡')
        self.assertIsNotNone(rows[0][1])
        self.assertEqual(rows[1][0], '改喝美式')
        self.assertIsNone(rows[1][1])

    async def test_archive_day_skips_existing_and_backfills_missing(self) -> None:
        yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
        start, _ = day_bounds(yesterday)
        await self.memory.add_event('group:2', 'group', 2, 9, '阿蓝', '昨天的记录', start + 60)
        missing = self.archive.missing_days(3)
        self.assertIn(('group:2', yesterday), missing)
        self.assertTrue(await self.archive.archive_day('group:2', yesterday))
        self.assertNotIn(('group:2', yesterday), self.archive.missing_days(3))
        self.assertFalse(await self.archive.archive_day('group:2', yesterday))

    async def test_group_person_scope_excludes_other_members(self) -> None:
        now = int(time.time())
        await self.memory.add_event('group:1', 'group', 1, 8, '小红', '我喜欢美式', now - 10)
        result = {
            'summary': '两个人聊了咖啡。',
            'topics': ['咖啡'],
            'open_loops': [],
            'user_facts': [
                {'user_id': '7', 'key': '喜好', 'value': '喜欢手冲咖啡'},
                {'user_id': '8', 'key': '喜好', 'value': '喜欢美式'},
            ],
            'group_facts': [{'key': '群主题', 'value': '咖啡店'}],
        }

        async def both(_conversation: str, _prompt: str) -> str:
            return json.dumps(result, ensure_ascii=False)

        self.archive._generate = both
        await self.archive.archive_window('group:1', now - 3600, now, session_id='s2')
        seven = self.archive.user_items('7', group_id='1', is_group=True)
        eight = self.archive.user_items('8', group_id='1', is_group=True)
        self.assertEqual([row['value'] for row in seven], ['喜欢手冲咖啡'])
        self.assertEqual([row['value'] for row in eight], ['喜欢美式'])

    async def test_rotation_splits_at_midnight_and_occurred_at_stays_in_day(self) -> None:
        yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
        today = datetime.now().date().isoformat()
        start, _ = day_bounds(yesterday)
        await self.memory.add_event('group:3', 'group', 3, 9, '阿蓝', '午夜前的记录', start + 3600)
        await self.memory.add_event('group:3', 'group', 3, 9, '阿蓝', '午夜后的记录', start + 86400 + 60)
        async def night(_conversation: str, _prompt: str) -> str:
            return json.dumps({
                'summary': '跨午夜的一段记录。',
                'topics': [],
                'user_facts': [
                    {'user_id': '9', 'key': '记录', 'value': '午夜前的记录'},
                ],
            }, ensure_ascii=False)

        self.archive._generate = night
        await self.archive.archive_range(
            'group:3', start + 3600, start + 86400 + 120, session_id='multi'
        )
        with closing(sqlite3.connect(self.path)) as db:
            days = db.execute(
                'SELECT day FROM memory_days WHERE conversation_id=? ORDER BY day', ('group:3',)
            ).fetchall()
            occurred = db.execute(
                "SELECT occurred_at FROM memory_items WHERE user_id='9' ORDER BY occurred_at"
            ).fetchall()
        self.assertEqual([row[0] for row in days], [yesterday, today])
        self.assertTrue(occurred)
        self.assertLess(int(occurred[0][0]), start + 86400)

    async def test_recent_context_is_bounded(self) -> None:
        now = int(time.time())
        await self.archive.archive_window('group:1', now - 3600, now, session_id='s1')
        text = self.archive.recent_context(
            'group:1', user_id='7', is_group=True, group_id='1', days=7
        )
        self.assertIn('近 7 天记忆', text)
        self.assertIn('咖啡', text)


class SessionRotateHookTests(unittest.TestCase):
    def test_reset_notifies_the_previous_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / 'memory.db', daily_hour=-1)
            calls: list[tuple[str, int, str]] = []
            store.on_rotate = lambda conversation, state: calls.append(
                (conversation, state.generation, state.session_id)
            )
            try:
                store.reset('group:1')
            finally:
                store.close()
            self.assertEqual(calls, [('group:1', 0, 'group-1')])


if __name__ == '__main__':
    unittest.main()
