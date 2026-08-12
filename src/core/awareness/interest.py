"""根据活动、人格状态和互动间隔累计主动消息兴趣值。

兴趣按真实经过时间积分，而不是按轮询次数增加；所有乘数都来自活动、关系、
精力、未回应次数和缺席时间，兴趣数值只用于行为决策，不直接进入模型提示词。

增长速率由五个既有输入相乘，不引入额外状态源：

    rate_per_minute = BASE * f_activity * f_favor * f_energy * f_ignored * f_absence

    f_activity  活动类别对主动消息的影响（空闲较高，编码和会议较低）
    f_favor     persona.intimacy 对主动联系意愿的影响，不改变回复人设
    f_energy    persona.energy 对可用精力的影响，随回合消耗并在睡眠时恢复
    f_ignored   未回应次数的衰减因子，替代固定冷却时间
    f_absence   距离上次聊天的时间因子，在上限内连续增加而不改变固定阈值

因此 persona 状态同时参与“是否主动发送”和“如何生成回复”两类决策。

兴趣值本身不进入提示词；连续数值只用于驱动行为，提供给模型的始终是自然语言。

按真实时间累积（`rate_per_minute * 实际过去的分钟数`），避免轮询间隔改变主动
搭话频率，也避免机器睡眠或错过轮询后产生额外的顺序依赖。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .signals import Activity, InputIntensity

# 兴趣达到 100 即具备主动发送资格；100 是数值单位定义，发送频率由 BASE 控制。
FULL = 100.0

# 各项乘数均为 1 时，BASE=4 表示约 25 分钟达到上限；关系和精力因子通常会降低该速率。
BASE = 4.0

# 每增加一次未回应记录，兴趣增长速率乘以该衰减因子。
IGNORED_DECAY = 0.6

# 想念感在 8 小时内平滑走到上限。它只控制「多久不说话会更想开口」这一件事，
# 与轮询频率、意图 TTL 或视觉等待不存在大小关系。
ABSENCE_SCALE_HOURS = 8.0

# 活动类别对应主动消息增长权重。未列出的类别使用 1.0，新增类别无需修改此表。
_ACTIVITY_WEIGHT: dict[Activity, float] = {
    'idle': 1.6,      # 空闲状态允许更快积累
    'gaming': 1.3,
    'video': 1.2,
    'music': 1.2,
    'browsing': 1.1,
    'files': 1.0,
    'reading': 0.7,
    'work': 0.6,
    'coding': 0.5,
    'chat': 0.4,
}

# 键鼠活动强度修正；away 状态显著降低增长速率。
_INTENSITY_WEIGHT: dict[InputIntensity, float] = {
    'away': 0.05,
    'light': 1.0,
    'busy': 0.7,
}


@dataclass(frozen=True)
class InterestState:
    """兴趣值本体。frozen——所有变化都返回新实例，方便单测和 trace 回放。"""

    value: float = 0.0
    updated_at: int = 0


@dataclass(frozen=True)
class InterestFactors:
    """记录一次兴趣值增长使用的五个乘数，并写入追踪事件。

    兴趣值增长会影响主动行为触发，保留这些乘数才能通过追踪事件复盘触发原因并维护
    计算规则；字段不承担额外的持久化职责。
    """

    activity: float
    favor: float
    energy: float
    ignored: float
    absence: float

    @property
    def rate_per_minute(self) -> float:
        """返回五个兴趣乘数和基础速率的乘积。

        :return: 每分钟兴趣增长量，单位为兴趣值/分钟。
        :side_effects: 不修改任何乘数。
        """
        return BASE * self.activity * self.favor * self.energy * self.ignored * self.absence

    def as_trace(self) -> dict[str, float]:
        """生成用于运行时追踪的四舍五入乘数快照。

        :return: 含五个乘数及每分钟速率的字典，数值保留三位小数。
        :side_effects: 不修改当前因素。
        """
        return {
            'fActivity': round(self.activity, 3),
            'fFavor': round(self.favor, 3),
            'fEnergy': round(self.energy, 3),
            'fIgnored': round(self.ignored, 3),
            'fAbsence': round(self.absence, 3),
            'ratePerMinute': round(self.rate_per_minute, 3),
        }


def factors_for(activity: Activity, intensity: InputIntensity, favor: float, energy: float,
                ignored: int, absence_hours: float) -> InterestFactors:
    """把当前情境和人物状态折算成五个兴趣乘数。

    favor / energy 是 persona 的 0~100 轴，除以 100 后参与计算；两者设置下限，
    防止数值过低使主动消息永远不可达。

    Args:
        activity: 当前前台活动类别。
        intensity: 当前键鼠输入强度。
        favor: 人物好感度，通常范围为 ``0`` 到 ``100``。
        energy: Bot 精力值，通常范围为 ``0`` 到 ``100``。
        ignored: 连续被忽略的主动意图次数，负值按 ``0`` 处理。
        absence_hours: 用户离开时长，单位为小时，负值按 ``0`` 处理。

    Returns:
        包含活动、好感、精力、忽略衰减和离线补偿五个乘数的 ``InterestFactors``。

    Side Effects:
        不修改输入状态和配置；计算结果使用固定上下限和衰减系数。
    """
    return InterestFactors(
        activity=_ACTIVITY_WEIGHT.get(activity, 1.0) * _INTENSITY_WEIGHT.get(intensity, 1.0),
        favor=max(0.25, favor / 100.0),
        energy=max(0.2, energy / 100.0),
        ignored=IGNORED_DECAY ** max(0, ignored),
        absence=1.0 + min(1.5, max(0.0, absence_hours) / ABSENCE_SCALE_HOURS),
    )


def initial_state(now: int) -> InterestState:
    """创建指定时间的零兴趣状态。

    :param now: 当前 Unix 毫秒时间戳。
    :return: `value=0.0` 且 `updated_at=now` 的不可变状态。
    :side_effects: 不修改外部状态。
    """
    return InterestState(value=0.0, updated_at=now)


def grow(state: InterestState, factors: InterestFactors, now: int) -> InterestState:
    """按真实流逝时间累积兴趣。

    Args:
        state: 上一次兴趣状态。
        factors: 当前情境对应的增长乘数。
        now: 当前 Unix 毫秒时间戳。

    Returns:
        使用实际经过分钟数计算的新状态；结果值限制在 ``[0, FULL]``。

    Side Effects:
        不修改输入状态。使用时间差而非调用次数，保证不同轮询间隔得到相同结果。
    """
    if state.updated_at <= 0:
        return replace(state, updated_at=now)
    elapsed_minutes = max(0.0, (now - state.updated_at) / 60_000)
    if elapsed_minutes <= 0:
        return state
    growth = factors.rate_per_minute * elapsed_minutes
    return InterestState(value=min(FULL, state.value + growth), updated_at=now)


def wants_to_speak(state: InterestState) -> bool:
    """判断兴趣值是否达到主动搭话阈值。

    :param state: 当前兴趣状态。
    :return: `value` 大于等于 `FULL` 时返回 `True`。
    :side_effects: 不修改状态。
    """
    return state.value >= FULL


def spend(state: InterestState, now: int) -> InterestState:
    """消耗一次主动发送资格并重置兴趣累计。

    Args:
        state: 当前兴趣状态；仅用于保持调用接口一致，函数不会修改它。
        now: 重置后的 Unix 毫秒时间戳。

    Returns:
        ``value=0.0`` 且 ``updated_at=now`` 的新状态。

    Side Effects:
        不修改输入状态。投放被拦截或意图入队时也必须调用，避免同一兴趣值重复触发。
    """
    return InterestState(value=0.0, updated_at=now)


def minutes_to_full(state: InterestState, factors: InterestFactors) -> float | None:
    """计算达到主动发送阈值所需的预计分钟数。

    Args:
        state: 当前兴趣状态。
        factors: 当前增长乘数。

    Returns:
        预计分钟数；增长速率不为正时返回 ``None``。
    """
    rate = factors.rate_per_minute
    if rate <= 0:
        return None
    return max(0.0, (FULL - state.value) / rate)
