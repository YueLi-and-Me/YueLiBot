"""
对话编排服务。

持有所有后端资源：按角色拆分的 LLM provider、MemoryStore、Persona、DayPlanService。
通过 WebSocket push 推事件给 Electron 主进程。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Mapping

import asyncio
import inspect
import json
import random
import sqlite3

from .chat_image import ChatImageDescriber, merge_image_descriptions
from .trace_console import mark_turn_start, render_action_decision, render_observation, render_turn, render_turn_error
from .vector import VectorService

from src.core.agent.character import pick_tone
from src.core.agent.action import ActionContext, ActionPolicy, AlwaysReplyPolicy, TurnPlanner
from src.core.agent.action_protocol import (
    ActionDecisionEvent,
    DecisionFrame,
    GateDisposition,
    GateInputFacts,
    PlatformCapabilities,
    available_actions,
)
from src.core.agent.conversation import ConversationAgent
from src.core.agent.conversation_gate import (
    GateRequest,
    GateResult,
    decide_disposition,
    mentions_bot_name,
)
from src.core.agent.expression import ExpressionSample, render_expression_habits, sample_expression_habits
from src.core.agent.expression_select import ExpressionSelector
from src.core.agent.history import close_dangling_say, fit_char_budget, normalize_history, strip_say_tags
from src.core.agent.parser import (
    MemoryEvent, MoodEvent, ParseEvent, PromiseEvent, ResponseParser, SayEndEvent, SayEvent, TextEvent,
)
from src.core.agent.prompt import (
    build_proactive_prompt,
    build_system_prompt,
    describe_resumption,
    render_action_protocol,
)
from src.core.agent.reply_necessity import (
    PRESENCE_WINDOW_MS,
    frequency_trigger_threshold,
    score_reply_necessity,
)
from src.core.agent.summarize import summarize
from src.core.awareness.sleep import SleepState
from src.core.common.clock import now as current_time
from src.core.common.logger import get_logger
from src.core.config.schema import Config, ConversationConfig
from src.core.llm_models.openai import LlmError
from src.core.llm_models.protocol import LlmProvider
from src.core.llm_models.snapshot import bind_render_params, dump as dump_llm_request
from src.core.memory.store import EpisodeInput, FactInput, MemoryStore, RecalledFact, StoredMessage
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
    CHAT_CONVERSATION_TEMPLATE_IDS,
    CHAT_PROACTIVE_TEMPLATE_IDS,
    CHAT_SYSTEM_TEMPLATE_IDS,
    CHAT_SYSTEM_VARIANT_COMPONENTS,
    prompt_metadata,
)
from src.core.schedule.plan import DayPlan, DayPlanService, ScheduleSleepState

logger = get_logger(__name__)

CHAT_POLL_INTERVAL_S = 0.1

# 上一轮生成期间插队到达的普通群消息，在上一回复落库后先沉降这段时间；
# 避免上一轮刚结束就立刻开启下一轮。@ 与名字命中不等待。
GROUP_CROSSED_MESSAGE_SETTLE_MS = 8_000

# 群历史首次回填时用于播种游标的历史条数与时间容差。
_BACKFILL_SEED_EVENT_LIMIT = 500
_BACKFILL_SEED_MESSAGE_LIMIT = 80
_BACKFILL_SEED_TIME_MATCH_MS = 10 * 60_000

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


@dataclass(frozen=True)
class _BufferedMessage:
    """一条已经持久化、等待聊天循环消费的入站消息。"""

    text: str
    context: ConversationContext
    mentioned_me: bool
    external_message_id: str | None
    bot_name: str | None
    message_id: int
    previous_message_at: int | None
    # 消息进入缓冲区的毫秒时间戳；用于识别「上一轮生成期间插队到达」的批次。
    accepted_at: int
    # 后台图片描述任务；结果为补齐描述后的完整正文，回合构建前必须等待。
    image_description_task: asyncio.Task[str] | None = None


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


@dataclass(frozen=True)
class _BatchGate:
    """本批合并事实的三态门控结果与判定输入。"""

    result: GateResult
    asleep: bool
    name_mentioned: bool
    reply_count: int
    mentioned_me: bool
    last_bot_reply_elapsed_ms: int | None = None


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
        image_describer: ChatImageDescriber | None = None,
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
        :param image_describer: 可选的聊天图片描述服务；为 ``None`` 时图片保持占位符。
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
        self._image_describer = image_describer
        self._default_action_policy = action_policy or TurnPlanner(AlwaysReplyPolicy())
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
        conversation_agent_cfg = cfg.conversation_agent
        self._conversation_mode = conversation_agent_cfg.mode
        self._conversation_selected_streams = frozenset(conversation_agent_cfg.selected_streams)
        self._trigger_mode = conversation_agent_cfg.trigger_mode
        self._frequency_talk_value = conversation_agent_cfg.frequency_talk_value
        self._reply_necessity_threshold = conversation_agent_cfg.reply_necessity_threshold
        # 扩展触发模式下的待处理候选累计；一旦产生 DELIBERATE 即清零。
        self._extended_pending: dict[int, int] = {}
        # 灰度关闭时不持有 Agent，避免任何意外调用；provider 未注入时同样置空。
        self._conversation_agent = (
            ConversationAgent(
                chat_provider,
                temperature=self._chat_temperature,
                max_tokens=self._chat_max_tokens,
            )
            if chat_provider is not None and conversation_agent_cfg.mode != 'off'
            else None
        )
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
        self._buffers: dict[int, list[_BufferedMessage]] = {}
        self._stream_claims: dict[int, str] = {}
        self._poll_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
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
        """启动由入站消息唤醒、固定心跳兜底的聊天缓冲循环。"""
        self._stop.clear()
        self._wake.clear()
        self._poll_task = asyncio.create_task(self._poll_loop(), name='chat-poll')

    async def shutdown(self) -> None:
        """停止聊天缓冲轮询并终止仍在执行的回复。"""
        self._stop.set()
        self._wake.set()
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
        """在消息到达时处理缓冲区，并用固定间隔心跳兜底。"""
        while not self._stop.is_set():
            # 先清除已消费的唤醒信号；若 tick 期间又有消息到达，新信号会保留。
            self._wake.clear()
            try:
                await self._tick()
            except Exception as exc:
                logger.warning('chat_tick_failed', error=str(exc))
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=CHAT_POLL_INTERVAL_S)
            except asyncio.TimeoutError:
                pass

    def _group_batch_in_crossed_settle(
        self,
        batch: list[_BufferedMessage],
        now: int,
    ) -> bool:
        """判断上一轮生成期间插队到达的普通群批次是否仍在沉降期。

        只有消息入缓冲时间早于上一条 Bot 回复落库时间的批次才需要沉降：
        这些消息在上一轮回复前已经到达，若上一轮刚结束就立刻开下一轮，
        会形成背靠背连回。@ 与名字命中属于明确注意力信号，不等待。

        :param batch: 同一 stream 的连续同一发送者批次。
        :param now: 当前毫秒时间戳。
        :return: 批次应继续在缓冲中等待时返回 True。
        """
        context = batch[0].context
        if context.stream.kind != 'group':
            return False
        # 沉降只作用于扩展触发口径管理的普通群候选；旧管线与未选中 stream
        # 保持既有逐批调度语义，避免离线与 shadow 观察行为被时间窗口改变。
        if not self.extended_trigger_enabled(context):
            return False
        if any(message.mentioned_me for message in batch):
            return False
        if any(mentions_bot_name(message.text, self._bot_names) for message in batch):
            return False
        last_reply_at = self.memory.last_assistant_reply_at(context.stream.id)
        if last_reply_at is None:
            return False
        crossed = any(message.accepted_at < last_reply_at for message in batch)
        if not crossed:
            return False
        return now - last_reply_at < GROUP_CROSSED_MESSAGE_SETTLE_MS

    async def _tick(self) -> None:
        """为每个空闲且有缓冲消息的 stream 启动一轮同发送者回复。"""
        now = current_time()
        for stream_id in tuple(self._buffers):
            buffered = self._buffers.get(stream_id)
            if not buffered:
                self._buffers.pop(stream_id, None)
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
            if self._group_batch_in_crossed_settle(batch, now):
                continue
            if not self.claim_stream(stream_id, 'reply'):
                continue
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
            按到达顺序将非空消息追加到对应 stream 缓冲区，不打断在飞回合；
            带图片来源时会创建后台描述任务，描述成功后再回写已落库正文。

        :raises ValueError: 入站上下文或平台归属不满足下游约束时由依赖服务抛出。
        :raises Exception: 任务内部异常会记录并推送 ``chat.error``，不会由返回的 task
                再次向调用方抛出。
        """
        trimmed = inbound.text.strip()
        if not trimmed:
            return
        stream_id = inbound.context.stream.id
        accepted_at = current_time()
        previous_message_at = self.memory.last_message_at(stream_id)
        message_id = self.memory.append_message(
            stream_id,
            inbound.context.person.id,
            'user',
            trimmed,
            accepted_at,
        )
        image_task: asyncio.Task[str] | None = None
        if inbound.image_sources:
            # 先以 [图片] 占位符确认接收并返回；描述成功后后台回写同一行正文。
            # 回合启动时再等待该任务，避免图片下载/VLM 拖住 HTTP 入站响应。
            image_task = asyncio.create_task(self._describe_image_message(
                stream_id,
                message_id,
                trimmed,
                inbound.image_sources,
            ))
            self._track_background_task(image_task)
        self._buffers.setdefault(stream_id, []).append(_BufferedMessage(
            text=trimmed,
            context=inbound.context,
            mentioned_me=inbound.mentioned_me,
            external_message_id=inbound.external_message_id,
            bot_name=inbound.bot_name,
            message_id=message_id,
            previous_message_at=previous_message_at,
            accepted_at=accepted_at,
            image_description_task=image_task,
        ))
        self._wake.set()

    def _track_background_task(self, task: asyncio.Task[str]) -> None:
        """登记后台任务并静默消化无人等待时的异常。

        :param task: 已创建的后台图片描述任务。
        :return: 无返回值。
        副作用：任务完成时读取一次异常，避免事件循环记录
            ``Task exception was never retrieved``；回合随后等待该任务时仍会
            重新抛出同一异常并进入正常错误处理。
        """
        def _consume_exception(done: asyncio.Task[str]) -> None:
            if done.cancelled():
                return
            try:
                done.exception()
            except Exception:
                pass

        task.add_done_callback(_consume_exception)

    async def _describe_image_message(
        self,
        stream_id: int,
        message_id: int,
        text: str,
        sources: tuple[str, ...],
    ) -> str:
        """在后台下载并描述一条入站消息的普通图片，补齐落库正文。

        :param stream_id: 消息所属 stream ID。
        :param message_id: 已写入 ``messages`` 表的占位符消息主键。
        :param text: 当前含 ``[图片]`` 占位符的消息正文。
        :param sources: 与正文普通图片顺序一致的来源引用。
        :return: 补齐描述后的正文；全部失败时返回原占位符正文。
        :raises sqlite3.Error: 描述成功后回写消息失败时抛出。
        副作用：描述可用时用合并后的正文更新对应消息行；不触发回合。
        """
        if not sources or self._image_describer is None:
            return text
        descriptions = await self._image_describer.describe_sources(sources)
        enriched = merge_image_descriptions(text, descriptions)
        if enriched != text:
            self.memory.update_message_content(stream_id, message_id, enriched)
        return enriched

    async def _materialize_batch_images(
        self,
        batch: list[_BufferedMessage],
    ) -> list[_BufferedMessage]:
        """等待批次内所有后台图片描述完成，并生成使用补齐正文的批次副本。

        :param batch: 已入缓冲、可能携带后台描述任务的原始消息批次。
        :return: 每条 ``text`` 均为描述补齐后正文的新批次；无图片的消息原样保留。
        :raises Exception: 任一后台任务异常时重新抛出，由回合错误处理记录。
        副作用：只读取任务结果，不回写数据库；数据库回写由后台任务完成。
        """
        pending = [
            message.image_description_task
            for message in batch
            if message.image_description_task is not None
        ]
        if pending:
            await asyncio.gather(*pending)
        return [
            replace(
                message,
                text=(
                    message.image_description_task.result()
                    if message.image_description_task is not None
                    else message.text
                ),
            )
            for message in batch
        ]

    async def _start_turn(
        self,
        batch: list[_BufferedMessage],
    ) -> int:
        """取一个已持久化的非空消息批次创建并启动回复回合。"""
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
            trace.emit(
                'user_input',
                turnId=turn,
                text=message.text,
                externalMessageId=message.external_message_id or '',
            )
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

            assistant_raw = ''
            reply_persisted = False
            # 默认使用占位符正文；图片任务完成前的异常处理仍需可读的批次文本。
            trimmed = '\n'.join(message.text for message in batch)
            try:
                now = current_time()
                asleep = self._sleep_state().asleep if self._sleep_state else False
                self.settle_elapsed(context, now, asleep)
                self.memory.sweep(now)

                # 图片描述在后台已尽力提前完成；这里等待结果后再做门控与上下文构建。
                # 等待只阻塞当前 stream 的回合任务，不阻塞其它 stream 的消息消费。
                # 结果写入新变量；闭包内重新绑定 batch 会使其成为局部变量，
                # 赋值前的首次读取会因此抛出 UnboundLocalError。
                materialized_batch = await self._materialize_batch_images(batch)
                trimmed = '\n'.join(message.text for message in materialized_batch)

                # 用户消息已在确认接收时落库，使用批次首条入队前的历史位置计算会话间隔。
                self._refresh_session(
                    context,
                    now,
                    last_message_at=materialized_batch[0].previous_message_at,
                    read_history=False,
                )
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
                    user_message_id_watermark=materialized_batch[-1].message_id,
                    batch_message_ids=tuple(
                        message.message_id for message in materialized_batch
                    ),
                )
                render_params: dict[str, dict[str, str]] = {}
                batch_gate = self._batch_gate(
                    context,
                    trimmed,
                    inbound.mentioned_me,
                    candidate_count=len(materialized_batch),
                )
                if batch_gate.result.reason_codes[0] in (
                    'frequency_wait',
                    'low_necessity',
                ):
                    # 扩展触发模式放行的无信号群消息未达到候选条件，由这里
                    # 补记 gate_dropped 并结束回合，不再走旧管线。
                    await self._handle_live_drop(context, materialized_batch, turn, batch_gate)
                    return
                scope = self._agent_scope(context, batch_gate.result.disposition)
                if scope == 'shadow':
                    # shadow 只记录 Agent 决策，之后仍走旧管线，可见行为不变。
                    await self._run_shadow_decision(
                        context,
                        materialized_batch,
                        prepared_context,
                        turn,
                        cancel_event,
                        batch_gate,
                    )
                    if not self._has_legacy_attention_signal(batch_gate):
                        # 仅由 frequency_budget / reply_necessity 产生的候选在旧
                        # signal 口径下本会被 DROP；shadow 阶段不因此唤醒旧管线。
                        return
                elif scope == 'live':
                    if batch_gate.result.disposition == 'drop':
                        await self._handle_live_drop(context, materialized_batch, turn, batch_gate)
                        return
                    await self._run_conversation_turn(
                        context=context,
                        batch=materialized_batch,
                        trimmed=trimmed,
                        turn=turn,
                        cancel_event=cancel_event,
                        sink=sink,
                        prepared=prepared_context,
                        batch_gate=batch_gate,
                        sender=sender,
                        render_params=render_params,
                    )
                    return
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
                    messages=tuple(prepared_context.raw_history),
                    batch_text=trimmed,
                ))
                trace.emit(
                    'turn_action',
                    turnId=turn,
                    action=action.action,
                    reason=action.reason,
                    length=action.length,
                    decisionSource=action_policy.decision_source,
                )
                if action.action == 'silent':
                    self._mark_stage(
                        context,
                        GATED,
                        f'未回复：{action.reason}',
                        turn_id=turn,
                    )
                    if context.stream.kind == 'group':
                        for message in materialized_batch:
                            self._emit_group_observation(
                                message,
                                action.reason,
                                message.text,
                                message.external_message_id,
                            )
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
                messages = await self._enrich_prepared_context(
                    prepared_context,
                    cancel_event,
                    render_params,
                    action.length,
                )
                system_template_ids = (
                    *CHAT_SYSTEM_TEMPLATE_IDS,
                    *(
                        template_id
                        for template_id in CHAT_SYSTEM_VARIANT_COMPONENTS
                        if template_id in render_params
                    ),
                )
                self._mark_stage(context, GENERATING, turn_id=turn)
                trace.emit(
                    'llm_request',
                    turnId=turn,
                    messages=messages,
                    temperature=self._chat_temperature,
                    maxTokens=self._chat_max_tokens,
                    renderParams=render_params,
                    **prompt_metadata('chat.system', system_template_ids),
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
                    sink.segments,
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
                if not reply_persisted:
                    # 已确认接收的用户消息属于历史；失败时只保存已经产生的助手正文。
                    self._persist_reply(context, assistant_raw)
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
                if not reply_persisted:
                    # 准备或投递失败不能删除已确认接收的用户消息。
                    self._persist_reply(context, assistant_raw)
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
        self._emit_group_observation(inbound, reason, text, inbound.external_message_id)
        return message_id

    async def describe_inbound_images(
        self,
        attachments: list[dict[str, Any]],
    ) -> list[str | None]:
        """描述旧协议已下载的 Base64 图片附件，失败项保持 ``None``。

        :param attachments: 旧适配器提交的图片附件列表；每项包含 Base64 ``data``。
        :return: 与输入等长的描述列表；未配置图片服务时全为 ``None``。
        副作用：只供旧 ``imageSegments`` 协议同步路径使用；新来源引用路径由
            :meth:`_describe_image_message` 在后台执行。
        """
        if self._image_describer is None:
            return [None for _ in attachments]
        return await self._image_describer.describe_attachments(attachments)

    def _external_group_message_seen(self, stream_id: int, external_id: str) -> bool:
        """判断某条群消息是否已经作为入站或观察事件进入过主体。"""
        row = self._db.execute(
            """SELECT 1 FROM pipeline_events
               WHERE stream_id = ?
                 AND kind IN ('user_input', 'observation')
                 AND json_extract(payload, '$.externalMessageId') = ?
               LIMIT 1""",
            (stream_id, external_id),
        ).fetchone()
        return row is not None

    def _known_group_external_ids(self, stream_id: int) -> set[str]:
        """读取指定群最近入站/观察事件中已登记的外部消息 ID。

        :param stream_id: 目标群 stream ID。
        :return: 非空 ``externalMessageId`` 集合；旧事件缺失该字段时不会报错。
        副作用：只读 pipeline_events。
        """
        rows = self._db.execute(
            """SELECT payload FROM pipeline_events
               WHERE stream_id = ?
                 AND kind IN ('user_input', 'observation')
               ORDER BY seq DESC LIMIT ?""",
            (stream_id, _BACKFILL_SEED_EVENT_LIMIT),
        ).fetchall()
        known: set[str] = set()
        for (payload,) in rows:
            try:
                data = json.loads(payload)
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict):
                external_id = str(data.get('externalMessageId') or '').strip()
                if external_id:
                    known.add(external_id)
        return known

    def _seed_group_backfill_cursor(
        self,
        stream_id: int,
        messages: list[dict[str, Any]],
    ) -> tuple[int, set[str]]:
        """首次回填前用已有数据播种去重游标，减少历史消息重复落库。

        优先使用 pipeline_events 中仍可追溯的外部消息 ID；旧事件没有该字段时，
        再用 ``messages`` 表最近用户消息的正文与时间窗匹配历史条目。两类数据
        都无法确认边界时返回 ``0``，此时仍按原逻辑逐条查重。

        :param stream_id: 目标群 stream ID。
        :param messages: 按时间升序排列的待回填消息。
        :return: ``(可安全跳过的最大 seq, 已知外部消息 ID 集合)``。
        副作用：只读数据库，不写入游标。
        """
        known_ids = self._known_group_external_ids(stream_id)
        seeded = 0
        for raw in messages:
            external_id = str(raw.get('externalMessageId') or '').strip()
            if external_id not in known_ids:
                continue
            try:
                message_seq = int(raw.get('messageSeq') or 0)
            except (TypeError, ValueError):
                message_seq = 0
            seeded = max(seeded, message_seq)

        if not seeded:
            seeded = self._seed_backfill_cursor_from_messages(stream_id, messages)
        return seeded, known_ids

    def _seed_backfill_cursor_from_messages(
        self,
        stream_id: int,
        messages: list[dict[str, Any]],
    ) -> int:
        """用最近已落库用户消息的正文时间窗估计历史回填边界。

        这是上线前旧事件缺少 ``externalMessageId`` 时的一次性兼容路径；正文与
        平台时间需落在 10 分钟窗口内才算命中，宁可少播种也不能跳过停机期间的
        新消息。
        """
        rows = self._db.execute(
            """SELECT content, created_at FROM messages
               WHERE stream_id = ? AND role = 'user'
               ORDER BY id DESC LIMIT ?""",
            (stream_id, _BACKFILL_SEED_MESSAGE_LIMIT),
        ).fetchall()
        if not rows:
            return 0
        recent = [(str(row[0]), int(row[1])) for row in rows]
        seeded = 0
        for raw in messages:
            text = str(raw.get('text') or '').strip()
            if not text:
                continue
            try:
                message_seq = int(raw.get('messageSeq') or 0)
                created_at = int(raw.get('createdAt') or 0)
            except (TypeError, ValueError):
                continue
            if not message_seq or created_at <= 0:
                continue
            for content, saved_at in recent:
                if content != text:
                    continue
                if abs(saved_at - created_at) <= _BACKFILL_SEED_TIME_MATCH_MS:
                    seeded = max(seeded, message_seq)
                    break
        return seeded

    def record_group_backfill(
        self,
        context: ConversationContext,
        messages: list[dict[str, Any]],
    ) -> int:
        """把停机期间错过的群历史落为观察消息，不触发模型回复。

        :param context: 目标群聊归属上下文。
        :param messages: 按时间升序排列的历史消息；每项包含发送者、正文、消息 ID、seq 和时间。
        :return: 本次实际新写入的消息条数。
        :raises sqlite3.Error: 消息或游标写入失败。

        副作用：
            写入用户历史、登记 ``backfill`` 观察事件，并持久化该群的去重游标。
        """
        if context.stream.kind != 'group':
            raise ValueError('record_group_backfill 只接受群聊消息')
        stream_id = context.stream.id
        key = f'group_backfill_cursor_{stream_id}'
        cursor = self.memory.read_json(key, {'last_seq': 0, 'recent_ids': []})
        if not isinstance(cursor, dict):
            cursor = {'last_seq': 0, 'recent_ids': []}
        recent_ids = [
            str(item) for item in cursor.get('recent_ids', [])
            if isinstance(item, (str, int))
        ]
        recent = set(recent_ids)
        last_seq = int(cursor.get('last_seq') or 0)
        if not last_seq and messages:
            seeded_seq, known_ids = self._seed_group_backfill_cursor(stream_id, messages)
            recent.update(known_ids)
            if seeded_seq:
                last_seq = seeded_seq
                logger.info(
                    'QQ 群历史回填游标已从已有数据播种',
                    streamId=stream_id,
                    lastSeq=last_seq,
                )
        written = 0

        for raw in messages:
            external_id = str(raw.get('externalMessageId') or '').strip()
            if not external_id or external_id in recent:
                continue
            try:
                message_seq = int(raw.get('messageSeq') or 0)
            except (TypeError, ValueError):
                message_seq = 0
            if message_seq and message_seq <= last_seq:
                continue
            if self._external_group_message_seen(stream_id, external_id):
                continue
            text = str(raw.get('text') or '').strip()
            if not text:
                continue
            created_at = int(raw.get('createdAt') or 0)
            if created_at <= 0:
                created_at = current_time()
            sender_context = self._registry.resolve_inbound(
                platform=context.stream.platform,
                stream_kind='group',
                stream_external_id=context.stream.external_id,
                sender_external_id=str(raw.get('senderExternalId') or ''),
                sender_nickname=str(raw.get('senderNickname') or ''),
                sender_group_card=str(raw.get('senderGroupCard') or ''),
                first_seen_at=created_at,
            )
            self.memory.append_message(
                stream_id,
                sender_context.person.id,
                'user',
                text,
                created_at,
            )
            sender = self._sender_metadata(sender_context)
            trace.emit(
                'observation',
                streamId=stream_id,
                personId=sender_context.person.id,
                text=text,
                reason='backfill',
                externalMessageId=external_id,
                **sender,
            )
            recent.add(external_id)
            if message_seq > last_seq:
                last_seq = message_seq
            written += 1

        if written:
            self.memory.write_json(
                key,
                {
                    'last_seq': last_seq,
                    'recent_ids': list(recent)[-200:],
                },
            )
        return written

    def _emit_group_observation(
        self,
        inbound: InboundMessage,
        reason: str,
        text: str,
        external_message_id: str | None = None,
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
            externalMessageId=external_message_id or '',
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

    def _refresh_session(
        self,
        context: ConversationContext,
        now: int,
        last_message_at: int | None = None,
        *,
        read_history: bool = True,
    ) -> int | None:
        """根据静默间隔刷新会话状态、临时语气和表达样本随机种子。

        :param context: 当前消息的会话上下文。
        :param now: 当前 Unix 毫秒时间戳。

        :param last_message_at: 已知的上一条消息时间；默认值为 ``None``。
        :param read_history: 是否从历史读取最近消息；普通主动消息使用默认值 ``True``，
            已持久化的缓冲批次传入 ``False``。
        :return: 本次应注入提示词的重逢间隔毫秒数；首次会话、群聊或间隔未超过阈值时返回
            ``None``。

        副作用：
            可能更新进程内会话的开始时间、语气、随机种子和重逢间隔；不写入数据库。
        """
        stream_id = context.stream.id
        state = self._session(stream_id)
        last = self.memory.last_message_at(stream_id) if read_history else last_message_at
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
        # 熟悉程度（认识了多少天）属 owner 专属关系信号，非 owner 不注入。
        acquaintance = (
            describe_acquaintance(self.memory.first_seen_at(context.person.id), now)
            if context.relationship_signals_enabled
            else ''
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
            **self._prompt_config_kwargs(context.relationship_signals_enabled),
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

    def _prompt_config_kwargs(self, relationship_enabled: bool) -> dict:
        """读取系统提示词所需的角色与用户配置。

        :param relationship_enabled: 是否注入 owner 专属关系信号（称呼偏好与关系）。
            非 owner（如群聊中的其他成员）必须传 ``False``；否则「对方希望你称呼 X /
            把对方当 Y 看待」会把 owner 的关系错误地套到每一个说话人身上（她会对
            群里所有人叫「哥哥」）。
        :return: 包含角色名、别名、用户称呼、关系、生日、人设和回复风格的字典。
        """
        bot = self._cfg.bot
        personality = self._cfg.personality
        return {
            'name': bot.name,
            'aliases': bot.aliases,
            'user_nickname': bot.user_nickname if relationship_enabled else '',
            'relationship': bot.relationship if relationship_enabled else '',
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

    @staticmethod
    def _order_working_memory_for_batch(
        messages: list[StoredMessage],
        batch_message_ids: tuple[int, ...] | None,
    ) -> list[StoredMessage]:
        """按本批消息边界重建交错落库后的历史顺序。

        上一轮生成期间到达的当前批消息会先于上一回复写入 messages 表，
        原始顺序形如 ``[上一用户, 当前批用户..., 上一 assistant]``。按当前
        批第一条用户消息把尾部 assistant 插回它前面，得到回合视角的正确
        顺序：``[上一用户, 上一 assistant, 当前批用户...]``。

        :param messages: 记忆服务返回的按落库顺序排列的消息。
        :param batch_message_ids: 本批用户消息主键；非 Agent 路径可传 ``None``。
        :return: 需要重排时返回新列表，否则返回原列表。
        """
        if not batch_message_ids:
            return messages
        batch_ids = set(batch_message_ids)
        last_user_index = max(
            index for index, message in enumerate(messages)
            if message.role == 'user'
        )
        trailing = messages[last_user_index + 1:]
        if not trailing:
            return messages
        first_batch_index = next(
            index for index, message in enumerate(messages)
            if message.role == 'user' and message.message_id in batch_ids
        )
        return [
            *messages[:first_batch_index],
            *trailing,
            *messages[first_batch_index:last_user_index + 1],
        ]

    def _prepare_turn_context(
        self,
        context: ConversationContext,
        query: str,
        now: int,
        platform_bot_name: str | None = None,
        user_message_id_watermark: int | None = None,
        batch_message_ids: tuple[int, ...] | None = None,
    ) -> _PreparedTurnContext:
        """组装不依赖模型调用的完整回合上下文。

        :param context: 当前会话上下文。
        :param query: 当前用户文本。
        :param now: 当前毫秒时间戳。
        :param platform_bot_name: 当前平台登录昵称；仅用于当前入站消息的称呼匹配。
        :param user_message_id_watermark: 可选的本批末条用户消息 ID；用于隔离后来落库的用户消息。
        :param batch_message_ids: 可选的本批用户消息主键；用于把上一回合回复
            插回当前批之前的正确历史位置。
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
        # 熟悉程度（认识了多少天）属 owner 专属关系信号，非 owner 不注入。
        acquaintance = (
            describe_acquaintance(self.memory.first_seen_at(context.person.id), now)
            if context.relationship_signals_enabled
            else ''
        )
        schedule_desc = (self._schedule.describe(now, self.current_sleep()) if self._schedule else None)
        resumption = self._take_resumption(context.stream.id)
        wm = self.memory.working_memory(
            context.stream.id,
            self._working_memory_messages,
            user_message_id_watermark,
        )
        wm = self._order_working_memory_for_batch(wm, batch_message_ids)
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
        reply_length: str | None = None,
        protocol_text: str | None = None,
    ) -> list[dict]:
        """将同一份已组装上下文渲染为模型消息。

        :param prepared: 决策前已完成一次性组装的回合上下文。
        :param facts: 可选的增强后事实列表；省略时使用词面排序结果。
        :param expression_habits: 可选表达习惯提示词块。
        :param render_params: 可选提示词渲染参数收集字典。
        :param reply_length: 当前轮规划出的回复篇幅。
        :param protocol_text: 可选的 Agent 动作协议文本；提供时整体替换
            系统提示词中的直接发言协议，而不是追加在末尾。
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
            reply_length=reply_length,
            tone=self._session(prepared.context.stream.id).tone,
            resumption=prepared.resumption,
            platform_name=prepared.platform_bot_name,
            render_params=render_params,
            protocol_text=protocol_text,
            **self._prompt_config_kwargs(prepared.context.relationship_signals_enabled),
        )
        # 读取历史时再次规范化，兼容早期中断留下的悬空标签；该操作对干净历史幂等。
        history = normalize_history(prepared.raw_history)
        return [{'role': 'system', 'content': system}, *fit_char_budget(history)]

    async def _enrich_prepared_context(
        self,
        prepared: _PreparedTurnContext,
        signal: asyncio.Event | None,
        render_params: dict[str, dict[str, str]],
        reply_length: str | None,
        protocol_text: str | None = None,
    ) -> list[dict]:
        """确认回复后，在既有上下文上附加向量与表达模型增强。

        :param prepared: 动作决策实际读取的同一份上下文。
        :param signal: 可选的表达选择取消信号。
        :param render_params: 提示词渲染参数收集字典。
        :param reply_length: 规划器选出的回复篇幅。
        :param protocol_text: 可选的 Agent 动作协议文本；透传给系统提示词
            渲染，使动作头先于正文成为唯一输出协议。
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
            reply_length=reply_length,
            protocol_text=protocol_text,
        )

    def _batch_gate(
        self,
        context: ConversationContext,
        batch_text: str,
        mentioned_me: bool,
        candidate_count: int = 1,
    ) -> _BatchGate:
        """按本批合并事实重算三态门控；只读取确定性输入，不调用模型。

        :param context: 本批消息的会话上下文。
        :param batch_text: 合并后的本批正文。
        :param mentioned_me: 本批是否包含协议 @。
        :param candidate_count: 本批候选消息数；扩展触发模式用它累计频率预算。
        :return: 门控结果与全部判定输入事实。
        """
        asleep = self._sleep_state().asleep if self._sleep_state else False
        reply_count = 0
        last_bot_reply_elapsed_ms: int | None = None
        if context.stream.kind == 'group':
            now = current_time()
            reply_count = self.memory.assistant_reply_count_since(
                context.stream.id,
                now - self._cfg.group_chat.reply_window_minutes * 60_000,
            )
            last_bot_reply_at = self.memory.last_assistant_reply_at(context.stream.id)
            if last_bot_reply_at is not None:
                last_bot_reply_elapsed_ms = now - last_bot_reply_at
        name_mentioned = (
            mentions_bot_name(batch_text, self._bot_names)
            if context.stream.kind == 'group'
            else False
        )
        result = decide_disposition(GateRequest(
            stream_kind=context.stream.kind,
            mentioned_me=mentioned_me,
            name_mentioned=name_mentioned,
            asleep=asleep,
            at_mention_must_reply=self._at_mention_must_reply,
            replies_in_window=reply_count,
            max_replies_in_window=self._cfg.group_chat.max_replies_in_window,
            last_bot_reply_elapsed_ms=last_bot_reply_elapsed_ms,
        ))
        plain_group_drop = (
            context.stream.kind == 'group'
            and result.disposition == 'drop'
            and result.reason_codes == ('attention_filtered',)
        )
        if plain_group_drop:
            if self.extended_trigger_enabled(context) and self._trigger_mode != 'signal':
                result = self._extended_group_gate(context, batch_text, candidate_count)
            else:
                # 扩展模式未接管时不留历史残留，避免切回扩展模式后旧计数
                # 造成立即触发。
                self._extended_pending.pop(context.stream.id, None)
        elif (
            context.stream.kind == 'group'
            and result.disposition in ('deliberate', 'force')
        ):
            # 只有真正获得候选机会的批次才清零扩展累计；asleep、rate_limited
            # 等硬边界 DROP 不消费候选机会，保留之前的累计。
            self._extended_pending.pop(context.stream.id, None)
        return _BatchGate(
            result=result,
            asleep=asleep,
            name_mentioned=name_mentioned,
            reply_count=reply_count,
            mentioned_me=mentioned_me,
            last_bot_reply_elapsed_ms=last_bot_reply_elapsed_ms,
        )

    def _extended_group_gate(
        self,
        context: ConversationContext,
        batch_text: str,
        candidate_count: int,
    ) -> GateResult:
        """按配置的扩展口径决定无信号群消息是否进入 DELIBERATE。

        frequency 使用发言频率预算累计候选数；reply_necessity 以内容信号
        为主，并把累计候选数作为辅助压力项。两种模式都保留休眠、频率硬上限
        等上游边界，且都只产生确定性候选，不替 Agent 决定回复与否。

        :param context: 当前会话上下文。
        :param batch_text: 本批合并正文。
        :param candidate_count: 本批候选消息数。
        :return: 扩展门控产生的 drop 或 deliberate 结果。
        """
        stream_id = context.stream.id
        pending = self._extended_pending.get(stream_id, 0) + candidate_count
        if self._trigger_mode == 'frequency':
            threshold = frequency_trigger_threshold(self._frequency_talk_value)
            if pending >= threshold:
                self._extended_pending.pop(stream_id, None)
                return GateResult('deliberate', ('frequency_budget',))
            self._extended_pending[stream_id] = pending
            return GateResult('drop', ('frequency_wait',))
        if self._trigger_mode == 'reply_necessity':
            threshold = max(1, self._reply_necessity_threshold)
            # 压力分母必须是消息条数尺度；复用 frequency 预算折算阈值，
            # 不再把 0~100 的评分阈值当成条数使用。
            backlog_scale = frequency_trigger_threshold(self._frequency_talk_value)
            presence_since = current_time() - PRESENCE_WINDOW_MS
            score = score_reply_necessity(
                [batch_text],
                pending_count=pending,
                backlog_scale=backlog_scale,
                recent_self_replies=self.memory.assistant_reply_count_since(
                    stream_id,
                    presence_since,
                ),
                recent_window_messages=self.memory.message_count_since(
                    stream_id,
                    presence_since,
                ),
            )
            if score.score >= threshold:
                self._extended_pending.pop(stream_id, None)
                return GateResult('deliberate', ('reply_necessity',))
            self._extended_pending[stream_id] = pending
            return GateResult('drop', ('low_necessity',))
        return GateResult('drop', ('attention_filtered',))

    def _has_legacy_attention_signal(self, batch_gate: _BatchGate) -> bool:
        """判断本批是否携带旧 signal 口径下的可见注意力信号。

        shadow 阶段扩展触发模式可能单独产生 frequency_budget /
        reply_necessity 候选；这些候选在旧口径下会被 attention_filtered
        DROP，因此不应顺手唤醒旧管线改变可见行为。

        :param batch_gate: 本批门控快照。
        :return: 存在 @、名字提及或自然回应窗口时返回 True。
        """
        return (
            batch_gate.mentioned_me
            or batch_gate.name_mentioned
            or 'natural_reply_window' in batch_gate.result.reason_codes
        )

    def extended_trigger_enabled(self, context: ConversationContext) -> bool:
        """判断当前 stream 是否启用 frequency / reply_necessity 扩展口径。

        off 与 selected_streams 清单外的 stream 保持原 signal 行为，避免
        灰度观察之外的群聊被新触发模式改变候选边界。

        :param context: 当前消息的会话上下文。
        :return: 该 stream 可应用扩展触发模式时返回 True。
        """
        if self._conversation_mode == 'off' or self._conversation_agent is None:
            return False
        if self._conversation_mode in ('shadow', 'enabled'):
            return True
        return (
            self._conversation_mode == 'selected_streams'
            and context.stream.external_id in self._conversation_selected_streams
        )

    @property
    def conversation_trigger_mode(self) -> str:
        """返回当前配置的候选触发口径；仅供入口门控读取。"""
        return self._trigger_mode

    def _agent_scope(
        self,
        context: ConversationContext,
        disposition: GateDisposition,
    ) -> str:
        """返回本 stream 在当前灰度配置下的 Agent 归属。

        :param context: 本批消息的会话上下文。
        :param disposition: 本批门控态。
        :return: live（真实决策）/ shadow（只记录）/ off（旧管线）。
        """
        mode = self._conversation_mode
        if mode == 'off' or self._conversation_agent is None:
            return 'off'
        if mode == 'enabled':
            return 'live'
        if mode == 'selected_streams':
            return (
                'live'
                if context.stream.external_id in self._conversation_selected_streams
                else 'off'
            )
        # shadow 只观察 DELIBERATE 候选；FORCE 场景不重复付费。
        return 'shadow' if disposition == 'deliberate' else 'off'

    def _agent_frame(
        self,
        context: ConversationContext,
        batch: list[_BufferedMessage],
        turn: int,
        disposition: GateDisposition,
    ) -> DecisionFrame:
        """按本批消息快照构造回合固定帧；平台能力当前为空。"""
        capabilities = PlatformCapabilities()
        return DecisionFrame(
            turn_id=turn,
            snapshot_id=f'turn-{turn}',
            stream_kind=context.stream.kind,
            disposition=disposition,
            selectable_message_ids=tuple(message.message_id for message in batch),
            message_watermark=batch[-1].message_id,
            available_actions=available_actions(
                context.stream.kind,
                disposition,
                capabilities,
            ),
            capabilities=capabilities,
        )

    def _agent_gate_inputs(
        self,
        frame: DecisionFrame,
        batch_gate: _BatchGate,
    ) -> GateInputFacts:
        """把本批门控事实组装为行动事件第 1 层。"""
        return GateInputFacts(
            stream_kind=frame.stream_kind,
            mentioned_me=batch_gate.mentioned_me,
            name_mentioned=batch_gate.name_mentioned,
            must_reply=frame.disposition == 'force',
            asleep=batch_gate.asleep,
            rate_limited='rate_limited' in batch_gate.result.reason_codes,
            recent_bot_replies=batch_gate.reply_count,
            candidate_message_ids=frame.selectable_message_ids,
            selectable_message_ids=frame.selectable_message_ids,
        )

    def _render_agent_protocol(self, frame: DecisionFrame) -> str:
        """渲染本回合的动作头协议文本。

        该文本由调用方整体替换系统提示词中的直接发言协议；shadow 与 live 共用，
        保证两条灰度路径看到的输出规则完全一致。

        :param frame: 本回合固定快照，提供动作空间、可选消息与平台能力。
        :return: 已注入运行时动作集与目标范围的协议文本。
        """
        return render_action_protocol(
            sorted(frame.available_actions),
            frame.selectable_message_ids,
            quote_supported=frame.capabilities.quote,
        )

    def _render_agent_messages(
        self,
        frame: DecisionFrame,
        messages: list[dict],
    ) -> list[dict]:
        """把已渲染消息整理为 Conversation Agent 实际提交的上下文。

        与旧管线不同，Agent 的助手历史必须去掉 ``<say>`` 外壳，否则模型会
        继续模仿历史里「回复以 <say> 开头」的旧格式；随后在真实用户消息前
        插入 reply/silent 两条完整 few-shot，并在末条用户消息上追加输出
        起点指令，使动作头先于正文成为生成侧最近的约束。

        :param frame: 本回合固定快照，提供动作空间与可选消息。
        :param messages: ``_render_prepared_context`` 产出的系统与历史消息。
        :return: 历史助手已纯文本化、带 few-shot 与末轮输出指令的新消息列表。
        """
        if len(messages) < 2:
            return messages
        history: list[dict] = []
        for message in messages[1:]:
            item = dict(message)
            if item.get('role') == 'assistant':
                content = item.get('content')
                if isinstance(content, str):
                    item['content'] = strip_say_tags(content)
            history.append(item)
        user_indexes = [
            index for index, message in enumerate(history)
            if message.get('role') == 'user'
        ]
        if not user_indexes:
            return messages
        last_user_index = user_indexes[-1]
        # 上一回合的 assistant 回复可能晚于当前用户消息落库：当前消息在模型
        # 生成期间到达时，落库顺序是 [上一用户, 当前用户, 上一回复]。同 stream
        # 的占用保证尾部 assistant 只可能属于上一回合，必须移到当前用户之前，
        # 而不是截断；否则 Agent 看不见自己刚说过的话，会重复回复。
        trailing_assistants = history[last_user_index + 1:]
        history = [
            *history[:last_user_index],
            *trailing_assistants,
            history[last_user_index],
        ]
        last_user_index = len(history) - 1
        history[last_user_index] = {
            **history[last_user_index],
            'content': (
                f"{history[last_user_index]['content']}\n\n"
                '[输出要求] 你下一条回复必须先输出 <decision> 动作标签；'
                '正文只能放在其后的 <say> 里，禁止在 <decision> 之前输出 '
                '<say>、普通文字或解释。'
            ),
        }
        return [
            messages[0],
            *history[:last_user_index],
            *self._agent_output_examples(frame),
            *history[last_user_index:],
        ]

    def _agent_output_examples(self, frame: DecisionFrame) -> list[dict[str, str]]:
        """构造紧邻当前轮次的 reply 与 silent 输出示例。

        系统提示词末尾的示例离生成位置较远；在真实用户消息前放两条同格式
        few-shot，可显著压低模型退回「先 <say>」旧习惯的概率。

        :param frame: 本回合固定快照，示例目标只取真实可选消息。
        :return: 按当前动作空间生成的 user/assistant 示例消息列表。
        """
        examples: list[dict[str, str]] = []
        targets = frame.selectable_message_ids
        if 'reply' in frame.available_actions and targets:
            examples.extend([
                {
                    'role': 'user',
                    'content': '[输出格式示例] 群里有人问你忙不忙，要不要现在一起上号。',
                },
                {
                    'role': 'assistant',
                    'content': (
                        f'<decision action="reply" targets="{targets[0]}" '
                        'reasons="direct_question" length="brief"/>'
                        '<say emotion="normal">不忙，刚刷完视频。</say>'
                        '<say emotion="smile">上号叫我，我这就来。</say>'
                    ),
                },
            ])
        if 'silent' in frame.available_actions:
            examples.extend([
                {
                    'role': 'user',
                    'content': '[输出格式示例] 群里在聊一个你不认识的人。',
                },
                {
                    'role': 'assistant',
                    'content': '<decision action="silent" reasons="others_conversation"/>',
                },
            ])
        return examples

    async def _run_shadow_decision(
        self,
        context: ConversationContext,
        batch: list[_BufferedMessage],
        prepared: _PreparedTurnContext,
        turn: int,
        cancel_event: asyncio.Event,
        batch_gate: _BatchGate,
    ) -> None:
        """shadow 灰度：对 DELIBERATE 候选调用 Agent 只记录决策，不改可见行为。

        决策由 Agent 自行落账 action_decision 事件；正文与副作用全部丢弃。
        任何异常都不能阻断其后的旧管线。
        """
        frame = self._agent_frame(context, batch, turn, batch_gate.result.disposition)
        gate_inputs = self._agent_gate_inputs(frame, batch_gate)
        messages = self._render_agent_messages(
            frame,
            self._render_prepared_context(
                prepared,
                protocol_text=self._render_agent_protocol(frame),
            ),
        )
        metadata = prompt_metadata(
            'chat.conversation', CHAT_CONVERSATION_TEMPLATE_IDS,
        )
        trace.emit(
            'llm_request',
            turnId=turn,
            messages=messages,
            temperature=self._chat_temperature,
            maxTokens=self._chat_max_tokens,
            **metadata,
        )
        try:
            outcome = await self._conversation_agent.run(
                frame,
                messages,
                gate_inputs,
                batch_gate.result.reason_codes,
                prompt_hash=metadata['promptHash'],
                model_task='chat.conversation.shadow',
                signal=cancel_event,
            )
        except Exception as exc:
            # shadow 只是观察通道，失败记录日志即可，绝不能影响旧管线行为。
            logger.warning('shadow_decision_failed', turnId=turn, error=str(exc))
            return
        decision = outcome.decision
        render_action_decision(
            turn=turn,
            agent_scope='shadow',
            event_status=outcome.event_status,
            detail=outcome.action_event.detail,
            action=decision.action if decision is not None else '',
            reason_codes=decision.reason_codes if decision is not None else (),
            target_message_ids=(
                decision.target_message_ids if decision is not None else ()
            ),
        )

    async def _run_conversation_turn(
        self,
        context: ConversationContext,
        batch: list[_BufferedMessage],
        trimmed: str,
        turn: int,
        cancel_event: asyncio.Event,
        sink: _TurnSink,
        prepared: _PreparedTurnContext,
        batch_gate: _BatchGate,
        sender: Dict[str, str],
        render_params: dict[str, dict[str, str]],
    ) -> None:
        """执行一次 Conversation Agent 调用并处理其结果。

        silent 只写行动决策事件，不产生任何用户可见输出；reply 复用既有 sink
        消费副作用与分句，随后持久化、人格结算与平台投递；模型/协议失败不
        流出任何正文，按失败状态呈现。
        """
        frame = self._agent_frame(context, batch, turn, batch_gate.result.disposition)
        gate_inputs = self._agent_gate_inputs(frame, batch_gate)
        messages = self._render_agent_messages(
            frame,
            await self._enrich_prepared_context(
                prepared,
                cancel_event,
                render_params,
                reply_length=None,
                protocol_text=self._render_agent_protocol(frame),
            ),
        )
        self._mark_stage(context, GENERATING, turn_id=turn)
        metadata = prompt_metadata(
            'chat.conversation', CHAT_CONVERSATION_TEMPLATE_IDS,
        )
        trace.emit(
            'llm_request',
            turnId=turn,
            messages=messages,
            temperature=self._chat_temperature,
            maxTokens=self._chat_max_tokens,
            renderParams=render_params,
            **metadata,
        )
        bind_render_params(render_params)
        assistant_raw: list[str] = []

        async def on_events(events: Iterable[ParseEvent]) -> None:
            await self._consume_events(events, sink)

        def on_chunk(chunk: dict[str, Any]) -> None:
            text = chunk.get('text')
            if text:
                assistant_raw.append(text)
            trace.emit('llm_chunk', turnId=turn, text=text, reasoning=chunk.get('reasoning'))

        outcome = await self._conversation_agent.run(
            frame,
            messages,
            gate_inputs,
            batch_gate.result.reason_codes,
            prompt_hash=metadata['promptHash'],
            model_task='chat.conversation',
            provider_name=getattr(self._chat_provider, 'provider', ''),
            model_name=getattr(self._chat_provider, 'model', ''),
            on_events=on_events,
            on_chunk=on_chunk,
            signal=cancel_event,
        )
        raw_text = ''.join(assistant_raw)
        trace.emit('llm_final', turnId=turn, text=raw_text)
        if outcome.event_status == 'silent_by_choice':
            assert outcome.decision is not None
            # silent 只写行动决策事件：不产生助手历史、TTS、事实或观察事件。
            self._mark_stage(
                context, GATED,
                f'她选择沉默：{", ".join(outcome.decision.reason_codes)}',
                turn_id=turn,
            )
            return
        if outcome.event_status != 'committed':
            render_turn_error(
                turn, sender['senderLabel'], trimmed,
                outcome.event_status, outcome.action_event.detail,
            )
            self._mark_stage(
                context, FAILED,
                f'{outcome.event_status}：{outcome.action_event.detail}',
                turn_id=turn,
            )
            if context.stream.platform == 'desktop':
                await self._emit(context.stream.id, 'chat.error', {
                    'turnId': turn,
                    'kind': 'error',
                    'message': outcome.action_event.detail,
                    'hint': '',
                })
            return
        assert outcome.decision is not None and outcome.decision.reply is not None
        # 历史只落可见正文：动作头不进入记忆，读历史时不会污染后续提示词。
        self.memory.append_message(
            context.stream.id,
            None,
            'assistant',
            ''.join(f'<say>{segment}</say>' for segment in sink.segments),
        )
        render_turn(
            turn,
            sender['senderLabel'],
            trimmed,
            messages,
            sink.segments,
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
            self._mark_stage(context, DISPATCHING, turn_id=turn)
            await self._emit(context.stream.id, 'chat.done', {'turnId': turn, 'kind': 'done'})
        else:
            try:
                await self._dispatch_outbound(context, turn, sink.segments)
            except Exception as exc:
                # 投递失败与决策分离：追加一条 delivery_failed 行动事件再上抛。
                delivery_event = ActionDecisionEvent(
                    turn_id=turn,
                    snapshot_id=frame.snapshot_id,
                    turn_message_watermark=frame.message_watermark,
                    gate_inputs=gate_inputs,
                    gate_disposition=frame.disposition,
                    gate_reason_codes=batch_gate.result.reason_codes,
                    available_actions=tuple(sorted(frame.available_actions)),
                    decision=None,
                    event_status='delivery_failed',
                    detail=str(exc),
                )
                trace.emit('action_decision', **delivery_event.to_dict())
                raise
        self._mark_stage(
            context, REPLIED, f'{len(raw_text)} 字', turn_id=turn,
        )
        asyncio.create_task(self._maybe_summarize(context.stream.id))

    async def _handle_live_drop(
        self,
        context: ConversationContext,
        batch: list[_BufferedMessage],
        turn: int,
        batch_gate: _BatchGate,
    ) -> None:
        """Agent 模式下批次级 DROP：与入口 DROP 相同的可见语义与审计。"""
        reason = batch_gate.result.reason_codes[0]
        self._mark_stage(context, GATED, f'未回复：{reason}', turn_id=turn)
        if context.stream.kind == 'group':
            for message in batch:
                self._emit_group_observation(
                    message,
                    reason,
                    message.text,
                    message.external_message_id,
                )
        frame = self._agent_frame(
            context, batch, turn, 'drop',
        )
        gate_inputs = self._agent_gate_inputs(frame, batch_gate)
        gate_event = ActionDecisionEvent(
            turn_id=turn,
            snapshot_id=frame.snapshot_id,
            turn_message_watermark=frame.message_watermark,
            gate_inputs=gate_inputs,
            gate_disposition='drop',
            gate_reason_codes=batch_gate.result.reason_codes,
            available_actions=(),
            decision=None,
            event_status='gate_dropped',
        )
        trace.emit('action_decision', **gate_event.to_dict())

    def bot_names(self) -> tuple[str, ...]:

        """返回 ``bot.toml`` 声明的主体名称和别名。

        :return: 配置加载时固定的主名称与别名元组。
        """

        return self._bot_names

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
            分句收集不区分平台，控制台摘要面板始终能拿到剥掉标签后的可见正文。
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
