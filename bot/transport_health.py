"""Read-only OneBot readiness: a running container is not a logged-in QQ."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from typing import Any
from urllib.parse import quote

import websockets

_lock = threading.Lock()
_cached: dict[str, Any] = {}
_cached_at = 0.0
_cached_target: tuple[str, str] | None = None


async def probe(url: str, token: str, *, timeout: float = 5.0) -> dict[str, Any]:
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    if token:
        url += ('&' if '?' in url else '?') + 'access_token=' + quote(token, safe='')
    try:
        async with asyncio.timeout(timeout):
            async with websockets.connect(url, additional_headers=headers, open_timeout=timeout,
                                          close_timeout=1, max_size=1024 * 1024) as ws:
                await ws.send(json.dumps({'action': 'get_status', 'params': {}, 'echo': 'readiness'}))
                async for raw in ws:
                    payload = json.loads(raw)
                    if not isinstance(payload, dict) or payload.get('echo') != 'readiness':
                        continue
                    if payload.get('status') == 'failed' or payload.get('retcode') not in (0, None):
                        return {'ready': False, 'state': 'status_rejected'}
                    data = payload.get('data')
                    if not isinstance(data, dict):
                        return {'ready': False, 'state': 'invalid_status'}
                    online = data.get('online') is True
                    ready = online and data.get('good') is not False
                    return {'ready': ready, 'state': 'online' if ready else 'login_required', 'online': online}
        return {'ready': False, 'state': 'disconnected'}
    except Exception:  # Never return handshake errors, URLs or authentication material.
        return {'ready': False, 'state': 'unreachable'}


def transport_health(config: dict[str, Any]) -> dict[str, Any]:
    """Bounded, briefly cached probe for the independent admin HTTP process."""
    global _cached, _cached_at, _cached_target
    section = config.get('onebot') or {}
    url = str(section.get('url') or 'ws://127.0.0.1:3001')
    token = os.environ.get('ONEBOT_TOKEN', '').strip() or str(section.get('token') or '')
    target = (url, token)
    with _lock:
        if _cached and _cached_target == target and time.monotonic() - _cached_at < 10:
            return dict(_cached)
        result = asyncio.run(probe(url, token))
        result['checked_at'] = int(time.time())
        _cached = result
        _cached_at = time.monotonic()
        _cached_target = target
        return dict(result)
