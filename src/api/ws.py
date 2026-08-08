"""
WebSocket 事件推流。

Python → Electron 的所有主动推送都走这个连接：
  chat.event / chat.done / chat.error
  voice.play
  vision.watching
  sleep.state

Electron → Python 的命令走 HTTP（更简单，有状态码，易排查）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Set, cast

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .auth import ws_auth

from src.common.logger import get_logger
from src.services.trace import trace
from src.webui.logs import webui_logs

logger = get_logger(__name__)
router = APIRouter()

# ─────────────────────────────────────────────────────────────────────
# 连接管理
# ─────────────────────────────────────────────────────────────────────

ClientKind = Literal['desktop', 'napcat']
_CLIENT_KINDS = frozenset({'desktop', 'napcat'})
_DESKTOP_STREAM_ID = 1


class _ConnectionManager:
    """管理按客户端分区的 WebSocket 订阅。"""

    def __init__(self) -> None:
        self._connections: Dict[ClientKind, Set[WebSocket]] = {
            'desktop': set(),
            'napcat': set(),
        }
        self._lock = asyncio.Lock()

    async def connect(self, client: ClientKind, ws: WebSocket) -> None:
        async with self._lock:
            self._connections[client].add(ws)

    async def disconnect(self, client: ClientKind, ws: WebSocket) -> None:
        async with self._lock:
            self._connections[client].discard(ws)

    async def push(self, stream_id: int, channel: str, payload: Any) -> int:
        """按 stream 所属分支推送，并返回实际完成投递的连接数。"""
        client: ClientKind = 'desktop' if stream_id == _DESKTOP_STREAM_ID else 'napcat'
        async with self._lock:
            connections: List[WebSocket] = list(self._connections[client])
        if not connections:
            if client == 'napcat':
                trace.emit('outbound_dropped', streamId=stream_id, channel=channel)
            return 0

        envelope = json.dumps(
            {'stream_id': stream_id, 'channel': channel, 'payload': payload},
            ensure_ascii=False,
        )
        delivered = 0
        failed: List[WebSocket] = []
        for ws in connections:
            try:
                await ws.send_text(envelope)
                delivered += 1
            except Exception as exc:
                logger.warning('ws_push_failed', client=client, channel=channel, error=str(exc))
                failed.append(ws)
        for ws in failed:
            await self.disconnect(client, ws)
        return delivered


manager = _ConnectionManager()


async def push(stream_id: int, channel: str, payload: Any) -> int:
    """模块级推送入口，供其他 service 调用。"""
    return await manager.push(stream_id, channel, payload)


# ─────────────────────────────────────────────────────────────────────
# 路由
# ─────────────────────────────────────────────────────────────────────

@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    # ★ 鉴权必须在 accept() 之前做。
    #   accept() 之后再发 close() 会触发一次完整的握手+关闭握手，
    #   对端能收到错误码；但 undici 对「accept 了再拒绝」这种组合会日志一条 warning
    #   然后认为连接成功，接着发 receive 就报错 —— 行为不一致且难排查。
    #   在 accept() 之前调 close() 在 ASGI 层面等于「直接关闭 TCP」，
    #   对端得到的是 1002 Protocol Error，客户端会老老实实停止重连。
    if not await ws_auth(websocket):
        await websocket.close(code=1008)   # 1008 = policy violation
        logger.warning("ws_auth_failed", client=websocket.client)
        return

    raw_client = websocket.query_params.get('client')
    if raw_client not in _CLIENT_KINDS:
        await websocket.close(code=1008)
        logger.warning('ws_client_invalid', client=raw_client)
        return
    client = cast(ClientKind, raw_client)

    # ★ 必须把客户端选的子协议回显回去。
    #   客户端发 "yueli-<token>"，服务端不回显 → RFC 6455 §4.1 规定握手失败。
    #   Python websockets 库宽松，但 undici（Electron 用的那个）严格遵守规范。
    selected_proto = None
    raw = websocket.headers.get("sec-websocket-protocol", "")
    for proto in raw.split(","):
        proto = proto.strip()
        if proto.startswith("yueli-"):
            selected_proto = proto
            break

    await websocket.accept(subprotocol=selected_proto)
    await manager.connect(client, websocket)
    logger.info('ws_connected', client=client, peer=str(websocket.client))

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        logger.info('ws_disconnected', client=client)
    except Exception as exc:
        logger.warning("ws_error", error=str(exc))
    finally:
        await manager.disconnect(client, websocket)


@router.websocket('/ws/logs')
async def webui_logs_endpoint(websocket: WebSocket) -> None:
    """向已登录浏览器推送与控制台同款的 ANSI 彩色日志。"""
    if not await ws_auth(websocket):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    subscriber, backlog = webui_logs.subscribe()
    try:
        for item in backlog:
            await websocket.send_json(item)
        while True:
            await websocket.send_json(await subscriber.queue.get())
    except WebSocketDisconnect:
        pass
    finally:
        webui_logs.unsubscribe(subscriber)
