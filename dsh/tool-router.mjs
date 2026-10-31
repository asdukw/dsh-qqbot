// Per-agent tool routing using the DSH tools.restrict() API.
//
// The QQ bot has far more custom tools than fit comfortably in one prompt.
// Keep a small, frequently used surface visible and let the model load one
// optional group on demand. Restrictions are scoped to the agent, so a group
// loaded in one QQ conversation never leaks into another.

export const name = 'tool-router'
export const inject = ['tools', 'systemPrompt']

const DOMAINS = {
  video: ['video_resolve', 'video_send'],
  cards: ['card_library', 'card_online_search', 'card_install'],
  history: ['history_sessions', 'history_read', 'chat_search'],
  memory: ['memory_search', 'memory_timeline', 'memory_person'],
  all: [
    'video_resolve', 'video_send',
    'card_library', 'card_online_search', 'card_install',
    'history_sessions', 'history_read', 'chat_search',
    'memory_search', 'memory_timeline', 'memory_person',
  ],
}

const OPTIONAL = [...new Set(Object.values(DOMAINS).flat())]
const state = new WeakMap()

function setRestriction(agent, denied) {
  const tools = agent?.ctx?.tools
  if (!tools || typeof tools.restrict !== 'function') {
    throw new Error('tool routing requires the DSH tools service')
  }
  const previous = state.get(agent)
  if (typeof previous === 'function') previous()
  let dispose
  try {
    dispose = tools.restrict({ deny: denied })
  } catch (error) {
    // Optional plugins (memory/video) may be disabled by environment. The
    // restriction API rejects unknown names, so retry without those names.
    const message = String(error?.message || '')
    const match = /unknown global tool(?:s)?\s+(.+?); known global tools:/.exec(message)
    if (!match) throw error
    const unknown = new Set([...match[1].matchAll(/"([^"]+)"/g)].map((item) => item[1]))
    if (!unknown.size) throw error
    dispose = tools.restrict({ deny: denied.filter((name) => !unknown.has(name)) })
  }
  state.set(agent, dispose)
  return dispose
}

const routeTool = {
  name: 'tool_route',
  description:
    'Load optional tool groups for the current conversation. The default surface is small: ' +
    'profile read/write/forget, web search/fetch, file tools and QQ reply/mention/reaction tools. ' +
    'Call this before using video, cards, history or long-term memory tools. ' +
    'Groups: video (video resolve/send), cards (card library/online search/install), ' +
    'history (raw session/chat logs), memory (consolidated dated summaries/user facts), ' +
    'all. Profile read/write/forget is always core and needs no routing. ' +
    'Loading a group replaces the previous optional set for this agent; core tools always stay available.',
  parameters: {
    type: 'object',
    properties: {
      domains: {
        type: 'array',
        items: { type: 'string', enum: Object.keys(DOMAINS) },
        description: 'One or more groups to load, e.g. ["memory"] or ["video","history"].',
      },
      reason: { type: 'string', description: 'Short reason for the route.' },
    },
    required: ['domains'],
    additionalProperties: false,
  },
  output: {
    schema: { type: 'string' },
    render: (_args, value) => [{ type: 'text', text: String(value) }],
  },
  async execute(args, exec) {
    const agent = exec?.agent
    const requested = Array.isArray(args?.domains) ? args.domains.map(String) : []
    const groups = requested.filter((name) => Object.hasOwn(DOMAINS, name))
    if (!groups.length) throw new Error(`unknown tool group; choose from ${Object.keys(DOMAINS).join(', ')}`)
    const enabled = new Set(groups.flatMap((name) => DOMAINS[name]))
    const denied = OPTIONAL.filter((name) => !enabled.has(name))
    setRestriction(agent, denied)
    return `已加载工具组：${groups.join(', ')}（本会话生效）`
  },
}

export function apply(ctx) {
  ctx.tools.register(routeTool)
  ctx.on('agent/created', ({ agent }) => {
    try {
      setRestriction(agent, OPTIONAL)
    } catch (error) {
      ctx.logger?.warn?.(`tool-router: cannot restrict tools: ${error?.message || error}`)
    }
    agent.ctx.systemPrompt.section({
      name: 'tool-routing',
      order: 9000,
      text:
        '默认只加载常用工具。需要视频/卡片/原始历史日志/长期记忆时，先调用 ' +
        'tool_route({domains:["memory"]}) 加载对应工具组，然后再调用具体工具；' +
        '画像读取、写入、遗忘始终是核心工具，无需路由；一组加载后当前会话保持可用，切换组会替换上一组可选工具。',
    })
  })
}
