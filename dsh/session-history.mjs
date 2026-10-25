import { createReadStream, existsSync } from 'node:fs'
import { readdir, stat } from 'node:fs/promises'
import { join } from 'node:path'
import { createInterface } from 'node:readline'
import { DatabaseSync } from 'node:sqlite'

// On-demand history for the QQ bot.
//
// DSH's own context only contains the active session generation. Older turns
// live in `$DSH_HOME/sessions/<workspace>/<session>/session.v4.jsonl`, which is
// outside the sandboxed file workspace, so the model cannot reach it with
// read/grep. These tools expose it read-only, scoped to the current QQ
// conversation:
//
// - history_sessions: list current and older session files for this chat
// - history_read:     read one session's user/assistant messages, with filters
// - chat_search:      search memory.db chat_events (includes group messages
//                     the bot was not addressed in; 90-day retention)
//
// Plugins run inside the DSH runtime as the qqbot user, so they can read both
// stores directly. Nothing here writes.

const SESSIONS_ROOT =
  process.env.DSH_SESSIONS_ROOT ||
  join(process.env.DSH_HOME || '/var/lib/qqbot/dsh-home', 'sessions')
const PROFILE_DB = process.env.USER_PROFILE_DB || '/var/lib/qqbot/dsh-state/profile.db'
const MEMORY_DB = process.env.QQBOT_MEMORY_DB || '/var/lib/qqbot/memory.db'
const TZ = 'Asia/Shanghai'
const DEFAULT_LIMIT = 16
const MAX_LIMIT = 60
const MAX_TEXT_CHARS = 700

let profileDb
let memoryDb

function openDb(path) {
  // Both stores are WAL databases owned by qqbot. A plain connection is more
  // reliable than a read-only one here (WAL readers may need the -shm file);
  // every query below is a SELECT.
  const db = new DatabaseSync(path)
  db.exec('PRAGMA busy_timeout = 5000')
  return db
}

function getProfileDb() {
  if (!existsSync(PROFILE_DB)) throw new Error(`profile database not found: ${PROFILE_DB}`)
  if (!profileDb) profileDb = openDb(PROFILE_DB)
  return profileDb
}

function getMemoryDb() {
  if (!existsSync(MEMORY_DB)) throw new Error(`memory database not found: ${MEMORY_DB}`)
  if (!memoryDb) memoryDb = openDb(MEMORY_DB)
  return memoryDb
}

// ── conversation identity ────────────────────────────────────────

function contextOf(sessionId) {
  const row = getProfileDb()
    .prepare(
      'SELECT scope, user_id, group_id, sender_name FROM conversations WHERE session_id = ?'
    )
    .get(String(sessionId ?? ''))
  if (!row) return null
  return {
    scope: String(row.scope),
    userId: String(row.user_id ?? ''),
    groupId: row.group_id === null || row.group_id === undefined ? null : String(row.group_id),
    senderName: String(row.sender_name ?? ''),
  }
}

