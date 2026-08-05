"""
WebSocket 鉴权中间件。

Python 进程启动时生成一次性 token，由 supervisor.ts 通过环境变量注入。
所有 WS 连接和 HTTP 请求都必须携带 Authorization: Bearer <token>，
其他本地进程无法伪造（仅凭 127.0.0.1 绑定不够）。
"""

from __future__ import annotations

import os
import secrets

from fastapi import HTTPException, WebSocket, status

# 从环境变量读取（supervisor 在拉起前设好）
_TOKEN: str = os.environ.get("YUELI_TOKEN", "")


def get_token() -> str:
    return _TOKEN


def verify_token(token: str) -> bool:
    """使用恒定时间比较，防止计时侧信道。"""
    if not _TOKEN:
        return False
    return secrets.compare_digest(token, _TOKEN)


def extract_bearer(authorization: str | None) -> str:
    if not authorization:
        return ""
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1]


def require_token(authorization: str | None) -> None:
    """HTTP 路由依赖项：token 错误直接 401。"""
    token = extract_bearer(authorization)
    if not verify_token(token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


async def ws_auth(websocket: WebSocket) -> bool:
    """
    WebSocket 握手期鉴权。

    客户端在握手阶段通过 Sec-WebSocket-Protocol 子协议字段传 token，
    格式：`yueli-<token>`。
    原因：浏览器 WebSocket API 不支持自定义 header，
    虽然 Electron 的 ws 库可以，但统一用子协议字段更规范。
    """
    subprotocols = websocket.headers.get("sec-websocket-protocol", "")
    for proto in subprotocols.split(","):
        proto = proto.strip()
        if proto.startswith("yueli-"):
            candidate = proto[len("yueli-"):]
            if verify_token(candidate):
                return True
    # 也接受 Authorization header（Electron WS 客户端可以发）
    auth_header = websocket.headers.get("authorization", "")
    return verify_token(extract_bearer(auth_header))
