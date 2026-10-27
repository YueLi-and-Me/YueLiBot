"""规划器心理活动（mind）的协议层回归。

盯住五件事：六个终局动作工具都把 mind 声明为第一个必填参数、认知工具不声明；
工具调用与 XML 动作头两条路径对缺失、空白与超长的 mind 给出同一个协议错误；
通过校验的 mind 进入决策、审计字典与校验时刻；写正文那一环只拿到这一轮的想法；
控制台的行动决策行带上 mind 全文。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, List

import json
import pytest

from src.core.agent.action_protocol import (
    COGNITIVE_ACTIONS,
    MIND_MAX_CHARS,
    TERMINAL_ACTIONS,
    DecisionFrame,
    DecisionHead,
    GateInputFacts,
    IllegalActionError,
    PlatformCapabilities,
    available_actions,
)
from src.core.agent.conversation import ConversationAgent
from src.core.agent.parser import DecisionEvent, ResponseParser
from src.core.agent.prompt import (
    SELECTABLE_PREVIEW_LIMIT,
    render_replyer_protocol,
    render_tool_protocol,
)
from src.core.agent.tool_schema import build_tool_definitions, decision_head_from_tool_call
from src.core.services.console import trace_console


def _caps() -> PlatformCapabilities:
    return PlatformCapabilities(
        react=True, available_reactions=('赞',), poke=True, quote=True,
    )


def _frame(**overrides: Any) -> DecisionFrame:
    caps = _caps()
    base: dict[str, Any] = dict(
        turn_id=7,
        snapshot_id='snap-7',
        stream_kind='group',
        disposition='deliberate',
        selectable_message_ids=(101, 102),
        message_watermark=102,
        available_actions=available_actions(
            'group', 'deliberate', caps,
            cognitive_rounds_left=2, allow_wait=True, allow_speak=True,
        ),
        capabilities=caps,
    )
    base.update(overrides)
    return DecisionFrame(**base)


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


class _TextProvider:
    """按脚本产出文本分片的替身。"""

    def __init__(self, chunks: List[str]) -> None:
        self.chunks = chunks
        self.calls = 0

    async def stream(self, **_kwargs: Any) -> AsyncIterator[dict[str, str]]:
        self.calls += 1
        for text in self.chunks:
            yield {'text': text}


class _ToolProvider:
    """每次调用都产出同一个工具调用的替身，并记录收到的消息序列。"""

    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments
        self.messages: List[List[dict[str, Any]]] = []

    async def stream(self, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        self.messages.append(list(kwargs.get('messages') or []))
        yield {'tool_calls': [{'id': 'call_1', 'name': self.name, 'arguments': self.arguments}]}


def _reply_arguments(**overrides: Any) -> str:
    payload: dict[str, Any] = {
        'mind': '他在问凌白为什么用 py 写，不是问我，但我知道答案，补一句',
        'target': '101',
        'reasons': ['can_add_value'],
        'length': 'brief',
        'reference': '补充为什么选 Python',
    }
    payload.update(overrides)
    return json.dumps({key: value for key, value in payload.items() if value is not None})


# ── 工具声明 ──────────────────────────────────────────────────────────


def test_every_terminal_tool_declares_mind_first_and_required() -> None:
    """六个终局动作都把 mind 放在 properties 与 required 的第一个。"""
    tools = {item['function']['name']: item['function'] for item in build_tool_definitions(_frame())}

    assert TERMINAL_ACTIONS <= set(tools)
    for action in TERMINAL_ACTIONS:
        parameters = tools[action]['parameters']
        assert next(iter(parameters['properties'])) == 'mind', action
        assert parameters['required'][0] == 'mind', action
        assert parameters['properties']['mind']['type'] == 'string'
        assert '不超过 60 字' in parameters['properties']['mind']['description']


def test_cognitive_tools_do_not_declare_mind() -> None:
    """认知动作只是先查一下，不声明心理活动。"""
    tools = {item['function']['name']: item['function'] for item in build_tool_definitions(_frame())}

    for action in COGNITIVE_ACTIONS:
        assert 'mind' not in tools[action]['parameters']['properties'], action


# ── 工具调用路径的校验 ──────────────────────────────────────────────────


def test_tool_call_without_mind_is_rejected() -> None:
    with pytest.raises(IllegalActionError, match='缺少必填字段：mind'):
        decision_head_from_tool_call('reply', _reply_arguments(mind=None), _frame())


def test_tool_call_with_blank_mind_is_rejected() -> None:
    with pytest.raises(IllegalActionError, match='非空 mind'):
        decision_head_from_tool_call('silent', json.dumps({
            'mind': '   ', 'reasons': ['others_conversation'],
        }), _frame())


def test_tool_call_with_oversized_mind_is_rejected_not_truncated() -> None:
    with pytest.raises(IllegalActionError, match=f'超过 {MIND_MAX_CHARS} 字'):
        decision_head_from_tool_call(
            'reply', _reply_arguments(mind='想' * (MIND_MAX_CHARS + 1)), _frame(),
        )


def test_tool_call_mind_at_the_limit_is_kept_verbatim() -> None:
    mind = '想' * MIND_MAX_CHARS

    head = decision_head_from_tool_call('reply', _reply_arguments(mind=mind), _frame())

    assert head.mind == mind
    assert head.to_decision('在的').mind == mind


def test_cognitive_tool_call_with_mind_is_an_unknown_field() -> None:
    with pytest.raises(IllegalActionError, match='未知字段：mind'):
        decision_head_from_tool_call(
            'recall', json.dumps({'query': '上次说的事', 'mind': '先查一下'}), _frame(),
        )


def test_head_validate_is_the_single_mind_rule() -> None:
    """两条路径共用 DecisionHead.validate：构造时不校验 mind，校验时才拒绝。"""
    head = DecisionHead(
        action='wait', target_message_ids=(), quote_message_id=None,
        reason_codes=('unfinished_thought',),
    )

    with pytest.raises(IllegalActionError, match='wait 动作必须填写非空 mind'):
        head.validate(_frame())


# ── XML 动作头路径 ────────────────────────────────────────────────────


def test_parser_reads_mind_attribute() -> None:
    events = ResponseParser().push(
        '<decision action="silent" mind="她们在聊自己的事" reasons="others_conversation"/>'
    )

    assert events == [DecisionEvent(
        action='silent', reasons='others_conversation', mind='她们在聊自己的事',
    )]


@pytest.mark.parametrize('head', [
    '<decision action="silent" reasons="others_conversation"/>',
    '<decision action="silent" mind="  " reasons="others_conversation"/>',
    f'<decision action="silent" mind="{"想" * (MIND_MAX_CHARS + 1)}" reasons="others_conversation"/>',
], ids=['missing', 'blank', 'oversized'])
async def test_xml_head_with_bad_mind_is_illegal_action(head: str) -> None:
    agent = ConversationAgent(_TextProvider([head]), temperature=0.7)

    outcome = await agent.run(_frame(), [{'role': 'user', 'content': '在吗'}], _gate_inputs(), ())

    assert outcome.event_status == 'illegal_action'
    assert outcome.decision is None
    assert outcome.decided_at is None
    assert 'mind' in outcome.action_event.detail


async def test_xml_cognitive_head_cannot_carry_mind() -> None:
    agent = ConversationAgent(
        _TextProvider(['<decision action="recall" query="上次" mind="先查查"/>']),
        temperature=0.7,
    )

    outcome = await agent.run(_frame(), [{'role': 'user', 'content': '在吗'}], _gate_inputs(), ())

    assert outcome.event_status == 'illegal_action'
    assert 'recall 动作不能携带 mind' in outcome.action_event.detail


async def test_xml_head_mind_reaches_decision_and_audit(monkeypatch) -> None:
    monkeypatch.setattr('src.core.agent.conversation.current_time', lambda: 1_700_000_000_123)
    agent = ConversationAgent(
        _TextProvider([
            '<decision action="silent" mind="她们俩在聊作业，不是对我说的" '
            'reasons="others_conversation"/>'
        ]),
        temperature=0.7,
    )

    outcome = await agent.run(_frame(), [{'role': 'user', 'content': '在吗'}], _gate_inputs(), ())

    assert outcome.event_status == 'silent_by_choice'
    assert outcome.decision is not None
    assert outcome.decision.mind == '她们俩在聊作业，不是对我说的'
    assert outcome.decided_at == 1_700_000_000_123
    assert outcome.action_event.to_dict()['decision']['mind'] == '她们俩在聊作业，不是对我说的'


# ── 工具调用模式的 Agent ──────────────────────────────────────────────


async def test_tool_mode_missing_mind_is_illegal_action_after_one_repair() -> None:
    planner = _ToolProvider('reply', _reply_arguments(mind=None))
    agent = ConversationAgent(
        planner, temperature=0.7, replyer=_TextProvider(['<say>不该被调用</say>']),
        tool_calling=True,
    )

    outcome = await agent.run(
        _frame(), [{'role': 'user', 'content': '在吗'}], _gate_inputs(), (),
        replyer_messages=_replyer_messages_recorder([]),
    )

    # 缺 mind 属于部分必填字段缺失：先回灌纠错重发一次，仍不合法才终局。
    assert len(planner.messages) == 2
    assert outcome.event_status == 'illegal_action'
    assert '缺少必填字段：mind' in outcome.action_event.detail


async def test_tool_mode_replyer_receives_only_this_rounds_mind(monkeypatch) -> None:
    monkeypatch.setattr('src.core.agent.conversation.current_time', lambda: 1_700_000_000_456)
    mind = '他在问凌白为什么用 py 写，不是问我，但我知道答案，补一句'
    planner = _ToolProvider('reply', _reply_arguments(mind=mind))
    received: List[str] = []
    agent = ConversationAgent(
        planner, temperature=0.7, replyer=_TextProvider(['<say>因为写得快</say>']),
        tool_calling=True,
    )

    outcome = await agent.run(
        _frame(), [{'role': 'user', 'content': '你为啥选py写'}], _gate_inputs(), (),
        replyer_messages=_replyer_messages_recorder(received),
    )

    assert outcome.event_status == 'committed'
    assert outcome.decision is not None and outcome.decision.mind == mind
    assert outcome.decided_at == 1_700_000_000_456
    assert outcome.action_event.to_dict()['decision']['mind'] == mind
    assert len(received) == 1
    assert f'你此刻心里的想法：{mind}。这是想法，不要原样说出来。' in received[0]


def _replyer_messages_recorder(received: List[str]):
    """返回一个按动作头渲染回复协议并记下协议文本的 replyer_messages 回调。"""

    async def build(head: DecisionHead, materials) -> List[dict]:
        assert head.mind is not None
        protocol = render_replyer_protocol(
            head.reference or '', head.length, mind=head.mind, materials=materials,
        )
        received.append(protocol)
        return [{'role': 'user', 'content': protocol}]

    return build


# ── 提示词 ──────────────────────────────────────────────────────────


def test_replyer_protocol_rejects_blank_mind() -> None:
    with pytest.raises(ValueError, match='心理活动'):
        render_replyer_protocol('接着回应', 'brief', mind='  ')


def test_replyer_protocol_does_not_double_the_full_stop() -> None:
    rendered = render_replyer_protocol('接着回应', 'brief', mind='先看着。')

    assert '你此刻心里的想法：先看着。这是想法，不要原样说出来。' in rendered


def test_tool_protocol_carries_mind_and_addressee_rules() -> None:
    text = render_tool_protocol([(101, '凌白: 因为')], False)

    assert '选定最终动作时都要填 mind，写这一轮你心里的想法；查询记忆、人物、知识和使用其他工具时不填：' in text
    assert '聊天记录里以 [我·心里] 开头的行是你当时的想法' in text
    assert '判断一条消息是在对谁说：' in text
    assert '4. 判断不了是不是对你说的时，选择沉默' in text


def test_selectable_notes_are_appended_after_truncation() -> None:
    """附注是确定事实，原文再长被截断时附注也必须完整保留。"""
    long_text = '凌白: ' + '很长' * SELECTABLE_PREVIEW_LIMIT
    note = '（没有引用你、没有 @ 你、也没有叫你）'

    text = render_tool_protocol([(101, long_text)], False, selectable_notes={101: note})

    line = next(line for line in text.splitlines() if line.startswith('  101 = '))
    assert line.endswith(f'…{note}')


# ── 控制台 ──────────────────────────────────────────────────────────


def test_console_decision_line_shows_full_mind(monkeypatch) -> None:
    blocks: List[str] = []
    monkeypatch.setattr(trace_console, '_emit_console_block', lambda text: blocks.append(text.plain))
    mind = '她在问凌白为什么用 py 写，不是问我，先看着' * 3

    trace_console.render_action_decision(
        turn=3, agent_scope='live', event_status='silent_by_choice',
        action='silent', reason_codes=('others_conversation',), mind=mind,
    )

    assert len(blocks) == 1
    assert f'心里：{mind}' in blocks[0]