function parseSessionId(sessionId) {
  const match = /^(group|private)-(\d+)(?:-g\d+)?$/.exec(String(sessionId ?? ''))
  if (!match) return null
  return match[1] === 'group'
    ? { scope: 'group', userId: '', groupId: match[2], senderName: '' }
    : { scope: 'private', userId: match[2], groupId: null, senderName: '' }
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

function sessionPrefix(ctx) {
  return ctx.scope === 'group' ? `group-${ctx.groupId}` : `private-${ctx.userId}`
}

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function matchesConversation(sessionId, ctx) {
  const prefix = sessionPrefix(ctx)
  return (
    sessionId === prefix ||
    new RegExp(`^${escapeRegExp(prefix)}-g\\d+$`).test(sessionId)
  )
}

// ── session JSONL discovery ──────────────────────────────────────

async function sessionFiles() {
  if (!existsSync(SESSIONS_ROOT)) return []
  let workspaces
  try {
    workspaces = await readdir(SESSIONS_ROOT, { withFileTypes: true })
  } catch {
    return []
  }
  const found = new Map()
  for (const workspace of workspaces) {
    if (!workspace.isDirectory()) continue
    const workspaceDir = join(SESSIONS_ROOT, workspace.name)
    let sessions
    try {
      sessions = await readdir(workspaceDir, { withFileTypes: true })
    } catch {
      continue
    }
    for (const session of sessions) {
      if (!session.isDirectory()) continue
      const dir = join(workspaceDir, session.name)
      let names
      try {
        names = await readdir(dir)
      } catch {
        continue
      }
      const name = names.find((item) => /^session.*\.jsonl$/.test(item))
      if (!name) continue
      const file = join(dir, name)
      let info
      try {
        info = await stat(file)
      } catch {
        continue
      }
      const previous = found.get(session.name)
      if (!previous || info.mtimeMs > previous.mtimeMs) {
        found.set(session.name, {
          sessionId: session.name,
          file,
          size: info.size,
          mtimeMs: info.mtimeMs,
        })
      }
    }
  }
  return [...found.values()]
}

async function scanSessionMeta(file) {
  let start = 0
  let last = 0
  let turns = 0
  const reader = createInterface({
    input: createReadStream(file, { encoding: 'utf8' }),
    crlfDelay: Infinity,
  })
  try {
    for await (const line of reader) {
      if (!line.trim()) continue
      let event
      try {
        event = JSON.parse(line)
      } catch {
        continue
      }
      const time = Number(event.time || 0)
      if (event.type === 'session' && !start) start = Number(event.createdAt || time || 0)
      if (event.type === 'turn/start') turns += 1
      if (time > last) last = time
    }
  } finally {
    reader.close()
  }
  return { start: start || last, last, turns }
}

async function findSession(sessionId, ctx) {
  if (!matchesConversation(sessionId, ctx)) {
    throw new Error('that session belongs to another conversation')
  }
  const files = await sessionFiles()
  const hit = files.find((item) => item.sessionId === sessionId)
  if (!hit) {
    throw new Error(`no session file named ${sessionId}; call history_sessions first`)
  }
  return hit
}

// ── parsing helpers ──────────────────────────────────────────────

function extractText(content) {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  const parts = []
  for (const block of content) {
    if (typeof block === 'string') {
      parts.push(block)
      continue
    }
    if (!block || typeof block !== 'object') continue
    if (block.type === 'text' && typeof block.text === 'string') parts.push(block.text)
    else if (block.type === 'image') parts.push('[图片]')
  }
  return parts.join('\n')
}

function cleanText(value) {
  return String(value || '').replace(/\s+/g, ' ').trim()
}

function clip(value, max = MAX_TEXT_CHARS) {
  const text = cleanText(value)
  return text.length > max ? `${text.slice(0, max)}…` : text
}

function parseWhen(value, end = false) {
  if (value === undefined || value === null || value === '') return null
  const text = String(value).trim()
  if (!text) return null
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) {
    return Date.parse(`${text}T${end ? '23:59:59' : '00:00:00'}+08:00`)
  }
  if (/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?$/.test(text)) {
    const base = text.replace(' ', 'T')
    const withSeconds = base.length === 16 ? `${base}:00` : base
    return Date.parse(`${withSeconds}+08:00`)
  }
  return Date.parse(text)
}

function timeFilters(args) {
  const since = parseWhen(args?.since)
  const until = parseWhen(args?.until, true)
  if (since !== null && Number.isNaN(since)) throw new Error('since is not a valid date/time')
  if (until !== null && Number.isNaN(until)) throw new Error('until is not a valid date/time')
  return { since, until }
}

function limitOf(args, fallback = DEFAULT_LIMIT) {
  const value = Number(args?.limit)
  if (!Number.isFinite(value) || value <= 0) return fallback
  return Math.min(MAX_LIMIT, Math.max(1, Math.floor(value)))
}

