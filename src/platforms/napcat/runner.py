"""编排 QQ 协议端、主体后端和入出站消息处理循环。

`NapcatRunner` 负责建立两条连接、按失败类型执行重试、过滤协议事件，并把主体
回复转换为 OneBot action；分类和字段解析委托给同目录的纯函数模块。

入站正文里的引用关系与合并转发结构必须在提交主体前补齐：被 ``@`` 的显示名经
成员信息接口解析，被引用消息的原文经 ``get_msg`` 还原，合并转发经
``get_forward_msg`` 还原为完整树。显示名和引用摘要带进程内缓存；附加解析失败时
保留原占位形态，不阻断消息正文入站。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Mapping
from urllib.parse import urlsplit
import asyncio

import httpx

from src.core.common.logger import get_logger
from src.core.platform_io.forward import ForwardMessageTree

from .backend import BackendClient, BackendOutbound, BackendPoke, BackendReaction
from .config import NapcatDocument
from .events import (
    QqInboundEvent,
    build_emoji_like_inbound_event,
    build_poke_inbound_event,
    classify_event,
    parse_inbound_event,
)
from .forward import parse_forward_content, parse_forward_response
from .segments import (
    is_emoji_image,
    mentioned_user_ids,
    message_to_text,
    outbound_message_batches,
    quoted_message_ids,
    reaction_emoji_id,
)
from .transport import (
    ActionError,
    NapcatTransport,
    ProtocolAuthenticationError,
    ProtocolHandshakeError,
)


logger = get_logger(__name__)

# 引用摘要保留的正文字符数上限。摘要只用于让模型认出被引用的是哪句话，
# 完整正文通常已经在聊天历史里，超出部分截断为省略号。
QUOTE_PREVIEW_LIMIT = 40
# 显示名与引用摘要缓存的条目上限，超出后按插入顺序淘汰最旧的条目。
# 群成员和被引用消息都是有限集合，这个上限只用于防止长时间运行后无限增长。
RESOLUTION_CACHE_LIMIT = 512


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
        副作用：保存配置并可能构造网络客户端，但不会建立连接。
        """
        self._config = config
        self._backend_port = backend_port
        self._token = token
        self._transport = transport or NapcatTransport(config.napcat)
        self._backend = backend or BackendClient(backend_port, token)
        self._connected_once = False
        # (群号, QQ 号) -> 显示名；私聊用空群号。群名片按群独立，不能跨群复用。
        self._display_names: Dict[tuple[str, str], str] = {}
        # 被引用消息 ID -> 已渲染的引用摘要，避免同一条消息被反复引用时重复查询。
        self._quote_previews: Dict[str, str] = {}
        # 消息 ID -> 该消息是否 Bot 自己发的。表情回应通知不携带目标消息的发送者，
        # 必须查一次协议端才能判断「回应是不是给 Bot 的」；同一条消息往往连着多个
        # 回应，缓存避免反复查询。
        self._own_message_ids: Dict[str, bool] = {}

    async def run(self) -> None:
        """建立 QQ 协议端和主体连接，并按错误类型维持或终止运行。

        :raises RuntimeError: 发生鉴权失败、握手失败、账号不匹配或其他不可重试错误。
        :raises asyncio.CancelledError: 调用方取消运行任务时原样传播。

        副作用：
            创建并维护协议端与主体连接；可按指数退避反复重连；停机或错误时关闭连接。
        """
        if not self._config.napcat.enabled:
            logger.info('QQ 适配器未启用，跳过协议端连接')
            return

        retry_count = 0
        # finally 兜底：Ctrl+C 走 CancelledError 时此前不关闭连接（幂等，与
        # except 分支里的重复关闭不冲突）。
        try:
            while True:
                try:
                    # 先确认协议端实际登录身份，再建立主体连接，避免向错误账号发送消息。
                    self_id = await self._transport.connect()
                    self_name = self._transport.self_name
                    _check_self_qq_matches(self._config.napcat.self_qq, self_id)
                    await self._backend.connect()
                    await self._backend.link_owner_identity(self._config.owner.qq)
                    # 只回填观察上下文，不触发回复；失败不阻断连接建立。
                    await self._backfill_recent_group_history(self_id, self_name)
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
        finally:
            await self._backend.close()
            await self._transport.close()

    async def _serve_connected(self, self_id: str, self_name: str) -> None:
        """并发运行协议入站和主体出站两个消费者。

        :param self_id: 协议端实际登录的 QQ 号。
        :param self_name: 协议端实际登录昵称。
        :return: 两个消费者都结束前不会正常返回。
        :raises Exception: 任一消费者抛出异常，或任一任务提前结束。
        副作用：创建两个异步任务；一个任务结束后取消另一个任务。
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

    async def _backfill_recent_group_history(self, self_id: str, self_name: str) -> None:
        """拉取白名单群的最近历史，只观察落库不触发回复。"""
        for group_id in self._config.group.list:
            try:
                response = await self._transport.call_action(
                    'get_group_msg_history',
                    {'group_id': int(group_id), 'count': 20, 'reverse_order': True},
                )
                messages = _parse_group_history(
                    response, group_id, self_id, self_name, self._config.owner.qq,
                    self._config.private, self._config.group,
                )
                if not messages:
                    continue
                await self._backend.submit_group_backfill(group_id, messages)
                logger.info(
                    'QQ 群历史回填完成',
                    groupId=group_id,
                    count=len(messages),
                )
            except Exception as exc:
                logger.warning(
                    'QQ 群历史回填失败，继续服务当前连接',
                    groupId=group_id,
                    error=str(exc),
                )

    def _remember_display_name(self, group_id: str, user_id: str, name: str) -> None:
        """把一个已知的 QQ 显示名写入解析缓存，并维持缓存容量上限。

        :param group_id: 群号；私聊传空字符串。
        :param user_id: QQ 号；为空时直接忽略。
        :param name: 群名片或昵称；为空时不覆盖已有条目。
        :return: 无返回值。
        副作用：修改 ``self._display_names``，必要时淘汰最早写入的条目。
        """
        if not user_id or not name.strip():
            return
        self._display_names[(group_id, user_id)] = name.strip()
        while len(self._display_names) > RESOLUTION_CACHE_LIMIT:
            self._display_names.pop(next(iter(self._display_names)))

    async def _resolve_mention_names(
        self,
        payload: Mapping[str, Any],
        raw_segments: List[Any],
        self_id: str,
    ) -> Dict[str, str]:
        """把消息里被 ``@`` 的 QQ 号解析为群名片或昵称。

        只有裸 QQ 号时模型分不清「@1624606785」点的是谁，既无法判断这句话是不是
        冲着自己来的，也读不懂群里的对话指向。发送者本人的名字直接取自事件字段，
        其余被提及者按 (群号, QQ 号) 查缓存，未命中才向协议端查询一次。

        :param payload: OneBot 原始消息事件，用于读取群号与发送者信息。
        :param raw_segments: 该消息的消息段列表。
        :param self_id: 机器人登录 QQ 号；其显示名由解析函数统一补齐，此处跳过。
        :return: QQ 号到显示名的映射；解析失败的号码不出现在映射里，渲染时保持裸号。
        副作用：对未缓存的提及对象调用一次协议端查询，并写入显示名缓存。
        """
        group_id = _optional_text(payload.get('group_id'))
        sender = payload.get('sender')
        if isinstance(sender, Mapping):
            # 发送者信息随每条消息下发，顺手入缓存可以让绝大多数提及免于查询。
            self._remember_display_name(
                group_id,
                _optional_text(sender.get('user_id')),
                _optional_text(sender.get('card')) or _optional_text(sender.get('nickname')),
            )

        resolved: Dict[str, str] = {}
        for user_id in mentioned_user_ids(raw_segments):
            if user_id == self_id:
                continue
            cached = self._display_names.get((group_id, user_id))
            if cached is None:
                cached = await self._query_display_name(group_id, user_id)
            if cached:
                resolved[user_id] = cached
        return resolved

    async def _query_display_name(self, group_id: str, user_id: str) -> str:
        """向协议端查询一个 QQ 号在当前会话中的显示名。

        :param group_id: 群号；为空时按陌生人资料查询昵称。
        :param user_id: 待查询的 QQ 号。
        :return: 群名片或昵称；查询失败或字段为空时返回空字符串。
        副作用：调用一次 ``get_group_member_info`` 或 ``get_stranger_info``，
            成功时写入显示名缓存。
        """
        action = 'get_group_member_info' if group_id else 'get_stranger_info'
        params: Dict[str, Any] = {'user_id': user_id}
        if group_id:
            params['group_id'] = group_id
        try:
            response = await self._transport.call_action(action, params)
        except Exception as exc:
            # 名字解析失败不影响消息本身入站，正文退回裸 QQ 号即可；
            # 抛出会让整条消息连同正文一起丢失，代价远大于少一个名字。
            logger.warning(
                'QQ 提及显示名解析失败，保留裸号码',
                groupId=group_id,
                userId=user_id,
                error=str(exc),
            )
            return ''
        data = response.get('data')
        if not isinstance(data, Mapping):
            return ''
        name = _optional_text(data.get('card')) or _optional_text(data.get('nickname'))
        self._remember_display_name(group_id, user_id, name)
        return name

    async def _query_member_identity(
        self,
        group_id: str,
        user_id: str,
    ) -> tuple[str, str]:
        """查询一个 QQ 号的账号昵称与本群名片，两者分开返回。

        与 ``_query_display_name`` 的区别是不做「名片优先、否则昵称」的合并：
        入站事件要把昵称与名片分别提交给主体，而主体把空名片视为「清除名片」，
        合并后再拆会把没有名片的人写成「名片等于昵称」，或把有名片的人抹空。

        :param group_id: 群号；为空时按陌生人资料查询，名片一律返回空串。
        :param user_id: 待查询的 QQ 号。
        :return: ``(昵称, 群名片)``；查询失败时两项均为空串。
        副作用：调用一次 ``get_group_member_info`` 或 ``get_stranger_info``。
        """
        action = 'get_group_member_info' if group_id else 'get_stranger_info'
        params: Dict[str, Any] = {'user_id': user_id}
        if group_id:
            params['group_id'] = group_id
        try:
            response = await self._transport.call_action(action, params)
        except Exception as exc:
            logger.warning(
                'QQ 成员信息查询失败',
                groupId=group_id,
                userId=user_id,
                error=str(exc),
            )
            return '', ''
        data = response.get('data')
        if not isinstance(data, Mapping):
            return '', ''
        return _optional_text(data.get('nickname')), _optional_text(data.get('card'))

    async def _reacted_message_is_mine(
        self,
        message_id: str,
        self_id: str,
    ) -> bool:
        """判断被贴表情回应的那条消息是不是 Bot 自己发的。

        协议端为群里所有回应都推送 group_msg_emoji_like，通知里只有
        目标消息 ID、没有发送者；不查询一次就会把群里所有人的回应都当成给 Bot 的。
        查询结果按消息 ID 缓存：同一条消息经常连着多个回应，逐次查询会放大
        串行入站循环的往返次数。

        :param message_id: 被回应消息的平台编号。
        :param self_id: 机器人登录 QQ 号。
        :return: 目标消息发送者是 Bot 时返回 True；查询失败一律按不是处理，
            漏一条回应的代价低于把群里回应错记为给 Bot 的。
        副作用：调用一次 get_msg 并写入消息归属缓存。
        """
        cached = self._own_message_ids.get(message_id)
        if cached is not None:
            return cached
        try:
            response = await self._transport.call_action(
                'get_msg', {'message_id': int(message_id)},
            )
        except (ActionError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning(
                'QQ 表情回应目标消息查询失败，已跳过',
                messageId=message_id,
                error=str(exc),
            )
            return False
        data = response.get('data')
        sender = data.get('sender') if isinstance(data, Mapping) else None
        sender_id = (
            _optional_text(sender.get('user_id'))
            if isinstance(sender, Mapping)
            else ''
        )
        verdict = sender_id == self_id
        self._own_message_ids[message_id] = verdict
        while len(self._own_message_ids) > RESOLUTION_CACHE_LIMIT:
            self._own_message_ids.pop(next(iter(self._own_message_ids)))
        return verdict

    async def _resolve_quote_previews(
        self,
        raw_segments: List[Any],
        self_id: str,
        self_name: str,
    ) -> Dict[str, str]:
        """把消息里的引用段还原为「回复 某人：原文」摘要。

        OneBot 的 ``reply`` 段只带被引用消息的 ID，正文完全不在事件里。缺少这段
        还原时，模型面对「[引用消息] ？」这类消息只能凭当前这条硬猜，回复经常
        答非所问；引用同时又是触发必回的强信号，所以这类盲回占比很高。

        :param raw_segments: 该消息的消息段列表。
        :param self_id: 机器人登录 QQ 号，用于识别引用的是 Bot 自己的消息。
        :param self_name: 机器人显示名，用于渲染引用自身消息的摘要。
        :return: 被引用消息 ID 到摘要文本的映射；还原失败的 ID 不出现在映射里。
        副作用：对未缓存的被引用消息调用一次 ``get_msg``，并写入摘要缓存。
        """
        previews: Dict[str, str] = {}
        for quoted_id in quoted_message_ids(raw_segments):
            cached = self._quote_previews.get(quoted_id)
            if cached is None:
                cached = await self._query_quote_preview(quoted_id, self_id, self_name)
            if cached:
                previews[quoted_id] = cached
        return previews

    async def _query_quote_preview(
        self,
        quoted_id: str,
        self_id: str,
        self_name: str,
    ) -> str:
        """向协议端取回被引用消息并渲染为单行摘要。

        :param quoted_id: 被引用消息的平台 ID。
        :param self_id: 机器人登录 QQ 号。
        :param self_name: 机器人显示名。
        :return: 形如 ``回复 某人：原文`` 的摘要；查询失败或正文为空时返回空字符串。
        副作用：调用一次 ``get_msg``，成功时写入摘要缓存。
        """
        try:
            response = await self._transport.call_action('get_msg', {'message_id': quoted_id})
        except Exception as exc:
            # 被引用消息可能已被撤回或超出协议端保留窗口；正文退回占位符，
            # 消息本身照常入站。
            logger.warning(
                'QQ 引用消息还原失败，保留占位符',
                messageId=quoted_id,
                error=str(exc),
            )
            return ''
        data = response.get('data')
        if not isinstance(data, Mapping):
            return ''

        sender = data.get('sender')
        sender_id = ''
        sender_name = ''
        if isinstance(sender, Mapping):
            sender_id = _optional_text(sender.get('user_id'))
            sender_name = (
                _optional_text(sender.get('card'))
                or _optional_text(sender.get('nickname'))
            )
        if sender_id == self_id:
            sender_name = self_name
        if not sender_name:
            sender_name = sender_id or '某人'

        segments = data.get('message')
        if isinstance(segments, list):
            body = message_to_text(segments, {self_id: self_name})
        else:
            # 部分协议端按 CQ 码字符串返回历史消息，此时只有 raw_message 可用。
            body = _optional_text(data.get('raw_message'))
        body = ' '.join(body.split())
        if not body:
            return ''
        if len(body) > QUOTE_PREVIEW_LIMIT:
            body = f'{body[:QUOTE_PREVIEW_LIMIT]}…'

        preview = f'回复 {sender_name}：{body}'
        self._quote_previews[quoted_id] = preview
        while len(self._quote_previews) > RESOLUTION_CACHE_LIMIT:
            self._quote_previews.pop(next(iter(self._quote_previews)))
        return preview

    async def _resolve_inbound_image_sources(
        self,
        payload: Mapping[str, Any],
        event: QqInboundEvent,
    ) -> QqInboundEvent:
        """把 gchat.qpic.cn 图片来源优先解析为 NapCat 本地文件引用。

        QQ CDN 对机器人进程的裸下载请求会返回 ``invalid fileid`` / ``download url
        has expired``；NapCat 的 ``get_image`` 动作能直接返回同一张图在 QQ 数据目录
        中的本地绝对路径，读取本地文件不受防盗链和链接时效影响。该步骤只解析路径，
        不在适配器侧读取图片字节，继续由主体后台下载与识别。

        :param payload: OneBot 原始消息事件，用于按段提取 ``data.file``。
        :param event: 已解析的入站事件；图片来源顺序与正文占位符一致。
        :return: 可能替换普通图片和表情包来源的新事件；解析失败时保持原值。
        副作用：仅对 QQ CDN 来源发起 ``get_image`` 动作，不读取或下载图片内容。
        """
        if not event.image_sources and not event.emoji_sources:
            return event
        raw_segments = payload.get('message')
        if not isinstance(raw_segments, list):
            return event

        # 普通图片和表情包分别保持与各自来源数组相同的协议顺序。
        image_file_names: List[str] = []
        emoji_file_names: List[str] = []
        for segment in raw_segments:
            if not isinstance(segment, Mapping) or segment.get('type') != 'image':
                continue
            data = segment.get('data')
            if not isinstance(data, Mapping):
                continue
            target = emoji_file_names if is_emoji_image(segment) else image_file_names
            target.append(str(data.get('file') or '').strip())
        if (
            len(image_file_names) != len(event.image_sources)
            or len(emoji_file_names) != len(event.emoji_sources)
        ):
            return event

        resolved_images = await asyncio.gather(*[
            self._resolve_image_source(source, file_name)
            for source, file_name in zip(event.image_sources, image_file_names)
        ])
        resolved_emojis = await asyncio.gather(*[
            self._resolve_image_source(source, file_name)
            for source, file_name in zip(event.emoji_sources, emoji_file_names)
        ])
        return replace(
            event,
            image_sources=tuple(resolved_images),
            emoji_sources=tuple(resolved_emojis),
        )

    async def _resolve_forward_messages(
        self,
        payload: Mapping[str, Any],
        event: QqInboundEvent,
    ) -> QqInboundEvent:
        """把顶层 ``forward`` 段解析为包含全部嵌套层级的消息树。

        协议事件通常只带转发资源编号，此时每个根转发调用一次
        ``get_forward_msg``；若事件已经内联 ``data.content``，直接解析而不重复
        请求。任一根解析失败时整条消息仍以 ``[转发消息]`` 占位入站，但不暴露
        半棵树给工具，避免多根转发的路径编号错位。
        """
        raw_segments = payload.get('message')
        if not isinstance(raw_segments, list):
            return event
        forward_segments = [
            segment
            for segment in raw_segments
            if isinstance(segment, Mapping) and segment.get('type') == 'forward'
        ]
        if not forward_segments:
            return event

        trees: List[ForwardMessageTree] = []
        for root_index, segment in enumerate(forward_segments):
            data = segment.get('data')
            try:
                if not isinstance(data, Mapping):
                    raise ValueError('顶层合并转发缺少对象类型的 data')
                inline_content = data.get('content')
                if inline_content is not None:
                    if not isinstance(inline_content, list):
                        raise ValueError('顶层合并转发的 data.content 必须是数组')
                    trees.append(parse_forward_content(inline_content))
                    continue
                forward_id = str(data.get('id') or '').strip()
                if not forward_id:
                    raise ValueError('顶层合并转发缺少 data.id')
                response = await self._transport.call_action(
                    'get_forward_msg',
                    {'message_id': forward_id},
                )
                trees.append(parse_forward_response(response))
            except (ActionError, asyncio.TimeoutError, ValueError) as exc:
                logger.warning(
                    'QQ 合并转发解析失败，保留正文占位且不开放读取工具',
                    messageId=event.external_message_id,
                    rootIndex=root_index,
                    forwardId=(
                        str(data.get('id') or '').strip()
                        if isinstance(data, Mapping)
                        else ''
                    ),
                    error=str(exc),
                )
                return event
        return replace(event, forward_messages=tuple(trees))

    async def _resolve_image_source(self, source: str, file_name: str) -> str:
        """把单张 QQ CDN 图片来源解析为本地 ``file://`` 引用。

        :param source: 入站事件中的图片来源 URL 或本地引用。
        :param file_name: 图片消息段的 ``data.file`` 文件名。
        :return: 解析成功时为 NapCat 返回绝对路径的 ``file://`` URI；非 QQ CDN、
            缺少文件名或解析失败时返回原始 ``source``。
        副作用：仅对 ``*.qpic.cn`` 来源调用 ``get_image`` 动作。
        """
        if not file_name or not source.startswith(('http://', 'https://')):
            return source
        host = (urlsplit(source).hostname or '').lower()
        if host != 'qpic.cn' and not host.endswith('.qpic.cn'):
            return source

        try:
            response = await self._transport.call_action(
                'get_image',
                {'file': file_name},
            )
        except Exception as exc:
            # 路径解析只是来源优化；失败时保留远程 URL，让主体按原 HTTP 来源处理，
            # 避免单张图片的协议端查询错误阻断整条消息入站。
            logger.warning(
                'QQ 图片本地路径解析失败，保留远程来源',
                file=file_name,
                error=str(exc),
            )
            return source

        data = response.get('data')
        if not isinstance(data, Mapping):
            return source
        local_path = str(data.get('file') or '').strip()
        if not local_path:
            return source
        path = Path(local_path)
        if not path.is_absolute():
            return source
        try:
            return path.as_uri()
        except ValueError as exc:
            logger.warning(
                'QQ 图片本地路径无法转换为 file URI，保留远程来源',
                file=file_name,
                path=local_path,
                error=str(exc),
            )
            return source

    async def _consume_protocol_events(self, self_id: str, self_name: str) -> None:
        """消费协议端事件并提交通过访问策略的入站消息。

        :param self_id: 协议端登录 QQ 号，用于忽略机器人自身消息和识别提及。
        :param self_name: 协议端登录昵称，用于生成入站事件中的机器人名称。
        :return: 仅在事件迭代器结束时返回。
        :raises Exception: 事件结构非法、主体提交失败且未被本方法捕获，或传输层断开。
        副作用：持续读取协议事件，并通过 HTTP 写入主体后端；拒绝的消息只记录日志。
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
            if kind == 'input_status':
                # 对方正在打字只是一条瞬时事实，提交失败不影响任何消息通路。
                try:
                    spoke = await self._backend.submit_typing(
                        _required_text(payload.get('user_id'), 'user_id 不能为空'),
                    )
                    if spoke:
                        logger.info(
                            'QQ 输入状态触发私聊追问',
                            userId=payload.get('user_id'),
                        )
                    else:
                        logger.debug(
                            '已检测 QQ 私聊输入状态，本次无需追问',
                            userId=payload.get('user_id'),
                        )
                except Exception as exc:
                    # 该支路不阻断普通消息，但不能静默忽略故障，否则现场只会表现为
                    # 「输入状态完全没生效」，无法判断断在协议端还是主体条件判断。
                    logger.warning(
                        '提交 QQ 输入状态失败',
                        userId=payload.get('user_id'),
                        error=str(exc),
                    )
                continue
            if kind == 'poke':
                # 通知不带昵称与群名片，必须先查询一次成员信息再提交：主体的
                # set_group_card 将空串视为「清除名片」，以空值提交会清除发起者
                # 已存的群名片。查询失败时仅记日志不提交：放弃本次入站，
                # 不以空名字写入人物档案。
                poke_group_id = _optional_text(payload.get('group_id'))
                poke_user_id = _optional_text(payload.get('user_id'))
                nickname, group_card = await self._query_member_identity(
                    poke_group_id, poke_user_id,
                )
                if not nickname:
                    logger.warning(
                        'QQ 戳一戳无法解析发起者，已跳过',
                        groupId=poke_group_id,
                        senderId=poke_user_id,
                    )
                    continue
                poke_event = build_poke_inbound_event(
                    payload, self_name, nickname, group_card,
                )
                logger.info(
                    '收到 QQ 戳一戳',
                    groupId=poke_group_id,
                    senderId=poke_user_id,
                    senderName=group_card or nickname,
                    text=poke_event.text,
                )
                try:
                    await self._backend.submit_inbound(poke_event)
                except (httpx.HTTPError, ValueError) as exc:
                    # 与普通消息同口径：单条提交失败只丢这一条，不拆连接。
                    logger.error(
                        'QQ 戳一戳提交失败',
                        streamExternalId=poke_event.stream_external_id,
                        error=str(exc),
                    )
                continue
            if kind == 'emoji_like':
                # 回应通知不带昵称与群名片，且不携带目标消息的发送者：先确认
                # 被贴表情的是不是 Bot 的消息（协议端只推「群里有回应」，谁的都推），
                # 再查发起者成员信息，两步与戳一戳同口径——查不到就只记日志不提交。
                like_group_id = _optional_text(payload.get('group_id'))
                like_user_id = _optional_text(payload.get('user_id'))
                target_message_id = _optional_text(payload.get('message_id'))
                if not target_message_id or not await self._reacted_message_is_mine(
                    target_message_id, self_id,
                ):
                    logger.debug(
                        '忽略给别人的消息贴的表情回应',
                        groupId=like_group_id,
                        targetMessageId=target_message_id,
                    )
                    continue
                nickname, group_card = await self._query_member_identity(
                    like_group_id, like_user_id,
                )
                if not nickname:
                    logger.warning(
                        'QQ 表情回应无法解析发起者，已跳过',
                        groupId=like_group_id,
                        senderId=like_user_id,
                        targetMessageId=target_message_id,
                    )
                    continue
                like_event = build_emoji_like_inbound_event(
                    payload, self_name, nickname, group_card,
                )
                logger.info(
                    '收到 QQ 表情回应',
                    groupId=like_group_id,
                    senderId=like_user_id,
                    senderName=group_card or nickname,
                    targetMessageId=target_message_id,
                    isAdd=payload.get('is_add') is not False,
                    likes=payload.get('likes'),
                    text=like_event.text,
                )
                try:
                    await self._backend.submit_inbound(like_event)
                except (httpx.HTTPError, ValueError) as exc:
                    # 与普通消息同口径：单条提交失败只丢这一条，不拆连接。
                    logger.error(
                        'QQ 表情回应提交失败',
                        streamExternalId=like_event.stream_external_id,
                        error=str(exc),
                    )
                continue
            if kind != 'message':
                # 未处理事件此前记在 debug，而文件与控制台都不落 debug，
                # 现场表现为「协议推了但一行痕迹都没有」，无法判断事件到没到适配器。
                logger.info(
                    '忽略未处理的 QQ 事件',
                    postType=payload.get('post_type'),
                    noticeType=payload.get('notice_type'),
                    subType=payload.get('sub_type'),
                    groupId=payload.get('group_id'),
                    senderId=payload.get('user_id'),
                )
                continue

            # 提及显示名与引用摘要都要在渲染正文之前备好，否则模型只能看到裸
            # QQ 号和不含内容的引用占位符。两者都只在解析失败时退回原占位形态。
            raw_segments = payload.get('message')
            raw_segments = raw_segments if isinstance(raw_segments, list) else []
            event = parse_inbound_event(
                payload,
                self_id,
                self_name,
                self._config.owner.qq,
                self._config.private,
                self._config.group,
                await self._resolve_mention_names(payload, raw_segments, self_id),
                await self._resolve_quote_previews(raw_segments, self_id, self_name),
            )
            if event is None:
                continue
            if not event.text.strip():
                logger.info('忽略空 QQ 消息', messageId=event.external_message_id)
                continue
            # QQ CDN 来源先由协议端解析成本地路径；仍然只提交来源引用，
            # 实际读取与 VLM 描述由主体后台完成，避免逐张下载阻塞串行入站循环。
            event = await self._resolve_inbound_image_sources(payload, event)
            # 合并转发树只解析结构，不下载媒体；解析完成后与消息一起提交主体，
            # 主体再按内部消息 ID 建立会话隔离的逐层读取缓存。
            event = await self._resolve_forward_messages(payload, event)
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
        副作用：持续读取主体 WebSocket，并向协议端发送 action；单条发送失败只记录日志。
        """
        async for outbound in self._backend.iter_outbound():
            if isinstance(outbound, BackendReaction):
                await self._apply_reaction(outbound)
                continue
            if isinstance(outbound, BackendPoke):
                await self._apply_poke(outbound)
                continue
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
                # 文字与表情包分别调用 action，让 QQ 生成独立消息气泡；先完整组装
                # 所有批次，避免元数据错误发生在文字已经发送之后。
                message_batches = outbound_message_batches(
                    outbound.segments,
                    outbound.emoji_refs,
                    outbound.emoji_sub_types,
                    outbound.quote_external_message_id,
                )
                target = _qq_number(outbound.stream_external_id, target_label)
                delays = _batch_delays_seconds(outbound)
                for index, message_segments in enumerate(message_batches):
                    # 打字节奏由主体按人格配置算好，适配器只负责照做；首项恒为 0，
                    # 因为模型生成本身已经占了十几秒，Bot 在对方视角里早就在打字了。
                    #
                    # 等待发生在出站消费循环内，会顺带推迟其它 stream 的这一轮
                    # 投递。选择阻塞而不是并发发送，是为了保住同一 stream 内的
                    # 气泡顺序；单轮总延迟在十秒量级，对聊天节奏可以接受。
                    if index < len(delays) and delays[index] > 0:
                        await asyncio.sleep(delays[index])
                    await self._transport.call_action(
                        action,
                        {
                            target_field: target,
                            'message': message_segments,
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

    async def _apply_poke(self, poke: BackendPoke) -> None:
        """在群里戳一戳指定成员。

        :param poke: 主体下发的戳一戳，含群号与被戳者 QQ 号。
        :return: ``None``。
        副作用：向协议端发送一次 action；发送失败只记录日志，不影响后续出站。
        """
        try:
            group_id = _qq_number(poke.stream_external_id, '戳一戳群')
            user_id = _qq_number(poke.target_external_id, '戳一戳目标')
        except ValueError as exc:
            logger.error(
                'QQ 戳一戳参数非法',
                streamId=poke.stream_id,
                targetId=poke.target_external_id,
                error=str(exc),
            )
            return
        try:
            await self._transport.call_action(
                'group_poke', {'group_id': group_id, 'user_id': user_id},
            )
        except (ActionError, asyncio.TimeoutError) as exc:
            # 记实际发出的群号而不只是内部 stream 编号：失败归因需要区分「群号
            # 解析错了」和「协议端拒绝了正确的调用」，只有内部编号时两者无法分辨。
            logger.error(
                'QQ 戳一戳失败',
                streamId=poke.stream_id,
                groupId=group_id,
                targetId=poke.target_external_id,
                error=str(exc),
            )

    async def _apply_reaction(self, reaction: BackendReaction) -> None:
        """给一条已有消息贴上表情回应。

        表情回应不产生新消息，因此不参与打字节奏，也不需要引用或分批：它就是
        一次 `set_msg_emoji_like` 调用。

        :param reaction: 主体下发的表情回应，含被回应消息的平台编号与语义标识。
        :return: ``None``。
        :raises ValueError: 语义标识不在映射表内，或消息编号不是合法数字。
        副作用：向协议端发送一次 action；发送失败只记录日志，不影响后续出站。
        """
        try:
            emoji_id = reaction_emoji_id(reaction.reaction)
            message_id = _qq_number(reaction.target_external_message_id, '表情回应目标消息')
        except ValueError as exc:
            # 映射缺失或编号非法属于协议不同步，必须留下明确记录而不是静默跳过。
            logger.error(
                'QQ 表情回应参数非法',
                streamId=reaction.stream_id,
                reaction=reaction.reaction,
                targetMessageId=reaction.target_external_message_id,
                error=str(exc),
            )
            return
        try:
            await self._transport.call_action(
                'set_msg_emoji_like',
                {'message_id': message_id, 'emoji_id': emoji_id},
            )
        except (ActionError, asyncio.TimeoutError) as exc:
            logger.error(
                'QQ 表情回应失败',
                streamId=reaction.stream_id,
                streamKind=reaction.stream_kind,
                targetId=reaction.stream_external_id,
                targetMessageId=reaction.target_external_message_id,
                error=str(exc),
            )


def _required_text(value: Any, message: str) -> str:
    """把协议字段规范化为非空字符串标识。

    :param value: OneBot 事件里的数字或字符串标识。
    :param message: 校验失败时使用的错误说明。
    :return: 去除首尾空白后的字符串。
    :raises ValueError: 值为空或去除空白后为空字符串。
    """
    text = str(value or '').strip()
    if not text:
        raise ValueError(message)
    return text


def _optional_text(value: Any) -> str:
    """把可能缺失的协议字段规范化为字符串，缺失时返回空字符串。

    :param value: OneBot 事件或 action 响应里的数字、字符串或 ``None``。
    :return: 去除首尾空白后的字符串；值为空时返回空字符串。
    """
    return str(value or '').strip()


def _batch_delays_seconds(outbound: BackendOutbound) -> List[float]:
    """把主体下发的逐条停顿对齐到实际的发送批次顺序。

    主体按「文字在前、表情包在后」的批次顺序下发停顿；未下发时按 0 处理，
    保证适配器在协议缺省下仍能发出全部消息。

    :param outbound: 主体下发的一条出站消息。
    :return: 与发送批次等长的等待秒数列表；主体未下发停顿时全为 0。
    """
    delays = [value / 1000 for value in outbound.batch_delays_ms]
    total = len(outbound.segments) + len(outbound.emoji_refs)
    return delays + [0.0] * (total - len(delays))


def _parse_group_history(
    response: Dict[str, Any],
    group_id: str,
    self_id: str,
    self_name: str,
    owner_qq: str,
    private_access: Any,
    group_access: Any,
) -> List[Dict[str, Any]]:
    """把 ``get_group_msg_history`` 响应转换为可回填的观察消息列表。

    提及的显示名直接用这批历史自带的发送者信息解析，不再逐个查协议端：回填是
    启动期的批量动作，为每个 ``@`` 发一次查询会把启动拖成几十次串行往返。这批
    历史里没出现过的号码保持裸号，引用同理只保留占位符。
    """
    data = response.get('data')
    if isinstance(data, dict):
        messages = data.get('messages', [])
    elif isinstance(data, list):
        messages = data
    else:
        return []
    if not isinstance(messages, list):
        return []

    mention_names: Dict[str, str] = {}
    for entry in messages:
        if not isinstance(entry, dict):
            continue
        sender = entry.get('sender')
        if not isinstance(sender, Mapping):
            continue
        user_id = _optional_text(sender.get('user_id'))
        name = _optional_text(sender.get('card')) or _optional_text(sender.get('nickname'))
        if user_id and name:
            mention_names[user_id] = name

    backfill: List[Dict[str, Any]] = []
    for entry in messages:
        if not isinstance(entry, dict):
            continue
        raw = dict(entry)
        raw['post_type'] = 'message'
        raw['message_type'] = 'group'
        raw['group_id'] = group_id
        raw['self_id'] = self_id
        raw['message_id'] = entry.get('message_id') or entry.get('messageId') or ''
        raw['message'] = entry.get('message') or []
        raw['sender'] = entry.get('sender') or {'user_id': entry.get('user_id')}
        try:
            event = parse_inbound_event(
                raw,
                self_id,
                self_name,
                owner_qq,
                private_access,
                group_access,
                mention_names,
            )
        except ValueError:
            continue
        if event is None or not event.text.strip():
            continue
        timestamp = entry.get('time') or entry.get('timestamp') or 0
        try:
            timestamp_ms = int(timestamp)
        except (TypeError, ValueError):
            timestamp_ms = 0
        if timestamp_ms < 10_000_000_000:
            timestamp_ms *= 1000
        try:
            message_seq = int(entry.get('message_seq') or entry.get('messageSeq') or 0)
        except (TypeError, ValueError):
            message_seq = 0
        backfill.append({
            'externalMessageId': event.external_message_id,
            'messageSeq': message_seq,
            'createdAt': timestamp_ms,
            'senderExternalId': event.sender_external_id,
            'senderNickname': event.sender_nickname,
            'senderGroupCard': event.sender_group_card,
            'text': event.text,
            'mentionedMe': event.mentioned_me,
        })
    # 历史接口通常按新到旧返回，落库前按 seq 升序恢复时间顺序。
    backfill.sort(key=lambda item: (item['messageSeq'], item['createdAt']))
    return backfill


class SelfQqMismatch(RuntimeError):
    """配置里的 self_qq 与协议端实际登录的号不一致。"""


def _check_self_qq_matches(configured: str, actual_self_id: str) -> None:
    """校验配置的机器人 QQ 号与协议端实际登录身份一致。

    :param configured: 配置文件中的 ``napcat.self_qq``。
    :param actual_self_id: 协议端 ``get_login_info`` 返回的登录 QQ 号。

    :raises SelfQqMismatch: 两个身份标识不一致。

    副作用：
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
    副作用：不执行 I/O。
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
    副作用：不修改异常对象或适配器状态。
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

    :param interval_sec: 配置的基础重连间隔，单位为秒；应为正数。
    :param retry_count: 从 ``1`` 开始的重试次数；小于 ``1`` 时按指数表达式的实际结果计算。

    :return: ``interval_sec`` 乘以 ``2 ** (retry_count - 1)``，放大倍数上限为 ``32``。

    :raises TypeError: 参数不是支持乘法、减法和幂运算的数值时抛出。
    """
    return interval_sec * min(2 ** (retry_count - 1), 32)
