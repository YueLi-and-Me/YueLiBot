"""提供主体后端到桌面端、QQ 适配器和 WebUI 的 WebSocket 推流。

Python → Electron 的所有主动推送都走这个连接：
  chat.event / chat.done / chat.error
  voice.play
  vision.watching
  sleep.state

Electron → Python 的命令走 HTTP（更简单，有状态码，易排查）。

连接按客户端类型分区，事件端点先回放数据库游标后的事件再订阅实时广播，
从而避免连接建立窗口内丢失事件。
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Set, cast

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .auth import ws_auth

from src.core.common.logger import get_logger
from src.core.observe import events as trace
from src.core.observe.events import LIVE_ONLY_KINDS, broadcaster
from src.core.observe.store import since as events_since
from src.core.webui.logs import webui_logs

logger = get_logger(__name__)
router = APIRouter()

# ─────────────────────────────────────────────────────────────────────
# 连接管理
# ─────────────────────────────────────────────────────────────────────

ClientKind = Literal['desktop', 'napcat']
_CLIENT_KINDS = frozenset({'desktop', 'napcat'})
_DESKTOP_STREAM_ID = 1


class _ConnectionManager:
    """管理按客户端分区的 WebSocket 订阅。

    `desktop` 对应固定桌面 stream，其他 stream 归入 `napcat`；连接集合由异步锁
    保护，推送时复制快照后在锁外执行网络发送。
    """

    def __init__(self) -> None:
        """创建空的桌面端和 QQ 适配器连接集合。

        :return: 无返回值。
        副作用：初始化连接字典和异步锁，不接受网络连接。
        """
        self._connections: Dict[ClientKind, Set[WebSocket]] = {
            'desktop': set(),
            'napcat': set(),
        }
        self._lock = asyncio.Lock()

    async def connect(self, client: ClientKind, ws: WebSocket) -> None:
        """登记一个已完成鉴权的 WebSocket。

        :param client: 客户端分区，只能为 `desktop` 或 `napcat`。
        :param ws: 待登记的 FastAPI WebSocket 对象。
        :return: 无返回值。
        副作用：在锁保护下向对应集合加入连接。
        """
        async with self._lock:
            self._connections[client].add(ws)

    async def disconnect(self, client: ClientKind, ws: WebSocket) -> None:
        """从客户端分区移除一个 WebSocket，重复移除安全。

        :param client: 客户端分区，只能为 `desktop` 或 `napcat`。
        :param ws: 待移除的 WebSocket 对象。
        :return: 无返回值。
        副作用：在锁保护下修改对应连接集合。
        """
        async with self._lock:
            self._connections[client].discard(ws)

    async def push(self, stream_id: int, channel: str, payload: Any) -> int:
        """向 stream 对应的客户端分区推送一条 JSON 信封消息。

        :param stream_id: 目标 stream 数据库 ID；固定桌面 stream 发送至 desktop 分区，
                其他 stream 发送至 napcat 分区。
        :param channel: 推送通道名称，例如 ``chat.event`` 或 ``voice.play``。
        :param payload: 通道负载；必须可由 ``json.dumps`` 序列化。

        :return: 成功调用 ``send_text`` 的连接数量；没有目标连接时返回 ``0``。

        :raises TypeError: 负载无法 JSON 序列化或参数不符合协议时抛出。

        副作用：
            在锁外向连接发送网络消息；发送失败的连接会从对应分区移除，napcat 无订阅者
            时记录 ``outbound_dropped`` 观测事件。

        性能：
            连接快照复制在锁内完成，网络发送按快照顺序串行执行，发送阶段不会阻塞连接登记。
        """
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
    """通过模块级连接管理器向指定 stream 推送一条消息。

    :param stream_id: 目标 stream 数据库 ID。
    :param channel: 推送通道名称。
    :param payload: 可 JSON 序列化的通道负载。

    :return: 实际完成发送的 WebSocket 连接数量。

    :raises TypeError: 负载无法 JSON 序列化时抛出。
    """
    return await manager.push(stream_id, channel, payload)


# ─────────────────────────────────────────────────────────────────────
# 路由
# ─────────────────────────────────────────────────────────────────────

@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    """鉴权并维持桌面端或 QQ 适配器的推送 WebSocket。

    :param websocket: FastAPI 注入的 WebSocket，必须带合法鉴权和 `client` 查询参数。
    :return: 客户端断开、鉴权失败或协议参数非法后返回。
    :raises Exception: 接收循环出现未覆盖的底层异常时记录后结束连接。
    副作用：在握手后登记连接，持续读取客户端心跳/帧，并在结束时移除连接。
    """
    # [WORKAROUND] WebSocket 鉴权失败连接兼容性约束
    #
    # 必须在 accept() 前执行鉴权失败后的 close()。
    # - 现象: accept() 后 close() 会让客户端误判连接已建立，随后 receive 阶段报错。
    # - 原理: accept() 前 close() 直接终止未确认会话，鉴权失败使用 1008 Policy Violation。
    # - 当前处理: 鉴权失败立即 close() 并返回，不进入 connect() 或 receive()。
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

    # 客户端选择 yueli-<token> 子协议时必须在 accept() 中回显。
    # 未回显时，严格客户端会拒绝握手；当前实现只回显请求头中实际出现的令牌协议。
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
    """向已登录浏览器回放并持续推送 ANSI 彩色日志。

    :param websocket: FastAPI 注入的 WebSocket 对象，必须携带有效认证凭据。

    :raises Exception: 日志回放或实时发送发生未覆盖的底层 WebSocket 错误时传播。

    副作用：
        鉴权通过后接受连接、发送现有 backlog 并订阅实时日志；连接结束时取消订阅。
    """
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


@router.websocket('/ws/events')
async def webui_events_endpoint(websocket: WebSocket) -> None:
    """按事件游标回放历史观测事件，再逐条推送实时事件。

    :param websocket: FastAPI 注入的 WebSocket 对象；查询参数 ``since`` 为可选非负序号。

    :raises ValueError: ``since`` 不是整数或为负数时关闭连接并返回协议错误。
    :raises Exception: 事件回放、实时订阅或 WebSocket 发送发生未覆盖错误时传播。

    副作用：
        鉴权通过后接受连接，先订阅实时广播再读取事件账本，按序号去重后发送；
        连接结束时取消广播订阅，队列溢出时以 1013 关闭连接。
    """
    if not await ws_auth(websocket):
        await websocket.close(code=1008)
        return

    raw_since = websocket.query_params.get('since', '0')
    try:
        since = int(raw_since)
    except ValueError:
        await websocket.close(code=1008)
        return
    if since < 0:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    # 必须先订阅实时广播，再读取历史账本；随后按 seq 去重，避免建立窗口丢事件。
    #
    # 事件账本只承载持久化事件，订阅时排除 LIVE_ONLY_KINDS：
    # - 现象: llm_chunk 在模型流式生成时按 token 触发，全部转发给浏览器会让观察面板
    #   每个分片都重渲染整棵事件树，打开面板即把单核 CPU 打满。
    # - 原因: 这些事件不落账、seq 为 None，既无法参与历史回放与按 seq 去重，又会灌满
    #   本订阅者的有界队列触发 1013 溢出关闭与重连抖动。
    # - 后果: 若恢复转发高频实时事件，观察面板会重新出现打开即满载 CPU 的问题。
    subscriber = broadcaster.subscribe(exclude_kinds=LIVE_ONLY_KINDS)
    try:
        page = events_since(since, 1_000)
        replay_frame: Dict[str, Any] = {'events': page.events}
        if page.truncated:
            replay_frame['truncated'] = True
            replay_frame['from'] = page.from_seq
        await websocket.send_json(replay_frame)
        last_sent_seq = page.events[-1]['seq'] if page.events else since

        while True:
            if subscriber.overflowed.is_set():
                await websocket.close(code=1013)
                return
            queue_task = asyncio.create_task(subscriber.queue.get())
            overflow_task = asyncio.create_task(subscriber.overflowed.wait())
            done, pending = await asyncio.wait(
                {queue_task, overflow_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if overflow_task in done and overflow_task.result():
                await websocket.close(code=1013)
                return
            entry = queue_task.result()
            entry_seq = entry.get('seq')
            if isinstance(entry_seq, int) and entry_seq <= last_sent_seq:
                continue
            await websocket.send_json({'events': [entry]})
            if isinstance(entry_seq, int):
                last_sent_seq = entry_seq
    except WebSocketDisconnect:
        pass
    finally:
        broadcaster.unsubscribe(subscriber)
