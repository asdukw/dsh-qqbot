"""Single-database story transactions, immutable menus, and acknowledged outbox."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from relationship_rules import ChatProposal, Relationship, chat_effect
from story_models import RunState, StoryDefinition, StorySettings, digest

SCHEMA = '''
CREATE TABLE IF NOT EXISTS story_schema (version INTEGER NOT NULL);
INSERT INTO story_schema(version) SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM story_schema);
CREATE TABLE IF NOT EXISTS story_worlds (
 id TEXT PRIMARY KEY, conversation TEXT NOT NULL, character TEXT NOT NULL,
 fingerprint TEXT NOT NULL, definition TEXT NOT NULL, created INTEGER NOT NULL,
 UNIQUE(conversation, character, fingerprint)
);
CREATE TABLE IF NOT EXISTS story_relations (
 id TEXT PRIMARY KEY, conversation TEXT NOT NULL, character TEXT NOT NULL,
 user TEXT NOT NULL, data TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
 archived INTEGER NOT NULL DEFAULT 0, day TEXT NOT NULL DEFAULT '',
 gain INTEGER NOT NULL DEFAULT 0, last_positive INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS story_relation_current
 ON story_relations(conversation,character,user) WHERE archived=0;
CREATE TABLE IF NOT EXISTS story_runs (
 id TEXT PRIMARY KEY, relation_id TEXT NOT NULL REFERENCES story_relations(id),
 world_id TEXT NOT NULL REFERENCES story_worlds(id), state TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active',
 scene TEXT NOT NULL, created INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS story_run_current
 ON story_runs(relation_id) WHERE status!='archived';
CREATE TABLE IF NOT EXISTS story_menus (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES story_runs(id),
 revision INTEGER NOT NULL, token TEXT NOT NULL, choices TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'draft', expires INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS story_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, relation_id TEXT NOT NULL,
 run_id TEXT NOT NULL DEFAULT '', action TEXT NOT NULL UNIQUE,
 kind TEXT NOT NULL, summary TEXT NOT NULL, data TEXT NOT NULL,
 created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS story_actions (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL, status TEXT NOT NULL,
 error TEXT NOT NULL DEFAULT '', updated INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS story_outbox (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL, revision INTEGER NOT NULL,
 token TEXT NOT NULL, menu_id TEXT NOT NULL, chunks TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', receipts TEXT NOT NULL DEFAULT '[]',
 error TEXT NOT NULL DEFAULT '', policy INTEGER NOT NULL, created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS story_settings (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS story_event_relation ON story_events(relation_id,id);
CREATE INDEX IF NOT EXISTS story_delivery_status ON story_outbox(status,created);
CREATE INDEX IF NOT EXISTS story_menu_run ON story_menus(run_id,status);
CREATE TABLE IF NOT EXISTS story_permissions (
 conversation TEXT NOT NULL, user TEXT NOT NULL, age TEXT NOT NULL DEFAULT 'unknown',
 source TEXT NOT NULL DEFAULT '', opted_in INTEGER NOT NULL DEFAULT 0,
 revision INTEGER NOT NULL DEFAULT 0, updated INTEGER NOT NULL,
 PRIMARY KEY(conversation,user)
);
CREATE TABLE IF NOT EXISTS story_confirmations (
 id TEXT PRIMARY KEY, conversation TEXT NOT NULL, character TEXT NOT NULL,
 user TEXT NOT NULL, operation TEXT NOT NULL, relation_id TEXT NOT NULL,
 revision INTEGER NOT NULL, expires INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS story_operations (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', result TEXT NOT NULL DEFAULT '',
 created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS story_generation_drafts (
 id TEXT PRIMARY KEY, conversation TEXT NOT NULL, character TEXT NOT NULL,
 brief TEXT NOT NULL, graph TEXT NOT NULL DEFAULT '', warnings TEXT NOT NULL DEFAULT '[]',
 status TEXT NOT NULL DEFAULT 'brief_ready', error TEXT NOT NULL DEFAULT '', updated INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS story_generation_attempts (
 id TEXT PRIMARY KEY, conversation TEXT NOT NULL, purpose TEXT NOT NULL,
 details TEXT NOT NULL, elapsed REAL NOT NULL, created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS story_generation_log (
 id TEXT PRIMARY KEY, conversation TEXT NOT NULL, purpose TEXT NOT NULL,
 provider TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
 elapsed REAL NOT NULL, created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS story_announcements (
 world_id TEXT PRIMARY KEY REFERENCES story_worlds(id), token TEXT NOT NULL,
 chunks TEXT NOT NULL, receipts TEXT NOT NULL DEFAULT '[]',
 status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS story_audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, kind TEXT NOT NULL,
 target TEXT NOT NULL, created INTEGER NOT NULL
);
'''


def uid() -> str:
    return uuid.uuid4().hex


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


class StoryStore:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.execute('PRAGMA foreign_keys=ON')
        self._db.executescript(SCHEMA)
        if self._db.execute('SELECT MAX(version) FROM story_schema').fetchone()[0] != 1:
            self._db.close()
            raise ValueError('unsupported story schema version')
        self._db.commit()

    @contextmanager
    def transaction(self):
        with self._lock:
            self._db.execute('BEGIN IMMEDIATE')
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _one(self, query: str, args: tuple = ()) -> dict | None:
        with self._lock:
            row = self._db.execute(query, args).fetchone()
            return dict(row) if row else None

    def settings(self, defaults: StorySettings) -> StorySettings:
        row = self._one('SELECT data FROM story_settings WHERE id=1')
        return StorySettings.model_validate(json.loads(row['data'])) if row else defaults

    def save_settings(self, data: dict, defaults: StorySettings) -> None:
        settings = StorySettings.model_validate({**self.settings(defaults).model_dump(), **data})
        with self.transaction() as db:
            db.execute('INSERT INTO story_settings VALUES(1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                       (settings.model_dump_json(),))
            db.execute("INSERT INTO story_audit(actor,kind,target,created) VALUES('admin','settings','global',?)", (int(time.time()),))

    def world(self, conversation: str, character: str, fingerprint: str = '') -> dict | None:
        if fingerprint:
            return self._one('SELECT * FROM story_worlds WHERE conversation=? AND character=? AND fingerprint=?',
                             (conversation, character, fingerprint))
        return self._one('SELECT * FROM story_worlds WHERE conversation=? AND character=? ORDER BY created DESC,id DESC LIMIT 1',
                         (conversation, character))

    def shared_world(self, character: str, fingerprint: str) -> dict | None:
        """Newest world for this card, regardless of conversation."""
        return self._one(
            'SELECT * FROM story_worlds WHERE character=? AND fingerprint=? '
            'ORDER BY created DESC,id DESC LIMIT 1',
            (character, fingerprint),
        )

    def save_world(self, conversation: str, character: str, fingerprint: str, definition: StoryDefinition) -> dict:
        with self.transaction() as db:
            db.execute('INSERT OR IGNORE INTO story_worlds VALUES(?,?,?,?,?,?)',
                       (uid(), conversation, character, fingerprint, definition.model_dump_json(), int(time.time())))
        result = self.world(conversation, character, fingerprint)
        assert result is not None
        return result

    def relation(self, conversation: str, character: str, user: str) -> dict:
        from personas import target_of
        scope, chat_id = target_of(conversation)
        if not user.isascii() or not user.isdigit() or int(user) <= 0 or (scope == 'private' and str(chat_id) != user):
            raise ValueError('invalid relationship owner')
        with self.transaction() as db:
            db.execute('INSERT OR IGNORE INTO story_relations(id,conversation,character,user,data) VALUES(?,?,?,?,?)',
                       (uid(), conversation, character, user, Relationship().model_dump_json()))
            row = db.execute('SELECT * FROM story_relations WHERE conversation=? AND character=? AND user=? AND archived=0',
                             (conversation, character, user)).fetchone()
            return dict(row)

    def run(self, relation_id: str) -> dict | None:
        return self._one("SELECT * FROM story_runs WHERE relation_id=? AND status!='archived'", (relation_id,))

    def snapshot(self, run_id: str) -> dict:
        row = self._one('SELECT r.*,p.conversation,p.character,p.user,p.data AS relation_data,p.revision AS relation_revision '
                        'FROM story_runs r JOIN story_relations p ON p.id=r.relation_id WHERE r.id=? AND p.archived=0', (run_id,))
        if row is None:
            raise ValueError('存档不存在或已归档')
        world = self._one('SELECT * FROM story_worlds WHERE id=?', (row['world_id'],))
        assert world is not None
        row['world'] = world
        row['relation'] = Relationship.model_validate_json(row.pop('relation_data'))
        row['state_model'] = RunState.model_validate_json(row['state'])
        return row

    def menu(self, menu_id: str) -> dict | None:
        return self._one('SELECT * FROM story_menus WHERE id=?', (menu_id,))

    def current_menu(self, run_id: str) -> dict | None:
        return self._one("SELECT * FROM story_menus WHERE run_id=? AND status!='consumed' ORDER BY rowid DESC LIMIT 1", (run_id,))

    def quoted_menu(self, conversation: str, user: str, message_id: int) -> dict | None:
        with self._lock:
            rows = self._db.execute(
                "SELECT o.menu_id,o.receipts FROM story_outbox o JOIN story_runs r ON r.id=o.run_id "
                "JOIN story_relations p ON p.id=r.relation_id WHERE p.conversation=? AND p.user=? "
                "AND p.archived=0 AND r.status!='archived' AND o.status='sent' ORDER BY o.created DESC LIMIT 100",
                (conversation, user),
            ).fetchall()
        for row in rows:
            receipts = json.loads(row['receipts'])
            if receipts and int(receipts[-1]['message_id']) == message_id:
                return self.menu(row['menu_id'])
        return None

    def events(self, relation_id: str, limit: int = 80, *, canonical: bool = False) -> list[dict]:
        with self._lock:
            order = "(kind='chat') ASC,id DESC" if canonical else 'id DESC'
            rows = self._db.execute('SELECT kind,summary,created FROM story_events WHERE relation_id=? ORDER BY ' + order + ' LIMIT ?',
                                    (relation_id, min(80, max(1, limit)))).fetchall()
        return [dict(row) for row in reversed(rows)]

    def reserve(self, action: str, run_id: str) -> bool:
        with self.transaction() as db:
            row = db.execute('SELECT status,run_id FROM story_actions WHERE id=?', (action,)).fetchone()
            if row and (row['status'] != 'failed' or row['run_id'] != run_id):
                return False
            if row:
                db.execute("UPDATE story_actions SET status='generating',error='',updated=? WHERE id=?", (int(time.time()), action))
            else:
                db.execute('INSERT INTO story_actions VALUES(?,?,?,\'\',?)', (action, run_id, 'generating', int(time.time())))
            return True

    def fail_action(self, action: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE story_actions SET status='failed',error='generation or validation failed',updated=? WHERE id=? AND status='generating'",
                       (int(time.time()), action))

    def policy_revision(self, conversation: str, user: str) -> int:
        row = self._one('SELECT revision FROM story_permissions WHERE conversation=? AND user=?', (conversation, user))
        return int(row['revision']) if row else 0

    def permission(self, conversation: str, user: str, *, age: str | None = None, opt_in: bool | None = None) -> dict:
        if age is not None and age not in ('unknown', 'adult_declared', 'minor'):
            raise ValueError('invalid age declaration')
        with self.transaction() as db:
            db.execute('INSERT OR IGNORE INTO story_permissions(conversation,user,updated) VALUES(?,?,?)',
                       (conversation, user, int(time.time())))
            if age is not None or opt_in is not None:
                if conversation.startswith('group:') and opt_in:
                    raise ValueError('群聊不能开启成人偏好')
                row = db.execute('SELECT * FROM story_permissions WHERE conversation=? AND user=?', (conversation, user)).fetchone()
                new_age = age if age is not None else row['age']
                enabled = bool(opt_in if opt_in is not None else row['opted_in']) and new_age == 'adult_declared'
                db.execute('UPDATE story_permissions SET age=?,source=?,opted_in=?,revision=revision+1,updated=? WHERE conversation=? AND user=?',
                           (new_age, 'self_declaration' if new_age != 'unknown' else '', int(enabled), int(time.time()), conversation, user))
                db.execute('INSERT INTO story_audit(actor,kind,target,created) VALUES(?,?,?,?)',
                           (user, 'content_preference', conversation, int(time.time())))
                db.execute("UPDATE story_menus SET status='stale' WHERE run_id IN "
                           '(SELECT r.id FROM story_runs r JOIN story_relations p ON p.id=r.relation_id WHERE p.conversation=? AND p.user=?) '
                           "AND status IN ('draft','sent')", (conversation, user))
                db.execute("UPDATE story_runs SET status='paused' WHERE status='active' AND relation_id IN "
                           '(SELECT id FROM story_relations WHERE conversation=? AND user=?)', (conversation, user))
                # Invalidate in-flight sends. Existing safe scenes can be explicitly resumed.
                db.execute("UPDATE story_outbox SET status='paused' WHERE status='pending' AND run_id IN "
                           '(SELECT r.id FROM story_runs r JOIN story_relations p ON p.id=r.relation_id WHERE p.conversation=? AND p.user=?)',
                           (conversation, user))
            return dict(db.execute('SELECT * FROM story_permissions WHERE conversation=? AND user=?', (conversation, user)).fetchone())

    def _publish(self, db: sqlite3.Connection, run_id: str, revision: int, token: str, menu_id: str,
                 choices: list[dict], chunks: list[str], expires: int, policy: int) -> None:
        db.execute("UPDATE story_menus SET status='stale' WHERE run_id=? AND status IN ('draft','sent')", (run_id,))
        db.execute("UPDATE story_outbox SET status='superseded' WHERE run_id=? AND status IN ('pending','paused','rejected')", (run_id,))
        db.execute('INSERT INTO story_menus VALUES(?,?,?,?,?,?,?)', (menu_id, run_id, revision, token, dump(choices), 'draft', expires))
        db.execute('INSERT INTO story_outbox(id,run_id,revision,token,menu_id,chunks,policy,created) VALUES(?,?,?,?,?,?,?,?)',
                   (uid(), run_id, revision, token, menu_id, dump(chunks), policy, int(time.time())))

    def start(self, relation: dict, world: dict, state: RunState, scene: str, token: str,
              menu_id: str, choices: list[dict], chunks: list[str], expires: int, policy: int) -> str:
        with self.transaction() as db:
            current = db.execute('SELECT revision,archived FROM story_relations WHERE id=?', (relation['id'],)).fetchone()
            if not current or current['archived'] or current['revision'] != relation['revision']:
                raise ValueError('关系已变化，请重新开始')
            run_id = uid()
            db.execute('INSERT INTO story_runs(id,relation_id,world_id,state,scene,created,status) VALUES(?,?,?,?,?,?,?)',
                       (run_id, relation['id'], world['id'], state.model_dump_json(), scene, int(time.time()), 'active'))
            self._publish(db, run_id, 0, token, menu_id, choices, chunks, expires, policy)
        return run_id

    def commit_choice(self, snapshot: dict, old_menu: dict, action: str, state: RunState,
                      relation: Relationship, scene: str, summary: str, token: str,
                      menu_id: str, choices: list[dict], chunks: list[str], expires: int,
                      policy: int, ended: bool) -> None:
        with self.transaction() as db:
            run = db.execute('SELECT revision,status FROM story_runs WHERE id=?', (snapshot['id'],)).fetchone()
            rel = db.execute('SELECT revision,archived FROM story_relations WHERE id=?', (snapshot['relation_id'],)).fetchone()
            menu = db.execute('SELECT status,expires FROM story_menus WHERE id=?', (old_menu['id'],)).fetchone()
            permission = db.execute('SELECT revision FROM story_permissions WHERE conversation=? AND user=?',
                                    (snapshot['conversation'], snapshot['user'])).fetchone()
            current_policy = permission[0] if permission else 0
            if (not run or run['status'] != 'active' or run['revision'] != snapshot['revision']
                    or not rel or rel['archived'] or rel['revision'] != snapshot['relation_revision']
                    or not menu or menu['status'] != 'sent' or menu['expires'] < time.time() or current_policy != policy):
                raise ValueError('剧情、关系、菜单或权限已变化，请继续当前存档')
            revision = snapshot['revision'] + 1
            db.execute("UPDATE story_menus SET status='consumed' WHERE id=?", (old_menu['id'],))
            db.execute('UPDATE story_relations SET data=?,revision=revision+1 WHERE id=?', (relation.model_dump_json(), snapshot['relation_id']))
            db.execute('UPDATE story_runs SET state=?,scene=?,revision=?,status=? WHERE id=?',
                       (state.model_dump_json(), scene, revision, 'ended' if ended else 'active', snapshot['id']))
            db.execute('INSERT INTO story_events(relation_id,run_id,action,kind,summary,data,created) VALUES(?,?,?,?,?,?,?)',
                       (snapshot['relation_id'], snapshot['id'], action, 'choice', summary[:600],
                        dump({'state': state.model_dump(), 'relation': relation.model_dump()}), int(time.time())))
            db.execute("UPDATE story_actions SET status='complete',updated=? WHERE id=? AND status='generating'", (int(time.time()), action))
            self._publish(db, snapshot['id'], revision, token, menu_id, choices, chunks, expires, policy)

    def outbox(self, run_id: str) -> dict | None:
        return self._one("SELECT * FROM story_outbox WHERE run_id=? AND status IN ('pending','sending','unknown','paused','rejected') ORDER BY rowid DESC LIMIT 1", (run_id,))

    def sending(self, delivery: str) -> bool:
        with self.transaction() as db:
            return db.execute("UPDATE story_outbox SET status='sending' WHERE id=? AND status='pending'", (delivery,)).rowcount == 1

    def receipt(self, delivery: str, receipts: list[dict]) -> None:
        with self.transaction() as db:
            db.execute("UPDATE story_outbox SET receipts=?,status='pending' WHERE id=? AND status='sending'", (dump(receipts), delivery))

    def delivered(self, delivery: dict) -> None:
        with self.transaction() as db:
            db.execute("UPDATE story_outbox SET status='sent',error='' WHERE id=?", (delivery['id'],))
            db.execute("UPDATE story_menus SET status='sent' WHERE id=? AND status='draft'", (delivery['menu_id'],))

    def delivery_failed(self, delivery: dict, *, unknown: bool) -> None:
        with self.transaction() as db:
            db.execute('UPDATE story_outbox SET status=?,error=? WHERE id=?',
                       ('unknown' if unknown else 'rejected', 'delivery unknown' if unknown else 'delivery rejected', delivery['id']))
            if unknown:
                db.execute("UPDATE story_runs SET status='unknown' WHERE id=? AND status IN ('active','ended')", (delivery['run_id'],))

    def replay(self, snapshot: dict, token: str, menu_id: str, choices: list[dict], chunks: list[str],
               expires: int, policy: int) -> None:
        with self.transaction() as db:
            row = db.execute('SELECT revision,status FROM story_runs WHERE id=?', (snapshot['id'],)).fetchone()
            rel = db.execute('SELECT archived FROM story_relations WHERE id=?', (snapshot['relation_id'],)).fetchone()
            if not row or not rel or rel['archived'] or row['revision'] != snapshot['revision'] or row['status'] == 'archived':
                raise ValueError('存档已变化')
            # Explicit resume can repeat prose, never effects. Supersede even unknown sends.
            db.execute("UPDATE story_outbox SET status='superseded' WHERE run_id=? AND status!='sent'", (snapshot['id'],))
            db.execute('UPDATE story_runs SET status=? WHERE id=?',
                       ('ended' if not choices else 'active', snapshot['id']))
            self._publish(db, snapshot['id'], snapshot['revision'], token, menu_id, choices, chunks, expires, policy)

    def pause(self, run_id: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE story_runs SET status='paused' WHERE id=? AND status IN ('active','ended')", (run_id,))
            db.execute("UPDATE story_outbox SET status='paused' WHERE run_id=? AND status='pending'", (run_id,))

    def recover(self) -> None:
        with self.transaction() as db:
            db.execute("UPDATE story_runs SET status='unknown' WHERE id IN (SELECT run_id FROM story_outbox WHERE status='sending')")
            db.execute("UPDATE story_outbox SET status='unknown',error='interrupted during send' WHERE status='sending'")
            db.execute("UPDATE story_announcements SET status='unknown',error='interrupted during send' WHERE status='sending'")
            db.execute("UPDATE story_actions SET status='failed',error='interrupted before commit' WHERE status='generating'")
            db.execute("UPDATE story_operations SET status='failed',result='interrupted; inspect before retry' WHERE status='executing'")

    def confirmation(self, relation: dict, operation: str) -> str:
        code = uid()[:12]
        with self.transaction() as db:
            db.execute('DELETE FROM story_confirmations WHERE expires<?', (int(time.time()),))
            db.execute('INSERT INTO story_confirmations VALUES(?,?,?,?,?,?,?,?)',
                       (code, relation['conversation'], relation['character'], relation['user'], operation,
                        relation['id'], relation['revision'], int(time.time()) + 300))
        return code

    def reset(self, relation: dict, code: str, operation: str = 'restart') -> None:
        with self.transaction() as db:
            current = db.execute('SELECT revision,archived FROM story_relations WHERE id=?', (relation['id'],)).fetchone()
            if not current or current['archived'] or current['revision'] != relation['revision']:
                raise ValueError('关系已变化，请重新确认')
            row = db.execute('SELECT * FROM story_confirmations WHERE id=?', (code,)).fetchone()
            if (not row or row['relation_id'] != relation['id'] or row['operation'] != operation
                    or row['revision'] != relation['revision'] or row['expires'] < time.time()):
                raise ValueError('确认已失效，请重新请求')
            db.execute('DELETE FROM story_confirmations WHERE id=?', (code,))
            run_ids = [row[0] for row in db.execute('SELECT id FROM story_runs WHERE relation_id=?', (relation['id'],))]
            for run_id in run_ids:
                db.execute("UPDATE story_runs SET status='archived' WHERE id=?", (run_id,))
                db.execute("UPDATE story_menus SET status='stale' WHERE run_id=?", (run_id,))
                db.execute("UPDATE story_outbox SET status='superseded' WHERE run_id=? AND status!='sent'", (run_id,))
            db.execute('UPDATE story_relations SET archived=1,revision=revision+1 WHERE id=?', (relation['id'],))
            if operation == 'forget':
                ids = [row[0] for row in db.execute('SELECT id FROM story_relations WHERE conversation=? AND character=? AND user=?',
                                                  (relation['conversation'], relation['character'], relation['user']))]
                for relation_id in ids:
                    for run_row in db.execute('SELECT id FROM story_runs WHERE relation_id=?', (relation_id,)).fetchall():
                        run_id = run_row[0]
                        db.execute("UPDATE story_outbox SET chunks='[]',receipts='[]',status='superseded' WHERE run_id=?", (run_id,))
                        db.execute("UPDATE story_menus SET choices='[]',status='stale' WHERE run_id=?", (run_id,))
                        db.execute("UPDATE story_runs SET state='{}',scene='',status='archived' WHERE id=?", (run_id,))
                    db.execute('DELETE FROM story_events WHERE relation_id=?', (relation_id,))
                    db.execute('UPDATE story_relations SET data=?,archived=1 WHERE id=?', (Relationship().model_dump_json(), relation_id))
            db.execute('INSERT INTO story_audit(actor,kind,target,created) VALUES(?,?,?,?)',
                       (relation['user'], operation, relation['conversation'] + ':' + relation['character'], int(time.time())))

    def chat(self, relation: dict, proposal: ChatProposal, action: str, settings: StorySettings,
             now: int | None = None) -> Relationship:
        now = int(time.time()) if now is None else now
        day = datetime.fromtimestamp(now, ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d')
        with self.transaction() as db:
            row = db.execute('SELECT * FROM story_relations WHERE id=? AND archived=0', (relation['id'],)).fetchone()
            if row is None:
                raise ValueError('relationship archived')
            result = Relationship.model_validate_json(row['data'])
            before = result.model_copy(deep=True)
            if db.execute('SELECT 1 FROM story_events WHERE action=?', (action,)).fetchone():
                return result
            # Duplicate evidence cannot farm rewards, even using a fresh message id.
            evidence_hash = digest({'kind': proposal.kind, 'evidence': proposal.evidence.strip()})
            duplicate = db.execute("SELECT 1 FROM story_events WHERE relation_id=? AND kind='chat' AND json_extract(data,'$.evidence_hash')=? AND created>=?",
                                   (relation['id'], evidence_hash, now - 86400)).fetchone()
            da, dt = chat_effect(proposal)
            gain = row['gain'] if row['day'] == day else 0
            positive = max(0, da) + max(0, dt)
            if duplicate or (positive and (gain + positive > settings.daily_chat_gain
                                           or now - row['last_positive'] < settings.chat_cooldown)):
                da, dt = 0, 0
                positive = 0
            result.affinity = max(0, min(100, result.affinity + da))
            result.trust = max(0, min(100, result.trust + dt))
            if proposal.impression:
                result.impression = proposal.impression
            db.execute('UPDATE story_relations SET data=?,revision=revision+1,day=?,gain=?,last_positive=? WHERE id=?',
                       (result.model_dump_json(), day, gain + positive, now if positive else row['last_positive'], relation['id']))
            db.execute('INSERT INTO story_events(relation_id,action,kind,summary,data,created) VALUES(?,?,?,?,?,?)',
                       (relation['id'], action, 'chat',
                        f'好感 {result.affinity - before.affinity:+d} / 信任 {result.trust - before.trust:+d}；{proposal.reason}',
                        dump({'evidence_hash': evidence_hash, 'relation': result.model_dump(),
                              'affinity_delta': result.affinity - before.affinity,
                              'trust_delta': result.trust - before.trust}), now))
            return result

    def queue(self, kind: str, payload: dict) -> str:
        if kind not in ('settings', 'retry', 'restart', 'link', 'import_legacy', 'regenerate'):
            raise ValueError('unknown operation')
        key = uid()
        with self.transaction() as db:
            db.execute('INSERT INTO story_operations(id,kind,payload,created) VALUES(?,?,?,?)', (key, kind, dump(payload), int(time.time())))
        return key

    def queued(self) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._db.execute("SELECT * FROM story_operations WHERE status='queued' ORDER BY created LIMIT 10")]

    def claim_operation(self, key: str) -> bool:
        with self.transaction() as db:
            return db.execute("UPDATE story_operations SET status='executing' WHERE id=? AND status='queued'", (key,)).rowcount == 1

    def finish_operation(self, key: str, success: bool) -> None:
        with self.transaction() as db:
            db.execute('UPDATE story_operations SET status=?,result=? WHERE id=?',
                       ('complete' if success else 'failed', 'ok' if success else 'operation failed; inspect bot logs', key))
            db.execute("INSERT INTO story_audit(actor,kind,target,created) VALUES('admin','operation',?,?)", (key, int(time.time())))

    def admin(self, *, offset: int = 0) -> dict:
        offset = max(0, offset)
        with self._lock:
            runs = [dict(row) for row in self._db.execute(
                'SELECT r.id,r.revision,r.status,r.created,p.conversation,p.character,p.user,p.data '
                'FROM story_runs r JOIN story_relations p ON p.id=r.relation_id ORDER BY r.created DESC,r.id LIMIT 30 OFFSET ?', (offset,))]
            for row in runs:
                relation = Relationship.model_validate_json(row.pop('data'))
                row['stage'] = relation.stage
            worlds = [dict(row) for row in self._db.execute("SELECT id,conversation,character,fingerprint,created,json_extract(definition, '$.title') AS title FROM story_worlds ORDER BY created DESC LIMIT 30 OFFSET ?", (offset,))]
            operations = [dict(row) for row in self._db.execute('SELECT id,kind,status,result,created FROM story_operations ORDER BY created DESC LIMIT 30')]
            deliveries = [dict(row) for row in self._db.execute("SELECT id,run_id,status,error,created FROM story_outbox WHERE status NOT IN ('sent','superseded') ORDER BY created DESC LIMIT 30")]
            audit = [dict(row) for row in self._db.execute('SELECT actor,kind,target,created FROM story_audit ORDER BY id DESC LIMIT 30')]
            generations = [dict(row) for row in self._db.execute('SELECT conversation,purpose,provider,model,status,elapsed,created FROM story_generation_log ORDER BY created DESC LIMIT 30')]
            attempts = [dict(row) for row in self._db.execute('SELECT conversation,purpose,details,elapsed,created FROM story_generation_attempts ORDER BY created DESC,rowid DESC LIMIT 30')]
            for item in attempts:
                item['details'] = json.loads(item['details'])
            drafts = [dict(row) for row in self._db.execute('SELECT conversation,character,status,error,warnings,updated FROM story_generation_drafts ORDER BY updated DESC LIMIT 30')]
            for item in drafts:
                item['warnings'] = json.loads(item['warnings'])
        return {'attempts': attempts, 'drafts': drafts, 'runs': runs, 'worlds': worlds, 'operations': operations, 'deliveries': deliveries,
                'audit': audit, 'generations': generations, 'offset': offset}

    def import_legacy(self, snapshot: dict, confirmed: str) -> None:
        if confirmed != snapshot['user']:
            raise ValueError('must confirm the target user id')
        with self.transaction() as db:
            if db.execute('SELECT 1 FROM story_events WHERE relation_id=?', (snapshot['relation_id'],)).fetchone():
                raise ValueError('only empty relationship histories can import legacy scores')
            row = db.execute('SELECT affinity FROM user_relations WHERE user_id=?', (snapshot['user'],)).fetchone()
            if row is None:
                raise ValueError('no legacy relationship')
            relation = Relationship(affinity=max(0, min(100, int(row[0]))))
            db.execute('UPDATE story_relations SET data=?,revision=revision+1 WHERE id=? AND archived=0',
                       (relation.model_dump_json(), snapshot['relation_id']))
            db.execute('INSERT INTO story_events(relation_id,action,kind,summary,data,created) VALUES(?,?,?,?,?,?)',
                       (snapshot['relation_id'], 'legacy:' + snapshot['relation_id'], 'migration',
                        '管理员明确导入旧好感；无恋爱/成人授权', '{}', int(time.time())))

    def pending_runs(self) -> list[str]:
        with self._lock:
            return [row[0] for row in self._db.execute("SELECT DISTINCT run_id FROM story_outbox WHERE status='pending' LIMIT 10")]

    def detail(self, key: str, *, kind: str = 'run', offset: int = 0) -> dict:
        if kind in ('world', 'graph'):
            world = self._one('SELECT * FROM story_worlds WHERE id=?', (key,))
            if world is None:
                raise ValueError('world not found')
            definition = json.loads(world.pop('definition'))
            nodes = definition.pop('nodes')
            if kind == 'graph':
                # Read-only graph view: definitions are capped at 24 nodes, so
                # returning the whole graph is bounded and avoids paging loops.
                return {**world, 'definition': definition, 'nodes': nodes, 'total': len(nodes)}
            return {**world, 'definition': definition, 'nodes': nodes[max(0, offset):max(0, offset) + 8], 'total': len(nodes)}
        snapshot = self.snapshot(key)
        with self.transaction() as db:
            db.execute("INSERT INTO story_audit(actor,kind,target,created) VALUES('admin','view_detail',?,?)", (key, int(time.time())))
        with self._lock:
            rows = self._db.execute('SELECT id,kind,summary,created FROM story_events WHERE relation_id=? ORDER BY id DESC LIMIT 30 OFFSET ?',
                                    (snapshot['relation_id'], max(0, offset))).fetchall()
        menu = self.current_menu(key)
        return {'id': key, 'conversation': snapshot['conversation'], 'character': snapshot['character'],
                'user': snapshot['user'], 'status': snapshot['status'], 'revision': snapshot['revision'],
                'relation': snapshot['relation'].model_dump(), 'stage': snapshot['relation'].stage,
                'scene': snapshot['scene'], 'state': json.loads(snapshot['state']),
                'menu': menu, 'events': [dict(row) for row in rows], 'offset': max(0, offset)}

    def generation_log(self, conversation: str, purpose: str, provider: str,
                       model: str, status: str, elapsed: float) -> None:
        with self.transaction() as db:
            db.execute('INSERT INTO story_generation_log VALUES(?,?,?,?,?,?,?,?)',
                       (uid(), conversation, purpose, provider, model, status, elapsed, int(time.time())))
            db.execute('DELETE FROM story_generation_log WHERE created<?', (int(time.time()) - 30 * 86400,))

    def announcement(self, world_id: str) -> dict | None:
        return self._one('SELECT * FROM story_announcements WHERE world_id=?', (world_id,))

    def prepare_announcement(self, world_id: str, token: str, chunks: list[str]) -> dict:
        with self.transaction() as db:
            db.execute('INSERT OR IGNORE INTO story_announcements(world_id,token,chunks) VALUES(?,?,?)',
                       (world_id, token, dump(chunks)))
        result = self.announcement(world_id)
        assert result is not None
        return result

    def update_announcement(self, world_id: str, status: str, receipts: list[dict], *, token: str, error: str = '') -> None:
        with self.transaction() as db:
            db.execute('UPDATE story_announcements SET status=?,receipts=?,token=?,error=? WHERE world_id=?',
                       (status, dump(receipts), token, error, world_id))

    def record_announcement(self, world_id: str, token: str, chunks: list[str], receipts: list[dict]) -> None:
        with self.transaction() as db:
            db.execute("INSERT INTO story_announcements(world_id,token,chunks,receipts,status) VALUES(?,?,?,?,'sent') "
                       "ON CONFLICT(world_id) DO UPDATE SET status='sent',receipts=excluded.receipts,token=excluded.token,error=''",
                       (world_id, token, dump(chunks), dump(receipts)))

    def draft(self, key: str) -> dict | None:
        return self._one('SELECT * FROM story_generation_drafts WHERE id=?', (key,))

    def save_draft(self, key: str, conversation: str, character: str, brief: str,
                   graph: str = '', warnings: list | None = None) -> None:
        with self.transaction() as db:
            db.execute('INSERT INTO story_generation_drafts(id,conversation,character,brief,graph,warnings,status,updated) '
                       'VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET brief=excluded.brief,graph=excluded.graph,'
                       "warnings=excluded.warnings,status=excluded.status,error='',updated=excluded.updated",
                       (key, conversation, character, brief, graph, dump(warnings or []),
                        'complete' if graph else 'brief_ready', int(time.time())))

    def draft_failed(self, key: str, code: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE story_generation_drafts SET status='graph_failed',error=?,updated=? WHERE id=?",
                       (code, int(time.time()), key))

    def generation_attempt(self, conversation: str, purpose: str, details: dict, elapsed: float) -> None:
        with self.transaction() as db:
            db.execute('INSERT INTO story_generation_attempts VALUES(?,?,?,?,?,?)',
                       (uid(), conversation, purpose, dump(details), elapsed, int(time.time())))
            db.execute('DELETE FROM story_generation_attempts WHERE created<?', (int(time.time()) - 30 * 86400,))
