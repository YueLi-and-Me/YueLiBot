"""一轮多工具执行语义验收。

对应工具系统方案第五节的语义表：按序串行执行、终局动作至多一个且出现即截断、
多认知工具按条数扣减预算、超支无终局按协议错误记账、本机故障原样上抛。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, List

import pytest

from src.core.agent.action_protocol import (
    DecisionFrame,
    GateInputFacts,
    PlatformCapabilities,
    available_actions,
)
from src.core.agent.cognition import CognitiveObservation, CognitiveScope
from src.core.agent.conversation import ConversationAgent
from src.core.observe.store import event_store
from src.core.tooling.cognitive import CognitiveToolExecutor
from src.core.tooling.registry import build_builtin_action_registry


class _ScriptedToolProvider:
    """每次调用返回脚本里下一轮的全部工具调用，并保留实际收到的消息流。"""

    def __init__(self, scripts: List[List[tuple[str, str]]]) -> None:
        self.scripts = scripts
        self.calls_made = 0
        self.seen_messages: List[List[dict]] = []

    async def stream(self, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        index = self.calls_made
        self.calls_made += 1
        self.seen_messages.append(list(kwargs.get('messages') or []))
        if index >= len(self.scripts):
            raise AssertionError(f'工具脚本只准备了 {len(self.scripts)} 轮')
        yield {'tool_calls': [
            {
                'id': f'call_{i + 1}',
                'name': name,
                'arguments': arguments,
            }
            for i, (name, arguments) in enumerate(self.scripts[index])
        ]}


class _Replyer:
    """只负责产出正文的替身模型。"""

    async def stream(self, **kwargs: Any) -> AsyncIterator[dict[str, str]]:
        del kwargs
        yield {'text': '<say>好。</say>'}


class _FakeAction:
    """记录检索词并返回固定观察的替身认知动作。"""

    def __init__(self, name: str, text: str = '查到了。') -> None:
        self.name = name
        self._text = text
        self.queries: List[str] = []

    async def execute(self, request: Any) -> CognitiveObservation:
        self.queries.append(request.query)
        return CognitiveObservation(text=self._text, hit_count=1)


class _BoomAction:
    """一执行就抛本机故障的替身认知动作。"""

    name = 'recall'

    async def execute(self, request: Any) -> CognitiveObservation:
        del request
        raise RuntimeError('数据库炸了')


def _caps() -> PlatformCapabilities:
    return PlatformCapabilities()


def _frame(*, cognitive_rounds: int = 2) -> DecisionFrame:
    return DecisionFrame(
        turn_id=7,
        snapshot_id='snap-7',
        stream_kind='group',
        disposition='deliberate',
        selectable_message_ids=(101, 102),
        message_watermark=102,
        available_actions=available_actions(
            'group', 'deliberate', _caps(), cognitive_rounds_left=cognitive_rounds,
        ),
        capabilities=_caps(),
    )


def _gate_inputs() -> GateInputFacts:
    return GateInputFacts(
        stream_kind='group',
        mentioned_me=False,
        name_mentioned=True,
        must_reply=False,
        asleep=False,
        rate_limited=False,
        recent_bot_replies=0,
        candidate_message_ids=(101, 102),
        selectable_message_ids=(101, 102),
    )


_SCOPE = CognitiveScope(stream_id=3, person_ids=(11, 12))


def _registry(actions: List[Any]) -> Any:
    registry = build_builtin_action_registry()
    for action in actions:
        registry.bind_action_executor(action.name, CognitiveToolExecutor(action))
    return registry


async def _run_tools(
    scripts: List[List[tuple[str, str]]],
    *,
    rounds: int,
    actions: List[Any],
    on_round=None,
):
    planner = _ScriptedToolProvider(scripts)
    agent = ConversationAgent(
        planner,
        temperature=0.7,
        replyer=_Replyer(),
        tool_calling=True,
        tool_registry=_registry(actions),
    )

    async def replyer_messages(_head: Any) -> list[dict]:
        return [
            {'role': 'system', 'content': '只负责把话说出来'},
            {'role': 'user', 'content': '请输出 say'},
        ]

    return agent, planner, await agent.run(
        _frame(cognitive_rounds=rounds),
        [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': '在么'}],
        _gate_inputs(),
        ('name_mentioned',),
        cognitive_scope=_SCOPE,
        cognitive_rounds=rounds,
        replyer_messages=replyer_messages,
        on_round=on_round,
    )


def _tool_events() -> list[dict]:
    """按时间正序返回本用例产生的工具执行事件。"""
    return list(reversed(event_store.search(kinds=['tool_execution']).events))


_REPLY_CALL = (
    'reply',
    '{"target": 101, "reasons": ["pending_thread"], "length": "brief", '
    '"reference": "接着回答"}',
)


async def test_multiple_cognitive_tools_execute_in_order() -> None:
    """一轮响应里的多个认知工具按返回顺序逐个执行，观察逐条回灌。"""
    recall = _FakeAction('recall', text='关于上次演出查到这些。')
    inspect = _FakeAction('inspect', text='水位之前有三条相关消息。')
    cognitive_rounds: List[Any] = []

    def on_round(round_outcome: Any) -> None:
        cognitive_rounds.append(round_outcome)

    _, planner, outcome = await _run_tools(
        [
            [
                ('recall', '{"query": "上次的演出"}'),
                ('inspect', '{"query": "演出"}'),
            ],
            [_REPLY_CALL],
        ],
        rounds=3,
        actions=[recall, inspect],
        on_round=on_round,
    )
    # 首个认知轮的明细在 on_round 回调里；终局轮的 cognitive_steps 恒为空。
    first_round = cognitive_rounds[0]
    assert first_round.event_status == 'cognitive_step'
    assert [step[0] for step in first_round.cognitive_steps] == ['recall', 'inspect']

    assert outcome.event_status == 'committed'
    assert outcome.cognitive_steps == ()
    assert recall.queries == ['上次的演出']
    assert inspect.queries == ['演出']
    # 两条观察各自成块回灌；预算 3 用掉 2 还有剩余，不追加收束指令。
    observation = planner.seen_messages[1][-1]['content']
    assert '动作：recall' in observation
    assert '查询：上次的演出' in observation
    assert '关于上次演出查到这些。' in observation
    assert '动作：inspect' in observation
    assert '水位之前有三条相关消息。' in observation
    assert '用完' not in observation
    events = _tool_events()
    assert [event['toolName'] for event in events] == ['recall', 'inspect']
    assert all(event['eventStatus'] == 'committed' for event in events)


async def test_mixed_response_recall_then_reply() -> None:
    """认知工具之后跟终局动作：检索照常执行，回合以终局动作结束。"""
    recall = _FakeAction('recall')
    _, _, outcome = await _run_tools(
        [[('recall', '{"query": "上次的演出"}'), _REPLY_CALL]],
        rounds=2,
        actions=[recall],
    )

    assert outcome.event_status == 'committed'
    assert [step[0] for step in outcome.cognitive_steps] == ['recall']
    assert recall.queries == ['上次的演出']
    assert [event['eventStatus'] for event in _tool_events()] == ['committed']


async def test_terminal_action_truncates_later_calls() -> None:
    """终局动作之后的调用不执行、不计为错误，只记 discarded 轻量事件。"""
    recall = _FakeAction('recall')
    _, _, outcome = await _run_tools(
        [
            [
                ('silent', '{"reasons": ["no_new_value"]}'),
                ('recall', '{"query": "不该执行"}'),
            ]
        ],
        rounds=2,
        actions=[recall],
    )

    assert outcome.event_status == 'silent_by_choice'
    assert outcome.cognitive_steps == ()
    assert recall.queries == [], '终局动作之后的认知工具必须不被执行'
    events = _tool_events()
    assert [event['eventStatus'] for event in events] == ['discarded']
    assert events[0]['toolName'] == 'recall'


async def test_overspent_budget_without_terminal_is_illegal() -> None:
    """预算 1 但一轮执行两个认知工具且没有终局动作：按协议错误记账，不降级。"""
    recall = _FakeAction('recall')
    inspect = _FakeAction('inspect')
    _, _, outcome = await _run_tools(
        [
            [
                ('recall', '{"query": "甲"}'),
                ('inspect', '{"query": "乙"}'),
            ]
        ],
        rounds=1,
        actions=[recall, inspect],
    )

    assert outcome.event_status == 'illegal_action'
    assert '预算耗尽' in outcome.action_event.detail
    # 执行不拦：两个工具都已执行，约束写在账本而不是执行前拦截。
    assert recall.queries == ['甲']
    assert inspect.queries == ['乙']


async def test_tool_crash_propagates_with_failed_event() -> None:
    """内置认知工具的本机故障原样上抛，并先记一条 failed 工具事件。"""
    with pytest.raises(RuntimeError, match='数据库炸了'):
        await _run_tools(
            [[('recall', '{"query": "甲"}')]],
            rounds=2,
            actions=[_BoomAction()],
        )

    events = _tool_events()
    assert [event['eventStatus'] for event in events] == ['failed']
    assert events[0]['toolName'] == 'recall'
