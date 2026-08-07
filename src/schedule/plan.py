"""
每日生成式日程服务。直接移植自 src/core/schedule/plan.ts。

惰性调用 ensure() 才触发模型；读取、描述和历史补叙均不会为过去日期补生成。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional, Protocol

import asyncio
import json
import re

from .daily import SCHEDULE
from src.common.clock import now as current_time
from src.common.logger import get_logger

logger = get_logger(__name__)

PLAN_PREFIX = 'day_plan:'
MAX_HOURS_FOR_HISTORY = 48
MAX_HOURS_FOR_ELAPSED_INTEGRATION = 48
DAY_MS = 24 * 60 * 60_000
HOUR_MS = 60 * 60_000
LATE_BEDTIME_MAX_MINUTES = 2 * 60


@dataclass
class DayPlanSlot:
    from_time: str   # HH:MM
    doing: str
    mood: str


@dataclass
class DayPlan:
    date: str
    slots: list[DayPlanSlot]
    bedtime_hint: str
    wake_hint: str
    theme: str
    carry_over: str


@dataclass
class ScheduleSleepState:
    asleep: bool
    drowsy: bool
    just_woke: bool = False


@dataclass
class DayPlanGenerationIssue:
    kind: str      # 'invalid-output' | 'provider-error'
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


def _format_clock(hour: int) -> str:
    return f'{hour:02d}:00'


def fallback_day_plan(date: str) -> DayPlan:
    return DayPlan(
        date=date,
        slots=[DayPlanSlot(from_time=_format_clock(s.from_hour), doing=s.doing, mood=s.mood)
               for s in SCHEDULE],
        bedtime_hint='23:00',
        wake_hint='07:00',
        theme='今天按熟悉的桌面节奏安静地度过。',
        carry_over='把昨天没收尾的念头慢慢想明白。',
    )


def planned_sleep_window(plan: DayPlan) -> tuple[int, int]:
    """返回 (bedtime_at_ms, wake_at_ms)。"""
    year, month, day = [int(x) for x in plan.date.split('-')]
    bedtime_minutes = clock_minutes(plan.bedtime_hint)
    wake_minutes = clock_minutes(plan.wake_hint)
    if bedtime_minutes is None:
        raise ValueError(f'非法 bedtimeHint：{plan.bedtime_hint}')
    if wake_minutes is None:
        raise ValueError(f'非法 wakeHint：{plan.wake_hint}')
    bedtime_day_offset = 1 if bedtime_minutes <= LATE_BEDTIME_MAX_MINUTES else 0

    # ★ 必须用 timedelta 加天，不能写 datetime(year, month, day + 1)。
    #   TS 的 new Date(y, m, 32) 会自动滚到下个月，Python 的 datetime 直接抛
    #   ValueError: day is out of range for month。
    #   月末最后一天（1/31、2/28…）的日程会在这里炸掉，而 :memory: 测试用的
    #   都是月中日期，永远碰不到。
    base = datetime(year, month, day)
    bedtime_dt = base + timedelta(
        days=bedtime_day_offset, hours=bedtime_minutes // 60, minutes=bedtime_minutes % 60
    )
    wake_dt = base + timedelta(
        days=1, hours=wake_minutes // 60, minutes=wake_minutes % 60
    )
    return (int(bedtime_dt.timestamp() * 1000), int(wake_dt.timestamp() * 1000))


# ─────────────────────────────────────────────────────────────────────
# 安全过滤（移植 safePlanText / safeDoing）
# ─────────────────────────────────────────────────────────────────────

_SENSITIVE_TEXT = re.compile(
    r'密码|口令|验证码|账号|银行卡|支付|金额|工资|客户|聊天记录|私信|@\w+|'
    r'[A-Za-z]:\\|\\\\|/Users/|/home/', re.IGNORECASE
)
_DISALLOWED_ACTION = re.compile(
    r'(?:帮你|替你).{0,12}(?:收|取|寄|拿|买)|'
    r'(?:我|今天|刚刚|已经|正在).{0,16}(?:出门|外出|通勤|上班|逛街|约会|拜访|收快递|取快递)'
)
_POSSIBLE_NAME = re.compile(r'(?:和|跟)[一-鿿]{2,3}(?:一起|聊天|见面)')


def _safe_plan_text(value: Any, min_len: int, max_len: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r'\s+', ' ', value).strip()
    if not min_len <= len(text) <= max_len:
        return None
    if _SENSITIVE_TEXT.search(text) or _DISALLOWED_ACTION.search(text) or _POSSIBLE_NAME.search(text):
        return None
    return text


def _safe_doing(value: Any) -> str | None:
    text = _safe_plan_text(value, 3, 72)
    if not text:
        return None
    normalized = re.sub(r'^我(?!们)[，、：:\s]*', '', text).strip()
    return normalized if len(normalized) >= 3 else None


def activity_avoidance_items(slots: list[DayPlanSlot]) -> list[str]:
    seen: dict[str, None] = {}
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

def parse_day_plan(raw: str, date: str) -> DayPlan | None:
    """模型输出只要有一处不符合生活边界就整体废弃。"""
    try:
        value = json.loads(raw)
    except Exception:
        return None
    if not isinstance(value, dict):
        return None
    if value.get('date') != date:
        return None
    slots_raw = value.get('slots')
    if not isinstance(slots_raw, list) or not (8 <= len(slots_raw) <= 10):
        return None

    slots: list[DayPlanSlot] = []
    previous = -1
    for item in slots_raw:
        if not isinstance(item, dict):
            return None
        from_val = item.get('from')
        if not isinstance(from_val, str):
            return None
        from_mins = clock_minutes(from_val)
        doing = _safe_doing(item.get('doing'))
        mood = _safe_plan_text(item.get('mood'), 2, 40)
        if from_mins is None or from_mins <= previous or not doing or not mood:
            return None
        previous = from_mins
        slots.append(DayPlanSlot(from_time=from_val, doing=doing, mood=mood))

    bedtime_hint = value.get('bedtimeHint', '')
    if not isinstance(bedtime_hint, str) or clock_minutes(bedtime_hint) is None:
        return None
    bedtime_minutes = clock_minutes(bedtime_hint)
    if bedtime_minutes < 21 * 60 and bedtime_minutes > LATE_BEDTIME_MAX_MINUTES:
        return None
    wake_hint = value.get('wakeHint', '')
    if not isinstance(wake_hint, str) or clock_minutes(wake_hint) is None:
        return None
    wake_minutes = clock_minutes(wake_hint)
    bedtime_offset = (bedtime_minutes + 24 * 60) if bedtime_minutes <= LATE_BEDTIME_MAX_MINUTES else bedtime_minutes
    sleep_duration = wake_minutes + 24 * 60 - bedtime_offset
    if not (4 * 60 <= sleep_duration <= 18 * 60):
        return None
    theme = _safe_plan_text(value.get('theme'), 4, 72)
    carry_over = _safe_plan_text(value.get('carryOver'), 4, 72)
    if not theme or not carry_over:
        return None
    return DayPlan(date=date, slots=slots, bedtime_hint=bedtime_hint,
                   wake_hint=wake_hint, theme=theme, carry_over=carry_over)


# ─────────────────────────────────────────────────────────────────────
# 日程描述（注入 prompt）
# ─────────────────────────────────────────────────────────────────────

def describe_mood_behavior(mood: str) -> str:
    if re.search(r'烦躁|恼火|焦躁|憋着劲', mood):
        return f'你现在{mood}：说话会更短，少绕弯子，但不会冲他发脾气；如果被追问，先坦白自己有点卡住。'
    if re.search(r'无聊|发闷|提不起劲', mood):
        return f'你现在{mood}：会主动抛一个小话题，句子松一点，也更容易顺着他的回答临时起意。'
    if re.search(r'低落|难过|失落|沮丧|发空', mood):
        return f'你现在{mood}：语速会慢下来，措辞更轻，不强装活泼；仍会认真回应，只是不急着把话题撑满。'
    if re.search(r'兴奋|雀跃|开心|轻快', mood):
        return f'你现在{mood}：句子会更有跳跃感，愿意多分享一个刚想到的细节，但不会连续轰炸式说个不停。'
    if re.search(r'期待|盼着|好奇', mood):
        return f'你现在{mood}：会更主动追问具体细节，措辞里藏不住一点盼头，也会给他留出回答的余地。'
    if re.search(r'苦恼|纠结|犯难|忐忑', mood):
        return f'你现在{mood}：会斟酌措辞，偶尔停一下再补充，愿意把卡住的地方说清楚，而不是假装已经想通。'
    intense = bool(re.search(r'很|特别|格外|强烈|莫名|极', mood))
    positive = bool(re.search(r'放松|平和|释然|温柔|满足|愉快|专注', mood))
    if positive:
        speed = '语速会稍快一些' if intense else '语速自然从容'
        return f'你现在{mood}：{speed}，句子保持完整，会顺着当前话题回应并留意他的感受。'
    return f'你现在{mood}：先用一句话说清当下感受，再围绕当前话题慢慢组织措辞；走神时会坦白停顿，不拿空泛情绪敷衍他。'


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
    labels: list[str] = []
    if month_day in fixed:
        labels.append(fixed[month_day])
    if anniversary_at > 0:
        ann = datetime.fromtimestamp(anniversary_at / 1000)
        if ann.month == now.month and ann.day == now.day:
            labels.append('认识纪念日')
    return '、'.join(labels) if labels else '没有特别节日'


def build_plan_prompt(date: str, weekday: str, occasion: str, persona: str,
                      yesterday_theme: str, yesterday_bedtime: str, yesterday_wake: str,
                      yesterday_carry_over: str, yesterday_avoided: str, density: str) -> str:
    return '\n'.join([
        '你替月璃安排一天。她是住在电脑桌面上的陪伴角色，但这张表是她自己的生活，不是等用户出现的值班表。',
        '只返回合法 JSON，不要 Markdown 或解释。',
        f'日期：{date}，{weekday}；特别日：{occasion}。',
        '', '# 她最近的状态', persona, '',
        f'昨天主题：{yesterday_theme}',
        f'昨天睡眠倾向：{yesterday_bedtime} 入睡，{yesterday_wake} 醒来。',
        f'昨天留下的小事：{yesterday_carry_over}',
        f'昨天已经做过、今天要避开重复：{yesterday_avoided}',
        f'最近互动：{density}', '',
        '输出结构：',
        '{"date":"YYYY-MM-DD","slots":[{"from":"HH:MM","doing":"省略主语的日常片段","mood":"会落到说话上的心情"}],'
        '"bedtimeHint":"HH:MM","wakeHint":"HH:MM","theme":"一句话主题","carryOver":"今天明确接着做的一件小事"}',
        '', '生活感：',
        '- 一天要有松紧：认真做点东西，也会走神、犯懒、卡住或临时换主意。不要排成自律博主的打卡清单。',
        '- 活动要具体，但不用每段都设计成能主动搭话的话题。她有些事只是自己想做。',
        '- mood 写会怎样影响措辞和反应，允许烦、闷、没精神或想独处；不要八段都温柔愉快。',
        '- theme 像她今天心里牵着的一根线，不要写口号。carryOver 留一件真的没做完、明天还能接上的小事。',
        '', '结构与边界：',
        '- slots 为 8 到 10 段，from 严格升序，覆盖早晚但不用机械整点；不要让一件事空泛地持续三小时。',
        '- slots 数组里的每一段必须是独立 JSON 对象，每个对象只能各有一个 from、doing、mood，禁止在同一对象里重复键。',
        '- bedtimeHint 必须在 21:00 到次日 02:00 之间；wakeHint 是次日倾向醒来的时间；它们都不是硬命令。',
        '- bedtimeHint 和 wakeHint 必须作为一组生活节奏一起设计：熬夜配晚起、早睡配早起；两者之间保留 4 到 18 小时。',
        '- 连续多天必须有明显方差，不能每天都挤在 23 点前后。',
        '- 月璃没有现实身体，不能替他收快递、操作现实物品、出门，或声称已经改变了现实世界。',
        '- doing 要省略「我/她/月璃」这类主语，写清正在琢磨什么、做到哪儿或为什么停住。',
        '- carryOver 不能为空，且今天至少一个 doing 要确实接上昨天留下的小事。',
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
    ) -> None:
        self._store = store
        self._persona_description = persona_description
        self._interaction_density = interaction_density
        self._anniversary_at = anniversary_at
        self._energy = energy
        self._last_interaction_at = last_interaction_at
        self._generator = generator
        self._inflight: Dict[str, asyncio.Task[DayPlan]] = {}
        self._generation_issues: dict[str, DayPlanGenerationIssue] = {}

    def get(self, now: int | None = None) -> DayPlan:
        now = now if now is not None else current_time()
        date = day_plan_date(now)
        return self._read(date) or fallback_day_plan(date)

    async def ensure(self, now: int | None = None) -> DayPlan:
        now = now if now is not None else current_time()
        date = day_plan_date(now)
        existing = self._read(date)
        if existing:
            return existing
        return await self._start_generation(date, now)

    def ensure_background(self, now: int | None = None) -> DayPlan:
        """当天计划缺失时立即返回备用计划，并在后台生成真实计划。"""
        now = now if now is not None else current_time()
        date = day_plan_date(now)
        existing = self._read(date)
        if existing:
            return existing
        self._start_generation(date, now)
        return fallback_day_plan(date)

    def _start_generation(self, date: str, now: int) -> asyncio.Task[DayPlan]:
        existing = self._inflight.get(date)
        if existing is not None:
            return existing
        task = asyncio.create_task(
            self._generate(date, datetime.fromtimestamp(now / 1000))
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

    def sleep_inputs(self, now: int | None = None) -> dict[str, Any]:
        now = now if now is not None else current_time()
        current_dt = datetime.fromtimestamp(now / 1000)
        today = self.get(now)
        yesterday_date = _previous_date(current_dt)
        yesterday = self._read(yesterday_date) or fallback_day_plan(yesterday_date)
        yesterday_wake = planned_sleep_window(yesterday)[1]
        today_bedtime = planned_sleep_window(today)[0]
        cycle_boundary = yesterday_wake + (today_bedtime - yesterday_wake) // 2
        plan = yesterday if now < cycle_boundary else today
        return {
            'date': plan.date, 'bedtime_hint': plan.bedtime_hint, 'wake_hint': plan.wake_hint,
            'energy': self._energy(), 'last_interaction_at': self._last_interaction_at(),
        }

    def sleep_hours_between(self, from_ms: int, to_ms: int, earlier_asleep: bool = False) -> float:
        if to_ms <= from_ms:
            return 0.0
        detailed_from = max(from_ms, to_ms - MAX_HOURS_FOR_ELAPSED_INTEGRATION * HOUR_MS)
        sleep_ms = (detailed_from - from_ms) if earlier_asleep else 0
        cursor = datetime.fromtimestamp(detailed_from / 1000).replace(hour=0, minute=0, second=0, microsecond=0)
        cursor -= timedelta(days=1)
        last_date = datetime.fromtimestamp(to_ms / 1000).replace(hour=0, minute=0, second=0, microsecond=0)
        while cursor <= last_date:
            date = day_plan_date(cursor)
            plan = self._read(date) or fallback_day_plan(date)
            bedtime_at, wake_at = planned_sleep_window(plan)
            overlap_start = max(detailed_from, bedtime_at)
            overlap_end = min(to_ms, wake_at)
            if overlap_end > overlap_start:
                sleep_ms += overlap_end - overlap_start
            cursor += timedelta(days=1)
        return sleep_ms / HOUR_MS

    def activities_between(self, from_dt: datetime, to_dt: datetime) -> list[str]:
        out: list[str] = []
        cursor = from_dt.replace(minute=0, second=0, microsecond=0)
        last_key = ''
        for _ in range(MAX_HOURS_FOR_HISTORY):
            if cursor > to_dt:
                break
            plan = self._read(day_plan_date(cursor)) or fallback_day_plan(day_plan_date(cursor))
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
        plan = parse_day_plan(raw_str, date)
        if plan is None:
            plan = self._read_legacy(raw, date)
        return plan

    async def _generate(self, date: str, now: datetime) -> DayPlan:
        fallback = fallback_day_plan(date)
        if self._generator is None:
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
            yesterday_carry_over=yesterday.carry_over if yesterday else '没有记录；今天要先埋下一件明天想接着做的小事。',
            yesterday_avoided='、'.join(activity_avoidance_items(yesterday.slots)) if yesterday else '没有记录；今天没有旧活动需要避开。',
            density=self._interaction_density(int(now.timestamp() * 1000)),
        )
        try:
            raw = await self._generator.generate(prompt)
            parsed = parse_day_plan(raw, date)
            if not parsed:
                retry_prompt = '\n'.join([prompt, '', '你上一次的 JSON 没通过本地结构校验。请从头重新生成完整 JSON。'])
                raw = await self._generator.generate(retry_prompt)
                parsed = parse_day_plan(raw, date)
            if not parsed:
                self._generation_issues[date] = DayPlanGenerationIssue(kind='invalid-output', raw=raw)
                logger.error('日程生成结果未通过结构校验', date=date)
            else:
                self._generation_issues.pop(date, None)
            plan = parsed or fallback
            self._store.write_json(_plan_key(date), _plan_to_dict(plan))
            return plan
        except Exception as exc:
            self._generation_issues[date] = DayPlanGenerationIssue(kind='provider-error', reason=str(exc))
            logger.error('日程生成失败', date=date, error=str(exc))
            self._store.write_json(_plan_key(date), _plan_to_dict(fallback))
            return fallback

    def _read_legacy(self, raw: Any, date: str) -> DayPlan | None:
        if not isinstance(raw, dict) or not isinstance(raw.get('slots'), list):
            return None
        slots_raw = raw['slots']
        if not (4 <= len(slots_raw) <= 10):
            return None
        slots: list[DayPlanSlot] = []
        previous = -1
        for item in slots_raw:
            if not isinstance(item, dict):
                return None
            from_mins = clock_minutes(item.get('from', ''))
            doing = _safe_doing(item.get('doing'))
            mood = _safe_plan_text(item.get('mood'), 2, 40)
            if from_mins is None or from_mins <= previous or not doing or not mood:
                return None
            previous = from_mins
            slots.append(DayPlanSlot(from_time=item['from'], doing=doing, mood=mood))
        bedtime_hint = raw.get('bedtimeHint', '23:00')
        if not isinstance(bedtime_hint, str) or clock_minutes(bedtime_hint) is None:
            bedtime_hint = '23:00'
        wake_hint = raw.get('wakeHint', '07:00')
        if not isinstance(wake_hint, str) or clock_minutes(wake_hint) is None:
            wake_hint = '07:00'
        theme = _safe_plan_text(raw.get('theme'), 4, 72)
        if not theme:
            return None
        carry_over = _safe_plan_text(raw.get('carryOver'), 4, 72) or '把昨天没收尾的念头慢慢想明白。'
        return DayPlan(date=date, slots=slots, bedtime_hint=bedtime_hint,
                       wake_hint=wake_hint, theme=theme, carry_over=carry_over)


def _plan_to_dict(plan: DayPlan) -> dict[str, Any]:
    return {
        'date': plan.date,
        'slots': [{'from': s.from_time, 'doing': s.doing, 'mood': s.mood} for s in plan.slots],
        'bedtimeHint': plan.bedtime_hint,
        'wakeHint': plan.wake_hint,
        'theme': plan.theme,
        'carryOver': plan.carry_over,
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
