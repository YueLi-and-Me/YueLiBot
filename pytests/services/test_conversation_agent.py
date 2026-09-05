"""Conversation 行动核心 Conversation Agent 验收。"""

from __future__ import annotations

from typing import Any, AsyncIterator, List

import pytest

from src.core.agent.action_protocol import (
    ConversationDecision,
    DecisionFrame,
    GateInputFacts,
    PlatformCapabilities,
    available_actions,
)
from src.core.agent.conversation import AgentOutcome, ConversationAgent
from src.core.agent.parser import DecisionEvent, ParseEvent, SayEvent, SayEndEvent, TextEvent
from src.core.llm_models.openai import LlmError
from src.core.observe.store import event_store


class _ScriptedProvider:
    """按脚本产出分片或在迭代中抛出异常的替身提供方。"""

    def __init__(self, chunks: List[str] | None = None, error: Exception | None = None) -> None:
        self.chunks = chunks or []
        self.error = error
        self.calls = 0

    async def stream(self, **_kwargs: Any) -> AsyncIterator[dict[str, str]]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        for text in self.chunks:
            yield {'text': text}


def _caps(**overrides: Any) -> PlatformCapabilities:
    return PlatformCapabilities(**overrides)


def _frame(**overrides: Any) -> DecisionFrame:
    base: dict[str, Any] = dict(
        turn_id=7,
        snapshot_id='snap-7',
        stream_kind='group',
        disposition='deliberate',
        selectable_message_ids=(101, 102),
        message_watermark=102,
        available_actions=available_actions('group', 'deliberate', _caps()),
        capabilities=_caps(),
    )
    base.update(overrides)
    return DecisionFrame(**base)


