"""Admin-only chat commands. Commands never enter model history."""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from card_fetch import CardFetchError, fetch_card_bytes
from models import ModelRecord, ModelRegistry

from cards import CardError, CardRecord

log = logging.getLogger(__name__)

# Bot.handle receives text with "[图片]" etc. appended for non-text segments.
_MARKER_RE = re.compile(r"\[(?:图片|表情|语音|视频|文件|动画表情)\]")


def strip_message_markers(text: str) -> str:
    """Drop the placeholders MessageParser adds for non-text segments."""
    return _MARKER_RE.sub("", text).strip()


def source_label(source: str) -> str:
    """A short, display-safe label for an install source (never a whole data URI)."""
    if source.startswith("data:"):
        return source.split(",", 1)[0][:80]
    return source if len(source) <= 300 else source[:297] + "..."

HELP = """可用命令:
/model          列出模型
/model <序号|model_id|provider/model>  切换模型
/card           列出角色卡
/card use <id|名称>      切换角色卡
/card search <关键词>    搜索角色卡
/card install <url|路径> 安装角色卡
/card show <id|名称>     查看角色卡
/card delete <id|名称>   删除角色卡
/card alias [add|remove] <名称>  管理别名（叫到别名也算直接找它）
/proactive [on|off|status|test]        群聊主动回复
/help           显示帮助"""

CARD_HELP = """/card                     列出角色卡
/card use <id|名称>        切换角色卡（立即生效）
/card show <id|名称>       查看详情
/card search <关键词>      在角色库中搜索
/card install <url|路径>   从 URL 或服务器路径安装；
                           也可以把 PNG 角色卡发给机器人并附 /card install
/card delete <id|名称>     从库中删除（不能删当前卡）
/card alias                查看当前卡的名称与别名
/card alias add <名称>     添加别名，群里直接叫它就会被当成在找它
/card alias remove <名称>  删除别名
/card alias list <id|名称> 查看别的卡的别名"""


