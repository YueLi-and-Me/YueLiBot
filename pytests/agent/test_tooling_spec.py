"""统一工具协议数据结构的形状自检回归。

这里只验「坏形状必须当场拒绝」：协议结构是执行层与注册表的公共底座，
形状错误晚到执行期才暴露会把工具故障错记成模型故障。
"""

from __future__ import annotations

import pytest

from src.core.agent.action_protocol import DecisionFrame, PlatformCapabilities, available_actions
from src.core.tooling.spec import (
    DEFAULT_TOOL_TIMEOUT_MS,
    ToolContext,
    ToolExecutionResult,
    ToolInvocation,
    ToolSpec,
)


def _frame() -> DecisionFrame:
    """构造一个合法的群聊回合帧，仅供上下文结构使用。"""
    caps = PlatformCapabilities()
    return DecisionFrame(
        turn_id=1,
        snapshot_id='snap-1',
        stream_kind='group',
        disposition='deliberate',
        selectable_message_ids=(101,),
        message_watermark=101,
        available_actions=available_actions('group', 'deliberate', caps),
        capabilities=caps,
    )


def test_tool_spec_defaults() -> None:
    """默认分类是外部工具、默认副作用是只读、默认超时取统一常量。"""
    spec = ToolSpec(name='demo')

    assert spec.kind == 'external'
    assert spec.side_effect == 'readonly'
    assert spec.timeout_ms == DEFAULT_TOOL_TIMEOUT_MS
    assert spec.capabilities == frozenset()


def test_tool_spec_rejects_blank_name() -> None:
    """工具名是模型可见的协议面，空白名字没有登记意义。"""
    with pytest.raises(ValueError):
        ToolSpec(name='   ')


def test_tool_spec_rejects_nonpositive_timeout() -> None:
    """超时非正等于没有约束，慢工具会把整个回合拖死。"""
    with pytest.raises(ValueError):
        ToolSpec(name='demo', timeout_ms=0)


def test_tool_spec_rejects_blank_capability() -> None:
    """能力集里的空字符串会让过滤永远对不上任何真实能力。"""
    with pytest.raises(ValueError):
        ToolSpec(name='demo', capabilities=frozenset({''}))


def test_tool_invocation_rejects_blank_name() -> None:
    """没有工具名的调用无法路由。"""
    with pytest.raises(ValueError):
        ToolInvocation(tool_name='')


def test_tool_result_failure_requires_reason() -> None:
    """失败的观察会回灌模型，没有原因的失败只会诱导模型再试一次。"""
    with pytest.raises(ValueError):
        ToolExecutionResult(tool_name='demo', success=False)


def test_tool_result_success_needs_no_reason() -> None:
    """成功结果不带失败原因是合法形状。"""
    result = ToolExecutionResult(tool_name='demo', success=True, observation='ok')

    assert result.error_message == ''
    assert result.observation == 'ok'


def test_tool_context_rejects_blank_snapshot() -> None:
    """快照标识把工具执行挂回回合账本，缺失会让审计断链。"""
    with pytest.raises(ValueError):
        ToolContext(
            stream_id=1,
            stream_kind='group',
            frame=_frame(),
            turn_id=1,
            snapshot_id='',
        )
