"""正则规则：对方那句话命中正则时，挑一种接法放进写正文的提示词。

规则来自 ``personality.regex_rules``，由管理员按自己群里的写法填写。本模块只做
纯计算：清洗并截取她要回的那句话、逐条匹配、按冷却系数决定注入哪一条、把命名组
捕获替换进反应提示。冷却表由调用方持有并在渲染前回写，本模块不持有任何全局状态，
随机源与时钟都由调用方注入，便于单测固定。

口径：

- 匹配对象先用 :func:`strip_machine_spans` 去掉表情包、图片、引用回复等平台占位段，
  再去首尾空白、截到前 :data:`REGEX_RULE_TEXT_LIMIT` 个字符，然后 ``re.search``；
  不做繁简转换。
- 冷却按会话各算：键为 ``(pattern, stream_id)``，值为上次注入的毫秒时间。注入概率为
  ``min(1, 距上次注入 / EXPRESSION_COOLDOWN_MS)``，从没注入过为 1；复用表达方式的
  冷却时间窗，不另设参数。
- 同一回合命中多条时，先各自按冷却系数过一次概率，过了的再按冷却系数加权只抽一条；
  没过的记 ``cooldown``，过了但没抽中的记 ``outvoted``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Literal, Optional, Sequence, Tuple

import random
import re

from .expression import EXPRESSION_COOLDOWN_MS
from src.core.config.schema import REACTION_GROUP_REFERENCE, RegexRuleConfig
from src.core.memory.high_frequency import strip_machine_spans

# 参与匹配的最大字符数。规则面向一句话的开头或整句句式，长文后半段命中多半是
# 误触；同时限住单次 re.search 的输入长度，与正则长度上限一起压住回溯风险。
REGEX_RULE_TEXT_LIMIT = 500

RuleResult = Literal['injected', 'cooldown', 'outvoted']


@dataclass(frozen=True)
class RuleOutcome:
    """一条命中规则在本回合的去向。

    :ivar rule: 规则在 ``personality.regex_rules`` 里的序号，从 1 起。
    :ivar pattern: 该规则的正则，调用方据此回写冷却表。
    :ivar result: ``injected`` 已注入；``cooldown`` 因冷却跳过；``outvoted`` 多条
        命中时落选。
    """

    rule: int
    pattern: str
    result: RuleResult


@dataclass(frozen=True)
class RuleMatch:
    """本回合的匹配结果。

    :ivar reaction: 要注入的替换后反应提示；没有规则被注入时为 ``None``。
    :ivar outcomes: 每条命中规则的去向，按规则序号排列；没有命中时为空列表。
    """

    reaction: Optional[str]
    outcomes: List[RuleOutcome]


def _cooldown_factor(last_injected_at: Optional[int], now: int) -> float:
    """计算一条规则在当前会话的冷却系数。

    :param last_injected_at: 该规则在该会话上次注入的毫秒时间；从没注入过为 ``None``。
    :param now: 当前毫秒时间戳。
    :return: [0.0, 1.0] 区间的系数：从没注入过或距上次注入达到
        :data:`EXPRESSION_COOLDOWN_MS` 为 1.0，刚注入过为 0.0。
    副作用：无。
    """

    if last_injected_at is None:
        return 1.0
    return max(0.0, min(1.0, (now - last_injected_at) / EXPRESSION_COOLDOWN_MS))


def _fill_reaction(reaction: str, match: re.Match[str]) -> str:
    """把反应提示里的 ``{名字}`` 换成命名组实际捕获到的文字。

    只认 :data:`REACTION_GROUP_REFERENCE` 的写法，其他花括号与〈〉原样保留。
    引用的组名在加载期已校验为该正则的命名组；组没有参与这次匹配时换成空串，
    与 ``re.Match.expand`` 对未参与匹配的组的处理一致。

    :param reaction: 规则的反应提示。
    :param match: 该规则这次的匹配结果。
    :return: 替换后的反应提示。
    副作用：无。
    """

    def capture(reference: re.Match[str]) -> str:
        return match.group(reference.group(1)) or ''

    return REACTION_GROUP_REFERENCE.sub(capture, reaction)


def match_regex_rules(
    rules: Sequence[RegexRuleConfig],
    text: str,
    *,
    stream_external_id: str,
    stream_id: int,
    now: int,
    cooldowns: Dict[Tuple[str, int], int],
    rng: random.Random,
) -> RuleMatch:
    """用她要回的那句话逐条匹配正则规则，决定这回合注入哪一条。

    :param rules: 当前配置里的正则规则，顺序即序号。
    :param text: 她这次要回的那条消息正文，可以带平台占位段。
    :param stream_external_id: 当前会话的外部 ID，与规则的 ``streams`` 比对；
        ``streams`` 为空的规则对所有会话生效。
    :param stream_id: 当前会话的数据库 ID，是冷却表键的一部分。
    :param now: 当前毫秒时间戳；调用方注入成功后应以同一个值回写冷却表。
    :param cooldowns: 调用方持有的冷却表，键为 ``(pattern, stream_id)``，值为上次
        注入的毫秒时间；本函数只读不写。
    :param rng: 随机源，由调用方注入以保证可复现。
    :return: 替换后的反应提示（或 ``None``）与每条命中规则的去向。不在生效会话里、
        没有命中的规则不出现在结果里。
    副作用：读取随机源状态；不修改 ``cooldowns``。
    """

    subject = strip_machine_spans(text).strip()[:REGEX_RULE_TEXT_LIMIT]
    hits: List[Tuple[int, RegexRuleConfig, re.Match[str], float]] = []
    for index, rule in enumerate(rules, start=1):
        if rule.streams and stream_external_id not in rule.streams:
            continue
        match = re.search(rule.pattern, subject)
        if match is None:
            continue
        factor = _cooldown_factor(cooldowns.get((rule.pattern, stream_id)), now)
        hits.append((index, rule, match, factor))
    if not hits:
        return RuleMatch(reaction=None, outcomes=[])

    # 先各自按冷却系数过一次概率：只命中一条时，注入概率恰为冷却系数。
    passed = [hit for hit in hits if rng.random() < hit[3]]
    chosen = rng.choices(passed, weights=[hit[3] for hit in passed], k=1)[0] if passed else None
    outcomes = [
        RuleOutcome(
            rule=index,
            pattern=rule.pattern,
            result=(
                'injected' if chosen is not None and index == chosen[0]
                else 'outvoted' if any(index == hit[0] for hit in passed)
                else 'cooldown'
            ),
        )
        for index, rule, _match, _factor in hits
    ]
    if chosen is None:
        return RuleMatch(reaction=None, outcomes=outcomes)
    _index, rule, match, _factor = chosen
    return RuleMatch(reaction=_fill_reaction(rule.reaction, match), outcomes=outcomes)
