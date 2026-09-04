"""实现事实记忆的指数衰减、冻结、复活和召回排序权重。

核心是指数衰减 + 双阈值滞回：
  retention = strength × 2^(-已过小时 / 半衰期)
  低于 FREEZE 转入非活跃（但绝不删除）
  必须回升过 REVIVE 才重新激活

预计算 due_at：避免周期性全表扫描，只在时钟走过 due_at 时才重新评估。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Optional

import math

FREEZE = 0.1
REVIVE = 0.15

HALF_LIFE_HOURS: Dict[str, float] = {
    '身份': 24 * 365,   # 名字、职业、家庭等长期身份信息
    '日期': 24 * 365,   # 生日、纪念日
    '偏好': 24 * 90,    # 喜好厌恶，变化很慢
    '习惯': 24 * 60,
    '关系': 24 * 90,
    '事件': 24 * 21,    # 发生过的具体事，会淡
    '状态': 12,         # 临时状态，本来就该很快过期
}

FACT_KINDS: FrozenSet[str] = frozenset(HALF_LIFE_HOURS)
DEFAULT_FACT_KIND = '事件'
DEFAULT_HALF_LIFE = HALF_LIFE_HOURS[DEFAULT_FACT_KIND]
MS_PER_HOUR = 3_600_000

# 「永久保留」是一种编码而不是状态列：把半衰期推到 100 年量级后，衰减在人的
# 时间尺度上不再可见，事实因此永远活跃，facts 表无需为此新增列。
PIN_HALF_LIFE_HOURS = 876_000.0


def is_pinned(half_life_hours: float) -> bool:
    """判断给定半衰期是否属于「永久保留」编码。

    :param half_life_hours: 事实行当前携带的半衰期小时数。
    :return: 半衰期达到 :data:`PIN_HALF_LIFE_HOURS` 时返回 ``True``。
    副作用：不修改输入值或衰减配置。
    """
    return half_life_hours >= PIN_HALF_LIFE_HOURS


def half_life_for(kind: Optional[str]) -> float:
    """返回指定事实类型的半衰期小时数。

    :param kind: 事实类型；未提供或不在枚举中时使用默认类别 ``事件`` 的半衰期。
    :return: 半衰期，单位为小时。
    副作用：不修改衰减配置。
    """
    return HALF_LIFE_HOURS.get(kind or DEFAULT_FACT_KIND, DEFAULT_HALF_LIFE)


def clamp_unit(v: float) -> float:
    """把数值限制在闭区间 `[0, 1]`。

    :param v: 任意浮点值。
    :return: 小于 0 时返回 0，大于 1 时返回 1，否则返回原值。
    副作用：不修改输入对象。
    """
    return max(0.0, min(1.0, v))


def retention(strength: float, updated_at: int, half_life_hours: float, now: int) -> float:
    """计算事实从上次更新时刻起的当前留存度。

    :param strength: 上次强化后的留存强度，按 0~1 截断。
    :param updated_at: 上次更新的 Unix 毫秒时间戳。
    :param half_life_hours: 半衰期，单位为小时，必须为正数。
    :param now: 当前 Unix 毫秒时间戳。
    :return: 当前留存度，范围为 0~1。
    副作用：不修改输入值或衰减配置。
    """
    hours = max(0.0, (now - updated_at) / MS_PER_HOUR)
    return clamp_unit(strength) * (2 ** (-hours / half_life_hours))


def freeze_due_at(strength: float, updated_at: int, half_life_hours: float) -> int:
    """预计算留存度首次降至冻结阈值的 Unix 毫秒时间戳。

    :param strength: 更新时的留存强度，按 ``0`` 到 ``1`` 截断。
    :param updated_at: 强度更新时间的 Unix 毫秒时间戳。
    :param half_life_hours: 半衰期，单位为小时，必须为正数。

    :return: 留存度达到 ``FREEZE`` 的预计时间；初始强度已不高于阈值时返回 ``updated_at``。

    :raises ValueError: 半衰期为零或负数，导致衰减时间无法计算。
    :raises TypeError: 参数不是支持数值运算的类型。
    """
    s = clamp_unit(strength)
    if s <= FREEZE:
        return updated_at
    t = half_life_hours * math.log2(s / FREEZE) * MS_PER_HOUR
    return updated_at + int(t)


def reinforce(current: float, boost: float = 0.35) -> float:
    """在事实被检索命中后按递减增量回补其留存强度。

    :param current: 当前留存强度，计算前按 ``0`` 到 ``1`` 截断。
    :param boost: 最大回补系数，默认 ``0.35``；回补量为 ``boost * (1-current)``。

    :return: 叠加回补量并限制在 ``[0, 1]`` 内的新强度。

    :raises TypeError: 参数不支持数值运算时抛出。
    """
    return clamp_unit(current + boost * (1 - current))


def relevance_from_bm25(bm25: float) -> float:
    """将 FTS5 ``bm25`` 分数转换为单调递增的词面相关度。

    :param bm25: FTS5 返回的 BM25 分数；该实现按负值表示相关度。

    :return: ``[0, 1)`` 范围内的相关度；BM25 为正时按零相关度处理。

    :raises TypeError: 参数不支持比较和算术运算时抛出。
    """
    r = max(0.0, -bm25)
    return r / (1.0 + r)


def retention_weight(retention_value: float) -> float:
    """将事实留存度映射为检索排序权重。

    :param retention_value: 当前留存度，通常范围为 ``0`` 到 ``1``。

    :return: 线性映射后的排序权重，留存度为 ``0`` 时为 ``0.35``，为 ``1`` 时为 ``1.0``。

    :raises TypeError: 参数不支持乘法和加法时抛出。
    """
    return 0.35 + 0.65 * retention_value


def score(bm25: float, retention_value: float) -> float:
    """计算事实检索排序分数：词面相关度乘以留存度权重。

    SQLite FTS5 的 BM25 值越小表示词面越相关；函数先取负值并归一化到 ``[0, 1)``，
    再乘以留存度权重，确保最终分数随词面相关度和留存度单调增加。

    :param bm25: FTS5 返回的 BM25 分数。
    :param retention_value: 当前事实留存度，通常范围为 ``0`` 到 ``1``。

    :return: 词面相关度与留存度权重的乘积。

    :raises TypeError: 参数不支持数值运算时抛出。
    """
    return relevance_from_bm25(bm25) * retention_weight(retention_value)


@dataclass
class DecayState:
    """保存一条事实在衰减曲线上的当前状态。

    :ivar strength: 上次更新时的强度，通常范围为 0 到 1。
    :ivar updated_at: 强度更新时间的 Unix 毫秒时间戳。
    :ivar half_life_hours: 事实类型对应的半衰期小时数。
    :ivar active: 是否仍参与召回。
    """

    strength: float
    updated_at: int
    half_life_hours: float
    active: bool


@dataclass
class DecayEval:
    """保存一次衰减评估的结果。

    :ivar retention: 当前留存度。
    :ivar active: 应用滞回规则后的活跃状态。
    :ivar due_at: 下一次需要重新评估的 Unix 毫秒时间戳。
    """

    retention: float
    active: bool
    due_at: int


def evaluate(state: DecayState, now: int) -> DecayEval:
    """按当前时刻计算事实留存度、活跃状态和下一次评估时间。

    :param state: 包含强度、更新时间、半衰期和当前活跃状态的衰减状态。
    :param now: 当前 Unix 毫秒时间戳。

    :return: ``DecayEval``，其中活跃状态使用冻结/复活双阈值滞回规则计算。

    :raises ValueError: 半衰期不为正数时由衰减计算抛出。
    :raises TypeError: 状态字段或时间戳不支持数值运算时抛出。
    """
    r = retention(state.strength, state.updated_at, state.half_life_hours, now)
    # 滞回：活跃态跌破 FREEZE 才冻结；非活跃态必须升过 REVIVE 才解冻
    active = (r > FREEZE) if state.active else (r >= REVIVE)
    return DecayEval(
        retention=r,
        active=active,
        due_at=freeze_due_at(state.strength, state.updated_at, state.half_life_hours),
    )
