"""QQ chat bot: NapCat (OneBot 11) + LLM + Character Card V3."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import random
import re
import signal
import sqlite3
import sys
import time
import tomllib
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from admins import parse_admin_ids
from card_fetch import CardFetchError, fetch_card_bytes_sync
from commands import CommandRouter, strip_leading_mention, strip_message_markers
from conversations import ConversationStore
from dsh_client import DSHClient, DSHConfig, DSHError
from feeds import (
    FeedPoller,
    FeedStore,
    bilibili_feed_url,
    feed_title,
    fetch_feed,
    format_entry,
    parse_feed,
)
from group_context import GroupContextBuffer, format_group_turn, format_proactive_turn
from images import ImageStore, StoredImage
from llm import LLMClient, LLMConfig, LLMError
from meme import (
    MemeError,
    extract_meme_marker,
    render_meme,
)
from meme import (
    available as meme_available,
)
from memory import Memory, format_recall
from models import EDITABLE, ModelRecord, ModelRegistry, parse_key
from onebot import (
    EMPTY_REPLY,
    MessageEvent,
    OneBotConfig,
    OneBotError,
    OneBotWSClient,
)
from proactive import DEFAULT_PROMPT, ProactivePolicy, ProactiveSettings
from prompt import build_messages, build_system_prompt
from schedule import extract_schedule, render_schedule
from sessions import SessionStore
from stickers import (
    StickerError,
    StickerStore,
    download_image,
    extract_sticker_marker,
    parse_tags,
    preview_jpeg,
)
from tasks import (
    TZ as TASK_TZ,
)
from tasks import (
    TaskScheduler,
    TaskStore,
    describe_record,
    describe_target,
    extract_task_management,
    extract_task_marker,
    next_run_for,
    parse_task,
    render_task_list,
    resolve_target,
    spec_of,
)
from usage import UsageStore
from voice import (
    VoiceError,
    extract_voice_marker,
    spoken_text,
    synthesize,
    synthesize_fish,
    synthesize_stepfun,
)

from cards import CardError, CardRecord, CardRegistry, CharacterCard

log = logging.getLogger("qqbot")
TZ = ZoneInfo("Asia/Shanghai")

# Model selection and parameters live in the registry; these env vars are ignored.
LEGACY_MODEL_ENV = (
    "LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL",
    "DSH_PROVIDER", "DSH_MODEL", "DSH_MAX_TOKENS",
)


@dataclass
class OneBotSection(OneBotConfig):
    """TOML section; the same shape OneBotWSClient consumes."""


@dataclass
class BotSection:
    reply_private: bool = True
    reply_group_at: bool = False
    history_turns: int = 24
    max_history_chars: int = 12000
    max_reply_chars: int = 1200
    max_input_chars: int = 4000
    lore_max_chars: int = 4000
    greeting_on_first_chat: bool = False
    notify_errors: bool = True
    error_message: str = "（我这边网络或模型出了点问题，等一下再试）"
    # Non-@ group messages kept per group and handed to the model on the next @.
    group_context_messages: int = 30
    group_context_chars: int = 3000
    # Recent group images attached to the next reply (the model cannot see them
    # once their message has scrolled into the text-only context window).
    group_context_images: int = 2
    # Privileged QQ numbers (ADMIN_QQ_IDS, comma/space separated).
    admin_qq_ids: tuple[int, ...] = ()
    # Heuristic proactive group replies (see [bot.proactive]).
    proactive: ProactiveSettings = field(default_factory=ProactiveSettings)


@dataclass
class PathsSection:
    character: str = "/etc/qqbot/character.json"
    database: str = "/var/lib/qqbot/memory.db"
    # Shared with the DSH user-profile plugin (USER_PROFILE_DB).
    profile_db: str = "/var/lib/qqbot/dsh-state/profile.db"


@dataclass
class LLMSection:
    # Fallback thin client only (DSH disabled). Model, endpoint and limits come
    # from the registry; these are transport/sampling knobs DSH does not expose.
    temperature: float = 1.0
    timeout: float = 60.0


@dataclass
class DSHSection:
    enabled: bool = True
    home: str = "/var/lib/qqbot/dsh-home"
    workspace: str = "/var/lib/qqbot/dsh-workspace"
    profile: str = "qqbot"
    request_timeout: float = 120.0
    # "placeholder" saves originals under workspace and sends a text
    # description/path; "inline" keeps the old ACP image-block behavior.
    image_mode: str = "placeholder"
    image_dir: str = "images"
    image_caption: bool = True
    caption_prompt: str = (
        "用一两句中文客观描述这张图片；其中有文字就照抄关键文字。只输出描述。"
    )
    image_max_bytes: int = 8 * 1024 * 1024
    image_keep_days: int = 14
    image_max_files: int = 1000
    # DSH session rotation. Generation 0 keeps the historical session id;
    # later generations append -gN. Old JSONL files are never deleted.
    session_rotate_idle_hours: float = 24.0
    session_rotate_idle_hours_private: float = 168.0
    session_rotate_max_hours: float = 0.0
    session_rotate_max_messages: int = 0
    session_rotate_daily_hour: int = 4


@dataclass(frozen=True)
class PreparedImage:
    """A stored image plus the short description shown to DSH."""

    sha1: str
    relative: str
    mime: str
    description: str
    data_uri: str = ""


@dataclass
class ModelsSection:
    # SQLite registry keyed by (provider, model_id): the only model configuration.
    database: str = "/var/lib/qqbot/models.db"
    patch: str = "/var/lib/qqbot/dsh-state/models.patch.json"


@dataclass
class CardsSection:
    # SQLite character-card library; the active row is loaded at runtime.
    database: str = "/var/lib/qqbot/characters.db"
    # Legacy single-card file: imported when the library is empty. While this
    # card stays active its mtime is watched, so editing it still hot-reloads.
    bootstrap: str = "/etc/qqbot/character.json"


@dataclass
class StickersSection:
    # Learned 表情包 library. Only images QQ marks as stickers are collected,
    # and only from group chats.
    database: str = "/var/lib/qqbot/stickers.db"
    directory: str = "/var/lib/qqbot/stickers"
    collect: bool = True
    max_per_hour: int = 20  # per group
    max_bytes: int = 8 * 1024 * 1024
    # Oldest/least-used stickers are dropped beyond this many.
    max_stored: int = 500
    # Sending: the model asks with a trailing "[表情: ...]" line.
    send: bool = True
    send_cooldown_seconds: int = 60
    max_sends_per_hour: int = 10


@dataclass
class FeedsSection:
    # RSS/Atom subscriptions (Bilibili via a configurable RSSHub mirror).
    enabled: bool = True
    database: str = "/var/lib/qqbot/feeds.db"
    rsshub_base: str = "https://rsshub.liumingye.cn"
    check_seconds: int = 300
    max_new: int = 5
    max_subs: int = 50


@dataclass
class MemeSection:
    # Text memes rendered onto a sticker (or a gradient card).
    enabled: bool = True
    max_chars: int = 24
    cooldown_seconds: int = 30
    directory: str = "/var/lib/qqbot/memes"


@dataclass
class VoiceSection:
    # TTS voice replies; the model asks with a trailing "[语音]".
    # provider=edge uses `voice` as an edge-tts name; provider=fish uses it as
    # the Fish Audio reference_id (see /model on api.fish.audio).
    enabled: bool = True
    provider: str = "edge"
    voice: str = "zh-CN-XiaoxiaoNeural"
    rate: str = "+0%"
    # fish (s2.1-pro-free) and stepfun (stepaudio-2.5-tts) read `voice` as
    # their own reference id / voice id.
    model: str = "stepaudio-3-tts"
    api_key_env: str = "STEPFUN_API_KEY"
    base_url: str = "https://api.stepfun.com"
    instruction: str = ""
    speed: float = 1.0
    # Chance to voice a short proactive reply the model did not mark [语音].
    probability: float = 0.4
    # 0 = no extra cap (provider limit is 1000); >0 limits the spoken text.
    max_chars: int = 0
    cooldown_seconds: int = 30


@dataclass
class TasksSection:
    # Admin-created scheduled messages; semantic parsing uses the thin client.
    database: str = "/var/lib/qqbot/tasks.db"
    enabled: bool = True
    check_seconds: int = 20
    max_tasks: int = 50
    max_per_chat: int = 10


@dataclass
class UsageSection:
    # Token ledger + per-message analytics used by the admin UI.
    database: str = "/var/lib/qqbot/usage.db"
    retention_days: int = 90
    event_retention_days: int = 90


@dataclass
class AppConfig:
    onebot: OneBotSection = field(default_factory=OneBotSection)
    llm: LLMSection = field(default_factory=LLMSection)
    dsh: DSHSection = field(default_factory=DSHSection)
    usage: UsageSection = field(default_factory=UsageSection)
    models: ModelsSection = field(default_factory=ModelsSection)
    cards: CardsSection = field(default_factory=CardsSection)
    stickers: StickersSection = field(default_factory=StickersSection)
    tasks: TasksSection = field(default_factory=TasksSection)
    voice: VoiceSection = field(default_factory=VoiceSection)
    meme: MemeSection = field(default_factory=MemeSection)
    feeds: FeedsSection = field(default_factory=FeedsSection)
    bot: BotSection = field(default_factory=BotSection)
    paths: PathsSection = field(default_factory=PathsSection)


def _section(table: dict, name: str) -> dict[str, Any]:
    value = table.get(name)
    return value if isinstance(value, dict) else {}


def load_env_file(path: str = "/etc/qqbot/bot.env") -> None:
    """Load KEY=VALUE pairs without overriding variables systemd already set."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config(path: str | Path) -> AppConfig:
    with open(path, "rb") as fh:
        table = tomllib.load(fh)

    onebot_raw = _section(table, "onebot")
    llm_raw = _section(table, "llm")
    dsh_raw = _section(table, "dsh")
    bot_raw = _section(table, "bot")
    paths_raw = _section(table, "paths")

    token = os.environ.get("ONEBOT_TOKEN", "").strip() or str(onebot_raw.get("token", "")).strip()

    def env_bool(name: str, default: bool) -> bool:
        value = os.environ.get(name)
        if value is None or not value.strip():
            return default
        return value.strip().lower() in ("1", "true", "yes", "on")

    image_mode = str(dsh_raw.get("image_mode", "placeholder")).strip().lower()
    if image_mode not in ("placeholder", "inline"):
        image_mode = "placeholder"
    dsh = DSHSection(
        enabled=env_bool("DSH_ENABLED", bool(dsh_raw.get("enabled", True))),
        home=os.environ.get("DSH_HOME", "").strip() or str(dsh_raw.get("home", "/var/lib/qqbot/dsh-home")),
        workspace=os.environ.get("DSH_WORKSPACE", "").strip() or str(dsh_raw.get("workspace", "/var/lib/qqbot/dsh-workspace")),
        profile=os.environ.get("DSH_PROFILE", "").strip() or str(dsh_raw.get("profile", "qqbot")),
        request_timeout=float(dsh_raw.get("request_timeout", 120.0)),
        image_mode=image_mode,
        image_dir=str(dsh_raw.get("image_dir", "images")).strip().strip("/") or "images",
        image_caption=bool(dsh_raw.get("image_caption", True)),
        caption_prompt=str(dsh_raw.get("caption_prompt") or DSHSection.caption_prompt),
        image_max_bytes=max(1024, int(dsh_raw.get("image_max_bytes", 8 * 1024 * 1024))),
        image_keep_days=max(0, int(dsh_raw.get("image_keep_days", 14))),
        image_max_files=max(0, int(dsh_raw.get("image_max_files", 1000))),
        session_rotate_idle_hours=max(0.0, float(dsh_raw.get("session_rotate_idle_hours", 24.0))),
        session_rotate_idle_hours_private=max(0.0, float(dsh_raw.get("session_rotate_idle_hours_private", 168.0))),
        session_rotate_max_hours=max(0.0, float(dsh_raw.get("session_rotate_max_hours", 0.0))),
        session_rotate_max_messages=max(0, int(dsh_raw.get("session_rotate_max_messages", 0))),
        session_rotate_daily_hour=int(dsh_raw.get("session_rotate_daily_hour", 4)),
    )

    admins_raw = os.environ.get("ADMIN_QQ_IDS", "").strip()
    if admins_raw:
        admin_qq_ids = parse_admin_ids(admins_raw)
    else:
        configured = bot_raw.get("admins", [])
        admin_qq_ids = tuple(
            int(value) for value in configured if str(value).strip().isdigit()
        ) if isinstance(configured, list) else ()

    proactive_raw = _section(bot_raw, "proactive")
    quiet_raw = proactive_raw.get("quiet_hours", [1, 7])
    quiet_hours = tuple(int(h) for h in quiet_raw) if isinstance(quiet_raw, list) else (1, 7)
    if len(quiet_hours) != 2:
        quiet_hours = (1, 7)
    proactive = ProactiveSettings(
        enabled=bool(proactive_raw.get("enabled", False)),
        groups=tuple(int(g) for g in proactive_raw.get("groups", []) if str(g).isdigit()),
        probability=min(1.0, max(0.0, float(proactive_raw.get("probability", 0.04)))),
        cooldown_seconds=int(proactive_raw.get("cooldown_seconds", 600)),
        max_per_hour=int(proactive_raw.get("max_per_hour", 4)),
        max_per_day=int(proactive_raw.get("max_per_day", 20)),
        min_context_messages=int(proactive_raw.get("min_context_messages", 2)),
        keywords=tuple(str(v) for v in proactive_raw.get("keywords", []) if str(v).strip()),
        aliases=tuple(str(v) for v in proactive_raw.get("aliases", []) if str(v).strip()),
        quiet_hours=quiet_hours,
        followup_seconds=max(0, int(proactive_raw.get("followup_seconds", 300))),
        followup_multiplier=max(1.0, float(proactive_raw.get("followup_multiplier", 2.0))),
        prompt=str(proactive_raw.get("prompt") or DEFAULT_PROMPT),
    )

    cards_raw = _section(table, "cards")
    cards = CardsSection(
        database=(
            os.environ.get("CARD_DB", "").strip()
            or str(cards_raw.get("database", "/var/lib/qqbot/characters.db"))
        ),
        bootstrap=(
            os.environ.get("CARD_BOOTSTRAP", "").strip()
            or str(
                cards_raw.get("bootstrap")
                or paths_raw.get("character", "/etc/qqbot/character.json")
            )
        ),
    )

    stickers_raw = _section(table, "stickers")
    stickers = StickersSection(
        database=(
            os.environ.get("STICKER_DB", "").strip()
            or str(stickers_raw.get("database", "/var/lib/qqbot/stickers.db"))
        ),
        directory=(
            os.environ.get("STICKER_DIR", "").strip()
            or str(stickers_raw.get("directory", "/var/lib/qqbot/stickers"))
        ),
        collect=bool(stickers_raw.get("collect", True)),
        max_per_hour=max(0, int(stickers_raw.get("max_per_hour", 20))),
        max_bytes=max(1024, int(stickers_raw.get("max_bytes", 8 * 1024 * 1024))),
        max_stored=max(0, int(stickers_raw.get("max_stored", 500))),
        send=bool(stickers_raw.get("send", True)),
        send_cooldown_seconds=max(0, int(stickers_raw.get("send_cooldown_seconds", 60))),
        max_sends_per_hour=max(0, int(stickers_raw.get("max_sends_per_hour", 10))),
    )

    feeds_raw = _section(table, "feeds")
    feeds = FeedsSection(
        enabled=bool(feeds_raw.get("enabled", True)),
        database=str(feeds_raw.get("database", "/var/lib/qqbot/feeds.db")),
        rsshub_base=str(feeds_raw.get("rsshub_base", "https://rsshub.liumingye.cn")),
        check_seconds=max(60, int(feeds_raw.get("check_seconds", 300))),
        max_new=max(1, int(feeds_raw.get("max_new", 5))),
        max_subs=max(0, int(feeds_raw.get("max_subs", 50))),
    )

    meme_raw = _section(table, "meme")
    meme = MemeSection(
        enabled=bool(meme_raw.get("enabled", True)),
        max_chars=max(4, int(meme_raw.get("max_chars", 24))),
        cooldown_seconds=max(0, int(meme_raw.get("cooldown_seconds", 30))),
        directory=str(meme_raw.get("directory", "/var/lib/qqbot/memes")),
    )

    voice_raw = _section(table, "voice")
    voice = VoiceSection(
        enabled=bool(voice_raw.get("enabled", True)),
        provider=str(voice_raw.get("provider", "edge")).lower(),
        voice=str(voice_raw.get("voice", "zh-CN-XiaoxiaoNeural")),
        rate=str(voice_raw.get("rate", "+0%")),
        model=str(voice_raw.get("model", "stepaudio-3-tts")),
        api_key_env=str(voice_raw.get("api_key_env", "STEPFUN_API_KEY")),
        base_url=str(voice_raw.get("base_url", "https://api.stepfun.com")),
        instruction=str(voice_raw.get("instruction", "")),
        speed=float(voice_raw.get("speed", 1.0)),
        probability=max(0.0, min(1.0, float(voice_raw.get("probability", 0.4)))),
        max_chars=max(0, int(voice_raw.get("max_chars", 0))),
        cooldown_seconds=max(0, int(voice_raw.get("cooldown_seconds", 30))),
    )

    tasks_raw = _section(table, "tasks")
    tasks = TasksSection(
        database=(
            os.environ.get("TASK_DB", "").strip()
            or str(tasks_raw.get("database", "/var/lib/qqbot/tasks.db"))
        ),
        enabled=bool(tasks_raw.get("enabled", True)),
        check_seconds=max(5, int(tasks_raw.get("check_seconds", 20))),
        max_tasks=max(0, int(tasks_raw.get("max_tasks", 50))),
        max_per_chat=max(0, int(tasks_raw.get("max_per_chat", 10))),
    )

    usage_raw = _section(table, "usage")
    usage = UsageSection(
        database=(
            os.environ.get("USAGE_DB", "").strip()
            or str(usage_raw.get("database", "/var/lib/qqbot/usage.db"))
        ),
        retention_days=max(0, int(usage_raw.get("retention_days", 90))),
        event_retention_days=max(0, int(usage_raw.get("event_retention_days", 90))),
    )

    models_raw = _section(table, "models")
    models = ModelsSection(
        database=os.environ.get("MODEL_DB", "").strip() or str(models_raw.get("database", "/var/lib/qqbot/models.db")),
        patch=os.environ.get("MODEL_PATCH", "").strip() or str(models_raw.get("patch", "/var/lib/qqbot/dsh-state/models.patch.json")),
    )

    return AppConfig(
        onebot=OneBotSection(
            url=str(onebot_raw.get("url", "ws://127.0.0.1:3001")),
            token=token,
            self_id=int(onebot_raw.get("self_id", 0) or 0),
        ),
        llm=LLMSection(
            temperature=float(llm_raw.get("temperature", 1.0)),
            timeout=float(llm_raw.get("timeout", 60.0)),
        ),
        dsh=dsh,
        usage=usage,
        models=models,
        cards=cards,
        stickers=stickers,
        tasks=tasks,
        voice=voice,
        meme=meme,
        feeds=feeds,
        bot=BotSection(
            reply_private=bool(bot_raw.get("reply_private", True)),
            reply_group_at=bool(bot_raw.get("reply_group_at", False)),
            history_turns=int(bot_raw.get("history_turns", 24)),
            max_history_chars=int(bot_raw.get("max_history_chars", 12000)),
            max_reply_chars=int(bot_raw.get("max_reply_chars", 1200)),
            max_input_chars=int(bot_raw.get("max_input_chars", 4000)),
            lore_max_chars=int(bot_raw.get("lore_max_chars", 4000)),
            greeting_on_first_chat=bool(bot_raw.get("greeting_on_first_chat", False)),
            notify_errors=bool(bot_raw.get("notify_errors", True)),
            error_message=str(bot_raw.get("error_message", "（我这边网络或模型出了点问题，等一下再试）")),
            group_context_messages=int(bot_raw.get("group_context_messages", 30)),
            group_context_chars=int(bot_raw.get("group_context_chars", 3000)),
            group_context_images=max(
                0, min(4, int(bot_raw.get("group_context_images", 2)))
            ),
            admin_qq_ids=admin_qq_ids,
            proactive=proactive,
        ),
        paths=PathsSection(
            character=str(paths_raw.get("character", "/etc/qqbot/character.json")),
            database=str(paths_raw.get("database", "/var/lib/qqbot/memory.db")),
            profile_db=(
                os.environ.get("USER_PROFILE_DB", "").strip()
                or str(paths_raw.get("profile_db", "/var/lib/qqbot/dsh-state/profile.db"))
            ),
        ),
    )


