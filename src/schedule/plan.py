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

from src.common.clock import now as current_time
from src.common.logger import get_logger
from src.config.schema import ScheduleConfig

logger = get_logger(__name__)

PLAN_PREFIX = 'day_plan:'
MAX_HOURS_FOR_HISTORY = 48
MAX_HOURS_FOR_ELAPSED_INTEGRATION = 48
HOUR_MS = 60 * 60_000


@dataclass
class DayPlanSlot:
    from_time: str   # HH:MM
    doing: str
    mood: str


@dataclass
class DayPlan:
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
    asleep: bool
    drowsy: bool
    just_woke: bool = False


@dataclass
class DayPlanGenerationIssue:
    kind: str      # 'invalid-output' | 'provider-error'
    attempted_at: int
    raw: str | None = None
    reason: str | None = None


# ─────────────────────────────────────────────────────────────────────
# 纯工具函数
# ─────────────────────────────────────────────────────────────────────

def day_plan_date(now: int | datetime) -> str:
    if isinstance(now, int):
        now = datetime.fromtimestamp(now / 1000)
    return now.strftime('%Y-%m-%d')


def clock_minutes(value: str) -> int | None:
    m = re.fullmatch(r'(\d{2}):(\d{2})', value)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def _fallback_slots(settings: ScheduleConfig) -> List[DayPlanSlot]:
    """按配置段数均匀铺开备用时段，内容完全取自用户配置。"""
    return [
        DayPlanSlot(
            from_time=f'{index * 24 // settings.min_slots:02d}:00',
            doing=settings.fallback_activity,
            mood=settings.fallback_mood,
        )
        for index in range(settings.min_slots)
    ]


def fallback_day_plan(
    date: str,
    config: ScheduleConfig | None = None,
) -> DayPlan:
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
    """按明确给出的配置解释一次作息窗口，不读取其它默认值。"""
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
    """返回 (bedtime_at_ms, wake_at_ms)。"""
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
    if not isinstance(value, str):
        return None
    text = re.sub(r'\s+', ' ', value).strip()
    if not min_len <= len(text) <= max_len:
        return None
    if _SENSITIVE_TEXT.search(text):
        return None
    return text


def _safe_doing(value: Any) -> str | None:
    text = _safe_plan_text(value, 1, 72)
    if not text:
        return None
    normalized = re.sub(r'^我(?!们)[，、：:\s]*', '', text).strip()
    return normalized or None


def activity_avoidance_items(slots: List[DayPlanSlot]) -> List[str]:
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
    """严格校验日程结构与敏感信息，不替可配置人设裁决生活方式。"""
    settings = config or ScheduleConfig()
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
        if not isinstance(item, dict):
            return None
        from_val = item.get('from')
        if not isinstance(from_val, str):
            return None
        from_mins = clock_minutes(from_val)
        doing = _safe_doing(item.get('doing'))
        mood = _safe_plan_text(item.get('mood'), 1, 40)
        if from_mins is None or from_mins <= previous or not doing or not mood:
            return None
        previous = from_mins
        slots.append(DayPlanSlot(from_time=from_val, doing=doing, mood=mood))

    bedtime_hint = value.get('bedtimeHint', '')
    if not isinstance(bedtime_hint, str) or clock_minutes(bedtime_hint) is None:
        return None
    wake_hint = value.get('wakeHint', '')
    if not isinstance(wake_hint, str) or clock_minutes(wake_hint) is None:
        return None
    theme = _safe_plan_text(value.get('theme'), 1, 72)
    carry_over = _safe_plan_text(value.get('carryOver'), 1, 72)
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
    return f'你此刻的状态是「{mood}」。让它自然影响反应，具体表达仍服从你的人设。'


# ─────────────────────────────────────────────────────────────────────
# 提示词构建
# ─────────────────────────────────────────────────────────────────────

def _weekday(now: datetime) -> str:
    return ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][now.weekday() % 7]
    # Python weekday: Mon=0..Sun=6; TS: Sun=0..Sat=6; correct via isoweekday
    # Actually let me use isoweekday: Mon=1..Sun=7
    # Fixed below:


