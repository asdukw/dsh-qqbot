"""面板音色目录与各 provider 的默认连接参数。

面板目前只上架两个 Fish Audio 音色（菲比、流萤），其余 84 个中文音色见
``docs/voices.md``。目录只含公开音色元数据，不含任何密钥；provider 默认值用于
面板切换时补齐连接参数，避免运维再手工改 config.toml。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

PROVIDERS = ("edge", "fish", "stepfun")

PROVIDER_LABELS = {
    "edge": "Edge TTS",
    "fish": "Fish Audio",
    "stepfun": "StepFun",
}

# 切换 provider 时使用的连接默认值；config.toml 仍是未切换时的来源。
PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "edge": {"model": "", "api_key_env": "", "base_url": ""},
    "fish": {
        "model": "s2.1-pro-free",
        "api_key_env": "FISH_API_KEY",
        "base_url": "https://api.fish.audio",
    },
    "stepfun": {
        "model": "stepaudio-3-tts",
        "api_key_env": "STEPFUN_API_KEY",
        "base_url": "https://api.stepfun.com",
    },
}

VOICE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
ENV_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
MAX_NAME = 80

# (reference_id, 显示名, 性别, 标签, 热度)。两个都是 Fish Audio 公开中文音色，
# 可用 s2.1-pro-free 免费合成；切回面板前请先在「音色」页试听。
_FEATURED_VOICES = (
    (
        "8e80c3dcdf894213a10b48e9b08ad8c6",
        "菲比",
        "female",
        "female / young / character-voice / gentle / friendly / expressive / anime / Mandarin",
        34,
    ),
    (
        "79b9954c15f24deeab2319ec5f44f3b0",
        "流萤",
        "female",
        "female / young / narration / calm / smooth / professional / Mandarin",
        196,
    ),
)


def validate_voice_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("音色 id 无效")
    voice = value.strip()
    if not VOICE_ID_RE.fullmatch(voice):
        raise ValueError("音色 id 只能包含字母、数字和 . _ : -，且不超过 128 字符")
    return voice


def validate_name(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("音色名称无效")
    name = " ".join(value.split())
    if len(name) > MAX_NAME or any(ord(char) < 32 for char in name):
        raise ValueError(f"音色名称不能超过 {MAX_NAME} 字符或包含控制字符")
    return name


def validate_provider(value: Any) -> str:
    provider = value.strip().lower() if isinstance(value, str) else ""
    if provider not in PROVIDER_DEFAULTS:
        raise ValueError("未知的语音 provider")
    return provider


def validate_connection(provider: str, *, model: Any, api_key_env: Any, base_url: Any) -> dict[str, str]:
    """校验落盘的连接字段，避免面板之外的进程写入任意 URL / 环境变量名。"""
    if not isinstance(model, str) or len(model) > 100 or any(ord(c) < 32 for c in model):
        raise ValueError("语音模型名无效")
    if api_key_env not in ("", None) and (
        not isinstance(api_key_env, str) or not ENV_NAME_RE.fullmatch(api_key_env)
    ):
        raise ValueError("api_key_env 不是合法的环境变量名")
    if base_url not in ("", None):
        if not isinstance(base_url, str) or len(base_url) > 200:
            raise ValueError("base_url 无效")
        parsed = urlparse(base_url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("base_url 必须是不含凭据和查询串的 http(s) 地址")
    if provider == "edge":
        # edge-tts 不使用连接参数，避免让旧值误导面板。
        return {"model": "", "api_key_env": "", "base_url": ""}
    return {
        "model": model.strip() if isinstance(model, str) else "",
        "api_key_env": str(api_key_env or ""),
        "base_url": str(base_url or ""),
    }


def provider_defaults(provider: str) -> dict[str, str]:
    return dict(PROVIDER_DEFAULTS[validate_provider(provider)])


def catalog() -> list[dict[str, Any]]:
    return [
        {
            "provider": "fish",
            "id": voice,
            "name": name,
            "gender": gender,
            "tags": tags,
            "likes": likes,
        }
        for voice, name, gender, tags, likes in _FEATURED_VOICES
    ]


def display_name(provider: str, voice: str) -> str:
    for entry in catalog():
        if entry["provider"] == provider and entry["id"] == voice:
            return str(entry["name"])
    return ""
