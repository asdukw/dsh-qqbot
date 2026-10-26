"""Character cards: parsing (V1 / V2 / V3, JSON or PNG) and the SQLite library.

The internal representation is always Character Card V3 (``data`` object).
Missing V3 fields get defaults; V2 cards are upgraded losslessly.
``CardRegistry`` stores normalised payloads and tracks the single active card.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import re
import sqlite3
import threading
import time
import uuid
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_CARD_METADATA = 2 * 1024 * 1024
MAX_REMOTE_CARD_BYTES = 12 * 1024 * 1024

V3_DEFAULTS: dict[str, Any] = {
    "name": "",
    "nickname": "",
    "description": "",
    "personality": "",
    "scenario": "",
    "first_mes": "",
    "mes_example": "",
    "creator_notes": "",
    "system_prompt": "",
    "post_history_instructions": "",
    "alternate_greetings": [],
    "group_only_greetings": [],
    "character_book": {"entries": []},
    "tags": [],
    "creator": "",
    "character_version": "",
    "extensions": {},
    "assets": [],
    "creator_notes_multilingual": {},
    "source": [],
    "creation_date": 0,
    "modification_date": 0,
}

_STRING_FIELDS = {
    "name", "nickname", "description", "personality", "scenario", "first_mes",
    "mes_example", "creator_notes", "system_prompt", "post_history_instructions",
    "creator", "character_version",
}
_LIST_FIELDS = {"alternate_greetings", "group_only_greetings", "tags", "source"}


class CardError(ValueError):
    pass


@dataclass
class LoreEntry:
    keys: list[str] = field(default_factory=list)
    secondary_keys: list[str] = field(default_factory=list)
    content: str = ""
    enabled: bool = True
    constant: bool = False
    selective: bool = False
    case_sensitive: bool = False
    use_regex: bool = False
    insertion_order: int = 0


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return value if isinstance(value, str) else str(value)


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [v for v in (_as_str(x).strip() for x in value) if v]
    return []


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalise_entry(raw: Any) -> LoreEntry | None:
    if not isinstance(raw, dict):
        return None
    content = _as_str(raw.get("content")).strip()
    if not content:
        return None
    extensions = raw.get("extensions")
    if not isinstance(extensions, dict):
        extensions = {}
    return LoreEntry(
        keys=_as_str_list(raw.get("keys")),
        secondary_keys=_as_str_list(raw.get("secondary_keys")),
        content=content,
        enabled=bool(raw.get("enabled", True)),
        constant=bool(raw.get("constant", False)),
        selective=bool(raw.get("selective", False)),
        case_sensitive=bool(raw.get("case_sensitive", False)),
        use_regex=bool(raw.get("use_regex", extensions.get("use_regex", False))),
        insertion_order=_as_int(raw.get("insertion_order"), 0),
    )


@dataclass
class Lorebook:
    entries: list[LoreEntry]

    @classmethod
    def from_card(cls, book: Any) -> Lorebook:
        if not isinstance(book, dict) or not isinstance(book.get("entries"), list):
            return cls([])
        entries = [e for e in (_normalise_entry(x) for x in book["entries"]) if e]
        return cls(entries)

    @staticmethod
    def _match(needle: str, haystack: str, case_sensitive: bool, use_regex: bool) -> bool:
        if not needle:
            return False
        if use_regex:
            try:
                return re.search(needle, haystack, 0 if case_sensitive else re.IGNORECASE) is not None
            except re.error:
                pass  # fall back to a literal match
        if case_sensitive:
            return needle in haystack
        return needle.lower() in haystack.lower()

    def select(self, text: str, max_chars: int = 4000) -> list[str]:
        hits: list[LoreEntry] = []
        for entry in self.entries:
            if not entry.enabled or not entry.content:
                continue
            matched = entry.constant or any(
                self._match(k, text, entry.case_sensitive, entry.use_regex) for k in entry.keys
            )
            if matched and entry.selective and entry.secondary_keys:
                matched = any(
                    self._match(k, text, entry.case_sensitive, entry.use_regex)
                    for k in entry.secondary_keys
                )
            if matched:
                hits.append(entry)

        hits.sort(key=lambda e: e.insertion_order)
        selected: list[str] = []
        used = 0
        for entry in hits:
            if used + len(entry.content) > max_chars:
                continue
            selected.append(entry.content)
            used += len(entry.content)
        return selected


def _iter_png_chunks(fh: Any, label: str = "input") -> Any:
    if fh.read(8) != PNG_SIGNATURE:
        raise CardError(f"{label} is not a PNG file")
    while True:
        header = fh.read(8)
        if len(header) < 8:
            return
        length = int.from_bytes(header[:4], "big")
        kind = header[4:8]
        data = fh.read(length)
        fh.read(4)  # CRC
        yield kind, data
        if kind == b"IEND":
            return


def _decode_payload(payload: bytes) -> dict[str, Any] | None:
    text = payload.strip()
    candidates: list[bytes] = [text]
    try:
        candidates.append(base64.b64decode(text + b"==" * (-len(text) % 4), validate=False))
    except (binascii.Error, ValueError):
        pass
    for candidate in candidates:
        try:
            obj = json.loads(candidate.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _read_png_card_stream(fh: Any, label: str = "PNG card") -> dict[str, Any]:
    found: dict[str, bytes] = {}
    for kind, data in _iter_png_chunks(fh, label):
        keyword = b""
        payload = b""
        if kind == b"tEXt":
            keyword, sep, payload = data.partition(b"\x00")
            if not sep:
                continue
        elif kind == b"iTXt":
            keyword, sep, rest = data.partition(b"\x00")
            if not sep or len(rest) < 3:
                continue
            compression_flag = rest[0:1]
            rest = rest[2:]  # compression method
            _lang, sep, rest = rest.partition(b"\x00")
            if not sep:
                continue
            _translated, sep, payload = rest.partition(b"\x00")
            if not sep:
                continue
            if compression_flag in (b"1", b"\x01"):
                try:
                    inflater = zlib.decompressobj()
                    payload = inflater.decompress(payload, MAX_CARD_METADATA + 1)
                    if len(payload) > MAX_CARD_METADATA or not inflater.eof or inflater.unused_data:
                        raise CardError('PNG card metadata exceeds limit or is incomplete')
                except zlib.error:
                    continue
        else:
            continue
        key = keyword.decode("latin-1")
        if key in ("ccv3", "chara") and key not in found:
            found[key] = payload

    for keyword in ("ccv3", "chara"):
        if keyword in found:
            obj = _decode_payload(found[keyword])
            if obj is not None:
                return obj
    raise CardError(f"no character card data found in {label}")


def _read_png_card(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return _read_png_card_stream(fh, str(path))


def _coerce_data(src: dict[str, Any], wrapped: bool) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key, default in V3_DEFAULTS.items():
        raw = src.get(key, default)
        if key in _STRING_FIELDS:
            data[key] = _as_str(raw, default)
        elif key in _LIST_FIELDS:
            data[key] = _as_str_list(raw)
        elif key == "character_book":
            data[key] = raw if isinstance(raw, dict) else {"entries": []}
        elif key == "extensions":
            data[key] = raw if isinstance(raw, dict) else {}
        elif key == "assets":
            data[key] = raw if isinstance(raw, list) else []
        elif key == "creator_notes_multilingual":
            data[key] = raw if isinstance(raw, dict) else {}
        elif key in ("creation_date", "modification_date"):
            data[key] = _as_int(raw, 0)
        else:
            data[key] = raw

    if not wrapped:
        # V1 / Pygmalion flat fields.
        data["name"] = data["name"] or _as_str(src.get("char_name"))
        data["description"] = data["description"] or _as_str(src.get("char_persona"))
        data["first_mes"] = data["first_mes"] or _as_str(src.get("char_greeting"))
        data["mes_example"] = data["mes_example"] or _as_str(src.get("example_dialogue"))
        data["scenario"] = data["scenario"] or _as_str(src.get("world_scenario"))

    if not data["name"]:
        data["name"] = "Assistant"
    return data


def normalise(raw: dict[str, Any]) -> dict[str, Any]:
    wrapped = isinstance(raw.get("data"), dict)
    src = raw["data"] if wrapped else raw
    if not isinstance(src, dict):
        raise CardError("character card payload is not an object")
    spec = raw.get("spec") if isinstance(raw.get("spec"), str) else ""
    return {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": _coerce_data(src, wrapped),
        "_import": {
            "spec": spec or ("chara_card_v2" if wrapped else "v1"),
            "spec_version": _as_str(raw.get("spec_version")),
        },
    }


def _validate_remote_bytes(data: bytes) -> None:
    if len(data) > MAX_REMOTE_CARD_BYTES:
        raise CardError('remote character card exceeds file limit')
    if data.startswith(PNG_SIGNATURE):
        stream = io.BytesIO(data[8:])
        while True:
            header = stream.read(8)
            if len(header) != 8:
                raise CardError('truncated PNG')
            length, kind = int.from_bytes(header[:4], 'big'), header[4:]
            if length > MAX_REMOTE_CARD_BYTES:
                raise CardError('PNG chunk exceeds limit')
            body, crc = stream.read(length), stream.read(4)
            if len(body) != length or len(crc) != 4 or int.from_bytes(crc, 'big') != zlib.crc32(kind + body) & 0xffffffff:
                raise CardError('invalid PNG chunk')
            if kind in (b'tEXt', b'iTXt', b'zTXt') and length > MAX_CARD_METADATA:
                raise CardError('PNG metadata exceeds limit')
            if kind == b'IEND':
                break
    elif len(data) > MAX_CARD_METADATA:
        raise CardError('JSON card exceeds metadata limit')
    # Bound JSON nesting before parsing; JSON strings may contain braces.
    if not data.startswith(PNG_SIGNATURE):
        quoted = escaped = False
        depth = 0
        for byte in data:
            if quoted:
                if escaped:
                    escaped = False
                elif byte == 92:
                    escaped = True
                elif byte == 34:
                    quoted = False
            elif byte == 34:
                quoted = True
            elif byte in (91, 123):
                depth += 1
                if depth > 32:
                    raise CardError('card JSON is too deeply nested')
            elif byte in (93, 125):
                depth -= 1


def _validate_remote_payload(raw: dict[str, Any]) -> None:
    wrapped = 'data' in raw
    if wrapped and raw.get('spec') not in ('chara_card_v2', 'chara_card_v3'):
        raise CardError('unrecognized remote card specification')
    src = raw.get('data') if wrapped else raw
    if not isinstance(src, dict):
        raise CardError('remote card data is not an object')
    name = src.get('name', src.get('char_name'))
    if not isinstance(name, str) or not name.strip() or len(name) > 256:
        raise CardError('remote card needs a valid character name')
    fields = ('description', 'personality', 'scenario', 'first_mes', 'char_persona', 'char_greeting')
    if not any(isinstance(src.get(key), str) and src[key].strip() for key in fields):
        raise CardError('remote response has no character content')
    pending: list[tuple[Any, int]] = [(raw, 0)]
    nodes = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if depth > 32 or nodes > 20000:
            raise CardError('card structure exceeds limits')
        if isinstance(value, dict):
            pending.extend((v, depth + 1) for v in value.values())
        elif isinstance(value, list):
            if len(value) > 2000:
                raise CardError('card array exceeds limit')
            pending.extend((v, depth + 1) for v in value)
        elif isinstance(value, str) and len(value) > 256000:
            raise CardError('card field exceeds limit')
    for key in _STRING_FIELDS:
        if key in src and not isinstance(src[key], str):
            raise CardError(f'invalid remote card field: {key}')


@dataclass
class CharacterCard:
    path: Path
    mtime: float
    payload: dict[str, Any]
    lorebook: Lorebook

    @property
    def data(self) -> dict[str, Any]:
        return self.payload["data"]

    @property
    def name(self) -> str:
        return self.data.get("nickname") or self.data.get("name") or "Assistant"

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        path: str | Path = "card",
        mtime: float = 0.0,
    ) -> CharacterCard:
        """Build a card from an already-normalised V3 payload (as stored in SQLite)."""
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise CardError("stored character card payload is invalid")
        return cls(
            path=Path(path),
            mtime=float(mtime),
            payload=payload,
            lorebook=Lorebook.from_card(payload["data"].get("character_book")),
        )

    @classmethod
    def from_bytes(cls, data: bytes, source: str = "download", *, strict: bool = False) -> CharacterCard:
        """Parse PNG or JSON, optionally requiring a bounded remote character card."""
        if strict:
            _validate_remote_bytes(data)
        if data[: len(PNG_SIGNATURE)] == PNG_SIGNATURE:
            raw = _read_png_card_stream(io.BytesIO(data), source)
        else:
            try:
                raw = json.loads(data.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CardError(
                    f"{source} is neither a PNG card nor valid JSON: {exc}"
                ) from exc
        if not isinstance(raw, dict):
            raise CardError(f"character card in {source} is not a JSON object")
        if strict:
            _validate_remote_payload(raw)
        return cls.from_payload(normalise(raw), path=source)

    @classmethod
    def load(cls, path: str | Path) -> CharacterCard:
        p = Path(path).expanduser().resolve()
        if not p.is_file():
            raise CardError(f"character card not found: {p}")
        if p.suffix.lower() == ".png":
            raw = _read_png_card(p)
        else:
            try:
                raw = json.loads(p.read_text(encoding="utf-8-sig"))
            except json.JSONDecodeError as exc:
                raise CardError(f"invalid JSON in {p}: {exc}") from exc
        if not isinstance(raw, dict):
            raise CardError(f"character card in {p} is not a JSON object")
        return cls.from_payload(normalise(raw), path=p, mtime=p.stat().st_mtime)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id      TEXT NOT NULL DEFAULT '',
    story_referenced  INTEGER NOT NULL DEFAULT 0,
    name              TEXT NOT NULL,
    nickname          TEXT NOT NULL DEFAULT '',
    aliases           TEXT NOT NULL DEFAULT '[]',
    description       TEXT NOT NULL DEFAULT '',
    personality       TEXT NOT NULL DEFAULT '',
    scenario          TEXT NOT NULL DEFAULT '',
    tags              TEXT NOT NULL DEFAULT '[]',
    creator           TEXT NOT NULL DEFAULT '',
    character_version TEXT NOT NULL DEFAULT '',
    fingerprint       TEXT NOT NULL,
    payload           TEXT NOT NULL,
    source_kind       TEXT NOT NULL DEFAULT 'file',
    source            TEXT NOT NULL DEFAULT '',
    source_mtime      REAL NOT NULL DEFAULT 0,
    installed_by      TEXT NOT NULL DEFAULT '',
    is_active         INTEGER NOT NULL DEFAULT 0,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cards_fingerprint ON cards (fingerprint);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cards_single_active
    ON cards (is_active) WHERE is_active = 1;
"""

