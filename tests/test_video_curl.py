"""Native callback contract and the shared metadata safety boundary, offline."""

from __future__ import annotations

import importlib
import socket
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from video_curl import response  # noqa: E402
from video_http import SafeHTTP  # noqa: E402
from video_platforms import VideoError  # noqa: E402

URL = 'https://www.bilibili.com/video/BV1xx411c7mD'
ERROR = 0xFFFFFFFF
OPTIONS = ('URL', 'RESOLVE', 'PROXY', 'NOPROXY', 'FOLLOWLOCATION', 'MAXREDIRS',
           'SSL_VERIFYPEER', 'SSL_VERIFYHOST', 'TIMEOUT_MS', 'CONNECTTIMEOUT_MS',
           'HTTP_CONTENT_DECODING', 'HTTPHEADER', 'WRITEFUNCTION', 'HEADERFUNCTION')


class NativeError(Exception):
    def __init__(self, code):
        self.code = code


class CurlTests(unittest.TestCase):
    def native(self, body=b'ok', head=b'HTTP/2 200\r\nContent-Type: text/html\r\n\r\n', error=None):
        self.handles = []
        test = self

        class Curl:
            def __init__(self):
                self.options = {}
                self.closed = False
                self.body_callbacks = 0
                test.handles.append(self)

            def impersonate(self, target, default_headers):
                test.assertEqual((target, default_headers), ('chrome151', True))
                return 0

            def setopt(self, key, value):
                self.options[key] = value

            def perform(self):
                if error:
                    raise NativeError(error)
                if self.options['HEADERFUNCTION'](head) == ERROR:
                    raise NativeError(23)
                for chunk in (body, b'extra' if len(body) > 16 else b''):
                    if chunk:
                        self.body_callbacks += 1
                        if self.options['WRITEFUNCTION'](chunk) == ERROR:
                            raise NativeError(23)

            def getinfo(self, key):
                return 200

            def close(self):
                self.closed = True

        return SimpleNamespace(__version__='0.17.0', Curl=Curl, CurlError=NativeError,
                               CurlOpt=SimpleNamespace(**{key: key for key in OPTIONS}),
                               CurlInfo=SimpleNamespace(RESPONSE_CODE='RESPONSE_CODE'))

    def test_pinning_tls_no_proxy_no_redirect_decode_and_cleanup(self):
        native = self.native(head=b'HTTP/1.1 100 Continue\r\n\r\nHTTP/2 200\r\nSet-Cookie: a=1\r\nSet-Cookie: b=2\r\n\r\n')
        with patch.dict(sys.modules, {'curl_cffi': native}):
            with response(URL, '8.8.8.8', {'Host': 'www.bilibili.com'}, 2, 100) as result:
                self.assertEqual(result.read(), b'ok')
                self.assertEqual(result.headers.get_list('set-cookie'), ['a=1', 'b=2'])
        options = self.handles[0].options
        self.assertEqual(options['URL'], URL.encode())
        self.assertEqual(options['RESOLVE'], [b'www.bilibili.com:443:8.8.8.8'])
        self.assertEqual(options['PROXY'], b'')
        self.assertEqual(options['NOPROXY'], b'*')
        self.assertEqual((options['SSL_VERIFYPEER'], options['SSL_VERIFYHOST']), (1, 2))
        self.assertEqual((options['FOLLOWLOCATION'], options['HTTP_CONTENT_DECODING']), (0, 0))
        self.assertEqual(options['TIMEOUT_MS'], 2000)
        self.assertTrue(self.handles[0].closed)

    def test_body_and_header_abort_are_native_sentinels_and_close(self):
        for body, head in ((b'x' * 100, b'HTTP/2 200\r\n\r\n'),
                           (b'ok', b'HTTP/2 200\r\nX: ' + b'x' * 65536 + b'\r\n\r\n')):
            with patch.dict(sys.modules, {'curl_cffi': self.native(body, head)}):
                with self.assertRaises(VideoError) as caught:
                    with response(URL, '8.8.8.8', {}, 2, 10):
                        self.fail('must abort before exposing response')
            self.assertEqual(caught.exception.code, 'LIMIT_EXCEEDED')
            self.assertLessEqual(self.handles[0].body_callbacks, 1)
            self.assertTrue(self.handles[0].closed)

    def test_missing_version_timeout_and_transport_fail_closed(self):
        bad = self.native()
        bad.__version__ = '0.0.0'
        for native, expected in ((bad, 'BACKEND_UNAVAILABLE'),
                                 (self.native(error=28), 'TIMEOUT'),
                                 (self.native(error=60), 'UPSTREAM_ERROR')):
            with patch.dict(sys.modules, {'curl_cffi': native}), self.assertRaises(VideoError) as caught:
                with response(URL, '8.8.8.8', {}, 2, 100):
                    self.fail('failed transport must not yield')
            self.assertEqual(caught.exception.code, expected)
        with patch('video_curl.importlib.import_module', side_effect=ImportError), self.assertRaises(VideoError) as caught:
            with response(URL, '8.8.8.8', {}, 2, 100):
                pass
        self.assertEqual(caught.exception.code, 'BACKEND_UNAVAILABLE')

    def test_callback_deadline_and_ipv6_pin(self):
        with patch.dict(sys.modules, {'curl_cffi': self.native()}):
            with patch('video_curl.time.monotonic', side_effect=[0, 3]), self.assertRaises(VideoError) as caught:
                with response(URL, '2001:4860:4860::8888', {}, 2, 100):
                    pass
        self.assertEqual(caught.exception.code, 'TIMEOUT')
        self.assertEqual(self.handles[0].options['RESOLVE'],
                         [b'www.bilibili.com:443:[2001:4860:4860::8888]'])
        self.assertTrue(self.handles[0].closed)

    def test_real_low_level_callback_abort(self):
        try:
            native = importlib.import_module('curl_cffi.curl')
        except ImportError:
            self.skipTest('pinned curl-cffi required for native callback verification')
        # No network: exercise the package's CFFI callback, not a mock return value.
        data = native.ffi.new('char[]', b'over budget')
        handle = native.ffi.new_handle(native._CallbackContext(lambda _chunk: ERROR))
        self.assertEqual(native.lib.write_callback(data, 1, 11, handle), ERROR)


