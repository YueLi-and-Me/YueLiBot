"""编排主动感知、睡眠状态、兴趣累积和主动消息投放。

本模块接收前台窗口事件，调用 ``src.core.awareness`` 中的纯决策函数，维护待投放的
意图和每日预算，并通过 ``ChatService`` 生成桌面主动消息。视觉服务、日程服务、
记忆存储和观察事件均通过构造函数或聊天服务注入；本模块不直接实现规则计算或
平台协议。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Protocol

import asyncio

from src.core.awareness.budget import (
    DAILY_BUDGET,
    InterruptContext,
    ProactiveState,
    after_speak,
    decide,
    decide_scene,
    initial_state,
)
from src.core.awareness.intent import IntentType, PendingIntent, eligible_intents, stash
from src.core.awareness.interest import (
    InterestFactors, InterestState, factors_for, grow, initial_state as initial_interest_state,
    minutes_to_full, spend, wants_to_speak,
)
from src.core.awareness.signals import Classified
from src.core.awareness.sleep import SleepStateController
from src.core.common.clock import now as current_time
from src.core.common.logger import get_logger
from src.core.observe import events as trace
from src.core.observe.events import enter_stage
from src.core.observe.stages import DISPATCHING, FAILED, GENERATING, REPLIED
from src.core.persona.state import status_label
from src.core.schedule.timeline import ActivityTimeline

logger = get_logger(__name__)

POLL_INTERVAL_S = 60.0


class ProactiveSensor(Protocol):
    """定义主动感知内核消费的平台中立信号接口。"""

    @property
    def signal(self) -> Classified | None: ...

    @property
    def visible(self) -> bool: ...

    @property
    def vision(self) -> Any | None: ...

    def startup(self) -> None: ...

    def ingest(self, body: dict[str, Any]) -> tuple[Classified, bool]: ...

    def minutes(self, now: int) -> int: ...

    def activity_text(self, now: int | None = None) -> str: ...

    def with_vision(self, situation: str) -> str: ...

    def has_vision_description(self) -> bool: ...

    def note_vision_spoke(self) -> None: ...

    def vision_stats(self) -> dict[str, Any]: ...

    def current_app(self) -> str: ...


class AwarenessService:
    """接收前台事件并协调睡眠、兴趣、预算和主动消息生命周期。"""

    def __init__(
        self,
        chat: Any,
        schedule: Any | None,
        timeline: ActivityTimeline,
        cfg: Any,
        push_event: Callable[[str, dict[str, Any]], Awaitable[None]],
        sensor: ProactiveSensor | None = None,
    ) -> None:
        """初始化主动感知服务及其状态控制器。

        :param chat: 提供记忆、人物、当前睡眠和主动生成能力的聊天服务。
        :param schedule: 可选日程服务；缺失时使用配置生成备用作息。
        :param timeline: Bot 实际生活活动的唯一连续时间线。
        :param cfg: 提供主动感知、视觉和日程配置的运行时配置。
        :param push_event: 异步向客户端推送状态事件的回调。
        :param sensor: 可选的平台信号提供者；缺失时服务以无信号模式运行。

        副作用：
            读取并恢复持久化的 promise 意图，创建前台监控、睡眠控制器和停止
            事件；不会启动后台轮询，需显式调用 ``startup``。
        """

        started_at = current_time()
        self.chat = chat
        self._schedule = schedule
        self._timeline = timeline
        self._cfg = cfg
        self._push_event = push_event
        self._sensor = sensor
        # 主动搭话的唯一出口是桌面 stream：桌宠关闭时没有窗口接收这些消息，
        # 运行兴趣累积与生成只会浪费模型调用，因此两个开关取与。
        self._enabled = cfg.generation.proactive.enabled and cfg.desktop_pet.enabled

        # 启动前恢复 promise，保证服务重建不会丢失尚未到期的主动意图。
        self._sleep = SleepStateController(timeline=timeline)
        self._budget: ProactiveState = initial_state(started_at)
        self._interest: InterestState = initial_interest_state(started_at)
        self._pending: list[PendingIntent] = self._restore_promises()
        self._last_pushed_sleep: tuple[bool, bool, bool] | None = None
        self._last_activity_id: int | None = None

        self._poll_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def _restore_promises(self) -> list[PendingIntent]:
        """从记忆存储恢复仍属于 promise 类型的待投放意图。

        :return: 通过字段和枚举校验的 promise 意图列表；损坏记录会记录警告并跳过，
            短时情境意图不会从存储恢复。

        副作用：
            读取记忆存储并为无效记录写入警告日志。
        """
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
        """将当前待投放列表中的 promise 意图写回记忆存储。

        副作用：
            覆盖存储中的 pending promise 集合；短 TTL 的 scene、idle 和 plan 意图
            不写入持久化数据。
        """
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
        """根据前台活动、关系状态和用户缺席时间计算兴趣增长因子。

        :param now: 当前毫秒时间戳。

        :return: 供兴趣状态机使用的 ``InterestFactors``。

        :raises Exception: 记忆或人物状态读取失败时直接传播。
        """
        classified = self._sensor.signal if self._sensor else None
        last_message_at = self.chat.memory.last_message_at(self.chat.desktop_context.stream.id)
        absence_hours = 0.0 if last_message_at is None else max(0.0, (now - last_message_at) / 3_600_000)
        persona = self.chat.persona.get(self.chat.desktop_context.person.id)
        return factors_for(
            classified.activity if classified else 'idle',
            classified.intensity if classified else 'light',
            persona.intimacy,
            persona.energy,
            self._budget.ignored,
            absence_hours,
        )

    def _grow_interest(self, now: int) -> None:
        """按当前因子推进兴趣状态并记录观察事件。

        :param now: 当前毫秒时间戳。

        副作用：
            更新内存中的兴趣值，并写入带因子快照的 ``interest`` 观察事件。
        """

        factors = self._interest_factors(now)
        self._interest = grow(self._interest, factors, now)
        trace.emit('interest', interest=round(self._interest.value, 3), **factors.as_trace())

    def _activity_text(self) -> str:
        """生成注入聊天提示词的实时活动描述。

        :return: 当前前台活动及有效视觉描述；没有前台分类结果时返回空字符串。

        Note:
            每次调用都从视觉服务读取当前有效缓存，避免把过期描述持久化到意图。
        """
        if self._sensor is None:
            return ''
        return self._sensor.with_vision(self._sensor.activity_text())

    def _with_vision(self, situation: str) -> str:
        """将当前有效视觉描述附加到活动情境。

        :param situation: 已生成的前台活动描述。

        :return: 含视觉描述的情境文本；视觉服务未配置或没有有效缓存时明确标注不可见。
        """
        return self._sensor.with_vision(situation) if self._sensor else situation

    # ------------------------------------------------------------ 对外只读（供 http.py 用）

    def current_app(self) -> str:
        """读取当前前台程序名，供视觉请求作为识别先验。

        :return: 最近分类结果中的应用名；尚未收到前台事件时返回空字符串。
        """
        return self._sensor.current_app() if self._sensor else ''

    @property
    def vision(self) -> Any | None:
        """返回已初始化的视觉服务。

        :return: 已配置且在启动时成功创建的 ``VisionService``，否则为 ``None``。
        """

        return self._sensor.vision if self._sensor else None

    def observability_fields(self, now: int | None = None) -> dict:
        """构造观察面板使用的睡眠、兴趣、前台和待投放状态。

        :param now: 可选的当前毫秒时间戳；省略时读取统一时钟。

        :return: 使用 camelCase 字段名的可序列化诊断字典；不包含截图内容。
        """
        now = now if now is not None else current_time()
        sleep_state = self._sleep.current(now)
        activity = self._timeline.current(now)
        recent_activities = self._timeline.between(now - 24 * 60 * 60_000, now + 1)[-12:]
        factors = self._interest_factors(now)
        classified = self._sensor.signal if self._sensor else None
        minutes = self._sensor.minutes(now) if self._sensor else 0
        state = self.chat.persona.inspect(self.chat.desktop_context.person.id)
        # 所有面板字段基于同一个 now 计算，避免前端看到跨毫秒采样的混合状态。
        return {
            'selfState': {
                'energy': state.energy,
                'mood': state.mood,
                'statusLabel': status_label(
                    state,
                    asleep=sleep_state.asleep,
                    just_woke=sleep_state.just_woke,
                    resting=sleep_state.resting,
                ),
            },
            'sleep': {
                'asleep': sleep_state.asleep,
                'justWoke': sleep_state.just_woke,
                'resting': sleep_state.resting,
            },
            'activity': {
                'id': activity.id,
                'kind': activity.kind,
                'doing': activity.doing,
                'mood': activity.mood,
                'energyPace': activity.energy_pace,
                'moodPace': activity.mood_pace,
                'advances': activity.advances,
                'startedAt': activity.started_at,
                'expectedUntil': activity.expected_until,
                'source': activity.source,
            },
            'activityTimeline': [
                {
                    'id': item.id,
                    'kind': item.kind,
                    'doing': item.doing,
                    'mood': item.mood,
                    'energyPace': item.energy_pace,
                    'moodPace': item.mood_pace,
                    'advances': item.advances,
                    'startedAt': item.started_at,
                    'expectedUntil': item.expected_until,
                    'endedAt': item.ended_at,
                    'source': item.source,
                }
                for item in recent_activities
            ],
            'intentionProgress': (
                self._schedule.intention_progress(now)
                if self._schedule is not None
                else []
            ),
            'impulse': {
                'used': self._budget.used,
                'remaining': max(0, DAILY_BUDGET - self._budget.used),
                'ignored': self._budget.ignored,
                'interest': self._interest.value,
                'minutesToFull': minutes_to_full(self._interest, factors),
                **factors.as_trace(),
            },
            'sensing': {
                'activity': classified.activity if classified else 'idle',
                'description': self._with_vision(self._sensor.activity_text(now)) if self._sensor else '',
                'minutes': minutes,
                'silent': classified.silent if classified else False,
                'pending': [
                    {
                        'type': intent.intent_type.name,
                        'remainingSeconds': max(0, round((intent.expires_at - now) / 1000)),
                    }
                    for intent in self._pending
                ],
                # 仅输出视觉调用计数，不输出截图或模型原文，避免观察接口泄露屏幕内容。
                'visionStats': (self._sensor.vision_stats() if self._sensor
                                else {'enabled': False, 'looks': 0, 'spoke': 0}),
            },
        }

    # ------------------------------------------------------------ 生命周期（注册进 lifecycle）

    async def startup(self) -> None:
        """绑定聊天回调并启动主动感知后台轮询。

        :return: ``None``。

        副作用：
            注入活动、睡眠和 promise 回调，按配置初始化视觉服务，并在主动感知
            开启时创建轮询 task。
        """

        self.chat.set_activity_provider(self._activity_text)
        self.chat.set_sleep_state_provider(lambda: self._sleep.current())
        self.chat.set_sleep_wake_handler(self._sleep.wake)
        self.chat.set_promise_handler(self.stash_promise)
        if self._sensor:
            self._sensor.startup()
        if not self._enabled:
            logger.info('proactive_service_disabled')
            return
        self._stop.clear()
        self._poll_task = asyncio.create_task(self._poll_loop(), name='awareness-poll')

    async def shutdown(self) -> None:
        """请求停止主动感知轮询并等待其退出。

        :return: ``None``。

        副作用：
            设置停止事件并最多等待 5 秒；超时或取消时取消轮询 task。
        """

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
        """接收一次前台窗口和输入强度快照。

        :param body: 至少可包含 ``process``、``title``、``fullscreen`` 和 ``visible``；
                ``input`` 可提供 keys、clicks、mouseDistance、idleSeconds、spanMs。

        副作用：
            更新前台分类和活动起始时间，写入不含窗口标题的观察事件；主动感知启用
            时创建前台处理和睡眠刷新后台任务。该同步入口不等待任何异步操作。
        """
        if self._sensor is None:
            return
        now = current_time()
        classified, window_changed = self._sensor.ingest(body)
        if not self._enabled:
            return
        asyncio.create_task(self._handle_foreground(classified, now, window_changed))
        asyncio.create_task(self._refresh_activity_state(now))

    # ------------------------------------------------------------ 主动搭话决策

    async def _handle_foreground(self, classified: Classified, now: int, window_changed: bool) -> None:
        """处理前台变化触发的待投放意图和场景意图。

        :param classified: 当前前台活动分类。
        :param now: 事件发生的毫秒时间戳。
        :param window_changed: 当前窗口是否相对上一条快照发生变化。

        副作用：
            刷新待投放队列；窗口变化时可能生成并投放一条 scene 意图。异常只记录
            警告，避免后台任务未处理异常。
        """

        try:
            # 前台事件也是 flush 点，短 TTL 不会被 60 秒 tick 整段跳过。
            await self._flush_pending(now)
            if window_changed:
                await self._consider_speak(classified, now, IntentType.Scene)
        except Exception as exc:
            logger.warning('proactive_speak_failed', error=str(exc), scene=window_changed)

    def _interrupt_context(self, classified: Classified, now: int) -> InterruptContext:
        """将当前前台、可见性和最近互动状态组合成打扰判定输入。

        :param classified: 当前前台活动分类。
        :param now: 当前毫秒时间戳。

        :return: 供预算和场景决策函数使用的 ``InterruptContext``。
        """

        return InterruptContext(
            now=now,
            silent=classified.silent,
            asleep=self.chat.current_sleep().asleep,
            visible=self._sensor.visible if self._sensor else True,
            responded_since_last=self._responded_since_last(now),
        )

    def _decision_for(self, intent: PendingIntent, classified: Classified, now: int) -> tuple[InterruptContext, Any]:
        """根据意图类型选择对应的打扰规则并计算判定。

        :param intent: 待判断的主动意图。
        :param classified: 当前前台活动分类。
        :param now: 当前毫秒时间戳。

        :return: ``(InterruptContext, decision)``，后者为场景或普通主动打扰决策对象。
        """

        ctx = self._interrupt_context(classified, now)
        decision = decide_scene(self._budget, ctx) if intent.intent_type == IntentType.Scene else decide(self._budget, ctx)
        return ctx, decision

    def _stash(self, intent: PendingIntent, now: int) -> None:
        """将未获准或暂时无法投放的意图加入待处理队列。

        :param intent: 待保存的主动意图。
        :param now: 当前毫秒时间戳，用于 trace 中计算等待时长。

        副作用：
            可能更新内存队列、写入 promise 持久化数据并发出 ``proactive_intent``
            观察事件；重复意图由 ``stash`` 规则处理。
        """
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
        """登记一条由聊天服务识别出的 promise 意图。

        :param earliest_at: 允许主动提及该约定的最早毫秒时间戳。
        :param subject: 用户原话；持久化时不使用模型改写内容。

        副作用：
            将 promise 加入待投放队列并按需写入记忆存储。
        """
        self._stash(PendingIntent(
            intent_type=IntentType.Promise,
            earliest_at=earliest_at,
            expires_at=earliest_at + IntentType.Promise.ttl_ms,
            activity='',
            wants_vision=False,
            subject=subject,
        ), current_time())

    async def _consider_speak(self, classified: Classified, now: int, intent_type: IntentType) -> None:
        """创建并尝试投放由当前活动触发的主动意图。

        :param classified: 当前前台活动分类。
        :param now: 当前毫秒时间戳。
        :param intent_type: scene 或 idle 等主动意图类型。

        副作用：
            可能消耗兴趣值、写入待投放队列、请求截图、调用主动模型并投放消息。
        """
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
        waiting_for_vision = bool(
            intent.wants_vision and self._sensor and self._sensor.vision
            and not self._sensor.has_vision_description()
        )
        delivered = await self._deliver(intent, classified, ctx, now)
        if not delivered and waiting_for_vision:
            self._stash(intent, now)

    async def _deliver(self, intent: PendingIntent, classified: Classified, ctx: InterruptContext,
                       now: int, expired_vision: bool = False) -> bool:
        """生成并投放一条已经通过打扰判定的主动意图。

        :param intent: 待投放意图。
        :param classified: 当前前台活动分类。
        :param ctx: 已计算的打扰上下文，用于更新预算。
        :param now: 当前毫秒时间戳。
        :param expired_vision: 视觉意图等待超时后是否允许在不可见状态下继续生成，
                默认 ``False``。

        :return: 成功生成并投放时返回 ``True``；需要等待截图或模型没有正文时返回
            ``False``。

        副作用：
            可能请求截图、调用主动模型、发送桌面消息、更新预算和观察事件。
            模型异常会记录失败阶段后继续向上抛出。
        """
        if (
            intent.wants_vision and self._sensor and self._sensor.vision
            and not self._sensor.has_vision_description() and not expired_vision
        ):
            # 单程请求：不等截图回传，更不把截图内容冻进这条 intent。
            await self._push_event('vision.capture_request', {'reason': intent.intent_type.name.lower()})
            return False

        minutes = self._sensor.minutes(now) if self._sensor else 0
        if intent.intent_type == IntentType.Plan:
            activity = self._timeline.current(now)
            base = (
                f'你此刻在「{activity.doing}」。这段状态会让你「{activity.mood}」。'
                '如果想主动提起，只说眼下真实发生的事，不要复述全天计划。'
            )
        elif intent.intent_type == IntentType.Promise:
            base = (
                f'他之前原话提过：「{intent.subject}」。如果提起，只能说你记得他提过，'
                '不要把它说成已经确定的共同安排。'
            )
        else:
            base = self._sensor.activity_text(now) if self._sensor else ''
        description_used = bool(self._sensor and self._sensor.has_vision_description())
        desktop_context = self.chat.desktop_context
        stream = desktop_context.stream
        if not self.chat.claim_stream(stream.id, 'proactive'):
            return False
        enter_stage(GENERATING, stream.id, '桌面')
        try:
            lines = await self.chat.compose_proactive(desktop_context, self._with_vision(base))
            if not lines:
                enter_stage(FAILED, stream.id, '桌面', '主动搭话模型未生成正文')
                return False
            enter_stage(DISPATCHING, stream.id, '桌面')
            turn = self.chat.speak_claimed(desktop_context, lines)
        except Exception as exc:
            enter_stage(FAILED, stream.id, '桌面', str(exc))
            raise
        finally:
            self.chat.release_stream(stream.id, 'proactive')
        enter_stage(
            REPLIED,
            stream.id,
            '桌面',
            f'{sum(len(line) for line in lines)} 字',
            turn,
        )
        if description_used:
            self._sensor.note_vision_spoke()
        self._budget = after_speak(self._budget, ctx)
        trace.emit(
            'proactive_intent',
            action='flush',
            intentType=intent.intent_type.name,
            waitedSeconds=max(0, round((now - intent.earliest_at) / 1000)),
        )
        return True

    async def _flush_pending(self, now: int) -> None:
        """清理过期意图并按优先级最多投放一条待处理消息。

        :param now: 当前毫秒时间戳。

        副作用：
            更新待投放队列和 promise 持久化，可能调用主动模型并发送消息；每次
            调用最多完成一条投放。
        """
        classified = self._sensor.signal if self._sensor else None
        if not self.chat.ready or not self._pending or classified is None:
            return
        remaining, candidates, expired = eligible_intents(self._pending, now)
        self._pending = remaining
        expired_vision: list[PendingIntent] = []
        for intent in expired:
            # 视觉请求本身是单程的。到期仍未回帧时，遵循既有「看不到就明说」防线，
        # 已形成的主动意图必须保留到执行或明确失效；其余超过 TTL 的意图才清理。
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
            ctx, decision = self._decision_for(intent, classified, now)
            if not decision.allow:
                continue
            delivered = await self._deliver(
                intent,
                classified,
                ctx,
                now,
                expired_vision=intent in expired_vision,
            )
            if delivered:
                self._pending = [item for item in self._pending if item is not intent]
                self._persist_promises()
            return

    def _responded_since_last(self, now: int) -> bool:
        """判断最近一条桌面消息是否发生在上次预算更新之后。

        :param now: 当前毫秒时间戳（保留在接口中用于调用方统一传递时间）。

        :return: 没有历史消息或最近消息晚于预算时间时返回 ``True``。
        """
        last_msg = self.chat.memory.last_message_at(self.chat.desktop_context.stream.id)
        if last_msg is None:
            return True
        return last_msg > self._budget.last_at

    # ------------------------------------------------------------ 睡眠状态推送

    async def _refresh_activity_state(self, now: int) -> None:
        """刷新活动派生的睡眠状态并在变化时推送客户端事件。

        :param now: 当前毫秒时间戳。

        副作用：
            可能写入睡眠转换观察事件并推送 ``sleep.state``；读取异常只记录警告，
            不中断前台事件处理。
        """
        try:
            state = self._sleep.current(now)
        except Exception as exc:
            logger.warning('activity_sleep_read_failed', error=str(exc))
            return
        snapshot = (state.asleep, state.just_woke, state.resting)
        if snapshot != self._last_pushed_sleep:
            self._last_pushed_sleep = snapshot
            trace.emit(
                'sleep_transition',
                asleep=state.asleep,
                resting=state.resting,
            )
            await self._push_event('sleep.state', {
                'asleep': state.asleep,
                'justWoke': state.just_woke,
                'resting': state.resting,
            })

    # ------------------------------------------------------------ 后台轮询

    async def _poll_loop(self) -> None:
        """以固定间隔执行主动感知 tick，直到收到停止事件。

        副作用：
            周期性刷新待投放队列、作息、日程和兴趣值；单次 tick 异常只记录日志。
        """
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
        """执行一次无前台事件时也必须运行的状态推进。

        该流程刷新待投放队列、按日程时段变化登记 plan 意图、更新睡眠状态，并
        在兴趣达到阈值时尝试生成 idle 意图。

        副作用：
            可能创建日程生成 task、更新兴趣与待投放队列并发送主动消息。
        """
        now = current_time()
        await self._flush_pending(now)
        if self._schedule:
            asyncio.create_task(self._schedule.ensure(now))
        activity = self._timeline.current(now)
        if self._last_activity_id is not None and activity.id != self._last_activity_id:
            self._stash(PendingIntent(
                intent_type=IntentType.Plan,
                earliest_at=now,
                expires_at=now + IntentType.Plan.ttl_ms,
                activity=self._sensor.signal.activity if self._sensor and self._sensor.signal else 'idle',
                wants_vision=False,
            ), now)
        self._last_activity_id = activity.id
        await self._refresh_activity_state(now)
        classified = self._sensor.signal if self._sensor else None
        if classified is not None:
            self._grow_interest(now)
            if wants_to_speak(self._interest):
                await self._consider_speak(classified, now, IntentType.Idle)
