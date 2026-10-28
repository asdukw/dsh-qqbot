"""Short-lived yt-dlp worker. Never imports user plugins or downloads media."""

from __future__ import annotations

import importlib
import json
import sys
from typing import Any
from urllib.parse import urlencode

import httpx
from video_http import SafeHTTP, handler_class
from video_platforms import VideoError, parse_source
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
    if cookie_path:
        try:
            cookies.load(cookie_path, ignore_discard=True, ignore_expires=False)
        except (OSError, ValueError) as exc:
            raise VideoError(
                "CREDENTIALS_INVALID", "Configured platform cookie file cannot be loaded"
            ) from exc
        allowed = "bilibili.com" if target.platform == "bilibili" else "douyin.com"
        for cookie in list(cookies):
            if cookie.domain.lstrip(".") != allowed and not cookie.domain.lstrip(".").endswith(
                "." + allowed
            ):
                cookies.clear(cookie.domain, cookie.path, cookie.name)
    if target.short:
        _, final, status, _ = transport.fetch(target.url, cookiejar=cookies)
        if status >= 400:
            raise VideoError("UPSTREAM_ERROR", "Short link could not be resolved")
        resolved = parse_source(final)
        if resolved.platform != target.platform or resolved.short:
            raise VideoError(
                "UNSUPPORTED_SOURCE", "Short link did not resolve to a supported video"
            )
        # An explicit part parameter must not silently override the destination's p.
        target = parse_source(final, payload.get("part"))

    if target.platform == "bilibili":
        query = (
            {"bvid": target.content_id}
            if target.content_id.startswith("BV")
            else {"aid": target.content_id[2:]}
        )
        data, _, status, _ = transport.fetch(
            "https://api.bilibili.com/x/web-interface/view?" + urlencode(query), cookiejar=cookies
        )
        if status != 200:
            code = {
                404: "NOT_FOUND",
                429: "RATE_LIMITED",
                403: "UPSTREAM_BLOCKED",
                412: "UPSTREAM_BLOCKED",
            }.get(status, "UPSTREAM_ERROR")
            raise VideoError(code, "Platform metadata request was rejected")
        detail = json.loads(data)
        code = detail.get("code")
        if code != 0:
            category = {-404: "NOT_FOUND", -412: "UPSTREAM_BLOCKED", -101: "AUTH_REQUIRED"}.get(
                code, "UPSTREAM_ERROR"
            )
            raise VideoError(category, "Platform metadata request was rejected")
        metadata = detail.get("data", {})
        if (target.content_id.startswith("BV") and metadata.get("bvid") != target.content_id) or (
            target.content_id.startswith("av") and str(metadata.get("aid")) != target.content_id[2:]
        ):
            raise VideoError("EXTRACTOR_BROKEN", "Platform returned a different video")
        pages = metadata.get("pages", [])
        if target.part > len(pages):
            raise VideoError("INVALID_SOURCE", "Requested Bilibili part does not exist")
        identity = detail.get("data", {}).get("bvid")
        target = parse_source(identity, target.part)

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
            def extract_subtitles(self, *args, **kwargs):
                return {}  # Never let _get_subtitles eagerly download caption bodies.

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
            return project(info, target)
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
