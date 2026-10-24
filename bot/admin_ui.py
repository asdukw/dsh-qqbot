"""Admin UI: grouped JSON APIs plus a React/shadcn static bundle.

Binds 127.0.0.1 only; reach it through Workbench port forwarding. The React
bundle is served from ``[admin] static_dir`` (default
``/opt/qqbot/admin/dist``) and uses hash routing, so the server only serves
``index.html`` for unknown non-asset paths. Authentication is a single password
from ADMIN_UI_PASSWORD with an HMAC-signed session cookie.

Every endpoint is read-only. The overview aggregates counts, a day-by-day
trend and alerts; ``/api/models``, ``/api/cards``, ``/api/tasks``,
``/api/feeds``, ``/api/stickers``, ``/api/conversations`` and ``/api/system``
each expose one page's data. ``/api/dashboard`` is kept as the original
single-page aggregate for compatibility.
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
import platform
import sqlite3
import threading
import time
import tomllib
from datetime import datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

from admin_insights import clean_conversation, fun_data, usage_data
from sessions import SessionStore
from usage import conversation_label

log = logging.getLogger("qqbot.admin")

COOKIE = "qqbot_admin"
FAIL_WINDOW = 60
FAIL_LIMIT = 5
TIME_FMT = "%Y-%m-%d %H:%M"
DEFAULT_STATIC_DIR = "/opt/qqbot/admin/dist"
TZ = ZoneInfo("Asia/Shanghai")
STARTED_AT = int(time.time())

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


def db_rows(path: str, query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    if not Path(path).is_file():
        log.debug("database missing: %s", path)
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        log.warning("cannot open %s: %s", path, exc)
        return []
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(query, params).fetchall()
    except sqlite3.Error as exc:
        log.warning("query failed on %s: %s", path, exc)
        return []
    finally:
        conn.close()


def scalar(path: str, query: str, params: tuple[Any, ...] = ()) -> Any:
    rows = db_rows(path, query, params)
    return rows[0][0] if rows else None


def db_paths(config: dict[str, Any]) -> dict[str, str]:
    """Every SQLite file the panel reads, keyed by the section that owns it."""
    paths = config.get("paths") or {}
    profile = str(paths.get("profile_db") or "").strip()
    if not profile:
        profile = (
            os.environ.get("USER_PROFILE_DB", "").strip()
            or "/var/lib/qqbot/dsh-state/profile.db"
        )
    return {
        "memory": str(paths.get("database", "/var/lib/qqbot/memory.db")),
        "profile": profile,
        "models": str(
            (config.get("models") or {}).get("database", "/var/lib/qqbot/models.db")
        ),
        "cards": str(
            (config.get("cards") or {}).get("database", "/var/lib/qqbot/characters.db")
        ),
        "tasks": str(
            (config.get("tasks") or {}).get("database", "/var/lib/qqbot/tasks.db")
        ),
        "feeds": str(
            (config.get("feeds") or {}).get("database", "/var/lib/qqbot/feeds.db")
        ),
        "stickers": str(
            (config.get("stickers") or {}).get("database", "/var/lib/qqbot/stickers.db")
        ),
        "usage": str(
            (config.get("usage") or {}).get("database", "/var/lib/qqbot/usage.db")
        ),
    }


def _json_list(value: Any) -> list[str]:
    try:
        data = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data]


def _day_start(offset_days: int = 0) -> int:
    now = datetime.now(TZ)
    day = now.date() - timedelta(days=max(0, int(offset_days)))
    return int(datetime(day.year, day.month, day.day, tzinfo=TZ).timestamp())


def _scope_of(conversation_id: str) -> str:
    for prefix in ("group", "private", "task"):
        if conversation_id.startswith(f"{prefix}:"):
            return prefix
    return "other"


def _chat_id_of(conversation_id: str) -> int | None:
    _, _, suffix = conversation_id.partition(":")
    try:
        return int(suffix)
    except (TypeError, ValueError):
        return None


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
</style></head><body><div class="wrap">{body}</div></body></html>""".encode()


