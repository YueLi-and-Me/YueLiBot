"""验证优雅关机端点的鉴权边界与聊天服务在飞回合收尾。

覆盖新增的 ``POST /runtime/shutdown``（401/503/置位）以及 ``ChatService.shutdown``
等待在飞回合结束的行为，确保退出路径不再是跳过清理的硬杀。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.api.auth import token_manager
from src.core.api.http import router as http_router
from src.core.api.state import app_state
from src.core.config.schema import Config
from src.core.services.chat import ChatService, _InflightTurn


@pytest.fixture
def client() -> TestClient:
    token_manager.configure('shutdown-test-token')
    app = FastAPI()
    app.include_router(http_router)
    with TestClient(app, client=('127.0.0.1', 50_000)) as c:
        yield c
    app_state.uvicorn_server = None


def test_shutdown_requires_auth(client: TestClient) -> None:
    assert client.post('/runtime/shutdown').status_code == 401


def test_shutdown_without_server_returns_503(client: TestClient) -> None:
    app_state.uvicorn_server = None
    response = client.post(
        '/runtime/shutdown',
        headers={'Authorization': 'Bearer shutdown-test-token'},
    )
    assert response.status_code == 503


def test_shutdown_sets_should_exit(client: TestClient) -> None:
    class _FakeServer:
        should_exit = False

    server = _FakeServer()
    app_state.uvicorn_server = server
    response = client.post(
        '/runtime/shutdown',
        headers={'Authorization': 'Bearer shutdown-test-token'},
    )
    assert response.status_code == 200
    assert response.json() == {'ok': True}
    assert server.should_exit is True


async def test_chat_shutdown_waits_for_inflight_turn(db) -> None:
    """shutdown 返回前，被 interrupt 的在飞回合任务必须已收尾。"""

    async def _noop(_channel, _payload, _stream_id=1):
        return None

    chat = ChatService(db, None, None, None, _noop, cfg=Config())
    cancel_event = asyncio.Event()
    finished = False

    async def _turn() -> None:
        nonlocal finished
        try:
            await cancel_event.wait()
        finally:
            finished = True

    chat._inflight[1] = _InflightTurn(
        task=asyncio.create_task(_turn()),
        cancel_event=cancel_event,
    )
    await chat.shutdown()
    assert finished
