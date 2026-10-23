"""Admin UI: JSON API plus a React/shadcn static bundle.

Binds 127.0.0.1 only; reach it through Workbench port forwarding. The React
bundle is served from ``[admin] static_dir`` (default
``/opt/qqbot/admin/dist``). Authentication is a single password from
ADMIN_UI_PASSWORD with an HMAC-signed session cookie. The API is read-only:
models, character cards, tasks, feeds and stickers, plus message counts.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import sqlite3
import threading
import time
import tomllib
from datetime import datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from admin_insights import clean_conversation, fun_data, usage_data

log = logging.getLogger("qqbot.admin")

COOKIE = "qqbot_admin"
FAIL_WINDOW = 60
FAIL_LIMIT = 5
TIME_FMT = "%Y-%m-%d %H:%M"
DEFAULT_STATIC_DIR = "/opt/qqbot/admin/dist"

STATIC_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}


def load_config(path: str) -> dict[str, Any]:
    try:
        with Path(path).open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.warning("cannot read %s: %s", path, exc)
        return {}


def db_rows(path: str, query: str) -> list[sqlite3.Row]:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        log.warning("cannot open %s: %s", path, exc)
        return []
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(query).fetchall()
    except sqlite3.Error as exc:
        log.warning("query failed on %s: %s", path, exc)
        return []
    finally:
        conn.close()


def scalar(path: str, query: str) -> Any:
    rows = db_rows(path, query)
    return rows[0][0] if rows else None


def ip_allowed(remote: str, cidrs: list[str] | tuple[str, ...] | None) -> bool:
    """Empty allowlist means any source; otherwise require a CIDR match."""
    if not cidrs:
        return True
    try:
        address = ipaddress.ip_address(remote)
    except ValueError:
        return False
    for entry in cidrs:
        try:
            if address in ipaddress.ip_network(str(entry).strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


def fmt_ts(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value)).strftime(TIME_FMT)
    except (TypeError, ValueError, OSError):
        return "-"


def issue_session(secret: bytes, hours: float, now: float | None = None) -> str:
    expires = int((now if now is not None else time.time()) + max(1.0, hours) * 3600)
    sig = hmac.new(secret, str(expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def valid_session(secret: bytes, value: str, now: float | None = None) -> bool:
    if not value or "." not in value:
        return False
    expires_text, _, sig = value.partition(".")
    if not expires_text.isdigit() or int(expires_text) < (now if now is not None else time.time()):
        return False
    expected = hmac.new(secret, expires_text.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def page(title: str, body: str) -> bytes:
    """Small server-rendered fallback for 403/404/503 responses."""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;padding:32px;background:#f4f8ff;color:#0f172a;font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}}
.wrap{{max-width:640px;margin:0 auto;border:1px solid #dbeafe;border-radius:18px;background:#fff;padding:28px;box-shadow:0 18px 45px -20px rgba(37,99,235,.35)}}
h1{{font-size:22px;margin:0 0 12px;color:#1d4ed8}} p{{color:#475569;margin:0}} a{{color:#2563eb}}
</style></head><body><div class="wrap">{body}</div></body></html>""".encode("utf-8")


