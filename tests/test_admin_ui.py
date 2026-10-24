from __future__ import annotations

import base64
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
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from admin_ui import (  # noqa: E402
    AdminHandler,
    ProviderModelsError,
    _model_list,
    _provider_models_url,
    conversations_data,
    db_paths,
    db_rows,
    fmt_ts,
    ip_allowed,
    issue_session,
    page,
    scalar,
    sticker_directory,
    sticker_target,
    valid_session,
)
from usage import UsageStore  # noqa: E402

# 1×1 transparent PNG: a real file the image endpoint can decode.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def rgba_png(width: int, height: int) -> bytes:
    """A PNG with a transparent border and an opaque red centre."""
    from io import BytesIO

    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle(
        [width // 5, height // 5, width * 4 // 5, height * 4 // 5],
        fill=(220, 30, 30, 255),
    )
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def broken_png() -> bytes:
    """A PNG with a valid signature and a corrupt IDAT chunk length."""
    import struct
    from io import BytesIO

    from PIL import Image

    out = BytesIO()
    Image.new("RGB", (100, 100), (10, 20, 30)).save(out, format="PNG")
    data = bytearray(out.getvalue())
    index = data.find(b"IDAT")
    data[index - 4:index] = struct.pack(">I", 0x82)
    return bytes(data)


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


class ProviderModelsUrlTests(unittest.TestCase):
    def test_base_url_variants(self) -> None:
        self.assertEqual(
            _provider_models_url("https://api.example/v1"),
            "https://api.example/v1/models",
        )
        self.assertEqual(
            _provider_models_url("https://api.example/v1/"),
            "https://api.example/v1/models",
        )
        self.assertEqual(
            _provider_models_url("https://api.example/v1/chat/completions"),
            "https://api.example/v1/models",
        )
        self.assertEqual(
            _provider_models_url("https://api.example/v1/models"),
            "https://api.example/v1/models",
        )
        self.assertEqual(
            _provider_models_url("https://api.example/v1?api-version=2024-01-01"),
            "https://api.example/v1/models?api-version=2024-01-01",
        )
        self.assertEqual(
            _provider_models_url("https://api.example/v1#frag"),
            "https://api.example/v1/models#frag",
        )
        with self.assertRaises(ValueError):
            _provider_models_url("")
        with self.assertRaises(ValueError):
            _provider_models_url("ftp://api.example/v1")
        with self.assertRaises(ValueError):
            _provider_models_url("https:///v1")

    def test_model_list_shapes(self) -> None:
        self.assertEqual(
            _model_list({"data": [{"id": "b"}, {"id": "a"}, {"id": "a"}]}),
            [{"id": "a", "owned_by": ""}, {"id": "b", "owned_by": ""}],
        )
        self.assertEqual(
            _model_list(["x", "y"]),
            [{"id": "x", "owned_by": ""}, {"id": "y", "owned_by": ""}],
        )
        self.assertEqual(
            _model_list({"models": [{"name": "z", "owner": "acme"}]}),
            [{"id": "z", "owned_by": "acme"}],
        )
        with self.assertRaises(ProviderModelsError):
            _model_list({"data": []})
        with self.assertRaises(ProviderModelsError):
            _model_list({"unexpected": "shape"})


class StickerDirectoryTests(unittest.TestCase):
    def test_env_override_matches_the_bot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "a.png"
            image.write_bytes(PNG_BYTES)
            config = {"stickers": {"database": str(root / "stickers.db")}}
            with patch.dict(os.environ, {"STICKER_DIR": str(root)}, clear=False):
                self.assertEqual(sticker_directory(config), root)
                self.assertEqual(sticker_target(config, image), image.resolve())
            with patch.dict(os.environ, {"STICKER_DIR": ""}, clear=False):
                # No env and no TOML value -> the bot's default, not the DB parent.
                self.assertEqual(
                    sticker_directory(config), Path("/var/lib/qqbot/stickers")
                )

    def test_sticker_db_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {"stickers": {"database": str(root / "other.db")}}
            with patch.dict(
                os.environ, {"STICKER_DB": str(root / "custom.db")}, clear=False
            ):
                self.assertEqual(db_paths(config)["stickers"], str(root / "custom.db"))


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


class FakeProviderHandler(BaseHTTPRequestHandler):
    """Tiny OpenAI-compatible /models endpoint for admin-UI tests."""

    status = 200
    auth = ""
    api_key = ""
    path = ""
    require_api_key = False
    respond_json = True
    model_ids = ("alpha", "beta")

    def do_GET(self) -> None:
        FakeProviderHandler.auth = self.headers.get("Authorization", "")
        FakeProviderHandler.api_key = self.headers.get("x-api-key", "")
        FakeProviderHandler.path = self.path
        authorized = (
            FakeProviderHandler.api_key == "test-key"
            if FakeProviderHandler.require_api_key
            else FakeProviderHandler.auth == "Bearer test-key"
        )
        status = FakeProviderHandler.status if authorized else 401
        if FakeProviderHandler.respond_json:
            body = json.dumps({
                "object": "list",
                "data": [
                    {"id": model_id, "owned_by": "acme"}
                    for model_id in FakeProviderHandler.model_ids
                ],
            }).encode()
            content_type = "application/json"
        else:
            body = b"<html>not json</html>"
            content_type = "text/html"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


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
        self.sticker_dir = root / "stickers"
        self.sticker_dir.mkdir()
        for name in ("a.png", "b.png"):
            (self.sticker_dir / name).write_bytes(PNG_BYTES)
        (self.sticker_dir / "c.png").write_bytes(rgba_png(600, 400))
        (self.sticker_dir / "broken.png").write_bytes(broken_png())
        (root / "outside.png").write_bytes(PNG_BYTES)
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
            "(1, 'sha1', ?, 'image/png', 1024, '开心', '[\"笑\"]', "
            "'group:42', 'collected', 'ok', 4, ?, ?)",
            (str(self.sticker_dir / "a.png"), now, now),
        )
        conn.execute(
            "INSERT INTO stickers VALUES "
            "(2, 'sha2', ?, 'image/png', 2048, '无语', '[]', "
            "'group:42', 'collected', 'banned', 0, ?, 0)",
            (str(self.sticker_dir / "b.png"), now),
        )
        conn.execute(
            "INSERT INTO stickers VALUES "
            "(3, 'sha3', ?, 'image/png', 4096, '点赞', '[\"好耶\"]', "
            "'manual', 'upload', 'ok', 1, ?, ?)",
            (str(self.sticker_dir / "c.png"), now, now),
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
            "stickers": {
                "database": self.dbs["stickers"],
                "directory": str(self.sticker_dir),
            },
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

    def post(self, path: str, payload: dict, cookie: str = "", content_type: str = "application/json"):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": content_type},
        )
        if cookie:
            request.add_header("Cookie", cookie)
        try:
            return urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            return exc

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
        rows = {
            row["id"]: row
            for row in conversations_data(self.config)["conversations"]
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

    def test_card_detail_endpoint(self) -> None:
        payload = {
            "spec": "chara_card_v3",
            "spec_version": "3.0",
            "data": {
                "name": "Phoebe",
                "first_mes": "你好，{{user}}。",
                "alternate_greetings": ["又见面了"],
                "character_book": {
                    "entries": [{"keys": ["修会"], "content": "隐海修会", "enabled": True}]
                },
                "assets": [
                    {"type": "icon", "name": "main", "uri": "data:image/png;base64," + "A" * 500},
                    {"type": "icon", "name": "remote", "uri": "https://example.test/a.png"},
                ],
            },
            "_import": {"spec": "chara_card_v2", "spec_version": "2.0"},
        }
        conn = sqlite3.connect(self.dbs["cards"])
        conn.execute(
            "INSERT INTO cards (id, name, fingerprint, payload, created_at, updated_at) "
            "VALUES (2, 'Phoebe', 'fp2', ?, 1, 2)",
            (json.dumps(payload),),
        )
        conn.commit()
        conn.close()

        self.assertEqual(self.get("/api/cards/2").status, 401)
        cookie, _ = self.login()
        detail = json.loads(self.get("/api/cards/2", cookie).read().decode())
        self.assertEqual(detail["id"], 2)
        self.assertEqual(detail["spec"], "chara_card_v3")
        self.assertEqual(detail["imported_from"]["spec"], "chara_card_v2")
        self.assertEqual(detail["data"]["first_mes"], "你好，{{user}}。")
        self.assertEqual(
            detail["data"]["character_book"]["entries"][0]["keys"], ["修会"]
        )
        embedded, remote = detail["data"]["assets"]
        self.assertTrue(embedded["uri"].startswith("data:image/png;base64,…"))
        self.assertLess(len(embedded["uri"]), 120)
        self.assertEqual(remote["uri"], "https://example.test/a.png")

        # The legacy fixture stores an empty payload; it still renders.
        legacy = json.loads(self.get("/api/cards/1", cookie).read().decode())
        self.assertEqual(legacy["data"], {})
        self.assertEqual(self.get("/api/cards/99", cookie).status, 404)
        self.assertEqual(self.get("/api/cards/abc", cookie).status, 404)

    def test_model_provider_management(self) -> None:
        cookie, _ = self.login()

        # Unauthenticated writes are rejected before touching SQLite.
        anonymous = self.post(
            "/api/models/providers",
            {"provider": "deepseek", "base_url": "https://api.deepseek.test"},
        )
        self.assertEqual(anonymous.status, 401)

        response = self.post(
            "/api/models/providers",
            {
                "provider": "deepseek",
                "display_name": "DeepSeek",
                "api": "openai-completions",
                "base_url": "https://api.deepseek.test",
                "api_key_env": "DEEPSEEK_API_KEY",
            },
            cookie,
        )
        self.assertEqual(response.status, 200)
        payload = json.loads(response.read().decode())
        providers = {row["provider"]: row for row in payload["providers"]}
        self.assertEqual(providers["deepseek"]["display_name"], "DeepSeek")
        self.assertEqual(providers["deepseek"]["model_count"], 0)
        self.assertFalse(providers["deepseek"]["api_key_set"])

        response = self.post(
            "/api/models/items",
            {
                "provider": "deepseek",
                "model_id": "deepseek-chat",
                "display_name": "DeepSeek Chat",
                "context_window": "64000",
                "max_output_tokens": "4096",
                "reasoning_effort": "high",
                "capabilities": {"reasoning_efforts": ["low", "high"]},
            },
            cookie,
        )
        self.assertEqual(response.status, 200)
        payload = json.loads(response.read().decode())
        model = next(
            row for row in payload["models"] if row["model_id"] == "deepseek-chat"
        )
        self.assertEqual(model["context_window"], 64000)
        self.assertEqual(model["reasoning_effort"], "high")
        self.assertEqual(model["base_url"], "https://api.deepseek.test")
        self.assertFalse(model["is_active"])

        # Invalid effort is rejected and the row is not left half-written.
        response = self.post(
            "/api/models/items",
            {
                "provider": "deepseek",
                "model_id": "deepseek-chat",
                "reasoning_effort": "max",
            },
            cookie,
        )
        self.assertEqual(response.status, 400)
        payload = json.loads(self.get("/api/models", cookie).read().decode())
        model = next(
            row for row in payload["models"] if row["model_id"] == "deepseek-chat"
        )
        self.assertEqual(model["reasoning_effort"], "high")

        response = self.post(
            "/api/models/active",
            {"provider": "deepseek", "model_id": "deepseek-chat"},
            cookie,
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(
            json.loads(response.read().decode())["active"], "deepseek/deepseek-chat"
        )

        # Active model/provider cannot be removed out from under the bot.
        response = self.post(
            "/api/models/items/delete",
            {"provider": "deepseek", "model_id": "deepseek-chat"},
            cookie,
        )
        self.assertEqual(response.status, 400)
        response = self.post(
            "/api/models/providers/delete", {"provider": "deepseek"}, cookie
        )
        self.assertEqual(response.status, 400)

        # Switch back, then clean up model and provider.
        self.assertEqual(
            self.post(
                "/api/models/active",
                {"provider": "opencode-go", "model_id": "flash"},
                cookie,
            ).status,
            200,
        )
        self.assertEqual(
            self.post(
                "/api/models/items/delete",
                {"provider": "deepseek", "model_id": "deepseek-chat"},
                cookie,
            ).status,
            200,
        )
        response = self.post(
            "/api/models/providers/delete", {"provider": "deepseek"}, cookie
        )
        self.assertEqual(response.status, 200)
        payload = json.loads(response.read().decode())
        self.assertNotIn("deepseek", {row["provider"] for row in payload["providers"]})

        # A provider without base_url is refused, and writes must be JSON.
        response = self.post(
            "/api/models/providers", {"provider": "nobase"}, cookie
        )
        self.assertEqual(response.status, 400)
        response = self.post(
            "/api/models/providers",
            {"provider": "nobase", "base_url": "https://x"},
            cookie,
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(response.status, 415)

    def test_provider_api_key_is_stored_but_never_returned(self) -> None:
        cookie, _ = self.login()
        response = self.post(
            "/api/models/providers",
            {
                "provider": "keyco",
                "display_name": "Key Co",
                "base_url": "https://key.example/v1",
                "api_key_env": "KEYCO_API_KEY",
                "api_key": "sk-very-secret",
            },
            cookie,
        )
        self.assertEqual(response.status, 200)
        raw = response.read().decode()
        self.assertNotIn("sk-very-secret", raw)
        payload = json.loads(raw)
        row = next(item for item in payload["providers"] if item["provider"] == "keyco")
        self.assertNotIn("api_key", row)
        self.assertEqual(row["api_key_source"], "stored")
        self.assertTrue(row["api_key_set"])

        # Editing other fields leaves the saved key untouched.
        response = self.post(
            "/api/models/providers",
            {"provider": "keyco", "display_name": "Key Co 2"},
            cookie,
        )
        self.assertEqual(response.status, 200)
        payload = json.loads(response.read().decode())
        row = next(item for item in payload["providers"] if item["provider"] == "keyco")
        self.assertEqual(row["api_key_source"], "stored")

        # An explicit empty value clears it and falls back to the env var.
        response = self.post(
            "/api/models/providers", {"provider": "keyco", "api_key": ""}, cookie
        )
        self.assertEqual(response.status, 200)
        payload = json.loads(response.read().decode())
        row = next(item for item in payload["providers"] if item["provider"] == "keyco")
        self.assertEqual(row["api_key_source"], "missing")
        self.assertFalse(row["api_key_set"])

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
        self.assertTrue(by_id[1]["has_image"])

    def test_sticker_images_are_served(self) -> None:
        self.assertEqual(self.get("/api/stickers/1/image").status, 401)

        cookie, _ = self.login()
        response = self.get("/api/stickers/1/image", cookie)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], "image/png")
        self.assertEqual(response.read(), PNG_BYTES)
        self.assertIn("immutable", response.headers["Cache-Control"])

        thumb = self.get("/api/stickers/1/image?thumb=1", cookie)
        self.assertEqual(thumb.status, 200)
        self.assertEqual(thumb.headers["Content-Type"], "image/jpeg")
        self.assertTrue(thumb.read().startswith(bytes([0xff, 0xd8, 0xff])))

        # A real-sized RGBA image is downscaled and flattened onto white.
        from io import BytesIO

        from PIL import Image

        large = self.get("/api/stickers/3/image?thumb=1", cookie)
        self.assertEqual(large.status, 200)
        with Image.open(BytesIO(large.read())) as opened:
            self.assertEqual(opened.format, "JPEG")
            preview = opened.convert("RGB")
        self.assertEqual(preview.size, (320, 213))
        self.assertTrue(all(channel > 240 for channel in preview.getpixel((2, 2))))
        red, green, _ = preview.getpixel((160, 106))
        self.assertGreater(red, 150)
        self.assertLess(green, 120)

        self.assertEqual(self.get("/api/stickers/999/image", cookie).status, 404)
        self.assertEqual(self.get("/api/stickers/1/nope", cookie).status, 404)
        self.assertEqual(self.get("/api/stickers//image", cookie).status, 404)
        self.assertEqual(self.get("/api/stickers/not-a-number/image", cookie).status, 404)
        self.assertEqual(
            self.get("/api/stickers/" + "1" * 5000 + "/image", cookie).status, 404
        )

        # A row whose file lives outside the library must never be served.
        conn = sqlite3.connect(self.dbs["stickers"])
        conn.execute(
            "INSERT INTO stickers VALUES "
            "(4, 'sha4', ?, 'image/png', 123, '越界', '[]', 'manual', 'upload', "
            "'ok', 0, ?, 0)",
            (str(Path(self.tmp.name) / "outside.png"), int(time.time())),
        )
        # A row inside the directory whose file was deleted is a 404 too.
        conn.execute(
            "INSERT INTO stickers VALUES "
            "(5, 'sha5', ?, 'image/png', 0, '已删除', '[]', 'manual', 'upload', "
            "'ok', 0, ?, 0)",
            (str(self.sticker_dir / "missing.png"), int(time.time())),
        )
        # Lexical containment would accept this; resolve() must reject it.
        conn.execute(
            "INSERT INTO stickers VALUES "
            "(6, 'sha6', ?, 'image/png', 0, '越界二', '[]', 'manual', 'upload', "
            "'ok', 0, ?, 0)",
            (str(self.sticker_dir / ".." / "outside.png"), int(time.time())),
        )
        # A file whose bytes decode badly still falls back to the original.
        conn.execute(
            "INSERT INTO stickers VALUES "
            "(7, 'sha7', ?, 'image/png', 0, '损坏', '[]', 'manual', 'upload', "
            "'ok', 0, ?, 0)",
            (str(self.sticker_dir / "broken.png"), int(time.time())),
        )
        conn.commit()
        conn.close()
        self.assertEqual(self.get("/api/stickers/4/image", cookie).status, 404)
        self.assertEqual(self.get("/api/stickers/5/image", cookie).status, 404)
        self.assertEqual(self.get("/api/stickers/6/image", cookie).status, 404)

        broken = self.get("/api/stickers/7/image?thumb=1", cookie)
        self.assertEqual(broken.status, 200)
        self.assertEqual(broken.headers["Content-Type"], "image/png")
        self.assertEqual(broken.read(), broken_png())

        stickers = json.loads(self.get("/api/stickers", cookie).read().decode())
        has_image = {row["id"]: row["has_image"] for row in stickers["stickers"]}
        self.assertTrue(has_image[1])
        self.assertTrue(has_image[3])
        self.assertTrue(has_image[7])
        self.assertFalse(has_image[4])
        self.assertFalse(has_image[5])
        self.assertFalse(has_image[6])

    def test_provider_model_list_endpoint(self) -> None:
        FakeProviderHandler.status = 200
        FakeProviderHandler.auth = ""
        FakeProviderHandler.api_key = ""
        FakeProviderHandler.path = ""
        FakeProviderHandler.require_api_key = False
        FakeProviderHandler.respond_json = True
        provider_server = ThreadingHTTPServer(("127.0.0.1", 0), FakeProviderHandler)
        thread = threading.Thread(target=provider_server.serve_forever, daemon=True)
        thread.start()
        try:
            port = provider_server.server_address[1]
            base_url = f"http://127.0.0.1:{port}/v1"

            # The endpoint stays behind the admin session.
            self.assertEqual(
                self.post("/api/models/provider-models", {"provider": "acme"}).status,
                401,
            )

            cookie, _ = self.login()
            response = self.post(
                "/api/models/providers",
                {
                    "provider": "acme",
                    "display_name": "Acme",
                    "api": "openai-completions",
                    "base_url": base_url,
                    "api_key": "test-key",
                },
                cookie,
            )
            self.assertEqual(response.status, 200)

            response = self.post(
                "/api/models/provider-models", {"provider": "acme"}, cookie
            )
            self.assertEqual(response.status, 200)
            body = response.read().decode()
            payload = json.loads(body)
            self.assertEqual(payload["provider"], "acme")
            self.assertEqual(payload["count"], 2)
            self.assertEqual(
                [row["id"] for row in payload["models"]], ["alpha", "beta"]
            )
            self.assertEqual(payload["models"][0]["owned_by"], "acme")
            self.assertNotIn("test-key", body)
            self.assertEqual(FakeProviderHandler.path, "/v1/models")
            self.assertEqual(FakeProviderHandler.auth, "Bearer test-key")

            # OpenCode-style gateways accept the key in x-api-key instead.
            FakeProviderHandler.require_api_key = True
            FakeProviderHandler.auth = ""
            FakeProviderHandler.api_key = ""
            response = self.post(
                "/api/models/provider-models", {"provider": "acme"}, cookie
            )
            self.assertEqual(response.status, 200)
            self.assertEqual(FakeProviderHandler.api_key, "test-key")
            self.assertEqual(FakeProviderHandler.auth, "")
            FakeProviderHandler.require_api_key = False

            # A key that only lives in the environment is picked up.
            os.environ["FAKE_PROVIDER_KEY"] = "test-key"
            try:
                response = self.post(
                    "/api/models/providers",
                    {
                        "provider": "envprov",
                        "display_name": "Env",
                        "api": "openai-completions",
                        "base_url": base_url,
                        "api_key_env": "FAKE_PROVIDER_KEY",
                    },
                    cookie,
                )
                self.assertEqual(response.status, 200)
                response = self.post(
                    "/api/models/provider-models", {"provider": "envprov"}, cookie
                )
                self.assertEqual(response.status, 200)
                self.assertEqual(FakeProviderHandler.auth, "Bearer test-key")
            finally:
                os.environ.pop("FAKE_PROVIDER_KEY", None)

            # No stored key and no env key: the provider's 401 surfaces as 502.
            os.environ.pop("MISSING_PROVIDER_KEY", None)
            response = self.post(
                "/api/models/providers",
                {
                    "provider": "nokey",
                    "display_name": "No Key",
                    "api": "openai-completions",
                    "base_url": base_url,
                    "api_key_env": "MISSING_PROVIDER_KEY",
                },
                cookie,
            )
            self.assertEqual(response.status, 200)
            response = self.post(
                "/api/models/provider-models", {"provider": "nokey"}, cookie
            )
            self.assertEqual(response.status, 502)
            self.assertIn("401", json.loads(response.read().decode())["error"])

            # Upstream errors are reported rather than raised.
            FakeProviderHandler.status = 401
            upstream = self.post(
                "/api/models/provider-models", {"provider": "acme"}, cookie
            )
            self.assertEqual(upstream.status, 502)
            self.assertIn("401", json.loads(upstream.read().decode())["error"])
            FakeProviderHandler.status = 200

            # An HTML gateway page is not JSON.
            FakeProviderHandler.respond_json = False
            broken = self.post(
                "/api/models/provider-models", {"provider": "acme"}, cookie
            )
            self.assertEqual(broken.status, 502)
            self.assertIn("JSON", json.loads(broken.read().decode())["error"])
            FakeProviderHandler.respond_json = True

            # Unreachable host -> network error, not a traceback.
            response = self.post(
                "/api/models/providers",
                {
                    "provider": "unreachable",
                    "display_name": "Unreachable",
                    "api": "openai-completions",
                    "base_url": "http://127.0.0.1:1/v1",
                    "api_key": "test-key",
                },
                cookie,
            )
            self.assertEqual(response.status, 200)
            down = self.post(
                "/api/models/provider-models", {"provider": "unreachable"}, cookie
            )
            self.assertEqual(down.status, 502)
            self.assertIn("网络请求失败", json.loads(down.read().decode())["error"])
        finally:
            FakeProviderHandler.status = 200
            FakeProviderHandler.require_api_key = False
            FakeProviderHandler.respond_json = True
            FakeProviderHandler.auth = ""
            FakeProviderHandler.api_key = ""
            FakeProviderHandler.path = ""
            provider_server.shutdown()
            provider_server.server_close()

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

    def test_fun_date_range(self) -> None:
        tz = ZoneInfo("Asia/Shanghai")
        today = datetime.now(tz).date()
        old_day = today - timedelta(days=10)
        far_day = today - timedelta(days=20)

        def noon(day) -> int:
            return int(
                datetime(day.year, day.month, day.day, 12, tzinfo=tz).timestamp()
            )

        conn = sqlite3.connect(self.dbs["memory"])
        conn.executemany(
            "INSERT INTO chat_events "
            "(conversation_id, chat_type, chat_id, user_id, sender_name, text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("group:42", "group", 42, 99, "旧人", "旧消息", noon(old_day)),
                ("group:42", "group", 42, 98, "远古人", "远古消息", noon(far_day)),
            ],
        )
        conn.commit()
        conn.close()

        cookie, _ = self.login()
        day = old_day.isoformat()
        response = self.get(f"/api/fun?start={day}&end={day}", cookie)
        self.assertEqual(response.status, 200)
        payload = json.loads(response.read().decode())
        self.assertEqual(payload["start"], day)
        self.assertEqual(payload["end"], day)
        self.assertEqual(payload["days"], 1)
        self.assertEqual(payload["messages"], 1)
        self.assertEqual([row["name"] for row in payload["ranking"]], ["旧人"])
        self.assertEqual(payload["conversations"][0]["id"], "group:42")
        self.assertEqual(payload["conversations"][0]["messages"], 1)
        self.assertEqual(payload["conversation_total"], 1)

        # The 13-day window includes today and the old event, not the far one.
        start = (today - timedelta(days=12)).isoformat()
        end = today.isoformat()
        response = self.get(f"/api/fun?start={start}&end={end}", cookie)
        payload = json.loads(response.read().decode())
        self.assertEqual(payload["days"], 13)
        self.assertEqual(payload["messages"], 3)
        names = {row["name"] for row in payload["ranking"]}
        self.assertTrue({"小明", "小红", "旧人"} <= names)
        self.assertNotIn("远古人", names)
        self.assertEqual(payload["conversations"][0]["messages"], 3)
        self.assertEqual(payload["conversation_total"], 1)

        # A wider window picks the far event up.
        wide_start = (today - timedelta(days=25)).isoformat()
        response = self.get(f"/api/fun?start={wide_start}&end={end}", cookie)
        payload = json.loads(response.read().decode())
        self.assertEqual(payload["days"], 26)
        self.assertEqual(payload["messages"], 4)
        self.assertIn("远古人", {row["name"] for row in payload["ranking"]})

        # Inverted bounds are normalised, and bad dates fall back to today.
        response = self.get(f"/api/fun?start={end}&end={start}", cookie)
        payload = json.loads(response.read().decode())
        self.assertEqual(payload["start"], start)
        self.assertEqual(payload["end"], end)

        response = self.get("/api/fun?start=nope&end=nope", cookie)
        payload = json.loads(response.read().decode())
        self.assertEqual(payload["start"], today.isoformat())
        self.assertEqual(payload["end"], today.isoformat())

        # A far-future date is clamped instead of overflowing datetime.max.
        response = self.get("/api/fun?start=9999-12-31&end=9999-12-31", cookie)
        self.assertEqual(response.status, 200)
        payload = json.loads(response.read().decode())
        self.assertEqual(payload["start"], today.isoformat())
        self.assertEqual(payload["end"], today.isoformat())

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
