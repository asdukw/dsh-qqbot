// Keyless web search provider for the DSH web capability seam.
//
// The official providers (@deepseek-ai/dsh-web-search-exa / -perplexity /
// -deepseek) all require their own API key. OpenCode's websearch tool instead
// talks to keyless search backends, and Tavily's keyless mode answers plain
// REST requests from this host. Registering a provider on the same `ctx.web`
// registry lets the official web_search / web_fetch tools work without a new
// credential.
//
// Contract (mirrors dsh-web-search-exa): an object with `id`, `available()`
// and `async search(request, signal)` returning `{sources, truncated}`.

export const name = 'web-search-keyless'
export const inject = ['web']

const ENDPOINT = 'https://api.tavily.com/search'

export function apply(ctx) {
  ctx.web.registerSearchProvider({
    id: 'keyless',

    available() {
      return true
    },

    async search(request, signal) {
      const maxResults = Math.min(Math.max(Number(request.maxResults) || 5, 1), 10)
      const response = await fetch(ENDPOINT, {
        method: 'POST',
        redirect: 'error',
        headers: {
          'content-type': 'application/json',
          accept: 'application/json',
          'x-tavily-access-mode': 'keyless',
          'user-agent': 'qqbot-web-search/1.0',
        },
        body: JSON.stringify({
          query: request.query,
          search_depth: 'basic',
          chunks_per_source: 1,
          max_results: maxResults,
        }),
        signal,
      })
      if (!response.ok) {
        throw new Error(`keyless search HTTP ${response.status}`)
      }
      const body = await response.json()
      const sources = (body.results ?? [])
        .filter((item) => item && typeof item.url === 'string' && item.url)
        .map((item) => ({
          url: item.url,
          ...(item.title ? { title: String(item.title) } : {}),
          ...(item.content ? { snippet: String(item.content).slice(0, 600) } : {}),
        }))
      return { sources, truncated: false }
    },
  })
}
