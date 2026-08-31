"""把 OneBot v11 协议事件解析为 QQ 适配器使用的入站结构。

本模块只执行事件分类、访问策略判断和字段归一化，不建立网络连接，也不提交
消息；`QqInboundEvent` 是解析结果，运行器据此调用主体后端接口。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Tuple

from src.core.platform_io.forward import ForwardMessageTree

from .config import GroupAccessConfig, PrivateAccessConfig
from .qq_faces import face_name
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
    # 有人戳了 Bot 自己。协议按 notice/notify/poke 推送，只有 target_id 指向
    # 登录账号时才归入本类；戳别人与 Bot 戳出去的回显都不算。
    'poke',
    # 有人给群消息贴表情回应。协议按 notice/group_msg_emoji_like 推送，
    # 目标是不是 Bot 自己发的消息由运行器查协议端确认，本类只表示「是回应通知」。
    'emoji_like',
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
    # 本条是「有人戳了 Bot」而不是普通消息。戳一戳没有正文也没有 @，正文里
    # 没有任何可供门控识别的信号，因此把这件事作为独立事实提交给主体门控。
    poked_me: bool = False
    # 本条是「有人给 Bot 发的消息贴了表情回应」。与戳一戳同口径：没有正文，
    # 门控靠独立事实抬入 DELIBERATE；贴表情非常频繁，绝不 FORCE。
    emoji_liked_me: bool = False
    # ``forward`` 段经 get_forward_msg 解析后的完整根树；正文仍保留稳定占位符，
    # 主体用内部消息编号和路径按需读取。
    forward_messages: Tuple[ForwardMessageTree, ...] = ()


def build_poke_inbound_event(
    payload: Mapping[str, Any],
    bot_name: str,
    sender_nickname: str,
    sender_group_card: str,
) -> QqInboundEvent:
    """把一条戳 Bot 的通知转换为可提交的入站事件。

    戳一戳在协议上是 notice 而不是 message：没有正文，也没有平台消息编号。
    因此正文由适配器合成，``external_message_id`` 留空——主体侧「不带编号的通道」
    是已支持的形态，引用逻辑会据此拒绝把它当作引用目标。

    昵称与群名片必须由调用方先查询后传入，不能留空：主体的 ``set_group_card``
    把空串视为「清除名片」，用空值提交会把发起者已存的群名片抹掉。

    :param payload: 已确认为戳 Bot 的 OneBot 通知映射。
    :param bot_name: 机器人显示名，用于渲染正文里的被戳对象。
    :param sender_nickname: 发起者账号昵称，不能为空。
    :param sender_group_card: 发起者在本群的名片；私聊为空字符串。
    :return: 可直接提交给主体入站接口的事件。
    :raises ValueError: 发起者 QQ 号缺失，或昵称为空。
    """
    sender_id = _sender_external_id(payload)
    if not sender_nickname.strip():
        raise ValueError('戳一戳发起者昵称不能为空，需先查询成员信息')
    group_id = payload.get('group_id')
    if group_id is not None:
        stream_kind: Literal['direct', 'group'] = 'group'
        stream_external_id = _required_identifier(group_id, 'group_id 不能为空')
    else:
        stream_kind = 'direct'
        stream_external_id = sender_id
    return QqInboundEvent(
        stream_kind=stream_kind,
        stream_external_id=stream_external_id,
        sender_external_id=sender_id,
        sender_nickname=sender_nickname,
        sender_group_card=sender_group_card,
        bot_name=bot_name,
        # raw_info 是协议端可选携带的展示片段（"戳了戳" / 自定义后缀）；缺失时
        # 退回统一措辞，正文始终写明动作对象，避免 Bot 读成「谁戳了谁」。
        text=f'[{_poke_action_text(payload)}{bot_name}]',
        mentioned_me=False,
        external_message_id='',
        poked_me=True,
    )


def _poke_action_text(payload: Mapping[str, Any]) -> str:
    """从戳一戳通知里取出动作措辞，缺失时退回默认说法。

    :param payload: OneBot 通知映射。
    :return: 形如 ``戳了戳`` 的动作短语，始终非空。
    """
    raw_info = payload.get('raw_info')
    if isinstance(raw_info, list):
        for segment in raw_info:
            if not isinstance(segment, Mapping):
                continue
            # 展示片段按 nudge 动作分段，取第一段非空文案即为动作措辞。
            if segment.get('type') == 'nor':
                text = _string_value(segment.get('txt'))
                if text:
                    return text
    return '戳了戳'


def is_emoji_like(payload: Mapping[str, Any]) -> bool:
    """判断协议事件是否为「给群消息贴表情回应」的通知。

    NapCat 把这类通知放在 post_type=notice、notice_type=group_msg_emoji_like
    下：user_id 是贴表情的人，message_id 是被贴的那条消息，likes 是该消息
    当前的全部回应条目，is_add 区分贴上（true）与取消（false）。目标消息
    是不是 Bot 自己发的，本函数不判断，由运行器查协议端确认。

    :param payload: 已解析的 OneBot 事件映射。
    :return: notice_type 等于 group_msg_emoji_like 时返回 True。
    """
    return (
        payload.get('post_type') == 'notice'
        and payload.get('notice_type') == 'group_msg_emoji_like'
    )


def build_emoji_like_inbound_event(
    payload: Mapping[str, Any],
    bot_name: str,
    sender_nickname: str,
    sender_group_card: str,
) -> QqInboundEvent:
    """把一条「给 Bot 的消息贴表情回应」通知转换为可提交的入站事件。

    回应通知在协议上不是消息：没有正文，也没有平台消息编号。因此正文由适配器
    合成（编号经 face_name 翻成名称，未知编号如实保留占位），
    external_message_id 留空——主体侧「不带编号的通道」是已支持的形态。
    emoji_liked_me 置真，门控据此抬入 DELIBERATE；贴表情非常频繁，绝不 FORCE。

    昵称与群名片必须由调用方先查询后传入，不能留空：主体的 set_group_card
    把空串视为「清除名片」，用空值提交会把发起者已存的群名片抹掉。

    :param payload: 已确认为表情回应且目标消息属于 Bot 的通知映射。
    :param bot_name: 机器人显示名。
    :param sender_nickname: 发起者账号昵称，不能为空。
    :param sender_group_card: 发起者在本群的名片；私聊为空字符串。
    :return: 可直接提交给主体入站接口的事件。
    :raises ValueError: 发起者 QQ 号缺失，或昵称为空。
    """
    sender_id = _sender_external_id(payload)
    if not sender_nickname.strip():
        raise ValueError('表情回应发起者昵称不能为空，需先查询成员信息')
    group_id = payload.get('group_id')
    if group_id is not None:
        stream_kind: Literal['direct', 'group'] = 'group'
        stream_external_id = _required_identifier(group_id, 'group_id 不能为空')
    else:
        stream_kind = 'direct'
        stream_external_id = sender_id
    names = _emoji_like_face_names(payload)
    # is_add 缺省按贴上处理：协议端真实事件恒带该字段，缺省只影响手工构造的测试数据。
    added = payload.get('is_add') is not False
    action = '表情回应' if added else '取消表情回应'
    if names:
        # 未知编号如实保留无名占位，不编名字；与 face 段入站渲染同口径。
        rendered = '、'.join(f'[表情：{name}]' if name else '[表情]' for name in names)
        text = f'[{action}：{rendered}]'
    else:
        text = f'[{action}]'
    return QqInboundEvent(
        stream_kind=stream_kind,
        stream_external_id=stream_external_id,
        sender_external_id=sender_id,
        sender_nickname=sender_nickname,
        sender_group_card=sender_group_card,
        bot_name=bot_name,
        text=text,
        mentioned_me=False,
        external_message_id='',
        emoji_liked_me=True,
    )


def _emoji_like_face_names(payload: Mapping[str, Any]) -> list[str | None]:
    """从回应通知的 likes 列表取出表情编号并翻成名称。

    :param payload: OneBot 表情回应通知映射。
    :return: 与 likes 顺序一致的名称列表；未知编号为 None，likes 缺失时
        返回空列表。
    """
    likes = payload.get('likes')
    if not isinstance(likes, list):
        return []
    names: list[str | None] = []
    for item in likes:
        if not isinstance(item, Mapping):
            continue
        face_id = _string_value(item.get('emoji_id'))
        if not face_id:
            continue
        names.append(face_name(face_id))
    return names


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


def is_poke_at_self(payload: Mapping[str, Any], self_id: str) -> bool:
    """判断协议事件是否为「有人戳了 Bot 自己」的通知。

    协议把戳一戳放在 ``notice/notify/poke`` 下，用 ``user_id`` 表示发起者、
    ``target_id`` 表示被戳者。群聊与私聊共用同一组字段，群聊额外带 ``group_id``。
    只有 ``target_id`` 指向登录账号才算数：群里两个人互戳同样会推送到这里，
    Bot 自己戳出去的回显也会带上自己的 ``user_id``。

    :param payload: 已解析的 OneBot 事件映射。
    :param self_id: 机器人登录 QQ 号，不能为空。
    :return: 事件是戳一戳且被戳者是 Bot 自己时返回 ``True``。
    :raises ValueError: ``self_id`` 为空。
    """
    if (
        payload.get('post_type') != 'notice'
        or payload.get('notice_type') != 'notify'
        or payload.get('sub_type') != 'poke'
    ):
        return False
    target_id = _string_value(payload.get('target_id'))
    return bool(target_id) and target_id == _required_identifier(
        self_id, 'self_id 不能为空',
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

    :return: 事件种类标识，包括 action 响应、心跳、请求、自身消息、拒绝消息、普通消息、
        私聊输入状态、戳 Bot 的戳一戳、给群消息贴的表情回应和其他未处理类型。

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
        if is_poke_at_self(payload, self_id):
            # 戳一戳同样受访问名单约束：名单外的群不该因为一次戳就绕过准入。
            group_id = payload.get('group_id')
            if group_id is not None:
                if not group_access.allows(
                    _required_identifier(group_id, 'group_id 不能为空'),
                ):
                    return 'group_denied'
                return 'poke'
            if not private_access.allows(
                _sender_external_id(payload),
                _required_identifier(owner_qq, 'owner_qq 不能为空'),
            ):
                return 'private_denied'
            return 'poke'
        if is_emoji_like(payload):
            # 表情回应只处理群聊形态，且同样受群白名单约束。
            group_id = payload.get('group_id')
            if group_id is None:
                return 'other'
            if not group_access.allows(
                _required_identifier(group_id, 'group_id 不能为空'),
            ):
                return 'group_denied'
            return 'emoji_like'
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
    mention_names: Mapping[str, str] | None = None,
    quote_previews: Mapping[str, str] | None = None,
) -> QqInboundEvent | None:
    """解析通过访问策略的私聊或白名单群消息为统一入站事件。

    :param payload: 已解析的 OneBot 消息事件映射。
    :param self_id: 机器人登录 QQ 号，用于识别自身消息和提及。
    :param self_name: 机器人在平台上的显示名称。
    :param owner_qq: 配置的 owner QQ 号。
    :param private_access: 私聊访问策略模型。
    :param group_access: 群聊白名单策略模型。
    :param mention_names: 可选的 QQ 号到显示名映射；缺省时提及只渲染为裸 QQ 号，
        模型无法判断被点名的是谁。机器人自身的映射由本函数补齐，调用方不必传入。
    :param quote_previews: 可选的被引用消息 ID 到摘要映射；缺省时引用只渲染为
        不含内容的占位符。

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
        text=message_to_text(
            raw_segments,
            {**(mention_names or {}), self_id: self_name},
            quote_previews,
        ),
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
