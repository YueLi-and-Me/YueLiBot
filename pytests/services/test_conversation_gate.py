"""Conversation 行动核心三态门控验收。"""

from __future__ import annotations

from typing import Any

import pytest

from src.core.agent.action_protocol import PlatformCapabilities, available_actions
from src.core.agent.conversation_gate import (
    DELIBERATE_GATE_CODES,
    DROP_GATE_CODES,
    FORCE_GATE_CODES,
    NATURAL_REPLY_WINDOW_MS,
    POKE_SIGNAL_LIMIT,
    GateRequest,
    GateResult,
    decide_disposition,
)


def _request(**overrides: Any) -> GateRequest:
    """用群聊默认值构造门控请求，仅覆盖需要变化的字段。"""
    base: dict[str, Any] = dict(
        stream_kind='group',
        mentioned_me=False,
        name_mentioned=False,
        asleep=False,
        at_mention_must_reply=True,
        replies_in_window=0,
        max_replies_in_window=3,
    )
    base.update(overrides)
    return GateRequest(**base)


def test_self_message_dropped_before_anything_else() -> None:
    """Bot 自己的消息直接 DROP，连私聊契约也不得回环。"""
    result = decide_disposition(_request(stream_kind='desktop', is_self_message=True))

    assert result.disposition == 'drop'
    assert result.reason_codes == ('self_message',)


def test_desktop_and_user_started_direct_are_forced() -> None:
    """桌面和用户主动发起的 QQ 私聊都必须回复。"""
    desktop = decide_disposition(_request(stream_kind='desktop'))
    direct = decide_disposition(_request(stream_kind='direct'))

    assert desktop.disposition == 'force'
    assert desktop.reason_codes == ('direct_conversation',)
    assert direct.disposition == 'force'
    assert direct.reason_codes == ('direct_conversation',)


def test_at_mention_must_reply_is_force() -> None:
    """真实 @ 且 @必回开启时 FORCE，先于休眠与频率硬限。"""
    result = decide_disposition(_request(
        mentioned_me=True,
        at_mention_must_reply=True,
        asleep=True,
        replies_in_window=5,
        max_replies_in_window=3,
    ))

    assert result.disposition == 'force'
    assert result.reason_codes == ('at_mention_must_reply',)


def test_at_mention_without_must_reply_is_deliberate() -> None:
    """真实 @ 但 @必回未开启时只进入意识，回不回由 Agent 决定。"""
    result = decide_disposition(_request(
        mentioned_me=True,
        at_mention_must_reply=False,
    ))

    assert result.disposition == 'deliberate'
    assert 'direct_mention' in result.reason_codes


def test_name_mention_enters_deliberate_without_forced_reply() -> None:
    """名字/别名出现时进入 DELIBERATE，门控不写死回复。"""
    result = decide_disposition(_request(name_mentioned=True))

    assert result.disposition == 'deliberate'
    assert 'name_mention' in result.reason_codes


def test_asleep_group_is_dropped() -> None:
    """群聊休眠时 DROP，不调用模型。"""
    result = decide_disposition(_request(asleep=True))

    assert result.disposition == 'drop'
    assert result.reason_codes == ('asleep',)


def test_rate_limited_group_is_dropped() -> None:
    """窗口内回复数达到硬上限时 DROP。"""
    result = decide_disposition(_request(
        replies_in_window=3,
        max_replies_in_window=3,
    ))

    assert result.disposition == 'drop'
    assert result.reason_codes == ('rate_limited',)


def test_plain_group_chatter_is_attention_filtered() -> None:
    """无任何注意力信号的群聊噪声在批次成形前即被 DROP。"""
    result = decide_disposition(_request())

    assert result.disposition == 'drop'
    assert result.reason_codes == ('attention_filtered',)


def test_question_enters_deliberate() -> None:
    """明确问题属于注意力信号，进入意识而非直接回复。"""
    result = decide_disposition(_request(is_clear_question=True))

    assert result.disposition == 'deliberate'
    assert 'clear_question' in result.reason_codes


def test_reply_to_bot_enters_deliberate() -> None:
    """回复 Bot 的消息进入 DELIBERATE。"""
    result = decide_disposition(_request(reply_to_bot=True))

    assert result.disposition == 'deliberate'
    assert 'reply_to_bot' in result.reason_codes


