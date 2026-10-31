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
assert(initial.includes('memory_search'))
const route = registered.find((tool) => tool.name === 'tool_route')
assert(route, 'tool_route must be registered')
const result = await route.execute({ domains: ['memory'] }, { agent })
assert(result.includes('memory'))
const after = restrictions[1].deny
assert(!after.includes('memory_search'))
assert(after.includes('video_resolve'))
const all = await route.execute({ domains: ['all'] }, { agent })
assert(all.includes('all'))
assert.deepEqual(restrictions[2].deny, [])
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
