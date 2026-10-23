"""Structured schedule payloads.

Schedule answers are the one place where free-form model prose is not good
enough: the adapter needs dates, teams and best-of to be machine-checkable.
The model is told to emit one fenced ```schedule block matching this schema;
the adapter parses it with Pydantic and renders a stable list from the
validated object. Anything that does not validate never reaches the chat.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field, field_validator

log = logging.getLogger(__name__)

BEIJING = timezone(timedelta(hours=8))

_FENCE_RE = re.compile(r"```(?:schedule|json)?[ \t]*\n(.*?)```", re.DOTALL)


class ScheduleMatch(BaseModel):
    """One upcoming match."""

    stage: str = ""
    start_time: datetime
    team_a: str = Field(min_length=1)
    team_b: str = Field(min_length=1)
    best_of: int | None = Field(default=None, ge=1, le=9)
    note: str = ""

    @field_validator("start_time")
    @classmethod
    def _assume_beijing(cls, value: datetime) -> datetime:
        # A naive timestamp from the model means Beijing time in this bot.
        return value if value.tzinfo is not None else value.replace(tzinfo=BEIJING)

    @field_validator("team_a", "team_b")
    @classmethod
    def _strip_teams(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("team name is empty")
        return value


class Schedule(BaseModel):
    """A tournament's upcoming matches."""

    event: str = ""
    matches: list[ScheduleMatch]

    @field_validator("matches")
    @classmethod
    def _sorted(cls, value: list[ScheduleMatch]) -> list[ScheduleMatch]:
        return sorted(value, key=lambda match: match.start_time)


def extract_schedule(text: str) -> tuple[str, Schedule | None]:
    """Split a ```schedule block out of a reply and validate it.

    Returns the reply with the block removed and the parsed Schedule (or None
    when there was no block or it failed validation).
    """
    if not text or "```" not in text:
        return text, None
    saw_broken_block = False
    for match in _FENCE_RE.finditer(text):
        payload = match.group(1).strip()
        if "matches" not in payload:
            continue
        saw_broken_block = True
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            log.warning("schedule block is not JSON: %s", exc)
            continue
        try:
            schedule = Schedule.model_validate(data)
        except Exception as exc:  # noqa: BLE001 - pydantic ValidationError
            log.warning("schedule block failed validation: %s", exc)
            continue
        if not schedule.matches:
            log.warning("schedule block has no matches")
            continue
        outer = (text[: match.start()] + text[match.end():]).strip()
        return outer, schedule
    if saw_broken_block:
        # Never leak a half-valid block into the chat; keep the prose around it.
        outer = _FENCE_RE.sub("", text).strip()
        return outer or "赛程解析失败，稍后再试。", None
    return text, None


def render_schedule(schedule: Schedule) -> str:
    """Render a validated schedule as a stable, source-free list."""
    title = schedule.event.strip() or "赛程"
    lines = [f"【{title}】"]
    for match in schedule.matches:
        when = match.start_time.astimezone(BEIJING).strftime("%m-%d %H:%M")
        stage = f"{match.stage} ｜ " if match.stage else ""
        best_of = f" ｜ BO{match.best_of}" if match.best_of else ""
        note = f"（{match.note}）" if match.note else ""
        lines.append(
            f"- {stage}{when} ｜ {match.team_a} vs {match.team_b}{best_of}{note}"
        )
    return "\n".join(lines)
