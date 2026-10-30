"""正则规则的匹配、替换、生效会话、冷却与多条取一。

对应任务书 ★2、★3、★5、★6、★7。用例直接调用纯函数 ``match_regex_rules``，
注入固定随机种子与固定毫秒时钟，冷却表由用例自己持有，不依赖真实时钟与全局状态。
对话内容全部是虚构的。
"""

from __future__ import annotations

from typing import Dict, List, Tuple

import random

from src.core.agent.expression import EXPRESSION_COOLDOWN_MS
from src.core.agent.regex_rule import has_matching_regex_rule, match_regex_rules
from src.core.config.schema import RegexRuleConfig

# 固定的「当前时间」毫秒时间戳；冷却系数全部相对它计算。
_NOW_MS = 1_800_000_000_000
_STREAM_ID = 5
_STREAM_EXTERNAL_ID = '10001'
# 「是这样的」句式规则，与新装预填的写法同构，但用例只依赖这里的副本。
_IS_LIKE_THIS = RegexRuleConfig(
    pattern=r'^(?P<n>\S{1,20})是这样的[。！!]*$',
    reaction='{n}是这样的，〈某类人〉只要〈做一件简单的事〉就可以',
)


def _match(
    rules: List[RegexRuleConfig],
    text: str,
    *,
    cooldowns: Dict[Tuple[str, int], int] | None = None,
    stream_id: int = _STREAM_ID,
    external_id: str = _STREAM_EXTERNAL_ID,
    seed: int = 7,
):
    """用固定时钟与固定种子跑一次匹配。"""

    return match_regex_rules(
        rules,
        text,
        stream_external_id=external_id,
        stream_id=stream_id,
        now=_NOW_MS,
        cooldowns={} if cooldowns is None else cooldowns,
        rng=random.Random(seed),
    )


class TestMatchText:
    """★2：先清洗占位段、去首尾空白，再截到 500 字后匹配。"""

    def test_placeholder_spans_are_stripped_before_anchored_match(self) -> None:
        result = _match([_IS_LIKE_THIS], '[表情包：一只猫歪头]  猫是这样的！ [图片：窗外]')

        assert result.reaction is not None
        assert result.reaction.startswith('猫是这样的，')
        assert [(item.rule, item.result) for item in result.outcomes] == [(1, 'injected')]

    def test_reply_header_is_stripped_too(self) -> None:
        result = _match([_IS_LIKE_THIS], '[回复 小雨：今天下雨吗] 天气是这样的')

        assert result.reaction is not None
        assert result.reaction.startswith('天气是这样的，')

    def test_text_beyond_500_chars_does_not_take_part(self) -> None:
        rule = RegexRuleConfig(pattern='早睡打卡', reaction='夸一句坚持')
        head = '嗯' * 500

        assert _match([rule], head + '早睡打卡').reaction is None
        assert _match([rule], head[:-4] + '早睡打卡').reaction == '夸一句坚持'

    def test_no_hit_returns_nothing(self) -> None:
        result = _match([_IS_LIKE_THIS], '今天晚上吃什么')

        assert result.reaction is None
        assert result.outcomes == []


class TestReaction:
    """★3：只替换 ``{名字}`` 形式的命名组引用，其余原样保留。"""

    def test_named_group_is_replaced_with_capture(self) -> None:
        result = _match([_IS_LIKE_THIS], '程序员是这样的')

        assert result.reaction == '程序员是这样的，〈某类人〉只要〈做一件简单的事〉就可以'

    def test_non_identifier_braces_and_angle_brackets_stay(self) -> None:
        rule = RegexRuleConfig(
            pattern=r'(?P<thing>\S+)好难',
            reaction='{thing}确实难，{ 不是组 }、{1}、{}和{{thing}}都别动，〈谁〉也别动',
        )

        result = _match([rule], '数学好难')

        assert result.reaction == '数学确实难，{ 不是组 }、{1}、{}和{数学}都别动，〈谁〉也别动'

    def test_reaction_without_reference_is_kept_verbatim(self) -> None:
        rule = RegexRuleConfig(pattern='晚安', reaction='回一句〈晚安的变体〉，别多说')

        assert _match([rule], '大家晚安').reaction == '回一句〈晚安的变体〉，别多说'

    def test_named_references_inside_angle_brackets_stay_verbatim(self) -> None:
        rule = RegexRuleConfig(
            pattern=r'(?P<who>\S+)来了',
            reaction='{who}来了，〈围绕{who}现编一句〉，再向{who}打招呼',
        )

        assert _match([rule], '小雨来了').reaction == (
            '小雨来了，〈围绕{who}现编一句〉，再向小雨打招呼'
        )


class TestStreams:
    """★5：生效会话按会话外部 ID 比对，留空表示全部会话。"""

    def test_listed_stream_matches(self) -> None:
        rule = RegexRuleConfig(pattern='晚安', reaction='晚安', streams=['20002', _STREAM_EXTERNAL_ID])

        assert _match([rule], '晚安').reaction == '晚安'

    def test_unlisted_stream_does_not_match_or_report(self) -> None:
        rule = RegexRuleConfig(pattern='晚安', reaction='晚安', streams=['20002'])

        result = _match([rule], '晚安')

        assert result.reaction is None
        assert result.outcomes == []

    def test_empty_streams_match_every_stream(self) -> None:
        rule = RegexRuleConfig(pattern='晚安', reaction='晚安')

        for external_id in ('10001', '20002', 'desktop'):
            assert _match([rule], '晚安', external_id=external_id).reaction == '晚安'


