"""Conversation 行动核心行动协议验收。"""

from __future__ import annotations

from typing import Any

import pytest

from src.core.agent.action_protocol import (
    ActionDecisionEvent,
    ConversationDecision,
    DecisionFrame,
    GateInputFacts,
    IllegalActionError,
    PlatformCapabilities,
    ReplyPayload,
    available_actions,
)


def _caps(**overrides: Any) -> PlatformCapabilities:
    """构造默认无引用、无 reaction 的平台能力。"""
    return PlatformCapabilities(**overrides)


def _frame(**overrides: Any) -> DecisionFrame:
    """构造群聊 DELIBERATE 默认帧：可选消息 101/102，水位 102。"""
    base: dict[str, Any] = dict(
        turn_id=7,
        snapshot_id='snap-7',
        stream_kind='group',
        disposition='deliberate',
        selectable_message_ids=(101, 102),
        message_watermark=102,
        available_actions=available_actions(
            'group', 'deliberate', _caps(),
        ),
        capabilities=_caps(),
    )
    base.update(overrides)
    return DecisionFrame(**base)


def _reply(text: str = "在的。", length: str = "brief") -> ReplyPayload:
    """构造默认的 brief 回复负载。"""
    return ReplyPayload(text=text, length=length)  # type: ignore[arg-type]


def _decision(**overrides: Any) -> ConversationDecision:
    """构造合法的 reply 决策，仅覆盖需要变化的字段。"""
    base: dict[str, Any] = dict(
        action='reply',
        target_message_ids=(101,),
        quote_message_id=None,
        reason_codes=('direct_question',),
        reply=_reply(),
    )
    base.update(overrides)
    return ConversationDecision(**base)


def test_valid_reply_decision_passes_frame_validation() -> None:
    """合法 reply 决策通过结构自检与帧内校验。"""
    decision = _decision(
        target_message_ids=(101, 102),
        quote_message_id=101,
        reason_codes=('direct_question', 'topic_continuation'),
    )
    frame = _frame(capabilities=_caps(quote=True))

    decision.validate(frame)


def test_valid_silent_decision_passes_frame_validation() -> None:
    """合法 silent 决策：空目标、无引用、无负载，只带沉默理由。"""
    decision = ConversationDecision(
        action='silent',
        target_message_ids=(),
        quote_message_id=None,
        reason_codes=('others_conversation', 'would_interrupt'),
        reply=None,
    )

    decision.validate(_frame())


def test_reply_requires_payload() -> None:
    """reply 动作必须携带正文负载。"""
    with pytest.raises(IllegalActionError):
        _decision(reply=None)


def test_reply_requires_at_least_one_target() -> None:
    """reply 动作必须指定至少一条目标消息。"""
    with pytest.raises(IllegalActionError):
        _decision(target_message_ids=())


def test_silent_rejects_reply_payload() -> None:
    """silent 不允许携带 reply 负载。"""
    with pytest.raises(IllegalActionError):
        _decision(action="silent", reason_codes=("low_relevance",))


def test_silent_rejects_targets_and_quote() -> None:
    """silent 不允许指向目标消息或携带引用。"""
    with pytest.raises(IllegalActionError):
        _decision(
            action='silent',
            target_message_ids=(101,),
            reason_codes=('low_relevance',),
            reply=None,
        )
    with pytest.raises(IllegalActionError):
        _decision(
            action='silent',
            quote_message_id=101,
            reason_codes=('low_relevance',),
            reply=None,
        )


def test_force_frame_rejects_silent() -> None:
    """FORCE 场景返回 silent 属于协议错误，不得降级成普通回复。"""
    silent = ConversationDecision(
        action='silent',
        target_message_ids=(),
        quote_message_id=None,
        reason_codes=('low_relevance',),
        reply=None,
    )
    frame = _frame(
        disposition='force',
        available_actions=available_actions('group', 'force', _caps()),
    )

    with pytest.raises(IllegalActionError):
        silent.validate(frame)


def test_drop_frame_rejects_any_decision() -> None:
    """DROP 候选不调用模型，任何决策都不存在合法性。"""
    frame = _frame(
        disposition='drop',
        available_actions=frozenset(),
    )

    with pytest.raises(IllegalActionError):
        _decision().validate(frame)


