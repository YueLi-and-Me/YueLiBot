"""
对话编排服务。

持有所有后端资源：按角色拆分的 LLM provider、MemoryStore、Persona、DayPlanService。
通过 WebSocket push 推事件给 Electron 主进程。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Mapping

import asyncio
import inspect
import random
import sqlite3

from .trace_console import mark_turn_start, render_observation, render_turn, render_turn_error
from .vector import VectorService

from src.core.agent.character import pick_tone
from src.core.agent.action import ActionContext, ActionPolicy, AlwaysReplyPolicy
from src.core.agent.expression import ExpressionSample, render_expression_habits, sample_expression_habits
from src.core.agent.expression_select import ExpressionSelector
from src.core.agent.history import close_dangling_say, fit_char_budget, normalize_history
from src.core.agent.parser import (
    MemoryEvent, MoodEvent, ParseEvent, PromiseEvent, ResponseParser, SayEndEvent, SayEvent, TextEvent,
)
from src.core.agent.prompt import build_proactive_prompt, build_system_prompt, describe_resumption
from src.core.agent.summarize import summarize
from src.core.awareness.sleep import SleepState
from src.core.common.clock import now as current_time
from src.core.common.logger import get_logger
from src.core.config.schema import Config, ConversationConfig
from src.core.llm_models.openai import LlmError
from src.core.llm_models.protocol import LlmProvider
from src.core.llm_models.snapshot import bind_render_params, dump as dump_llm_request
from src.core.memory.store import EpisodeInput, FactInput, MemoryStore, RecalledFact
from src.core.observe import events as trace
from src.core.observe.events import bind_origin, enter_stage
from src.core.observe.stages import CONTEXT, DISPATCHING, EXPRESSION, FAILED, GATED, GENERATING, REPLIED, Stage
from src.core.persona.state import MoodDelta, Persona, describe_acquaintance, describe_persona
from src.core.platform_io.broker import PlatformBroker
from src.core.platform_io.registry import StreamRegistry
from src.core.platform_io.types import (
    ConversationContext,
    IdentityRef,
    InboundMessage,
    OutboundMessage,
    PersonRef,
    StreamRef,
)
from src.core.prompts.registry import (
    CHAT_PROACTIVE_TEMPLATE_IDS,
    CHAT_SYSTEM_TEMPLATE_IDS,
    prompt_metadata,
)
from src.core.schedule.plan import DayPlan, DayPlanService, ScheduleSleepState

logger = get_logger(__name__)

CHAT_POLL_INTERVAL_S = 0.1

# 旧测试和诊断脚本仍会读取这个换算值；唯一默认来源是配置模型。
SESSION_GAP_MS = ConversationConfig().session_gap_minutes * 60_000

_HINTS: dict[str, str] = {
    'auth': 'API Key 无效，检查 providers.toml',
    'model': '模型 ID 不对，检查 models.toml',
    'quota': '限流或余额不足，稍等一下',
    'network': '连不上模型接口，检查网络或代理',
    'timeout': '模型迟迟不出字，可能在排队；换个模型或调大首字超时',
    'blocked': '这句被内容审核拦了，换个说法',
}


@dataclass
class _InflightTurn:
    """可被指定 stream 打断的一次流式对话。"""

    task: asyncio.Task[None]
    cancel_event: asyncio.Event


@dataclass
class _SessionState:
    """一个 stream 内稳定的语气、表达样本和单次重逢上下文。"""

    started_at: int | None = None
    tone: str | None = None
    seed: int = 0
    resumption_gap_ms: int | None = None


@dataclass
class _TurnSink:
    """单轮流式解析状态。"""

    context: ConversationContext
    cancel_event: asyncio.Event
    turn: int
    now: int
    source_text: str
    # 解析副作用。
    side_effects: list[dict] = field(default_factory=list)
    # 非桌面平台的出站正文。
    segments: list[str] = field(default_factory=list)
    segment: list[str] | None = None
    interrupted: bool = False


@dataclass(frozen=True)
class _PreparedTurnContext:
    """保存一次性组装完成、可继续附加模型增强的回合上下文。"""

    context: ConversationContext
    query: str
    now: int
    platform_bot_name: str | None
    fact_candidates: list[RecalledFact]
    episodes: list[str]
    persona: str
    acquaintance: str
    activity: str | None
    schedule: str | None
    resumption: str | None
    raw_history: list[dict[str, str]]


class ChatService:
    """协调消息归属、记忆召回、模型流式输出、解析副作用和平台投递。

    ``push_event`` 负责向桌面客户端推送观察与解析事件，``speak_audio`` 可选地
    将完整 ``<say>`` 分句交给语音服务；外部平台通过 ``PlatformBroker`` 投递，
    桌面消息继续使用解析事件链路。
    """

    def __init__(
        self,
        db: sqlite3.Connection,
        chat_provider: LlmProvider | None,
        proactive_provider: LlmProvider | None,
        summary_provider: LlmProvider | None,
        push_event: Callable[[str, Any, int], Any],
        *,
        cfg: Config,
        speak_audio: Callable[[str, int], Any] | None = None,
        vector: VectorService | None = None,
        broker: PlatformBroker | None = None,
        expression_provider: LlmProvider | None = None,
        action_policy: ActionPolicy | None = None,
        action_policies: Mapping[str, ActionPolicy] | None = None,
    ) -> None:
        """初始化对话服务及其数据库、模型和平台依赖。

        :param db: 已完成迁移的 SQLite 连接。
        :param chat_provider: 普通对话模型；为 ``None`` 时服务不可接收对话。
        :param proactive_provider: 主动消息模型；可为 ``None``。
        :param summary_provider: 摘要模型；可为 ``None``。
        :param push_event: 接收频道、载荷和 stream ID 的事件推送回调。
        :param cfg: 已校验的运行时配置。
        :param speak_audio: 可选的同步或异步语音回调。
        :param vector: 可选向量服务；省略时创建禁用实例。
        :param broker: 可选的非桌面平台出站路由器。
        :param expression_provider: 可选的表达样本选择模型。
        :param action_policy: 可选的回合内动作策略；省略时始终回复。
        :param action_policies: 按 stream kind 装配的动作策略；未配置类型使用默认策略。

        副作用：
            创建记忆、注册表和人格服务，读取 desktop 上下文，并保存当天 owner
            人格快照；不会启动异步任务。
        """

        self._db = db
        self._chat_provider = chat_provider
        self._proactive_provider = proactive_provider
        self._summary_provider = summary_provider
        self._push_event = push_event
        self._speak_audio = speak_audio
        self._broker = broker
        self._default_action_policy = action_policy or AlwaysReplyPolicy()
        self._action_policies = dict(action_policies or {})
        # 打断时用来叫停已经在播的音频；由 __main__ 注入 TtsService.cancel。
        self._cancel_audio: Callable[[int], Any] | None = None
        # 流式解析时按 stream 攒当前这句 <say> 的正文，收完整句才送去合成。
        self._speech_buffer: dict[int, list[str]] = {}
        self._vector = vector or VectorService(None, None)
        self._cfg = cfg
        self._bot_display_name = cfg.bot.name
        self._summary_personality = cfg.personality.personality
        conversation = cfg.conversation
        generation = cfg.generation
        self._working_memory_messages = conversation.working_memory_messages
        self._summarize_trigger_messages = conversation.summarize_trigger_messages
        self._summarize_batch_messages = conversation.summarize_batch_messages
        self._session_gap_ms = conversation.session_gap_minutes * 60_000
        self._fact_recall_limit = conversation.fact_recall_limit
        self._recalled_episode_limit = conversation.recalled_episode_limit
        self._recent_episode_limit = conversation.recent_episode_limit
        self._episode_context_limit = conversation.episode_context_limit
        self._chat_temperature = generation.chat.temperature
        self._chat_max_tokens = generation.chat.token_limit
        self._proactive_temperature = generation.proactive.temperature
        self._proactive_max_tokens = generation.proactive.token_limit
        self._summary_temperature = generation.summary.temperature
        self._summary_max_tokens = generation.summary.token_limit
        self._bot_names: tuple[str, ...] = (cfg.bot.name, *cfg.bot.aliases)
        self._at_mention_must_reply = cfg.group_chat.at_mention_must_reply
        self._name_mention_probability = cfg.group_chat.name_mention_probability
        self._group_persona_weight = cfg.group_chat.persona_weight
        self._perception_surfaces = frozenset(cfg.perception.surfaces)
        self._expression_habits = tuple(cfg.personality.expression_habits)
        self._proactive_expression_habits = tuple(
            cfg.personality.proactive_expression_habits
        )
        self._expression_selector = (
            ExpressionSelector(
                expression_provider,
                temperature=generation.expression.temperature,
                max_tokens=generation.expression.token_limit,
                candidates=self._expression_habits,
            )
            if expression_provider is not None and self._expression_habits
            else None
        )
        self.memory = MemoryStore(db)
        self._registry = StreamRegistry(db)
        self._desktop_context = self._registry.desktop_context()
        self.persona = Persona(db)
        self.persona.snapshot_daily(self._desktop_context.person.id)
        self._turn_id = 0
        self._inflight: dict[int, _InflightTurn] = {}
        self._buffers: dict[int, list[InboundMessage]] = {}
        self._stream_claims: dict[int, str] = {}
        self._poll_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._sessions: dict[int, _SessionState] = {}
        self._summarizing: set[int] = set()
        self._active_turns: dict[int, int] = {}
        self._activity: Callable[[], str] | None = None
        self._sleep_state: Callable[[], SleepState] | None = None
        self._promise_handler: Callable[[int, str], None] | None = None
        self._schedule: DayPlanService | None = None

    @property
    def ready(self) -> bool:
        """判断普通对话模型是否已经注入。

        :return: 已配置 ``chat_provider`` 时返回 ``True``，否则返回 ``False``。
        """

        return self._chat_provider is not None

    @property
    def desktop_context(self) -> ConversationContext:
        """返回唯一 desktop 会话的完整归属上下文。

        :return: 由 ``StreamRegistry`` 校验得到的 desktop stream 和 owner person 引用。
        """
        return self._desktop_context

    def set_schedule(self, svc: DayPlanService) -> None:
        """绑定日程服务。

        :param svc: 提供日程读取、生成和作息计算的服务实例。

        副作用：
            替换当前日程依赖；不会立即读取日程或调用模型。
        """

        self._schedule = svc

    def set_action_policy(self, stream_kind: str, policy: ActionPolicy) -> None:
        """为一种会话类型绑定回合动作策略。

        :param stream_kind: 会话类型标识，由组合根决定其平台语义。
        :param policy: 对该类会话生效的动作策略。
        副作用：替换后续新回合使用的策略，不影响已启动的回合。
        """
        self._action_policies[stream_kind] = policy

    def set_activity_provider(self, fn: Callable[[], str]) -> None:
        """绑定实时活动描述回调。

        :param fn: 无参数并返回当前活动文本的回调。

        副作用：
            替换后续提示词构建使用的活动来源。
        """

        self._activity = fn

    def set_sleep_state_provider(self, fn: Callable[[], SleepState]) -> None:
        """绑定睡眠状态回调。

        :param fn: 无参数并返回当前睡眠状态的回调。

        副作用：
            替换对话和人格结算读取的睡眠状态来源。
        """

        self._sleep_state = fn

    def set_promise_handler(self, fn: Callable[[int, str], None]) -> None:
        """绑定解析后约定的统一调度回调。

        :param fn: 接收提醒时间戳和约定正文的同步回调。

        副作用：
            替换后续对话轮次使用的约定处理器；不会立即执行回调或写入约定。
        """
        self._promise_handler = fn

    def _persona_weight(self, context: ConversationContext) -> float:
        """根据会话类型返回人格增量倍率。

        :param context: 已解析 stream 和人物归属的会话上下文。

        :return: 群聊使用配置的群聊人格倍率，其他会话返回 ``1.0``。

        副作用：
            仅读取上下文和服务配置，不修改人格状态。
        """
        return self._group_persona_weight if context.stream.kind == 'group' else 1.0

    def current_sleep(self) -> ScheduleSleepState:
        """读取当前睡眠状态并转换为调度服务使用的类型。

        :return: 当前 ``asleep``、``drowsy`` 和 ``just_woke`` 标志；未绑定状态回调时
            返回三个标志均为 ``False`` 的默认值。
        """

        s = self._sleep_state() if self._sleep_state else None
        if s is None:
            return ScheduleSleepState(asleep=False, drowsy=False)
        return ScheduleSleepState(asleep=s.asleep, drowsy=s.drowsy, just_woke=s.just_woke)

    async def ensure_schedule(self, now: int | None = None) -> None:
        """确保指定时间对应的日程已经可用。

        :param now: 可选的当前毫秒时间戳；省略时读取统一时钟。

        副作用：
            绑定日程服务时可能调用模型并写入日程存储；未绑定时不执行操作。
        """

        if self._schedule:
            await self._schedule.ensure(now or current_time())

    def settle_elapsed(
        self,
        context: ConversationContext,
        now: int | None = None,
        earlier_asleep: bool = False,
    ) -> None:
        """结算指定人物自上次状态更新时间以来的作息影响。

        :param context: 已完成会话和人物归属解析的上下文。
        :param now: 可选的当前毫秒时间戳；省略时读取统一时钟。
        :param earlier_asleep: 区间起点之前已入睡时是否计入被截断的睡眠时间。

        副作用：
            owner 上会更新人格状态并保存每日快照；非 owner 只读取状态，不写入
            关系信号。
        """

        now = now or current_time()
        person_id = context.person.id
        before = self.persona.get(person_id)
        if self._schedule:
            asleep_hours = self._schedule.sleep_hours_between(before.updated_at, now, earlier_asleep)
        else:
            asleep_hours = 0.0
        if context.relationship_signals_enabled:
            self.persona.apply_elapsed(person_id, now, asleep_hours)
            self.persona.snapshot_daily(person_id, now)

    async def startup(self) -> None:
        """启动固定间隔的聊天缓冲轮询。"""
        self._stop.clear()
        self._poll_task = asyncio.create_task(self._poll_loop(), name='chat-poll')

    async def shutdown(self) -> None:
        """停止聊天缓冲轮询并终止仍在执行的回复。"""
        self._stop.set()
        task = self._poll_task
        self._poll_task = None
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
        for stream_id in tuple(self._inflight):
            self.interrupt(stream_id)

    async def _poll_loop(self) -> None:
        """以固定间隔处理当前各 stream 的非空缓冲区。"""
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:
                logger.warning('chat_tick_failed', error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=CHAT_POLL_INTERVAL_S)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        """为每个空闲且有缓冲消息的 stream 启动一轮同发送者回复。"""
        for stream_id in tuple(self._buffers):
            buffered = self._buffers.get(stream_id)
            if not buffered:
                self._buffers.pop(stream_id, None)
                continue
            if not self.claim_stream(stream_id, 'reply'):
                continue
            # 群聊中不同人物的关系与事实彼此独立，只消费连续同一人物的前缀。
            person_id = buffered[0].context.person.id
            boundary = next(
                (
                    index
                    for index, message in enumerate(buffered[1:], start=1)
                    if message.context.person.id != person_id
                ),
                len(buffered),
            )
            batch = buffered[:boundary]
            del buffered[:boundary]
            if not buffered:
                self._buffers.pop(stream_id, None)
            try:
                await self._start_turn(batch)
            except Exception:
                self._buffers.setdefault(stream_id, [])[:0] = batch
                self.release_stream(stream_id, 'reply')
                raise

    async def send(self, inbound: InboundMessage) -> None:
        """把一条携带完整归属上下文的入站消息放入对应 stream 缓冲区。

        :param inbound: 已完成 stream、person 和 identity 解析的消息。

        :return: ``None``；入缓冲时尚未创建回合。

        副作用：
            按到达顺序将非空消息追加到对应 stream 缓冲区，不打断在飞回合。

        :raises ValueError: 入站上下文或平台归属不满足下游约束时由依赖服务抛出。
        :raises Exception: 任务内部异常会记录并推送 ``chat.error``，不会由返回的 task
                再次向调用方抛出。
        """
        trimmed = inbound.text.strip()
        if not trimmed:
            return
        stream_id = inbound.context.stream.id
        self._buffers.setdefault(stream_id, []).append(InboundMessage(
            text=trimmed,
            context=inbound.context,
            mentioned_me=inbound.mentioned_me,
            external_message_id=inbound.external_message_id,
            bot_name=inbound.bot_name,
        ))

    async def _start_turn(self, batch: list[InboundMessage]) -> int:
        """取一个非空消息批次创建并启动回复回合。"""
        if not batch:
            raise ValueError('回复批次不能为空')
        last_message = batch[-1]
        context = last_message.context
        stream_id = context.stream.id
        if any(message.context.stream.id != stream_id for message in batch):
            raise ValueError('同一回复批次只能包含一个 stream')
        trimmed = '\n'.join(message.text for message in batch)
        inbound = InboundMessage(
            text=trimmed,
            context=context,
            mentioned_me=any(message.mentioned_me for message in batch),
            bot_name=next(
                (message.bot_name for message in reversed(batch) if message.bot_name is not None),
                None,
            ),
        )

        turn = self._next_turn()
        self._active_turns[stream_id] = turn
        mark_turn_start(turn)
        sender = self._sender_metadata(context)
        # 绑定来源，这一轮后续的每条 trace 都会带上，不必逐个 kind 拼
        bind_origin(
            stream_id=stream_id,
            platform=context.stream.platform,
            person_id=context.person.id,
            person_kind=context.person.kind,
            sender_external_id=sender['senderExternalId'],
            sender_nickname=sender['senderNickname'],
            sender_group_card=sender['senderGroupCard'],
            sender_display_name=sender['senderDisplayName'],
            sender_label=sender['senderLabel'],
            bot_name=self._bot_display_name,
        )
        for message in batch:
            trace.emit('user_input', turnId=turn, text=message.text)
        if context.stream.platform == 'desktop':
            await self._emit(stream_id, 'chat.start', {
                'turnId': turn,
                'kind': 'start',
            })
        if not self._chat_provider:
            await self._emit(stream_id, 'chat.error', {
                'turnId': turn,
                'kind': 'error',
                'message': '对话未初始化',
                'hint': '检查 providers.toml 和 models.toml',
            })
            self.release_stream(stream_id, 'reply')
            return turn

        cancel_event = asyncio.Event()

        async def _run() -> None:
            """执行当前回合的上下文构建、模型流读取、历史持久化和结果投递。

            :raises LlmError: 模型调用失败；可根据错误类型决定回滚、保留或推送错误事件。
            :raises Exception: 其他上下文、数据库或出站处理异常；函数记录诊断并推送错误事件。

            副作用：
                写入用户和助手历史，更新阶段和观测事件，可能调用人格结算、摘要任务、
                桌面 WebSocket 或外部平台出站驱动。
            """

            user_msg_ids: list[int] = []
            assistant_raw = ''
            reply_persisted = False
            try:
                now = current_time()
                asleep = self._sleep_state().asleep if self._sleep_state else False
                self.settle_elapsed(context, now, asleep)
                self.memory.sweep(now)

                # 必须在写入当前用户消息前计算会话间隔，否则 last_message_at 会变成 now。
                self._refresh_session(context, now)
                for message in batch:
                    user_msg_ids.append(self.memory.append_message(
                        stream_id,
                        message.context.person.id,
                        'user',
                        message.text,
                        now,
                    ))
                if cancel_event.is_set():
                    return

                # parser 保留跨 chunk 的标签状态，sink 聚合副作用和非桌面分句。
                parser = ResponseParser()
                sink = _TurnSink(
                    context=context,
                    cancel_event=cancel_event,
                    turn=turn,
                    now=now,
                    source_text=trimmed,
                )
                self._mark_stage(context, CONTEXT, turn_id=turn)
                prepared_context = self._prepare_turn_context(
                    context,
                    trimmed,
                    now,
                    inbound.bot_name,
                )
                decision_messages = self._render_prepared_context(prepared_context)
                # 协议 @ 必回属于入口契约，明确绕过群聊存在感策略。
                action_policy = (
                    self._default_action_policy
                    if inbound.mentioned_me and self._at_mention_must_reply
                    else self._action_policies.get(
                        context.stream.kind,
                        self._default_action_policy,
                    )
                )
                action = await action_policy.decide(ActionContext(
                    turn_id=turn,
                    stream_id=stream_id,
                    messages=tuple(decision_messages),
                ))
                trace.emit(
                    'turn_action',
                    turnId=turn,
                    action=action.action,
                    reason=action.reason,
                    decisionSource=type(action_policy).__name__,
                )
                if action.action == 'silent':
                    self._mark_stage(
                        context,
                        GATED,
                        f'未回复：{action.reason}',
                        turn_id=turn,
                    )
                    if context.stream.kind == 'group':
                        for message in batch:
                            self._emit_group_observation(message, action.reason, message.text)
                    if context.stream.platform == 'desktop':
                        await self._emit(stream_id, 'chat.silent', {
                            'turnId': turn,
                            'kind': 'silent',
                            'reason': action.reason,
                        })
                    return
                if action.action != 'reply':
                    raise ValueError(f'未知回合动作：{action.action}')
                if self._schedule:
                    self._schedule.ensure_background(now)
                render_params: dict[str, dict[str, str]] = {}
                messages = await self._enrich_prepared_context(
                    prepared_context,
                    cancel_event,
                    render_params,
                )
                self._mark_stage(context, GENERATING, turn_id=turn)
                trace.emit(
                    'llm_request',
                    turnId=turn,
                    messages=messages,
                    temperature=self._chat_temperature,
                    maxTokens=self._chat_max_tokens,
                    renderParams=render_params,
                    **prompt_metadata('chat.system', CHAT_SYSTEM_TEMPLATE_IDS),
                )
                bind_render_params(render_params)
                async for chunk in self._chat_provider.stream(
                    messages=messages,
                    temperature=self._chat_temperature,
                    max_tokens=self._chat_max_tokens,
                ):
                    if cancel_event.is_set():
                        # 取消只停止后续读取，已收到的正文仍按历史一致性规则保存。
                        sink.interrupted = True
                        break
                    trace.emit('llm_chunk', turnId=turn, text=chunk.get('text'), reasoning=chunk.get('reasoning'))
                    if not chunk.get('text'):
                        continue
                    assistant_raw += chunk['text']
                    await self._consume_events(parser.push(chunk['text']), sink)
                    if sink.interrupted:
                        break

                # 正常结束时冲刷未闭合标签；中断时避免把取消后的残余缓冲当作完整事件。
                if not sink.interrupted:
                    await self._consume_events(parser.flush(), sink)

                # 先保存已产生的助手正文，再判断是否中断，确保历史与已经展示的内容一致。
                self._persist_reply(context, assistant_raw)
                reply_persisted = True
                if sink.interrupted:
                    return

                trace.emit('llm_final', turnId=turn, text=assistant_raw)
                render_turn(
                    turn,
                    sender['senderLabel'],
                    trimmed,
                    messages,
                    assistant_raw,
                    sink.side_effects,
                    self._bot_display_name,
                )
                try:
                    self.persona.apply_turn(
                        context.person.id,
                        current_time(),
                        weight=self._persona_weight(context),
                    )
                except Exception as exc:
                    # 人格结算是附加状态，失败不能回滚已经展示并持久化的对话正文。
                    logger.warning('persona_apply_turn_failed', turnId=turn, error=str(exc))
                if context.stream.platform == 'desktop':
                    # 桌面端消费解析事件；外部平台只在整轮完成后发送分句。
                    self._mark_stage(context, DISPATCHING, turn_id=turn)
                    await self._emit(stream_id, 'chat.done', {'turnId': turn, 'kind': 'done'})
                else:
                    await self._dispatch_outbound(
                        context,
                        turn,
                        sink.segments,
                    )
                self._mark_stage(
                    context, REPLIED, f'{len(assistant_raw)} 字', turn_id=turn,
                )
                asyncio.create_task(self._maybe_summarize(context.stream.id))

            except LlmError as exc:
                if exc.kind == 'aborted':
                    # 用户主动中断不是模型故障，但已生成正文仍须进入历史。
                    if not reply_persisted:
                        self._persist_reply(context, assistant_raw)
                    return
                if not reply_persisted and user_msg_ids:
                    # 模型尚未产出正文时回滚用户消息，避免留下无法对应的未完成回合。
                    self._rollback_batch_or_keep(context, user_msg_ids, assistant_raw)
                hint = _HINTS.get(exc.kind, '')
                snapshot = dump_llm_request('chat', exc.kind, str(exc), {
                    'turnId': turn,
                    'stage': trace.current_stage_id(),
                    'streamId': stream_id,
                })
                trace.emit('llm_error', turnId=turn, errorKind=exc.kind, message=str(exc),
                           snapshotPath=str(snapshot) if snapshot else None)
                render_turn_error(turn, sender['senderLabel'], trimmed, exc.kind, str(exc))
                self._mark_stage(context, FAILED, f'{exc.kind}：{exc}', turn_id=turn)
                if context.stream.platform == 'desktop':
                    await self._emit(stream_id, 'chat.error', {
                        'turnId': turn,
                        'kind': 'error',
                        'message': str(exc),
                        'hint': hint,
                    })
            except Exception as exc:
                if not reply_persisted and user_msg_ids:
                    # 非模型异常沿用同一历史一致性策略，再生成诊断快照。
                    self._rollback_batch_or_keep(context, user_msg_ids, assistant_raw)
                snapshot = dump_llm_request('chat', type(exc).__name__, str(exc), {
                    'turnId': turn,
                    'stage': trace.current_stage_id(),
                    'streamId': stream_id,
                })
                logger.error(
                    '对话处理失败',
                    streamId=stream_id,
                    turnId=turn,
                    error=str(exc),
                    snapshot=str(snapshot) if snapshot else None,
                )
                trace.emit('llm_error', turnId=turn, errorKind='unknown', message=str(exc),
                           snapshotPath=str(snapshot) if snapshot else None)
                render_turn_error(turn, sender['senderLabel'], trimmed, 'unknown', str(exc))
                self._mark_stage(context, FAILED, str(exc), turn_id=turn)
                await self._emit(
                    stream_id,
                    'chat.error',
                    {'turnId': turn, 'kind': 'error', 'message': str(exc)},
                )

        task = asyncio.create_task(_run())
        inflight = _InflightTurn(task=task, cancel_event=cancel_event)
        self._inflight[stream_id] = inflight

        def _remove_completed(done_task: asyncio.Task[None]) -> None:
            """仅移除仍对应当前回合的完成任务，避免旧回调覆盖新任务。

            :param done_task: 已完成的异步回合任务。

            副作用：
                当任务仍是当前 stream 的活动任务时，从活动任务映射中移除它；否则不操作。
            """

            current = self._inflight.get(stream_id)
            if current is inflight and current.task is done_task:
                self._inflight.pop(stream_id, None)
            self.release_stream(stream_id, 'reply')

        task.add_done_callback(_remove_completed)
        return turn

    def claim_stream(self, stream_id: int, source: str) -> bool:
        """尝试为一个驱动源占用 stream，并记录竞争失败。"""
        active_source = self._stream_claims.get(stream_id)
        if active_source is None:
            self._stream_claims[stream_id] = source
            return True
        trace.emit(
            'turn_competition',
            streamId=stream_id,
            activeSource=active_source,
            blockedSource=source,
        )
        return False

    def release_stream(self, stream_id: int, source: str) -> None:
        """仅释放仍由指定驱动源持有的 stream。"""
        if self._stream_claims.get(stream_id) == source:
            self._stream_claims.pop(stream_id, None)

    def _mark_stage(
        self,
        context: ConversationContext,
        stage: Stage,
        detail: str = '',
        turn_id: int | None = None,
    ) -> None:
        """为当前会话登记阶段看板和观测事件。

        :param context: 当前会话上下文。
        :param stage: 要登记的阶段定义。
        :param detail: 可选阶段详情，默认空字符串。
        :param turn_id: 可选回合 ID；省略时使用该 stream 的活动回合。

        副作用：
            更新全局阶段看板和当前事件上下文，并广播阶段事件。
        """
        stream = context.stream
        if stream.platform == 'desktop':
            name = '桌面'
        else:
            kind = '群聊' if stream.kind == 'group' else '私聊'
            name = f'{stream.platform.upper()} {kind} {stream.external_id}'
        active_turn_id = self._active_turns.get(stream.id) if turn_id is None else turn_id
        enter_stage(stage, stream.id, name, detail, active_turn_id)

    def record_group_observation(self, inbound: InboundMessage, reason: str = '') -> int:
        """保存被群聊回复门控拒绝的入站消息及原因。

        :param inbound: 已完成 stream、人物和身份解析的群聊消息。
        :param reason: 门控拒绝原因，默认空字符串。

        :return: 新写入的用户消息 ID。

        :raises ValueError: 入站消息不是群聊，或正文为空。
        :raises sqlite3.Error: 消息写入失败。

        副作用：
            将消息写入 L1 历史，登记 observation 事件并渲染观察输出；不启动模型生成。
        """
        context = inbound.context
        if context.stream.kind != 'group':
            raise ValueError('record_group_observation 只接受群聊消息')
        text = inbound.text.strip()
        if not text:
            raise ValueError('群聊消息正文不能为空')
        message_id = self.memory.append_message(
            context.stream.id,
            context.person.id,
            'user',
            text,
            current_time(),
        )
        self._emit_group_observation(inbound, reason, text)
        return message_id

    def _emit_group_observation(
        self,
        inbound: InboundMessage,
        reason: str,
        text: str,
    ) -> None:
        """登记已保存群聊消息的观察事件并渲染控制台输出。

        :param inbound: 已完成 stream、人物和身份解析的群聊消息。
        :param reason: 本次不回复消息的策略原因。
        :param text: 已去除首尾空白且已经写入历史的消息正文。

        :raises ValueError: 入站消息不是群聊，或正文为空。

        副作用：
            登记 observation 事件并渲染观察输出，不重复写入消息历史。
        """
        context = inbound.context
        if context.stream.kind != 'group':
            raise ValueError('_emit_group_observation 只接受群聊消息')
        if not text:
            raise ValueError('群聊消息正文不能为空')
        sender = self._sender_metadata(context)
        trace.emit(
            'observation',
            streamId=context.stream.id,
            personId=context.person.id,
            text=text,
            reason=reason,
            **sender,
        )
        render_observation(sender['senderLabel'], text, reason)

    def _session(self, stream_id: int) -> _SessionState:
        """取得或创建一个 stream 的内存会话状态。

        :param stream_id: ``streams.id`` 稳定主键。

        :return: 与 stream 绑定的 ``_SessionState``；首次访问会创建默认状态。

        副作用：
            可能向进程内会话映射新增一项，不写入数据库。
        """

        state = self._sessions.get(stream_id)
        if state is None:
            state = _SessionState()
            self._sessions[stream_id] = state
        return state

    def _refresh_session(self, context: ConversationContext, now: int) -> int | None:
        """根据静默间隔刷新会话状态、临时语气和表达样本随机种子。

        :param context: 当前消息的会话上下文。
        :param now: 当前 Unix 毫秒时间戳。

        :return: 本次应注入提示词的重逢间隔毫秒数；首次会话、群聊或间隔未超过阈值时返回
            ``None``。

        副作用：
            可能更新进程内会话的开始时间、语气、随机种子和重逢间隔；不写入数据库。
        """
        stream_id = context.stream.id
        state = self._session(stream_id)
        last = self.memory.last_message_at(stream_id)
        gap_ms = now - last if last is not None else None
        if (state.started_at is None
                or last is None
                or (gap_ms is not None and gap_ms > self._session_gap_ms)):
            state.started_at = now
            personality = self._cfg.personality
            state.tone = pick_tone(
                probability=personality.tone_probability,
                variants=personality.tone_variants,
            )
            state.seed = random.randrange(1 << 30)
        # owner 门控判断关系信号归属；group 门控判断静默间隔是否代表会话重逢，
        # 两者输入和业务语义不同，必须分别保留。
        if (not context.relationship_signals_enabled
                or context.stream.kind == 'group'
                or gap_ms is None
                or gap_ms <= self._session_gap_ms):
            state.resumption_gap_ms = None
        else:
            state.resumption_gap_ms = gap_ms
        return state.resumption_gap_ms

    def _take_resumption(self, stream_id: int) -> str | None:
        """读取并清除当前 stream 的一次性重逢间隔描述。

        :param stream_id: 目标 stream 数据库 ID。

        :return: 当前待消费的中文重逢描述；没有待消费间隔时返回 ``None``。

        副作用：
            清除会话状态中的重逢间隔，确保同一间隔只注入一次系统提示词。
        """
        state = self._session(stream_id)
        gap_ms = state.resumption_gap_ms
        state.resumption_gap_ms = None
        if gap_ms is None:
            return None
        return describe_resumption(gap_ms)

    def _session_rng(self, stream_id: int) -> random.Random:
        """创建绑定到当前会话种子的随机数生成器。

        :param stream_id: 目标 stream 数据库 ID。

        :return: 使用该会话随机种子的 ``random.Random`` 实例。

        副作用：
            仅读取进程内会话状态；不推进共享随机源状态。
        """
        return random.Random(self._session(stream_id).seed)

    def _persist_reply(self, context: ConversationContext, assistant_raw: str) -> None:
        """将已生成的助手正文写入历史，并补齐流式中断留下的未闭合 ``<say>``。

        :param context: 当前会话上下文。
        :param assistant_raw: 模型已产生的原始助手文本。

        :raises sqlite3.Error: 助手消息写入失败。

        副作用：
            可能向 L1 messages 表追加助手消息并提交事务；空正文不写入。
        """
        text = close_dangling_say(assistant_raw)
        if text:
            self.memory.append_message(
                context.stream.id,
                None,
                'assistant',
                text,
                current_time(),
            )

    def _rollback_or_keep(
        self,
        context: ConversationContext,
        user_msg_id: int,
        assistant_raw: str,
    ) -> None:
        """按已展示正文决定失败回合的历史保留策略。

        如果模型没有产生可见正文，则删除对应用户消息；如果已经产生正文，则保留
        用户消息和规范化后的助手消息，避免历史只剩无来源的助手回复。

        :param context: 当前会话上下文。
        :param user_msg_id: 已写入的用户消息 ID。
        :param assistant_raw: 模型已产生的原始助手文本。

        :raises sqlite3.Error: 删除用户消息或写入助手消息失败。

        副作用：
            修改当前回合的 L1 历史记录；不回滚已经发送给客户端的内容。
        """
        if close_dangling_say(assistant_raw):
            self._persist_reply(context, assistant_raw)
        else:
            self.memory.delete_message(context.stream.id, user_msg_id)

    def _rollback_batch_or_keep(
        self,
        context: ConversationContext,
        user_msg_ids: list[int],
        assistant_raw: str,
    ) -> None:
        """失败时保留整批用户消息与单份回复，或删除整批用户消息。"""
        if close_dangling_say(assistant_raw):
            self._persist_reply(context, assistant_raw)
            return
        for user_msg_id in user_msg_ids:
            self.memory.delete_message(context.stream.id, user_msg_id)

    def interrupt(self, stream_id: int) -> None:
        """仅取消指定 stream 的活动对话、语音任务和未完成语音缓冲。

        :param stream_id: 目标 stream 数据库 ID。

        副作用：
            设置活动回合取消事件，清除该 stream 的语音缓冲和活动回合；必要时异步调用
            音频取消回调。其他 stream 的任务和状态不受影响。
        """
        inflight = self._inflight.pop(stream_id, None)
        if inflight is not None and not inflight.task.done():
            inflight.cancel_event.set()
        # 未完成语音缓冲不能跨回合保留，否则下一轮会复用上一轮的尾部文本。
        self._speech_buffer.pop(stream_id, None)
        turn = self._active_turns.pop(stream_id, None)
        if self._cancel_audio and turn is not None:
            try:
                result = self._cancel_audio(turn)
                if inspect.isawaitable(result):
                    asyncio.create_task(result)
            except Exception as exc:
                logger.warning('cancel_audio_failed', error=str(exc))

    def speak(self, context: ConversationContext, lines: list[dict]) -> int | None:
        """投放主动生成的已结构化分句。

        :param context: 目标会话和人物归属上下文。
        :param lines: 包含 ``text`` 和可选 ``emotion`` 字段的分句列表。

        :return: 新建的回合 ID；输入为空或 stream 正忙时返回 ``None``。

        副作用：
            stream 空闲时异步推送解析事件，触发语音回调，并将带 ``<say>`` 标签的
            助手正文写入记忆；stream 正忙时只记录竞争事件。

        :raises KeyError: 分句缺少必需的 ``text`` 字段。
        """

        if not lines:
            return None
        stream_id = context.stream.id
        if not self.claim_stream(stream_id, 'proactive'):
            return None
        try:
            return self.speak_claimed(context, lines)
        finally:
            self.release_stream(stream_id, 'proactive')

    def speak_claimed(self, context: ConversationContext, lines: list[dict]) -> int:
        """投放已经取得 proactive stream 占用权的结构化分句。"""
        if not lines:
            raise ValueError('主动分句不能为空')
        stream_id = context.stream.id
        if self._stream_claims.get(stream_id) != 'proactive':
            raise RuntimeError('主动投放前必须取得 stream 占用权')
        turn = self._next_turn()
        self._active_turns[stream_id] = turn
        texts: list[str] = []
        asyncio.create_task(self._emit(stream_id, 'chat.start', {
            'turnId': turn,
            'kind': 'start',
        }))
        for line in lines:
            # 先发解析事件再写入记忆，使桌面端和外部平台共享同一回合轨迹。
            asyncio.create_task(
                self._emit_parse_event(context, turn, SayEvent(emotion=line.get('emotion')))
            )
            asyncio.create_task(self._emit_parse_event(context, turn, TextEvent(value=line['text'])))
            asyncio.create_task(self._emit_parse_event(context, turn, SayEndEvent()))
            self._dispatch_speech(context, line['text'], turn)
            texts.append(f'<say>{line["text"]}</say>')
        asyncio.create_task(self._emit(context.stream.id, 'chat.done', {'turnId': turn, 'kind': 'done'}))
        # 记忆保存带有 <say> 边界，后续摘要和回放可以区分主动分句而不依赖前端事件。
        self.memory.append_message(
            stream_id,
            None,
            'assistant',
            ''.join(texts),
        )
        return turn

    async def compose_proactive(
        self,
        context: ConversationContext,
        situation: str,
    ) -> list[dict] | None:
        """构造并调用主动消息模型，解析为结构化分句。

        :param context: 目标会话和人物归属上下文。
        :param situation: 当前前台活动或触发意图的情境描述。

        :return: 模型输出解析后的分句列表；未配置主动模型、调用失败或正文无法解析时
            返回 ``None``。

        副作用：
            可能确保日程存在、读取关系/记忆/历史、调用主动模型并记录请求事件。
            模型异常被转换为 ``None``，调用方据此放弃本次投放。
        """

        if not self._proactive_provider:
            return None
        now = current_time()
        self._refresh_session(context, now)
        if self._schedule:
            await self._schedule.ensure(now)
        # 主动消息使用与普通对话相同的人格和记忆边界，但只读取少量上下文以控制延迟。
        persona_desc = describe_persona(self.persona.get(context.person.id))
        acquaintance = describe_acquaintance(
            self.memory.first_seen_at(context.person.id),
            now,
        )
        schedule_desc = (self._schedule.describe(now, self.current_sleep())
                         if self._schedule else '')
        render_params: dict[str, dict[str, str]] = {}
        base_prompt = build_system_prompt(
            now=datetime.fromtimestamp(now / 1000),
            persona=persona_desc,
            acquaintance=acquaintance,
            facts=[
                fact.content
                for fact in self.memory.top_facts(context.person.id, 5, now)
            ],
            episodes=[episode.summary for episode in self.memory.recent_episodes(
                context.stream.id, 2
            )],
            schedule=schedule_desc,
            expression_habits=render_expression_habits(
                sample_expression_habits(
                    self._proactive_expression_habits,
                    limit=3,
                    rng=self._session_rng(context.stream.id),
                )
            ),
            tone=self._session(context.stream.id).tone,
            resumption=self._take_resumption(context.stream.id),
            render_params=render_params,
            **self._prompt_config_kwargs(),
        )
        system = build_proactive_prompt(base_prompt, situation, render_params)
        raw = ''
        try:
            # 主动模型只接收一个 system 消息，避免将触发情境误当作用户新问题。
            messages = [{'role': 'system', 'content': system}]
            trace.emit(
                'llm_request',
                messages=messages,
                temperature=self._proactive_temperature,
                maxTokens=self._proactive_max_tokens,
                renderParams=render_params,
                **prompt_metadata('chat.proactive', CHAT_PROACTIVE_TEMPLATE_IDS),
            )
            bind_render_params(render_params)
            async for chunk in self._proactive_provider.stream(
                messages=messages,
                temperature=self._proactive_temperature,
                max_tokens=self._proactive_max_tokens,
            ):
                if chunk.get('text'):
                    raw += chunk['text']
        except Exception:
            return None
        # 统一通过响应解析器提取 <say> 边界，保证主动消息与普通流式回复格式一致。
        return _extract_lines(raw)

    def diary_payload(self, now: int | None = None) -> dict:
        """构造日记页面所需的历史 episode、当天日程和事实摘要。

        :param now: 可选的当前毫秒时间戳；省略时读取统一时钟。

        :return: 可序列化的日记载荷，不包含模型凭证。
        """

        now = now or current_time()
        memories = [
            {'content': fact.content, 'frozen': fact.frozen}
            for fact in self.memory.all_facts(self._desktop_context.person.id, now)
        ]
        today = self._schedule.get(now) if self._schedule else None
        return {
            'entries': self.memory.all_episodes(),
            'today': _plan_to_dict(today) if today else None,
            'memories': memories,
            'now': now,
        }

    def observability_snapshot(self, stream_id: int, now: int | None = None) -> dict:
        """读取指定 stream 的会话观察快照。

        :param stream_id: ``streams.id`` 稳定主键。
        :param now: 可选的当前毫秒时间戳；省略时读取统一时钟。

        :return: 包含主体精力、日程、待处理消息数和会话参与人的可序列化字典；不展开
            单个人物的关系和事实。

        :raises ValueError: stream 不存在时由注册表抛出。
        """
        now = now or current_time()
        stream = self._registry.stream(stream_id)
        participants = [
            self._conversation_participant(person, stream)
            for person in self._registry.list_persons(stream.id)
        ]
        return {
            'now': now,
            'selfState': {
                'energy': self.persona.inspect(self._desktop_context.person.id).energy,
            },
            'schedule': _plan_to_dict(self._schedule.get(now)) if self._schedule else None,
            'conversation': {
                'workingMessages': self.memory.pending_count(stream.id),
                'participants': participants,
            },
        }

    def list_person_profiles(self) -> List[Dict[str, Any]]:
        """列出人物画像索引。

        :return: 每个人物的身份与会话归属摘要，不展开关系状态和事实正文。
        """
        return [self._person_summary(person) for person in self._registry.list_persons()]

    def person_profile(self, person_id: int, now: int | None = None) -> Dict[str, Any]:
        """组装指定人物的身份、关系和事实画像。

        :param person_id: ``persons.id`` 稳定主键。
        :param now: 可选的当前毫秒时间戳；省略时读取统一时钟。

        :return: 包含人物摘要、亲密度和当前事实列表的字典。

        :raises ValueError: 人物不存在时由注册表抛出。
        """
        now = now or current_time()
        person = self._registry.person(person_id)
        summary = self._person_summary(person)
        state = self.persona.inspect(person.id)
        # 关系快照与事实列表使用同一时间点，避免画像字段跨时钟读取产生不一致。
        summary.update({
            'bond': {
                'intimacy': state.intimacy,
                'updatedAt': state.updated_at,
            },
            'facts': [
                {
                    'id': fact.id,
                    'kind': fact.kind,
                    'content': fact.content,
                    'retention': fact.retention,
                    'score': fact.score,
                    'dueAt': fact.due_at,
                    'frozen': fact.frozen,
                }
                for fact in self.memory.all_facts(person.id, now)
            ],
        })
        return summary

    def _person_summary(
        self,
        person: PersonRef,
        preferred_platform: str | None = None,
    ) -> Dict[str, Any]:
        """将人物注册表引用转换为跨语言人物画像摘要。

        :param person: 已解析的人物引用。
        :param preferred_platform: 可选优先显示平台；没有匹配身份时按注册表顺序回退。

        :return: 包含人物基本信息、平台身份、实际发言 stream 和群成员关系的可序列化字典。

        :raises ValueError: 人物不存在时由注册表查询抛出。
        :raises sqlite3.Error: 读取身份、stream 或群成员关系失败。

        副作用：
            只读注册表，不调用模型、不修改人物数据。
        """
        identities = self._registry.list_identities(person.id)
        # 身份、stream 和群成员分开返回，调用方可按平台权限选择展示字段。
        return {
            'id': person.id,
            'kind': person.kind,
            'displayName': self._profile_display_name(person, identities, preferred_platform),
            'firstSeenAt': person.first_seen_at,
            'identities': [
                {
                    'platform': identity.platform,
                    'externalId': identity.external_id,
                    'displayName': identity.display_name,
                }
                for identity in identities
            ],
            'streams': [
                {
                    'id': stream.id,
                    'platform': stream.platform,
                    'kind': stream.kind,
                    'externalId': stream.external_id,
                }
                for stream in self._registry.list_person_streams(person.id)
            ],
            'groupMemberships': [
                {
                    'streamId': membership.stream_id,
                    'groupExternalId': membership.group_external_id,
                    'groupCard': membership.group_card,
                }
                for membership in self._registry.group_memberships(person.id)
            ],
        }

    def _conversation_participant(
        self,
        person: PersonRef,
        stream: StreamRef,
    ) -> Dict[str, Any]:
        """构造当前会话参与人的身份、显示名和群名片摘要。

        :param person: 会话参与人的人物引用。
        :param stream: 当前会话引用。

        :return: 包含人物 ID、类型、会话显示名、平台外部 ID、账号昵称和群名片的字典。

        :raises RuntimeError: 非桌面会话缺少平台身份，或群聊缺少群成员关系。
        :raises ValueError: 人物或 stream 不存在，或平台没有可用显示名。
        :raises sqlite3.Error: 读取归属关系失败。
        """
        identities = self._registry.list_identities(person.id)
        # 非桌面会话必须绑定稳定 identity；仅桌面会话允许使用内部人物资料生成显示名。
        identity = next(
            (item for item in identities if item.platform == stream.platform),
            None,
        )
        if stream.platform != 'desktop' and identity is None:
            raise RuntimeError(
                f'person {person.id} 在会话平台 {stream.platform} 缺少 identity'
            )
        group_card = ''
        if stream.kind == 'group':
            # 群名片属于 person-stream 关系，不能从全局 identity 推导。
            membership = next(
                (
                    item for item in self._registry.group_memberships(person.id)
                    if item.stream_id == stream.id
                ),
                None,
            )
            if membership is None:
                raise RuntimeError(
                    f'person {person.id} 在群 stream {stream.id} 缺少 membership'
                )
            group_card = membership.group_card
        return {
            'id': person.id,
            'kind': person.kind,
            'displayName': (
                self._registry.stream_display_name(person.id, stream.id)
                if stream.platform != 'desktop'
                else self._profile_display_name(person, identities, stream.platform)
            ),
            'externalId': identity.external_id if identity is not None else '',
            'nickname': identity.display_name if identity is not None else '',
            'groupCard': group_card,
        }

    def _sender_metadata(self, context: ConversationContext) -> Dict[str, str]:
        """生成观测和终端展示使用的发送者元数据。

        :param context: 已解析当前 stream、人物、身份和群名片的会话上下文。

        :return: 包含外部 ID、账号昵称、群名片、最终显示名和展示标签的字符串字典；桌面消息
            使用固定的本地用户标签。

        :raises RuntimeError: 非桌面上下文缺少稳定平台身份。
        """
        identity = context.identity
        if context.stream.platform == 'desktop':
            return {
                'senderExternalId': '',
                'senderNickname': '',
                'senderGroupCard': '',
                'senderDisplayName': '你',
                'senderLabel': '你',
            }
        if identity is None:
            raise RuntimeError('非桌面入站上下文缺少稳定平台 identity')
        display_name = context.group_card or identity.display_name
        if identity.platform == 'qq':
            if context.group_card and context.group_card != identity.display_name:
                sender_label = (
                    f'{context.group_card}（QQ昵称：{identity.display_name} · '
                    f'QQ号：{identity.external_id}）'
                )
            else:
                sender_label = f'{identity.display_name}（QQ号：{identity.external_id}）'
        else:
            sender_label = f'{display_name}（{identity.platform}：{identity.external_id}）'
        return {
            'senderExternalId': identity.external_id,
            'senderNickname': identity.display_name,
            'senderGroupCard': context.group_card,
            'senderDisplayName': display_name,
            'senderLabel': sender_label,
        }

    def _profile_display_name(
        self,
        person: PersonRef,
        identities: List[IdentityRef],
        preferred_platform: str | None,
    ) -> str:
        """按平台优先级选择人物画像显示名，并为未绑定身份生成明确占位名。

        :param person: 人物引用。
        :param identities: 已加载的平台身份列表。
        :param preferred_platform: 可选优先平台标识。

        :return: 优先平台显示名、首个身份显示名、owner 用户昵称或未绑定联系人占位名。

        副作用：
            仅读取人物和身份数据，不修改配置或注册表。
        """
        preferred = next(
            (identity for identity in identities if identity.platform == preferred_platform),
            None,
        )
        if preferred is not None:
            return preferred.display_name
        if identities:
            return identities[0].display_name
        if person.kind == 'owner':
            user_nickname = self._cfg.bot.user_nickname.strip()
            if user_nickname:
                return user_nickname
            return '用户本人'
        return f'未绑定联系人 #{person.id}'

    def _next_turn(self) -> int:
        """分配进程内单调递增的回合 ID。

        :return: 新分配的正整数回合 ID。

        副作用：
            更新进程内回合计数器；不会写入数据库。
        """

        self._turn_id += 1
        return self._turn_id

    def _relationship_kwargs(self) -> dict:
        """读取构建关系提示词所需的用户称呼配置。

        :return: 包含 ``user_nickname`` 和 ``relationship`` 的字典。
        """
        bot_cfg = self._cfg.bot
        return {
            'user_nickname': bot_cfg.user_nickname,
            'relationship': bot_cfg.relationship,
        }

    def _prompt_config_kwargs(self) -> dict:
        """读取系统提示词所需的角色与用户配置。

        :return: 包含角色名、别名、用户称呼、关系、生日、人设和回复风格的字典。
        """
        bot = self._cfg.bot
        personality = self._cfg.personality
        return {
            'name': bot.name,
            'aliases': bot.aliases,
            'user_nickname': bot.user_nickname,
            'relationship': bot.relationship,
            'birthday': personality.birthday,
            'personality': personality.personality,
            'reply_style': personality.reply_style,
        }

    async def _pick_expression_habits(
        self,
        context: ConversationContext,
        query: str,
        history: list[dict[str, str]],
        signal: asyncio.Event | None,
    ) -> list[ExpressionSample]:
        """为当前回复选择表达习惯样本。

        :param context: 当前会话上下文，用于阶段和错误 trace。
        :param query: 当前用户文本或主动情境。
        :param history: 已组装的对话历史。
        :param signal: 可选的取消信号。

        :return: 选择出的表达样本；选择器未配置、输入错误或非中断模型错误时返回空列表。

        :raises LlmError: 选择过程被主动中断时向上抛出。
        """
        if self._expression_selector is None:
            trace.emit('expression_select', source='disabled', count=0)
            return []
        self._mark_stage(context, EXPRESSION)
        try:
            # 只把最近历史传给选择器，避免表达习惯选择占用完整上下文预算。
            picked = await self._expression_selector.select(query, history[-8:], signal=signal)
        except LlmError as exc:
            if exc.kind == 'aborted':
                raise
            return self._expression_selection_failed(context, type(exc).__name__, str(exc))
        except ValueError as exc:
            return self._expression_selection_failed(context, 'ValueError', str(exc))
        trace.emit(
            'expression_select',
            source='model',
            count=len(picked),
            habits=picked,
        )
        return picked

    def _expression_selection_failed(
        self,
        context: ConversationContext,
        error_type: str,
        message: str,
    ) -> list[ExpressionSample]:
        """记录表达样本选择失败并跳过本轮样本注入。

        :param context: 当前会话上下文。
        :param error_type: 错误类型名称。
        :param message: 错误详情。

        :return: 空表达样本列表。

        副作用：
            写入模型请求诊断快照和观察事件。
        """
        snapshot = dump_llm_request('expression', error_type, message, {
            'stage': trace.current_stage_id(),
            'streamId': context.stream.id,
            'turnId': self._active_turns.get(context.stream.id),
        })
        logger.error('expression_select_failed', errorType=error_type, error=message,
                     snapshot=str(snapshot) if snapshot else None)
        trace.emit('expression_select', source='model', count=0,
                   errorType=error_type, error=message,
                   snapshotPath=str(snapshot) if snapshot else None)
        return []

    def _prepare_turn_context(
        self,
        context: ConversationContext,
        query: str,
        now: int,
        platform_bot_name: str | None = None,
    ) -> _PreparedTurnContext:
        """组装不依赖模型调用的完整回合上下文。

        :param context: 当前会话上下文。
        :param query: 当前用户文本。
        :param now: 当前毫秒时间戳。
        :param platform_bot_name: 当前平台登录昵称；仅用于当前入站消息的称呼匹配。
        :return: 可供动作决策读取、并可在确认回复后继续增强的上下文。

        副作用：
            读取记忆、人格、日程和活动状态，并消费一次重逢提示；不调用模型，
            不强化召回事实。
        """
        fact_candidates = self.memory.recall_facts(
            context.person.id,
            query,
            self._fact_recall_limit,
            now,
            reinforce_matches=False,
            return_candidates=True,
        )
        recalled = self.memory.recall_episodes(
            context.stream.id,
            query,
            self._recalled_episode_limit,
        )
        recent = self.memory.recent_episodes(
            context.stream.id,
            self._recent_episode_limit,
        )
        seen_ids: set[int] = set()
        episodes = []
        for e in [*recalled, *recent]:
            # 召回结果与最近 episode 可能重叠，按 ID 去重后再限制上下文数量。
            if e.id not in seen_ids:
                seen_ids.add(e.id)
                episodes.append(e)
        episodes = episodes[:self._episode_context_limit]
        state = self.persona.get(context.person.id)
        persona_desc = describe_persona(state)
        acquaintance = describe_acquaintance(
            self.memory.first_seen_at(context.person.id),
            now,
        )
        schedule_desc = (self._schedule.describe(now, self.current_sleep()) if self._schedule else None)
        resumption = self._take_resumption(context.stream.id)
        wm = self.memory.working_memory(
            context.stream.id,
            self._working_memory_messages,
        )
        raw_history = self._history_for_context(context, wm)
        # 感知开关和 owner 归属分别控制“能否看见”和“是否允许应用用户关系状态”。
        activity = None
        if (context.stream.kind in self._perception_surfaces
                and context.person.kind == 'owner'
                and self._activity is not None):
            activity = self._activity()
        return _PreparedTurnContext(
            context=context,
            query=query,
            now=now,
            platform_bot_name=platform_bot_name,
            fact_candidates=fact_candidates,
            episodes=[episode.summary for episode in episodes],
            persona=persona_desc,
            acquaintance=acquaintance,
            activity=activity,
            schedule=schedule_desc,
            resumption=resumption,
            raw_history=raw_history,
        )

    def _render_prepared_context(
        self,
        prepared: _PreparedTurnContext,
        *,
        facts: list[RecalledFact] | None = None,
        expression_habits: str | None = None,
        render_params: dict[str, dict[str, str]] | None = None,
    ) -> list[dict]:
        """将同一份已组装上下文渲染为模型消息。

        :param prepared: 决策前已完成一次性组装的回合上下文。
        :param facts: 可选的增强后事实列表；省略时使用词面排序结果。
        :param expression_habits: 可选表达习惯提示词块。
        :param render_params: 可选提示词渲染参数收集字典。
        :return: 首项为 system 消息、后续为裁剪后历史消息的列表。
        副作用：只读取配置和会话语调，不读写数据库、不调用模型。
        """
        selected_facts = (
            prepared.fact_candidates[:self._fact_recall_limit]
            if facts is None
            else facts
        )
        system = build_system_prompt(
            now=datetime.fromtimestamp(prepared.now / 1000),
            persona=prepared.persona,
            acquaintance=prepared.acquaintance,
            facts=[fact.content for fact in selected_facts],
            episodes=prepared.episodes,
            activity=prepared.activity,
            schedule=prepared.schedule,
            expression_habits=expression_habits,
            tone=self._session(prepared.context.stream.id).tone,
            resumption=prepared.resumption,
            platform_name=prepared.platform_bot_name,
            render_params=render_params,
            **self._prompt_config_kwargs(),
        )
        # 读取历史时再次规范化，兼容早期中断留下的悬空标签；该操作对干净历史幂等。
        history = normalize_history(prepared.raw_history)
        return [{'role': 'system', 'content': system}, *fit_char_budget(history)]

    async def _enrich_prepared_context(
        self,
        prepared: _PreparedTurnContext,
        signal: asyncio.Event | None,
        render_params: dict[str, dict[str, str]],
    ) -> list[dict]:
        """确认回复后，在既有上下文上附加向量与表达模型增强。

        :param prepared: 动作决策实际读取的同一份上下文。
        :param signal: 可选的表达选择取消信号。
        :param render_params: 提示词渲染参数收集字典。
        :return: 使用增强后事实排序和表达习惯渲染的最终模型消息。
        副作用：调用向量与表达模型，并强化最终实际用于回复的事实 ID。
        """
        query_embedding = await self._vector.embed_query(prepared.query)
        facts = self.memory.rank_recalled_facts(
            prepared.fact_candidates,
            query_embedding,
            self._fact_recall_limit,
        )
        self.memory.reinforce_recalled_facts(
            prepared.context.person.id,
            facts,
            prepared.now,
        )
        expression_habits = render_expression_habits(
            await self._pick_expression_habits(
                prepared.context,
                prepared.query,
                prepared.raw_history,
                signal,
            )
        )
        return self._render_prepared_context(
            prepared,
            facts=facts,
            expression_habits=expression_habits,
            render_params=render_params,
        )

    def bot_names(self, platform_name: str | None = None) -> tuple[str, ...]:
        """返回群聊文本称呼候选。

        :param platform_name: 可选的当前平台登录昵称；仅追加到本次调用结果，不修改
                全局配置。

        :return: 去重后的主体名称和别名元组。

        :raises ValueError: ``platform_name`` 只有空白字符。
        """
        names = list(self._bot_names)
        if platform_name is not None:
            normalized = platform_name.strip()
            if not normalized:
                raise ValueError('平台机器人昵称不能为空')
            names.append(normalized)
        return tuple(dict.fromkeys(names))

    @property
    def at_mention_must_reply(self) -> bool:
        """返回协议 @ 是否绕过群聊回复门控。

        :return: 配置值。
        """

        return self._at_mention_must_reply

    @property
    def name_mention_probability(self) -> float:
        """返回文本称呼触发回复的概率。

        :return: [0, 1] 范围内的配置值。
        """

        return self._name_mention_probability

    def _history_for_context(self, context: ConversationContext, messages: list[Any]) -> list[dict]:
        """将记忆消息转换为模型历史，并在群聊中补充发送者显示名。

        :param context: 当前会话上下文。
        :param messages: 记忆服务返回的消息对象列表。

        :return: 仅含 ``role`` 和 ``content`` 的模型消息列表；原始记忆对象不被修改。

        :raises RuntimeError: 群聊用户消息缺少发送者人物 ID。
        """
        history: list[dict] = []
        for message in messages:
            content = message.content
            if context.stream.kind == 'group' and message.role == 'user':
                if message.sender_person_id is None:
                    raise RuntimeError('群聊 user 历史缺少 sender_person_id')
                name = self._registry.stream_display_name(
                    message.sender_person_id,
                    context.stream.id,
                )
                content = f'{name}: {content}'
            history.append({'role': message.role, 'content': content})
        return history

    async def _consume_events(self, events: Iterable[ParseEvent], sink: _TurnSink) -> None:
        """消费解析事件，应用副作用并按平台选择输出路径。

        :param events: 响应解析器产生的事件迭代器。
        :param sink: 当前回合的聚合状态。

        副作用：
            写入事实、人格和 promise 状态，推送桌面解析事件，或向非桌面 sink
            聚合按 ``<say>`` 边界切分的出站文本；取消信号会提前结束消费。
        """
        context = sink.context
        for event in events:
            if sink.cancel_event.is_set():
                sink.interrupted = True
                return
            self._handle_side_effects(
                context, event, sink.now, sink.turn, sink.side_effects, sink.source_text,
            )
            if context.stream.platform == 'desktop':
                self._track_speech(context, event, sink.turn)
                await self._emit_parse_event(context, sink.turn, event)
            else:
                sink.segment = _collect_outbound_segment(event, sink.segments, sink.segment)

    def _handle_side_effects(
        self, context: ConversationContext, event: ParseEvent, now: int, turn: int,
        sink: list[dict] | None = None,
        source_text: str | None = None,
    ) -> None:
        """应用单个解析事件携带的事实、情绪或约定副作用。

        :param context: 当前会话上下文。
        :param event: 解析器产生的事件。
        :param now: 当前回合的毫秒时间戳。
        :param turn: 当前对话回合 ID。
        :param sink: 可选的面板副作用列表。
        :param source_text: 当前用户原话；promise 事件必须提供。

        :raises ValueError: promise 事件缺少用户原话。
        :raises Exception: 记忆、人格或 promise 回调写入失败时直接传播。

        副作用：
            可能写入事实、人格状态或待投放 promise，并发出对应观察事件。
        """

        if isinstance(event, MemoryEvent) and event.content:
            # 事实先写入统一记忆存储，向量补算由上层异步服务处理。
            memory_kind = event.memory_type or '未分类'
            self.memory.add_fact(
                context.person.id,
                FactInput(content=event.content, kind=memory_kind),
                now,
            )
            trace.emit('memory_fact', turnId=turn, content=event.content, memoryKind=memory_kind)
            if sink is not None:
                sink.append({'kind': 'memory_fact', 'content': event.content, 'memoryKind': memory_kind})
        elif isinstance(event, MoodEvent):
            # 群聊关系增量由上下文决定权重，Persona 本身不感知平台会话。
            self.persona.apply_mood(
                context.person.id,
                MoodDelta(favor=event.favor, energy=event.energy),
                now,
                weight=self._persona_weight(context),
            )
            trace.emit('mood_delta', turnId=turn, favor=event.favor, energy=event.energy)
            if sink is not None:
                sink.append({'kind': 'mood_delta', 'favor': event.favor, 'energy': event.energy})
        elif isinstance(event, PromiseEvent):
            # promise 只允许 owner 关系信号进入主动调度，联系人消息不能改变主体计划。
            if not context.relationship_signals_enabled:
                logger.warning(
                    'promise_rejected_for_person',
                    turnId=turn,
                    personKind=context.person.kind,
                )
                return
            if self._promise_handler is None:
                logger.warning('promise_handler_missing', turnId=turn)
                return
            if source_text is None:
                raise ValueError('约定事件必须关联本轮用户原话')
            self._promise_handler(event.at, source_text)
            trace.emit('promise_stashed', turnId=turn, at=event.at, subject=source_text)
            if sink is not None:
                sink.append({'kind': 'promise_stashed', 'at': event.at, 'subject': source_text})

    def _dispatch_speech(self, context: ConversationContext, text: str, turn: int) -> None:
        """将一条桌面 ``<say>`` 分句交给语音回调。

        :param context: 当前会话上下文。
        :param text: 待合成的分句文本。
        :param turn: 关联的对话回合 ID。

        副作用：
            可能调用同步或异步语音回调；回调异常只记录警告，不影响文本消息流程。
        """
        if context.stream.kind != 'desktop' or not self._speak_audio:
            return
        line = text.strip()
        if not line:
            return
        try:
            result = self._speak_audio(line, turn)
            # TtsService.speak 已自行创建任务，其他注入实现可能返回协程，因此分别处理两种返回形式。
            if inspect.isawaitable(result):
                asyncio.create_task(result)
        except Exception as exc:
            logger.warning('speak_audio_failed', turnId=turn, error=str(exc))

    def _track_speech(self, context: ConversationContext, event: ParseEvent, turn: int) -> None:
        """在流式解析过程中聚合一条完整 ``<say>`` 分句并触发合成。

        :param context: 当前会话上下文。
        :param event: 当前解析事件。
        :param turn: 关联的对话回合 ID。

        副作用：
            更新指定 stream 的台词缓冲；收到 ``SayEndEvent`` 时调用语音回调。
            每个分句独立发送，以便音频与桌面解析事件保持顺序。
        """
        if not self._speak_audio:
            return
        stream_id = context.stream.id
        if isinstance(event, SayEvent):
            self._speech_buffer[stream_id] = []
        elif isinstance(event, TextEvent):
            self._speech_buffer.setdefault(stream_id, []).append(event.value)
        elif isinstance(event, SayEndEvent):
            line = ''.join(self._speech_buffer.pop(stream_id, []))
            self._dispatch_speech(context, line, turn)

    async def _emit(self, stream_id: int, channel: str, payload: Any) -> None:
        """向客户端推送事件并隔离推送层异常。

        :param stream_id: 目标 stream ID。
        :param channel: 事件频道名称。
        :param payload: 事件载荷。

        副作用：
            调用注入的事件回调；回调异常只写警告日志，不阻断对话任务。
        """

        try:
            await self._push_event(channel, payload, stream_id)
        except Exception as exc:
            logger.warning('emit_failed', streamId=stream_id, channel=channel, error=str(exc))

    async def _emit_parse_event(
        self,
        context: ConversationContext,
        turn: int,
        event: ParseEvent,
    ) -> None:
        """将解析事件转换为桌面端 ``chat.event`` 载荷。

        :param context: 当前会话上下文。
        :param turn: 对话回合 ID。
        :param event: 解析器产生的事件。

        副作用：
            仅在 desktop stream 上通过 ``_emit`` 推送一个解析事件；未知事件类型
            不产生输出。
        """

        if context.stream.platform != 'desktop':
            return
        # 仅传输前端可渲染的事件字段，内部对象和未定义事件不越过平台边界。
        if isinstance(event, SayEvent):
            ev = {'turnId': turn, 'kind': 'parse',
                  'event': {'type': 'say', **({'emotion': event.emotion} if event.emotion else {}),
                             **({'gesture': event.gesture} if event.gesture else {})}}
        elif isinstance(event, TextEvent):
            ev = {'turnId': turn, 'kind': 'parse', 'event': {'type': 'text', 'value': event.value}}
        elif isinstance(event, SayEndEvent):
            ev = {'turnId': turn, 'kind': 'parse', 'event': {'type': 'sayEnd'}}
        elif isinstance(event, MemoryEvent):
            ev = {'turnId': turn, 'kind': 'parse',
                  'event': {'type': 'memory', 'content': event.content,
                             **({'memoryType': event.memory_type} if event.memory_type else {})}}
        elif isinstance(event, MoodEvent):
            ev = {'turnId': turn, 'kind': 'parse',
                  'event': {'type': 'mood',
                             **({'favor': event.favor} if event.favor is not None else {}),
                             **({'energy': event.energy} if event.energy is not None else {})}}
        else:
            return
        await self._emit(context.stream.id, 'chat.event', ev)

    async def _dispatch_outbound(
        self,
        context: ConversationContext,
        turn: int,
        segments: list[str],
    ) -> None:
        """将非桌面整轮回复交给平台 broker。

        :param context: 目标会话上下文。
        :param turn: 对话回合 ID。
        :param segments: 已按 ``<say>`` 边界切分的正文列表。

        副作用：
            可能调用平台驱动并写入投递观察事件；空列表只记录警告并返回。

        :raises RuntimeError: 桌面 stream 误走 broker，或非桌面 stream 未配置 broker。
        :raises DeliveryError: 平台驱动未注册或投递失败时由 broker 传播。
        """

        self._mark_stage(context, DISPATCHING, turn_id=turn)
        if context.stream.platform == 'desktop':
            raise RuntimeError('desktop stream 不能经由非桌面 broker 投递')
        if self._broker is None:
            raise RuntimeError('非桌面 stream 未配置 PlatformBroker')
        if not segments:
            logger.warning('outbound_reply_empty', streamId=context.stream.id, turnId=turn)
            return
        # Broker 负责平台驱动选择和失败归一化，服务层只提交已切分的整轮正文。
        receipt = await self._broker.dispatch(OutboundMessage(
            stream=context.stream,
            segments=segments,
        ))
        trace.emit(
            'outbound_delivered',
            platform=receipt.platform,
            streamId=receipt.stream_id,
            turnId=turn,
        )

    async def _maybe_summarize(self, stream_id: int) -> None:
        """在待摘要消息达到阈值时异步生成并保存 episode。

        :param stream_id: 待检查的会话 stream ID。

        副作用：
            读取待摘要消息、调用摘要模型并写入 episode；同一 stream 同时只允许
            一个摘要任务。摘要异常不会影响已完成的对话回合。
        """

        if stream_id in self._summarizing or not self._summary_provider:
            return
        if self.memory.pending_count(stream_id) < self._summarize_trigger_messages:
            return
        self._summarizing.add(stream_id)
        try:
            # 单个 stream 使用内存集合去重，避免连续回复重复启动摘要任务。
            batch = self.memory.oldest_pending(
                stream_id,
                self._summarize_batch_messages,
            )
            if len(batch) < 4:
                return
            # 摘要输入只保留 role/content，避免将内部消息 ID 暴露给模型。
            msgs = [{'role': m['role'], 'content': m['content']} for m in batch]
            episode = await summarize(
                self._summary_provider,
                msgs,
                temperature=self._summary_temperature,
                max_tokens=self._summary_max_tokens,
                character_name=self._bot_display_name,
                character_personality=self._summary_personality,
            )
            if not episode:
                return
            # episode 写入后由 MemoryStore 标记对应消息已处理，下一轮从队列继续。
            self.memory.add_episode(
                stream_id,
                EpisodeInput(
                    summary=episode.summary,
                    cues=episode.recall_cues,
                    started_at=batch[0]['created_at'],
                    ended_at=batch[-1]['created_at'],
                    message_ids=[message['id'] for message in batch],
                ),
            )
        except Exception:
            # 摘要是后台附加任务，失败不能回滚已完成的对话或阻断下一轮。
            pass
        finally:
            self._summarizing.discard(stream_id)


def _extract_lines(raw: str) -> list[dict] | None:
    """将带协议标签的完整模型响应提取为分句字典。

    :param raw: 模型返回的完整文本。

    :return: 每个 ``<say>`` 分句的 ``text`` 和可选 ``emotion`` 字典；没有完整正文时
        返回 ``None``。
    """

    parser = ResponseParser()
    lines: list[dict] = []
    cur: dict | None = None
    for e in [*parser.push(raw), *parser.flush()]:
        if isinstance(e, SayEvent):
            cur = {'text': '', **({'emotion': e.emotion} if e.emotion else {})}
        elif isinstance(e, TextEvent) and cur is not None:
            cur['text'] += e.value
        elif isinstance(e, SayEndEvent) and cur is not None:
            if cur['text'].strip():
                lines.append({**cur, 'text': cur['text'].strip()})
            cur = None
    return lines if lines else None


def _collect_outbound_segment(
    event: ParseEvent,
    segments: list[str],
    current: list[str] | None,
) -> list[str] | None:
    """按解析器识别的 ``say`` 边界收集外部平台正文。

    :param event: 当前解析事件。
    :param segments: 已完成的分句列表，会被原地追加。
    :param current: 当前尚未结束的分句片段列表。

    :return: 更新后的当前分句片段；收到 ``SayEndEvent`` 后返回 ``None``。

    副作用：
        可能向 ``segments`` 原地追加一条非空分句，不重新扫描完整响应文本。
    """
    if isinstance(event, SayEvent):
        return []
    if isinstance(event, TextEvent):
        if current is None:
            current = []
        current.append(event.value)
        return current
    if isinstance(event, SayEndEvent):
        if current is not None:
            text = ''.join(current).strip()
            if text:
                segments.append(text)
        return None
    return current


def _plan_to_dict(plan: DayPlan | None) -> dict | None:
    """将可选日程对象转换为前端使用的字典。

    :param plan: 待转换日程；可以为 ``None``。

    :return: JSON 兼容日程字典；输入为 ``None`` 时返回 ``None``。
    """

    if plan is None:
        return None
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
