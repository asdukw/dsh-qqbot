"""Admin-only chat commands. Commands never enter model history."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from card_fetch import CardFetchError, fetch_card_bytes
from feeds import FeedError
from llm import LLMError
from models import ModelRecord, ModelRegistry
from onebot import OneBotError
from relations import render_relation
from stickers import StickerError, download_image
from tasks import TZ as TASK_TZ
from tasks import describe_record, describe_target, render_task_list

from cards import CardError, CardRecord

log = logging.getLogger(__name__)

# Bot.handle receives text with "[图片]" etc. appended for non-text segments.
_MARKER_RE = re.compile(r"\[(?:图片|表情包|表情|语音|视频|文件|动画表情)\]")


_LEADING_MENTION_RE = re.compile(r"^@\S+\s+(?=/)")

def strip_message_markers(text: str) -> str:
    """Drop the placeholders MessageParser adds for non-text segments."""
    return _MARKER_RE.sub("", text).strip()


def strip_leading_mention(text: str) -> str:
    """Drop a typed "@name " prefix in front of a slash command.

    QQ sometimes delivers a typed "@bot" as plain text instead of a mention.
    Only applies when a command follows, so ordinary chatter is untouched.
    """
    return _LEADING_MENTION_RE.sub("", text, count=1).strip()


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
/reset          归档当前 DSH 会话，下一条消息开始新会话
/sticker        表情库管理与导入
/task           定时任务（仅管理员）
/sub            RSS / B 站订阅（仅管理员）
/relation       查看 bot 对你的印象（所有人可用）
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


TASK_HELP = """/task                     列出定时任务
/task add <一句话>        语义化创建，例如：每天早上8点提醒大家喝水
/task delete <id>         删除任务
/task pause|resume <id>   暂停 / 恢复
/task run <id>            立即执行一次（不改动排期）"""


SUB_HELP = """/sub add <uid|空间链接|RSS>  订阅；B 站 UID 走 RSSHub
/sub list                    当前会话的订阅
/sub del <id>                删除订阅
/sub check                   立即检查一次所有订阅"""


RELATION_HELP = """/relation                查看 bot 对你的印象
/relation <QQ号>         查看 bot 对这个人的公开印象
/relation set <QQ> <0-100>  管理员设置好感度
/relation reset <QQ>        管理员清空记录"""


STICKER_HELP = """/sticker                     列出最近收藏的表情包
/sticker list [数量]          同上
/sticker search <关键词>      按描述/标签/来源搜索
/sticker show <id>            查看详情（文件路径、大小、来源）
/sticker add                  把附带的图片收藏进库
/sticker import               导入 QQ 账号的收藏表情
/sticker tag <id> <描述>      手动写描述（供发送时按语境挑选）
/sticker send <id>            手动发一张，测试发送链路
/sticker delete <id>          删除（同时删文件）
/sticker ban|unban <id>       禁用/恢复，禁用的不会被发送
/sticker stats                数量与占用空间"""


class CommandRouter:
    def __init__(self, bot: Any):
        self.bot = bot

    def is_command(self, text: str) -> bool:
        return text.startswith("/")

    async def handle(self, event: Any, text: str) -> None:
        text = strip_message_markers(text)
        parts = text.strip().split()
        name = parts[0].lower()
        if name in ("/relation", "/关系", "/好感度", "/印象"):
            await self._relation(event, parts[1:])
            return
        if not self.bot.is_admin(event.user_id):
            log.info("ignoring command from non-admin %s: %.80s", event.user_id, text)
            return
        if name in ("/help", "/?"):
            await self.bot.send_text(event, HELP)
        elif name in ("/model", "/models"):
            await self._model(event, parts[1:])
        elif name in ("/card", "/cards"):
            await self._card(event, parts[1:])
        elif name == "/proactive":
            await self._proactive(event, parts[1:])
        elif name in ("/reset", "/new"):
            await self._reset(event)
        elif name in ("/sticker", "/stickers"):
            await self._sticker(event, parts[1:])
        elif name in ("/task", "/tasks", "/remind"):
            await self._task(event, parts[1:])
        elif name in ("/sub", "/subs", "/subscribe"):
            await self._sub(event, parts[1:])
        else:
            await self.bot.send_text(event, f"未知命令: {name}\n{HELP}")

    async def _relation(self, event: Any, args: list[str]) -> None:
        """Public read command; admins can also set/reset records."""
        store = getattr(self.bot, "relations", None)
        if store is None:
            await self.bot.send_text(event, "关系系统未启用。")
            return
        action = args[0].lower() if args else ""
        is_admin = self.bot.is_admin(event.user_id)
        if action in ("help", "?", "-h", "--help"):
            await self.bot.send_text(event, RELATION_HELP)
            return
        if action in ("set", "reset"):
            if not is_admin:
                await self.bot.send_text(event, "只有管理员可以修改好感度。")
                return
            if len(args) < 2 or not args[1].isdigit():
                await self.bot.send_text(event, f"用法: /relation {action} <QQ号>")
                return
            target = args[1]
            if action == "reset":
                removed = store.reset(target)
                await self.bot.send_text(
                    event, "已清空这条关系记录。" if removed else "没有这个人的记录。"
                )
                return
            if len(args) < 3 or not args[2].isdigit():
                await self.bot.send_text(event, "用法: /relation set <QQ号> <0-100>")
                return
            record = store.set_affinity(target, args[2])
            await self.bot.send_text(
                event,
                f"已设置 {target} 的好感度为 {record.affinity}/100（{record.tier}）。",
            )
            return

        if action and not action.isdigit():
            await self.bot.send_text(event, RELATION_HELP)
            return
        target = int(action) if action else event.user_id
        is_self = target == event.user_id
        record = store.get(target)
        if is_self:
            label = (
                (record.display_name if record and record.display_name else "")
                or event.sender_name
                or str(target)
            )
        else:
            label = (
                (record.display_name if record and record.display_name else "")
                or f"QQ {target}"
            )
        full = is_self or is_admin
        include_private = event.message_type == "private" and (is_self or is_admin)
        facts = store.facts(target, include_private=include_private and full)
        await self.bot.send_text(
            event,
            render_relation(record, label, facts, full=full, start=store.start),
        )

    async def _reset(self, event: Any) -> None:
        if event.message_type == "group" and event.group_id is not None:
            conversation_id = f"group:{event.group_id}"
        elif event.message_type == "private":
            conversation_id = f"private:{event.user_id}"
        else:
            await self.bot.send_text(event, "无法识别当前会话")
            return
        state = self.bot.reset_session(conversation_id)
        if state is None:
            await self.bot.send_text(event, "DSH 未启用，没有可重置的会话")
        else:
            await self.bot.send_text(event, f"已开启新会话：{state.session_id}")

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

    # --------------------------------------------------------------- tasks

    async def _task(self, event: Any, args: list[str]) -> None:
        store = getattr(self.bot, "tasks", None)
        if store is None:
            await self.bot.send_text(event, "定时任务没有启用（[tasks] enabled = false）。")
            return
        action = args[0].lower() if args else "list"

        if action in ("add", "create", "+"):
            description = " ".join(args[1:]).strip()
            if not description:
                await self.bot.send_text(
                    event,
                    "用法: /task add <用一句话描述>\n"
                    "例如: /task add 每天早上8点提醒大家喝水",
                )
                return
            try:
                record, next_run = await self.bot.create_task(event, description)
            except (ValueError, LLMError) as exc:
                await self.bot.send_text(event, f"创建失败: {exc}")
                return
            await self.bot.send_text(
                event,
                f"已创建任务 #{record.id}：{describe_record(record)}\n"
                f"发送到: {describe_target(record)}\n"
                f"下次执行: {next_run.strftime('%Y-%m-%d %H:%M')}（北京时间）\n"
                f"内容: {record.message}\n"
                f"用 /task delete {record.id} 可以删掉。",
            )
            return

        if action in ("list", "ls"):
            rows = store.list()
            await self.bot.send_text(event, self._render_tasks(rows))
            return

        if action in ("delete", "rm", "remove"):
            record = store.get(int(args[1])) if len(args) > 1 and args[1].isdigit() else None
            if record is None:
                await self.bot.send_text(event, "用法: /task delete <id>")
                return
            store.delete(record.id)
            await self.bot.send_text(event, f"已删除任务 #{record.id}")
            return

        if action in ("pause", "stop"):
            record = store.get(int(args[1])) if len(args) > 1 and args[1].isdigit() else None
            if record is None:
                await self.bot.send_text(event, "用法: /task pause <id>")
                return
            store.set_enabled(record.id, False, None)
            await self.bot.send_text(event, f"已暂停任务 #{record.id}")
            return

        if action in ("resume", "start"):
            record = store.get(int(args[1])) if len(args) > 1 and args[1].isdigit() else None
            if record is None:
                await self.bot.send_text(event, "用法: /task resume <id>")
                return
            try:
                updated = self.bot.resume_task(record)
            except ValueError as exc:
                await self.bot.send_text(event, f"恢复失败: {exc}")
                return
            when = datetime.fromtimestamp(updated.next_run, TASK_TZ).strftime("%m-%d %H:%M")
            await self.bot.send_text(event, f"已恢复任务 #{record.id}，下次 {when}")
            return

        if action in ("run", "test"):
            record = store.get(int(args[1])) if len(args) > 1 and args[1].isdigit() else None
            if record is None:
                await self.bot.send_text(event, "用法: /task run <id>")
                return
            try:
                await self.bot._send_scheduled(record)
            except (OneBotError, OSError) as exc:
                await self.bot.send_text(event, f"执行失败: {exc}")
                return
            await self.bot.send_text(event, f"已执行任务 #{record.id}")
            return

        if action in ("help", "?"):
            await self.bot.send_text(event, TASK_HELP)
            return

        await self.bot.send_text(event, f"未知任务命令: {action}\n{TASK_HELP}")

    @staticmethod
    def _render_tasks(rows: list[Any]) -> str:
        return render_task_list(rows)

    # --------------------------------------------------------------- feeds

    @staticmethod
    def _chat_target(event: Any) -> tuple[str, int]:
        if (
            getattr(event, "message_type", "private") == "group"
            and getattr(event, "group_id", None) is not None
        ):
            return "group", int(event.group_id)
        return "private", int(event.user_id)

    async def _sub(self, event: Any, args: list[str]) -> None:
        store = getattr(self.bot, "feeds", None)
        if store is None:
            await self.bot.send_text(event, "订阅没有启用（[feeds] enabled = false）。")
            return
        action = args[0].lower() if args else "list"

        if action in ("add", "create", "+"):
            target = " ".join(args[1:]).strip()
            if not target:
                await self.bot.send_text(
                    event,
                    "用法: /sub add <UID|空间链接|RSS 地址>\n"
                    "例如: /sub add 546195",
                )
                return
            try:
                record, created = await self.bot.add_feed(event, target)
            except (ValueError, FeedError, OSError) as exc:
                await self.bot.send_text(event, f"订阅失败: {exc}")
                return
            state = "已订阅" if created else "已经订阅过"
            await self.bot.send_text(
                event,
                f"{state} #{record.id}：{record.title}\n{record.feed_url}",
            )
            return

        if action in ("list", "ls"):
            chat_type, chat_id = self._chat_target(event)
            rows = store.list_for(chat_type, chat_id)
            if not rows:
                await self.bot.send_text(event, "当前会话还没有订阅。用 /sub add <UID|RSS> 添加。")
                return
            lines = [f"订阅（共 {len(rows)} 个）:"]
            for record in rows:
                state = f"  ⚠ {record.last_error[:60]}" if record.last_error else ""
                lines.append(f"#{record.id} {record.title}{state}\n  {record.feed_url}")
            await self.bot.send_text(event, "\n".join(lines))
            return

        if action in ("del", "delete", "rm", "remove"):
            feed_id = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
            if feed_id is None:
                await self.bot.send_text(event, "用法: /sub del <id>")
                return
            record = store.get(feed_id)
            chat_type, chat_id = self._chat_target(event)
            if record is None or (record.chat_type, record.chat_id) != (chat_type, chat_id):
                await self.bot.send_text(event, f"当前会话没有订阅 #{feed_id}")
                return
            store.delete(feed_id)
            await self.bot.send_text(event, f"已删除订阅 #{feed_id}（{record.title}）")
            return

        if action == "check":
            poller = getattr(self.bot, "_feed_poller", None)
            if poller is None:
                await self.bot.send_text(event, "订阅轮询器没有启动。")
                return
            sent = await poller.tick()
            await self.bot.send_text(event, f"检查完成，转发了 {sent} 条更新。")
            return

        if action in ("help", "?"):
            await self.bot.send_text(event, SUB_HELP)
            return

        await self.bot.send_text(event, f"未知订阅命令: {action}\n{SUB_HELP}")

    # ----------------------------------------------------------- stickers

    async def _sticker(self, event: Any, args: list[str]) -> None:
        store = getattr(self.bot, "stickers", None)
        if store is None:
            await self.bot.send_text(event, "表情库没有启用（[stickers] 未配置或打开失败）。")
            return
        action = args[0].lower() if args else "list"
        rest = args[1:]

        if action in ("list", "ls"):
            limit = int(rest[0]) if rest and rest[0].isdigit() else 20
            rows = store.list(limit=limit)
            stats = store.stats()
            await self.bot.send_text(
                event,
                self._render_stickers(
                    rows,
                    f"表情库（共 {stats['count']} 张，{stats['banned']} 张禁用，"
                    f"{stats['bytes'] / 1024 / 1024:.1f} MB）:",
                ),
            )
            return

        if action in ("search", "find"):
            query = " ".join(rest).strip()
            if not query:
                await self.bot.send_text(event, "用法: /sticker search <关键词>")
                return
            rows = store.search(query, limit=20)
            await self.bot.send_text(
                event,
                self._render_stickers(rows, f"搜索「{query}」:", empty="没有匹配的表情。"),
            )
            return

        if action in ("show", "info"):
            record = store.get(int(rest[0])) if rest and rest[0].isdigit() else None
            if record is None:
                await self.bot.send_text(event, "用法: /sticker show <id>")
                return
            await self.bot.send_text(
                event,
                f"#{record.id}  {'禁用' if record.status == 'banned' else '可用'}\n"
                f"描述: {record.description or '-'}\n"
                f"标签: {', '.join(record.tags) or '-'}\n"
                f"大小: {record.size / 1024:.0f} KB  {record.mime or '-'}\n"
                f"来源: {record.source_kind} {record.source or '-'}\n"
                f"使用: {record.uses} 次\n"
                f"文件: {record.path}",
            )
            return

        if action in ("delete", "rm", "remove"):
            record = store.get(int(rest[0])) if rest and rest[0].isdigit() else None
            if record is None:
                await self.bot.send_text(event, "用法: /sticker delete <id>")
                return
            store.delete(record.id)
            await self.bot.send_text(
                event, f"已删除 #{record.id}（剩 {store.count()} 张）"
            )
            return

        if action in ("ban", "unban"):
            record = store.get(int(rest[0])) if rest and rest[0].isdigit() else None
            if record is None:
                await self.bot.send_text(event, f"用法: /sticker {action} <id>")
                return
            status = "banned" if action == "ban" else "ok"
            try:
                store.set_status(record.id, status)
            except ValueError as exc:
                await self.bot.send_text(event, str(exc))
                return
            await self.bot.send_text(
                event, f"#{record.id} 已{'禁用' if status == 'banned' else '恢复'}"
            )
            return

        if action == "stats":
            stats = store.stats()
            await self.bot.send_text(
                event,
                f"表情库: {stats['count']} 张，禁用 {stats['banned']} 张，"
                f"占用 {stats['bytes'] / 1024 / 1024:.1f} MB",
            )
            return

        if action == "send":
            await self._send_sticker(event, rest)
            return

        if action == "tag":
            await self._tag_sticker(event, rest)
            return

        if action == "import":
            await self._import_stickers(event)
            return

        if action == "add":
            await self._add_sticker(event)
            return

        if action in ("help", "?"):
            await self.bot.send_text(event, STICKER_HELP)
            return

        await self.bot.send_text(event, f"未知表情库命令: {action}\n{STICKER_HELP}")

    async def _add_sticker(self, event: Any) -> None:
        store = self.bot.stickers
        images = getattr(event, "images", ())
        if not images:
            await self.bot.send_text(
                event, "用法：把表情图片作为图片发给机器人，并附上 /sticker add"
            )
            return
        try:
            data, mime = await download_image(
                images[0], max_bytes=self.bot.config.stickers.max_bytes
            )
            record, created = await asyncio.to_thread(
                store.add_bytes,
                data,
                mime=mime,
                source=f"admin:{event.user_id}",
                source_kind="admin",
                max_bytes=self.bot.config.stickers.max_bytes,
            )
        except (StickerError, OSError, sqlite3.Error) as exc:
            await self.bot.send_text(event, f"收藏失败: {exc}")
            return
        store.evict(self.bot.config.stickers.max_stored)
        state = "已收藏" if created else "库里已经有这张"
        await self.bot.send_text(event, f"{state}: #{record.id}（共 {store.count()} 张）")

    async def _send_sticker(self, event: Any, args: list[str]) -> None:
        store = self.bot.stickers
        if args and args[0].isdigit():
            record = store.get(int(args[0]))
        else:
            record = store.pick(" ".join(args)) if args else None
        if record is None:
            await self.bot.send_text(event, "用法: /sticker send <id|标签>")
            return
        if record.status != "ok":
            await self.bot.send_text(
                event, f"#{record.id} 已禁用，先 /sticker unban {record.id}"
            )
            return
        try:
            data = await asyncio.to_thread(Path(record.path).read_bytes)
        except OSError as exc:
            await self.bot.send_text(event, f"读取表情失败: {exc}")
            return
        file = "base64://" + base64.b64encode(data).decode("ascii")
        try:
            in_group = (
                getattr(event, "message_type", "private") == "group"
                and getattr(event, "group_id", None) is not None
            )
            if in_group:
                await self.bot.onebot.send_group_image(event.group_id, file)
            else:
                await self.bot.onebot.send_private_image(event.user_id, file)
        except OneBotError as exc:
            await self.bot.send_text(event, f"发送失败: {exc}")
            return
        store.mark_used(record.id)
        store.evict(self.bot.config.stickers.max_stored)
        if not in_group:
            await self.bot.send_text(event, f"已发送 #{record.id} {record.description}")

    async def _tag_sticker(self, event: Any, args: list[str]) -> None:
        store = self.bot.stickers
        record = store.get(int(args[0])) if args and args[0].isdigit() else None
        if record is None or len(args) < 2:
            await self.bot.send_text(event, "用法: /sticker tag <id> <描述> [标签,标签]")
            return
        body = " ".join(args[1:]).strip()
        description, _, raw_tags = body.partition("|")
        tags = tuple(tag.strip() for tag in raw_tags.replace("，", ",").split(",") if tag.strip())
        try:
            updated = store.describe(record.id, description.strip(), tags)
        except (ValueError, sqlite3.Error) as exc:
            await self.bot.send_text(event, f"写入失败: {exc}")
            return
        await self.bot.send_text(
            event,
            f"#{updated.id} 描述: {updated.description or '-'}  "
            f"标签: {', '.join(updated.tags) or '-'}",
        )

    async def _import_stickers(self, event: Any) -> None:
        store = self.bot.stickers
        try:
            data = await self.bot.onebot.call("fetch_custom_face", {"count": 50})
        except OneBotError as exc:
            await self.bot.send_text(event, f"读取 QQ 收藏表情失败: {exc}")
            return
        urls = (
            [url for url in data if isinstance(url, str) and url.startswith("http")]
            if isinstance(data, list)
            else []
        )
        if not urls:
            await self.bot.send_text(event, "QQ 收藏里没有表情。")
            return
        added = skipped = failed = 0
        for url in urls[:50]:
            try:
                raw, mime = await download_image(
                    url, max_bytes=self.bot.config.stickers.max_bytes
                )
                _record, created = await asyncio.to_thread(
                    store.add_bytes,
                    raw,
                    mime=mime,
                    source="qq-favorites",
                    source_kind="custom_face",
                    max_bytes=self.bot.config.stickers.max_bytes,
                )
            except (StickerError, OSError, sqlite3.Error):
                failed += 1
                continue
            if created:
                added += 1
            else:
                skipped += 1
        store.evict(self.bot.config.stickers.max_stored)
        await self.bot.send_text(
            event,
            f"导入完成：新增 {added}，已存在 {skipped}，失败 {failed}。"
            f"库中共 {store.count()} 张。",
        )

    @staticmethod
    def _render_stickers(
        rows: list[Any],
        title: str,
        empty: str = "表情库是空的。群里发表情会自动收藏，或用 /sticker import。",
    ) -> str:
        if not rows:
            return empty
        lines = [title]
        for record in rows:
            state = " [禁用]" if record.status == "banned" else ""
            desc = f" {record.description}" if record.description else ""
            lines.append(
                f"#{record.id} {record.size / 1024:.0f}KB "
                f"{record.source_kind}{state}{desc}"
            )
        return "\n".join(lines)

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
