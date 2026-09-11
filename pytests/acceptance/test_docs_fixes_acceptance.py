from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import sqlite3

import pytest

from src.core.config.schema import Config
from src.core.agent.prompt import build_system_prompt
from src.core.observe.store import EventStore
from src.core.prompts.registry import (
    CHAT_SYSTEM_COMPONENTS,
    PromptTemplate,
    configure_prompts,
    get_prompt,
    reset_prompts_for_tests,
    update_prompt,
)
from src.core.services.proactive import AwarenessService
from src.core.schedule.timeline import ActivityTimeline
from src.core.services.dev.replay import (
    _rebuild_messages,
    _render_current_prompt,
    replay_event,
    replay_task,
)


class _Chat:
    ready = False

    def __init__(self) -> None:
        self.memory = _Memory()
        self.persona = _Persona()
        self.desktop_context = type('Context', (), {
            'person': type('Person', (), {'id': 1})(),
            'stream': type('Stream', (), {'id': 1})(),
        })()
        self.messages: list[Any] = []

    def current_sleep(self):
        return type('Sleep', (), {'asleep': False})()

    async def summarize_deep_sleep(self, sleep):
        """无头桩不实现汇总；真实 ChatService 由独立用例覆盖。"""
        return None

    def set_activity_provider(self, provider):
        self.activity_provider = provider

    def set_sleep_state_provider(self, provider):
        self.sleep_provider = provider

    def set_promise_handler(self, handler):
        self.promise_handler = handler


class _Memory:
    def load_pending_promises(self):
        return []

    def save_pending_promises(self, values):
        self.promises = values

    def last_message_at(self, stream_id):
        return None


class _Persona:
    def get(self, person_id):
        return type('Persona', (), {'intimacy': 0, 'energy': 0, 'mood': 50})()

    def inspect(self, person_id):
        return self.get(person_id)


async def _push(channel, payload):
    return None


@pytest.mark.asyncio
async def test_headless_awareness_has_no_signal_or_delivery(
    db: sqlite3.Connection,
) -> None:
    service = AwarenessService(_Chat(), None, ActivityTimeline(db), Config(), _push)
    await service._tick()
    await service._flush_pending(1)
    fields = service.observability_fields(1_760_000_000_000)
    assert fields['sensing'] == {
        'activity': 'idle',
        'description': '',
        'minutes': 0,
        'silent': False,
        'pending': [],
        'visionStats': {'enabled': False, 'looks': 0, 'spoke': 0},
    }


def test_replay_task_maps_all_prompt_ids() -> None:
    expected = {
        'chat.system': 'chat',
        'chat.proactive': 'chat',
        'summary': 'summary',
        'schedule': 'schedule',
        'expression.select': 'expression',
        'vision.glance': 'vision',
    }
    for prompt_id, task in expected.items():
        assert replay_task({'seq': 1, 'promptId': prompt_id}) == task


def test_replay_renders_current_prompt_and_hashes_actual_text() -> None:
    event = {
        'seq': 1,
        'promptId': 'vision.glance',
        'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': '旧提示词'}]}],
        'renderParams': {'vision.glance': {'app_hint': '当前程序是测试应用。'}},
    }
    messages, prompt_hash = _rebuild_messages(event)
    actual = messages[0]['content'][0]['text']
    assert '旧提示词' not in actual
    assert '{{' not in actual and '}}' not in actual
    assert '不查询或补充任何当前业务数据' not in actual
    assert prompt_hash == sha256(actual.encode('utf-8')).hexdigest()[:8]


def test_replay_rejects_legacy_event_without_render_params() -> None:
    with pytest.raises(ValueError, match='早于渲染参数落库'):
        _rebuild_messages({
            'seq': 1,
            'promptId': 'summary',
            'messages': [{'role': 'system', 'content': '旧提示词'}],
        })


