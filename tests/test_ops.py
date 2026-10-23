from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

import alert  # noqa: E402
from backup import backup_database, prune  # noqa: E402


class BackupTests(unittest.TestCase):
    def test_backup_copies_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.db"
            target = root / "target.db"
            db = sqlite3.connect(source)
            db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")
            db.execute("INSERT INTO t (value) VALUES ('hello')")
            db.commit()
            db.close()

            backup_database(source, target)
            copy = sqlite3.connect(target)
            try:
                self.assertEqual(
                    copy.execute("SELECT value FROM t").fetchone()[0], "hello"
                )
            finally:
                copy.close()

    def test_prune_drops_only_old_archives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "qqbot-backup-20260101-000000.tar.gz"
            new = root / "qqbot-backup-20260102-000000.tar.gz"
            other = root / "keep-me.txt"
            for path in (old, new, other):
                path.write_text("x")
            now = time.time()
            os.utime(old, (now - 40 * 86400, now - 40 * 86400))
            os.utime(new, (now, now))
            removed = prune(root, keep_days=14, now=now)
            self.assertEqual(removed, [old])
            self.assertFalse(old.exists())
            self.assertTrue(new.exists())
            self.assertTrue(other.exists())


class AlertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_admin_ids_from_env(self) -> None:
        ids = alert.admin_ids({"ADMIN_QQ_IDS": "1, 2 3"}, {})
        self.assertEqual(ids, [1, 2, 3])

    def test_admin_ids_from_config(self) -> None:
        ids = alert.admin_ids({}, {"bot": {"admins": [7, 8]}})
        self.assertEqual(ids, [7, 8])

    def test_admin_ids_ignores_junk(self) -> None:
        self.assertEqual(alert.admin_ids({"ADMIN_QQ_IDS": "abc,"}, {}), [])

    def test_failure_text_reports_systemd_result(self) -> None:
        old = dict(os.environ)
        try:
            os.environ.update(
                {"SERVICE_RESULT": "start-limit-hit", "EXIT_CODE": "signal", "EXIT_STATUS": "9"}
            )
            text = alert.failure_text()
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertIn("start-limit-hit", text)
        self.assertIn("signal", text)

    def test_cooldown(self) -> None:
        state = Path(self.tmp.name) / "state"
        original = alert.STATE
        alert.STATE = str(state)
        try:
            self.assertTrue(alert.cooldown_ok(600, force=False))
            alert.mark_sent()
            self.assertFalse(alert.cooldown_ok(600, force=False))
            self.assertTrue(alert.cooldown_ok(600, force=True))
            state.write_text(str(time.time() - 700), encoding="utf-8")
            self.assertTrue(alert.cooldown_ok(600, force=False))
        finally:
            alert.STATE = original


if __name__ == "__main__":
    unittest.main()
