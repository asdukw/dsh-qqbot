from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node is required for native plugin checks')
class ToolRouterPluginTests(unittest.TestCase):
    def test_default_surface_and_domain_switch(self) -> None:
        plugin = (Path(__file__).resolve().parents[1] / 'dsh/tool-router.mjs').as_uri()
        script = r'''
import assert from 'node:assert/strict'
import { apply } from PLUGIN
const registered = []
const restrictions = []
let created = null
const ctx = {
  tools: { register: (tool) => registered.push(tool) },
  on: (event, callback) => { if (event === 'agent/created') created = callback },
  logger: { warn: () => {} },
}
apply(ctx)
const agent = {
  ctx: {
    tools: {
      restrict: (filter) => {
        restrictions.push(filter)
        return () => {}
      },
    },
    systemPrompt: { section: () => {} },
  },
}
created({ agent })
assert.equal(restrictions.length, 1)
const initial = restrictions[0].deny
assert(initial.includes('video_resolve'))
assert(initial.includes('card_library'))
assert(initial.includes('history_sessions'))
assert(initial.includes('memory_search'))
const route = registered.find((tool) => tool.name === 'tool_route')
assert(route, 'tool_route must be registered')
assert.deepEqual(route.parameters.properties.domains.items.enum, ['video', 'cards', 'history', 'memory', 'all'])
assert(!route.description.includes('media'))
const video = await route.execute({ domains: ['video'] }, { agent })
assert(video.includes('video'))
let after = restrictions[1].deny
assert(!after.includes('video_resolve'))
assert(after.includes('card_library'))
assert(after.includes('history_sessions'))
const cards = await route.execute({ domains: ['cards'] }, { agent })
after = restrictions[2].deny
assert(!after.includes('card_library'))
assert(after.includes('video_resolve'))
const combined = await route.execute({ domains: ['history', 'memory'] }, { agent })
after = restrictions[3].deny
assert(!after.includes('history_sessions'))
assert(!after.includes('memory_search'))
assert(after.includes('video_resolve'))
assert(after.includes('card_library'))
assert(combined.includes('history'))
const replacement = await route.execute({ domains: ['video'] }, { agent })
after = restrictions[4].deny
assert(!after.includes('video_resolve'))
assert(after.includes('history_sessions'))
assert(after.includes('memory_search'))
const all = await route.execute({ domains: ['all'] }, { agent })
assert(all.includes('all'))
assert.deepEqual(restrictions[5].deny, [])
assert(!route.parameters.properties.domains.items.enum.includes('media'))
'''.replace('PLUGIN', json.dumps(plugin))
        result = subprocess.run(
            ['node', '--input-type=module', '-e', script],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
