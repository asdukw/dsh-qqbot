"""Pinned-extractor transport: bounded HTTPS, public DNS, no fallback or proxies."""

from __future__ import annotations

import importlib
import io
import socket
import time
import zlib
from email.message import Message
from http.cookiejar import CookieJar
from typing import Any, cast
from urllib.parse import urljoin
from urllib.request import Request as CookieRequest

import httpx
from card_fetch import CardFetchError, _public_ip
from video_platforms import VideoError, checked_url, parse_source, platform_http_error

MAX_RESPONSE = 2 * 1024 * 1024
MAX_TOTAL = 8 * 1024 * 1024


class SafeHTTP:
    def __init__(self, platform: str, *, browser: bool = False):
        self.platform = platform
        self.browser = browser and platform == "bilibili"
        self.requests = 0
        self.total = 0
        self.failure: VideoError | None = None

    def fetch(
        self, url: str, *, headers=None, cookiejar: CookieJar | None = None, timeout: float = 8.0,
        stop_at_video: bool = False
    ) -> tuple[bytes, str, int, dict]:
        deadline = time.monotonic() + min(10, max(0.1, timeout))
        with httpx.Client(trust_env=False, follow_redirects=False) as client:
            for _ in range(6):
                checked_url(url, self.platform)
                self.requests += 1
                if self.requests > 24:
                    raise VideoError("LIMIT_EXCEEDED", "Video request budget exceeded")
                original = httpx.URL(url)
                try:
                    answers = socket.getaddrinfo(original.host, 443, type=socket.SOCK_STREAM)
                    addresses = [_public_ip(str(answer[4][0])) for answer in answers]
                except (OSError, CardFetchError) as exc:
                    raise VideoError(
                        "UPSTREAM_ERROR", "Video destination unavailable or forbidden"
                    ) from exc
                if not addresses:
                    raise VideoError("UPSTREAM_ERROR", "Video destination unavailable")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise VideoError("TIMEOUT", "Video request timed out")
                # Rebuild credentials for the actual destination on every hop.
                safe_headers = {
                    k: str(v)
                    for k, v in (headers or {}).items()
                    if k.lower() in ("user-agent", "accept", "accept-language")
                }
                safe_headers.update(
                    {
                        "Host": original.host,
                        "Accept-Encoding": "identity",
                        "Referer": (
                            "https://www.bilibili.com/"
                            if self.platform == "bilibili"
                            else "https://www.douyin.com/"
                        ),
                    }
                )
                req = CookieRequest(url)
                if cookiejar is not None:
                    cookiejar.add_cookie_header(req)
                    if cookie := req.get_header("Cookie"):
                        safe_headers["Cookie"] = cookie
                client.cookies.clear()  # Never send cookies stored against the pinned IP.
                if self.browser:
                    # Let the maintained browser profile supply its matching User-Agent.
                    safe_headers = {k: v for k, v in safe_headers.items() if k.lower() != "user-agent"}
                    from video_curl import response as curl_response
                    context = curl_response(url, addresses[0], safe_headers, remaining, MAX_RESPONSE)
                else:
                    context = client.stream(
                        "GET", original.copy_with(host=addresses[0]), headers=safe_headers,
                        extensions={"sni_hostname": original.host}, timeout=remaining)
                with context as response:
                    if cookiejar is not None:
                        message = Message()
                        for key, value in response.headers.multi_items():
                            message.add_header(key, value)

                        class CookieResponse:
                            def __init__(self, value):
                                self.message = value

                            def info(self):
                                return self.message

                        cookiejar.extract_cookies(cast(Any, CookieResponse(message)), req)
                    if response.status_code in (301, 302, 303, 307, 308):
                        if self.browser:
                            # curl_cffi buffers redirect bodies; count them so the
                            # MAX_TOTAL budget covers every hop, not only 200s.
                            self.total += len(response.read())
                            if self.total > MAX_TOTAL:
                                raise VideoError("LIMIT_EXCEEDED", "Video response too large")
                        location = response.headers.get("location")
                        if not location:
                            raise VideoError("UPSTREAM_ERROR", "Invalid video redirect")
                        destination = urljoin(url, location)
                        checked_url(destination, self.platform)
                        if stop_at_video:
                            target = parse_source(destination)
                            if target.platform != self.platform:
                                raise VideoError('UNSUPPORTED_SOURCE', 'Short link changed platform', stage='shortlink')
                            if not target.short:
                                # A validated Location identifies the video. Do not fetch its HTML
                                # here; extraction owns subsequent requests and their error stage.
                                return b'', destination, response.status_code, {}
                        url = destination
                        continue
                    size = raw = 0
                    chunks = []
                    encoding = response.headers.get("content-encoding", "identity").lower()
                    if encoding not in ("identity", "gzip", "deflate"):
                        raise VideoError("UPSTREAM_ERROR", "Unsupported response encoding")
                    decoder = (
                        zlib.decompressobj(
                            16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS
                        )
                        if encoding != "identity"
                        else None
                    )
                    for chunk in response.iter_raw(chunk_size=16384):
                        raw += len(chunk)
                        if raw > MAX_RESPONSE or time.monotonic() > deadline:
                            raise VideoError("LIMIT_EXCEEDED", "Video response budget exceeded")
                        data = (
                            decoder.decompress(chunk, MAX_RESPONSE - size + 1) if decoder else chunk
                        )
                        size += len(data)
                        self.total += len(data)
                        if size > MAX_RESPONSE or self.total > MAX_TOTAL:
                            raise VideoError("LIMIT_EXCEEDED", "Video response too large")
                        chunks.append(data)
                    if decoder and (not decoder.eof or decoder.unused_data):
                        raise VideoError("UPSTREAM_ERROR", "Invalid compressed response")
                    return b"".join(chunks), url, response.status_code, dict(response.headers)
            raise VideoError("LIMIT_EXCEEDED", "Too many video redirects")