def test_action_outside_available_actions_rejected() -> None:
    """模型只能在运行时给出的动作空间内选择。"""
    react = _decision(action="react", reply=None, reaction="128074")

    with pytest.raises(IllegalActionError):
        react.validate(_frame())


def test_target_outside_selectable_rejected() -> None:
    """目标消息必须属于本回合 selectable_message_ids。"""
    decision = _decision(target_message_ids=(103,))

    with pytest.raises(IllegalActionError):
        decision.validate(_frame())


def test_target_beyond_watermark_rejected() -> None:
    """目标不得指向回合水位之后的未来消息。"""
    # 帧构造即拒绝晚于水位的可选消息，目标校验因此不可能放行未来消息。
    with pytest.raises(ValueError):
        _frame(
            selectable_message_ids=(101, 103),
            message_watermark=102,
        )


def test_quote_must_be_selectable_and_require_capability() -> None:
    """引用必须属于可选集，且平台不支持引用时决策不得携带引用。"""
    decision = _decision(quote_message_id=103)
    frame = _frame(capabilities=_caps(quote=True))
    with pytest.raises(IllegalActionError):
        decision.validate(frame)

    decision = _decision(quote_message_id=101)
    frame = _frame(capabilities=_caps(quote=False))
    with pytest.raises(IllegalActionError):
        decision.validate(frame)


def test_free_text_reason_code_rejected() -> None:
    """reason_codes 封闭枚举，模型自由编造直接拒绝。"""
    with pytest.raises(IllegalActionError):
        _decision(reason_codes=('i_feel_like_it',))


def test_reason_code_domain_must_match_action() -> None:
    """回复与沉默理由分域，跨域组合属于自相矛盾。"""
    with pytest.raises(IllegalActionError):
        _decision(reason_codes=('would_interrupt',))
    with pytest.raises(IllegalActionError):
        _decision(
            action='silent',
            target_message_ids=(),
            quote_message_id=None,
            reason_codes=('direct_question',),
            reply=None,
        )


def test_react_requires_single_target_and_no_payload() -> None:
    """react 只允许一条目标消息，且不能与正文或引用混用。"""
    caps = _caps(
        react=True,
        available_reactions=("128074",),
    )
    frame = _frame(
        available_actions=available_actions("group", "deliberate", caps),
        capabilities=caps,
    )
    react = _decision(
        action='react',
        target_message_ids=(101,),
        quote_message_id=None,
        reason_codes=('natural_reaction',),
        reply=None,
        reaction='128074',
    )

    react.validate(frame)

    with pytest.raises(IllegalActionError):
        _decision(
            action='react',
            target_message_ids=(101, 102),
            quote_message_id=None,
            reason_codes=('natural_reaction',),
            reply=None,
            reaction='128074',
        )

    # 贴哪个表情最终要落到平台的封闭编号上，漏写就没有可执行的动作。
    with pytest.raises(IllegalActionError):
        _decision(
            action='react',
            target_message_ids=(101,),
            quote_message_id=None,
            reason_codes=('natural_reaction',),
            reply=None,
        )

    # 平台没有这个反应时不能硬发：能力集是运行时给的封闭集合。
    with pytest.raises(IllegalActionError):
        _decision(
            action='react',
            target_message_ids=(101,),
            quote_message_id=None,
            reason_codes=('natural_reaction',),
            reply=None,
            reaction='不存在的反应',
        ).validate(frame)


def test_available_actions_desktop_and_direct_exclude_silent() -> None:
    """私聊与桌面第一版动作集合不含 silent。"""
    for kind in ('desktop', 'direct'):
        actions = available_actions(kind, "force", _caps())

        assert actions == frozenset({'reply'})


def test_available_actions_group_force_excludes_silent() -> None:
    """群聊真实 @ 且 @必回时，动作集合中没有 silent。"""
    actions = available_actions("group", "force", _caps())

    assert actions == frozenset({'reply'})