# Fields are searched with LIKE so Chinese works without an FTS tokenizer.
_SEARCH_FIELDS = ("name", "nickname", "description", "personality", "scenario", "tags", "creator")


def fingerprint(payload: dict[str, Any]) -> str:
    """Stable hash of the normalised card data, used to spot re-installs."""
    data = payload.get("data", payload)
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_ALIAS_LIMIT = 8
_ALIAS_MAX_CHARS = 32


def _normalise_aliases(
    values: Any, *, name: str = "", nickname: str = ""
) -> list[str]:
    """Trim, dedupe and bound the names that address this card.

    Single-character aliases are dropped: matching them inside ordinary chat
    would fire far too often, and ``ProactivePolicy.named`` ignores them anyway.
    """
    if isinstance(values, str):
        values = values.replace("，", ",").split(",")
    if not isinstance(values, (list, tuple, set)):
        return []
    reserved = {name.strip(), nickname.strip()}
    aliases: list[str] = []
    for value in values:
        alias = str(value).strip()
        if len(alias) < 2 or len(alias) > _ALIAS_MAX_CHARS:
            continue
        if alias in reserved or alias in aliases:
            continue
        aliases.append(alias)
        if len(aliases) >= _ALIAS_LIMIT:
            break
    return aliases


@dataclass(frozen=True)
class CardRecord:
    id: int
    character_id: str
    name: str
    nickname: str
    aliases: tuple[str, ...]
    description: str
    personality: str
    scenario: str
    tags: tuple[str, ...]
    creator: str
    character_version: str
    fingerprint: str
    payload: dict[str, Any]
    source_kind: str
    source: str
    source_mtime: float
    installed_by: str
    is_active: bool
    created_at: int
    updated_at: int

    @property
    def display_name(self) -> str:
        return self.nickname or self.name or "Assistant"

    @property
    def card(self) -> CharacterCard:
        return CharacterCard.from_payload(
            self.payload,
            path=self.source or f"card-{self.id}",
            mtime=float(self.updated_at),
        )


