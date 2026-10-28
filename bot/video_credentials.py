"""Operator-managed platform cookies. Secrets never belong in public status results."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from http.cookiejar import Cookie, DefaultCookiePolicy
from pathlib import Path

MAX_CONTENT = 65536
DEFAULT_DIRECTORY = '/var/lib/qqbot/video-credentials'
PLATFORMS = ('bilibili', 'douyin')
HOSTS = {'bilibili': ('www.bilibili.com', 'api.bilibili.com'), 'douyin': ('www.douyin.com',)}
_LOCK = threading.RLock()
_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")


class CredentialError(ValueError):
    def __init__(self):
        super().__init__('Invalid or unavailable platform cookie configuration')


def platform_name(platform: str) -> str:
    if platform not in PLATFORMS:
        raise CredentialError()
    return platform


def validate_content(platform: str, format: str, content: str) -> list[dict]:
    platform_name(platform)
    if not isinstance(content, str) or not content.strip() or len(content.encode('utf-8')) > MAX_CONTENT:
        raise CredentialError()
    records = []
    if format == 'header':
        if chr(10) in content or chr(13) in content:
            raise CredentialError()
        value = content.strip()
        if value.lower().startswith('cookie:'):
            value = value[7:].strip()
        if any(ord(c) < 32 or ord(c) > 126 for c in value):
            raise CredentialError()
        seen = set()
        for pair in value.split(';'):
            name, sep, data = pair.strip().partition('=')
            if not sep or not _NAME.fullmatch(name) or name in seen or not data or any(c in data for c in '\\"'):
                raise CredentialError()
            if any(ord(c) <= 32 for c in data):
                raise CredentialError()
            seen.add(name)
            for host in HOSTS[platform]:
                records.append(dict(domain=host, subdomains=False, path='/', secure=True,
                                    expires=None, name=name, value=data))
    elif format == 'netscape':
        if not content.startswith(('# Netscape HTTP Cookie File', '# HTTP Cookie File')):
            raise CredentialError()
        seen = set()
        for line in content.splitlines():
            if line.startswith('#HttpOnly_'):
                line = line[10:]
            elif not line or line.startswith('#'):
                continue
            fields = line.split('\t')
            if len(fields) != 7:
                raise CredentialError()
            domain, subs, path, secure, expiry, name, value = fields
            root = 'bilibili.com' if platform == 'bilibili' else 'douyin.com'
            host = domain.removeprefix('.')
            if (not re.fullmatch(r'[a-z0-9.-]+', host) or not (host == root or host.endswith('.' + root))
                    or subs not in ('TRUE', 'FALSE') or secure not in ('TRUE', 'FALSE')
                    or domain.startswith('.') != (subs == 'TRUE') or not path.startswith('/')
                    or any(ord(c) < 32 or ord(c) > 126 for c in path + value)
                    or not expiry.isascii() or not expiry.isdigit() or len(expiry) > 12
                    or not _NAME.fullmatch(name) or not value or (domain, path, name) in seen):
                raise CredentialError()
            if any(not label or label.startswith('-') or label.endswith('-') for label in host.split('.')):
                raise CredentialError()
            seen.add((domain, path, name))
            records.append(dict(domain=domain, subdomains=subs == 'TRUE', path=path,
                                secure=secure == 'TRUE', expires=int(expiry) or None,
                                name=name, value=value))
    else:
        raise CredentialError()
    if not 0 < len(records) <= 256:
        raise CredentialError()
    return records


def _safe_path(directory: str, platform: str, *, create=False) -> Path:
    platform_name(platform)
    root = Path(directory)
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise CredentialError()
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
    target = root / (platform + '.json')
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise CredentialError()
    return target


def read_state(path: Path) -> dict:
    with _LOCK:
        return _read_state(path)


def _read_state(path: Path) -> dict:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise CredentialError()
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise CredentialError()
        state = json.loads(raw)
        if (state.get('schema') != 1 or state.get('state') not in ('managed', 'cleared')
                or not re.fullmatch(r'[a-f0-9]{32}', state.get('generation', ''))
                or not isinstance(state.get('cookies'), list) or len(state['cookies']) > 256
                or type(state.get('updated_at')) is not int
                or (state['state'] == 'managed' and not state['cookies'])
                or (state['state'] == 'cleared' and state['cookies'])):
            raise CredentialError()
        for item in state['cookies']:
            if (not isinstance(item, dict) or set(item) != {'domain', 'subdomains', 'path', 'secure', 'expires', 'name', 'value'}
                    or type(item['subdomains']) is not bool or type(item['secure']) is not bool
                    or item['expires'] is not None and (type(item['expires']) is not int or item['expires'] < 0)
                    or not isinstance(item['domain'], str) or not isinstance(item['path'], str)
                    or not isinstance(item['name'], str) or not isinstance(item['value'], str)
                    or not _NAME.fullmatch(item['name']) or not item['value']
                    or not re.fullmatch(r'\.?[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?', item['domain'])
                    or item['domain'].startswith('.') != item['subdomains']
                    or not item['path'].startswith('/')
                    or any(ord(c) < 32 or ord(c) > 126 for c in item['value'] + item['path'])):
                raise CredentialError()
        return state
    except (OSError, ValueError, TypeError, AttributeError):
        raise CredentialError() from None


def effective(directory: str, platform: str, legacy: str = '') -> tuple[str, tuple]:
    path = _safe_path(directory, platform)
    if path.exists():
        state = read_state(path)
        return (str(path) if state['state'] == 'managed' else '', ('managed', state['generation']))
    if legacy:
        try:
            info = Path(legacy).stat()
            return legacy, ('legacy', legacy, info.st_mtime_ns, info.st_size)
        except OSError:
            raise CredentialError() from None
    return '', ('anonymous',)


def status(directory: str, platform: str, legacy: str = '') -> dict:
    path = _safe_path(directory, platform)
    if path.exists():
        state = read_state(path)
        return dict(platform=platform, configured=state['state'] == 'managed',
                    source=state['state'], updated_at=state['updated_at'])
    return dict(platform=platform, configured=bool(legacy and Path(legacy).is_file()),
                source='legacy' if legacy else 'missing', updated_at=None)


def save(directory: str, platform: str, format: str | None = None, content: str | None = None,
         *, clear=False) -> dict:
    if not clear and (not isinstance(format, str) or not isinstance(content, str)):
        raise CredentialError()
    cookies = [] if clear else validate_content(platform, format or '', content or '')
    state = dict(schema=1, state='cleared' if clear else 'managed', generation=secrets.token_hex(16),
                 updated_at=int(time.time()), cookies=cookies)
    with _LOCK:
        path = _safe_path(directory, platform, create=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.cookies-', dir=path.parent)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(state, stream, ensure_ascii=True)
                stream.flush()
                os.fsync(stream.fileno())
            _safe_path(directory, platform)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return status(directory, platform)


def load_jar(path: str, jar, platform: str, generation: str | None = None):
    with _LOCK:
        _load_jar(path, jar, platform, generation)


def _load_jar(path: str, jar, platform: str, generation: str | None = None):
    """Read managed JSON or the existing explicit operator Netscape path into memory."""
    if not path:
        return
    platform_name(platform)
    jar.set_policy(DefaultCookiePolicy(strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain))
    try:
        if Path(path).is_symlink():
            raise CredentialError()
        with open(path, 'rb') as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise CredentialError()
        if raw.lstrip().startswith(b'{'):
            state = read_state(Path(path))
            if generation is not None and state['generation'] != generation:
                raise CredentialError()
            records = state['cookies'] if state['state'] == 'managed' else []
            for item in records:
                root = 'bilibili.com' if platform == 'bilibili' else 'douyin.com'
                domain = item['domain'].lstrip('.')
                if not (domain == root or domain.endswith('.' + root)):
                    raise CredentialError()
                jar.set_cookie(Cookie(0, item['name'], item['value'], None, False,
                    item['domain'], item['subdomains'], item['domain'].startswith('.'),
                    item['path'], True, item['secure'], item['expires'], item['expires'] is None,
                    None, None, {}))
        else:
            # Validate without logging parser lines; only this platform is accepted.
            records = validate_content(platform, 'netscape', raw.decode('utf-8'))
            for item in records:
                jar.set_cookie(Cookie(0, item['name'], item['value'], None, False,
                    item['domain'], item['subdomains'], item['domain'].startswith('.'),
                    item['path'], True, item['secure'], item['expires'], item['expires'] is None,
                    None, None, {}))
        jar.clear_expired_cookies()
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        raise CredentialError() from None
