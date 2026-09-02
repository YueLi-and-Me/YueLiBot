"""按消息编号与路径逐层浏览合并转发内容的内置工具插件。

本模块承载工具本体与 ``ToolPlugin`` 的三个挂载点：``@tool`` 装饰的方法同时
给出模型可见声明与执行体；``observe_inbound`` 在入站路径把完整转发树写入
有界会话缓存；``stream_capabilities`` 只在缓存里确有转发内容的会话贡献
``forward_message`` 能力，让工具声明按会话收窄。除挂载点外不依赖聊天服务，
宿主经清单加载本插件后把收集到的工具登记进 ToolRegistry。

工具缓存只保存当前进程已经接收的完整转发树，以 ``(stream_id, message_id)``
隔离会话；模型每次只得到选中层的节点和下一层路径。这样既能访问任意深度，
也不会因为一条大型转发在首轮就挤满上下文。
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Tuple

import json

from src.core.platform_io.forward import ForwardMessageTree
from src.core.tooling.spec import (
    ToolContext,
    ToolExecutionResult,
    ToolInvocation,
)
from src.plugin_system import PluginManifest, ToolPlugin, tool


DEFAULT_FORWARD_CACHE_LIMIT = 128
DEFAULT_FORWARD_OBSERVATION_MAX_CHARS = 16000
MIN_FORWARD_OBSERVATION_MAX_CHARS = 128


class ForwardMessagePlugin(ToolPlugin):
    """有界保存入站转发树，并按路径执行只读查询。"""

    def __init__(
        self,
        manifest: PluginManifest,
        cache_limit: int = DEFAULT_FORWARD_CACHE_LIMIT,
        observation_max_chars: int = DEFAULT_FORWARD_OBSERVATION_MAX_CHARS,
    ) -> None:
        """创建进程内缓存，并为分页正文和续读提示保留足够观察空间。

        :param manifest: 已校验的插件清单。
        :param cache_limit: 会话缓存容量上限，必须大于 0。
        :param observation_max_chars: 单次观察的字符上限，不得小于
            :data:`MIN_FORWARD_OBSERVATION_MAX_CHARS`，否则分页提示放不下。
        :raises ValueError: 容量或观察上限非法。
        """
        super().__init__(manifest)
        if cache_limit <= 0:
            raise ValueError('合并转发缓存容量必须大于 0')
        if observation_max_chars < MIN_FORWARD_OBSERVATION_MAX_CHARS:
            raise ValueError(
                '合并转发观察字符上限不能小于 '
                f'{MIN_FORWARD_OBSERVATION_MAX_CHARS}'
            )
        self._cache_limit = cache_limit
        self._observation_max_chars = observation_max_chars
        self._cache: Dict[
            Tuple[int, int], Tuple[ForwardMessageTree, ...]
        ] = {}

    @tool(
        name='read_forward_message',
        description=(
            '当聊天记录里出现 [转发消息：发送者：内容预览｜共 N 条] 形态的占位时，'
            '用该行的内部消息编号逐层读取完整内容；[转发消息：内容读取失败] '
            '表示该转发不可读取，无需调用；默认读取一层，返回的 path 可定位子树；'
            '嵌套很深时可设置 depth 在一次调用里展开多层；结果出现 next_offset 时，'
            '保持其他参数不变并传入 offset 可继续读取，避免深层或超长单层内容被截断。'
        ),
        parameters={
            'type': 'object',
            'properties': {
                'message_id': {
                    'type': 'integer',
                    'minimum': 1,
                    'description': '聊天记录里的内部消息编号。',
                },
                'path': {
                    'type': 'array',
                    'items': {'type': 'integer', 'minimum': 0},
                    'description': '上一次结果给出的嵌套路径；首次读取时省略。',
                },
                'depth': {
                    'type': 'integer',
                    'minimum': 1,
                    'description': '本次从选中位置向下展开的层数，默认 1。',
                },
                'offset': {
                    'type': 'integer',
                    'minimum': 0,
                    'description': '结果分页字符游标，首次读取时省略或设为 0。',
                },
            },
            'required': ['message_id'],
            'additionalProperties': False,
        },
        capabilities=('forward_message',),
    )
    async def read_forward_message(
        self,
        invocation: ToolInvocation,
        context: ToolContext,
    ) -> ToolExecutionResult:
        """读取根层或路径指定层，并返回下一层的稳定路径。"""
        try:
            message_id, path, depth, offset = _parse_arguments(invocation.arguments)
        except ValueError as exc:
            return _failure(invocation.tool_name, str(exc))

        # 回合帧是本轮模型可见消息的时间边界。入站处理仍可能在模型思考期间
        # 缓存更新消息，若只按 stream_id 查缓存，模型猜中后续编号就能越过水位
        # 读取尚未进入当前快照的内容。
        if message_id > context.frame.message_watermark:
            return _failure(
                invocation.tool_name,
                f'合并转发消息 {message_id} 晚于当前回合消息水位 '
                f'{context.frame.message_watermark}',
            )

        trees = self._cache.get((context.stream_id, message_id))
        if trees is None:
            # 工具声明按会话给出（见 stream_capabilities），可读性却是按消息的：
            # 同一个会话里有的转发解析成功进了缓存，有的因协议端超时或结构损坏
            # 没进。模型只能看到正文里清一色的 [转发消息] 占位，分不出哪条能读，
            # 因此失败信息必须把可读的编号一并给出，否则它只能继续猜。
            return _failure(
                invocation.tool_name,
                f'当前会话中没有可读取的合并转发消息 {message_id}'
                f'{self._readable_hint(context.stream_id, context.frame.message_watermark)}',
            )
        try:
            selected = _select_trees(trees, path)
        except ValueError as exc:
            return _failure(invocation.tool_name, str(exc))

        rendered = _render_selection(message_id, selected, depth)
        try:
            observation, next_offset = _page_observation(
                rendered,
                offset,
                self._observation_max_chars,
            )
        except ValueError as exc:
            return _failure(invocation.tool_name, str(exc))
        return ToolExecutionResult(
            tool_name=invocation.tool_name,
            success=True,
            observation=observation,
            metadata={
                'messageId': message_id,
                'path': path,
                'depth': depth,
                'offset': offset,
                'nextOffset': next_offset,
            },
        )

    def observe_inbound(
        self,
        stream_id: int,
        message_id: int,
        inbound: Any,
    ) -> None:
        """把一条已落库入站消息的转发根树写入有界会话缓存。

        入站消息不含转发根树时本方法没有任何效果；容量超限时按最旧条目
        淘汰。
        """
        self._remember(stream_id, message_id, inbound.forward_messages)

    def stream_capabilities(self, stream_id: int) -> FrozenSet[str]:
        """只在缓存过转发树的会话贡献 ``forward_message``，其余会话为空。

        能力跟随缓存而不是协议端静态声明：没有可读内容的会话里，工具声明
        出现只会诱导模型调用后必败。
        """
        if self._has_stream(stream_id):
            return frozenset({'forward_message'})
        return frozenset()

    def _readable_hint(self, stream_id: int, watermark: int) -> str:
        """列出该会话当前可读的合并转发编号，供失败信息给出可用取值。

        :param stream_id: 会话编号。
        :param watermark: 本回合消息水位；晚于水位的缓存不列出，否则模型能从
            失败信息里得知尚未进入本轮快照的消息存在。
        :return: 以分号起头的中文补充说明；没有可读内容时说明这一事实。
        """
        readable = sorted(
            cached_message_id
            for cached_stream_id, cached_message_id in self._cache
            if cached_stream_id == stream_id and cached_message_id <= watermark
        )
        if not readable:
            return '；本会话目前没有任何可读取的合并转发'
        listed = '、'.join(str(message_id) for message_id in readable)
        return f'；本会话可读取的是 {listed}'

    def _remember(
        self,
        stream_id: int,
        message_id: int,
        trees: Tuple[ForwardMessageTree, ...],
    ) -> None:
        """把一条已落库入站消息的转发根树写入有界会话缓存。"""
        if stream_id <= 0:
            raise ValueError('合并转发缓存的 stream_id 必须大于 0')
        if message_id <= 0:
            raise ValueError('合并转发缓存的 message_id 必须大于 0')
        if not trees:
            return
        key = (stream_id, message_id)
        # 重写同一键时先删除，确保它重新成为最新条目。
        self._cache.pop(key, None)
        self._cache[key] = trees
        while len(self._cache) > self._cache_limit:
            self._cache.pop(next(iter(self._cache)))

    def _has_stream(self, stream_id: int) -> bool:
        """判断当前会话是否至少缓存过一条可读合并转发。"""
        return any(key_stream_id == stream_id for key_stream_id, _ in self._cache)


def _parse_arguments(
    arguments: Dict[str, Any],
) -> Tuple[int, Tuple[int, ...], int, int]:
    """校验直接调用场景的参数；模型链路还会在注册表提前校验一次。"""
    unknown = sorted(
        str(key)
        for key in arguments
        if key not in {'message_id', 'path', 'depth', 'offset'}
    )
    if unknown:
        raise ValueError(f'合并转发工具包含未知字段：{", ".join(unknown)}')
    message_id = arguments.get('message_id')
    if (
        not isinstance(message_id, int)
        or isinstance(message_id, bool)
        or message_id <= 0
    ):
        raise ValueError('message_id 必须是大于 0 的整数')
    raw_path = arguments.get('path', [])
    if not isinstance(raw_path, list):
        raise ValueError('path 必须是非负整数数组')
    if any(
        not isinstance(index, int) or isinstance(index, bool) or index < 0
        for index in raw_path
    ):
        raise ValueError('path 必须是非负整数数组')
    depth = arguments.get('depth', 1)
    if not isinstance(depth, int) or isinstance(depth, bool) or depth <= 0:
        raise ValueError('depth 必须是大于 0 的整数')
    offset = arguments.get('offset', 0)
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError('offset 必须是非负整数')
    return message_id, tuple(raw_path), depth, offset


def _page_observation(
    rendered: str,
    offset: int,
    page_max_chars: int,
) -> Tuple[str, int | None]:
    """按稳定字符游标分页，使超长平层或单片段的尾部仍然可达。"""
    if offset >= len(rendered):
        raise ValueError(
            f'offset {offset} 已到达或超出合并转发内容末尾'
            f'（总字符数 {len(rendered)}）'
        )
    prefix = f'[合并转发分页，offset={offset}]\n' if offset else ''
    remaining = rendered[offset:]
    if len(prefix) + len(remaining) <= page_max_chars:
        return f'{prefix}{remaining}', None

    # 续读提示也是模型可见观察的一部分，必须从正文预算中预留。用总长度的
    # 位数估算最宽游标，可避免页尾跨十进制位数时预算来回振荡。
    content_limit = page_max_chars - len(prefix) - len(
        _continuation_hint(len(rendered))
    )
    if content_limit <= 0:
        raise ValueError('合并转发观察字符上限过小，无法容纳分页提示')
    page_end = offset + content_limit
    page = rendered[offset:page_end]
    suffix = _continuation_hint(page_end)
    return f'{prefix}{page}{suffix}', page_end


def _continuation_hint(next_offset: int) -> str:
    """生成模型可见的稳定续读游标说明。"""
    return (
        f'\n[内容未完；next_offset={next_offset}。请保持 message_id、path、depth '
        f'不变，并在下一次调用中设置 offset={next_offset}]'
    )


def _select_trees(
    roots: Tuple[ForwardMessageTree, ...],
    path: Tuple[int, ...],
) -> List[Tuple[Tuple[int, ...], ForwardMessageTree]]:
    """沿路径选择一层；空路径返回全部根树。"""
    if not path:
        return [((index,), tree) for index, tree in enumerate(roots)]

    candidates = roots
    selected: ForwardMessageTree | None = None
    for depth, index in enumerate(path):
        if index >= len(candidates):
            raise ValueError(
                f'合并转发路径无效：第 {depth + 1} 级索引 {index} 超出范围'
            )
        selected = candidates[index]
        candidates = _direct_nested_trees(selected)
    assert selected is not None
    return [(path, selected)]


def _direct_nested_trees(tree: ForwardMessageTree) -> Tuple[ForwardMessageTree, ...]:
    """按节点和片段原序收集当前层可继续展开的直接子树。"""
    nested_trees: List[ForwardMessageTree] = []
    for node in tree.nodes:
        for part in node.parts:
            if part.kind == 'forward' and part.nested is not None:
                nested_trees.append(part.nested)
    return tuple(nested_trees)


def _render_selection(
    message_id: int,
    selected: List[Tuple[Tuple[int, ...], ForwardMessageTree]],
    depth: int,
) -> str:
    """展示选中位置以下指定层数，并为尚未展开的子树保留可复制路径。"""
    lines = [f'[合并转发消息 {message_id}，展开深度 {depth}]']
    for root_index, (base_path, tree) in enumerate(selected):
        if len(selected) > 1:
            lines.append(f'\n根转发 {root_index + 1}，path={_path_text(base_path)}')
        _render_tree(lines, tree, base_path, depth, '')
    if any(_direct_nested_trees(tree) for _, tree in selected):
        lines.append(
            '\n可继续使用结果中的 path 定位子树，或增大 depth 一次展开更多层。'
        )
    return '\n'.join(lines)


def _render_tree(
    lines: List[str],
    tree: ForwardMessageTree,
    base_path: Tuple[int, ...],
    remaining_depth: int,
    indent: str,
) -> None:
    """深度优先渲染一棵树；每层先保留原片段顺序，再展开直接子树。"""
    nested: List[Tuple[Tuple[int, ...], ForwardMessageTree]] = []
    nested_index = 0
    for node in tree.nodes:
        content: List[str] = []
        for part in node.parts:
            if part.kind == 'text':
                content.append(part.text)
                continue
            assert part.nested is not None
            nested_path = (*base_path, nested_index)
            if remaining_depth > 1:
                content.append(
                    f'[嵌套合并转发 #{nested_index + 1}，已在下方展开]'
                )
            else:
                content.append(
                    f'[嵌套合并转发，path={_path_text(nested_path)}]'
                )
            nested.append((nested_path, part.nested))
            nested_index += 1
        lines.append(f'{indent}【{node.sender_name}】：{"".join(content)}')
    if remaining_depth <= 1:
        return
    for nested_number, (nested_path, nested_tree) in enumerate(nested, start=1):
        lines.append(f'{indent}  [展开嵌套 #{nested_number}]')
        _render_tree(
            lines,
            nested_tree,
            nested_path,
            remaining_depth - 1,
            f'{indent}  ',
        )


def _path_text(path: Tuple[int, ...]) -> str:
    """把路径渲染为稳定 JSON 数组，便于模型原样复用。"""
    return json.dumps(list(path), ensure_ascii=False)


def _failure(tool_name: str, message: str) -> ToolExecutionResult:
    """构造可回灌模型的显式工具失败结果。"""
    return ToolExecutionResult(
        tool_name=tool_name,
        success=False,
        error_message=message,
    )


__all__ = [
    'DEFAULT_FORWARD_CACHE_LIMIT',
    'DEFAULT_FORWARD_OBSERVATION_MAX_CHARS',
    'MIN_FORWARD_OBSERVATION_MAX_CHARS',
    'ForwardMessagePlugin',
]
