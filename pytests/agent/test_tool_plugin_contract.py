"""验证工具插件契约：@tool 声明、收集顺序与挂载点的默认行为。

这是后续所有工具插件的唯一依据，因此锁死的是语义：装饰器产出的仍是 ToolSpec、
被装饰的方法本身即执行体、插件内重名当场暴露、未声明的挂载点必须是安全默认值。

依赖 ``src.plugin_system.tools`` 与 ``src.plugin_system.components``。
"""

from __future__ import annotations

from typing import FrozenSet

import pytest

from src.core.agent.action_protocol import (
    DecisionFrame,
    PlatformCapabilities,
    available_actions,
)
from src.core.tooling.spec import ToolContext, ToolExecutionResult, ToolInvocation
from src.plugin_system import (
    PluginManifest,
    ToolPlugin,
    inbound_observe,
    tool,
)


def _manifest() -> PluginManifest:
    """构造一份工具插件清单。"""
    return PluginManifest(
        plugin_id='test.sample-tool',
        plugin_type='tool',
        name='Sample',
        version='0.1.0',
        description='测试用工具插件',
    )


def _context() -> ToolContext:
    """构造一份最小工具上下文；本模块只验证转发，不依赖帧的具体内容。"""
    caps = PlatformCapabilities()
    return ToolContext(
        stream_id=3,
        stream_kind='group',
        frame=DecisionFrame(
            turn_id=1,
            snapshot_id='turn-1',
            stream_kind='group',
            disposition='deliberate',
            selectable_message_ids=(1,),
            message_watermark=1,
            available_actions=available_actions(
                'group', 'deliberate', caps, cognitive_rounds_left=1,
            ),
            capabilities=caps,
        ),
        turn_id=1,
        snapshot_id='turn-1',
    )


class _SamplePlugin(ToolPlugin):
    """声明两个工具与一个观察组件。"""

    def __init__(self) -> None:
        """记录执行与观察调用，供断言检查。"""
        super().__init__(_manifest())
        self.seen: list[tuple[int, int]] = []

    @tool(name='beta', description='第二个工具')
    async def handle_beta(
        self,
        invocation: ToolInvocation,
        context: ToolContext,
    ) -> ToolExecutionResult:
        """回显工具名，用于验证绑定方法确实被调用。"""
        return ToolExecutionResult(
            tool_name=invocation.tool_name,
            success=True,
            observation='beta 执行了',
        )

    @tool(
        name='alpha',
        description='第一个工具',
        parameters={'type': 'object', 'properties': {'q': {'type': 'string'}}},
        capabilities=('forward_message',),
        timeout_ms=1234,
    )
    async def handle_alpha(
        self,
        invocation: ToolInvocation,
        context: ToolContext,
    ) -> ToolExecutionResult:
        """空实现，只用于检查声明。"""
        return ToolExecutionResult(tool_name=invocation.tool_name, success=True)

    @inbound_observe()
    def observe_inbound(self, stream_id: int, message_id: int, inbound: object) -> None:
        """记录观察到的消息。"""
        self.seen.append((stream_id, message_id))

    def stream_capabilities(self, stream_id: int) -> FrozenSet[str]:
        """只在指定会话贡献能力。"""
        return frozenset({'forward_message'}) if stream_id == 3 else frozenset()


class _BarePlugin(ToolPlugin):
    """不声明任何组件，也不覆写挂载点，用于验证默认值。"""


def test_decorator_collects_specs_sorted_by_name() -> None:
    """收集结果按工具名排序，保证登记顺序可复现。"""
    tools = _SamplePlugin().tools()

    assert [spec.name for spec, _executor in tools] == ['alpha', 'beta']


def test_decorator_produces_toolspec_with_declared_fields() -> None:
    """装饰器产出的仍是 ToolSpec，字段逐项透传。"""
    spec = dict((s.name, s) for s, _e in _SamplePlugin().tools())['alpha']

    assert spec.description == '第一个工具'
    assert spec.parameters['properties']['q']['type'] == 'string'
    assert spec.capabilities == frozenset({'forward_message'})
    assert spec.timeout_ms == 1234
    # kind 固定为 external：终局与认知动作是封闭枚举，不允许插件自称那两类。
    assert spec.kind == 'external'
    assert spec.side_effect == 'readonly'


@pytest.mark.asyncio
async def test_decorated_method_is_the_executor() -> None:
    """被装饰的方法本身即执行体，绑定的 self 已就位。"""
    executor = dict((s.name, e) for s, e in _SamplePlugin().tools())['beta']

    result = await executor.execute(
        ToolInvocation(tool_name='beta', arguments={}),
        _context(),
    )

    assert result.success is True
    assert result.observation == 'beta 执行了'


def test_plugin_without_tools_returns_empty() -> None:
    """没有声明工具的插件返回空列表，而不是报错。"""
    assert _BarePlugin(_manifest()).tools() == []


def test_default_mount_points_are_inert() -> None:
    """未声明组件的插件必须是安全默认值：不观察、不贡献任何能力。"""
    plugin = _BarePlugin(_manifest())

    assert plugin.tools() == []
    assert plugin.inbound_observers() == []
    assert plugin.inbound_rewrites() == []
    assert plugin.commands() == []
    assert plugin.stream_capabilities(3) == frozenset()


def test_inbound_observer_component_is_collected_and_bound() -> None:
    """``@inbound_observe`` 声明的方法被收集为组件，调用时 self 已就位。"""
    plugin = _SamplePlugin()

    observers = plugin.inbound_observers()

    assert len(observers) == 1
    observers[0](3, 9, object())
    assert plugin.seen == [(3, 9)]


def test_duplicate_tool_name_within_plugin_is_rejected() -> None:
    """插件内重名当场暴露；跨插件重名由注册表拒绝，两层判据不重叠。"""

    class _Duplicated(ToolPlugin):
        @tool(name='same', description='一号')
        async def first(self, invocation, context):  # type: ignore[no-untyped-def]
            """占位。"""

        @tool(name='same', description='二号')
        async def second(self, invocation, context):  # type: ignore[no-untyped-def]
            """占位。"""

    with pytest.raises(ValueError, match='重复声明了工具 same'):
        _Duplicated(_manifest()).tools()


def test_sync_handler_is_rejected_at_declaration() -> None:
    """执行体必须是 async：同步实现会在执行层被 await 炸掉，越早拒绝越好定位。"""
    with pytest.raises(ValueError, match='必须是 async 方法'):

        class _Sync(ToolPlugin):
            @tool(name='sync-tool', description='同步实现')
            def handler(self, invocation, context):  # type: ignore[no-untyped-def]
                """占位。"""


def test_manifest_type_is_carried() -> None:
    """工具插件的清单类型是 tool，宿主据此分派到工具挂载点。"""
    assert _SamplePlugin().manifest.plugin_type == 'tool'