const timeFormat = new Intl.DateTimeFormat('zh-CN', {
  timeZone: TZ,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

function formatTime(ms) {
  return ms ? timeFormat.format(new Date(ms)) : '-'
}

function formatSize(bytes) {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`
  return `${bytes} B`
}

function escapeLike(value) {
  return String(value).replace(/[\\%_]/g, (match) => `\\${match}`)
}

function text(value) {
  return [{ type: 'text', text: String(value) }]
}

function stringOutput() {
  return { schema: { type: 'string' }, render: (_args, value) => text(value) }
}

// ── tools ────────────────────────────────────────────────────────

const historySessions = {
  name: 'history_sessions',
  description:
    "List this conversation's DSH session files, including older generations that are no longer " +
    'in your current context. Use this before history_read when the user asks about earlier chats.',
  parameters: { type: 'object', properties: {}, additionalProperties: false },
  output: stringOutput(),
  async execute(_args, exec) {
    const ctx = requireContext(exec)
    const current = String(exec?.agent?.id ?? '')
    const files = (await sessionFiles()).filter((item) =>
      matchesConversation(item.sessionId, ctx)
    )
    if (!files.length) return '（没有找到这个会话的 DSH session 文件）'

    const rows = []
    for (const file of files) {
      try {
        const meta = await scanSessionMeta(file.file)
        rows.push({ ...file, ...meta })
      } catch {
        continue
      }
    }
    rows.sort((left, right) => right.last - left.last)

    const lines = [
      `本会话（${ctx.scope === 'group' ? `群 ${ctx.groupId}` : `私聊 ${ctx.userId}`}）的 session：`,
    ]
    for (const row of rows) {
      const marker = row.sessionId === current ? ' [当前]' : ''
      lines.push(
        `- ${row.sessionId}${marker}  ${formatTime(row.start)} → ${formatTime(row.last)}  ` +
          `${row.turns} 轮  ${formatSize(row.size)}`
      )
    }
    lines.push('用 history_read 读取某一份；session 参数填上面的名字（当前会话可省略）。')
    return lines.join('\n')
  },
}

const historyRead = {
  name: 'history_read',
  description:
    "Read one DSH session's user and assistant messages from disk. Supports keyword and " +
    'date filters, and can read older generations. History is scoped to the current QQ chat.',
  parameters: {
    type: 'object',
    properties: {
      session: {
        type: 'string',
        description:
          'Session name from history_sessions, e.g. group-1021216328. Omit or use "current" for the active session.',
      },
      query: { type: 'string', description: 'Optional keyword substring filter' },
      since: {
        type: 'string',
        description: 'Optional start time in Beijing time: YYYY-MM-DD or YYYY-MM-DD HH:mm',
      },
      until: {
        type: 'string',
        description: 'Optional end time in Beijing time: YYYY-MM-DD or YYYY-MM-DD HH:mm',
      },
      limit: {
        type: 'integer',
        minimum: 1,
        maximum: MAX_LIMIT,
        description: `Maximum messages to return (default ${DEFAULT_LIMIT})`,
      },
      order: {
        type: 'string',
        enum: ['newest', 'oldest'],
        description: 'Pick the newest window (default) or the oldest window; output stays chronological',
      },
    },
    additionalProperties: false,
  },
  output: stringOutput(),
  async execute(args, exec) {
    const ctx = requireContext(exec)
    const requested = String(args?.session ?? '').trim()
    const sessionId =
      !requested || requested === 'current' ? String(exec?.agent?.id ?? '') : requested
    if (!sessionId) throw new Error('no current session is available')
    const file = await findSession(sessionId, ctx)
    const { since, until } = timeFilters(args)
    const limit = limitOf(args)
    const order = args?.order === 'oldest' ? 'oldest' : 'newest'
    const query = cleanText(args?.query).toLowerCase()

    const items = []
    const reader = createInterface({
      input: createReadStream(file.file, { encoding: 'utf8' }),
      crlfDelay: Infinity,
    })
    try {
      for await (const line of reader) {
        if (!line.trim()) continue
        let event
        try {
          event = JSON.parse(line)
        } catch {
          continue
        }
        if (event.type !== 'user/message' && event.type !== 'assistant/message') continue
        const time = Number(event.time || 0)
        if (since !== null && time < since) continue
        if (until !== null && time > until) continue
        const content =
          event.type === 'user/message'
            ? event.data?.content
            : event.data?.message?.content
        const body = cleanText(extractText(content))
        if (!body) continue
        if (query && !body.toLowerCase().includes(query)) continue
        items.push({
          seq: Number(event.seq || 0),
          time,
          role: event.type === 'user/message' ? 'user' : 'assistant',
          text: body,
        })
      }
    } finally {
      reader.close()
    }

    if (!items.length) {
      return '没有匹配的消息；可以放宽 query 或日期，或先调用 history_sessions 查看可用 session。'
    }
    // Keep the file's conversational order (user, then assistant). `order`
    // chooses which end of the matching window to return, not the display order.
    items.sort((left, right) => left.seq - right.seq || left.time - right.time)
    const selected = order === 'oldest' ? items.slice(0, limit) : items.slice(-limit)
    const lines = [
      `session=${sessionId}  matches=${items.length}  showing=${selected.length}  ` +
        `window=${order}  (chronological)`,
    ]
    for (const item of selected) {
      lines.push(
        `[${formatTime(item.time)}] ${item.role === 'user' ? '用户' : '助手'}: ${clip(item.text)}`
      )
    }
    return lines.join('\n')
  },
}

const chatSearch = {
  name: 'chat_search',
  description:
    'Search this chat\'s inbound messages in memory.db (chat_events). Unlike the model context, ' +
    'this includes group messages that did not mention the bot. Text only, inbound only, 90-day retention.',
  parameters: {
    type: 'object',
    properties: {
      query: { type: 'string', description: 'Optional keyword substring filter' },
      since: {
        type: 'string',
        description: 'Optional start time in Beijing time: YYYY-MM-DD or YYYY-MM-DD HH:mm',
      },
      until: {
        type: 'string',
        description: 'Optional end time in Beijing time: YYYY-MM-DD or YYYY-MM-DD HH:mm',
      },
      limit: {
        type: 'integer',
        minimum: 1,
        maximum: MAX_LIMIT,
        description: `Maximum messages to return (default ${DEFAULT_LIMIT})`,
      },
    },
    additionalProperties: false,
  },
  output: stringOutput(),
  async execute(args, exec) {
    const ctx = requireContext(exec)
    const conversationId = conversationIdOf(ctx)
    const { since, until } = timeFilters(args)
    const limit = limitOf(args)
    const query = cleanText(args?.query)

    const clauses = ['conversation_id = ?']
    const params = [conversationId]
    if (since !== null) {
      clauses.push('created_at >= ?')
      params.push(Math.floor(since / 1000))
    }
    if (until !== null) {
      clauses.push('created_at <= ?')
      params.push(Math.floor(until / 1000))
    }
    if (query) {
      clauses.push("text LIKE ? ESCAPE '\\'")
      params.push(`%${escapeLike(query)}%`)
    }
    params.push(limit)

    let rows
    try {
      rows = getMemoryDb()
        .prepare(
          `SELECT created_at, sender_name, user_id, text FROM chat_events
           WHERE ${clauses.join(' AND ')}
           ORDER BY created_at DESC LIMIT ?`
        )
        .all(...params)
    } catch (error) {
      return `chat_events 不可用：${error.message}`
    }

    if (!rows.length) {
      return `chat_events 里没有匹配的入站消息（${conversationId}）。`
    }
    const lines = [
      `chat_events ${conversationId}（仅入站消息，不含你的回复；显示最新 ${rows.length} 条）：`,
    ]
    for (const row of rows) {
      const who = row.sender_name || String(row.user_id || '?')
      lines.push(
        `[${formatTime(Number(row.created_at) * 1000)}] ${who}: ${clip(row.text)}`
      )
    }
    return lines.join('\n')
  },
}

const SYSTEM_NOTE =
  '历史记录工具（只在用户问到更早的聊天时按需使用，不要每轮都查）：' +
  'history_sessions 列出本会话的 DSH session（含旧 generation）；' +
  'history_read 读取指定 session 的 JSONL，可按关键词/日期过滤，返回用户消息和你的回复；' +
  'chat_search 查 memory.db 的入站消息，包含群里未 @ 你的普通消息（保留 90 天，不含你的回复）。' +
  '先查再回答，不要假装记得没读过的内容；只查当前群/私聊，不要跨会话。' +
  '旧 session 可能属于此前人设阶段，其中的角色自述只是历史台词，不代表你现在的身份或经历。'

export const name = 'session-history'
export const inject = ['tools', 'systemPrompt']

export function apply(ctx) {
  for (const tool of [historySessions, historyRead, chatSearch]) {
    ctx.tools.register(tool)
  }
  ctx.on('agent/created', ({ agent }) => {
    agent.ctx.systemPrompt.section({
      name: 'session-history',
      order: 8100,
      text: SYSTEM_NOTE,
    })
  })
}