def handler_class():
    networking: Any = importlib.import_module("yt_dlp.networking")
    errors: Any = importlib.import_module("yt_dlp.networking.exceptions")
    RequestHandler, Response = networking.RequestHandler, networking.Response
    HTTPError, TransportError = errors.HTTPError, errors.TransportError

    class VideoRH(RequestHandler):
        _SUPPORTED_URL_SCHEMES = ("https",)
        _SUPPORTED_PROXY_SCHEMES = ()

        def __init__(self, *, transport: SafeHTTP, **kwargs):
            super().__init__(**kwargs)
            self.transport = transport

        def _check_extensions(self, extensions):
            super()._check_extensions(extensions)
            extensions.pop("cookiejar", None)
            extensions.pop("timeout", None)

        def _send(self, request):
            try:
                if (
                    request.method != "GET"
                    or request.data is not None
                    or any(request.proxies.values())
                ):
                    raise VideoError(
                        "UNSUPPORTED_SOURCE", "Only bounded platform GET requests are allowed"
                    )
                data, url, status, headers = self.transport.fetch(
                    request.url,
                    headers=self._get_headers(request),
                    cookiejar=self._get_cookiejar(request),
                    timeout=self._calculate_timeout(request),
                )
                headers = {
                    k: v
                    for k, v in headers.items()
                    if k.lower() not in ("content-encoding", "content-length")
                }
                response = Response(io.BytesIO(data), url, headers, status=status)
                if status >= 400:
                    self.transport.failure = platform_http_error(status, self.transport.platform, 'extractor')
                    raise HTTPError(response)
                return response
            except VideoError as exc:
                self.transport.failure = exc
                raise TransportError("Video destination rejected or unavailable") from exc
            except httpx.TimeoutException as exc:
                self.transport.failure = VideoError("TIMEOUT", "Video network request timed out")
                raise TransportError("Video network request timed out") from exc
            except (httpx.HTTPError, OSError, ValueError, zlib.error) as exc:
                raise TransportError("Video network request failed") from exc

    return VideoRH