def _chat_replay_event() -> dict[str, Any]:
    return {
        'seq': 1,
        'promptId': 'chat.system',
        'messages': [{'role': 'system', 'content': '旧系统提示词'}],
        'renderParams': {
            'chat.boundaries': {},
            'chat.discipline': {},
            'chat.length.brief': {},
            'chat.protocol': {
                'emotions': 'normal',
                'gestures': 'heart',
                'emoji_rule': '本轮不支持发送表情包，不要写 <emoji> 标签。',
            },
            'chat.system': {
                'name': '测试角色', 'identity': '测试身份', 'relationship': '',
                'time_context': '测试时间', 'birthday_note': '', 'resumption': '',
                'persona': '', 'activity': '', 'scene': '',
                'jargon': '', 'impressions': '', 'shared_groups': '',
                'facts': '', 'episodes': '',
                'reply_style': '自然回复', 'tone': '', 'expression_habits': '',
                'length': '',
                'discipline': '冻结的旧纪律', 'boundaries': '冻结的旧边界',
                'protocol': '冻结的旧协议',
            },
        },
    }


def _production_chat_event() -> dict[str, Any]:
    render_params: dict[str, dict[str, str]] = {}
    prompt = build_system_prompt(
        name='测试角色',
        birthday='2000-01-01',
        personality='测试身份',
        reply_style='自然回复',
        reply_length='brief',
        now=datetime(2026, 8, 12, 14, 0),
        render_params=render_params,
    )
    return {
        'seq': 1,
        'promptId': 'chat.system',
        'promptHash': '模板集合指纹',
        'messages': [{'role': 'system', 'content': prompt}],
        'renderParams': render_params,
    }


class _ReplayTextProvider:
    async def stream(self, **kwargs: Any):
        yield {'text': '重放输出'}


def test_replay_prompt_matches_production_byte_for_byte() -> None:
    event = _production_chat_event()
    messages, _ = _rebuild_messages(event)

    assert messages[0]['content'] == event['messages'][0]['content']


@pytest.mark.asyncio
async def test_unchanged_replay_uses_matching_prompt_hashes(tmp_path: Path) -> None:
    store = EventStore()
    store.configure(tmp_path / 'replay-hash.db')
    event = _production_chat_event()
    source = store.append('llm_request', 'generating', 1, 1, {
        key: value for key, value in event.items() if key != 'seq'
    })
    try:
        result = await replay_event(store, _ReplayTextProvider(), source['seq'])
        assert result['originalPromptHash'] == result['replayPromptHash']
    finally:
        store.close()


@pytest.mark.asyncio
async def test_changed_protocol_uses_different_prompt_hashes(tmp_path: Path) -> None:
    configure_prompts(tmp_path)
    store = EventStore()
    store.configure(tmp_path / 'changed-protocol.db')
    event = _production_chat_event()
    source = store.append('llm_request', 'generating', 1, 1, {
        key: value for key, value in event.items() if key != 'seq'
    })
    try:
        update_prompt('chat.protocol', '新版协议 {{emotions}} / {{gestures}} / {{emoji_rule}}')
        result = await replay_event(store, _ReplayTextProvider(), source['seq'])
        assert result['originalPromptHash'] != result['replayPromptHash']
        assert result['templatePromptHash'] == '模板集合指纹'
    finally:
        store.close()
        reset_prompts_for_tests()


@pytest.mark.asyncio
async def test_vision_replay_hashes_original_text_part(tmp_path: Path) -> None:
    store = EventStore()
    store.configure(tmp_path / 'vision-hash.db')
    original_prompt = get_prompt('vision.glance').render(app_hint='当前程序是测试应用。')
    source = store.append('llm_request', 'watching', 1, 1, {
        'promptId': 'vision.glance',
        'promptHash': '视觉模板集合指纹',
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': original_prompt},
                {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}},
            ],
        }],
        'renderParams': {'vision.glance': {'app_hint': '当前程序是测试应用。'}},
    })
    try:
        result = await replay_event(store, _ReplayTextProvider(), source['seq'])
        expected = sha256(original_prompt.encode('utf-8')).hexdigest()[:8]
        assert result['originalPromptHash'] == expected
        assert result['replayPromptHash'] == expected
    finally:
        store.close()


