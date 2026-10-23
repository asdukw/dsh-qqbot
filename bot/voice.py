"""Text-to-speech replies for the persona bot (edge-tts, free).

The model asks for a voice reply with a trailing "[语音]" marker; the adapter
renders the visible text to mp3 and sends it as a QQ voice message. NapCat
accepts base64 mp3 in a `record` segment (verified against the live instance).
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
MAX_CHARS = 80

_VOICE_RE = re.compile(r"\[语音\]")


class VoiceError(RuntimeError):
    pass


def extract_voice_marker(text: str) -> tuple[str, bool]:
    """Split a "[语音]" marker out of a reply; return (visible, wants_voice)."""
    if not text or "[语音]" not in text:
        return text, False
    return _VOICE_RE.sub("", text, count=1).strip(), True


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
            if chunk.get("type") == "audio" and chunk.get("data"):
                chunks.extend(chunk["data"])
    except Exception as exc:  # noqa: BLE001 - network/service errors
        raise VoiceError(f"tts failed: {exc}") from exc
    if not chunks:
        raise VoiceError("tts returned no audio")
    return bytes(chunks)