def warn_legacy_model_env() -> None:
    stale = [name for name in LEGACY_MODEL_ENV if os.environ.get(name, "").strip()]
    if stale:
        log.warning(
            "ignoring %s: model selection and parameters come from the model registry "
            "(use --use-model / --set-model)", ", ".join(stale),
        )


def load_active_model(config: AppConfig, write_patch: bool = True) -> ModelRecord:
    """Return the active registry row and, for DSH, render the route patch."""
    registry = ModelRegistry(config.models.database)
    try:
        record = registry.active()
        if record is None:
            raise ValueError(
                f"no active model in {config.models.database}; run --use-model provider/model_id"
            )
        if write_patch:
            registry.write_dsh_patch(config.models.patch)
    finally:
        registry.close()
    return record


def model_api_key(record: ModelRecord) -> str:
    """The provider key saved in the registry, else its environment fallback."""
    return (record.api_key or "").strip() or os.environ.get(record.api_key_env, "").strip()


def model_signature(config: AppConfig) -> tuple | None:
    """Current active route; cheap enough to read once per turn."""
    registry = ModelRegistry(config.models.database)
    try:
        return registry.active_signature()
    finally:
        registry.close()


def make_dsh_client(config: AppConfig, record: ModelRecord, system_prompt: str = "") -> DSHClient:
    log.info(
        "model %s: context=%d max_output=%d effort=%s",
        record.key, record.context_window, record.max_output_tokens,
        record.effective_effort or "(model default)",
    )
    return DSHClient(
        DSHConfig(
            home=config.dsh.home,
            workspace=config.dsh.workspace,
            profile=config.dsh.profile,
            provider=record.provider,
            model=record.model_id,
            max_tokens=record.max_output_tokens,
            reasoning_effort=record.effective_effort,
            request_timeout=config.dsh.request_timeout,
            patches=(config.models.patch,),
        ),
        system_prompt=system_prompt,
    )


def make_llm_client(config: AppConfig, record: ModelRecord) -> LLMClient:
    return LLMClient(LLMConfig(
        api_key=model_api_key(record),
        base_url=record.base_url,
        model=record.model_id,
        provider=record.provider,
        max_tokens=record.max_output_tokens,
        temperature=config.llm.temperature,
        timeout=config.llm.timeout,
        reasoning_effort=record.effective_effort,
    ))


