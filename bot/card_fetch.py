"""Bounded card fetching; autonomous requests additionally pin public DNS addresses."""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

MAX_CARD_BYTES = 12 * 1024 * 1024
MAX_PAGE_BYTES = 1024 * 1024
DEFAULT_TIMEOUT = 30.0
_SIZE_HINT = f"{MAX_CARD_BYTES // 1024 // 1024} MB"


class CardFetchError(RuntimeError):
    pass


def _decode_data_uri(source: str) -> bytes:
    header, sep, payload = source.partition(",")
    if not sep or ";base64" not in header:
        raise CardFetchError("unsupported data URI (expected base64)")
    if len(payload) > (MAX_CARD_BYTES + 2) // 3 * 4:
        raise CardFetchError(f"card file is larger than {_SIZE_HINT}")
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CardFetchError(f"invalid data URI: {exc}") from exc
    if len(data) > MAX_CARD_BYTES:
        raise CardFetchError(f"card file is larger than {_SIZE_HINT}")
    return data


async def fetch_card_bytes(source: str, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    """Legacy, explicit administrator URL/attachment import (including data URIs)."""
    if source.startswith("data:"):
        return _decode_data_uri(source)
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover
        raise CardFetchError("httpx is not installed") from exc
    try:
        async with asyncio.timeout(timeout), httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0), follow_redirects=True
        ) as client, client.stream("GET", source) as response:
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
    except Exception as exc:  # noqa: BLE001
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
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0), follow_redirects=True) as client:
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


@dataclass(frozen=True)
class RemoteCardContent:
    data: bytes
    url: str
    content_type: str


def public_url(source: str) -> str:
    """Validate syntax; DNS validation is repeated for every actual connection."""
    try:
        parts = urlsplit(source)
        if (len(source) > 4096 or parts.scheme != 'https' or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.port not in (None, 443) or '\\' in source
                or any(ord(c) <= 32 for c in source)):
            raise ValueError('invalid URL')
        host = parts.hostname.encode('idna').decode('ascii')
        if host.rstrip('.').lower() == 'localhost':
            raise ValueError('local host')
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            _public_ip(str(address))
        return source.split('#', 1)[0]
    except (ValueError, UnicodeError) as exc:
        raise CardFetchError('only public HTTPS URLs on port 443 are allowed') from exc


def _public_ip(value: str) -> str:
    address = ipaddress.ip_address(value)
    if (not address.is_global or address.is_multicast or address.is_unspecified
            or getattr(address, 'ipv4_mapped', None) is not None
            or getattr(address, 'sixtofour', None) is not None
            or getattr(address, 'teredo', None) is not None):
        raise CardFetchError('destination is not a public Internet address')
    return str(address)


async def _resolve_public(host: str) -> str:
    results = await asyncio.get_running_loop().getaddrinfo(
        host, 443, type=socket.SOCK_STREAM,
    )
    if not results:
        raise CardFetchError('destination has no addresses')
    # Reject a mixed public/private answer, not merely the first address.
    addresses = [_public_ip(str(result[4][0])) for result in results]
    return addresses[0]


async def fetch_public_card_content(
    source: str, *, max_bytes: int = MAX_CARD_BYTES, timeout: float = DEFAULT_TIMEOUT,
) -> RemoteCardContent:
    """Safe discovery/download path. Connect to the checked IP with original TLS SNI.

    No proxy, cookies, authentication, automatic redirects or HTTP decompression.
    The original URL is retained for attribution, never the pinned IP URL.
    """
    import httpx
    try:
        async with asyncio.timeout(timeout), httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10), follow_redirects=False,
            trust_env=False,
        ) as client:
            current = source
            seen: set[str] = set()
            for _ in range(6):
                current = public_url(current)
                if current in seen:
                    raise CardFetchError('redirect loop')
                seen.add(current)
                original = httpx.URL(current)
                ip = await _resolve_public(original.host)
                pinned = original.copy_with(host=ip)
                async with client.stream(
                    'GET', pinned,
                    headers={'Host': original.host, 'Accept-Encoding': 'identity',
                             'User-Agent': 'qqbot-card-fetch/1.0'},
                    extensions={'sni_hostname': original.host},
                ) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get('location', '')
                        if not location:
                            raise CardFetchError('redirect has no location')
                        current = urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        raise CardFetchError(f'card source HTTP {response.status_code}')
                    if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                        raise CardFetchError('compressed HTTP responses are not accepted')
                    length = response.headers.get('content-length')
                    if length and int(length) > max_bytes:
                        raise CardFetchError('remote content exceeds size limit')
                    size = 0
                    chunks = []
                    async for chunk in response.aiter_raw():
                        size += len(chunk)
                        if size > max_bytes:
                            raise CardFetchError('remote content exceeds size limit')
                        chunks.append(chunk)
                    return RemoteCardContent(b''.join(chunks), current,
                                             response.headers.get('content-type', '').lower())
            raise CardFetchError('too many redirects')
    except CardFetchError:
        raise
    except Exception as exc:  # noqa: BLE001 - do not expose signed URLs or proxy secrets
        raise CardFetchError(f'remote card request failed ({type(exc).__name__})') from exc
