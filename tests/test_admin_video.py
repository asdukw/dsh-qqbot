from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from email.message import Message
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from admin_ui import COOKIE, AdminHandler, issue_session  # noqa: E402
from admin_video import handle_post, local_channel  # noqa: E402

SECRET = 'private-cookie-sentinel=='


class AdminVideoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.previous = AdminHandler.config
        AdminHandler.config = {'video': {'credentials_directory': str(root / 'credentials')}}
        self.environment = patch.dict(os.environ, {'ADMIN_UI_PASSWORD': 'password', 'ADMIN_UI_SECRET': 'local-test'})
        self.environment.start()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), AdminHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = f'127.0.0.1:{self.server.server_port}'
        self.cookie = f'{COOKIE}={issue_session(b"local-test", 1)}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        AdminHandler.config = self.previous
        self.environment.stop()
        self.tmp.cleanup()

    def request(self, method='GET', path='/api/video/credentials', payload=None, *,
                auth=True, headers=None, raw=None):
        connection = http.client.HTTPConnection(self.host, timeout=5)
        base = {'Content-Type': 'application/json', 'Origin': 'http://' + self.host}
        if auth:
            base['Cookie'] = self.cookie
        base.update(headers or {})
        body = raw if raw is not None else json.dumps(payload).encode() if payload is not None else None
        connection.request(method, path, body=body, headers=base)
        response = connection.getresponse()
        status, cache = response.status, response.getheader('Cache-Control')
        data = response.read().decode()
        connection.close()
        self.assertNotIn(SECRET, data)
        return status, json.loads(data), cache

    def test_auth_save_replace_status_delete_and_no_secret_logs(self):
        self.assertEqual(self.request(auth=False)[0], 401)
        self.assertEqual(self.request('POST', payload={'platform': 'bilibili', 'format': 'header', 'content': SECRET}, auth=False)[0], 401)
        with self.assertLogs('qqbot.admin', level='INFO') as logs:
            code, data, cache = self.request()
            self.assertEqual((code, cache), (200, 'no-store'))
            self.assertTrue(data['can_write'])
            self.assertFalse(data['platforms'][0]['configured'])
            payload = {'platform': 'bilibili', 'format': 'header', 'content': 'SESSDATA=' + SECRET}
            self.assertEqual(self.request('POST', payload=payload)[0], 200)
            data = self.request()[1]
            self.assertTrue(data['platforms'][0]['configured'])
            self.assertEqual(data['platforms'][0]['source'], 'managed')
            self.assertFalse(data['platforms'][1]['enabled'])
            self.assertEqual(self.request('POST', payload={**payload, 'content': 'SESSDATA=replacement'})[0], 200)
            self.assertEqual(self.request('POST', '/api/video/credentials/delete', {'platform': 'bilibili'})[0], 200)
            item = self.request()[1]['platforms'][0]
            self.assertEqual((item['source'], item['configured']), ('cleared', False))
        self.assertNotIn(SECRET, '\n'.join(logs.output))
        self.assertNotIn('SESSDATA', '\n'.join(logs.output))

    def test_strict_json_size_types_and_no_truncation(self):
        for raw in (b'[]', b'[' * 1100 + b']' * 1100, b'{', b'\xff', b'{"platform":"bilibili","platform":"douyin"}',
                    json.dumps({'platform': 'bilibili', 'format': 'header', 'content': '', 'path': '/private'}).encode(),
                    json.dumps({'platform': 'bilibili', 'format': 'header', 'content': True}).encode(),
                    json.dumps({'platform': ['bilibili'], 'format': 'header', 'content': SECRET}).encode()):
            with self.subTest(raw=raw[:20]):
                self.assertEqual(self.request('POST', raw=raw)[0], 400)
        self.assertEqual(self.request('POST', raw=b'x' * 100000)[0], 413)
        self.assertEqual(self.request('POST', payload={'platform': 'bilibili', 'format': 'header', 'content': 'x=' + 'a' * 66000})[0], 400)
        self.assertFalse(self.request()[1]['platforms'][0]['configured'])

    def test_origin_host_and_proxy_headers_cannot_authorize_public_write(self):
        payload = {'platform': 'bilibili', 'format': 'header', 'content': 'x=' + SECRET}
        for headers in ({'Origin': 'http://evil.test'}, {'Origin': 'https://' + self.host},
                        {'Origin': 'null'}, {'Host': 'example.com', 'Origin': 'http://example.com'},
                        {'Host': 'example.com', 'Origin': 'https://example.com', 'X-Forwarded-Proto': 'https'},
                        {'Origin': 'http://' + self.host + '/extra'}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request('POST', payload=payload, headers=headers)[0], 403)
        self.assertFalse(self.request(headers={'Host': 'example.com'})[1]['can_write'])
        self.assertEqual(self.request('POST', payload=payload, headers={'Content-Type': 'text/plain'})[0], 415)
        message = Message()
        message['Host'] = self.host
        message['Origin'] = 'http://' + self.host
        handler = SimpleNamespace(headers=message, client_address=('203.0.113.1', 123))
        self.assertFalse(local_channel(handler, mutation=True))
        handler.client_address = ('127.0.0.1', 123)
        self.assertTrue(local_channel(handler, mutation=True))
        message['Host'] = self.host
        self.assertFalse(local_channel(handler, mutation=True))

    def test_duplicate_headers_and_transfer_encoding_are_rejected(self):
        from io import BytesIO
        for key, value in (('Content-Length', '2'), ('Host', self.host), ('Origin', 'http://' + self.host),
                           ('Content-Type', 'application/json'), ('Cookie', self.cookie),
                           ('Transfer-Encoding', 'chunked')):
            headers = Message()
            for name, item in (('Host', self.host), ('Origin', 'http://' + self.host),
                               ('Content-Type', 'application/json'), ('Content-Length', '2'),
                               ('Cookie', self.cookie), (key, value)):
                headers[name] = item
            handler = SimpleNamespace(headers=headers, client_address=('127.0.0.1', 1),
                _api_guard=Mock(return_value=True), _send_json=Mock(), connection=Mock(), rfile=BytesIO(b'{}'))
            handle_post(handler, '/api/video/credentials')
            self.assertIn(handler._send_json.call_args.args[0], (400, 403, 415))

    def test_missing_origin_rejected_and_storage_errors_are_sanitized(self):
        connection = http.client.HTTPConnection(self.host, timeout=5)
        connection.request('POST', '/api/video/credentials/delete', b'{"platform":"bilibili"}',
                           {'Cookie': self.cookie, 'Content-Type': 'application/json'})
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        connection.close()
        with patch('admin_video.save_credentials', side_effect=OSError(SECRET)):
            self.assertEqual(self.request('POST', payload={'platform': 'bilibili', 'format': 'header', 'content': 'x=' + SECRET})[0], 400)


if __name__ == '__main__':
    unittest.main()
