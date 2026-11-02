from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node is required for native plugin checks')
class MemoryArchivePluginTests(unittest.TestCase):
    def test_registers_tools_without_touching_the_prompt(self) -> None:
        """日摘要只能按需检索：插件不再注册 systemPrompt 段，也不挂 agent/created。

        假的 ctx 故意不提供 `on`，插件若再加常驻注入会在 apply 时直接抛错。
        """
        plugin = (Path(__file__).resolve().parents[1] / 'dsh/memory-archive.mjs').as_uri()
        script = r'''
import assert from 'node:assert/strict'
import { apply, inject } from PLUGIN
const registered = []
const ctx = {
  tools: { register: (tool) => registered.push(tool) },
  logger: { warn: () => {} },
}
apply(ctx)
assert.deepEqual(inject, ['tools'], 'the plugin must not depend on systemPrompt')
assert.deepEqual(
  registered.map((tool) => tool.name).sort(),
  ['memory_person', 'memory_search', 'memory_timeline'],
)
const search = registered.find((tool) => tool.name === 'memory_search')
assert(search.description.includes('daily summaries'))
assert.deepEqual(
  Object.keys(search.parameters.properties),
  ['query', 'since', 'until', 'limit'],
)
for (const tool of registered) {
  assert.equal(typeof tool.execute, 'function')
  assert.equal(tool.name.includes('recent'), false)
}
'''.replace('PLUGIN', json.dumps(plugin))
        result = subprocess.run(
            ['node', '--input-type=module', '-e', script],
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, 'QQBOT_MEMORY_ENABLED': '1'},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
