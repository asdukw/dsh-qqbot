"""Bounded declarative story definitions; no executable scripts or model tools."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import Field, field_validator, model_validator
from relationship_rules import Relationship, StrictModel

ID = r'^[a-z][a-z0-9_]{0,39}$'


class StorySettings(StrictModel):
    enabled: bool = False
    timeout: int = Field(default=120, ge=5, le=180)
    stage_timeout: int = Field(default=300, ge=10, le=360)
    prepare_timeout: int = Field(default=600, ge=20, le=720)
    queue_timeout: int = Field(default=20, ge=1, le=120)
    brief_tokens: int = Field(default=1800, ge=512, le=4000)
    scene_tokens: int = Field(default=1200, ge=256, le=4000)
    output_mode: Literal['legacy', 'text', 'json_object', 'json_schema'] = 'legacy'
    max_tokens: int = Field(default=6000, ge=512, le=16000)
    max_context_chars: int = Field(default=24000, ge=4000, le=64000)
    concurrency: int = Field(default=2, ge=1, le=4)
    menu_hours: int = Field(default=168, ge=1, le=720)
    daily_chat_gain: int = Field(default=5, ge=0, le=20)
    chat_cooldown: int = Field(default=120, ge=0, le=3600)
    json_mode: bool = False
    character_preferences: dict[str, dict[str, str | bool]] = Field(default_factory=dict, max_length=100)

    @field_validator('character_preferences')
    @classmethod
    def preferences(cls, value: dict[str, dict[str, str | bool]]) -> dict[str, dict[str, str | bool]]:
        for key, options in value.items():
            if not re.fullmatch(r'[a-f0-9]{32}', key):
                raise ValueError('preferences require a stable local character id')
            if set(options) - {'enabled', 'tone', 'pace', 'boundaries'}:
                raise ValueError('unknown character preference')
            for name, option in options.items():
                if name == 'enabled' and type(option) is not bool:
                    raise ValueError('enabled must be boolean')
                if name != 'enabled' and (not isinstance(option, str) or len(option) > 300):
                    raise ValueError('character preference must be <=300 characters')
        return value


class RunState(StrictModel):
    node: str
    turn: int = 0
    flags: list[str] = Field(default_factory=list)
    consumed: list[str] = Field(default_factory=list)
    pending: list[PendingEffect] = Field(default_factory=list)


class Condition(StrictModel):
    flags: list[str] = Field(default_factory=list, max_length=12)
    not_flags: list[str] = Field(default_factory=list, max_length=12)
    affinity: int = Field(default=0, ge=0, le=100)
    trust: int = Field(default=0, ge=0, le=100)

    def accepts(self, state: RunState, relation: Relationship) -> bool:
        return (all(flag in state.flags for flag in self.flags)
                and not any(flag in state.flags for flag in self.not_flags)
                and relation.affinity >= self.affinity and relation.trust >= self.trust)


class Effect(StrictModel):
    affinity: int = Field(default=0, ge=-3, le=3)
    trust: int = Field(default=0, ge=-3, le=3)
    flags: list[str] = Field(default_factory=list, max_length=8)
    milestone: Literal['', 'shared_experience', 'promise', 'conflict', 'repair'] = ''
    intent: Literal['', 'friendship', 'offer_romance', 'accept_romance', 'decline', 'boundary'] = ''
    reason: str = Field(default='', max_length=100)

    @model_validator(mode='after')
    def no_boundary_penalty(self) -> Effect:
        if self.intent in ('decline', 'boundary', 'friendship') and (self.affinity < 0 or self.trust < 0):
            raise ValueError('respecting a boundary cannot penalize the player')
        return self


class PendingEffect(StrictModel):
    id: str
    due: int
    effect: Effect


RunState.model_rebuild()


class DelayedEffect(StrictModel):
    id: str = Field(pattern=ID)
    after_turns: int = Field(ge=1, le=8)
    effect: Effect


class Choice(StrictModel):
    id: str = Field(pattern=ID)
    text: str = Field(min_length=1, max_length=100)
    target: str = Field(pattern=ID)
    condition: Condition = Field(default_factory=Condition)
    effect: Effect = Field(default_factory=Effect)
    once: bool = True
    delayed: list[DelayedEffect] = Field(default_factory=list, max_length=2)


class Node(StrictModel):
    id: str = Field(pattern=ID)
    chapter: str = Field(min_length=1, max_length=80)
    beat: str = Field(min_length=1, max_length=400)
    choices: list[Choice] = Field(default_factory=list, max_length=4)
    ending: str = Field(default='', max_length=100)

    @model_validator(mode='after')
    def playable(self) -> Node:
        if self.ending:
            if self.choices:
                raise ValueError('ending cannot contain choices')
        elif not 2 <= len(self.choices) <= 4:
            raise ValueError('non-ending requires 2-4 choices')
        elif not any(choice.condition == Condition() and choice.effect.intent != 'accept_romance' for choice in self.choices):
            raise ValueError('non-ending needs an unconditional fallback')
        return self


class WorldBrief(StrictModel):
    title: str = Field(min_length=1, max_length=80)
    background: str = Field(min_length=1, max_length=1200)
    synopsis: str = Field(min_length=1, max_length=600)
    motivation: str = Field(min_length=1, max_length=400)
    character: str = Field(min_length=1, max_length=1200)
    public_character: str = Field(min_length=1, max_length=400)
    behavior: dict[str, str] = Field(default_factory=dict, max_length=5)

    @field_validator('behavior')
    @classmethod
    def bounded_behavior(cls, value: dict[str, str]) -> dict[str, str]:
        if any(len(text) > 400 or key not in ('初识', '熟悉', '信任', '亲近', '恋人')
               for key, text in value.items()):
            raise ValueError('invalid stage behavior')
        return value


class StoryGraphDraft(StrictModel):
    start: str = Field(pattern=ID)
    nodes: list[Node] = Field(min_length=3, max_length=24)

    @model_validator(mode='after')
    def graph(self) -> StoryGraphDraft:
        nodes = {node.id: node for node in self.nodes}
        if len(nodes) != len(self.nodes) or self.start not in nodes:
            raise ValueError('duplicate nodes or missing start')
        choice_ids: set[str] = set()
        for node in self.nodes:
            for choice in node.choices:
                if choice.id in choice_ids or choice.target not in nodes:
                    raise ValueError('duplicate choice or missing target')
                choice_ids.add(choice.id)
                flags = choice.effect.flags + choice.condition.flags + choice.condition.not_flags
                flags += [flag for delayed in choice.delayed for flag in delayed.effect.flags]
                if any(not re.fullmatch(ID, flag) for flag in flags):
                    raise ValueError('invalid flag')
                if len({item.id for item in choice.delayed}) != len(choice.delayed):
                    raise ValueError('duplicate delayed event')
                for effect in [choice.effect, *(item.effect for item in choice.delayed)]:
                    if 'romance_offered' in effect.flags:
                        raise ValueError('reserved flag cannot be assigned directly')
                if any(item.effect.intent for item in choice.delayed):
                    raise ValueError('relationship choices cannot be deferred')
                if choice.effect.intent == 'accept_romance':
                    if (choice.condition.affinity < 65 or choice.condition.trust < 60
                            or 'romance_offered' not in choice.condition.flags):
                        raise ValueError('romance choice must expose explicit prerequisites')
        produced = {'romance_offered'} | {flag for node in self.nodes for choice in node.choices
                                         for flag in choice.effect.flags}
        produced |= {flag for node in self.nodes for choice in node.choices
                     for item in choice.delayed for flag in item.effect.flags}
        if any(flag not in produced for node in self.nodes for choice in node.choices
               for flag in choice.condition.flags + choice.condition.not_flags):
            raise ValueError('condition references an undeclared flag')
        visited: set[str] = set()
        visiting: set[str] = set()

        def walk(key: str) -> None:
            if key in visiting:
                raise ValueError('story graph must be acyclic')
            if key in visited:
                return
            visiting.add(key)
            for choice in nodes[key].choices:
                walk(choice.target)
            visiting.remove(key)
            visited.add(key)

        for key in nodes:
            walk(key)
        reachable: set[str] = set()

        def visit(key: str) -> None:
            if key in reachable:
                return
            reachable.add(key)
            for choice in nodes[key].choices:
                visit(choice.target)

        visit(self.start)
        if not any(nodes[key].ending for key in reachable):
            raise ValueError('no ending reachable from start')
        return self

    def node(self, key: str) -> Node:
        return next(node for node in self.nodes if node.id == key)


class StoryDefinition(WorldBrief, StoryGraphDraft):
    version: Literal[1] = 1


class SceneText(StrictModel):
    text: str = Field(min_length=1, max_length=1800)


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def available(node: Node, state: RunState, relation: Relationship) -> list[Choice]:
    return [choice for choice in node.choices if choice.condition.accepts(state, relation)
            and (not choice.once or choice.id not in state.consumed)]


def apply_effect(relation: Relationship, state: RunState, effect: Effect) -> list[str]:
    before = (relation.affinity, relation.trust, relation.stage)
    relation.affinity = max(0, min(100, relation.affinity + effect.affinity))
    relation.trust = max(0, min(100, relation.trust + effect.trust))
    state.flags = list(dict.fromkeys([*state.flags, *effect.flags]))
    if effect.milestone and effect.milestone not in relation.milestones:
        relation.milestones.append(effect.milestone)
    if effect.intent == 'offer_romance':
        state.flags = list(dict.fromkeys([*state.flags, 'romance_offered']))
    elif effect.intent == 'accept_romance':
        if ('romance_offered' not in state.flags or relation.affinity < 65
                or relation.trust < 60 or 'shared_experience' not in relation.milestones):
            raise ValueError('mutual romance prerequisites not met')
        relation.route = 'romance'
        if 'mutual_romance' not in relation.milestones:
            relation.milestones.append('mutual_romance')
    elif effect.intent in ('friendship', 'decline'):
        relation.route = 'friendship'
        relation.milestones = [key for key in relation.milestones if key != 'mutual_romance']
    feedback = []
    for label, delta in [('好感', relation.affinity - before[0]), ('信任', relation.trust - before[1])]:
        if delta:
            feedback.append(f'{label} {delta:+d}')
    if effect.milestone:
        feedback.append({'shared_experience': '共同经历已记录', 'promise': '约定已记录',
                         'conflict': '分歧已记录', 'repair': '关系修复已记录'}[effect.milestone])
    if effect.reason:
        feedback.append(effect.reason)
    if relation.stage != before[2]:
        feedback.append(f'关系阶段：{relation.stage}')
    return feedback


def advance(state: RunState, relation: Relationship, choice: Choice) -> tuple[RunState, Relationship, list[str]]:
    state, relation = state.model_copy(deep=True), relation.model_copy(deep=True)
    feedback = apply_effect(relation, state, choice.effect)
    state.turn += 1
    state.node = choice.target
    state.consumed.append(choice.id)
    for item in choice.delayed:
        state.pending.append(PendingEffect(id=f'{choice.id}:{item.id}', due=state.turn + item.after_turns,
                                          effect=item.effect))
    due = [item for item in state.pending if item.due <= state.turn]
    state.pending = [item for item in state.pending if item.due > state.turn]
    for item in due:
        feedback.extend(apply_effect(relation, state, item.effect))
    return state, relation, feedback


def story_warnings(story: StoryGraphDraft, *, budget: int = 256) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    visited: set[str] = set()
    stack = [story.start]
    while stack:
        key = stack.pop()
        if key not in visited:
            visited.add(key)
            stack.extend(choice.target for choice in story.node(key).choices)
    if len(visited) != len(story.nodes):
        warnings.append({'code': 'isolated_nodes', 'path': '$.nodes'})
    if not 6 <= len(story.nodes) <= 8:
        warnings.append({'code': 'outline_size_advisory', 'path': '$.nodes'})
    for index, node in enumerate(story.nodes):
        signatures = {(choice.target, choice.effect.model_dump_json(),
                       tuple(item.model_dump_json() for item in choice.delayed)) for choice in node.choices}
        if node.choices and len(signatures) == 1:
            warnings.append({'code': 'equivalent_choices', 'path': f'$.nodes[{index}].choices'})
    sampled: set[str] = set()
    exhausted = False
    # Actual initial values and representative threshold boundaries, not a
    # maximum-score proof of all possible interleavings with ordinary chat.
    for score, trust in ((0, 0), (20, 10), (44, 39), (45, 40), (64, 59), (65, 60), (100, 100)):
        queue = [(RunState(node=story.start), Relationship(affinity=score, trust=trust))]
        count = 0
        seen: set[str] = set()
        while queue and count < budget:
            state, relation = queue.pop()
            signature = state.model_dump_json() + relation.model_dump_json()
            if signature in seen:
                continue
            seen.add(signature)
            count += 1
            sampled.add(state.node)
            for choice in available(story.node(state.node), state, relation):
                try:
                    next_state, next_relation, _ = advance(state, relation, choice)
                except ValueError:
                    continue
                queue.append((next_state, next_relation))
        exhausted |= bool(queue)
    if exhausted:
        warnings.append({'code': 'coverage_budget_unknown', 'path': '$.nodes'})
    if visited - sampled:
        warnings.append({'code': 'conditional_reachability_unproven', 'path': '$.nodes'})
    return warnings[:20]
