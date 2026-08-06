"""适配器与主体 Python 后端之间的 HTTP/WS 客户端。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Mapping

import json

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

import httpx

from src.common.logger import get_logger

from .events import QqInboundEvent


logger = get_logger(__name__)


class BackendDisconnected(ConnectionError):
    """主体出站 WebSocket 断开。"""


@dataclass(frozen=True)
class BackendOutbound:
    """主体发给 QQ 适配器的一条整轮私聊回复。"""

    stream_id: int
    stream_kind: str
    stream_external_id: str
    segments: List[str]


class BackendClient:
    """向主体提交入站消息，并顺序读取 qq.send 出站消息。"""

    def __init__(self, port: int, token: str, http_timeout_sec: float = 30.0) -> None:
        if port <= 0 or port > 65535:
            raise ValueError('主体 backend 端口必须在 1 到 65535 之间')
        if not token.strip():
            raise ValueError('主体 backend token 不能为空')
        self._base_url = f'http://127.0.0.1:{port}'
        self._ws_url = f'ws://127.0.0.1:{port}/ws?client=napcat'
        self._token = token
        self._http_timeout_sec = http_timeout_sec
        self._http: httpx.AsyncClient | None = None
        self._ws: ClientConnection | None = None

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._ws.state is State.OPEN

    async def connect(self) -> None:
        await self.close()
        headers = {'Authorization': f'Bearer {self._token}'}
        self._http = httpx.AsyncClient(
            timeout=self._http_timeout_sec,
            headers=headers,
            base_url=self._base_url,
        )
        try:
            self._ws = await connect(self._ws_url, additional_headers=headers)
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        websocket = self._ws
        self._ws = None
        if websocket is not None:
            await websocket.close()
        client = self._http
        self._http = None
        if client is not None:
            await client.aclose()

    async def submit_inbound(self, event: QqInboundEvent) -> Dict[str, Any]:
        """严格按主体 body alias 提交一条 QQ 入站消息。"""
        client = self._http
        if client is None:
            raise BackendDisconnected('主体 HTTP 尚未连接')
        response = await client.post(
            '/platform/inbound',
            json={
                'platform': 'qq',
                'streamKind': event.stream_kind,
                'streamExternalId': event.stream_external_id,
                'senderExternalId': event.sender_external_id,
                'senderName': event.sender_name,
                'text': event.text,
                'mentionedMe': event.mentioned_me,
                'externalMessageId': event.external_message_id,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError('主体 /platform/inbound 响应必须是 JSON 对象')
        return payload

    async def next_outbound(self) -> BackendOutbound:
        """读取主体 WS，忽略不属于适配器的通道。"""
        websocket = self._ws
        if websocket is None:
            raise BackendDisconnected('主体出站 WebSocket 尚未连接')
        while True:
            try:
                raw = await websocket.recv()
            except ConnectionClosed as exc:
                raise BackendDisconnected(f'主体出站 WebSocket 断开：{exc}') from exc
            if raw is None:
                raise BackendDisconnected('主体出站 WebSocket 已关闭')
            payload = _decode_payload(raw)
            if payload.get('channel') != 'qq.send':
                logger.debug('忽略主体出站通道', channel=payload.get('channel'))
                continue
            return _parse_outbound(payload)

    async def iter_outbound(self) -> AsyncIterator[BackendOutbound]:
        while True:
            yield await self.next_outbound()


def _decode_payload(raw: str | bytes) -> Dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8')
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError('主体 WS 报文顶层必须是 JSON 对象')
    return payload


def _parse_outbound(payload: Mapping[str, Any]) -> BackendOutbound:
    stream_id = payload.get('stream_id')
    if not isinstance(stream_id, int) or isinstance(stream_id, bool) or stream_id <= 0:
        raise ValueError('主体 qq.send 缺少合法 stream_id')
    body = payload.get('payload')
    if not isinstance(body, Mapping):
        raise ValueError('主体 qq.send 缺少对象类型的 payload')
    stream_kind = body.get('streamKind')
    if not isinstance(stream_kind, str) or not stream_kind.strip():
        raise ValueError('主体 qq.send 缺少 streamKind')
    stream_external_id = body.get('streamExternalId')
    if not isinstance(stream_external_id, str) or not stream_external_id.strip():
        raise ValueError('主体 qq.send 缺少 streamExternalId')
    raw_segments = body.get('segments')
    if not isinstance(raw_segments, list) or not all(isinstance(item, str) for item in raw_segments):
        raise ValueError('主体 qq.send 的 segments 必须是字符串数组')
    segments = [item.strip() for item in raw_segments]
    if not all(segments):
        raise ValueError('主体 qq.send 的 segments 不能包含空字符串')
    return BackendOutbound(
        stream_id=stream_id,
        stream_kind=stream_kind.strip(),
        stream_external_id=stream_external_id.strip(),
        segments=segments,
    )
