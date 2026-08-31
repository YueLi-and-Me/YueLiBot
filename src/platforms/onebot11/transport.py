"""实现连接协议端正向 WebSocket 的传输层。

`NapcatTransport` 用一个 reader 协程统一接收 action 响应和业务事件，通过 echo
把响应分发给挂起的 Future，并用异步队列按到达顺序暴露业务事件；连接断开时，
所有挂起调用和事件消费者都会收到明确异常。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Mapping
from uuid import uuid4

import asyncio
import json

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidStatus
from websockets.protocol import State

from src.core.common.logger import get_logger

from .config import NapcatConnectionConfig
from .events import is_action_response


logger = get_logger(__name__)


class TransportDisconnected(ConnectionError):
    """协议端连接已经断开；所有挂起 action 都应收到这个异常。"""


class ActionError(RuntimeError):
    """协议端明确返回非 ok 状态。"""

    def __init__(self, action: str, response: Mapping[str, Any]) -> None:
        """保存失败 action 及协议端原始响应并构造可读错误信息。

        :param action: 已发送但被协议端拒绝的 action 名称。
        :param response: 协议端返回的完整响应映射。
        :return: 无返回值；实例的 `response` 是输入映射的浅复制。
        副作用：初始化异常属性，不执行网络 I/O。
        """
        self.action = action
        self.response = dict(response)
        # 协议端在失败响应里用 message / wording 说明原因，wording 是面向人的
        # 中文措辞。只打 status 与 retcode 会把唯一可归因的信息丢掉——现场只剩
        # 一个裸错误码，既判不出是参数问题还是业务拒绝，也无法复现。
        # 两个字段内容常有重合，去重后按出现顺序拼接。
        reasons: list[str] = []
        for key in ('message', 'wording'):
            text = str(response.get(key, '')).strip()
            if text and text not in reasons:
                reasons.append(text)
        detail = f'，原因：{" / ".join(reasons)}' if reasons else ''
        super().__init__(
            f'action {action} 失败：status={response.get("status")!r} '
            f'retcode={response.get("retcode")!r}{detail}'
        )


class ProtocolHandshakeError(ConnectionError):
    """协议端拒绝 WebSocket 握手，连接地址或协议端配置需要检查。"""

    def __init__(self, status_code: int) -> None:
        """记录协议端拒绝 WebSocket 握手时返回的 HTTP 状态码。

        :param status_code: 协议端返回的 HTTP 状态码。
        :return: 无返回值。
        副作用：设置 `status_code` 属性并初始化异常消息。
        """
        self.status_code = status_code
        super().__init__(f'协议端拒绝 WebSocket 握手：HTTP {status_code}')


class ProtocolAuthenticationError(ProtocolHandshakeError):
    """协议端明确拒绝访问令牌，重试不会改变鉴权结果。"""


_DISCONNECTED = object()


class NapcatTransport:
    """协议端 WebSocket 客户端；一条 reader 协程负责所有入站报文。

    发送 action 通过锁串行化，接收由 `_read_loop` 独占，从而避免多个协程直接
    读取同一个 WebSocket 导致响应错配。
    """

    def __init__(self, config: NapcatConnectionConfig) -> None:
        """创建尚未连接的协议传输层。

        :param config: 已完成字段校验的协议端连接配置。
        :raises TypeError: 配置对象不是兼容的 `NapcatConnectionConfig` 实例时，
            后续属性访问会暴露类型错误。
        副作用：初始化连接状态、发送锁、响应 Future 表和事件队列；不执行网络 I/O。
        """
        self._config = config
        self._ws: ClientConnection | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._pending: Dict[str, asyncio.Future[Dict[str, Any]]] = {}
        self._event_queue: asyncio.Queue[object] = asyncio.Queue()
        self._disconnect_error: TransportDisconnected | None = None
        self._self_id: str | None = None
        self._self_name: str | None = None

    @property
    def uri(self) -> str:
        """返回按当前主机和端口拼出的 WebSocket URI。

        :return: 形如 `ws://host:port` 的连接地址。
        副作用：不执行网络 I/O。
        """
        return f'ws://{self._config.host}:{self._config.port}'

    @property
    def self_id(self) -> str:
        """返回最近一次握手得到的登录 QQ 号。

        :return: 协议端登录 QQ 号。
        :raises RuntimeError: 尚未成功连接并完成 `get_login_info` 时抛出。
        副作用：不执行 I/O。
        """
        if self._self_id is None:
            raise RuntimeError('协议端登录信息尚未获取')
        return self._self_id

    @property
    def self_name(self) -> str:
        """返回最近一次握手得到的登录昵称。

        :return: 协议端登录昵称。
        :raises RuntimeError: 尚未成功连接并完成 `get_login_info` 时抛出。
        副作用：不执行 I/O。
        """
        if self._self_name is None:
            raise RuntimeError('协议端登录昵称尚未获取')
        return self._self_name

    @property
    def pending_count(self) -> int:
        """返回当前等待协议端响应的 action 数量。

        :return: 已登记但尚未完成的 action Future 数量。
        副作用：不执行 I/O。
        """
        return len(self._pending)

    @property
    def connected(self) -> bool:
        """返回 WebSocket 是否已建立且仍处于开放状态。

        :return: 连接存在并且状态为 `OPEN` 时返回 `True`，否则返回 `False`。
        副作用：不执行 I/O。
        """
        return self._ws is not None and self._ws.state is State.OPEN

    async def connect(self) -> str:
        """建立协议端 WebSocket 连接，并读取登录 QQ 号和昵称。

        :return: 协议端实际登录的机器人 QQ 号。

        :raises ProtocolAuthenticationError: 协议端以 HTTP 401 或 403 拒绝鉴权。
        :raises ProtocolHandshakeError: 协议端以其他非成功状态拒绝 WebSocket 握手。
        :raises ValueError: 登录信息响应缺少合法 QQ 号或昵称。
        :raises Exception: WebSocket 建立、登录信息 action 或响应解析失败时传播原始异常。

        副作用：
            关闭旧连接，创建 WebSocket 和 reader 任务，并更新登录身份；失败时清理已创建资源。
        """
        await self.close()
        self._event_queue = asyncio.Queue()
        self._disconnect_error = None
        headers = None
        if self._config.token:
            headers = {'Authorization': f'Bearer {self._config.token}'}
        try:
            websocket = await connect(
                self.uri,
                additional_headers=headers,
            )
        except InvalidStatus as exc:
            status_code = exc.response.status_code
            error_type = (
                ProtocolAuthenticationError
                if status_code in (401, 403)
                else ProtocolHandshakeError
            )
            raise error_type(status_code) from exc
        self._ws = websocket
        self._reader_task = asyncio.create_task(self._read_loop(websocket))
        try:
            response = await self.call_action('get_login_info')
            data = response.get('data')
            if not isinstance(data, Mapping):
                raise ValueError('get_login_info 响应缺少对象类型的 data')
            self._self_id = _required_identifier(data.get('user_id', data.get('self_id')), '登录 QQ 号')
            self._self_name = _required_identifier(data.get('nickname'), '登录 QQ 昵称')
            return self._self_id
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        """关闭 WebSocket、reader 任务，并唤醒所有挂起调用。

        副作用：清空登录信息和连接引用，取消 reader 任务，把所有 pending
            Future 置为 `TransportDisconnected` 异常；重复调用安全。
        :raises Exception: 底层 WebSocket 关闭失败时可能抛出原始异常。
        """
        websocket = self._ws
        reader = self._reader_task
        self._ws = None
        self._reader_task = None
        self._self_id = None
        self._self_name = None
        if websocket is not None:
            await websocket.close()
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        self._fail_pending(TransportDisconnected('协议端连接已关闭'))

    async def call_action(
        self,
        action: str,
        params: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """串行发送一个协议 action，等待匹配 echo 的响应并校验成功状态。

        :param action: 协议端 action 名称，必须为非空字符串。
        :param params: 可选 action 参数映射；发送前复制为普通字典，默认使用空对象。

        :return: 协议端返回的 JSON 对象。

        :raises TransportDisconnected: 当前 WebSocket 未连接或连接在等待期间断开。
        :raises ActionError: 协议端返回非 ``ok`` 状态。
        :raises asyncio.TimeoutError: 等待响应超过配置的 action 超时时间。
        :raises ValueError: action 响应结构不符合协议时由响应处理逻辑抛出。
        :raises TypeError: action 或参数映射无法序列化时抛出。

        副作用：
            登记并最终移除一个 pending Future；通过发送锁串行写入 WebSocket。
        """
        websocket = self._ws
        if websocket is None or websocket.state is not State.OPEN:
            raise TransportDisconnected('协议端尚未连接')
        echo = uuid4().hex
        future: asyncio.Future[Dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        request = {
            'action': action,
            'params': dict(params or {}),
            'echo': echo,
        }
        try:
            async with self._send_lock:
                await websocket.send(json.dumps(request, ensure_ascii=False))
            response = await asyncio.wait_for(
                future,
                timeout=self._config.action_timeout_sec,
            )
            if str(response.get('status', '')).lower() != 'ok':
                raise ActionError(action, response)
            return response
        finally:
            self._pending.pop(echo, None)

    async def next_event(self) -> Dict[str, Any]:
        """按协议到达顺序读取下一条业务事件。

        :return: 顶层为字典的协议事件对象。

        :raises TransportDisconnected: reader 检测到连接断开或显式关闭了传输层。
        :raises TypeError: 队列中的事件不是 JSON 对象。

        副作用：
            消费事件队列中的一项；断线哨兵不会被重新放回队列。
        """
        item = await self._event_queue.get()
        if item is _DISCONNECTED:
            error = self._disconnect_error or TransportDisconnected('协议端连接已断开')
            raise error
        if not isinstance(item, dict):
            raise TypeError('协议事件必须是 JSON 对象')
        return item

    async def iter_events(self) -> AsyncIterator[Dict[str, Any]]:
        """以单消费者方式持续顺序产生协议业务事件。

        :yield: 按 WebSocket 到达顺序排列的协议 JSON 对象。

        :raises TransportDisconnected: 连接尚未建立或在迭代期间断开。
        :raises TypeError: 收到的队列项不是 JSON 对象。

        副作用：
            持续消费事件队列；生成器结束时不自动关闭 WebSocket。
        """
        while True:
            yield await self.next_event()

    async def _read_loop(self, websocket: ClientConnection) -> None:
        """独占读取 WebSocket 并分发响应或业务事件。

        :param websocket: 当前连接对应的 WebSocket 实例。
        :return: 连接结束、任务取消或读取异常后返回。
        :raises asyncio.CancelledError: 调用方取消 reader 任务时原样抛出。
        副作用：完成匹配 echo 的 Future，把业务事件放入队列；异常或断开时
            设置断开原因、失败所有 pending Future，并投递断开哨兵。
        """
        try:
            async for raw in websocket:
                payload = _decode_payload(raw)
                if is_action_response(payload):
                    echo = str(payload['echo']).strip()
                    future = self._pending.get(echo)
                    if future is not None and not future.done():
                        future.set_result(payload)
                    else:
                        logger.warning('未知 action echo', echo=echo)
                    continue
                await self._event_queue.put(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._disconnect_error = TransportDisconnected(f'协议端 WebSocket 断开：{exc}')
        finally:
            if self._ws is websocket:
                self._ws = None
            self._fail_pending(self._disconnect_error or TransportDisconnected('协议端连接已断开'))
            await self._event_queue.put(_DISCONNECTED)

    def _fail_pending(self, error: Exception) -> None:
        """将所有尚未完成的 action Future 置为异常完成。

        :param error: 交给每个挂起 Future 的异常实例。
        :return: `None`。
        副作用：修改所有未完成 Future 的状态；已完成 Future 保持不变。
        :performance: 按当前 pending 数量线性遍历。
        """
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)


def _decode_payload(raw: str | bytes) -> Dict[str, Any]:
    """把协议端文本或 UTF-8 字节报文解析为 JSON 对象。

    :param raw: WebSocket 接收到的文本或字节内容。
    :return: 顶层为字典的 JSON 数据。
    :raises UnicodeDecodeError: 字节报文不是合法 UTF-8。
    :raises json.JSONDecodeError: 报文不是合法 JSON。
    :raises ValueError: JSON 顶层不是对象。
    副作用：不修改连接状态。
    """
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8')
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError('协议报文顶层必须是 JSON 对象')
    return payload


def _required_identifier(value: Any, label: str) -> str:
    """将协议标识转换为非空字符串。

    :param value: 原始标识值，可以是数字或字符串。
    :param label: 错误信息中使用的字段名称。
    :return: 去除首尾空白后的字符串。
    :raises ValueError: 值为 `None` 或规范化后为空。
    副作用：不执行 I/O。
    """
    if value is None:
        raise ValueError(f'{label} 不能为空')
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f'{label} 不能为空')
    return normalized
