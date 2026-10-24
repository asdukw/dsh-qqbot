"""Persistent chat-image store used by the DSH placeholder path.

Images are deduplicated by SHA-1 and written atomically under the DSH
workspace, so the model can read the original later with the file tool while
the session history only carries a short description and a relative path.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path

MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


@dataclass(frozen=True)
class StoredImage:
    sha1: str
    path: Path
    relative: str
    mime: str
    size: int


class ImageStore:
    """Write chat images under ``<workspace>/<subdir>`` and prune old files."""

    def __init__(
        self,
        workspace: str | Path,
        subdir: str = "images",
        *,
        max_bytes: int = 8 * 1024 * 1024,
        keep_days: int = 14,
        max_files: int = 1000,
    ):
        self.workspace = Path(workspace)
        self.subdir = str(subdir or "").strip().strip("/") or ""
        self.root = self.workspace / self.subdir if self.subdir else self.workspace
        self.max_bytes = max(1024, int(max_bytes))
        self.keep_days = max(0, int(keep_days))
        self.max_files = max(0, int(max_files))
        self.root.mkdir(parents=True, exist_ok=True)
        self.prune()

    def relative(self, name: str) -> str:
        return f"{self.subdir}/{name}" if self.subdir else name

    def save(self, data: bytes, mime: str) -> StoredImage:
        if not data:
            raise ValueError("empty image")
        if len(data) > self.max_bytes:
            raise ValueError(f"image is {len(data)} bytes, over {self.max_bytes}")
        digest = hashlib.sha1(data).hexdigest()
        ext = MIME_EXT.get((mime or "").lower(), ".img")
        name = f"{digest}{ext}"
        target = self.root / name
        if not target.exists():
            tmp = self.root / f".{name}.{os.getpid()}.tmp"
            try:
                tmp.write_bytes(data)
                tmp.replace(target)
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass
        return StoredImage(digest, target, self.relative(name), mime, len(data))

    def prune(self) -> None:
        """Drop files older than ``keep_days``, then the oldest over ``max_files``."""
        now = time.time()
        cutoff = now - self.keep_days * 86400 if self.keep_days else 0
        files: list[tuple[float, Path]] = []
        try:
            entries = list(self.root.iterdir())
        except OSError:
            return
        for path in entries:
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
            except OSError:
                continue
            if cutoff and stat.st_mtime < cutoff:
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            files.append((stat.st_mtime, path))
        if self.max_files and len(files) > self.max_files:
            files.sort()
            for _, path in files[: len(files) - self.max_files]:
                try:
                    path.unlink()
                except OSError:
                    pass
