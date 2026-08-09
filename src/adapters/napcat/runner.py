"""QQ 适配器运行器：顺序处理入站，整轮发送出站。"""

from __future__ import annotations

import asyncio

import httpx

from src.common.logger import get_logger

from .backend import BackendClient
from .config import NapcatDocument
from .events import classify_event, parse_inbound_event
from .transport import (
    ActionError,
    NapcatTransport,
    ProtocolAuthenticationError,
    ProtocolHandshakeError,
)


logger = get_logger(__name__)


class NapcatRunner:
    """管理协议端与主体连接，并接通允许的 QQ 私聊与群聊。"""

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
        """连接协议端并保持运行，断线按失败类型决定重试还是退出。"""
        if not self._config.napcat.enabled:
            logger.info('QQ 适配器未启用，跳过协议端连接')
            return

        retry_count = 0
        while True:
            try:
                # 连协议端，核对登录的号，再接主体
                self_id = await self._transport.connect()
                _check_self_qq_matches(self._config.napcat.self_qq, self_id)
                await self._backend.connect()
                await self._backend.link_owner_identity(self._config.owner.qq)
                self._connected_once = True
                logger.info(
                    'QQ 适配器已连接',
                    protocol=f'{self._config.napcat.host}:{self._config.napcat.port}',
                    selfId=self_id,
                    backendPort=self._backend_port,
                    retryCount=retry_count,
                )
                retry_count = 0
                await self._serve_connected(self_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._backend.close()
                await self._transport.close()
                if not _is_retryable(exc):
                    logger.error(
                        'QQ 适配器启动失败，停止重试',
                        protocol=f'{self._config.napcat.host}:{self._config.napcat.port}',
                        error=str(exc),
                    )
                    raise RuntimeError(f'QQ 适配器启动失败，已停止重试：{exc}') from exc

                # 暂时性故障：退避后重试，只有第一次打完整警告
                retry_count += 1
                delay = _retry_delay(
                    self._config.napcat.reconnect_interval_sec,
                    retry_count,
                )
                phase = '重连' if self._connected_once else '首次连接'
                log_fields = {
                    'protocol': f'{self._config.napcat.host}:{self._config.napcat.port}',
                    'intervalSec': delay,
                    'retryCount': retry_count,
                    'error': str(exc),
                }
                if retry_count == 1:
                    logger.warning(
                        f'QQ 协议端{phase}暂不可用，准备重试；请确认协议端已启动且连接已启用',
                        **log_fields,
                    )
                else:
                    logger.debug(
                        f'QQ 协议端{phase}仍不可用，继续重试',
                        **log_fields,
                    )
                await asyncio.sleep(delay)

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
            kind = classify_event(
                payload,
                self_id,
                self._config.owner.qq,
                self._config.private,
                self._config.group,
            )
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
            if kind == 'private_denied':
                logger.info(
                    'QQ 私聊访问被拒',
                    userId=payload.get('user_id'),
                    mode=self._config.private.mode,
                    reason='不在私聊访问名单中',
                )
                continue
            if kind == 'group_denied':
                logger.info(
                    'QQ 群聊访问被拒',
                    groupId=payload.get('group_id'),
                    mode=self._config.group.mode,
                    reason='群聊不在白名单中',
                )
                continue
            if kind != 'message':
                logger.debug('忽略未知 QQ 事件', postType=payload.get('post_type'))
                continue

            event = parse_inbound_event(
                payload,
                self_id,
                self._config.owner.qq,
                self._config.private,
                self._config.group,
            )
            if event is None:
                continue
            if not event.text.strip():
                logger.info('忽略空 QQ 消息', messageId=event.external_message_id)
                continue
            try:
                await self._backend.submit_inbound(event)
            except httpx.ReadTimeout as exc:
                logger.error(
                    'QQ 入站消息提交超时',
                    streamExternalId=event.stream_external_id,
                    messageId=event.external_message_id,
                    error=str(exc),
                )
            except httpx.HTTPStatusError as exc:
                # 主体拒收：丢这一条继续下一条，不拆连接
                logger.error(
                    'QQ 入站消息被主体拒绝',
                    streamExternalId=event.stream_external_id,
                    messageId=event.external_message_id,
                    status=exc.response.status_code,
                    error=str(exc),
                )

    async def _consume_backend_outbound(self) -> None:
        async for outbound in self._backend.iter_outbound():
            if outbound.stream_kind == 'direct':
                action = 'send_private_msg'
                target_field = 'user_id'
                target_label = '私聊目标'
            elif outbound.stream_kind == 'group':
                action = 'send_group_msg'
                target_field = 'group_id'
                target_label = '群聊目标'
            else:
                raise ValueError(f'QQ 出站 streamKind 不受支持：{outbound.stream_kind}')
            try:
                await self._transport.call_action(
                    action,
                    {
                        target_field: _qq_number(outbound.stream_external_id, target_label),
                        'message': ''.join(outbound.segments),
                    },
                )
            except (ActionError, asyncio.TimeoutError) as exc:
                logger.error(
                    'QQ 消息发送失败',
                    streamId=outbound.stream_id,
                    streamKind=outbound.stream_kind,
                    targetId=outbound.stream_external_id,
                    error=str(exc),
                )


class SelfQqMismatch(RuntimeError):
    """配置里的 self_qq 与协议端实际登录的号不一致。"""


def _check_self_qq_matches(configured: str, actual_self_id: str) -> None:
    """核对配置里的 self_qq 与协议端实际登录的号。"""
    if configured == actual_self_id:
        return
    raise SelfQqMismatch(
        f'配置里的 napcat.self_qq 是 {configured}，但协议端登录的是 {actual_self_id}。'
        f'请把 napcat.self_qq 改成 {actual_self_id}，或者检查是不是连错了协议端'
    )


def _qq_number(value: str, target_label: str) -> int:
    normalized = value.strip()
    if not normalized.isdigit():
        raise ValueError(f'{target_label}不是数字 QQ 号：{value!r}')
    return int(normalized)


def _is_retryable(error: BaseException) -> bool:
    """判断这个异常该不该重试。"""
    if isinstance(
        error,
        (ProtocolAuthenticationError, ProtocolHandshakeError, ActionError, SelfQqMismatch),
    ):
        return False
    # httpx 的异常不继承 ConnectionError/OSError，要单独列
    if isinstance(error, httpx.TransportError):
        return True
    return isinstance(error, (ConnectionError, OSError, asyncio.TimeoutError))


def _retry_delay(interval_sec: float, retry_count: int) -> float:
    """按配置间隔指数退避，最多放大到 32 倍。"""
    return interval_sec * min(2 ** (retry_count - 1), 32)