def build_dsh_system_prompt(card) -> str:
    prompt = build_system_prompt(card.data, card.name, "用户", datetime.now(TZ), [])
    return (
        prompt
        + "\n\n[设定资料]\n角色设定资料保存在 memory/lore.md；需要时用 grep/read 查阅。"
        + "\n\n[上下文说明]\n每一轮消息开头会给出当前时间、会话和发言者；时间和说话人以那一行为准。"
        + "\n\n[联网与赛程]\n有时效性的问题（赛事赛程、新闻、版本改动等）先用 web_search；需要网页正文再用 web_fetch。回答赛程/日程时不要写散文，改为只输出一个 ```schedule 代码块，内容是 JSON："
          '{"event":"赛事名","matches":[{"stage":"瑞士轮","start_time":"2026-10-24T01:00:00+08:00","team_a":"GEN","team_b":"IG","best_of":1}]}'
          "。start_time 用 ISO 8601 带时区（北京时间用 +08:00），best_of 可省略；不要附来源链接，代码块外最多一句话。其他话题不受此限制。"
    )


def write_lore(card, workspace: str) -> None:
    path = Path(workspace) / "memory" / "lore.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {card.name} 的设定资料", ""]
    for entry in card.lorebook.entries:
        if not entry.enabled or not entry.content.strip():
            continue
        keys = " / ".join(entry.keys) or "常驻"
        lines.append(f"## {keys}\n\n{entry.content.strip()}\n")
    if len(lines) == 2:
        lines.append("（这张卡没有世界书条目。）")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(path)


def _shrink_image(data: bytes, mime: str) -> tuple[bytes, str]:
    """Downscale large chat images before they go to the model.

    Every image also stays in the DSH session history, so sending 1-8 MB
    originals makes later requests proportionally slower. Re-encoding to WebP
    at 1024px cuts that by an order of magnitude with no useful detail loss.
    """
    if len(data) <= 250 * 1024:
        return data, mime
    try:
        from io import BytesIO

        from PIL import Image, ImageOps

        with Image.open(BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image)
            if max(image.size) > 1024:
                image.thumbnail((1024, 1024))
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGB")
            output = BytesIO()
            image.save(output, format="WEBP", quality=80)
            return output.getvalue(), "image/webp"
    except Exception as exc:  # noqa: BLE001 - keep the original on any failure
        log.warning("image shrink failed: %s", exc)
        return data, mime


def split_text(text: str, limit: int) -> list[str]:
    text = text.strip()
    if limit <= 0 or len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        window = text[:limit]
        cut = -1
        for sep in ("\n", "。", "！", "？", "；", ".", "!", "?", ";", " "):
            cut = max(cut, window.rfind(sep))
        if cut < limit // 2:
            cut = limit
        else:
            cut += 1
        chunk = text[:cut].strip()
        if chunk:
            chunks.append(chunk)
        text = text[cut:].strip()
    return chunks


