"""把 OneBot 11 协议端的合并转发响应还原为平台中立消息树。

``get_forward_msg`` 的响应包装存在多种真实形状（``data`` 直接是节点数组、
``data.messages`` / ``data.content``、再包一层 ``data.data.*``），由
``_response_messages`` 统一识别；其中嵌套 ``forward`` 段是否内联
``data.content`` 由协议端决定，缺失时本模块用调用方注入的解析器按
``data.id`` 再取一层。解析保留每个节点内的片段顺序；补取失败或补取内容
非法时该嵌套层降级为文本片段，不牵连整棵根树，段自身既无 ``content`` 又
无 ``id``、或资源编号循环引用属结构错误，仍然上抛。

[WORKAROUND] 协议端不内联嵌套转发正文
- 现象：转发套转发时，顶层 ``get_forward_msg`` 结果里的内层 ``forward`` 段只有
  ``data.id``。按「缺 content 即报错」处理会丢弃整棵根树，正文只剩 ``[转发消息]``。
- 原因：``get_forward_msg`` 只展开被请求的那一层，内层仍是待解析的资源编号。
- 后果：读取工具的能力按会话声明，一旦树被丢弃，模型仍会看到工具并调用一次，
  得到「当前会话中没有可读取的合并转发消息」，白费一个认知轮次。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, List, Mapping, Tuple

from src.core.logging.logger import get_logger
from src.core.platform_io.forward import (
    ForwardMessagePart,
    ForwardMessageTree,
    ForwardNode,
)

from .segments import segment_to_text


logger = get_logger(__name__)


# 按资源编号取一层合并转发内容，返回协议端 ``get_forward_msg`` 的原始响应。
NestedForwardResolver = Callable[[str], Awaitable[Mapping[str, Any]]]

# 嵌套层补取失败降级成的文本片段；根级占位树的单节点正文复用同一段文本。
FORWARD_LAYER_UNREADABLE_TEXT = '[这一层的转发内容读取失败]'

# 正文预览的单行字符预算，包含首尾的 ``[转发消息：`` 与 ``共 N 条]``。
# 只设一个常量：预览条数与单条字数互相牵制的双常量在调整时必须同时改，
# 单预算下「装不下就停」由节点粒度自然给出边界。
FORWARD_PREVIEW_MAX_CHARS = 120

# 预览里嵌套转发的短标记：只提示存在，不展开，要看全的走读取工具。
FORWARD_PREVIEW_NESTED_MARKER = '[嵌套转发]'


class ForwardStructureError(ValueError):
    """嵌套转发段自身的结构损坏：资源编号循环引用，或既无 content 又无 id。

    这类错误描述段本身不可解析，降级成「读取失败」文本会掩盖结构漂移，
    必须穿过逐层降级的捕获向上传播；其余取内容失败才允许降级。
    """


def unreadable_forward_tree() -> ForwardMessageTree:
    """构造根级占位树：单节点、正文为读取失败说明。

    多根转发中某根失败时用它占住位置，保证根数量与正文占位个数一致；
    发送者用带方括号的标记而不是任何形似人名的文本，模型由此区分
    「这一层读不到」与真实节点，不会把占位当成某个发言者。
    """
    return ForwardMessageTree(nodes=(ForwardNode(
        sender_name='[读取失败]',
        parts=(ForwardMessagePart.text_part(FORWARD_LAYER_UNREADABLE_TEXT),),
    ),))


def forward_tree_preview(tree: ForwardMessageTree) -> str:
    """把一棵已解析的转发树渲染成单行预览，只读平台中立的树结构。

    按节点顺序在 :data:`FORWARD_PREVIEW_MAX_CHARS` 预算内填充
    ``发送者：正文`` 片段，装不下的节点整体不出现——用省略号截出半个
    节点会让模型把残句当成完整发言；末尾恒为真实总条数，预览出来的
    条数与总数不一致同样会误导。单行是硬要求：正文会进历史、进记忆
    抽取、进表达学习，多行块会撑散这些按行组织的结构。

    :param tree: 已解析的转发树。
    :return: 形如 ``[转发消息：张三：内容｜李四：内容｜共 12 条]`` 的单行
        预览；一个节点都装不下时仅剩 ``[转发消息：共 N 条]``。
    """
    total = len(tree.nodes)
    suffix = f'｜共 {total} 条]'
    used = len('[转发消息：') + len(suffix)
    pieces: List[str] = []
    for node in tree.nodes:
        piece = f'{node.sender_name}：{_preview_node_text(node)}'
        cost = len(piece) + (len('｜') if pieces else 0)
        if used + cost > FORWARD_PREVIEW_MAX_CHARS:
            break
        pieces.append(piece)
        used += cost
    if not pieces:
        return f'[转发消息：共 {total} 条]'
    return f'[转发消息：{"｜".join(pieces)}{suffix}'


def _preview_node_text(node: ForwardNode) -> str:
    """把节点内有序片段压成单行文本：文本压平换行，嵌套转发换短标记。"""
    chunks: List[str] = []
    for part in node.parts:
        if part.kind == 'text':
            chunks.append(' '.join(part.text.split()))
        else:
            chunks.append(FORWARD_PREVIEW_NESTED_MARKER)
    return ''.join(chunks)


async def parse_forward_response(
    response: Mapping[str, Any],
    resolve_nested: NestedForwardResolver,
) -> ForwardMessageTree:
    """解析 ``get_forward_msg`` 的完整响应对象。

    :param response: 协议端返回的响应映射。
    :param resolve_nested: 未内联嵌套层的取内容解析器。
    :return: 已展开全部嵌套层级的消息树。
    :raises ValueError: 响应形状无法识别、节点数组为空，或存在节点结构错误；
        嵌套层补取失败不在此列，已在解析器内降级为文本片段。
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
    :raises ValueError: 节点结构不完整、没有可读内容，或嵌套段出现结构错误
        （资源编号循环引用、既无 content 又无 id）；嵌套层补取失败不在此列，
        已降级为文本片段。
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
                parts.append(await _parse_nested(data, resolve_nested, ancestor_ids))
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
) -> ForwardMessagePart:
    """解析一个嵌套转发段，返回嵌套树片段或降级后的文本片段。

    内联正文直接解析，结构错误照常上抛；缺失内联时按资源编号补取一层，
    补取抛异常或补取内容非法只说明这一层读不到，降级为文本片段而不是
    丢弃外层已解析的内容，并记一条 warning——降级不留痕等于把故障藏进正文。循环引用与「既无 content 又无 id」是段自身的
    结构错误，属于 :class:`ForwardStructureError`，任何一层都不降级。

    :param data: 嵌套 ``forward`` 段的 ``data`` 映射。
    :param resolve_nested: 未内联嵌套层的取内容解析器。
    :param ancestor_ids: 当前解析路径上已按编号取过的祖先资源编号。
    :return: 该层的 ``forward`` 片段；补取失败时为说明读取失败的 ``text`` 片段。
    :raises ForwardStructureError: 资源编号循环引用，或段既无 content 又无 id。
    """
    content = data.get('content')
    if isinstance(content, list):
        # 内联正文是有限数据，不经过协议端，沿用当前的祖先编号链即可；
        # 其结构错误不走降级——降级吸收的是协议端往返的失败，本地数据
        # 损坏应当上抛让根级按失败根处理，而不是被静默改写成文本。
        return ForwardMessagePart.forward_part(await parse_forward_content(
            content, resolve_nested, ancestor_ids,
        ))
    forward_id = str(data.get('id') or '').strip()
    if not forward_id:
        raise ForwardStructureError('嵌套合并转发既没有 data.content 也没有 data.id')
    # 自引用的资源编号会让「按编号再取一层」无限递归并持续发起协议端请求，
    # 取之前先在当前路径上断链。
    if forward_id in ancestor_ids:
        raise ForwardStructureError(
            f'嵌套合并转发的资源编号 {forward_id} 出现循环引用'
        )
    # 取内容与解析内容分开捕获，捕获宽度按各自的失败来源给：
    # 协议端往返什么异常都可能抛（超时、连接断开、协议端自定义错误），这是
    # 外部边界，捕获得宽；而解析是我们自己的代码，只有 ValueError 表示「拿回来
    # 的内容不合法」，捕获宽了会把解析器自身的 TypeError / AttributeError 也
    # 改写成「这一层读取失败」，等于用降级掩盖自己的 bug。
    try:
        response = await resolve_nested(forward_id)
    except Exception as exc:
        # 降级必须留痕：这一层读不到时正文只多一句说明，若不记日志，现场就只剩
        # 「内容少了一块」，连是哪个资源编号取失败都无从查起——2026-09-01 那次
        # 定案靠的正是适配器控制台里这条 error 原文。
        logger.warning(
            '嵌套合并转发补取失败，该层降级为文本占位',
            forwardId=forward_id,
            error=str(exc),
        )
        return ForwardMessagePart.text_part(FORWARD_LAYER_UNREADABLE_TEXT)
    try:
        tree = await parse_forward_content(
            _response_messages(response),
            resolve_nested,
            (*ancestor_ids, forward_id),
        )
    except ForwardStructureError:
        # 更深层的结构错误同样不可降级，穿过本层的捕获继续上抛。
        # 它是 ValueError 的子类，这条分支必须排在下面那条之前。
        raise
    except ValueError as exc:
        logger.warning(
            '嵌套合并转发补取内容不合法，该层降级为文本占位',
            forwardId=forward_id,
            error=str(exc),
        )
        return ForwardMessagePart.text_part(FORWARD_LAYER_UNREADABLE_TEXT)
    return ForwardMessagePart.forward_part(tree)


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
    'FORWARD_LAYER_UNREADABLE_TEXT',
    'FORWARD_PREVIEW_MAX_CHARS',
    'ForwardStructureError',
    'NestedForwardResolver',
    'forward_tree_preview',
    'parse_forward_content',
    'parse_forward_response',
    'unreadable_forward_tree',
]
