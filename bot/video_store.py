"""Small durable receipt ledger: acquisition failure is not unknown delivery."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from video_platforms import VideoError


class VideoStore:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        Path(path).chmod(0o600)
        self.db.execute('''CREATE TABLE IF NOT EXISTS video_deliveries (
            conversation TEXT NOT NULL, message INTEGER NOT NULL, identity TEXT NOT NULL,
            status TEXT NOT NULL, receipt INTEGER, updated REAL NOT NULL,
            PRIMARY KEY(conversation,message))''')
        self.db.execute("UPDATE video_deliveries SET status='unknown' WHERE status='sending'")
        self.db.execute("UPDATE video_deliveries SET status='acquisition_failed' WHERE status='preparing'")
        self.db.commit()

    def claim(self, conversation: str, message: int, identity: str) -> dict | None:
        now = time.time()
        with self.db:
            self.db.execute("DELETE FROM video_deliveries WHERE updated < ? AND status NOT IN ('preparing','sending')",
                            (now - 30 * 86400,))
            existing = self.db.execute('SELECT * FROM video_deliveries WHERE conversation=? AND message=?',
                                       (conversation, message)).fetchone()
            if existing is not None:
                return dict(existing)
            if self.db.execute('SELECT count(*) FROM video_deliveries').fetchone()[0] >= 10000:
                raise VideoError('BUSY', 'Video receipt ledger is full')
            inserted = self.db.execute("INSERT OR IGNORE INTO video_deliveries VALUES(?,?,?,'preparing',NULL,?)",
                                       (conversation, message, identity, now)).rowcount
            if inserted:
                return None
            return dict(self.db.execute('SELECT * FROM video_deliveries WHERE conversation=? AND message=?',
                                        (conversation, message)).fetchone())

    def set(self, conversation: str, message: int, status: str, receipt: int | None = None):
        with self.db:
            self.db.execute('UPDATE video_deliveries SET status=?,receipt=?,updated=? WHERE conversation=? AND message=?',
                            (status, receipt, time.time(), conversation, message))

    def close(self):
        self.db.close()
