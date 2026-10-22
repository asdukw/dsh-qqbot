import { DatabaseSync } from 'node:sqlite'
import { mkdirSync } from 'node:fs'
import { dirname } from 'node:path'

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
    CREATE TABLE IF NOT EXISTS facts (
      session_id TEXT NOT NULL,
      key TEXT NOT NULL,
      value TEXT NOT NULL,
      confidence REAL NOT NULL DEFAULT 1.0,
      source TEXT,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      PRIMARY KEY (session_id, key)
    );
    CREATE INDEX IF NOT EXISTS idx_facts_session ON facts (session_id, updated_at DESC);
  `)
  return db
}

function sessionOf(exec) {
  return String(exec?.agent?.id ?? 'unknown')
}

function readFacts(sessionId, key) {
  const conn = getDb()
  if (key) {
    const row = conn.prepare(
      'SELECT key, value, updated_at FROM facts WHERE session_id = ? AND key = ?'
    ).get(sessionId, key)
    return row ? [row] : []
  }
  return conn.prepare(
    'SELECT key, value, updated_at FROM facts WHERE session_id = ? ORDER BY updated_at DESC LIMIT 200'
  ).all(sessionId)
}

function writeFact(sessionId, key, value) {
  const now = Date.now()
  getDb().prepare(`
    INSERT INTO facts (session_id, key, value, confidence, source, created_at, updated_at)
    VALUES (?, ?, ?, 1.0, 'chat', ?, ?)
    ON CONFLICT(session_id, key) DO UPDATE SET
      value = excluded.value,
      updated_at = excluded.updated_at
  `).run(sessionId, key, value, now, now)
}

function forgetFact(sessionId, key) {
  const result = getDb().prepare(
    'DELETE FROM facts WHERE session_id = ? AND key = ?'
  ).run(sessionId, key)
  return Number(result.changes) > 0
}

function formatFacts(rows) {
  const lines = []
  let used = 0
  for (const row of rows.slice(0, MAX_PROMPT_FACTS)) {
    const line = `- ${row.key}: ${row.value}`
    if (used + line.length > MAX_PROMPT_CHARS) break
    lines.push(line)
    used += line.length + 1
  }
  return lines.join('\n')
}

function renderProfile(sessionId) {
  const rows = readFacts(sessionId)
  if (!rows.length) {
    return '（暂无长期画像。用户分享稳定事实时，调用 profile_write 记录。）'
  }
  return formatFacts(rows)
}

function text(value) {
  return [{ type: 'text', text: String(value) }]
}

const readTool = {
  name: 'profile_read',
  description:
    'Read the current user\'s long-term profile. Omit key to read every remembered fact.',
  parameters: {
    type: 'object',
    properties: {
      key: { type: 'string', description: 'Optional fact key, e.g. 称呼 / 所在城市' },
    },
    additionalProperties: false,
  },
  output: { schema: { type: 'string' }, render: (_args, value) => text(value) },
  async execute(args, exec) {
    const sessionId = sessionOf(exec)
    const key = String(args?.key ?? '').trim()
    const rows = readFacts(sessionId, key || undefined)
    if (!rows.length) return key ? `没有找到：${key}` : '（暂无长期画像）'
    return formatFacts(rows)
  },
}

const writeTool = {
  name: 'profile_write',
  description:
    'Create or update one stable fact about the current user: name, nickname, city, school, ' +
    'work, birthday, preferences, relationships. Use a short stable key such as "称呼".',
  parameters: {
    type: 'object',
    properties: {
      key: { type: 'string', description: 'Short stable key, e.g. 称呼 / 所在城市 / 生日' },
      value: { type: 'string', description: 'The fact to remember' },
    },
    required: ['key', 'value'],
    additionalProperties: false,
  },
  output: { schema: { type: 'string' }, render: (_args, value) => text(value) },
  async execute(args, exec) {
    const sessionId = sessionOf(exec)
    const key = String(args?.key ?? '').trim()
    const value = String(args?.value ?? '').trim()
    if (!key || !value) throw new Error('key and value are required')
    writeFact(sessionId, key, value)
    return `已记住：${key} = ${value}`
  },
}

const forgetTool = {
  name: 'profile_forget',
  description: 'Remove one remembered fact about the current user by key.',
  parameters: {
    type: 'object',
    properties: { key: { type: 'string', description: 'The key to remove' } },
    required: ['key'],
    additionalProperties: false,
  },
  output: { schema: { type: 'string' }, render: (_args, value) => text(value) },
  async execute(args, exec) {
    const sessionId = sessionOf(exec)
    const key = String(args?.key ?? '').trim()
    if (!key) throw new Error('key is required')
    return forgetFact(sessionId, key) ? `已忘记：${key}` : `没有找到：${key}`
  },
}

const MEMORY_NOTE =
  '用户画像存在 SQLite 里，由 profile_read / profile_write / profile_forget 维护。' +
  '事件、约定和重要片段写到工作目录的 memory/notes/ 下；需要回忆时用 glob/grep/read。' +
  '用户说出稳定事实时用 profile_write，不要只记在对话里。'

export const name = 'user-profile'
export const inject = ['tools', 'systemPrompt']

export function apply(ctx) {
  getDb()
  ctx.tools.register(readTool)
  ctx.tools.register(writeTool)
  ctx.tools.register(forgetTool)

  ctx.on('agent/created', ({ agent }) => {
    const sessionId = String(agent.id)
    agent.ctx.systemPrompt.variable('user_profile', () => renderProfile(sessionId))
    agent.ctx.systemPrompt.section({
      name: 'user-profile',
      order: 8000,
      text:
        '关于当前用户的长期记忆（仅在与对话相关时使用，不要生硬复述）：\n{{user_profile}}\n\n' +
        MEMORY_NOTE,
    })
  })
}
