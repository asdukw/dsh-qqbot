"""Admin UI: grouped JSON APIs plus a React/shadcn static bundle.

Binds 127.0.0.1 only; reach it through Workbench port forwarding. The React
bundle is served from ``[admin] static_dir`` (default
``/opt/qqbot/admin/dist``) and uses hash routing, so the server only serves
``index.html`` for unknown non-asset paths. Authentication is a single password
from ADMIN_UI_PASSWORD with an HMAC-signed session cookie.

Every page reads SQLite directly. The model page is the exception: it can add
providers and models through authenticated ``POST /api/models/*`` endpoints,
which write through ``ModelRegistry`` (the same code the bot uses). The
approval page also writes: ``POST /api/requests/action`` queues an approve or
reject decision in ``approvals.db``, and the bot worker executes it through
NapCat. The sticker page can delete one sticker and block its perceptual hash
for future uploads via ``POST /api/stickers/delete``. The overview aggregates
counts, a day-by-day trend and alerts;
``/api/models``, ``/api/cards``, ``/api/tasks``, ``/api/feeds``,
``/api/stickers``, ``/api/requests`` and ``/api/system`` each expose one
page's data;
``/api/cards/<id>`` returns one card's full V3 payload for the detail view;
``/api/stickers/<id>/image`` streams a sticker file (``?thumb=1`` for a
bounded JPEG thumbnail) so the panel can show the images themselves.
``/api/dashboard`` is kept as the original single-page aggregate for
compatibility. ``POST /api/models/provider-models`` asks a saved provider
for its model list so the panel does not need a hand-typed model id.
The voice page switches the active TTS voice online through
``POST /api/voice/active`` (written to the shared voice state file the bot
re-reads before every voice reply) and previews it through
``POST /api/voice/preview``.
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
from collections import OrderedDict
from datetime import datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

import httpx
from admin_insights import clean_conversation, fun_data, usage_data
from admin_story import queue_story, story_data, story_detail
from admin_video import handle_post as video_credentials_post
from admin_video import status_payload as video_credentials_payload
from admin_voice import apply_voice_action, preview_audio, voice_data
from approvals import ApprovalRecord, ApprovalStore
from models import (
    BUILTIN_PROVIDERS,
    PROVIDER_PRESETS,
    ModelRecord,
    ModelRegistry,
    ProviderRecord,
)
from sessions import SessionStore
from stickers import StickerStore
from transport_health import transport_health
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

# Only the formats StickerStore accepts are ever served back.
STICKER_IMAGE_MIME = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
THUMB_PX = 320
THUMB_MAX_PIXELS = 12_000_000
THUMB_CACHE_LIMIT = 512
THUMB_WORKERS = 2
_thumb_cache: OrderedDict[tuple[str, int], tuple[bytes, str]] = OrderedDict()
_thumb_lock = threading.Lock()
_thumb_slots = threading.BoundedSemaphore(THUMB_WORKERS)


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
            os.environ.get("STICKER_DB", "").strip()
            or (config.get("stickers") or {}).get(
                "database", "/var/lib/qqbot/stickers.db"
            )
        ),
        "usage": str(
            (config.get("usage") or {}).get("database", "/var/lib/qqbot/usage.db")
        ),
        "approvals": str(
            (config.get("approvals") or {}).get(
                "database", "/var/lib/qqbot/approvals.db"
            )
        ),
    }


def sticker_directory(config: dict[str, Any]) -> Path:
    """Root that every sticker file must live under before we serve it.

    Mirrors bot/main.py: the STICKER_DIR override wins over [stickers]
    directory, and the fallback matches StickerStore's default.
    """
    env = os.environ.get("STICKER_DIR", "").strip()
    if env:
        return Path(env)
    section = config.get("stickers") or {}
    configured = str(section.get("directory") or "").strip()
    return Path(configured or "/var/lib/qqbot/stickers")


def sticker_target(config: dict[str, Any], raw_path: Any) -> Path | None:
    """Resolve one stored path, refusing anything outside the library root."""
    stored = str(raw_path or "").strip()
    if not stored:
        return None
    try:
        target = Path(stored).resolve(strict=True)
        target.relative_to(sticker_directory(config).resolve())
    except (OSError, ValueError, RuntimeError):
        return None
    return target if target.is_file() else None


def _make_thumb(data: bytes) -> bytes | None:
    """First frame of a sticker as a small JPEG; None keeps the original."""
    try:
        from PIL import Image, ImageOps
    except ImportError:  # pragma: no cover - Pillow ships with the bot
        return None
    try:
        with Image.open(BytesIO(data)) as opened:
            # Read the dimensions before decoding: a compressed image bomb can
            # still hold hundreds of megapixels and would OOM this process.
            if opened.width * opened.height > THUMB_MAX_PIXELS:
                return None
            opened.seek(0)
            # exif_transpose copies the whole bitmap even without an
            # orientation tag, so only pay for it when the tag is there.
            image = (
                ImageOps.exif_transpose(opened) if opened.getexif().get(274) else opened
            )
            if image.mode == "RGB":
                target = image
            elif image.mode in ("RGBA", "LA", "P", "PA"):
                rgba = image.convert("RGBA")
                target = Image.new("RGB", rgba.size, (255, 255, 255))
                target.paste(rgba, mask=rgba.getchannel("A"))
            else:
                target = image.convert("RGB")
            target.thumbnail((THUMB_PX, THUMB_PX), Image.Resampling.LANCZOS)
            out = BytesIO()
            target.save(out, format="JPEG", quality=82, optimize=True)
            return out.getvalue()
    except Exception as exc:  # noqa: BLE001 - untrusted decoders raise anything
        log.debug("cannot thumbnail sticker: %s", exc)
        return None


def sticker_store(config: dict[str, Any]) -> StickerStore:
    section = config.get("stickers") or {}
    return StickerStore(
        str(section.get("database", "/var/lib/qqbot/stickers.db")),
        sticker_directory(config),
        phash_distance=max(0, min(64, int(section.get("phash_distance", 4)))),
    )


def delete_sticker(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Delete one sticker and optionally block its perceptual hash."""
    try:
        sticker_id = int(payload.get("id") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("id 必须是数字") from exc
    if sticker_id <= 0:
        raise ValueError("缺少表情 id")
    raw_block = payload.get("block_similar", True)
    if isinstance(raw_block, str):
        block_similar = raw_block.strip().lower() in ("1", "true", "yes", "on")
    else:
        block_similar = bool(raw_block)
    store = sticker_store(config)
    try:
        deleted, blocked = store.delete_blocking(
            sticker_id, block_similar=block_similar
        )
        remaining = store.count()
    finally:
        store.close()
    if deleted == 0:
        raise ValueError("表情不存在或已被删除")
    return {
        "id": sticker_id,
        "deleted": deleted,
        "blocked": blocked,
        "remaining": remaining,
    }


def sticker_image(
    config: dict[str, Any], sticker_id: int, thumb: bool = False
) -> tuple[bytes, str] | None:
    """Read one sticker from disk, optionally as a cached thumbnail."""
    path = db_paths(config)["stickers"]
    rows = db_rows(
        path, "SELECT path, mime FROM stickers WHERE id = ?", (sticker_id,)
    )
    if not rows:
        return None
    row = rows[0]
    target = sticker_target(config, row["path"])
    if target is None:
        log.warning("sticker #%s file missing or outside the library", sticker_id)
        return None
    try:
        data = target.read_bytes()
    except OSError as exc:
        log.warning("cannot read sticker #%s: %s", sticker_id, exc)
        return None
    mime = str(row["mime"] or "").strip().lower()
    if mime not in STICKER_IMAGE_MIME:
        mime = STATIC_MIME.get(target.suffix.lower(), "")
    if mime not in STICKER_IMAGE_MIME:
        return None
    if not thumb:
        return data, mime

    try:
        stamp = target.stat().st_mtime_ns
    except OSError:
        return data, mime
    key = (str(target), stamp)
    with _thumb_lock:
        cached = _thumb_cache.get(key)
        if cached is not None:
            _thumb_cache.move_to_end(key)
            return cached
    with _thumb_slots:
        thumb_data = _make_thumb(data)
    if thumb_data is None:
        # Undecodable (or Pillow absent): showing the original beats a blank tile.
        return data, mime
    result = (thumb_data, "image/jpeg")
    with _thumb_lock:
        _thumb_cache[key] = result
        _thumb_cache.move_to_end(key)
        while len(_thumb_cache) > THUMB_CACHE_LIMIT:
            _thumb_cache.popitem(last=False)
    return result


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


def _models_registry(config: dict[str, Any]) -> ModelRegistry | None:
    """Open the registry for a read; missing files are an empty page, not a 500."""
    path = db_paths(config)["models"]
    if not Path(path).is_file():
        return None
    try:
        return ModelRegistry(path)
    except (OSError, sqlite3.Error, ValueError) as exc:
        log.warning("cannot open model registry %s: %s", path, exc)
        return None


def _provider_payload(provider: ProviderRecord) -> dict[str, Any]:
    """Provider metadata for the UI. Never includes the key itself."""
    env_key = os.environ.get(provider.api_key_env, "") if provider.api_key_env else ""
    stored = provider.has_stored_key
    if stored:
        source = "stored"
    elif env_key.strip():
        source = "env"
    else:
        source = "missing"
    return {
        "provider": provider.provider,
        "display_name": provider.display_name,
        "api": provider.api,
        "base_url": provider.base_url,
        "enabled": provider.enabled,
        "builtin": provider.builtin,
        "model_count": provider.model_count,
        "enabled_models": provider.enabled_models,
        "active_model_id": provider.active_model_id,
        "api_key_set": source != "missing",
        "api_key_source": source,
        "updated_at": provider.updated_at,
    }


def _model_payload(model: ModelRecord) -> dict[str, Any]:
    return {
        "provider": model.provider,
        "model_id": model.model_id,
        "display_name": model.display_name,
        "api": model.api,
        "base_url": model.base_url,
        "context_window": model.context_window,
        "max_output_tokens": model.max_output_tokens,
        "reasoning_effort": model.reasoning_effort,
        "capabilities": model.capabilities,
        "enabled": model.enabled,
        "is_active": model.is_active,
        "updated_at": model.updated_at,
        "provider_display_name": model.provider_display_name,
        "provider_enabled": model.provider_enabled,
        "builtin": model.provider in BUILTIN_PROVIDERS,
    }


class ProviderModelsError(RuntimeError):
    """The upstream provider could not return its model list."""


def _provider_api_key(record: ProviderRecord) -> str:
    """Stored key wins; otherwise read the provider's configured env var."""
    key = record.api_key.strip()
    if not key and record.api_key_env:
        key = os.environ.get(record.api_key_env, "").strip()
    return key


def _provider_models_url(base_url: str) -> str:
    base = str(base_url or "").strip()
    if not base:
        raise ValueError("供应商未配置 base_url")
    try:
        parsed = httpx.URL(base)
    except httpx.InvalidURL as exc:
        raise ValueError(f"供应商 base_url 无效：{exc}") from exc
    if parsed.scheme not in ("http", "https"):
        raise ValueError("供应商 base_url 必须是 http(s) 地址")
    if not parsed.host:
        raise ValueError("供应商 base_url 缺少主机名")
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[: -len("/chat/completions")]
    if not path.endswith("/models"):
        path = f"{path}/models"
    if not path.startswith("/"):
        path = "/" + path
    # copy_with keeps a query string such as ?api-version=... intact.
    return str(parsed.copy_with(path=path))


def _model_list(payload: Any) -> list[dict[str, str]]:
    """Normalise the common OpenAI-compatible /models response shapes."""
    items: Any = payload
    if isinstance(payload, dict):
        items = payload.get("data")
        if not isinstance(items, list):
            items = payload.get("models")
    if not isinstance(items, list):
        raise ProviderModelsError("上游返回的模型列表格式无法识别")
    found: dict[str, str] = {}
    for item in items:
        if isinstance(item, str):
            model_id, owned_by = item.strip(), ""
        elif isinstance(item, dict):
            model_id = str(item.get("id") or item.get("name") or "").strip()
            owned_by = str(
                item.get("owned_by") or item.get("owner") or ""
            ).strip()
        else:
            continue
        if model_id:
            found.setdefault(model_id, owned_by)
    if not found:
        raise ProviderModelsError("上游没有返回任何模型")
    return [{"id": model_id, "owned_by": found[model_id]} for model_id in sorted(found)]


MAX_MODELS_BYTES = 2 * 1024 * 1024


def _read_prefix(response: httpx.Response, limit: int) -> bytes:
    """Read a short prefix of an error body and always close the response."""
    data = bytearray()
    try:
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) >= limit:
                break
    finally:
        response.close()
    return bytes(data[:limit])


