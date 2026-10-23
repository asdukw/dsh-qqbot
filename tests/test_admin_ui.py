from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from admin_ui import (  # noqa: E402
    AdminHandler,
    db_rows,
    fmt_ts,
    ip_allowed,
    issue_session,
    page,
    scalar,
    valid_session,
)


class SessionTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        token = issue_session(b"secret", 1, now=1000)
        self.assertTrue(valid_session(b"secret", token, now=1001))
        self.assertFalse(valid_session(b"other", token, now=1001))

    def test_expired_and_tampered(self) -> None:
        token = issue_session(b"secret", 1, now=1000)
        self.assertFalse(valid_session(b"secret", token, now=1000 + 3700))
        expires, _, _ = token.partition(".")
        self.assertFalse(valid_session(b"secret", f"{expires}.deadbeef", now=1001))
        self.assertFalse(valid_session(b"secret", "nonsense", now=1001))


class HelperTests(unittest.TestCase):
    def test_page_escapes_title(self) -> None:
        out = page("<script>x</script>", "<p>body</p>").decode()
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("<script>x</script>", out)

    def test_db_rows_and_scalar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "x.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE t (id INTEGER, name TEXT)")
            conn.execute("INSERT INTO t VALUES (1, 'a'), (2, 'b')")
            conn.commit()
            conn.close()
            self.assertEqual(len(db_rows(str(db), "SELECT * FROM t")), 2)
            self.assertEqual(scalar(str(db), "SELECT COUNT(*) FROM t"), 2)
            self.assertIsNone(scalar(str(db), "SELECT x FROM missing"))
            self.assertEqual(db_rows(str(Path(tmp) / "nope.db"), "SELECT 1"), [])

    def test_fmt_ts(self) -> None:
        self.assertIn("2026-", fmt_ts(1792743000))
        self.assertEqual(fmt_ts(None), "-")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AllowlistTests(unittest.TestCase):
    def test_empty_means_any(self) -> None:
        self.assertTrue(ip_allowed("1.2.3.4", []))
        self.assertTrue(ip_allowed("1.2.3.4", None))

    def test_cidr_match(self) -> None:
        self.assertTrue(ip_allowed("203.0.113.9", ["203.0.113.0/24"]))
        self.assertFalse(ip_allowed("198.51.100.1", ["203.0.113.0/24"]))
        self.assertFalse(ip_allowed("not-an-ip", ["203.0.113.0/24"]))


class HttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.dbs = {}
        for name, schema in {
            "memory": "CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id TEXT, role TEXT, content TEXT, created_at INTEGER)",
            "models": "CREATE TABLE models (provider TEXT, model_id TEXT, context_window INTEGER, is_active INTEGER, enabled INTEGER)",
            "cards": "CREATE TABLE cards (id INTEGER, name TEXT, nickname TEXT, aliases TEXT, is_active INTEGER)",
            "tasks": "CREATE TABLE tasks (id INTEGER, kind TEXT, time_of_day TEXT, weekday INTEGER, every_minutes INTEGER, mode TEXT, enabled INTEGER, next_run INTEGER, message TEXT)",
            "feeds": "CREATE TABLE feeds (id INTEGER, chat_type TEXT, chat_id INTEGER, title TEXT, feed_url TEXT, last_error TEXT)",
            "stickers": "CREATE TABLE stickers (id INTEGER PRIMARY KEY, status TEXT)",
        }.items():
            path = root / f"{name}.db"
            conn = sqlite3.connect(path)
            conn.executescript(schema)
            conn.commit()
            conn.close()
            self.dbs[name] = str(path)

        conn = sqlite3.connect(self.dbs["models"])
        conn.execute("INSERT INTO models VALUES ('opencode-go', 'flash', 1000000, 1, 1)")
        conn.commit()
        conn.close()
        conn = sqlite3.connect(self.dbs["cards"])
        conn.execute("INSERT INTO cards VALUES (1, '卡', '<script>card</script>', '[]', 1)")
        conn.commit()
        conn.close()
        conn = sqlite3.connect(self.dbs["tasks"])
        conn.execute("INSERT INTO tasks VALUES (1, 'daily', '08:00', NULL, NULL, 'fixed', 1, 1792800000, '喝水')")
        conn.commit()
        conn.close()
        conn = sqlite3.connect(self.dbs["feeds"])
        conn.execute("INSERT INTO feeds VALUES (1, 'group', 42, '央视新闻', 'https://x', '')")
        conn.commit()
        conn.close()

        dist = root / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text(
            '<!doctype html><div id="root">admin-spa</div>', encoding="utf-8"
        )
        (dist / "assets" / "app-abc.js").write_text("console.log(1)", encoding="utf-8")

        self.config = {
            "paths": {"database": self.dbs["memory"]},
            "models": {"database": self.dbs["models"]},
            "cards": {"database": self.dbs["cards"]},
            "tasks": {"database": self.dbs["tasks"]},
            "feeds": {"database": self.dbs["feeds"]},
            "stickers": {"database": self.dbs["stickers"]},
            "admin": {"session_hours": 1, "static_dir": str(dist)},
        }
        self.old_env = {k: os.environ.get(k) for k in ("ADMIN_UI_PASSWORD", "ADMIN_UI_SECRET")}
        os.environ["ADMIN_UI_PASSWORD"] = "test-password"
        os.environ["ADMIN_UI_SECRET"] = "test-secret"
        AdminHandler.config = self.config
        AdminHandler.static_dir = str(dist)
        AdminHandler._failures.clear()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), AdminHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        AdminHandler._failures.clear()
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def get(self, path: str, cookie: str = ""):
        request = urllib.request.Request(self.base + path)
        if cookie:
            request.add_header("Cookie", cookie)
        try:
            return urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            return exc

    def login(self, password: str = "test-password"):
        data = json.dumps({"password": password}).encode()
        request = urllib.request.Request(
            self.base + "/api/login",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        opener = urllib.request.build_opener(NoRedirect)
        try:
            response = opener.open(request, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        cookie = response.headers.get("Set-Cookie", "").split(";")[0]
        return cookie, response

    def test_healthz_needs_no_auth(self) -> None:
        response = self.get("/healthz")
        self.assertEqual(response.status, 200)

    def test_spa_and_dashboard_require_login(self) -> None:
        response = self.get("/")
        self.assertEqual(response.status, 200)
        self.assertIn("admin-spa", response.read().decode())

        response = self.get("/api/dashboard")
        self.assertEqual(response.status, 401)

        session = self.get("/api/session")
        self.assertEqual(json.loads(session.read().decode()), {"authenticated": False})

        cookie, login_response = self.login()
        self.assertEqual(login_response.status, 200)
        self.assertIn("qqbot_admin=", cookie)
        self.assertEqual(json.loads(login_response.read().decode()), {"ok": True})

        response = self.get("/api/dashboard", cookie)
        text = response.read().decode()
        self.assertEqual(response.status, 200)
        payload = json.loads(text)
        self.assertEqual(payload["active_model"], "opencode-go/flash")
        self.assertEqual(payload["active_card"], "<script>card</script>")
        self.assertEqual(payload["messages"]["total"], 0)
        self.assertEqual(payload["tasks"], {"total": 1, "enabled": 1})
        self.assertEqual(payload["feeds"], {"total": 1})
        self.assertEqual(payload["task_rows"][0]["message"], "喝水")
        self.assertEqual(payload["feed_rows"][0]["title"], "央视新闻")
        self.assertEqual(payload["card_rows"][0]["aliases"], [])
        self.assertGreater(payload["generated_at"], 0)

    def test_wrong_password_is_rejected(self) -> None:
        _, response = self.login("nope")
        self.assertEqual(response.status, 401)
        self.assertIn("密码不对", response.read().decode())

    def test_static_assets_are_cached(self) -> None:
        response = self.get("/assets/app-abc.js")
        self.assertEqual(response.status, 200)
        self.assertIn("javascript", response.headers.get("Content-Type", ""))
        self.assertIn("immutable", response.headers.get("Cache-Control", ""))

    def test_logout_clears_the_session(self) -> None:
        cookie, _ = self.login()
        request = urllib.request.Request(self.base + "/api/logout", data=b"", method="POST")
        request.add_header("Cookie", cookie)
        response = urllib.request.urlopen(request, timeout=5)
        self.assertEqual(response.status, 200)
        self.assertIn("Max-Age=0", response.headers.get("Set-Cookie", ""))
        # Logout is cookie-based: the browser drops it. A replayed cookie
        # remains valid until its HMAC expiry, so only assert the clear header.
        session = json.loads(self.get("/api/session").read().decode())
        self.assertFalse(session["authenticated"])

    def test_path_traversal_is_rejected(self) -> None:
        response = self.get("/assets/../../etc/passwd")
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