def _gate_inputs(**overrides: Any) -> GateInputFacts:
    base: dict[str, Any] = dict(
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
    base.update(overrides)
    return GateInputFacts(**base)


def _agent(provider: Any) -> ConversationAgent:
    return ConversationAgent(provider, temperature=0.7)


_REPLY_HEAD = (
    '<decision action="reply" targets="101,102" quote="101" '
    'reasons="direct_question,topic_continuation" length="brief"/>'
)
_REPLY_HEAD_NO_QUOTE = (
    '<decision action="reply" targets="101" reasons="direct_question" length="brief"/>'
)
_SILENT_HEAD = '<decision action="silent" reasons="others_conversation,would_interrupt"/>'


async def _collect_events(events: List[ParseEvent], sink: List[ParseEvent]) -> None:
    """把回调收到的事件追加到测试收集列表。"""
    sink.extend(events)


async def test_extended_trigger_helpers_are_deterministic() -> None:
    """扩展触发模式提供纯确定性阈值与评分，不引入随机决策。"""
    from src.core.agent.reply_necessity import (
        ReplyNecessityScore,
        frequency_trigger_threshold,
        score_reply_necessity,
    )

    assert frequency_trigger_threshold(0.6) == 2
    assert frequency_trigger_threshold(1.0) == 1

    worthy_text = (
        "这个报错怎么解决，帮我看看具体步骤，"
        + "请把每一步都讲清楚。" * 12
    )
    worthy = score_reply_necessity(
        [worthy_text],
        pending_count=0,
        backlog_scale=2,
    )
    assert isinstance(worthy, ReplyNecessityScore)
    # 内容驱动：问题 + 请求 + 两档长度共 80 分，无需积压即可达默认阈值。
    assert worthy.score >= 80
    assert "问题" in worthy.detail
    assert "请求:帮我" in worthy.detail

    plain = score_reply_necessity(
        ["哈哈"],
        pending_count=1,
        backlog_scale=2,
    )
    assert plain.score < 80


async def test_reply_necessity_presence_penalty_reduces_over_reply() -> None:
    """最近发言占比越高，普通群消息触发分越低，形成平滑自我收敛。"""
    from src.core.agent.reply_necessity import (
        PRESENCE_PENALTY_MAX,
        PRESENCE_WINDOW_MS,
        score_reply_necessity,
    )

    def scored(self_replies: int, total_messages: int) -> int:
        return score_reply_necessity(
            ["这个怎么弄"],
            pending_count=2,
            backlog_scale=2,
            recent_self_replies=self_replies,
            recent_window_messages=total_messages,
        ).score

    low_presence = scored(1, 20)
    high_presence = scored(15, 20)
    assert high_presence < low_presence
    assert "存在感" in score_reply_necessity(
        ["这个怎么弄"],
        pending_count=2,
        backlog_scale=2,
        recent_self_replies=15,
        recent_window_messages=20,
    ).detail
    assert PRESENCE_WINDOW_MS == 5 * 60_000
    assert 0 < PRESENCE_PENALTY_MAX <= 25


async def test_reply_releases_body_only_after_valid_head() -> None:
    provider = _ScriptedProvider([
        '<decision action="reply" targets="101" quote="101" reasons="direct_question" length="brief"/>',
        '<say emotion="normal">在的</say>',
    ])
    collected: List[ParseEvent] = []

    outcome = await _agent(provider).run(
        _frame(capabilities=_caps(quote=True)),
        [{'role': 'system', 'content': 's'}],
        _gate_inputs(),
        ('name_mention',),
        on_events=lambda events: _collect_events(events, collected),
        prompt_hash="abc12345",
        provider_name='openai',
        model_name='m1',
    )

    assert provider.calls == 1
    assert outcome.event_status == 'committed'
    assert outcome.decision is not None
    assert outcome.decision.action == 'reply'
    assert outcome.decision.target_message_ids == (101,)
    assert outcome.decision.quote_message_id == 101
    assert outcome.decision.reason_codes == ('direct_question',)
    assert outcome.decision.reply is not None
    assert outcome.decision.reply.text == '在的'
    assert outcome.decision.reply.length == 'brief'
    assert outcome.body_text == '在的'
    assert not any(isinstance(event, DecisionEvent) for event in collected)
    assert isinstance(collected[0], SayEvent)
    assert isinstance(collected[-1], SayEndEvent)

    payloads = event_store.search(turn_id=7, kinds=["action_decision"]).events
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload['eventStatus'] == 'committed'
    assert payload['inputs']['nameMentioned'] is True
    assert payload['gate']['disposition'] == 'deliberate'
    assert payload['gate']['reasonCodes'] == ['name_mention']
    assert payload['gate']['availableActions'] == ['reply', 'silent']
    assert payload['decision']['action'] == 'reply'
    assert payload['decision']['targetMessageIds'] == [101]
    assert payload['decision']['reply']['text'] == '在的'
    assert payload['version']['promptHash'] == 'abc12345'
    assert payload['version']['provider'] == 'openai'
    assert payload['version']['model'] == 'm1'


async def test_head_split_across_chunks() -> None:
    provider = _ScriptedProvider([
        '<decision action="reply" targets="10',
        '1" reasons="direct_question" length="long"/>',
        '<say>好</say>',
    ])

    outcome = await _agent(provider).run(
        _frame(),
        [{'role': 'system', 'content': 's'}],
        _gate_inputs(),
        ('name_mention',)
    )

    assert outcome.event_status == 'committed'
    assert outcome.decision is not None and outcome.decision.reply is not None
    assert outcome.decision.reply.length == 'long'
    assert outcome.body_text == '好'


async def test_silent_produces_only_head_and_no_body() -> None:
    provider = _ScriptedProvider([_SILENT_HEAD])
    collected: List[ParseEvent] = []

    outcome = await _agent(provider).run(
        _frame(),
        [{'role': 'system', 'content': 's'}],
        _gate_inputs(),
        ('name_mention',),
        on_events=lambda events: _collect_events(events, collected),
    )

    assert outcome.event_status == 'silent_by_choice'
    assert outcome.decision is not None
    assert outcome.decision.action == 'silent'
    assert outcome.body_text == ''
    assert collected == []
    payload = event_store.search(turn_id=7, kinds=["action_decision"]).events[0]
    assert payload['eventStatus'] == 'silent_by_choice'
    assert payload['decision']['action'] == 'silent'
    assert payload['decision']['reasonCodes'] == ['others_conversation', 'would_interrupt']


async def test_silent_drops_trailing_body() -> None:
    """静默动作头之后的正文不得解析、不得流出。"""
    provider = _ScriptedProvider([
        '<decision action="silent" reasons="low_relevance"/>',
        '<say>不该出现</say>',
    ])
    collected: List[ParseEvent] = []

    outcome = await _agent(provider).run(
        _frame(),
        [{'role': 'system', 'content': 's'}],
        _gate_inputs(),
        ('name_mention',),
        on_events=lambda events: _collect_events(events, collected),
    )

    assert outcome.event_status == 'silent_by_choice'
    assert outcome.body_text == ''
    assert collected == []


async def test_text_before_head_is_parse_error_and_dropped() -> None:
    provider = _ScriptedProvider(['<say>先说</say>', _REPLY_HEAD_NO_QUOTE])
    collected: List[ParseEvent] = []

    outcome = await _agent(provider).run(
        _frame(),
        [{'role': 'system', 'content': 's'}],
        _gate_inputs(),
        ('name_mention',),
        on_events=lambda events: _collect_events(events, collected),
    )

    assert outcome.event_status == 'parse_error'
    assert outcome.decision is None
    assert collected == []
    assert '动作头之前出现了 SayEvent' in outcome.action_event.detail


async def test_missing_head_is_parse_error() -> None:
    """模型什么都没输出时同样按解析失败落账，不能伪装成沉默。"""
    provider = _ScriptedProvider([])

    outcome = await _agent(provider).run(
        _frame(),
        [{'role': 'system', 'content': 's'}],
        _gate_inputs(),
        ('name_mention',)
    )

    assert outcome.event_status == 'parse_error'
    assert outcome.decision is None
    assert '没有动作头' in outcome.action_event.detail


async def test_reply_without_body_is_illegal_action() -> None:
    provider = _ScriptedProvider([_REPLY_HEAD_NO_QUOTE])

    outcome = await _agent(provider).run(
        _frame(),
        [{'role': 'system', 'content': 's'}],
        _gate_inputs(),
        ('name_mention',)
    )

    assert outcome.event_status == 'illegal_action'
    assert outcome.decision is None
    assert 'reply 动作头之后没有可见正文或表情包' in outcome.action_event.detail


async def test_unknown_action_is_illegal() -> None:
    provider = _ScriptedProvider([
        '<decision action="dance" targets="101" reasons="direct_question" length="brief"/>',
    ])

    outcome = await _agent(provider).run(
        _frame(),
        [{'role': 'system', 'content': 's'}],
        _gate_inputs(),
        ('name_mention',)
    )

    assert outcome.event_status == 'illegal_action'
    assert '未知动作：dance' in outcome.action_event.detail


async def test_free_reason_code_is_illegal() -> None:
    provider = _ScriptedProvider([
        '<decision action="reply" targets="101" reasons="i_feel_like_it" length="brief"/>',
        '<say>在</say>',
    ])

    outcome = await _agent(provider).run(
        _frame(),
        [{'role': 'system', 'content': 's'}],
        _gate_inputs(),
        ('name_mention',)
    )

    assert outcome.event_status == 'illegal_action'
    assert '未知 reason_code' in outcome.action_event.detail


async def test_target_outside_selectable_is_illegal() -> None:
    provider = _ScriptedProvider([
        '<decision action="reply" targets="103" reasons="direct_question" length="brief"/>',
        '<say>在</say>',
    ])

    outcome = await _agent(provider).run(
        _frame(),
        [{'role': 'system', 'content': 's'}],
        _gate_inputs(),
        ('name_mention',)
    )

    assert outcome.event_status == 'illegal_action'
    assert '不在本回合 selectable_message_ids 内' in outcome.action_event.detail


async def test_quote_without_capability_is_illegal() -> None:
    provider = _ScriptedProvider([
        '<decision action="reply" targets="101" quote="101" reasons="direct_question" length="brief"/>',
        '<say>在</say>',
    ])

    outcome = await _agent(provider).run(
        _frame(),
        [{'role': 'system', 'content': 's'}],
        _gate_inputs(),
        ('name_mention',)
    )

    assert outcome.event_status == 'illegal_action'
    assert '平台不支持引用' in outcome.action_event.detail


async def test_reply_head_without_length_is_illegal() -> None:
    provider = _ScriptedProvider([
        '<decision action="reply" targets="101" reasons="direct_question"/>',
        '<say>在</say>',
    ])

    outcome = await _agent(provider).run(
        _frame(),
        [{'role': 'system', 'content': 's'}],
        _gate_inputs(),
        ('name_mention',)
    )

    assert outcome.event_status == 'illegal_action'
    assert '必须声明 brief 或 long 篇幅' in outcome.action_event.detail


async def test_force_frame_silent_is_protocol_error() -> None:
    """FORCE 场景模型根本没有 silent 选项，非法输出按协议错误处理。"""
    provider = _ScriptedProvider([_SILENT_HEAD])
    frame = _frame(
        disposition='force',
        available_actions=frozenset({'reply', 'silent'}),
    )

    outcome = await _agent(provider).run(
        frame,
        [{'role': 'system', 'content': 's'}],
        _gate_inputs(),
        ('at_mention_must_reply',)
    )

    assert outcome.event_status == 'illegal_action'
    assert 'FORCE 场景不允许 silent' in outcome.action_event.detail


async def test_provider_timeout_is_timeout_status() -> None:
    provider = _ScriptedProvider(error=LlmError("timeout", "超时"))

    outcome = await _agent(provider).run(
        _frame(),
        [{'role': 'system', 'content': 's'}],
        _gate_inputs(),
        ('name_mention',)
    )

    assert outcome.event_status == 'timeout'
    assert outcome.decision is None
    assert '超时' in outcome.action_event.detail


async def test_provider_error_is_provider_error_status() -> None:
    provider = _ScriptedProvider(error=LlmError("network", "断网"))

    outcome = await _agent(provider).run(
        _frame(),
        [{'role': 'system', 'content': 's'}],
        _gate_inputs(),
        ('name_mention',)
    )

    assert outcome.event_status == 'provider_error'
    assert outcome.decision is None


async def test_aborted_re_raises_without_action_event() -> None:
    """用户主动中断不属于八种行动事件状态，原样上抛且不落账。"""
    provider = _ScriptedProvider(error=LlmError("aborted", "中断"))

    with pytest.raises(LlmError):
        await _agent(provider).run(
            _frame(),
            [{'role': 'system', 'content': 's'}],
            _gate_inputs(),
            ('name_mention',)
        )

    assert event_store.search(turn_id=7, kinds=["action_decision"]).events == []


async def test_on_chunk_observes_every_chunk() -> None:
    chunks = [_REPLY_HEAD_NO_QUOTE, '<say>在</say>']
    provider = _ScriptedProvider(chunks)
    seen: List[dict[str, str]] = []

    outcome = await _agent(provider).run(
        _frame(),
        [{'role': 'system', 'content': 's'}],
        _gate_inputs(),
        ('name_mention',),
        on_chunk=seen.append,
    )

    assert outcome.event_status == 'committed'
    assert [chunk['text'] for chunk in seen] == chunks


async def test_duplicate_decision_tag_after_head_is_ignored() -> None:
    provider = _ScriptedProvider([
        _REPLY_HEAD_NO_QUOTE,
        _SILENT_HEAD,
        '<say>还在</say>',
    ])
    collected: List[ParseEvent] = []

    outcome = await _agent(provider).run(
        _frame(),
        [{'role': 'system', 'content': 's'}],
        _gate_inputs(),
        ('name_mention',),
        on_events=lambda events: _collect_events(events, collected),
    )

    assert outcome.event_status == 'committed'
    assert outcome.decision is not None and outcome.decision.action == 'reply'
    assert outcome.body_text == '还在'
    assert not any(isinstance(event, DecisionEvent) for event in collected)


async def test_react_head_with_capability() -> None:
    """平台已验证 reaction 时，react 动作头合法且不产出正文。"""
    caps = _caps(react=True, available_reactions=("128074",))
    frame = _frame(
        available_actions=available_actions('group', 'deliberate', caps),
        capabilities=caps,
    )
    provider = _ScriptedProvider([
        '<decision action="react" targets="101" reaction="128074" '
        'reasons="natural_reaction"/>',
    ])
    collected: List[ParseEvent] = []

    outcome = await _agent(provider).run(
        frame,
        [{'role': 'system', 'content': 's'}],
        _gate_inputs(),
        ('name_mention',),
        on_events=lambda events: _collect_events(events, collected),
    )

    assert outcome.event_status == 'committed'
    assert outcome.decision is not None and outcome.decision.action == 'react'
    assert outcome.decision.target_message_ids == (101,)
    assert outcome.decision.reaction == '128074'
    assert outcome.body_text == ''
    assert collected == []

async def test_action_protocol_template_renders_actions_and_targets() -> None:
    """动作空间与可选消息由运行时注入，不写死在模板里。"""
    from src.core.agent.prompt import render_action_protocol

    text = render_action_protocol(
        ["reply", "silent"],
        [(101, "凌白: 小璃出来"), (102, "凌白: 在不在")],
        quote_supported=True,
    )

    assert 'reply / silent' in text
    # 可选消息必须带原文锚点，模型才能把编号对应回具体消息。
    assert '  101 = 凌白: 小璃出来' in text
    assert '  102 = 凌白: 在不在' in text
    assert '不引用就不写 quote 属性' in text
    # 示例目标来自真实可选消息，reply 与 silent 都在动作空间内时两条都渲染。
    assert '# reply（回复）' in text
    assert '<decision action="reply" targets="101"' in text
    assert '# silent（不回复）' in text
    assert '<decision action="silent" reasons="others_conversation"/>' in text
    assert 'normal / happy / smile' in text


async def test_action_protocol_quote_rule_follows_capability() -> None:
    """模型不能自选引用目标时，提示词明确禁止 quote 属性。

    措辞不再断言「本平台不支持引用」：QQ 群聊的引用由投递层按需要自动挂上，
    这条规则只约束动作头里能不能出现 quote 属性。
    """
    from src.core.agent.prompt import render_action_protocol

    text = render_action_protocol(["reply"], [], quote_supported=False)

    assert '不要写 quote 属性，需要指向哪一条由 targets 决定' in text
    assert '本平台不支持引用' not in text
    # 可选消息为空时不渲染 reply 示例，也不得出现 targets="0" 兜底。
    assert '# reply（回复）' not in text
    assert 'targets="0"' not in text
    # silent 不在动作空间内时同样不给示例：示例是模型最容易照抄的部分。
    assert '# silent（不回复）' not in text


async def test_system_prompt_protocol_text_replaces_direct_say_protocol() -> None:
    """Agent 协议必须替换而非追加，避免与直接输出 <say> 的旧指令竞争。"""
    from src.core.agent.prompt import build_system_prompt

    base = build_system_prompt(
        name='测试角色',
        birthday='',
        personality='',
        reply_style='',
    )
    assert '只输出下列标签' in base

    agent_prompt = build_system_prompt(
        name='测试角色',
        birthday='',
        personality='',
        reply_style='',
        protocol_text='先写 <decision>，正文只能在其后的 <say> 里',
    )
    assert '# 输出格式' in agent_prompt
    assert '先写 <decision>' in agent_prompt
    assert '只输出下列标签' not in agent_prompt


async def test_action_protocol_template_placeholders_strict() -> None:
    """新模板占位符声明与文件内容严格一致。"""
    from pathlib import Path

    from src.core.prompts.registry import TEMPLATE_PLACEHOLDERS, validate_prompt_text

    builtin = Path("src/core/prompts/chat.action.protocol.md").read_text(encoding="utf-8")
    declared = validate_prompt_text("chat.action.protocol", builtin)

    assert declared == TEMPLATE_PLACEHOLDERS["chat.action.protocol"]
    assert declared == {
        "available_actions",
        "selectable_messages",
        "turn_scope",
        "quote_rule",
        "emotions",
        "gestures",
        "reply_example",
        "silent_example",
        "emoji_rule",
        "cognition_rule",
        "react_rule",
        "poke_rule",
        "wait_rule",
        "speak_rule",
    }



def _messages_from(build):
    """把同步的消息组装函数包成 Agent 需要的异步回调。"""

    async def call(head):
        return build(head)

    return call


class _RecordingProvider(_ScriptedProvider):
    """在脚本基础上记录每次收到的消息序列，用于检查两级各自看到什么。"""

    def __init__(self, chunks: List[str] | None = None) -> None:
        super().__init__(chunks)
        self.messages: List[List[dict[str, Any]]] = []

    async def stream(self, **kwargs: Any) -> AsyncIterator[dict[str, str]]:
        self.messages.append(list(kwargs.get('messages') or []))
        async for chunk in super().stream(**kwargs):
            yield chunk


async def test_split_reply_takes_body_from_replyer_only() -> None:
    """注入回复生成模型后，正文只能来自它，决策模型写的正文一律丢弃。

    两个模型各写一份正文时若都放行，最终发出哪一句就成了运气问题；决策模型的
    职责必须到动作头为止。
    """
    planner = _RecordingProvider([
        '<decision action="reply" targets="101" reasons="can_add_value" '
        'length="brief" reference="他在吐槽引擎，接一句"/>',
        '<say>这句是决策模型写的，不许发出去</say>',
    ])
    replyer = _RecordingProvider(['<say>这句才是她说的</say>'])
    agent = ConversationAgent(planner, temperature=0.8, replyer=replyer)

    outcome = await agent.run(
        _frame(),
        [{'role': 'user', 'content': '在吗'}],
        _gate_inputs(),
        (),
        replyer_messages=_messages_from(
            lambda head: [
                {'role': 'user', 'content': f'背景：{head.reference}｜篇幅：{head.length}'},
            ]
        ),
    )

    assert outcome.event_status == 'committed'
    assert outcome.decision is not None
    assert outcome.decision.action == 'reply'
    assert outcome.body_text == '这句才是她说的'
    assert '决策模型写的' not in outcome.body_text
    # 决策产出的背景说明确实传给了回复生成模型。
    assert replyer.messages[0][0]['content'] == '背景：他在吐槽引擎，接一句｜篇幅：brief'
    assert planner.calls == 1 and replyer.calls == 1


async def test_split_reply_rejects_plain_text_without_say() -> None:
    """回复生成的供应商诊断裸文本不能被隐式包装成月璃台词。"""
    planner = _ScriptedProvider([
        '<decision action="reply" targets="101" reasons="can_add_value" '
        'length="brief" reference="接一句"/>',
    ])
    replyer = _ScriptedProvider([
        'The prompt could not be submitted. ',
        'The prompt contains sensitive words.',
    ])
    collected: List[ParseEvent] = []
    agent = ConversationAgent(planner, temperature=0.8, replyer=replyer)

    outcome = await agent.run(
        _frame(),
        [{'role': 'user', 'content': '在吗'}],
        _gate_inputs(),
        (),
        on_events=lambda events: _collect_events(events, collected),
        replyer_messages=_messages_from(lambda head: [{'role': 'user', 'content': 'x'}]),
    )

    assert outcome.event_status == 'parse_error'
    assert outcome.decision is None
    assert outcome.body_text == ''
    assert '<say>' in outcome.action_event.detail
    assert collected == []


async def test_split_reply_discards_staged_events_after_trailing_plain_text() -> None:
    """回复后段违反协议时，前段台词和副作用也不能提前放出。"""
    planner = _ScriptedProvider([
        '<decision action="reply" targets="101" reasons="can_add_value" '
        'length="brief" reference="接一句"/>',
    ])
    replyer = _ScriptedProvider([
        '<say>这句不能提前发</say><memory type="偏好">不应落库</memory>',
        '供应商报错',
    ])
    collected: List[ParseEvent] = []
    agent = ConversationAgent(planner, temperature=0.8, replyer=replyer)

    outcome = await agent.run(
        _frame(),
        [{'role': 'user', 'content': '在吗'}],
        _gate_inputs(),
        (),
        on_events=lambda events: _collect_events(events, collected),
        replyer_messages=_messages_from(lambda head: [{'role': 'user', 'content': 'x'}]),
    )

    assert outcome.event_status == 'parse_error'
    assert outcome.body_text == ''
    assert collected == []


async def test_split_skips_replyer_for_silent() -> None:
    """沉默不产出可见产物，不该为它多付一次模型往返。"""
    planner = _ScriptedProvider(['<decision action="silent" reasons="no_new_value"/>'])
    replyer = _ScriptedProvider(['<say>不该被调用</say>'])
    agent = ConversationAgent(planner, temperature=0.8, replyer=replyer)

    outcome = await agent.run(
        _frame(),
        [{'role': 'user', 'content': '在吗'}],
        _gate_inputs(),
        (),
        replyer_messages=_messages_from(lambda head: [{'role': 'user', 'content': 'x'}]),
    )

    assert outcome.event_status == 'silent_by_choice'
    assert replyer.calls == 0


async def test_without_replyer_body_still_comes_from_single_call() -> None:
    """未注入回复生成模型时行为与拆分前逐字相同。"""
    planner = _ScriptedProvider([
        '<decision action="reply" targets="101" reasons="can_add_value" length="brief"/>',
        '<say>单次调用产出的正文</say>',
    ])
    agent = ConversationAgent(planner, temperature=0.8)

    outcome = await agent.run(
        _frame(), [{'role': 'user', 'content': '在吗'}], _gate_inputs(), (),
    )

    assert outcome.event_status == 'committed'
    assert outcome.body_text == '单次调用产出的正文'


class _RecordingParamsProvider(_ScriptedProvider):
    """记录每次调用收到的采样参数，用于检查两级是否各用各的。"""

    def __init__(self, chunks: List[str] | None = None) -> None:
        super().__init__(chunks)
        self.params: List[tuple[Any, Any]] = []

    async def stream(self, **kwargs: Any) -> AsyncIterator[dict[str, str]]:
        self.params.append((kwargs.get('temperature'), kwargs.get('max_tokens')))
        async for chunk in super().stream(**kwargs):
            yield chunk


class _ToolProvider:
    """按脚本产出工具调用增量的替身，并记录收到的工具声明。"""

    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments
        self.tools: List[List[dict[str, Any]]] = []
        self.calls = 0

    async def stream(self, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        self.calls += 1
        self.tools.append(list(kwargs.get('tools') or []))
        yield {'tool_calls': [{
            'id': 'call_1', 'name': self.name, 'arguments': self.arguments,
        }]}


async def test_tool_calling_decision_drives_replyer() -> None:
    """工具调用产出决策、replyer 产出正文，两级各司其职。"""
    planner = _ToolProvider(
        'reply',
        '{"target": "101", "reasons": ["direct_question"], "length": "brief", '
        '"reference": "他直接问了，答一句"}',
    )
    replyer = _ScriptedProvider(['<say>在的</say>'])
    agent = ConversationAgent(
        planner, temperature=0.8, replyer=replyer, tool_calling=True,
    )

    outcome = await agent.run(
        _frame(),
        [{'role': 'user', 'content': '在吗'}],
        _gate_inputs(),
        (),
        replyer_messages=_messages_from(
            lambda head: [{'role': 'user', 'content': head.reference or ''}]
        ),
    )

    assert outcome.event_status == 'committed'
    assert outcome.decision is not None and outcome.decision.action == 'reply'
    assert outcome.decision.target_message_ids == (101,)
    assert outcome.body_text == '在的'
    # 工具声明按本轮动作集下发。
    assert {item['function']['name'] for item in planner.tools[0]} == set(
        _frame().available_actions
    )


async def test_tool_calling_silent_needs_no_replyer() -> None:
    """选中沉默工具时不产出正文，也不调用回复生成模型。"""
    planner = _ToolProvider('silent', '{"reasons": ["others_conversation"]}')
    replyer = _ScriptedProvider(['<say>不该被调用</say>'])
    agent = ConversationAgent(
        planner, temperature=0.8, replyer=replyer, tool_calling=True,
    )

    outcome = await agent.run(
        _frame(), [{'role': 'user', 'content': '在吗'}], _gate_inputs(), (),
        replyer_messages=_messages_from(lambda head: [{'role': 'user', 'content': 'x'}]),
    )

    assert outcome.event_status == 'silent_by_choice'
    assert replyer.calls == 0


async def test_tool_calling_react_needs_no_replyer() -> None:
    """表情回应已经是完整终局动作，不得再调用回复生成模型补正文。"""
    planner = _ToolProvider(
        'react',
        '{"target": "101", "reaction": "吃瓜", "reasons": ["natural_reaction"]}',
    )
    replyer = _ScriptedProvider(['<say>不该被调用</say>'])
    agent = ConversationAgent(
        planner, temperature=0.8, replyer=replyer, tool_calling=True,
    )
    capabilities = _caps(react=True, available_reactions=('吃瓜',))
    frame = _frame(
        capabilities=capabilities,
        available_actions=available_actions('group', 'deliberate', capabilities),
    )

    outcome = await agent.run(
        frame, [{'role': 'user', 'content': '在吗'}], _gate_inputs(), (),
        replyer_messages=_messages_from(lambda head: [{'role': 'user', 'content': 'x'}]),
    )

    assert outcome.event_status == 'committed'
    assert outcome.decision is not None and outcome.decision.action == 'react'
    assert outcome.decision.reaction == '吃瓜'
    assert outcome.body_text == ''
    assert replyer.calls == 0


async def test_tool_calling_rejects_illegal_arguments() -> None:
    """越界目标仍按 illegal_action 失败，硬边界不因换表达方式而放松。"""
    planner = _ToolProvider(
        'reply',
        '{"target": 999, "reasons": ["direct_question"], "length": "brief", '
        '"reference": "x"}',
    )
    replyer = _ScriptedProvider(['<say>不该被调用</say>'])
    agent = ConversationAgent(
        planner, temperature=0.8, replyer=replyer, tool_calling=True,
    )

    outcome = await agent.run(
        _frame(), [{'role': 'user', 'content': '在吗'}], _gate_inputs(), (),
        replyer_messages=_messages_from(lambda head: [{'role': 'user', 'content': 'x'}]),
    )

    assert outcome.event_status == 'illegal_action'
    assert replyer.calls == 0


async def test_tool_calling_rejects_xml_fallback() -> None:
    """工具模式只认工具调用，旧 XML 动作头不能成为隐式兼容路径。"""
    planner = _ScriptedProvider([
        '<decision action="reply" targets="101" reasons="direct_question" '
        'length="brief"/><say>不该被接受</say>',
    ])
    replyer = _ScriptedProvider(['<say>也不该被调用</say>'])
    agent = ConversationAgent(
        planner,
        temperature=0.8,
        replyer=replyer,
        tool_calling=True,
    )

    outcome = await agent.run(
        _frame(),
        [{'role': 'user', 'content': '在吗'}],
        _gate_inputs(),
        (),
        replyer_messages=_messages_from(
            lambda head: [{'role': 'user', 'content': head.reference or ''}]
        ),
    )

    assert outcome.event_status == 'parse_error'
    assert outcome.decision is None
    assert '没有通过工具选择动作' in outcome.action_event.detail
    assert replyer.calls == 0


def test_tool_calling_requires_replyer() -> None:
    """工具调用只产出决策，没有 replyer 就没有正文来源，必须在构造期拒绝。"""
    with pytest.raises(ValueError, match='必须同时注入 replyer'):
        ConversationAgent(_ScriptedProvider(), temperature=0.8, tool_calling=True)


async def test_two_stages_use_their_own_sampling_params() -> None:
    """决策与表达各用各的采样参数：一级要判断稳定，一级要表达自然。"""
    planner = _RecordingParamsProvider([
        '<decision action="reply" targets="101" reasons="can_add_value" '
        'length="brief" reference="接一句"/>',
    ])
    replyer = _RecordingParamsProvider(['<say>在的</say>'])
    agent = ConversationAgent(
        planner,
        temperature=0.3,
        max_tokens=1024,
        replyer=replyer,
        replyer_temperature=0.95,
        replyer_max_tokens=400,
    )

    await agent.run(
        _frame(), [{'role': 'user', 'content': '在吗'}], _gate_inputs(), (),
        replyer_messages=_messages_from(lambda head: [{'role': 'user', 'content': 'x'}]),
    )

    assert planner.params[0] == (0.3, 1024)
    assert replyer.params[0] == (0.95, 400)


async def test_replyer_params_default_to_decision_params() -> None:
    """没单独配时表达层沿用决策那一档，不会因为拆分就悄悄换了采样口径。"""
    planner = _RecordingParamsProvider([
        '<decision action="reply" targets="101" reasons="can_add_value" '
        'length="brief" reference="接一句"/>',
    ])
    replyer = _RecordingParamsProvider(['<say>在的</say>'])
    agent = ConversationAgent(planner, temperature=0.6, max_tokens=800, replyer=replyer)

    await agent.run(
        _frame(), [{'role': 'user', 'content': '在吗'}], _gate_inputs(), (),
        replyer_messages=_messages_from(lambda head: [{'role': 'user', 'content': 'x'}]),
    )

    assert replyer.params[0] == (0.6, 800)