def test_available_actions_deliberate_opens_react_only_with_capability() -> None:
    """react 仅在平台已验证 reaction 能力时进入动作集。"""
    plain = available_actions("group", "deliberate", _caps())
    assert plain == frozenset({'reply', 'silent'})

    caps = _caps(react=True, available_reactions=("128074",))
    extended = available_actions("group", "deliberate", caps)
    assert extended == frozenset({'reply', 'silent', 'react'})


def test_available_actions_drop_is_empty() -> None:
    """DROP 不调用模型，动作集为空。"""
    assert available_actions("group", "drop", _caps()) == frozenset()


def test_platform_capabilities_reject_react_without_reactions() -> None:
    """开启 react 却给不出反应标识属于配置错误，显式拒绝。"""
    with pytest.raises(ValueError):
        _caps(react=True, available_reactions=())


def _gate_inputs(**overrides: Any) -> GateInputFacts:
    """构造默认的门控输入事实。"""
    base: dict[str, Any] = dict(
        stream_kind='group',
        mentioned_me=True,
        name_mentioned=False,
        must_reply=True,
        asleep=False,
        rate_limited=False,
        recent_bot_replies=1,
        candidate_message_ids=(101, 102),
        selectable_message_ids=(101, 102),
    )
    base.update(overrides)
    return GateInputFacts(**base)


def test_action_event_has_four_audit_layers() -> None:
    """行动事件四层可查：输入事实 / 门控 / 决策 / 版本信息。"""
    event = ActionDecisionEvent(
        turn_id=7,
        snapshot_id='snap-7',
        turn_message_watermark=102,
        gate_inputs=_gate_inputs(),
        gate_disposition='deliberate',
        gate_reason_codes=('direct_mention',),
        available_actions=('reply', 'silent'),
        decision=_decision(),
        event_status='committed',
        prompt_hash='abc123',
        model_task='chat.system',
        provider='openai',
        model='test-model',
        latency_ms=420,
    )

    payload = event.to_dict()

    assert payload['eventStatus'] == 'committed'
    assert payload['inputs']['mentionedMe'] is True
    assert payload['gate']['disposition'] == 'deliberate'
    assert payload['gate']['reasonCodes'] == ['direct_mention']
    assert payload['gate']['availableActions'] == ['reply', 'silent']
    assert payload['decision']['action'] == 'reply'
    assert payload['decision']['targetMessageIds'] == [101]
    assert payload['decision']['reply']['text'] == '在的。'
    assert payload['version']['promptHash'] == 'abc123'
    assert payload['version']['latencyMs'] == 420


def test_event_status_distinguishes_silence_from_failures() -> None:
    """自主沉默、门控丢弃与模型失败必须是不同的事件状态。"""
    silent = ActionDecisionEvent(
        turn_id=7,
        snapshot_id='snap-7',
        turn_message_watermark=102,
        gate_inputs=_gate_inputs(),
        gate_disposition='deliberate',
        gate_reason_codes=('name_mention',),
        available_actions=('reply', 'silent'),
        decision=ConversationDecision(
            action='silent',
            target_message_ids=(),
            quote_message_id=None,
            reason_codes=('attention_elsewhere',),
            reply=None,
        ),
        event_status='silent_by_choice',
    )
    dropped = ActionDecisionEvent(
        turn_id=8,
        snapshot_id='snap-8',
        turn_message_watermark=103,
        gate_inputs=_gate_inputs(),
        gate_disposition='drop',
        gate_reason_codes=('attention_filtered',),
        available_actions=(),
        decision=None,
        event_status='gate_dropped',
    )
    failed = ActionDecisionEvent(
        turn_id=9,
        snapshot_id='snap-9',
        turn_message_watermark=104,
        gate_inputs=_gate_inputs(),
        gate_disposition='deliberate',
        gate_reason_codes=('name_mention',),
        available_actions=('reply', 'silent'),
        decision=None,
        event_status='provider_error',
    )

    assert silent.to_dict()['eventStatus'] == 'silent_by_choice'
    assert silent.to_dict()['decision']['action'] == 'silent'
    assert dropped.to_dict()['eventStatus'] == 'gate_dropped'
    assert dropped.to_dict()['decision'] is None
    assert failed.to_dict()['eventStatus'] == 'provider_error'
    assert failed.to_dict()['decision'] is None

