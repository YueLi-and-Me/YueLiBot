"""连接协议端正向 WebSocket 的传输层。"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Mapping
from uuid import uuid4

import asyncio
import json

from websockets.asyncio.client import ClientConnection, connect
from websockets.protocol import State

from src.common.logger import get_logger

from .config import NapcatConnectionConfig
from .events import is_action_response


logger = get_logger(__name__)


class TransportDisconnected(ConnectionError):
    """协议端连接已经断开；所有挂起 action 都应收到这个异常。"""


class ActionError(RuntimeError):
    """协议端明确返回非 ok 状态。"""

    def __init__(self, action: str, response: Mapping[str, Any]) -> None:
        self.action = action
        self.response = dict(response)
        super().__init__(
            f'action {action} 失败：status={response.get("status")!r} '
            f'retcode={response.get("retcode")!r}'
        )


_DISCONNECTED = object()


class NapcatTransport:
    """协议端 WS client；一条 reader 协程负责所有入站报文。"""

    def __init__(self, config: NapcatConnectionConfig) -> None:
        self._config = config
        self._ws: ClientConnection | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._pending: Dict[str, asyncio.Future[Dict[str, Any]]] = {}
        self._event_queue: asyncio.Queue[object] = asyncio.Queue()
        self._disconnect_error: TransportDisconnected | None = None
        self._self_id: str | None = None

    @property
    def uri(self) -> str:
        return f'ws://{self._config.host}:{self._config.port}'

    @property
    def self_id(self) -> str:
        if self._self_id is None:
            raise RuntimeError('协议端登录信息尚未获取')
        return self._self_id

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._ws.state is State.OPEN

    async def connect(self) -> str:
        """连接协议端并先取登录 QQ 号；连接失败由上层决定是否重试。"""
        await self.close()
        self._event_queue = asyncio.Queue()
        self._disconnect_error = None
        headers = None
        if self._config.token:
            headers = {'Authorization': f'Bearer {self._config.token}'}
        websocket = await connect(
            self.uri,
            additional_headers=headers,
        )
        self._ws = websocket
        self._reader_task = asyncio.create_task(self._read_loop(websocket))
        try:
            response = await self.call_action('get_login_info')
            data = response.get('data')
            if not isinstance(data, Mapping):
                raise ValueError('get_login_info 响应缺少对象类型的 data')
            self._self_id = _required_identifier(data.get('user_id', data.get('self_id')), '登录 QQ 号')
            return self._self_id
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        websocket = self._ws
        reader = self._reader_task
        self._ws = None
        self._reader_task = None
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
        """登记 Future、串行发送、等待响应，并在 finally 清理 pending。"""
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
        """按协议到达顺序取一条事件；断线通过异常唤醒消费者。"""
        item = await self._event_queue.get()
        if item is _DISCONNECTED:
            error = self._disconnect_error or TransportDisconnected('协议端连接已断开')
            raise error
        if not isinstance(item, dict):
            raise TypeError('协议事件必须是 JSON 对象')
        return item

    async def iter_events(self) -> AsyncIterator[Dict[str, Any]]:
        """以单消费者方式顺序遍历协议事件。"""
        while True:
            yield await self.next_event()

    async def _read_loop(self, websocket: ClientConnection) -> None:
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
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)


def _decode_payload(raw: str | bytes) -> Dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8')
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError('协议报文顶层必须是 JSON 对象')
    return payload


def _required_identifier(value: Any, label: str) -> str:
    if value is None:
        raise ValueError(f'{label} 不能为空')
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f'{label} 不能为空')
    return normalized