def test_natural_reply_window_signal_comes_from_recent_replies() -> None:
    """距上一条 Bot 回复足够近时，自然回应窗口仍然敞开。"""
    result = decide_disposition(_request(
        replies_in_window=1,
        max_replies_in_window=3,
        last_bot_reply_elapsed_ms=NATURAL_REPLY_WINDOW_MS,
    ))

    assert result.disposition == 'deliberate'
    assert 'natural_reply_window' in result.reason_codes


def test_ten_minute_reply_count_no_longer_opens_natural_window() -> None:
    """十分钟窗口内回过话但超过短跟进时限时，普通群消息不再自然触发。"""
    result = decide_disposition(_request(
        replies_in_window=1,
        max_replies_in_window=3,
        last_bot_reply_elapsed_ms=NATURAL_REPLY_WINDOW_MS + 1,
    ))

    assert result.disposition == 'drop'
    assert result.reason_codes == ('attention_filtered',)


def test_missing_last_reply_age_closes_natural_window() -> None:
    """调用方未提供上一条回复时间时，不得仅凭回复计数打开自然窗口。"""
    result = decide_disposition(_request(replies_in_window=1, max_replies_in_window=3))

    assert result.disposition == 'drop'
    assert result.reason_codes == ('attention_filtered',)


def test_poke_enters_deliberate_but_never_forces() -> None:
    """被戳是明确的直接互动，抬入 DELIBERATE；但不带内容，不能强制回复。

    连戳会刷屏，而她的动作集里本来就有 poke 可以戳回去，所以接不接由她决定。
    """
    result = decide_disposition(_request(poked_me=True))

    assert result.disposition == 'deliberate'
    assert 'direct_poke' in result.reason_codes


def test_poke_still_yields_to_sleep() -> None:
    """休眠先于被戳生效：睡着了就是睡着了，戳也不醒。"""
    result = decide_disposition(_request(poked_me=True, asleep=True))

    assert result.reason_codes == ('asleep',)


def test_poke_over_window_limit_is_dropped() -> None:
    """同一信号窗口内超出上限的 poke 直接丢弃：一段连戳最多把 Bot 戳出来三次。

    第二个断言刻意带上自然接话窗口与话题延续两个事实：丢弃必须排在注意力信号
    收集之前，否则 Bot 刚回过上一次戳，紧随其后的戳一戳会被 natural_reply_window
    原样接住——只把 direct_poke 从抬入码中去掉无效。
    """
    last_allowed = decide_disposition(_request(
        poked_me=True, pokes_in_window=POKE_SIGNAL_LIMIT,
    ))

    assert last_allowed.disposition == 'deliberate'
    assert 'direct_poke' in last_allowed.reason_codes

    over_limit = decide_disposition(_request(
        poked_me=True,
        pokes_in_window=POKE_SIGNAL_LIMIT + 1,
        last_bot_reply_elapsed_ms=0,
        current_topic_available=True,
    ))

    assert over_limit.disposition == 'drop'
    assert over_limit.reason_codes == ('poke_repeat',)


def test_direct_signals_are_not_silenced_by_the_reply_cap() -> None:
    """撞上频率硬上限时，直接点名类信号仍然放行；自发参与才被压住。

    上限的判据是她自己说了多少，与「这句话是不是冲着她来的」无关。真机上出现过
    「小璃你要为我做主啊」这类明确点名被上限丢弃、她完全没有反应的情况。
    """
    # @必回关闭，让真实 @ 也走 DELIBERATE，四种直接信号可以用同一套断言。
    capped = dict(
        replies_in_window=3,
        max_replies_in_window=3,
        at_mention_must_reply=False,
    )
    for field, code in (
        ('poked_me', 'direct_poke'),
        ('name_mentioned', 'name_mention'),
        ('reply_to_bot', 'reply_to_bot'),
        ('mentioned_me', 'direct_mention'),
    ):
        result = decide_disposition(_request(**capped, **{field: True}))
        assert result.disposition == 'deliberate', field
        assert code in result.reason_codes, field

    # 没有任何直接信号时，上限照旧生效——自然接话窗口不能越过它。
    spontaneous = decide_disposition(_request(
        **capped, last_bot_reply_elapsed_ms=0, current_topic_available=True,
    ))

    assert spontaneous.reason_codes == ('rate_limited',)


