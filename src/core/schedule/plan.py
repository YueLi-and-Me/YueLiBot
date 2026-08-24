"""
每日生成式日程与可配置作息服务。

惰性调用 ensure() 才触发模型；读取、描述和历史补叙均不会为过去日期补生成。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

import asyncio
import json
import re

from src.core.common.clock import now as current_time
from src.core.common.logger import get_logger
from src.core.config.schema import ScheduleConfig
from src.core.llm_models.snapshot import bind_render_params
from src.core.persona.state import (
    ElapsedEffect,
    EnergyTier,
    PersonaState,
    describe_persona_for_planning,
    energy_tier,
)
from src.core.prompts.registry import get_prompt

logger = get_logger(__name__)

PLAN_PREFIX = 'day_plan:'
MAX_HOURS_FOR_HISTORY = 48
MAX_HOURS_FOR_ELAPSED_INTEGRATION = 48
HOUR_MS = 60 * 60_000
BASE_AWAKE_DRAIN = 2.0
REST_RECOVERY_RATE = 4.0


@dataclass
class DayPlanSlot:
    """日程中的一个时间段及其活动和情绪描述。"""

    from_time: str   # HH:MM
    doing: str
    mood: str
    energy_pace: int = 0


@dataclass
class DayPlan:
    """某个自然日的完整日程和作息配置。"""

    date: str
    slots: List[DayPlanSlot]
    bedtime_hint: str
    wake_hint: str
    theme: str
    carry_over: str
    sleep_enabled: bool
    bedtime_day_boundary: str


@dataclass
class ScheduleSleepState:
    """日程服务提供给对话编排的睡眠状态。"""

    asleep: bool
    drowsy: bool
    just_woke: bool = False


@dataclass
class DayPlanGenerationIssue:
    """最近一次日程生成失败的可观测信息。"""

    kind: str      # 'invalid-output' | 'provider-error'
    attempted_at: int
    raw: str | None = None
    reason: str | None = None


# ─────────────────────────────────────────────────────────────────────
# 纯工具函数
# ─────────────────────────────────────────────────────────────────────

def day_plan_date(now: int | datetime) -> str:
    """将毫秒时间戳或本地日期时间转换为日程日期字符串。

    :param now: 毫秒级 Unix 时间戳，或待转换的 ``datetime``。

    :return: ``YYYY-MM-DD`` 格式的本地日期。

    :raises TypeError: 输入不是整数或 ``datetime`` 时由类型操作直接抛出。
    """

    if isinstance(now, int):
        now = datetime.fromtimestamp(now / 1000)
    return now.strftime('%Y-%m-%d')


def clock_minutes(value: str) -> int | None:
    """解析严格的 ``HH:MM`` 时刻。

    :param value: 两位小时和两位分钟组成的字符串，小时范围 00 至 23，分钟范围
            00 至 59。

    :return: 从午夜起计算的分钟数；格式或范围不合法时返回 ``None``。
    """

    m = re.fullmatch(r'(\d{2}):(\d{2})', value)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def _fallback_slots(settings: ScheduleConfig) -> List[DayPlanSlot]:
    """按配置的最少时段数均匀生成备用时间段。

    :param settings: 提供时段数量、活动和情绪默认文本的调度配置。

    :return: 从 00:00 开始均匀分布的备用时段列表。

    :raises ValueError: 配置中的最少时段数不适合作为除数时由计算直接暴露。
    """
    return [
        DayPlanSlot(
            from_time=f'{index * 24 // settings.min_slots:02d}:00',
            doing=settings.fallback_activity,
            mood=settings.fallback_mood,
            energy_pace=0,
        )
        for index in range(settings.min_slots)
    ]


def fallback_day_plan(
    date: str,
    config: ScheduleConfig | None = None,
) -> DayPlan:
    """创建指定日期的配置驱动备用日程。

    :param date: ``YYYY-MM-DD`` 格式的日程日期。
    :param config: 可选调度配置；省略时使用默认配置。

    :return: 不调用模型、完全由配置默认值组成的 ``DayPlan``。
    """

    settings = config or ScheduleConfig()
    return DayPlan(
        date=date,
        slots=_fallback_slots(settings),
        bedtime_hint=settings.fallback_bedtime,
        wake_hint=settings.fallback_wake,
        theme=settings.fallback_theme,
        carry_over=settings.fallback_carry_over,
        sleep_enabled=settings.sleep_enabled,
        bedtime_day_boundary=settings.bedtime_day_boundary,
    )


def planned_sleep_window_from_hints(
    date: str,
    bedtime_hint: str,
    wake_hint: str,
    bedtime_day_boundary_hint: str,
) -> Tuple[int, int]:
    """按明确的时刻提示计算一次跨日作息窗口。

    :param date: ``YYYY-MM-DD`` 格式的基准日期。
    :param bedtime_hint: 入睡时刻，严格使用 ``HH:MM``。
    :param wake_hint: 起床时刻，严格使用 ``HH:MM``。
    :param bedtime_day_boundary_hint: 判断入睡时刻属于次日的边界时刻。

    :return: ``(bedtime_at_ms, wake_at_ms)``，单位均为本地毫秒时间戳。

    :raises ValueError: 日期或任一时刻提示无法解析。
    """
    year, month, day = [int(x) for x in date.split('-')]
    bedtime_minutes = clock_minutes(bedtime_hint)
    wake_minutes = clock_minutes(wake_hint)
    bedtime_day_boundary = clock_minutes(bedtime_day_boundary_hint)
    if bedtime_minutes is None:
        raise ValueError(f'非法 bedtimeHint：{bedtime_hint}')
    if wake_minutes is None:
        raise ValueError(f'非法 wakeHint：{wake_hint}')
    if bedtime_day_boundary is None:
        raise ValueError(f'非法 bedtimeDayBoundary：{bedtime_day_boundary_hint}')
    bedtime_day_offset = (
        1 if bedtime_minutes <= bedtime_day_boundary else 0
    )
    wake_day_offset = (
        bedtime_day_offset
        if wake_minutes > bedtime_minutes
        else bedtime_day_offset + 1
    )

    # 必须用 timedelta 加天，避免月末直接给 day 加一触发日期越界。
    base = datetime(year, month, day)
    bedtime_dt = base + timedelta(
        days=bedtime_day_offset, hours=bedtime_minutes // 60, minutes=bedtime_minutes % 60
    )
    wake_dt = base + timedelta(
        days=wake_day_offset,
        hours=wake_minutes // 60,
        minutes=wake_minutes % 60,
    )
    return (int(bedtime_dt.timestamp() * 1000), int(wake_dt.timestamp() * 1000))


def planned_sleep_window(plan: DayPlan) -> Tuple[int, int]:
    """根据日程中的作息提示返回入睡和起床时间。

    :param plan: 包含日期、入睡、起床和日期边界提示的日程。

    :return: ``(bedtime_at_ms, wake_at_ms)``，单位均为本地毫秒时间戳。

    :raises ValueError: 日程中的日期或时刻提示不合法。
    """
    return planned_sleep_window_from_hints(
        plan.date,
        plan.bedtime_hint,
        plan.wake_hint,
        plan.bedtime_day_boundary,
    )


# ─────────────────────────────────────────────────────────────────────
# 文本结构与敏感信息过滤
# ─────────────────────────────────────────────────────────────────────

_SENSITIVE_TEXT = re.compile(
    r'密码|口令|验证码|账号|银行卡|支付|金额|工资|客户|聊天记录|私信|@\w+|'
    r'[A-Za-z]:\\|\\\\|/Users/|/home/', re.IGNORECASE
)


def _safe_plan_text(value: Any, min_len: int, max_len: int) -> str | None:
    """规范化并过滤日程文本。

    :param value: 待校验的任意输入值。
    :param min_len: 规范化文本允许的最小字符数。
    :param max_len: 规范化文本允许的最大字符数。

    :return: 折叠空白后的文本；类型、长度或敏感信息校验失败时返回 ``None``。
    """

    if not isinstance(value, str):
        return None
    text = re.sub(r'\s+', ' ', value).strip()
    if not min_len <= len(text) <= max_len:
        return None
    if _SENSITIVE_TEXT.search(text):
        return None
    return text


def _safe_doing(value: Any) -> str | None:
    """校验活动文本并移除开头的第一人称口头前缀。

    :param value: 待校验的任意活动文本。

    :return: 可写入日程的活动描述；不合法或只剩前缀时返回 ``None``。
    """

    text = _safe_plan_text(value, 1, 72)
    if not text:
        return None
    normalized = re.sub(r'^我(?!们)[，、：:\s]*', '', text).strip()
    return normalized or None


def _parse_pace(value: Any) -> tuple[int, bool]:
    """解析日程状态节奏字段并区分类型错误与越界整数。

    :param value: 模型或历史数据中的 pace 原始值。

    :return: ``(pace, valid)``。字段缺失、布尔值和非整数按兼容值 0 接收；
        只有超出 [-3, 3] 的整数会令 ``valid`` 为 ``False``。
    """

    if type(value) is not int:
        return (0, True)
    return (value, -3 <= value <= 3)


def activity_avoidance_items(slots: List[DayPlanSlot]) -> List[str]:
    """提取历史日程中需要避免重复安排的活动摘要。

    :param slots: 历史日程时段列表。

    :return: 按首次出现顺序去重、截断到 28 个字符的活动片段列表。
    """

    seen: Dict[str, None] = {}
    for slot in slots:
        first = re.sub(r'^我(?!们)[，、：:\s]*', '', slot.doing)
        first = re.sub(r'^(?:正在|正|又在|还在|继续|准备|开始|慢慢|刚刚|刚|在)', '', first)
        first = re.split(r'[，。；！？]', first, 1)[0].strip()
        if len(first) >= 2:
            seen[first[:28]] = None
    return list(seen.keys())


# ─────────────────────────────────────────────────────────────────────
# 日程解析
# ─────────────────────────────────────────────────────────────────────

def parse_day_plan(
    raw: str,
    date: str,
    config: ScheduleConfig | None = None,
) -> DayPlan | None:
    """解析并严格校验模型生成的日程 JSON。

    :param raw: 模型返回的 JSON 文本。
    :param date: 期望的 ``YYYY-MM-DD`` 日程日期。
    :param config: 可选调度配置；省略时使用默认配置。

    :return: 所有字段、时段顺序、长度和敏感信息检查均通过时返回 ``DayPlan``；任一
        检查失败或 JSON 无法解析时返回 ``None``。
    """
    settings = config or ScheduleConfig()
    # 解析、日期和时段数量任一不匹配都返回 None，由服务层选择备用日程。
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    if value.get('date') != date:
        return None
    slots_raw = value.get('slots')
    if not isinstance(slots_raw, list) or not (
        settings.min_slots <= len(slots_raw) <= settings.max_slots
    ):
        return None

    slots: List[DayPlanSlot] = []
    previous = -1
    for item in slots_raw:
        # 时段必须按严格递增的 HH:MM 排列，避免同一时刻存在多个活动。
        if not isinstance(item, dict):
            return None
        from_val = item.get('from')
        if not isinstance(from_val, str):
            return None
        from_mins = clock_minutes(from_val)
        doing = _safe_doing(item.get('doing'))
        mood = _safe_plan_text(item.get('mood'), 1, 40)
        energy_pace, energy_pace_valid = _parse_pace(item.get('energyPace'))
        if (
            from_mins is None
            or from_mins <= previous
            or not doing
            or not mood
            or not energy_pace_valid
        ):
            return None
        previous = from_mins
        slots.append(
            DayPlanSlot(
                from_time=from_val,
                doing=doing,
                mood=mood,
                energy_pace=energy_pace,
            )
        )

    bedtime_hint = value.get('bedtimeHint', '')
    if not isinstance(bedtime_hint, str) or clock_minutes(bedtime_hint) is None:
        return None
    wake_hint = value.get('wakeHint', '')
    if not isinstance(wake_hint, str) or clock_minutes(wake_hint) is None:
        return None
    theme = _safe_plan_text(value.get('theme'), 1, 72)
    carry_over = _safe_plan_text(value.get('carryOver'), 1, 72)
    # 主题和延续事项属于必填上下文，缺失时不接受部分有效的模型输出。
    if not theme or not carry_over:
        return None
    return DayPlan(
        date=date,
        slots=slots,
        bedtime_hint=bedtime_hint,
        wake_hint=wake_hint,
        theme=theme,
        carry_over=carry_over,
        sleep_enabled=settings.sleep_enabled,
        bedtime_day_boundary=settings.bedtime_day_boundary,
    )


# ─────────────────────────────────────────────────────────────────────
# 日程描述（注入 prompt）
# ─────────────────────────────────────────────────────────────────────

def describe_mood_behavior(mood: str) -> str:
    """把日程情绪字段转换为提示词中的行为约束。

    :param mood: 当前时段的情绪描述。

    :return: 要求模型让情绪影响反应、但继续服从人设的中文提示文本。
    """

    return f'你此刻的状态是「{mood}」。让它自然影响反应，具体表达仍服从你的人设。'


# ─────────────────────────────────────────────────────────────────────
# 提示词构建
# ─────────────────────────────────────────────────────────────────────

def _weekday(now: datetime) -> str:
    """按 ``datetime.weekday`` 计算星期名称。

    :param now: 待转换的本地日期时间。

    :return: 中文星期名称。
    """

    return ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][now.weekday() % 7]


def _weekday_cn(now: datetime) -> str:
    """使用本地日期时间返回中文星期名称。

    :param now: 待转换的本地日期时间。

    :return: ``周一`` 至 ``周日`` 之一。
    """

    mapping = {1: '周一', 2: '周二', 3: '周三', 4: '周四', 5: '周五', 6: '周六', 7: '周日'}
    return mapping[now.isoweekday()]


def _day_occasion(now: datetime, anniversary_at: int) -> str:
    """收集当前日期的固定节日和相识纪念日标签。

    :param now: 待判断的本地日期时间。
    :param anniversary_at: 相识时间的毫秒时间戳；非正值表示没有配置纪念日。

    :return: 以中文顿号连接的节日标签；没有匹配项时返回 ``没有特别节日``。
    """

    fixed = {'01-01': '元旦', '02-14': '情人节', '05-01': '劳动节',
              '10-01': '国庆节', '12-25': '圣诞节'}
    month_day = now.strftime('%m-%d')
    labels: List[str] = []
    if month_day in fixed:
        labels.append(fixed[month_day])
    if anniversary_at > 0:
        ann = datetime.fromtimestamp(anniversary_at / 1000)
        if ann.month == now.month and ann.day == now.day:
            labels.append('认识纪念日')
    return '、'.join(labels) if labels else '没有特别节日'


def build_plan_prompt(
    date: str,
    weekday: str,
    occasion: str,
    persona: str,
    yesterday_theme: str,
    yesterday_bedtime: str,
    yesterday_wake: str,
    yesterday_carry_over: str,
    yesterday_avoided: str,
    density: str,
    character_name: str,
    character_personality: str,
    schedule_config: ScheduleConfig | None = None,
    render_params: dict[str, dict[str, str]] | None = None,
) -> str:
    """构造日程模型请求的完整提示词。

    :param date: 目标日程日期。
    :param weekday: 目标日期的中文星期名称。
    :param occasion: 节日或纪念日描述。
    :param persona: 当前关系与主体状态描述。
    :param yesterday_theme: 前一日日程主题；无记录时由调用方传入占位说明。
    :param yesterday_bedtime: 前一日入睡提示。
    :param yesterday_wake: 前一起床提示。
    :param yesterday_carry_over: 前一日需要延续的事项。
    :param yesterday_avoided: 前一日活动中应避免机械重复的摘要。
    :param density: 近期互动密度描述。
    :param character_name: 角色名称。
    :param character_personality: 角色性格描述。
    :param schedule_config: 可选调度配置；省略时使用默认配置。

    :return: 使用 ``schedule`` 模板渲染后的模型提示词。

    :raises ValueError: 模板占位符集合不匹配时由模板注册表抛出。
    """

    settings = schedule_config or ScheduleConfig()
    # 自动睡眠只决定是否进入离线状态；休息窗口仍承担精力昼夜节律的时间边界。
    sleep_rule = (
        '- 已启用睡眠状态。bedtimeHint 与 wakeHint 可以是任意合法 HH:MM，具体节奏服从角色设定，'
        '不强行套用人类夜间作息。'
        if settings.sleep_enabled
        else '- 不启用睡眠状态。bedtimeHint 与 wakeHint 仍需填写合法 HH:MM，作为角色每天的'
        '休息窗口和精力节律；运行时不会因此进入睡着离线状态，也不要编造睡眠情节。'
    )
    # 所有配置值转换为模板字符串，避免模板注册表接收未声明类型。
    values = {
        'character_name': character_name,
        'date': date,
        'weekday': weekday,
        'occasion': occasion,
        'character_personality': character_personality,
        'persona': persona,
        'yesterday_theme': yesterday_theme,
        'yesterday_bedtime': yesterday_bedtime,
        'yesterday_wake': yesterday_wake,
        'yesterday_carry_over': yesterday_carry_over,
        'yesterday_avoided': yesterday_avoided,
        'density': density,
        'min_slots': str(settings.min_slots),
        'max_slots': str(settings.max_slots),
        'sleep_rule': sleep_rule,
    }
    if render_params is not None:
        render_params['schedule'] = values
    return get_prompt('schedule').render(**values)


# ─────────────────────────────────────────────────────────────────────
# DayPlanService
# ─────────────────────────────────────────────────────────────────────

class _DayPlanStore(Protocol):
    """日程持久化所需的最小 JSON 读写协议。"""

    def read_json(self, key: str, fallback: Any) -> Any:
        """读取键值并在键不存在时返回备用值。

        :param key: 日程存储键。
        :param fallback: 键不存在时返回的值。

        :return: 存储中的 JSON 兼容值或 ``fallback``。
        """

        ...

    def write_json(self, key: str, value: Any) -> None:
        """写入一个 JSON 兼容值。

        :param key: 日程存储键。
        :param value: 待写入的 JSON 兼容值。

        副作用：
            更新底层日程存储。
        """

        ...


def _plan_key(date: str) -> str:
    """生成日程在键值存储中的稳定键名。

    :param date: ``YYYY-MM-DD`` 格式的日程日期。

    :return: 由 ``PLAN_PREFIX`` 和日期拼接出的存储键。
    """

    return f'{PLAN_PREFIX}{date}'


def _previous_date(now: datetime) -> str:
    """计算给定本地日期时间的前一日日期字符串。

    :param now: 当前本地日期时间。

    :return: 前一日的 ``YYYY-MM-DD`` 日期字符串。
    """

    return day_plan_date(now - timedelta(days=1))


def _merge_intervals(intervals: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """合并相交或首尾相接的毫秒时间区间。

    :param intervals: 任意顺序的左闭右开区间。

    :return: 按起点升序排列且互不重叠的区间。
    """

    merged: List[Tuple[int, int]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _is_within_intervals(point: int, intervals: List[Tuple[int, int]]) -> bool:
    """判断毫秒时刻是否位于任一左闭右开区间。"""

    return any(start <= point < end for start, end in intervals)


def _slot_boundary_times(
    plan: DayPlan,
    day_start: datetime,
    lower: int,
    upper: int,
) -> List[int]:
    """返回指定自然日内落入积分范围的日程时段边界。"""

    boundaries: List[int] = []
    for slot in plan.slots:
        minutes = clock_minutes(slot.from_time)
        if minutes is None:
            continue
        boundary = int((day_start + timedelta(minutes=minutes)).timestamp() * 1000)
        if lower < boundary < upper:
            boundaries.append(boundary)
    return boundaries


class DayPlanService:
    """提供日程读取、惰性生成、作息计算和历史活动查询。"""

    def __init__(
        self,
        store: _DayPlanStore,
        persona_state: Callable[[], PersonaState],
        interaction_density: Callable[[int], str],
        anniversary_at: Callable[[], int],
        last_interaction_at: Callable[[], int | None],
        character_name: str,
        character_personality: str,
        generator: Any | None = None,
        schedule_config: ScheduleConfig | None = None,
    ) -> None:
        """初始化日程服务及其依赖回调。

        :param store: 提供 JSON 读写能力的日程存储。
        :param persona_state: 返回当前人物关系与主体状态的无参回调。
        :param interaction_density: 根据毫秒时间戳返回互动密度描述的回调。
        :param anniversary_at: 返回相识时间毫秒时间戳的回调。
        :param last_interaction_at: 返回最近互动毫秒时间戳或 ``None`` 的回调。
        :param character_name: 角色名称。
        :param character_personality: 角色性格描述。
        :param generator: 可选的异步模型生成器，需提供 ``generate(prompt)`` 方法。
        :param schedule_config: 可选调度配置；省略时使用默认配置。

        副作用：
            仅初始化内存状态，不读取存储或调用模型。
        """
        # 保存注入回调而不在构造期读取存储，确保服务可在数据库和模型装配后复用。
        self._store = store
        self._persona_state = persona_state
        self._interaction_density = interaction_density
        self._anniversary_at = anniversary_at
        self._last_interaction_at = last_interaction_at
        self._generator = generator
        self._character_name = character_name
        self._character_personality = character_personality
        self._config = schedule_config or ScheduleConfig()
        self._inflight: Dict[str, asyncio.Task[DayPlan]] = {}
        self._generation_issues: Dict[str, DayPlanGenerationIssue] = {}

    def get(self, now: int | None = None) -> DayPlan:
        """读取当天已保存日程，不触发模型生成。

        :param now: 可选的当前毫秒时间戳；省略时读取统一时钟。

        :return: 已保存且通过校验的当天日程；不存在或无效时返回配置备用日程。
        """

        now = now if now is not None else current_time()
        date = day_plan_date(now)
        return self._read(date) or fallback_day_plan(date, self._config)

    async def ensure(self, now: int | None = None) -> DayPlan:
        """确保当天存在真实日程，必要时等待一次异步生成。

        :param now: 可选的当前毫秒时间戳；省略时读取统一时钟。

        :return: 已保存日程、生成成功的新日程或生成失败时的备用日程。

        副作用：
            可能调用模型、写入日程存储并更新生成失败冷却状态；同日期并发请求
            共享同一个生成任务。
        """

        now = now if now is not None else current_time()
        date = day_plan_date(now)
        existing = self._read_generation_result(date)
        if existing:
            return existing
        if self._generation_is_cooling_down(date, now):
            return fallback_day_plan(date, self._config)
        return await self._start_generation(date, now)

    def ensure_background(self, now: int | None = None) -> DayPlan:
        """当天日程缺失时立即返回备用计划并启动后台生成。

        :param now: 可选的当前毫秒时间戳；省略时读取统一时钟。

        :return: 已保存日程、冷却期备用日程或立即生成的备用日程。

        副作用：
            可能创建一个异步生成任务，但不会等待模型响应。
        """
        now = now if now is not None else current_time()
        date = day_plan_date(now)
        existing = self._read_generation_result(date)
        if existing:
            return existing
        if self._generation_is_cooling_down(date, now):
            return fallback_day_plan(date, self._config)
        self._start_generation(date, now)
        return fallback_day_plan(date, self._config)

    def _start_generation(self, date: str, now: int) -> asyncio.Task[DayPlan]:
        """为指定日期创建或复用唯一的后台生成任务。

        :param date: ``YYYY-MM-DD`` 格式的目标日期。
        :param now: 启动生成时的毫秒时间戳。

        :return: 当前日期对应的异步日程生成任务。

        副作用：
            创建 asyncio task 并登记到 ``_inflight``；任务结束后自动清理登记。
        """
        existing = self._inflight.get(date)
        if existing is not None:
            return existing
        task = asyncio.create_task(
            self._generate(
                date,
                datetime.fromtimestamp(now / 1000),
                now,
            )
        )
        self._inflight[date] = task
        task.add_done_callback(
            lambda completed, generated_date=date: self._finish_generation(
                generated_date,
                completed,
            )
        )
        return task

    def _finish_generation(
        self,
        date: str,
        task: asyncio.Task[DayPlan],
    ) -> None:
        """清理已结束的生成任务并记录未处理异常。

        :param date: 任务对应的日程日期。
        :param task: 已完成或被取消的异步生成任务。

        副作用：
            从 ``_inflight`` 移除当前任务；非取消异常会写入错误日志。
        """
        if self._inflight.get(date) is task:
            self._inflight.pop(date, None)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.exception('日程后台生成任务异常', date=date)

    def describe(self, now: int, sleep: ScheduleSleepState, *, include_activity: bool = False) -> str:
        """生成当前日程和睡眠状态的中文提示文本。

        :param now: 当前毫秒时间戳。
        :param sleep: 当前睡眠状态。
        :param include_activity: 是否连当前时段的具体活动一并渲染；默认只给情绪与
            作息影响，详见 :func:`describe_day_plan`。

        :return: 可注入对话提示词的行为描述。
        """

        return describe_day_plan(
            self.get(now),
            datetime.fromtimestamp(now / 1000),
            self._persona_state(),
            sleep,
            include_activity=include_activity,
        )

    def generation_issue(self, now: int) -> DayPlanGenerationIssue | None:
        """读取指定日期最近一次生成失败记录。

        :param now: 指定日期内的毫秒时间戳。

        :return: 当天生成问题，或没有失败记录时返回 ``None``。
        """

        return self._generation_issues.get(day_plan_date(now))

    def _generation_is_cooling_down(self, date: str, now: int) -> bool:
        """判断指定日期是否仍处于生成失败后的重试冷却期。

        :param date: ``YYYY-MM-DD`` 格式的日程日期。
        :param now: 当前毫秒时间戳。

        :return: 最近一次失败存在且尚未达到配置重试间隔时返回 ``True``。
        """

        issue = self._generation_issues.get(date)
        return (
            issue is not None
            and now < issue.attempted_at
            + self._config.generation_retry_interval_minutes * 60_000
        )

    def sleep_inputs(self, now: int | None = None) -> Dict[str, Any]:
        """收集睡眠状态机所需的当前日程和交互输入。

        :param now: 可选的当前毫秒时间戳；省略时读取统一时钟。

        :return: 包含日期、作息提示、睡眠开关、精力和最近互动时间的字典。
        """

        now = now if now is not None else current_time()
        current_dt = datetime.fromtimestamp(now / 1000)
        today = self.get(now)
        yesterday_date = _previous_date(current_dt)
        yesterday = self._read(yesterday_date) or fallback_day_plan(
            yesterday_date,
            self._config,
        )
        if today.sleep_enabled and yesterday.sleep_enabled:
            yesterday_wake = planned_sleep_window(yesterday)[1]
            today_bedtime = planned_sleep_window(today)[0]
            cycle_boundary = yesterday_wake + (today_bedtime - yesterday_wake) // 2
            plan = yesterday if now < cycle_boundary else today
        else:
            plan = today
        return {
            'date': plan.date, 'bedtime_hint': plan.bedtime_hint, 'wake_hint': plan.wake_hint,
            'sleep_enabled': plan.sleep_enabled, 'energy': self._persona_state().energy,
            'bedtime_day_boundary': plan.bedtime_day_boundary,
            'last_interaction_at': self._last_interaction_at(),
        }

    def _rest_intervals_on_day(
        self,
        day_start: datetime,
        day_end_ms: int,
    ) -> List[Tuple[int, int]]:
        """收集一个自然日内由相邻两份日程形成的休息区间。

        bedtime 可按日期边界归到计划日次日，wake 最晚再落到后一天；因此回看
        前两份日程并合并实际交集，才能覆盖非人类作息的长休息尾段。休息掩码
        始终与自动睡眠开关无关。
        """

        day_start_ms = int(day_start.timestamp() * 1000)
        intervals: List[Tuple[int, int]] = []
        for offset in (-2, -1, 0):
            plan_date = day_plan_date(day_start + timedelta(days=offset))
            plan = self._read(plan_date) or fallback_day_plan(plan_date, self._config)
            rest_start, rest_end = planned_sleep_window(plan)
            clipped_start = max(day_start_ms, rest_start)
            clipped_end = min(day_end_ms, rest_end)
            if clipped_end > clipped_start:
                intervals.append((clipped_start, clipped_end))
        return _merge_intervals(intervals)

    @staticmethod
    def _daily_energy_pace_mean(
        plan: DayPlan,
        day_start: datetime,
        day_start_ms: int,
        day_end_ms: int,
        rest_intervals: List[Tuple[int, int]],
    ) -> float:
        """计算整日清醒时段的时长加权 energyPace 均值。"""

        boundaries = {
            day_start_ms,
            day_end_ms,
            *_slot_boundary_times(plan, day_start, day_start_ms, day_end_ms),
        }
        for rest_start, rest_end in rest_intervals:
            boundaries.update((rest_start, rest_end))
        ordered = sorted(boundaries)
        awake_ms = 0
        weighted_pace_ms = 0.0
        for segment_start, segment_end in zip(ordered, ordered[1:]):
            midpoint = segment_start + (segment_end - segment_start) // 2
            if _is_within_intervals(midpoint, rest_intervals):
                continue
            slot = _slot_at(plan, datetime.fromtimestamp(midpoint / 1000))
            duration_ms = segment_end - segment_start
            awake_ms += duration_ms
            weighted_pace_ms += duration_ms * slot.energy_pace
        return weighted_pace_ms / awake_ms if awake_ms else 0.0

    @staticmethod
    def _energy_delta_on_day(
        plan: DayPlan,
        day_start: datetime,
        from_ms: int,
        to_ms: int,
        rest_intervals: List[Tuple[int, int]],
        pace_mean: float,
    ) -> float:
        """对单个自然日内的查询片段积分精力变化。"""

        boundaries = {
            from_ms,
            to_ms,
            *_slot_boundary_times(plan, day_start, from_ms, to_ms),
        }
        for rest_start, rest_end in rest_intervals:
            if from_ms < rest_start < to_ms:
                boundaries.add(rest_start)
            if from_ms < rest_end < to_ms:
                boundaries.add(rest_end)
        energy_delta = 0.0
        ordered = sorted(boundaries)
        for segment_start, segment_end in zip(ordered, ordered[1:]):
            midpoint = segment_start + (segment_end - segment_start) // 2
            hours = (segment_end - segment_start) / HOUR_MS
            if _is_within_intervals(midpoint, rest_intervals):
                energy_delta += hours * REST_RECOVERY_RATE
                continue
            slot = _slot_at(plan, datetime.fromtimestamp(midpoint / 1000))
            normalized_pace = slot.energy_pace - pace_mean
            energy_delta += hours * BASE_AWAKE_DRAIN * (normalized_pace - 1)
        return energy_delta

    def integrate_between(
        self,
        from_ms: int,
        to_ms: int,
        earlier_resting: bool = False,
    ) -> ElapsedEffect:
        """按日程休息窗口和清醒 pace 积分经过时间的精力变化。

        :param from_ms: 区间起点毫秒时间戳。
        :param to_ms: 区间终点毫秒时间戳。
        :param earlier_resting: 超出最近 48 小时的早期区间是否按持续休息处理。

        :return: 可由人格服务直接应用的精力增量；终点不晚于起点时返回零增量。

        性能：
            仅对最近 48 小时逐自然日、时段和休息边界积分；更早区间维持原有
            “全休息或全清醒”的截断语义，避免离线跨度导致无界读取。
        """

        if to_ms <= from_ms:
            return ElapsedEffect(energy_delta=0.0)
        detailed_from = max(
            from_ms,
            to_ms - MAX_HOURS_FOR_ELAPSED_INTEGRATION * HOUR_MS,
        )
        earlier_hours = (detailed_from - from_ms) / HOUR_MS
        earlier_rate = REST_RECOVERY_RATE if earlier_resting else -BASE_AWAKE_DRAIN
        energy_delta = earlier_hours * earlier_rate

        cursor = datetime.fromtimestamp(detailed_from / 1000).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        while int(cursor.timestamp() * 1000) < to_ms:
            day_start_ms = int(cursor.timestamp() * 1000)
            next_day = cursor + timedelta(days=1)
            day_end_ms = int(next_day.timestamp() * 1000)
            query_start = max(detailed_from, day_start_ms)
            query_end = min(to_ms, day_end_ms)
            if query_end > query_start:
                date = day_plan_date(cursor)
                plan = self._read(date) or fallback_day_plan(date, self._config)
                rest_intervals = self._rest_intervals_on_day(cursor, day_end_ms)
                pace_mean = self._daily_energy_pace_mean(
                    plan,
                    cursor,
                    day_start_ms,
                    day_end_ms,
                    rest_intervals,
                )
                energy_delta += self._energy_delta_on_day(
                    plan,
                    cursor,
                    query_start,
                    query_end,
                    rest_intervals,
                    pace_mean,
                )
            cursor = next_day
        return ElapsedEffect(energy_delta=energy_delta)

    def activities_between(self, from_dt: datetime, to_dt: datetime) -> List[str]:
        """提取时间区间内按小时变化的活动描述。

        :param from_dt: 本地日期时间区间起点。
        :param to_dt: 本地日期时间区间终点。

        :return: 按时间顺序去重后的活动文本，最多检查 48 个小时。
        """

        out: List[str] = []
        cursor = from_dt.replace(minute=0, second=0, microsecond=0)
        last_key = ''
        for _ in range(MAX_HOURS_FOR_HISTORY):
            if cursor > to_dt:
                break
            plan_date = day_plan_date(cursor)
            plan = self._read(plan_date) or fallback_day_plan(plan_date, self._config)
            slot = _slot_at(plan, cursor)
            key = f'{plan.date}:{slot.from_time}'
            if key != last_key:
                out.append(f'{cursor.hour}点左右{slot.doing}')
                last_key = key
            cursor += timedelta(hours=1)
        return out

    def _read(self, date: str) -> DayPlan | None:
        """读取并解析指定日期的当前或兼容旧格式日程。

        :param date: ``YYYY-MM-DD`` 格式的日程日期。

        :return: 通过当前格式或旧格式校验的日程；不存在或无法解析时返回 ``None``。
        """

        raw = self._store.read_json(_plan_key(date), None)
        if raw is None:
            return None
        raw_str = json.dumps(raw) if not isinstance(raw, str) else raw
        plan = parse_day_plan(raw_str, date, self._config)
        if plan is None:
            plan = self._read_legacy(raw, date)
        return plan

    def _read_generation_result(self, date: str) -> DayPlan | None:
        """读取真实生成结果，并将模型启用时的备用日程视为缺失。

        :param date: ``YYYY-MM-DD`` 格式的日程日期。

        :return: 可作为真实生成结果使用的日程，或 ``None``。
        """

        plan = self._read(date)
        if self._generator is not None and plan == fallback_day_plan(date, self._config):
            return None
        return plan

    async def _generate(
        self,
        date: str,
        now: datetime,
        attempted_at: int,
    ) -> DayPlan:
        """调用生成器创建、校验并保存指定日期的日程。

        :param date: 目标日程日期。
        :param now: 目标日期对应的本地日期时间。
        :param attempted_at: 本次生成尝试的毫秒时间戳，用于记录冷却起点。

        :return: 校验通过并保存的模型日程，或生成器未配置、返回非法内容、抛出错误时的
            备用日程。

        副作用：
            最多调用生成器两次，写入日程存储，并更新 ``_generation_issues``。
            生成器异常会被记录为 provider-error，不会继续向后台任务传播。
        """

        fallback = fallback_day_plan(date, self._config)
        if self._generator is None:
            # 未配置生成器时仍保存备用计划，后续读取可区分“已初始化”与“完全缺失”。
            self._generation_issues.pop(date, None)
            self._store.write_json(_plan_key(date), _plan_to_dict(fallback))
            return fallback
        # 前一日数据只用于提示词上下文，不会因读取历史日期触发生成。
        yesterday = self._read(_previous_date(now))
        render_params: dict[str, dict[str, str]] = {}
        prompt = build_plan_prompt(
            date=date, weekday=_weekday_cn(now),
            occasion=_day_occasion(now, self._anniversary_at()),
            persona=describe_persona_for_planning(self._persona_state()),
            yesterday_theme=yesterday.theme if yesterday else '昨天没有留存计划，不要写得像固定流水线。',
            yesterday_bedtime=yesterday.bedtime_hint if yesterday else '没有记录',
            yesterday_wake=yesterday.wake_hint if yesterday else '没有记录',
            yesterday_carry_over=yesterday.carry_over if yesterday else '没有记录',
            yesterday_avoided='、'.join(activity_avoidance_items(yesterday.slots)) if yesterday else '没有记录；今天没有旧活动需要避开。',
            density=self._interaction_density(int(now.timestamp() * 1000)),
            character_name=self._character_name,
            character_personality=self._character_personality,
            schedule_config=self._config,
            render_params=render_params,
        )
        try:
            # 首次输出失败时只重试一次，避免单次日程请求无限占用模型资源。
            bind_render_params(render_params)
            raw = await self._generator.generate(prompt)
            parsed = parse_day_plan(raw, date, self._config)
            if not parsed:
                retry_prompt = '\n'.join([prompt, '', '你上一次的 JSON 没通过本地结构校验。请从头重新生成完整 JSON。'])
                bind_render_params(render_params)
                raw = await self._generator.generate(retry_prompt)
                parsed = parse_day_plan(raw, date, self._config)
            if not parsed:
                # 结构校验失败记录原始正文，便于定位模型输出格式问题。
                self._generation_issues[date] = DayPlanGenerationIssue(
                    kind='invalid-output',
                    attempted_at=attempted_at,
                    raw=raw,
                )
                logger.error(
                    '日程生成结果未通过结构校验',
                    date=date,
                    bodyChars=len(raw),
                )
                return fallback
            self._generation_issues.pop(date, None)
            # 只有通过本地校验的日程才写入存储，防止坏 JSON 污染后续睡眠计算。
            self._store.write_json(_plan_key(date), _plan_to_dict(parsed))
            return parsed
        except Exception as exc:
            self._generation_issues[date] = DayPlanGenerationIssue(
                kind='provider-error',
                attempted_at=attempted_at,
                reason=str(exc),
            )
            logger.error('日程生成失败', date=date, error=str(exc))
            return fallback

    def _read_legacy(self, raw: Any, date: str) -> DayPlan | None:
        """解析早期日程数据结构并补齐当前配置字段。

        :param raw: 存储中读取的任意 JSON 值。
        :param date: 目标日程日期。

        :return: 能通过旧格式兼容校验的当前 ``DayPlan``；否则返回 ``None``。
        """

        if not isinstance(raw, dict) or not isinstance(raw.get('slots'), list):
            return None
        # 旧格式只在当前时段数量范围内兼容，缺少必需字段时交给备用计划处理。
        slots_raw = raw['slots']
        if not (
            self._config.min_slots
            <= len(slots_raw)
            <= self._config.max_slots
        ):
            return None
        slots: List[DayPlanSlot] = []
        previous = -1
        for item in slots_raw:
            # 复用当前格式的时刻、活动和情绪校验，避免兼容路径放宽数据约束。
            if not isinstance(item, dict):
                return None
            from_mins = clock_minutes(item.get('from', ''))
            doing = _safe_doing(item.get('doing'))
            mood = _safe_plan_text(item.get('mood'), 1, 40)
            energy_pace, energy_pace_valid = _parse_pace(item.get('energyPace'))
            if (
                from_mins is None
                or from_mins <= previous
                or not doing
                or not mood
                or not energy_pace_valid
            ):
                return None
            previous = from_mins
            slots.append(
                DayPlanSlot(
                    from_time=item['from'],
                    doing=doing,
                    mood=mood,
                    energy_pace=energy_pace,
                )
            )
        bedtime_hint = raw.get('bedtimeHint', self._config.fallback_bedtime)
        if not isinstance(bedtime_hint, str) or clock_minutes(bedtime_hint) is None:
            bedtime_hint = self._config.fallback_bedtime
        wake_hint = raw.get('wakeHint', self._config.fallback_wake)
        if not isinstance(wake_hint, str) or clock_minutes(wake_hint) is None:
            wake_hint = self._config.fallback_wake
        theme = _safe_plan_text(raw.get('theme'), 1, 72)
        if not theme:
            return None
        carry_over = (
            _safe_plan_text(raw.get('carryOver'), 1, 72)
            or self._config.fallback_carry_over
        )
        return DayPlan(
            date=date,
            slots=slots,
            bedtime_hint=bedtime_hint,
            wake_hint=wake_hint,
            theme=theme,
            carry_over=carry_over,
            sleep_enabled=self._config.sleep_enabled,
            bedtime_day_boundary=self._config.bedtime_day_boundary,
        )


def _plan_to_dict(plan: DayPlan) -> Dict[str, Any]:
    """将日程数据类转换为存储格式字典。

    :param plan: 待序列化的日程对象。

    :return: 使用持久化字段命名约定的 JSON 兼容字典。
    """

    return {
        'date': plan.date,
        'slots': [
            {
                'from': slot.from_time,
                'doing': slot.doing,
                'mood': slot.mood,
                'energyPace': slot.energy_pace,
            }
            for slot in plan.slots
        ],
        'bedtimeHint': plan.bedtime_hint,
        'wakeHint': plan.wake_hint,
        'theme': plan.theme,
        'carryOver': plan.carry_over,
        'sleepEnabled': plan.sleep_enabled,
        'bedtimeDayBoundary': plan.bedtime_day_boundary,
    }


def _slot_at(plan: DayPlan, now: datetime) -> DayPlanSlot:
    """查找当前时刻生效的最后一个日程时段。

    :param plan: 按时间升序排列的日程。
    :param now: 本地日期时间。

    :return: 当前时刻对应的时段；若尚未到首个时段则返回首个时段。
    """

    minutes = now.hour * 60 + now.minute
    current = plan.slots[0]
    for slot in plan.slots:
        from_mins = clock_minutes(slot.from_time)
        if from_mins is not None and from_mins <= minutes:
            current = slot
        else:
            break
    return current


# 活动问句匹配。只认「问你在干什么/忙不忙」这一类，不含作息问句：
# 睡着、犯困、刚醒是状态，本来就无条件注入，不需要问才给。
#
# 容忍误命中（「你干嘛这么凶」同样会命中）：命中只是把当前活动作为备答材料递进去，
# 注入文案写成条件句，不构成「本轮必须交代自己在干什么」的指令。
_ACTIVITY_QUESTION = re.compile(r'干嘛|干什么|干啥|做什么|做啥|忙什么|忙啥|忙不忙|在忙|在干')


def asks_about_activity(text: str) -> bool:
    """判断本回合来消息里有没有在问她此刻在做什么。

    :param text: 本回合合并后的用户原文。

    :return: 命中活动问句时返回 ``True``，用于决定是否注入当前时段的具体活动。
    """

    return bool(_ACTIVITY_QUESTION.search(text))


def _energy_behavior(state: PersonaState) -> str:
    """把精力档转换为当前回合所需的最小行为提示。"""

    tier = energy_tier(state)
    if tier is EnergyTier.HIGH:
        return '今天整体精神很好，反应可以更轻快，但不用因此变得吵闹'
    if tier is EnergyTier.TIRED:
        return '今天整体有点累，反应可以稍短、稍慢，但不要反复宣告困倦'
    if tier is EnergyTier.SPENT:
        return '今天整体已经精疲力尽，反应要明显简短迟缓，但不要自动催对方睡觉'
    return ''


def describe_day_plan(
    plan: DayPlan,
    now: datetime,
    state: PersonaState,
    sleep: ScheduleSleepState,
    *,
    include_activity: bool = False,
) -> str:
    """将日程时段和睡眠状态渲染为对话行为提示。

    默认只渲染时段的影响（情绪、精力、作息），不渲染时段写的具体活动。

    - 现象：活动一旦以「此刻你在收拾书桌、准备洗漱」的形式逐轮注入，模型就把它
      当成本轮要交代的内容；一个时段横跨一两个小时，于是连续二十多条回复都以
      「我去洗漱了」收尾，而她始终没有真的离开，比不提日程更假。
    - 原因：喂进去的是一串可叙述的动作，再靠「这只是背景、别主动说」压制，等于让
      模型在两条互相矛盾的指令之间取舍，产出必然摇摆。
    - 后果：约束层压不住这件事，此前加过的纪律说明并未止住播报；要改的是喂什么，
      而不是喂完再限制。把活动改回默认注入，播报行为会立刻回归。

    :param plan: 当前自然日的日程。
    :param now: 当前本地日期时间。
    :param state: 当前人物关系与主体精力状态。
    :param sleep: 睡眠、困倦和刚醒状态。
    :param include_activity: 是否连具体活动一并渲染。仅在对方开口问起
        （见 :func:`asks_about_activity`），或主动搭话本就以日程为由头时为 ``True``。

    :return: 描述当前情绪、作息行为，以及按需附带具体活动的中文文本。
    """

    if sleep.asleep:
        slot = DayPlanSlot(from_time='00:00', doing='在睡觉', mood='被叫醒时会有些迷迷糊糊')
    else:
        slot = _slot_at(plan, now)
    if sleep.just_woke:
        lines = ['你刚醒没多久，还在慢慢把意识拢回来；别装得已经精神十足，语气应有一点迷糊和迟缓。']
    else:
        current_behavior = describe_mood_behavior(slot.mood).rstrip('。')
        energy_behavior = '' if sleep.asleep or sleep.drowsy else _energy_behavior(state)
        if energy_behavior:
            current_behavior = f'{current_behavior}；{energy_behavior}'
        lines = [f'{current_behavior}。']
        if include_activity:
            doing = slot.doing if slot.doing.startswith('你') else f'你{slot.doing}'
            # 写成条件句：既覆盖误命中的场合，也挡住「顺势宣告下一步」这类越界发挥。
            lines.append(
                f'如果他是在问你在干什么：此刻{doing}。照实答一句就够，'
                '不用展开讲，也不要顺势宣告你接下来要去做什么。'
            )
    if sleep.asleep:
        lines.append('你已经睡着了；如果他现在找你说话，你是被叫醒的，反应要符合刚醒时的迷糊。')
    elif sleep.drowsy:
        lines.append(
            f'你开始犯困，本来想在{plan.bedtime_hint}左右休息；'
            '困意体现在回话变短、变懒、接话没那么起劲，不是反复宣告自己要去睡。'
        )
    return '\n'.join(lines)
