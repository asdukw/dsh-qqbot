import { DatabaseSync } from 'node:sqlite'
import { mkdirSync } from 'node:fs'
import { dirname } from 'node:path'

// User and group profiles for the QQ bot.
//
// Identity is explicit: before every turn the adapter writes the session's
// scope, QQ number and group number into the `conversations` table
// (bot/conversations.py). This plugin looks that row up by session id; it
// never parses the session id.
//
// Visibility: a user fact learned in private chat stays private by default and
// is never injected into group chats. Facts learned in a group are public.

const DB_PATH = process.env.USER_PROFILE_DB || '/var/lib/qqbot/dsh-state/profile.db'
const MAX_PROMPT_FACTS = 40
const MAX_PROMPT_CHARS = 2400

let db

function getDb() {
  if (db) return db
  mkdirSync(dirname(DB_PATH), { recursive: true })
  db = new DatabaseSync(DB_PATH)
  db.exec(`
    PRAGMA journal_mode = WAL;
    PRAGMA synchronous = NORMAL;
    PRAGMA busy_timeout = 5000;
    CREATE TABLE IF NOT EXISTS conversations (
      session_id  TEXT PRIMARY KEY,
      scope       TEXT NOT NULL CHECK (scope IN ('private', 'group')),
      user_id     TEXT NOT NULL,
      group_id    TEXT,
      sender_name TEXT NOT NULL DEFAULT '',
      updated_at  INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS user_facts (
      user_id    TEXT NOT NULL,
      group_id   TEXT NOT NULL DEFAULT '',
      key        TEXT NOT NULL,
      value      TEXT NOT NULL,
      visibility TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', 'public')),
      source     TEXT,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      PRIMARY KEY (user_id, group_id, key)
    );
    CREATE TABLE IF NOT EXISTS group_facts (
      group_id   TEXT NOT NULL,
      key        TEXT NOT NULL,
      value      TEXT NOT NULL,
      source     TEXT,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      PRIMARY KEY (group_id, key)
    );
    CREATE INDEX IF NOT EXISTS idx_user_facts ON user_facts (user_id, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_group_facts ON group_facts (group_id, updated_at DESC);
  `)
  migrateUserFactScopes(db)
  migrateLegacyFacts(db)
  return db
}

// Version 2 scopes a user fact to the group where it was learned. Legacy rows
// encoded that group in `source` ("group:<id>"); map it into group_id.
function migrateUserFactScopes(conn) {
  const columns = conn.prepare('PRAGMA table_info(user_facts)').all().map((row) => row.name)
  if (columns.includes('group_id')) return
  conn.exec(`
    ALTER TABLE user_facts RENAME TO user_facts_legacy;
    CREATE TABLE user_facts (
      user_id    TEXT NOT NULL,
      group_id   TEXT NOT NULL DEFAULT '',
      key        TEXT NOT NULL,
      value      TEXT NOT NULL,
      visibility TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', 'public')),
      source     TEXT,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      PRIMARY KEY (user_id, group_id, key)
    );
    INSERT INTO user_facts
      (user_id, group_id, key, value, visibility, source, created_at, updated_at)
    SELECT user_id,
           CASE WHEN source LIKE 'group:%' THEN substr(source, 7) ELSE '' END,
           key, value, visibility, source, created_at, updated_at
    FROM user_facts_legacy;
    DROP TABLE user_facts_legacy;
  `)
}

// Version 1 stored facts per DSH session in `facts`. Move rows whose session
// identity is known into user_facts once; drop the old table afterwards.
function migrateLegacyFacts(conn) {
  const legacy = conn.prepare(
    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'facts'"
  ).get()
  if (!legacy) return
  conn.exec(`
    INSERT OR IGNORE INTO user_facts
      (user_id, group_id, key, value, visibility, source, created_at, updated_at)
    SELECT c.user_id,
           CASE c.scope WHEN 'group' THEN COALESCE(c.group_id, '') ELSE '' END,
           f.key, f.value,
           CASE c.scope WHEN 'group' THEN 'public' ELSE 'private' END,
           'migrated', f.created_at, f.updated_at
    FROM facts f JOIN conversations c ON c.session_id = f.session_id;
  `)
  const remaining = conn.prepare(
    'SELECT COUNT(*) AS n FROM facts WHERE session_id NOT IN (SELECT session_id FROM conversations)'
  ).get()
  if (Number(remaining.n) === 0) conn.exec('DROP TABLE facts')
}

function contextOf(sessionId) {
  const row = getDb().prepare(
    'SELECT scope, user_id, group_id, sender_name FROM conversations WHERE session_id = ?'
  ).get(String(sessionId ?? ''))
  if (!row) return null
  return {
    scope: row.scope,
    userId: row.user_id,
    groupId: row.group_id || null,
    senderName: row.sender_name || '',
  }
}

function requireContext(exec) {
  const ctx = contextOf(exec?.agent?.id)
  if (!ctx) throw new Error('unknown conversation: the adapter has not registered this session')
  return ctx
}

