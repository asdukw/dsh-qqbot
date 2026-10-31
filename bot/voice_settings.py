"""面板在线切换的音色覆盖值。

bot 和 admin 是独立进程，共用 ``/var/lib/qqbot`` 下的 JSON 文件：
admin 写入后，bot 在下一条语音回复前读取，因此不需要重启。文件不存在表示沿用
config.toml 的 ``[voice]``。写入是临时文件 + ``os.replace`` 的原子替换，文件权限
0600；读取拒绝符号链接并校验全部字段。

``voice-probability.json`` 单独保存「未标记 [语音] 的回复转成语音」的概率覆盖值，
与音色选择互不影响；清除它即回到 config.toml 的默认值。
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from voice_catalog import validate_connection, validate_name, validate_provider, validate_voice_id

DEFAULT_STATE_FILE = "/var/lib/qqbot/voice-state.json"
DEFAULT_PROBABILITY_FILE = "/var/lib/qqbot/voice-probability.json"
MAX_BYTES = 64 * 1024
_LOCK = threading.RLock()


class VoiceSettingsError(ValueError):
    """面板写入的音色状态无效或不可读。"""


@dataclass(frozen=True)
class VoiceSelection:
    provider: str
    voice: str
    name: str
    model: str
    api_key_env: str
    base_url: str
    updated_at: int = 0


@dataclass(frozen=True)
class VoiceProbability:
    probability: float
    updated_at: int = 0


def state_path(settings: Mapping[str, Any] | None) -> str:
    """从 config.toml 的 [voice] 取状态文件路径。"""
    raw = (settings or {}).get("state_file")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return DEFAULT_STATE_FILE


def probability_path(settings: Mapping[str, Any] | None) -> str:
    """从 config.toml 的 [voice] 取概率覆盖文件路径。"""
    raw = (settings or {}).get("probability_file")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return DEFAULT_PROBABILITY_FILE


def _reject_symlink(path: Path) -> None:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise VoiceSettingsError("音色状态文件不能是符号链接")


def _read_bytes(path: Path) -> bytes | None:
    try:
        _reject_symlink(path)
        if not path.exists():
            return None
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise VoiceSettingsError("音色状态文件类型无效")
            raw = stream.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    except VoiceSettingsError:
        raise
    except OSError:
        raise VoiceSettingsError("音色状态文件不可读") from None
    if len(raw) > MAX_BYTES:
        raise VoiceSettingsError("音色状态文件过大")
    return raw


def _write_bytes(path: Path, body: bytes) -> None:
    with _LOCK:
        _reject_symlink(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".voice-", dir=path.parent)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def _remove(path: Path) -> None:
    with _LOCK:
        try:
            _reject_symlink(path)
        except VoiceSettingsError:
            # 符号链接本身可以直接删除，删除不会跟随到目标。
            if not path.is_symlink():
                raise
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            raise VoiceSettingsError("音色状态文件无法删除") from None


def _parse(state: Any) -> VoiceSelection:
    if not isinstance(state, dict) or state.get("schema") != 1:
        raise VoiceSettingsError("音色状态格式无效")
    updated_at = state.get("updated_at")
    if type(updated_at) is not int or updated_at < 0:
        raise VoiceSettingsError("音色状态时间戳无效")
    try:
        provider = validate_provider(state.get("provider"))
        voice = validate_voice_id(state.get("voice"))
        name = validate_name(state.get("name"))
        connection = validate_connection(
            provider,
            model=state.get("model"),
            api_key_env=state.get("api_key_env"),
            base_url=state.get("base_url"),
        )
    except ValueError as exc:
        raise VoiceSettingsError(str(exc)) from None
    if provider != "edge" and (not connection["model"] or not connection["base_url"]):
        raise VoiceSettingsError("音色状态缺少模型或 base_url")
    return VoiceSelection(provider=provider, voice=voice, name=name, updated_at=updated_at, **connection)


def _parse_probability(state: Any) -> VoiceProbability:
    if not isinstance(state, dict) or state.get("schema") != 1:
        raise VoiceSettingsError("语音概率状态格式无效")
    updated_at = state.get("updated_at")
    if type(updated_at) is not int or updated_at < 0:
        raise VoiceSettingsError("语音概率时间戳无效")
    value = state.get("probability")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VoiceSettingsError("语音概率必须是数字")
    probability = float(value)
    if not 0.0 <= probability <= 1.0:
        raise VoiceSettingsError("语音概率必须在 0 到 1 之间")
    return VoiceProbability(probability=probability, updated_at=updated_at)


class VoiceStore:
    """一个状态文件的读写入口；admin 和 bot 各自构造，互不加锁。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def active(self) -> VoiceSelection | None:
        raw = _read_bytes(self.path)
        if raw is None:
            return None
        try:
            return _parse(json.loads(raw))
        except (ValueError, TypeError):
            raise VoiceSettingsError("音色状态不是合法 JSON") from None

    def save(self, selection: VoiceSelection) -> VoiceSelection:
        record = _parse(
            {
                "schema": 1,
                "updated_at": int(time.time()),
                "provider": selection.provider,
                "voice": selection.voice,
                "name": selection.name,
                "model": selection.model,
                "api_key_env": selection.api_key_env,
                "base_url": selection.base_url,
            }
        )
        body = json.dumps(
            {
                "schema": 1,
                "updated_at": record.updated_at,
                "provider": record.provider,
                "voice": record.voice,
                "name": record.name,
                "model": record.model,
                "api_key_env": record.api_key_env,
                "base_url": record.base_url,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        _write_bytes(self.path, body)
        return record

    def clear(self) -> None:
        _remove(self.path)


class ProbabilityStore:
    """语音概率覆盖值的读写入口；schema 与音色状态文件独立。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def get(self) -> VoiceProbability | None:
        raw = _read_bytes(self.path)
        if raw is None:
            return None
        try:
            return _parse_probability(json.loads(raw))
        except (ValueError, TypeError):
            raise VoiceSettingsError("语音概率状态不是合法 JSON") from None

    def save(self, probability: float) -> VoiceProbability:
        record = _parse_probability(
            {
                "schema": 1,
                "updated_at": int(time.time()),
                "probability": probability,
            }
        )
        body = json.dumps(
            {
                "schema": 1,
                "updated_at": record.updated_at,
                "probability": record.probability,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        _write_bytes(self.path, body)
        return record

    def clear(self) -> None:
        _remove(self.path)