class Bot:
    def __init__(
        self,
        config: AppConfig,
        cards: CardRegistry,
        memory: Memory,
        client: LLMClient,
        dsh: DSHClient | None = None,
        conversations: ConversationStore | None = None,
        stickers: StickerStore | None = None,
        tasks: TaskStore | None = None,
        feeds: FeedStore | None = None,
        usage: UsageStore | None = None,
    ):
        self.config = config
        self.cards = cards
        self.stickers = stickers
        self.tasks = tasks
        self.feeds = feeds
        self.memory = memory
        self.client = client
        self.dsh = dsh
        self.image_store: ImageStore | None = None
        self._usage = usage
        self._usage_path = config.usage.database
        self._usage_retention = config.usage.retention_days
        self.client.usage_sink = self._record_client_usage
        self.sessions = SessionStore(
            config.paths.database,
            idle_hours=config.dsh.session_rotate_idle_hours,
            idle_hours_private=config.dsh.session_rotate_idle_hours_private,
            max_hours=config.dsh.session_rotate_max_hours,
            max_messages=config.dsh.session_rotate_max_messages,
            daily_hour=config.dsh.session_rotate_daily_hour,
        )
        self._seed_sessions()
        if (
            config.dsh.image_mode == "placeholder"
            and os.path.abspath(config.dsh.workspace) != os.path.abspath("/var/lib/qqbot/dsh-workspace")
        ):
            log.warning(
                "dsh.workspace=%s does not match the sandbox workspaceRoot "
                "/var/lib/qqbot/dsh-workspace; placeholder image paths may be unreadable",
                config.dsh.workspace,
            )
        self.conversations = conversations
        self.group_context = GroupContextBuffer(config.bot.group_context_messages)
        self.onebot = OneBotWSClient(config.onebot, self.handle, self.handle_notice)
        self.commands = CommandRouter(self)
        self.proactive = ProactivePolicy(config.bot.proactive)
        self._closed = False
        self._locks: dict[str, asyncio.Lock] = {}
        self._GLOBAL_LIMIT = 3
        self._global = asyncio.Semaphore(self._GLOBAL_LIMIT)
        self._card: CharacterCard | None = None
        self._card_sig: tuple | None = None
        self._card_lock = asyncio.Lock()
        # Recent message ids the bot sent per group, so quote-replies count.
        self._own_messages: dict[int, deque[int]] = {}
        # Names from the active card, used to notice "大肥鱼，..." without an @.
        self._card_names: tuple[str, ...] = ()
        # Recent display names by QQ id, so notices (which carry no name) can
        # still say who poked whom.
        self._names: dict[int, str] = {}
        # Poke replies are rate limited per chat.
        self._poke_last: dict[int, float] = {}
        self._poke_cooldown = 8.0
        # Voice replies are rate limited per chat as well.
        self._voice_last: dict[int, float] = {}
        self._meme_last: dict[int, float] = {}
        self._system_prompt = ""
        self._model_lock = asyncio.Lock()
        self._model_sig = model_signature(config)
        self._scheduler: TaskScheduler | None = None
        self._feed_poller: FeedPoller | None = None
        # Sticker collection runs off the message path, one task per message.
        self._tasks: set[asyncio.Task] = set()
        self._sticker_budget: dict[int, deque[float]] = {}
        self._sticker_sends: dict[int, deque[float]] = {}
        self._sticker_last: dict[int, float] = {}

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.config.bot.admin_qq_ids

    @property
    def model_key(self) -> str | None:
        return self._model_sig[0] if self._model_sig else None

    async def send_text(self, event: MessageEvent, text: str) -> None:
        await self._send(event, text)

    async def reload_model(self) -> None:
        await self._maybe_reload_model()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        if self._scheduler is not None:
            await self._scheduler.stop()
        if self._feed_poller is not None:
            await self._feed_poller.stop()
        await self.onebot.close()
        if self.dsh is not None:
            self.dsh.close()
        if self.conversations is not None:
            self.conversations.close()
        await self.client.close()
        if self._usage is not None:
            self._usage.close()
        self.memory.close()
        self.sessions.close()
        self.cards.close()
        if self.stickers is not None:
            self.stickers.close()
        if self.tasks is not None:
            self.tasks.close()
        if self.feeds is not None:
            self.feeds.close()

    async def handle(self, event: MessageEvent) -> None:
        if event.sender_name:
            self._names[event.user_id] = event.sender_name
        text = event.text[: self.config.bot.max_input_chars]
        if not text and not event.images:
            return

        self._log_chat_event(event, text)
        command_text = strip_leading_mention(strip_message_markers(text))
        if self.commands.is_command(command_text):
            await self.commands.handle(event, command_text)
            return

        if event.message_type == "group":
            if event.group_id is None:
                return
            if (
                event.text == EMPTY_REPLY
                and event.reply_to is not None
                and event.reply_to not in self._own_messages.get(event.group_id, ())
            ):
                # A quote with no text only addresses the bot when it quotes
                # the bot; otherwise it is background chatter.
                return
            if event.stickers:
                self._spawn(self._collect_stickers(event))
            conversation_id = f"group:{event.group_id}"
            # The card (and therefore its names) may have been switched by
            # /card use or --use-card since the last message.
            await self.ensure_card()
            addressed = event.at_me or self._addresses_bot(event, text)
            if not self.config.bot.reply_group_at or not addressed:
                self.group_context.add(
                    event.group_id,
                    event.sender_name,
                    text,
                    images=event.images,
                    user_id=event.user_id,
                )
                if not addressed:
                    await self._maybe_reply_proactively(conversation_id, event, text)
                return
        elif event.message_type == "private":
            if not self.config.bot.reply_private:
                return
            conversation_id = f"private:{event.user_id}"
        else:
            return

        await self._maybe_reload_model()
        lock = self._locks.setdefault(conversation_id, asyncio.Lock())
        async with lock, self._global:
            await self._reply(conversation_id, event, text)

    async def handle_notice(self, payload: dict) -> None:
        """Turn poke notices into context lines or a direct reply."""
        poke = self.onebot.parse_notice(payload)
        if poke is None or poke.user_id == self.config.onebot.self_id:
            return
        name = self._names.get(poke.user_id) or str(poke.user_id)
        target = self._names.get(poke.target_id) or str(poke.target_id)

        if poke.target_id != self.config.onebot.self_id:
            if poke.group_id is not None:
                self.group_context.add(
                    poke.group_id,
                    name,
                    f"[戳了戳 {target}]",
                    user_id=poke.user_id,
                )
            return

        key = poke.group_id if poke.group_id is not None else poke.user_id
        now = time.time()
        if now - self._poke_last.get(key, 0.0) < self._poke_cooldown:
            return
        self._poke_last[key] = now
        await self.handle(
            MessageEvent(
                message_type="group" if poke.group_id is not None else "private",
                user_id=poke.user_id,
                message_id=0,
                text="[有人戳了戳你]",
                at_me=poke.group_id is not None,
                sender_name=name,
                group_id=poke.group_id,
            )
        )

    def _addresses_bot(self, event: MessageEvent, text: str) -> bool:
        """Name mentions and quote-replies to the bot count as being addressed.

        Only meaningful while group replies are enabled; with reply_group_at
        off the bot only speaks proactively.
        """
        if not self.config.bot.reply_group_at:
            return False
        if self.proactive.named(text, self._card_names):
            return True
        if event.reply_to is not None and event.group_id is not None:
            return event.reply_to in self._own_messages.get(event.group_id, ())
        return False

    # ------------------------------------------------------------- tasks

    @staticmethod
    def _append_note(reply: str, note: str) -> str:
        return f"{reply}" + ("" if not reply else chr(10)) + note

    async def _handle_task_management(
        self, event: MessageEvent, reply: str, action: str, argument: str
    ) -> str:
        """List/delete/pause/resume tasks in the current chat, admins only."""
        if not self.is_admin(event.user_id):
            return reply
        store = self.tasks
        if store is None or not self.config.tasks.enabled:
            return reply
        chat_type, chat_id = resolve_target(
            "here", event.message_type, event.user_id, event.group_id
        )
        rows = [
            row
            for row in store.list(limit=200)
            if row.chat_type == chat_type and row.chat_id == chat_id
        ]
        if action == "列表":
            return self._append_note(reply, render_task_list(rows))

        record = None
        if argument.isdigit():
            record = next((row for row in rows if row.id == int(argument)), None)
        elif argument:
            matches = [
                row for row in rows if argument in row.instruction or argument in row.message
            ]
            if len(matches) == 1:
                record = matches[0]
            elif len(matches) > 1:
                listing = chr(10).join(
                    f"#{row.id} {describe_record(row)} {row.instruction}" for row in matches[:5]
                )
                return self._append_note(reply, f"有多个匹配，请用编号：{chr(10)}{listing}")
        if record is None:
            return self._append_note(reply, f"没找到匹配的任务：{argument or '（空）'}")

        if action == "删除":
            store.delete(record.id)
            note = f"已删除任务 #{record.id}（{describe_record(record)}）"
        elif action == "暂停":
            store.set_enabled(record.id, False, None)
            note = f"已暂停任务 #{record.id}"
        else:  # 恢复
            try:
                next_run = next_run_for(spec_of(record), datetime.now(TASK_TZ))
            except ValueError as exc:
                return self._append_note(reply, f"恢复失败：{exc}")
            store.set_enabled(record.id, True, next_run)
            note = (
                f"已恢复任务 #{record.id}，下次 "
                f"{next_run.strftime('%m-%d %H:%M')}"
            )
        return self._append_note(reply, note)

    async def _handle_task_request(
        self, event: MessageEvent, reply: str, description: str
    ) -> str:
        """Create a task the model asked for; non-admins are silently ignored."""
        if not self.is_admin(event.user_id):
            log.info("ignoring task request from non-admin %s", event.user_id)
            return reply
        try:
            record, next_run = await self.create_task(event, description)
        except (ValueError, LLMError) as exc:
            log.warning("task request failed: %s", exc)
            note = f"（定时任务创建失败：{exc}）"
            return f"{reply}\n{note}" if reply else note
        line = (
            f"（已创建任务 #{record.id}：{describe_record(record)}，"
            f"发送到{describe_target(record)}，"
            f"下次 {next_run.strftime('%m-%d %H:%M')}，内容：{record.message}）"
        )
        return f"{reply}\n{line}" if reply else line

    def _meme_hint(self) -> str:
        if not self.config.meme.enabled or not meme_available():
            return ""
        return (
            chr(10) + "（想发梗图时，在回复最后单独一行写 "
            f"[梗图: 不超过{self.config.meme.max_chars}字的文字]，会挑一张表情包当底图。）"
        )

    def _voice_hint(self) -> str:
        if not self.config.voice.enabled:
            return ""
        limit = (
            f"控制在 {self.config.voice.max_chars} 字以内"
            if self.config.voice.max_chars > 0
            else "不要长篇大论（上限 1000 字）"
        )
        return (
            chr(10) + "（对方明确要求发语音/听语音/用语音说时，你必须使用 [语音]："
            "在回复最后单独一行写 [语音]，把要说的正文" + limit +
            "；括号里的动作描写不会被读出来。其他时候想用语音也可以用。）"
        )

    def _task_hint(self, event: MessageEvent) -> str:
        if (
            self.tasks is None
            or not self.config.tasks.enabled
            or not self.is_admin(event.user_id)
        ):
            return ""
        return (
            "\n" + "（对方是管理员：想让我定时提醒/定时发消息时，在回复最后单独一行写 "
            "[定时: 原始描述]，例如 [定时: 每天早上8点提醒我起床]；"
            "查看/删除/暂停现有任务时写 [定时列表]、[定时删除: 关键词]、"
            "[定时暂停: 关键词]、[定时恢复: 关键词]。adapter 会执行并附上结果。"
            "其他情况正常聊天。）"
        )

    def start_scheduler(self) -> None:
        if self.tasks is None or not self.config.tasks.enabled:
            return
        self._scheduler = TaskScheduler(
            self.tasks,
            self._send_scheduled,
            check_seconds=self.config.tasks.check_seconds,
            ready=lambda: self.onebot.connected,
        )
        self._scheduler.start()
        log.info("task scheduler started (%d task(s))", self.tasks.count())

    async def _send_to_chat(self, chat_type: str, chat_id: int, text: str) -> None:
        if chat_type == "group":
            data = await self.onebot.send_group(chat_id, text)
            self._remember_own_message(chat_id, int(data.get("message_id") or 0))
            self.proactive.note_speaking(chat_id, time.time())
        else:
            await self.onebot.send_private(chat_id, text)

    async def _send_scheduled(self, task: Any) -> None:
        text = task.message
        if getattr(task, "mode", "fixed") == "generate":
            text = await self._compose_task_message(task)
        await self._send_to_chat(task.chat_type, task.chat_id, text)

    def start_feeds(self) -> None:
        if self.feeds is None or not self.config.feeds.enabled:
            return
        self._feed_poller = FeedPoller(
            self.feeds,
            self._send_feed,
            check_seconds=self.config.feeds.check_seconds,
            max_new=self.config.feeds.max_new,
        )
        self._feed_poller.start()
        log.info("feed poller started (%d subscription(s))", self.feeds.count())

    async def _send_feed(self, record: Any, entry: Any) -> None:
        await self._send_to_chat(
            record.chat_type, record.chat_id, format_entry(record.title, entry)
        )

    async def add_feed(
        self, event: MessageEvent, target: str
    ) -> tuple[Any, bool]:
        store = self.feeds
        if store is None or not self.config.feeds.enabled:
            raise ValueError("订阅没有启用")
        if self.config.feeds.max_subs and store.count() >= self.config.feeds.max_subs:
            raise ValueError(f"订阅数已达上限（{self.config.feeds.max_subs}）")
        target = target.strip()
        source = "rss"
        if target.isdigit() or "bilibili.com" in target:
            url = bilibili_feed_url(target, self.config.feeds.rsshub_base)
            source = "bilibili"
        elif target.startswith(("http://", "https://")):
            url = target
        else:
            raise ValueError("请给 B 站 UID、空间链接或 RSS 地址")
        data = await fetch_feed(url)
        entries = parse_feed(data)
        title = feed_title(data) or url
        chat_type, chat_id = resolve_target(
            "here", event.message_type, event.user_id, event.group_id
        )
        record, created = store.add(
            chat_type=chat_type,
            chat_id=chat_id,
            feed_url=url,
            title=title,
            source=source,
            seen=tuple(entry.id for entry in entries),
        )
        return record, created

    async def _compose_task_message(self, task: Any) -> str:
        """Let DSH (tools included) or the card write the scheduled line."""
        conversation_id = (
            f"group:{task.chat_id}"
            if getattr(task, "chat_type", "") == "group"
            else f"private:{task.chat_id}"
        )
        prompt = (
            f"[定时任务] {task.message}。现在是 "
            f"{datetime.now(TZ).strftime('%Y-%m-%d %H:%M')}，"
            "按人设完成这件事，只输出要发送的内容。"
        )
        if self.dsh is not None:
            try:
                reply = await asyncio.to_thread(
                    self.dsh.run, f"task-{task.id}", prompt
                )
            except DSHError as exc:
                log.warning("task #%s DSH generation failed: %s", task.id, exc)
            else:
                if reply.strip():
                    return reply.strip()
        card = self._card
        if card is None:
            return task.message
        system = build_system_prompt(card.data, card.name, "群友", datetime.now(TZ), [])
        try:
            reply = await self.client.chat([
                {
                    "role": "system",
                    "content": system
                    + "\n\n[定时任务]\n现在是任务触发时间，按人设发一句提醒，"
                      "不要解释，也不要提自己是 AI。",
                },
                {
                    "role": "user",
                    "content": f"任务：{task.message}。只输出要发送的那一句话。",
                },
            ], purpose="task_generate", conversation_id=conversation_id)
        except LLMError as exc:
            log.warning("task #%s generation failed: %s", task.id, exc)
            return task.message
        return reply.strip() or task.message

    async def create_task(
        self, event: MessageEvent, description: str
    ) -> tuple[Any, Any]:
        """Parse and store an admin task; returns (record, next_run)."""
        store = self.tasks
        if store is None or not self.config.tasks.enabled:
            raise ValueError("定时任务没有启用")
        if self.config.tasks.max_tasks and store.count() >= self.config.tasks.max_tasks:
            raise ValueError(f"任务数已达上限（{self.config.tasks.max_tasks}）")
        now = datetime.now(TASK_TZ)
        if event.message_type == "group" and event.group_id is not None:
            origin_conversation = f"group:{event.group_id}"
        elif event.message_type == "private":
            origin_conversation = f"private:{event.user_id}"
        else:
            origin_conversation = ""
        spec, _ = await parse_task(
            self.client, description, now, conversation_id=origin_conversation
        )
        # Parsing takes time; schedule from the moment the task is created.
        next_run = next_run_for(spec, datetime.now(TASK_TZ))
        if self.config.tasks.max_tasks and store.count() >= self.config.tasks.max_tasks:
            raise ValueError(f"任务数已达上限（{self.config.tasks.max_tasks}）")
        chat_type, chat_id = resolve_target(
            spec.target,
            event.message_type,
            event.user_id,
            event.group_id,
        )
        if (
            self.config.tasks.max_per_chat
            and store.count_for(chat_type, chat_id) >= self.config.tasks.max_per_chat
        ):
            raise ValueError(
                f"这个会话的任务数已达上限（{self.config.tasks.max_per_chat}）"
            )
        record = store.add(
            chat_type=chat_type,
            chat_id=int(chat_id),
            created_by=str(event.user_id),
            instruction=description,
            spec=spec,
            next_run=next_run,
        )
        return record, next_run

    def resume_task(self, record: Any) -> Any:
        store = self.tasks
        assert store is not None
        return store.set_enabled(record.id, True, next_run_for(spec_of(record), datetime.now(TASK_TZ)))

    # ---------------------------------------------------------- stickers

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _sticker_budget_ok(self, group_id: int) -> bool:
        limit = self.config.stickers.max_per_hour
        if limit <= 0:
            return False
        now = time.time()
        window = self._sticker_budget.setdefault(group_id, deque())
        while window and now - window[0] > 3600:
            window.popleft()
        if len(window) >= limit:
            return False
        window.append(now)
        return True

    async def _collect_stickers(self, event: MessageEvent) -> None:
        """Download a group's stickers into the library; never blocks a reply."""
        store = self.stickers
        if store is None or not self.config.stickers.collect:
            return
        if event.message_type != "group" or event.group_id is None:
            return
        for url in event.stickers[:2]:
            if not self._sticker_budget_ok(event.group_id):
                return
            try:
                data, mime = await download_image(
                    url, max_bytes=self.config.stickers.max_bytes
                )
                record, created = await asyncio.to_thread(
                    store.add_bytes,
                    data,
                    mime=mime,
                    source=f"group:{event.group_id}",
                    source_kind="chat",
                    max_bytes=self.config.stickers.max_bytes,
                )
            except (StickerError, OSError, sqlite3.Error) as exc:
                log.warning("sticker collect failed: %s", exc)
                continue
            except Exception:  # noqa: BLE001 - a sticker must not break chat
                log.exception("sticker collect crashed")
                return
            if created:
                log.info(
                    "sticker #%s collected from group %s (%d bytes)",
                    record.id, event.group_id, record.size,
                )
                await self._tag_sticker(record, f"group:{event.group_id}")
                await asyncio.to_thread(store.evict, self.config.stickers.max_stored)

    async def _tag_sticker(self, record: Any, conversation_id: str = "") -> None:
        """Ask the vision model for a description and a few emotion tags."""
        store = self.stickers
        if store is None:
            return
        try:
            data = await asyncio.to_thread(Path(record.path).read_bytes)
        except OSError as exc:
            log.warning("cannot read sticker #%s: %s", record.id, exc)
            return
        # Big animated GIFs time out the vision request; send one small frame.
        preview = await asyncio.to_thread(preview_jpeg, data)
        if preview is not None:
            data, mime = preview, "image/jpeg"
        elif len(data) > 512 * 1024:
            log.warning(
                "sticker #%s too large to tag without a preview (%d bytes)",
                record.id, len(data),
            )
            return
        else:
            mime = record.mime or "image/png"
        uri = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        try:
            raw = await self.client.chat([
                {"role": "system", "content": "你是一个表情包标注助手。"},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "这是一个聊天表情包。用不超过 20 个字描述画面和情绪；"
                                "再给 1-3 个情绪标签（如 开心/无语/生气/委屈/得意/疑惑/累/吃饭/睡觉）；"
                                "最后判断是否适合在群聊发送：色情、暴力、违法、辱骂、聊天截图、"
                                "文字过多都算不合适。只返回 JSON："
                                '{"desc": "...", "tags": ["..."], "safe": true}'
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": uri}},
                    ],
                },
            ], purpose="sticker_tag", conversation_id=conversation_id)
        except LLMError as exc:
            log.warning("sticker tagging failed: %s", exc)
            return
        description, tags, safe = parse_tags(raw)
        try:
            await asyncio.to_thread(store.describe, record.id, description, tags)
            if not safe:
                await asyncio.to_thread(store.set_status, record.id, "banned")
        except (ValueError, sqlite3.Error) as exc:
            log.warning("cannot store sticker tags: %s", exc)
            return
        log.info(
            "sticker #%s tagged: %s %s%s",
            record.id,
            description,
            ",".join(tags) or "-",
            "" if safe else " (banned by moderation)",
        )

    def _sticker_hint(self) -> str:
        store = self.stickers
        if store is None or not self.config.stickers.send:
            return ""
        try:
            stats = store.stats()
        except sqlite3.Error:
            return ""
        if stats["count"] <= stats["banned"]:
            return ""
        return (
            "\n（想发表情包时，在回复最后单独加一行 [表情: 语气词]，"
            "例如 [表情: 无语]；不想发就省略这一行。）"
        )

    async def _send_sticker(self, event: MessageEvent, hint: str) -> None:
        store = self.stickers
        if store is None or not self.config.stickers.send:
            return
        key = event.group_id if event.group_id is not None else event.user_id
        now = time.time()
        window = self._sticker_sends.setdefault(key, deque())
        while window and now - window[0] > 3600:
            window.popleft()
        if len(window) >= self.config.stickers.max_sends_per_hour:
            return
        if now - self._sticker_last.get(key, 0.0) < self.config.stickers.send_cooldown_seconds:
            return
        record = await asyncio.to_thread(store.pick, hint)
        if record is None:
            return
        try:
            data = await asyncio.to_thread(Path(record.path).read_bytes)
        except OSError as exc:
            log.warning("cannot read sticker #%s: %s", record.id, exc)
            return
        file = "base64://" + base64.b64encode(data).decode("ascii")
        try:
            if event.message_type == "group" and event.group_id is not None:
                sent = await self.onebot.send_group_image(event.group_id, file)
                self._remember_own_message(
                    event.group_id, int(sent.get("message_id") or 0)
                )
            else:
                await self.onebot.send_private_image(event.user_id, file)
        except (OneBotError, TypeError, ValueError) as exc:
            log.error("failed to send sticker: %s", exc)
            return
        window.append(now)
        self._sticker_last[key] = now
        store.mark_used(record.id)
        log.info("sent sticker #%s to %s (hint=%r)", record.id, key, hint)

    async def _send_voice(self, event: MessageEvent, text: str) -> bool:
        config = self.config.voice
        if not config.enabled:
            log.info("voice skipped: disabled")
            return False
        text = " ".join(text.split())
        if not text:
            log.info("voice skipped: empty spoken text")
            return False
        if config.max_chars > 0 and len(text) > config.max_chars:
            log.info("voice skipped: spoken text %d > %d chars", len(text), config.max_chars)
            return False
        key = event.group_id if event.group_id is not None else event.user_id
        now = time.time()
        if now - self._voice_last.get(key, 0.0) < config.cooldown_seconds:
            log.info("voice skipped: cooldown (%ss)", config.cooldown_seconds)
            return False
        try:
            if config.provider == "fish":
                audio = await synthesize_fish(
                    text,
                    reference_id=config.voice,
                    model=config.model,
                    api_key_env=config.api_key_env,
                    base_url=config.base_url,
                )
            elif config.provider == "stepfun":
                audio = await synthesize_stepfun(
                    text,
                    voice=config.voice,
                    model=config.model,
                    api_key_env=config.api_key_env,
                    base_url=config.base_url,
                    instruction=config.instruction,
                    speed=config.speed,
                )
            else:
                audio = await synthesize(text, voice=config.voice, rate=config.rate)
        except VoiceError as exc:
            log.warning("voice synthesis failed: %s", exc)
            return False
        file = "base64://" + base64.b64encode(audio).decode("ascii")
        try:
            if event.message_type == "group" and event.group_id is not None:
                sent = await self.onebot.send_group_record(event.group_id, file)
                self._remember_own_message(
                    event.group_id, int(sent.get("message_id") or 0)
                )
            else:
                await self.onebot.send_private_record(event.user_id, file)
        except (OneBotError, TypeError, ValueError) as exc:
            log.error("failed to send voice: %s", exc)
            return False
        self._voice_last[key] = now
        log.info("voice reply sent to %s (%d bytes)", key, len(audio))
        return True

    async def _send_meme(self, event: MessageEvent, text: str) -> bool:
        config = self.config.meme
        if not config.enabled:
            return False
        text = " ".join(text.split())[: config.max_chars]
        if not text:
            return False
        key = event.group_id if event.group_id is not None else event.user_id
        now = time.time()
        if now - self._meme_last.get(key, 0.0) < config.cooldown_seconds:
            return False
        base: str | None = None
        base_record = None
        if self.stickers is not None:
            base_record = self.stickers.pick("")
            base = base_record.path if base_record is not None else None
        path = None
        try:
            path = await asyncio.to_thread(
                render_meme,
                text,
                base_image=base,
                output_dir=config.directory,
            )
            data = await asyncio.to_thread(Path(path).read_bytes)
        except (MemeError, OSError) as exc:
            log.warning("meme render failed: %s", exc)
            if path is not None:
                Path(path).unlink(missing_ok=True)
            return False
        file = "base64://" + base64.b64encode(data).decode("ascii")
        try:
            if event.message_type == "group" and event.group_id is not None:
                sent = await self.onebot.send_group_image(event.group_id, file)
                self._remember_own_message(
                    event.group_id, int(sent.get("message_id") or 0)
                )
            else:
                await self.onebot.send_private_image(event.user_id, file)
        except (OneBotError, TypeError, ValueError) as exc:
            log.error("failed to send meme: %s", exc)
            return False
        finally:
            Path(path).unlink(missing_ok=True)
        self._meme_last[key] = now
        if base_record is not None and self.stickers is not None:
            self.stickers.mark_used(base_record.id)
        log.info("meme sent to %s: %s", key, text)
        return True

    async def _recall_block(self, event: MessageEvent, text: str) -> str:
        """Related older messages from this conversation, for the prompt."""
        if len(text.strip()) < 3:
            return ""
        if event.message_type == "group" and event.group_id is not None:
            conversation_id = f"group:{event.group_id}"
        elif event.message_type == "private":
            conversation_id = f"private:{event.user_id}"
        else:
            return ""
        try:
            rows = await self.memory.recall(conversation_id, text, limit=3)
        except sqlite3.Error as exc:
            log.warning("memory recall failed: %s", exc)
            return ""
        block = format_recall(rows)
        return f"\n\n[相关历史]\n{block}" if block else ""

    def _voice_roll(self, text: str) -> bool:
        """Random gate for voicing an unmarked proactive reply."""
        config = self.config.voice
        if not config.enabled or config.probability <= 0:
            return False
        cleaned = " ".join((text or "").split())
        if not cleaned:
            return False
        return random.random() < config.probability

    async def _send_reply(
        self,
        event: MessageEvent,
        conversation_id: str,
        reply: str,
        *,
        proactive: bool = False,
    ) -> None:
        """Send the visible text, then an optional trailing sticker."""
        outer, schedule = extract_schedule(reply)
        if schedule is not None:
            rendered = render_schedule(schedule)
            reply = f"{outer}\n\n{rendered}".strip() if outer else rendered
        reply, task_description = extract_task_marker(reply)
        if task_description is not None:
            reply = await self._handle_task_request(event, reply, task_description)
        reply, management = extract_task_management(reply)
        if management is not None:
            reply = await self._handle_task_management(event, reply, *management)
        reply, meme_text = extract_meme_marker(reply)
        reply, wants_voice = extract_voice_marker(reply)
        visible, hint = extract_sticker_marker(reply)
        spoken = spoken_text(visible)
        if wants_voice:
            log.info("voice marker found (spoken=%d chars)", len(spoken))
        if not wants_voice and proactive and self._voice_roll(spoken):
            wants_voice = True
            log.info("voice roll hit for a proactive reply")
        voice_sent = (
            await self._send_voice(event, spoken) if wants_voice and spoken else False
        )
        if not voice_sent:
            for chunk in split_text(visible, self.config.bot.max_reply_chars):
                await self._send(event, chunk)
                await asyncio.sleep(0.35)
        meme_sent = (
            await self._send_meme(event, meme_text) if meme_text is not None else False
        )
        if hint is not None and not meme_sent:
            await self._send_sticker(event, hint)

    def _remember_own_message(self, group_id: int, message_id: int) -> None:
        if message_id <= 0:
            return
        ids = self._own_messages.get(group_id)
        if ids is None:
            ids = self._own_messages[group_id] = deque(maxlen=200)
        ids.append(message_id)

    def _context_header(self, event: MessageEvent) -> str:
        now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
        who = f"{event.sender_name}({event.user_id})"
        if event.sender_nickname and event.sender_nickname != event.sender_name:
            who += f" 昵称:{event.sender_nickname}"
        if self.is_admin(event.user_id):
            who += "（管理员）"
        if event.message_type == "group" and event.group_id is not None:
            return f"[群聊 {event.group_id} | 当前时间 {now} | 发言者 {who}]"
        return f"[私聊 | 当前时间 {now} | 用户 {who}]"

    def _turn_prompt(self, event: MessageEvent, text: str) -> tuple[str, int]:
        """Per-chat context: fresh time/identity plus that chat's recent lines."""
        if event.message_type == "group" and event.group_id is not None:
            lines = self.group_context.drain(event.group_id)
            body = format_group_turn(
                lines,
                event.sender_name,
                text,
                self.config.bot.group_context_chars,
                user_id=event.user_id,
            )
            return (
                self._context_header(event)
                + "\n"
                + body
                + self._sticker_hint()
                + self._task_hint(event)
                + self._voice_hint()
                + self._meme_hint(),
                len(lines),
            )
        return (
            self._context_header(event)
            + "\n"
            + text
            + self._sticker_hint()
            + self._task_hint(event)
            + self._voice_hint()
            + self._meme_hint(),
            0,
        )

    def refresh_card(self, force: bool = False) -> bool:
        """Load the active card, reloading when /card use or --use-card changed it."""
        try:
            if self.cards.sync_bootstrap(self.config.cards.bootstrap):
                log.info("bootstrap character card changed on disk; re-imported it")
            signature = self.cards.active_signature()
        except (CardError, OSError, sqlite3.Error) as exc:
            log.error("cannot read the character card library: %s", exc)
            return False
        if signature is None:
            log.error("no active character card; install one with /card install")
            return False
        if not force and signature == self._card_sig and self._card is not None:
            return True
        try:
            record = self.cards.active()
        except (CardError, OSError, sqlite3.Error) as exc:
            log.error("cannot load the active character card: %s", exc)
            return False
        if record is None:
            log.error("no active character card; install one with /card install")
            return False
        try:
            card = record.card
        except CardError as exc:
            log.error("cannot parse character card #%s: %s", record.id, exc)
            return False
        try:
            write_lore(card, self.config.dsh.workspace)
        except OSError as exc:
            log.error("cannot write lore file: %s", exc)
        self._card = card
        self._card_sig = signature
        self._card_names = tuple(
            dict.fromkeys(
                name.strip()
                for name in (
                    card.data.get("name"),
                    card.data.get("nickname"),
                    *record.aliases,
                )
                if isinstance(name, str) and name.strip()
            )
        )
        self._system_prompt = build_dsh_system_prompt(card)
        if self.dsh is not None:
            self.dsh.set_system_prompt(self._system_prompt)
        log.info("active character card: #%s %s", record.id, card.name)
        return True

    async def ensure_card(self) -> bool:
        """Reload the active card when it changed, off the event loop.

        Restarting the DSH runtime waits for any in-flight turn, so it must not
        run on the loop thread; message handlers call this before replying.
        """
        async with self._card_lock:
            return await asyncio.to_thread(self.refresh_card)

    async def reload_card(self) -> bool:
        """Force a reload after /card use.

        Runs in a worker thread: restarting the DSH runtime waits for any
        in-flight turn, and that must not stall the event loop.
        """
        async with self._card_lock:
            return await asyncio.to_thread(self.refresh_card, True)

    async def _maybe_reply_proactively(
        self, conversation_id: str, event: MessageEvent, text: str
    ) -> None:
        if self.dsh is None or event.group_id is None:
            return
        now = time.time()
        hour = datetime.now(TZ).hour
        context_count = len(
            self.group_context.peek(event.group_id, self.config.bot.group_context_messages)
        )
        lock = self._locks.setdefault(conversation_id, asyncio.Lock())
        if not self.proactive.should_reply(
            event.group_id, text, context_count, now, hour, locked=lock.locked()
        ):
            return
        async with lock, self._global:
            # Waiting for the lock may have consumed quota or crossed a
            # cooldown; re-check everything except the random gate.
            now = time.time()
            context_count = len(
                self.group_context.peek(event.group_id, self.config.bot.group_context_messages)
            )
            if not self.proactive.should_reply(
                event.group_id, text, context_count, now, datetime.now(TZ).hour,
                rng=lambda: 0.0,
            ):
                return
            await self._reply_proactive(conversation_id, event)

    async def force_proactive(self, event: MessageEvent) -> None:
        """Admin test hook: run a proactive turn regardless of the policy."""
        if event.group_id is None:
            await self._send(event, "主动回复只能在群里测试")
            return
        if self.dsh is None:
            await self._send(event, "DSH 未启用，无法测试主动回复")
            return
        conversation_id = f"group:{event.group_id}"
        if not self.group_context.peek(event.group_id):
            self.group_context.add(
                event.group_id,
                event.sender_name,
                event.text,
                images=event.images,
                user_id=event.user_id,
            )
        await self._maybe_reload_model()
        lock = self._locks.setdefault(conversation_id, asyncio.Lock())
        async with lock, self._global:
            await self._reply_proactive(conversation_id, event)

    async def _reply_proactive(self, conversation_id: str, event: MessageEvent) -> None:
        dsh = self.dsh
        if dsh is None or event.group_id is None:
            return
        if not await self.ensure_card():
            return

        session_id, _ = self._session_for(conversation_id, is_group=True)
        if self.conversations is not None:
            try:
                await asyncio.to_thread(
                    self.conversations.record,
                    session_id, "group", event.user_id, event.group_id, event.sender_name,
                )
            except sqlite3.Error as exc:
                log.error("cannot record conversation identity: %s", exc)

        lines = self.group_context.peek(event.group_id, self.config.bot.group_context_messages)
        image_urls = self.group_context.images(
            event.group_id, self.config.bot.group_context_images
        )
        prompt = (
            self._context_header(event)
            + "\n"
            + format_proactive_turn(
                lines,
                self.config.bot.proactive.prompt,
                self.config.bot.group_context_chars,
            )
            + self._sticker_hint()
        )
        images: list[str] = []
        prepared: list[PreparedImage] = []
        if image_urls:
            if self.config.dsh.image_mode == "inline":
                images = await self._image_urls(image_urls)
            else:
                prepared = await self._prepare_images(
                    image_urls, conversation_id=conversation_id
                )
                prompt += self._image_hint(prepared)
        image_count = len(prepared) if prepared else len(images)
        await self.memory.add(conversation_id, "user", f"{event.sender_name}: {event.text}")
        log.info(
            "proactive reply: group=%s trigger=%s(%s) context=%d images=%d",
            event.group_id, event.sender_name, event.user_id, len(lines), image_count,
        )
        started = time.time()
        try:
            reply, events = await asyncio.to_thread(
                self._dsh_turn, dsh, session_id, prompt, images
            )
        except DSHError as exc:
            log.error("proactive DSH failed after %.1fs: %s", time.time() - started, exc)
            return
        self._record_dsh_usage(conversation_id, session_id, "proactive", events)
        log.info("proactive DSH turn took %.1fs", time.time() - started)
        self._touch_session(conversation_id)

        self.proactive.record(event.group_id, time.time())
        self.group_context.drain(event.group_id)
        log.info("LLM turn took %.1fs (images=%d)", time.time() - started, image_count)
        await self.memory.add(conversation_id, "assistant", reply)
        await self._send_reply(event, conversation_id, reply, proactive=True)

    async def _maybe_reload_model(self) -> None:
        """Pick up --use-model / --set-model changes on the next turn."""
        try:
            signature = model_signature(self.config)
        except (OSError, sqlite3.Error, ValueError) as exc:
            log.warning("cannot read the model registry: %s", exc)
            return
        if signature == self._model_sig:
            return
        async with self._model_lock:
            try:
                signature = model_signature(self.config)
            except (OSError, sqlite3.Error, ValueError) as exc:
                log.warning("cannot read the model registry: %s", exc)
                return
            if signature == self._model_sig:
                return
            await self._swap_model()

    async def _swap_model(self) -> None:
        # Every turn holds one global slot; taking all of them means no turn is
        # still using the clients we are about to close.
        for _ in range(self._GLOBAL_LIMIT):
            await self._global.acquire()
        try:
            try:
                record = load_active_model(self.config)
            except (OSError, ValueError) as exc:
                log.error("cannot reload the model registry: %s", exc)
                return
            if not model_api_key(record):
                log.error(
                    "no API key for %s: set it in the admin UI or put %s in bot.env; "
                    "keeping the current model",
                    record.key, record.api_key_env,
                )
                return
            try:
                new_client = make_llm_client(self.config, record)
                new_dsh = make_dsh_client(self.config, record) if self.config.dsh.enabled else None
            except (OSError, ValueError, LLMError) as exc:
                log.error("cannot apply model %s: %s", record.key, exc)
                return
            old_client, old_dsh = self.client, self.dsh
            self.client, self.dsh = new_client, new_dsh
            # The persona may have changed while the new runtime was built;
            # force the next turn to re-apply it to this runtime.
            self._card_sig = None
            if new_dsh is not None:
                new_dsh.set_system_prompt(self._system_prompt)
            self._model_sig = model_signature(self.config)
            log.info(
                "model switched to %s: context=%d max_output=%d effort=%s",
                record.key, record.context_window, record.max_output_tokens,
                record.effective_effort or "(model default)",
            )
            if old_dsh is not None:
                try:
                    old_dsh.close()
                except Exception:  # noqa: BLE001 - the swap already succeeded
                    log.exception("error closing the previous DSH runtime")
            try:
                await old_client.close()
            except Exception:  # noqa: BLE001
                log.exception("error closing the previous LLM client")
        finally:
            for _ in range(self._GLOBAL_LIMIT):
                self._global.release()

    async def _reply(self, conversation_id: str, event: MessageEvent, text: str) -> None:
        if self.dsh is None:
            await self._reply_llm(conversation_id, event, text)
        else:
            await self._reply_dsh(conversation_id, event, text)

    def _turn_images(self, event: MessageEvent) -> list[str]:
        """Images this turn should see: its own plus recent ones from the group.

        Call this before _turn_prompt(), which drains the group buffer.
        """
        urls = list(event.images)
        if event.message_type == "group" and event.group_id is not None:
            urls.extend(
                self.group_context.images(
                    event.group_id, self.config.bot.group_context_images
                )
            )
        seen: set[str] = set()
        unique: list[str] = []
        for url in urls:
            if url not in seen:
                seen.add(url)
                unique.append(url)
        return unique

    def _usage_store(self) -> UsageStore | None:
        store = getattr(self, "_usage", None)
        if store is not None:
            return store
        try:
            path = getattr(self, "_usage_path", "") or self.config.usage.database
            retention = getattr(
                self, "_usage_retention", self.config.usage.retention_days
            )
            store = UsageStore(path, retention_days=retention)
        except (OSError, sqlite3.Error) as exc:
            log.warning("cannot open usage ledger: %s", exc)
            return None
        self._usage = store
        return store

    def _record_client_usage(
        self,
        purpose: str,
        conversation_id: str,
        usage: dict,
        provider: str,
        model: str,
    ) -> None:
        store = self._usage_store()
        if store is not None:
            store.record_client(
                purpose, conversation_id, usage, provider=provider, model=model
            )

    def _record_dsh_usage(
        self, conversation_id: str, session_id: str, purpose: str, events: list
    ) -> None:
        if not events:
            return
        store = self._usage_store()
        if store is not None:
            store.record_dsh(conversation_id, session_id, purpose, events)

    @staticmethod
    def _dsh_turn(dsh: Any, session_id: str, prompt: str, images: Any = ()) -> tuple[str, list]:
        runner = getattr(dsh, "run_with_events", None)
        if runner is not None:
            return runner(session_id, prompt, images)
        return dsh.run(session_id, prompt, images), []

    def _log_chat_event(self, event: MessageEvent, text: str) -> None:
        if event.message_type == "group" and event.group_id is not None:
            conversation_id = f"group:{event.group_id}"
            chat_id = event.group_id
        elif event.message_type == "private":
            conversation_id = f"private:{event.user_id}"
            chat_id = event.user_id
        else:
            return
        self._spawn(
            self._record_chat_event(
                conversation_id, event.message_type, chat_id, event, text
            )
        )

    async def _record_chat_event(
        self,
        conversation_id: str,
        chat_type: str,
        chat_id: int,
        event: MessageEvent,
        text: str,
    ) -> None:
        try:
            await self.memory.add_event(
                conversation_id,
                chat_type,
                chat_id,
                event.user_id,
                event.sender_name,
                text,
                created_at=int(time.time()),
            )
        except (OSError, sqlite3.Error) as exc:
            log.warning("cannot record chat event: %s", exc)

    def _session_for(self, conversation_id: str, *, is_group: bool) -> tuple[str, bool]:
        store = getattr(self, "sessions", None)
        if store is None:
            return conversation_id.replace(":", "-"), False
        state, rotated = store.current(conversation_id, is_group=is_group)
        if rotated:
            log.info(
                "rotated DSH session: conv=%s -> %s (generation=%d)",
                conversation_id, state.session_id, state.generation,
            )
        return state.session_id, rotated

    def _touch_session(self, conversation_id: str) -> None:
        store = getattr(self, "sessions", None)
        if store is not None:
            store.touch(conversation_id)

    def reset_session(self, conversation_id: str):
        """Force a new DSH generation; used by the admin /reset command."""
        store = getattr(self, "sessions", None)
        if store is None:
            return None
        state = store.reset(conversation_id)
        log.info(
            "manual DSH session reset: conv=%s -> %s (generation=%d)",
            conversation_id, state.session_id, state.generation,
        )
        return state

    def _seed_sessions(self) -> None:
        """Seed rotation metadata for DSH sessions that predate this table."""
        root = Path(self.config.dsh.home) / "sessions"
        if not root.is_dir():
            return
        seeded = 0
        for path in root.rglob("session.v4.jsonl"):
            session_id = path.parent.name
            if re.fullmatch(r"(?:group|private)-\d+", session_id) is None:
                continue
            conversation_id = session_id.replace("-", ":", 1)
            existing = self.sessions.info(conversation_id)
            if existing is not None and (existing.generation != 0 or existing.messages > 0):
                continue
            started_at = 0
            last_active = 0
            messages = 0
            try:
                with path.open(encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            event = json.loads(line)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            continue
                        stamp = int(event.get("time") or 0) // 1000
                        if stamp > 0:
                            started_at = stamp if started_at <= 0 else min(started_at, stamp)
                            last_active = max(last_active, stamp)
                        if event.get("type") == "user/message":
                            messages += 1
            except OSError:
                continue
            if started_at <= 0 or last_active <= 0:
                try:
                    last_active = int(path.stat().st_mtime)
                except OSError:
                    continue
                started_at = last_active
            try:
                if self.sessions.seed_existing(
                    conversation_id,
                    started_at=started_at,
                    last_active=last_active,
                    messages=messages,
                ):
                    seeded += 1
            except sqlite3.Error as exc:
                log.warning("cannot seed session %s: %s", session_id, exc)
        if seeded:
            log.info("seeded %d existing DSH session(s) for rotation", seeded)

    def _image_store(self) -> ImageStore:
        store = self.image_store
        if store is None:
            config = self.config.dsh
            store = ImageStore(
                config.workspace,
                config.image_dir,
                max_bytes=config.image_max_bytes,
                keep_days=config.image_keep_days,
                max_files=config.image_max_files,
            )
            self.image_store = store
        return store

    async def _fetch_image(self, raw: str) -> tuple[bytes, str] | None:
        """Download or decode one image exactly once."""
        if not raw:
            return None
        max_bytes = self.config.dsh.image_max_bytes
        if raw.startswith("data:"):
            header, _, payload = raw.partition(",")
            mime = header[5:].split(";")[0].strip().lower() or "image/jpeg"
            if "base64" not in header.lower():
                log.warning("unsupported image data URI")
                return None
            if mime not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
                mime = "image/jpeg"
            try:
                data = base64.b64decode(payload, validate=True)
            except (ValueError, TypeError):
                log.warning("invalid image data URI")
                return None
            if len(data) > max_bytes:
                log.warning("image too large (%d bytes), skipping", len(data))
                return None
            return data, mime

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=True
            ) as client:
                response = await client.get(raw)
            if response.status_code != 200:
                log.warning("image download HTTP %s: %.120s", response.status_code, raw)
                return None
            data = response.content
            if len(data) > max_bytes:
                log.warning("image too large (%d bytes), skipping", len(data))
                return None
            mime = (response.headers.get("content-type") or "image/jpeg")
            mime = mime.split(";")[0].strip().lower()
            if mime not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
                mime = "image/jpeg"
            return data, mime
        except httpx.HTTPError as exc:
            log.warning("image download failed: %s", exc)
            return None

    async def _caption_image(
        self,
        stored: StoredImage,
        data: bytes,
        mime: str,
        *,
        conversation_id: str = "",
    ) -> str:
        """Return a cached one-off description, generating it on first sight."""
        cached = await self.memory.caption_get(stored.sha1)
        if cached:
            return cached
        uri = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        try:
            caption = await self.client.chat([
                {"role": "system", "content": "你是一个图片描述助手。"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self.config.dsh.caption_prompt},
                        {"type": "image_url", "image_url": {"url": uri}},
                    ],
                },
            ], purpose="image_caption", conversation_id=conversation_id)
        except LLMError as exc:
            log.warning("image caption failed: %s", exc)
            return ""
        caption = " ".join(str(caption or "").split()).strip()[:500]
        if not caption:
            return ""
        await self.memory.caption_set(
            stored.sha1,
            caption,
            model=getattr(self.client.config, "model", ""),
            path=stored.relative,
            mime=mime,
        )
        return caption

    async def _prepare_images(
        self,
        urls: list[str],
        *,
        limit: int = 4,
        conversation_id: str = "",
    ) -> list[PreparedImage]:
        """Store each image once and return its path plus description."""
        prepared: list[PreparedImage] = []
        store = self._image_store()
        for raw in urls[:limit]:
            fetched = await self._fetch_image(raw)
            if fetched is None:
                continue
            data, mime = fetched
            data, mime = await asyncio.to_thread(_shrink_image, data, mime)
            try:
                stored = await asyncio.to_thread(store.save, data, mime)
            except (OSError, ValueError) as exc:
                log.warning("cannot store image: %s", exc)
                continue
            description = ""
            if self.config.dsh.image_caption:
                description = await self._caption_image(
                    stored, data, mime, conversation_id=conversation_id
                )
            prepared.append(PreparedImage(
                sha1=stored.sha1,
                relative=stored.relative,
                mime=mime,
                description=description,
                data_uri=f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
            ))
        return prepared

    def _image_hint(self, images: list[PreparedImage]) -> str:
        """Tell the model where each image went and that it can read it on demand."""
        if not images:
            return ""
        lines = [
            "",
            "[图片附件]",
            "为节省上下文，本轮图片不以内联图片块发送；原图已保存在工作区，下面是描述和路径。",
        ]
        for index, image in enumerate(images, 1):
            description = image.description or "（描述生成失败，需要时用 read 查看原图）"
            lines.append(f"- 图片 {index}：{description}")
            lines.append(f"  原图：{image.relative}")
        lines.append("如果描述不足以判断细节，用 read_image 工具读取对应原图（file_path 填上面的路径）；不需要时不要读。")
        return "\n".join(lines)

    async def _image_urls(self, urls: list[str]) -> list[str]:
        """Turn OneBot image URLs into data URIs the model API can read."""
        fetched: list[str] = []
        for raw in urls[:4]:
            if raw.startswith("data:"):
                fetched.append(raw)
                continue
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=True
                ) as client:
                    response = await client.get(raw)
                if response.status_code != 200:
                    log.warning("image download HTTP %s: %.120s", response.status_code, raw)
                    fetched.append(raw)
                    continue
                data = response.content
                if len(data) > 8 * 1024 * 1024:
                    log.warning("image too large (%d bytes), skipping", len(data))
                    continue
                mime = (response.headers.get("content-type") or "image/jpeg")
                mime = mime.split(";")[0].strip().lower()
                if mime not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
                    mime = "image/jpeg"
                data, mime = await asyncio.to_thread(_shrink_image, data, mime)
                fetched.append(
                    f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
                )
            except httpx.HTTPError as exc:
                log.warning("image download failed: %s", exc)
                fetched.append(raw)
        return fetched

    async def _image_captions(self, urls: list[str]) -> list[str]:
        """Short descriptions for images the DSH path cannot attach directly."""
        captions: list[str] = []
        for uri in await self._image_urls(urls):
            try:
                caption = await self.client.chat([
                    {"role": "system", "content": "你是一个图片描述助手。"},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "用一两句中文客观描述这张图片；其中有文字就照抄关键文字。",
                            },
                            {"type": "image_url", "image_url": {"url": uri}},
                        ],
                    },
                ], purpose="image_caption")
            except LLMError as exc:
                log.warning("image caption failed: %s", exc)
                continue
            caption = caption.strip()
            if caption:
                captions.append(caption)
        return captions

    async def _reply_dsh(self, conversation_id: str, event: MessageEvent, text: str) -> None:
        dsh = self.dsh
        if dsh is None:
            return
        if not await self.ensure_card():
            await self._send(event, self.config.bot.error_message)
            return

        session_id, _ = self._session_for(
            conversation_id, is_group=event.message_type == "group"
        )
        if self.conversations is not None:
            # Explicit identity for the profile plugin; written before the turn starts.
            try:
                await asyncio.to_thread(
                    self.conversations.record,
                    session_id, event.message_type, event.user_id, event.group_id, event.sender_name,
                )
            except sqlite3.Error as exc:
                log.error("cannot record conversation identity: %s", exc)

        # Images are stored under the DSH workspace and referenced by text:
        # the model sees a description plus a path, not an image block that
        # would be replayed from the session on every later turn.
        image_urls = self._turn_images(event)
        prompt, context_lines = self._turn_prompt(event, text)
        prompt += await self._recall_block(event, text)
        images: list[str] = []
        prepared: list[PreparedImage] = []
        if image_urls:
            if self.config.dsh.image_mode == "inline":
                images = await self._image_urls(image_urls)
            else:
                prepared = await self._prepare_images(
                    image_urls, conversation_id=conversation_id
                )
                prompt += self._image_hint(prepared)
        image_count = len(prepared) if prepared else len(images)

        await self.memory.add(conversation_id, "user", text)
        log.info(
            "replying via DSH: conv=%s session=%s user=%s(%s) chars=%d group_context=%d images=%d",
            conversation_id, session_id, event.sender_name, event.user_id, len(text),
            context_lines, image_count,
        )
        started = time.time()
        try:
            reply, events = await asyncio.to_thread(
                self._dsh_turn, dsh, session_id, prompt, images
            )
        except DSHError as exc:
            log.error("DSH failed after %.1fs: %s", time.time() - started, exc)
            if self.config.bot.notify_errors:
                await self._send(event, self.config.bot.error_message)
            return
        self._record_dsh_usage(conversation_id, session_id, "chat", events)
        elapsed = time.time() - started
        self._touch_session(conversation_id)
        log.info("DSH turn took %.1fs (images=%d, prompt=%d chars)", elapsed, image_count, len(prompt))

        await self.memory.add(conversation_id, "assistant", reply)
        await self._send_reply(event, conversation_id, reply)

    async def _reply_llm(self, conversation_id: str, event: MessageEvent, text: str) -> None:
        if not await self.ensure_card() or self._card is None:
            await self._send(event, self.config.bot.error_message)
            return
        card = self._card

        data = card.data
        history = await self.memory.history(conversation_id, self.config.bot.history_turns * 2)
        await self.memory.add(conversation_id, "user", text)

        if self.config.bot.greeting_on_first_chat and not history:
            greeting = data.get("first_mes", "").strip()
            if greeting:
                await self._send(event, greeting)

        context = "\n".join(item["content"] for item in history[-4:])
        lore = card.lorebook.select(
            f"{text}\n{context}",
            max_chars=self.config.bot.lore_max_chars,
        )
        now = datetime.now(TZ)
        # Read the group buffer before _turn_prompt() drains it.
        image_urls = self._turn_images(event)
        turn_text, _ = self._turn_prompt(event, text)
        turn_text += await self._recall_block(event, text)
        messages = build_messages(
            data=data,
            history=history,
            user_text=turn_text,
            char_name=card.name,
            user_name=event.sender_name,
            now=now,
            lore_chunks=lore,
            max_history_chars=self.config.bot.max_history_chars,
        )

        log.info(
            "replying: conv=%s user=%s(%s) chars=%d history=%d lore=%d",
            conversation_id, event.sender_name, event.user_id, len(text), len(history), len(lore),
        )

        images = await self._image_urls(image_urls)
        if images:
            for message in reversed(messages):
                if message.get("role") == "user":
                    text_part = str(message.get("content") or "")
                    message["content"] = [
                        {"type": "text", "text": text_part},
                        *(
                            {"type": "image_url", "image_url": {"url": url}}
                            for url in images
                        ),
                    ]
                    break
            log.info("attached %d image(s) to conv=%s", len(images), conversation_id)

        started = time.time()
        try:
            reply = await self.client.chat(
                messages, purpose="chat", conversation_id=conversation_id
            )
        except LLMError as exc:
            log.error("LLM failed after %.1fs: %s", time.time() - started, exc)
            if self.config.bot.notify_errors:
                await self._send(event, self.config.bot.error_message)
            return

        await self.memory.add(conversation_id, "assistant", reply)
        await self._send_reply(event, conversation_id, reply)

    async def _send(self, event: MessageEvent, text: str) -> None:
        if not text:
            return
        try:
            if event.message_type == "group" and event.group_id is not None:
                data = await self.onebot.send_group(event.group_id, text)
                self._remember_own_message(event.group_id, int(data.get("message_id") or 0))
                self.proactive.note_speaking(event.group_id, time.time())
            else:
                await self.onebot.send_private(event.user_id, text)
        except (OneBotError, OSError) as exc:
            log.error("failed to send message: %s", exc)


