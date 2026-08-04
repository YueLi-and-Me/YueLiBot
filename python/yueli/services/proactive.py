"""
主动感知与打扰编排。对应 docs/python-rework.md 里的
services/proactive.py（← main/proactive.ts ProactiveGate）。

职责比单纯的"gate"更宽：吃前台事件、维护睡眠状态机、缓存视觉上下文、
跑后台轮询驱动就寝/起床过渡与空闲态主动搭话。所有决策都委托给已经
单测覆盖的纯函数（classify / budget / sleep），这里只负责编排和状态持有。
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

import asyncio

from yueli.api.ws import push
from yueli.awareness.budget import (
    InterruptContext, ProactiveState, after_speak, decide, decide_scene, describe_budget, initial_state,
)
from yueli.awareness.classify import Classified, ForegroundInfo, classify, describe_activity
from yueli.awareness.monitor import ForegroundProcessMonitor
from yueli.awareness.sleep import SleepInputs, SleepStateController
from yueli.common.clock import now as current_time
from yueli.common.logger import get_logger
from yueli.schedule.plan import day_plan_date, fallback_day_plan
from yueli.services.trace import trace

if TYPE_CHECKING:
    from yueli.services.vision import VisionProvider, VisionService

logger = get_logger(__name__)

POLL_INTERVAL_S = 60.0


class AwarenessService:
    """吃前台/截图事件，驱动睡眠状态与主动搭话决策；注册进 lifecycle。"""

    def __init__(self, chat: Any, schedule: Any | None, cfg: Any,
                 vision_provider: VisionProvider | None = None) -> None:
        self.chat = chat
        self._schedule = schedule
        self._cfg = cfg
        self._vision_provider = vision_provider

        self._monitor = ForegroundProcessMonitor()
        self._sleep = SleepStateController(input_source=self._sleep_inputs, state_store=chat.memory)
        self._budget: ProactiveState = initial_state(current_time())
        self._vision: VisionService | None = None

        self._last_classified: Classified | None = None
        self._last_activity_since: int = current_time()
        self._last_visible = True
        self._last_pushed_asleep: bool | None = None
        self._vision_spoke_count = 0

        self._poll_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------ 依赖注入回调

    def _sleep_inputs(self, now: int) -> SleepInputs:
        date = day_plan_date(now)
        plan = self._schedule.get(now) if self._schedule else fallback_day_plan(date)
        return SleepInputs(
            date=plan.date, bedtime_hint=plan.bedtime_hint, wake_hint=plan.wake_hint,
            energy=self.chat.persona.get().energy,
            last_interaction_at=self.chat.memory.last_message_at(),
        )

    def _activity_text(self) -> str:
        """注入给 ChatService 的情境文本。

        ★ 视觉描述从这里进正常对话：ChatService 通过 set_activity_provider
          拿情境，所以只要在这儿把描述拼进去，chat.py 完全不需要知道 vision
          的存在。此前视觉描述只在主动搭话路径被消费，用户打字聊天时她手里
          只有「他在写代码」这种粗粒度标签——等于配了眼睛却没接上。
        """
        if not self._last_classified:
            return ''
        minutes = max(0, (current_time() - self._last_activity_since) // 60_000)
        return self._with_vision(describe_activity(self._last_classified, minutes))

    def _with_vision(self, situation: str) -> str:
        """把最近一次屏幕描述拼到情境文本上。

        ★ 视觉功能开着却没拿到描述时，必须明说「这会儿看不到」，不能什么都不说。
          什么都不说的后果实测过：她会从对话历史和旧记忆里翻出以前看到的界面，
          当成现在的屏幕讲得有鼻子有眼。没看到就说没看到，比编一个强。
        """
        if not self._vision:
            return situation
        description = self._vision.chat_glance()
        if not description:
            return f'{situation}\n（你这会儿看不到他的屏幕。他要是问起，就直说这会儿没看清，别拿以前看到过的界面充数。）'
        return (
            f'{situation}\n（你刚瞥了一眼屏幕，看到的就是这些：{description}。'
            '问到屏幕上有什么，只能依据这一句；以前看到过的界面、文件夹属于回忆，'
            '别当成现在还在那儿。）'
        )

    # ------------------------------------------------------------ 对外只读（供 http.py 用）

    def current_app(self) -> str:
        """当前前台程序名，喂给视觉模型当先验。没有分类结果就是空串。"""
        return self._last_classified.app if self._last_classified else ''

    @property
    def vision(self) -> VisionService | None:
        return self._vision

    def observability_fields(self, now: int | None = None) -> dict:
        """给 /observability 用——sleep/budget/sensing 三块，key 名对齐前端已经在读的 camelCase。"""
        now = now if now is not None else current_time()
        sleep_eval = self._sleep.inspect(now)
        budget_desc = describe_budget(self._budget, now)
        minutes = max(0, (now - self._last_activity_since) // 60_000)
        return {
            'sleep': {
                'asleep': sleep_eval.asleep, 'drowsy': sleep_eval.drowsy, 'justWoke': sleep_eval.just_woke,
                'probability': sleep_eval.probability, 'cutoff': sleep_eval.cutoff,
                'minutesFromBedtime': sleep_eval.minutes_from_bedtime,
                'naturalWakeTargetAt': sleep_eval.natural_wake_target_at,
                'effectiveWakeAt': sleep_eval.effective_wake_at,
                'sleepDebtDelayMinutes': sleep_eval.sleep_debt_delay_minutes,
            },
            'budget': {
                'used': budget_desc['used'], 'remaining': budget_desc['remaining'],
                'ignored': budget_desc['ignored'], 'cooldownMinutes': budget_desc['cooldown_minutes'],
                'nextAllowedInMinutes': budget_desc['next_allowed_in_minutes'],
            },
            'sensing': {
                'activity': self._last_classified.activity if self._last_classified else 'idle',
                'description': self._activity_text(),
                'minutes': minutes,
                'silent': self._last_classified.silent if self._last_classified else False,
                'visionStats': ({**self._vision.stats(), 'spoke': self._vision_spoke_count}
                               if self._vision else {'enabled': False, 'looks': 0, 'spoke': 0}),
            },
        }

    # ------------------------------------------------------------ 生命周期（注册进 lifecycle）

    async def startup(self) -> None:
        self.chat.set_activity_provider(self._activity_text)
        self.chat.set_sleep_state_provider(lambda: self._sleep.current())
        if self._cfg.vision.ready and self._vision_provider:
            from yueli.services.vision import VisionService
            self._vision = VisionService(self._cfg, push, self._vision_provider)
            logger.info('vision_service_ready', model=self._vision_provider.model)
        elif self._cfg.vision.ready:
            logger.warning('vision_service_disabled', reason='视觉模型配置无效，请检查后端启动日志')
        self._stop.clear()
        self._poll_task = asyncio.create_task(self._poll_loop(), name='awareness-poll')

    async def shutdown(self) -> None:
        self._stop.set()
        task = self._poll_task
        self._poll_task = None
        if task:
            try:
                await asyncio.wait_for(task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()

    # ------------------------------------------------------------ 前台事件摄入

    def on_foreground(self, body: dict) -> None:
        """赋给 app_state.foreground_callback；HTTP 处理器同步调用，保持轻量。"""
        now = current_time()
        info = ForegroundInfo(
            process=str(body.get('process') or ''),
            title=body.get('title'),
            fullscreen=bool(body.get('fullscreen')),
        )
        if 'visible' in body:
            self._last_visible = bool(body.get('visible'))

        obs = self._monitor.observe(info)
        classified = classify(info)
        if obs.process_changed or self._last_classified is None or classified.activity != self._last_classified.activity:
            self._last_activity_since = now
        self._last_classified = classified

        # ★ 不带 title——classify() 已经把标题吃掉了，trace 不能把它漏出来。
        trace.emit('foreground', process=info.process, activity=classified.activity,
                    silent=classified.silent, windowChanged=obs.window_changed)

        if obs.window_changed:
            asyncio.create_task(self._speak_safely(classified, now, scene=True))

        asyncio.create_task(self._refresh_sleep(now))

    # ------------------------------------------------------------ 主动搭话决策

    async def _speak_safely(self, classified: Classified, now: int, scene: bool) -> None:
        try:
            await self._consider_speak(classified, now, scene)
        except Exception as exc:
            logger.warning('proactive_speak_failed', error=str(exc), scene=scene)

    async def _consider_speak(self, classified: Classified, now: int, scene: bool) -> None:
        if not self.chat.ready:
            return
        ctx = InterruptContext(
            now=now, silent=classified.silent, asleep=self.chat.current_sleep().asleep,
            visible=self._last_visible, responded_since_last=self._responded_since_last(now),
        )
        decision = decide_scene(self._budget, ctx) if scene else decide(self._budget, ctx)
        trace.emit('proactive_decision', scene=scene, allow=decision.allow, reason=decision.reason)
        if not decision.allow:
            return
        minutes = max(0, (now - self._last_activity_since) // 60_000)
        base = describe_activity(classified, minutes)
        situation = self._with_vision(base)
        description_used = situation != base
        lines = await self.chat.compose_proactive(situation)
        if not lines:
            return
        self.chat.speak(lines)
        if description_used:
            self._vision_spoke_count += 1
        self._budget = after_speak(self._budget, ctx)

    def _responded_since_last(self, now: int) -> bool:
        last_msg = self.chat.memory.last_message_at()
        if last_msg is None:
            return True
        return last_msg > self._budget.last_at

    # ------------------------------------------------------------ 睡眠状态推送

    async def _refresh_sleep(self, now: int) -> None:
        try:
            state = self._sleep.current(now)
        except Exception as exc:
            logger.warning('sleep_eval_failed', error=str(exc))
            return
        if state.asleep != self._last_pushed_asleep:
            self._last_pushed_asleep = state.asleep
            trace.emit('sleep_transition', asleep=state.asleep, drowsy=state.drowsy,
                       probability=state.probability)
            await push('sleep.state', {
                'asleep': state.asleep, 'drowsy': state.drowsy,
                'justWoke': state.just_woke, 'probability': state.probability,
            })

    # ------------------------------------------------------------ 后台轮询

    async def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:
                logger.warning('awareness_tick_failed', error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL_S)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        """没有新前台事件也要跑：长时间 AFK 的就寝/起床过渡、空闲态主动搭话。"""
        now = current_time()
        if self._schedule:
            asyncio.create_task(self._schedule.ensure(now))
        await self._refresh_sleep(now)
        if self._last_classified is not None:
            await self._consider_speak(self._last_classified, now, scene=False)
