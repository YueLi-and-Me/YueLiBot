"""consult 认知动作验收。

对应 开发文档 memory-w4-knowledge.md（不随代码分发） 第四节与第五节：
- ★W4-5：consult 在轮次预算耗尽时不出现在动作空间里；
- consult 与 recall / inspect 并列走既有 ReAct 回环：执行后观察作为消息回灌，
  认知轮不放出任何用户可见事件；
- 观察渲染沿用 recall 的形态（按条列出、_clip 截断），无命中时明确「没找到」；
- 命中计数经 touch_knowledge 落 hit_count / last_hit_at，不参与打分。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, List

import pytest

from src.core.agent.action_protocol import (
    COGNITIVE_ACTIONS,
    DecisionFrame,
    GateInputFacts,
    IllegalActionError,
    PlatformCapabilities,
    available_actions,
)
from src.core.agent.cognition import (
    CognitiveRequest,
    CognitiveScope,
    ConsultAction,
)
from src.core.agent.conversation import ConversationAgent
from src.core.agent.prompt import _cognition_protocol_rule
from src.core.agent.tool_schema import build_tool_definitions, decision_head_from_tool_call
from src.core.tooling.cognitive import CognitiveToolExecutor
from src.core.tooling.registry import build_builtin_action_registry
from src.core.memory.knowledge import index_knowledge
from src.core.memory.similarity import exact_key
from src.core.memory.store import MemoryStore
from src.core.observe.store import event_store

_NOW = 1_750_000_000_000


def _seed_knowledge(db, contents: list[str]) -> list[int]:
    MemoryStore(db)
    ids = []
    for content in contents:
        cur = db.execute(
            'INSERT INTO knowledge (content, content_key, source, created_at)'
            ' VALUES (?, ?, ?, ?)',
            (content, exact_key(content), 'migrate-m4', _NOW),
        )
        ids.append(cur.lastrowid)
    db.commit()
    index_knowledge(db, limit=1000)
    return ids


def _request(query: str, action: str = 'consult') -> CognitiveRequest:
    return CognitiveRequest(
        action=action,
        query=query,
        stream_id=1,
        stream_kind='group',
        person_ids=(1,),
        message_watermark=0,
    )


def _frame(*, cognitive_rounds: int = 2, **overrides: Any) -> DecisionFrame:
    base: dict[str, Any] = dict(
        turn_id=7,
        snapshot_id='snap-7',
        stream_kind='group',
        disposition='deliberate',
        selectable_message_ids=(101, 102),
        message_watermark=102,
        available_actions=available_actions(
            'group', 'deliberate', PlatformCapabilities(),
            cognitive_rounds_left=cognitive_rounds,
        ),
        capabilities=PlatformCapabilities(),
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


def test_consult_absent_when_budget_exhausted() -> None:
    """★W4-5：轮次预算耗尽时 consult 不在动作集里；有预算时在。"""
    with_budget = available_actions(
        'group', 'deliberate', PlatformCapabilities(), cognitive_rounds_left=2,
    )
    assert 'consult' in with_budget
    exhausted = available_actions(
        'group', 'deliberate', PlatformCapabilities(), cognitive_rounds_left=0,
    )
    assert 'consult' not in exhausted
    assert not (set(exhausted) & COGNITIVE_ACTIONS)


async def test_consult_renders_hits_and_counts(db) -> None:
    """命中时按条列出并截断，命中计数落库；无命中时明确「没找到」。"""
    ids = _seed_knowledge(db, ['勾股定理：直角三角形两直角边的平方和等于斜边的平方'])
    action = ConsultAction(db)

    result = await action.execute(_request('勾股定理'))

    assert result.hit_count == 1
    assert '勾股定理' in result.text
    assert result.text.startswith('关于「勾股定理」')
    assert '\n- ' in result.text, '沿用 recall 的按条列出形态'
    row = db.execute(
        'SELECT hit_count, last_hit_at FROM knowledge WHERE id = ?', (ids[0],)
    ).fetchone()
    assert row[0] == 1 and row[1] is not None, '命中计数必须落库'

    empty = await action.execute(_request('完全不存在的概念词'))
    assert empty.hit_count == 0
    assert '没有找到' in empty.text
    assert empty.text.strip(), '无命中也必须给出明确观察，不允许空串'


async def test_consult_uses_embed_query_when_available(db) -> None:
    """向量查询是可插拔的：给了就用，失败就退回 BM25，不中断调用方。"""
    import struct

    _seed_knowledge(db, ['折射率匹配是光学迷彩的原理'])
    calls: list[str] = []

    async def embed_query(text: str) -> bytes:
        calls.append(text)
        return struct.pack('4f', 1.0, 0.0, 0.0, 0.0)

    with_vector = ConsultAction(db, embed_query=embed_query)
    result = await with_vector.execute(_request('折射率'))
    assert calls == ['折射率']
    assert result.hit_count == 1

    async def failing_embed_query(text: str) -> bytes:
        raise RuntimeError('向量服务不可用')

    fallback = ConsultAction(db, embed_query=failing_embed_query)
    result = await fallback.execute(_request('折射率'))
    assert result.hit_count == 1, '向量服务失败必须退回 BM25，不抛异常'


def test_consult_tool_schema_roundtrip() -> None:
    """工具声明与回读：consult 的 query 必填、形状校验与既有认知动作同口径。"""
    frame = _frame(cognitive_rounds=2)
    tools = build_tool_definitions(frame)
    consult = next(t for t in tools if t['function']['name'] == 'consult')
    params = consult['function']['parameters']
    assert 'query' in params['properties']
    assert params['required'] == ['query']
    assert consult['function']['description'], '工具说明必须说清什么时候用它'

    head = decision_head_from_tool_call('consult', '{"query": "勾股定理"}', frame)
    assert head.action == 'consult' and head.query == '勾股定理'

    with pytest.raises(IllegalActionError):
        decision_head_from_tool_call('consult', '{}', frame)
    with pytest.raises(IllegalActionError):
        decision_head_from_tool_call('consult', '{"query": "勾股定理", "target": 1}', frame)
    # 预算耗尽的帧里 consult 是越界动作，按协议失败处理。
    with pytest.raises(IllegalActionError):
        decision_head_from_tool_call(
            'consult', '{"query": "勾股定理"}', _frame(cognitive_rounds=0)
        )


def test_prompt_mentions_consult_only_when_available() -> None:
    """提示词按动作集渲染：动作集里没有 consult 时绝不能出现。"""
    with_consult = _cognition_protocol_rule(
        frozenset({'reply', 'silent', 'recall', 'consult'}), (101,), 2,
    )
    assert 'consult' in with_consult
    assert '查' in with_consult and '知识' in with_consult

    without = _cognition_protocol_rule(frozenset({'reply', 'silent', 'recall'}), (101,), 2)
    assert 'consult' not in without

    tool_mode = _cognition_protocol_rule(
        frozenset({'reply', 'consult'}), (101,), 2, tool_mode=True,
    )
    assert 'consult' not in tool_mode or '知识' in tool_mode
    assert '<decision' not in tool_mode, '工具模式下不写 XML 语法'


class _ConsultThenReplyProvider:
    """第一轮选 consult，第二轮回复；记录每轮实际收到的消息。"""

    def __init__(self, query: str) -> None:
        self.query = query
        self.calls = 0
        self.seen_messages: List[List[dict]] = []

    async def stream(self, **kwargs: Any) -> AsyncIterator[dict[str, str]]:
        index = self.calls
        self.calls += 1
        self.seen_messages.append(list(kwargs.get('messages') or []))
        if index == 0:
            yield {'text': f'<decision action="consult" query="{self.query}"/>'}
        else:
            yield {
                'text': (
                    '<decision action="reply" targets="101" reasons="direct_question"'
                    ' length="brief"/><say>查到了，是这样。</say>'
                )
            }


async def test_consult_flows_through_react_loop(db) -> None:
    """走完整 ReAct 回环：认知轮不放出用户可见事件，观察回灌进下一轮。"""
    _seed_knowledge(db, ['勾股定理：直角三角形两直角边的平方和等于斜边的平方'])
    provider = _ConsultThenReplyProvider('勾股定理')
    released: list = []

    async def on_events(events: list) -> None:
        if provider.calls == 1:
            raise AssertionError('认知轮放出了用户可见事件')
        released.extend(events)

    registry = build_builtin_action_registry()
    registry.bind_action_executor('consult', CognitiveToolExecutor(ConsultAction(db)))
    outcome = await ConversationAgent(
        provider, temperature=0.7, tool_registry=registry,
    ).run(
        _frame(cognitive_rounds=2),
        [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': '在么'}],
        _gate_inputs(),
        ('name_mentioned',),
        cognitive_scope=CognitiveScope(stream_id=1, person_ids=(1,)),
        cognitive_rounds=2,
        on_events=on_events,
    )

    assert provider.calls == 2
    assert outcome.event_status == 'committed'
    assert outcome.cognitive_rounds_used == 1
    second_round = provider.seen_messages[1]
    assert second_round[-2]['role'] == 'assistant'
    assert 'consult' in second_round[-2]['content']
    assert '[检索结果]' in second_round[-1]['content']
    assert '勾股定理' in second_round[-1]['content']

    events = list(reversed(event_store.search(kinds=['action_decision']).events))
    assert [e['eventStatus'] for e in events] == ['cognitive_step', 'committed']
    assert events[0]['decision']['action'] == 'consult'
    assert events[0]['observation'], '观察摘要必须进账本'
