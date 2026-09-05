"""验证 WebUI 只读观察面板的接口结构和鉴权边界。

本模块覆盖运行状态、事件追踪、人物信息和会话列表的响应字段，
确保只读观察接口需要令牌且不会修改业务状态。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.api.auth import SESSION_COOKIE_NAME, token_manager
from src.core.api.http import router as http_router
from src.core.api.ws import router as ws_router
from src.core.logging.logger import WebUiLogHandler
from src.core.memory.store import FactInput
from src.core.observe.store import event_store
from src.core.persona.state import EventDelta
from src.core.platform_io.registry import StreamRegistry
from src.core.config.schema import Config
from src.core.services.chat import ChatService
from src.core.webui.logs import webui_logs


async def _noop(_channel: str, _payload: Any, _stream_id: int = 1) -> None:
    return None


def test_observability_snapshot_keeps_only_conversation_fields(db) -> None:
    registry = StreamRegistry(db)
    desktop = registry.desktop_context()
    contact = registry.create_person('contact', first_seen_at=1_700_000_000_000)
    registry.link_identity(contact, 'qq', '10001', '测试联系人')
    direct = registry.get_or_create_stream('qq', 'direct', '10001')
    chat = ChatService(db, None, None, None, _noop, cfg=Config())
    now = 1_700_000_100_000

    chat.persona.apply_event(contact.id, EventDelta(favor=3), now, weight=1.0)
    chat.memory.add_fact(desktop.person.id, FactInput(kind='偏好', content='桌面事实'), now)
    chat.memory.add_fact(contact.id, FactInput(kind='偏好', content='QQ 事实'), now)
    chat.memory.append_message(desktop.stream.id, desktop.person.id, 'user', '桌面消息', now)
    chat.memory.append_message(direct.id, contact.id, 'user', 'QQ 消息一', now)
    chat.memory.append_message(direct.id, contact.id, 'user', 'QQ 消息二', now + 1)

    desktop_payload = chat.observability_snapshot(desktop.stream.id, now=now + 2)
    direct_payload = chat.observability_snapshot(direct.id, now=now + 2)

    assert 'persona' not in desktop_payload
    assert 'memory' not in direct_payload
    assert desktop_payload['conversation']['workingMessages'] == 1
    assert direct_payload['conversation']['workingMessages'] == 2
    assert [person['displayName'] for person in desktop_payload['conversation']['participants']] == ['用户本人']
    assert [person['displayName'] for person in direct_payload['conversation']['participants']] == ['测试联系人']


def test_observability_snapshot_rejects_unknown_stream(db) -> None:
    chat = ChatService(db, None, None, None, _noop, cfg=Config())

    with pytest.raises(ValueError, match='stream 99999 不存在'):
        chat.observability_snapshot(99999)


@pytest.fixture
def web_client() -> TestClient:
    token_manager.configure('browser-test-token')
    app = FastAPI()
    app.include_router(ws_router)
    app.include_router(http_router)
    with TestClient(app) as client:
        yield client


def test_browser_login_exchanges_token_for_httponly_cookie(web_client: TestClient) -> None:
    assert web_client.get('/auth/session').json() == {'authenticated': False}
    response = web_client.post('/auth/login', json={'token': 'browser-test-token'})

    assert response.status_code == 200
    assert response.json() == {'ok': True}
    assert 'browser-test-token' not in response.text
    cookie = response.headers['set-cookie']
    assert cookie.startswith(f'{SESSION_COOKIE_NAME}=')
    assert 'HttpOnly' in cookie
    assert 'SameSite=strict' in cookie
    assert 'Path=/' in cookie
    assert web_client.get('/auth/session').json() == {'authenticated': True}
    assert web_client.get('/streams').status_code == 200


def test_browser_login_cookie_does_not_reuse_master_token(web_client: TestClient) -> None:
    response = web_client.post('/auth/login', json={'token': 'browser-test-token'})

    assert response.status_code == 200
    assert response.cookies[SESSION_COOKIE_NAME] != 'browser-test-token'


def test_master_token_is_rejected_as_browser_cookie() -> None:
    token_manager.configure('browser-test-token')
    app = FastAPI()
    app.include_router(http_router)
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, 'browser-test-token')

        assert client.get('/streams').status_code == 401


def test_logout_revokes_only_current_browser_session() -> None:
    token_manager.configure('browser-test-token')
    app = FastAPI()
    app.include_router(http_router)
    with TestClient(app) as first, TestClient(app) as second:
        first.post('/auth/login', json={'token': 'browser-test-token'}).raise_for_status()
        second.post('/auth/login', json={'token': 'browser-test-token'}).raise_for_status()
        first_cookie = first.cookies[SESSION_COOKIE_NAME]
        second_cookie = second.cookies[SESSION_COOKIE_NAME]

        assert first_cookie != second_cookie
        assert first.post('/auth/logout').status_code == 200
        first.cookies.set(SESSION_COOKIE_NAME, first_cookie)
        assert first.get('/streams').status_code == 401
        assert second.get('/streams').status_code == 200


def test_reconfiguring_master_token_invalidates_existing_browser_sessions() -> None:
    token_manager.configure('browser-test-token')
    app = FastAPI()
    app.include_router(http_router)
    with TestClient(app) as client:
        client.post('/auth/login', json={'token': 'browser-test-token'}).raise_for_status()
        assert client.get('/streams').status_code == 200

        token_manager.configure('next-process-token')

        assert client.get('/streams').status_code == 401


def test_browser_data_endpoints_reject_unauthenticated_requests() -> None:
    app = FastAPI()
    app.include_router(http_router)
    with TestClient(app) as client:
        assert client.get('/health').status_code == 200
        for path in (
            '/runtime/health',
            '/streams',
            '/observability?streamId=1',
            '/api/persons',
            '/api/persons/1',
            '/diary',
        ):
            assert client.get(path).status_code == 401
        assert client.get('/debug/trace').status_code == 404


def test_browser_websocket_accepts_login_cookie(web_client: TestClient) -> None:
    web_client.post('/auth/login', json={'token': 'browser-test-token'}).raise_for_status()

    with web_client.websocket_connect('/ws?client=desktop') as websocket:
        websocket.close()


def test_webui_log_stream_keeps_module_color_and_chinese_alias(web_client: TestClient) -> None:
    webui_logs.clear()
    WebUiLogHandler()(None, 'warning', {
        'timestamp': '08-08 15:00:00',
        'level': 'warning',
        'logger': 'src.core.services.chat.service',
        'event': '测试日志',
    })
    web_client.post('/auth/login', json={'token': 'browser-test-token'}).raise_for_status()

    with web_client.websocket_connect('/ws/logs') as websocket:
        item = websocket.receive_json()

    assert item['seq'] == 1
    assert '[对话]' in item['line']
    assert '测试日志' in item['line']
    assert '\033[' in item['line']


def test_event_websocket_replays_disconnect_gap_without_duplicates(
    web_client: TestClient,
) -> None:
    first = event_store.append('user_input', 'received', 1, 1, {'text': '第一轮'})
    web_client.post('/auth/login', json={'token': 'browser-test-token'}).raise_for_status()
    with web_client.websocket_connect('/ws/events?since=0') as websocket:
        initial = websocket.receive_json()['events']
    assert [entry['seq'] for entry in initial] == [first['seq']]

    for turn in (2, 3):
        event_store.append('user_input', 'received', 1, turn, {'text': f'第{turn}轮'})
        event_store.append('llm_final', 'generating', 1, turn, {'text': '回复'})
    with web_client.websocket_connect(f"/ws/events?since={first['seq']}") as websocket:
        replay = websocket.receive_json()['events']
    seqs = [entry['seq'] for entry in replay]
    assert len(replay) == 4
    assert seqs == sorted(set(seqs))


def test_event_websocket_reports_truncated_replay(web_client: TestClient) -> None:
    for index in range(1_005):
        event_store.append('probe', '', 1, index, {'index': index})
    web_client.post('/auth/login', json={'token': 'browser-test-token'}).raise_for_status()
    with web_client.websocket_connect('/ws/events?since=0') as websocket:
        frame = websocket.receive_json()
    assert frame['truncated'] is True
    assert frame['from'] > 1
    assert len(frame['events']) == 1_000
