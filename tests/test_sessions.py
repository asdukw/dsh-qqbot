from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from sessions import TZ, SessionStore  # noqa: E402


class SessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "memory.db"
        self.store = SessionStore(
            self.path,
            idle_hours=24,
            idle_hours_private=168,
            max_hours=0,
            max_messages=0,
            daily_hour=4,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_first_session_is_generation_zero(self) -> None:
        state, rotated = self.store.current("group:7", is_group=True, now=1_000_000)
        self.assertFalse(rotated)
        self.assertEqual(state.generation, 0)
        self.assertEqual(state.session_id, "group-7")
        self.assertEqual(state.messages, 0)

    def test_group_idle_rotation(self) -> None:
        now = 1_000_000
        self.store.current("group:7", is_group=True, now=now)
        self.store.touch("group:7", now=now)
        state, rotated = self.store.current(
            "group:7", is_group=True, now=now + 24 * 3600 + 1
        )
        self.assertTrue(rotated)
        self.assertEqual(state.generation, 1)
        self.assertEqual(state.session_id, "group-7-g1")
        self.assertEqual(state.messages, 0)

    def test_private_idle_uses_separate_budget(self) -> None:
        now = 1_000_000
        self.store.current("private:7", is_group=False, now=now)
        self.store.touch("private:7", now=now)
        state, rotated = self.store.current(
            "private:7", is_group=False, now=now + 24 * 3600
        )
        self.assertFalse(rotated)
        self.assertEqual(state.session_id, "private-7")
        state, rotated = self.store.current(
            "private:7", is_group=False, now=now + 168 * 3600 + 1
        )
        self.assertTrue(rotated)
        self.assertEqual(state.session_id, "private-7-g1")

    def test_daily_rotation_after_quiet_hour(self) -> None:
        started = int(datetime(2026, 10, 23, 10, 0, tzinfo=TZ).timestamp())
        self.store.current("group:7", is_group=True, now=started)
        # Same day, after midnight but before 04:00: no daily rotate.
        before = int(datetime(2026, 10, 24, 3, 0, tzinfo=TZ).timestamp())
        state, rotated = self.store.current("group:7", is_group=True, now=before)
        self.assertFalse(rotated)
        self.assertEqual(state.session_id, "group-7")
        # First message after 04:00 next day rotates.
        after = int(datetime(2026, 10, 24, 4, 1, tzinfo=TZ).timestamp())
        state, rotated = self.store.current("group:7", is_group=True, now=after)
        self.assertTrue(rotated)
        self.assertEqual(state.session_id, "group-7-g1")
        # Later the same day stays on the new generation.
        later = int(datetime(2026, 10, 24, 12, 0, tzinfo=TZ).timestamp())
        state, rotated = self.store.current("group:7", is_group=True, now=later)
        self.assertFalse(rotated)
        self.assertEqual(state.session_id, "group-7-g1")

    def test_max_messages_rotation(self) -> None:
        self.store.max_messages = 2
        now = 1_000_000
        self.store.current("group:7", is_group=True, now=now)
        self.store.touch("group:7", now=now)
        self.store.touch("group:7", now=now + 1)
        state, rotated = self.store.current("group:7", is_group=True, now=now + 2)
        self.assertTrue(rotated)
        self.assertEqual(state.session_id, "group-7-g1")

    def test_seed_refreshes_an_unseeded_generation_zero_row(self) -> None:
        now = 1_000_000
        self.store.current("group:7", is_group=True, now=now)
        changed = self.store.seed_existing(
            "group:7",
            started_at=now - 2 * 86400,
            last_active=now - 86400,
            messages=5,
        )
        self.assertTrue(changed)
        state = self.store.info("group:7")
        assert state is not None
        self.assertEqual(state.started_at, now - 2 * 86400)
        self.assertEqual(state.last_active, now - 86400)
        self.assertEqual(state.messages, 5)
        # Once it has turns, a later seed must not overwrite live state.
        self.assertFalse(
            self.store.seed_existing(
                "group:7", started_at=now, last_active=now, messages=0
            )
        )

    def test_seed_existing_uses_real_timestamps(self) -> None:
        now = 1_000_000
        self.store.seed_existing(
            "group:7", started_at=now - 2 * 86400, last_active=now - 2 * 86400
        )
        state, rotated = self.store.current("group:7", is_group=True, now=now)
        self.assertTrue(rotated)
        self.assertEqual(state.session_id, "group-7-g1")

    def test_manual_reset_increments_generation(self) -> None:
        now = 1_000_000
        self.store.current("group:7", is_group=True, now=now)
        first = self.store.reset("group:7", now=now)
        second = self.store.reset("group:7", now=now)
        self.assertEqual(first.session_id, "group-7-g1")
        self.assertEqual(second.session_id, "group-7-g2")


if __name__ == "__main__":
    unittest.main()
