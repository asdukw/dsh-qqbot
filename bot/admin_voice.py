"""面板的音色目录、在线切换与试听。

面板只写 ``/var/lib/qqbot/voice-state.json``；bot 在下一条语音回复前读取该文件，
所以切换不需要重启服务。试听走与实际回复相同的 provider 参数，正文固定，避免面板
变成任意文本的 TTS 入口。
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import asdict
from typing import Any

from voice import (
    DEFAULT_VOICE,
    VoiceError,
    synthesize,
    synthesize_fish,
    synthesize_stepfun,
)
from voice_catalog import (
    PROVIDER_LABELS,
    catalog,
    display_name,
    provider_defaults,
    validate_connection,
    validate_name,
    validate_provider,
    validate_voice_id,
)
from voice_settings import VoiceSelection, VoiceStore, state_path

SAMPLE_TEXT = "你好呀，听听这个音色的感觉怎么样？"
PREVIEW_TIMEOUT = 25.0
PREVIEW_MAX_BYTES = 2 * 1024 * 1024


def _config_voice(config: dict[str, Any]) -> dict[str, Any]:
    """config.toml [voice] 的字段，与 bot/main.py 的解析保持同一套默认值。"""
    raw = config.get("voice") or {}
    provider = str(raw.get("provider", "edge")).lower()
    if provider not in PROVIDER_LABELS:
        provider = "edge"
    try:
        speed = float(raw.get("speed", 1.0))
    except (TypeError, ValueError):
        speed = 1.0
    return {
        "provider": provider,
        "voice": str(raw.get("voice", DEFAULT_VOICE)),
        "name": "",
        "model": str(raw.get("model", "stepaudio-3-tts")),
        "api_key_env": str(raw.get("api_key_env", "STEPFUN_API_KEY")),
        "base_url": str(raw.get("base_url", "https://api.stepfun.com")),
        "rate": str(raw.get("rate", "+0%")),
        "instruction": str(raw.get("instruction", "")),
        "speed": speed,
    }


def _resolved(config: dict[str, Any]) -> tuple[dict[str, Any], str, int | None]:
    """返回 (生效值, "panel"|"config", 面板更新时间)。"""
    entry = _config_voice(config)
    selection = VoiceStore(state_path(config.get("voice"))).active()
    if selection is None:
        entry["name"] = display_name(entry["provider"], entry["voice"])
        return entry, "config", None
    entry.update(
        provider=selection.provider,
        voice=selection.voice,
        name=selection.name,
        model=selection.model,
        api_key_env=selection.api_key_env,
        base_url=selection.base_url,
    )
    return entry, "panel", selection.updated_at


def _key_ready(provider: str, api_key_env: str) -> bool:
    if provider == "edge":
        return True
    return bool(api_key_env and os.environ.get(api_key_env, "").strip())


def _public(entry: dict[str, Any]) -> dict[str, Any]:
    provider = str(entry["provider"])
    api_key_env = str(entry.get("api_key_env") or "")
    return {
        "provider": provider,
        "voice": str(entry["voice"]),
        "name": str(entry.get("name") or ""),
        "model": str(entry.get("model") or ""),
        "api_key_env": api_key_env,
        "base_url": str(entry.get("base_url") or ""),
        "key_required": provider != "edge",
        "key_ready": _key_ready(provider, api_key_env),
    }


def _providers() -> list[dict[str, Any]]:
    rows = []
    for provider, label in PROVIDER_LABELS.items():
        defaults = provider_defaults(provider)
        rows.append(
            {
                "id": provider,
                "label": label,
                **defaults,
                "key_required": provider != "edge",
                "key_ready": _key_ready(provider, defaults["api_key_env"]),
            }
        )
    return rows


def voice_data(config: dict[str, Any]) -> dict[str, Any]:
    active, source, updated_at = _resolved(config)
    base = _config_voice(config)
    if source == "config":
        base["name"] = active["name"]
    return {
        "generated_at": int(time.time()),
        "enabled": bool((config.get("voice") or {}).get("enabled", True)),
        "source": source,
        "updated_at": updated_at,
        "state_file": state_path(config.get("voice")),
        "active": _public(active),
        "config_default": _public(base),
        "providers": _providers(),
        "catalog": catalog(),
    }


def _validate_payload(payload: Any) -> tuple[str, str, str]:
    if not isinstance(payload, dict):
        raise ValueError("请求格式无效")
    allowed = {"provider", "voice", "name"}
    if set(payload) - allowed:
        raise ValueError("请求包含未知字段")
    provider = validate_provider(payload.get("provider"))
    voice = validate_voice_id(payload.get("voice"))
    name = validate_name(payload.get("name"))
    return provider, voice, name


def _selection(config: dict[str, Any], provider: str, voice: str, name: str) -> VoiceSelection:
    """同一 provider 沿用当前连接，跨 provider 使用该 provider 的默认连接。"""
    current, _, _ = _resolved(config)
    if provider == current["provider"]:
        connection = validate_connection(
            provider,
            model=current.get("model"),
            api_key_env=current.get("api_key_env"),
            base_url=current.get("base_url"),
        )
    else:
        connection = provider_defaults(provider)
    return VoiceSelection(
        provider=provider,
        voice=voice,
        name=name or display_name(provider, voice) or voice,
        model=connection["model"],
        api_key_env=connection["api_key_env"],
        base_url=connection["base_url"],
    )


def apply_voice_action(config: dict[str, Any], payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("请求格式无效")
    store = VoiceStore(state_path(config.get("voice")))
    if payload.get("clear") is True:
        if set(payload) != {"clear"}:
            raise ValueError("请求包含未知字段")
        store.clear()
        return voice_data(config)
    if "clear" in payload:
        raise ValueError("clear 必须是 true")
    provider, voice, name = _validate_payload(payload)
    store.save(_selection(config, provider, voice, name))
    return voice_data(config)


async def _render(entry: dict[str, Any]) -> bytes:
    provider = entry["provider"]
    if provider == "fish":
        return await synthesize_fish(
            SAMPLE_TEXT,
            reference_id=entry["voice"],
            model=entry["model"],
            api_key_env=entry["api_key_env"],
            base_url=entry["base_url"],
            timeout=PREVIEW_TIMEOUT,
        )
    if provider == "stepfun":
        return await synthesize_stepfun(
            SAMPLE_TEXT,
            voice=entry["voice"],
            model=entry["model"],
            api_key_env=entry["api_key_env"],
            base_url=entry["base_url"],
            instruction=entry["instruction"],
            speed=entry["speed"],
            timeout=PREVIEW_TIMEOUT,
        )
    return await synthesize(SAMPLE_TEXT, voice=entry["voice"], rate=entry["rate"])


def preview_audio(config: dict[str, Any], payload: Any) -> bytes:
    """用当前或指定音色合成一小段试听音频。"""
    if not isinstance(payload, dict):
        raise ValueError("请求格式无效")
    if set(payload) - {"provider", "voice"}:
        raise ValueError("请求包含未知字段")
    if payload:
        provider, voice, _ = _validate_payload(payload)
        entry = asdict(_selection(config, provider, voice, ""))
    else:
        resolved, _, _ = _resolved(config)
        entry = dict(resolved)
    if entry["provider"] != "edge":
        if not entry.get("model") or not entry.get("base_url"):
            raise ValueError("该 provider 缺少模型或 base_url")
        if not _key_ready(entry["provider"], str(entry.get("api_key_env") or "")):
            raise ValueError(f"{entry.get('api_key_env') or 'API Key'} 未配置，无法试听")
    try:
        audio = asyncio.run(asyncio.wait_for(_render(entry), timeout=PREVIEW_TIMEOUT + 5))
    except VoiceError as exc:
        raise ValueError(str(exc)) from None
    except TimeoutError:
        raise ValueError("试听超时，请稍后再试") from None
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"试听失败：{type(exc).__name__}") from None
    if not audio:
        raise ValueError("试听没有返回音频")
    if len(audio) > PREVIEW_MAX_BYTES:
        raise ValueError("试听音频过大")
    return audio
