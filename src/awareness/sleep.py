"""
睡眠概率评估与状态机。

双 sigmoid 乘积模型：就寝端上升 × 起床端下降，无距离折返。
滞回门槛：入睡需 ≥ cutoff，醒来需 < wakeHysteresis < cutoff，避免边界抖动。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Optional

from src.common.clock import now as current_time
from src.schedule.plan import planned_sleep_window_from_hints

WAKE_GRACE_MS = 10 * 60_000
WAKE_TRANSITION_MS = 40 * 60_000
_RECENT_CHAT_MS = 25 * 60_000
_DROWSY_PROBABILITY = 0.3
_WAKE_HYSTERESIS = 0.22
_SLEEP_RUNTIME_KEY = 'sleep_runtime'
_MINUTE_MS = 60_000
_LATE_SLEEP_COMPENSATION = 0.5
_SLEEP_SIGMOID_MINUTES = 42


@dataclass
class SleepState:
    """描述当前睡眠状态机对外暴露的状态。

    :ivar asleep: 是否进入睡眠。
    :ivar drowsy: 尚未睡眠但概率达到困倦阈值。
    :ivar just_woke: 是否处于刚醒过渡期。
    :ivar probability: 当前睡眠概率，范围通常为 0 到 1。
    """

    asleep: bool
    drowsy: bool
    just_woke: bool
    probability: float


@dataclass
class SleepInputs:
    """提供睡眠概率评估所需的配置和上下文。

    :ivar date: 当前本地日期键。
    :ivar bedtime_hint: 配置中的就寝时间提示。
    :ivar wake_hint: 配置中的起床时间提示。
    :ivar energy: 当前精力轴数值，通常范围为 0 到 100。
    :ivar last_interaction_at: 最近互动的 Unix 毫秒时间戳，可为 `None`。
    :ivar sleep_enabled: 是否启用睡眠逻辑。
    :ivar bedtime_day_boundary: 跨日作息的日期边界规则。
    """

    date: str
    bedtime_hint: str
    wake_hint: str
    energy: float
    last_interaction_at: int | None
    sleep_enabled: bool
    bedtime_day_boundary: str


@dataclass
class SleepEvaluation(SleepState):
    """在基础睡眠状态外包含调试和观测用的计算中间值。

    :ivar cutoff: 本日期经过稳定抖动后的入睡阈值，默认值为 0.0。
    :ivar minutes_from_bedtime: 当前时间相对计划就寝时间的分钟数。
    :ivar effective_wake_at: 应用迟睡补偿和滞回后的有效醒来时间戳。
    :ivar natural_wake_target_at: 未叠加醒来概率滞回的自然醒目标时间戳。
    :ivar sleep_debt_delay_minutes: 因睡眠债延后的分钟数，默认值为 0.0。
    """

    cutoff: float = 0.0
    minutes_from_bedtime: float = 0.0
    effective_wake_at: int = 0
    natural_wake_target_at: int = 0
    sleep_debt_delay_minutes: float = 0.0


def _sigmoid(x: float) -> float:
    """计算标准 logistic sigmoid 值。

    :param x: 任意实数输入。
    :return: `1 / (1 + exp(-x))`，范围为 0 到 1。
    :raises OverflowError: 极端负输入导致指数运算溢出时由 `math.exp` 抛出。
    :side_effects: 不修改外部状态。
    """
    return 1.0 / (1.0 + math.exp(-x))


def _logit(p: float) -> float:
    """计算概率值的 logit 变换。

    :param p: 严格位于 0 和 1 之间的概率。
    :return: `log(p / (1-p))`。
    :raises ValueError: `p` 小于等于 0 或大于等于 1。
    :side_effects: 不修改状态。
    """
    if p <= 0 or p >= 1:
        raise ValueError(f'睡眠概率门槛必须位于 0 到 1 之间：{p}')
    return math.log(p / (1 - p))


def sleep_jitter(date: str) -> float:
    """根据日期文本计算稳定的无随机源抖动值。

    Args:
        date: 日期键字符串；相同输入必须得到相同结果。

    Returns:
        ``[0.0, 1.0)`` 范围内的浮点值，用于在不同自然日引入确定性微调。

    Raises:
        TypeError: ``date`` 不是可迭代字符串时抛出。

    Side Effects:
        不访问随机源、不写入状态；运行时间与日期字符串长度线性相关。
    """
    h = 2166136261
    for ch in date:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return (h % 10_000) / 10_000


def sleep_jitter_minutes(date: str) -> float:
    """把稳定日期抖动转换为 -12 到 12 分钟的偏移量。

    :param date: 用于生成稳定哈希的日期字符串。
    :return: 当日固定的分钟偏移量。
    :side_effects: 不访问随机源，输入相同则结果相同。
    """
    return sleep_jitter(date) * 24 - 12


def evaluate_sleep(
    inputs: SleepInputs,
    now: int,
    previously_asleep: bool,
    jitter: float | None = None,
    wake_hysteresis: float | None = None,
    sleep_started_at: int | None = None,
) -> SleepEvaluation:
    """根据双 sigmoid 模型评估当前时间是否应睡眠。

    :param inputs: 作息配置、精力和最近互动输入。
    :param now: 当前 Unix 毫秒时间戳。
    :param previously_asleep: 上一次是否已睡眠，用于应用醒来滞回。
    :param jitter: 可选的 0 到 1 稳定抖动值；省略时按日期计算。
    :param wake_hysteresis: 可选醒来概率阈值；省略时使用模块默认值。
    :param sleep_started_at: 本次睡眠开始时间戳，用于计算睡眠债，可为 `None`。
    :return: 包含状态、概率、阈值和有效作息时间的评估结果。
    :raises ValueError: 时间提示无法被日程服务解析，或醒来滞回不在 0 到 1 之间。
    :side_effects: 不写入状态；仅调用日程解析函数。
    :performance: 计算为常数时间，不依赖历史消息长度。
    """
    if not inputs.sleep_enabled:
        return SleepEvaluation(
            asleep=False,
            drowsy=False,
            just_woke=False,
            probability=0.0,
        )

    # 复用日程服务对跨日作息的解释，避免睡眠状态机另写一套日期算法。
    bedtime_at, wake_at = planned_sleep_window_from_hints(
        inputs.date,
        inputs.bedtime_hint,
        inputs.wake_hint,
        inputs.bedtime_day_boundary,
    )

    stable_jitter = jitter if jitter is not None else sleep_jitter(inputs.date)
    cutoff = 0.72 + (stable_jitter - 0.5) * 0.12
    effective_wake_hysteresis = wake_hysteresis if wake_hysteresis is not None else _WAKE_HYSTERESIS

    energy_shift = (50 - inputs.energy) * 1.8
    since_interaction = float('inf') if inputs.last_interaction_at is None else (now - inputs.last_interaction_at)
    if since_interaction <= 0:
        chat_delay = -75.0
    elif since_interaction < _RECENT_CHAT_MS:
        chat_delay = -75 * (1 - since_interaction / _RECENT_CHAT_MS)
    else:
        chat_delay = 0.0

    jitter_val = (stable_jitter - 0.5) * 24
    bedtime_threshold_offset = _logit(cutoff) * _SLEEP_SIGMOID_MINUTES
    effective_bedtime_at = bedtime_at - int((energy_shift + chat_delay + jitter_val + bedtime_threshold_offset) * _MINUTE_MS)

    planned_sleep_minutes = (wake_at - bedtime_at) / _MINUTE_MS
    if sleep_started_at is None:
        projected_sleep_minutes = planned_sleep_minutes
    else:
        projected_sleep_minutes = max(0.0, (wake_at - sleep_started_at) / _MINUTE_MS)
    missing_sleep_minutes = max(0.0, planned_sleep_minutes - projected_sleep_minutes)
    sleep_debt_delay_minutes = missing_sleep_minutes * _LATE_SLEEP_COMPENSATION

    natural_wake_target_at = wake_at + int((sleep_debt_delay_minutes + jitter_val) * _MINUTE_MS)
    wake_threshold_offset = _logit(effective_wake_hysteresis) * _SLEEP_SIGMOID_MINUTES
    effective_wake_at = natural_wake_target_at + int(wake_threshold_offset * _MINUTE_MS)

    minutes_from_bedtime = (now - bedtime_at) / _MINUTE_MS
    bedtime_rise = _sigmoid((now - effective_bedtime_at) / _MINUTE_MS / _SLEEP_SIGMOID_MINUTES)
    wake_fall = _sigmoid((effective_wake_at - now) / _MINUTE_MS / _SLEEP_SIGMOID_MINUTES)
    probability = bedtime_rise * wake_fall

    asleep = (probability > effective_wake_hysteresis) if previously_asleep else (probability >= cutoff)

    return SleepEvaluation(
        asleep=asleep,
        drowsy=not asleep and probability >= _DROWSY_PROBABILITY,
        just_woke=False,
        probability=probability,
        cutoff=cutoff,
        minutes_from_bedtime=minutes_from_bedtime,
        effective_wake_at=effective_wake_at,
        natural_wake_target_at=natural_wake_target_at,
        sleep_debt_delay_minutes=sleep_debt_delay_minutes,
    )


class SleepStateController:
    """管理可持久化的睡眠状态和人工唤醒宽限期。

    控制器按需从 `input_source` 读取当前输入，在第一次访问时从 `state_store` 恢复
    状态；`forced_asleep` 存在时优先覆盖概率模型。
    """

    def __init__(
        self,
        input_source: Callable[[int], SleepInputs],
        wake_grace_ms: int = WAKE_GRACE_MS,
        state_store: Any = None,
        forced_asleep: Callable[[], bool | None] | None = None,
    ) -> None:
        """创建睡眠状态控制器。

        :param input_source: 根据 Unix 毫秒时间戳返回 `SleepInputs` 的回调。
        :param wake_grace_ms: 手动唤醒后保持清醒的宽限时长，默认值为 `WAKE_GRACE_MS`。
        :param state_store: 可选的 JSON 状态存储，需提供 `read_json` 与 `write_json`。
        :param forced_asleep: 可选强制睡眠回调；返回 `None` 时使用概率模型。
        :side_effects: 初始化内存状态，不立即调用输入源或状态存储。
        """
        self._input_source = input_source
        self._wake_grace_ms = wake_grace_ms
        self._state_store = state_store
        self._forced_asleep = forced_asleep
        self._woken_until = 0
        self._sleeping = False
        self._sleep_started_at: int | None = None
        self._woke_at: int | None = None
        self._restored = False

    def current(self, now: int | None = None) -> SleepState:
        """计算并返回当前对外睡眠状态，同时持久化状态转移。

        :param now: 可选 Unix 毫秒时间戳；省略时读取当前时钟。
        :return: 当前睡眠、困倦和刚醒标志及概率。
        :raises Exception: 输入源、强制状态回调或状态恢复失败时传播原始异常。
        :side_effects: 首次调用可能读取状态存储，状态发生变化时写回存储。
        """
        now = now if now is not None else current_time()
        self._restore()
        forced = self._forced_asleep() if self._forced_asleep else None
        if forced is not None:
            self._sleeping = forced
            return SleepState(asleep=forced, drowsy=False, just_woke=False, probability=1.0 if forced else 0.0)
        inputs = self._input_source(now)
        if not inputs.sleep_enabled:
            self._disable_sleep()
            return SleepState(asleep=False, drowsy=False, just_woke=False, probability=0.0)
        evaluated = evaluate_sleep(inputs, now, self._sleeping, sleep_started_at=self._sleep_started_at)
        if now < self._woken_until:
            self._change_state(False, now)
            just_woke = self._is_just_woke(now, evaluated.natural_wake_target_at)
            return SleepState(asleep=False, drowsy=False, just_woke=just_woke, probability=evaluated.probability)
        self._change_state(evaluated.asleep, now)
        just_woke = not evaluated.asleep and self._is_just_woke(now, evaluated.natural_wake_target_at)
        return SleepState(asleep=evaluated.asleep, drowsy=evaluated.drowsy,
                          just_woke=just_woke, probability=evaluated.probability)

    def inspect(self, now: int | None = None) -> SleepEvaluation:
        """计算完整睡眠评估，但不改变睡眠状态机转移。

        :param now: 可选 Unix 毫秒时间戳；省略时读取当前时钟。
        :return: 含阈值、作息时间和睡眠债信息的完整评估。
        :raises Exception: 输入源、强制状态回调或恢复存储失败时传播原始异常。
        :side_effects: 只进行必要的状态恢复，不因评估结果调用 `_change_state`。
        """
        now = now if now is not None else current_time()
        self._restore()
        inputs = self._input_source(now)
        forced = self._forced_asleep() if self._forced_asleep else None
        if forced is not None:
            return SleepEvaluation(asleep=forced, drowsy=False, just_woke=False,
                                   probability=1.0 if forced else 0.0)
        if not inputs.sleep_enabled:
            return SleepEvaluation(
                asleep=False,
                drowsy=False,
                just_woke=False,
                probability=0.0,
            )
        evaluated = evaluate_sleep(inputs, now, self._sleeping, sleep_started_at=self._sleep_started_at)
        return SleepEvaluation(
            **{**evaluated.__dict__, 'just_woke': not evaluated.asleep and self._is_just_woke(now, evaluated.natural_wake_target_at)}
        )

    def wake(self, now: int | None = None) -> SleepState:
        """手动唤醒 Bot 并在宽限期内抑制概率模型重新入睡。

        :param now: 可选 Unix 毫秒时间戳；省略时读取当前时钟。
        :return: 应用唤醒宽限后的当前睡眠状态。
        :side_effects: 设置宽限截止时间，清除睡眠状态，并可能写入状态存储。
        """
        now = now if now is not None else current_time()
        self._woken_until = now + self._wake_grace_ms
        self._change_state(False, now)
        return self.current(now)

    def _change_state(self, asleep: bool, now: int) -> None:
        """在睡眠/清醒状态实际变化时更新时间标记并持久化。

        :param asleep: 新的睡眠状态。
        :param now: 状态变化时间的 Unix 毫秒时间戳。
        :return: 无返回值；状态未变化时直接返回。
        :side_effects: 修改睡眠开始/醒来时间，并在变化后写入状态存储。
        """
        if asleep == self._sleeping:
            return
        if asleep and not self._sleeping:
            self._sleep_started_at = now
        else:
            self._woke_at = now
            self._sleep_started_at = None
        self._sleeping = asleep
        self._persist()

    def _disable_sleep(self) -> None:
        """清除睡眠状态机的运行时状态。

        :return: 无返回值。
        :side_effects: 清除睡眠标志、睡眠开始时间和醒来时间，并在需要时持久化。
        """
        if not self._sleeping and self._sleep_started_at is None:
            return
        self._sleeping = False
        self._sleep_started_at = None
        self._woke_at = None
        self._persist()

    def _is_just_woke(self, now: int, natural_wake_target_at: int) -> bool:
        """判断当前时间是否仍处于醒来过渡窗口。

        :param now: 当前 Unix 毫秒时间戳。
        :param natural_wake_target_at: 自然醒目标时间戳；当前实现保留该参数以保持
            评估接口语义，窗口判断使用实际 `_woke_at`。
        :return: 最近一次清醒转移发生在 `WAKE_TRANSITION_MS` 内时返回 `True`。
        :side_effects: 不修改状态。
        """
        if self._woke_at is None:
            return False
        return (now - self._woke_at) < WAKE_TRANSITION_MS

    def _persist(self) -> None:
        """把睡眠运行时最小状态写入可选状态存储。

        :return: 无返回值；未配置存储时不执行任何操作。
        :side_effects: 以固定键写入当前睡眠标志和开始时间。
        :raises Exception: 状态存储写入失败时传播原始异常。
        """
        if self._state_store:
            self._state_store.write_json(_SLEEP_RUNTIME_KEY, {
                'sleeping': self._sleeping,
                'sleepStartedAt': self._sleep_started_at,
            })

    def _restore(self) -> None:
        """从状态存储恢复一次睡眠状态并校验字段关系。

        :return: 无返回值；重复调用或未配置存储时直接返回。
        :side_effects: 最多读取一次状态存储，必要时清理不一致状态并写回。
        :raises Exception: 状态存储读取或修复写入失败时传播原始异常。
        """
        if self._restored or not self._state_store:
            return
        self._restored = True
        saved = self._state_store.read_json(_SLEEP_RUNTIME_KEY, None)
        if saved and isinstance(saved, dict):
            saved_sleeping = bool(saved.get('sleeping', False))
            saved_started_at = saved.get('sleepStartedAt')
            started_at_is_valid = (
                isinstance(saved_started_at, int)
                and not isinstance(saved_started_at, bool)
            )
            if saved_sleeping and started_at_is_valid:
                self._sleeping = True
                self._sleep_started_at = saved_started_at
                return
            self._sleeping = False
            self._sleep_started_at = None
            # 睡眠中必须有开始时间，清醒时开始时间必须为空。
            if saved_sleeping or saved_started_at is not None:
                self._persist()
