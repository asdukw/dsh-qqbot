"""Minimal chat-completions client with hard-coded OpenAI-compatible providers."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class Provider:
    base_url: str
    api_key_env: str
    default_model: str = ""


# Only OpenAI Chat Completions compatible endpoints belong here.
# Model defaults are filled in only where the provider's naming is stable;
# otherwise set LLM_MODEL (or model = "..." in config.toml).
PROVIDERS: dict[str, Provider] = {
    "opencode": Provider(
        "https://opencode.ai/inference/openai/v1",
        "OPENCODE_API_KEY",
        "deepseek-v4.1-flash",
    ),
    "opencode-go": Provider(
        "https://opencode.ai/zen/go/v1",
        "OPENCODE_API_KEY",
        "deepseek-v4.1-flash",
    ),
    "deepseek": Provider(
        "https://api.deepseek.com",
        "DEEPSEEK_API_KEY",
        "deepseek-flash",
    ),
    "openai": Provider("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "openrouter": Provider("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "siliconflow": Provider("https://api.siliconflow.cn/v1", "SILICONFLOW_API_KEY"),
    "dashscope": Provider(
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "DASHSCOPE_API_KEY",
    ),
    "moonshot": Provider("https://api.moonshot.cn/v1", "MOONSHOT_API_KEY"),
    "zhipu": Provider("https://open.bigmodel.cn/api/paas/v4", "ZHIPU_API_KEY"),
    "custom": Provider("", "LLM_API_KEY"),
    # Local test route used by dsh/fake_llm.py.
    "fake": Provider("http://127.0.0.1:8000/v1", "FAKE_KEY", "fake-model"),
}


def get_provider(name: str) -> Provider:
    provider = PROVIDERS.get(name.strip().lower())
    if provider is None:
        known = ", ".join(sorted(PROVIDERS))
        raise ValueError(f"unknown LLM provider {name!r}; choose one of: {known}")
    return provider


@dataclass
class LLMConfig:
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    provider: str = "opencode"
    max_tokens: int = 800
    temperature: float = 1.0
    timeout: float = 60.0
    thinking: str = ""          # "", "enabled", "disabled"
    reasoning_effort: str = ""  # "", "low", "medium", "high"


class LLMClient:
    def __init__(self, config: LLMConfig):
        if not config.base_url:
            raise LLMError("LLM base_url is empty")
        if not config.model:
            raise LLMError("LLM model is empty (set LLM_MODEL or model in config.toml)")
        self.config = config
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        if config.provider in ("opencode", "opencode-go"):
            # OpenCode Go requires a stable session id for routing.
            headers["x-opencode-session"] = "qqbot-fallback"
        self._client = httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            timeout=httpx.Timeout(config.timeout, connect=10.0),
            headers=headers,
        )

    def _payload(self, messages: list[dict[str, Any]]) -> dict:
        payload: dict = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "stream": False,
        }
        if self.config.thinking in ("enabled", "disabled"):
            payload["thinking"] = {"type": self.config.thinking}
        if self.config.reasoning_effort:
            payload["reasoning_effort"] = self.config.reasoning_effort
        return payload

    async def chat(self, messages: list[dict[str, Any]]) -> str:
        payload = self._payload(messages)
        last_error: Exception | None = None

        for attempt in range(3):
            try:
                response = await self._client.post("/chat/completions", json=payload)
            except httpx.TimeoutException:
                last_error = LLMError("LLM request timed out")
                log.warning("LLM timeout (attempt %d/3)", attempt + 1)
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            except httpx.HTTPError as exc:
                last_error = LLMError(f"LLM connection error: {exc}")
                log.warning("LLM connection error: %s", exc)
                await asyncio.sleep(1.5 * (attempt + 1))
                continue

            if response.status_code == 429 or response.status_code >= 500:
                last_error = LLMError(f"LLM HTTP {response.status_code}: {response.text[:300]}")
                log.warning("LLM HTTP %s (attempt %d/3), retrying", response.status_code, attempt + 1)
                await asyncio.sleep(2.0 * (attempt + 1))
                continue

            if response.status_code >= 400:
                raise LLMError(f"LLM HTTP {response.status_code}: {response.text[:500]}")

            try:
                body = response.json()
                content = body["choices"][0]["message"].get("content") or ""
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise LLMError(f"unexpected LLM response: {response.text[:300]}") from exc

            content = content.strip()
            if not content:
                raise LLMError("LLM returned an empty reply")
            return content

        raise last_error or LLMError("LLM request failed")

    async def close(self) -> None:
        await self._client.aclose()
