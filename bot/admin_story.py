"""Bounded story administration helpers; mutations are executed by the bot."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from personas import target_of
from story_models import StorySettings
from story_store import StoryStore


def story_data(path: str, defaults: dict, *, offset: int = 0) -> dict:
    if not Path(path).is_file():
        return {'settings': StorySettings.model_validate(defaults).model_dump(),
                'runs': [], 'worlds': [], 'operations': [], 'deliveries': [], 'offset': 0}
    store = StoryStore(path)
    try:
        return {**store.admin(offset=offset), 'settings': store.settings(StorySettings.model_validate(defaults)).model_dump()}
    finally:
        store.close()


def story_detail(path: str, key: str, *, kind: str = 'run', offset: int = 0) -> dict:
    store = StoryStore(path)
    try:
        return store.detail(key, kind=kind, offset=offset)
    finally:
        store.close()


def queue_story(path: str, payload: dict[str, Any], defaults: dict) -> dict:
    kind = payload.get('kind')
    if kind not in ('settings', 'retry', 'restart', 'link', 'import_legacy', 'regenerate'):
        raise ValueError('未知操作')
    data = payload.get('data')
    if not isinstance(data, dict):
        raise ValueError('data must be an object')
    store = StoryStore(path)
    try:
        if kind == 'settings':
            StorySettings.model_validate({**store.settings(StorySettings.model_validate(defaults)).model_dump(), **data})
        elif kind == 'link':
            if set(data) != {'card_id', 'original_id'} or any(type(v) is not int or v <= 0 for v in data.values()):
                raise ValueError('明确指定 card_id 与 original_id')
        elif kind == 'regenerate':
            if set(data) != {'conversation', 'card_id'} or type(data['card_id']) is not int:
                raise ValueError('明确指定 conversation 与 card_id')
            target_of(str(data['conversation']))
        else:
            required = {'run_id', 'revision', 'conversation', 'character', 'user'}
            if not required <= set(data) or set(data) - required - {'confirmed'}:
                raise ValueError('必须指定完整存档目标、版本和操作')
            snapshot = store.snapshot(str(data['run_id']))
            if any(data[key] != snapshot[key] for key in ('conversation', 'character', 'user', 'revision')):
                raise ValueError('存档目标或版本已变化，请刷新')
            if kind in ('restart', 'import_legacy') and data.get('confirmed') != snapshot['user']:
                raise ValueError('输入目标 QQ 号以确认操作')
        return {'id': store.queue(str(kind), data), 'status': 'queued'}
    finally:
        store.close()
