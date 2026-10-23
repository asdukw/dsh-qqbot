"""Minimal admin UI for the qqbot host.

Binds 127.0.0.1 only; reach it through Workbench port forwarding. v1 is a
read-only dashboard: models, character cards, tasks, feeds and stickers, plus
basic message counts. Authentication is a single password from
ADMIN_UI_PASSWORD, with an HMAC-signed session cookie.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import ipaddress
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
from urllib.parse import parse_qs

log = logging.getLogger("qqbot.admin")

COOKIE = "qqbot_admin"
FAIL_WINDOW = 60
FAIL_LIMIT = 5
TIME_FMT = "%Y-%m-%d %H:%M"


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
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root{{color-scheme:light dark;--bg:#f6f7f9;--fg:#1c1e21;--muted:#667085;--card:#fff;--line:#e4e7ec;--accent:#2563eb}}
@media (prefers-color-scheme:dark){{:root{{--bg:#101114;--fg:#e6e6e6;--muted:#98a2b3;--card:#181a1f;--line:#2a2d35;--accent:#60a5fa}}}}
*{{box-sizing:border-box}}
body{{margin:0;padding:24px;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}}
h1{{font-size:22px;margin:0 0 16px}} h2{{font-size:16px;margin:24px 0 8px}}
.wrap{{max-width:960px;margin:0 auto}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}}
.k{{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
.v{{font-size:22px;font-weight:600;margin-top:4px}}
table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}}
th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);font-size:14px;vertical-align:top}}
th{{color:var(--muted);font-weight:500}} tr:last-child td{{border-bottom:0}}
.bad{{color:#dc2626}} .muted{{color:var(--muted)}}
form{{display:flex;gap:8px;margin-top:12px}} input{{flex:1;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)}}
button{{padding:10px 16px;border:0;border-radius:8px;background:var(--accent);color:#fff;font-weight:600;cursor:pointer}}
a{{color:var(--accent)}}
</style></head><body><div class="wrap">{body}</div></body></html>""".encode("utf-8")


