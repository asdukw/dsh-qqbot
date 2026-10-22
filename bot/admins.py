"""Admin QQ ids, read from a comma/space separated environment value."""

from __future__ import annotations

import re

_SEPARATORS = re.compile(r"[,;\s]+")


def parse_admin_ids(raw: str) -> tuple[int, ...]:
    """Parse ADMIN_QQ_IDS into unique ints, ignoring malformed entries."""
    ids: list[int] = []
    for part in _SEPARATORS.split(raw.strip()):
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            continue
        if value > 0 and value not in ids:
            ids.append(value)
    return tuple(ids)
