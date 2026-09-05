"""后端就绪公告的时序断言。

本模块断言公告发出的时序，而不是公告文本的格式。
  把公告挂在 FastAPI 的 lifespan 上曾经通过了所有既有测试，因为 lifespan 确实跑完了——
  错的是 uvicorn **先跑 lifespan、后绑 socket**，那时候端口还没开始监听。
  所以下面每条断言都打在可观察后果上：公告那一刻能不能连上、失败时有没有公告。
"""

from contextlib import closing
from typing import Any, Dict, List

import asyncio
import socket

import pytest
from pathlib import Path

import uvicorn

from src import main
from src.main import _ReadyAnnouncingServer, _bind_backend_socket


async def _empty_app(scope: Dict[str, Any], receive: Any, send: Any) -> None:
    """最小 ASGI 应用；这些测试只关心服务器起停，不关心业务。"""
    if scope['type'] == 'lifespan':
        while True:
            message = await receive()
            if message['type'] == 'lifespan.startup':
                await send({'type': 'lifespan.startup.complete'})
            elif message['type'] == 'lifespan.shutdown':
                await send({'type': 'lifespan.shutdown.complete'})
                return


async def _failing_lifespan_app(scope: Dict[str, Any], receive: Any, send: Any) -> None:
    """装配阶段就失败的应用，用来验「没起来就不许宣告就绪」。"""
    if scope['type'] == 'lifespan':
        await receive()
        await send({'type': 'lifespan.startup.failed', 'message': '装配失败'})


def _server(app: Any) -> _ReadyAnnouncingServer:
    # 运行时坐标没有默认值：服务器必须拿到真实的端口、token 与凭据文件路径，
    # 才能在监听建立后公告唯一一次 WebUI 入口。
    # children 传空列表：本文件只验证就绪公告的时机，不拉任何真实子进程。
    return _ReadyAnnouncingServer(
        uvicorn.Config(app, log_level='critical', access_log=False),
        port=7999,
        token='0' * 64,
        runtime_path=Path('data/runtime/backend.json'),
        children=[],
    )


def _record_webui_ready(monkeypatch: pytest.MonkeyPatch) -> List[tuple]:
    """记录 WebUI 就绪框的调用，顺带避免测试输出里出现 token。"""
    printed: List[tuple] = []
    monkeypatch.setattr(
        main,
        '_announce_webui_ready',
        lambda port, token, runtime_path: printed.append((port, token, runtime_path)),
    )
    return printed


def _can_connect(port: int) -> bool:
    with closing(socket.socket()) as probe:
        probe.settimeout(2)
        return probe.connect_ex(('127.0.0.1', port)) == 0


def _record_connectivity_at_announcement(
    monkeypatch: pytest.MonkeyPatch,
    port: int,
) -> List[bool]:
    """把就绪公告换成一次探针，记录「宣告那一刻端口能不能连」。"""
    observed: List[bool] = []
    monkeypatch.setattr(main, '_announce_ready', lambda: observed.append(_can_connect(port)))
    return observed


def test_ready_announced_only_after_socket_accepts_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """发出就绪公告时，端口必须已经处于监听状态。"""
    sock = _bind_backend_socket(0)
    port = sock.getsockname()[1]
    observed = _record_connectivity_at_announcement(monkeypatch, port)
    printed = _record_webui_ready(monkeypatch)
    server = _server(_empty_app)

    async def scenario() -> None:
        # 先确认「绑了但还没监听」时确实连不上，否则下面那条断言
        # 可能只是因为探针恒为真才通过。
        assert _can_connect(port) is False
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started:
            await asyncio.sleep(0.01)
        server.should_exit = True
        await serving

    asyncio.run(scenario())

    assert observed == [True]
    # WebUI 就绪框和就绪公告是同一个时刻的事：地址打出来时端口就必须已经能连。
    assert printed == [(7999, '0' * 64, Path('data/runtime/backend.json'))]


def test_ready_not_announced_when_startup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """装配失败时不得发出就绪公告，避免调用方误判后端状态。"""
    sock = _bind_backend_socket(0)
    observed = _record_connectivity_at_announcement(monkeypatch, sock.getsockname()[1])
    printed = _record_webui_ready(monkeypatch)
    server = _server(_failing_lifespan_app)

    with pytest.raises(SystemExit):
        asyncio.run(server.serve(sockets=[sock]))

    assert observed == []
    # 装配失败时同样不许打 WebUI 就绪框：那会把人指向一个不响应的连接。
    assert printed == []
    sock.close()


def test_bound_socket_is_held_until_handed_to_the_server() -> None:
    """就绪公告对应的端口必须持续由服务器持有，避免公告后出现可抢占窗口。"""
    sock = _bind_backend_socket(0)
    port = sock.getsockname()[1]

    with closing(socket.socket()) as intruder:
        with pytest.raises(OSError):
            intruder.bind(('127.0.0.1', port))

    sock.close()
