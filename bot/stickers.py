"""SQLite-backed sticker (表情包) library learned from group chat.

Phase 1: keep what the groups post and let an admin inspect it. Sending and
vision tagging sit on top of this store; the schema already has room for a
description/tags so they do not need a migration.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import random
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

MAX_BYTES = 8 * 1024 * 1024

# Magic bytes are checked instead of trusting the URL: QQ image URLs often carry
# no extension and the Content-Type header is sometimes generic.
_SIGNATURES = (
    (bytes.fromhex("89504e470d0a1a0a"), "image/png", ".png"),
    (bytes.fromhex("ffd8ff"), "image/jpeg", ".jpg"),
    (bytes.fromhex("474946383961"), "image/gif", ".gif"),
    (bytes.fromhex("474946383761"), "image/gif", ".gif"),
)

VALID_STATUS = ("ok", "banned")

# The model asks to send a sticker with a trailing "[表情: 无语]" line.
_MARKER_RE = re.compile(r"\[表情[:：]\s*([^\]\n]{0,24})\]")


def extract_sticker_marker(text: str) -> tuple[str, str | None]:
    """Split "[表情: hint]" out of a reply; return (visible text, hint)."""
    match = _MARKER_RE.search(text or "")
    if match is None:
        return text, None
    hint = match.group(1).strip()
    cleaned = _MARKER_RE.sub("", text, count=1)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, hint


def parse_tags(raw: str) -> tuple[str, tuple[str, ...], bool]:
    """Read {"desc", "tags", "safe"} out of a model reply, tolerantly.

    `safe` defaults to True so a model that omits it cannot ban everything.
    """
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return raw.strip()[:200], (), True
    try:
        payload = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return raw.strip()[:200], (), True
    if not isinstance(payload, dict):
        return raw.strip()[:200], (), True
    desc = str(payload.get("desc") or payload.get("description") or "").strip()
    raw_tags = payload.get("tags") or payload.get("emotions") or []
    tags = (
        tuple(str(tag).strip() for tag in raw_tags if str(tag).strip())[:5]
        if isinstance(raw_tags, list)
        else ()
    )
    safe = payload.get("safe", True)
    return (desc or raw.strip()[:200]), tags, bool(safe)


class StickerError(RuntimeError):
    pass


def preview_jpeg(data: bytes, max_px: int = 512) -> bytes | None:
    """First frame as a small JPEG, for vision calls with request size limits."""
    try:
        from PIL import Image, ImageOps
    except ImportError:  # pragma: no cover - Pillow ships with the bot
        return None
    from io import BytesIO

    try:
        with Image.open(BytesIO(data)) as opened:
            if opened.width * opened.height > 24_000_000:
                return None
            opened.seek(0)
            image = (
                ImageOps.exif_transpose(opened) if opened.getexif().get(274) else opened
            )
            if image.mode == "RGB":
                target = image
            elif image.mode in ("RGBA", "LA", "P", "PA"):
                rgba = image.convert("RGBA")
                target = Image.new("RGB", rgba.size, (255, 255, 255))
                target.paste(rgba, mask=rgba.getchannel("A"))
            else:
                target = image.convert("RGB")
            target.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
            out = BytesIO()
            target.save(out, format="JPEG", quality=85, optimize=True)
            return out.getvalue()
    except Exception as exc:  # noqa: BLE001 - tagging must survive bad files
        log.debug("cannot build sticker preview: %s", exc)
        return None


def sniff_mime(data: bytes) -> tuple[str, str]:
    """Return (mime, suffix) for a supported image, or ("", "") otherwise."""
    for magic, mime, suffix in _SIGNATURES:
        if data.startswith(magic):
            return mime, suffix
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return "", ""


def dhash_hex(data: bytes, size: int = 8) -> str:
    """64-bit difference hash of the first frame; "" when it cannot decode."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow ships with the bot
        return ""
    try:
        with Image.open(BytesIO(data)) as opened:
            opened.seek(0)
            if opened.mode in ("RGBA", "LA", "P", "PA"):
                rgba = opened.convert("RGBA")
                image = Image.new("RGB", rgba.size, (255, 255, 255))
                image.paste(rgba, mask=rgba.getchannel("A"))
            else:
                image = opened.convert("RGB")
            gray = image.convert("L").resize(
                (size + 1, size), Image.Resampling.LANCZOS
            )
            pixels = gray.tobytes()
    except Exception:  # noqa: BLE001 - dedupe must not break collection
        return ""
    bits = 0
    for row in range(size):
        base = row * (size + 1)
        for col in range(size):
            bits = (bits << 1) | (pixels[base + col] > pixels[base + col + 1])
    return f"{bits:0{size * size // 4}x}"


