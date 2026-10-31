import { existsSync } from 'node:fs'
import { DatabaseSync } from 'node:sqlite'

// Read-only long-term memory tools for the QQ bot.
//
// memory_days (one dated summary per conversation) and memory_items
// (user/group facts with provenance) are written by bot/memory_archive.py.
// The model can search them on demand; writes are handled by the extraction
// pipeline and the existing profile tools, never from here.

const PROFILE_DB = process.env.USER_PROFILE_DB || '/var/lib/qqbot/dsh-state/profile.db'
const MEMORY_DB = process.env.QQBOT_MEMORY_DB || '/var/lib/qqbot/memory.db'
const TZ = 'Asia/Shanghai'
const DEFAULT_LIMIT = 8
const MAX_LIMIT = 20

let profileDb
let memoryDb

function open(path) {
  const db = new DatabaseSync(path)
  db.exec('PRAGMA busy_timeout = 5000')
  return db
}

function getProfileDb() {
  if (!existsSync(PROFILE_DB)) throw new Error(`profile database not found: ${PROFILE_DB}`)
  if (!profileDb) profileDb = open(PROFILE_DB)
  return profileDb
}

function getMemoryDb() {
  if (!existsSync(MEMORY_DB)) throw new Error(`memory database not found: ${MEMORY_DB}`)
  if (!memoryDb) memoryDb = open(MEMORY_DB)
  return memoryDb
}

function contextOf(sessionId) {
  const row = getProfileDb()
    .prepare('SELECT scope, user_id, group_id, sender_name FROM conversations WHERE session_id = ?')
    .get(String(sessionId ?? ''))
  if (!row) return null
  return {
    scope: String(row.scope),
    userId: String(row.user_id ?? ''),
    groupId: row.group_id == null ? '' : String(row.group_id),
    senderName: String(row.sender_name ?? ''),
  }
}

function parseSessionId(sessionId) {
  const match = /^(group|private)-(\d+)(?:-g\d+)?$/.exec(String(sessionId ?? ''))
  if (!match) return null
  return match[1] === 'group'
    ? { scope: 'group', userId: '', groupId: match[2], senderName: '' }
    : { scope: 'private', userId: match[2], groupId: '', senderName: '' }
}

function requireContext(exec) {
  const sessionId = String(exec?.agent?.id ?? '')
  const ctx = contextOf(sessionId) || parseSessionId(sessionId)
  if (!ctx) throw new Error('unknown conversation: the adapter has not registered this session')
  if (ctx.scope === 'group' && !ctx.groupId) throw new Error('group id is missing for this session')
  return ctx
}

function conversationIdOf(ctx) {
  return ctx.scope === 'group' ? `group:${ctx.groupId}` : `private:${ctx.userId}`
}

function clampLimit(value) {
  const number = Number(value)
  if (!Number.isFinite(number)) return DEFAULT_LIMIT
  return Math.max(1, Math.min(MAX_LIMIT, Math.floor(number)))
}

function dayBounds(since, until) {
  const start = /^\d{4}-\d{2}-\d{2}$/.test(since || '') ? since : '1970-01-01'
  const end = /^\d{4}-\d{2}-\d{2}$/.test(until || '') ? until : '9999-12-31'
  const startTs = Math.floor(new Date(`${start}T00:00:00+08:00`).getTime() / 1000)
  const endTs = Math.floor(new Date(`${end}T23:59:59+08:00`).getTime() / 1000)
  return { start, end, startTs, endTs }
}

function terms(query) {
  const result = []
  for (const chunk of String(query || '').match(/[一-鿿]+|[A-Za-z0-9]{2,}/g) || []) {
    if (chunk.length <= 2) continue
    const step = chunk.length <= 12 ? 1 : 2
    for (let i = 0; i + 3 <= chunk.length; i += step) {
      const term = chunk.slice(i, i + 3)
      if (!result.includes(term)) result.push(term)
      if (result.length >= 8) return result
    }
  }
  return result
}