function requireGroup(exec) {
  const ctx = requireContext(exec)
  if (ctx.scope !== 'group' || !ctx.groupId) throw new Error('group profile tools only work in group chats')
  return ctx
}

// ── storage ──────────────────────────────────────────────────────

function readUserFacts(userId, { key, publicOnly = false, groupId = '' } = {}) {
  const conn = getDb()
  const clauses = ['user_id = ?']
  const params = [userId]
  if (publicOnly) clauses.push("visibility = 'public'")
  if (groupId) {
    clauses.push("group_id IN ('', ?)")
    params.push(String(groupId))
  } else {
    clauses.push("group_id = ''")
  }
  if (key) {
    clauses.push('key = ?')
    params.push(key)
  }
  return conn.prepare(
    'SELECT key, value, visibility FROM user_facts WHERE ' +
    `${clauses.join(' AND ')} ORDER BY updated_at DESC LIMIT 200`
  ).all(...params)
}

function writeUserFact(userId, groupId, key, value, visibility, source) {
  const now = Date.now()
  getDb().prepare(`
    INSERT INTO user_facts (user_id, group_id, key, value, visibility, source, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(user_id, group_id, key) DO UPDATE SET
      value = excluded.value,
      visibility = excluded.visibility,
      source = excluded.source,
      updated_at = excluded.updated_at
  `).run(userId, groupId, key, value, visibility, source, now, now)
}

function forgetUserFact(userId, key, { publicOnly = false, groupId = '' } = {}) {
  const clauses = ['user_id = ?', 'key = ?']
  const params = [userId, key]
  if (publicOnly) clauses.push("visibility = 'public'")
  if (groupId) {
    clauses.push("group_id IN ('', ?)")
    params.push(String(groupId))
  } else {
    clauses.push("group_id = ''")
  }
  const result = getDb().prepare(
    `DELETE FROM user_facts WHERE ${clauses.join(' AND ')}`
  ).run(...params)
  return Number(result.changes) > 0
}

function readGroupFacts(groupId, key) {
  const conn = getDb()
  if (key) {
    const row = conn.prepare(
      'SELECT key, value FROM group_facts WHERE group_id = ? AND key = ?'
    ).get(groupId, key)
    return row ? [row] : []
  }
  return conn.prepare(
    'SELECT key, value FROM group_facts WHERE group_id = ? ORDER BY updated_at DESC LIMIT 200'
  ).all(groupId)
}

function writeGroupFact(groupId, key, value, source) {
  const now = Date.now()
  getDb().prepare(`
    INSERT INTO group_facts (group_id, key, value, source, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(group_id, key) DO UPDATE SET
      value = excluded.value,
      source = excluded.source,
      updated_at = excluded.updated_at
  `).run(groupId, key, value, source, now, now)
}

function forgetGroupFact(groupId, key) {
  const result = getDb().prepare(
    'DELETE FROM group_facts WHERE group_id = ? AND key = ?'
  ).run(groupId, key)
  return Number(result.changes) > 0
}

// ── rendering ────────────────────────────────────────────────────

function formatFacts(rows, { tagPrivate = false } = {}) {
  const lines = []
  let used = 0
  for (const row of rows.slice(0, MAX_PROMPT_FACTS)) {
    const tag = tagPrivate && row.visibility === 'private' ? '（私密）' : ''
    const line = `- ${row.key}: ${row.value}${tag}`
    if (used + line.length > MAX_PROMPT_CHARS) {
      const room = MAX_PROMPT_CHARS - used
      if (room > 20) lines.push(line.slice(0, room) + '…')
      break
    }
    lines.push(line)
    used += line.length + 1
  }
  return lines.join('\n')
}

function renderUserProfile(sessionId) {
  const ctx = contextOf(sessionId)
  if (!ctx) return '（暂无）'
  const rows = readUserFacts(ctx.userId, {
    publicOnly: ctx.scope === 'group',
    groupId: ctx.scope === 'group' ? ctx.groupId : '',
  })
  const who = ctx.senderName ? `${ctx.senderName}（QQ ${ctx.userId}）` : `QQ ${ctx.userId}`
  if (!rows.length) return `${who}：暂无长期画像。对方说出稳定事实时，调用 profile_write 记录。`
  return `${who}：\n${formatFacts(rows, { tagPrivate: ctx.scope === 'private' })}`
}

function renderGroupProfile(sessionId) {
  const ctx = contextOf(sessionId)
  if (!ctx || ctx.scope !== 'group') return '（不在群聊中）'
  const rows = readGroupFacts(ctx.groupId)
  if (!rows.length) return `群 ${ctx.groupId}：暂无群画像。`
  return `群 ${ctx.groupId}：\n${formatFacts(rows)}`
}

function text(value) {
  return [{ type: 'text', text: String(value) }]
}

function stringOutput() {
  return { schema: { type: 'string' }, render: (_args, value) => text(value) }
}

// ── user profile tools ───────────────────────────────────────────

