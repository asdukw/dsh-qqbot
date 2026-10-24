from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from admin_ui import (  # noqa: E402
    AdminHandler,
    conversations_data,
    db_rows,
    fmt_ts,
    ip_allowed,
    issue_session,
    page,
    scalar,
    valid_session,
)
from usage import UsageStore  # noqa: E402


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


class ConversationMergeTests(unittest.TestCase):
    def test_usage_without_conversation_id_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            usage = UsageStore(root / "usage.db")
            usage.record(
                source="thin",
                purpose="check",
                conversation_id="",
                input_tokens=10,
                output_tokens=1,
                cache_read_tokens=0,
            )
            usage.close()
            config = {
                "paths": {
                    "database": str(root / "memory.db"),
                    "profile_db": str(root / "profile.db"),
                },
                "usage": {"database": str(root / "usage.db")},
            }
            self.assertEqual(conversations_data(config)["conversations"], [])


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
            "memory": """
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE chat_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    chat_type TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    sender_name TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE session_state (
                    conversation_id TEXT PRIMARY KEY,
                    generation INTEGER NOT NULL DEFAULT 0,
                    started_at INTEGER NOT NULL,
                    last_active INTEGER NOT NULL,
                    messages INTEGER NOT NULL DEFAULT 0
                );
            """,
            "models": """
                CREATE TABLE models (
                    provider TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    api TEXT NOT NULL DEFAULT 'openai-completions',
                    base_url TEXT NOT NULL,
                    api_key_env TEXT NOT NULL DEFAULT 'LLM_API_KEY',
                    context_window INTEGER NOT NULL,
                    max_output_tokens INTEGER NOT NULL DEFAULT 2048,
                    reasoning_effort TEXT NOT NULL DEFAULT 'low',
                    capabilities TEXT NOT NULL DEFAULT '{}',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    is_active INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (provider, model_id)
                );
            """,
            "cards": """
                CREATE TABLE cards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    nickname TEXT NOT NULL DEFAULT '',
                    aliases TEXT NOT NULL DEFAULT '[]',
                    description TEXT NOT NULL DEFAULT '',
                    personality TEXT NOT NULL DEFAULT '',
                    scenario TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '[]',
                    creator TEXT NOT NULL DEFAULT '',
                    character_version TEXT NOT NULL DEFAULT '',
                    fingerprint TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    source_kind TEXT NOT NULL DEFAULT 'file',
                    source TEXT NOT NULL DEFAULT '',
                    source_mtime REAL NOT NULL DEFAULT 0,
                    installed_by TEXT NOT NULL DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
            """,
            "tasks": """
                CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_type TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    created_by TEXT NOT NULL,
                    instruction TEXT NOT NULL,
                    message TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    time_of_day TEXT,
                    weekday INTEGER,
                    run_at INTEGER,
                    every_minutes INTEGER,
                    mode TEXT NOT NULL DEFAULT 'fixed',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    next_run INTEGER NOT NULL,
                    last_run INTEGER NOT NULL DEFAULT 0,
                    runs INTEGER NOT NULL DEFAULT 0
                );
            """,
            "feeds": """
                CREATE TABLE feeds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_type TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    feed_url TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'rss',
                    seen TEXT NOT NULL DEFAULT '[]',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    last_check INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT ''
                );
            """,
            "stickers": """
                CREATE TABLE stickers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sha1 TEXT NOT NULL UNIQUE,
                    path TEXT NOT NULL,
                    mime TEXT NOT NULL DEFAULT '',
                    size INTEGER NOT NULL DEFAULT 0,
                    description TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '[]',
                    source TEXT NOT NULL DEFAULT '',
                    source_kind TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'ok',
                    uses INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    last_used INTEGER NOT NULL DEFAULT 0
                );
            """,
        }.items():
            path = root / f"{name}.db"
            conn = sqlite3.connect(path)
            conn.executescript(schema)
            conn.commit()
            conn.close()
            self.dbs[name] = str(path)

        now = int(time.time())
        conn = sqlite3.connect(self.dbs["models"])
        conn.execute(
            "INSERT INTO models VALUES "
            "('opencode-go', 'flash', 'Flash', 'openai-completions', "
            "'https://example.test/v1', 'LLM_API_KEY', 1000000, 4096, 'low', "
            "'{\"tools\": true}', 1, 1, ?)",
            (now,),
        )
        conn.commit()
        conn.close()
        conn = sqlite3.connect(self.dbs["cards"])
        conn.execute(
            "INSERT INTO cards "
            "(id, name, nickname, aliases, description, personality, scenario, tags, "
            "creator, character_version, fingerprint, payload, source_kind, source, "
            "source_mtime, installed_by, is_active, created_at, updated_at) "
            "VALUES (1, '卡', '<script>card</script>', '[\"小鱼\"]', '一条鱼', "
            "'温和', '群里', '[\"日常\"]', '作者', '1.0', 'fp', '{}', 'file', "
            "'/tmp/card.json', 0, 'admin', 1, ?, ?)",
            (now, now),
        )
        conn.commit()
        conn.close()
        conn = sqlite3.connect(self.dbs["tasks"])
        conn.execute(
            "INSERT INTO tasks "
            "(id, chat_type, chat_id, created_by, instruction, message, kind, "
            "time_of_day, weekday, run_at, every_minutes, mode, enabled, created_at, "
            "next_run, last_run, runs) "
            "VALUES (1, 'group', 42, '1', '每天八点提醒', '喝水', 'daily', '08:00', "
            "NULL, NULL, NULL, 'fixed', 1, ?, 1792800000, ?, 3)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO tasks "
            "(id, chat_type, chat_id, created_by, instruction, message, kind, "
            "time_of_day, weekday, run_at, every_minutes, mode, enabled, created_at, "
            "next_run, last_run, runs) "
            "VALUES (2, 'private', 7, '1', '暂停的任务', '晚安', 'daily', '23:00', "
            "NULL, NULL, NULL, 'generate', 0, ?, 0, 0, 0)",
            (now,),
        )
        conn.commit()
        conn.close()
        conn = sqlite3.connect(self.dbs["feeds"])
        conn.execute(
            "INSERT INTO feeds "
            "(id, chat_type, chat_id, feed_url, title, source, seen, enabled, "
            "created_at, last_check, last_error) "
            "VALUES (1, 'group', 42, 'https://x', '央视新闻', 'rss', '[\"a\",\"b\"]', "
            "1, ?, ?, '')",
            (now, now),
        )
        conn.execute(
            "INSERT INTO feeds "
            "(id, chat_type, chat_id, feed_url, title, source, seen, enabled, "
            "created_at, last_check, last_error) "
            "VALUES (2, 'private', 7, 'https://y', 'UP 主动态', 'bilibili', '[]', "
            "1, ?, ?, 'HTTP 412')",
            (now, now),
        )
        conn.commit()
        conn.close()
        conn = sqlite3.connect(self.dbs["stickers"])
        conn.execute(
            "INSERT INTO stickers VALUES "
            "(1, 'sha1', '/tmp/a.png', 'image/png', 1024, '开心', '[\"笑\"]', "
            "'group:42', 'collected', 'ok', 4, ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO stickers VALUES "
            "(2, 'sha2', '/tmp/b.webp', 'image/webp', 2048, '无语', '[]', "
            "'group:42', 'collected', 'banned', 0, ?, 0)",
            (now,),
        )
        conn.execute(
            "INSERT INTO stickers VALUES "
            "(3, 'sha3', '/tmp/c.jpg', 'image/jpeg', 4096, '点赞', '[\"好耶\"]', "
            "'manual', 'upload', 'ok', 1, ?, ?)",
            (now, now),
        )
        conn.commit()
        conn.close()

        conn = sqlite3.connect(self.dbs["memory"])
        conn.execute(
            "INSERT INTO chat_events "
            "(conversation_id, chat_type, chat_id, user_id, sender_name, text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("group:42", "group", 42, 1, "小明", "hello hello 测试测试", now),
        )
        conn.execute(
            "INSERT INTO chat_events "
            "(conversation_id, chat_type, chat_id, user_id, sender_name, text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("group:42", "group", 42, 2, "小红", "hello 今天吃什么", now),
        )
        conn.execute(
            "INSERT INTO session_state VALUES (?, ?, ?, ?, ?)",
            ("group:42", 2, now - 1000, now - 10, 5),
        )
        conn.commit()
        conn.close()

        profile_db = root / "profile.db"
        conn = sqlite3.connect(profile_db)
        conn.execute(
            "CREATE TABLE conversations (session_id TEXT PRIMARY KEY, scope TEXT NOT NULL, "
            "user_id TEXT NOT NULL, group_id TEXT, sender_name TEXT NOT NULL DEFAULT '', "
            "updated_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO conversations VALUES (?, ?, ?, ?, ?, ?)",
            ("group-42-g2", "group", "2", "42", "小红", now),
        )
        conn.commit()
        conn.close()

        usage = UsageStore(root / "usage.db")
        usage.record(
            source="dsh", purpose="chat", conversation_id="group:42",
            input_tokens=100, output_tokens=10, cache_read_tokens=900,
            created_at=now,
        )
        usage.record(
            source="thin", purpose="image_caption", conversation_id="private:7",
            input_tokens=20, output_tokens=5, cache_read_tokens=0,
            created_at=now,
        )
        usage.close()

        dist = root / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text(
            '<!doctype html><div id="root">admin-spa</div>', encoding="utf-8"
        )
        (dist / "assets" / "app-abc.js").write_text("console.log(1)", encoding="utf-8")

        self.config = {
            "paths": {
                "database": self.dbs["memory"],
                "profile_db": str(profile_db),
            },
            "models": {"database": self.dbs["models"]},
            "cards": {"database": self.dbs["cards"]},
            "tasks": {"database": self.dbs["tasks"]},
            "feeds": {"database": self.dbs["feeds"]},
            "stickers": {"database": self.dbs["stickers"]},
            "usage": {"database": str(root / "usage.db"), "retention_days": 90},
            "dsh": {
                "home": str(root / "dsh-home"),
                "workspace": str(root / "dsh-workspace"),
                "profile": "qqbot",
            },
            "admin": {"host": "127.0.0.1", "port": 8080, "session_hours": 1, "static_dir": str(dist)},
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

        response = self.get("/api/overview")
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
        self.assertEqual(payload["tasks"], {"total": 2, "enabled": 1})
        self.assertEqual(payload["feeds"], {"total": 2})
        self.assertEqual(payload["task_rows"][0]["message"], "喝水")
        self.assertEqual(payload["feed_rows"][0]["title"], "央视新闻")
        self.assertEqual(payload["card_rows"][0]["aliases"], ["小鱼"])
        self.assertGreater(payload["generated_at"], 0)

    def test_overview_reports_counts_trend_and_alerts(self) -> None:
        cookie, _ = self.login()
        response = self.get("/api/overview?days=7", cookie)
        self.assertEqual(response.status, 200)
        payload = json.loads(response.read().decode())

        self.assertEqual(payload["days"], 7)
        self.assertEqual(payload["active_model"], "opencode-go/flash")
        self.assertEqual(payload["active_card"], "<script>card</script>")
        self.assertEqual(len(payload["trend"]), 7)
        self.assertEqual(sum(row["messages"] for row in payload["trend"]), 2)
        self.assertEqual(sum(row["tokens"] for row in payload["trend"]), 1035)
        self.assertEqual(payload["today"]["messages"], 2)
        self.assertEqual(payload["today"]["tokens"], 1035)
        self.assertEqual(payload["today"]["calls"], 2)
        self.assertEqual(payload["counts"]["conversations"], 2)
        self.assertEqual(payload["counts"]["models"], {"total": 1, "enabled": 1})
        self.assertEqual(payload["counts"]["cards"], {"total": 1})
        self.assertEqual(payload["counts"]["tasks"], {"total": 2, "enabled": 1})
        self.assertEqual(payload["counts"]["feeds"], {"total": 2, "errors": 1})
        self.assertEqual(payload["counts"]["stickers"], {"total": 3, "banned": 1})
        self.assertEqual(payload["alerts"][0]["kind"], "feed")
        self.assertIn("412", payload["alerts"][0]["detail"])
        self.assertEqual(payload["recent_messages"][0]["sender_name"], "小红")

    def test_conversations_merge_events_sessions_and_usage(self) -> None:
        cookie, _ = self.login()
        response = self.get("/api/conversations", cookie)
        self.assertEqual(response.status, 200)
        rows = {
            row["id"]: row
            for row in json.loads(response.read().decode())["conversations"]
        }

        group = rows["group:42"]
        self.assertEqual(group["scope"], "group")
        self.assertEqual(group["chat_id"], 42)
        self.assertEqual(group["messages"], 2)
        self.assertEqual(group["tokens"], 1010)
        self.assertEqual(group["generation"], 2)
        self.assertEqual(group["session_id"], "group-42-g2")
        self.assertEqual(group["session_messages"], 5)

        private = rows["private:7"]
        self.assertEqual(private["scope"], "private")
        self.assertEqual(private["chat_id"], 7)
        self.assertEqual(private["messages"], 0)
        self.assertEqual(private["tokens"], 25)
        self.assertIsNone(private["session_id"])

    def test_model_and_card_endpoints(self) -> None:
        cookie, _ = self.login()

        models = json.loads(self.get("/api/models", cookie).read().decode())
        self.assertEqual(models["active"], "opencode-go/flash")
        row = models["models"][0]
        self.assertEqual(row["display_name"], "Flash")
        self.assertEqual(row["capabilities"], {"tools": True})
        self.assertTrue(row["is_active"])

        cards = json.loads(self.get("/api/cards", cookie).read().decode())
        row = cards["cards"][0]
        self.assertEqual(row["nickname"], "<script>card</script>")
        self.assertEqual(row["aliases"], ["小鱼"])
        self.assertEqual(row["tags"], ["日常"])
        self.assertEqual(row["description"], "一条鱼")
        self.assertTrue(row["is_active"])

    def test_task_feed_and_sticker_endpoints(self) -> None:
        cookie, _ = self.login()

        tasks = json.loads(self.get("/api/tasks", cookie).read().decode())
        self.assertEqual(tasks["tasks"][0]["id"], 1)
        self.assertTrue(tasks["tasks"][0]["enabled"])
        self.assertEqual(tasks["tasks"][1]["mode"], "generate")
        self.assertFalse(tasks["tasks"][1]["enabled"])

        feeds = json.loads(self.get("/api/feeds", cookie).read().decode())
        self.assertEqual(feeds["errors"], 1)
        self.assertEqual(feeds["feeds"][0]["seen_count"], 2)
        self.assertEqual(feeds["feeds"][1]["last_error"], "HTTP 412")

        stickers = json.loads(self.get("/api/stickers", cookie).read().decode())
        self.assertEqual(stickers["totals"], {"total": 3, "banned": 1, "used": 2})
        by_id = {row["id"]: row for row in stickers["stickers"]}
        self.assertEqual(by_id[1]["tags"], ["笑"])
        self.assertEqual(by_id[2]["status"], "banned")

    def test_system_endpoint_lists_databases(self) -> None:
        cookie, _ = self.login()
        response = self.get("/api/system", cookie)
        self.assertEqual(response.status, 200)
        payload = json.loads(response.read().decode())

        self.assertEqual(payload["admin"]["port"], 8080)
        self.assertEqual(payload["dsh"]["profile"], "qqbot")
        self.assertEqual(payload["usage"]["retention_days"], 90)
        self.assertGreaterEqual(payload["uptime_seconds"], 0)
        self.assertTrue(payload["python"].startswith("3."))
        dbs = {row["key"]: row for row in payload["databases"]}
        self.assertTrue(dbs["memory"]["exists"])
        self.assertGreater(dbs["memory"]["size"], 0)
        self.assertEqual(dbs["usage"]["path"], str(Path(self.tmp.name) / "usage.db"))

    def test_usage_and_fun_can_filter_conversation(self) -> None:
        self.assertEqual(self.get("/api/usage?days=7").status, 401)
        cookie, _ = self.login()

        response = self.get("/api/usage?days=7", cookie)
        self.assertEqual(response.status, 200)
        usage = json.loads(response.read().decode())
        self.assertEqual(usage["totals"]["total"], 1035)
        self.assertEqual(
            {row["id"] for row in usage["conversations"]},
            {"group:42", "private:7"},
        )

        filtered = self.get("/api/usage?days=7&conversation=group:42", cookie)
        self.assertEqual(filtered.status, 200)
        payload = json.loads(filtered.read().decode())
        self.assertEqual(payload["totals"]["total"], 1010)

        fun = self.get("/api/fun?conversation=group:42", cookie)
        self.assertEqual(fun.status, 200)
        payload = json.loads(fun.read().decode())
        self.assertEqual({row["name"] for row in payload["ranking"]}, {"小明", "小红"})
        self.assertTrue(
            any(word["text"] == "hello" and word["count"] >= 3 for word in payload["words"])
        )

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
