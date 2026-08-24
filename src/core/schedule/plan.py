"""生成并读取「今天的方向」，实际生活状态由活动时间线承载。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Protocol

import asyncio
import json
import re
import sqlite3

from src.core.common.clock import now as current_time
from src.core.common.logger import get_logger
from src.core.config.schema import ScheduleConfig
from src.core.llm_models.snapshot import bind_render_params
from src.core.persona.state import (
    ElapsedEffect,
    EnergyTier,
    MoodTier,
    PersonaState,
    describe_persona_for_planning,
    energy_tier,
    mood_tier,
)
from src.core.prompts.registry import get_prompt
from src.core.schedule.timeline import (
    ActivityDecisionContext,
    ActivityDecisionService,
    ActivityGenerator,
    ActivityTimeline,
)

logger = get_logger(__name__)

PLAN_PREFIX = 'day_plan:'

_CLOCK_TIME = re.compile(
    r'\d{1,2}\s*[:：]\s*\d{2}|\d{1,2}\s*点(?:半|钟)?'
)
_SENSITIVE_TEXT = re.compile(
    r'密码|口令|验证码|账号|银行卡|支付|金额|工资|客户|聊天记录|私信|@\w+|'
    r'[A-Za-z]:\\|\\\\|/Users/|/home/',
    re.IGNORECASE,
)
_ACTIVITY_QUESTION = re.compile(
    r'干嘛|干什么|干啥|做什么|做啥|忙什么|忙啥|忙不忙|在忙|在干'
)


@dataclass(frozen=True)
class DayPlanIntention:
    """今天想做成的一件主线事项，以及已经跨日滚动的天数。"""

    what: str
    carried_days: int = 0


@dataclass(frozen=True)
class DayPlan:
    """不带钟点和活动事实的当日方向。"""

    date: str
    theme: str
    intentions: List[DayPlanIntention]
    rough_rhythm: str


@dataclass(frozen=True)
class ScheduleSleepState:
    """活动时间线为对话描述提供的休息状态。"""

    asleep: bool
    just_woke: bool = False
    resting: bool = False


@dataclass(frozen=True)
class DayPlanGenerationIssue:
    """最近一次方向生成失败的可观测信息。"""

    kind: str
    attempted_at: int
    raw: str | None = None
    reason: str | None = None


class _DayPlanStore(Protocol):
    """方向持久化所需的最小 JSON 存储协议。"""

    def read_json(self, key: str, fallback: Any) -> Any:
        """读取键值；键不存在时返回调用方给出的值。"""

        ...

    def write_json(self, key: str, value: Any) -> None:
        """写入 JSON 兼容值。"""

        ...


def day_plan_date(now: int | datetime) -> str:
    """把本地毫秒时间戳或日期时间转换成 ``YYYY-MM-DD``。"""

    if isinstance(now, int):
        now = datetime.fromtimestamp(now / 1000)
    return now.strftime('%Y-%m-%d')


def _plan_key(date: str) -> str:
    """返回一份当日方向在 meta 存储中的固定键。"""

    return f'{PLAN_PREFIX}{date}'


def _previous_date(now: datetime) -> str:
    """返回目标日期的前一天。"""

    return day_plan_date(now - timedelta(days=1))


def _weekday_cn(now: datetime) -> str:
    """返回目标日期的中文星期名称。"""

    return ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][now.weekday()]


def _day_occasion(now: datetime, anniversary_at: int) -> str:
    """收集固定节日与相识纪念日标签。"""

    fixed = {
        '01-01': '元旦',
        '02-14': '情人节',
        '05-01': '劳动节',
        '10-01': '国庆节',
        '12-25': '圣诞节',
    }
    labels: List[str] = []
    label = fixed.get(now.strftime('%m-%d'))
    if label is not None:
        labels.append(label)
    if anniversary_at > 0:
        anniversary = datetime.fromtimestamp(anniversary_at / 1000)
        if (anniversary.month, anniversary.day) == (now.month, now.day):
            labels.append('认识纪念日')
    return '、'.join(labels) if labels else '没有特别节日'


def _safe_plan_text(value: Any, minimum: int, maximum: int) -> str | None:
    """折叠空白并拒绝超长、敏感或非字符串规划文本。"""

    if not isinstance(value, str):
        return None
    text = re.sub(r'\s+', ' ', value).strip()
    if not minimum <= len(text) <= maximum or _SENSITIVE_TEXT.search(text):
        return None
    return text


def fallback_day_plan(
    date: str,
    config: ScheduleConfig | None = None,
) -> DayPlan:
    """在模型不可用时返回不含钟点的中性方向，不生成任何活动事实。"""

    settings = config or ScheduleConfig()
    return DayPlan(
        date=date,
        theme=settings.fallback_theme,
        intentions=[
            DayPlanIntention('推进今天最想做成的一件事'),
            DayPlanIntention('给一直拖着的事情做个取舍'),
            DayPlanIntention('留一点时间给真正感兴趣的东西'),
        ],
        rough_rhythm='今天顺着实际状态调整节奏',
    )


def parse_day_plan(
    raw: str,
    date: str,
    config: ScheduleConfig | None = None,
) -> DayPlan | None:
    """严格解析新方向结构；旧 slots 结构和任何规划钟点整体判无效。"""

    # 保留 config 参数以维持调度解析接口一致；新形态的结构边界不再由时段配置决定。
    del config
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or value.get('date') != date:
        return None
    if 'slots' in value or 'bedtimeHint' in value or 'wakeHint' in value:
        return None
    theme = _safe_plan_text(value.get('theme'), 2, 72)
    rough_rhythm = _safe_plan_text(value.get('roughRhythm'), 2, 72)
    intentions_raw = value.get('intentions')
    if (
        theme is None
        or rough_rhythm is None
        or _CLOCK_TIME.search(rough_rhythm)
        or not isinstance(intentions_raw, list)
        or not 3 <= len(intentions_raw) <= 5
    ):
        return None
    intentions: List[DayPlanIntention] = []
    for item in intentions_raw:
        if not isinstance(item, dict):
            return None
        what = _safe_plan_text(item.get('what'), 2, 72)
        carried_days = item.get('carriedDays')
        if (
            what is None
            or _CLOCK_TIME.search(what)
            or not isinstance(carried_days, int)
            or isinstance(carried_days, bool)
            or not 0 <= carried_days <= 3650
        ):
            return None
        intentions.append(
            DayPlanIntention(what=what, carried_days=carried_days)
        )
    return DayPlan(
        date=date,
        theme=theme,
        intentions=intentions,
        rough_rhythm=rough_rhythm,
    )


def _plan_to_dict(plan: DayPlan) -> Dict[str, Any]:
    """把方向对象序列化为唯一的新持久化形态。"""

    return {
        'date': plan.date,
        'theme': plan.theme,
        'intentions': [
            {
                'what': intention.what,
                'carriedDays': intention.carried_days,
            }
            for intention in plan.intentions
        ],
        'roughRhythm': plan.rough_rhythm,
    }


def build_plan_prompt(
    *,
    date: str,
    weekday: str,
    occasion: str,
    persona: str,
    yesterday_theme: str,
    unfinished_intentions: str,
    density: str,
    character_name: str,
    character_personality: str,
    schedule_config: ScheduleConfig | None = None,
    render_params: dict[str, dict[str, str]] | None = None,
) -> str:
    """渲染只规划 theme、intentions 与 roughRhythm 的日方向提示词。"""

    settings = schedule_config or ScheduleConfig()
    sleep_rule = (
        '实际活动允许自然选择睡觉；roughRhythm 只写作息感觉，不能写钟点。'
        if settings.sleep_enabled
        else '当前不允许实际活动进入睡眠；roughRhythm 只写节奏感觉，不要承诺睡觉时刻。'
    )
    values = {
        'character_name': character_name,
        'date': date,
        'weekday': weekday,
        'occasion': occasion,
        'character_personality': character_personality,
        'persona': persona,
        'yesterday_theme': yesterday_theme,
        'unfinished_intentions': unfinished_intentions,
        'density': density,
        'sleep_rule': sleep_rule,
    }
    if render_params is not None:
        render_params['schedule'] = values
    return get_prompt('schedule').render(**values)


def asks_about_activity(text: str) -> bool:
    """判断本回合是否在问 Bot 此刻实际做什么。"""

    return bool(_ACTIVITY_QUESTION.search(text))


def describe_mood_behavior(mood: str) -> str:
    """把活动情绪落点转换成最小行为提示。"""

    return f'你此刻的状态是「{mood}」。让它自然影响反应，具体表达仍服从你的人设。'


def _energy_behavior(state: PersonaState) -> str:
    """把精力档转换为当前回合的最小行为提示。"""

    tier = energy_tier(state)
    if tier is EnergyTier.HIGH:
        return '现在整体精神很好，反应可以更轻快，但不用因此变得吵闹'
    if tier is EnergyTier.TIRED:
        return '现在有点累，反应可以稍短、稍慢，但不要反复宣告困倦'
    if tier is EnergyTier.SPENT:
        return '现在已经精疲力尽，反应要明显简短迟缓，但不要自动催对方睡觉'
    return ''


def _mood_behavior(state: PersonaState) -> str:
    """把非平稳心情档转换为当前回合的最小行为提示。"""

    tier = mood_tier(state)
    if tier is MoodTier.GOOD:
        return '现在心情不错，可以自然流露一点期待感，但不要无缘无故持续兴奋'
    if tier is MoodTier.LOW:
        return '现在心情偏低，反应可以更收着些，但不要每句话都重复低落'
    return ''


class DayPlanService:
    """维护当天方向，并把活动决策、描述、历史和状态积分接到同一时间线。"""

    def __init__(
        self,
        store: _DayPlanStore,
        persona_state: Callable[[], PersonaState],
        interaction_density: Callable[[int], str],
        anniversary_at: Callable[[], int],
        last_interaction_at: Callable[[], int | None],
        character_name: str,
        character_personality: str,
        generator: ActivityGenerator | None = None,
        schedule_config: ScheduleConfig | None = None,
        *,
        db: sqlite3.Connection | None = None,
        timeline: ActivityTimeline | None = None,
        activity_generator: ActivityGenerator | None = None,
    ) -> None:
        """装配方向生成和活动决策依赖；不在构造期读库或调用模型。"""

        if timeline is None:
            if db is None:
                raise ValueError('DayPlanService 必须显式传入 db 或 timeline')
            timeline = ActivityTimeline(db)
        self._store = store
        self._persona_state = persona_state
        self._interaction_density = interaction_density
        self._anniversary_at = anniversary_at
        self._last_interaction_at = last_interaction_at
        self._character_name = character_name
        self._character_personality = character_personality
        self._generator = generator
        self._config = schedule_config or ScheduleConfig()
        self._timeline = timeline
        self._inflight: Dict[str, asyncio.Task[DayPlan]] = {}
        self._generation_issues: Dict[str, DayPlanGenerationIssue] = {}
        if activity_generator is not None:
            decision = ActivityDecisionService(
                activity_generator,
                self.activity_decision_context,
            )
            self._timeline.set_decider(decision.decide)

    @property
    def timeline(self) -> ActivityTimeline:
        """返回服务绑定的唯一活动时间线。"""

        return self._timeline

    def get(self, now: int | None = None) -> DayPlan:
        """只读当天有效方向；旧结构或缺失时返回不含活动事实的中性方向。"""

        now = now if now is not None else current_time()
        date = day_plan_date(now)
        return self._read(date) or fallback_day_plan(date, self._config)

    async def ensure(self, now: int | None = None) -> DayPlan:
        """确保当天方向存在；同日期的并发调用共享同一个生成任务。"""

        now = now if now is not None else current_time()
        date = day_plan_date(now)
        existing = self._read_generation_result(date)
        if existing is not None:
            return existing
        if self._generation_is_cooling_down(date, now):
            return fallback_day_plan(date, self._config)
        return await self._start_generation(date, now)

    def ensure_background(self, now: int | None = None) -> DayPlan:
        """方向缺失时立即返回中性方向，并在事件循环中启动生成。"""

        now = now if now is not None else current_time()
        date = day_plan_date(now)
        existing = self._read_generation_result(date)
        if existing is not None:
            return existing
        if not self._generation_is_cooling_down(date, now):
            self._start_generation(date, now)
        return fallback_day_plan(date, self._config)

    def describe(
        self,
        now: int,
        sleep: ScheduleSleepState,
        *,
        include_activity: bool = False,
    ) -> str:
        """从真实当前活动构造对话行为提示，全天方向不冒充正在发生的事。"""

        activity = self._timeline.current(now)
        state = self._persona_state()
        if sleep.just_woke:
            lines = [
                '你刚醒没多久，还在慢慢把意识拢回来；别装得已经精神十足，'
                '语气应有一点迷糊和迟缓。'
            ]
        else:
            current_behavior = describe_mood_behavior(activity.mood).rstrip('。')
            energy_behavior = (
                '' if sleep.asleep or sleep.resting else _energy_behavior(state)
            )
            mood_behavior = '' if sleep.asleep else _mood_behavior(state)
            if energy_behavior:
                current_behavior = f'{current_behavior}；{energy_behavior}'
            if mood_behavior:
                current_behavior = f'{current_behavior}；{mood_behavior}'
            variance = self.plan_variance(now)
            if variance is not None:
                # 偏离感知与此刻状态合并成同一句；不另起重复的系统段，也不要求纠正。
                current_behavior = f'{current_behavior}；{variance}'
            lines = [f'{current_behavior}。']
            if include_activity:
                doing = activity.doing
                lines.append(
                    f'如果他是在问你在干什么：此刻你在{doing}。照实答一句就够，'
                    '不用展开讲，也不要顺势宣告接下来要做什么。'
                )
        if sleep.asleep:
            lines.append('你已经睡着了；现在有人找你时，是外部消息把你叫醒。')
        elif sleep.resting:
            lines.append('你正在休息，精力不会继续下降，但仍然清醒并会正常回应。')
        return '\n'.join(lines)

    def integrate_between(
        self,
        from_ms: int,
        to_ms: int,
        *_unused: Any,
    ) -> ElapsedEffect:
        """把人格结算直接委托给真实活动时间线。"""

        return self._timeline.integrate_between(from_ms, to_ms)

    def activities_between(self, from_dt: datetime, to_dt: datetime) -> List[str]:
        """从真实活动日志回忆指定区间，而不是反查当时计划。"""

        activities = self._timeline.between(
            int(from_dt.timestamp() * 1000),
            int(to_dt.timestamp() * 1000),
        )
        return [
            f'{datetime.fromtimestamp(activity.started_at / 1000):%H点%M分}{activity.doing}'
            for activity in activities
        ]

    def generation_issue(self, now: int) -> DayPlanGenerationIssue | None:
        """返回指定日期最近一次方向生成问题。"""

        return self._generation_issues.get(day_plan_date(now))

    def intention_progress(self, now: int) -> List[Dict[str, Any]]:
        """对照当天方向与真实 advances，返回可观测的逐条推进状态。"""

        plan = self.get(now)
        advanced = self._timeline.advanced_intention_indexes(plan.date)
        return [
            {
                'index': index,
                'what': intention.what,
                'carriedDays': intention.carried_days,
                'advanced': index in advanced,
            }
            for index, intention in enumerate(plan.intentions, start=1)
        ]

    def plan_variance(self, now: int) -> str | None:
        """当天过大半后至多指出一条尚未推进的方向，不把偏离当成故障。"""

        current_dt = datetime.fromtimestamp(now / 1000)
        day_start = current_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        progress = (current_dt - day_start) / (day_end - day_start)
        if progress < 0.6:
            return None
        unfinished = next(
            (
                item
                for item in self.intention_progress(now)
                if not item['advanced']
            ),
            None,
        )
        if unfinished is None:
            return None
        return (
            f'你知道今天本来想「{unfinished["what"]}」，到现在还没碰；'
            '这只是你自己察觉到了偏离，不代表必须立刻改回计划'
        )

    def activity_decision_context(self, now: int) -> ActivityDecisionContext:
        """组合下一步活动真正需要的连续状态与当日方向。"""

        plan = self.get(now)
        advanced = self._timeline.advanced_intention_indexes(plan.date)
        current_activity = self._timeline.current(now)
        intention_lines: List[str] = []
        for index, intention in enumerate(plan.intentions, start=1):
            if current_activity.advances == index:
                progress = '在做'
            elif index in advanced:
                progress = '推进过'
            else:
                progress = '还没'
            carried = (
                f'，已经滚了 {intention.carried_days} 天'
                if intention.carried_days > 0
                else ''
            )
            intention_lines.append(
                f'{index}. {intention.what}（{progress}{carried}）'
            )
        last_interaction = self._last_interaction_at()
        if last_interaction is None:
            interaction = '还没有互动记录'
        else:
            elapsed_minutes = max(0, (now - last_interaction) // 60_000)
            interaction = f'他 {elapsed_minutes} 分钟前还在跟你说话'
        state = self._persona_state()
        return ActivityDecisionContext(
            character_name=self._character_name,
            character_personality=self._character_personality,
            persona=(
                f'精力 {state.energy:.0f}，心情 {state.mood:.0f}。'
                f'{describe_persona_for_planning(state)}'
            ),
            sleep_history=self._timeline.last_sleep_summary(now),
            intentions='\n'.join(intention_lines),
            intention_count=len(plan.intentions),
            rough_rhythm=plan.rough_rhythm,
            recent_activities=self._timeline.recent_summary(now),
            interaction=interaction,
            sleep_enabled=self._config.sleep_enabled,
        )

    def _unfinished_intentions(
        self,
        plan: DayPlan | None,
    ) -> List[DayPlanIntention]:
        """筛出前一天没有被任何真实活动推进的意向，并把滚动天数加一。"""

        if plan is None:
            return []
        advanced = self._timeline.advanced_intention_indexes(plan.date)
        return [
            DayPlanIntention(
                what=intention.what,
                carried_days=intention.carried_days + 1,
            )
            for index, intention in enumerate(plan.intentions, start=1)
            if index not in advanced
        ]

    @staticmethod
    def _unfinished_prompt(intentions: List[DayPlanIntention]) -> str:
        """把未完成意向和真实滚动天数渲染成模型可取舍的清单。"""

        if not intentions:
            return '没有；昨天想做的都推进过，或没有有效的新结构方向。'
        return '\n'.join(
            f'{index}. {intention.what}（已经滚了 {intention.carried_days} 天）'
            for index, intention in enumerate(intentions, start=1)
        )

    def _read(self, date: str) -> DayPlan | None:
        """只读当前结构；旧 slots 计划明确失效并等待当天重新生成。"""

        raw = self._store.read_json(_plan_key(date), None)
        if raw is None:
            return None
        raw_text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        return parse_day_plan(raw_text, date, self._config)

    def _read_generation_result(self, date: str) -> DayPlan | None:
        """区分真实生成结果和模型启用时持久化过的中性方向。"""

        plan = self._read(date)
        if (
            self._generator is not None
            and plan == fallback_day_plan(date, self._config)
        ):
            return None
        return plan

    def _generation_is_cooling_down(self, date: str, now: int) -> bool:
        """判断最近生成失败是否仍处于明确的重试冷却期。"""

        issue = self._generation_issues.get(date)
        return (
            issue is not None
            and now < issue.attempted_at
            + self._config.generation_retry_interval_minutes * 60_000
        )

    def _start_generation(self, date: str, now: int) -> asyncio.Task[DayPlan]:
        """创建或复用一个日期唯一的方向生成任务。"""

        existing = self._inflight.get(date)
        if existing is not None:
            return existing
        task = asyncio.create_task(
            self._generate(date, datetime.fromtimestamp(now / 1000), now)
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
        """清理日期任务登记并消费后台异常。"""

        if self._inflight.get(date) is task:
            self._inflight.pop(date, None)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.exception('方向后台生成任务异常', date=date)

    async def _generate(
        self,
        date: str,
        now: datetime,
        attempted_at: int,
    ) -> DayPlan:
        """生成、严格复验并保存当天方向；错误只形成可观测问题。"""

        fallback = fallback_day_plan(date, self._config)
        if self._generator is None:
            self._generation_issues.pop(date, None)
            self._store.write_json(_plan_key(date), _plan_to_dict(fallback))
            return fallback
        yesterday = self._read(_previous_date(now))
        unfinished = self._unfinished_intentions(yesterday)
        render_params: dict[str, dict[str, str]] = {}
        prompt = build_plan_prompt(
            date=date,
            weekday=_weekday_cn(now),
            occasion=_day_occasion(now, self._anniversary_at()),
            persona=describe_persona_for_planning(self._persona_state()),
            yesterday_theme=(
                yesterday.theme
                if yesterday is not None
                else '昨天没有有效的新结构方向，不要据旧时刻表续写。'
            ),
            unfinished_intentions=self._unfinished_prompt(unfinished),
            density=self._interaction_density(int(now.timestamp() * 1000)),
            character_name=self._character_name,
            character_personality=self._character_personality,
            schedule_config=self._config,
            render_params=render_params,
        )
        try:
            bind_render_params(render_params)
            raw = await self._generator.generate(prompt)
            parsed = parse_day_plan(raw, date, self._config)
            if parsed is None:
                retry_prompt = '\n'.join([
                    prompt,
                    '',
                    '上一次 JSON 没通过本地结构或钟点校验。请从头生成完整 JSON。',
                ])
                bind_render_params(render_params)
                raw = await self._generator.generate(retry_prompt)
                parsed = parse_day_plan(raw, date, self._config)
            if parsed is None:
                self._generation_issues[date] = DayPlanGenerationIssue(
                    kind='invalid-output',
                    attempted_at=attempted_at,
                    raw=raw,
                )
                logger.error(
                    '方向生成结果未通过结构校验',
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
            logger.error('方向生成失败', date=date, error=str(exc))
            return fallback