def _weekday_cn(now: datetime) -> str:
    mapping = {1: '周一', 2: '周二', 3: '周三', 4: '周四', 5: '周五', 6: '周六', 7: '周日'}
    return mapping[now.isoweekday()]


def _day_occasion(now: datetime, anniversary_at: int) -> str:
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
    character_name: str = '角色',
    character_identity: str = '',
    character_boundaries: str = '',
    schedule_config: ScheduleConfig | None = None,
) -> str:
    settings = schedule_config or ScheduleConfig()
    sleep_rule = (
        '- 已启用睡眠状态。bedtimeHint 与 wakeHint 可以是任意合法 HH:MM，具体节奏服从角色设定，'
        '不强行套用人类夜间作息。'
        if settings.sleep_enabled
        else '- 不启用睡眠状态。bedtimeHint 与 wakeHint 仍需填写合法 HH:MM 以保持结构稳定，'
        '但运行时会忽略它们，不要为了填字段编造睡眠情节。'
    )
    return '\n'.join([
        f'你为「{character_name}」规划这一天。日程属于这个角色自己，不是等用户出现的值班表。',
        '只返回合法 JSON，不要 Markdown 或解释。',
        f'日期：{date}，{weekday}；特别日：{occasion}。',
        '', '# 角色设定', character_identity,
        '', '# 角色边界', character_boundaries,
        '', '# 当前状态', persona, '',
        f'昨天主题：{yesterday_theme}',
        f'昨天休息时段提示：{yesterday_bedtime} 到 {yesterday_wake}。',
        f'昨天留下的小事：{yesterday_carry_over}',
        f'昨天已经做过、今天要避开重复：{yesterday_avoided}',
        f'最近互动：{density}', '',
        '输出结构：',
        '{"date":"YYYY-MM-DD","slots":[{"from":"HH:MM","doing":"省略主语的活动或状态","mood":"会影响回应的当前状态"}],'
        '"bedtimeHint":"HH:MM","wakeHint":"HH:MM","theme":"一句话主题","carryOver":"今天明确接着做的一件小事"}',
        '', '角色一致性：',
        '- 活动、节奏和状态必须服从角色设定；可以贴近日常，也可以是奇幻、机械、数据生命或其它非人类形态。',
        '- 不要擅自把角色改写成普通人，也不要为了显得有生活而塞进与设定冲突的吃饭、上班或睡觉。',
        '- 活动要具体，但不用每段都设计成能主动搭话的话题。角色有些事只是自己想做。',
        '- mood 写会怎样影响当下反应，具体表达方式仍由人设决定，不要擅自规定温柔、嘴硬或热情。',
        '- theme 是今天持续的一条线。carryOver 可以写明天继续的事；确实没有时写「无」。',
        '', '结构与边界：',
        f'- slots 为 {settings.min_slots} 到 {settings.max_slots} 段，from 严格升序；段数和分布服从角色自己的节奏。',
        '- slots 数组里的每一段必须是独立 JSON 对象，每个对象只能各有一个 from、doing、mood，禁止在同一对象里重复键。',
        sleep_rule,
        '- doing 省略「我」和角色名等主语，写清活动、状态或变化。',
        '- carryOver 不能为空；昨天确有未完成事项时，今天至少一个 doing 要接上它。',
        '- 禁止具体人名、地名、公司名、文件路径、完整文件名、私人聊天、账号、金额。',
    ])


# ─────────────────────────────────────────────────────────────────────
# DayPlanService
# ─────────────────────────────────────────────────────────────────────

class _DayPlanStore(Protocol):
    def read_json(self, key: str, fallback: Any) -> Any: ...
    def write_json(self, key: str, value: Any) -> None: ...


def _plan_key(date: str) -> str:
    return f'{PLAN_PREFIX}{date}'


def _previous_date(now: datetime) -> str:
    return day_plan_date(now - timedelta(days=1))


