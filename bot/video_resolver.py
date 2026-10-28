"""Stable, bounded public results independent of the extraction backend."""

from __future__ import annotations

import math
from dataclasses import dataclass
from urllib.parse import urlsplit

from video_platforms import VideoError, VideoTarget


@dataclass
class VideoSettings:
    enabled: bool = True
    bilibili_enabled: bool = True
    douyin_enabled: bool = False
    timeout: float = 30.0
    concurrency: int = 2
    calls_per_turn: int = 3
    cache_seconds: int = 300
    cache_entries: int = 128
    bilibili_cookies: str = ""
    douyin_cookies: str = ""

    def cookie_file(self, platform: str) -> str:
        return self.bilibili_cookies if platform == "bilibili" else self.douyin_cookies


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def text(value, limit=500):
    return value[:limit] if isinstance(value, str) else None


def cover(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.query
            or len(value) > 1000
            or not any(
                host == domain or host.endswith("." + domain)
                for domain in ("hdslb.com", "douyinpic.com", "byteimg.com")
            )
        ):
            return None
        return value
    except ValueError:
        return None


def project(info: dict, target: VideoTarget) -> dict:
    if info.get("_type", "video") != "video" or info.get("entries") is not None:
        raise VideoError(
            "UNSUPPORTED_SOURCE", "Collections and multi-entry results are not supported"
        )
    resources = []
    seen = set()
    for fmt in (info.get("formats") or [])[:200]:
        if not isinstance(fmt, dict) or not fmt.get("url"):
            continue
        kind = (
            "audio"
            if fmt.get("vcodec") == "none"
            else "video"
            if fmt.get("acodec") == "none"
            else "combined"
        )
        item = {
            "kind": kind,
            "container": text(fmt.get("ext"), 16),
            "width": number(fmt.get("width")),
            "height": number(fmt.get("height")),
            "video_codec": text(fmt.get("vcodec"), 40),
            "audio_codec": text(fmt.get("acodec"), 40),
            "size_bytes": number(fmt.get("filesize")),
        }
        key = tuple(item.values())
        if key not in seen:
            seen.add(key)
            resources.append(item)
        if len(resources) == 12:
            break
    return {
        "schema_version": 1,
        "status": "ok",
        "platform": target.platform,
        "content_id": target.content_id,
        "canonical_url": target.url,
        "kind": "video",
        "title": text(info.get("title"), 300),
        "description": text(info.get("description"), 1200),
        "author": {
            "name": text(info.get("uploader"), 120),
            "id": text(info.get("uploader_id"), 100),
        },
        "duration_seconds": number(info.get("duration")),
        "timestamp": number(info.get("timestamp")),
        "cover_url": cover(info.get("thumbnail")),
        "selected_part": target.part,
        "subtitles": {"status": "unknown", "items": []},
        "resources": resources,
        "availability": text(info.get("availability"), 40) or "unknown",
        "warnings": [
            "Metadata is not a transcript or proof of playback; subtitles were not fetched.",
            "Only the selected part is resolved; default is part 1.",
        ]
        + (["Douyin support is experimental."] if target.platform == "douyin" else []),
    }
