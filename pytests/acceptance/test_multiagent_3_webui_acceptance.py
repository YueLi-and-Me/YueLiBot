"""multiagent-3-webui 的 V-1 至 V-12 验收用例。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

import pytest

from src.core.api.auth import token_manager
from src.core.api.http import router
from src.core.api.state import app_state
from src.core.observe.store import EventStore, event_store
from src.core.prompts.registry import configure_prompts, reset_prompts_for_tests
from src.core.services.dev.replay import replay_event



def _row_count(db: sqlite3.Connection, table: str) -> int:
    """按固定分支读取表行数；标识位不支持参数绑定，整句字面量是唯一安全形态。"""
    if table == "messages":
        return db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    if table == "persona_bond":
        return db.execute("SELECT COUNT(*) FROM persona_bond").fetchone()[0]
    if table == "facts":
        return db.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    raise ValueError(f"未登记的表名：{table}")



class _ReplayProvider:
    ready = True

    async def stream(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.85,
        max_tokens: int | None = None,
        signal: Any = None,
        response_format: dict[str, str] | None = None,
    ) -> AsyncIterator[dict[str, str]]:
        assert '测试角色' in messages[0]['content']
        assert '原请求上下文快照' not in messages[0]['content']
        yield {'text': '重放输出'}


def _client(host: str = '127.0.0.1') -> TestClient:
    token_manager.configure('acceptance-token')
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, client=(host, 50_000))
    client.headers['Authorization'] = 'Bearer acceptance-token'
    return client


def test_v1_v2_stage_board_survives_restart_and_keeps_first_same_stage_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.core.observe.store as store_module

    path = tmp_path / 'events.db'
    store = EventStore()
    store.configure(path)
    for at, turn in ((1_000, 1), (2_000, 2), (3_000, 3)):
        store.append('stage', 'received', 7, turn, {'streamName': 'QQ 私聊', 'detail': str(turn)}, at=at)
    monkeypatch.setattr(store_module, 'current_time', lambda: 5_000)
    before = store.current_stages()
    store.close()
    store.configure(path)
    after = store.current_stages()
    assert before == after
    assert after[0]['stageElapsedMs'] == 4_000
    store.close()


def test_v3_event_turn_query_matches_database() -> None:
    for index in range(5):
        event_store.append('probe', '', 1, 88, {'index': index})
    event_store.append('probe', '', 1, 89, {'index': 99})
    connection = event_store._connection
    assert connection is not None
    expected = connection.execute(
        'SELECT COUNT(*) FROM pipeline_events WHERE turn_id = 88'
    ).fetchone()[0]
    with _client() as client:
        payload = client.get('/events?turnId=88&limit=1000').json()
    assert len(payload['events']) == expected


def test_v4_to_v7_prompt_edit_validation_reload_fixed_and_loopback(tmp_path: Path) -> None:
    configure_prompts(tmp_path)
    target = tmp_path / 'prompts' / 'summary.md'
    try:
        with _client() as client:
            before = client.get('/prompts/summary').json()['promptHash']
            invalid = client.put('/prompts/summary', json={'content': '{{character_name}}'})
            assert invalid.status_code == 400
            assert not target.exists()
            valid_text = '验收版 {{character_name}} / {{character_personality}}'
            valid = client.put('/prompts/summary', json={'content': valid_text})
            assert valid.status_code == 200
            assert valid.json()['promptHash'] != before
            fixed = client.put('/prompts/chat.boundaries', json={'content': '禁止修改'})
            assert fixed.status_code == 403
            assert client.get('/prompts/chat.boundaries').status_code == 200
        with _client('192.0.2.10') as remote:
            denied = remote.put('/prompts/summary', json={'content': valid_text})
            assert denied.status_code == 403
    finally:
        reset_prompts_for_tests()


@pytest.mark.asyncio
async def test_v8_v9_replay_preserves_business_tables_and_uses_replay_kinds(
    db: sqlite3.Connection,
) -> None:
    request = event_store.append('llm_request', 'generating', 1, 4, {
        'messages': [
            {'role': 'system', 'content': '原上下文'},
            {'role': 'user', 'content': '你好'},
        ],
        'temperature': 0.5,
        'maxTokens': 100,
        'promptId': 'chat.system',
        'promptHash': 'oldhash',
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
                    'discipline': '只说已知内容', 'boundaries': '遵守边界',
                'protocol': '输出测试协议',
            },
        },
    })
    event_store.append('llm_final', 'generating', 1, 4, {'text': '原输出'})
    before = {
        table: _row_count(db, table)
        for table in ('messages', 'persona_bond', 'facts')
    }
    llm_count = len(event_store.search(kinds=['llm_request'], limit=1_000).events)
    result = await replay_event(event_store, _ReplayProvider(), request['seq'])
    after = {
        table: _row_count(db, table)
        for table in ('messages', 'persona_bond', 'facts')
    }
    assert before == after
    assert result['originalOutput'] == '原输出'
    assert result['replayOutput'] == '重放输出'
    assert len(event_store.search(kinds=['llm_request'], limit=1_000).events) == llm_count
    assert len(event_store.search(kinds=['replay_request'], limit=1_000).events) == 1
    assert len(event_store.search(kinds=['replay_final'], limit=1_000).events) == 1


def test_v10_deleting_override_returns_to_builtin(tmp_path: Path) -> None:
    configure_prompts(tmp_path)
    try:
        with _client() as client:
            client.put('/prompts/summary', json={
                'content': '覆盖 {{character_name}} / {{character_personality}}',
            }).raise_for_status()
            assert client.delete('/prompts/summary').status_code == 200
            items = client.get('/prompts').json()['prompts']
        summary = next(item for item in items if item['id'] == 'summary')
        assert summary['source'] == 'builtin'
    finally:
        reset_prompts_for_tests()


def test_v11_electron_window_and_debug_trace_are_absent() -> None:
    root = Path(__file__).parents[2]
    for relative in (
        'electron/renderer/observability.html',
        'electron/renderer/observability.ts',
        'electron/preload/observability.ts',
        'electron/main/platform/observabilityWindow.ts',
    ):
        assert not (root / relative).exists()
    with _client() as client:
        assert client.get('/debug/trace').status_code == 404


def test_v12_expired_stream_disappears_from_stage_board(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.core.observe.store as store_module

    monkeypatch.setattr(store_module, '_CLEANUP_EVERY', 1)
    store = EventStore()
    store.configure(tmp_path / 'retention.db', retention_hours=1)
    store.append('stage', 'received', 1, 1, {'streamName': '过期'}, at=1_000)
    store.append('stage', 'received', 2, 2, {'streamName': '当前'}, at=3_602_000)
    assert [item['streamId'] for item in store.current_stages()] == [2]
    store.close()


@pytest.mark.asyncio
async def test_replay_http_uses_chat_router() -> None:
    request = event_store.append('llm_request', 'generating', 1, 4, {
        'messages': [{'role': 'system', 'content': '上下文'}],
        'promptId': 'chat.system',
        'promptHash': 'before',
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
                    'discipline': '只说已知内容', 'boundaries': '遵守边界',
                'protocol': '输出测试协议',
            },
        },
    })
    event_store.append('llm_final', 'generating', 1, 4, {'text': '原输出'})
    previous = app_state.routers
    provider = _ReplayProvider()
    app_state.routers = SimpleNamespace(for_task=lambda task: provider)
    try:
        with _client() as client:
            response = client.post('/replay', json={'seq': request['seq']})
        assert response.status_code == 200
        assert response.json()['replayOutput'] == '重放输出'
    finally:
        app_state.routers = previous
