"""定义平台中立的合并转发消息树及其 HTTP 边界序列化。

平台适配器负责把各自协议的转发结构还原为本模块的数据类，主体只保存已经
完整解析的不可变树。模型侧不会直接看到整棵树，而是由只读工具按路径逐层
展开；因此这里保留节点内文本和嵌套转发的原始顺序，不做扁平化。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Mapping, Set, Tuple


ForwardPartKind = Literal['text', 'forward']


@dataclass(frozen=True)
class ForwardMessagePart:
    """一条转发节点中的有序内容片段。"""

    kind: ForwardPartKind
    text: str = ''
    nested: ForwardMessageTree | None = None

    def __post_init__(self) -> None:
        """保证文本与嵌套转发两种形态互斥且内容完整。"""
        if self.kind == 'text':
            if not self.text:
                raise ValueError('合并转发文本片段不能为空')
            if self.nested is not None:
                raise ValueError('合并转发文本片段不能携带 nested')
            return
        if self.kind == 'forward':
            if self.nested is None:
                raise ValueError('合并转发嵌套片段必须携带 nested')
            if self.text:
                raise ValueError('合并转发嵌套片段不能携带 text')
            return
        raise ValueError(f'不支持的合并转发片段类型：{self.kind}')

    @classmethod
    def text_part(cls, text: str) -> ForwardMessagePart:
        """构造一个保留原文的文本片段。"""
        return cls(kind='text', text=text)

    @classmethod
    def forward_part(cls, nested: ForwardMessageTree) -> ForwardMessagePart:
        """构造一个嵌套转发片段。"""
        return cls(kind='forward', nested=nested)


@dataclass(frozen=True)
class ForwardNode:
    """合并转发中的一条发送者消息。"""

    sender_name: str
    parts: Tuple[ForwardMessagePart, ...]

    def __post_init__(self) -> None:
        """拒绝无法辨认发送者或没有任何可读内容的节点。"""
        if not self.sender_name.strip():
            raise ValueError('合并转发节点的发送者不能为空')
        if not self.parts:
            raise ValueError('合并转发节点的内容不能为空')


@dataclass(frozen=True)
class ForwardMessageTree:
    """一层合并转发，由按协议顺序排列的节点组成。"""

    nodes: Tuple[ForwardNode, ...]

    def __post_init__(self) -> None:
        """空转发不能伪装成成功解析的消息树。"""
        if not self.nodes:
            raise ValueError('合并转发消息不能为空')


def forward_tree_to_payload(tree: ForwardMessageTree) -> Dict[str, Any]:
    """把消息树转换为适配器与主体之间的 JSON 对象。"""
    nodes: List[Dict[str, Any]] = []
    for node in tree.nodes:
        parts: List[Dict[str, Any]] = []
        for part in node.parts:
            if part.kind == 'text':
                parts.append({'kind': 'text', 'text': part.text})
                continue
            assert part.nested is not None
            parts.append({
                'kind': 'forward',
                'nested': forward_tree_to_payload(part.nested),
            })
        nodes.append({'senderName': node.sender_name, 'parts': parts})
    return {'nodes': nodes}


def forward_tree_from_payload(payload: Mapping[str, Any]) -> ForwardMessageTree:
    """严格还原一棵消息树，拒绝半合法或字段漂移的 HTTP 载荷。"""
    if not isinstance(payload, Mapping):
        raise ValueError('合并转发载荷必须是对象')
    _reject_unknown_keys(payload, {'nodes'}, '合并转发载荷')
    raw_nodes = payload.get('nodes')
    if not isinstance(raw_nodes, list):
        raise ValueError('合并转发载荷的 nodes 必须是数组')
    if not raw_nodes:
        raise ValueError('合并转发载荷的 nodes 不能为空')

    nodes: List[ForwardNode] = []
    for node_index, raw_node in enumerate(raw_nodes):
        if not isinstance(raw_node, Mapping):
            raise ValueError(f'合并转发第 {node_index + 1} 个节点必须是对象')
        _reject_unknown_keys(raw_node, {'senderName', 'parts'}, '合并转发节点')
        sender_name = raw_node.get('senderName')
        if not isinstance(sender_name, str) or not sender_name.strip():
            raise ValueError(f'合并转发第 {node_index + 1} 个节点缺少发送者')
        raw_parts = raw_node.get('parts')
        if not isinstance(raw_parts, list):
            raise ValueError(f'合并转发第 {node_index + 1} 个节点的 parts 必须是数组')
        if not raw_parts:
            raise ValueError(f'合并转发第 {node_index + 1} 个节点的 parts 不能为空')
        parts = tuple(
            _part_from_payload(raw_part, node_index, part_index)
            for part_index, raw_part in enumerate(raw_parts)
        )
        nodes.append(ForwardNode(sender_name=sender_name.strip(), parts=parts))
    return ForwardMessageTree(nodes=tuple(nodes))


def _part_from_payload(
    raw_part: Any,
    node_index: int,
    part_index: int,
) -> ForwardMessagePart:
    """还原单个有序片段，并把错误定位到具体节点和位置。"""
    label = f'合并转发第 {node_index + 1} 个节点的第 {part_index + 1} 个片段'
    if not isinstance(raw_part, Mapping):
        raise ValueError(f'{label}必须是对象')
    kind = raw_part.get('kind')
    if kind == 'text':
        _reject_unknown_keys(raw_part, {'kind', 'text'}, label)
        text = raw_part.get('text')
        if not isinstance(text, str) or not text:
            raise ValueError(f'{label}缺少非空 text')
        return ForwardMessagePart.text_part(text)
    if kind == 'forward':
        _reject_unknown_keys(raw_part, {'kind', 'nested'}, label)
        nested = raw_part.get('nested')
        if not isinstance(nested, Mapping):
            raise ValueError(f'{label}缺少对象类型的 nested')
        return ForwardMessagePart.forward_part(forward_tree_from_payload(nested))
    raise ValueError(f'{label}的 kind 不受支持：{kind}')


def _reject_unknown_keys(
    value: Mapping[str, Any],
    allowed: Set[str],
    label: str,
) -> None:
    """拒绝边界对象中的未知字段，避免协议漂移被静默忽略。"""
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise ValueError(f'{label}包含未知字段：{", ".join(unknown)}')


__all__ = [
    'ForwardMessagePart',
    'ForwardMessageTree',
    'ForwardNode',
    'ForwardPartKind',
    'forward_tree_from_payload',
    'forward_tree_to_payload',
]