class DayPlanService:
    def __init__(
        self,
        store: _DayPlanStore,
        persona_description: Callable[[], str],
        interaction_density: Callable[[int], str],
        anniversary_at: Callable[[], int],
        energy: Callable[[], float],
        last_interaction_at: Callable[[], int | None],
        generator: Any | None = None,
        character_name: str = '角色',
        character_identity: str = '',
        character_boundaries: str = '',
        schedule_config: ScheduleConfig | None = None,
    ) -> None:
        self._store = store
        self._persona_description = persona_description
        self._interaction_density = interaction_density
        self._anniversary_at = anniversary_at
        self._energy = energy
        self._last_interaction_at = last_interaction_at
        self._generator = generator
        self._character_name = character_name
        self._character_identity = character_identity
        self._character_boundaries = character_boundaries
        self._config = schedule_config or ScheduleConfig()
        self._inflight: Dict[str, asyncio.Task[DayPlan]] = {}
        self._generation_issues: Dict[str, DayPlanGenerationIssue] = {}

    def get(self, now: int | None = None) -> DayPlan:
        now = now if now is not None else current_time()
        date = day_plan_date(now)
        return self._read(date) or fallback_day_plan(date, self._config)

    async def ensure(self, now: int | None = None) -> DayPlan:
        now = now if now is not None else current_time()
        date = day_plan_date(now)
        existing = self._read_generation_result(date)
        if existing:
            return existing
        if self._generation_is_cooling_down(date, now):
            return fallback_day_plan(date, self._config)
        return await self._start_generation(date, now)

    def ensure_background(self, now: int | None = None) -> DayPlan:
        """当天计划缺失时立即返回备用计划，并在后台生成真实计划。"""
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
        if self._inflight.get(date) is task:
            self._inflight.pop(date, None)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.exception('日程后台生成任务异常', date=date)

    def describe(self, now: int, sleep: ScheduleSleepState) -> str:
        return describe_day_plan(self.get(now), datetime.fromtimestamp(now / 1000), sleep)

    def generation_issue(self, now: int) -> DayPlanGenerationIssue | None:
        return self._generation_issues.get(day_plan_date(now))

    def _generation_is_cooling_down(self, date: str, now: int) -> bool:
        issue = self._generation_issues.get(date)
        return (
            issue is not None
            and now < issue.attempted_at
            + self._config.generation_retry_interval_minutes * 60_000
        )

    def sleep_inputs(self, now: int | None = None) -> Dict[str, Any]:
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
            'sleep_enabled': plan.sleep_enabled, 'energy': self._energy(),
            'bedtime_day_boundary': plan.bedtime_day_boundary,
            'last_interaction_at': self._last_interaction_at(),
        }

    def sleep_hours_between(self, from_ms: int, to_ms: int, earlier_asleep: bool = False) -> float:
        if to_ms <= from_ms:
            return 0.0
        detailed_from = max(from_ms, to_ms - MAX_HOURS_FOR_ELAPSED_INTEGRATION * HOUR_MS)
        sleep_ms = (
            detailed_from - from_ms
            if earlier_asleep and self._config.sleep_enabled
            else 0
        )
        cursor = datetime.fromtimestamp(detailed_from / 1000).replace(hour=0, minute=0, second=0, microsecond=0)
        cursor -= timedelta(days=1)
        last_date = datetime.fromtimestamp(to_ms / 1000).replace(hour=0, minute=0, second=0, microsecond=0)
        while cursor <= last_date:
            date = day_plan_date(cursor)
            plan = self._read(date) or fallback_day_plan(date, self._config)
            if not plan.sleep_enabled:
                cursor += timedelta(days=1)
                continue
            bedtime_at, wake_at = planned_sleep_window(plan)
            overlap_start = max(detailed_from, bedtime_at)
            overlap_end = min(to_ms, wake_at)
            if overlap_end > overlap_start:
                sleep_ms += overlap_end - overlap_start
            cursor += timedelta(days=1)
        return sleep_ms / HOUR_MS

    def activities_between(self, from_dt: datetime, to_dt: datetime) -> List[str]:
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
        raw = self._store.read_json(_plan_key(date), None)
        if raw is None:
            return None
        raw_str = json.dumps(raw) if not isinstance(raw, str) else raw
        plan = parse_day_plan(raw_str, date, self._config)
        if plan is None:
            plan = self._read_legacy(raw, date)
        return plan

    def _read_generation_result(self, date: str) -> DayPlan | None:
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
        fallback = fallback_day_plan(date, self._config)
        if self._generator is None:
            self._generation_issues.pop(date, None)
            self._store.write_json(_plan_key(date), _plan_to_dict(fallback))
            return fallback
        yesterday = self._read(_previous_date(now))
        prompt = build_plan_prompt(
            date=date, weekday=_weekday_cn(now),
            occasion=_day_occasion(now, self._anniversary_at()),
            persona=self._persona_description(),
            yesterday_theme=yesterday.theme if yesterday else '昨天没有留存计划，不要写得像固定流水线。',
            yesterday_bedtime=yesterday.bedtime_hint if yesterday else '没有记录',
            yesterday_wake=yesterday.wake_hint if yesterday else '没有记录',
            yesterday_carry_over=yesterday.carry_over if yesterday else '没有记录',
            yesterday_avoided='、'.join(activity_avoidance_items(yesterday.slots)) if yesterday else '没有记录；今天没有旧活动需要避开。',
            density=self._interaction_density(int(now.timestamp() * 1000)),
            character_name=self._character_name,
            character_identity=self._character_identity,
            character_boundaries=self._character_boundaries,
            schedule_config=self._config,
        )
        try:
            raw = await self._generator.generate(prompt)
            parsed = parse_day_plan(raw, date, self._config)
            if not parsed:
                retry_prompt = '\n'.join([prompt, '', '你上一次的 JSON 没通过本地结构校验。请从头重新生成完整 JSON。'])
                raw = await self._generator.generate(retry_prompt)
                parsed = parse_day_plan(raw, date, self._config)
            if not parsed:
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
        if not isinstance(raw, dict) or not isinstance(raw.get('slots'), list):
            return None
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
            if not isinstance(item, dict):
                return None
            from_mins = clock_minutes(item.get('from', ''))
            doing = _safe_doing(item.get('doing'))
            mood = _safe_plan_text(item.get('mood'), 1, 40)
            if from_mins is None or from_mins <= previous or not doing or not mood:
                return None
            previous = from_mins
            slots.append(DayPlanSlot(from_time=item['from'], doing=doing, mood=mood))
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
    return {
        'date': plan.date,
        'slots': [{'from': s.from_time, 'doing': s.doing, 'mood': s.mood} for s in plan.slots],
        'bedtimeHint': plan.bedtime_hint,
        'wakeHint': plan.wake_hint,
        'theme': plan.theme,
        'carryOver': plan.carry_over,
        'sleepEnabled': plan.sleep_enabled,
        'bedtimeDayBoundary': plan.bedtime_day_boundary,
    }


