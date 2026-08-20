"""
对话编排服务。

持有所有后端资源：按角色拆分的 LLM provider、MemoryStore、Persona、DayPlanService。
通过 WebSocket push 推事件给 Electron 主进程。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from html import escape
from typing import Any, Callable, Dict, Iterable, List, Mapping

import asyncio
import inspect
import json
import random
import re
import sqlite3

from .chat_image import (
    ChatImageDescriber,
    merge_emoji_descriptions,
    merge_image_descriptions,
)
from .emoji import EmojiLibrary
from .trace_console import mark_turn_start, render_action_decision, render_observation, render_turn, render_turn_error
from .vector import VectorService

from src.core.agent.character import pick_tone
from src.core.agent.action import ActionContext, ActionPolicy, AlwaysReplyPolicy, TurnPlanner
from src.core.agent.action_protocol import (
    REACTION_IDS,
    ActionDecisionEvent,
    DecisionFrame,
    DecisionHead,
    GateDisposition,
    GateInputFacts,
    PlatformCapabilities,
    available_actions,
)
from src.core.agent.cognition import (
    CognitiveExecutor,
    CognitiveScope,
    InspectAction,
    RecallAction,
)
from src.core.agent.conversation import AgentOutcome, ConversationAgent
from src.core.agent.observer import SceneObserver, SceneSnapshot
from src.core.agent.conversation_gate import (
    GateRequest,
    GateResult,
    ONGOING_TOPIC_MESSAGE_SPAN,
    decide_disposition,
    mentions_bot_name,
)
from src.core.agent.expression import ExpressionSample, render_expression_habits, sample_expression_habits
from src.core.agent.expression_select import ExpressionSelector
from src.core.agent.history import (
    close_dangling_say,
    fit_char_budget,
    normalize_history,
    strip_say_tags,
    strip_side_effect_tags,
)
from src.core.agent.parser import (
    EmojiEvent, MemoryEvent, MoodEvent, ParseEvent, PromiseEvent, ResponseParser, SayEndEvent,
    SayEvent, TextEvent,
)
from src.core.agent.prompt import (
    build_itemized_system_prompt,
    build_proactive_prompt,
    build_system_prompt,
    describe_resumption,
    render_action_protocol,
    render_replyer_protocol,
    render_tool_protocol,
)
from src.core.agent.reply_necessity import (
    PRESENCE_WINDOW_MS,
    frequency_trigger_threshold,
    score_reply_necessity,
)
from src.core.agent.segmentation import split_into_bubbles, typing_delay_seconds
from src.core.agent.summarize import summarize
from src.core.awareness.sleep import SleepState
from src.core.common.clock import now as current_time
from src.core.common.logger import get_logger
from src.core.config.schema import Config, ConversationConfig, TypingConfig
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
    OutboundPoke,
    OutboundReaction,
    PersonRef,
    StreamRef,
)
from src.core.prompts.registry import (
    CHAT_CONVERSATION_TEMPLATE_IDS,
    CHAT_PROACTIVE_TEMPLATE_IDS,
    CHAT_REPLYER_TEMPLATE_IDS,
    CHAT_SYSTEM_TEMPLATE_IDS,
    CHAT_SYSTEM_VARIANT_COMPONENTS,
    CHAT_TOOL_REPLYER_TEMPLATE_IDS,
    CHAT_TOOL_TEMPLATE_IDS,
    prompt_metadata,
)
from src.core.schedule.plan import DayPlan, DayPlanService, ScheduleSleepState, asks_about_activity

logger = get_logger(__name__)

CHAT_POLL_INTERVAL_S = 0.1

# 部分 Gemini 兼容网关会把 system 单独提取；保留一条固定的非 system 指令，
# 既满足其 contents 非空约束，也不把触发情境伪装成用户提出的新问题。
PROACTIVE_TRIGGER_MESSAGE = '请按上面的要求开始。'

# 对齐群聊既有回复窗口，在同一窗口内最多发送一张表情包。
EMOJI_MAX_PER_REPLY_WINDOW = 1
# 场景观察读取的历史条数。刻意宽于工作记忆窗口：观察的价值就在于看到比单轮
# 上下文更长的一段，否则它只是把对话模型已经看过的东西再读一遍。
SCENE_WINDOW_MESSAGES = 60

# 一个回合内最多允许的内部轮次。
#
# 回合是**系统驱动的循环**而不是一次性调用：产出可见产物（reply / react / poke /
# speak）之后不结束，而是把她自己刚做的事并回上下文再问一次，直到她表示这轮做完了
# （silent）、退回缓冲等下文（wait）、或失败。这个数只是防失控的保险——正常收束靠
# 她自己表态，撞上限属于异常路径，会强制收尾并留下明确记录。
MAX_TURN_ROUNDS = 10

# 续跑轮插入在候选块与输出要求之间的提示模板，``recap`` 由回合循环填入她这一轮
# 实际做过的事。两件事必须同时说清，缺任意一件续跑轮都会跑偏：
#
# 1. **她这一轮已经动过手了**。回合循环的收束靠她自己表态，不知道自己动过手就
#    只会把刚说的话换个说法再说一遍——缺这条提示时同一批消息会被连续产出十次
#    可见产物。
# 2. **做了什么以这句为准，不依赖历史里那份副本**。
#    - 现象：续跑轮的历史里可能一条真实消息都没有，只剩当前候选块。
#    - 原因：``_order_working_memory_for_batch`` 会把跨轮落库的上一条回复插到
#      当前批之前；一旦它落在历史开头（工作记忆窗口或字符预算的裁剪边界正好
#      切在这里），``normalize_history`` 会按「system 之后必须由 user 起头」的
#      端点要求丢掉开头的 assistant，她刚说的那句就此消失。react / poke 更是
#      从不写助手历史。
#    - 后果：把这句提示改回「看上面历史最后一条」，她会在看不见自己发言的情况
#      下对同一批消息重复表态。
_CONTINUATION_NOTICE = (
    '[本回合续跑] {recap}。这件事不一定出现在上面的历史里，以这句为准；'
    '上面那几条是你还没有回应过的新消息。'
    '确实还有要补的、或者有人接上了新的话，就继续；'
    '已经说完了就用 silent 收束这一轮，'
    '不要把刚说过的意思换个说法再说一遍。'
)


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
class _DirectFollowUpState:
    """一段私聊静默的正常回复锚点、定时追问结果与后台任务。"""

    context: ConversationContext
    silence_started_at: int
    target_user_message_id: int
    normal_reply_message_at: int
    follow_up_message_at: int | None = None
    typing_opportunity_considered: bool = False
    task: asyncio.Task[None] | None = None


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
    # 已按模型目标情绪选中的可发送引用；只有真实命中才进入出站和历史。
    emoji_items: list[tuple[str, str, int]] = field(default_factory=list)


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
    # 与 raw_history 同源、但每条用户消息带 [编号] 前缀的 Agent 专用变体。
    # 动作头的 targets 必须落在消息编号上，编号只有在历史里逐行可见时模型
    # 才能指认；旧管线不需要编号，因此两份历史分开保存而不是就地改写。
    agent_history: list[dict[str, str]]


@dataclass(frozen=True)
class _BatchGate:
    """本批合并事实的三态门控结果与判定输入。"""

    result: GateResult
    asleep: bool
    name_mentioned: bool
    reply_count: int
    mentioned_me: bool
    last_bot_reply_elapsed_ms: int | None = None


@dataclass(frozen=True)
class _RoundResult:
    """回合内一轮的收束原因，以及供下一轮续跑提示引用的动作事实。"""

    # acted：产出了可见产物，还该再看一眼；declined：她表示这轮做完了；
    # paused：批次退回缓冲等下文；failed：模型或协议失败。
    reason: str
    # 她这一轮做了什么的第一人称陈述；只有 acted 非空，其余分支没有可见产物。
    recap: str = ''


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
        planner_provider: LlmProvider | None = None,
        replyer_provider: LlmProvider | None = None,
        scene_provider: LlmProvider | None = None,
        image_describer: ChatImageDescriber | None = None,
        emoji_library: EmojiLibrary | None = None,
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
        self._emoji_library = emoji_library
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
        # 决策与表达各自一档采样参数。拆分关闭时决策那一档不参与，走的仍是 chat。
        self._planner_temperature = generation.planner.temperature
        self._planner_max_tokens = generation.planner.token_limit
        self._replyer_temperature = generation.replyer.temperature
        self._replyer_max_tokens = generation.replyer.token_limit
        conversation_agent_cfg = cfg.conversation_agent
        self._conversation_mode = conversation_agent_cfg.mode
        self._conversation_selected_streams = frozenset(conversation_agent_cfg.selected_streams)
        self._trigger_mode = conversation_agent_cfg.trigger_mode
        self._frequency_talk_value = conversation_agent_cfg.frequency_talk_value
        self._reply_necessity_threshold = conversation_agent_cfg.reply_necessity_threshold
        self._cognitive_rounds = conversation_agent_cfg.max_cognitive_rounds
        self._scene_refresh_messages = cfg.group_chat.scene_refresh_messages
        # 同一 stream 同时只跑一个观察任务；观察比对话慢得多，重入只会互相盖写。
        self._observing: set[int] = set()
        # stream_id -> 进入等待时的缓冲长度。她选择「先等等」之后，本批消息被放回
        # 缓冲；在缓冲长度没有变化（也就是没有任何新消息进来）之前不再重开回合，
        # 否则轮询周期一到就会把同一批重新问一遍模型。
        self._waiting: dict[int, int] = {}
        self._speak_enabled = cfg.group_chat.self_started_topics
        # 扩展触发模式下的待处理候选累计；一旦产生 DELIBERATE 即清零。
        self._extended_pending: dict[int, int] = {}
        # 已经放弃自然跟进的群 stream。她在群聊里选择 silent 即加入，成功回复即移出；
        # 门控据此关闭自然回应窗口，让「说够了没有」由她自己的动作决定而不是回复计数。
        self._follow_up_declined: set[int] = set()
        # 决策与表达是否分成两次模型调用。三个条件缺一不可：配置打开、两级各自
        # 的 provider 都在。配置打开但 provider 缺位时保持单次调用，而不是让回合
        # 在运行期才失败——那会表现为她突然不说话，现场极难定位。
        self._split_replyer = (
            conversation_agent_cfg.split_replyer
            and planner_provider is not None
            and replyer_provider is not None
        )
        # 工具调用只产出决策，正文必须由回复生成那一级写，因此它依赖拆分。
        # 配置单开工具调用而没开拆分时按关闭处理，不在运行期才失败。
        self._tool_calling = conversation_agent_cfg.tool_calling and self._split_replyer
        # 拆分后决策走 planner 槽、表达走 replyer 槽；两个槽留空即继承 chat，
        # 因此不配模型也能打开开关，只是两级用同一个模型、延迟收益为零。
        decision_provider = planner_provider if self._split_replyer else chat_provider
        # 灰度关闭时不持有 Agent，避免任何意外调用；provider 未注入时同样置空。
        self._conversation_agent = (
            ConversationAgent(
                decision_provider,
                # 拆分后决策走 planner 那一档；未拆分时这一级同时负责正文，
                # 参数必须留在 chat 上，否则改 planner 会意外改掉旧路径的语气。
                temperature=(
                    self._planner_temperature
                    if self._split_replyer else self._chat_temperature
                ),
                max_tokens=(
                    self._planner_max_tokens
                    if self._split_replyer else self._chat_max_tokens
                ),
                replyer=replyer_provider if self._split_replyer else None,
                replyer_temperature=self._replyer_temperature,
                replyer_max_tokens=self._replyer_max_tokens,
                tool_calling=self._tool_calling,
            )
            if decision_provider is not None and conversation_agent_cfg.mode != 'off'
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
        # 认知动作只在 ReAct 开启时构造：轮次预算为 0 时执行器永远不会被调用，
        # 持有它只会让「关闭即回退到单轮」这条性质多一处需要复核的地方。
        # 情景分析有自己的模型槽。它最初借用摘要那一档（同为「把一段历史概括成
        # 一句话」），但这件事已经从群聊后台画像扩展到私聊即时决策，落在关键路径
        # 上——借用意味着调摘要会意外改掉它。槽留空即继承 chat，默认参数沿用摘要
        # 那一档的低温度，因此单开不改变任何现有行为。
        observer_provider = scene_provider if scene_provider is not None else summary_provider
        self._scene_observer = (
            SceneObserver(
                observer_provider,
                temperature=generation.scene.temperature,
                max_tokens=generation.scene.token_limit,
            )
            if observer_provider is not None
            else None
        )
        # 同一个情景分析 Agent 同时服务群聊周期画像和私聊即时决策；刷新条数只控制
        # 群聊后台调度，不决定 Agent 是否存在。它与 reply / silent 决策 Agent 分离。
        self._cognitive_executor = (
            CognitiveExecutor([
                RecallAction(self.memory, self._registry.stream_display_name),
                InspectAction(self.memory, self._registry.stream_display_name),
            ])
            if self._cognitive_rounds > 0
            else None
        )
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
        # 每个 stream 在当前静默期内已经因输入状态催过几次；对方一发消息就清零。
        self._typing_nudges: dict[int, int] = {}
        # 本段静默的输入状态机会是否已经交给行动核心评估。它独立于定时任务状态，
        # 因为后端重启后可以从数据库恢复会话，却不会恢复旧 asyncio 定时任务。
        self._typing_opportunities_considered: set[int] = set()
        # 私聊正常回复后只安排一轮定时追问；状态同时保存输入检测使用的原始静默锚点。
        self._direct_follow_ups: dict[int, _DirectFollowUpState] = {}
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
        follow_up_tasks = [
            state.task
            for state in self._direct_follow_ups.values()
            if state.task is not None and not state.task.done()
        ]
        for follow_up_task in follow_up_tasks:
            follow_up_task.cancel()
        if follow_up_tasks:
            await asyncio.gather(*follow_up_tasks, return_exceptions=True)
        self._direct_follow_ups.clear()
        self._typing_opportunities_considered.clear()
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
            # 等待中的 stream 只被新消息唤醒：她要等的就是「对方把话说完」，
            # 没有下文就一直等着——这与真人「等着等着就算了」是同一个行为，
            # 而不是卡住：任何一条新消息都会解除等待并强制她表态。
            waiting_at = self._waiting.get(stream_id)
            if waiting_at is not None and len(buffered) <= waiting_at:
                continue
            if not self.claim_stream(stream_id, 'reply'):
                continue
            del buffered[:boundary]
            if not buffered:
                self._buffers.pop(stream_id, None)
            # 已经等过一次的批次这一轮必须表态；标记在取走批次时消费掉。
            waited_once = self._waiting.pop(stream_id, None) is not None
            try:
                await self._start_turn(batch, allow_wait=not waited_once)
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
        # 对方开口即视为静默期结束：取消尚未发出的定时追问，并让下一段静默
        # 重新计算主动追问和输入状态追问次数。
        self._cancel_direct_follow_up(stream_id)
        self._typing_nudges.pop(stream_id, None)
        self._typing_opportunities_considered.discard(stream_id)
        accepted_at = current_time()
        previous_message_at = self.memory.last_message_at(stream_id)
        message_id = self.memory.append_message(
            stream_id,
            inbound.context.person.id,
            'user',
            trimmed,
            accepted_at,
            inbound.external_message_id,
        )
        image_task: asyncio.Task[str] | None = None
        if inbound.image_sources or inbound.emoji_sources:
            # 先以稳定占位符确认接收并返回；描述成功后后台回写同一行正文。
            # 回合启动时再等待该任务，避免图片下载/VLM 拖住 HTTP 入站响应。
            image_task = asyncio.create_task(self._describe_image_message(
                stream_id,
                message_id,
                trimmed,
                inbound.image_sources,
                inbound.emoji_sources,
                inbound.emoji_sub_types,
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
        emoji_sources: tuple[str, ...],
        emoji_sub_types: tuple[int, ...],
    ) -> str:
        """在后台描述普通图片和表情包，并补齐落库正文及表情包库。

        :param stream_id: 消息所属 stream ID。
        :param message_id: 已写入 ``messages`` 表的占位符消息主键。
        :param text: 当前含 ``[图片]`` 占位符的消息正文。
        :param sources: 与正文普通图片顺序一致的来源引用。
        :param emoji_sources: 与正文表情包顺序一致的来源引用。
        :param emoji_sub_types: 与表情包来源逐项对齐的 OneBot 图片子类型。
        :return: 补齐描述后的正文；全部失败时返回原占位符正文。
        :raises sqlite3.Error: 描述成功后回写消息失败时抛出。
        副作用：描述可用时用合并后的正文更新对应消息行；不触发回合。
        """
        if (not sources and not emoji_sources) or self._image_describer is None:
            return text
        image_task = (
            asyncio.create_task(self._image_describer.describe_sources(sources))
            if sources
            else None
        )
        emoji_task = (
            asyncio.create_task(self._image_describer.describe_emoji_sources(emoji_sources))
            if emoji_sources
            else None
        )
        descriptions = await image_task if image_task is not None else []
        emoji_descriptions = await emoji_task if emoji_task is not None else []
        enriched = merge_image_descriptions(text, descriptions)
        enriched = merge_emoji_descriptions(enriched, emoji_descriptions)
        if self._emoji_library is not None:
            for description, sub_type in zip(
                emoji_descriptions,
                emoji_sub_types,
                strict=True,
            ):
                if description is None:
                    continue
                await self._emoji_library.register(
                    description.image_bytes,
                    description.emotion_tags,
                    description.media_type,
                    description.content_hash,
                    sub_type,
                )
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
        *,
        allow_wait: bool = False,
    ) -> int:
        """取一个已持久化的非空消息批次创建并启动回复回合。

        :param allow_wait: 本批是否还能选择「先等等」；已经等过一次时传 False。
        """
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
                        allow_wait=allow_wait,
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
                        # 与 Agent 路径同一条口径：看过并决定不接就关闭自然回应窗口。
                        # 旧管线同样受该窗口影响，只在一侧维护会让清单外的群永远敞开。
                        self._follow_up_declined.add(context.stream.id)
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
                self._persist_reply(context, assistant_raw, sink.emoji_items)
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
                    model_name=getattr(self._chat_provider, 'model', ''),
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
                        sink.emoji_items,
                    )
                self._mark_stage(
                    context, REPLIED, f'{len(assistant_raw)} 字', turn_id=turn,
                )
                self._arm_direct_follow_up(context)
                # 她刚开过口，对话仍在她这边：与 Agent 路径同口径重新敞开自然回应窗口。
                self._follow_up_declined.discard(context.stream.id)
                asyncio.create_task(self._maybe_summarize(context.stream.id))
            except LlmError as exc:
                if exc.kind == 'aborted':
                    # 用户主动中断不是模型故障，但已生成正文仍须进入历史。
                    if not reply_persisted:
                        self._persist_reply(context, assistant_raw, sink.emoji_items)
                    return
                if not reply_persisted:
                    # 已确认接收的用户消息属于历史；失败时只保存已经产生的助手正文。
                    self._persist_reply(context, assistant_raw, sink.emoji_items)
                hint = _HINTS.get(exc.kind, '')
                snapshot = dump_llm_request('chat', exc.kind, str(exc), {
                    'turnId': turn,
                    'stage': trace.current_stage_id(),
                    'streamId': stream_id,
                })
                trace.emit('llm_error', turnId=turn, errorKind=exc.kind, message=str(exc),
                           snapshotPath=str(snapshot) if snapshot else None)
                render_turn_error(
                    turn, sender['senderLabel'], trimmed, exc.kind, str(exc),
                    model_name=getattr(self._chat_provider, 'model', ''),
                )
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
                    self._persist_reply(context, assistant_raw, sink.emoji_items)
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
                render_turn_error(
                    turn, sender['senderLabel'], trimmed, 'unknown', str(exc),
                    model_name=getattr(self._chat_provider, 'model', ''),
                )
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
            inbound.external_message_id,
        )
        if inbound.image_sources or inbound.emoji_sources:
            task = asyncio.create_task(self._describe_image_message(
                context.stream.id,
                message_id,
                text,
                inbound.image_sources,
                inbound.emoji_sources,
                inbound.emoji_sub_types,
            ))
            self._track_background_task(task)
        self._emit_group_observation(inbound, reason, text, inbound.external_message_id)
        # 只观察不回复的群消息同样推进场景：她对群里的理解不该只在自己开口时才更新。
        self._schedule_scene_observation(context)
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
                external_id,
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

    def _persist_reply(
        self,
        context: ConversationContext,
        assistant_raw: str,
        emoji_items: list[tuple[str, str, int]] | None = None,
    ) -> None:
        """将已生成的助手正文写入历史，并补齐流式中断留下的未闭合 ``<say>``。

        :param context: 当前会话上下文。
        :param assistant_raw: 模型已产生的原始助手文本。

        :raises sqlite3.Error: 助手消息写入失败。

        副作用：
            可能向 L1 messages 表追加助手消息并提交事务；空正文不写入。
        """
        text = close_dangling_say(assistant_raw)
        # 模型声明只是意图；历史只记录实际命中并准备发送的表情包，频控据此统计。
        text = re.sub(r'</?emoji\b[^>]*>', '', text, flags=re.IGNORECASE).strip()
        text += _emoji_history_markup(emoji_items or [])
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

    async def note_peer_typing(self, context: ConversationContext) -> bool:
        """收到「对方正在输入」通知，让行动核心决定是否值得据此追问。

        协议端在对方打字期间反复推送该通知，绝大多数时候她都不该有反应——正常
        一来一回的对话里看到对方打字就开口，是监控不是聊天。只有一种情形值得
        交给行动核心考虑：**她说完话之后对方长时间没回，直到现在才看见对方开始
        打字**。满足时间条件只会创建一次 ``reply / silent`` 决策机会，不代表必发。

        晾了多久、之前已经追问过几次会先与历史一起交给独立的情景分析 Agent；
        Conversation Agent 再结合该画像选择动作。它选择 ``silent`` 时不产生消息，
        也不消耗本段静默的追问次数。

        :param context: 已完成归属解析的会话上下文。
        :return: 本次是否真的发了话。
        副作用：命中条件时调用 Conversation Agent；仅在其选择 ``reply`` 后投递。
        """
        stream_id = context.stream.id
        # 群聊不推送输入状态；即使将来推送，群里盯着某人打字也不合适。
        if context.stream.kind != 'direct':
            return False
        nudge = self._cfg.typing.nudge
        if not nudge.enabled or nudge.max_per_silence <= 0:
            return False
        if self._agent_scope(context, 'deliberate') != 'live':
            return False
        if self._sleep_state is not None and self._sleep_state().asleep:
            return False
        last_reply_at = self.memory.last_assistant_reply_at(stream_id)
        if last_reply_at is None:
            return False
        now = current_time()
        silence_anchor = self._typing_silence_anchor(stream_id, last_reply_at)
        if now - silence_anchor < nudge.peer_silence_minutes * 60_000:
            return False
        # 正常回复之后对方已经回过话，就不算同一段静默；定时追问不改变这个锚点。
        last_peer_at = self.memory.last_user_message_at(stream_id)
        if last_peer_at is not None and last_peer_at > silence_anchor:
            return False
        nudges = self._typing_nudges.get(stream_id, 0)
        if nudges >= nudge.max_per_silence:
            return False
        state = self._direct_follow_ups.get(stream_id)
        # NapCat 会在同一段输入过程中连续上报状态。三分钟阈值只允许创建一轮
        # 决策机会；即使模型选择 silent，也不能下一条状态通知立刻重跑两级 Agent。
        # 独立集合覆盖后端重启后没有 _DirectFollowUpState 的历史静默会话。
        if (
            stream_id in self._typing_opportunities_considered
            or state is not None and state.typing_opportunity_considered
        ):
            return False
        situation = self._typing_situation(now - silence_anchor, nudges)
        target = self._follow_up_target(context, state)
        if target is None:
            return False
        if not self.claim_stream(stream_id, 'proactive'):
            return False
        self._typing_opportunities_considered.add(stream_id)
        if state is not None:
            state.typing_opportunity_considered = True
        try:
            decision = await self._decide_direct_follow_up(context, target, situation)
            if decision is None:
                return False
            turn, lines = decision
            # 模型生成期间对方可能已经发出消息；send() 会移除同一个状态，且消息
            # 主键检查覆盖毫秒时间戳相同的极端情况，过期追问不能压在新消息前面。
            if state is not None and self._direct_follow_ups.get(stream_id) is not state:
                return False
            if self.memory.has_user_messages_after(stream_id, target.message_id):
                return False
            await self._speak_claimed_external(context, lines, turn=turn)
            self._typing_nudges[stream_id] = nudges + 1
            if state is None:
                # 重启前遗留会话没有定时状态；成功发出这一轮后，新助手消息自然成为
                # 下一段静默锚点，达到下一次阈值时仍允许受次数上限约束的机会。
                self._typing_opportunities_considered.discard(stream_id)
            return True
        finally:
            self.release_stream(stream_id, 'proactive')

    def _typing_silence_anchor(self, stream_id: int, last_reply_at: int) -> int:
        """返回输入状态检测使用的静默起点。

        一分钟定时追问属于同一段静默，不能把三分钟输入检测推迟到第四分钟；只有
        当前最后一条助手消息仍是本状态里的正常回复或定时追问时，才复用原始锚点。
        其他主动消息或输入状态追问会成为新的最后回复，并自然恢复原有计时口径。
        """
        state = self._direct_follow_ups.get(stream_id)
        if state is None:
            return last_reply_at
        if last_reply_at in (
            state.normal_reply_message_at,
            state.follow_up_message_at,
        ):
            return state.silence_started_at
        return last_reply_at

    def _cancel_direct_follow_up(self, stream_id: int) -> None:
        """结束指定私聊的当前静默状态，并取消尚未完成的定时追问。"""
        state = self._direct_follow_ups.pop(stream_id, None)
        if state is None or state.task is None or state.task.done():
            return
        if state.task is not asyncio.current_task():
            state.task.cancel()

    def _arm_direct_follow_up(self, context: ConversationContext) -> None:
        """在一次成功的私聊正常回复后安排唯一一轮情景决策机会。"""
        stream_id = context.stream.id
        self._cancel_direct_follow_up(stream_id)
        follow_up = self._cfg.typing.follow_up
        if (
            context.stream.kind != 'direct'
            or not follow_up.enabled
            or self._agent_scope(context, 'deliberate') != 'live'
            or self._poll_task is None
            or self._stop.is_set()
            or self._buffers.get(stream_id)
        ):
            return
        normal_reply_message_at = self.memory.last_assistant_reply_at(stream_id)
        if normal_reply_message_at is None:
            return
        target = self._follow_up_target(context)
        if target is None:
            return
        state = _DirectFollowUpState(
            context=context,
            silence_started_at=current_time(),
            target_user_message_id=target.message_id,
            normal_reply_message_at=normal_reply_message_at,
        )
        self._direct_follow_ups[stream_id] = state
        state.task = asyncio.create_task(
            self._run_direct_follow_up(state),
            name=f'direct-follow-up-{stream_id}',
        )

    async def _run_direct_follow_up(self, state: _DirectFollowUpState) -> None:
        """等待配置阈值后创建一次追问决策机会；取消与失败都不会循环重试。"""
        stream_id = state.context.stream.id
        try:
            deadline = (
                state.silence_started_at
                + int(self._cfg.typing.follow_up.peer_silence_minutes * 60_000)
            )
            await asyncio.sleep(max(0, deadline - current_time()) / 1000)
            await self._attempt_direct_follow_up(state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                '私聊静默主动追问失败',
                streamId=stream_id,
                error=str(exc),
            )
        finally:
            current = self._direct_follow_ups.get(stream_id)
            if current is state and state.task is asyncio.current_task():
                state.task = None

    async def _attempt_direct_follow_up(self, state: _DirectFollowUpState) -> bool:
        """复核静默事实，再让行动核心选择 ``reply`` 或 ``silent``。"""
        context = state.context
        stream_id = context.stream.id
        if self._direct_follow_ups.get(stream_id) is not state:
            return False
        if state.follow_up_message_at is not None or self._buffers.get(stream_id):
            return False
        if self._sleep_state is not None and self._sleep_state().asleep:
            return False
        if self.memory.last_assistant_reply_at(stream_id) != state.normal_reply_message_at:
            return False
        if self.memory.has_user_messages_after(stream_id, state.target_user_message_id):
            return False
        target = self._follow_up_target(context, state)
        if target is None:
            return False
        if not self.claim_stream(stream_id, 'proactive'):
            return False
        try:
            silence_ms = current_time() - state.silence_started_at
            decision = await self._decide_direct_follow_up(
                context,
                target,
                self._scheduled_follow_up_situation(silence_ms),
            )
            if decision is None or self._direct_follow_ups.get(stream_id) is not state:
                return False
            if self.memory.has_user_messages_after(stream_id, state.target_user_message_id):
                return False
            turn, lines = decision
            await self._speak_claimed_external(context, lines, turn=turn)
            state.follow_up_message_at = self.memory.last_assistant_reply_at(stream_id)
            self._typing_nudges[stream_id] = self._typing_nudges.get(stream_id, 0) + 1
            logger.info(
                '私聊静默触发主动追问',
                streamId=stream_id,
                silenceMinutes=round(silence_ms / 60_000, 1),
            )
            return True
        finally:
            self.release_stream(stream_id, 'proactive')

    def _follow_up_target(
        self,
        context: ConversationContext,
        state: _DirectFollowUpState | None = None,
    ) -> StoredMessage | None:
        """读取本次主动决策可回复的最近一条真实用户消息。

        定时任务保存消息主键，后续输入状态必须继续指向同一段静默的原始消息；没有
        状态时则取当前工作记忆里的最后一条用户消息。找不到目标就放弃，而不是构造
        不可审计的虚拟消息。
        """
        history = self.memory.working_memory(
            context.stream.id,
            self._working_memory_messages,
        )
        target_id = state.target_user_message_id if state is not None else None
        for message in reversed(history):
            if message.role == 'user' and (
                target_id is None or message.message_id == target_id
            ):
                return message
        return None

    async def _decide_direct_follow_up(
        self,
        context: ConversationContext,
        target: StoredMessage,
        situation: str,
    ) -> tuple[int, list[dict]] | None:
        """以完整会话情景运行一次仅含 ``reply / silent`` 的主动决策。

        这是用户私聊已经获得正常回复之后的主动唤醒：只有主动机会允许在
        ``reply / silent`` 间判断，且没有认知、等待或平台副作用动作。只有合法
        ``reply`` 才返回可投递分句；选择 ``silent`` 或协议失败都不产生用户可见内容。
        """
        if self._conversation_agent is None or self._scene_observer is None:
            return None
        now = current_time()
        self._refresh_session(context, now)
        if self._schedule:
            await self._schedule.ensure(now)
        # 情景分析与行动决策是两个独立 Agent：前者只描述话题和气氛，不碰动作；
        # 后者读取该画像后才在 reply / silent 中选择。分析失败时不绕过它硬追问。
        scene = await self._observe_direct_scene(
            context,
            trigger='direct_follow_up',
        )
        if scene is None:
            return None
        turn = self._next_turn()
        capabilities = PlatformCapabilities()
        frame = DecisionFrame(
            turn_id=turn,
            snapshot_id=f'direct-follow-up-{turn}',
            stream_kind='direct',
            disposition='deliberate',
            selectable_message_ids=(target.message_id,),
            message_watermark=target.message_id,
            available_actions=frozenset({'reply', 'silent'}),
            capabilities=capabilities,
        )
        selectable_messages = [(target.message_id, strip_say_tags(target.content))]
        protocol = (
            render_tool_protocol(
                selectable_messages,
                quote_supported=False,
                available_actions=frame.available_actions,
            )
            if self._tool_calling
            else render_action_protocol(
                sorted(frame.available_actions),
                selectable_messages,
                quote_supported=False,
            )
        )
        prepared = self._prepare_turn_context(
            context,
            target.content,
            now,
            user_message_id_watermark=target.message_id,
        )
        render_params: dict[str, dict[str, str]] = {}
        follow_up_context = '\n'.join((
            '# 当前主动跟进机会',
            situation,
            '这不是必须发言的通知。请结合上面的完整对话判断：只有话题明显未完、'
            '对方仍需要回应或支持、先前约定需要续上，或者此刻追问在你们的关系里'
            '确实自然时才选 reply；话题已经自然结束、只是礼貌收尾、没有新价值、'
            '继续追问会打扰时选 silent。不要为了到点而硬找一句话。',
        ))

        def add_follow_up_context(rendered: list[dict]) -> list[dict]:
            """把本次主动机会按当前消息模式放入同一份上下文。"""
            with_scene = self._with_direct_scene_analysis(
                rendered,
                scene,
                itemized=self._tool_calling,
            )
            if self._tool_calling:
                return [
                    *with_scene,
                    {'role': 'user', 'content': follow_up_context},
                ]
            return [{
                **with_scene[0],
                'content': '\n\n'.join((
                    with_scene[0]['content'],
                    follow_up_context,
                )),
            }, *with_scene[1:]]

        rendered = add_follow_up_context(self._render_prepared_context(
            prepared,
            protocol_text=protocol,
            render_params=render_params,
            decision_only=self._tool_calling,
        ))
        rendered.append({
            'role': 'user',
            'content': (
                '[主动决策触发，不是对方的新消息] 现在只判断要不要续接上一段对话。'
                '请调用一个动作工具表达决定，除此之外不要输出正文或解释。'
                if self._tool_calling else
                '[主动决策触发，不是对方的新消息] 现在只判断要不要续接上一段对话。'
                '必须先输出 <decision>；选择 reply 时再输出简短自然的 <say>，选择 '
                'silent 后不要输出任何正文。'
            ),
        })
        messages = self._render_agent_messages(
            frame,
            rendered,
            protocol_text=protocol,
        )
        gate_inputs = GateInputFacts(
            stream_kind='direct',
            mentioned_me=False,
            name_mentioned=False,
            must_reply=False,
            asleep=False,
            rate_limited=False,
            recent_bot_replies=self._typing_nudges.get(context.stream.id, 0),
            candidate_message_ids=(target.message_id,),
            selectable_message_ids=(target.message_id,),
            current_topic_available=True,
        )
        metadata = prompt_metadata(
            'chat.conversation',
            CHAT_TOOL_TEMPLATE_IDS if self._tool_calling else CHAT_CONVERSATION_TEMPLATE_IDS,
        )
        trace.emit(
            'llm_request',
            turnId=turn,
            messages=messages,
            temperature=self._planner_temperature if self._split_replyer else self._chat_temperature,
            maxTokens=self._planner_max_tokens if self._split_replyer else self._chat_max_tokens,
            followUp=True,
            renderParams=render_params,
            **metadata,
        )
        bind_render_params(render_params)
        raw_parts: list[str] = []

        def on_chunk(chunk: dict[str, Any]) -> None:
            text = chunk.get('text')
            if text:
                raw_parts.append(text)
            trace.emit(
                'llm_chunk',
                turnId=turn,
                text=text,
                reasoning=chunk.get('reasoning'),
            )

        async def replyer_messages(head: DecisionHead) -> list[dict]:
            """为工具决策选中的主动回复组装表达层 item 流。"""
            replyer_protocol = render_replyer_protocol(
                head.reference or '',
                head.length,
                emoji_enabled=False,
            )
            replyer_context = add_follow_up_context(
                await self._enrich_prepared_context(
                    prepared,
                    None,
                    render_params,
                    reply_length=head.length,
                    protocol_text=replyer_protocol,
                )
            )
            replyer_items = self._render_agent_messages(
                frame,
                replyer_context,
                protocol_text=replyer_protocol,
            )
            trace.emit(
                'llm_request',
                turnId=turn,
                messages=replyer_items,
                temperature=self._replyer_temperature,
                maxTokens=self._replyer_max_tokens,
                followUp=True,
                renderParams=render_params,
                **prompt_metadata('chat.replyer', CHAT_TOOL_REPLYER_TEMPLATE_IDS),
            )
            bind_render_params(render_params)
            return replyer_items

        outcome = await self._conversation_agent.run(
            frame,
            messages,
            gate_inputs,
            ('direct_follow_up_opportunity',),
            prompt_hash=metadata['promptHash'],
            model_task='chat.conversation.follow_up',
            provider_name=getattr(self._chat_provider, 'provider', ''),
            model_name=getattr(self._chat_provider, 'model', ''),
            on_chunk=on_chunk,
            replyer_messages=replyer_messages if self._tool_calling else None,
        )
        raw_text = ''.join(raw_parts)
        trace.emit('llm_final', turnId=turn, text=raw_text)
        if outcome.decision is not None:
            render_action_decision(
                turn=turn,
                agent_scope='live',
                event_status=outcome.event_status,
                action=outcome.decision.action,
                reason_codes=outcome.decision.reason_codes,
                target_message_ids=outcome.decision.target_message_ids,
            )
        if outcome.event_status == 'silent_by_choice':
            logger.info(
                '私聊主动跟进决定沉默',
                streamId=context.stream.id,
                reasonCodes=list(outcome.decision.reason_codes) if outcome.decision else [],
            )
            return None
        if outcome.event_status != 'committed' or outcome.decision is None:
            logger.warning(
                '私聊主动跟进决策失败',
                streamId=context.stream.id,
                status=outcome.event_status,
                detail=outcome.action_event.detail,
            )
            return None
        lines = _extract_lines(raw_text)
        if not lines:
            logger.warning('私聊主动跟进正文为空', streamId=context.stream.id)
            return None
        return turn, lines

    async def _observe_direct_scene(
        self,
        context: ConversationContext,
        *,
        trigger: str,
    ) -> SceneSnapshot | None:
        """为一次私聊动作机会运行情景分析并缓存画像。

        :param context: 当前私聊上下文。
        :param trigger: 供观察事件区分首轮输入与后续主动跟进的稳定标识。
        :return: 合法的话题与气氛画像；分析失败时返回 ``None``，调用方必须保持沉默。
        副作用：调用情景分析 Agent，成功后更新当前 stream 的场景画像缓存。
        """
        if self._scene_observer is None:
            return None
        messages = self.memory.working_memory(
            context.stream.id,
            SCENE_WINDOW_MESSAGES,
        )
        if not messages:
            return None
        lines = self._scene_observation_lines(context, messages)
        try:
            snapshot = await self._scene_observer.observe(
                lines,
                messages[-1].message_id,
            )
        except Exception as exc:
            logger.warning(
                '私聊情景分析失败',
                streamId=context.stream.id,
                trigger=trigger,
                error=str(exc),
            )
            return None
        self.memory.write_json(self._scene_key(context.stream.id), snapshot.to_dict())
        trace.emit(
            'scene_observed',
            turnId=None,
            streamId=context.stream.id,
            topic=snapshot.topic,
            atmosphere=snapshot.atmosphere,
            trigger=trigger,
        )
        return snapshot

    @staticmethod
    def _with_direct_scene_analysis(
        messages: list[dict],
        scene: SceneSnapshot,
        *,
        itemized: bool = False,
    ) -> list[dict]:
        """把独立情景分析结果放进私聊动作决策上下文。

        :param messages: 首项必须是 system 的已渲染模型消息。
        :param scene: SceneObserver 刚产出的合法场景画像。
        :param itemized: 是否把画像追加为独立 user item；传统角色模式仍并入 system。
        :return: 已加入画像的新消息列表；调用方原列表保持不变。
        :raises ValueError: 消息列表为空或首项不是 system 时抛出。
        """
        if not messages or messages[0].get('role') != 'system':
            raise ValueError('私聊情景分析只能注入以 system 开头的模型消息')
        scene_context = '\n'.join((
            '# 私聊情景分析结果',
            f'当前话题：{scene.topic}',
            f'当前气氛：{scene.atmosphere}',
            '这是独立情景分析 Agent 对当前互动的描述，只提供决策背景。'
            '是否回复仍由 Conversation Agent 根据动作空间自行判断。',
        ))
        if itemized:
            return [
                *messages,
                {'role': 'user', 'content': scene_context},
            ]
        system = {
            **messages[0],
            'content': '\n\n'.join((
                messages[0]['content'],
                scene_context,
            )),
        }
        return [system, *messages[1:]]

    @staticmethod
    def _scheduled_follow_up_situation(silence_ms: int) -> str:
        """把达到配置阈值的未回复状态渲染为第一次主动机会。"""
        minutes = max(1, silence_ms // 60_000)
        return '\n'.join((
            f'你上一句发出去已经 {minutes} 分钟了，对方一直没有回复。',
            '这是这段静默里的第一次主动跟进机会，不代表一定要追问。',
            '若决定开口，请顺着刚才的话自然说一句，不要报时，也不要提系统、计时器。',
        ))

    @staticmethod
    def _typing_situation(silence_ms: int, nudges: int) -> str:
        """把等待时长与已催次数渲染成她的主观感受。

        情境写成她看到的事实而不是系统报告：模型据此自行选择语气，代码不规定
        这次该催还是该缓和。

        :param silence_ms: 她上次发言至今的静默毫秒数。
        :param nudges: 本次静默期内已经催过的次数。
        :return: 交给主动消息模型的情境文本。
        """
        minutes = silence_ms // 60_000
        lines = [
            f'你上一句发出去已经 {minutes} 分钟了，他一直没回。',
            '现在你看到他那边开始打字了，话还没发出来。',
        ]
        if nudges:
            lines.append(f'这段时间里你已经催过 {nudges} 次。')
        lines.append('这是一次可选的跟进机会，不是必须开口。')
        lines.append(
            '若决定开口，就顺着这个情形自然地说一句，别复述你等了多久，'
            '也别提「输入状态」这种词。'
        )
        return '\n'.join(lines)

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

    def speak_claimed(
        self,
        context: ConversationContext,
        lines: list[dict],
        *,
        turn: int | None = None,
    ) -> int:
        """投放已经取得 proactive stream 占用权的结构化分句。

        调用方已运行行动决策时可传入同一个 ``turn``，保证决策与观察事件属于同一
        回合。非桌面平台必须改用 ``_speak_claimed_external``，避免只写历史未投递。
        """
        if not lines:
            raise ValueError('主动分句不能为空')
        stream_id = context.stream.id
        if self._stream_claims.get(stream_id) != 'proactive':
            raise RuntimeError('主动投放前必须取得 stream 占用权')
        if turn is None:
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

    async def _speak_claimed_external(
        self,
        context: ConversationContext,
        lines: list[dict],
        *,
        turn: int,
    ) -> int:
        """等待非桌面平台真实投递成功后，再记录这条主动消息。"""
        if context.stream.platform == 'desktop':
            raise ValueError('桌面主动消息不应走外部平台投递')
        await self._dispatch_outbound(
            context,
            turn,
            [line['text'] for line in lines],
            [],
        )
        return self.speak_claimed(context, lines, turn=turn)

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
            messages = [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': PROACTIVE_TRIGGER_MESSAGE},
            ]
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
        # 时段里的具体活动只在他这轮真的问起时才注入，否则日程只以情绪和作息影响本轮语气。
        schedule_desc = (
            self._schedule.describe(
                now,
                self.current_sleep(),
                include_activity=asks_about_activity(query),
            )
            if self._schedule else None
        )
        resumption = self._take_resumption(context.stream.id)
        wm = self.memory.working_memory(
            context.stream.id,
            self._working_memory_messages,
            user_message_id_watermark,
        )
        # raw_history 还服务于旧管线和表达选择器，继续保留角色交替所需的逻辑
        # 顺序；工具 Agent 单独读取原始落库顺序的拍平变体。这样 shadow 开工具
        # 时也不会悄悄改变随后那次旧管线调用的可见行为。
        ordered_wm = self._order_working_memory_for_batch(wm, batch_message_ids)
        raw_history = self._history_for_context(context, ordered_wm)
        agent_wm = wm if self._tool_calling else ordered_wm
        agent_history = self._history_for_context(
            context,
            agent_wm,
            label_message_ids=True,
            flatten=self._tool_calling,
        )
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
            agent_history=agent_history,
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
        decision_only: bool = False,
    ) -> list[dict]:
        """将同一份已组装上下文渲染为模型消息。

        :param prepared: 决策前已完成一次性组装的回合上下文。
        :param facts: 可选的增强后事实列表；省略时使用词面排序结果。
        :param expression_habits: 可选表达习惯提示词块。
        :param render_params: 可选提示词渲染参数收集字典。
        :param reply_length: 当前轮规划出的回复篇幅。
        :param protocol_text: 可选的 Agent 协议文本。传统角色模式把它整体替换进
            system；工具模式据此选择 item 流，协议本身由 ``_render_agent_messages``
            放在末尾。它同时是「本次渲染属于 Agent 路径」的唯一判据。
        :param decision_only: 本次渲染只用于产出动作决策；透传给系统提示词，
            省略回复风格、语调与表达样本三块。
        :return: 首项为 system 消息；传统模式后接裁剪历史，工具模式后接独立的
            运行时上下文 item 与裁剪历史。
        副作用：只读取配置和会话语调，不读写数据库、不调用模型。
        """
        selected_facts = (
            prepared.fact_candidates[:self._fact_recall_limit]
            if facts is None
            else facts
        )
        prompt_kwargs = self._prompt_config_kwargs(
            prepared.context.relationship_signals_enabled,
        )
        shared_context = {
            'now': datetime.fromtimestamp(prepared.now / 1000),
            'persona': prepared.persona,
            'acquaintance': prepared.acquaintance,
            'facts': [fact.content for fact in selected_facts],
            'episodes': prepared.episodes,
            'activity': prepared.activity,
            'schedule': prepared.schedule,
            'expression_habits': expression_habits,
            'reply_length': reply_length,
            'tone': self._session(prepared.context.stream.id).tone,
            'resumption': prepared.resumption,
            'platform_name': prepared.platform_bot_name,
            'scene': self._scene_for_prompt(prepared.context),
            'render_params': render_params,
            'decision_only': decision_only,
            **prompt_kwargs,
        }
        if self._tool_calling and protocol_text is not None:
            # 工具模式的运行时背景不再塞进一个大 system：时间、画像、记忆等
            # 各自保留 item 边界，历史也不做角色合并。协议由
            # _render_agent_messages 放在整个序列末尾，确保它始终是最近的约束。
            system, context_items = build_itemized_system_prompt(**shared_context)
            history = fit_char_budget(
                prepared.agent_history,
                preserve_items=True,
            )
            return [
                {'role': 'system', 'content': system},
                *({'role': 'user', 'content': item} for item in context_items),
                *history,
            ]

        system = build_system_prompt(
            protocol_text=protocol_text,
            emoji_enabled=self._emoji_available(prepared.context),
            **shared_context,
        )
        # Agent 路径读带 [编号] 前缀的历史变体，动作头的 targets 才有可指认的
        # 锚点；旧管线仍读不带编号的原始历史，可见行为完全不受影响。
        source_history = (
            prepared.agent_history if protocol_text is not None else prepared.raw_history
        )
        # 读取历史时再次规范化，兼容早期中断留下的悬空标签；该操作对干净历史幂等。
        history = normalize_history(source_history)
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
        current_topic_available = False
        if context.stream.kind == 'group':
            now = current_time()
            reply_count = self.memory.assistant_reply_count_since(
                context.stream.id,
                now - self._cfg.group_chat.reply_window_minutes * 60_000,
            )
            last_bot_reply_at = self.memory.last_assistant_reply_at(context.stream.id)
            if last_bot_reply_at is not None:
                last_bot_reply_elapsed_ms = now - last_bot_reply_at
                current_topic_available = self.topic_still_hers(
                    context.stream.id, last_bot_reply_at,
                )
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
            current_topic_available=current_topic_available,
            follow_up_declined=self.follow_up_declined(context.stream.id),
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
        *,
        cognitive_rounds: int = 0,
        allow_wait: bool = False,
    ) -> DecisionFrame:
        """按本批消息快照构造回合固定帧，并冻结平台可见产物能力。

        :param cognitive_rounds: 本回合的认知轮次预算；只影响首轮动作集，
            后续各轮由 ConversationAgent 按剩余预算重算。shadow 通道传 0：
            它的用途是观察决策口径，不该为此额外付若干次模型往返。
        :param allow_wait: 本批是否还能「先等等」；已经等过一次的批次传 False，
            此时 wait 不在动作集里，她必须表态。

        speak（起一个不接任何人的话头）按配置开关进入动作集：它不需要独立触发，
        只是在已有候选里多一个选项，因此与 reply 共用同一条频率闸门。
        """
        react_enabled = self._react_available(context)
        capabilities = PlatformCapabilities(
            emoji=self._emoji_available(context),
            react=react_enabled,
            available_reactions=REACTION_IDS if react_enabled else (),
            poke=self._poke_available(context),
        )
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
                cognitive_rounds_left=cognitive_rounds,
                allow_wait=allow_wait,
                allow_speak=self._speak_enabled,
            ),
            capabilities=capabilities,
        )

    def _cognitive_scope(self, frame: DecisionFrame, stream_id: int) -> CognitiveScope:
        """按本回合水位冻结认知检索的会话与人物范围。

        范围在回合开始时定死：水位之后新到的发言者不进入检索范围，使她这一回合
        「能想起谁的事」不随批次外消息漂移。

        :param frame: 本回合固定快照。
        :param stream_id: 当前会话 ID。
        :return: 供本回合全部认知动作共用的检索范围。
        """
        return CognitiveScope(
            stream_id=stream_id,
            person_ids=tuple(
                self.memory.recent_speakers(stream_id, frame.message_watermark)
            ),
        )

    def _emoji_available(self, context: ConversationContext) -> bool:
        """判断当前 QQ stream 是否仍有表情包库和窗口发送额度。"""

        if (
            context.stream.platform != 'qq'
            or self._emoji_library is None
            or not self._emoji_library.has_sendable()
        ):
            return False
        since = current_time() - self._cfg.group_chat.reply_window_minutes * 60_000
        return (
            self.memory.emoji_reply_count_since(context.stream.id, since)
            < EMOJI_MAX_PER_REPLY_WINDOW
        )

    def _react_available(self, context: ConversationContext) -> bool:
        """判断当前 stream 能否执行 QQ 表情回应。

        三个条件缺一不可：平台是 QQ（只有它有这个协议动作）、会话是群聊（私聊
        两个人贴表情没有「让别人看见我在回应谁」的意义）、以及配置显式开启。

        默认关闭是有意的：语义反应名到 QQ 表情编号的映射没有逐个真机核对过，
        贴错表情不会报错、只会显示成另一个表情，属于「不报错的错」。

        **不设独立频率预算**：她要贴表情，先得拿到这一轮候选机会，那已经受门控
        与 ``max_replies_in_window`` 约束；再加一个窗口常量只会多一组互相牵制的
        数字，而贴表情本身比发言轻得多。

        :param context: 当前会话上下文。
        :return: 允许 react 进入动作集时返回 ``True``。
        """
        return (
            context.stream.platform == 'qq'
            and context.stream.kind == 'group'
            and self._cfg.group_chat.reactions_enabled
        )

    def _poke_available(self, context: ConversationContext) -> bool:
        """判断当前 stream 能否戳一戳。

        条件与表情回应同构（QQ + 群聊 + 配置开关），但**默认关闭**：贴表情是
        安静的，戳一戳会给对方推送提醒，扰动量级完全不同，该由使用者主动打开。

        :param context: 当前会话上下文。
        :return: 允许 poke 进入动作集时返回 ``True``。
        """
        return (
            context.stream.platform == 'qq'
            and context.stream.kind == 'group'
            and self._cfg.group_chat.pokes_enabled
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

    def _selectable_message_previews(
        self,
        batch: list[_BufferedMessage],
    ) -> list[tuple[int, str]]:
        """把本批可选消息渲染为「消息 ID + 展示原文」序列。

        群聊原文按历史同一口径带上发送者显示名，使协议块里的清单与模型看到
        的历史行逐字对应；私聊历史本就不带名字，因此只给正文。

        :param batch: 本回合已完成图片描述补齐的批次消息。
        :return: 与 ``selectable_message_ids`` 同序的 ``(消息 ID, 原文)`` 列表。
        :raises ValueError: 群聊消息的发送者在注册表中不存在时由注册表抛出。
        """
        previews: list[tuple[int, str]] = []
        for message in batch:
            context = message.context
            if context.stream.kind == 'group':
                name = self._registry.stream_display_name(
                    context.person.id,
                    context.stream.id,
                )
                previews.append((message.message_id, f'{name}: {message.text}'))
            else:
                previews.append((message.message_id, message.text))
        return previews

    def _render_agent_protocol(
        self,
        frame: DecisionFrame,
        batch: list[_BufferedMessage],
        context: ConversationContext,
    ) -> str:
        """渲染本回合的动作头协议文本。

        该文本由调用方整体替换系统提示词中的直接发言协议；shadow 与 live 共用，
        保证两条灰度路径看到的输出规则完全一致。

        可选消息必须连同原文一起写进协议：消息 ID 是数据库主键，在对话历史里
        没有任何可见锚点，只给一串孤立数字时模型会把 targets 填成「凌白最后
        一条」这类描述，整轮按 illegal_action 失败、用户侧表现为她不回话。

        :param frame: 本回合固定快照，提供动作空间、可选消息与平台能力。
        :param batch: 与 ``frame.selectable_message_ids`` 同源的批次消息；
            自主回合没有待接消息，传空列表。
        :param context: 当前会话上下文；自主回合没有批次可反查，必须显式给出。
        :return: 已注入运行时动作集与目标锚点清单的协议文本。
        """
        target_person = (
            self._registry.stream_display_name(context.person.id, context.stream.id)
            if context.stream.kind == 'group' and batch
            else ''
        )
        if self._tool_calling:
            # 工具调用模式下动作空间由函数签名承载，提示词只留目标锚点与选择
            # 口径；两套输出协议同时出现会让模型在写 XML 与调工具之间摇摆。
            return render_tool_protocol(
                self._selectable_message_previews(batch),
                quote_supported=frame.capabilities.quote,
                target_person=target_person,
                cognitive_rounds=self._cognitive_rounds,
                available_actions=frame.available_actions,
            )
        return render_action_protocol(
            sorted(frame.available_actions),
            self._selectable_message_previews(batch),
            quote_supported=frame.capabilities.quote,
            emoji_enabled=frame.capabilities.emoji,
            target_person=target_person,
            cognitive_rounds=self._cognitive_rounds,
            available_reactions=frame.capabilities.available_reactions,
        )

    def _render_agent_messages(
        self,
        frame: DecisionFrame,
        messages: list[dict],
        continuation_recap: str = '',
        protocol_text: str | None = None,
    ) -> list[dict]:
        """把已渲染消息整理为 Conversation Agent 实际提交的上下文。

        XML 模式下，助手历史去掉 ``<say>`` 外壳，再在真实用户消息前插入
        reply/silent few-shot，并把输出要求并进末条用户消息。工具模式下不再做
        任何角色重排或合并：system 之后的时间、画像、历史与续跑提示全部保持
        独立 user item，工具/回复协议作为最后一项。

        :param frame: 本回合固定快照，提供动作空间与可选消息。
        :param messages: ``_render_prepared_context`` 产出的系统与历史消息。
        :param continuation_recap: 续跑轮里她上一轮做了什么；非空时按
            ``_CONTINUATION_NOTICE`` 渲染成提示，插在候选块与输出指令之间。
        :param protocol_text: 工具模式必需的末轮协议；XML 模式已在 system 内，
            因此忽略该参数。
        :return: 按当前协议模式整理完成的新消息列表。
        :raises ValueError: 工具模式缺少协议，或上游仍传入 assistant 历史。
        """
        if self._tool_calling:
            if not protocol_text or not protocol_text.strip():
                raise ValueError('工具调用的 item 流缺少末轮协议')
            if not messages or messages[0].get('role') != 'system':
                raise ValueError('工具调用的 item 流必须以 system 开头')
            invalid_roles = [
                item.get('role') for item in messages[1:]
                if item.get('role') != 'user'
            ]
            if invalid_roles:
                raise ValueError(
                    f'工具调用的 item 流只能包含 user 上下文，收到：{invalid_roles}'
                )
            flattened = [dict(item) for item in messages]
            if continuation_recap:
                flattened.append({
                    'role': 'user',
                    'content': _CONTINUATION_NOTICE.format(recap=continuation_recap),
                })
            flattened.append({
                'role': 'user',
                'content': protocol_text.rstrip(),
            })
            return flattened

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
        # 续跑提示并进候选块所在的这条 user 消息，而不是再追加一条：输出要求必须
        # 留在整个序列的最末，它是生成侧最近的约束，被别的文字挤开之后动作头就
        # 不再是模型最先要交代的东西。
        continuation = (
            _CONTINUATION_NOTICE.format(recap=continuation_recap) + '\n\n'
            if continuation_recap else ''
        )
        output_rule = (
            '[输出要求] 你下一条回复必须先输出 <decision> 动作标签；'
            '正文只能放在其后的 <say> 里，禁止在 <decision> 之前输出 '
            '<say>、普通文字或解释。'
        )
        history[last_user_index] = {
            **history[last_user_index],
            'content': (
                f"{history[last_user_index]['content']}\n\n"
                f'{continuation}'
                f'{output_rule}'
            ).rstrip() if (continuation or output_rule) else history[last_user_index]['content'],
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
        protocol_text = self._render_agent_protocol(frame, batch, context)
        render_params: dict[str, dict[str, str]] = {}
        messages = self._render_agent_messages(
            frame,
            self._render_prepared_context(
                prepared,
                render_params=render_params,
                protocol_text=protocol_text,
                decision_only=self._tool_calling,
            ),
            protocol_text=protocol_text,
        )
        metadata = prompt_metadata(
            'chat.conversation',
            CHAT_TOOL_TEMPLATE_IDS if self._tool_calling else CHAT_CONVERSATION_TEMPLATE_IDS,
        )
        trace.emit(
            'llm_request',
            turnId=turn,
            messages=messages,
            temperature=self._planner_temperature if self._split_replyer else self._chat_temperature,
            maxTokens=self._planner_max_tokens if self._split_replyer else self._chat_max_tokens,
            renderParams=render_params,
            **metadata,
        )
        bind_render_params(render_params)

        async def replyer_messages(head: DecisionHead) -> list[dict]:
            """为工具模式的影子发言生成随后会被完整丢弃的合法正文。"""
            replyer_protocol = render_replyer_protocol(
                head.reference or '',
                head.length,
                emoji_enabled=frame.capabilities.emoji,
            )
            replyer_context = self._render_prepared_context(
                prepared,
                render_params=render_params,
                reply_length=head.length,
                protocol_text=replyer_protocol,
            )
            replyer_items = self._render_agent_messages(
                frame,
                replyer_context,
                protocol_text=replyer_protocol,
            )
            trace.emit(
                'llm_request',
                turnId=turn,
                messages=replyer_items,
                temperature=self._replyer_temperature,
                maxTokens=self._replyer_max_tokens,
                renderParams=render_params,
                shadow=True,
                **prompt_metadata('chat.replyer', CHAT_TOOL_REPLYER_TEMPLATE_IDS),
            )
            bind_render_params(render_params)
            return replyer_items

        try:
            outcome = await self._conversation_agent.run(
                frame,
                messages,
                gate_inputs,
                batch_gate.result.reason_codes,
                prompt_hash=metadata['promptHash'],
                model_task='chat.conversation.shadow',
                signal=cancel_event,
                replyer_messages=replyer_messages if self._tool_calling else None,
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
        allow_wait: bool = False,
    ) -> None:
        """把一个回合作为**系统驱动的循环**跑完，而不是一次性调用。

        产出可见产物不等于回合结束：她说完一句之后，真人还会再看一眼群里，确认
        自己是不是说完了、有没有人接上。因此 reply / react / poke / speak 之后
        重建上下文（此时历史里已经有她刚说的那句）再问一次，直到她自己表态收束。

        收束由她决定，不由动作类型决定：
        - ``declined``（silent）：她表示这轮做完了，退出循环；
        - ``paused``（wait）：批次已退回缓冲等下文，退出循环；
        - ``failed``：模型或协议失败，退出循环；
        - ``acted``：产出了可见产物，重建上下文继续下一轮。

        ``MAX_TURN_ROUNDS`` 只是防失控的保险。撞上限说明她连续 10 轮都没有表示
        说完，那是异常而不是正常收束，因此留一条明确记录而不是静默结束。

        续跑轮消费的是**她生成期间新到的消息**，原批次不再累加：那一批她上一轮
        已经接过了。她上一轮做了什么由 ``_RoundResult.recap`` 显式带给下一轮，
        不依赖历史里那份副本是否幸存，原因见 ``_CONTINUATION_NOTICE``。

        :param batch: 首轮的消息批次；续跑各轮换成新到的那一批。
        :param allow_wait: 首轮是否还能「先等等」；同一批只允许等一次，因此
            续跑各轮一律不再给 wait。
        副作用：每轮一次模型往返与一次可见产物投递；上下文与 sink 逐轮重建。
        """
        # 回合级副作用只在整个循环收束后结算一次。人格结算、摘要触发、场景观察
        # 都是「这个回合发生过什么」的账，逐轮各记一遍会让多轮回合把人格推动几倍。
        acted = False
        exhausted = True
        # 上一轮的动作事实；首轮为空，此后每轮由 _RoundResult 带过来。
        recap = ''
        for round_index in range(MAX_TURN_ROUNDS):
            if cancel_event.is_set():
                exhausted = False
                break
            if round_index > 0:
                # 并入她生成期间到达的新消息。**没有新消息就不再续跑**：这一轮
                # 唯一能变的就是「群里又说了什么」，什么都没变时再问一次模型，
                # 产出只可能是「我说完了」，不值一次往返。
                pending = self._buffers.get(context.stream.id)
                if not pending:
                    exhausted = False
                    break
                arrived = list(pending)
                del pending[:]
                self._buffers.pop(context.stream.id, None)
                # 候选块只放新到的消息：原批次她上一轮已经接过了，累加进来会让她
                # 对着已回复过的句子再答一次，也会把她自己那条回复挤出历史。
                batch = await self._materialize_batch_images(arrived)
                trimmed = '\n'.join(message.text for message in batch)
                # 重建上下文与 sink：历史里已经多了她上一轮说的话，sink 的分句与
                # 副作用是单轮状态，复用会把上一轮的正文再发一次。
                now = current_time()
                prepared = self._prepare_turn_context(
                    context,
                    trimmed,
                    now,
                    prepared.platform_bot_name,
                    user_message_id_watermark=batch[-1].message_id,
                    batch_message_ids=tuple(
                        message.message_id for message in batch
                    ),
                )
                sink = _TurnSink(
                    context=context,
                    cancel_event=cancel_event,
                    turn=turn,
                    now=now,
                    source_text=trimmed,
                )
            result = await self._run_conversation_round(
                context,
                batch,
                trimmed,
                turn,
                cancel_event,
                sink,
                prepared,
                batch_gate,
                sender,
                render_params,
                allow_wait=allow_wait and round_index == 0,
                continuation_recap=recap,
            )
            acted = acted or result.reason == 'acted'
            if result.reason != 'acted':
                exhausted = False
                break
            recap = result.recap
        if exhausted:
            logger.warning(
                'conversation_turn_rounds_exhausted',
                turnId=turn,
                streamId=context.stream.id,
                maxRounds=MAX_TURN_ROUNDS,
            )
            self._mark_stage(
                context, GATED,
                f'连续 {MAX_TURN_ROUNDS} 轮没有收束，本回合强制结束',
                turn_id=turn,
            )
        if not acted:
            return
        # 本回合确实产出过可见产物，才结算这一次。
        try:
            self.persona.apply_turn(
                context.person.id,
                current_time(),
                weight=self._persona_weight(context),
            )
        except Exception as exc:
            # 人格结算是附加状态，失败不能回滚已经展示并持久化的对话正文。
            logger.warning('persona_apply_turn_failed', turnId=turn, error=str(exc))
        asyncio.create_task(self._maybe_summarize(context.stream.id))
        self._schedule_scene_observation(context)

    async def _run_conversation_round(
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
        allow_wait: bool = False,
        continuation_recap: str = '',
    ) -> _RoundResult:
        """执行**一轮** Conversation Agent 调用并处理其结果。

        silent 只写行动决策事件，不产生任何用户可见输出；reply 复用既有 sink
        消费副作用与分句，随后持久化、人格结算与平台投递；模型/协议失败不
        流出任何正文，按失败状态呈现。

        :param continuation_recap: 上一轮她做了什么的第一人称陈述；非空即表示
            本轮是回合内的续跑轮，据此在候选块与输出要求之间插入续跑提示。她的
            上一句话在历史里可能已被裁掉，因此这个事实必须由调用方带进来，不能
            让她去历史里找（详见 ``_CONTINUATION_NOTICE``）。
        :return: 本轮的收束原因与动作事实，由 ``_run_conversation_turn`` 据此决定
            继续还是退出循环，并把动作事实转交下一轮。
        """
        frame = self._agent_frame(
            context,
            batch,
            turn,
            batch_gate.result.disposition,
            cognitive_rounds=self._cognitive_rounds,
            allow_wait=allow_wait,
        )
        gate_inputs = self._agent_gate_inputs(frame, batch_gate)
        protocol_text = self._render_agent_protocol(frame, batch, context)
        if self._split_replyer:
            # 拆分后决策这一次不做表达增强：向量排序与表达样本挑选都只影响
            # 「话怎么说」，而这一次调用不写正文。挪到回复生成那一侧还顺带
            # 省掉一整类浪费——她选择 silent 时，表达选择那次模型调用根本
            # 不会发生，而合并调用时那笔钱是无论如何都要先付的。
            rendered = self._render_prepared_context(
                prepared,
                render_params=render_params,
                protocol_text=protocol_text,
                decision_only=True,
            )
        else:
            rendered = await self._enrich_prepared_context(
                prepared,
                cancel_event,
                render_params,
                reply_length=None,
                protocol_text=protocol_text,
            )
        messages = self._render_agent_messages(
            frame,
            rendered,
            continuation_recap=continuation_recap,
            protocol_text=protocol_text,
        )
        self._mark_stage(context, GENERATING, turn_id=turn)
        metadata = prompt_metadata(
            'chat.conversation',
            CHAT_TOOL_TEMPLATE_IDS if self._tool_calling else CHAT_CONVERSATION_TEMPLATE_IDS,
        )
        trace.emit(
            'llm_request',
            turnId=turn,
            messages=messages,
            temperature=self._planner_temperature if self._split_replyer else self._chat_temperature,
            maxTokens=self._planner_max_tokens if self._split_replyer else self._chat_max_tokens,
            renderParams=render_params,
            **metadata,
        )
        bind_render_params(render_params)
        assistant_raw: list[str] = []

        async def replyer_messages(head: DecisionHead) -> list[dict]:
            """按已定的动作头组装回复生成那一次调用的消息序列。

            人格分两层：决策那一次只拿身份、关系与记忆，这一次才补上表达层
            （向量排序后的事实、表达样本、语调）。两级共用同一份 ``prepared``，
            因此历史、事实候选与场景完全同源，不会出现「决策依据」和「说话依据」
            各说各话；差别只在表达增强与协议段。

            :param head: 已通过校验的动作头，提供背景说明与篇幅。
            :return: 回复生成那一次调用的完整消息序列。
            副作用：一次向量检索与一次表达选择模型调用。
            """
            replyer_protocol = render_replyer_protocol(
                head.reference or '',
                head.length,
                emoji_enabled=frame.capabilities.emoji,
            )
            replyer_context = await self._enrich_prepared_context(
                prepared,
                cancel_event,
                render_params,
                reply_length=head.length,
                protocol_text=replyer_protocol,
            )
            messages = self._render_agent_messages(
                frame,
                replyer_context,
                continuation_recap=continuation_recap,
                protocol_text=replyer_protocol,
            )
            trace.emit(
                'llm_request',
                turnId=turn,
                messages=messages,
                temperature=self._replyer_temperature,
                maxTokens=self._replyer_max_tokens,
                renderParams=render_params,
                **prompt_metadata(
                    'chat.replyer',
                    CHAT_TOOL_REPLYER_TEMPLATE_IDS
                    if self._tool_calling else CHAT_REPLYER_TEMPLATE_IDS,
                ),
            )
            bind_render_params(render_params)
            return messages

        async def on_events(events: Iterable[ParseEvent]) -> None:
            await self._consume_events(events, sink)

        def on_chunk(chunk: dict[str, Any]) -> None:
            text = chunk.get('text')
            if text:
                assistant_raw.append(text)
            trace.emit('llm_chunk', turnId=turn, text=text, reasoning=chunk.get('reasoning'))

        def on_round(round_outcome: AgentOutcome) -> None:
            """把认知轮显示到控制台。

            认知轮不产生任何用户可见产物，不渲染的话终端上只会看到「她沉默了
            十几秒然后说了句话」，中间查了什么完全不可见。
            """
            assert round_outcome.decision is not None
            render_action_decision(
                turn=turn,
                agent_scope='live',
                event_status=round_outcome.event_status,
                action=round_outcome.decision.action,
                query=round_outcome.decision.query or '',
                observation=round_outcome.observation,
            )

        outcome = await self._conversation_agent.run(
            frame,
            messages,
            gate_inputs,
            batch_gate.result.reason_codes,
            prompt_hash=metadata['promptHash'],
            model_task='chat.conversation',
            provider_name=getattr(self._chat_provider, 'provider', ''),
            model_name=getattr(self._chat_provider, 'model', ''),
            cognitive_executor=self._cognitive_executor,
            # 关闭 ReAct 时连范围都不算：那是一次真实的数据库查询，
            # 为一个永远不会被消费的字段付账没有意义。
            cognitive_scope=(
                self._cognitive_scope(frame, context.stream.id)
                if self._cognitive_executor is not None
                else None
            ),
            cognitive_rounds=self._cognitive_rounds,
            on_events=on_events,
            on_chunk=on_chunk,
            on_round=on_round,
            replyer_messages=replyer_messages if self._split_replyer else None,
            signal=cancel_event,
        )
        raw_text = ''.join(assistant_raw)
        trace.emit('llm_final', turnId=turn, text=raw_text)
        # 终局动作一律先在控制台留一行「她决定做什么、为什么」。此前只有 reply 会
        # 通过 render_turn 露面，silent / wait / react / poke 全是空白——终端上看不出
        # 她到底是在思考、在等、还是根本没被叫醒。
        if outcome.decision is not None:
            render_action_decision(
                turn=turn,
                agent_scope='live',
                event_status=outcome.event_status,
                action=outcome.decision.action,
                reason_codes=outcome.decision.reason_codes,
                target_message_ids=outcome.decision.target_message_ids,
            )
        if outcome.event_status == 'silent_by_choice':
            assert outcome.decision is not None
            if context.stream.kind == 'group':
                # 她看过这一轮并决定不接，自然回应窗口就此关闭；后续普通群消息
                # 重新回到攒批判断，直到真信号或她自己再次开口把窗口打开。
                # wait 与 react 不置位：前者是「话还没说完」，后者仍是参与。
                self._follow_up_declined.add(context.stream.id)
            # silent 只写行动决策事件：不产生助手历史、TTS、事实或观察事件。
            self._mark_stage(
                context, GATED,
                f'她选择沉默：{", ".join(outcome.decision.reason_codes)}',
                turn_id=turn,
            )
            return _RoundResult('declined')
        if outcome.event_status != 'committed':
            render_turn_error(
                turn, sender['senderLabel'], trimmed,
                outcome.event_status, outcome.action_event.detail,
                model_name=getattr(self._chat_provider, 'model', ''),
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
            return _RoundResult('failed')
        assert outcome.decision is not None
        if outcome.decision.action == 'wait':
            self._hold_batch_for_wait(context, batch, turn, outcome)
            return _RoundResult('paused')
        if outcome.decision.action == 'poke':
            recap = await self._apply_poke(
                context, batch, turn, outcome, frame, gate_inputs, batch_gate,
            )
            return _RoundResult('acted', recap)
        if outcome.decision.action == 'react':
            recap = await self._apply_reaction(
                context, turn, outcome, frame, gate_inputs, batch_gate,
            )
            return _RoundResult('acted', recap)
        # speak 与 reply 的产物形态完全相同（正文 + 可选表情包），区别只在有没有
        # 目标消息，因此共用下面这条持久化与投递路径；_quote_target 对空目标返回
        # None，speak 自然不会挂引用。
        assert outcome.decision.reply is not None
        # 历史只落可见正文：动作头不进入记忆，读历史时不会污染后续提示词。
        visible_markup = (
            ''.join(f'<say>{segment}</say>' for segment in sink.segments)
            + _emoji_history_markup(sink.emoji_items)
        )
        if visible_markup:
            self.memory.append_message(
                context.stream.id,
                None,
                'assistant',
                visible_markup,
            )
        render_turn(
            turn,
            sender['senderLabel'],
            trimmed,
            messages,
            sink.segments,
            sink.side_effects,
            self._bot_display_name,
            model_name=getattr(self._chat_provider, 'model', ''),
        )
        if context.stream.platform == 'desktop':
            self._mark_stage(context, DISPATCHING, turn_id=turn)
            await self._emit(context.stream.id, 'chat.done', {'turnId': turn, 'kind': 'done'})
        else:
            try:
                await self._dispatch_outbound(
                    context,
                    turn,
                    sink.segments,
                    sink.emoji_items,
                    self._quote_target(context, outcome.decision.target_message_ids),
                )
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
        self._arm_direct_follow_up(context)
        # 她刚开过口，对话仍在她这边：重新敞开自然回应窗口，让紧接着说的话
        # 不必再经过回复必要性评分就能进入她的视野。
        self._follow_up_declined.discard(context.stream.id)
        return _RoundResult('acted', _spoken_recap(sink.segments, sink.emoji_items))

    def _hold_batch_for_wait(
        self,
        context: ConversationContext,
        batch: list[_BufferedMessage],
        turn: int,
        outcome: AgentOutcome,
    ) -> None:
        """把本批消息放回缓冲，等对方把话说完再决定。

        wait 与 silent 的区别是**这批消息算不算处理过**：silent 是「我决定不接
        这茬」，批次就此消费；wait 是「话还没说完，我先不表态」，批次退回缓冲，
        下次与新到的消息合并成更大的一批重新判断，她那时能同时接住前因后果。

        三件事必须一起做，少一件都会让等待失效：

        1. **批次放回缓冲头部**——放头部而不是尾部，因为后到的消息本来就更晚，
           这样合并后仍是时间正序（与 `_tick` 异常回滚路径同一个写法）。
        2. **累计器加回去**——扩展触发口径在拿到候选时清零了累计，不加回去的话
           这批退回后再也攒不够条数，她将永远不再看到它们。
        3. **标记等待**——`_tick` 据此在没有新消息之前不再重开回合，否则轮询
           周期一到就会把同一批重新问一遍模型。

        没有新消息就一直等下去，这是有意的：她在等一句没有来的话，那就一直没有
        回应，和真人「等着等着就算了」是同一个行为。任何一条新消息都会解除等待，
        并且那一轮 wait 已不在动作集里，她必须表态。

        :param context: 当前会话上下文。
        :param batch: 本轮取走但决定不消费的消息批次。
        :param turn: 对话回合 ID。
        :param outcome: 已确认 action 为 wait 的 Agent 结果。
        副作用：修改消息缓冲、扩展累计器与等待标记；不产生任何用户可见输出。
        """
        assert outcome.decision is not None
        stream_id = context.stream.id
        buffered = self._buffers.setdefault(stream_id, [])
        buffered[:0] = batch
        self._extended_pending[stream_id] = (
            self._extended_pending.get(stream_id, 0) + len(batch)
        )
        self._waiting[stream_id] = len(buffered)
        self._mark_stage(
            context, GATED,
            f'她先等等：{", ".join(outcome.decision.reason_codes)}',
            turn_id=turn,
        )

    async def _apply_poke(
        self,
        context: ConversationContext,
        batch: list[_BufferedMessage],
        turn: int,
        outcome: AgentOutcome,
        frame: DecisionFrame,
        gate_inputs: GateInputFacts,
        batch_gate: _BatchGate,
    ) -> str:
        """戳一戳目标消息的发送者。

        目标沿用消息编号而不是新开一个「人物编号」目标空间：跨人物选目标那件事
        还没有解决（人物归属要连带重绑关系与事实），再引入一套目标空间只会让
        同一个未解问题多一个入口。发送者由消息反查。

        :param context: 当前会话上下文。
        :param batch: 本回合批次，用于把目标消息反查回发送者。
        :param turn: 对话回合 ID。
        :param outcome: 已确认 action 为 poke 的 Agent 结果。
        :param frame: 本回合固定快照，用于组装投递失败事件。
        :param gate_inputs: 第 1 层确定性输入事实。
        :param batch_gate: 本批门控结果。

        :return: 供续跑轮引用的动作事实。戳一戳不写助手历史，这个动作在下一轮
            的上下文里唯一的痕迹就是这句话。
        :raises RuntimeError: 目标消息不在本批内，或发送者没有该平台身份。
        :raises DeliveryError: 平台驱动不支持戳一戳或调用失败。
        副作用：调用平台驱动并写入投递观察事件；失败追加 delivery_failed 事件再上抛。
        """
        assert outcome.decision is not None
        self._mark_stage(context, DISPATCHING, turn_id=turn)
        try:
            if self._broker is None:
                raise RuntimeError('非桌面 stream 未配置 PlatformBroker')
            target_id = outcome.decision.target_message_ids[0]
            target = next(
                (item for item in batch if item.message_id == target_id), None,
            )
            if target is None:
                raise RuntimeError(f'戳一戳目标消息 {target_id} 不在本回合批次内')
            external_id = next(
                (
                    identity.external_id
                    for identity in self._registry.list_identities(target.context.person.id)
                    if identity.platform == context.stream.platform
                ),
                '',
            )
            if not external_id:
                raise RuntimeError(
                    f'人物 {target.context.person.id} 没有 {context.stream.platform} 身份，无法戳'
                )
            receipt = await self._broker.dispatch_poke(OutboundPoke(
                stream=context.stream,
                target_external_id=external_id,
            ))
        except Exception as exc:
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
        trace.emit(
            'outbound_delivered',
            platform=receipt.platform,
            streamId=receipt.stream_id,
            turnId=turn,
        )
        self._mark_stage(context, REPLIED, '戳了一下', turn_id=turn)
        return '你刚戳了 {} 一下'.format(
            self._registry.stream_display_name(
                target.context.person.id, context.stream.id,
            )
        )

    async def _apply_reaction(
        self,
        context: ConversationContext,
        turn: int,
        outcome: AgentOutcome,
        frame: DecisionFrame,
        gate_inputs: GateInputFacts,
        batch_gate: _BatchGate,
    ) -> str:
        """投递一次表情回应，并按与回复相同的口径结算人格。

        表情回应**不写助手历史**：她这一轮没有说任何话，往历史里塞一条伪造的
        「消息」会污染后续提示词，让她以为自己讲过什么。这一轮的记录是
        ``action_decision`` 事件本身。

        人格结算沿用回复那一套权重，不为 react 单独发明一个更轻的系数：她确实
        参与了这一轮，而一轮只允许一个动作，再加一个互相牵制的常量换不来什么。

        :param context: 当前会话上下文。
        :param turn: 对话回合 ID。
        :param outcome: 已确认 action 为 react 的 Agent 结果。
        :param frame: 本回合固定快照，用于组装投递失败事件。
        :param gate_inputs: 第 1 层确定性输入事实。
        :param batch_gate: 本批门控结果。

        :return: 供续跑轮引用的动作事实。这一轮没有助手历史，下一轮能看见这个
            动作的唯一途径就是这句话。
        :raises RuntimeError: 目标消息没有平台编号，或非桌面 stream 未配置 broker。
        :raises DeliveryError: 平台驱动不支持表情回应或调用失败。

        副作用：调用平台驱动、写入投递观察事件并结算人格；失败时追加一条
            delivery_failed 行动事件再上抛。
        """
        assert outcome.decision is not None and outcome.decision.reaction is not None
        self._mark_stage(context, DISPATCHING, turn_id=turn)
        try:
            if self._broker is None:
                raise RuntimeError('非桌面 stream 未配置 PlatformBroker')
            target_id = outcome.decision.target_message_ids[0]
            external_id = self.memory.external_message_id(context.stream.id, target_id)
            if not external_id:
                # 内部 ID 发不出去。历史消息没有回填平台编号时无法回应，如实失败，
                # 不退化成「改成发条消息」——那是替她改主意。
                raise RuntimeError(
                    f'消息 {target_id} 没有平台编号，无法贴表情回应'
                )
            receipt = await self._broker.dispatch_reaction(OutboundReaction(
                stream=context.stream,
                target_external_message_id=external_id,
                reaction=outcome.decision.reaction,
            ))
        except Exception as exc:
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
        trace.emit(
            'outbound_delivered',
            platform=receipt.platform,
            streamId=receipt.stream_id,
            turnId=turn,
        )
        self._mark_stage(
            context, REPLIED, f'贴了个「{outcome.decision.reaction}」', turn_id=turn,
        )
        return f'你刚给消息 [{target_id}] 贴了个「{outcome.decision.reaction}」'

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

    def topic_still_hers(self, stream_id: int, last_bot_reply_at: int) -> bool:
        """判断她上次开口之后群里聊过的话是否还没走远。

        用消息距离而不是时间：群热时几秒能刷十几条、话题早换了，群温吞时三分钟
        才两句、话题一点没变。时限口径由 natural_reply_window 单独承担，两者
        在门控里取并集。

        :param stream_id: 目标群 stream ID。
        :param last_bot_reply_at: 她上一条回复的落库毫秒时间戳。
        :return: 从该时刻起（含她那条）累计消息不超过 ONGOING_TOPIC_MESSAGE_SPAN
            条时返回 True。
        :raises sqlite3.Error: 统计消息表失败时由记忆层抛出。
        副作用：只读 messages 表。
        """
        spanned = self.memory.message_count_since(stream_id, last_bot_reply_at)
        return spanned <= ONGOING_TOPIC_MESSAGE_SPAN

    def follow_up_declined(self, stream_id: int) -> bool:
        """返回她是否已在这个群的上一次跟进机会里主动选择了沉默。

        入口门控与批次门控必须读同一份事实，否则 reply_gate 审计事件会报告一个
        与实际生效判定不同的门控态，现场排查时无法据此还原真实路径。

        :param stream_id: 目标 stream ID。
        :return: 她放弃过且此后没有再开口时返回 True。
        """
        return stream_id in self._follow_up_declined

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

    def _history_for_context(
        self,
        context: ConversationContext,
        messages: list[Any],
        *,
        label_message_ids: bool = False,
        flatten: bool = False,
    ) -> list[dict]:
        """将记忆消息转换为模型历史，补充发言时刻，并在群聊中补充发送者显示名。

        用户消息带 ``HH:MM`` 前缀（跨天时带 ``MM-DD HH:MM``），让「这条是刚说的
        还是十分钟前说的」「两个人之间隔了多久」成为她能直接读到的事实，而不必由
        门控常量代为判断。传统角色模式下她自己的历史回复不加时刻：assistant 行
        仍是输出范例，行首前缀可能被模仿进动作头。扁平模式没有这种格式示范职责，
        因而每一方都带时间与说话人，落库顺序就是可直接阅读的事件顺序。

        :param context: 当前会话上下文。
        :param messages: 记忆服务返回的消息对象列表。
        :param label_message_ids: 是否给每条用户消息加 ``[编号] `` 前缀。仅
            Conversation Agent 上下文需要：动作头的 targets 是消息主键，主键
            不逐行可见时模型无法指认，会把 targets 写成人名或「最后一条」这类
            描述，整轮按 illegal_action 失败。她自己的历史回复不加编号——本
            回合只允许把批次内的用户消息作为目标，给助手行编号只会诱导越界。
        :param flatten: 把她自己的发言也渲染成 ``user`` 角色，用显示名区分
            说话人，并在转角色前清掉历史协议与副作用标签。工具调用模式专用：
            那里动作由函数签名承载，助手行不再承担
            「这是你的输出格式」的示范作用，而拍平能一次性拆掉一整条脆弱链路
            ——角色必须交替这条端点要求，逼出了「把跨轮落库的回复重排到批次
            之前」，重排又可能把它顶到开头被 ``normalize_history`` 丢掉。拍平
            之后不存在开头 assistant，也就没有那个丢法。

        :return: 仅含 ``role`` 和 ``content`` 的模型消息列表；原始记忆对象不被修改。

        :raises RuntimeError: 群聊用户消息缺少发送者人物 ID。
        """
        history: list[dict] = []
        last_stamped_date = None
        for message in messages:
            content = message.content
            role = message.role
            if flatten and message.role == 'assistant':
                # 转成 user 之前先按 assistant 语义清理；一旦改完角色，
                # normalize_history 就不会再替它剥副作用标签与 <say> 外壳。
                content = strip_say_tags(
                    close_dangling_say(strip_side_effect_tags(content)),
                )
                if not content:
                    continue
                content = f'{self._bot_display_name}: {content}'
                role = 'user'
            if context.stream.kind == 'group' and message.role == 'user':
                if message.sender_person_id is None:
                    raise RuntimeError('群聊 user 历史缺少 sender_person_id')
                name = self._registry.stream_display_name(
                    message.sender_person_id,
                    context.stream.id,
                )
                content = f'{name}: {content}'
            if message.role == 'user' or (flatten and message.role == 'assistant'):
                spoken_at = datetime.fromtimestamp(message.created_at / 1000)
                # 跨天才带日期：工作记忆可能横跨若干天，但同一天内逐行重复日期
                # 只会挤占上下文；系统提示词已经给出「现在是几点」，她据此就能算出
                # 每条消息离现在多久、彼此间隔多长。
                if last_stamped_date == spoken_at.date():
                    stamp = spoken_at.strftime('%H:%M')
                else:
                    stamp = spoken_at.strftime('%m-%d %H:%M')
                    last_stamped_date = spoken_at.date()
                content = f'{stamp} {content}'
            if label_message_ids and message.role == 'user':
                content = f'[{message.message_id}] {content}'
            history.append({'role': role, 'content': content})
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
            if (
                isinstance(event, EmojiEvent)
                and not sink.emoji_items
                and self._emoji_available(context)
                and self._emoji_library is not None
            ):
                selection = await self._emoji_library.select(event.emotion)
                if selection is not None:
                    sink.emoji_items.append((
                        event.emotion,
                        selection.send_ref,
                        selection.sub_type,
                    ))
                    trace.emit(
                        'emoji_selected',
                        turnId=sink.turn,
                        streamId=context.stream.id,
                        emotion=event.emotion,
                    )
            self._handle_side_effects(
                context, event, sink.now, sink.turn, sink.side_effects, sink.source_text,
            )
            if context.stream.platform == 'desktop':
                self._track_speech(context, event, sink.turn)
                await self._emit_parse_event(context, sink.turn, event)
            sink.segment = _collect_outbound_segment(
                event, sink.segments, sink.segment, self._cfg.typing,
            )

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

    def _quote_target(
        self,
        context: ConversationContext,
        target_message_ids: tuple[int, ...],
    ) -> str | None:
        """判断这一轮回复是否需要挂引用，并给出被引用消息的平台编号。

        群里消息滚动快，而她一轮生成要十几秒，回复落地时目标常常已经被后面的
        消息冲开，旁观者看不出她在接哪一句。因此判据只有一条：目标消息之后本
        stream 已经出现更新的消息，就挂引用。私聊只有两个人，任何时候都不会认错
        对象，一律不引用。

        引用与否不进模型的动作头：目标已经由模型选定，引用只是这个选择在平台上的
        呈现方式，属于代码强制的可读性边界，多给模型一个字段只会多一种写错的方式。

        :param context: 目标会话上下文。
        :param target_message_ids: 决策选中的目标消息内部 ID；为空表示无目标。
        :return: 被引用消息的平台编号；不需要或无法引用时返回 ``None``。
        """
        if context.stream.kind != 'group' or not target_message_ids:
            return None
        target_id = target_message_ids[0]
        if not self.memory.has_user_messages_after(context.stream.id, target_id):
            return None
        # 平台编号缺失说明这条消息早于编号落库改动，或来自不带编号的通道，
        # 此时只能不引用；不能拿内部 ID 冒充平台编号发出去。
        return self.memory.external_message_id(context.stream.id, target_id)

    async def _dispatch_outbound(
        self,
        context: ConversationContext,
        turn: int,
        segments: list[str],
        emoji_items: list[tuple[str, str, int]],
        quote_external_message_id: str | None = None,
    ) -> None:
        """将非桌面整轮回复交给平台 broker。

        :param context: 目标会话上下文。
        :param turn: 对话回合 ID。
        :param segments: 已按 ``<say>`` 边界切分的正文列表。
        :param emoji_items: 已按目标情绪命中的可发送表情包引用。
        :param quote_external_message_id: 第一条气泡要引用的平台消息编号；
            ``None`` 表示不引用。

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
        if not segments and not emoji_items:
            logger.warning('outbound_reply_empty', streamId=context.stream.id, turnId=turn)
            return
        # Broker 负责平台驱动选择和失败归一化；图片引用由 QQ 驱动透传给适配器。
        receipt = await self._broker.dispatch(OutboundMessage(
            stream=context.stream,
            segments=segments,
            emoji_refs=tuple(reference for _emotion, reference, _sub_type in emoji_items),
            emoji_sub_types=tuple(sub_type for _emotion, _reference, sub_type in emoji_items),
            batch_delays_ms=self._batch_delays_ms(segments, len(emoji_items)),
            quote_external_message_id=quote_external_message_id,
        ))
        trace.emit(
            'outbound_delivered',
            platform=receipt.platform,
            streamId=receipt.stream_id,
            turnId=turn,
        )

    def _batch_delays_ms(self, segments: list[str], emoji_count: int) -> tuple[int, ...]:
        """按人格配置算出每个发送批次发出前的停顿。

        节奏在主体侧算好随出站载荷下发，适配器只负责照做：打字速度是角色行为
        参数，不该散落到各平台适配器里各算一套。

        :param segments: 已按打字习惯切分的气泡文本。
        :param emoji_count: 排在文字之后的表情包张数。
        :return: 与「每条文字一批、每张表情包一批」逐项对齐的毫秒停顿；首项恒为
            0，因为模型生成本身已经占用了十几秒，她在对方视角里早就在打字了。
        """
        typing = self._cfg.typing
        text_delays = [
            0 if index == 0 else int(typing_delay_seconds(segment, typing) * 1000)
            for index, segment in enumerate(segments)
        ]
        # 表情包不逐字打，用固定的挑图时间；整轮只有表情包时同样不等第一条。
        emoji_delay = int(typing.emoji_pick_seconds * 1000) if typing.delay_enabled else 0
        emoji_delays = [emoji_delay] * emoji_count
        delays = text_delays + emoji_delays
        if delays:
            delays[0] = 0
        return tuple(delays)

    @staticmethod
    def _scene_key(stream_id: int) -> str:
        """返回场景画像在 meta 表里的键名。"""
        return f'scene:{stream_id}'

    def _scene_for_prompt(
        self,
        context: ConversationContext,
    ) -> tuple[str, str] | None:
        """读取当前 stream 的场景画像，供系统提示词渲染。

        只在群聊注入：私聊主动跟进会把刚生成的画像放进专用决策块，不复用群聊
        标题；桌面同样不注入。观察关闭时返回 None，整块省略。

        :param context: 当前会话上下文。
        :return: ``(话题, 气氛)``；没有画像、观察关闭或非群聊时返回 ``None``。
        """
        if (
            context.stream.kind != 'group'
            or self._scene_observer is None
            or self._scene_refresh_messages <= 0
        ):
            return None
        snapshot = SceneSnapshot.from_dict(
            self.memory.read_json(self._scene_key(context.stream.id), None)
        )
        if snapshot is None:
            return None
        return snapshot.topic, snapshot.atmosphere

    def _schedule_scene_observation(self, context: ConversationContext) -> None:
        """按新增消息条数决定要不要在后台重算场景画像。

        观察**不进对话的等待路径**：它是独立的后台任务，算完写进 meta 表，供之后
        若干轮直接读。这样她的首字延迟一点不受影响，代价只是画像会稍微旧一点。

        节流只靠「自上次观察以来新增了多少条」一个数：消息来得慢就自然算得少，
        来得快才算得勤。**不再叠一个最小时间间隔**——两个互相牵制的节流常量正是
        本项目吃过亏的形态。

        :param context: 当前会话上下文；非群聊或观察关闭时直接返回。
        副作用：可能创建一个后台任务；同一 stream 已有观察在跑时跳过。
        """
        if (
            context.stream.kind != 'group'
            or self._scene_observer is None
            or self._scene_refresh_messages <= 0
        ):
            return
        stream_id = context.stream.id
        if stream_id in self._observing:
            return
        snapshot = SceneSnapshot.from_dict(
            self.memory.read_json(self._scene_key(stream_id), None)
        )
        since = snapshot.observed_message_id if snapshot is not None else 0
        if self.memory.message_count_after(stream_id, since) < self._scene_refresh_messages:
            return
        self._observing.add(stream_id)
        self._track_background_task(
            asyncio.create_task(self._run_scene_observation(context))
        )

    async def _run_scene_observation(self, context: ConversationContext) -> str:
        """在后台读一段群聊历史并写入新的场景画像。

        :param context: 目标群聊上下文。
        :return: 便于后台任务追踪的说明字符串。
        副作用：一次模型调用与一次 meta 表写入；无论成败都释放并发标记。
            观察失败**只记日志、保留旧画像**——它是附加背景，绝不能影响对话。
        """
        stream_id = context.stream.id
        try:
            messages = self.memory.working_memory(stream_id, SCENE_WINDOW_MESSAGES)
            if not messages:
                return 'scene_observed_empty'
            lines = self._scene_observation_lines(context, messages)
            snapshot = await self._scene_observer.observe(
                lines, messages[-1].message_id,
            )
            self.memory.write_json(self._scene_key(stream_id), snapshot.to_dict())
            trace.emit(
                'scene_observed',
                # 必须显式置空 turnId，否则观察事件在控制台上完全消失：
                # - 现象：观察正常产出并写入画像，终端与 WebUI 日志面板一行都看不到。
                # - 原因：asyncio 任务继承创建时刻的 contextvar 快照，本任务因此带上了
                #   调度它的那个回合的 turnId；控制台出口对带 turnId 的事件一律跳过，
                #   理由是「已由轮末合成面板整体呈现」，而观察跑在面板渲染之后。
                # - 后果：观察不属于任何回合，置空后走独立信息框，两条支路都不会漏。
                turnId=None,
                streamId=stream_id,
                topic=snapshot.topic,
                atmosphere=snapshot.atmosphere,
            )
            return 'scene_observed'
        except Exception as exc:
            logger.warning('scene_observation_failed', streamId=stream_id, error=str(exc))
            return 'scene_observation_failed'
        finally:
            self._observing.discard(stream_id)

    def _scene_observation_lines(
        self,
        context: ConversationContext,
        messages: list[StoredMessage],
    ) -> list[str]:
        """把历史渲染为情景分析 Agent 所需的「说话人：内容」行。

        普通对话提示词的助手历史刻意不加名称，避免模型模仿；情景分析并不生成对话，
        必须显式标清双方，否则私聊里会分不出哪句是 Bot 说的、哪句是对方说的。
        """
        lines: list[str] = []
        for message in messages:
            if message.role == 'assistant':
                speaker = self._bot_display_name
            else:
                if message.sender_person_id is None:
                    raise RuntimeError('情景分析的用户消息缺少发送者人物 ID')
                speaker = self._registry.stream_display_name(
                    message.sender_person_id,
                    context.stream.id,
                )
            lines.append(f'{speaker}: {strip_say_tags(message.content)}')
        return lines

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
    typing: TypingConfig,
) -> list[str] | None:
    """按解析器识别的 ``say`` 边界收集外部平台正文。

    :param event: 当前解析事件。
    :param segments: 已完成的分句列表，会被原地追加。
    :param current: 当前尚未结束的分句片段列表。
    :param typing: 打字节奏配置，决定一条台词切成几条气泡。

    :return: 更新后的当前分句片段；收到 ``SayEndEvent`` 后返回 ``None``。

    副作用：
        可能向 ``segments`` 原地追加一条或多条非空分句，不重新扫描完整响应文本。
        一个 ``<say>`` 按打字习惯再切成气泡，因此追加条数可能多于 ``<say>`` 数。
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
            # 在这里切分而不是在投递侧：平台出站、助手历史和控制台渲染共用这份
            # segments，切分前置才能保证三者看到的气泡完全一致。
            segments.extend(split_into_bubbles(''.join(current), typing))
        return None
    return current


def _emoji_history_markup(items: list[tuple[str, str, int]]) -> str:
    """把实际命中的目标情绪序列化为可统计的助手历史标签。"""

    return ''.join(
        f'<emoji emotion="{escape(emotion, quote=True)}"/>'
        for emotion, _reference, _sub_type in items
    )


def _spoken_recap(segments: list[str], emoji_items: list[tuple[str, str, int]]) -> str:
    """把一轮回复的可见产物写成她自己的第一人称陈述，供续跑轮引用。

    :param segments: 本轮实际投递的分句正文。
    :param emoji_items: 本轮实际命中的表情包条目。
    :return: 形如「你刚说了：「……」」的陈述；只发了表情包时如实只说表情包。
    """

    spoken = ' '.join(segment.strip() for segment in segments if segment.strip())
    if spoken and emoji_items:
        return f'你刚说了：「{spoken}」，还发了个表情包'
    if spoken:
        return f'你刚说了：「{spoken}」'
    return '你刚发了个表情包'


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