async def check_llm(config: AppConfig) -> int:
    try:
        record = load_active_model(config, write_patch=False)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not model_api_key(record):
        print(
            f"error: no API key for {record.key}; set it in the admin UI "
            f"or put {record.api_key_env} in bot.env",
            file=sys.stderr,
        )
        return 2
    client = make_llm_client(config, record)
    try:
        reply = await client.chat([
            {"role": "system", "content": "你是一个测试助手。"},
            {"role": "user", "content": "用一句话确认你能正常工作。"},
        ])
        print(f"model: {record.key}")
        print(f"reply: {reply}")
        return 0
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.close()


async def check_dsh(config: AppConfig) -> int:
    try:
        record = load_active_model(config)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not model_api_key(record):
        print(
            f"error: no API key for {record.key}; set it in the admin UI "
            f"or put {record.api_key_env} in bot.env",
            file=sys.stderr,
        )
        return 2
    client = make_dsh_client(config, record, system_prompt="你是一个测试助手。")
    try:
        session_id = f"selftest-{int(time.time())}"
        reply = await asyncio.to_thread(client.run, session_id, "用一句话确认你能正常工作。")
        print(f"model: {record.key} (effort={record.effective_effort or 'default'})")
        print(f"reply: {reply}")
        return 0
    except DSHError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()