def dashboard_data(config: dict[str, Any]) -> dict[str, Any]:
    """Read the dashboard directly from the bot's SQLite databases."""
    cfg = config
    paths = cfg.get("paths") or {}
    memory_db = str(paths.get("database", "/var/lib/qqbot/memory.db"))
    models_db = str((cfg.get("models") or {}).get("database", "/var/lib/qqbot/models.db"))
    cards_db = str((cfg.get("cards") or {}).get("database", "/var/lib/qqbot/characters.db"))
    tasks_db = str((cfg.get("tasks") or {}).get("database", "/var/lib/qqbot/tasks.db"))
    feeds_db = str((cfg.get("feeds") or {}).get("database", "/var/lib/qqbot/feeds.db"))
    stickers_db = str((cfg.get("stickers") or {}).get("database", "/var/lib/qqbot/stickers.db"))

    active_model = scalar(models_db, "SELECT provider || '/' || model_id FROM models WHERE is_active = 1") or "-"
    active_card = scalar(cards_db, "SELECT COALESCE(NULLIF(nickname,''), name) FROM cards WHERE is_active = 1") or "-"
    messages = int(scalar(memory_db, "SELECT COUNT(*) FROM messages") or 0)
    last_message = scalar(memory_db, "SELECT MAX(created_at) FROM messages")
    tasks_total = int(scalar(tasks_db, "SELECT COUNT(*) FROM tasks") or 0)
    tasks_on = int(scalar(tasks_db, "SELECT COUNT(*) FROM tasks WHERE enabled = 1") or 0)
    feeds_total = int(scalar(feeds_db, "SELECT COUNT(*) FROM feeds") or 0)
    stickers = int(scalar(stickers_db, "SELECT COUNT(*) FROM stickers") or 0)
    stickers_banned = int(scalar(stickers_db, "SELECT COUNT(*) FROM stickers WHERE status = 'banned'") or 0)

    model_rows = []
    for row in db_rows(
        models_db,
        "SELECT provider, model_id, context_window, is_active, enabled "
        "FROM models ORDER BY provider, model_id",
    ):
        model_rows.append({
            "provider": str(row["provider"]),
            "model_id": str(row["model_id"]),
            "context_window": int(row["context_window"] or 0),
            "is_active": bool(row["is_active"]),
            "enabled": bool(row["enabled"]),
        })

    card_rows = []
    for row in db_rows(
        cards_db,
        "SELECT id, name, nickname, aliases, is_active FROM cards ORDER BY id",
    ):
        try:
            aliases = json.loads(row["aliases"] or "[]")
        except (TypeError, json.JSONDecodeError):
            aliases = []
        if not isinstance(aliases, list):
            aliases = []
        card_rows.append({
            "id": int(row["id"]),
            "name": str(row["name"] or ""),
            "nickname": str(row["nickname"] or ""),
            "aliases": [str(alias) for alias in aliases],
            "is_active": bool(row["is_active"]),
        })

    task_rows = []
    for row in db_rows(
        tasks_db,
        "SELECT id, kind, time_of_day, weekday, every_minutes, mode, enabled, next_run, message "
        "FROM tasks ORDER BY enabled DESC, next_run LIMIT 20",
    ):
        task_rows.append({
            "id": int(row["id"]),
            "kind": str(row["kind"] or ""),
            "time_of_day": row["time_of_day"],
            "weekday": row["weekday"],
            "every_minutes": row["every_minutes"],
            "mode": str(row["mode"] or ""),
            "enabled": bool(row["enabled"]),
            "next_run": row["next_run"],
            "message": str(row["message"] or ""),
        })

    feed_rows = []
    for row in db_rows(
        feeds_db,
        "SELECT id, chat_type, chat_id, title, feed_url, last_error FROM feeds ORDER BY id LIMIT 20",
    ):
        feed_rows.append({
            "id": int(row["id"]),
            "chat_type": str(row["chat_type"] or ""),
            "chat_id": int(row["chat_id"] or 0),
            "title": str(row["title"] or ""),
            "feed_url": str(row["feed_url"] or ""),
            "last_error": str(row["last_error"] or ""),
        })

    return {
        "generated_at": int(time.time()),
        "active_model": str(active_model),
        "active_card": str(active_card),
        "messages": {"total": messages, "last_at": last_message},
        "tasks": {"total": tasks_total, "enabled": tasks_on},
        "feeds": {"total": feeds_total},
        "stickers": {"total": stickers, "banned": stickers_banned},
        "model_rows": model_rows,
        "card_rows": card_rows,
        "task_rows": task_rows,
        "feed_rows": feed_rows,
    }