def _slot_at(plan: DayPlan, now: datetime) -> DayPlanSlot:
    minutes = now.hour * 60 + now.minute
    current = plan.slots[0]
    for slot in plan.slots:
        from_mins = clock_minutes(slot.from_time)
        if from_mins is not None and from_mins <= minutes:
            current = slot
        else:
            break
    return current


def describe_day_plan(plan: DayPlan, now: datetime, sleep: ScheduleSleepState) -> str:
    if sleep.asleep:
        slot = DayPlanSlot(from_time='00:00', doing='在睡觉', mood='被叫醒时会有些迷迷糊糊')
    else:
        slot = _slot_at(plan, now)
    doing = slot.doing if slot.doing.startswith('你') else f'你{slot.doing}'
    if sleep.just_woke:
        lines = ['你刚醒没多久，还在慢慢把意识拢回来；别装得已经精神十足，语气应有一点迷糊和迟缓。']
    else:
        lines = [f'此刻{doing}。{describe_mood_behavior(slot.mood)}']
    if sleep.asleep:
        lines.append('你已经睡着了；如果他现在找你说话，你是被叫醒的，反应要符合刚醒时的迷糊。')
    elif sleep.drowsy:
        lines.append(f'你开始犯困，原本想在{plan.bedtime_hint}左右休息；语气会带一点"再待十分钟就睡"的困意。')
    return '\n'.join(lines)
