"""验证外部只读工具从注册、执行到观察回灌的完整决策链。"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Tuple

import pytest

from src.core.agent.action_protocol import (
    DecisionFrame,
    GateInputFacts,
    PlatformCapabilities,
    available_actions,
)
from src.core.agent.conversation import ConversationAgent
from src.core.tooling.registry import build_builtin_action_registry
from src.core.tooling.spec import (
    ToolContext,
    ToolExecutionResult,
    ToolInvocation,
    ToolSpec,
)


class _ScriptedProvider:
    """逐次返回预设分片，并记录每轮声明与消息。"""

    provider = 'fake'
    model = 'fake-model'

    def __init__(self, scripts: List[List[Dict[str, Any]]]) -> None:
        self._scripts = scripts
        self.calls = 0
        self.seen_messages: List[List[Dict[str, Any]]] = []
        self.seen_tools: List[List[Dict[str, Any]]] = []

    async def stream(
        self,
        *,
        messages: List[Dict[str, Any]],
        tools=None,
        **_kwargs: Any,
    ) -> AsyncIterator[Dict[str, Any]]:
        self.seen_messages.append([dict(item) for item in messages])
        self.seen_tools.append(list(tools or []))
        script = self._scripts[self.calls]
        self.calls += 1
        for chunk in script:
            yield chunk


class _RecordingExecutor:
    """记录调用并返回固定观察文本。"""

    def __init__(self) -> None:
        self.calls: List[Tuple[ToolInvocation, ToolContext]] = []

    async def execute(
        self,
        invocation: ToolInvocation,
        context: ToolContext,
    ) -> ToolExecutionResult:
        self.calls.append((invocation, context))
        return ToolExecutionResult(
            tool_name=invocation.tool_name,
            success=True,
            observation='顶层有一条嵌套转发，path=[0, 0]。',
        )


class _FailingExecutor:
    """模拟缓存或工具实现自身损坏，而不是模型服务商错误。"""

    async def execute(
        self,
        invocation: ToolInvocation,
        context: ToolContext,
    ) -> ToolExecutionResult:
        del invocation, context
        # 执行器自身抛出的 TimeoutError 也不是 wait_for 的执行时限到期。
        raise TimeoutError('工具缓存读取失败')


class _WrongNameExecutor:
    """模拟执行器违反结果名称必须与调用名称一致的本机契约。"""

    async def execute(
        self,
        invocation: ToolInvocation,
        context: ToolContext,
    ) -> ToolExecutionResult:
        del invocation, context
        return ToolExecutionResult(
            tool_name='another_tool',
            success=True,
            observation='错误结果',
        )


def _frame(*, forward_message: bool, rounds: int) -> DecisionFrame:
    caps = PlatformCapabilities(
        plugin_capabilities=(
            frozenset({'forward_message'}) if forward_message else frozenset()
        ),
    )
    return DecisionFrame(
        turn_id=7,
        snapshot_id='snap-7',
        stream_kind='group',
        disposition='deliberate',
        selectable_message_ids=(101,),
        message_watermark=101,
        available_actions=available_actions(
            'group', 'deliberate', caps, cognitive_rounds_left=rounds,
        ),
        capabilities=caps,
    )


def _gate_inputs() -> GateInputFacts:
    return GateInputFacts(
        stream_kind='group',
        mentioned_me=True,
        name_mentioned=False,
        must_reply=True,
        asleep=False,
        rate_limited=False,
        recent_bot_replies=0,
        candidate_message_ids=(101,),
        selectable_message_ids=(101,),
    )


def _tool_spec() -> ToolSpec:
    return ToolSpec(
        name='read_forward_message',
        description='逐层读取合并转发消息。',
        parameters={
            'type': 'object',
            'properties': {
                'message_id': {'type': 'integer', 'minimum': 1},
                'path': {
                    'type': 'array',
                    'items': {'type': 'integer', 'minimum': 0},
                },
            },
            'required': ['message_id'],
            'additionalProperties': False,
        },
        capabilities=frozenset({'forward_message'}),
    )


def _tool_context(frame: DecisionFrame) -> ToolContext:
    return ToolContext(
        stream_id=9,
        stream_kind='group',
        frame=frame,
        turn_id=frame.turn_id,
        snapshot_id=frame.snapshot_id,
    )


async def test_registered_tool_executes_and_observation_returns_to_next_round() -> None:
    """模型工具调用应执行一次、留审计记录，并把结果作为下一轮 user item 回灌。"""
    planner = _ScriptedProvider([
        [{'tool_calls': [{
            'id': 'call-forward',
            'name': 'read_forward_message',
            'arguments': '{"message_id": 101}',
        }]}],
        [{'tool_calls': [{
            'id': 'call-reply',
            'name': 'reply',
            'arguments': (
                '{"target": 101, "reasons": ["directly_addressed"], '
                '"length": "brief", "reference": "读完转发后回答"}'
            ),
        }]}],
    ])
    replyer = _ScriptedProvider([[{'text': '<say>我看完了。</say>'}]])
    executor = _RecordingExecutor()
    registry = build_builtin_action_registry()
    registry.register_tool(_tool_spec(), executor)
    frame = _frame(forward_message=True, rounds=2)
    intermediate: List[Any] = []
    agent = ConversationAgent(
        planner,
        temperature=0.2,
        replyer=replyer,
        tool_calling=True,
        tool_registry=registry,
    )

    outcome = await agent.run(
        frame,
        [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': '看转发'}],
        _gate_inputs(),
        ('name_mentioned',),
        cognitive_rounds=2,
        tool_context=_tool_context(frame),
        on_round=intermediate.append,
        replyer_messages=lambda _head: _replyer_messages(),
    )

    assert outcome.event_status == 'committed'
    assert outcome.cognitive_rounds_used == 1
    assert len(executor.calls) == 1
    invocation, context = executor.calls[0]
    assert invocation.call_id == 'call-forward'
    assert invocation.arguments == {'message_id': 101}
    assert context.stream_id == 9
    assert len(intermediate) == 1
    tool_event = intermediate[0].action_event.to_dict()
    assert tool_event['toolInvocation'] == {
        'name': 'read_forward_message',
        'callId': 'call-forward',
        'arguments': {'message_id': 101},
    }
    assert tool_event['gate']['availableTools'] == ['read_forward_message']
    assert planner.calls == 2
    observation = planner.seen_messages[1][-1]['content']
    assert '工具：read_forward_message' in observation
    assert '"message_id": 101' in observation
    assert 'path=[0, 0]' in observation


async def _replyer_messages() -> List[Dict[str, Any]]:
    return [
        {'role': 'system', 'content': '只输出 say'},
        {'role': 'user', 'content': '请回答'},
    ]


def test_tool_definition_requires_capability_and_cognitive_budget() -> None:
    """没有可读转发或认知预算为零时，工具都不能出现在模型声明中。"""
    registry = build_builtin_action_registry()
    registry.register_tool(_tool_spec(), _RecordingExecutor())

    enabled = registry.build_tool_definitions(_frame(forward_message=True, rounds=2))
    no_content = registry.build_tool_definitions(_frame(forward_message=False, rounds=2))
    no_budget = registry.build_tool_definitions(_frame(forward_message=True, rounds=0))

    enabled_names = {item['function']['name'] for item in enabled}
    no_content_names = {item['function']['name'] for item in no_content}
    no_budget_names = {item['function']['name'] for item in no_budget}
    assert 'read_forward_message' in enabled_names
    assert 'read_forward_message' not in no_content_names
    assert 'read_forward_message' not in no_budget_names


async def test_disabled_tool_calling_does_not_claim_tools_were_declared() -> None:
    """XML 决策模式没有下发工具时，审计事件不能声称模型看到了外部工具。"""
    planner = _ScriptedProvider([[
        {'text': '<decision action="silent" reasons="others_conversation"/>'},
    ]])
    registry = build_builtin_action_registry()
    registry.register_tool(_tool_spec(), _RecordingExecutor())
    frame = _frame(forward_message=True, rounds=2)
    agent = ConversationAgent(
        planner,
        temperature=0.2,
        tool_registry=registry,
    )

    outcome = await agent.run(
        frame,
        [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': '旁观消息'}],
        _gate_inputs(),
        ('natural_timing',),
        cognitive_rounds=2,
    )

    assert outcome.event_status == 'silent_by_choice'
    assert planner.seen_tools == [[]]
    assert 'availableTools' not in outcome.action_event.to_dict()['gate']


async def test_executor_fault_is_not_attributed_to_model_provider() -> None:
    """模型已合法调用工具后发生的本机故障必须原样抛出，不得记成 provider_error。"""
    planner = _ScriptedProvider([[{'tool_calls': [{
        'id': 'broken-call',
        'name': 'read_forward_message',
        'arguments': '{"message_id": 101}',
    }]}]])
    registry = build_builtin_action_registry()
    registry.register_tool(_tool_spec(), _FailingExecutor())
    frame = _frame(forward_message=True, rounds=2)
    agent = ConversationAgent(
        planner,
        temperature=0.2,
        replyer=_ScriptedProvider([]),
        tool_calling=True,
        tool_registry=registry,
    )

    with pytest.raises(TimeoutError, match='工具缓存读取失败'):
        await agent.run(
            frame,
            [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': '看转发'}],
            _gate_inputs(),
            ('name_mentioned',),
            cognitive_rounds=2,
            tool_context=_tool_context(frame),
        )


async def test_executor_result_name_mismatch_is_local_contract_error() -> None:
    """错误结果名属于本机执行器契约故障，不能被记到模型服务商名下。"""
    planner = _ScriptedProvider([[{'tool_calls': [{
        'id': 'wrong-name-call',
        'name': 'read_forward_message',
        'arguments': '{"message_id": 101}',
    }]}]])
    registry = build_builtin_action_registry()
    registry.register_tool(_tool_spec(), _WrongNameExecutor())
    frame = _frame(forward_message=True, rounds=2)
    agent = ConversationAgent(
        planner,
        temperature=0.2,
        replyer=_ScriptedProvider([]),
        tool_calling=True,
        tool_registry=registry,
    )

    with pytest.raises(RuntimeError, match='工具执行结果名称不一致'):
        await agent.run(
            frame,
            [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': '看转发'}],
            _gate_inputs(),
            ('name_mentioned',),
            cognitive_rounds=2,
            tool_context=_tool_context(frame),
        )


async def test_unknown_or_malformed_tool_call_is_illegal_action() -> None:
    """未知字段必须在执行前作为协议错误暴露，执行器不能收到半合法参数。

    第一次坏调用会回灌拒绝原因纠错重试一次；重试仍带未知字段才落
    illegal_action，两次尝试都不触碰执行器。
    """
    malformed = {'tool_calls': [{
        'id': 'bad-call',
        'name': 'read_forward_message',
        'arguments': '{"message_id": 101, "unknown": true}',
    }]}
    planner = _ScriptedProvider([[malformed], [malformed]])
    executor = _RecordingExecutor()
    registry = build_builtin_action_registry()
    registry.register_tool(_tool_spec(), executor)
    frame = _frame(forward_message=True, rounds=2)
    agent = ConversationAgent(
        planner,
        temperature=0.2,
        replyer=_ScriptedProvider([]),
        tool_calling=True,
        tool_registry=registry,
    )

    outcome = await agent.run(
        frame,
        [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': '看转发'}],
        _gate_inputs(),
        ('name_mentioned',),
        cognitive_rounds=2,
        tool_context=_tool_context(frame),
    )

    assert outcome.event_status == 'illegal_action'
    assert '未知字段：unknown' in outcome.action_event.detail
    assert '1 次工具调用纠错' in outcome.action_event.detail
    assert planner.calls == 2
    assert '[被拒绝的工具调用]' in planner.seen_messages[1][-1]['content']
    assert executor.calls == []
