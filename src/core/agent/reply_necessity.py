"""Conversation Agent 扩展触发模式的确定性评分与频率预算。

默认 ``signal`` 模式只把点名、@ 和自然回应窗口送入 DELIBERATE。为了让
用户在 bot 群等场景观察 Agent，本模块提供两种可选的确定性触发口径：

- ``frequency``：按 ``talk_value`` 把低频发言预算折算成候选消息阈值，
  攒够消息才允许一次无信号 DELIBERATE；
- ``reply_necessity``：内容驱动地为当前批次计算 0~100 的回复必要性评分，
  达到阈值才允许无信号 DELIBERATE；积压消息只作为辅助压力项。

两种模式都只决定「是否值得进入意识」，不决定「回不回」；进入
DELIBERATE 后仍由 Conversation Agent 自主选择 reply / silent / react。

``reply_necessity`` 只被群聊 ``attention_filtered`` 路径调用，因此本模块
不再保留 @、点名、私聊相关性等在该调用点永远为 False 的分支；评分由
内容信号、短反应惩罚与积压压力组成。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, log1p
from typing import Sequence

import re

# 频率阈值默认取倒数，例如 talk_value=0.6 时每 2 条消息唤醒一次。
DEFAULT_FREQUENCY_TALK_VALUE = 0.6
# 回复必要性默认触发线；内容驱动下一条「问题 + 请求 + 较长文本」正好达线。
DEFAULT_REPLY_NECESSITY_THRESHOLD = 80

QUESTION_TERMS = ("怎么", "如何", "为什么", "有没有")
STRONG_REQUEST_TERMS = ("帮我", "帮忙", "能不能", "可以吗", "要不要")
SHORT_REACTIONS = frozenset({"哈哈", "哈哈哈", "草", "笑死", "好", "嗯", "啊", "哦", "6", "666", "？", "?"})

# 内容信号权重按默认阈值 80 标定：问题 30 + 请求 30 + 两档长度共 20，
# 使一条真正值得回复且写清来意的无点名群消息无需积压即可达线。
QUESTION_SCORE = 30
STRONG_REQUEST_SCORE = 30
LONG_TEXT_SCORE = 10
LONGER_TEXT_SCORE = 10
SHORT_REACTION_PENALTY = 25


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


def _request_reason(text: str) -> str:
    """返回命中的请求类原因。

    群聊无直接上下文时，只有明确请人帮忙的措辞才计为请求；「可以吗 /
    要不要」等更偏商量语气，不在无信号群消息里单独计分。
    """
    hits = [term for term in STRONG_REQUEST_TERMS if term in text]
    if "能不能" in hits and not text.startswith("能不能"):
        hits.remove("能不能")
    for term in ("可以吗", "要不要"):
        if term in hits:
            hits.remove(term)
    return "/".join(hits)


def _short_reaction(texts: Sequence[str]) -> bool:
    """判断批次是否基本由短反应或表情占位组成。"""
    normalized = [" ".join(text.split()).strip() for text in texts if text.strip()]
    if not normalized:
        return True
    if any(len(text) > 8 for text in normalized):
        return False
    return all(text in SHORT_REACTIONS for text in normalized)


def _pressure_score(pending_count: int, backlog_scale: int) -> int:
    """按待处理候选数计算压力分，分母使用消息条数尺度。

    ``backlog_scale`` 不是 0~100 的评分阈值，而是「多少条未处理消息算一份
    完整压力」的条数；调用方使用 frequency 预算折算出的消息阈值，避免评分
    阈值与消息条数量纲耦合。
    """
    normalized_scale = max(1, backlog_scale)
    ratio = max(0.0, pending_count / normalized_scale)
    if ratio <= 1.0:
        return min(50, int(round(50 * ratio * ratio)))
    overflow_factor = min(1.0, log1p(ratio - 1.0) / log1p(4.0))
    return min(100, 50 + int(round(50 * overflow_factor)))


def score_reply_necessity(
    texts: Sequence[str],
    *,
    pending_count: int,
    backlog_scale: int,
) -> ReplyNecessityScore:
    """计算 0~100 的回复必要性评分。

    该函数只服务群聊 ``attention_filtered`` 路径：@、点名或最近 Bot 发言
    已在入口门控直接进入 DELIBERATE，不会到达这里；因此评分只由内容信号、
    短反应惩罚与积压压力组成。

    :param texts: 本批清洗后的候选文本。
    :param pending_count: 尚未触发 Agent 的候选累计条数，用于压力分。
    :param backlog_scale: 压力归一化的消息条数尺度；达到该条数时压力分记 50。
    :return: 包含最终得分与中文评分明细的不可变结果。
    """
    cleaned = [" ".join((text or "").split()).strip() for text in texts]
    combined = "\n".join(text for text in cleaned if text)

    content_score = 0
    content_reasons: list[str] = []
    if any(_is_question(text) for text in cleaned):
        content_score += QUESTION_SCORE
        content_reasons.append("问题")
    request_reason = _request_reason(combined)
    if request_reason:
        content_score += STRONG_REQUEST_SCORE
        content_reasons.append(f"请求:{request_reason}")
    if len(combined) >= 40:
        content_score += LONG_TEXT_SCORE
        content_reasons.append("长文本")
    if len(combined) >= 120:
        content_score += LONGER_TEXT_SCORE
        content_reasons.append("较长文本")
    if _short_reaction(cleaned):
        content_score -= SHORT_REACTION_PENALTY
        content_reasons.append("短反应")

    pressure_score = _pressure_score(pending_count, backlog_scale)
    raw_score = content_score + pressure_score
    final_score = max(0, min(100, raw_score))

    parts = [f"最终={final_score}", f"原始={raw_score}"]
    if content_score:
        parts.append(f"内容={content_score}({','.join(content_reasons)})")
    if pressure_score:
        parts.append(f"压力={pressure_score}")
    return ReplyNecessityScore(score=final_score, detail=" ".join(parts))