def test_ongoing_topic_catches_what_the_time_window_misses() -> None:
    """时限已过但她开口之后群里没聊几句时，由 ongoing_topic 接住。

    真机场景：她 17:00:44 发言，17:03:55 的「肘，我们去收拾他」距上一条回复
    3 分 11 秒（超过 90 秒时限），中间只隔了 1 条消息（话题没有走远）。
    """
    result = decide_disposition(_request(
        replies_in_window=2,
        max_replies_in_window=3,
        last_bot_reply_elapsed_ms=NATURAL_REPLY_WINDOW_MS * 3,
        current_topic_available=True,
    ))

    assert result.disposition == 'deliberate'
    assert result.reason_codes == ('ongoing_topic',)


def test_declined_follow_up_closes_ongoing_topic_too() -> None:
    """她放弃跟进后，两条跟进口径一起关闭，不只关掉时限那条。"""
    result = decide_disposition(_request(
        last_bot_reply_elapsed_ms=0,
        current_topic_available=True,
        follow_up_declined=True,
    ))

    assert result.disposition == 'drop'
    assert result.reason_codes == ('attention_filtered',)


def test_declined_follow_up_closes_natural_window() -> None:
    """她在上一次跟进机会里选择沉默后，自然回应窗口关闭。"""
    result = decide_disposition(_request(
        replies_in_window=2,
        max_replies_in_window=3,
        last_bot_reply_elapsed_ms=0,
        follow_up_declined=True,
    ))

    assert result.disposition == 'drop'
    assert result.reason_codes == ('attention_filtered',)


def test_reply_count_alone_no_longer_closes_natural_window() -> None:
    """只要她没有放弃跟进，十分钟窗口内回过几条都不关闭自然回应窗口。

    回复计数与「这轮话是不是冲着她来的」没有因果关系；关闭条件是她自己的
    终局动作，硬边界由 max_replies_in_window 单独承担。
    """
    result = decide_disposition(_request(
        replies_in_window=2,
        max_replies_in_window=3,
        last_bot_reply_elapsed_ms=0,
    ))

    assert result.disposition == 'deliberate'
    assert 'natural_reply_window' in result.reason_codes


def test_multiple_signals_collect_multiple_codes() -> None:
    """多个注意力信号同时命中时，原因码全部进入审计结果。"""
    result = decide_disposition(_request(
        name_mentioned=True,
        reply_to_bot=True,
        is_clear_question=True,
        recognizable_target=True,
    ))

    assert result.disposition == 'deliberate'
    assert set(result.reason_codes) == {
        'name_mention', 'reply_to_bot', 'clear_question', 'recognizable_target',
    }


def test_gate_result_rejects_foreign_code() -> None:
    """门控原因码封闭：其他门控态的原因码不得混入。"""
    with pytest.raises(ValueError):
        GateResult('drop', ('name_mention',))


def test_gate_result_rejects_unknown_disposition() -> None:
    """门控态只有三态，未知态直接拒绝。"""
    with pytest.raises(ValueError):
        GateResult('skip', ('asleep',))  # type: ignore[arg-type]


def test_gate_code_sets_are_disjoint() -> None:
    """三态原因码互不重叠，保证审计事件一眼可辨门控层。"""
    assert not (DROP_GATE_CODES & FORCE_GATE_CODES)
    assert not (DROP_GATE_CODES & DELIBERATE_GATE_CODES)
    assert not (FORCE_GATE_CODES & DELIBERATE_GATE_CODES)


def test_gate_request_rejects_negative_window_counts() -> None:
    """负回复数与零窗口上限会翻转频率比较，必须显式拒绝。"""
    with pytest.raises(ValueError):
        _request(replies_in_window=-1)
    with pytest.raises(ValueError):
        _request(max_replies_in_window=0)
    with pytest.raises(ValueError):
        _request(last_bot_reply_elapsed_ms=-1)


def test_drop_disposition_has_empty_action_space() -> None:
    """DROP 不调用模型，动作空间必须为空。"""
    actions = available_actions('group', 'drop', PlatformCapabilities())

    assert actions == frozenset()

