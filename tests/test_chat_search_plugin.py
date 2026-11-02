from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node is required for native plugin checks')
class ChatSearchPluginTests(unittest.TestCase):
    def test_space_separated_keywords_are_anded(self) -> None:
        """空格分隔的多个词是 AND：整串子串匹配会让 "Magpie 作者" 这类查询必然落空。"""
        # node 关闭 sqlite 句柄可能滞后，Windows 上删除目录会短暂失败。
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)

        memory_db = root / 'memory.db'
        with sqlite3.connect(memory_db) as db:
            db.execute(
                'CREATE TABLE chat_events (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                'conversation_id TEXT NOT NULL, chat_type TEXT NOT NULL, chat_id INTEGER NOT NULL, '
                'user_id INTEGER NOT NULL, sender_name TEXT NOT NULL DEFAULT "", '
                'text TEXT NOT NULL DEFAULT "", created_at INTEGER NOT NULL)'
            )
            db.executemany(
                'INSERT INTO chat_events (conversation_id, chat_type, chat_id, user_id, '
                'sender_name, text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                [
                    ('group:123', 'group', 123, 1, '甲', '感觉 magpie 有点划时代的产品', 1_000),
                    ('group:123', 'group', 123, 2, '乙', '作者履历看起来不错', 2_000),
                    ('group:123', 'group', 123, 3, '丙', 'magpie 的作者是谁', 3_000),
                ],
            )

        profile_db = root / 'profile.db'
        with sqlite3.connect(profile_db) as db:
            db.execute(
                'CREATE TABLE conversations (session_id TEXT PRIMARY KEY, scope TEXT NOT NULL, '
                'user_id TEXT NOT NULL, group_id TEXT, sender_name TEXT NOT NULL DEFAULT "", '
                'updated_at INTEGER NOT NULL)'
            )
            db.execute(
                "INSERT INTO conversations VALUES ('group-123', 'group', '0', '123', '甲', 0)"
            )

        plugin = (Path(__file__).resolve().parents[1] / 'dsh/session-history.mjs').as_uri()
        script = r'''
import assert from 'node:assert/strict'
import { apply } from PLUGIN
const tools = []
apply({
  tools: { register: (tool) => tools.push(tool) },
  on: () => {},
  logger: { warn: () => {} },
})
const chatSearch = tools.find((tool) => tool.name === 'chat_search')
assert(chatSearch, 'chat_search must be registered')
assert(chatSearch.parameters.properties.query.description.includes('space-separated'))
const exec = { agent: { id: 'group-123' } }

const single = await chatSearch.execute({ query: 'magpie' }, exec)
assert.equal(single.split('\n').length, 1 + 2, 'two rows mention magpie: ' + single)

const both = await chatSearch.execute({ query: 'magpie 作者' }, exec)
assert.equal(both.split('\n').length, 1 + 1, 'only one row has both words: ' + both)
assert(both.includes('作者是谁'))

const none = await chatSearch.execute({ query: 'magpie 完全不存在的词' }, exec)
assert(none.includes('没有匹配'), none)

const blank = await chatSearch.execute({ query: '   ' }, exec)
assert.equal(blank.split('\n').length, 1 + 3, 'blank query returns everything: ' + blank)
'''.replace('PLUGIN', json.dumps(plugin))
        result = subprocess.run(
            ['node', '--input-type=module', '-e', script],
            capture_output=True,
            text=True,
            timeout=20,
            env={
                **os.environ,
                'QQBOT_MEMORY_DB': str(memory_db),
                'USER_PROFILE_DB': str(profile_db),
                'DSH_HOME': str(root),
            },
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