function matchingIds(query) {
  const text = String(query || '').trim()
  if (!text) return null
  const db = getMemoryDb()
  const found = new Set()
  const words = terms(text)
  if (words.length) {
    const match = words.map((word) => `"${word.replaceAll('"', '""')}"`).join(' OR ')
    try {
      for (const row of db.prepare('SELECT id FROM memory_fts WHERE memory_fts MATCH ? LIMIT 200').all(match)) {
        found.add(String(row.id))
      }
      if (found.size) return found
    } catch {
      // trigram MATCH can reject short/invalid queries; fall through to LIKE.
    }
  }
  try {
    for (const row of db.prepare('SELECT id FROM memory_fts WHERE text LIKE ? LIMIT 200').all(`%${text}%`)) {
      found.add(String(row.id))
    }
    return found
  } catch {
    // Older SQLite builds may not ship FTS5; let the caller return all rows
    // rather than failing the whole tool.
    return null
  }
}

function isoTs(epoch) {
  return new Date(Number(epoch) * 1000).toLocaleDateString('sv-SE', { timeZone: TZ })
}

function search(ctx, { query = '', since = '', until = '', limit = DEFAULT_LIMIT } = {}) {
  const { end, start, startTs, endTs } = dayBounds(since, until)
  const db = getMemoryDb()
  const conversation = conversationIdOf(ctx)
  const matched = matchingIds(query)
  const rows = []
  const days = db.prepare(
    'SELECT id,day,summary,topics,open_loops FROM memory_days ' +
    'WHERE conversation_id=? AND day>=? AND day<=? ORDER BY day DESC LIMIT 120'
  ).all(conversation, start, end)
  for (const row of days) {
    if (matched && !matched.has(`day:${row.id}`)) continue
    rows.push({
      kind: 'day',
      date: String(row.day),
      importance: 3,
      text: String(row.summary),
      extra: JSON.parse(row.topics || '[]').concat(JSON.parse(row.open_loops || '[]')),
    })
  }
  const items = ctx.scope === 'group'
    ? db.prepare(
      "SELECT id,key,value,importance,occurred_at FROM memory_items " +
      "WHERE invalid_at IS NULL AND ((scope='group' AND group_id=?) OR " +
      "(scope='user' AND visibility='public' AND group_id IN ('',?)))"
    ).all(ctx.groupId, ctx.groupId)
    : db.prepare(
      "SELECT id,key,value,importance,occurred_at FROM memory_items " +
      "WHERE invalid_at IS NULL AND scope='user' AND user_id=? AND group_id=''"
    ).all(ctx.userId)
  for (const row of items) {
    if (Number(row.occurred_at) < startTs || Number(row.occurred_at) > endTs) continue
    if (matched && !matched.has(`item:${row.id}`)) continue
    rows.push({
      kind: 'item',
      date: isoTs(row.occurred_at),
      importance: Number(row.importance) || 0,
      text: `${row.key}: ${row.value}`,
      extra: [],
    })
  }
  rows.sort((a, b) => (b.importance - a.importance) || b.date.localeCompare(a.date))
  return rows.slice(0, clampLimit(limit))
}

function formatRows(rows) {
  if (!rows.length) return '（没有找到相关记忆）'
  return rows.map((row) => {
    const extra = row.extra?.length ? `（${row.extra.join('；')}）` : ''
    return `- ${row.date}｜${row.text}${extra}`
  }).join('\n')
}

function userItems(ctx, { query = '', since = '', until = '', limit = DEFAULT_LIMIT } = {}) {
  const { end, start, startTs, endTs } = dayBounds(since, until)
  const db = getMemoryDb()
  const matched = matchingIds(query)
  const rows = ctx.scope === 'group'
    ? db.prepare(
      "SELECT id,key,value,importance,occurred_at FROM memory_items " +
      "WHERE invalid_at IS NULL AND scope='user' AND visibility='public' AND group_id IN ('',?)"
    ).all(ctx.groupId)
    : db.prepare(
      "SELECT id,key,value,importance,occurred_at FROM memory_items " +
      "WHERE invalid_at IS NULL AND scope='user' AND user_id=? AND group_id=''"
    ).all(ctx.userId)
  return rows
    .filter((row) => Number(row.occurred_at) >= startTs && Number(row.occurred_at) <= endTs)
    .filter((row) => !matched || matched.has(`item:${row.id}`))
    .sort((a, b) => (Number(b.importance) - Number(a.importance)) ||
      (Number(b.occurred_at) - Number(a.occurred_at)))
    .slice(0, clampLimit(limit))
    .map((row) => ({
      kind: 'item',
      date: isoTs(row.occurred_at),
      importance: Number(row.importance) || 0,
      text: `${row.key}: ${row.value}`,
      extra: [],
    }))
}