def _read_body(response: httpx.Response, limit: int) -> bytes:
    data = bytearray()
    try:
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > limit:
                raise ProviderModelsError("上游响应过大")
    finally:
        response.close()
    return bytes(data)


def provider_models(config: dict[str, Any], provider: str) -> dict[str, Any]:
    """Fetch one provider's model ids from its OpenAI-compatible endpoint."""
    provider = str(provider or "").strip()
    if not provider:
        raise ValueError("缺少 provider")
    registry = _models_registry(config)
    if registry is None:
        raise ValueError("模型数据库不存在，请先启动 qqbot")
    try:
        record = registry.get_provider(provider)
    finally:
        registry.close()
    if record is None:
        raise ValueError(f"供应商 {provider!r} 不存在")
    url = _provider_models_url(record.base_url)
    headers = {"Accept": "application/json", "User-Agent": "qqbot-admin"}
    key = _provider_api_key(record)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        # No redirects: a custom x-api-key header is not stripped on a
        # cross-origin redirect, and the key must only go to the provider.
        with httpx.Client(
            timeout=httpx.Timeout(15.0, connect=8.0), follow_redirects=False
        ) as client:
            request = client.build_request("GET", url, headers=headers)
            response = client.send(request, stream=True)
            if key and response.status_code in (401, 403):
                # Some OpenAI-compatible gateways (OpenCode Zen) expect the
                # key in x-api-key instead of a Bearer token.
                response.close()
                alt = {**headers}
                alt.pop("Authorization", None)
                alt["x-api-key"] = key
                response = client.send(
                    client.build_request("GET", url, headers=alt), stream=True
                )
            if 300 <= response.status_code < 400:
                response.close()
                raise ProviderModelsError(
                    f"上游返回了重定向（HTTP {response.status_code}），"
                    "请把供应商 base_url 改成最终地址"
                )
            if response.status_code >= 400:
                detail = " ".join(
                    _read_prefix(response, 512).decode("utf-8", "replace").split()
                )[:200]
                hint = (
                    "（请检查供应商的 API Key）"
                    if response.status_code in (401, 403)
                    else ""
                )
                message = f"上游返回 HTTP {response.status_code}{hint}"
                raise ProviderModelsError(f"{message}：{detail}" if detail else message)
            body = _read_body(response, MAX_MODELS_BYTES)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise ProviderModelsError(f"网络请求失败：{exc}") from exc
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError) as exc:
        raise ProviderModelsError("上游返回的不是 JSON") from exc
    models = _model_list(payload)
    return {"provider": provider, "count": len(models), "models": models}


