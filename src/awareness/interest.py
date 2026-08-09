"""
主动搭话的冲动层：兴趣值。

原来这一层是 budget.py 里的固定冷却——距上次说话不到 30 分钟就闭嘴。够安全，
但它是整个主动系统里最不像人的部分：一个人不会掐着表说话，她是攒着攒着就想
说了，而且攒得快不快取决于她当时什么状态、他在忙什么。

所以换成兴趣值。参考 LingChat 的做法，但**不抄它的随机数**：它是
`interest += random(5, 10)` 再叠一张活动加权表，结果只是「不可预测」，
不是「像人」——随机数不带任何关于她是谁的信息。

这里的增速由五个**已经存在**的输入相乘，不新造状态源：

    rate_per_minute = BASE * f_activity * f_favor * f_energy * f_ignored * f_absence

    f_activity  他在干什么（闲着涨得快，写代码涨得慢，人不在几乎不涨）
    f_favor     她和他的关系深度 —— persona.intimacy，只影响主动联系意愿，不决定人设
    f_energy    她累不累 —— persona.energy，apply_turn 每轮扣、睡觉回涨
    f_ignored   她被冷落了几次 —— 替代原来 cooldown_for() 的指数退避。
                「她没那么起劲了」比「冷却期变长」像人得多。
    f_absence   距离上次聊天有多久 —— 他消失久了，她会有一点惦记，但不会跳变成
                一个固定概率阈值。

于是 persona 第一次真正驱动**要不要开口**，而不只是开口以后**怎么说**。

★ 兴趣值本身绝不进 prompt。遵循 persona/state.py 立的规矩：连续数值只用来
  驱动行为，喂给模型的永远是自然语言。

★ 按真实时间累积（rate_per_minute * 实际过去的分钟数），不是「每 tick 加一次」。
  否则调 proactive.py 的轮询间隔会连带改掉她的说话频率——那就成了一条藏起来的
  顺序依赖，正是上一轮屏幕感知翻车的形状。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .classify import Activity, InputIntensity

# 兴趣攒满即开口。100 是单位定义，不是可调参数——要调她多话不多话，调 BASE。
FULL = 100.0

# 各项乘数都为 1 时攒满需要多少分钟的倒数刻度：BASE=4 → 25 分钟到顶。
# 实际上 f_favor/f_energy 通常在 0.5 上下，所以典型间隔会明显长于这个值。
BASE = 4.0

# 被忽略一次，增速打的折。连着不理她，她会越来越蔫（而不是越来越急）。
IGNORED_DECAY = 0.6

# 想念感在 8 小时内平滑走到上限。它只控制「多久不说话会更想开口」这一件事，
# 与轮询频率、意图 TTL 或视觉等待不存在大小关系。
ABSENCE_SCALE_HOURS = 8.0

# 他在干什么 → 她想插话的意愿。刻意不写满 11 类：没列到的走 1.0，
# 加新 activity 时不必回来改这张表。
_ACTIVITY_WEIGHT: dict[Activity, float] = {
    'idle': 1.6,      # 闲着，最容易搭话
    'gaming': 1.3,    # 打游戏，她想凑热闹
    'video': 1.2,
    'music': 1.2,
    'browsing': 1.1,
    'files': 1.0,
    'reading': 0.7,   # 在读东西，别太吵
    'work': 0.6,
    'coding': 0.5,    # 写代码最专注，最该克制
    'chat': 0.4,      # 他正在跟别人说话
}

# 键鼠强度 → 修正。人不在电脑前时几乎不涨：说了也没人听，攒着等他回来。
_INTENSITY_WEIGHT: dict[InputIntensity, float] = {
    'away': 0.05,
    'light': 1.0,
    'busy': 0.7,      # 手上正忙，别打断
}


@dataclass(frozen=True)
class InterestState:
    """兴趣值本体。frozen——所有变化都返回新实例，方便单测和 trace 回放。"""

    value: float = 0.0
    updated_at: int = 0


@dataclass(frozen=True)
class InterestFactors:
    """一次增长里五个乘数的取值。全部进 trace。

    兴趣值让开口时机不再可预测，那么「她那次为什么开口」就只能靠回放这四个
    数字来解释。所以它们不是调试残留，是这个设计能被维护的前提。
    """

    activity: float
    favor: float
    energy: float
    ignored: float
    absence: float

    @property
    def rate_per_minute(self) -> float:
        return BASE * self.activity * self.favor * self.energy * self.ignored * self.absence

    def as_trace(self) -> dict[str, float]:
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
    """把当前情境和她的状态折算成五个乘数。

    favor / energy 是 persona 的 0~100 轴，直接除以 100 用——不引入新的
    归一化常量。两者都设了地板：再累、关系再浅也不至于彻底不想说话，只是慢。
    """
    return InterestFactors(
        activity=_ACTIVITY_WEIGHT.get(activity, 1.0) * _INTENSITY_WEIGHT.get(intensity, 1.0),
        favor=max(0.25, favor / 100.0),
        energy=max(0.2, energy / 100.0),
        ignored=IGNORED_DECAY ** max(0, ignored),
        absence=1.0 + min(1.5, max(0.0, absence_hours) / ABSENCE_SCALE_HOURS),
    )


def initial_state(now: int) -> InterestState:
    return InterestState(value=0.0, updated_at=now)


def grow(state: InterestState, factors: InterestFactors, now: int) -> InterestState:
    """按真实流逝时间累积兴趣。

    ★ 用 now - updated_at 而不是「每次调用加一份」：轮询间隔漂了、错过了几个
      tick、机器睡眠醒来，结果都一致。单测里有一条专门守这个（同样的时长拆成
      1 次和 10 次调用，结果必须相同）。
    """
    if state.updated_at <= 0:
        return replace(state, updated_at=now)
    elapsed_minutes = max(0.0, (now - state.updated_at) / 60_000)
    if elapsed_minutes <= 0:
        return state
    growth = factors.rate_per_minute * elapsed_minutes
    return InterestState(value=min(FULL, state.value + growth), updated_at=now)


def wants_to_speak(state: InterestState) -> bool:
    return state.value >= FULL


def spend(state: InterestState, now: int) -> InterestState:
    """她已经开口了（或者已经"想说过了"但没说成）——兴趣归零。

    ★ 被闸门挡下、意图进小本本时同样要调这个。否则兴趣一直满着，下一个 tick
      立刻又想说，会攒出一串连发。她想说话这件事只发生一次。
    """
    return InterestState(value=0.0, updated_at=now)


def minutes_to_full(state: InterestState, factors: InterestFactors) -> float | None:
    """还有多久攒满，给观察面板用。增速为零时返回 None（她这会儿不会主动开口）。"""
    rate = factors.rate_per_minute
    if rate <= 0:
        return None
    return max(0.0, (FULL - state.value) / rate)
