"""
主动感知与打扰编排。

职责比单纯的 gate 更宽：吃前台事件、维护睡眠状态机、累积兴趣值、暂存被闸门
挡下的念头，并在合适的真实事件上投放。纯决策仍留在 awareness 子模块，这里只
负责编排、状态持有和 trace。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Awaitable, Callable, TYPE_CHECKING

import asyncio

from src.awareness.budget import (
    DAILY_BUDGET,
    InterruptContext,
    ProactiveState,
    after_speak,
    decide,
    decide_scene,
    initial_state,
)
from src.awareness.classify import Classified, ForegroundInfo, classify, classify_input, describe_activity
from src.awareness.intent import IntentType, PendingIntent, eligible_intents, stash
from src.awareness.interest import (
    InterestFactors, InterestState, factors_for, grow, initial_state as initial_interest_state,
    minutes_to_full, spend, wants_to_speak,
)
from src.awareness.monitor import ForegroundProcessMonitor
from src.awareness.sleep import SleepInputs, SleepStateController
from src.common.clock import now as current_time
from src.common.logger import get_logger
from src.schedule.plan import _slot_at, day_plan_date, describe_day_plan, fallback_day_plan
from src.services.trace import trace

if TYPE_CHECKING:
    from src.services.vision import VisionProvider, VisionService


logger = get_logger(__name__)

POLL_INTERVAL_S = 60.0


class AwarenessService:
    """吃前台/截图事件，驱动睡眠状态与主动搭话决策；注册进 lifecycle。"""

    def __init__(
        self,
        chat: Any,
        schedule: Any | None,
        cfg: Any,
        push_event: Callable[[str, dict[str, Any]], Awaitable[None]],
        vision_provider: VisionProvider | None = None,
    ) -> None:
        started_at = current_time()
        self.chat = chat
        self._schedule = schedule
        self._cfg = cfg
        self._push_event = push_event
        self._vision_provider = vision_provider
        self._enabled = cfg.generation.proactive.enabled

        self._monitor = ForegroundProcessMonitor()
        self._sleep = SleepStateController(input_source=self._sleep_inputs, state_store=chat.memory)
        self._budget: ProactiveState = initial_state(started_at)
        self._interest: InterestState = initial_interest_state(started_at)
        self._pending: list[PendingIntent] = self._restore_promises()
        self._vision: VisionService | None = None

        self._last_classified: Classified | None = None
        self._last_activity_since = started_at
        self._last_visible = True
        self._last_pushed_asleep: bool | None = None
        self._last_plan_slot = ''
        self._vision_spoke_count = 0

        self._poll_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------ 依赖注入回调

    def _sleep_inputs(self, now: int) -> SleepInputs:
        date = day_plan_date(now)
        plan = self._schedule.get(now) if self._schedule else fallback_day_plan(date)
        desktop_context = self.chat.desktop_context
        return SleepInputs(
            date=plan.date,
            bedtime_hint=plan.bedtime_hint,
            wake_hint=plan.wake_hint,
            energy=self.chat.persona.get(desktop_context.person.id).energy,
            last_interaction_at=self.chat.memory.last_message_at(desktop_context.stream.id),
        )

    def _restore_promises(self) -> list[PendingIntent]:
        """只在启动时装回约定；情境类念头重启后已经不新鲜。"""
        restored: list[PendingIntent] = []
        for raw in self.chat.memory.load_pending_promises():
            try:
                intent = PendingIntent(
                    intent_type=IntentType(raw['intentType']),
                    earliest_at=int(raw['earliestAt']),
                    expires_at=int(raw['expiresAt']),
                    activity=str(raw['activity']),
                    wants_vision=bool(raw['wantsVision']),
                    subject=str(raw['subject']),
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning('pending_promise_restore_failed', error=str(exc))
                continue
            if intent.intent_type == IntentType.Promise:
                restored = stash(restored, intent)
        return restored

    def _persist_promises(self) -> None:
        """约定跨重启保存；短 TTL 的情境意图不进库。"""
        self.chat.memory.save_pending_promises([
            {
                'intentType': int(intent.intent_type),
                'earliestAt': intent.earliest_at,
                'expiresAt': intent.expires_at,
                'activity': intent.activity,
                'wantsVision': intent.wants_vision,
                'subject': intent.subject,
            }
            for intent in self._pending if intent.intent_type == IntentType.Promise
        ])

    def _interest_factors(self, now: int) -> InterestFactors:
        classified = self._last_classified or classify(None)
        last_message_at = self.chat.memory.last_message_at(self.chat.desktop_context.stream.id)
        absence_hours = 0.0 if last_message_at is None else max(0.0, (now - last_message_at) / 3_600_000)
        persona = self.chat.persona.get(self.chat.desktop_context.person.id)
        return factors_for(
            classified.activity,
            classified.intensity,
            persona.reliance,
            persona.energy,
            self._budget.ignored,
            absence_hours,
        )

    def _grow_interest(self, now: int) -> None:
        factors = self._interest_factors(now)
        self._interest = grow(self._interest, factors, now)
        trace.emit('interest', interest=round(self._interest.value, 3), **factors.as_trace())

    def _activity_text(self) -> str:
        """注入 ChatService 的实时情境，且不让视觉描述从缓存中冻结进意图队列。"""
        if not self._last_classified:
            return ''
        minutes = max(0, (current_time() - self._last_activity_since) // 60_000)
        return self._with_vision(describe_activity(self._last_classified, minutes))

    def _with_vision(self, situation: str) -> str:
        """把最近一次屏幕描述拼进实时情境；没有就明确告知模型不可见。"""
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
        """给 /observability 用；key 名对齐前端的 camelCase。"""
        now = now if now is not None else current_time()
        sleep_eval = self._sleep.inspect(now)
        factors = self._interest_factors(now)
        minutes = max(0, (now - self._last_activity_since) // 60_000)
        return {
            'sleep': {
                'asleep': sleep_eval.asleep,
                'drowsy': sleep_eval.drowsy,
                'justWoke': sleep_eval.just_woke,
                'probability': sleep_eval.probability,
                'cutoff': sleep_eval.cutoff,
                'minutesFromBedtime': sleep_eval.minutes_from_bedtime,
                'naturalWakeTargetAt': sleep_eval.natural_wake_target_at,
                'effectiveWakeAt': sleep_eval.effective_wake_at,
                'sleepDebtDelayMinutes': sleep_eval.sleep_debt_delay_minutes,
            },
            'impulse': {
                'used': self._budget.used,
                'remaining': max(0, DAILY_BUDGET - self._budget.used),
                'ignored': self._budget.ignored,
                'interest': self._interest.value,
                'minutesToFull': minutes_to_full(self._interest, factors),
                **factors.as_trace(),
            },
            'sensing': {
                'activity': self._last_classified.activity if self._last_classified else 'idle',
                'description': self._activity_text(),
                'minutes': minutes,
                'silent': self._last_classified.silent if self._last_classified else False,
                'pending': [
                    {
                        'type': intent.intent_type.name,
                        'remainingSeconds': max(0, round((intent.expires_at - now) / 1000)),
                    }
                    for intent in self._pending
                ],
                'visionStats': ({**self._vision.stats(), 'spoke': self._vision_spoke_count}
                                if self._vision else {'enabled': False, 'looks': 0, 'spoke': 0}),
            },
        }

    # ------------------------------------------------------------ 生命周期（注册进 lifecycle）

    async def startup(self) -> None:
        self.chat.set_activity_provider(self._activity_text)
        self.chat.set_sleep_state_provider(lambda: self._sleep.current())
        self.chat.set_promise_handler(self.stash_promise)
        if self._cfg.vision.ready and self._vision_provider:
            from src.services.vision import VisionService
            self._vision = VisionService(self._cfg, self._push_event, self._vision_provider)
            logger.info('vision_service_ready', model=self._vision_provider.model)
        elif self._cfg.vision.ready:
            logger.warning('vision_service_disabled', reason='视觉模型配置无效，请检查后端启动日志')
        if not self._enabled:
            logger.info('proactive_service_disabled')
            return
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

        input_snapshot = body.get('input')
        if isinstance(input_snapshot, dict):
            intensity = classify_input(
                int(input_snapshot.get('keys', 0)),
                int(input_snapshot.get('clicks', 0)),
                float(input_snapshot.get('mouseDistance', 0)),
                int(input_snapshot.get('idleSeconds', 0)),
                int(input_snapshot.get('spanMs', 8_000)),
            )
        else:
            intensity = 'light'

        obs = self._monitor.observe(info)
        classified = classify(info)
        classified.intensity = intensity
        if obs.process_changed or self._last_classified is None or classified.activity != self._last_classified.activity:
            self._last_activity_since = now
        self._last_classified = classified

        # ★ 不带 title——classify() 已经把标题吃掉了，trace 不能把它漏出来。
        trace.emit(
            'foreground',
            process=info.process,
            activity=classified.activity,
            intensity=classified.intensity,
            silent=classified.silent,
            windowChanged=obs.window_changed,
        )
        if not self._enabled:
            return
        asyncio.create_task(self._handle_foreground(classified, now, obs.window_changed))
        asyncio.create_task(self._refresh_sleep(now))

    # ------------------------------------------------------------ 主动搭话决策

    async def _handle_foreground(self, classified: Classified, now: int, window_changed: bool) -> None:
        try:
            # 前台事件也是 flush 点，短 TTL 不会被 60 秒 tick 整段跳过。
            await self._flush_pending(now)
            if window_changed:
                await self._consider_speak(classified, now, IntentType.Scene)
        except Exception as exc:
            logger.warning('proactive_speak_failed', error=str(exc), scene=window_changed)

    def _interrupt_context(self, classified: Classified, now: int) -> InterruptContext:
        return InterruptContext(
            now=now,
            silent=classified.silent,
            asleep=self.chat.current_sleep().asleep,
            visible=self._last_visible,
            responded_since_last=self._responded_since_last(now),
        )

    def _decision_for(self, intent: PendingIntent, classified: Classified, now: int) -> tuple[InterruptContext, Any]:
        ctx = self._interrupt_context(classified, now)
        decision = decide_scene(self._budget, ctx) if intent.intent_type == IntentType.Scene else decide(self._budget, ctx)
        return ctx, decision

    def _stash(self, intent: PendingIntent, now: int) -> None:
        before = self._pending
        self._pending = stash(self._pending, intent)
        if self._pending != before:
            trace.emit(
                'proactive_intent',
                action='stash',
                intentType=intent.intent_type.name,
                waitedSeconds=max(0, round((now - intent.earliest_at) / 1000)),
            )
            self._persist_promises()

    def stash_promise(self, earliest_at: int, subject: str) -> None:
        """供 ChatService 消费 promise；subject 保留用户原话，不采纳模型转述。"""
        self._stash(PendingIntent(
            intent_type=IntentType.Promise,
            earliest_at=earliest_at,
            expires_at=earliest_at + IntentType.Promise.ttl_ms,
            activity='',
            wants_vision=False,
            subject=subject,
        ), current_time())

    async def _consider_speak(self, classified: Classified, now: int, intent_type: IntentType) -> None:
        if not self.chat.ready:
            return
        intent = PendingIntent(
            intent_type=intent_type,
            earliest_at=now,
            expires_at=now + intent_type.ttl_ms,
            activity=classified.activity,
            wants_vision=intent_type == IntentType.Scene,
        )
        ctx, decision = self._decision_for(intent, classified, now)
        trace.emit('proactive_decision', scene=intent_type == IntentType.Scene,
                   allow=decision.allow, reason=decision.reason)
        if intent_type == IntentType.Idle:
            # 已经动过一次念头。被挡下时同样清空，避免下一个 tick 连续触发。
            self._interest = spend(self._interest, now)
        if not decision.allow:
            self._stash(intent, now)
            return
        waiting_for_vision = bool(intent.wants_vision and self._vision and not self._vision.chat_glance())
        delivered = await self._deliver(intent, classified, ctx, now)
        if not delivered and waiting_for_vision:
            self._stash(intent, now)

    async def _deliver(self, intent: PendingIntent, classified: Classified, ctx: InterruptContext,
                       now: int, expired_vision: bool = False) -> bool:
        if intent.wants_vision and self._vision and not self._vision.chat_glance() and not expired_vision:
            # 单程请求：不等截图回传，更不把截图内容冻进这条 intent。
            await self._push_event('vision.capture_request', {'reason': intent.intent_type.name.lower()})
            return False

        minutes = max(0, (now - self._last_activity_since) // 60_000)
        if intent.intent_type == IntentType.Plan and self._schedule:
            base = describe_day_plan(
                self._schedule.get(now), datetime.fromtimestamp(now / 1000), self.chat.current_sleep(),
            )
        elif intent.intent_type == IntentType.Promise:
            base = (
                f'他之前原话提过：「{intent.subject}」。如果提起，只能说你记得他提过，'
                '不要把它说成已经确定的共同安排。'
            )
        else:
            base = describe_activity(classified, minutes)
        description_used = bool(self._vision and self._vision.chat_glance())
        desktop_context = self.chat.desktop_context
        lines = await self.chat.compose_proactive(desktop_context, self._with_vision(base))
        if not lines:
            return False
        self.chat.speak(desktop_context, lines)
        if description_used:
            self._vision_spoke_count += 1
        self._budget = after_speak(self._budget, ctx)
        trace.emit(
            'proactive_intent',
            action='flush',
            intentType=intent.intent_type.name,
            waitedSeconds=max(0, round((now - intent.earliest_at) / 1000)),
        )
        return True

    async def _flush_pending(self, now: int) -> None:
        """剔除过期项、按优先级尝试投放；一轮只投一条。"""
        if not self.chat.ready or not self._pending or self._last_classified is None:
            return
        remaining, candidates, expired = eligible_intents(self._pending, now)
        self._pending = remaining
        expired_vision: list[PendingIntent] = []
        for intent in expired:
            # 视觉请求本身是单程的。到期仍未回帧时，遵循既有「看不到就明说」防线，
            # 不能把已经形成的开口念头无声吞掉；其他过期意图按 TTL 丢弃。
            if intent.wants_vision:
                expired_vision.append(intent)
            else:
                trace.emit(
                    'proactive_intent',
                    action='expire',
                    intentType=intent.intent_type.name,
                    waitedSeconds=max(0, round((now - intent.earliest_at) / 1000)),
                )
        if expired:
            self._persist_promises()

        for intent in sorted([*candidates, *expired_vision], key=lambda item: item.intent_type, reverse=True):
            ctx, decision = self._decision_for(intent, self._last_classified, now)
            if not decision.allow:
                continue
            delivered = await self._deliver(
                intent,
                self._last_classified,
                ctx,
                now,
                expired_vision=intent in expired_vision,
            )
            if delivered:
                self._pending = [item for item in self._pending if item is not intent]
                self._persist_promises()
            return

    def _responded_since_last(self, now: int) -> bool:
        last_msg = self.chat.memory.last_message_at(self.chat.desktop_context.stream.id)
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
            await self._push_event('sleep.state', {
                'asleep': state.asleep,
                'drowsy': state.drowsy,
                'justWoke': state.just_woke,
                'probability': state.probability,
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
        """没有新前台事件也要跑：睡眠过渡、日程节点和兴趣值。"""
        now = current_time()
        await self._flush_pending(now)
        if self._schedule:
            asyncio.create_task(self._schedule.ensure(now))
            plan = self._schedule.get(now)
            slot = _slot_at(plan, datetime.fromtimestamp(now / 1000))
            slot_key = f'{plan.date}:{slot.from_time}'
            if self._last_plan_slot and slot_key != self._last_plan_slot:
                self._stash(PendingIntent(
                    intent_type=IntentType.Plan,
                    earliest_at=now,
                    expires_at=now + IntentType.Plan.ttl_ms,
                    activity=self._last_classified.activity if self._last_classified else 'idle',
                    wants_vision=False,
                ), now)
            self._last_plan_slot = slot_key
        await self._refresh_sleep(now)
        if self._last_classified is not None:
            self._grow_interest(now)
            if wants_to_speak(self._interest):
                await self._consider_speak(self._last_classified, now, IntentType.Idle)
