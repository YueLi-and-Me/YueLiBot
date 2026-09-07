"""验证合并转发工具插件的清单、工具声明与两个挂载点。

迁移规格的验收断言：清单能通过契约层校验且类型为 tool；``tools()`` 收集到
恰好一个声明，且与迁移前 ``ForwardMessageTool.spec()`` 逐字段相同；
``observe_inbound`` 之后仅被观察过的会话贡献 ``forward_message`` 能力。

依赖 ``src.plugin_system`` 与插件目录本身。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

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
from src.core.tooling.spec import ToolSpec
from src.plugin_system import PluginManifest, manifest_from_payload

# 插件目录名含连字符，不是合法 Python 包名；与契约层 loader 同法按文件路径
# 加载入口模块，模块名用插件标识派生避免同名覆盖。
_PLUGIN_DIR = (
    Path(__file__).resolve().parents[2]
    / 'src' / 'plugins' / 'built_in' / 'forward-message'
)


def _load_plugin_module() -> object:
    """执行插件入口模块并返回模块对象。"""
    entry = _PLUGIN_DIR / 'plugin.py'
    spec = importlib.util.spec_from_file_location('yueli_forward_message', entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_plugin_module = _load_plugin_module()
ForwardMessagePlugin = _plugin_module.ForwardMessagePlugin


def _manifest() -> PluginManifest:
    """从插件目录读取并经契约层校验清单。"""
    payload = json.loads(
        (_PLUGIN_DIR / '_manifest.json').read_text(encoding='utf-8'),
    )
    return manifest_from_payload(payload)


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


def _single_tree() -> ForwardMessageTree:
    """构造一棵单节点转发树。"""
    return ForwardMessageTree(nodes=(
        ForwardNode(
            sender_name='用户',
            parts=(ForwardMessagePart.text_part('正文'),),
        ),
    ))


def test_manifest_passes_contract_validation_as_tool_plugin() -> None:
    """清单能通过 manifest_from_payload 校验，类型是 tool、标识符合规格。"""
    manifest = _manifest()

    assert manifest.plugin_type == 'tool'
    assert manifest.plugin_id == 'yueli.forward-message'


def test_tools_collects_single_spec_identical_to_prewritten_one() -> None:
    """收集到恰好一个工具，其声明与迁移前 spec() 逐字段相同。

    期望值逐字抄自迁移前 ``ForwardMessageTool.spec()``：ToolSpec 是 frozen
    dataclass，``==`` 覆盖全部字段，含默认值携带的 kind / side_effect /
    timeout_ms / metadata。此断言把声明钉死，防止迁移后的书写位置变化
    带来无声漂移。
    """
    plugin = ForwardMessagePlugin(_manifest())

    collected = plugin.tools()

    assert len(collected) == 1
    expected = ToolSpec(
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
        capabilities=frozenset({'forward_message'}),
    )
    spec = collected[0][0]
    assert spec == expected
    assert spec.kind == 'external'
    assert spec.side_effect == 'readonly'


def test_observe_inbound_gates_capability_per_stream() -> None:
    """观察过转发消息的会话贡献 forward_message 能力，未观察过的为空集合。"""
    plugin = ForwardMessagePlugin(_manifest())

    for handler in plugin.inbound_observers():
        handler(7, 101, _inbound(7, (_single_tree(),)))

    assert plugin.stream_capabilities(7) == frozenset({'forward_message'})
    assert plugin.stream_capabilities(8) == frozenset()


def test_observe_inbound_without_forward_trees_contributes_nothing() -> None:
    """入站消息没有转发根树时不建缓存条目，会话能力保持空集合。"""
    plugin = ForwardMessagePlugin(_manifest())

    for handler in plugin.inbound_observers():
        handler(7, 101, _inbound(7, ()))

    assert plugin.stream_capabilities(7) == frozenset()