def hamming_distance(left: str, right: str) -> int:
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except (TypeError, ValueError):
        return 64


async def download_image(url: str, max_bytes: int = MAX_BYTES) -> tuple[bytes, str]:
    """Fetch an image from http(s) or a data URI; return (bytes, mime)."""
    if url.startswith("data:"):
        header, _, payload = url.partition(",")
        try:
            data = base64.b64decode(payload, validate=True)
        except (ValueError, TypeError) as exc:
            raise StickerError(f"invalid data URI: {exc}") from exc
        mime = header[5:].split(";")[0].strip().lower()
    else:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=True
            ) as client:
                response = await client.get(url)
        except httpx.HTTPError as exc:
            raise StickerError(f"download failed: {exc}") from exc
        if response.status_code != 200:
            raise StickerError(f"download HTTP {response.status_code}: {url[:120]}")
        data = response.content
        mime = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not data:
        raise StickerError("empty image")
    if len(data) > max_bytes:
        raise StickerError(f"image too large ({len(data)} bytes > {max_bytes})")
    return data, mime


_SCHEMA = """
CREATE TABLE IF NOT EXISTS stickers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sha1        TEXT NOT NULL UNIQUE,
    path        TEXT NOT NULL,
    mime        TEXT NOT NULL DEFAULT '',
    size        INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    tags        TEXT NOT NULL DEFAULT '[]',
    source      TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'ok',
    uses        INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL,
    last_used   INTEGER NOT NULL DEFAULT 0,
    phash       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_stickers_created ON stickers (created_at);
CREATE INDEX IF NOT EXISTS idx_stickers_status ON stickers (status);
"""


@dataclass(frozen=True)
class StickerRecord:
    id: int
    sha1: str
    path: str
    mime: str
    size: int
    description: str
    tags: tuple[str, ...]
    source: str
    source_kind: str
    status: str
    uses: int
    created_at: int
    last_used: int
    phash: str


def _record(row: sqlite3.Row) -> StickerRecord:
    try:
        raw_tags = json.loads(row["tags"] or "[]")
    except (TypeError, json.JSONDecodeError):
        raw_tags = []
    tags = (
        tuple(str(tag) for tag in raw_tags if str(tag).strip())
        if isinstance(raw_tags, list)
        else ()
    )
    return StickerRecord(
        id=int(row["id"]),
        sha1=row["sha1"] or "",
        path=row["path"] or "",
        mime=row["mime"] or "",
        size=int(row["size"] or 0),
        description=row["description"] or "",
        tags=tags,
        source=row["source"] or "",
        source_kind=row["source_kind"] or "",
        status=row["status"] or "ok",
        uses=int(row["uses"] or 0),
        created_at=int(row["created_at"]),
        last_used=int(row["last_used"] or 0),
        phash=str(row["phash"] or ""),
    )


