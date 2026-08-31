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
from src.core.platform_io.forward import forward_tree_to_payload

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
    # 主体算好的逐批停顿，与「文字在前、表情包在后」的发送批次对齐；缺省表示不等待。
    batch_delays_ms: tuple[int, ...] = ()
    # 第一条气泡要引用的平台消息编号；为空表示不引用。
    quote_external_message_id: str = ''
    # 主体发起本次投递的回合编号，只在投递失败回传时原样带回，使失败能落到发起
    # 它的那一轮上。0 表示主体没有回合上下文。
    turn_id: int = 0


@dataclass(frozen=True)
class BackendReaction:
    """主体发给 QQ 适配器的一次表情回应。

    与 :class:`BackendOutbound` 是两种东西：那个是发消息，这个是给已有消息贴表情，
    协议端对应的是完全不同的 action。共用一个类型只会让适配器靠字段有无猜意图。
    """

    stream_id: int
    stream_kind: Literal['direct', 'group']
    stream_external_id: str
    target_external_message_id: str
    reaction: str
    # 语义同 :attr:`BackendOutbound.turn_id`。
    turn_id: int = 0


@dataclass(frozen=True)
class BackendPoke:
    """主体发给 QQ 适配器的一次戳一戳。"""

    stream_id: int
    stream_kind: Literal['direct', 'group']
    stream_external_id: str
    target_external_id: str
    # 语义同 :attr:`BackendOutbound.turn_id`。
    turn_id: int = 0


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
        body: Dict[str, Any] = {
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
            'pokedMe': event.poked_me,
            'emojiLikedMe': event.emoji_liked_me,
        }
        # 空集合不发送新字段，保持旧适配器报文与既有精确载荷测试不变。
        if event.forward_messages:
            body['forwardMessages'] = [
                forward_tree_to_payload(tree) for tree in event.forward_messages
            ]
        response = await client.post(
            '/platform/inbound',
            json=body,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError('主体 /platform/inbound 响应必须是 JSON 对象')
        return payload

    async def submit_typing(self, sender_external_id: str) -> bool:
        """把私聊输入状态通知提交给主体。

        协议端在对方打字期间会反复推送，主体自行决定是否据此开口，因此这里不做
        节流；返回的 ``spoke`` 供调用方区分「检测到但无需追问」与「已触发追问」。

        :param sender_external_id: 正在输入的对方 QQ 号。
        :return: 主体是否据此触发了一次主动发言。
        :raises BackendDisconnected: HTTP 客户端尚未建立连接。
        :raises httpx.HTTPError: 请求失败或主体返回非成功 HTTP 状态码。
        :raises ValueError: 主体响应不是包含布尔 ``spoke`` 的 JSON 对象。
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
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get('spoke'), bool):
            raise ValueError('主体 /platform/typing 响应必须包含布尔 spoke')
        return payload['spoke']

    async def report_delivery_failure(
        self,
        *,
        stream_id: int,
        turn_id: int,
        action: str,
        target: str,
        error: str,
    ) -> None:
        """把一次出站动作在协议端的失败回报给主体。

        主体的投递回执只证明报文送到了适配器，真正的 action 在这里才发出；不回报
        就会出现「主体记 outbound_delivered、控制台显示动作成功，实际什么都没发生」
        的假成功。回报本身失败只记日志：协议端故障不应再连带中断出站循环。

        :param stream_id: 主体的会话编号。
        :param turn_id: 发起该投递的回合编号；0 表示没有回合上下文。
        :param action: 失败的动作类别，取 send / react / poke。
        :param target: 动作目标的平台标识（QQ 号或消息编号），无目标时为空串。
        :param error: 协议端返回的失败原因原文。
        :return: ``None``。
        :raises BackendDisconnected: HTTP 客户端尚未建立连接。
        副作用：向主体投递失败接口发送一次 POST 请求。
        """
        client = self._http
        if client is None:
            raise BackendDisconnected('主体 HTTP 尚未连接')
        response = await client.post(
            '/platform/delivery/failed',
            json={
                'platform': 'qq',
                'streamId': stream_id,
                'turnId': turn_id,
                'action': action,
                'target': target,
                'error': error,
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

    async def next_outbound(self) -> BackendOutbound | BackendReaction | BackendPoke:
        """从主体出站 WebSocket 读取下一条 QQ 通道消息或表情回应。

        :return: 下一条通过协议校验的 ``BackendOutbound`` 或 ``BackendReaction``。

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
            channel = payload.get('channel')
            if channel == 'qq.send':
                return _parse_outbound(payload)
            if channel == 'qq.react':
                return _parse_reaction(payload)
            if channel == 'qq.poke':
                return _parse_poke(payload)
            logger.debug('忽略主体出站通道', channel=channel)

    async def iter_outbound(
        self,
    ) -> AsyncIterator[BackendOutbound | BackendReaction | BackendPoke]:
        """持续按主体 WebSocket 到达顺序产生适配器出站消息与表情回应。

        :return: 每次迭代返回一条已校验的 :class:`BackendOutbound` 或
            :class:`BackendReaction`。
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


def _parse_turn_id(body: Mapping[str, Any], channel: str) -> int:
    """从出站报文里取发起投递的回合编号。

    该字段可选：主体只在有回合上下文时下发，缺省表示后台补发一类没有回合归属的
    投递。字段存在却类型不对属于协议不同步，必须当场暴露而不是按 0 放过——静默
    放过会让投递失败回传丢掉归属，失败重新变得无法归因。

    :param body: 出站报文的 payload 对象。
    :param channel: 通道名，仅用于错误信息定位。
    :return: 正整数回合编号；字段缺省时返回 0。
    :raises ValueError: 字段存在但不是正整数。
    """
    raw = body.get('turnId')
    if raw is None:
        return 0
    if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
        raise ValueError(f'主体 {channel} 的 turnId 必须是正整数')
    return raw


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
    raw_delays = body.get('batchDelaysMs', [])
    if not isinstance(raw_delays, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) and item >= 0
        for item in raw_delays
    ):
        raise ValueError('主体 qq.send 的 batchDelaysMs 必须是非负整数数组')
    batch_delays_ms = tuple(raw_delays)
    if batch_delays_ms and len(batch_delays_ms) != len(segments) + len(emoji_refs):
        raise ValueError('主体 qq.send 的 batchDelaysMs 与发送批次数量必须一致')
    if not segments and not emoji_refs:
        raise ValueError('主体 qq.send 必须包含文本或表情包')
    raw_quote = body.get('quoteExternalMessageId', '')
    if not isinstance(raw_quote, str):
        raise ValueError('主体 qq.send 的 quoteExternalMessageId 必须是字符串')
    return BackendOutbound(
        stream_id=stream_id,
        stream_kind=stream_kind,
        stream_external_id=stream_external_id.strip(),
        segments=segments,
        emoji_refs=emoji_refs,
        emoji_sub_types=emoji_sub_types,
        batch_delays_ms=batch_delays_ms,
        quote_external_message_id=raw_quote.strip(),
        turn_id=_parse_turn_id(body, 'qq.send'),
    )


def _parse_poke(payload: Mapping[str, Any]) -> BackendPoke:
    """校验并转换主体 `qq.poke` 报文。

    :param payload: 已解析的主体戳一戳报文。
    :return: 去除首尾空白后的 :class:`BackendPoke`。
    :raises ValueError: 缺少字段、字段类型错误或流类型不受支持。
    副作用：不执行 I/O，也不修改传入映射。
    """
    stream_id = payload.get('stream_id')
    if not isinstance(stream_id, int) or isinstance(stream_id, bool) or stream_id <= 0:
        raise ValueError('主体 qq.poke 缺少合法 stream_id')
    body = payload.get('payload')
    if not isinstance(body, Mapping):
        raise ValueError('主体 qq.poke 缺少对象类型的 payload')
    stream_kind = body.get('streamKind')
    if stream_kind not in {'direct', 'group'}:
        raise ValueError(f'主体 qq.poke 的 streamKind 不受支持：{stream_kind}')
    stream_external_id = body.get('streamExternalId')
    if not isinstance(stream_external_id, str) or not stream_external_id.strip():
        raise ValueError('主体 qq.poke 缺少非空 streamExternalId')
    target = body.get('targetExternalId')
    if not isinstance(target, str) or not target.strip():
        raise ValueError('主体 qq.poke 缺少非空 targetExternalId')
    return BackendPoke(
        stream_id=stream_id,
        stream_kind=stream_kind,
        stream_external_id=stream_external_id.strip(),
        target_external_id=target.strip(),
        turn_id=_parse_turn_id(body, 'qq.poke'),
    )


def _parse_reaction(payload: Mapping[str, Any]) -> BackendReaction:
    """校验并转换主体 `qq.react` 报文。

    :param payload: 已解析的主体表情回应报文，必须包含正整数 `stream_id` 和
        对象型 `payload`，其内部必须给出流类型、流 ID、被回应消息平台编号与
        语义反应标识。
    :return: 去除首尾空白后的 :class:`BackendReaction`。
    :raises ValueError: 缺少字段、字段类型错误或流类型不受支持。
    副作用：不执行 I/O，也不修改传入映射。
    """
    stream_id = payload.get('stream_id')
    if not isinstance(stream_id, int) or isinstance(stream_id, bool) or stream_id <= 0:
        raise ValueError('主体 qq.react 缺少合法 stream_id')
    body = payload.get('payload')
    if not isinstance(body, Mapping):
        raise ValueError('主体 qq.react 缺少对象类型的 payload')
    stream_kind = body.get('streamKind')
    if stream_kind not in {'direct', 'group'}:
        raise ValueError(f'主体 qq.react 的 streamKind 不受支持：{stream_kind}')
    stream_external_id = body.get('streamExternalId')
    if not isinstance(stream_external_id, str) or not stream_external_id.strip():
        raise ValueError('主体 qq.react 缺少非空 streamExternalId')
    target = body.get('targetExternalMessageId')
    if not isinstance(target, str) or not target.strip():
        raise ValueError('主体 qq.react 缺少非空 targetExternalMessageId')
    reaction = body.get('reaction')
    if not isinstance(reaction, str) or not reaction.strip():
        raise ValueError('主体 qq.react 缺少非空 reaction')
    return BackendReaction(
        stream_id=stream_id,
        stream_kind=stream_kind,
        stream_external_id=stream_external_id.strip(),
        target_external_message_id=target.strip(),
        reaction=reaction.strip(),
        turn_id=_parse_turn_id(body, 'qq.react'),
    )