class TestCooldown:
    """★6：注入概率为 min(1, 距上次注入 / 6 小时)，按会话各算。"""

    def test_just_injected_rule_is_skipped_as_cooldown(self) -> None:
        cooldowns = {(_IS_LIKE_THIS.pattern, _STREAM_ID): _NOW_MS}

        result = _match([_IS_LIKE_THIS], '猫是这样的', cooldowns=cooldowns)

        assert result.reaction is None
        assert [(item.rule, item.result) for item in result.outcomes] == [(1, 'cooldown')]

    def test_three_hours_later_injects_about_half(self) -> None:
        cooldowns = {(_IS_LIKE_THIS.pattern, _STREAM_ID): _NOW_MS - EXPRESSION_COOLDOWN_MS // 2}
        rng = random.Random(20261028)
        trials = 4000
        injected = 0
        for _ in range(trials):
            result = match_regex_rules(
                [_IS_LIKE_THIS],
                '猫是这样的',
                stream_external_id=_STREAM_EXTERNAL_ID,
                stream_id=_STREAM_ID,
                now=_NOW_MS,
                cooldowns=cooldowns,
                rng=rng,
            )
            injected += result.reaction is not None

        # 4000 次伯努利(0.5) 的标准差约 0.008，±0.03 足够排除误判。
        assert abs(injected / trials - 0.5) < 0.03

    def test_six_hours_later_always_injects(self) -> None:
        cooldowns = {(_IS_LIKE_THIS.pattern, _STREAM_ID): _NOW_MS - EXPRESSION_COOLDOWN_MS}

        for seed in range(50):
            assert _match([_IS_LIKE_THIS], '猫是这样的', cooldowns=cooldowns, seed=seed).reaction

    def test_cooldown_in_group_a_does_not_affect_group_b(self) -> None:
        cooldowns = {(_IS_LIKE_THIS.pattern, 1): _NOW_MS}

        in_a = _match([_IS_LIKE_THIS], '猫是这样的', cooldowns=cooldowns, stream_id=1)
        in_b = _match([_IS_LIKE_THIS], '猫是这样的', cooldowns=cooldowns, stream_id=2)

        assert in_a.reaction is None
        assert in_b.reaction is not None

    def test_matching_never_writes_cooldown_table(self) -> None:
        cooldowns: Dict[Tuple[str, int], int] = {}

        _match([_IS_LIKE_THIS], '猫是这样的', cooldowns=cooldowns)

        assert cooldowns == {}


class TestOneAtATime:
    """★7：同一回合命中多条时只注入一条，其余记为落选。"""

    def test_multiple_hits_inject_exactly_one(self) -> None:
        rules = [
            RegexRuleConfig(pattern='猫', reaction='甲'),
            RegexRuleConfig(pattern='是这样的', reaction='乙'),
            RegexRuleConfig(pattern=r'^\S+是', reaction='丙'),
            RegexRuleConfig(pattern='狗', reaction='丁'),
        ]

        for seed in range(30):
            result = _match(rules, '猫是这样的', seed=seed)
            results = {item.rule: item.result for item in result.outcomes}

            # 第 4 条没命中，不出现在结果里。
            assert set(results) == {1, 2, 3}
            assert list(results.values()).count('injected') == 1
            assert list(results.values()).count('outvoted') == 2
            injected = next(rule for rule, value in results.items() if value == 'injected')
            assert result.reaction == rules[injected - 1].reaction

    def test_cooldown_rule_is_reported_separately_from_outvoted(self) -> None:
        rules = [
            RegexRuleConfig(pattern='猫', reaction='甲'),
            RegexRuleConfig(pattern='是这样的', reaction='乙'),
        ]
        cooldowns = {('猫', _STREAM_ID): _NOW_MS}

        result = _match(rules, '猫是这样的', cooldowns=cooldowns)

        assert [(item.rule, item.result) for item in result.outcomes] == [
            (1, 'cooldown'), (2, 'injected'),
        ]
        assert result.reaction == '乙'


def test_attention_match_uses_the_same_stream_scope_and_cleaned_text() -> None:
    """入口候选与正文注入共用会话范围、清洗、500 字符限制。"""
    rule = RegexRuleConfig(pattern='^猫是这样的$', reaction='接一句', streams=['10001'])

    assert has_matching_regex_rule([rule], '[表情包：猫] 猫是这样的', '10001')
    assert not has_matching_regex_rule([rule], '猫是这样的', '20002')
    assert not has_matching_regex_rule([rule], '普通消息', '10001')
    assert not has_matching_regex_rule(
        [RegexRuleConfig(pattern='晚安', reaction='接一句')], '嗯' * 500 + '晚安', '10001',
    )
