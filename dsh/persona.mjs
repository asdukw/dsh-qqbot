// Persona text is prepared by the adapter before every turn, not shared globally.
import { DatabaseSync } from 'node:sqlite'

let db
function connection() {
  if (!db) db = new DatabaseSync(
    process.env.USER_PROFILE_DB || '/var/lib/qqbot/dsh-state/profile.db',
    { readOnly: true },
  )
  return db
}

export function personaFor(sessionId) {
  const row = connection().prepare(
    'SELECT prompt, persona_token FROM session_personas WHERE session_id = ?',
  ).get(String(sessionId))
  if (!row?.prompt || !row?.persona_token) {
    throw new Error('persona not prepared for this session; refusing unscoped generation')
  }
  return row.prompt
}

export const name = 'conversation-persona'
export const inject = ['systemPrompt']
export function apply(ctx) {
  ctx.on('agent/created', ({ agent }) => {
    agent.ctx.systemPrompt.section({
      name: 'qqbot:conversation-persona',
      order: 10,
      text: () => personaFor(agent.id),
      interpolate: false,
    })
  })
}
