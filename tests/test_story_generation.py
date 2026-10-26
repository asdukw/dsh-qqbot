from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from llm import ChatResult  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from relationship_rules import Relationship  # noqa: E402
from story_generation import GenerationError, StoryGenerator, strict_schema, validation_issues  # noqa: E402
from story_models import (  # noqa: E402
    RunState,
    StoryDefinition,
    StoryGraphDraft,
    StorySettings,
    WorldBrief,
    story_warnings,
)
from story_store import StoryStore  # noqa: E402
from test_story import card, definition  # noqa: E402


def parts():
    raw = definition().model_dump()
    brief = WorldBrief.model_validate({key: raw[key] for key in WorldBrief.model_fields})
    graph = StoryGraphDraft.model_validate({key: raw[key] for key in StoryGraphDraft.model_fields})
    return brief, graph


class StagedGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_specific_repair_reuses_candidate(self):
        brief, graph = parts()
        client = SimpleNamespace(complete=AsyncMock(side_effect=[ChatResult('bad', 'stop', {}),
            ChatResult(brief.model_dump_json(), 'stop', {}), ChatResult(graph.model_dump_json(), 'stop', {})]))
        result = await StoryGenerator(client, StorySettings()).outline(card('Alice'), 'group:42')
        self.assertEqual(result.title, definition().title)
        self.assertEqual(client.complete.await_count, 3)
        repair = client.complete.await_args_list[1]
        self.assertEqual(repair.kwargs['purpose'], 'story_brief_repair')
        self.assertEqual(repair.args[0][2]['content'], 'bad')
        self.assertIn('json_syntax', repair.args[0][-1]['content'])
        self.assertEqual(client.complete.await_args.kwargs['purpose'], 'story_graph')
        self.assertEqual(client.complete.await_args.kwargs['attempts'], 1)
        self.assertNotIn('tools', client.complete.await_args.kwargs)

    async def test_truncation_policy_refusal_empty_do_not_blindly_retry(self):
        for result, code in [(ChatResult('{}', 'length', {}), 'truncated'),
                             (ChatResult('性交', 'stop', {}), 'content_policy'),
                             (ChatResult('', 'stop', {}), 'empty'),
                             (ChatResult('{}', 'content_filter', {}), 'refused')]:
            client = SimpleNamespace(complete=AsyncMock(return_value=result))
            with self.assertRaises(GenerationError) as caught:
                await StoryGenerator(client, StorySettings()).outline(card('Alice'), 'group:42')
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(client.complete.await_count, 1)

    async def test_error_ctx_serialization_and_field_feedback(self):
        raw = definition().model_dump()
        raw['nodes'][0]['choices'][0]['target'] = 'opening'
        with self.assertRaises(ValidationError) as caught:
            StoryDefinition.model_validate(raw)
        issues = validation_issues(caught.exception)
        serialized = json.dumps(issues)
        self.assertIn('acyclic', serialized)
        self.assertNotIn('ctx', serialized)
        self.assertNotIn('雨中的', serialized)
        brief, graph = parts()
        bad = graph.model_dump()
        bad['nodes'][0]['choices'][0]['target'] = 'missing'
        client = SimpleNamespace(complete=AsyncMock(side_effect=[
            ChatResult(brief.model_dump_json(), 'stop', {}), ChatResult(json.dumps(bad), 'stop', {}),
            ChatResult(graph.model_dump_json(), 'stop', {})]))
        await StoryGenerator(client, StorySettings()).outline(card('Alice'), 'group:42')
        self.assertIn('missing target', client.complete.await_args.args[0][-1]['content'])

    async def test_checkpoint_survives_graph_failure_restart_and_input_changes(self):
        brief, graph = parts()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'profile.db'
            store = StoryStore(path)
            client = SimpleNamespace(complete=AsyncMock(side_effect=[ChatResult(brief.model_dump_json(), 'stop', {}),
                                        ChatResult('{}', 'length', {})]))
            generator = StoryGenerator(client, StorySettings())
            generator.checkpoints = store
            try:
                with self.assertRaises(GenerationError):
                    await generator.outline(card('Alice'), 'group:42', character_id='a', fingerprint='fp')
                self.assertEqual(store.admin()['worlds'], [])
            finally:
                store.close()
            store = StoryStore(path)
            try:
                generator.checkpoints = store
                client.complete = AsyncMock(return_value=ChatResult(graph.model_dump_json(), 'stop', {}))
                await generator.outline(card('Alice'), 'group:42', character_id='a', fingerprint='fp')
                self.assertEqual(client.complete.await_count, 1)
                self.assertEqual(client.complete.await_args.kwargs['purpose'], 'story_graph')
                await generator.outline(card('Alice'), 'group:42', character_id='a', fingerprint='fp')
                self.assertEqual(client.complete.await_count, 1)
                client.complete = AsyncMock(side_effect=[ChatResult(brief.model_dump_json(), 'stop', {}), ChatResult(graph.model_dump_json(), 'stop', {})])
                await generator.outline(card('Alice'), 'group:42', {'pace': 'different'}, character_id='a', fingerprint='fp')
                self.assertEqual(client.complete.await_count, 2)
            finally:
                store.close()

    async def test_deadline_and_scene_specific_output_budget(self):
        async def slow(*args, **kwargs):
            await asyncio.sleep(0.04)
            return ChatResult('{}', 'stop', {})
        generator = StoryGenerator(SimpleNamespace(complete=AsyncMock(side_effect=slow)), StorySettings())
        with self.assertRaises(GenerationError) as caught:
            await generator._generate(WorldBrief, {}, 'story_brief', 'group:42', deadline=time.monotonic() + 0.01)
        self.assertEqual(caught.exception.code, 'call_timeout')
        client = SimpleNamespace(complete=AsyncMock(return_value=ChatResult('{"text":"安全场景"}', 'stop', {})))
        await StoryGenerator(client, StorySettings(scene_tokens=512)).scene(definition(), RunState(node='opening'), Relationship(), [], 'group:42')
        self.assertEqual(client.complete.await_args.kwargs['max_tokens'], 512)

    async def test_isolated_nodes_and_sample_budget_are_warnings(self):
        raw = definition().model_dump()
        raw['nodes'].append({'id': 'optional', 'chapter': '支线', 'beat': '未关联支线', 'ending': '支线终章'})
        accepted = StoryDefinition.model_validate(raw)
        self.assertIn('isolated_nodes', [item['code'] for item in story_warnings(accepted)])
        self.assertIn('coverage_budget_unknown', [item['code'] for item in story_warnings(accepted, budget=1)])
        raw['nodes'][0]['choices'][0]['condition'] = {'affinity': 65, 'trust': 60}
        StoryDefinition.model_validate(raw)

    async def test_queue_timeout_and_bounded_diagnostics(self):
        client = SimpleNamespace(complete=AsyncMock())
        generator = StoryGenerator(client, StorySettings())
        await generator._slots.acquire()
        await generator._slots.acquire()
        with self.assertRaises(GenerationError) as caught:
            await generator._generate(WorldBrief, {}, 'story_brief', 'group:42', deadline=time.monotonic() + 0.01)
        self.assertEqual(caught.exception.code, 'queue_timeout')
        client.complete.assert_not_awaited()
        generator._slots.release()
        generator._slots.release()
        diagnostics = []
        generator.diagnostics = lambda *args: diagnostics.append(args)
        client.complete = AsyncMock(return_value=ChatResult('{bad', 'stop', {}))
        with self.assertRaises(GenerationError) as caught:
            await generator._generate(WorldBrief, {}, 'story_brief', 'group:42')
        self.assertEqual(caught.exception.code, 'validation')
        self.assertEqual(client.complete.await_count, 2)
        self.assertEqual(len(diagnostics), 2)
        self.assertNotIn('{bad', json.dumps(diagnostics))

    async def test_native_schema_is_explicit_and_conservative(self):
        schema = strict_schema(StoryGraphDraft.model_json_schema())
        self.assertEqual(set(schema['required']), {'start', 'nodes'})
        self.assertFalse(schema['additionalProperties'])
        client = SimpleNamespace(complete=AsyncMock(return_value=ChatResult('{"text":"安全场景"}', 'stop', {})))
        await StoryGenerator(client, StorySettings(output_mode='json_schema')).scene(definition(), RunState(node='opening'), Relationship(), [], 'group:42')
        self.assertIn('json_schema', client.complete.await_args.kwargs)
        with self.assertRaises(ValueError):
            strict_schema(WorldBrief.model_json_schema())


if __name__ == '__main__':
    unittest.main()
