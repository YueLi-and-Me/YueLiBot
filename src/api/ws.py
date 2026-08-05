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

import asyncio
import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from src.common.logger import get_logger
from .auth import ws_auth

logger = get_logger(__name__)
router = APIRouter()

# ─────────────────────────────────────────────────────────────────────
# 连接管理
# ─────────────────────────────────────────────────────────────────────

class _ConnectionManager:
    """管理当前活跃的 WebSocket 连接（同一时刻只有 Electron 一个客户端）。"""

    def __init__(self) -> None:
        self._ws: WebSocket | None = None
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        async with self._lock:
            if self._ws is not None:
                # 旧连接仍在：关闭它（Electron 重启时会重连）
                try:
                    await self._ws.close(code=1001)
                except Exception:
                    pass
            self._ws = ws

    def disconnect(self) -> None:
        self._ws = None

    async def push(self, channel: str, payload: Any) -> None:
        """推送一条消息；若无连接则静默丢弃。"""
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send_text(json.dumps({"channel": channel, "payload": payload},
                                          ensure_ascii=False))
        except Exception as exc:
            logger.warning("ws_push_failed", channel=channel, error=str(exc))
            self._ws = None


manager = _ConnectionManager()


async def push(channel: str, payload: Any) -> None:
    """模块级推送入口，供其他 service 调用。"""
    await manager.push(channel, payload)


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
    await manager.connect(websocket)
    logger.info("ws_connected", client=str(websocket.client))

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        logger.info("ws_disconnected")
    except Exception as exc:
        logger.warning("ws_error", error=str(exc))
    finally:
        manager.disconnect()
