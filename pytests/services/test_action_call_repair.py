"""动作工具调用纠错回路验收。

覆盖六件事：
1. 网关丢弃参数（空对象直达校验层）时，回灌拒绝原因重发一次即可自愈，
   回合正常 committed；
2. 重试仍不合法按 illegal_action 终局，且模型调用次数恰好是原始一次加纠错一次；
3. XML 动作头路径的协议错误保持直接终局，不进入纠错回路；
4. 工具模式下模型只输出正文（不调任何工具）时回灌纠错重发一次即可自愈；
5. 纠错后仍不调工具按 parse_error 终局，且正文不流向用户；
6. 正文先于工具调用流出时正文被丢弃、调用照常结算，不触发纠错重发。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, List

from src.core.agent.action_protocol import (
    DecisionFrame,
    GateInputFacts,
    PlatformCapabilities,
    available_actions,
)
from src.core.agent.conversation import ConversationAgent
from src.core.agent.parser import ParseEvent


class _ToolCallProvider:
    """逐次返回脚本给定的工具调用，并保留每次实际收到的消息序列。"""

    def __init__(self, calls: List[tuple[str, str]]) -> None:
        self.calls = calls
        self.seen_messages: List[List[dict]] = []

    async def stream(self, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        index = len(self.seen_messages)
        self.seen_messages.append(list(kwargs.get('messages') or []))
        if index >= len(self.calls):
            raise AssertionError(f'工具脚本只准备了 {len(self.calls)} 次，第 {index + 1} 次无输出')
        name, arguments = self.calls[index]
        yield {'tool_calls': [{
            'id': f'call_{index + 1}',
            'name': name,
            'arguments': arguments,
        }]}


class _TextProvider:
    """逐次返回脚本给定的文本分片；脚本耗尽再被调用即视为多余的纠错重试。"""

    def __init__(self, scripts: List[List[str]]) -> None:
        self.scripts = scripts
        self.seen_messages: List[List[dict]] = []

    async def stream(self, **kwargs: Any) -> AsyncIterator[dict[str, str]]:
        index = len(self.seen_messages)
        self.seen_messages.append(list(kwargs.get('messages') or []))
        if index >= len(self.scripts):
            raise AssertionError(f'脚本只准备了 {len(self.scripts)} 次，第 {index + 1} 次无输出')
        for text in self.scripts[index]:
            yield {'text': text}


class _ChunkProvider:
    """逐次回放脚本给定的原始增量分片，用于构造正文与工具调用混流的响应。"""

    def __init__(self, scripts: List[List[dict[str, Any]]]) -> None:
        self.scripts = scripts
        self.seen_messages: List[List[dict]] = []

    async def stream(self, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        index = len(self.seen_messages)
        self.seen_messages.append(list(kwargs.get('messages') or []))
        if index >= len(self.scripts):
            raise AssertionError(f'脚本只准备了 {len(self.scripts)} 次，第 {index + 1} 次无输出')
        for chunk in self.scripts[index]:
            yield chunk


def _caps() -> PlatformCapabilities:
    return PlatformCapabilities()


def _frame() -> DecisionFrame:
    return DecisionFrame(
        turn_id=7,
        snapshot_id='snap-7',
        stream_kind='group',
        disposition='deliberate',
        selectable_message_ids=(101, 102),
        message_watermark=102,
        available_actions=available_actions('group', 'deliberate', _caps()),
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


async def _replyer_messages(_head: Any) -> list[dict]:
    return [
        {'role': 'system', 'content': '只负责把话说出来'},
        {'role': 'user', 'content': '请输出 say'},
    ]


async def _collect_events(events: List[ParseEvent], sink: List[ParseEvent]) -> None:
    sink.extend(events)


_VALID_REPLY = (
    '{"target": 101, "reasons": ["directly_addressed"], "length": "brief", '
    '"reference": "对方在叫她，回应这条消息"}'
)


async def test_empty_arguments_are_repaired_and_round_commits() -> None:
    """空参数工具调用回灌纠错后自愈：两次模型调用、回合 committed。"""
    planner = _ToolCallProvider([
        ('reply', '{}'),
        ('reply', _VALID_REPLY),
    ])
    replyer = _TextProvider([['<say emotion="normal">嗯，在呢。</say>']])
    agent = ConversationAgent(
        planner, temperature=0.7, replyer=replyer, tool_calling=True,
    )

    outcome = await agent.run(
        _frame(),
        [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': '小璃别睡了'}],
        _gate_inputs(),
        ('name_mentioned',),
        replyer_messages=_replyer_messages,
    )

    assert outcome.event_status == 'committed'
    assert outcome.decision is not None and outcome.decision.action == 'reply'
    # 模型调用次数：原始一次 + 纠错一次。
    assert len(planner.seen_messages) == 2
    # 第二次尝试的消息尾部带纠错回灌，且原始消息不被修改。
    second_attempt = planner.seen_messages[1]
    assert len(second_attempt) == 3
    correction = second_attempt[-1]
    assert correction['role'] == 'user'
    assert '[被拒绝的工具调用]' in correction['content']
    assert '缺少必填字段' in correction['content']
    assert len(planner.seen_messages[0]) == 2
    # 审计事件留下纠错记数。
    assert '1 次工具调用纠错' in outcome.action_event.detail


async def test_persistent_fault_exhausts_repair_and_fails() -> None:
    """纠错后仍不合法：恰好两次调用后按 illegal_action 终局。"""
    planner = _ToolCallProvider([
        ('reply', '{}'),
        ('reply', '{}'),
    ])
    replyer = _TextProvider([['<say emotion="normal">不会到达。</say>']])
    agent = ConversationAgent(
        planner, temperature=0.7, replyer=replyer, tool_calling=True,
    )

    outcome = await agent.run(
        _frame(),
        [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': '小璃别睡了'}],
        _gate_inputs(),
        ('name_mentioned',),
        replyer_messages=_replyer_messages,
    )

    assert outcome.event_status == 'illegal_action'
    assert '缺少必填字段' in outcome.action_event.detail
    assert '1 次工具调用纠错' in outcome.action_event.detail
    # 预算是原始一次 + 纠错一次，不允许更多重试。
    assert len(planner.seen_messages) == 2
    # 回复生成不参与失败回合。
    assert len(replyer.seen_messages) == 0


async def test_xml_protocol_error_is_not_repaired() -> None:
    """XML 动作头路径的协议错误直接终局：只有一次模型调用。"""
    provider = _TextProvider([
        ['<decision action="silent" reasons="not_a_real_code"/>'],
    ])
    agent = ConversationAgent(provider, temperature=0.7)

    outcome = await agent.run(
        _frame(),
        [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': '在么'}],
        _gate_inputs(),
        ('name_mentioned',),
    )

    assert outcome.event_status == 'illegal_action'
    assert len(provider.seen_messages) == 1
    assert '纠错' not in outcome.action_event.detail


def _reply_call(index: int = 1) -> dict[str, Any]:
    """构造一个参数合法的 reply 工具调用分片。"""
    return {'tool_calls': [{
        'id': f'call_{index}',
        'name': 'reply',
        'arguments': _VALID_REPLY,
    }]}


async def test_prose_without_tool_call_is_repaired() -> None:
    """模型改用正文作答：回灌一次「必须调用工具」后自愈，回合 committed。"""
    planner = _ChunkProvider([
        [{'text': '凌'}, {'text': '白，怎么了'}],
        [_reply_call(2)],
    ])
    replyer = _TextProvider([['<say emotion="normal">嗯，在呢。</say>']])
    agent = ConversationAgent(
        planner, temperature=0.7, replyer=replyer, tool_calling=True,
    )
    events: List[ParseEvent] = []

    outcome = await agent.run(
        _frame(),
        [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': '还真是'}],
        _gate_inputs(),
        ('name_mentioned',),
        replyer_messages=_replyer_messages,
        on_events=lambda batch: _collect_events(batch, events),
    )

    assert outcome.event_status == 'committed'
    assert outcome.decision is not None and outcome.decision.action == 'reply'
    assert len(planner.seen_messages) == 2
    correction = planner.seen_messages[1][-1]
    assert correction['role'] == 'user'
    assert '[无效的响应]' in correction['content']
    assert '没有通过工具选择动作' in correction['content']
    # 被丢弃的决策正文既不进入回合产物，也不流向用户。
    assert '凌白，怎么了' not in outcome.body_text
    assert all('凌白，怎么了' not in str(event) for event in events)
    assert '1 次工具调用纠错' in outcome.action_event.detail


async def test_persistent_prose_exhausts_repair_and_parse_errors() -> None:
    """纠错后仍不调工具：按 parse_error 终局，且回复生成不参与。"""
    planner = _ChunkProvider([
        [{'text': '哥哥'}],
        [{'text': '哥哥你看'}],
    ])
    replyer = _TextProvider([['<say emotion="normal">不会到达。</say>']])
    agent = ConversationAgent(
        planner, temperature=0.7, replyer=replyer, tool_calling=True,
    )

    outcome = await agent.run(
        _frame(),
        [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': '在么'}],
        _gate_inputs(),
        ('name_mentioned',),
        replyer_messages=_replyer_messages,
    )

    assert outcome.event_status == 'parse_error'
    assert '工具调用模式收到正文' in outcome.action_event.detail
    assert '1 次工具调用纠错' in outcome.action_event.detail
    assert len(planner.seen_messages) == 2
    assert len(replyer.seen_messages) == 0


async def test_empty_response_is_repaired_as_missing_action() -> None:
    """整条响应为空同属缺失工具调用：纠错原因按「没有选择任何动作」措辞。"""
    planner = _ChunkProvider([
        [],
        [_reply_call(2)],
    ])
    replyer = _TextProvider([['<say emotion="normal">嗯。</say>']])
    agent = ConversationAgent(
        planner, temperature=0.7, replyer=replyer, tool_calling=True,
    )

    outcome = await agent.run(
        _frame(),
        [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': '在么'}],
        _gate_inputs(),
        ('name_mentioned',),
        replyer_messages=_replyer_messages,
    )

    assert outcome.event_status == 'committed'
    assert len(planner.seen_messages) == 2
    correction = planner.seen_messages[1][-1]
    assert '模型没有选择任何动作' in correction['content']


async def test_prose_before_tool_call_does_not_trigger_repair() -> None:
    """正文先于工具调用流出：正文丢弃、调用照常结算，只有一次模型调用。"""
    planner = _ChunkProvider([
        [{'text': '好的，我来'}, _reply_call(1)],
    ])
    replyer = _TextProvider([['<say emotion="normal">在呢。</say>']])
    agent = ConversationAgent(
        planner, temperature=0.7, replyer=replyer, tool_calling=True,
    )

    outcome = await agent.run(
        _frame(),
        [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': '在么'}],
        _gate_inputs(),
        ('name_mentioned',),
        replyer_messages=_replyer_messages,
    )

    assert outcome.event_status == 'committed'
    assert outcome.decision is not None and outcome.decision.action == 'reply'
    assert len(planner.seen_messages) == 1
    assert '好的，我来' not in outcome.body_text
    assert '纠错' not in outcome.action_event.detail
