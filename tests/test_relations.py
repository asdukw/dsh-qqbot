from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from relations import (  # noqa: E402
    RelationMarkers,
    RelationStore,
    affinity_bar,
    extract_relation_markers,
    render_relation,
    tier_for,
)


class MarkerTests(unittest.TestCase):
    def test_extracts_and_strips_all_markers(self) -> None:
        visible, markers = extract_relation_markers(
            "谢谢你！[好感+2][印象: 有礼貌，会道谢][关系: 损友]"
        )
        self.assertEqual(visible, "谢谢你！")
        self.assertEqual(markers.delta, 2)
        self.assertEqual(markers.impression, "有礼貌，会道谢")
        self.assertEqual(markers.relationship, "损友")

    def test_delta_formats_and_multiple_markers(self) -> None:
        for raw, delta in (
            ("[好感度 +3]", 3),
            ("[好感：-2]", -2),
            ("[好感1]", 1),
            ("[好感度: -1]", -1),
        ):
            visible, markers = extract_relation_markers(f"好{raw}")
            self.assertEqual(visible, "好")
            self.assertEqual(markers.delta, delta)
        visible, markers = extract_relation_markers(
            "a[好感+1]b[好感+2][印象: 一][印象: 二]"
        )
        self.assertEqual(visible, "ab")
        self.assertEqual(markers.delta, 3)
        self.assertEqual(markers.impression, "二")

    def test_plain_text_is_untouched(self) -> None:
        visible, markers = extract_relation_markers("普通回复")
        self.assertEqual(visible, "普通回复")
        self.assertFalse(markers.has_update)

    def test_marker_only_reply_becomes_empty(self) -> None:
        visible, markers = extract_relation_markers("[好感+2][印象: 有礼貌]")
        self.assertEqual(visible, "")
        self.assertTrue(markers.has_update)


class TierTests(unittest.TestCase):
    def test_tiers_and_bar(self) -> None:
        self.assertEqual(tier_for(0), "陌生")
        self.assertEqual(tier_for(20), "认识")
        self.assertEqual(tier_for(45), "朋友")
        self.assertEqual(tier_for(65), "亲近")
        self.assertEqual(tier_for(90), "亲密")
        self.assertEqual(affinity_bar(0), "░" * 10)
        self.assertEqual(affinity_bar(100), "█" * 10)


class RelationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "profile.db"
        self.store = RelationStore(
            self.path,
            start=20,
            max_delta=3,
            cooldown_seconds=60,
            max_changes_per_day=3,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_touch_creates_a_default_record(self) -> None:
        self.store.touch(7, "小明")
        record = self.store.get(7)
        assert record is not None
        self.assertEqual(record.affinity, 20)
        self.assertEqual(record.tier, "认识")
        self.assertEqual(record.display_name, "小明")
        self.assertEqual(self.store.count(), 1)

    def test_apply_clamps_and_respects_cooldown(self) -> None:
        self.store.touch(7, "小明")
        record = self.store.apply(
            7,
            RelationMarkers(delta=10, impression="有礼貌", relationship="损友"),
            now=1000,
        )
        assert record is not None
        self.assertEqual(record.affinity, 23)  # max_delta = 3
        self.assertEqual(record.impression, "有礼貌")
        self.assertEqual(record.relationship, "损友")
        self.assertIsNone(self.store.apply(7, RelationMarkers(delta=1), now=1030))
        record = self.store.apply(7, RelationMarkers(delta=-10), now=1100)
        assert record is not None
        self.assertEqual(record.affinity, 20)

    def test_group_and_private_impressions_are_separate(self) -> None:
        self.store.touch(7, "小明")
        record = self.store.apply(
            7,
            RelationMarkers(delta=1, impression="公开印象", relationship="损友"),
            now=1000,
            scope="group",
        )
        assert record is not None
        self.assertEqual(record.public_impression, "公开印象")
        self.assertEqual(record.impression, "")
        record = self.store.apply(
            7, RelationMarkers(delta=1, impression="私下印象"), now=1100
        )
        assert record is not None
        self.assertEqual(record.public_impression, "公开印象")
        self.assertEqual(record.impression, "私下印象")
        self.assertEqual(record.affinity, 22)

    def test_prompt_context_scope(self) -> None:
        self.store.touch(7, "小明")
        self.store.apply(
            7,
            RelationMarkers(impression="公开印象", relationship="损友"),
            now=1000,
            scope="group",
        )
        self.store.apply(7, RelationMarkers(impression="私下印象"), now=1100)
        private = self.store.prompt_context(7)
        group = self.store.prompt_context(7, scope="group")
        self.assertIn("私下印象", private)
        self.assertNotIn("公开印象", private)
        self.assertIn("关系：损友", private)
        self.assertIn("公开印象", group)
        self.assertNotIn("私下印象", group)
        self.assertNotIn("关系：损友", group)

    def test_concurrent_apply_is_serialized(self) -> None:
        self.store.close()
        self.store = RelationStore(
            self.path,
            start=20,
            max_delta=3,
            cooldown_seconds=0,
            max_changes_per_day=10,
        )
        self.store.touch(7, "小明")
        barrier = threading.Barrier(2)

        def worker(delta: int) -> None:
            barrier.wait()
            self.store.apply(7, RelationMarkers(delta=delta), now=1000)

        threads = [threading.Thread(target=worker, args=(d,)) for d in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        record = self.store.get(7)
        assert record is not None
        self.assertEqual(record.affinity, 23)
        self.assertEqual(record.updates_today, 2)

    def test_affinity_stays_within_bounds(self) -> None:
        self.store.close()
        self.store = RelationStore(
            self.path,
            start=20,
            max_delta=3,
            cooldown_seconds=0,
            max_changes_per_day=3,
        )
        self.store.touch(7, "小明")
        self.store.set_affinity(7, 100)
        record = self.store.apply(7, RelationMarkers(delta=5), now=1000)
        assert record is not None
        self.assertEqual(record.affinity, 100)
        record = self.store.set_affinity(7, -5)
        self.assertEqual(record.affinity, 0)

    def test_daily_change_cap_and_day_rollover(self) -> None:
        self.store.close()
        self.store = RelationStore(
            self.path,
            start=20,
            max_delta=3,
            cooldown_seconds=0,
            max_changes_per_day=1,
        )
        self.store.touch(7, "小明")
        now = 1_800_000_000
        self.assertIsNotNone(self.store.apply(7, RelationMarkers(delta=1), now=now))
        self.assertIsNone(self.store.apply(7, RelationMarkers(delta=1), now=now + 10))
        # The counter resets on the next Asia/Shanghai day.
        self.assertIsNotNone(
            self.store.apply(7, RelationMarkers(delta=1), now=now + 86400)
        )

    def test_touch_preserves_affinity_and_impression(self) -> None:
        self.store.touch(7, "小明")
        record = self.store.apply(
            7, RelationMarkers(delta=2, impression="有礼貌"), now=1000
        )
        assert record is not None
        self.store.touch(7, "小明改名")
        again = self.store.get(7)
        assert again is not None
        self.assertEqual(again.affinity, record.affinity)
        self.assertEqual(again.impression, "有礼貌")
        self.assertEqual(again.display_name, "小明改名")

    def test_reset_removes_the_record(self) -> None:
        self.store.touch(7, "小明")
        self.assertTrue(self.store.reset(7))
        self.assertIsNone(self.store.get(7))
        self.assertFalse(self.store.reset(7))

    def test_facts_are_group_scoped(self) -> None:
        conn = sqlite3.connect(self.path)
        conn.execute(
            "CREATE TABLE user_facts (user_id TEXT, group_id TEXT NOT NULL DEFAULT '', "
            "key TEXT, value TEXT, visibility TEXT, updated_at INTEGER, "
            "PRIMARY KEY (user_id, group_id, key))"
        )
        conn.executemany(
            "INSERT INTO user_facts VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("7", "", "city", "杭州", "public", 2),
                ("7", "", "secret", "怕黑", "private", 1),
                ("7", "42", "weather", "长春", "public", 3),
                ("7", "43", "weather", "北京", "public", 4),
            ],
        )
        conn.commit()
        conn.close()
        self.assertEqual(self.store.facts(7), [("city", "杭州")])
        self.assertEqual(
            self.store.facts(7, include_private=True),
            [("city", "杭州"), ("secret", "怕黑")],
        )
        self.assertEqual(
            self.store.facts(7, group_id="42"),
            [("weather", "长春"), ("city", "杭州")],
        )
        self.assertEqual(
            self.store.facts(7, group_id="43"),
            [("weather", "北京"), ("city", "杭州")],
        )

    def test_facts_fall_back_to_source_before_migration(self) -> None:
        conn = sqlite3.connect(self.path)
        conn.execute(
            "CREATE TABLE user_facts (user_id TEXT, key TEXT, value TEXT, "
            "visibility TEXT, source TEXT, updated_at INTEGER, "
            "PRIMARY KEY (user_id, key))"
        )
        conn.executemany(
            "INSERT INTO user_facts VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("7", "city", "杭州", "public", None, 2),
                ("7", "secret", "怕黑", "private", "private", 1),
                ("7", "weather", "长春", "public", "group:42", 3),
            ],
        )
        conn.commit()
        conn.close()
        self.assertEqual(self.store.facts(7), [("city", "杭州")])
        self.assertEqual(
            self.store.facts(7, group_id="42"),
            [("weather", "长春"), ("city", "杭州")],
        )
        self.assertEqual(self.store.facts(7, group_id="43"), [("city", "杭州")])

    def test_render_scopes_impressions(self) -> None:
        self.store.touch(7, "小明")
        record = self.store.apply(
            7,
            RelationMarkers(delta=1, impression="公开印象", relationship="损友"),
            now=1000,
            scope="group",
        )
        record = self.store.apply(
            7, RelationMarkers(impression="私下印象"), now=1100
        )
        assert record is not None
        full_private = render_relation(record, "小明", [], full=True)
        full_group = render_relation(record, "小明", [], full=True, scope="group")
        limited = render_relation(
            record, "QQ 7", [("city", "杭州")], full=False, scope="group"
        )
        self.assertIn("私下印象", full_private)
        self.assertIn("损友", full_private)
        self.assertIn("公开印象", full_group)
        self.assertNotIn("私下印象", full_group)
        self.assertNotIn("损友", full_group)
        self.assertNotIn("私下印象", limited)
        self.assertNotIn("公开印象", limited)
        self.assertIn("公开画像", limited)
