"""Bounded libcurl adapter; extraction and platform signing stay in yt-dlp."""

from __future__ import annotations

import importlib
import time
from contextlib import closing, contextmanager
from typing import Any

import httpx
from video_platforms import VideoError

VERSION = '0.17.0'
MAX_HEADERS = 64 * 1024


@contextmanager
def response(url, address, headers, timeout, max_bytes):
    try:
        native: Any = importlib.import_module('curl_cffi')
        if native.__version__ != VERSION:
            raise ImportError('unsupported curl-cffi version')
    except ImportError as exc:
        raise VideoError('BACKEND_UNAVAILABLE', 'Install the pinned curl-cffi video dependency') from exc
    opt = native.CurlOpt
    host = httpx.URL(url).host
    pinned = f'[{address}]' if ':' in address else address
    body, head = bytearray(), bytearray()
    failure = None
    deadline = time.monotonic() + timeout

    def collect(chunk, output, limit):
        nonlocal failure
        if time.monotonic() > deadline:
            failure = VideoError('TIMEOUT', 'Video network request timed out')
        elif len(output) + len(chunk) > limit:
            failure = VideoError('LIMIT_EXCEEDED', 'Video response budget exceeded')
        if failure is not None:
            return 0xFFFFFFFF  # curl_cffi requires CURL_WRITEFUNC_ERROR, not a short count.
        output.extend(chunk)
        return len(chunk)

    with closing(native.Curl()) as curl:
        if curl.impersonate('chrome151', default_headers=True) != 0:
            raise VideoError('BACKEND_UNAVAILABLE', 'Browser-compatible video transport is unavailable')
        for key, value in (
            (opt.URL, url.encode()),
            (opt.RESOLVE, [f'{host}:443:{pinned}'.encode()]),
            (opt.PROXY, b''), (opt.NOPROXY, b'*'),
            (opt.FOLLOWLOCATION, 0), (opt.MAXREDIRS, 0),
            (opt.SSL_VERIFYPEER, 1), (opt.SSL_VERIFYHOST, 2),
            (opt.TIMEOUT_MS, max(1, int(timeout * 1000))),
            (opt.CONNECTTIMEOUT_MS, max(1, int(timeout * 1000))),
            (opt.HTTP_CONTENT_DECODING, 0),
            (opt.HTTPHEADER, [f'{k}: {v}'.encode() for k, v in headers.items()]),
            (opt.WRITEFUNCTION, lambda chunk: collect(chunk, body, max_bytes)),
            (opt.HEADERFUNCTION, lambda chunk: collect(chunk, head, MAX_HEADERS)),
        ):
            curl.setopt(key, value)
        try:
            curl.perform()
        except native.CurlError as exc:
            if failure is not None:
                raise failure from exc
            if exc.code == 28:
                raise VideoError('TIMEOUT', 'Video network request timed out') from exc
            raise VideoError('UPSTREAM_ERROR', 'Video network request failed') from exc
        if failure is not None:
            raise failure
        status = curl.getinfo(native.CurlInfo.RESPONSE_CODE)
    # Ignore informational header blocks, retaining repeated Set-Cookie fields.
    block = bytes(head).rstrip(b'\r\n').rsplit(b'\r\n\r\n', 1)[-1]
    fields = []
    for line in block.split(b'\r\n')[1:]:
        key, sep, value = line.partition(b':')
        if not sep:
            raise VideoError('UPSTREAM_ERROR', 'Invalid video response headers')
        fields.append((key, value.strip()))
    result = httpx.Response(status, headers=fields, stream=httpx.ByteStream(bytes(body)))
    try:
        yield result
    finally:
        result.close()
