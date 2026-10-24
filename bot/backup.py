"""Back up the bot's SQLite databases into one dated archive.

Run by qqbot-backup.timer as the qqbot user. Uses SQLite's online backup API,
so it is safe while the bot is writing (WAL).
"""

from __future__ import annotations

import argparse
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path

DEFAULT_DATABASES = (
    "/var/lib/qqbot/models.db",
    "/var/lib/qqbot/characters.db",
    "/var/lib/qqbot/memory.db",
    "/var/lib/qqbot/stickers.db",
    "/var/lib/qqbot/tasks.db",
    "/var/lib/qqbot/usage.db",
    "/var/lib/qqbot/dsh-state/profile.db",
)
DEFAULT_OUTPUT = "/var/lib/qqbot/backups"
DEFAULT_KEEP_DAYS = 14
PREFIX = "qqbot-backup-"


def backup_database(source: Path, target: Path) -> None:
    """Consistent snapshot even while the bot holds the database open."""
    src = sqlite3.connect(source, timeout=10)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def prune(directory: Path, keep_days: int, now: float | None = None) -> list[Path]:
    cutoff = (now if now is not None else time.time()) - keep_days * 86400
    removed: list[Path] = []
    for path in directory.glob(f"{PREFIX}*.tar.gz"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path)
        except OSError:
            continue
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="back up qqbot SQLite databases")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--keep-days", type=int, default=DEFAULT_KEEP_DAYS)
    parser.add_argument("database", nargs="*", default=list(DEFAULT_DATABASES))
    args = parser.parse_args(argv)

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    archive = output / f"{PREFIX}{stamp}.tar.gz"

    with tempfile.TemporaryDirectory(prefix="qqbot-backup-") as tmp:
        work = Path(tmp)
        copied: list[str] = []
        for name in args.database:
            source = Path(name)
            if not source.is_file():
                continue
            target = work / source.name
            try:
                backup_database(source, target)
            except sqlite3.Error as exc:
                print(f"skip {source}: {exc}")
                continue
            copied.append(source.name)
        if not copied:
            print("no databases found; nothing to back up")
            return 1
        with tarfile.open(archive, "w:gz") as tar:
            for name in copied:
                tar.add(work / name, arcname=name)

    removed = prune(output, args.keep_days)
    print(f"backup: {archive} ({len(copied)} database(s))")
    if removed:
        print(f"pruned: {len(removed)} archive(s) older than {args.keep_days} day(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
