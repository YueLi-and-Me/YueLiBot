"""Conversation Agent 扩展触发模式的确定性评分与频率预算。

默认 ``signal`` 模式只把点名、@ 和自然回应窗口送入 DELIBERATE。为了让
用户在 bot 群等场景观察 Agent，本模块提供两种可选的确定性触发口径：

- ``frequency``：按 ``talk_value`` 把低频发言预算折算成候选消息阈值，
  攒够消息才允许一次无信号 DELIBERATE；
- ``reply_necessity``：为当前批次计算 0~100 的回复必要性评分，达到阈值
  才允许无信号 DELIBERATE。

两种模式都只决定「是否值得进入意识」，不决定「回不回」；进入
DELIBERATE 后仍由 Conversation Agent 自主选择 reply / silent / react。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, log1p
from typing import Sequence

import re

# 频率阈值默认取倒数，例如 talk_value=0.6 时每 2 条消息唤醒一次。
DEFAULT_FREQUENCY_TALK_VALUE = 0.6
# 回复必要性默认触发线；@ / 点名在相关性上直接越过该线。
DEFAULT_REPLY_NECESSITY_THRESHOLD = 80

QUESTION_TERMS = ("怎么", "如何", "为什么", "有没有")
STRONG_REQUEST_TERMS = ("帮我", "帮忙", "能不能", "可以吗", "要不要")
WEAK_REQUEST_TERMS = ("需要", "求", "看看", "试试")
OPINION_TERMS = ("你觉得", "你认为", "怎么看", "有什么建议")
SHORT_REACTIONS = frozenset({"哈哈", "哈哈哈", "草", "笑死", "好", "嗯", "啊", "哦", "6", "666", "？", "?"})


@dataclass(frozen=True)
class ReplyNecessityScore:
    """回复必要性评分结果，保留可审计的评分明细。"""

    score: int
    detail: str


def frequency_trigger_threshold(talk_value: float) -> int:
    """把发言频率预算折算为触发一轮候选所需的消息数。

    :param talk_value: 群聊主动发言频率预算，取值范围 ``(0, 1]``。
    :return: 至少为 1 的消息阈值；``0.6`` 折算为 2。
    :raises ValueError: ``talk_value`` 不在允许范围内。
    """
    if not 0.0 < talk_value <= 1.0:
        raise ValueError('发言频率预算必须大于 0 且不超过 1')
    return max(1, int(ceil(1.0 / talk_value)))


def _is_question(text: str) -> bool:
    """判断当前文本是否像真实问题，而非单纯短符号。"""
    if not text or len(text) < 4:
        return False
    if any(term in text for term in QUESTION_TERMS):
        return True
    if re.search(r"[吗呢](?:[？?。！!~～…]*$)", text) and len(text) <= 80:
        return True
    return bool(re.search(r"[？?](?:$|[。！!~～…])", text) and len(text) <= 120)


def _request_reason(text: str, *, direct_context: bool) -> str:
    """返回命中的请求类原因；弱请求只在直接上下文生效。"""
    hits = [term for term in STRONG_REQUEST_TERMS if term in text]
    if "能不能" in hits and not direct_context and not text.startswith("能不能"):
        hits.remove("能不能")
    if not direct_context:
        for term in ("可以吗", "要不要"):
            if term in hits:
                hits.remove(term)
    if hits:
        return "/".join(hits)
    if direct_context:
        weak_hits = [term for term in WEAK_REQUEST_TERMS if term in text]
        if weak_hits:
            return "/".join(weak_hits)
    return ""


def _opinion_reason(text: str, *, direct_context: bool) -> str:
    """返回命中的意见征询原因；群聊无直接上下文不视为征询。"""
    if not direct_context:
        return ""
    hits = [term for term in OPINION_TERMS if term in text]
    if hits:
        return "/".join(hits)
    if re.search(r"(?:你).{0,6}怎么看|怎么看.{0,6}(?:你)", text):
        return "怎么看"
    return ""


def _short_reaction(texts: Sequence[str]) -> bool:
    """判断批次是否基本由短反应或表情占位组成。"""
    normalized = [" ".join(text.split()).strip() for text in texts if text.strip()]
    if not normalized:
        return True
    if any(len(text) > 8 for text in normalized):
        return False
    return all(text in SHORT_REACTIONS for text in normalized)


def _presence_penalty(recent_self_replies: int, recent_window_messages: int) -> int:
    """按最近窗口内 Bot 发言占比计算存在感惩罚。"""
    if recent_self_replies <= 0 or recent_window_messages <= 0:
        return 0
    ratio = min(1.0, recent_self_replies / recent_window_messages)
    if ratio <= 0.25:
        return 0
    progress = min(1.0, (ratio - 0.25) / (0.60 - 0.25))
    return int(round(25 * progress))


def _pressure_score(pending_count: int, trigger_threshold: int) -> int:
    """按待处理候选数计算压力分；超过阈值后使用对数增长封顶。"""
    normalized_threshold = max(1, trigger_threshold)
    ratio = max(0.0, pending_count / normalized_threshold)
    if ratio <= 1.0:
        return min(50, int(round(50 * ratio * ratio)))
    overflow_factor = min(1.0, log1p(ratio - 1.0) / log1p(4.0))
    return min(100, 50 + int(round(50 * overflow_factor)))


def score_reply_necessity(
    texts: Sequence[str],
    *,
    has_at: bool,
    has_mention: bool,
    is_group_chat: bool,
    recent_self_replies: int,
    recent_window_messages: int,
    pending_count: int,
    trigger_threshold: int,
) -> ReplyNecessityScore:
    """计算 0~100 的回复必要性评分。

    :param texts: 本批清洗后的候选文本。
    :param has_at: 是否包含真实 @。
    :param has_mention: 是否包含配置名称/别名。
    :param is_group_chat: 是否为群聊；私聊/桌面有直接对话相关性。
    :param recent_self_replies: 最近窗口内 Bot 回复数。
    :param recent_window_messages: 最近窗口内总消息数。
    :param pending_count: 尚未触发 Agent 的候选累计值，用于压力分。
    :param trigger_threshold: 触发阈值，同时用于压力归一化。
    :return: 包含最终得分与中文评分明细的不可变结果。
    """
    if has_at:
        relevance_score = 100
        relevance_reason = "@"
    elif has_mention:
        relevance_score = 80
        relevance_reason = "提及"
    elif not is_group_chat:
        relevance_score = 40
        relevance_reason = "私聊"
    else:
        relevance_score = 0
        relevance_reason = "普通"

    direct_context = relevance_score > 0
    cleaned = [" ".join((text or "").split()).strip() for text in texts]
    combined = "\n".join(text for text in cleaned if text)

    content_score = 0
    content_reasons: list[str] = []
    if any(_is_question(text) for text in cleaned):
        content_score += 15
        content_reasons.append("问题")
    request_reason = _request_reason(combined, direct_context=direct_context)
    if request_reason:
        content_score += 20
        content_reasons.append(f"请求:{request_reason}")
    opinion_reason = _opinion_reason(combined, direct_context=direct_context)
    if opinion_reason:
        content_score += 20
        content_reasons.append(f"征询:{opinion_reason}")
    if len(combined) >= 40:
        content_score += 5
        content_reasons.append("长文本")
    if len(combined) >= 120:
        content_score += 10
        content_reasons.append("较长文本")
    if _short_reaction(cleaned):
        content_score -= 25
        content_reasons.append("短反应")

    pressure_score = _pressure_score(pending_count, trigger_threshold)
    presence_penalty = _presence_penalty(recent_self_replies, recent_window_messages)
    raw_score = relevance_score + content_score + pressure_score - presence_penalty
    final_score = max(0, min(100, raw_score))

    parts = [f"最终={final_score}", f"原始={raw_score}"]
    if relevance_score:
        parts.append(f"强相关={relevance_score}({relevance_reason})")
    if content_score:
        parts.append(f"内容={content_score}({','.join(content_reasons)})")
    if pressure_score:
        parts.append(f"压力={pressure_score}")
    if presence_penalty:
        parts.append(f"存在感=-{presence_penalty}")
    return ReplyNecessityScore(score=final_score, detail=" ".join(parts))