async def check_onebot(config: AppConfig) -> int:
    onebot = OneBotWSClient(config.onebot, _ignore_message)
    task = asyncio.create_task(onebot.run())
    try:
        await onebot.wait_connected(15)
        await asyncio.sleep(1.5)  # let NapCat finish its lifecycle event
        info = await onebot.get_login_info()
        print(f"connected: {info}")
        return 0
    except (TimeoutError, OneBotError) as exc:
        print(f"error: could not reach NapCat OneBot WebSocket: {exc}", file=sys.stderr)
        return 1
    finally:
        await onebot.close()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


async def _ignore_message(_event: MessageEvent) -> None:
    return None


def build_bot(config: AppConfig) -> tuple[Bot, ModelRecord] | None:
    """Build the adapter and its stores, or log why that failed."""
    try:
        record = load_active_model(config, write_patch=config.dsh.enabled)
    except (OSError, ValueError) as exc:
        log.error("cannot load model registry: %s", exc)
        return None
    if not model_api_key(record):
        log.error(
            "no API key for %s; set it in the admin UI or put %s in bot.env",
            record.key, record.api_key_env,
        )
        return None
    if not config.onebot.self_id:
        log.warning("onebot.self_id is 0; set it in config.toml")

    cards = CardRegistry(config.cards.database)
    try:
        active = cards.bootstrap(config.cards.bootstrap)
        if active is None:
            raise CardError(
                "character card library is empty and "
                f"{config.cards.bootstrap} does not exist; run --install-card <url|path>"
            )
        log.info(
            "character card library loaded: %d card(s), active #%s %s",
            cards.count(), active.id, active.display_name,
        )
    except (CardError, OSError, sqlite3.Error) as exc:
        log.error("cannot load the character card library: %s", exc)
        cards.close()
        return None

    memory = Memory(
        config.paths.database,
        event_retention_days=config.usage.event_retention_days,
    )
    client = make_llm_client(config, record)
    dsh_client = make_dsh_client(config, record) if config.dsh.enabled else None
    conversations = ConversationStore(config.paths.profile_db) if dsh_client is not None else None
    stickers: StickerStore | None
    try:
        stickers = StickerStore(config.stickers.database, config.stickers.directory)
        log.info("sticker library loaded: %d stored", stickers.count())
    except (OSError, sqlite3.Error) as exc:
        log.error("cannot open the sticker library: %s", exc)
        stickers = None
    tasks: TaskStore | None
    try:
        tasks = TaskStore(config.tasks.database) if config.tasks.enabled else None
        if tasks is not None:
            log.info("task store loaded: %d task(s)", tasks.count())
    except (OSError, sqlite3.Error) as exc:
        log.error("cannot open the task store: %s", exc)
        tasks = None
    feeds: FeedStore | None
    try:
        feeds = FeedStore(config.feeds.database) if config.feeds.enabled else None
        if feeds is not None:
            log.info("feed store loaded: %d subscription(s)", feeds.count())
    except (OSError, sqlite3.Error) as exc:
        log.error("cannot open the feed store: %s", exc)
        feeds = None
    usage: UsageStore | None
    try:
        usage = UsageStore(
            config.usage.database, retention_days=config.usage.retention_days
        )
        processed = usage.backfill_dsh(Path(config.dsh.home) / "sessions")
        if processed:
            log.info(
                "usage ledger backfilled from %d DSH usage event(s)", processed
            )
    except (OSError, sqlite3.Error) as exc:
        log.warning("cannot initialize usage ledger: %s", exc)
        usage = None

    bot = Bot(
        config, cards, memory, client, dsh_client, conversations, stickers, tasks,
        feeds, usage,
    )
    if not bot.refresh_card(force=True):
        log.error("cannot load the active character card")
        return None
    log.info("model %s via %s", record.key, "DSH" if dsh_client is not None else "fallback client")
    log.info("admins: %s", ", ".join(str(i) for i in config.bot.admin_qq_ids) or "(none)")
    return bot, record


