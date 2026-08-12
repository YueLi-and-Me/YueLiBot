"""编排 QQ 协议端、主体后端和入出站消息处理循环。

`NapcatRunner` 负责建立两条连接、按失败类型执行重试、过滤协议事件，并把主体
回复转换为 OneBot action；分类和字段解析委托给同目录的纯函数模块。
"""

from __future__ import annotations

import asyncio

import httpx

from src.core.common.logger import get_logger

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
    """管理协议端与主体连接，并接通允许的 QQ 私聊与群聊。

    运行器保持单一的协议事件消费者和主体出站消费者，任一连接任务提前结束都会
    结束当前会话并由外层重试策略重新建立连接。
    """

    def __init__(
        self,
        config: NapcatDocument,
        backend_port: int,
        token: str,
        transport: NapcatTransport | None = None,
        backend: BackendClient | None = None,
    ) -> None:
        """创建 QQ 适配器运行器。

        :param config: 已完成 Pydantic 校验的 QQ 适配器配置。
        :param backend_port: 主体 HTTP/WS 服务端口，必须传给 `BackendClient`。
        :param token: 主体 API 鉴权 token。
        :param transport: 可选的协议传输实现；为空时创建真实的
            :class:`NapcatTransport`，测试可传入替身。
        :param backend: 可选的主体客户端；为空时创建 :class:`BackendClient`。
        :raises ValueError: 默认客户端发现主体端口或 token 非法时抛出。
        :side_effects: 保存配置并可能构造网络客户端，但不会建立连接。
        """
        self._config = config
        self._backend_port = backend_port
        self._token = token
        self._transport = transport or NapcatTransport(config.napcat)
        self._backend = backend or BackendClient(backend_port, token)
        self._connected_once = False

    async def run(self) -> None:
        """建立 QQ 协议端和主体连接，并按错误类型维持或终止运行。

        Raises:
            RuntimeError: 发生鉴权失败、握手失败、账号不匹配或其他不可重试错误。
            asyncio.CancelledError: 调用方取消运行任务时原样传播。

        Side Effects:
            创建并维护协议端与主体连接；可按指数退避反复重连；停机或错误时关闭连接。
        """
        if not self._config.napcat.enabled:
            logger.info('QQ 适配器未启用，跳过协议端连接')
            return

        retry_count = 0
        while True:
            try:
                # 先确认协议端实际登录身份，再建立主体连接，避免向错误账号发送消息。
                self_id = await self._transport.connect()
                self_name = self._transport.self_name
                _check_self_qq_matches(self._config.napcat.self_qq, self_id)
                await self._backend.connect()
                await self._backend.link_owner_identity(self._config.owner.qq)
                self._connected_once = True
                logger.info(
                    'QQ 适配器已连接',
                    protocol=f'{self._config.napcat.host}:{self._config.napcat.port}',
                    selfId=self_id,
                    selfName=self_name,
                    backendPort=self._backend_port,
                    retryCount=retry_count,
                )
                retry_count = 0
                await self._serve_connected(self_id, self_name)
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

                # 可恢复故障使用退避重试；仅首次失败记录完整警告，后续降低日志级别。
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

    async def _serve_connected(self, self_id: str, self_name: str) -> None:
        """并发运行协议入站和主体出站两个消费者。

        :param self_id: 协议端实际登录的 QQ 号。
        :param self_name: 协议端实际登录昵称。
        :return: 两个消费者都结束前不会正常返回。
        :raises Exception: 任一消费者抛出异常，或任一任务提前结束。
        :side_effects: 创建两个异步任务；一个任务结束后取消另一个任务。
        :performance: 两个消费者并行运行，但每个方向保持单消费者顺序。
        """
        event_task = asyncio.create_task(self._consume_protocol_events(self_id, self_name))
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

    async def _consume_protocol_events(self, self_id: str, self_name: str) -> None:
        """消费协议端事件并提交通过访问策略的入站消息。

        :param self_id: 协议端登录 QQ 号，用于忽略机器人自身消息和识别提及。
        :param self_name: 协议端登录昵称，用于生成入站事件中的机器人名称。
        :return: 仅在事件迭代器结束时返回。
        :raises Exception: 事件结构非法、主体提交失败且未被本方法捕获，或传输层断开。
        :side_effects: 持续读取协议事件，并通过 HTTP 写入主体后端；拒绝的消息只记录日志。
        """
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
                self_name,
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
        """消费主体出站回复并调用协议端发送私聊或群聊消息。

        :return: 仅在主体出站迭代器结束时返回。
        :raises ValueError: 主体出站流类型不是 `direct` 或 `group`，或目标 QQ 号非法。
        :raises Exception: 传输层发生未被发送错误处理分支覆盖的异常。
        :side_effects: 持续读取主体 WebSocket，并向协议端发送 action；单条发送失败只记录日志。
        """
        async for outbound in self._backend.iter_outbound():
            # 先映射协议 action 和目标字段，再统一校验外部 QQ 标识。
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
                # 单条发送错误只影响当前消息；连接和后续出站流继续保持可用。
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
    """校验配置的机器人 QQ 号与协议端实际登录身份一致。

    Args:
        configured: 配置文件中的 ``napcat.self_qq``。
        actual_self_id: 协议端 ``get_login_info`` 返回的登录 QQ 号。

    Raises:
        SelfQqMismatch: 两个身份标识不一致。

    Side Effects:
        不执行网络请求，也不修改传入字符串或运行器状态。
    """
    if configured == actual_self_id:
        return
    raise SelfQqMismatch(
        f'配置里的 napcat.self_qq 是 {configured}，但协议端登录的是 {actual_self_id}。'
        f'请把 napcat.self_qq 改成 {actual_self_id}，或者检查是不是连错了协议端'
    )


def _qq_number(value: str, target_label: str) -> int:
    """把出站目标标识转换为协议端需要的整数 QQ 号。

    :param value: 主体出站消息中的外部目标 ID。
    :param target_label: 错误信息中展示的目标名称。
    :return: 输入去除空白后的十进制整数。
    :raises ValueError: 输入为空或包含非数字字符。
    :side_effects: 不执行 I/O。
    """
    normalized = value.strip()
    if not normalized.isdigit():
        raise ValueError(f'{target_label}不是数字 QQ 号：{value!r}')
    return int(normalized)


def _is_retryable(error: BaseException) -> bool:
    """判断异常是否属于可通过重新建立连接恢复的传输错误。

    :param error: 适配器运行期间捕获的异常。
    :return: 传输中断、连接失败或超时等可重试错误返回 ``True``；鉴权、握手、动作和
        自身账号校验错误返回 ``False``。
    :side_effects: 不修改异常对象或适配器状态。
    """
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
    """计算当前重试次数对应的指数退避时长。

    Args:
        interval_sec: 配置的基础重连间隔，单位为秒；应为正数。
        retry_count: 从 ``1`` 开始的重试次数；小于 ``1`` 时按指数表达式的实际结果计算。

    Returns:
        ``interval_sec`` 乘以 ``2 ** (retry_count - 1)``，放大倍数上限为 ``32``。

    Raises:
        TypeError: 参数不是支持乘法、减法和幂运算的数值时抛出。
    """
    return interval_sec * min(2 ** (retry_count - 1), 32)
