"""Private media selection and pinned, bounded CDN downloads; never model-facing."""

from __future__ import annotations

import socket
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from card_fetch import CardFetchError, _public_ip, public_url
from video_platforms import VideoError
from video_resolver import number

MAX_BYTES = 64 * 1024 * 1024
# Separate from metadata hosts. Unknown CDN families fail closed.
CDNS = {'bilibili': ('bilivideo.com', 'bilivideo.cn', 'bilivideo.net'),
        'douyin': ('douyinvod.com',)}


def media_url(url: str, platform: str) -> str:
    try:
        public_url(url)
        host = (urlsplit(url).hostname or '').lower()
        if not any(host == d or host.endswith('.' + d) for d in CDNS.get(platform, ())):
            raise ValueError('unsupported CDN')
    except (ValueError, CardFetchError) as exc:
        raise VideoError('UNSUPPORTED_MEDIA', 'Media destination is not an approved platform CDN') from exc
    return url


def hd_dimensions(width, height):
    width, height = number(width), number(height)
    return (width is not None and height is not None and min(width, height) > 0
            and min(width, height) <= 720 and max(width, height) <= 1280)


def select_formats(info: dict, max_bytes: int, max_duration: float) -> tuple[list[dict], bool]:
    duration = number(info.get('duration'))
    if duration is None or duration <= 0 or duration > max_duration:
        raise VideoError('LIMIT_EXCEEDED', 'Video duration is unknown or exceeds the sending limit')
    if info.get('is_live') or info.get('live_status') in ('is_live', 'is_upcoming'):
        raise VideoError('UNSUPPORTED_MEDIA', 'Live videos cannot be sent')
    formats = [f for f in (info.get('formats') or [])[:200] if isinstance(f, dict)]

    def direct(f):
        return (isinstance(f.get('url'), str) and not f.get('fragments')
                and f.get('protocol', 'https') in ('https', 'http')
                and f.get('ext') in ('mp4', 'm4a') and not f.get('has_drm'))

    def estimate(f):
        size = number(f.get('filesize')) or number(f.get('filesize_approx'))
        bitrate = number(f.get('tbr'))
        return size or (bitrate * 1000 * duration / 8 * 1.1 if bitrate else None)

    def h264(f):
        return str(f.get('vcodec', '')).lower().startswith(('avc1', 'avc3', 'h264'))

    def aac(f):
        return str(f.get('acodec', '')).lower().startswith(('mp4a', 'aac'))

    audio = [f for f in formats if direct(f) and f.get('vcodec') == 'none' and aac(f)]
    videos = [f for f in formats if direct(f) and h264(f)
              and hd_dimensions(f.get('width'), f.get('height'))]
    silent = info.get('audio_channels') == 0
    candidates = []
    for video in videos:
        tracks = [[video]] if aac(video) else [[video, a] for a in audio]
        if not tracks and silent and video.get('acodec') == 'none':
            tracks = [[video]]
        for pair in tracks:
            estimates = [estimate(f) for f in pair]
            if any(size is None for size in estimates):
                continue
            cost = sum(size for size in estimates if size is not None)
            if cost <= max_bytes:
                candidates.append((min(video['width'], video['height']), -cost, pair))
    if not candidates:
        raise VideoError('UNSUPPORTED_MEDIA', 'No supported H.264/AAC MP4 fits the sending limits')
    chosen = max(candidates, key=lambda item: item[:2])[2]
    return chosen, silent and len(chosen) == 1 and chosen[0].get('acodec') == 'none'


def download_tracks(info: dict, platform: str, directory: str, max_bytes: int,
                    max_duration: float, timeout: float = 35) -> dict:
    tracks, silent = select_formats(info, min(MAX_BYTES, max_bytes), max_duration)
    root = Path(directory)
    if not root.is_dir() or root.is_symlink():
        raise VideoError('UNAVAILABLE', 'Private video workspace is unavailable')
    deadline = time.monotonic() + min(35, timeout)
    total = requests = 0
    artifacts = []
    with httpx.Client(trust_env=False, follow_redirects=False) as client:
        for index, fmt in enumerate(tracks):
            path = root / f'input{index}.mp4'
            current = fmt['url']
            with path.open('xb') as output:
                for _ in range(6):
                    media_url(current, platform)
                    requests += 1
                    if requests > 12:
                        raise VideoError('LIMIT_EXCEEDED', 'Media request budget exceeded')
                    original = httpx.URL(current)
                    try:
                        answers = socket.getaddrinfo(original.host, 443, type=socket.SOCK_STREAM)
                        ips = [_public_ip(str(answer[4][0])) for answer in answers]
                    except (OSError, CardFetchError) as exc:
                        raise VideoError('UPSTREAM_ERROR', 'Media destination is unavailable or forbidden') from exc
                    remaining = deadline - time.monotonic()
                    if not ips or remaining <= 0:
                        raise VideoError('TIMEOUT', 'Media download timed out')
                    client.cookies.clear()
                    headers = {'Host': original.host, 'Accept-Encoding': 'identity',
                               'User-Agent': 'Mozilla/5.0', 'Referer':
                               'https://www.bilibili.com/' if platform == 'bilibili' else 'https://www.douyin.com/'}
                    with client.stream('GET', original.copy_with(host=ips[0]), headers=headers,
                                       extensions={'sni_hostname': original.host},
                                       timeout=min(8, remaining)) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get('location')
                            if not location:
                                raise VideoError('UPSTREAM_ERROR', 'Invalid media redirect')
                            current = urljoin(current, location)
                            continue
                        if response.status_code != 200:
                            raise VideoError('UPSTREAM_BLOCKED', 'Platform media request was rejected')
                        if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                            raise VideoError('UNSUPPORTED_MEDIA', 'Compressed media responses are not accepted')
                        kind = response.headers.get('content-type', '').split(';')[0].lower()
                        if kind and kind not in ('video/mp4', 'audio/mp4', 'application/octet-stream',
                                                'binary/octet-stream', 'video/x-m4v', 'audio/x-m4a'):
                            raise VideoError('UNSUPPORTED_MEDIA', 'Unexpected media response type')
                        length = response.headers.get('content-length')
                        if length and (not length.isdigit() or int(length) > max_bytes - total):
                            raise VideoError('LIMIT_EXCEEDED', 'Video exceeds the download size limit')
                        for chunk in response.iter_raw(chunk_size=65536):
                            total += len(chunk)
                            if total > max_bytes or total > MAX_BYTES:
                                raise VideoError('LIMIT_EXCEEDED', 'Video exceeds the download size limit')
                            if time.monotonic() >= deadline:
                                raise VideoError('TIMEOUT', 'Media download timed out')
                            output.write(chunk)
                        break
                else:
                    raise VideoError('LIMIT_EXCEEDED', 'Too many media redirects')
            if not path.stat().st_size:
                raise VideoError('UNSUPPORTED_MEDIA', 'Platform returned empty media')
            artifacts.append(path.name)
    return {'files': artifacts, 'silent': silent}
