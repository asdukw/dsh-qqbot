"""Conversation-scoped cards and durable, acknowledged persona cutovers."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cards import CardRecord, CardRegistry, CharacterCard

_SCHEMA = """
CREATE TABLE IF NOT EXISTS persona_bindings (
    conversation_id TEXT PRIMARY KEY,
    card_id INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    payload TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    cutoff INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS persona_switches (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    card_id INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    payload TEXT NOT NULL,
    actor TEXT NOT NULL,
    expected_token TEXT NOT NULL,
    applied_token TEXT NOT NULL DEFAULT '',
    phase TEXT NOT NULL DEFAULT 'queued',
    announcement TEXT NOT NULL DEFAULT '',
    opening TEXT NOT NULL DEFAULT '',
    receipts TEXT NOT NULL DEFAULT '[]',
    opening_pending INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_persona_switch_pending
ON persona_switches(conversation_id)
WHERE phase NOT IN ('complete', 'failed', 'cancelled');
"""


def target_of(conversation_id: str) -> tuple[str, int]:
    if not re.fullmatch(r'(group|private):[1-9]\d*', conversation_id):
        raise ValueError('conversation must be group:<QQ id> or private:<QQ id>')
    scope, number = conversation_id.split(':')
    return scope, int(number)


@dataclass(frozen=True)
class Persona:
    conversation_id: str
    record: CardRecord
    card: CharacterCard
    token: str
    epoch: int = 0
    cutoff: int = 0


class PersonaStore:
    def __init__(self, path: str | Path, cards: CardRegistry):
        self.cards = cards
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.executescript(_SCHEMA)
        columns = {row[1] for row in self._db.execute('PRAGMA table_info(persona_switches)')}
        if 'applied_token' not in columns:
            self._db.execute("ALTER TABLE persona_switches ADD COLUMN applied_token TEXT NOT NULL DEFAULT ''")
        if 'story_prepared' not in columns:
            self._db.execute("ALTER TABLE persona_switches ADD COLUMN story_prepared TEXT NOT NULL DEFAULT ''")
        self._db.commit()

    def effective(self, conversation_id: str) -> Persona:
        target_of(conversation_id)
        with self._lock:
            row = self._db.execute(
                'SELECT * FROM persona_bindings WHERE conversation_id = ?', (conversation_id,)
            ).fetchone()
        record = self.cards.get(row['card_id']) if row else self.cards.active()
        if record is None:
            raise ValueError('no effective character card')
        if row:
            card = CharacterCard.from_payload(json.loads(row['payload']))
            return Persona(conversation_id, record, card,
                           f"{row['epoch']}:{row['fingerprint']}", row['epoch'], row['cutoff'])
        return Persona(conversation_id, record, record.card, f'0:{record.fingerprint}')

    def enqueue(self, conversation_id: str, card: CardRecord, *, actor: str,
                request_id: str = '') -> dict[str, Any]:
        target_of(conversation_id)
        current = self.effective(conversation_id)
        _ = card.card  # Validate before publishing work.
        with self._lock, self._db:
            if request_id:
                existing = self._db.execute(
                    'SELECT * FROM persona_switches WHERE id = ?', (request_id,)
                ).fetchone()
                if existing:
                    if existing['conversation_id'] != conversation_id or existing['fingerprint'] != card.fingerprint:
                        raise ValueError('request id already used for another switch')
                    return dict(existing)
            pending = self._db.execute(
                "SELECT * FROM persona_switches WHERE conversation_id = ? "
                "AND phase NOT IN ('complete', 'failed', 'cancelled')", (conversation_id,)
            ).fetchone()
            if pending:
                if pending['fingerprint'] == card.fingerprint:
                    return dict(pending)
                raise ValueError('this conversation already has an unfinished switch')
            stamp = int(time.time())
            phase = 'complete' if current.card.payload == card.payload else 'queued'
            job_id = request_id or uuid.uuid4().hex
            self._db.execute(
                'INSERT INTO persona_switches '
                '(id, conversation_id, card_id, fingerprint, payload, actor, expected_token, '
                'phase, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (job_id, conversation_id, card.id, card.fingerprint,
                 json.dumps(card.payload, ensure_ascii=False), actor, current.token, phase, stamp, stamp),
            )
        return self.job(job_id)

    def job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute('SELECT * FROM persona_switches WHERE id = ?', (job_id,)).fetchone()
        if row is None:
            raise ValueError('unknown switch request')
        return dict(row)

    def pending(self, conversation_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM persona_switches WHERE conversation_id = ? "
                "AND phase NOT IN ('complete', 'failed', 'cancelled') "
                "AND (phase != 'queued' OR applied_token != '')",
                (conversation_id,),
            ).fetchone()
        return dict(row) if row else None

    def queued(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM persona_switches WHERE phase = 'queued' ORDER BY created_at, id"
            ).fetchall()
        return [dict(row) for row in rows]

    def claim(self, job_id: str) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE persona_switches SET phase = 'preparing', updated_at = ? "
                "WHERE id = ? AND phase = 'queued'", (int(time.time()), job_id),
            )
        return cursor.rowcount == 1

    def update(self, job_id: str, **fields: Any) -> None:
        allowed = {'phase', 'announcement', 'opening', 'receipts', 'opening_pending', 'error', 'story_prepared'}
        if not fields or not fields.keys() <= allowed:
            raise ValueError('invalid switch update')
        with self._lock, self._db:
            self._db.execute(
                'UPDATE persona_switches SET ' + ', '.join(f'{key} = ?' for key in fields)
                + ', updated_at = ? WHERE id = ?', (*fields.values(), int(time.time()), job_id),
            )

    def bind(self, job_id: str, cutoff: int) -> Persona:
        job = self.job(job_id)
        current = self.effective(job['conversation_id'])
        if current.token != job['expected_token']:
            raise ValueError('persona changed while switch was queued')
        with self._lock, self._db:
            self._db.execute(
                'INSERT INTO persona_bindings '
                '(conversation_id, card_id, fingerprint, payload, epoch, cutoff, updated_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(conversation_id) DO UPDATE SET '
                'card_id=excluded.card_id, fingerprint=excluded.fingerprint, payload=excluded.payload, '
                'epoch=excluded.epoch, cutoff=excluded.cutoff, updated_at=excluded.updated_at',
                (job['conversation_id'], job['card_id'], job['fingerprint'], job['payload'],
                 current.epoch + 1, cutoff, int(time.time())),
            )
            self._db.execute(
                "UPDATE persona_switches SET phase = 'bound', applied_token = ?, updated_at = ? WHERE id = ?",
                (f"{current.epoch + 1}:{job['fingerprint']}", int(time.time()), job_id),
            )
        return self.effective(job['conversation_id'])

    def story_seen(self, conversation_id: str, fingerprint: str) -> bool:
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM persona_switches WHERE conversation_id=? AND fingerprint=? "
                "AND phase='complete' AND story_prepared NOT IN ('','null') LIMIT 1",
                (conversation_id, fingerprint),
            ).fetchone() is not None

    def opening(self, conversation_id: str) -> str:
        with self._lock:
            row = self._db.execute(
                "SELECT opening FROM persona_switches WHERE conversation_id = ? "
                "AND opening_pending = 1 AND phase = 'complete' ORDER BY created_at DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        return str(row['opening']) if row else ''

    def consume_opening(self, conversation_id: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                'UPDATE persona_switches SET opening_pending = 0 WHERE conversation_id = ?',
                (conversation_id,),
            )

    def recover(self) -> None:
        """Never replay a send with an unknown delivery outcome after restart."""
        with self._lock, self._db:
            self._db.execute("UPDATE persona_switches SET phase = 'queued' WHERE phase = 'preparing'")
            self._db.execute(
                "UPDATE persona_switches SET phase = 'unknown', error = 'interrupted during send; inspect receipts' "
                "WHERE phase IN ('announcing', 'opening')"
            )
            self._db.execute(
                "UPDATE persona_switches SET phase = 'paused', error = 'interrupted; explicit resume required' "
                "WHERE phase IN ('announced', 'bound')"
            )

    def resume(self, job_id: str, *, delivered: bool = False) -> None:
        job = self.job(job_id)
        if job['phase'] not in ('paused', 'unknown'):
            raise ValueError('only paused/unknown jobs can be reconciled')
        persona = self.effective(job['conversation_id'])
        bound = bool(job['applied_token']) and persona.token == job['applied_token']
        if job['applied_token'] and not bound:
            raise ValueError('applied binding no longer matches this job')
        if delivered:
            if not bound:
                raise ValueError('announcement accepted; use resume to apply the binding')
            self.update(job_id, phase='complete', opening_pending=int(bool(job['opening'])), error='')
        else:
            # Explicit operator authorization to retry the incomplete phase.
            self.update(job_id, phase='queued', error='')

    def close(self) -> None:
        with self._lock:
            self._db.close()