class AdminHandler(BaseHTTPRequestHandler):
    server_version = "qqbot-admin/1"
    config: dict[str, Any] = {}

    # ------------------------------------------------------------- plumbing
    def log_message(self, format: str, *args: Any) -> None:
        log.info("%s %s", self.address_string(), format % args)

    def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self._send(HTTPStatus.SEE_OTHER, b"", {"Location": location})

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
    def _source_allowed(self) -> bool:
        cidrs = (self.config.get("admin") or {}).get("allow_cidrs") or []
        if ip_allowed(self.client_address[0], cidrs):
            return True
        self._send(HTTPStatus.FORBIDDEN, page("禁止访问", "<h1>403</h1><p>来源 IP 不在白名单内。</p>"))
        return False

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send(HTTPStatus.OK, b"ok\n")
            return
        if self.path.startswith("/logout"):
            self._send(
                HTTPStatus.SEE_OTHER,
                b"",
                {"Location": "/", "Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"},
            )
            return
        if not self._source_allowed():
            return
        if not self._valid_session():
            self._login_page()
            return
        self._send(HTTPStatus.OK, page("qqbot admin", self._dashboard()))

    def do_POST(self) -> None:
        if not self._source_allowed():
            return
        if self.path != "/login":
            self._send(HTTPStatus.NOT_FOUND, b"not found\n")
            return
        if not self._allow_login():
            self._send(HTTPStatus.TOO_MANY_REQUESTS, page("登录受限", "<h1>尝试过于频繁</h1><p>请一分钟后再试。</p>"))
            return
        length = min(int(self.headers.get("Content-Length") or 0), 4096)
        body = self.rfile.read(length).decode("utf-8", "replace")
        submitted = (parse_qs(body).get("password") or [""])[0]
        expected = self._password()
        if not expected:
            self._send(
                HTTPStatus.SERVICE_UNAVAILABLE,
                page("未配置", "<h1>未设置密码</h1><p>请在 bot.env 中设置 ADMIN_UI_PASSWORD。</p>"),
            )
            return
        if not hmac.compare_digest(submitted, expected):
            self._record_failure()
            self._login_page(error="密码不对。")
            return
        cookie = self._issue()
        self._send(
            HTTPStatus.SEE_OTHER,
            b"",
            {
                "Location": "/",
                "Set-Cookie": f"{COOKIE}={cookie}; Path=/; HttpOnly; SameSite=Strict; Max-Age={int(self._session_hours()*3600)}",
            },
        )

    # ------------------------------------------------------------------- view
    def _login_page(self, error: str = "") -> None:
        message = f'<p class="bad">{html.escape(error)}</p>' if error else ""
        body = f"""<h1>qqbot admin</h1>
<p class="muted">只读仪表盘 · 通过 Workbench 端口转发访问</p>{message}
<form method="post" action="/login">
  <input type="password" name="password" placeholder="管理员密码" autofocus>
  <button type="submit">登录</button>
</form>"""
        self._send(HTTPStatus.OK, page("登录 · qqbot admin", body))

    def _dashboard(self) -> str:
        cfg = self.config
        paths = cfg.get("paths") or {}
        memory_db = str(paths.get("database", "/var/lib/qqbot/memory.db"))
        models_db = str((cfg.get("models") or {}).get("database", "/var/lib/qqbot/models.db"))
        cards_db = str((cfg.get("cards") or {}).get("database", "/var/lib/qqbot/characters.db"))
        tasks_db = str((cfg.get("tasks") or {}).get("database", "/var/lib/qqbot/tasks.db"))
        feeds_db = str((cfg.get("feeds") or {}).get("database", "/var/lib/qqbot/feeds.db"))
        stickers_db = str((cfg.get("stickers") or {}).get("database", "/var/lib/qqbot/stickers.db"))

        active_model = scalar(models_db, "SELECT provider || '/' || model_id FROM models WHERE is_active = 1") or "-"
        active_card = scalar(cards_db, "SELECT COALESCE(NULLIF(nickname,''), name) FROM cards WHERE is_active = 1") or "-"
        messages = scalar(memory_db, "SELECT COUNT(*) FROM messages") or 0
        last_message = scalar(memory_db, "SELECT MAX(created_at) FROM messages")
        tasks_total = scalar(tasks_db, "SELECT COUNT(*) FROM tasks") or 0
        tasks_on = scalar(tasks_db, "SELECT COUNT(*) FROM tasks WHERE enabled = 1") or 0
        feeds_total = scalar(feeds_db, "SELECT COUNT(*) FROM feeds") or 0
        stickers = scalar(stickers_db, "SELECT COUNT(*) FROM stickers") or 0
        stickers_banned = scalar(stickers_db, "SELECT COUNT(*) FROM stickers WHERE status = 'banned'") or 0

        cards = f"""<div class="grid">
<div class="card"><div class="k">当前模型</div><div class="v" style="font-size:16px">{html.escape(str(active_model))}</div></div>
<div class="card"><div class="k">当前角色卡</div><div class="v" style="font-size:16px">{html.escape(str(active_card))}</div></div>
<div class="card"><div class="k">消息记录</div><div class="v">{int(messages):,}</div><div class="muted">最后 {fmt_ts(last_message)}</div></div>
<div class="card"><div class="k">任务</div><div class="v">{tasks_on}/{tasks_total}</div><div class="muted">启用/总数</div></div>
<div class="card"><div class="k">订阅</div><div class="v">{feeds_total}</div></div>
<div class="card"><div class="k">表情库</div><div class="v">{stickers}</div><div class="muted">{stickers_banned} 张禁用</div></div>
</div>"""

        task_rows = db_rows(
            tasks_db,
            "SELECT id, kind, time_of_day, weekday, every_minutes, mode, enabled, next_run, message "
            "FROM tasks ORDER BY enabled DESC, next_run LIMIT 20",
        )
        tasks_html = "<p class='muted'>没有任务。</p>"
        if task_rows:
            rows = "".join(
                "<tr>"
                f"<td>#{row['id']}</td>"
                f"<td>{html.escape(str(row['kind']))}"
                + (f" {html.escape(str(row['time_of_day']))}" if row["time_of_day"] else "")
                + (f" 周{int(row['weekday']) + 1}" if row["weekday"] is not None else "")
                + (f" 每 {row['every_minutes']} 分" if row["every_minutes"] else "")
                + "</td>"
                f"<td>{'开' if row['enabled'] else '<span class=bad>停</span>'}</td>"
                f"<td>{fmt_ts(row['next_run'])}</td>"
                f"<td>{html.escape(str(row['mode']))}</td>"
                f"<td>{html.escape(str(row['message'])[:60])}</td>"
                "</tr>"
                for row in task_rows
            )
            tasks_html = f"<table><tr><th>ID</th><th>排期</th><th>状态</th><th>下次</th><th>文案</th><th>内容</th></tr>{rows}</table>"

        feed_rows = db_rows(
            feeds_db,
            "SELECT id, chat_type, chat_id, title, feed_url, last_error FROM feeds ORDER BY id LIMIT 20",
        )
        feeds_html = "<p class='muted'>没有订阅。</p>"
        if feed_rows:
            rows = "".join(
                "<tr>"
                f"<td>#{row['id']}</td>"
                f"<td>{html.escape(str(row['title']))}</td>"
                f"<td>{html.escape(str(row['chat_type']))} {row['chat_id']}</td>"
                f"<td class=\"{'bad' if row['last_error'] else 'muted'}\">{html.escape(str(row['last_error'] or '-')[:80])}</td>"
                "</tr>"
                for row in feed_rows
            )
            feeds_html = f"<table><tr><th>ID</th><th>标题</th><th>目标</th><th>错误</th></tr>{rows}</table>"

        model_rows = db_rows(models_db, "SELECT provider, model_id, context_window, is_active, enabled FROM models ORDER BY provider, model_id")
        models_html = "".join(
            f"<li>{html.escape(row['provider'] + '/' + row['model_id'])}"
            f" — ctx {int(row['context_window']):,}"
            f"{' · 当前' if row['is_active'] else ''}"
            f"{'' if row['enabled'] else ' · 禁用'}</li>"
            for row in model_rows
        ) or "<li class='muted'>没有模型</li>"

        card_rows = db_rows(cards_db, "SELECT id, name, nickname, aliases, is_active FROM cards ORDER BY id")
        cards_html = "".join(
            f"<li>#{row['id']} {html.escape(str(row['nickname'] or row['name']))}"
            f"{' · 当前' if row['is_active'] else ''}"
            f"{' · 别名: ' + html.escape(str(row['aliases'])) if row['aliases'] and row['aliases'] != '[]' else ''}</li>"
            for row in card_rows
        ) or "<li class='muted'>没有角色卡</li>"

        return f"""<div style="display:flex;justify-content:space-between;align-items:baseline">
<h1>qqbot admin</h1><a href="/logout">退出</a></div>
<p class="muted">只读仪表盘 · {datetime.now().strftime(TIME_FMT)}</p>
{cards}
<h2>定时任务</h2>{tasks_html}
<h2>订阅</h2>{feeds_html}
<h2>模型</h2><ul>{models_html}</ul>
<h2>角色卡</h2><ul>{cards_html}</ul>
<p class="muted">v1 只读；任务/表情/订阅的写操作下一步再加。</p>"""


def main() -> int:
    parser = argparse.ArgumentParser(description="qqbot admin UI")
    parser.add_argument("--config", default="/etc/qqbot/config.toml")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config(args.config)
    admin = config.get("admin") or {}
    host = str(admin.get("host", "127.0.0.1"))
    port = int(admin.get("port", 8080))
    if not os.environ.get("ADMIN_UI_PASSWORD"):
        log.warning("ADMIN_UI_PASSWORD is not set; login will refuse until it is")

    AdminHandler.config = config
    server = ThreadingHTTPServer((host, port), AdminHandler)
    log.info("admin UI listening on http://%s:%d (password %s)", host, port, "set" if os.environ.get("ADMIN_UI_PASSWORD") else "missing")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