async def run_ws(config: AppConfig) -> int:
    built = build_bot(config)
    if built is None:
        return 2
    bot, record = built

    stop = asyncio.Event()

    def request_stop(*_: Any) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            pass

    runner = asyncio.create_task(bot.onebot.run())
    bot.start_scheduler()
    bot.start_feeds()
    log.info("bot started (%s via WebSocket)", record.key)
    try:
        await stop.wait()
    finally:
        log.info("stopping")
        await bot.close()
        runner.cancel()
        try:
            await runner
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    return 0


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def print_models(registry: ModelRegistry) -> None:
    for record in registry.list():
        marker = "*" if record.is_active else " "
        state = "on " if record.enabled else "off"
        effort = record.effective_effort or "-"
        print(
            f"{marker} {state} {record.key:<40} ctx={record.context_window:>9,} "
            f"max_out={record.max_output_tokens:>6,} effort={effort:<6} {record.display_name}"
        )


def print_cards(records: list[CardRecord]) -> None:
    if not records:
        print("(no character cards installed)")
        return
    for record in records:
        marker = "*" if record.is_active else " "
        tags = f" [{', '.join(record.tags)}]" if record.tags else ""
        aliases = f"  aliases={', '.join(record.aliases)}" if record.aliases else ""
        print(
            f"{marker} #{record.id:<4} {record.display_name}{tags}"
            f"  source={record.source_kind}{aliases}"
        )


