"""把 OneBot 11 协议端的合并转发响应还原为平台中立消息树。

``get_forward_msg`` 的响应包装存在多种真实形状（``data`` 直接是节点数组、
``data.messages`` / ``data.content``、再包一层 ``data.data.*``），由
``_response_messages`` 统一识别；其中嵌套 ``forward`` 段是否内联
``data.content`` 由协议端决定，缺失时本模块用调用方注入的解析器按
``data.id`` 再取一层。解析保留每个节点内的片段顺序，任何取不到的嵌套正文都
直接报错，避免对外声称支持深层浏览却只保存一个不可展开的编号。

[WORKAROUND] 协议端不内联嵌套转发正文
- 现象：转发套转发时，顶层 ``get_forward_msg`` 结果里的内层 ``forward`` 段只有
  ``data.id``。按「缺 content 即报错」处理会丢弃整棵根树，正文只剩 ``[转发消息]``。
- 原因：``get_forward_msg`` 只展开被请求的那一层，内层仍是待解析的资源编号。
- 后果：读取工具的能力按会话声明，一旦树被丢弃，模型仍会看到工具并调用一次，
  得到「当前会话中没有可读取的合并转发消息」，白费一个认知轮次。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, List, Mapping, Tuple

from src.core.platform_io.forward import (
    ForwardMessagePart,
    ForwardMessageTree,
    ForwardNode,
)

from .segments import segment_to_text


# 按资源编号取一层合并转发内容，返回协议端 ``get_forward_msg`` 的原始响应。
NestedForwardResolver = Callable[[str], Awaitable[Mapping[str, Any]]]


async def parse_forward_response(
    response: Mapping[str, Any],
    resolve_nested: NestedForwardResolver,
) -> ForwardMessageTree:
    """解析 ``get_forward_msg`` 的完整响应对象。

    :param response: 协议端返回的响应映射。
    :param resolve_nested: 未内联嵌套层的取内容解析器。
    :return: 已展开全部嵌套层级的消息树。
    :raises ValueError: 响应结构不完整，或任一层节点无法解析。
    """
    return await parse_forward_content(_response_messages(response), resolve_nested)


async def parse_forward_content(
    messages: List[Any],
    resolve_nested: NestedForwardResolver,
    ancestor_ids: Tuple[str, ...] = (),
) -> ForwardMessageTree:
    """递归解析一层转发节点，嵌套层数由实际协议数据决定。

    :param messages: 一层转发的节点数组。
    :param resolve_nested: 未内联嵌套层的取内容解析器。
    :param ancestor_ids: 当前解析路径上已按编号取过的祖先资源编号，用于断开
        自引用；调用方无需传入。
    :return: 该层及其全部嵌套层级的消息树。
    :raises ValueError: 节点结构不完整、没有可读内容，或嵌套层取不到正文。
    """
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
                parts.append(ForwardMessagePart.forward_part(
                    await _parse_nested(data, resolve_nested, ancestor_ids)
                ))
                continue
            text = segment_to_text(segment)
            if text:
                parts.append(ForwardMessagePart.text_part(text))
        if not parts:
            raise ValueError(f'合并转发第 {node_index + 1} 个节点没有可读内容')
        nodes.append(ForwardNode(sender_name=sender_name, parts=tuple(parts)))
    return ForwardMessageTree(nodes=tuple(nodes))


async def _parse_nested(
    data: Mapping[str, Any],
    resolve_nested: NestedForwardResolver,
    ancestor_ids: Tuple[str, ...],
) -> ForwardMessageTree:
    """解析一个嵌套转发段：优先用内联正文，缺失时按资源编号再取一层。"""
    content = data.get('content')
    if isinstance(content, list):
        # 内联正文是有限数据，不经过协议端，沿用当前的祖先编号链即可。
        return await parse_forward_content(content, resolve_nested, ancestor_ids)
    forward_id = str(data.get('id') or '').strip()
    if not forward_id:
        raise ValueError('嵌套合并转发既没有 data.content 也没有 data.id')
    # 自引用的资源编号会让「按编号再取一层」无限递归并持续发起协议端请求，
    # 取之前先在当前路径上断链。
    if forward_id in ancestor_ids:
        raise ValueError(f'嵌套合并转发的资源编号 {forward_id} 出现循环引用')
    response = await resolve_nested(forward_id)
    return await parse_forward_content(
        _response_messages(response),
        resolve_nested,
        (*ancestor_ids, forward_id),
    )


def _response_messages(response: Mapping[str, Any]) -> List[Any]:
    """校验 ``get_forward_msg`` 响应并取出该层的节点数组。

    :param response: 协议端 ``get_forward_msg`` 的原始响应。
    :return: 该层的转发节点数组，保证非空。
    :raises ValueError: 响应不是对象、节点数组为空，或 ``data`` 不在五种已知
        形状之列；未识别时错误信息携带 ``data`` 的实际类型与顶层键名。
    """
    # [WORKAROUND] 协议端对 get_forward_msg 的响应包装存在多种真实形状
    # - 现象：只认 ``data.messages`` 时，``data`` 直接是节点数组、``data.content``、
    #   ``data.data.messages`` / ``data.data.content`` 这些同样真实存在的返回会被
    #   判为结构损坏，整棵根树丢弃，正文退回占位符、读取工具无树可开放。
    # - 原因：OneBot 11 标准未规定 ``get_forward_msg`` 的响应形状，不同协议端及
    #   同一协议端的不同版本各自选择包装层级与字段名。
    # - 后果：漏认一种形状等于在该版本协议端上禁用转发读取；反过来把识别不了
    #   的形状静默当成空转发，会把结构漂移伪装成解析成功。因此只识别有据可查
    #   的五种形状，全部对不上时报错并携带实际类型与顶层键名，空数组单独报错。
    if not isinstance(response, Mapping):
        raise ValueError('合并转发响应必须是对象')
    data = response.get('data')
    inner = data.get('data') if isinstance(data, Mapping) else None
    for candidate in (
        data if isinstance(data, list) else None,
        _array_field(data, 'messages'),
        _array_field(data, 'content'),
        _array_field(inner, 'messages'),
        _array_field(inner, 'content'),
    ):
        if isinstance(candidate, list):
            if not candidate:
                raise ValueError('合并转发响应的节点数组不能为空')
            return candidate
    keys = (
        '、'.join(sorted(str(key) for key in data))
        if isinstance(data, Mapping)
        else '（无）'
    )
    raise ValueError(
        '合并转发响应的 data 不是已知的节点数组形状：'
        f'实际类型 {type(data).__name__}，顶层键 {keys}'
    )


def _array_field(container: Any, key: str) -> List[Any] | None:
    """读取对象字段，仅当值是数组时返回，否则返回 ``None``。"""
    if not isinstance(container, Mapping):
        return None
    value = container.get(key)
    return value if isinstance(value, list) else None


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


__all__ = [
    'NestedForwardResolver',
    'parse_forward_content',
    'parse_forward_response',
]
