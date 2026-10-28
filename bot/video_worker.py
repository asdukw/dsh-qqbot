"""Short-lived extractor; only explicit internal materialize mode downloads bounded media."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from video_credentials import CredentialError, load_jar
from video_http import SafeHTTP, handler_class
from video_platforms import VideoError, parse_source, platform_http_error
from video_resolver import project

VERSION = "2026.10.26"


class Quiet:
    def debug(self, *args, **kwargs):
        pass

    info = warning = error = stdout = debug


def extract(payload: dict) -> dict:
    yt_dlp: Any = importlib.import_module("yt_dlp")
    globals_ = importlib.import_module("yt_dlp.globals")
    __version__ = importlib.import_module("yt_dlp.version").__version__

    if __version__ != VERSION:
        raise VideoError(
            "BACKEND_UNAVAILABLE",
            "Unsupported extractor version; upgrade requires regression testing",
        )
    # auto_init=False alone does not disable plugin loading in YoutubeDL.__init__.
    globals_.plugin_dirs.value = []
    globals_.all_plugins_loaded.value = True
    BiliBiliIE: Any = importlib.import_module("yt_dlp.extractor.bilibili").BiliBiliIE
    DouyinIE: Any = importlib.import_module("yt_dlp.extractor.tiktok").DouyinIE

    target = parse_source(payload["source"], payload.get("part"))
    transport = SafeHTTP(target.platform)
    cookies = importlib.import_module("yt_dlp.cookies").YoutubeDLCookieJar()
    cookie_path = payload.get("cookie_file", "")
    try:
        expected_stat = payload.get('credential_stat')
        if cookie_path and expected_stat:
            info = Path(cookie_path).stat()
            if [info.st_mtime_ns, info.st_size] != expected_stat:
                raise CredentialError()
        load_jar(cookie_path, cookies, target.platform, payload.get('credential_generation'))
        if cookie_path and expected_stat:
            info = Path(cookie_path).stat()
            if [info.st_mtime_ns, info.st_size] != expected_stat:
                raise CredentialError()
    except (CredentialError, OSError) as exc:
        raise VideoError('CREDENTIALS_INVALID', 'Configured platform cookie file cannot be loaded') from exc
    if target.short:
        _, final, status, _ = transport.fetch(target.url, cookiejar=cookies, stop_at_video=True)
        if status >= 400:
            raise platform_http_error(status, target.platform, "shortlink")
        resolved = parse_source(final)
        if resolved.platform != target.platform or resolved.short:
            raise VideoError(
                "UNSUPPORTED_SOURCE", "Short link did not resolve to a supported video"
            )
        # An explicit part parameter must not silently override the destination's p.
        target = parse_source(final, payload.get("part"))

    rh = handler_class()
    logger: Any = Quiet()

    class RestrictedYDL(yt_dlp.YoutubeDL):
        def build_request_director(self, handlers, preferences=None):
            RequestDirector = importlib.import_module("yt_dlp.networking.common").RequestDirector

            director = RequestDirector(logger=logger)
            director.add_handler(
                rh(
                    transport=transport,
                    logger=logger,
                    headers=self.params.get("http_headers"),
                    cookiejar=cookies,
                    proxies={},
                    timeout=8,
                )
            )
            return director

    params = {
        "quiet": True,
        "no_warnings": True,
        "logger": logger,
        "cachedir": False,
        "skip_download": True,
        "noplaylist": True,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "listsubtitles": False,
        "getcomments": False,
        "proxy": "",
        "socket_timeout": 8,
        "extractor_retries": 0,
        "retries": 0,
        "enable_file_urls": False,
        "js_runtimes": {},
        "remote_components": [],
        "ignore_no_formats_error": True,
        "http_headers": {"User-Agent": "Mozilla/5.0"},
    }
    with RestrictedYDL(params, auto_init=False) as ydl:
        ydl.cookiejar = cookies

        class MetadataBiliIE(BiliBiliIE):
            validated = None
            page_list = None
            selected_cid = None
            embedded_play = None
            restricted = False

            def _search_json(self, pattern, string, name, video_id, **kwargs):
                value = super()._search_json(pattern, string, name, video_id, **kwargs)
                if '__INITIAL_STATE__' in pattern:
                    if isinstance(value, dict) and isinstance(value.get('error'), dict):
                        reason = value['error'].get('trueCode')
                        if reason in (-403, -404):
                            raise VideoError('AUTH_REQUIRED' if reason == -403 else 'NOT_FOUND',
                                             'Platform denied video access', stage='extractor')
                    if not isinstance(value, dict) or not isinstance(value.get('videoData'), dict):
                        raise VideoError('EXTRACTOR_BROKEN', 'Bilibili initial video data is unavailable')
                    data = value['videoData']
                    bvid, aid, pages = data.get('bvid'), data.get('aid'), data.get('pages')
                    if ((target.content_id.startswith('BV') and bvid != target.content_id)
                            or (target.content_id.startswith('av') and str(aid) != target.content_id[2:])):
                        raise VideoError('EXTRACTOR_BROKEN', 'Platform returned a different video')
                    if (data.get('rights', {}).get('is_stein_gate') or data.get('redirect_url')):
                        raise VideoError('UNSUPPORTED_SOURCE', 'Interactive or redirected collections are not supported')
                    if not isinstance(pages, list) or not pages or len(pages) > 1000:
                        raise VideoError('EXTRACTOR_BROKEN', 'Bilibili parts are unavailable')
                    if target.part > len(pages):
                        raise VideoError('INVALID_SOURCE', 'Requested Bilibili part does not exist')
                    chosen = pages[target.part - 1]
                    if not isinstance(chosen, dict) or type(chosen.get('cid')) is not int or chosen['cid'] <= 0:
                        raise VideoError('EXTRACTOR_BROKEN', 'Bilibili selected part is invalid')
                    self.validated = parse_source(bvid, target.part)
                    self.page_list = [{'page': i + 1, 'part': p.get('part', '')}
                                      for i, p in enumerate(pages) if isinstance(p, dict)]
                    if len(self.page_list) != len(pages):
                        raise VideoError('EXTRACTOR_BROKEN', 'Bilibili parts are invalid')
                    self.selected_cid = chosen['cid']
                    self.restricted = bool(data.get('is_upower_exclusive') or data.get('is_preview'))
                elif '__playinfo__' in pattern and isinstance(value, dict):
                    data = value.get('data')
                    if (isinstance(data, dict) and data.get('cid') == self.selected_cid
                            and isinstance(data.get('dash'), dict) and not self.restricted
                            and isinstance(data['dash'].get('video'), list)
                            and any(isinstance(f, dict) and (f.get('baseUrl') or f.get('base_url'))
                                    for f in data['dash']['video'])
                            and not data.get('is_preview')
                            and not data.get('is_upower_exclusive') and value.get('code', 0) == 0):
                        self.embedded_play = data
                    else:
                        # Even an authenticated extractor must not use another part's embedded tracks.
                        return None
                return value

            def _download_json(self, url, video_id, *args, **kwargs):
                if urlsplit(url).path == '/x/player/pagelist':
                    if self.validated is None or self.page_list is None:
                        raise VideoError('EXTRACTOR_BROKEN', 'Bilibili validation hook was not invoked')
                    return {'code': 0, 'data': self.page_list}
                result = super()._download_json(url, video_id, *args, **kwargs)
                # nav legitimately reports anonymous status while still supplying WBI keys.
                anonymous_nav = (urlsplit(url).path == '/x/web-interface/nav'
                                 and isinstance(result, dict) and result.get('code') == -101
                                 and isinstance(result.get('data'), dict) and result['data'].get('wbi_img'))
                if isinstance(result, dict) and result.get('code') in (-412, -352, -101, -404) and not anonymous_nav:
                    code = {-412: 'UPSTREAM_BLOCKED', -352: 'RATE_LIMITED',
                            -101: 'AUTH_REQUIRED', -404: 'NOT_FOUND'}[result['code']]
                    raise VideoError(code, 'Platform API rejected the request; do not repeatedly retry', stage='extractor')
                return result

            def _download_playinfo(self, bvid, cid, headers=None, query=None, fatal=True):
                if self.validated is None or bvid != self.validated.content_id or cid != self.selected_cid:
                    raise VideoError('EXTRACTOR_BROKEN', 'Bilibili playback identity mismatch')
                if self.embedded_play is not None and not (query or {}).get('qn'):
                    return self.embedded_play
                return super()._download_playinfo(bvid, cid, headers=headers, query=query, fatal=fatal)

            def _get_chapters(self, *args, **kwargs):
                return None

            def extract_subtitles(self, *args, **kwargs):
                return {}  # Never fetch caption bodies.

        class MetadataDouyinIE(DouyinIE):
            def _parse_aweme_video_app(self, detail):
                if (
                    detail.get("images")
                    or detail.get("image_post_info")
                    or detail.get("aweme_type") in (2, 68)
                    or not detail.get("video")
                ):
                    raise VideoError("UNSUPPORTED_SOURCE", "Only Douyin video posts are supported")
                return super()._parse_aweme_video_app(detail)

        ie = MetadataBiliIE(ydl) if target.platform == "bilibili" else MetadataDouyinIE(ydl)
        try:
            # Call only the explicitly chosen extractor. Do not dispatch url/playlist results.
            info = ie.extract(target.url)
            if transport.failure is not None:
                raise transport.failure
            if not isinstance(info, dict):
                raise VideoError("EXTRACTOR_BROKEN", "Extractor returned no video metadata")
            if target.platform == 'bilibili':
                if ie.validated is None:
                    raise VideoError('EXTRACTOR_BROKEN', 'Bilibili identity was not validated')
                target = ie.validated
            expected = target.content_id
            actual = str(info.get("id", ""))
            if target.platform == "douyin" and actual != expected:
                raise VideoError("EXTRACTOR_BROKEN", "Extractor returned a different video")
            if target.platform == "bilibili":
                base = actual.split("_p", 1)[0]
                if expected.startswith("BV") and base != expected:
                    raise VideoError("EXTRACTOR_BROKEN", "Extractor returned a different video")
                if "_p" in actual and actual.rsplit("_p", 1)[-1] != str(target.part):
                    raise VideoError("EXTRACTOR_BROKEN", "Extractor returned a different part")
                pages = info.get("entries")
                if pages is not None:
                    raise VideoError(
                        "UNSUPPORTED_SOURCE", "Interactive or multi-entry videos are not supported"
                    )
            result = project(info, target)
            if payload.get('materialize'):
                from video_media import download_tracks
                result['artifacts'] = download_tracks(
                    info, target.platform, payload['directory'], payload['max_bytes'],
                    payload['max_duration'], payload.get('download_timeout', 35))
            return result
        except VideoError:
            raise
        except Exception as exc:
            if failure := getattr(transport, "failure", None):
                raise failure from exc
            # Classify without returning raw URLs, cookies or extractor diagnostics.
            message = str(exc).lower()
            if "fresh cookies" in message:
                code, detail = (
                    "CREDENTIALS_REQUIRED",
                    "Fresh operator-provided platform cookies are required",
                )
            elif any(s in message for s in ("login", "log in", "logged in")):
                code, detail = "AUTH_REQUIRED", "Platform authorization is required"
            elif any(s in message for s in ("premium", "supporter-only", "geo restrict")):
                code, detail = "ACCESS_RESTRICTED", "Video access is restricted"
            elif "429" in message:
                code, detail = "RATE_LIMITED", "Platform rate limit reached"
            elif any(s in message for s in ("403", "-412", "captcha", "verification")):
                code, detail = "UPSTREAM_BLOCKED", "Platform rejected the request"
            elif any(s in message for s in ("404", "not found", "deleted", "不存在")):
                code, detail = "NOT_FOUND", "Video is unavailable or deleted"
            else:
                code, detail = (
                    "EXTRACTOR_BROKEN",
                    "Video extraction failed; platform or backend may have changed",
                )
            raise VideoError(code, detail) from exc


def main():
    try:
        raw = sys.stdin.buffer.read(8193)
        if len(raw) > 8192:
            raise VideoError("INVALID_SOURCE", "Worker request too large")
        result = extract(json.loads(raw))
    except VideoError as exc:
        result = exc.result()
    except httpx.TimeoutException:
        result = VideoError("TIMEOUT", "Video network request timed out").result()
    except (httpx.HTTPError, OSError):
        result = VideoError("UPSTREAM_ERROR", "Video network request failed").result()
    except ImportError:
        result = VideoError(
            "BACKEND_UNAVAILABLE", "Pinned yt-dlp backend is not installed"
        ).result()
    except Exception:
        result = VideoError("UPSTREAM_ERROR", "Video worker failed").result()
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
