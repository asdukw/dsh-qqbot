"""Fetch character card bytes over HTTP for admin installs.

``httpx`` is imported lazily so the rest of the bot (and the unit tests) keep
working in environments where only the stdlib is installed.
"""

from __future__ import annotations

import base64
import binascii

MAX_CARD_BYTES = 12 * 1024 * 1024
DEFAULT_TIMEOUT = 30.0
_SIZE_HINT = f"{MAX_CARD_BYTES // 1024 // 1024} MB"


class CardFetchError(RuntimeError):
    pass


def _decode_data_uri(source: str) -> bytes:
    header, sep, payload = source.partition(",")
    if not sep or ";base64" not in header:
        raise CardFetchError("unsupported data URI (expected base64)")
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CardFetchError(f"invalid data URI: {exc}") from exc
    if len(data) > MAX_CARD_BYTES:
        raise CardFetchError(f"card file is larger than {_SIZE_HINT}")
    return data


async def fetch_card_bytes(source: str, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    """Download a card from http(s), or decode a ``data:`` URI."""
    if source.startswith("data:"):
        return _decode_data_uri(source)
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - httpx is a runtime dependency
        raise CardFetchError("httpx is not installed") from exc
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0), follow_redirects=True
        ) as client:
            async with client.stream("GET", source) as response:
                if response.status_code != 200:
                    raise CardFetchError(f"HTTP {response.status_code} from {source}")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_CARD_BYTES:
                        raise CardFetchError(f"card file is larger than {_SIZE_HINT}")
                    chunks.append(chunk)
                return b"".join(chunks)
    except CardFetchError:
        raise
    except Exception as exc:  # noqa: BLE001 - network failures come in many shapes
        raise CardFetchError(f"cannot download {source}: {exc}") from exc


def fetch_card_bytes_sync(source: str, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    """Blocking variant for command-line use."""
    if source.startswith("data:"):
        return _decode_data_uri(source)
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover
        raise CardFetchError("httpx is not installed") from exc
    try:
        with httpx.Client(
            timeout=httpx.Timeout(timeout, connect=10.0), follow_redirects=True
        ) as client:
            with client.stream("GET", source) as response:
                if response.status_code != 200:
                    raise CardFetchError(f"HTTP {response.status_code} from {source}")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_CARD_BYTES:
                        raise CardFetchError(f"card file is larger than {_SIZE_HINT}")
                    chunks.append(chunk)
                return b"".join(chunks)
    except CardFetchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CardFetchError(f"cannot download {source}: {exc}") from exc
