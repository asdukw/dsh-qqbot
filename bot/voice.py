"""Text-to-speech replies for the persona bot (edge-tts, free).

The model asks for a voice reply with a trailing "[语音]" marker; the adapter
renders the visible text to mp3 and sends it as a QQ voice message. NapCat
accepts base64 mp3 in a `record` segment (verified against the live instance).
"""

from __future__ import annotations

import logging
import os
import re

import httpx

log = logging.getLogger(__name__)

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
DEFAULT_FISH_MODEL = "s2.1-pro-free"
DEFAULT_STEPFUN_MODEL = "stepaudio-3-tts"
STEPFUN_INSTRUCTION_MODELS = ("stepaudio-2.5-tts", "stepaudio-3-tts")
MAX_CHARS = 1000  # provider-side safety cap

_VOICE_RE = re.compile(r"\[语音\]")
_STAGE_RE = re.compile(r"\*[^*]*\*|（[^）]*）|\([^)]*\)")


class VoiceError(RuntimeError):
    pass


def spoken_text(text: str) -> str:
    """Drop stage directions (*...*, （...）) before reading text aloud."""
    cleaned = _STAGE_RE.sub(" ", text or "")
    return " ".join(cleaned.split())


def extract_voice_marker(text: str) -> tuple[str, bool]:
    """Split a "[语音]" marker out of a reply; return (visible, wants_voice)."""
    if not text or "[语音]" not in text:
        return text, False
    return _VOICE_RE.sub("", text, count=1).strip(), True


async def synthesize_stepfun(
    text: str,
    *,
    voice: str,
    model: str = DEFAULT_STEPFUN_MODEL,
    api_key_env: str = "STEPFUN_API_KEY",
    base_url: str = "https://api.stepfun.com",
    instruction: str = "",
    speed: float = 1.0,
    timeout: float = 60.0,
) -> bytes:
    """Render text with StepFun's /v1/audio/speech (Chinese voices)."""
    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key:
        raise VoiceError(f"{api_key_env} is not set")
    if not (voice or "").strip():
        raise VoiceError("stepfun voice is not configured")
    text = " ".join((text or "").split())
    if not text:
        raise VoiceError("empty text")
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
    payload: dict = {
        "model": model,
        "input": text,
        "voice": voice.strip(),
        "response_format": "mp3",
        "sample_rate": 24000,
        "text_normalization": "enhanced",
    }
    if speed and speed != 1.0:
        payload["speed"] = max(0.5, min(2.0, float(speed)))
    if instruction.strip() and model.startswith(STEPFUN_INSTRUCTION_MODELS):
        payload["instruction"] = instruction.strip()[:200]
    try:
        async with httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={"Authorization": f"Bearer {api_key}"},
        ) as client:
            response = await client.post("/v1/audio/speech", json=payload)
    except httpx.HTTPError as exc:
        raise VoiceError(f"stepfun tts failed: {exc}") from exc
    if response.status_code != 200:
        raise VoiceError(
            f"stepfun tts HTTP {response.status_code}: {response.text[:200]}"
        )
    if not response.content:
        raise VoiceError("stepfun tts returned no audio")
    return response.content


async def synthesize_fish(
    text: str,
    *,
    reference_id: str,
    model: str = DEFAULT_FISH_MODEL,
    api_key_env: str = "FISH_API_KEY",
    base_url: str = "https://api.fish.audio",
    timeout: float = 60.0,
) -> bytes:
    """Render text with Fish Audio's TTS (s2.1-pro-free works without credit)."""
    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key:
        raise VoiceError(f"{api_key_env} is not set")
    if not (reference_id or "").strip():
        raise VoiceError("fish voice reference_id is not configured")
    text = " ".join((text or "").split())
    if not text:
        raise VoiceError("empty text")
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
    try:
        async with httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={
                "Authorization": f"Bearer {api_key}",
                "model": model,
            },
        ) as client:
            response = await client.post(
                "/v1/tts",
                json={
                    "text": text,
                    "reference_id": reference_id.strip(),
                    "format": "mp3",
                    "mp3_bitrate": 128,
                    "latency": "normal",
                },
            )
    except httpx.HTTPError as exc:
        raise VoiceError(f"fish tts failed: {exc}") from exc
    if response.status_code != 200:
        raise VoiceError(
            f"fish tts HTTP {response.status_code}: {response.text[:200]}"
        )
    if not response.content:
        raise VoiceError("fish tts returned no audio")
    return response.content


async def synthesize(
    text: str,
    voice: str = DEFAULT_VOICE,
    rate: str = "+0%",
    volume: str = "+0%",
) -> bytes:
    """Render text to mp3 bytes with edge-tts."""
    text = " ".join((text or "").split())
    if not text:
        raise VoiceError("empty text")
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
    try:
        import edge_tts
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise VoiceError("edge-tts is not installed") from exc

    communicate = edge_tts.Communicate(text, voice=voice, rate=rate, volume=volume)
    chunks = bytearray()
    try:
        async for chunk in communicate.stream():
            audio = chunk.get("data")
            if chunk.get("type") == "audio" and audio:
                chunks.extend(audio)
    except Exception as exc:  # noqa: BLE001 - network/service errors
        raise VoiceError(f"tts failed: {exc}") from exc
    if not chunks:
        raise VoiceError("tts returned no audio")
    return bytes(chunks)
