"""验证合并转发内置工具插件的逐层浏览、隔离和有界缓存。

原 ``ForwardMessageTool`` 迁为 ``src/plugins/built_in/forward-message`` 插件后，
本文件仍是行为基线：十一条用例的断言与迁移前逐字相同，只把构造、入站观察与
执行改走插件的三个挂载点。缓存与转发树解析逻辑的任何变化都会在这里暴露。

依赖插件目录与 ``src.core.tooling`` 协议。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Dict, List

from src.core.agent.action_protocol import (
    DecisionFrame,
    PlatformCapabilities,
    available_actions,
)
from src.core.platform_io.forward import (
    ForwardMessagePart,
    ForwardMessageTree,
    ForwardNode,
)
from src.core.platform_io.types import (
    ConversationContext,
    InboundMessage,
    PersonRef,
    StreamRef,
)
from src.core.tooling.spec import ToolContext, ToolExecutionResult, ToolInvocation
from src.plugin_system import PluginManifest

# 插件目录名含连字符，不是合法 Python 包名；与契约层 loader 同法按文件路径
# 加载入口模块，模块名用插件标识派生避免同名覆盖。
_PLUGIN_ENTRY = (
    Path(__file__).resolve().parents[2]
    / 'src' / 'plugins' / 'built_in' / 'forward-message' / 'plugin.py'
)
_spec = importlib.util.spec_from_file_location(
    'yueli_forward_message', _PLUGIN_ENTRY,
)
_plugin_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_plugin_module)
ForwardMessagePlugin = _plugin_module.ForwardMessagePlugin


def _manifest() -> PluginManifest:
    """构造一份工具插件清单；清单文件的校验另由插件契约测试覆盖。"""
    return PluginManifest(
        plugin_id='yueli.forward-message',
        plugin_type='tool',
        name='YueLi-Forward-Message',
        version='0.1.0',
        description='按消息编号与路径逐层浏览合并转发内容',
    )


def _inbound(
    stream_id: int,
    forward_messages: tuple[ForwardMessageTree, ...],
) -> InboundMessage:
    """构造一条只携带转发根树的入站消息，其余字段取最小合法值。"""
    return InboundMessage(
        text='转发',
        context=ConversationContext(
            stream=StreamRef(
                id=stream_id,
                platform='qq',
                kind='group',
                external_id=f'group-{stream_id}',
            ),
            person=PersonRef(id=1, kind='contact', first_seen_at=0),
        ),
        forward_messages=forward_messages,
    )


def _text_tree(sender: str, text: str) -> ForwardMessageTree:
    return ForwardMessageTree(nodes=(
        ForwardNode(sender_name=sender, parts=(ForwardMessagePart.text_part(text),)),
    ))


def _wrap_tree(level: int, nested: ForwardMessageTree) -> ForwardMessageTree:
    return ForwardMessageTree(nodes=(
        ForwardNode(
            sender_name=f'第{level}层',
            parts=(
                ForwardMessagePart.text_part(f'前{level}'),
                ForwardMessagePart.forward_part(nested),
                ForwardMessagePart.text_part(f'后{level}'),
            ),
        ),
    ))


def _deep_tree(depth: int) -> ForwardMessageTree:
    tree = _text_tree('最深层', '叶子正文')
    for level in reversed(range(depth)):
        tree = _wrap_tree(level, tree)
    return tree


def _context(stream_id: int = 7, message_watermark: int = 101) -> ToolContext:
    caps = PlatformCapabilities(forward_message=True)
    frame = DecisionFrame(
        turn_id=3,
        snapshot_id='turn-3',
        stream_kind='group',
        disposition='deliberate',
        selectable_message_ids=(101,),
        message_watermark=message_watermark,
        available_actions=available_actions(
            'group', 'deliberate', caps, cognitive_rounds_left=2,
        ),
        capabilities=caps,
    )
    return ToolContext(
        stream_id=stream_id,
        stream_kind='group',
        frame=frame,
        turn_id=3,
        snapshot_id='turn-3',
    )


async def _execute(
    plugin: ForwardMessagePlugin,
    arguments: Dict[str, Any],
    stream_id: int = 7,
    message_watermark: int = 101,
):
    """经 tools() 收集出的执行体调用工具，与宿主登记后的执行路径一致。"""
    executor = dict(
        (spec.name, bound) for spec, bound in plugin.tools()
    )['read_forward_message']
    return await executor.execute(
        ToolInvocation(tool_name='read_forward_message', arguments=arguments),
        _context(stream_id, message_watermark),
    )


async def _read_all_pages(
    plugin: ForwardMessagePlugin,
    arguments: Dict[str, Any],
) -> List[ToolExecutionResult]:
    """沿工具返回的 nextOffset 读取完同一份确定性渲染结果。"""
    pages: List[ToolExecutionResult] = []
    offset = 0
    for _ in range(100):
        page_arguments = dict(arguments)
        page_arguments['offset'] = offset
        result = await _execute(plugin, page_arguments)
        assert result.success is True
        pages.append(result)
        next_offset = result.metadata['nextOffset']
        if next_offset is None:
            return pages
        assert isinstance(next_offset, int) and next_offset > offset
        offset = next_offset
    raise AssertionError('合并转发分页未在 100 次读取内结束')


async def test_first_read_expands_one_level_and_returns_nested_path() -> None:
    """首次读取只展示顶层，内层正文必须通过返回路径继续读取。"""
    plugin = ForwardMessagePlugin(_manifest())
    plugin.observe_inbound(
        7, 101, _inbound(7, (_wrap_tree(0, _text_tree('内层', '秘密正文')),)),
    )

    result = await _execute(plugin, {'message_id': 101})

    assert result.success is True
    assert '第0层' in result.observation
    assert '前0' in result.observation
    assert '后0' in result.observation
    assert 'path=[0, 0]' in result.observation
    assert '秘密正文' not in result.observation


async def test_selected_path_expands_exactly_one_more_level() -> None:
    """选择嵌套路径后只展开目标层，下一层仍给出可继续使用的路径。"""
    deepest = _text_tree('最深层', '最深正文')
    middle = ForwardMessageTree(nodes=(ForwardNode(
        sender_name='中层',
        parts=(
            ForwardMessagePart.text_part('中层正文'),
            ForwardMessagePart.forward_part(deepest),
        ),
    ),))
    plugin = ForwardMessagePlugin(_manifest())
    plugin.observe_inbound(7, 101, _inbound(7, (_wrap_tree(0, middle),)))

    result = await _execute(plugin, {'message_id': 101, 'path': [0, 0]})

    assert result.success is True
    assert '中层正文' in result.observation
    assert 'path=[0, 0, 0]' in result.observation
    assert '最深正文' not in result.observation


async def test_deep_path_has_no_small_depth_limit() -> None:
    """工具可沿 64 层路径定位叶子，不把深层转发整体摊平。"""
    plugin = ForwardMessagePlugin(_manifest())
    plugin.observe_inbound(7, 101, _inbound(7, (_deep_tree(64),)))

    result = await _execute(
        plugin,
        {'message_id': 101, 'path': [0, *([0] * 64)]},
    )

    assert result.success is True
    assert '最深层' in result.observation
    assert '叶子正文' in result.observation


async def test_single_call_can_expand_many_nested_levels() -> None:
    """认知轮次有限时可用 depth 一次展开 64 层链，而非每层再付一轮。"""
    plugin = ForwardMessagePlugin(_manifest())
    plugin.observe_inbound(7, 101, _inbound(7, (_deep_tree(64),)))

    result = await _execute(plugin, {'message_id': 101, 'depth': 65})

    assert result.success is True
    assert '第0层' in result.observation
    assert '第63层' in result.observation
    assert '最深层' in result.observation
    assert '叶子正文' in result.observation


async def test_wide_flat_message_tail_is_reachable_by_offset() -> None:
    """单层多节点超过观察上限时，尾部可通过字符游标继续读取。"""
    tree = ForwardMessageTree(nodes=tuple(
        ForwardNode(
            sender_name=f'发送者{index}',
            parts=(ForwardMessagePart.text_part(f'第{index}条-' + '内容' * 20),),
        )
        for index in range(20)
    ) + (
        ForwardNode(
            sender_name='尾部发送者',
            parts=(ForwardMessagePart.text_part('平层终点'),),
        ),
    ))
    plugin = ForwardMessagePlugin(_manifest(), observation_max_chars=160)
    plugin.observe_inbound(7, 101, _inbound(7, (tree,)))

    pages = await _read_all_pages(plugin, {'message_id': 101})

    assert len(pages) > 1
    assert len(pages[0].observation) <= 160
    assert 'next_offset=' in pages[0].observation
    assert pages[0].metadata['nextOffset'] is not None
    assert pages[-1].metadata['nextOffset'] is None
    assert pages[-1].observation.endswith('平层终点')


async def test_single_long_text_tail_is_reachable_by_offset() -> None:
    """单个文本片段本身超过上限时也不能因缺少节点游标而永久丢失尾部。"""
    plugin = ForwardMessagePlugin(_manifest(), observation_max_chars=128)
    plugin.observe_inbound(
        7, 101, _inbound(7, (_text_tree('长文用户', '正文' * 500 + '长文终界'),)),
    )

    pages = await _read_all_pages(plugin, {'message_id': 101})

    assert len(pages) > 1
    assert all(len(page.observation) <= 128 for page in pages)
    assert 'next_offset=' in pages[0].observation
    assert pages[-1].observation.endswith('长文终界')


async def test_offset_at_or_after_content_end_is_visible_failure() -> None:
    """无效分页游标应明确报出总长度，不能静默返回空成功。"""
    plugin = ForwardMessagePlugin(_manifest())
    plugin.observe_inbound(7, 101, _inbound(7, (_text_tree('用户', '短正文'),)))

    result = await _execute(plugin, {'message_id': 101, 'offset': 9999})

    assert result.success is False
    assert 'offset 9999 已到达或超出合并转发内容末尾' in result.error_message
    assert '总字符数' in result.error_message


async def test_invalid_path_is_visible_tool_failure() -> None:
    """越界路径应返回精确失败观察，不能退回根层或抛成模型故障。"""
    plugin = ForwardMessagePlugin(_manifest())
    plugin.observe_inbound(7, 101, _inbound(7, (_text_tree('用户', '正文'),)))

    result = await _execute(plugin, {'message_id': 101, 'path': [0, 0]})

    assert result.success is False
    assert '第 2 级索引 0 超出范围' in result.error_message


async def test_message_cache_is_isolated_by_stream() -> None:
    """同一个内部消息编号不能被其他会话读取。"""
    plugin = ForwardMessagePlugin(_manifest())
    plugin.observe_inbound(
        7, 101, _inbound(7, (_text_tree('用户', '仅本群可见'),)),
    )

    result = await _execute(plugin, {'message_id': 101}, stream_id=8)

    assert result.success is False
    assert '当前会话中没有可读取的合并转发消息 101' in result.error_message
    # 另一个会话什么都没缓存，不能反过来把本会话的编号漏给它。
    assert '本会话目前没有任何可读取的合并转发' in result.error_message


async def test_failure_lists_readable_message_ids() -> None:
    """读不到时必须给出本会话可读的编号，不能让模型继续猜。

    工具声明按会话给出，可读性却是按消息的：同一会话里有的转发解析成功进了
    缓存，有的因协议端超时或结构损坏没进，而正文里都只是 [转发消息] 占位。
    失败信息不列出可读编号，模型就只能挨个试。
    """
    plugin = ForwardMessagePlugin(_manifest())
    plugin.observe_inbound(7, 100, _inbound(7, (_text_tree('用户', '可读一'),)))
    plugin.observe_inbound(7, 102, _inbound(7, (_text_tree('用户', '可读二'),)))

    result = await _execute(plugin, {'message_id': 101}, message_watermark=102)

    assert result.success is False
    assert '本会话可读取的是 100、102' in result.error_message


async def test_failure_hint_stops_at_round_watermark() -> None:
    """可读编号清单不能越过本回合水位，否则失败信息会泄露在飞消息。"""
    plugin = ForwardMessagePlugin(_manifest())
    plugin.observe_inbound(7, 100, _inbound(7, (_text_tree('用户', '快照内'),)))
    plugin.observe_inbound(7, 200, _inbound(7, (_text_tree('用户', '快照后'),)))

    result = await _execute(plugin, {'message_id': 101}, message_watermark=150)

    assert result.success is False
    assert '本会话可读取的是 100' in result.error_message
    assert '200' not in result.error_message


async def test_message_after_round_watermark_cannot_be_read() -> None:
    """在飞回合不能读取其固定快照之后才入站并写入缓存的转发。"""
    plugin = ForwardMessagePlugin(_manifest())
    plugin.observe_inbound(
        7, 102, _inbound(7, (_text_tree('后来用户', '未来消息正文'),)),
    )

    result = await _execute(
        plugin,
        {'message_id': 102},
        message_watermark=101,
    )

    assert result.success is False
    assert '消息 102 晚于当前回合消息水位 101' in result.error_message
    assert '未来消息正文' not in result.error_message


async def test_cache_evicts_oldest_message_at_limit() -> None:
    """原始转发树缓存有明确容量上限，长期运行时不会无限增长。"""
    plugin = ForwardMessagePlugin(_manifest(), cache_limit=2)
    plugin.observe_inbound(7, 101, _inbound(7, (_text_tree('甲', '一'),)))
    plugin.observe_inbound(7, 102, _inbound(7, (_text_tree('乙', '二'),)))
    plugin.observe_inbound(7, 103, _inbound(7, (_text_tree('丙', '三'),)))

    evicted = await _execute(plugin, {'message_id': 101})
    retained = await _execute(
        plugin,
        {'message_id': 103},
        message_watermark=103,
    )

    assert evicted.success is False
    assert retained.success is True
    assert '三' in retained.observation


async def test_textless_layer_is_summarized_instead_of_listed() -> None:
    """整层都是非文本占位时概括成一行，不逐条列出。

    真机 2026-09-02：一层 20 条全是 [表情包]/[图片]，逐条列出对模型零信息量，
    却让她为此白花两个认知轮次。概括保留「多少条、都是什么」，丢掉的只是
    「第几条是谁发的」——在全是占位的层里那不构成信息。
    """
    media = ForwardMessageTree(nodes=tuple(
        ForwardNode(
            sender_name='『』',
            parts=(ForwardMessagePart.text_part(
                '[图片]' if index % 9 == 0 else '[表情包]'
            ),),
        )
        for index in range(20)
    ))
    plugin = ForwardMessagePlugin(_manifest())
    plugin.observe_inbound(7, 101, _inbound(7, (media,)))

    result = await _execute(plugin, {'message_id': 101})

    assert result.success is True
    assert '这一层 20 条，全部为非文本内容' in result.observation
    assert '[表情包]×17' in result.observation
    assert '[图片]×3' in result.observation
    # 20 行同样的占位不该再出现
    assert result.observation.count('[表情包]') == 1


async def test_nested_marker_carries_size_and_textlessness() -> None:
    """未展开的嵌套要带上规模，模型才能在展开前判断值不值。"""
    media = ForwardMessageTree(nodes=tuple(
        ForwardNode(
            sender_name='『』',
            parts=(ForwardMessagePart.text_part('[表情包]'),),
        )
        for _ in range(20)
    ))
    talk = ForwardMessageTree(nodes=(
        ForwardNode(
            sender_name='群友',
            parts=(ForwardMessagePart.text_part('这个真的假的'),),
        ),
    ))
    root = ForwardMessageTree(nodes=(
        ForwardNode(sender_name='『』', parts=(
            ForwardMessagePart.forward_part(media),
            ForwardMessagePart.forward_part(talk),
        )),
    ))
    plugin = ForwardMessagePlugin(_manifest())
    plugin.observe_inbound(7, 101, _inbound(7, (root,)))

    result = await _execute(plugin, {'message_id': 101})

    assert '[嵌套合并转发，20 条，无文本，path=[0, 0]]' in result.observation
    # 有正文的子树只报条数，不标「无文本」
    assert '[嵌套合并转发，1 条，path=[0, 1]]' in result.observation
