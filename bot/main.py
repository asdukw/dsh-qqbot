"""QQ chat bot: NapCat (OneBot 11) + LLM + Character Card V3."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from cards import CardError, CardStore
from dsh_client import DSHClient, DSHConfig, DSHError
from llm import LLMClient, LLMConfig, LLMError, get_provider
from memory import Memory
from onebot import MessageEvent, OneBotClient, OneBotError
from prompt import build_messages, build_system_prompt

log = logging.getLogger("qqbot")
TZ = ZoneInfo("Asia/Shanghai")


@dataclass
class OneBotSection:
    url: str = "ws://127.0.0.1:3001"
    token: str = ""
    self_id: int = 0


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


@dataclass
class PathsSection:
    character: str = "/etc/qqbot/character.json"
    database: str = "/var/lib/qqbot/memory.db"


@dataclass
class DSHSection:
    enabled: bool = True
    home: str = "/var/lib/qqbot/dsh-home"
    workspace: str = "/var/lib/qqbot/dsh-workspace"
    profile: str = "qqbot"
    provider: str = "opencode"
    model: str = "deepseek-v4.1-flash"
    max_tokens: int = 2048
    request_timeout: float = 120.0


@dataclass
class AppConfig:
    onebot: OneBotSection = field(default_factory=OneBotSection)
    llm: LLMConfig = field(default_factory=LLMConfig)
    dsh: DSHSection = field(default_factory=DSHSection)
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
    dsh_raw = _section(table, "dsh")
    bot_raw = _section(table, "bot")
    paths_raw = _section(table, "paths")

    llm_raw = _section(table, "llm")
    legacy = False
    if not llm_raw:
        llm_raw = _section(table, "deepseek")
        legacy = True

    provider_name = (
        os.environ.get("LLM_PROVIDER", "").strip()
        or str(llm_raw.get("provider", "opencode")).strip()
        or "opencode"
    )
    provider = get_provider(provider_name)

    # Environment wins, then an explicit [llm] override, then the provider preset.
    # Legacy [deepseek] files keep their tuning values but not a stale base_url/model.
    base_url = (
        os.environ.get("LLM_BASE_URL", "").strip()
        or ("" if legacy else str(llm_raw.get("base_url", "")).strip())
        or provider.base_url
    )
    model = (
        os.environ.get("LLM_MODEL", "").strip()
        or ("" if legacy else str(llm_raw.get("model", "")).strip())
        or provider.default_model
    )
    api_key = (
        os.environ.get("LLM_API_KEY", "").strip()
        or os.environ.get(provider.api_key_env, "").strip()
        or str(llm_raw.get("api_key", "")).strip()
    )
    if not base_url:
        raise ValueError(f"LLM base_url is empty for provider {provider_name!r}; set LLM_BASE_URL")
    if not model:
        raise ValueError(f"LLM model is empty for provider {provider_name!r}; set LLM_MODEL")

    token = os.environ.get("ONEBOT_TOKEN", "").strip() or str(onebot_raw.get("token", "")).strip()

    def env_bool(name: str, default: bool) -> bool:
        value = os.environ.get(name)
        if value is None or not value.strip():
            return default
        return value.strip().lower() in ("1", "true", "yes", "on")

    dsh = DSHSection(
        enabled=env_bool("DSH_ENABLED", bool(dsh_raw.get("enabled", True))),
        home=os.environ.get("DSH_HOME", "").strip() or str(dsh_raw.get("home", "/var/lib/qqbot/dsh-home")),
        workspace=os.environ.get("DSH_WORKSPACE", "").strip() or str(dsh_raw.get("workspace", "/var/lib/qqbot/dsh-workspace")),
        profile=os.environ.get("DSH_PROFILE", "").strip() or str(dsh_raw.get("profile", "qqbot")),
        provider=os.environ.get("DSH_PROVIDER", "").strip() or str(dsh_raw.get("provider", "opencode")),
        model=os.environ.get("DSH_MODEL", "").strip() or str(dsh_raw.get("model", "deepseek-v4.1-flash")),
        max_tokens=int(os.environ.get("DSH_MAX_TOKENS", "").strip() or dsh_raw.get("max_tokens", 2048)),
        request_timeout=float(dsh_raw.get("request_timeout", 120.0)),
    )

    return AppConfig(
        onebot=OneBotSection(
            url=str(onebot_raw.get("url", "ws://127.0.0.1:3001")),
            token=token,
            self_id=int(onebot_raw.get("self_id", 0) or 0),
        ),
        llm=LLMConfig(
            api_key=api_key,
            base_url=base_url,
            model=model,
            provider=provider_name,
            max_tokens=int(llm_raw.get("max_tokens", 800)),
            temperature=float(llm_raw.get("temperature", 1.0)),
            timeout=float(llm_raw.get("timeout", 60.0)),
            thinking=str(llm_raw.get("thinking", "")),
            reasoning_effort=str(llm_raw.get("reasoning_effort", "")),
        ),
        dsh=dsh,
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
        ),
        paths=PathsSection(
            character=str(paths_raw.get("character", "/etc/qqbot/character.json")),
            database=str(paths_raw.get("database", "/var/lib/qqbot/memory.db")),
        ),
    )


def dsh_api_key(config: AppConfig) -> str:
    try:
        env_name = get_provider(config.dsh.provider).api_key_env
    except ValueError:
        env_name = "LLM_API_KEY"
    return os.environ.get("LLM_API_KEY", "").strip() or os.environ.get(env_name, "").strip()


def build_dsh_system_prompt(card) -> str:
    prompt = build_system_prompt(card.data, card.name, "用户", datetime.now(TZ), [])
    return prompt + "\n\n[设定资料]\n角色设定资料保存在 memory/lore.md；需要时用 grep/read 查阅。"


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
    path.write_text("\n".join(lines), encoding="utf-8")


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
        cards: CardStore,
        memory: Memory,
        client: LLMClient,
        dsh: DSHClient | None = None,
    ):
        self.config = config
        self.cards = cards
        self.memory = memory
        self.client = client
        self.dsh = dsh
        self.onebot = OneBotClient(config.onebot, self.handle)
        self._locks: dict[str, asyncio.Lock] = {}
        self._global = asyncio.Semaphore(3)
        self._card_mtime = 0.0

    async def handle(self, event: MessageEvent) -> None:
        if event.message_type == "group":
            if not self.config.bot.reply_group_at or not event.at_me:
                return
            conversation_id = f"group:{event.group_id}:{event.user_id}"
        elif event.message_type == "private":
            if not self.config.bot.reply_private:
                return
            conversation_id = f"private:{event.user_id}"
        else:
            return

        text = event.text[: self.config.bot.max_input_chars]
        if not text:
            return

        lock = self._locks.setdefault(conversation_id, asyncio.Lock())
        async with lock, self._global:
            await self._reply(conversation_id, event, text)

    async def _reply(self, conversation_id: str, event: MessageEvent, text: str) -> None:
        if self.dsh is not None:
            await self._reply_dsh(conversation_id, event, text)
        else:
            await self._reply_llm(conversation_id, event, text)

    async def _reply_dsh(self, conversation_id: str, event: MessageEvent, text: str) -> None:
        dsh = self.dsh
        if dsh is None:
            return
        try:
            card = self.cards.get()
        except (CardError, OSError) as exc:
            log.error("cannot load character card: %s", exc)
            await self._send(event, self.config.bot.error_message)
            return

        if card.mtime != self._card_mtime:
            try:
                write_lore(card, self.config.dsh.workspace)
            except OSError as exc:
                log.error("cannot write lore file: %s", exc)
            dsh.set_system_prompt(build_dsh_system_prompt(card))
            self._card_mtime = card.mtime

        session_id = conversation_id.replace(":", "-")
        await self.memory.add(conversation_id, "user", text)
        log.info(
            "replying via DSH: conv=%s session=%s user=%s(%s) chars=%d",
            conversation_id, session_id, event.sender_name, event.user_id, len(text),
        )
        try:
            reply = await asyncio.to_thread(dsh.run, session_id, text)
        except DSHError as exc:
            log.error("DSH failed: %s", exc)
            if self.config.bot.notify_errors:
                await self._send(event, self.config.bot.error_message)
            return

        await self.memory.add(conversation_id, "assistant", reply)
        for chunk in split_text(reply, self.config.bot.max_reply_chars):
            await self._send(event, chunk)
            await asyncio.sleep(0.35)

    async def _reply_llm(self, conversation_id: str, event: MessageEvent, text: str) -> None:
        try:
            card = self.cards.get()
        except (CardError, OSError) as exc:
            log.error("cannot load character card: %s", exc)
            await self._send(event, self.config.bot.error_message)
            return

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
        messages = build_messages(
            data=data,
            history=history,
            user_text=text,
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
        try:
            reply = await self.client.chat(messages)
        except LLMError as exc:
            log.error("LLM failed: %s", exc)
            if self.config.bot.notify_errors:
                await self._send(event, self.config.bot.error_message)
            return

        await self.memory.add(conversation_id, "assistant", reply)
        for chunk in split_text(reply, self.config.bot.max_reply_chars):
            await self._send(event, chunk)
            await asyncio.sleep(0.35)

    async def _send(self, event: MessageEvent, text: str) -> None:
        if not text:
            return
        try:
            if event.message_type == "group" and event.group_id is not None:
                await self.onebot.send_group(event.group_id, text)
            else:
                await self.onebot.send_private(event.user_id, text)
        except (OneBotError, OSError) as exc:
            log.error("failed to send message: %s", exc)


async def check_llm(config: AppConfig) -> int:
    if not config.llm.api_key:
        print("error: API key for the selected provider is not set", file=sys.stderr)
        return 2
    client = LLMClient(config.llm)
    try:
        reply = await client.chat([
            {"role": "system", "content": "你是一个测试助手。"},
            {"role": "user", "content": "用一句话确认你能正常工作。"},
        ])
        print(f"provider: {config.llm.provider}")
        print(f"model: {config.llm.model}")
        print(f"reply: {reply}")
        return 0
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.close()


async def check_dsh(config: AppConfig) -> int:
    key = dsh_api_key(config)
    if not key:
        print(
            f"error: API key for DSH provider {config.dsh.provider!r} is not set",
            file=sys.stderr,
        )
        return 2
    client = DSHClient(
        DSHConfig(
            home=config.dsh.home,
            workspace=config.dsh.workspace,
            profile=config.dsh.profile,
            provider=config.dsh.provider,
            model=config.dsh.model,
            max_tokens=config.dsh.max_tokens,
            request_timeout=config.dsh.request_timeout,
        ),
        system_prompt="你是一个测试助手。",
    )
    try:
        session_id = f"selftest-{int(time.time())}"
        reply = await asyncio.to_thread(client.run, session_id, "用一句话确认你能正常工作。")
        print(f"provider: {config.dsh.provider}")
        print(f"model: {config.dsh.model}")
        print(f"reply: {reply}")
        return 0
    except DSHError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()


async def check_onebot(config: AppConfig) -> int:
    onebot = OneBotClient(config.onebot, _ignore_message)
    task = asyncio.create_task(onebot.run())
    try:
        await onebot.wait_connected(15)
        await asyncio.sleep(1.5)  # let NapCat finish its lifecycle event
        info = await onebot.get_login_info()
        print(f"connected: {info}")
        return 0
    except (OneBotError, asyncio.TimeoutError) as exc:
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


async def run(config: AppConfig) -> int:
    if config.dsh.enabled:
        if not dsh_api_key(config):
            log.error(
                "API key for DSH provider %r is not set (put it in /etc/qqbot/bot.env)",
                config.dsh.provider,
            )
            return 2
    elif not config.llm.api_key:
        log.error("API key for provider %r is not set (put it in /etc/qqbot/bot.env)", config.llm.provider)
        return 2
    if not config.onebot.self_id:
        log.warning("onebot.self_id is 0; set it in config.toml")

    cards = CardStore(config.paths.character)
    try:
        card = cards.get()
        log.info("character card loaded: %s (%s)", card.name, card.path)
    except (CardError, OSError) as exc:
        log.error("cannot load character card: %s", exc)
        return 2

    memory = Memory(config.paths.database)
    client = LLMClient(config.llm)
    dsh_client = None
    if config.dsh.enabled:
        dsh_client = DSHClient(
            DSHConfig(
                home=config.dsh.home,
                workspace=config.dsh.workspace,
                profile=config.dsh.profile,
                provider=config.dsh.provider,
                model=config.dsh.model,
                max_tokens=config.dsh.max_tokens,
                request_timeout=config.dsh.request_timeout,
            )
        )

    stop = asyncio.Event()

    def request_stop(*_: Any) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            pass

    bot = Bot(config, cards, memory, client, dsh_client)
    runner = asyncio.create_task(bot.onebot.run())
    if dsh_client is not None:
        log.info("bot started (DSH provider=%s model=%s)", config.dsh.provider, config.dsh.model)
    else:
        log.info("bot started (provider=%s model=%s)", config.llm.provider, config.llm.model)
    try:
        await stop.wait()
    finally:
        log.info("stopping")
        await bot.onebot.close()
        runner.cancel()
        try:
            await runner
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        if dsh_client is not None:
            dsh_client.close()
        await client.close()
        memory.close()
    return 0


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> int:
    parser = argparse.ArgumentParser(description="QQ chat bot (NapCat + LLM)")
    parser.add_argument("--config", default="/etc/qqbot/config.toml")
    parser.add_argument("--check", action="store_true", help="test the configured model API and exit")
    parser.add_argument("--check-dsh", action="store_true", help="test the DSH runtime and exit")
    parser.add_argument("--check-onebot", action="store_true", help="test the NapCat connection and exit")
    parser.add_argument("--show-card", action="store_true", help="print the parsed character card and exit")
    args = parser.parse_args()

    setup_logging()
    load_env_file()
    try:
        config = load_config(args.config)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        print(f"error: cannot load config {args.config}: {exc}", file=sys.stderr)
        return 2

    if args.show_card:
        try:
            card = CardStore(config.paths.character).get()
        except (CardError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"path: {card.path}")
        print(f"imported as: {card.payload['_import']}")
        print(f"name: {card.name}")
        print(f"description chars: {len(card.data['description'])}")
        print(f"lorebook entries: {len(card.lorebook.entries)}")
        return 0

    if args.check_dsh or (args.check and config.dsh.enabled):
        return asyncio.run(check_dsh(config))
    if args.check:
        return asyncio.run(check_llm(config))
    if args.check_onebot:
        return asyncio.run(check_onebot(config))
    return asyncio.run(run(config))


if __name__ == "__main__":
    raise SystemExit(main())
