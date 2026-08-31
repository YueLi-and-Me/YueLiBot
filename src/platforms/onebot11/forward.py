"""把 OneBot 11 协议端的合并转发响应还原为平台中立消息树。

``get_forward_msg`` 会在顶层返回 ``data.messages``；其中嵌套 ``forward``
消息段应继续携带 ``data.content``。解析器保留每个节点内的片段顺序，任何缺失的
嵌套正文都直接报错，避免对外声称支持深层浏览却只保存一个不可展开的编号。
"""

from __future__ import annotations

from typing import Any, List, Mapping

from src.core.platform_io.forward import (
    ForwardMessagePart,
    ForwardMessageTree,
    ForwardNode,
)

from .segments import segment_to_text


def parse_forward_response(response: Mapping[str, Any]) -> ForwardMessageTree:
    """解析 ``get_forward_msg`` 的完整响应对象。"""
    if not isinstance(response, Mapping):
        raise ValueError('合并转发响应必须是对象')
    data = response.get('data')
    if not isinstance(data, Mapping):
        raise ValueError('合并转发响应缺少对象类型的 data')
    messages = data.get('messages')
    if not isinstance(messages, list):
        raise ValueError('合并转发响应的 messages 必须是数组')
    if not messages:
        raise ValueError('合并转发响应的 messages 不能为空')
    return parse_forward_content(messages)


def parse_forward_content(messages: List[Any]) -> ForwardMessageTree:
    """递归解析一层转发节点，嵌套层数由实际协议数据决定。"""
    if not isinstance(messages, list):
        raise ValueError('合并转发内容必须是数组')
    if not messages:
        raise ValueError('合并转发内容不能为空')

    nodes: List[ForwardNode] = []
    for node_index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            raise ValueError(f'合并转发第 {node_index + 1} 个节点必须是对象')
        raw_segments = message.get('message')
        if not isinstance(raw_segments, list):
            raise ValueError(
                f'合并转发第 {node_index + 1} 个节点的 message 必须是数组'
            )
        sender_name = _sender_name(message.get('sender'), node_index)
        parts: List[ForwardMessagePart] = []
        for segment_index, segment in enumerate(raw_segments):
            if not isinstance(segment, Mapping):
                raise ValueError(
                    f'合并转发第 {node_index + 1} 个节点的第 '
                    f'{segment_index + 1} 个消息段必须是对象'
                )
            if segment.get('type') == 'forward':
                data = segment.get('data')
                if not isinstance(data, Mapping):
                    raise ValueError('嵌套合并转发缺少对象类型的 data')
                content = data.get('content')
                if not isinstance(content, list):
                    raise ValueError('嵌套合并转发缺少 data.content')
                parts.append(
                    ForwardMessagePart.forward_part(parse_forward_content(content))
                )
                continue
            text = segment_to_text(segment)
            if text:
                parts.append(ForwardMessagePart.text_part(text))
        if not parts:
            raise ValueError(f'合并转发第 {node_index + 1} 个节点没有可读内容')
        nodes.append(ForwardNode(sender_name=sender_name, parts=tuple(parts)))
    return ForwardMessageTree(nodes=tuple(nodes))


def _sender_name(raw_sender: Any, node_index: int) -> str:
    """按群名片、昵称、账号编号选择发送者，协议缺损时精确失败。"""
    if not isinstance(raw_sender, Mapping):
        raise ValueError(
            f'合并转发第 {node_index + 1} 个节点缺少对象类型的 sender'
        )
    for key in ('card', 'nickname', 'user_id'):
        value = raw_sender.get(key)
        if value is None:
            continue
        name = str(value).strip()
        if name:
            return name
    raise ValueError(
        f'合并转发第 {node_index + 1} 个节点的 sender '
        '缺少非空 card、nickname 或 user_id'
    )


__all__ = ['parse_forward_content', 'parse_forward_response']
