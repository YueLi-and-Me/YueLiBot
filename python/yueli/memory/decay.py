"""
记忆强度的衰减与复活。直接移植自 src/core/memory/decay.ts。

核心是指数衰减 + 双阈值滞回：
  retention = strength × 2^(-已过小时 / 半衰期)
  低于 FREEZE 转入非活跃（但绝不删除）
  必须回升过 REVIVE 才重新激活

预计算 due_at：避免周期性全表扫描，只在时钟走过 due_at 时才重新评估。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

FREEZE = 0.1
REVIVE = 0.15

HALF_LIFE_HOURS: dict[str, float] = {
    '身份': 24 * 365,   # 名字、职业、家庭 —— 基本不该忘
    '日期': 24 * 365,   # 生日、纪念日
    '偏好': 24 * 90,    # 喜好厌恶，变化很慢
    '习惯': 24 * 60,
    '关系': 24 * 90,
    '事件': 24 * 21,    # 发生过的具体事，会淡
    '状态': 12,         # 临时状态，本来就该很快过期
}

DEFAULT_HALF_LIFE = 24 * 30
MS_PER_HOUR = 3_600_000


def half_life_for(kind: str | None) -> float:
    return HALF_LIFE_HOURS.get(kind or '', DEFAULT_HALF_LIFE) if kind else DEFAULT_HALF_LIFE


def clamp_unit(v: float) -> float:
    return max(0.0, min(1.0, v))


def retention(strength: float, updated_at: int, half_life_hours: float, now: int) -> float:
    """当前留存度。strength 是上次变动时的值，从 updated_at 起算衰减。"""
    hours = max(0.0, (now - updated_at) / MS_PER_HOUR)
    return clamp_unit(strength) * (2 ** (-hours / half_life_hours))


def freeze_due_at(strength: float, updated_at: int, half_life_hours: float) -> int:
    """预计算留存度跌到 FREEZE 的时刻。"""
    s = clamp_unit(strength)
    if s <= FREEZE:
        return updated_at
    t = half_life_hours * math.log2(s / FREEZE) * MS_PER_HOUR
    return updated_at + int(t)


def reinforce(current: float, boost: float = 0.35) -> float:
    """被检索命中后回补强度。回补量随现有留存度递减。"""
    return clamp_unit(current + boost * (1 - current))


def relevance_from_bm25(bm25: float) -> float:
    """把 FTS5 的 bm25() 归一成 [0,1) 的词面相关度，越相关越大。"""
    r = max(0.0, -bm25)
    return r / (1.0 + r)


def retention_weight(retention_value: float) -> float:
    """留存度权重。抽出来是为了让向量融合也能吃到遗忘曲线，而不是只有 BM25 那一半。"""
    return 0.35 + 0.65 * retention_value


def score(bm25: float, retention_value: float) -> float:
    """
    检索排序分数：词面相关度 × 留存度权重。

    SQLite FTS5 的 bm25() 越相关值越小（负得越多），所以先取 -bm25 翻正，
    再用 r/(1+r) 归一到 [0,1)——这条曲线对匹配质量单调**递增**。

    ★ 这里曾经写成 1/(1+r)，方向正好是反的：越相关的事实分越低，配合
      recall_facts() 里的 sort(reverse=True)，实际召回的是候选池里匹配
      最差的那几条。同一处符号混淆还有第二个表现：bm25 为正时（词太常见
      导致 IDF<0）会被 max(0.0, ...) 钳成 0，旧式子给出 1.0 满分，即最烂
      的匹配拿最高分。现在这两种情况都归到 0 分。
      排序方向由 tests/test_recall_golden.py 用可观察的召回结果钉住。
    """
    return relevance_from_bm25(bm25) * retention_weight(retention_value)


@dataclass
class DecayState:
    strength: float
    updated_at: int
    half_life_hours: float
    active: bool


@dataclass
class DecayEval:
    retention: float
    active: bool
    due_at: int


def evaluate(state: DecayState, now: int) -> DecayEval:
    """按当前时刻评估一条记忆：留存度、是否活跃、下次该重新评估的时刻。"""
    r = retention(state.strength, state.updated_at, state.half_life_hours, now)
    # 滞回：活跃态跌破 FREEZE 才冻结；非活跃态必须升过 REVIVE 才解冻
    active = (r > FREEZE) if state.active else (r >= REVIVE)
    return DecayEval(
        retention=r,
        active=active,
        due_at=freeze_due_at(state.strength, state.updated_at, state.half_life_hours),
    )
