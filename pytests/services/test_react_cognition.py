"""ReAct 认知循环验收。

对应 docs/multiagent-react-cognition.md 第六节的六条验收：
1. 预算为 0 时退回单轮，行为与引入 ReAct 之前一致；
2. 认知轮不放出任何用户可见事件；
3. 末轮动作集不含认知动作，模型再选记为 illegal_action 而非降级；
4. 一个回合多条 action_decision：snapshotId 相同、roundIndex 递增、末条为终局；
5. 检索为空时观察文本明确写「没找到」；
6. 中断在任意一轮原样上抛且不写行动决策事件。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, List

import pytest

from src.core.agent.action_protocol import (
    COGNITIVE_ACTIONS,
    DecisionFrame,
    GateInputFacts,
    PlatformCapabilities,
    available_actions,
)
from src.core.agent.cognition import (
    CognitiveObservation,
    CognitiveScope,
    InspectAction,
    RecallAction,
)
from src.core.agent.conversation import ConversationAgent
from src.core.agent.parser import ParseEvent
from src.core.llm_models.openai import LlmError
from src.core.observe.store import event_store
from src.core.tooling.cognitive import CognitiveToolExecutor
from src.core.tooling.registry import ToolRegistry, build_builtin_action_registry


class _MultiTurnProvider:
    """每次调用产出脚本里的下一段输出，用于驱动多轮回环。"""

    def __init__(self, scripts: List[List[str]]) -> None:
        self.scripts = scripts
        self.calls = 0
        self.seen_messages: List[List[dict]] = []

    async def stream(self, **kwargs: Any) -> AsyncIterator[dict[str, str]]:
        index = self.calls
        self.calls += 1
        self.seen_messages.append(list(kwargs.get('messages') or []))
        if index >= len(self.scripts):
            raise AssertionError(f'脚本只准备了 {len(self.scripts)} 轮，第 {index + 1} 轮无输出')
        for text in self.scripts[index]:
            yield {'text': text}


class _MultiTurnToolProvider:
    """逐轮返回一个工具调用，并保留实际收到的扁平消息流。"""

    def __init__(self, calls: List[tuple[str, str]]) -> None:
        self.calls = calls
        self.seen_messages: List[List[dict]] = []

    async def stream(self, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        index = len(self.seen_messages)
        self.seen_messages.append(list(kwargs.get('messages') or []))
        if index >= len(self.calls):
            raise AssertionError(f'工具脚本只准备了 {len(self.calls)} 轮')
        name, arguments = self.calls[index]
        yield {'tool_calls': [{
            'id': f'call_{index + 1}',
            'name': name,
            'arguments': arguments,
        }]}


class _FakeAction:
    """返回固定观察的替身认知动作，避免测试依赖真实数据库。"""

    def __init__(self, name: str, text: str = '查到了：他上周去看了演出。', hits: int = 1) -> None:
        self.name = name
        self._text = text
        self._hits = hits
        self.queries: List[str] = []

    async def execute(self, request: Any) -> CognitiveObservation:
        self.queries.append(request.query)
        return CognitiveObservation(text=self._text, hit_count=self._hits)


def _caps(**overrides: Any) -> PlatformCapabilities:
    return PlatformCapabilities(**overrides)


def _frame(*, cognitive_rounds: int = 2, **overrides: Any) -> DecisionFrame:
    base: dict[str, Any] = dict(
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


def _registry(actions: List[Any]) -> ToolRegistry:
    """把替身认知动作包装成工具执行器并绑定进注册表。"""
    registry = build_builtin_action_registry()
    for action in actions:
        registry.bind_action_executor(action.name, CognitiveToolExecutor(action))
    return registry


def _agent(provider: Any, registry: ToolRegistry | None = None) -> ConversationAgent:
    return ConversationAgent(provider, temperature=0.7, tool_registry=registry)


_RECALL_HEAD = '<decision action="recall" query="上次的演出"/>'
_REPLY = (
    '<decision action="reply" targets="101" reasons="pending_thread" length="brief"/>'
    '<say emotion="smile">想起来了，就那场。</say>'
)
_SCOPE = CognitiveScope(stream_id=3, person_ids=(11, 12))


def _events() -> list[dict]:
    """按时间正序返回本用例产生的行动决策事件。

    ``event_store.search`` 是倒序返回的，而本文件全部断言都在看「一个回合里
    这些轮的先后」，因此这里统一翻正；conftest 的 autouse 夹具已保证每个用例
    有独立的事件库，不需要再做基线切片。
    """
    return list(reversed(event_store.search(kinds=['action_decision']).events))


async def _run(provider: Any, *, rounds: int, actions: List[Any], **overrides: Any):
    return await _agent(provider, _registry(actions)).run(
        _frame(cognitive_rounds=rounds),
        [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': '在么'}],
        _gate_inputs(),
        ('name_mentioned',),
        cognitive_scope=_SCOPE,
        cognitive_rounds=rounds,
        **overrides,
    )


# 1. 预算为 0 时只调一次模型，动作集里没有认知动作。
async def test_zero_budget_degrades_to_single_round() -> None:
    provider = _MultiTurnProvider([[_REPLY]])
    outcome = await _run(provider, rounds=0, actions=[_FakeAction('recall')])

    assert provider.calls == 1
    assert outcome.event_status == 'committed'
    assert outcome.cognitive_rounds_used == 0
    assert not (set(outcome.action_event.available_actions) & COGNITIVE_ACTIONS)


# 2. 认知轮不放出任何用户可见事件，终局轮才开始流出正文。
async def test_cognitive_round_emits_no_visible_events() -> None:
    provider = _MultiTurnProvider([[_RECALL_HEAD], [_REPLY]])
    released: list[ParseEvent] = []

    async def on_events(events: list[ParseEvent]) -> None:
        # 认知轮结束时这里必须一次都没被调用过。
        if provider.calls == 1:
            raise AssertionError('认知轮放出了用户可见事件')
        released.extend(events)

    outcome = await _run(
        provider, rounds=2, actions=[_FakeAction('recall')], on_events=on_events,
    )

    assert provider.calls == 2
    assert outcome.event_status == 'committed'
    assert outcome.cognitive_rounds_used == 1
    assert released, '终局轮应当放出正文事件'


# 3. 末轮动作集不含认知动作；模型仍写认知动作按协议失败处理，不降级成回复。
async def test_budget_exhaustion_is_illegal_action_not_fallback() -> None:
    provider = _MultiTurnProvider([[_RECALL_HEAD], [_RECALL_HEAD]])
    outcome = await _run(provider, rounds=1, actions=[_FakeAction('recall')])

    assert provider.calls == 2
    assert outcome.event_status == 'illegal_action'
    assert outcome.decision is None
    assert '不在本回合可用动作' in outcome.action_event.detail
    # 末轮动作集里认知动作已被减掉，约束写在动作空间而不是异常处理里。
    assert not (set(outcome.action_event.available_actions) & COGNITIVE_ACTIONS)


# 4. 一个回合的多条事件：snapshotId 相同、roundIndex 递增、末条为终局。
async def test_rounds_are_auditable_as_one_chain() -> None:
    provider = _MultiTurnProvider([[_RECALL_HEAD], [_REPLY]])
    await _run(provider, rounds=2, actions=[_FakeAction('recall')])

    chain = _events()
    assert [event['roundIndex'] for event in chain] == [0, 1]
    assert {event['snapshotId'] for event in chain} == {'snap-7'}
    assert chain[0]['eventStatus'] == 'cognitive_step'
    assert chain[0]['decision']['query'] == '上次的演出'
    assert chain[0]['observation']
    assert chain[-1]['eventStatus'] == 'committed'


# 5. 检索为空时观察必须明确写「没找到」，不能是空串。
async def test_empty_recall_states_nothing_found(db) -> None:
    from src.core.memory.store import MemoryStore
    from src.core.agent.cognition import CognitiveRequest

    store = MemoryStore(db)
    action = RecallAction(store, lambda person_id, stream_id: '凌白', db)
    result = await action.execute(CognitiveRequest(
        action='recall',
        query='完全不存在的东西',
        stream_id=1,
        stream_kind='group',
        person_ids=(1,),
        message_watermark=0,
    ))

    assert result.hit_count == 0
    assert '没有想起' in result.text

    inspect = InspectAction(store, lambda person_id, stream_id: '凌白')
    empty = await inspect.execute(CognitiveRequest(
        action='inspect',
        query='完全不存在的东西',
        stream_id=1,
        stream_kind='group',
        person_ids=(1,),
        message_watermark=999,
    ))
    assert empty.hit_count == 0
    assert '没有找到' in empty.text


# 6. 中断在认知轮之后的任意一轮原样上抛，且不为那一轮写行动决策事件。
async def test_abort_in_later_round_propagates_without_event() -> None:
    class _AbortingProvider(_MultiTurnProvider):
        async def stream(self, **kwargs: Any) -> AsyncIterator[dict[str, str]]:
            index = self.calls
            self.calls += 1
            if index == 0:
                yield {'text': _RECALL_HEAD}
                return
            raise LlmError('aborted', '用户中断')
            yield {'text': ''}   # 使函数保持为异步生成器；不可达

    provider = _AbortingProvider([])
    with pytest.raises(LlmError):
        await _run(provider, rounds=2, actions=[_FakeAction('recall')])

    chain = _events()
    # 只有认知轮那一条落账；中断轮不写事件。
    assert [event['eventStatus'] for event in chain] == ['cognitive_step']


# 7. 观察以「她自己的动作头 + 检索结果」两条消息回灌，模型下一轮看得见自己查过什么。
async def test_observation_is_fed_back_as_two_messages() -> None:
    provider = _MultiTurnProvider([[_RECALL_HEAD], [_REPLY]])
    await _run(provider, rounds=2, actions=[_FakeAction('recall')])

    second_round = provider.seen_messages[1]
    assert second_round[-2]['role'] == 'assistant'
    assert second_round[-2]['content'] == _RECALL_HEAD
    assert second_round[-1]['role'] == 'user'
    assert '[检索结果]' in second_round[-1]['content']
    # 预算 2 用掉 1，后面还有机会，此时不该催她收束。
    assert '用完' not in second_round[-1]['content']


async def test_tool_observation_is_folded_into_one_user_item() -> None:
    planner = _MultiTurnToolProvider([
        ('recall', '{"query": "上次的演出"}'),
        (
            'reply',
            '{"target": 101, "reasons": ["pending_thread"], "length": "brief", '
            '"reference": "查到上次演唱会后继续回答"}',
        ),
    ])
    replyer = _MultiTurnProvider([['<say>想起来了，就那场。</say>']])
    agent = ConversationAgent(
        planner,
        temperature=0.7,
        replyer=replyer,
        tool_calling=True,
        tool_registry=_registry([_FakeAction('recall')]),
    )

    async def replyer_messages(_head: Any) -> list[dict]:
        return [
            {'role': 'system', 'content': '只负责把话说出来'},
            {'role': 'user', 'content': '请输出 say'},
        ]

    outcome = await agent.run(
        _frame(cognitive_rounds=2),
        [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': '在么'}],
        _gate_inputs(),
        ('name_mentioned',),
        cognitive_scope=_SCOPE,
        cognitive_rounds=2,
        replyer_messages=replyer_messages,
    )

    assert outcome.event_status == 'committed'
    assert outcome.cognitive_rounds_used == 1
    second_round = planner.seen_messages[1]
    assert [item['role'] for item in second_round] == ['system', 'user', 'user']
    observation = second_round[-1]['content']
    assert '[已完成的工具调用]' in observation
    assert '动作：recall' in observation
    assert '查询：上次的演出' in observation
    assert '[工具返回]' in observation
    assert '查到了：他上周去看了演出。' in observation
    assert '<decision' not in observation


# 7b. 预算用尽的那一次回灌必须把「不能再查了」说清楚，与动作集口径一致。
async def test_final_round_notice_is_appended_when_budget_runs_out() -> None:
    provider = _MultiTurnProvider([[_RECALL_HEAD], [_REPLY]])
    await _run(provider, rounds=1, actions=[_FakeAction('recall')])

    assert '用完' in provider.seen_messages[1][-1]['content']


# 8. 认知动作头不要求 reasons：漏写 reasons 不能被判成协议失败。
async def test_cognitive_head_without_reasons_is_valid() -> None:
    provider = _MultiTurnProvider([['<decision action="inspect" query="显卡"/>'], [_REPLY]])
    outcome = await _run(provider, rounds=2, actions=[_FakeAction('inspect')])

    assert outcome.event_status == 'committed'
    assert outcome.cognitive_rounds_used == 1


# 9. 认知动作缺 query 是协议失败，不允许拿空检索词去查。
async def test_cognitive_head_without_query_is_illegal() -> None:
    provider = _MultiTurnProvider([['<decision action="recall"/>']])
    outcome = await _run(provider, rounds=2, actions=[_FakeAction('recall')])

    assert outcome.event_status == 'illegal_action'
    assert 'query' in outcome.action_event.detail


# 10. 走完整 ChatService 链路的接线：她先 recall，再用检索结果回复。
async def test_chat_service_wires_react_end_to_end(db) -> None:
    """验证接线而不是协议：执行器构造、检索范围、提示词与观察回灌都真的接上了。"""
    from src.core.config.schema import Config
    from src.core.platform_io.types import DeliveryReceipt, InboundMessage, OutboundMessage
    from src.core.services.chat import ChatService

    class _ChatProvider:
        def __init__(self, scripts: List[List[str]]) -> None:
            self.scripts = scripts
            self.calls = 0
            self.messages: List[List[dict]] = []

        async def stream(self, messages=None, **_kwargs: Any):
            index = self.calls
            self.calls += 1
            self.messages.append(list(messages or []))
            for text in self.scripts[min(index, len(self.scripts) - 1)]:
                yield {'text': text}

    class _Broker:
        def __init__(self) -> None:
            self.dispatched: List[Any] = []

        async def dispatch(self, message: Any) -> Any:
            self.dispatched.append(message)
            return DeliveryReceipt(
                platform=message.stream.platform,
                stream_id=message.stream.id,
                external_message_ids=['fake-1'],
            )

    async def _noop(_channel: str, _payload: Any, _stream_id: int) -> None:
        return None

    config = Config()
    config.bot.name = '月璃'
    config.conversation_agent.mode = 'enabled'
    config.conversation_agent.max_cognitive_rounds = 2
    provider = _ChatProvider([
        ['<decision action="inspect" query="演出"/>'],
        [
            '<decision action="reply" targets="1" reasons="pending_thread" length="brief"/>',
            '<say emotion="smile">想起来了。</say>',
        ],
    ])
    chat = ChatService(db, provider, None, None, _noop, cfg=config, broker=_Broker())
    context = chat._registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id='86420',
        sender_external_id='97531',
        sender_nickname='账号昵称',
        sender_group_card='小李',
        first_seen_at=1_000_000,
    )
    await chat.send(InboundMessage(text='月璃还记得那个演出吗', context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    assert provider.calls == 2, '第一轮认知动作之后应当再发起一次模型调用'
    # 系统提示词里必须出现认知动作说明，否则模型根本不知道能查。
    assert 'inspect' in provider.messages[0][0]['content']
    # 第二轮把检索结果回灌了进去。
    assert '[检索结果]' in provider.messages[1][-1]['content']
    chain = _events()
    assert [event['eventStatus'] for event in chain] == ['cognitive_step', 'committed']
    stored = [message.content for message in chat.memory.working_memory(context.stream.id, 20)]
    assert any('想起来了' in content for content in stored)
