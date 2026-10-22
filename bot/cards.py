"""Character card loading for V1 / V2 / V3, including PNG-embedded cards.

The internal representation is always Character Card V3 (``data`` object).
Missing V3 fields get defaults; V2 cards are upgraded losslessly.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

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
    def from_card(cls, book: Any) -> "Lorebook":
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


def _iter_png_chunks(path: Path):
    with path.open("rb") as fh:
        if fh.read(8) != PNG_SIGNATURE:
            raise CardError(f"{path} is not a PNG file")
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
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _read_png_card(path: Path) -> dict[str, Any]:
    found: dict[str, bytes] = {}
    for kind, data in _iter_png_chunks(path):
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
                    payload = zlib.decompress(payload)
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
    raise CardError(f"no character card data found in {path}")


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
    def load(cls, path: str | Path) -> "CharacterCard":
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
        payload = normalise(raw)
        return cls(
            path=p,
            mtime=p.stat().st_mtime,
            payload=payload,
            lorebook=Lorebook.from_card(payload["data"].get("character_book")),
        )


class CardStore:
    """Load a card and reload it automatically when the file changes."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._card: CharacterCard | None = None
        self._mtime = 0.0

    def get(self) -> CharacterCard:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if self._card is None or mtime != self._mtime:
            self._card = CharacterCard.load(self.path)
            self._mtime = mtime
        return self._card
