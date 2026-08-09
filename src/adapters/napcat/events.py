"""OneBot v11 协议事件到 QQ 入站结构的纯解析。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from .config import GroupAccessConfig, PrivateAccessConfig
from .segments import mentions_user, message_to_text


EventKind = Literal[
    'action_response',
    'heartbeat',
    'request',
    'self_message',
    'private_denied',
    'group_denied',
    'message',
    'other',
]


@dataclass(frozen=True)
class QqInboundEvent:
    """适配器准备提交给主体 /platform/inbound 的字段。"""

    stream_kind: Literal['direct', 'group']
    stream_external_id: str
    sender_external_id: str
    sender_name: str
    bot_name: str
    text: str
    mentioned_me: bool
    external_message_id: str


def is_action_response(payload: Mapping[str, Any]) -> bool:
    """判断是不是我们发出去的请求的回复（带非空 echo）。"""
    echo = payload.get('echo')
    return isinstance(echo, str) and bool(echo.strip())


def is_heartbeat(payload: Mapping[str, Any]) -> bool:
    """识别协议端探活事件，供运行器静默丢弃。"""
    return payload.get('meta_event_type') == 'heartbeat'


def classify_event(
    payload: Mapping[str, Any],
    self_id: str,
    owner_qq: str,
    private_access: PrivateAccessConfig,
    group_access: GroupAccessConfig,
) -> EventKind:
    """给运行器一个可记录的事件判定，不执行任何 I/O。"""
    if is_action_response(payload):
        return 'action_response'
    if is_heartbeat(payload):
        return 'heartbeat'
    post_type = payload.get('post_type')
    if post_type == 'request':
        return 'request'
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
    """解析允许的私聊或白名单群消息；其余类型返回 None 交给运行器记录。"""
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
    group_id = payload.get('group_id')
    stream_kind: Literal['direct', 'group'] = 'group' if group_id is not None else 'direct'
    stream_external_id = (
        _required_identifier(group_id, 'group_id 不能为空')
        if group_id is not None
        else sender_id
    )
    sender = payload.get('sender')
    sender_name = ''
    if isinstance(sender, Mapping):
        sender_name = _string_value(sender.get('card')) or _string_value(sender.get('nickname'))
    if not sender_name:
        sender_name = sender_id
    return QqInboundEvent(
        stream_kind=stream_kind,
        stream_external_id=stream_external_id,
        sender_external_id=sender_id,
        sender_name=sender_name,
        bot_name=_required_identifier(self_name, '机器人登录昵称不能为空'),
        text=message_to_text(raw_segments, {self_id: self_name}),
        mentioned_me=mentions_user(raw_segments, self_id),
        external_message_id=message_id,
    )


def _sender_external_id(payload: Mapping[str, Any]) -> str:
    sender = payload.get('sender')
    sender_id = sender.get('user_id') if isinstance(sender, Mapping) else None
    if sender_id is None:
        sender_id = payload.get('user_id')
    return _required_identifier(sender_id, '消息缺少 user_id')


def _required_identifier(value: Any, message: str) -> str:
    normalized = _string_value(value)
    if not normalized:
        raise ValueError(message)
    return normalized


def _string_value(value: Any) -> str:
    if value is None:
        return ''
    return str(value).strip()
