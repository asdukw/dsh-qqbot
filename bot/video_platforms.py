"""Single-video identifiers; transport validation is repeated on every request."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from card_fetch import CardFetchError, public_url


class VideoError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def result(self) -> dict:
        return {
            "schema_version": 1,
            "status": "error",
            "error": {
                "code": self.code,
                "message": str(self),
            },
        }


@dataclass(frozen=True)
class VideoTarget:
    platform: str
    content_id: str
    url: str
    part: int = 1
    short: bool = False


BILI_ID = re.compile(r"(?:BV[0-9A-Za-z]{10}|av[1-9][0-9]{0,19})\Z")
HOSTS = {
    "bilibili": {
        "www.bilibili.com",
        "bilibili.com",
        "m.bilibili.com",
        "b23.tv",
        "api.bilibili.com",
    },
    "douyin": {"www.douyin.com", "douyin.com", "v.douyin.com"},
}
SHORT_HOSTS = {"b23.tv": "bilibili", "v.douyin.com": "douyin"}


def checked_url(url: str, platform: str) -> str:
    try:
        public_url(url)
        host = (urlsplit(url).hostname or "").lower()
        if host not in HOSTS.get(platform, set()):
            raise ValueError("unsupported host")
    except (ValueError, CardFetchError) as exc:
        raise VideoError(
            "INVALID_SOURCE", "Only supported public platform HTTPS URLs are allowed"
        ) from exc
    return url


def parse_source(source: str, part: int | None = None) -> VideoTarget:
    if not isinstance(source, str) or not 0 < len(source) <= 4096:
        raise VideoError(
            "INVALID_SOURCE", "Source must be a link or video ID (at most 4096 characters)"
        )
    if part is not None and (type(part) is not int or not 1 <= part <= 1000):
        raise VideoError("INVALID_SOURCE", "Part must be an integer from 1 to 1000")
    source = source.strip()
    if BILI_ID.fullmatch(source):
        source = f"https://www.bilibili.com/video/{source}"
    links = list(dict.fromkeys(re.findall(r"https?://[^\s<>\"　]+", source)))
    links = list(dict.fromkeys(link.rstrip(".,;!?)，。；！？”》）") for link in links))
    if len(links) != 1:
        raise VideoError("INVALID_SOURCE", "Provide exactly one video link or BV/AV identifier")
    url = links[0]
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError as exc:
        raise VideoError("INVALID_SOURCE", "Invalid video URL") from exc
    platform = next((p for p, hosts in HOSTS.items() if host in hosts), "")
    if not platform:
        raise VideoError("UNSUPPORTED_SOURCE", "This platform is not supported")
    checked_url(url, platform)
    parts = urlsplit(url)
    if host in SHORT_HOSTS:
        if not re.fullmatch(r"/[A-Za-z0-9_-]{1,100}/?", parts.path):
            raise VideoError("UNSUPPORTED_SOURCE", "Unsupported short link")
        return VideoTarget(platform, "", url.split("#", 1)[0], part or 1, True)
    if platform == "bilibili":
        match = re.fullmatch(r"/video/(BV[0-9A-Za-z]{10}|av[1-9][0-9]{0,19})/?", parts.path)
        if match and host != "api.bilibili.com":
            query = parse_qs(parts.query, keep_blank_values=True)
            selected = query.get("p", [])
            if selected and (len(selected) != 1 or not selected[0].isdigit()):
                raise VideoError("INVALID_SOURCE", "Invalid Bilibili part")
            url_part = int(selected[0]) if selected else None
            if url_part is not None and (
                not 1 <= url_part <= 1000 or part is not None and part != url_part
            ):
                raise VideoError("INVALID_SOURCE", "Conflicting or out-of-range Bilibili part")
            identity = match[1]
            number = part or url_part or 1
            return VideoTarget(
                platform, identity, f"https://www.bilibili.com/video/{identity}?p={number}", number
            )
    elif match := re.fullmatch(r"/video/([1-9][0-9]{0,24})/?", parts.path):
        if part not in (None, 1):
            raise VideoError("INVALID_SOURCE", "Douyin videos do not have parts")
        return VideoTarget(platform, match[1], f"https://www.douyin.com/video/{match[1]}")
    raise VideoError(
        "UNSUPPORTED_SOURCE",
        "Only individual video pages are supported (not live, galleries or collections)",
    )