function timeline(ctx, { since = '', until = '', limit = DEFAULT_LIMIT } = {}) {
  const { end, start } = dayBounds(since, until)
  const rows = getMemoryDb().prepare(
    'SELECT day,summary,topics,open_loops FROM memory_days ' +
    'WHERE conversation_id=? AND day>=? AND day<=? ORDER BY day DESC LIMIT ?'
  ).all(conversationIdOf(ctx), start, end, clampLimit(limit))
  return rows.map((row) => ({
    kind: 'day',
    date: String(row.day),
    importance: 3,
    text: String(row.summary),
    extra: JSON.parse(row.topics || '[]').concat(JSON.parse(row.open_loops || '[]')),
  }))
}

function recentMemory(sessionId) {
  try {
    const ctx = contextOf(sessionId)
    if (!ctx) return ''
    const since = new Date(Date.now() - 7 * 86400000).toLocaleDateString('sv-SE', { timeZone: TZ })
    const rows = search(ctx, { since, limit: 8 })
    if (!rows.length) return ''
    return formatRows(rows)
  } catch {
    return ''
  }
}

function text(value) {
  return [{ type: 'text', text: String(value) }]
}

function stringOutput() {
  return { schema: { type: 'string' }, render: (_args, value) => text(value) }
}

const searchTool = {
  name: 'memory_search',
  description:
    'Search long-term memory for the current conversation and speaker: dated daily summaries ' +
    'and consolidated facts. Use it to recall what happened days or months ago. ' +
    'Group chats only see this group and public facts; private chats only see this private user. ' +
    'since/until are YYYY-MM-DD.',
  parameters: {
    type: 'object',
    properties: {
      query: { type: 'string', description: 'Keywords; empty returns the most important recent memories' },
      since: { type: 'string', description: 'Start date, YYYY-MM-DD' },
      until: { type: 'string', description: 'End date, YYYY-MM-DD' },
      limit: { type: 'integer', description: '1-20, default 8' },
    },
    additionalProperties: false,
  },
  output: stringOutput(),
  async execute(args, exec) {
    const ctx = requireContext(exec)
    return formatRows(search(ctx, args || {}))
  },
}

const timelineTool = {
  name: 'memory_timeline',
  description:
    'List dated daily summaries for the current conversation. Use it for "what did we talk about ' +
    'last week / a month ago" before drilling into details.',
  parameters: {
    type: 'object',
    properties: {
      since: { type: 'string', description: 'Start date, YYYY-MM-DD' },
      until: { type: 'string', description: 'End date, YYYY-MM-DD' },
      limit: { type: 'integer', description: '1-20, default 8' },
    },
    additionalProperties: false,
  },
  output: stringOutput(),
  async execute(args, exec) {
    const ctx = requireContext(exec)
    return formatRows(timeline(ctx, args || {}))
  },
}

const personTool = {
  name: 'memory_person',
  description:
    'Read consolidated long-term facts about the current speaker (not other users): preferences, ' +
    'events, plans and relationship notes. Group chats only return public facts from this group; ' +
    'private chats return private facts.',
  parameters: {
    type: 'object',
    properties: {
      query: { type: 'string', description: 'Optional keywords' },
      since: { type: 'string', description: 'Start date, YYYY-MM-DD' },
      until: { type: 'string', description: 'End date, YYYY-MM-DD' },
      limit: { type: 'integer', description: '1-20, default 8' },
    },
    additionalProperties: false,
  },
  output: stringOutput(),
  async execute(args, exec) {
    const ctx = requireContext(exec)
    return formatRows(userItems(ctx, args || {}))
  },
}

export const name = 'memory-archive'
export const inject = ['tools', 'systemPrompt']

export function apply(ctx) {
  for (const tool of [searchTool, timelineTool, personTool]) ctx.tools.register(tool)
  ctx.on('agent/created', ({ agent }) => {
    const sessionId = String(agent.id)
    agent.ctx.systemPrompt.variable('memory_recent', () => recentMemory(sessionId))
    agent.ctx.systemPrompt.section({
      name: 'memory-archive',
      order: 8002,
      text:
        '长期记忆（按需使用，不要生硬复述；需要更早的细节时调用 memory_search / ' +
        'memory_timeline / memory_person）：\n{{memory_recent}}',
    })
  })
}