def overview_data(config: dict[str, Any], days: int = 7) -> dict[str, Any]:
    """Dashboard summary: counts, today's activity, a day-by-day trend, alerts."""
    days = max(1, min(int(days), 90))
    paths = db_paths(config)
    start = _day_start(days - 1)
    today_start = _day_start()

    message_counts = {
        str(row["day"]): int(row["n"] or 0)
        for row in db_rows(
            paths["memory"],
            "SELECT date(created_at, 'unixepoch', '+8 hours') AS day, COUNT(*) AS n "
            "FROM chat_events WHERE created_at >= ? GROUP BY day",
            (start,),
        )
    }
    token_counts = {
        str(row["day"]): int(row["tokens"] or 0)
        for row in db_rows(
            paths["usage"],
            "SELECT date(created_at, 'unixepoch', '+8 hours') AS day, "
            "COALESCE(SUM(total_tokens), 0) AS tokens FROM usage_events "
            "WHERE created_at >= ? GROUP BY day",
            (start,),
        )
    }
    today = datetime.now(TZ).date()
    trend = []
    for offset in range(days - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        trend.append({
            "day": day,
            "messages": message_counts.get(day, 0),
            "tokens": token_counts.get(day, 0),
        })

    today_messages = int(
        scalar(
            paths["memory"],
            "SELECT COUNT(*) FROM chat_events WHERE created_at >= ?",
            (today_start,),
        )
        or 0
    )
    today_conversations = int(
        scalar(
            paths["memory"],
            "SELECT COUNT(DISTINCT conversation_id) FROM chat_events "
            "WHERE created_at >= ?",
            (today_start,),
        )
        or 0
    )
    today_tokens = int(
        scalar(
            paths["usage"],
            "SELECT COALESCE(SUM(total_tokens), 0) FROM usage_events "
            "WHERE created_at >= ?",
            (today_start,),
        )
        or 0
    )
    today_calls = int(
        scalar(
            paths["usage"],
            "SELECT COUNT(*) FROM usage_events WHERE created_at >= ?",
            (today_start,),
        )
        or 0
    )

    conversations = conversations_data(config)["conversations"]
    counts = {
        "conversations": len(conversations),
        "messages": int(
            scalar(paths["memory"], "SELECT COUNT(*) FROM messages") or 0
        ),
        "models": {
            "total": int(
                scalar(paths["models"], "SELECT COUNT(*) FROM models") or 0
            ),
            "enabled": int(
                scalar(
                    paths["models"],
                    "SELECT COUNT(*) FROM models WHERE enabled = 1",
                )
                or 0
            ),
        },
        "cards": {
            "total": int(
                scalar(paths["cards"], "SELECT COUNT(*) FROM cards") or 0
            ),
        },
        "tasks": {
            "total": int(
                scalar(paths["tasks"], "SELECT COUNT(*) FROM tasks") or 0
            ),
            "enabled": int(
                scalar(
                    paths["tasks"],
                    "SELECT COUNT(*) FROM tasks WHERE enabled = 1",
                )
                or 0
            ),
        },
        "feeds": {
            "total": int(
                scalar(paths["feeds"], "SELECT COUNT(*) FROM feeds") or 0
            ),
            "errors": int(
                scalar(
                    paths["feeds"],
                    "SELECT COUNT(*) FROM feeds WHERE last_error <> ''",
                )
                or 0
            ),
        },
        "stickers": {
            "total": int(
                scalar(paths["stickers"], "SELECT COUNT(*) FROM stickers")
                or 0
            ),
            "banned": int(
                scalar(
                    paths["stickers"],
                    "SELECT COUNT(*) FROM stickers WHERE status = 'banned'",
                )
                or 0
            ),
        },
    }

    alerts = []
    for row in db_rows(
        paths["feeds"],
        "SELECT id, chat_type, chat_id, title, feed_url, last_error, last_check "
        "FROM feeds WHERE last_error <> '' ORDER BY last_check DESC, id DESC LIMIT 5",
    ):
        alerts.append({
            "kind": "feed",
            "id": int(row["id"]),
            "title": str(row["title"] or row["feed_url"] or "订阅"),
            "detail": str(row["last_error"] or ""),
            "at": row["last_check"] or None,
        })

    recent_messages = []
    for row in db_rows(
        paths["memory"],
        "SELECT conversation_id, sender_name, text, created_at FROM chat_events "
        "ORDER BY id DESC LIMIT 8",
    ):
        conversation_id = str(row["conversation_id"] or "")
        recent_messages.append({
            "conversation_id": conversation_id,
            "label": conversation_label(conversation_id),
            "sender_name": str(row["sender_name"] or ""),
            "text": str(row["text"] or "")[:140],
            "created_at": int(row["created_at"] or 0),
        })

    active_model = scalar(
        paths["models"],
        "SELECT provider || '/' || model_id FROM models WHERE is_active = 1",
    )
    active_card = scalar(
        paths["cards"],
        "SELECT COALESCE(NULLIF(nickname, ''), name) FROM cards WHERE is_active = 1",
    )
    return {
        "generated_at": int(time.time()),
        "days": days,
        "active_model": str(active_model or "-"),
        "active_card": str(active_card or "-"),
        "counts": counts,
        "today": {
            "messages": today_messages,
            "conversations": today_conversations,
            "tokens": today_tokens,
            "calls": today_calls,
        },
        "trend": trend,
        "alerts": alerts,
        "recent_messages": recent_messages,
    }


def conversations_data(config: dict[str, Any]) -> dict[str, Any]:
    """Known conversations, merged from chat events, session state and usage."""
    paths = db_paths(config)
    events: dict[str, sqlite3.Row] = {}
    for row in db_rows(
        paths["memory"],
        "SELECT e.conversation_id, COUNT(*) AS messages, MAX(e.created_at) AS last_at, "
        "(SELECT x.sender_name FROM chat_events x "
        " WHERE x.conversation_id = e.conversation_id ORDER BY x.id DESC LIMIT 1) "
        " AS sender_name, "
        "(SELECT x.chat_id FROM chat_events x "
        " WHERE x.conversation_id = e.conversation_id ORDER BY x.id DESC LIMIT 1) "
        " AS chat_id "
        "FROM chat_events e GROUP BY e.conversation_id",
    ):
        conversation_id = str(row["conversation_id"] or "")
        if conversation_id:
            events[conversation_id] = row

    sessions: dict[str, sqlite3.Row] = {}
    for row in db_rows(
        paths["memory"],
        "SELECT conversation_id, generation, started_at, last_active, messages "
        "FROM session_state",
    ):
        conversation_id = str(row["conversation_id"] or "")
        if conversation_id:
            sessions[conversation_id] = row

    usage: dict[str, sqlite3.Row] = {}
    for row in db_rows(
        paths["usage"],
        "SELECT conversation_id, COALESCE(SUM(total_tokens), 0) AS tokens, "
        "MAX(created_at) AS last_at FROM usage_events GROUP BY conversation_id",
    ):
        conversation_id = str(row["conversation_id"] or "")
        if conversation_id:
            usage[conversation_id] = row

    identities: dict[str, sqlite3.Row] = {}
    for row in db_rows(
        paths["profile"],
        "SELECT session_id, sender_name, updated_at FROM conversations "
        "ORDER BY updated_at DESC",
    ):
        session_id = str(row["session_id"] or "")
        if session_id:
            identities[session_id] = row

    rows = []
    for conversation_id in set(events) | set(sessions) | set(usage):
        event = events.get(conversation_id)
        session = sessions.get(conversation_id)
        totals = usage.get(conversation_id)
        generation = int(session["generation"]) if session is not None else None
        session_id = (
            SessionStore.session_id_for(conversation_id, generation)
            if generation is not None
            else None
        )
        identity = identities.get(session_id) if session_id else None
        timestamps = [
            int(value)
            for value in (
                event["last_at"] if event is not None else None,
                session["last_active"] if session is not None else None,
                totals["last_at"] if totals is not None else None,
            )
            if value is not None
        ]
        rows.append({
            "id": conversation_id,
            "scope": _scope_of(conversation_id),
            "label": conversation_label(conversation_id),
            "chat_id": (
                int(event["chat_id"])
                if event is not None and event["chat_id"] is not None
                else _chat_id_of(conversation_id)
            ),
            "sender_name": str(
                (identity["sender_name"] if identity is not None else "")
                or (event["sender_name"] if event is not None else "")
                or ""
            ),
            "messages": int(event["messages"]) if event is not None else 0,
            "last_at": max(timestamps) if timestamps else None,
            "tokens": int(totals["tokens"]) if totals is not None else 0,
            "generation": generation,
            "session_id": session_id,
            "session_messages": int(session["messages"]) if session is not None else 0,
            "session_started_at": (
                int(session["started_at"]) if session is not None else None
            ),
            "session_last_active": (
                int(session["last_active"]) if session is not None else None
            ),
        })
    rows.sort(key=lambda row: (row["last_at"] or 0, row["id"]), reverse=True)
    return {"generated_at": int(time.time()), "conversations": rows}


def models_data(config: dict[str, Any]) -> dict[str, Any]:
    path = db_paths(config)["models"]
    rows = []
    for row in db_rows(
        path,
        "SELECT provider, model_id, display_name, api, base_url, api_key_env, "
        "context_window, max_output_tokens, reasoning_effort, capabilities, "
        "enabled, is_active, updated_at FROM models "
        "ORDER BY is_active DESC, provider, model_id",
    ):
        try:
            capabilities = json.loads(row["capabilities"] or "{}")
        except (TypeError, ValueError):
            capabilities = {}
        if not isinstance(capabilities, dict):
            capabilities = {}
        rows.append({
            "provider": str(row["provider"]),
            "model_id": str(row["model_id"]),
            "display_name": str(row["display_name"] or row["model_id"]),
            "api": str(row["api"] or ""),
            "base_url": str(row["base_url"] or ""),
            "api_key_env": str(row["api_key_env"] or ""),
            "context_window": int(row["context_window"] or 0),
            "max_output_tokens": int(row["max_output_tokens"] or 0),
            "reasoning_effort": str(row["reasoning_effort"] or ""),
            "capabilities": capabilities,
            "enabled": bool(row["enabled"]),
            "is_active": bool(row["is_active"]),
            "updated_at": int(row["updated_at"] or 0),
        })
    active = next(
        (f"{row['provider']}/{row['model_id']}" for row in rows if row["is_active"]),
        "-",
    )
    return {"generated_at": int(time.time()), "active": active, "models": rows}


def cards_data(config: dict[str, Any]) -> dict[str, Any]:
    path = db_paths(config)["cards"]
    cards = []
    for row in db_rows(
        path,
        "SELECT id, name, nickname, aliases, description, tags, creator, "
        "character_version, source_kind, source, installed_by, is_active, "
        "created_at, updated_at FROM cards ORDER BY is_active DESC, id",
    ):
        description = str(row["description"] or "")
        cards.append({
            "id": int(row["id"]),
            "name": str(row["name"] or ""),
            "nickname": str(row["nickname"] or ""),
            "aliases": _json_list(row["aliases"]),
            "description": description[:240],
            "description_chars": len(description),
            "tags": _json_list(row["tags"]),
            "creator": str(row["creator"] or ""),
            "character_version": str(row["character_version"] or ""),
            "source_kind": str(row["source_kind"] or ""),
            "source": str(row["source"] or ""),
            "installed_by": str(row["installed_by"] or ""),
            "is_active": bool(row["is_active"]),
            "created_at": int(row["created_at"] or 0),
            "updated_at": int(row["updated_at"] or 0),
        })
    active = next(
        (card["nickname"] or card["name"] for card in cards if card["is_active"]),
        "-",
    )
    return {"generated_at": int(time.time()), "active_card": active, "cards": cards}


def tasks_data(config: dict[str, Any]) -> dict[str, Any]:
    path = db_paths(config)["tasks"]
    tasks = []
    for row in db_rows(
        path,
        "SELECT id, chat_type, chat_id, created_by, instruction, message, kind, "
        "time_of_day, weekday, run_at, every_minutes, mode, enabled, created_at, "
        "next_run, last_run, runs FROM tasks "
        "ORDER BY enabled DESC, next_run ASC, id ASC",
    ):
        tasks.append({
            "id": int(row["id"]),
            "chat_type": str(row["chat_type"] or ""),
            "chat_id": int(row["chat_id"] or 0),
            "created_by": str(row["created_by"] or ""),
            "instruction": str(row["instruction"] or ""),
            "message": str(row["message"] or ""),
            "kind": str(row["kind"] or ""),
            "time_of_day": row["time_of_day"],
            "weekday": row["weekday"],
            "run_at": row["run_at"],
            "every_minutes": row["every_minutes"],
            "mode": str(row["mode"] or ""),
            "enabled": bool(row["enabled"]),
            "created_at": int(row["created_at"] or 0),
            "next_run": row["next_run"],
            "last_run": int(row["last_run"] or 0),
            "runs": int(row["runs"] or 0),
        })
    enabled = sum(1 for task in tasks if task["enabled"])
    return {
        "generated_at": int(time.time()),
        "tasks": tasks,
        "totals": {"total": len(tasks), "enabled": enabled},
    }


def feeds_data(config: dict[str, Any]) -> dict[str, Any]:
    path = db_paths(config)["feeds"]
    feeds = []
    for row in db_rows(
        path,
        "SELECT id, chat_type, chat_id, title, feed_url, source, seen, enabled, "
        "created_at, last_check, last_error FROM feeds ORDER BY enabled DESC, id",
    ):
        feeds.append({
            "id": int(row["id"]),
            "chat_type": str(row["chat_type"] or ""),
            "chat_id": int(row["chat_id"] or 0),
            "title": str(row["title"] or ""),
            "feed_url": str(row["feed_url"] or ""),
            "source": str(row["source"] or ""),
            "seen_count": len(_json_list(row["seen"])),
            "enabled": bool(row["enabled"]),
            "created_at": int(row["created_at"] or 0),
            "last_check": int(row["last_check"] or 0),
            "last_error": str(row["last_error"] or ""),
        })
    errors = sum(1 for feed in feeds if feed["last_error"])
    return {
        "generated_at": int(time.time()),
        "feeds": feeds,
        "total": len(feeds),
        "errors": errors,
    }


def stickers_data(config: dict[str, Any]) -> dict[str, Any]:
    path = db_paths(config)["stickers"]
    stickers = []
    for row in db_rows(
        path,
        "SELECT id, description, tags, source, source_kind, status, uses, size, "
        "mime, created_at, last_used FROM stickers "
        "ORDER BY CASE status WHEN 'ok' THEN 0 ELSE 1 END, last_used DESC, id DESC",
    ):
        stickers.append({
            "id": int(row["id"]),
            "description": str(row["description"] or ""),
            "tags": _json_list(row["tags"]),
            "source": str(row["source"] or ""),
            "source_kind": str(row["source_kind"] or ""),
            "status": str(row["status"] or ""),
            "uses": int(row["uses"] or 0),
            "size": int(row["size"] or 0),
            "mime": str(row["mime"] or ""),
            "created_at": int(row["created_at"] or 0),
            "last_used": int(row["last_used"] or 0),
        })
    return {
        "generated_at": int(time.time()),
        "stickers": stickers,
        "totals": {
            "total": len(stickers),
            "banned": sum(1 for item in stickers if item["status"] == "banned"),
            "used": sum(1 for item in stickers if item["uses"] > 0),
        },
    }


def system_data(config: dict[str, Any], static_dir: str = "") -> dict[str, Any]:
    databases = []
    for key, path in db_paths(config).items():
        file = Path(path)
        try:
            size = file.stat().st_size
        except OSError:
            size = None
        databases.append({
            "key": key,
            "path": str(path),
            "exists": file.is_file(),
            "size": size,
        })
    admin = config.get("admin") or {}
    dsh = config.get("dsh") or {}
    usage = config.get("usage") or {}
    return {
        "generated_at": int(time.time()),
        "started_at": STARTED_AT,
        "uptime_seconds": max(0, int(time.time()) - STARTED_AT),
        "python": platform.python_version(),
        "static_dir": static_dir,
        "admin": {
            "host": str(admin.get("host", "127.0.0.1")),
            "port": int(admin.get("port", 8080)),
            "session_hours": float(admin.get("session_hours", 12)),
            "allow_cidrs": [str(item) for item in (admin.get("allow_cidrs") or [])],
        },
        "dsh": {
            "home": str(dsh.get("home", "/var/lib/qqbot/dsh-home")),
            "workspace": str(dsh.get("workspace", "/var/lib/qqbot/dsh-workspace")),
            "profile": str(dsh.get("profile", "qqbot")),
            "image_mode": str(dsh.get("image_mode", "")),
        },
        "usage": {
            "retention_days": int(usage.get("retention_days", 90)),
            "event_retention_days": int(usage.get("event_retention_days", 90)),
        },
        "databases": databases,
    }


def dashboard_data(config: dict[str, Any]) -> dict[str, Any]:
    """Backward-compatible aggregate used by the original single-page view."""
    paths = db_paths(config)
    overview = overview_data(config)
    models = models_data(config)["models"]
    cards = cards_data(config)["cards"]
    tasks = tasks_data(config)["tasks"]
    feeds = feeds_data(config)["feeds"]
    counts = overview["counts"]
    return {
        "generated_at": overview["generated_at"],
        "active_model": overview["active_model"],
        "active_card": overview["active_card"],
        "messages": {
            "total": counts["messages"],
            "last_at": scalar(paths["memory"], "SELECT MAX(created_at) FROM messages"),
        },
        "tasks": counts["tasks"],
        "feeds": {"total": counts["feeds"]["total"]},
        "stickers": counts["stickers"],
        "model_rows": [
            {
                "provider": row["provider"],
                "model_id": row["model_id"],
                "context_window": row["context_window"],
                "is_active": row["is_active"],
                "enabled": row["enabled"],
            }
            for row in models
        ],
        "card_rows": [
            {
                "id": card["id"],
                "name": card["name"],
                "nickname": card["nickname"],
                "aliases": card["aliases"],
                "is_active": card["is_active"],
            }
            for card in cards
        ],
        "task_rows": tasks[:20],
        "feed_rows": feeds[:20],
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

    def _api_guard(self) -> bool:
        if self._valid_session():
            return True
        self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return False

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
        resources = {
            "/api/conversations": conversations_data,
            "/api/models": models_data,
            "/api/cards": cards_data,
            "/api/tasks": tasks_data,
            "/api/feeds": feeds_data,
            "/api/stickers": stickers_data,
        }
        if path in resources:
            if not self._api_guard():
                return
            self._send_json(HTTPStatus.OK, resources[path](self.config))
            return
        if path == "/api/system":
            if not self._api_guard():
                return
            self._send_json(
                HTTPStatus.OK, system_data(self.config, self.static_dir)
            )
            return
        if path == "/api/overview":
            if not self._api_guard():
                return
            query = parse_qs(urlparse(self.path).query)
            try:
                days = int((query.get("days") or ["7"])[0])
            except (TypeError, ValueError):
                days = 7
            self._send_json(HTTPStatus.OK, overview_data(self.config, days))
            return
        if path == "/api/dashboard":
            if not self._api_guard():
                return
            self._send_json(HTTPStatus.OK, dashboard_data(self.config))
            return
        if path in ("/api/usage", "/api/fun"):
            if not self._api_guard():
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
