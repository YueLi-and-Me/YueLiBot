"""把 OneBot v11 协议事件解析为 QQ 适配器使用的入站结构。

本模块只执行事件分类、访问策略判断和字段归一化，不建立网络连接，也不提交
消息；`QqInboundEvent` 是解析结果，运行器据此调用主体后端接口。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from .config import GroupAccessConfig, PrivateAccessConfig
from .segments import (
    emoji_source_urls,
    emoji_sub_types,
    image_source_urls,
    mentions_user,
    message_to_text,
)


EventKind = Literal[
    'action_response',
    'heartbeat',
    'request',
    'self_message',
    'private_denied',
    'group_denied',
    'message',
    # 私聊输入状态：对方在输入框打字时协议端会持续推送，用于催促类主动发言。
    'input_status',
    'other',
]


@dataclass(frozen=True)
class QqInboundEvent:
    """适配器准备提交给主体 /platform/inbound 的字段。"""

    stream_kind: Literal['direct', 'group']
    stream_external_id: str
    sender_external_id: str
    sender_nickname: str
    sender_group_card: str
    bot_name: str
    text: str
    mentioned_me: bool
    external_message_id: str
    # 普通图片下载来源；顺序与 text 中的 [图片] 占位符一致。
    # 适配器只传来源引用，下载与 VLM 描述由主体后台执行，避免阻塞串行入站循环。
    image_sources: tuple[str, ...] = ()
    # 表情包来源单独对齐 [表情包] 占位符，主体使用情绪标签提示词识别。
    emoji_sources: tuple[str, ...] = ()
    # 与 emoji_sources 逐项对齐；主体登记后会在再次发送时还原给 OneBot。
    emoji_sub_types: tuple[int, ...] = ()


def is_action_response(payload: Mapping[str, Any]) -> bool:
    """判断协议事件是否为带非空 ``echo`` 的 action 响应。

    :param payload: 已解析的 OneBot 事件映射。

    :return: ``echo`` 为非空字符串时返回 ``True``，否则返回 ``False``。

    副作用：
        仅读取事件字段，不修改输入映射。
    """
    echo = payload.get('echo')
    return isinstance(echo, str) and bool(echo.strip())


def is_heartbeat(payload: Mapping[str, Any]) -> bool:
    """判断协议事件是否为 ``meta_event_type=heartbeat`` 探活事件。

    :param payload: 已解析的 OneBot 事件映射。

    :return: ``meta_event_type`` 等于 ``heartbeat`` 时返回 ``True``，否则返回 ``False``。

    副作用：
        仅读取事件字段，不修改输入映射。
    """
    return payload.get('meta_event_type') == 'heartbeat'


def is_input_status(payload: Mapping[str, Any]) -> bool:
    """判断协议事件是否为私聊输入状态通知。

    协议端在对方于输入框打字期间反复推送该通知，且不提供「停止输入」的对应
    事件，因此调用方只能把它当作「此刻对方正在打字」的瞬时事实，不能当作可以
    持续查询的状态。

    :param payload: 已解析的 OneBot 事件映射。
    :return: ``notice_type=notify`` 且 ``sub_type=input_status`` 时返回 ``True``。
    """
    return (
        payload.get('post_type') == 'notice'
        and payload.get('notice_type') == 'notify'
        and payload.get('sub_type') == 'input_status'
    )


def classify_event(
    payload: Mapping[str, Any],
    self_id: str,
    owner_qq: str,
    private_access: PrivateAccessConfig,
    group_access: GroupAccessConfig,
) -> EventKind:
    """根据协议类型、机器人身份和访问策略分类单个入站事件。

    :param payload: 已解析的 OneBot 事件映射。
    :param self_id: 机器人登录 QQ 号。
    :param owner_qq: 配置的 owner QQ 号。
    :param private_access: 私聊访问策略模型。
    :param group_access: 群聊白名单策略模型。

    :return: 事件种类标识，包括 action 响应、心跳、请求、自身消息、拒绝消息、普通消息
        和其他未处理类型。

    :raises ValueError: 消息事件缺少必要的身份字段或字段无法规范化。

    副作用：
        仅读取事件和访问策略，不执行网络、持久化或消息提交操作。
    """
    if is_action_response(payload):
        return 'action_response'
    if is_heartbeat(payload):
        return 'heartbeat'
    post_type = payload.get('post_type')
    if post_type == 'request':
        return 'request'
    if post_type == 'notice':
        if not is_input_status(payload):
            return 'other'
        # 输入状态只有私聊会推送，访问名单与私聊消息完全一致。
        if not private_access.allows(
            _sender_external_id(payload),
            _required_identifier(owner_qq, 'owner_qq 不能为空'),
        ):
            return 'private_denied'
        return 'input_status'
    if post_type != 'message':
        return 'other'

    sender_id = _sender_external_id(payload)
    if sender_id == _required_identifier(self_id, 'self_id 不能为空'):
        return 'self_message'
    group_id = payload.get('group_id')
    if group_id is not None:
        normalized_group_id = _required_identifier(group_id, 'group_id 不能为空')
        if not group_access.allows(normalized_group_id):
            return 'group_denied'
        return 'message'
    if not private_access.allows(
        sender_id,
        _required_identifier(owner_qq, 'owner_qq 不能为空'),
    ):
        return 'private_denied'
    return 'message'


def parse_inbound_event(
    payload: Mapping[str, Any],
    self_id: str,
    self_name: str,
    owner_qq: str,
    private_access: PrivateAccessConfig,
    group_access: GroupAccessConfig,
) -> QqInboundEvent | None:
    """解析通过访问策略的私聊或白名单群消息为统一入站事件。

    :param payload: 已解析的 OneBot 消息事件映射。
    :param self_id: 机器人登录 QQ 号，用于识别自身消息和提及。
    :param self_name: 机器人在平台上的显示名称。
    :param owner_qq: 配置的 owner QQ 号。
    :param private_access: 私聊访问策略模型。
    :param group_access: 群聊白名单策略模型。

    :return: 规范化后的 ``QqInboundEvent``；事件不是允许处理的普通消息时返回 ``None``。

    :raises ValueError: 消息缺少发送者、消息 ID、消息段列表或必要字段类型不正确。

    副作用：
        仅读取和转换输入事件，不执行网络请求或持久化操作。
    """
    # 先执行事件分类，拒绝消息、机器人自身消息和不支持类型不进入字段解析流程。
    if classify_event(
        payload,
        self_id,
        owner_qq,
        private_access,
        group_access,
    ) != 'message':
        return None

    sender_id = _sender_external_id(payload)
    message_id = _required_identifier(payload.get('message_id'), 'message_id 不能为空')
    raw_segments = payload.get('message')
    if not isinstance(raw_segments, list):
        raise ValueError('message 必须是 array 格式的消息段列表')
    # group_id 同时决定 stream 类型和外部标识；私聊使用发送者外部号作为 stream 标识。
    group_id = payload.get('group_id')
    stream_kind: Literal['direct', 'group'] = 'group' if group_id is not None else 'direct'
    stream_external_id = (
        _required_identifier(group_id, 'group_id 不能为空')
        if group_id is not None
        else sender_id
    )
    sender = payload.get('sender')
    sender_nickname = ''
    sender_group_card = ''
    if isinstance(sender, Mapping):
        # 群名片只属于当前群 stream，不能覆盖跨平台 identity 的账号昵称。
        sender_nickname = _string_value(sender.get('nickname'))
        if stream_kind == 'group':
            sender_group_card = _string_value(sender.get('card'))
    if not sender_nickname:
        sender_nickname = sender_id
    return QqInboundEvent(
        stream_kind=stream_kind,
        stream_external_id=stream_external_id,
        sender_external_id=sender_id,
        sender_nickname=sender_nickname,
        sender_group_card=sender_group_card,
        bot_name=_required_identifier(self_name, '机器人登录昵称不能为空'),
        text=message_to_text(raw_segments, {self_id: self_name}),
        mentioned_me=mentions_user(raw_segments, self_id),
        external_message_id=message_id,
        image_sources=image_source_urls(raw_segments),
        emoji_sources=emoji_source_urls(raw_segments),
        emoji_sub_types=emoji_sub_types(raw_segments),
    )


def _sender_external_id(payload: Mapping[str, Any]) -> str:
    """从 sender.user_id 或事件级 user_id 提取发送者外部标识。

    :param payload: 已解析的 OneBot 事件对象。
    :return: 去除空白后的发送者 QQ 号或其他协议标识。
    :raises ValueError: 两个候选字段都缺失或为空。
    副作用：不修改事件对象。
    """
    sender = payload.get('sender')
    sender_id = sender.get('user_id') if isinstance(sender, Mapping) else None
    if sender_id is None:
        sender_id = payload.get('user_id')
    return _required_identifier(sender_id, '消息缺少 user_id')


def _required_identifier(value: Any, message: str) -> str:
    """把协议字段规范化为非空字符串。

    :param value: 原始协议字段，可以是数字、字符串或 `None`。
    :param message: 字段缺失时用于构造异常的中文错误信息。
    :return: `str(value).strip()` 的结果。
    :raises ValueError: 规范化结果为空。
    副作用：不修改输入值。
    """
    normalized = _string_value(value)
    if not normalized:
        raise ValueError(message)
    return normalized


def _string_value(value: Any) -> str:
    """把可选协议值转换为空安全的去空白字符串。

    :param value: 任意协议值；`None` 被视为空字符串。
    :return: 非 `None` 值的字符串表示去除首尾空白后的结果。
    副作用：不执行 I/O，也不抛出本函数主动定义的异常。
    """
    if value is None:
        return ''
    return str(value).strip()