class StickerStore:
    """Files on disk plus one SQLite row per unique image."""

    def __init__(
        self,
        database: str | Path,
        directory: str | Path,
        *,
        phash_distance: int = 4,
    ):
        self.path = Path(database)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.phash_distance = max(0, min(64, int(phash_distance)))
        self._phashes_ready = False
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        columns = {
            str(row["name"])
            for row in self._db.execute("PRAGMA table_info(stickers)").fetchall()
        }
        if "phash" not in columns:
            self._db.execute(
                "ALTER TABLE stickers ADD COLUMN phash TEXT NOT NULL DEFAULT ''"
            )
        self._db.commit()

    # --------------------------------------------------------------- reads

    def backfill_phashes(self) -> int:
        """Hash rows stored before perceptual dedupe existed (idempotent)."""
        with self._lock:
            if self._phashes_ready:
                return 0
            self._phashes_ready = True
            rows = self._db.execute(
                "SELECT id, path FROM stickers WHERE phash = ''"
            ).fetchall()
        updated = 0
        for row in rows:
            path = Path(row["path"]) if row["path"] else None
            if path is None or not path.is_file():
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            phash = dhash_hex(data)
            if not phash:
                continue
            with self._lock, self._db:
                self._db.execute(
                    "UPDATE stickers SET phash = ? WHERE id = ?",
                    (phash, int(row["id"])),
                )
            updated += 1
        if updated:
            log.info("backfilled %d sticker perceptual hash(es)", updated)
        return updated

    def count(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT COUNT(*) FROM stickers").fetchone()
        return int(row[0]) if row else 0

    def get(self, sticker_id: int) -> StickerRecord | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM stickers WHERE id = ?", (sticker_id,)
            ).fetchone()
        return _record(row) if row else None

    def by_sha1(self, sha1: str) -> StickerRecord | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM stickers WHERE sha1 = ?", (sha1,)
            ).fetchone()
        return _record(row) if row else None

    def list(self, limit: int = 20, status: str | None = None) -> list[StickerRecord]:
        sql = "SELECT * FROM stickers"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 200)))
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        return [_record(row) for row in rows]

    def search(self, query: str, limit: int = 20) -> list[StickerRecord]:
        needle = f"%{query.strip()}%"
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM stickers WHERE description LIKE ? OR tags LIKE ? "
                "OR source LIKE ? ORDER BY id DESC LIMIT ?",
                (needle, needle, needle, max(1, min(int(limit), 200))),
            ).fetchall()
        return [_record(row) for row in rows]

    def pick(self, hint: str = "") -> StickerRecord | None:
        """Choose a sendable sticker, preferring ones matching the hint."""
        rows = self.list(limit=200, status="ok")
        if not rows:
            return None
        needle = (hint or "").strip().lower()
        if needle:
            matched = [
                row
                for row in rows
                if needle in row.description.lower()
                or any(
                    needle in tag.lower() or tag.lower() in needle
                    for tag in row.tags
                )
            ]
            if matched:
                rows = matched
        tagged = [row for row in rows if row.description or row.tags]
        return random.choice(tagged or rows)

    def evict(self, max_count: int) -> int:
        """Delete the least-used, oldest stickers until at most max_count remain."""
        if max_count <= 0:
            return 0
        with self._lock, self._db:
            total = self._db.execute("SELECT COUNT(*) FROM stickers").fetchone()[0]
            excess = int(total) - int(max_count)
            if excess <= 0:
                return 0
            rows = self._db.execute(
                "SELECT id, path FROM stickers ORDER BY uses, last_used, created_at, id "
                "LIMIT ?",
                (excess,),
            ).fetchall()
            self._db.executemany(
                "DELETE FROM stickers WHERE id = ?", [(row["id"],) for row in rows]
            )
        for row in rows:
            path = Path(row["path"]) if row["path"] else None
            if path is not None and path.is_file():
                try:
                    path.unlink()
                except OSError as exc:
                    log.warning("cannot remove evicted sticker %s: %s", path, exc)
        return len(rows)

    def stats(self) -> dict[str, int]:
        with self._lock:
            total = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM stickers"
            ).fetchone()
            banned = self._db.execute(
                "SELECT COUNT(*) FROM stickers WHERE status = 'banned'"
            ).fetchone()
        return {
            "count": int(total[0] or 0),
            "bytes": int(total[1] or 0),
            "banned": int(banned[0] or 0),
        }

    # -------------------------------------------------------------- writes

    def add_bytes(
        self,
        data: bytes,
        *,
        mime: str = "",
        source: str = "",
        source_kind: str = "chat",
        max_bytes: int = MAX_BYTES,
    ) -> tuple[StickerRecord, bool]:
        """Store one image; return (record, created) and dedupe by content."""
        if not data:
            raise StickerError("empty image")
        if len(data) > max_bytes:
            raise StickerError(f"image too large ({len(data)} bytes > {max_bytes})")
        digest = hashlib.sha1(data).hexdigest()
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM stickers WHERE sha1 = ?", (digest,)
            ).fetchone()
        if row is not None:
            return _record(row), False

        sniffed, suffix = sniff_mime(data)
        if not suffix:
            raise StickerError("unsupported image format (want png/jpeg/gif/webp)")

        phash = dhash_hex(data)
        self.backfill_phashes()
        if phash:
            with self._lock:
                candidates = self._db.execute(
                    "SELECT id, phash FROM stickers WHERE phash != ''"
                ).fetchall()
            nearest: tuple[int, int] | None = None
            for candidate in candidates:
                distance = hamming_distance(phash, str(candidate["phash"]))
                if distance <= self.phash_distance and (
                    nearest is None or distance < nearest[0]
                ):
                    nearest = (distance, int(candidate["id"]))
            if nearest is not None:
                record = self.get(nearest[1])
                if record is not None:
                    log.info(
                        "sticker near-duplicate of #%s (hamming=%d); reusing it",
                        record.id,
                        nearest[0],
                    )
                    return record, False

        target = self.directory / f"{digest}{suffix}"
        if not target.exists():
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(target)

        now = int(time.time())
        with self._lock, self._db:
            self._db.execute(
                """
                INSERT INTO stickers
                    (sha1, path, mime, size, source, source_kind, created_at, phash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sha1) DO NOTHING
                """,
                (
                    digest,
                    str(target),
                    sniffed,
                    len(data),
                    source[:200],
                    source_kind[:40],
                    now,
                    phash,
                ),
            )
            row = self._db.execute(
                "SELECT * FROM stickers WHERE sha1 = ?", (digest,)
            ).fetchone()
        return _record(row), True

    def describe(self, sticker_id: int, description: str, tags: Any = ()) -> StickerRecord:
        """Store vision tags; filled by the later tagging phase or by hand."""
        cleaned = [str(tag).strip() for tag in tags if str(tag).strip()][:10]
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT id FROM stickers WHERE id = ?", (sticker_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"sticker #{sticker_id} does not exist")
            self._db.execute(
                "UPDATE stickers SET description = ?, tags = ? WHERE id = ?",
                (
                    description.strip()[:500],
                    json.dumps(cleaned, ensure_ascii=False),
                    sticker_id,
                ),
            )
            row = self._db.execute(
                "SELECT * FROM stickers WHERE id = ?", (sticker_id,)
            ).fetchone()
        return _record(row)

    def set_status(self, sticker_id: int, status: str) -> StickerRecord:
        if status not in VALID_STATUS:
            raise ValueError(f"unknown sticker status {status!r}")
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE stickers SET status = ? WHERE id = ?", (status, sticker_id)
            )
            if cursor.rowcount == 0:
                raise ValueError(f"sticker #{sticker_id} does not exist")
            row = self._db.execute(
                "SELECT * FROM stickers WHERE id = ?", (sticker_id,)
            ).fetchone()
        return _record(row)

    def mark_used(self, sticker_id: int) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE stickers SET uses = uses + 1, last_used = ? WHERE id = ?",
                (int(time.time()), sticker_id),
            )

    def delete(self, sticker_id: int) -> bool:
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT path FROM stickers WHERE id = ?", (sticker_id,)
            ).fetchone()
            if row is None:
                return False
            self._db.execute("DELETE FROM stickers WHERE id = ?", (sticker_id,))
        path = Path(row["path"]) if row["path"] else None
        if path is not None and path.is_file():
            try:
                path.unlink()
            except OSError as exc:
                log.warning("cannot remove sticker file %s: %s", path, exc)
        return True

    def close(self) -> None:
        self._db.close()
