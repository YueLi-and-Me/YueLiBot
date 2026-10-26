"""按近期聊天负载计算回合精力消耗；持久化与事务由 Persona 负责。

负载按回合权重累积并连续衰减，所有会话共享，避免多群分别计数放大总消耗。
它仅调整固定开口成本，不改变好感、模型事件或日程活动的结算。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import exp, isfinite
from typing import Tuple


# 无近期负载时的基础消耗（点/回合），实际扣费还要乘会话权重与动态系数。
TURN_ENERGY_COST = 0.3
# 负载的衰减时间常数（毫秒）；停聊 24 小时后保留约 37%，午夜不重置。
TURN_LOAD_TAU_MS = 24 * 3_600_000
# 折算负载达到 80 时，动态系数为 1/2。均匀高频聊天的长期消耗渐近 24 点/天，
# 这不是每日硬上限：突然增加聊天量时，负载尚未适应，仍会额外消耗精力。
TURN_LOAD_REFERENCE = 80.0
TURN_ENERGY_LOAD_KEY = 'persona_turn_energy_load'


@dataclass(frozen=True)
class TurnEnergyLoad:
    """一次结算后的全局折算回合负载，以及对应的毫秒时间戳。"""

    effective_turns: float
    updated_at: int

    def __post_init__(self) -> None:
        if not isfinite(self.effective_turns) or self.effective_turns < 0:
            raise ValueError('近期聊天负载必须是非负有限数')

    def charge(self, now: int, weight: float) -> Tuple[float, TurnEnergyLoad]:
        """先衰减已有负载，再计算本轮扣费并累加本轮权重。

        :param now: 本轮结算的毫秒时间戳，不得早于上次回合结算。
        :param weight: 会话权重，范围 0~1；零权重不会增加负载。
        :return: 本轮精力成本与新的负载状态；不读取时钟或数据库。
        :raises ValueError: 时间倒退或权重越界；无效输入不能被当作空负载。
        """
        if now < self.updated_at:
            raise ValueError('回合结算时间不能早于上次聊天负载的更新时间')
        if not isfinite(weight) or not 0.0 <= weight <= 1.0:
            raise ValueError('回合权重必须是 0 到 1 之间的有限数')
        load = self.effective_turns * exp(-(now - self.updated_at) / TURN_LOAD_TAU_MS)
        coefficient = TURN_LOAD_REFERENCE / (TURN_LOAD_REFERENCE + load)
        cost = TURN_ENERGY_COST * weight * coefficient
        return cost, TurnEnergyLoad(load + weight, now)
