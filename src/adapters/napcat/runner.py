"""QQ 适配器运行器：顺序处理入站，整轮发送出站。"""

from __future__ import annotations

import asyncio

from src.common.logger import get_logger

from .backend import BackendClient
from .config import NapcatDocument
from .events import classify_event, parse_inbound_event
from .transport import ActionError, NapcatTransport


logger = get_logger(__name__)


class NapcatRunner:
    """管理协议端与主体连接，并只接通 M3 direct 私聊。"""

    def __init__(
        self,
        config: NapcatDocument,
        backend_port: int,
        token: str,
        transport: NapcatTransport | None = None,
        backend: BackendClient | None = None,
    ) -> None:
        self._config = config
        self._backend_port = backend_port
        self._token = token
        self._transport = transport or NapcatTransport(config.napcat)
        self._backend = backend or BackendClient(backend_port, token)
        self._connected_once = False

    async def run(self) -> None:
        """首次连接失败直接退出，已连上后断线才按配置重连。"""
        while True:
            try:
                self_id = await self._transport.connect()
                await self._backend.connect()
                self._connected_once = True
                logger.info(
                    'QQ 适配器已连接',
                    protocol=f'{self._config.napcat.host}:{self._config.napcat.port}',
                    selfId=self_id,
                    backendPort=self._backend_port,
                )
                await self._serve_connected(self_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._backend.close()
                await self._transport.close()
                if not self._connected_once:
                    logger.error('QQ 适配器首次连接失败', error=str(exc))
                    raise RuntimeError(f'首次连接 QQ 协议端失败：{exc}') from exc
                logger.warning(
                    'QQ 适配器连接断开，准备重连',
                    intervalSec=self._config.napcat.reconnect_interval_sec,
                    error=str(exc),
                )
                await asyncio.sleep(self._config.napcat.reconnect_interval_sec)

    async def _serve_connected(self, self_id: str) -> None:
        event_task = asyncio.create_task(self._consume_protocol_events(self_id))
        outbound_task = asyncio.create_task(self._consume_backend_outbound())
        done, pending = await asyncio.wait(
            {event_task, outbound_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()
        raise RuntimeError('QQ 适配器连接任务提前结束')

    async def _consume_protocol_events(self, self_id: str) -> None:
        async for payload in self._transport.iter_events():
            kind = classify_event(payload, self_id, self._config.owner.qq)
            if kind == 'action_response':
                continue
            if kind == 'heartbeat':
                continue
            if kind == 'request':
                logger.info('忽略 QQ request 事件', requestType=payload.get('request_type'))
                continue
            if kind == 'self_message':
                logger.info('忽略 QQ 自发消息', messageId=payload.get('message_id'))
                continue
            if kind == 'non_owner_private':
                logger.info('忽略非 owner QQ 私聊', userId=payload.get('user_id'))
                continue
            if kind != 'message':
                logger.debug('忽略未知 QQ 事件', postType=payload.get('post_type'))
                continue

            event = parse_inbound_event(payload, self_id, self._config.owner.qq)
            if event is None:
                continue
            if event.stream_kind != 'direct':
                logger.info('M3 忽略 QQ 群聊消息', groupId=event.stream_external_id)
                continue
            if not event.text.strip():
                logger.info('忽略空 QQ 私聊消息', messageId=event.external_message_id)
                continue
            await self._backend.submit_inbound(event)

    async def _consume_backend_outbound(self) -> None:
        async for outbound in self._backend.iter_outbound():
            if outbound.stream_kind != 'direct':
                logger.warning(
                    'M3 拒绝非私聊出站消息',
                    streamId=outbound.stream_id,
                    streamKind=outbound.stream_kind,
                )
                continue
            try:
                await self._transport.call_action(
                    'send_private_msg',
                    {
                        'user_id': _qq_number(outbound.stream_external_id),
                        'message': ''.join(outbound.segments),
                    },
                )
            except (ActionError, asyncio.TimeoutError) as exc:
                logger.error(
                    'QQ 私聊发送失败',
                    streamId=outbound.stream_id,
                    userId=outbound.stream_external_id,
                    error=str(exc),
                )


def _qq_number(value: str) -> int:
    normalized = value.strip()
    if not normalized.isdigit():
        raise ValueError(f'私聊目标不是数字 QQ 号：{value!r}')
    return int(normalized)
