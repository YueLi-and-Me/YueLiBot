"""提供 HTTP 和 WebSocket 共用的鉴权原语。

Python 进程启动时生成一次性 token，经显式 TokenManager 注入认证层。
Electron 与平台适配器通过 Authorization: Bearer <token> 认证；浏览器登录
成功后只持有 HttpOnly Cookie。其他本地进程无法伪造（仅凭 127.0.0.1
绑定不够）。

`TokenManager` 只保存当前进程 token；HTTP 使用 Bearer 或 HttpOnly Cookie，
WebSocket 额外支持握手子协议中的 `yueli-<token>`。
"""

from __future__ import annotations

from fastapi import HTTPException, WebSocket, status

import secrets

SESSION_COOKIE_NAME = 'yueli_session'


class TokenManager:
    """持有当前 Python 后端进程唯一的认证 token。

    实例不负责生成、持久化或轮换 token；启动流程通过 :meth:`configure` 注入一次，
    之后所有校验都使用恒定时间比较。
    """

    def __init__(self) -> None:
        """创建尚未配置 token 的管理器。

        :return: 无返回值。
        副作用：把内部 token 初始化为空字符串，不执行 I/O。
        """
        self._token = ''

    def configure(self, token: str) -> None:
        """设置当前进程使用的认证 token。

        :param token: 非空认证 token；默认值不适用。
        :raises ValueError: `token` 为空字符串。
        副作用：覆盖当前保存的 token；调用后旧 token 立即失效。
        """
        if not token:
            raise ValueError('后端认证 token 不能为空')
        self._token = token

    def get(self) -> str:
        """返回已配置的认证 token。

        :return: 当前进程 token。
        :raises RuntimeError: 尚未调用 :meth:`configure` 或 token 为空。
        副作用：不执行 I/O。
        """
        if not self._token:
            raise RuntimeError('后端认证 token 尚未初始化')
        return self._token

    def verify(self, token: str) -> bool:
        """使用恒定时间比较判断 token 是否匹配当前 token。

        :param token: 待校验的候选 token。
        :return: 管理器已配置且候选值匹配时返回 `True`，否则返回 `False`。
        副作用：不修改 token 或管理器状态。
        """
        if not self._token:
            return False
        return secrets.compare_digest(token, self._token)


token_manager = TokenManager()


def get_token() -> str:
    """读取模块级 token 管理器中的当前认证 token。

    :return: 当前进程认证 token。
    :raises RuntimeError: token 尚未初始化。
    副作用：不执行 I/O。
    """
    return token_manager.get()


def verify_token(token: str) -> bool:
    """使用恒定时间比较校验候选认证 token。

    :param token: 待校验的候选 token；未配置当前 token 时校验必定失败。

    :return: 候选 token 与当前进程 token 完全匹配时返回 ``True``，否则返回 ``False``。

    副作用：
        仅读取进程内 token；不修改管理器状态，不执行网络或持久化操作。
    """
    return token_manager.verify(token)


def extract_bearer(authorization: str | None) -> str:
    """从 Authorization 头提取 Bearer token。

    :param authorization: 原始 Authorization 头，可为 `None`。
    :return: 头部格式为 `Bearer <token>` 时返回 `<token>`，其他情况返回空字符串。
    副作用：不执行鉴权比较，也不修改输入。
    """
    if not authorization:
        return ""
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1]


def require_token(authorization: str | None, session_token: str | None = None) -> None:
    """校验 HTTP Bearer 头或 HttpOnly 会话 Cookie，并在失败时返回 401。

    :param authorization: 可选 ``Authorization`` 请求头，支持 ``Bearer <token>`` 格式。
    :param session_token: 可选会话 Cookie；默认值为 ``None``。

    :raises fastapi.HTTPException: Bearer token 和会话 Cookie 均未通过恒定时间校验时，
            抛出状态码为 401 的异常。

    副作用：
        仅读取请求凭据；不创建、轮换或持久化会话。
    """
    bearer_token = extract_bearer(authorization)
    if not verify_token(bearer_token) and not verify_token(session_token or ''):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="认证失败")


async def ws_auth(websocket: WebSocket) -> bool:
    """在 WebSocket 握手阶段校验子协议、Authorization 头或会话 Cookie。

    客户端可以通过 ``Sec-WebSocket-Protocol`` 传递 ``yueli-<token>``，也可以
    使用 Authorization 头或 ``yueli_session`` Cookie。浏览器 WebSocket API
    不支持自定义请求头，因此子协议是浏览器端的兼容认证路径。

    :param websocket: FastAPI 注入的 WebSocket 对象。

    :return: 任一凭据通过 token 校验时返回 ``True``，否则返回 ``False``。

    副作用：
        仅读取握手头、Cookie 和子协议，不接受连接、不发送关闭帧。
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