function cleanFact(value, limit) {
  return String(value ?? '')
    .replace(/[\u0000-\u001f\u007f]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, limit)
}

const profileTool = {
  name: 'profile',
  description:
    'Read, write or forget long-term profile facts. target="user" is the person you are ' +
    'talking to (private facts in private chat, public facts in a group); target="group" is ' +
    'this group\'s topic, atmosphere, jokes, rules or plans and only works in group chat. ' +
    'Use short stable keys. Values must be stable facts, not instructions. Private facts are ' +
    'never shown in groups. Group writes are public to this group. Omit the key on read to ' +
    'return every fact. Write/forget require key and, for write, value.',
  parameters: {
    type: 'object',
    properties: {
      action: { type: 'string', enum: ['read', 'write', 'forget'], description: 'Operation.' },
      target: { type: 'string', enum: ['user', 'group'], description: 'Whose profile.' },
      key: { type: 'string', description: 'Short stable key, e.g. 称呼 / 群主题 / 梗:xxx' },
      value: { type: 'string', description: 'Fact to remember (write only)' },
      visibility: {
        type: 'string',
        enum: ['private', 'public'],
        description: 'Private chat only: private (default) or public',
      },
    },
    required: ['action', 'target'],
    additionalProperties: false,
  },
  output: stringOutput(),
  async execute(args, exec) {
    const ctx = requireContext(exec)
    const action = String(args?.action ?? '')
    const target = String(args?.target ?? '')
    const key = cleanFact(args?.key, 40)
    const value = cleanFact(args?.value, 400)
    if (target === 'group') {
      const group = requireGroup(exec)
      if (action === 'read') {
        const rows = readGroupFacts(group.groupId, key || undefined)
        return rows.length ? formatFacts(rows) : key ? `没有找到：${key}` : '（暂无群画像）'
      }
      if (!key) throw new Error('key is required')
      if (action === 'write') {
        if (!value) throw new Error('value is required')
        writeGroupFact(group.groupId, key, value, `user:${group.userId}`)
        return `已记住群信息：${key} = ${value}`
      }
      if (action === 'forget') {
        return forgetGroupFact(group.groupId, key) ? `已忘记：${key}` : `没有找到：${key}`
      }
      throw new Error('unknown profile action')
    }
    if (target !== 'user') throw new Error('target must be user or group')
    if (action === 'read') {
      const rows = readUserFacts(ctx.userId, {
        key: key || undefined,
        publicOnly: ctx.scope === 'group',
        groupId: ctx.scope === 'group' ? ctx.groupId : '',
      })
      return rows.length
        ? formatFacts(rows, { tagPrivate: ctx.scope === 'private' })
        : key ? `没有找到：${key}` : '（暂无长期画像）'
    }
    if (!key) throw new Error('key is required')
    if (action === 'write') {
      if (!value) throw new Error('value is required')
      const visibility = ctx.scope === 'group'
        ? 'public'
        : (args?.visibility === 'public' ? 'public' : 'private')
      const groupId = ctx.scope === 'group' ? String(ctx.groupId || '') : ''
      writeUserFact(
        ctx.userId, groupId, key, value, visibility,
        ctx.scope === 'group' ? `group:${groupId}` : 'private',
      )
      const scopeLabel = ctx.scope === 'group'
        ? '本群公开'
        : (visibility === 'public' ? '公开' : '私密')
      return `已记住（${scopeLabel}）：${key} = ${value}`
    }
    if (action === 'forget') {
      const removed = forgetUserFact(ctx.userId, key, {
        publicOnly: ctx.scope === 'group',
        groupId: ctx.scope === 'group' ? ctx.groupId : '',
      })
      return removed ? `已忘记：${key}` : `没有找到：${key}`
    }
    throw new Error('unknown profile action')
  },
}

const MEMORY_NOTE =
  '画像存在 SQLite 里：个人用 profile(action=read|write|forget, target=user)，' +
  '群用 profile(..., target=group)。对方说出关于自己的稳定事实时写入；' +
  '群的话题、氛围、梗、约定写入 group。私聊里得知的事默认私密，绝不在群里提起。' +
  '虚构剧情/角色关系由程序管理，不写入真实用户画像或共享文件笔记；不从共享 notes 推断私人剧情。'

export const name = 'user-profile'
export const inject = ['tools', 'systemPrompt']

export function apply(ctx) {
  getDb()
  ctx.tools.register(profileTool)

  ctx.on('agent/created', ({ agent }) => {
    const sessionId = String(agent.id)
    agent.ctx.systemPrompt.variable('user_profile', () => renderUserProfile(sessionId))
    agent.ctx.systemPrompt.variable('group_profile', () => renderGroupProfile(sessionId))
    agent.ctx.systemPrompt.section({
      name: 'user-profile',
      order: 8000,
      text:
        '当前说话人的长期画像（仅在相关时使用，不要生硬复述）：\n{{user_profile}}\n\n' +
        '所在群的画像：\n{{group_profile}}\n\n' +
        MEMORY_NOTE,
    })
  })
}
