"""
WebSocket 鉴权中间件。

Python 进程启动时生成一次性 token，经显式 TokenManager 注入认证层。
Electron 与平台适配器通过 Authorization: Bearer <token> 认证；浏览器登录
成功后只持有 HttpOnly Cookie。其他本地进程无法伪造（仅凭 127.0.0.1
绑定不够）。
"""

from __future__ import annotations

from fastapi import HTTPException, WebSocket, status

import secrets

SESSION_COOKIE_NAME = 'yueli_session'


class TokenManager:
    """持有当前 Python 后端进程唯一的认证 token。"""

    def __init__(self) -> None:
        self._token = ''

    def configure(self, token: str) -> None:
        if not token:
            raise ValueError('后端认证 token 不能为空')
        self._token = token

    def get(self) -> str:
        if not self._token:
            raise RuntimeError('后端认证 token 尚未初始化')
        return self._token

    def verify(self, token: str) -> bool:
        if not self._token:
            return False
        return secrets.compare_digest(token, self._token)


token_manager = TokenManager()


def get_token() -> str:
    return token_manager.get()


def verify_token(token: str) -> bool:
    """使用恒定时间比较，防止计时侧信道。"""
    return token_manager.verify(token)


def extract_bearer(authorization: str | None) -> str:
    if not authorization:
        return ""
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1]


def require_token(authorization: str | None, session_token: str | None = None) -> None:
    """HTTP 路由依赖项：Bearer 与 HttpOnly Cookie 均走同一恒定时间校验。"""
    bearer_token = extract_bearer(authorization)
    if not verify_token(bearer_token) and not verify_token(session_token or ''):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="认证失败")


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
    if verify_token(extract_bearer(auth_header)):
        return True
    return verify_token(websocket.cookies.get(SESSION_COOKIE_NAME, ''))