def models_data(config: dict[str, Any]) -> dict[str, Any]:
    registry = _models_registry(config)
    if registry is None:
        return {
            "generated_at": int(time.time()),
            "active": "-",
            "providers": [],
            "models": [],
            "presets": [
                {key: value for key, value in preset.items() if key != "api_key_env"}
                for preset in PROVIDER_PRESETS
            ],
        }
    try:
        providers = [_provider_payload(row) for row in registry.list_providers()]
        models = [_model_payload(row) for row in registry.list()]
        active = registry.active()
        return {
            "generated_at": int(time.time()),
            "active": active.key if active else "-",
            "providers": providers,
            "models": models,
            "presets": [
                {key: value for key, value in preset.items() if key != "api_key_env"}
                for preset in PROVIDER_PRESETS
            ],
        }
    finally:
        registry.close()


def _string_field(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    return "" if value is None else str(value).strip()


def _enabled_field(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return "1" if str(value).strip().lower() in ("1", "true", "yes", "on") else "0"


def _provider_fields(payload: dict[str, Any]) -> dict[str, str]:
    fields = {
        name: _string_field(payload, name)
        for name in ("display_name", "api", "base_url", "api_key_env", "api_key")
        if name in payload and payload[name] is not None
    }
    if "enabled" in payload:
        fields["enabled"] = _enabled_field(payload["enabled"])
    return fields


def _model_fields(payload: dict[str, Any]) -> dict[str, str]:
    fields = {
        name: _string_field(payload, name)
        for name in ("display_name", "context_window", "max_output_tokens", "reasoning_effort")
        if name in payload and payload[name] is not None
    }
    if "enabled" in payload:
        fields["enabled"] = _enabled_field(payload["enabled"])
    if "capabilities" in payload:
        capabilities = payload["capabilities"]
        if isinstance(capabilities, (dict, list)):
            fields["capabilities"] = json.dumps(capabilities, ensure_ascii=False)
        else:
            fields["capabilities"] = str(capabilities)
    return fields


def apply_models_action(
    config: dict[str, Any], action: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Apply one model-page mutation and return the refreshed page payload."""
    path = db_paths(config)["models"]
    if not Path(path).is_file():
        raise ValueError("模型数据库不存在，请先启动 qqbot")
    registry = ModelRegistry(path)
    try:
        provider = _string_field(payload, "provider")
        model_id = _string_field(payload, "model_id")
        if action == "provider.upsert":
            registry.upsert_provider(provider, _provider_fields(payload))
        elif action == "provider.delete":
            registry.delete_provider(provider)
        elif action == "model.upsert":
            registry.upsert(provider, model_id, _model_fields(payload))
        elif action == "model.delete":
            registry.delete_model(provider, model_id)
        elif action == "model.activate":
            registry.use(provider, model_id)
        else:
            raise ValueError(f"unknown action {action!r}")
    finally:
        registry.close()
    return models_data(config)


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


# Embedded asset blobs (``data:`` URIs) can be megabytes; the detail view only
# needs to know they exist.
_ASSET_URI_PREVIEW = 120


def _asset_payload(asset: Any) -> Any:
    if not isinstance(asset, dict):
        return asset
    uri = asset.get("uri")
    if isinstance(uri, str) and uri.startswith("data:") and len(uri) > _ASSET_URI_PREVIEW:
        header = uri.split(",", 1)[0]
        return {**asset, "uri": f"{header},… ({len(uri)} chars)"}
    return asset


def card_detail_data(config: dict[str, Any], card_id: int) -> dict[str, Any] | None:
    """Return one card's stored V3 payload plus its library metadata."""
    rows = db_rows(
        db_paths(config)["cards"],
        "SELECT id, name, nickname, aliases, tags, creator, character_version, "
        "fingerprint, payload, source_kind, source, installed_by, is_active, "
        "created_at, updated_at FROM cards WHERE id = ?",
        (card_id,),
    )
    if not rows:
        return None
    row = rows[0]
    try:
        payload = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    data = payload.get("data")
    if not isinstance(data, dict):
        data = {}
    if isinstance(data.get("assets"), list):
        data = {**data, "assets": [_asset_payload(asset) for asset in data["assets"]]}
    imported = payload.get("_import")
    return {
        "generated_at": int(time.time()),
        "id": int(row["id"]),
        "name": str(row["name"] or ""),
        "nickname": str(row["nickname"] or ""),
        "aliases": _json_list(row["aliases"]),
        "tags": _json_list(row["tags"]),
        "creator": str(row["creator"] or ""),
        "character_version": str(row["character_version"] or ""),
        "fingerprint": str(row["fingerprint"] or ""),
        "character_id": str(dict((db_rows(db_paths(config)["cards"], "SELECT character_id FROM cards WHERE id=?", (card_id,)) or [{}])[0]).get("character_id", "")),
        "source_kind": str(row["source_kind"] or ""),
        "source": str(row["source"] or ""),
        "installed_by": str(row["installed_by"] or ""),
        "is_active": bool(row["is_active"]),
        "created_at": int(row["created_at"] or 0),
        "updated_at": int(row["updated_at"] or 0),
        "spec": str(payload.get("spec") or "chara_card_v3"),
        "spec_version": str(payload.get("spec_version") or "3.0"),
        "imported_from": imported if isinstance(imported, dict) else {},
        "data": data,
    }


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


def approvals_data(config: dict[str, Any]) -> dict[str, Any]:
    enabled = bool((config.get("approvals") or {}).get("enabled", True))
    path = db_paths(config)["approvals"]
    if not enabled or not Path(path).is_file():
        return {
            "generated_at": int(time.time()),
            "enabled": enabled,
            "pending": 0,
            "requests": [],
        }
    try:
        store = ApprovalStore(path, readonly=True)
    except (OSError, sqlite3.Error):
        return {
            "generated_at": int(time.time()),
            "enabled": enabled,
            "pending": 0,
            "requests": [],
        }
    try:
        records = store.list(limit=200)
        pending = store.active_count()
    except sqlite3.Error:
        records = []
        pending = 0
    finally:
        store.close()
    return {
        "generated_at": int(time.time()),
        "enabled": enabled,
        "pending": pending,
        "requests": [_approval_payload(record) for record in records],
    }


def _approval_payload(record: ApprovalRecord) -> dict[str, Any]:
    """Public shape for the admin page; the OneBot flag stays server-side."""
    return {
        "id": record.id,
        "kind": record.kind,
        "user_id": record.user_id,
        "group_id": record.group_id,
        "group_name": record.group_name,
        "requester_name": record.requester_name,
        "comment": record.comment,
        "status": record.status,
        "action": record.action,
        "error": record.error,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "handled_at": record.handled_at,
    }


def queue_approval(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Queue an approve/reject decision; the bot worker executes it."""
    try:
        request_id = int(payload.get("id") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("id 必须是数字") from exc
    if request_id <= 0:
        raise ValueError("缺少请求 id")
    action = str(payload.get("action") or "").strip().lower()
    if action not in ("approve", "reject"):
        raise ValueError("action 必须是 approve 或 reject")
    if not bool((config.get("approvals") or {}).get("enabled", True)):
        raise ValueError("审批功能未启用（[approvals] enabled = false）")
    store = ApprovalStore(db_paths(config)["approvals"])
    try:
        record = store.queue(request_id, action)
    finally:
        store.close()
    return _approval_payload(record)


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
        "SELECT id, path, description, tags, source, source_kind, status, uses, "
        "size, mime, created_at, last_used FROM stickers "
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
            "has_image": sticker_target(config, row["path"]) is not None,
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
        "transport": transport_health(config),
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
    def _safe_request_path(self) -> str:
        try:
            path = urlparse(getattr(self, "path", "")).path or "-"
        except ValueError:
            return "<invalid>"
        if path in {
            "/", "/index.html", "/login", "/logout", "/healthz",
            "/api/login", "/api/logout", "/api/session",
        }:
            return path
        if path.startswith("/api/"):
            return "/api/*"
        if path.startswith("/assets/"):
            return "/assets/*"
        return "<other>"

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        # The default implementation logs the raw request line, including its
        # query string. Keep access logs useful without recording credentials.
        method = getattr(self, "command", "-")
        if method not in {"GET", "POST", "HEAD", "OPTIONS"}:
            method = "OTHER"
        status = str(code) if isinstance(code, int) and 100 <= code <= 599 else "-"
        log.info(
            "http request method=%s path=%s status=%s bytes=%s",
            method, self._safe_request_path(), status, size,
        )

    def log_message(self, format: str, *args: Any) -> None:
        # Fallback for malformed requests and server errors. Never format the
        # parent class's arguments because they may contain a raw request line.
        del format, args
        log.warning("http event outcome=malformed_or_error")

    def _log_login(self, outcome: str, *, level: int = logging.INFO) -> None:
        log.log(level, "admin login outcome=%s ip=%s", outcome, self.client_address[0])

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

    def _mutation_guard(self) -> bool:
        """Reject form posts and cross-origin writes.

        The session cookie is SameSite=Strict; requiring JSON on top keeps a
        stray HTML form (and older browsers) from reaching the write endpoints.
        """
        content_type = (self.headers.get("Content-Type") or "").lower()
        if "application/json" not in content_type:
            self._send_json(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"error": "Content-Type must be application/json"},
            )
            return False
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != (self.headers.get("Host") or ""):
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "cross-origin request rejected"})
            return False
        return True

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
        now = time.time()
        with self._fail_lock:
            failures = self._failures.setdefault(self.client_address[0], [])
            count = sum(now - stamp < FAIL_WINDOW for stamp in failures)
            failures.append(now)
            # Emit once when entering the limited state, not on every 429.
            if count == FAIL_LIMIT - 1:
                self._log_login("rate_limited", level=logging.WARNING)

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

        if path == '/readyz':
            result = transport_health(self.config)
            self._send_json(HTTPStatus.OK if result['ready'] else HTTPStatus.SERVICE_UNAVAILABLE, result)
            return

        if path == '/api/video/credentials':
            if not self._api_guard():
                return
            try:
                self._send_json(HTTPStatus.OK, video_credentials_payload(self))
            except (ValueError, OSError):
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {'error': 'Video credential status is unavailable'})
            return
        if path == "/api/session":
            self._send_json(HTTPStatus.OK, {"authenticated": self._valid_session()})
            return
        if path == '/api/story' or path.startswith('/api/story/'):
            if not self._api_guard():
                return
            query = parse_qs(urlparse(self.path).query)
            try:
                offset = max(0, int((query.get('offset') or ['0'])[0]))
                if path == '/api/story':
                    result = story_data(db_paths(self.config)['profile'], self.config.get('story') or {}, offset=offset)
                else:
                    key = path.removeprefix('/api/story/')
                    kind = (query.get('kind') or ['run'])[0]
                    result = story_detail(db_paths(self.config)['profile'], key, kind=kind, offset=offset)
                self._send_json(HTTPStatus.OK, result)
            except (ValueError, OSError, sqlite3.Error):
                self._send_json(HTTPStatus.BAD_REQUEST, {'error': '剧情数据不可用或参数无效'})
            return
        if path == "/api/voice":
            if not self._api_guard():
                return
            try:
                self._send_json(HTTPStatus.OK, voice_data(self.config))
            except (ValueError, OSError):
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE, {"error": "音色配置不可用"}
                )
            return
        resources = {
            "/api/models": models_data,
            "/api/cards": cards_data,
            "/api/tasks": tasks_data,
            "/api/feeds": feeds_data,
            "/api/stickers": stickers_data,
            "/api/requests": approvals_data,
        }
        if path in resources:
            if not self._api_guard():
                return
            self._send_json(HTTPStatus.OK, resources[path](self.config))
            return
        if path.startswith("/api/stickers/"):
            if not self._api_guard():
                return
            raw_id, _, action = path.removeprefix("/api/stickers/").partition("/")
            try:
                sticker_id = (
                    int(raw_id) if raw_id.isascii() and raw_id.isdigit() else -1
                )
            except ValueError:
                sticker_id = -1
            if action != "image" or not 0 <= sticker_id < 2**63:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            query = parse_qs(urlparse(self.path).query)
            thumb = (query.get("thumb") or ["0"])[0].lower() not in ("", "0", "false")
            image = sticker_image(self.config, sticker_id, thumb=thumb)
            if image is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "表情文件不存在"})
                return
            body, content_type = image
            self._send_raw(
                HTTPStatus.OK,
                body,
                content_type,
                cache="private, max-age=31536000, immutable",
            )
            return
        if path.startswith("/api/cards/"):
            if not self._api_guard():
                return
            raw_id = path.removeprefix("/api/cards/")
            detail = card_detail_data(self.config, int(raw_id)) if raw_id.isdigit() else None
            if detail is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "角色卡不存在"})
            else:
                self._send_json(HTTPStatus.OK, detail)
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
                    HTTPStatus.OK,
                    fun_data(
                        self.config,
                        conversation,
                        (query.get("start") or [""])[0],
                        (query.get("end") or [""])[0],
                    ),
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
                self._log_login("password_not_configured", level=logging.DEBUG)
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "未设置 ADMIN_UI_PASSWORD"},
                )
                return
            if not hmac.compare_digest(submitted, expected):
                self._record_failure()
                self._log_login("invalid_password", level=logging.WARNING)
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "密码不对"})
                return
            cookie = self._issue()
            # This records session issuance, not receipt by the browser.
            self._log_login("success")
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

        if path in ('/api/video/credentials', '/api/video/credentials/delete'):
            video_credentials_post(self, path)
            return

        if path in ("/api/voice/active", "/api/voice/preview"):
            if not self._api_guard() or not self._mutation_guard():
                return
            payload = self._read_payload()
            try:
                if path == "/api/voice/active":
                    self._send_json(
                        HTTPStatus.OK, apply_voice_action(self.config, payload)
                    )
                else:
                    body = preview_audio(self.config, payload)
                    self._send_raw(HTTPStatus.OK, body, "audio/mpeg", cache="no-store")
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except OSError:
                log.exception("voice settings write failed")
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "音色设置写入失败"}
                )
            return

        if path == '/api/story/action':
            if not self._api_guard() or not self._mutation_guard():
                return
            try:
                length = int(self.headers.get('Content-Length') or 0)
                if not 0 < length <= 4096:
                    # Drain only a small, bounded excess so closing an unread
                    # request does not reset the connection before HTTP 413.
                    if 0 < length <= 65536:
                        self.rfile.read(length)
                    self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {'error': '请求须为 1–4096 字节，未截断保存'})
                    self.close_connection = True
                    return
                result = queue_story(db_paths(self.config)['profile'], self._read_payload(), self.config.get('story') or {})
                self._send_json(HTTPStatus.OK, result)
            except (ValueError, OSError, sqlite3.Error):
                self._send_json(HTTPStatus.BAD_REQUEST, {'error': '无效剧情操作，目标或版本可能已变化'})
            return
        if path == "/api/models/provider-models":
            if not self._api_guard() or not self._mutation_guard():
                return
            payload = self._read_payload()
            try:
                result = provider_models(
                    self.config, _string_field(payload, "provider")
                )
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except ProviderModelsError as exc:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
                return
            self._send_json(HTTPStatus.OK, result)
            return

        if path == "/api/requests/action":
            if not self._api_guard() or not self._mutation_guard():
                return
            try:
                result = queue_approval(self.config, self._read_payload())
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except (OSError, sqlite3.Error) as exc:
                log.exception("approval action failed")
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": f"写入失败: {exc}"},
                )
                return
            self._send_json(HTTPStatus.OK, result)
            return

        if path == "/api/stickers/delete":
            if not self._api_guard() or not self._mutation_guard():
                return
            try:
                result = delete_sticker(self.config, self._read_payload())
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except (OSError, sqlite3.Error) as exc:
                log.exception("sticker delete failed")
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": f"删除失败: {exc}"},
                )
                return
            self._send_json(HTTPStatus.OK, result)
            return

        model_actions = {
            "/api/models/providers": "provider.upsert",
            "/api/models/providers/delete": "provider.delete",
            "/api/models/items": "model.upsert",
            "/api/models/items/delete": "model.delete",
            "/api/models/active": "model.activate",
        }
        if path in model_actions:
            if not self._api_guard() or not self._mutation_guard():
                return
            try:
                result = apply_models_action(
                    self.config, model_actions[path], self._read_payload()
                )
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except (OSError, sqlite3.Error) as exc:
                log.exception("model mutation failed")
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": f"写入失败: {exc}"},
                )
                return
            self._send_json(HTTPStatus.OK, result)
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
