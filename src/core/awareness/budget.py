"""管理主动搭话的每日预算、场景预留和节流决策。

所有默认值偏保守：少说一句只是平淡，多说一句会让人想卸载。

状态按本地日期滚动；高优先级事件可以绕过普通预算，但普通场景会预留固定
数量的事件槽位，避免低价值搭话耗尽整日额度。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

DAILY_BUDGET = 5
SCENE_RESERVED_EVENT_SLOTS = 2


@dataclass
class ProactiveState:
    """保存主动搭话预算在某个自然日的运行状态。

    :ivar day_key: `年-月-日` 格式的本地日期键。
    :ivar used: 当日已消耗的普通主动搭话次数，默认值为 0。
    :ivar last_at: 最近一次主动搭话的 Unix 毫秒时间戳，默认值为 0。
    :ivar ignored: 连续未得到用户回应的次数，默认值为 0。
    """

    day_key: str
    used: int = 0
    last_at: int = 0
    ignored: int = 0


@dataclass
class InterruptContext:
    """描述一次主动搭话决策所需的即时环境。

    :ivar now: 当前 Unix 毫秒时间戳。
    :ivar silent: 是否处于强制静默环境，默认值为 `False`。
    :ivar asleep: Bot 是否处于睡眠状态，默认值为 `False`。
    :ivar visible: 用户是否可见或在场，默认值为 `True`。
    :ivar responded_since_last: 上次主动搭话后用户是否回应，默认值为 `True`。
    :ivar priority: 可选优先级；`high` 可绕过普通预算，默认值为 `None`。
    """

    now: int
    silent: bool = False
    asleep: bool = False
    visible: bool = True
    responded_since_last: bool = True
    priority: Literal['normal', 'high'] | None = None


@dataclass
class Decision:
    """表示主动搭话是否被预算层允许。

    :ivar allow: 允许投放时为 `True`。
    :ivar reason: 拒绝原因标识；允许时为 `None`，默认值为 `None`。
    """

    allow: bool
    reason: str | None = None


def day_key_of(now: int) -> str:
    """把 Unix 毫秒时间戳转换为本地自然日键。

    :param now: Unix 毫秒时间戳。
    :return: `年-月-日` 格式的本地日期字符串。
    :raises (OverflowError, OSError, ValueError): 时间戳超出系统日期转换范围时由
        `datetime.fromtimestamp` 抛出。
    副作用：读取本地时区规则，不修改状态。
    """
    d = datetime.fromtimestamp(now / 1000)
    return f'{d.year}-{d.month}-{d.day}'


def initial_state(now: int) -> ProactiveState:
    """以指定时间创建当日的空主动搭话预算状态。

    :param now: Unix 毫秒时间戳。
    :return: `used=0`、`ignored=0` 且日期为 `now` 所在本地日的状态。
    副作用：不修改外部状态。
    """
    return ProactiveState(day_key=day_key_of(now))


def _rollover(state: ProactiveState, now: int) -> ProactiveState:
    """在跨自然日时清零当日计数并保留跨日连续信息。

    :param state: 旧预算状态。
    :param now: 当前 Unix 毫秒时间戳。
    :return: 日期未变化时返回原实例；跨日时返回新状态。
    副作用：不修改输入状态。
    """
    key = day_key_of(now)
    if key == state.day_key:
        return state
    return ProactiveState(day_key=key, used=0, last_at=state.last_at, ignored=state.ignored)


def decide(state: ProactiveState, ctx: InterruptContext) -> Decision:
    """根据静默、睡眠、可见性和每日预算判断是否允许主动搭话。

    :param state: 当前预算状态。
    :param ctx: 当前环境及优先级。
    :return: 带允许标志和拒绝原因的决策；`high` 优先级可绕过预算。
    副作用：不修改输入状态，跨日计算只创建临时状态。
    """
    if ctx.silent:
        return Decision(allow=False, reason='silent')
    if ctx.asleep:
        return Decision(allow=False, reason='asleep')
    if not ctx.visible:
        return Decision(allow=False, reason='hidden')
    s = _rollover(state, ctx.now)
    if ctx.priority == 'high':
        return Decision(allow=True)
    if s.used >= DAILY_BUDGET:
        return Decision(allow=False, reason='budget')
    return Decision(allow=True)


def decide_scene(state: ProactiveState, ctx: InterruptContext) -> Decision:
    """执行普通场景主动搭话决策，并保留场景预留额度。

    :param state: 当前预算状态。
    :param ctx: 当前环境及优先级。
    :return: 场景可用时的基础决策；触及预留槽位时返回 `scene-reserve` 拒绝原因。
    副作用：不修改输入状态。
    """
    base = decide(state, ctx)
    if not base.allow or ctx.priority == 'high':
        return base
    s = _rollover(state, ctx.now)
    if s.used >= DAILY_BUDGET - SCENE_RESERVED_EVENT_SLOTS:
        return Decision(allow=False, reason='scene-reserve')
    return base


def after_speak(state: ProactiveState, ctx: InterruptContext) -> ProactiveState:
    """记录一次主动搭话后的预算消耗和用户回应情况。

    :param state: 搭话前预算状态。
    :param ctx: 这次搭话使用的时间和优先级信息。
    :return: 更新后的不可变预算状态；高优先级搭话不消耗普通额度。
    副作用：不修改输入状态。
    """
    s = _rollover(state, ctx.now)
    return ProactiveState(
        day_key=s.day_key,
        used=s.used if ctx.priority == 'high' else s.used + 1,
        last_at=ctx.now,
        ignored=0 if ctx.responded_since_last else s.ignored + 1,
    )


def after_user_spoke(state: ProactiveState) -> ProactiveState:
    """在用户主动发言后清除连续未回应计数。

    :param state: 当前预算状态。
    :return: `ignored` 已清零的新状态；原值为 0 时直接返回原实例。
    副作用：不修改输入状态。
    """
    if state.ignored == 0:
        return state
    return ProactiveState(day_key=state.day_key, used=state.used,
                          last_at=state.last_at, ignored=0)
