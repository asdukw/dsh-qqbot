"""Admin-only chat commands. Commands never enter model history."""

from __future__ import annotations

import logging
import time
from typing import Any

from models import ModelRecord, ModelRegistry

log = logging.getLogger(__name__)

HELP = """可用命令:
/model          列出模型
/model <序号|model_id|provider/model>  切换模型
/proactive [on|off|status|test]        群聊主动回复
/help           显示帮助"""


class CommandRouter:
    def __init__(self, bot: Any):
        self.bot = bot

    def is_command(self, text: str) -> bool:
        return text.startswith("/")

    async def handle(self, event: Any, text: str) -> None:
        if not self.bot.is_admin(event.user_id):
            log.info("ignoring command from non-admin %s: %.80s", event.user_id, text)
            return
        parts = text.strip().split()
        name = parts[0].lower()
        if name in ("/help", "/?"):
            await self.bot.send_text(event, HELP)
        elif name in ("/model", "/models"):
            await self._model(event, parts[1:])
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