class CommandRouter:
    def __init__(self, bot: Any):
        self.bot = bot

    def is_command(self, text: str) -> bool:
        return text.startswith("/")

    async def handle(self, event: Any, text: str) -> None:
        text = strip_message_markers(text)
        if not self.bot.is_admin(event.user_id):
            log.info("ignoring command from non-admin %s: %.80s", event.user_id, text)
            return
        parts = text.strip().split()
        name = parts[0].lower()
        if name in ("/help", "/?"):
            await self.bot.send_text(event, HELP)
        elif name in ("/model", "/models"):
            await self._model(event, parts[1:])
        elif name in ("/card", "/cards"):
            await self._card(event, parts[1:])
        elif name == "/proactive":
            await self._proactive(event, parts[1:])
        else:
            await self.bot.send_text(event, f"未知命令: {name}\n{HELP}")

    async def _model(self, event: Any, args: list[str]) -> None:
        registry = ModelRegistry(self.bot.config.models.database)
        try:
            rows = registry.list()
            if not args:
                await self.bot.send_text(event, self._render(rows))
                return
            target = " ".join(args).strip()
            record = self._resolve(rows, target)
            if record is None:
                await self.bot.send_text(event, f"找不到模型: {target}\n{self._render(rows)}")
                return
            if not record.enabled:
                await self.bot.send_text(event, f"模型已禁用: {record.key}")
                return
            before = registry.active()
            registry.use(record.provider, record.model_id)
            await self.bot.reload_model()
            if before is not None and before.key == record.key:
                await self.bot.send_text(event, f"当前已经是: {record.key}")
            else:
                await self.bot.send_text(event, f"已切换模型: {record.key}")
        finally:
            registry.close()

    # ------------------------------------------------------------ cards

    async def _card(self, event: Any, args: list[str]) -> None:
        registry = self.bot.cards
        action = args[0].lower() if args else "list"
        rest = args[1:]
        target = " ".join(rest).strip()

        if action in ("list", "ls"):
            await self.bot.send_text(
                event, self._render_cards(registry.list(), f"角色卡 (* = 当前，共 {registry.count()} 张):")
            )
        elif action in ("search", "find"):
            if not target:
                await self.bot.send_text(event, "用法: /card search <关键词>")
                return
            rows = registry.search(target, limit=10)
            await self.bot.send_text(
                event, self._render_cards(rows, f"搜索「{target}」:", empty="没有匹配的角色卡。")
            )
        elif action in ("use", "switch"):
            await self._use_card(event, target)
        elif action in ("show", "info"):
            await self._show_card(event, target)
        elif action in ("install", "add"):
            await self._install_card(event, rest)
        elif action in ("delete", "rm", "remove"):
            await self._delete_card(event, target)
        elif action in ("alias", "aliases", "nick"):
            await self._card_alias(event, rest)
        elif action in ("help", "?"):
            await self.bot.send_text(event, CARD_HELP)
        else:
            await self.bot.send_text(event, f"未知角色卡命令: {action}\n{CARD_HELP}")

    async def _use_card(self, event: Any, target: str) -> None:
        if not target:
            await self.bot.send_text(event, "用法: /card use <id|名称>")
            return
        record = self.bot.cards.resolve(target)
        if record is None:
            await self.bot.send_text(event, await self._not_found(target))
            return
        if record.is_active:
            await self.bot.send_text(event, f"当前已经是 #{record.id} {record.display_name}")
            return
        try:
            self.bot.cards.use(record.id)
        except ValueError as exc:
            await self.bot.send_text(event, str(exc))
            return
        if await self.bot.reload_card():
            await self.bot.send_text(
                event, f"已切换角色卡: #{record.id} {record.display_name}（立即生效）"
            )
        else:
            await self.bot.send_text(
                event, f"已切换到 #{record.id}，但加载失败，详见日志"
            )

    async def _show_card(self, event: Any, target: str) -> None:
        if not target:
            await self.bot.send_text(event, "用法: /card show <id|名称>")
            return
        record = self.bot.cards.resolve(target)
        if record is None:
            await self.bot.send_text(event, await self._not_found(target))
            return
        try:
            card = record.card
        except CardError as exc:
            await self.bot.send_text(event, f"这张卡的数据已损坏: {exc}")
            return
        data = card.data
        lines = [
            f"#{record.id} {record.display_name}" + ("（当前）" if record.is_active else ""),
            f"来源: {record.source_kind} {record.source or '-'}",
            f"标签: {', '.join(record.tags) or '-'}",
            f"别名: {', '.join(record.aliases) or '-'}",
            f"作者: {record.creator or '-'}  版本: {record.character_version or '-'}",
            f"简介: {len(data.get('description', ''))} 字符  "
            f"世界书: {len(card.lorebook.entries)} 条",
            f"安装: {record.installed_by or '-'}",
        ]
        preview = data.get("description", "").strip()
        if preview:
            lines.append("——")
            lines.append(preview[:300] + ("…" if len(preview) > 300 else ""))
        await self.bot.send_text(event, "\n".join(lines))

    async def _install_card(self, event: Any, args: list[str]) -> None:
        source = " ".join(args).strip()
        images = getattr(event, "images", ())
        try:
            if source.startswith(("http://", "https://", "data:")):
                data = await fetch_card_bytes(source)
                source_kind, label = "url", source_label(source)
            elif source:
                path = Path(source.removeprefix("file://")).expanduser()
                data = await asyncio.to_thread(path.read_bytes)
                source_kind, label = "file", source_label(str(path.resolve()))
            elif images:
                data = await fetch_card_bytes(images[0])
                source_kind, label = "image", source_label(images[0])
            else:
                await self.bot.send_text(
                    event,
                    "用法: /card install <url|路径>\n"
                    "或者把 PNG 角色卡作为图片发给机器人，并附上 /card install",
                )
                return
        except (CardFetchError, OSError, ValueError) as exc:
            await self.bot.send_text(event, f"读取角色卡失败: {exc}")
            return

        try:
            record, created = self.bot.cards.install_bytes(
                data,
                source=label,
                source_kind=source_kind,
                installed_by=str(event.user_id),
            )
        except (CardError, sqlite3.Error) as exc:
            await self.bot.send_text(event, f"解析角色卡失败: {exc}")
            return

        if created:
            total = self.bot.cards.count()
            await self.bot.send_text(
                event,
                f"已安装 #{record.id} {record.display_name}（共 {total} 张）\n"
                f"用 /card use {record.id} 切换。",
            )
        else:
            await self.bot.send_text(
                event, f"这张卡已经在库里: #{record.id} {record.display_name}"
            )

    async def _delete_card(self, event: Any, target: str) -> None:
        if not target:
            await self.bot.send_text(event, "用法: /card delete <id|名称>")
            return
        record = self.bot.cards.resolve(target)
        if record is None:
            await self.bot.send_text(event, await self._not_found(target))
            return
        try:
            self.bot.cards.delete(record.id)
        except ValueError as exc:
            await self.bot.send_text(event, str(exc))
            return
        await self.bot.send_text(
            event, f"已删除 #{record.id} {record.display_name}（剩 {self.bot.cards.count()} 张）"
        )

    async def _card_alias(self, event: Any, args: list[str]) -> None:
        registry = self.bot.cards
        action = args[0].lower() if args else "list"

        if action in ("add", "+"):
            name = " ".join(args[1:]).strip()
            if not name:
                await self.bot.send_text(event, "用法: /card alias add <名称>")
                return
            record = registry.active()
            if record is None:
                await self.bot.send_text(event, "还没有激活的角色卡。")
                return
            try:
                updated = registry.add_alias(record.id, name)
            except (ValueError, sqlite3.Error) as exc:
                await self.bot.send_text(event, f"添加别名失败: {exc}")
                return
            await self.bot.send_text(
                event,
                f"已为 #{updated.id} {updated.display_name} 添加别名「{name}」\n"
                f"当前别名: {', '.join(updated.aliases) or '-'}（下一条群消息生效）",
            )
            return

        if action in ("remove", "rm", "del", "delete", "-"):
            name = " ".join(args[1:]).strip()
            if not name:
                await self.bot.send_text(event, "用法: /card alias remove <名称>")
                return
            record = registry.active()
            if record is None:
                await self.bot.send_text(event, "还没有激活的角色卡。")
                return
            try:
                updated = registry.remove_alias(record.id, name)
            except (ValueError, sqlite3.Error) as exc:
                await self.bot.send_text(event, f"删除别名失败: {exc}")
                return
            await self.bot.send_text(
                event,
                f"已删除别名「{name}」\n"
                f"当前别名: {', '.join(updated.aliases) or '-'}",
            )
            return

        if action not in ("list", "ls"):
            await self.bot.send_text(event, CARD_HELP)
            return

        target = " ".join(args[1:]).strip()
        if target:
            record = registry.resolve(target)
            if record is None:
                await self.bot.send_text(event, await self._not_found(target))
                return
        else:
            record = registry.active()
            if record is None:
                await self.bot.send_text(event, "还没有激活的角色卡。")
                return
        await self.bot.send_text(
            event,
            f"#{record.id} {record.display_name}"
            + ("（当前）" if record.is_active else "")
            + f"\n名称: {record.name}"
            + f"\n昵称: {record.nickname or '-'}"
            + f"\n别名: {', '.join(record.aliases) or '-'}"
            + "\n用 /card alias add <名称> 添加。",
        )

    async def _not_found(self, target: str) -> str:
        matches = self.bot.cards.search(target, limit=5)
        if not matches:
            return f"找不到角色卡: {target}\n{CARD_HELP}"
        return self._render_cards(matches, f"「{target}」有多个匹配，请用 id 指定:")

    @staticmethod
    def _render_cards(
        rows: list[CardRecord], title: str, empty: str = "角色卡库是空的。用 /card install <url|路径> 安装。"
    ) -> str:
        if not rows:
            return empty
        lines = [title]
        for record in rows[:30]:
            mark = "*" if record.is_active else " "
            tags = f" [{', '.join(record.tags[:4])}]" if record.tags else ""
            lines.append(f"{mark} #{record.id} {record.display_name}{tags}")
        if len(rows) > 30:
            lines.append(f"…还有 {len(rows) - 30} 张，用 /card search 缩小范围")
        if title.endswith(":"):
            lines.append("用法: /card use <id|名称>")
        return "\n".join(lines)

    async def _proactive(self, event: Any, args: list[str]) -> None:
        settings = self.bot.config.bot.proactive
        action = args[0].lower() if args else "status"
        if action in ("on", "off"):
            settings.enabled = action == "on"
            state = "开启" if settings.enabled else "关闭"
            await self.bot.send_text(event, f"主动回复已{state}（本次运行有效）")
            return
        if action == "test":
            await self.bot.force_proactive(event)
            return
        start, end = settings.quiet_hours
        lines = [
            f"主动回复: {'开' if settings.enabled else '关'}（本次运行）",
            f"概率: {settings.probability:.0%}  冷却: {settings.cooldown_seconds}s",
            f"配额: {settings.max_per_hour}/小时, {settings.max_per_day}/天",
            f"最小上下文: {settings.min_context_messages} 条",
            f"接话窗口: {settings.followup_seconds}s ×{settings.followup_multiplier:g}",
            f"别名直呼: {', '.join(settings.aliases) or '-'}",
            f"关键词: {', '.join(settings.keywords) or '-'}",
            f"群: {'全部' if not settings.groups else ', '.join(map(str, settings.groups))}",
            f"安静时段: {start:02d}:00-{end:02d}:00",
        ]
        group_id = getattr(event, "group_id", None)
        if group_id is not None:
            status = self.bot.proactive.status(group_id, time.time())
            last = status["last_reply_ago"]
            lines.append(
                f"本群: 上次 {last if last is not None else '-'}s 前, "
                f"{status['hour_count']}/{status['max_per_hour']} 本小时, "
                f"{status['day_count']}/{status['max_per_day']} 今天"
            )
        await self.bot.send_text(event, "\n".join(lines))

    @staticmethod
    def _render(rows: list[ModelRecord]) -> str:
        lines = ["模型列表 (* = 当前):"]
        for index, row in enumerate(rows, 1):
            mark = "*" if row.is_active else " "
            state = "" if row.enabled else " (disabled)"
            lines.append(
                f"{index}. {mark} {row.key}  ctx={row.context_window:,} "
                f"effort={row.effective_effort or '-'}{state}"
            )
        lines.append("用法: /model <序号|model_id|provider/model>")
        return "\n".join(lines)

    @staticmethod
    def _resolve(rows: list[ModelRecord], target: str) -> ModelRecord | None:
        if target.isdigit():
            index = int(target)
            return rows[index - 1] if 1 <= index <= len(rows) else None
        for row in rows:
            if row.key == target:
                return row
        exact = [row for row in rows if row.model_id == target]
        if len(exact) == 1:
            return exact[0]
        needle = target.lower()
        matches = [
            row for row in rows
            if needle in row.key.lower() or needle in row.display_name.lower()
        ]
        return matches[0] if len(matches) == 1 else None
