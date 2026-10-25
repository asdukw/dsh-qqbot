from __future__ import annotations

import json
import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from admin_ui import FAIL_LIMIT, FAIL_WINDOW, AdminHandler  # noqa: E402

SECRET = "private-request-sentinel"


class AdminLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = object.__new__(AdminHandler)
        self.handler.client_address = ("127.0.0.1", 1234)
        self.handler.command = "POST"
        self.handler.path = "/api/login"
        self.handler._failures = {}
        self.handler._source_allowed = Mock(return_value=True)
        self.handler._read_payload = Mock(return_value={"password": SECRET})
        self.handler._password = Mock(return_value="expected-password-sentinel")
        self.handler._send_json = Mock()
        self.handler._send_raw = Mock()

    def test_access_log_never_formats_arbitrary_method_path_or_errors(self) -> None:
        with self.assertLogs("qqbot.admin", level="INFO") as logs:
            for path in (f"/api/session?token={SECRET}", f"/api/{SECRET}", f"/assets/{SECRET}", f"/{SECRET}", f"http://[invalid/{SECRET}"):
                self.handler.path = path
                self.handler.command = SECRET
                self.handler.log_request(401)
                self.handler.log_error("bad request %s", SECRET)
        output = "\n".join(logs.output)
        self.assertNotIn(SECRET, output)
        for expected in ("method=OTHER", "path=/api/session", "path=/api/*", "path=/assets/*", "path=<other>", "path=<invalid>", "status=401"):
            self.assertIn(expected, output)

    def test_malformed_request_uses_safe_fallback(self) -> None:
        for request in (
            f"{SECRET} /?token={SECRET} INVALID\r\n\r\n",
            f"GET /?token={SECRET} HTTP/{SECRET}\r\n\r\n",
            f"{SECRET} /?token={SECRET} HTTP/1.1\r\n\r\n",
        ):
            with self.subTest(request=request):
                handler = object.__new__(AdminHandler)
                handler.client_address = ("127.0.0.1", 1234)
                handler.rfile = BytesIO(request.encode())
                handler.wfile = BytesIO()
                with self.assertLogs("qqbot.admin", level="INFO") as logs:
                    handler.handle_one_request()
                self.assertNotIn(SECRET, "\n".join(logs.output))
                self.assertRegex(handler.wfile.getvalue().decode(), r"Error code: (400|501)")

    def test_limit_event_once_per_window_and_source(self) -> None:
        with patch("admin_ui.time.time", return_value=100):
            with self.assertLogs("qqbot.admin", level="WARNING") as logs:
                for _ in range(FAIL_LIMIT):
                    self.handler.do_POST()
                for _ in range(3):
                    self.handler.do_POST()
            self.assertEqual(self.handler._send_json.call_args.args[0], 429)
        output = "\n".join(logs.output)
        self.assertEqual(output.count("outcome=rate_limited"), 1)
        self.assertEqual(output.count("outcome=invalid_password"), FAIL_LIMIT)
        self.assertNotIn(SECRET, output)
        with patch("admin_ui.time.time", return_value=100 + FAIL_WINDOW + 1):
            with self.assertLogs("qqbot.admin", level="WARNING") as renewed:
                for _ in range(FAIL_LIMIT):
                    self.handler.do_POST()
            self.assertEqual(self.handler._send_json.call_args.args[0], 401)
        self.assertEqual("\n".join(renewed.output).count("outcome=rate_limited"), 1)
        other = object.__new__(AdminHandler)
        other.client_address = ("127.0.0.2", 1234)
        other._failures = self.handler._failures
        self.assertTrue(other._allow_login())

    def test_password_not_configured_keeps_503_and_no_secret(self) -> None:
        self.handler._password = Mock(return_value="")
        with self.assertLogs("qqbot.admin", level="DEBUG") as logs:
            self.handler.do_POST()
        self.assertEqual(self.handler._send_json.call_args.args[0], 503)
        self.assertIn("outcome=password_not_configured", "\n".join(logs.output))
        self.assertNotIn(SECRET, "\n".join(logs.output))

    def test_success_is_after_session_issuance_and_keeps_cookie(self) -> None:
        self.handler._password = Mock(return_value=SECRET)
        self.handler._issue = Mock(return_value="cookie-sentinel")
        self.handler._session_hours = Mock(return_value=1)
        for path, status in (("/api/login", 200), ("/login", 303)):
            with self.subTest(path=path):
                self.handler.path = path
                with self.assertLogs("qqbot.admin", level="INFO") as logs:
                    self.handler.do_POST()
                sent = self.handler._send_json if status == 200 else self.handler._send_raw
                self.assertEqual(sent.call_args.args[0], status)
                cookie = sent.call_args.kwargs["headers"]["Set-Cookie"]
                for expected in ("cookie-sentinel", "HttpOnly", "SameSite=Strict", "Max-Age=3600"):
                    self.assertIn(expected, cookie)
                output = "\n".join(logs.output)
                self.assertIn("outcome=success ip=127.0.0.1", output)
                self.assertNotIn(SECRET, output)
                self.assertNotIn("cookie-sentinel", output)
        self.handler._issue = Mock(side_effect=RuntimeError("expected"))
        with self.assertNoLogs("qqbot.admin", level="INFO"), self.assertRaises(RuntimeError):
            self.handler.do_POST()

    def test_body_headers_and_forged_forwarded_ip_are_not_logged(self) -> None:
        self.handler.path = f"/api/login?password={SECRET}"
        self.handler.headers = {"Cookie": SECRET, "Authorization": SECRET, "X-Forwarded-For": SECRET}
        self.handler._read_payload = Mock(return_value=json.loads(json.dumps({"password": SECRET})))
        with self.assertLogs("qqbot.admin", level="INFO") as logs:
            self.handler.do_POST()
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertIn("ip=127.0.0.1", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
