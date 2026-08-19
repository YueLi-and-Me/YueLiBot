"""提供 QQ 适配器与主体 Python 后端之间的双通道通信。

本模块使用 HTTP 提交解析后的入站事件，并使用主体提供的 WebSocket 顺序接收
`qq.send` 出站消息；`BackendClient` 负责连接生命周期，`BackendOutbound` 和
解析函数负责把外部 JSON 转成适配器内部使用的严格结构。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Literal, Mapping

import json

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

import httpx

from src.core.common.logger import get_logger

from .events import QqInboundEvent


logger = get_logger(__name__)


class BackendDisconnected(ConnectionError):
    """主体出站 WebSocket 断开。"""


@dataclass(frozen=True)
class BackendOutbound:
    """主体发给 QQ 适配器的一条文本和/或表情包回复。"""

    stream_id: int
    stream_kind: Literal['direct', 'group']
    stream_external_id: str
    segments: List[str]
    emoji_refs: tuple[str, ...] = ()
    emoji_sub_types: tuple[int, ...] = ()


class BackendClient:
    """向主体提交入站消息，并顺序读取 `qq.send` 出站消息。

    实例同时持有 HTTP 客户端和 WebSocket 连接；调用方应在使用完毕后调用
    :meth:`close`，否则底层连接和挂起的网络资源可能无法及时释放。
    """

    def __init__(self, port: int, token: str, http_timeout_sec: float = 30.0) -> None:
        """创建尚未连接到主体后端的客户端。

        :param port: 主体 HTTP/WS 服务端口，取值范围为 1 到 65535。
        :param token: 主体 API 使用的非空 Bearer token。
        :param http_timeout_sec: HTTP 请求超时时间，单位为秒，默认值为 30.0；
            非正值由底层 HTTP 客户端拒绝。
        :raises ValueError: `port` 超出合法端口范围，或 `token` 为空白字符串。
        副作用：只保存连接参数，不会在构造阶段创建网络连接。
        """
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
        """返回主体 WebSocket 当前是否处于开放状态。

        :return: WebSocket 已创建且状态为 `OPEN` 时返回 `True`，否则返回 `False`。
        副作用：不执行 I/O。
        """
        return self._ws is not None and self._ws.state is State.OPEN

    async def connect(self) -> None:
        """重建主体 HTTP 和 WebSocket 连接。

        :raises Exception: WebSocket 握手或 HTTP 客户端创建失败时重新抛出原始异常；
            失败前已创建的资源会先关闭。
        副作用：关闭旧连接，创建带 Bearer 鉴权头的 HTTP 客户端和 WebSocket
            连接；成功后 `connected` 返回 `True`。
        :performance: 每次调用都会关闭并重新建立连接，不应在单条消息级别频繁调用。
        """
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
        """幂等关闭主体 WebSocket 和 HTTP 客户端。

        副作用：释放网络连接并清空内部连接引用；重复调用不会产生额外错误。
        :raises Exception: 底层 WebSocket 或 HTTP 客户端关闭操作失败时向调用方暴露该错误。
        """
        websocket = self._ws
        self._ws = None
        if websocket is not None:
            await websocket.close()
        client = self._http
        self._http = None
        if client is not None:
            await client.aclose()

    async def submit_inbound(self, event: QqInboundEvent) -> Dict[str, Any]:
        """按主体入站协议提交一条已规范化的 QQ 消息。

        :param event: 由事件解析器生成的 QQ 入站事件，字段必须满足主体接口协议。

        :return: 主体 ``/platform/inbound`` 返回的 JSON 对象。

        :raises BackendDisconnected: HTTP 客户端尚未建立连接。
        :raises httpx.HTTPError: 请求失败或主体返回非成功 HTTP 状态码。
        :raises ValueError: 主体响应顶层不是 JSON 对象。

        副作用：
            向主体服务提交一条入站消息并等待响应；普通图片只发送
            ``imageSources`` 来源引用，不在适配器下载或编码图片字节。
        """
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
                'senderNickname': event.sender_nickname,
                'senderGroupCard': event.sender_group_card,
                'botName': event.bot_name,
                'text': event.text,
                'mentionedMe': event.mentioned_me,
                'externalMessageId': event.external_message_id,
                'imageSources': list(event.image_sources),
                'emojiSources': list(event.emoji_sources),
                'emojiSubTypes': list(event.emoji_sub_types),
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError('主体 /platform/inbound 响应必须是 JSON 对象')
        return payload

    async def submit_typing(self, sender_external_id: str) -> None:
        """把私聊输入状态通知提交给主体。

        协议端在对方打字期间会反复推送，主体自行决定是否据此开口，因此这里不做
        节流也不关心响应内容；提交失败只由调用方记录日志，绝不能影响消息通路。

        :param sender_external_id: 正在输入的对方 QQ 号。
        :raises BackendDisconnected: HTTP 客户端尚未建立连接。
        :raises httpx.HTTPError: 请求失败或主体返回非成功 HTTP 状态码。
        副作用：向主体输入状态接口发送一次 POST 请求。
        """
        client = self._http
        if client is None:
            raise BackendDisconnected('主体 HTTP 尚未连接')
        response = await client.post(
            '/platform/typing',
            json={
                'platform': 'qq',
                'streamKind': 'direct',
                'streamExternalId': sender_external_id,
                'senderExternalId': sender_external_id,
            },
        )
        response.raise_for_status()

    async def submit_group_backfill(
        self,
        group_id: str,
        messages: List[Dict[str, Any]],
    ) -> None:
        """把停机期间错过的群历史提交给主体只观察落库。

        :param group_id: 目标群外部 ID。
        :param messages: 按时间升序排列的历史消息字典列表。
        :raises BackendDisconnected: HTTP 客户端尚未建立连接。
        :raises httpx.HTTPError: 请求失败或主体返回非成功 HTTP 状态码。
        副作用：向主体回填接口发送一次 POST 请求。
        """
        client = self._http
        if client is None:
            raise BackendDisconnected('主体 HTTP 尚未连接')
        response = await client.post(
            '/platform/group/backfill',
            json={
                'platform': 'qq',
                'streamExternalId': group_id,
                'messages': messages,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError('主体 /platform/group/backfill 响应必须是 JSON 对象')

    async def link_owner_identity(self, owner_qq: str) -> None:
        """在启动消息消费者前，将配置中的 owner QQ 号绑定到主体 owner 身份。

        :param owner_qq: owner 的 QQ 号；允许输入首尾空白，但规范化后必须为数字字符串。

        :raises BackendDisconnected: HTTP 客户端尚未建立连接。
        :raises ValueError: QQ 号为空、包含非数字字符，或主体未确认绑定成功。
        :raises httpx.HTTPError: 请求失败或主体返回非成功 HTTP 状态码。

        副作用：
            向主体身份绑定接口发送一次 POST 请求；不修改配置对象。
        """
        client = self._http
        if client is None:
            raise BackendDisconnected('主体 HTTP 尚未连接')
        normalized = owner_qq.strip()
        if not normalized.isdigit():
            raise ValueError(f'owner QQ 号必须是数字：{owner_qq!r}')
        response = await client.post(
            '/platform/identity/link',
            json={
                'platform': 'qq',
                'externalId': normalized,
                'displayName': normalized,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get('ok') is not True:
            raise ValueError('主体 owner identity 绑定未确认成功')

    async def next_outbound(self) -> BackendOutbound:
        """从主体出站 WebSocket 读取下一条 QQ 通道消息。

        :return: 下一条通过协议校验的 ``BackendOutbound`` 消息。

        :raises BackendDisconnected: WebSocket 尚未连接、连接关闭或读取过程中断开。
        :raises ValueError: 收到的报文不是合法 JSON 对象或不符合 ``qq.send`` 协议。
        :raises json.JSONDecodeError: WebSocket 文本不是合法 JSON。

        副作用：
            持续消费 WebSocket 帧；非 ``qq.send`` 通道消息被记录后丢弃。
        """
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
        """持续按主体 WebSocket 到达顺序产生适配器出站消息。

        :return: 每次迭代返回一条已校验的 :class:`BackendOutbound`。
        :raises BackendDisconnected: 尚未连接或主体连接中途断开。
        :raises ValueError: 主体报文不是符合协议的 JSON 对象。
        副作用：持续消费 WebSocket；生成器取消时由底层异步迭代器结束。
        """
        while True:
            yield await self.next_outbound()


def _decode_payload(raw: str | bytes) -> Dict[str, Any]:
    """把主体 WebSocket 的文本或 UTF-8 字节报文解析为 JSON 对象。

    :param raw: WebSocket 返回的文本或字节报文。
    :return: 顶层为对象的 JSON 映射。
    :raises UnicodeDecodeError: 字节报文不是合法 UTF-8。
    :raises json.JSONDecodeError: 报文不是合法 JSON。
    :raises ValueError: JSON 顶层不是对象。
    副作用：不修改输入或客户端状态。
    """
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8')
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError('主体 WS 报文顶层必须是 JSON 对象')
    return payload


def _parse_outbound(payload: Mapping[str, Any]) -> BackendOutbound:
    """校验并转换主体 `qq.send` 报文。

    :param payload: 已解析的主体出站报文，必须包含正整数 `stream_id` 和对象型
        `payload`，其内部必须包含合法流类型、流 ID，以及至少一种文本或表情包产物。
    :return: 去除段首尾空白后的 :class:`BackendOutbound`。
    :raises ValueError: 缺少字段、字段类型错误、流类型不支持或段内容为空。
    副作用：不执行 I/O，也不修改传入映射。
    """
    stream_id = payload.get('stream_id')
    if not isinstance(stream_id, int) or isinstance(stream_id, bool) or stream_id <= 0:
        raise ValueError('主体 qq.send 缺少合法 stream_id')
    body = payload.get('payload')
    if not isinstance(body, Mapping):
        raise ValueError('主体 qq.send 缺少对象类型的 payload')
    stream_kind = body.get('streamKind')
    if stream_kind not in {'direct', 'group'}:
        raise ValueError('主体 qq.send 的 streamKind 必须是 direct 或 group')
    stream_external_id = body.get('streamExternalId')
    if not isinstance(stream_external_id, str) or not stream_external_id.strip():
        raise ValueError('主体 qq.send 缺少 streamExternalId')
    raw_segments = body.get('segments')
    if not isinstance(raw_segments, list) or not all(isinstance(item, str) for item in raw_segments):
        raise ValueError('主体 qq.send 的 segments 必须是字符串数组')
    segments = [item.strip() for item in raw_segments]
    if not all(segments):
        raise ValueError('主体 qq.send 的 segments 不能包含空字符串')
    raw_emoji_refs = body.get('emojiRefs', [])
    if not isinstance(raw_emoji_refs, list) or not all(
        isinstance(item, str) for item in raw_emoji_refs
    ):
        raise ValueError('主体 qq.send 的 emojiRefs 必须是字符串数组')
    emoji_refs = tuple(item.strip() for item in raw_emoji_refs)
    if not all(emoji_refs):
        raise ValueError('主体 qq.send 的 emojiRefs 不能包含空字符串')
    raw_emoji_sub_types = body.get('emojiSubTypes', [])
    if not isinstance(raw_emoji_sub_types, list) or not all(
        isinstance(item, int)
        and not isinstance(item, bool)
        and item >= 0
        and item not in {0, 4, 9}
        for item in raw_emoji_sub_types
    ):
        raise ValueError('主体 qq.send 的 emojiSubTypes 必须是表情包子类型整数数组')
    emoji_sub_types = tuple(raw_emoji_sub_types)
    if len(emoji_refs) != len(emoji_sub_types):
        raise ValueError('主体 qq.send 的 emojiRefs 与 emojiSubTypes 数量必须一致')
    if not segments and not emoji_refs:
        raise ValueError('主体 qq.send 必须包含文本或表情包')
    return BackendOutbound(
        stream_id=stream_id,
        stream_kind=stream_kind,
        stream_external_id=stream_external_id.strip(),
        segments=segments,
        emoji_refs=emoji_refs,
        emoji_sub_types=emoji_sub_types,
    )
