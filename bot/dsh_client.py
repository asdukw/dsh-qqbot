"""Thin wrapper around the DeepSeek Harness Python SDK.

The DSH runtime owns session history, compaction, file tools, and the
user-profile plugin. The adapter only maps a QQ conversation to a session id
and passes the user's text through.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

try:
    from deepseek_harness import DeepSeekHarness
except ImportError:  # pragma: no cover - reported at runtime
    DeepSeekHarness = None  # type: ignore[assignment]


class DSHError(RuntimeError):
    pass


@dataclass
class DSHConfig:
    enabled: bool = True
    home: str = "/var/lib/qqbot/dsh-home"
    workspace: str = "/var/lib/qqbot/dsh-workspace"
    profile: str = "qqbot"
    provider: str = "opencode"
    model: str = "deepseek-v4.1-flash"
    request_timeout: float = 120.0


def _turn_error(events: Any) -> str:
    for event in reversed(events or []):
        if not isinstance(event, dict) or event.get("type") != "turn/end":
            continue
        reason = event.get("data", {}).get("reason") or {}
        if reason.get("kind") != "error":
            continue
        error = reason.get("error") or {}
        code = error.get("code") or "ERROR"
        message = error.get("message") or "unknown error"
        return f"{code}: {message}"
    return ""


class DSHClient:
    def __init__(self, config: DSHConfig, system_prompt: str = ""):
        self.config = config
        self._system_prompt = system_prompt
        self._harness: Any = None
        self._sessions: set[str] = set()
        self._lock = threading.Lock()

    def set_system_prompt(self, prompt: str) -> None:
        """Restart the runtime if the card persona changed.

        DSH reads DSH_SYSTEM_PROMPT when the child process starts. Sessions are
        persisted on disk, so a restart keeps conversation history.
        """
        prompt = prompt or ""
        with self._lock:
            if prompt == self._system_prompt:
                return
            self._system_prompt = prompt
            self._reset_locked()

    def _reset_locked(self) -> None:
        if self._harness is not None:
            try:
                self._harness.close()
            except Exception:  # noqa: BLE001 - best effort during restart
                log.exception("error closing DSH runtime")
            self._harness = None
            self._sessions.clear()

    def _ensure_locked(self):
        if self._harness is not None:
            return self._harness
        if DeepSeekHarness is None:
            raise DSHError("deepseek-harness-sdk is not installed in this venv")

        os.environ["DSH_SYSTEM_PROMPT"] = self._system_prompt
        os.environ.setdefault("DSH_HOME", self.config.home)
        log.info(
            "starting DSH runtime (home=%s profile=%s provider=%s model=%s)",
            self.config.home, self.config.profile, self.config.provider, self.config.model,
        )
        try:
            self._harness = DeepSeekHarness(
                dsh_home=self.config.home,
                cwd=self.config.workspace,
                profile=self.config.profile,
                provider=self.config.provider,
                model=self.config.model,
                request_timeout_seconds=self.config.request_timeout,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the bot
            raise DSHError(f"cannot start DSH runtime: {exc}") from exc
        return self._harness

    def _run_locked(self, harness: Any, session_id: str, text: str) -> Any:
        if session_id in self._sessions:
            return harness.run(text, session_id=session_id)
        try:
            result = harness.run(text, session_id=session_id)
        except Exception as exc:  # noqa: BLE001 - resume an existing session
            if "already exists" not in str(exc).lower():
                raise
            log.info("resuming existing DSH session %s", session_id)
            session = harness.resume_session(session_id)
            result = session.run(text)
        self._sessions.add(session_id)
        return result

    def run(self, session_id: str, text: str) -> str:
        with self._lock:
            harness = self._ensure_locked()
            try:
                result = self._run_locked(harness, session_id, text)
            except Exception as exc:  # noqa: BLE001 - reset for the next turn
                log.exception("DSH run failed")
                self._reset_locked()
                raise DSHError(f"DSH run failed: {exc}") from exc

        reply = (getattr(result, "final_response", "") or "").strip()
        if reply:
            return reply
        reason = _turn_error(getattr(result, "events", None))
        finish = getattr(result, "finish_reason", None)
        raise DSHError(reason or f"DSH turn ended without a reply ({finish or 'no reason'})")

    def close(self) -> None:
        with self._lock:
            self._reset_locked()