def _record(row: sqlite3.Row) -> CardRecord:
    try:
        payload = json.loads(row["payload"])
    except (TypeError, json.JSONDecodeError):
        payload = {}
    try:
        raw_tags = json.loads(row["tags"] or "[]")
    except (TypeError, json.JSONDecodeError):
        raw_tags = []
    tags = tuple(str(tag) for tag in raw_tags if str(tag).strip()) if isinstance(raw_tags, list) else ()
    try:
        raw_aliases = json.loads(row["aliases"] or "[]")
    except (TypeError, json.JSONDecodeError):
        raw_aliases = []
    aliases = (
        tuple(str(alias) for alias in raw_aliases if str(alias).strip())
        if isinstance(raw_aliases, list)
        else ()
    )
    return CardRecord(
        id=int(row["id"]),
        character_id=row["character_id"],
        name=row["name"] or "Assistant",
        nickname=row["nickname"] or "",
        aliases=aliases,
        description=row["description"] or "",
        personality=row["personality"] or "",
        scenario=row["scenario"] or "",
        tags=tags,
        creator=row["creator"] or "",
        character_version=row["character_version"] or "",
        fingerprint=row["fingerprint"] or "",
        payload=payload if isinstance(payload, dict) else {},
        source_kind=row["source_kind"] or "",
        source=row["source"] or "",
        source_mtime=float(row["source_mtime"] or 0.0),
        installed_by=row["installed_by"] or "",
        is_active=bool(row["is_active"]),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


class CardRegistry:
    """Normalised V3 card library with a fallback default.

    Conversation bindings and durable switch requests live in personas.py;
    changing a conversation never changes this library's default selection.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(cards)")}
        if "aliases" not in columns:
            self._db.execute(
                "ALTER TABLE cards ADD COLUMN aliases TEXT NOT NULL DEFAULT '[]'"
            )
        if "character_id" not in columns:
            self._db.execute("ALTER TABLE cards ADD COLUMN character_id TEXT NOT NULL DEFAULT ''")
        if "story_referenced" not in columns:
            self._db.execute("ALTER TABLE cards ADD COLUMN story_referenced INTEGER NOT NULL DEFAULT 0")
        for row in self._db.execute("SELECT id FROM cards WHERE character_id='' ").fetchall():
            self._db.execute("UPDATE cards SET character_id=? WHERE id=?", (uuid.uuid4().hex, row[0]))
        self._db.commit()

    # --------------------------------------------------------------- reads

    def count(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT COUNT(*) FROM cards").fetchone()
        return int(row[0]) if row else 0

    def list(self) -> list[CardRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM cards ORDER BY is_active DESC, name COLLATE NOCASE, id"
            ).fetchall()
        return [_record(row) for row in rows]

    def get(self, card_id: int) -> CardRecord | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        return _record(row) if row else None

    def active(self) -> CardRecord | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM cards WHERE is_active = 1").fetchone()
        return _record(row) if row else None

    def active_signature(self) -> tuple | None:
        """Identity of the active card; changes when the card is switched or updated."""
        with self._lock:
            row = self._db.execute(
                "SELECT id, updated_at, fingerprint FROM cards WHERE is_active = 1"
            ).fetchone()
        if row is None:
            return None
        return (int(row["id"]), int(row["updated_at"]), row["fingerprint"])

    def resolve(self, target: str) -> CardRecord | None:
        """An id, an exact name/nickname, or a unique case-insensitive substring."""
        target = target.strip()
        if not target:
            return None
        if target.isdigit():
            return self.get(int(target))
        needle = target.lower()
        rows = self.list()
        exact = [
            r for r in rows
            if r.name.lower() == needle
            or r.nickname.lower() == needle
            or any(alias.lower() == needle for alias in r.aliases)
        ]
        if len(exact) == 1:
            return exact[0]
        partial = [
            r for r in rows
            if needle in r.name.lower()
            or needle in r.nickname.lower()
            or any(needle in alias.lower() for alias in r.aliases)
        ]
        return partial[0] if len(partial) == 1 else None

    def search(self, query: str, limit: int = 10) -> list[CardRecord]:
        """AND across whitespace-separated terms, OR across card fields."""
        terms = [term for term in re.split(r"\s+", query.strip()) if term]
        if not terms:
            return self.list()[:limit]

        def like(term: str) -> str:
            escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            return f"%{escaped}%"

        clauses: list[str] = []
        params: list[Any] = []
        for term in terms:
            pattern = like(term)
            fields = " OR ".join(f"{name} LIKE ? ESCAPE '\\'" for name in _SEARCH_FIELDS)
            clauses.append(f"({fields})")
            params.extend([pattern] * len(_SEARCH_FIELDS))

        first = like(terms[0])
        sql = (
            "SELECT * FROM cards WHERE " + " AND ".join(clauses)
            + " ORDER BY (name LIKE ? ESCAPE '\\') DESC,"
            " (nickname LIKE ? ESCAPE '\\') DESC, updated_at DESC LIMIT ?"
        )
        with self._lock:
            rows = self._db.execute(sql, (*params, first, first, limit)).fetchall()
        return [_record(row) for row in rows]

    # -------------------------------------------------------------- writes

    def install_payload(
        self,
        payload: dict[str, Any],
        *,
        source_kind: str = "manual",
        source: str = "",
        installed_by: str = "",
        source_mtime: float = 0.0,
    ) -> tuple[CardRecord, bool]:
        """Insert a normalised card; return the row and whether it was new."""
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise CardError("character card payload is not a normalised V3 object")
        data = payload["data"]
        digest = fingerprint(payload)
        source = source[:512]
        with self._lock, self._db:
            now = int(time.time())
            cursor = self._db.execute(
                """
                INSERT INTO cards
                    (name, nickname, description, personality, scenario, tags, creator,
                     character_version, fingerprint, payload, source_kind, source,
                     source_mtime, installed_by, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(fingerprint) DO NOTHING
                """,
                (
                    _as_str(data.get("name")) or "Assistant",
                    _as_str(data.get("nickname")),
                    _as_str(data.get("description")),
                    _as_str(data.get("personality")),
                    _as_str(data.get("scenario")),
                    json.dumps(_as_str_list(data.get("tags")), ensure_ascii=False),
                    _as_str(data.get("creator")),
                    _as_str(data.get("character_version")),
                    digest,
                    json.dumps(payload, ensure_ascii=False),
                    source_kind,
                    source,
                    float(source_mtime),
                    installed_by,
                    now,
                    now,
                ),
            )
            created = cursor.rowcount > 0
            self._db.execute("UPDATE cards SET character_id=? WHERE fingerprint=? AND character_id=''",
                             (uuid.uuid4().hex, digest))
            row = self._db.execute(
                "SELECT * FROM cards WHERE fingerprint = ? ORDER BY id LIMIT 1", (digest,)
            ).fetchone()
        return _record(row), created

    def install_card(
        self,
        card: CharacterCard,
        *,
        source_kind: str = "manual",
        source: str = "",
        installed_by: str = "",
        source_mtime: float = 0.0,
    ) -> tuple[CardRecord, bool]:
        return self.install_payload(
            card.payload,
            source_kind=source_kind,
            source=source,
            installed_by=installed_by,
            source_mtime=source_mtime,
        )

    def install_path(
        self,
        path: str | Path,
        *,
        source_kind: str = "file",
        installed_by: str = "",
    ) -> tuple[CardRecord, bool]:
        p = Path(path).expanduser().resolve()
        card = CharacterCard.load(p)
        return self.install_card(
            card,
            source_kind=source_kind,
            source=str(p),
            installed_by=installed_by,
            source_mtime=p.stat().st_mtime,
        )

    def install_bytes(
        self,
        data: bytes,
        *,
        source: str = "",
        source_kind: str = "url",
        installed_by: str = "",
    ) -> tuple[CardRecord, bool]:
        card = CharacterCard.from_bytes(data, source or "download")
        return self.install_card(
            card,
            source_kind=source_kind,
            source=source,
            installed_by=installed_by,
        )

    def use(self, card_id: int) -> CardRecord:
        with self._lock, self._db:
            row = self._db.execute("SELECT id FROM cards WHERE id = ?", (card_id,)).fetchone()
            if row is None:
                raise ValueError(f"character card #{card_id} does not exist")
            self._db.execute("UPDATE cards SET is_active = 0 WHERE is_active = 1")
            self._db.execute(
                "UPDATE cards SET is_active = 1, updated_at = ? WHERE id = ?",
                (int(time.time()), card_id),
            )
            row = self._db.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        return _record(row)

    def set_aliases(self, card_id: int, aliases: Any) -> CardRecord:
        """Replace the card's address aliases; its own name/nickname are dropped."""
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
            if row is None:
                raise ValueError(f"character card #{card_id} does not exist")
            cleaned = _normalise_aliases(
                aliases, name=row["name"] or "", nickname=row["nickname"] or ""
            )
            self._db.execute(
                "UPDATE cards SET aliases = ?, updated_at = ? WHERE id = ?",
                (json.dumps(cleaned, ensure_ascii=False), int(time.time()), card_id),
            )
            row = self._db.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        return _record(row)

    def add_alias(self, card_id: int, alias: str) -> CardRecord:
        record = self.get(card_id)
        if record is None:
            raise ValueError(f"character card #{card_id} does not exist")
        wanted = alias.strip()
        if wanted in record.aliases:
            return record
        cleaned = _normalise_aliases([wanted], name=record.name, nickname=record.nickname)
        if not cleaned:
            raise ValueError(
                f"{wanted!r} cannot be an alias: use at least two characters "
                "that differ from the card's name/nickname"
            )
        return self.set_aliases(card_id, [*record.aliases, *cleaned])

    def remove_alias(self, card_id: int, alias: str) -> CardRecord:
        record = self.get(card_id)
        if record is None:
            raise ValueError(f"character card #{card_id} does not exist")
        wanted = alias.strip()
        remaining = [item for item in record.aliases if item != wanted]
        if len(remaining) == len(record.aliases):
            raise ValueError(f"character card #{card_id} has no alias {wanted!r}")
        return self.set_aliases(card_id, remaining)

    def mark_story(self, card_id: int) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE cards SET story_referenced=1 WHERE id=?", (card_id,))

    def link_character(self, card_id: int, original_id: int) -> CardRecord:
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
            source = self._db.execute("SELECT character_id FROM cards WHERE id=?", (original_id,)).fetchone()
            if row is None or source is None:
                raise ValueError('unknown character card')
            if self._referenced(card_id):
                raise ValueError('cannot relink a referenced character version')
            self._db.execute('UPDATE cards SET character_id=? WHERE id=?', (source[0], card_id))
        result = self.get(card_id)
        assert result is not None
        return result

    def _referenced(self, card_id: int) -> bool:
        row = self._db.execute("SELECT story_referenced FROM cards WHERE id=?", (card_id,)).fetchone()
        if row and row[0]:
            return True
        for table in ("persona_bindings", "persona_switches"):
            exists = self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if exists and self._db.execute(
                f"SELECT 1 FROM {table} WHERE card_id = ? LIMIT 1", (card_id,)
            ).fetchone():
                return True
        return False

    def delete(self, card_id: int) -> bool:
        with self._lock, self._db:
            if self._referenced(card_id):
                raise ValueError("cannot delete a card referenced by a conversation or switch")
            # Conditional delete so a concurrent --use-card cannot make us remove
            # the row after we checked it; at most one row is ever active.
            cursor = self._db.execute(
                "DELETE FROM cards WHERE id = ? AND is_active = 0", (card_id,)
            )
            if cursor.rowcount:
                return True
            row = self._db.execute(
                "SELECT is_active FROM cards WHERE id = ?", (card_id,)
            ).fetchone()
            if row is None:
                return False
            raise ValueError("cannot delete the active card; switch with /card use first")

    def ensure_active(self) -> CardRecord | None:
        active = self.active()
        if active is not None:
            return active
        with self._lock:
            row = self._db.execute("SELECT id FROM cards ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return None
        return self.use(int(row["id"]))

    def bootstrap(self, path: str | Path, *, installed_by: str = "bootstrap") -> CardRecord | None:
        """Import the legacy card file when the library is empty, then ensure one is active."""
        p = Path(path).expanduser()
        if self.count() == 0 and p.is_file():
            self.install_path(p, source_kind="bootstrap", installed_by=installed_by)
        return self.ensure_active()

    def sync_bootstrap(self, path: str | Path) -> bool:
        """Re-import the bootstrap file when it changed on disk.

        Only applies while the bootstrap card is active, which keeps the old
        "edit character.json and it hot-reloads" workflow working.
        """
        active = self.active()
        if active is None or active.source_kind != "bootstrap" or not active.source:
            return False
        p = Path(path).expanduser()
        try:
            if p.resolve() != Path(active.source).resolve():
                return False
            mtime = p.stat().st_mtime
        except OSError:
            return False
        if abs(mtime - active.source_mtime) < 1e-6:
            return False
        card = CharacterCard.load(p)
        data = card.payload["data"]
        digest = fingerprint(card.payload)
        with self._lock, self._db:
            duplicate = self._db.execute(
                "SELECT id FROM cards WHERE fingerprint = ? AND id != ?", (digest, active.id)
            ).fetchone()
            if self._referenced(active.id) or (duplicate and self._referenced(int(duplicate[0]))):
                # Referenced versions are immutable; an explicit install creates a new version.
                return False
            # If the edited file now equals another card, that copy is redundant.
            self._db.execute(
                "DELETE FROM cards WHERE fingerprint = ? AND id != ?", (digest, active.id)
            )
            self._db.execute(
                """
                UPDATE cards SET name = ?, nickname = ?, description = ?, personality = ?,
                    scenario = ?, tags = ?, creator = ?, character_version = ?,
                    fingerprint = ?, payload = ?, source = ?, source_mtime = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    _as_str(data.get("name")) or "Assistant",
                    _as_str(data.get("nickname")),
                    _as_str(data.get("description")),
                    _as_str(data.get("personality")),
                    _as_str(data.get("scenario")),
                    json.dumps(_as_str_list(data.get("tags")), ensure_ascii=False),
                    _as_str(data.get("creator")),
                    _as_str(data.get("character_version")),
                    digest,
                    json.dumps(card.payload, ensure_ascii=False),
                    str(p.resolve()),
                    float(mtime),
                    int(time.time()),
                    active.id,
                ),
            )
        return True

    def close(self) -> None:
        with self._lock:
            self._db.close()