class AdminHandler(BaseHTTPRequestHandler):
    server_version = "qqbot-admin/2"
    config: dict[str, Any] = {}
    static_dir: str = DEFAULT_STATIC_DIR

    # ------------------------------------------------------------- plumbing
    def log_message(self, format: str, *args: Any) -> None:
        log.info("%s %s", self.address_string(), format % args)

    def _send_raw(
        self,
        status: int,
        body: bytes,
        content_type: str = "text/html; charset=utf-8",
        cache: str = "no-store",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; font-src 'self' data:; base-uri 'self'; "
            "form-action 'self'; frame-ancestors 'none'",
        )
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._send_raw(status, body, headers=headers)

    def _send_json(
        self, status: int, payload: dict[str, Any], headers: dict[str, str] | None = None
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_raw(status, body, "application/json; charset=utf-8", headers=headers)

    def _redirect(self, location: str) -> None:
        self._send_raw(HTTPStatus.SEE_OTHER, b"", headers={"Location": location})

    def _read_payload(self) -> dict[str, Any]:
        length = min(int(self.headers.get("Content-Length") or 0), 4096)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        content_type = (self.headers.get("Content-Type") or "").lower()
        if "application/json" in content_type:
            try:
                payload = json.loads(raw or "{}")
            except json.JSONDecodeError:
                return {}
            return payload if isinstance(payload, dict) else {}
        return {key: values[0] for key, values in parse_qs(raw).items()}

    # ------------------------------------------------------------------ auth
    def _password(self) -> str:
        return os.environ.get("ADMIN_UI_PASSWORD", "")

    def _secret(self) -> bytes:
        secret = os.environ.get("ADMIN_UI_SECRET", "")
        if not secret:
            secret = self._password() or "qqbot-admin"
        return secret.encode("utf-8")

    def _session_hours(self) -> float:
        try:
            return max(1.0, float((self.config.get("admin") or {}).get("session_hours", 12)))
        except (TypeError, ValueError):
            return 12.0

    def _issue(self) -> str:
        return issue_session(self._secret(), self._session_hours())

    def _valid_session(self) -> bool:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        raw = cookie.get(COOKIE)
        return raw is not None and valid_session(self._secret(), raw.value)

    _failures: dict[str, list[float]] = {}
    _fail_lock = threading.Lock()

    def _allow_login(self) -> bool:
        now = time.time()
        with self._fail_lock:
            window = [t for t in self._failures.get(self.client_address[0], []) if now - t < FAIL_WINDOW]
            self._failures[self.client_address[0]] = window
            return len(window) < FAIL_LIMIT

    def _record_failure(self) -> None:
        with self._fail_lock:
            self._failures.setdefault(self.client_address[0], []).append(time.time())

    # ----------------------------------------------------------------- routes
    def _is_api(self) -> bool:
        return urlparse(self.path).path.startswith("/api/")

    def _source_allowed(self) -> bool:
        cidrs = (self.config.get("admin") or {}).get("allow_cidrs") or []
        if ip_allowed(self.client_address[0], cidrs):
            return True
        if self._is_api():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "来源 IP 不在白名单内"})
        else:
            self._send(HTTPStatus.FORBIDDEN, page("禁止访问", "<h1>403</h1><p>来源 IP 不在白名单内。</p>"))
        return False

    def _serve_static(self, path: str) -> bool:
        root = Path(self.static_dir).resolve()
        relative = unquote(path).lstrip("/") or "index.html"
        try:
            target = (root / relative).resolve()
            target.relative_to(root)
        except (OSError, ValueError):
            return False
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            if relative.startswith("assets/"):
                return False
            target = root / "index.html"
            if not target.is_file():
                return False
            relative = "index.html"
        try:
            body = target.read_bytes()
        except OSError:
            return False
        content_type = STATIC_MIME.get(target.suffix.lower(), "application/octet-stream")
        cache = "public, max-age=31536000, immutable" if relative.startswith("assets/") else "no-store"
        self._send_raw(HTTPStatus.OK, body, content_type, cache)
        return True

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            self._send(HTTPStatus.OK, b"ok\n")
            return
        if not self._source_allowed():
            return

        if path == "/api/session":
            self._send_json(HTTPStatus.OK, {"authenticated": self._valid_session()})
            return
        if path == "/api/dashboard":
            if not self._valid_session():
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            self._send_json(HTTPStatus.OK, dashboard_data(self.config))
            return
        if path in ("/api/usage", "/api/fun"):
            if not self._valid_session():
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            query = parse_qs(urlparse(self.path).query)
            conversation = clean_conversation(
                (query.get("conversation") or [""])[0]
            )
            if path == "/api/usage":
                try:
                    days = int((query.get("days") or ["7"])[0])
                except (TypeError, ValueError):
                    days = 7
                self._send_json(
                    HTTPStatus.OK, usage_data(self.config, days, conversation)
                )
            else:
                self._send_json(
                    HTTPStatus.OK, fun_data(self.config, conversation)
                )
            return
        if path in ("/logout", "/api/logout"):
            self._send_raw(
                HTTPStatus.SEE_OTHER,
                b"",
                headers={
                    "Location": "/",
                    "Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict",
                },
            )
            return
        if path.startswith("/api/"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        if path in ("/", "/index.html", "/login"):
            path = "/index.html"
        if self._serve_static(path):
            return
        if not (Path(self.static_dir) / "index.html").is_file():
            self._send(
                HTTPStatus.SERVICE_UNAVAILABLE,
                page(
                    "admin UI 未构建",
                    "<h1>admin UI 未构建</h1><p>请先在 admin/ 目录执行 "
                    "<code>npm run build</code>，再把 dist 部署到 "
                    "<code>/opt/qqbot/admin/dist</code>。</p>",
                ),
            )
            return
        self._send(HTTPStatus.NOT_FOUND, page("未找到", "<h1>404</h1><p>页面不存在。</p>"))

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self._source_allowed():
            return

        if path in ("/api/login", "/login"):
            if not self._allow_login():
                self._send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "尝试过于频繁，请一分钟后再试"})
                return
            payload = self._read_payload()
            submitted = str(payload.get("password") or "")
            expected = self._password()
            if not expected:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "未设置 ADMIN_UI_PASSWORD"},
                )
                return
            if not hmac.compare_digest(submitted, expected):
                self._record_failure()
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "密码不对"})
                return
            cookie = self._issue()
            cookie_header = (
                f"{COOKIE}={cookie}; Path=/; HttpOnly; SameSite=Strict; "
                f"Max-Age={int(self._session_hours() * 3600)}"
            )
            if path == "/login":
                self._send_raw(
                    HTTPStatus.SEE_OTHER,
                    b"",
                    headers={"Location": "/", "Set-Cookie": cookie_header},
                )
            else:
                self._send_json(HTTPStatus.OK, {"ok": True}, headers={"Set-Cookie": cookie_header})
            return

        if path in ("/api/logout", "/logout"):
            headers = {"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"}
            if path == "/logout":
                headers["Location"] = "/"
                self._send_raw(HTTPStatus.SEE_OTHER, b"", headers=headers)
            else:
                self._send_json(HTTPStatus.OK, {"ok": True}, headers=headers)
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})


def main() -> int:
    parser = argparse.ArgumentParser(description="qqbot admin UI")
    parser.add_argument("--config", default="/etc/qqbot/config.toml")
    parser.add_argument("--static-dir", default="")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config(args.config)
    admin = config.get("admin") or {}
    host = str(admin.get("host", "127.0.0.1"))
    port = int(admin.get("port", 8080))
    static_dir = (
        args.static_dir
        or os.environ.get("ADMIN_UI_STATIC_DIR", "").strip()
        or str(admin.get("static_dir", DEFAULT_STATIC_DIR))
    )
    if not os.environ.get("ADMIN_UI_PASSWORD"):
        log.warning("ADMIN_UI_PASSWORD is not set; login will refuse until it is")
    if not (Path(static_dir) / "index.html").is_file():
        log.warning("admin UI bundle missing at %s; run npm run build in admin/", static_dir)

    AdminHandler.config = config
    AdminHandler.static_dir = static_dir
    server = ThreadingHTTPServer((host, port), AdminHandler)
    log.info(
        "admin UI listening on http://%s:%d (password %s, static %s)",
        host,
        port,
        "set" if os.environ.get("ADMIN_UI_PASSWORD") else "missing",
        static_dir,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
