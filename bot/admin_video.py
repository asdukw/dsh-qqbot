"""Credential API helpers: strict local transport and secret-free status."""

from __future__ import annotations

import ipaddress
import json
from urllib.parse import urlparse

from video_credentials import DEFAULT_DIRECTORY, PLATFORMS, CredentialError
from video_credentials import save as save_credentials
from video_credentials import status as credential_status


def local_channel(handler, *, mutation=False) -> bool:
    try:
        host = handler.headers.get('Host', '')
        if len(handler.headers.get_all('Host', [])) != 1 or not host or any(c.isspace() for c in host):
            return False
        parsed = urlparse('http://' + host)
        name = parsed.hostname
        _ = parsed.port  # Reject malformed port syntax before accepting a loopback authority.
        if parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment:
            return False
        if name != 'localhost' and not ipaddress.ip_address(name or '').is_loopback:
            return False
        if not ipaddress.ip_address(handler.client_address[0]).is_loopback:
            return False
        if mutation:
            if len(handler.headers.get_all('Origin', [])) != 1:
                return False
            origin = urlparse(handler.headers.get('Origin', ''))
            if origin.scheme != 'http' or origin.netloc != host or origin.path or origin.query or origin.fragment:
                return False
        return True
    except (ValueError, TypeError):
        return False


def status_payload(handler) -> dict:
    settings = handler.config.get('video') or {}
    directory = str(settings.get('credentials_directory', DEFAULT_DIRECTORY))
    platforms = []
    for platform in PLATFORMS:
        item = credential_status(directory, platform, str(settings.get(platform + '_cookies', '')))
        item['enabled'] = bool(settings.get('enabled', True) and settings.get(platform + '_enabled', platform == 'bilibili'))
        platforms.append(item)
    return {'platforms': platforms, 'can_write': local_channel(handler),
            'notice': 'Cookies are never returned. Configured is not authenticated; HTTP 412 may persist.'}


def reject(handler, code, message):
    # Drain only an unambiguous bounded body so Windows clients receive the HTTP error.
    lengths = handler.headers.get_all('Content-Length', [])
    if len(lengths) == 1 and lengths[0].isascii() and lengths[0].isdigit() and not handler.headers.get_all('Transfer-Encoding', []):
        length = int(lengths[0])
        if 0 < length <= 196608:
            try:
                handler.connection.settimeout(2)
                handler.rfile.read(length)
            except OSError:
                pass
    handler._send_json(code, {'error': message})


def handle_post(handler, path: str):
    if not handler._api_guard():
        return
    handler.close_connection = True
    if not local_channel(handler, mutation=True):
        reject(handler, 403, 'Cookie changes require same-origin loopback access through a local port forward')
        return
    if (len(handler.headers.get_all('Content-Type', [])) != 1
            or handler.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json'):
        reject(handler, 415, 'Content-Type must be application/json')
        return
    if (len(handler.headers.get_all('Content-Length', [])) != 1
            or handler.headers.get_all('Transfer-Encoding', [])
            or len(handler.headers.get_all('Cookie', [])) > 1):
        reject(handler, 400, 'Invalid request headers')
        return
    try:
        value = handler.headers['Content-Length']
        if not value.isascii() or not value.isdigit():
            raise CredentialError()
        length = int(value)
        if not 0 < length <= 98304:
            if 0 < length <= 196608:
                handler.rfile.read(length)
            handler._send_json(413, {'error': 'Cookie request must be at most 96 KiB; nothing was saved'})
            return
        handler.connection.settimeout(10)
        raw = handler.rfile.read(length)
        if len(raw) != length:
            raise CredentialError()
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise CredentialError()
                result[key] = value
            return result
        payload = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs)
        clear = path.endswith('/delete')
        if not isinstance(payload, dict) or set(payload) != ({'platform'} if clear else {'platform', 'format', 'content'}):
            raise CredentialError()
        if not isinstance(payload['platform'], str) or payload['platform'] not in PLATFORMS:
            raise CredentialError()
        if not clear and (not isinstance(payload['format'], str) or not isinstance(payload['content'], str)):
            raise CredentialError()
        directory = str((handler.config.get('video') or {}).get('credentials_directory', DEFAULT_DIRECTORY))
        save_credentials(directory, payload['platform'], payload.get('format'), payload.get('content'), clear=clear)
        handler._send_json(200, status_payload(handler))
    except (ValueError, OSError, TypeError, KeyError, UnicodeError, RecursionError):
        handler._send_json(400, {'error': 'Invalid or unavailable platform cookie configuration'})