from test_video import VideoHTTPTests as _VideoHTTPTests  # noqa: E402


class BrowserHTTPTests(_VideoHTTPTests):
    def fetch(self, handler, url=URL, addresses=('8.8.8.8',), transport=None, **kwargs):
        @contextmanager
        def native(url, address, headers, timeout, max_bytes):
            self.assertFalse(any(key.lower() == 'user-agent' for key in headers))
            original = httpx.URL(url)
            request = httpx.Request('GET', original.copy_with(host=address), headers=headers,
                                    extensions={'sni_hostname': original.host})
            result = handler(request)
            if result.is_stream_consumed:
                result.stream = httpx.ByteStream(result.content)
                result.is_stream_consumed = False
                result.is_closed = False
            try:
                yield result
            finally:
                result.close()

        dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443)) for ip in addresses]
        transport = transport or SafeHTTP('bilibili', browser=True)
        transport.browser = True
        with patch('video_curl.response', native), patch('socket.getaddrinfo', return_value=dns):
            return transport.fetch(url, **kwargs)


class BrowserTransportWiringTests(unittest.TestCase):
    def test_browser_mode_is_only_used_for_bilibili(self) -> None:
        self.assertTrue(SafeHTTP('bilibili', browser=True).browser)
        self.assertFalse(SafeHTTP('douyin', browser=True).browser)
        self.assertFalse(SafeHTTP('bilibili', browser=False).browser)


del _VideoHTTPTests