def test_replay_chat_uses_current_protocol_text(tmp_path: Path) -> None:
    configure_prompts(tmp_path)
    try:
        update_prompt('chat.protocol', '新版协议 {{emotions}} / {{gestures}} / {{emoji_rule}}')
        messages, _ = _rebuild_messages(_chat_replay_event())
        assert '新版协议 normal / heart' in messages[0]['content']
    finally:
        reset_prompts_for_tests()


def test_replay_hash_changes_with_current_protocol(tmp_path: Path) -> None:
    configure_prompts(tmp_path)
    try:
        _, before_hash = _rebuild_messages(_chat_replay_event())
        update_prompt('chat.protocol', '新版协议 {{emotions}} / {{gestures}} / {{emoji_rule}}')
        _, after_hash = _rebuild_messages(_chat_replay_event())
        assert after_hash != before_hash
    finally:
        reset_prompts_for_tests()


def test_replay_chat_uses_current_system_template(tmp_path: Path) -> None:
    configure_prompts(tmp_path)
    try:
        current = get_prompt('chat.system').text
        update_prompt('chat.system', f'当前系统模板\n{current}')
        messages, _ = _rebuild_messages(_chat_replay_event())
        assert messages[0]['content'].startswith('当前系统模板\n')
    finally:
        reset_prompts_for_tests()


def test_replay_chat_rejects_user_first_message() -> None:
    event = _chat_replay_event()
    event['messages'] = [{'role': 'user', 'content': '用户正文'}]
    with pytest.raises(ValueError, match='首条消息角色应为 system'):
        _rebuild_messages(event)


def test_replay_derives_new_chat_component_from_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.core.prompts.registry as registry_module
    import src.core.services.dev.replay as replay_module

    original_system = get_prompt('chat.system')
    temporary_templates = {
        'chat.extra': PromptTemplate(
            id='chat.extra',
            text='当前额外规则',
            source=Path('临时模板'),
            placeholders=frozenset(),
            sha256='temporary-extra',
        ),
        'chat.system': PromptTemplate(
            id='chat.system',
            text=f'{original_system.text}\n{{{{extra}}}}',
            source=Path('临时系统模板'),
            placeholders=original_system.placeholders | frozenset({'extra'}),
            sha256='temporary-system',
        ),
    }
    monkeypatch.setitem(CHAT_SYSTEM_COMPONENTS, 'chat.extra', 'extra')
    monkeypatch.setattr(
        registry_module,
        'get_prompt',
        lambda template_id: (
            temporary_templates[template_id]
            if template_id in temporary_templates
            else get_prompt(template_id)
        ),
    )
    monkeypatch.setattr(replay_module, 'render_chat_system', registry_module.render_chat_system)
    event = _chat_replay_event()
    event['renderParams']['chat.extra'] = {}
    event['renderParams']['chat.system']['extra'] = '冻结的旧额外规则'

    prompt = _render_current_prompt(
        event,
        (*CHAT_SYSTEM_COMPONENTS, 'chat.system'),
    )

    assert '当前额外规则' in prompt
    assert '冻结的旧额外规则' not in prompt


def test_stage_scan_is_single_query_and_marks_truncation(tmp_path: Path) -> None:
    store = EventStore()
    store.configure(tmp_path / 'events.db')
    for index in range(55):
        store.append('stage', 'generating', 1, index, {'streamName': '桌面'}, at=index)
    snapshot = store.current_stages(scan_limit=50)[0]
    assert snapshot['stageStartedAtTruncated'] is True
    store.append('stage', 'replied', 1, 56, {'streamName': '桌面'}, at=56)
    snapshot = store.current_stages(scan_limit=50)[0]
    assert snapshot['stageStartedAtTruncated'] is False
    store.close()