def manage_cards(config: AppConfig, args: argparse.Namespace) -> int:
    registry = CardRegistry(config.cards.database)
    try:
        registry.bootstrap(config.cards.bootstrap)
        if args.install_card:
            source = args.install_card
            if source.startswith(("http://", "https://", "data:")):
                data = fetch_card_bytes_sync(source)
                record, created = registry.install_bytes(
                    data, source=source, source_kind="url", installed_by="cli",
                )
            else:
                record, created = registry.install_path(source, installed_by="cli")
            state = "installed" if created else "already installed"
            print(f"{state}: #{record.id} {record.display_name}")
            registry.ensure_active()
        if args.use_card:
            record = registry.resolve(args.use_card)
            if record is None:
                raise ValueError(f"no such character card: {args.use_card}")
            registry.use(record.id)
        if args.add_card_alias or args.remove_card_alias:
            active = registry.active()
            if active is None:
                raise ValueError("no active character card; run --use-card first")
            if args.add_card_alias:
                active = registry.add_alias(active.id, args.add_card_alias)
            if args.remove_card_alias:
                active = registry.remove_alias(active.id, args.remove_card_alias)
            print(f"aliases for #{active.id} {active.display_name}: {', '.join(active.aliases) or '-'}")
        print_cards(registry.search(args.search_cards) if args.search_cards else registry.list())
        if (
            args.install_card
            or args.use_card
            or args.add_card_alias
            or args.remove_card_alias
        ):
            print("the running bot picks this up on its next group message")
        return 0
    except (CardError, CardFetchError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        registry.close()


def manage_models(config: AppConfig, args: argparse.Namespace) -> int:
    registry = ModelRegistry(config.models.database)
    try:
        if args.set_model:
            provider, model_id = parse_key(args.set_model[0])
            fields: dict[str, str] = {}
            for item in args.set_model[1:]:
                name, sep, value = item.partition("=")
                if not sep:
                    raise ValueError(f"expected FIELD=VALUE, got {item!r}")
                fields[name.strip()] = value
            registry.upsert(provider, model_id, fields)
        if args.use_model:
            registry.use(*parse_key(args.use_model))
        print_models(registry)
        if args.set_model or args.use_model:
            print("the running bot picks this up on its next message")
        return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        registry.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="QQ chat bot (NapCat + LLM)")
    parser.add_argument("--config", default="/etc/qqbot/config.toml")
    parser.add_argument("--check", action="store_true", help="test the configured model API and exit")
    parser.add_argument("--check-dsh", action="store_true", help="test the DSH runtime and exit")
    parser.add_argument("--check-onebot", action="store_true", help="test the NapCat connection and exit")
    parser.add_argument("--show-card", action="store_true", help="print the parsed character card and exit")
    parser.add_argument("--list-models", action="store_true", help="print the model registry and exit")
    parser.add_argument("--use-model", metavar="PROVIDER/MODEL", help="make a registered model active and exit")
    parser.add_argument(
        "--set-model", nargs="+", metavar=("PROVIDER/MODEL", "FIELD=VALUE"),
        help=f"create or update a model row and exit; fields: {', '.join(EDITABLE)}",
    )
    parser.add_argument("--list-cards", action="store_true", help="print the character card library and exit")
    parser.add_argument("--use-card", metavar="ID|NAME", help="make a character card active and exit")
    parser.add_argument("--install-card", metavar="URL|PATH", help="install a character card and exit")
    parser.add_argument("--search-cards", metavar="QUERY", help="search the character card library and exit")
    parser.add_argument(
        "--add-card-alias", metavar="NAME",
        help="add an address alias to the active character card and exit",
    )
    parser.add_argument(
        "--remove-card-alias", metavar="NAME",
        help="remove an address alias from the active character card and exit",
    )
    args = parser.parse_args()

    setup_logging()
    load_env_file()
    try:
        config = load_config(args.config)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        print(f"error: cannot load config {args.config}: {exc}", file=sys.stderr)
        return 2
    warn_legacy_model_env()

    if args.show_card:
        registry = CardRegistry(config.cards.database)
        try:
            record = registry.bootstrap(config.cards.bootstrap)
            if record is None:
                print("error: no character card installed", file=sys.stderr)
                return 1
            card = record.card
            print(f"id: #{record.id}")
            print(f"name: {card.name}")
            print(f"source: {record.source_kind} {record.source or '-'}")
            print(f"imported as: {card.payload.get('_import', {})}")
            print(f"description chars: {len(card.data['description'])}")
            print(f"lorebook entries: {len(card.lorebook.entries)}")
            return 0
        except (CardError, OSError, sqlite3.Error) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        finally:
            registry.close()

    if args.list_models or args.use_model or args.set_model:
        return manage_models(config, args)

    if (
        args.list_cards
        or args.use_card
        or args.install_card
        or args.search_cards
        or args.add_card_alias
        or args.remove_card_alias
    ):
        return manage_cards(config, args)

    if args.check_dsh or (args.check and config.dsh.enabled):
        return asyncio.run(check_dsh(config))
    if args.check:
        return asyncio.run(check_llm(config))
    if args.check_onebot:
        return asyncio.run(check_onebot(config))
    return asyncio.run(run_ws(config))


if __name__ == "__main__":
    raise SystemExit(main())
