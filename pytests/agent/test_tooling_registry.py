"""统一工具注册表的登记、解析、声明过滤与外部工具接入回归。

动作声明与 tool_schema 同源生成、逐字一致；外部工具按当前帧能力和认知预算
过滤。这里盯住重名拒绝、解析分派与不具备外部能力时的声明输出不变。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.core.agent.action_protocol import (
    ALL_ACTIONS,
    COGNITIVE_ACTIONS,
    TERMINAL_ACTIONS,
    DecisionFrame,
    PlatformCapabilities,
    available_actions,
)
from src.core.agent.tool_schema import build_tool_definitions
from src.core.tooling.registry import ToolRegistry, build_builtin_action_registry
from src.core.tooling.spec import ToolContext, ToolExecutionResult, ToolInvocation, ToolSpec


class _EchoExecutor:
    """把参数原样写回观察的最小执行器，仅供登记路径测试。"""

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolExecutionResult:
        del context
        return ToolExecutionResult(
            tool_name=invocation.tool_name,
            success=True,
            observation=str(invocation.arguments),
        )


def _caps(**overrides: Any) -> PlatformCapabilities:
    return PlatformCapabilities(**overrides)


def _frame(**overrides: Any) -> DecisionFrame:
    caps = overrides.pop('capabilities', _caps())
    base: dict[str, Any] = dict(
        turn_id=7,
        snapshot_id='snap-7',
        stream_kind='group',
        disposition='deliberate',
        selectable_message_ids=(101, 102),
        message_watermark=102,
        available_actions=available_actions('group', 'deliberate', caps),
        capabilities=caps,
    )
    base.update(overrides)
    return DecisionFrame(**base)


def test_builtin_registry_registers_all_actions() -> None:
    """内置注册表覆盖动作协议的全部封闭枚举，不在这里复制第二份名单。"""
    registry = build_builtin_action_registry()

    assert set(registry.action_names()) == set(ALL_ACTIONS)
    assert len(TERMINAL_ACTIONS) + len(COGNITIVE_ACTIONS) == len(ALL_ACTIONS)


def test_registry_definitions_match_action_definitions() -> None:
    """注册表输出与动作声明逐字一致：判据只有 tool_schema 一份。"""
    frame = _frame()
    registry = build_builtin_action_registry()

    assert registry.build_tool_definitions(frame) == build_tool_definitions(frame)


def test_duplicate_action_registration_rejected() -> None:
    """工具名是模型可见的协议面，重名必须当场暴露而不是静默覆盖。"""
    registry = build_builtin_action_registry()

    with pytest.raises(ValueError):
        registry.register_action('reply')


def test_tool_name_colliding_with_action_rejected() -> None:
    """外部工具不允许占用封闭动作名，否则解析无法区分两条路径。"""
    registry = build_builtin_action_registry()

    with pytest.raises(ValueError):
        registry.register_tool(ToolSpec(name='reply'), _EchoExecutor())


def test_duplicate_tool_registration_rejected() -> None:
    """同名外部工具同样拒绝，第一个登记者胜出没有静默语义。"""
    registry = ToolRegistry()

    registry.register_tool(ToolSpec(name='demo'), _EchoExecutor())
    with pytest.raises(ValueError):
        registry.register_tool(ToolSpec(name='demo'), _EchoExecutor())


def test_resolve_action_routes_to_action_kind() -> None:
    """动作名解析为动作路径，声明与执行器都为空。"""
    registry = build_builtin_action_registry()

    resolved = registry.resolve('recall')

    assert resolved is not None
    assert resolved.kind == 'action'
    assert resolved.spec is None
    assert resolved.executor is None


def test_resolve_tool_routes_to_tool_kind() -> None:
    """外部工具解析为工具路径，声明与执行器齐备。"""
    registry = ToolRegistry()
    spec = ToolSpec(name='demo', description='演示工具')
    executor = _EchoExecutor()
    registry.register_tool(spec, executor)

    resolved = registry.resolve('demo')

    assert resolved is not None
    assert resolved.kind == 'tool'
    assert resolved.spec is spec
    assert resolved.executor is executor


def test_resolve_unknown_returns_none() -> None:
    """未登记的名字返回空，由解析链路按非法动作处理，注册表不替它拍板。"""
    registry = build_builtin_action_registry()

    assert registry.resolve('made_up_tool') is None
