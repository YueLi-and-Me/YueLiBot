"""
对话编排服务。

持有所有后端资源：按角色拆分的 LLM provider、MemoryStore、Persona、DayPlanService。
通过 WebSocket push 推事件给 Electron 主进程。
"""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from datetime import datetime
from html import escape
from pathlib import Path
from typing import (
    Any,
    Callable,
    Deque,
    Dict,
    Iterable,
    Mapping,
)

import asyncio
import inspect
import json
import random
import re
import sqlite3

from ..media.chat_image import (
    ChatImageDescriber,
    merge_emoji_descriptions,
    merge_image_descriptions,
)
from ..media.emoji import EmojiBannedError, EmojiContentRejectedError, EmojiLibrary
from ..console.trace_console import mark_turn_start, render_action_decision, render_turn, render_turn_error
from ..maintenance.vector import VectorService

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
    CognitiveScope,
    InspectAction,
    ConsultAction,
    RecallAction,
)
from src.core.agent.conversation import AgentOutcome, ConversationAgent
from src.core.agent.observer import SceneObserver, SceneSnapshot
from src.core.agent.conversation_gate import ONGOING_TOPIC_MESSAGE_SPAN
from src.core.agent.expression import render_expression_habits
from src.core.agent.impression import ConversationImpressions
from src.core.agent.jargon import InjectedTerms, lookup_jargon
from src.core.agent.profile import refresh_profiles
from src.core.agent.expression_select import ExpressionSelector
from src.core.agent.history import (
    close_dangling_say,
    strip_say_tags,
)
from src.core.agent.parser import (
    EmojiEvent,
    ParseEvent,
    ResponseParser,
    SayEndEvent,
    SayEvent,
    TextEvent,
)
from src.core.agent.prompt import (
    build_proactive_prompt,
    build_system_prompt,
    describe_resumption,
    render_action_protocol,
    render_replyer_protocol,
    render_tool_protocol,
)
from src.core.agent.segmentation import typing_delay_seconds
from src.core.agent.summarize import summarize
from src.core.awareness.sleep import SleepState
from src.core.runtime.clock import now as current_time
from src.core.logging.logger import get_logger
from src.core.config.schema import Config, TypingConfig
from src.core.llm_models.openai import LlmError
from src.core.llm_models.protocol import LlmProvider
from src.core.llm_models.snapshot import bind_render_params, dump as dump_llm_request
from src.core.memory.tuning import tuned_value
from src.core.memory.store import (
    MemoryStore,
    StoredMessage,
    format_assistant_poke_action,
    format_assistant_reaction_action,
)
from src.core.observe import events as trace
from src.core.observe.events import bind_origin, enter_stage
from src.core.observe.source import source_label
from src.core.observe.stages import (
    CONTEXT,
    DISPATCHING,
    FAILED,
    GATED,
    GENERATING,
    REPLIED,
    Stage,
)
from src.core.observe.store import max_turn_id
from src.core.persona.state import (
    Persona,
    describe_acquaintance,
    describe_persona,
    status_label,
)
from src.core.platform_io.broker import PlatformBroker
from src.core.platform_io.registry import StreamRegistry
from src.core.services.maintenance.memory_feedback import register_prompt_entries
from src.core.platform_io.types import (
    ConversationContext,
    InboundMessage,
    OutboundPoke,
    OutboundReaction,
    StreamKind,
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
from src.core.schedule.plan import DayPlanService, ScheduleSleepState, asks_about_activity
from src.core.tooling.cognitive import CognitiveToolExecutor
from src.core.tooling.registry import build_builtin_action_registry
from src.core.tooling.spec import ToolContext
from src.plugin_system import PluginContext, PluginRegistry

logger = get_logger(__name__)

from .constants import (
    CHAT_POLL_INTERVAL_S,
    DIRECT_WAIT_TIMEOUT_S,
    MAX_TURN_ROUNDS,
    PLUGIN_ROOTS,
    PROACTIVE_TRIGGER_MESSAGE,
    SCENE_WINDOW_MESSAGES,
    _BACKGROUND_BATCH_RETRY_LIMIT,
    _HINTS,
)


from .agent_protocol import AgentProtocolMixin
from .background import BackgroundTaskMixin
from .capabilities import PlatformCapabilityMixin
from .context_build import ContextBuildMixin
from .follow_up import DirectFollowUpMixin
from .gating import BatchGateMixin
from .group_observe import GroupObservationMixin
from .outbound import OutboundDispatchMixin, _send_ref_content_hash
from .profiles import PersonProfileMixin
from .scene import SceneObservationMixin
from .helpers import (
    _collect_outbound_segment,
    _emoji_history_markup,
    _extract_lines,
    _facts_for_prompt,
    _plan_to_dict,
)
from .state import (
    _BatchFailureTracker,
    _BatchGate,
    _BufferedMessage,
    _DirectFollowUpState,
    _InflightTurn,
    _PreparedTurnContext,
    _RoundResult,
    _SessionState,
    _TurnSink,
    _WaitHold,
)


class ChatService(
    AgentProtocolMixin,
    BackgroundTaskMixin,
    BatchGateMixin,
    ContextBuildMixin,
    DirectFollowUpMixin,
    GroupObservationMixin,
    OutboundDispatchMixin,
    PersonProfileMixin,
    PlatformCapabilityMixin,
    SceneObservationMixin,
):
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
        data_dir: Path = Path('data'),
        speak_audio: Callable[[str, int], Any] | None = None,
        vector: VectorService | None = None,
        broker: PlatformBroker | None = None,
        expression_provider: LlmProvider | None = None,
        planner_provider: LlmProvider | None = None,
        replyer_provider: LlmProvider | None = None,
        scene_provider: LlmProvider | None = None,
        memory_provider: LlmProvider | None = None,
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
        # 黑话召回的服务级状态：Bot 自己的名字与别名（含对用户的称呼）永不作为
        # 黑话注入；已注入集合做跨轮去重，进程内、重启即消失。
        self._jargon_protected_names: tuple[str, ...] = (
            cfg.bot.name, *cfg.bot.aliases, cfg.bot.user_nickname,
        )
        self._jargon_injected = InjectedTerms()
        self._summary_personality = cfg.personality.personality
        conversation = cfg.conversation
        generation = cfg.generation
        self._working_memory_messages = conversation.working_memory_messages
        self._summarize_trigger_messages = conversation.summarize_trigger_messages
        self._summarize_batch_messages = conversation.summarize_batch_messages
        # 事实抽取与摘要同形态但各走各的游标：摘要用 episode_id 表达「已消费」，
        # 抽取用 meta 里的独立游标，共用同一判据会相互消费对方的输入且不报错。
        self._memory_provider = memory_provider
        if memory_provider is None:
            # 没有 memory 路由时抽取整条功能是关的。这句必须在启动时说出来：
            # _maybe_extract_facts 的三处前置判断都是静默 return，没有这条日志，
            # 「已接线但永远不产出」在外部看来与「正常但这段对话没什么可记的」完全一样。
            logger.warning('fact_extract_disabled', reason='memory 模型路由不可用')
        self._fact_extract_trigger = conversation.fact_extract_trigger_messages
        self._fact_extract_batch = conversation.fact_extract_batch_messages
        self._extracting: set[int] = set()
        # 抽取与学习各自的连续失败计数，用于给确定性失败一个出口；两个任务游标
        # 独立，计数也必须独立，否则一边的失败会误清另一边的进度。
        self._extract_failures = _BatchFailureTracker(_BACKGROUND_BATCH_RETRY_LIMIT)
        # 表达学习的在飞守卫，与抽取同形态但互不相干：两个任务各走各的游标，
        # 同一会话同一时刻各只允许一个在飞。
        self._learning_expressions: set[int] = set()
        self._expression_failures = _BatchFailureTracker(_BACKGROUND_BATCH_RETRY_LIMIT)
        # 画像刷新不按会话分派，全局一把闸：它读的是本地事实，与当前是哪条会话无关。
        self._refreshing_profiles = False
        self._session_gap_ms = conversation.session_gap_minutes * 60_000
        self._fact_recall_limit = conversation.fact_recall_limit
        self._private_facts_in_group = conversation.private_facts_in_group
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
        # stream_id -> 进入等待时的缓冲长度。Bot 选择「先等等」之后，本批消息被放回
        # 缓冲；在缓冲长度没有变化（也就是没有任何新消息进来）之前不再重开回合，
        # 否则轮询周期一到就会把同一批重新问一遍模型。
        self._waiting: dict[int, _WaitHold] = {}
        self._speak_enabled = cfg.group_chat.self_started_topics
        # 扩展触发模式下的待处理候选累计；一旦产生 DELIBERATE 即清零。
        self._extended_pending: dict[int, int] = {}
        # 已经放弃自然跟进的群 stream。Bot 在群聊里选择 silent 即加入，成功回复即移出；
        # 门控据此关闭自然回应窗口，让「说够了没有」由 Bot 自己的动作决定而不是回复计数。
        self._follow_up_declined: set[int] = set()
        # 同一 stream 最近 60 秒的 poke 到达时间。只登记协议明确标记的 poked_me，
        # 普通消息与适配器合成正文都不能影响该计数；重启后清空符合短窗口语义。
        self._poke_arrivals: Dict[int, Deque[int]] = {}
        # 决策与表达是否分成两次模型调用。三个条件缺一不可：配置打开、两级各自
        # 的 provider 都在。配置打开但 provider 缺位时保持单次调用，而不是让回合
        # 在运行期才失败——那会表现为 Bot 突然不说话，现场极难定位。
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
        # 平台标识 -> 该平台协议端实测可用的能力集合，由适配器每次连接成功后上报。
        # 进程内状态而非落库：能力属于「当前这条连接指向的协议端」，重启后必须
        # 重新探测，持久化会让上一次的结论在协议端已经变化后继续生效。
        self._platform_capabilities: Dict[str, frozenset[str]] = {}
        # 工具注册表按进程装配一次：动作声明按回合帧动态生成，外部工具也按
        # 当前会话能力与剩余认知预算过滤后再下发。
        self._tool_registry = build_builtin_action_registry()
        # 插件在构造期发现并登记工具与命令，与 on_load 的先后是刻意的：登记必须在
        # ConversationAgent 拿到注册表之前完成，而 on_load 可能要做 I/O，只能等到
        # startup。因此 tools() 不得依赖 on_load 建立的状态，该约束写在契约里。
        # 宿主入口工厂必须在 discover 之前就位：注册表在发现阶段（bind_config 之后、
        # on_load 之前）注入它。漏传不会报错，只会让插件在真正用到 ctx 的那一刻才炸
        # ——真机上就是这么暴露的：链接插件取 ctx.host.https_proxy 时抛
        # 「宿主入口尚未注入」，而三条线的用例各自注入桩件，全绿。
        self._plugins = PluginRegistry(
            context_factory=lambda plugin_id, plugin_dir: PluginContext(
                plugin_id, plugin_dir, data_dir, cfg,
            ),
        )
        self._plugins.discover(PLUGIN_ROOTS)
        for plugin in self._plugins.tool_plugins():
            for spec, executor in plugin.tools():
                self._tool_registry.register_tool(spec, executor)
        # 命令与工具同批注册：重名当场抛错终止启动，而不是静默丢一条命令。
        self._plugins.register_commands()
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
                tool_registry=self._tool_registry,
            )
            if decision_provider is not None and conversation_agent_cfg.mode != 'off'
            else None
        )
        self._proactive_temperature = generation.proactive.temperature
        self._proactive_max_tokens = generation.proactive.token_limit
        self._summary_temperature = generation.summary.temperature
        self._summary_max_tokens = generation.summary.token_limit
        self._memory_temperature = generation.memory.temperature
        self._memory_max_tokens = generation.memory.token_limit
        self._bot_names: tuple[str, ...] = (cfg.bot.name, *cfg.bot.aliases)
        self._at_mention_must_reply = cfg.group_chat.at_mention_must_reply
        self._name_mention_probability = cfg.group_chat.name_mention_probability
        self._group_persona_weight = cfg.group_chat.persona_weight
        self._perception_surfaces = frozenset(cfg.perception.surfaces)
        # 表达方式的唯一来源是 expressions 表，候选池按会话按轮变化，因此选择器
        # 只持有模型调用参数：有 provider 就构造，候选空与否在每轮挑选时判断。
        self._expression_selector = (
            ExpressionSelector(
                expression_provider,
                temperature=generation.expression.temperature,
                max_tokens=generation.expression.token_limit,
            )
            if expression_provider is not None
            else None
        )
        self.memory = MemoryStore(db)
        # 会话印象是事实检索的第二检索词；状态只在进程内，复用 memory 模型槽。
        self._impressions = ConversationImpressions(
            self.memory, memory_provider, conversation.working_memory_messages,
        )
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
        # 认知动作只在 ReAct 开启时绑定执行器：轮次预算为 0 时执行器永远不会被
        # 调用，绑定它只会让「关闭即回退到单轮」这条性质多一处需要复核的地方。
        # consult 已在 COGNITIVE_ACTIONS 里，动作空间会把它发给模型；执行器缺
        # 这一条就会在 Bot 真的选中时撞 KeyError，装配必须同步。
        if self._cognitive_rounds > 0:
            self._tool_registry.bind_action_executor(
                'recall',
                CognitiveToolExecutor(
                    RecallAction(
                        # 跨人物召回会带回从未在本平台出现的人物，显示名解析
                        # 需要「本平台查不到就退回任一平台身份」的宽容变体，
                        # 否则桌面端问到只有 QQ 身份的人会让整次检索抛错。
                        self.memory, self._registry.stream_display_name_or_any, db,
                        private_in_group=self._private_facts_in_group,
                    )
                ),
            )
            self._tool_registry.bind_action_executor(
                'inspect',
                CognitiveToolExecutor(
                    InspectAction(self.memory, self._registry.stream_display_name)
                ),
            )
            self._tool_registry.bind_action_executor(
                'consult',
                CognitiveToolExecutor(
                    ConsultAction(db, embed_query=self._vector.embed_query)
                ),
            )
        self._desktop_context = self._registry.desktop_context()
        self.persona = Persona(db)
        self.persona.snapshot_daily(self._desktop_context.person.id)
        # 从账本里已出现过的最大回合 ID 接着发号，而不是每次启动从 0 重来。
        # - 现象：turn_id 与上次运行的回合撞号，WebUI 按 turnId 聚合时把不同启动的
        #   对话并成一张卡。真机实测 turn_id=33 同时装着 4 次启动的 4 条 user_input、
        #   横跨 31 小时；摘要卡因此会把某次提问与几小时后另一次的回复配成一对。
        # - 原因：回合 ID 由进程内计数器分配（见 _next_turn），而事件账本跨重启持久化。
        # - 后果：改回从 0 起算会让这种错误配对重新出现，重启越频繁越严重。
        self._turn_id = max_turn_id()
        self._inflight: dict[int, _InflightTurn] = {}
        self._buffers: dict[int, list[_BufferedMessage]] = {}
        self._stream_claims: dict[int, str] = {}
        self._poll_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._sessions: dict[int, _SessionState] = {}
        self._summarizing: set[int] = set()
        self._summary_failures = _BatchFailureTracker(_BACKGROUND_BATCH_RETRY_LIMIT)
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
        self._wake_sleep: Callable[[int], SleepState] | None = None
        self._promise_handler: Callable[[int, str], None] | None = None
        self._schedule: DayPlanService | None = None

    def apply_config(self, cfg: Config) -> None:
        """把本对象持有的配置引用切到重载后的新对象上。

        配置热重载不重建服务，只把各持有方的引用换掉；引用一换，所有读取点
        下次读到的就是新值。图片描述器由本服务持有，这里一并换掉，
        调用方不必知道它的存在。

        :param cfg: 重载后的运行时配置。
        :return: ``None``。
        副作用：重绑自身与图片描述器的配置引用；不重建任何对象。
        """

        self._cfg = cfg
        if self._image_describer is not None:
            self._image_describer.apply_config(cfg)

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

    def set_sleep_wake_handler(self, fn: Callable[[int], SleepState]) -> None:
        """绑定由确定性入站事件触发的睡眠打断回调。"""

        self._wake_sleep = fn

    def wake_from_inbound(self, now: int) -> SleepState | None:
        """让私聊或协议 @ 在门控放行后立即结束当前 sleep 活动。"""

        return self._wake_sleep(now) if self._wake_sleep is not None else None

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

        :return: 当前 ``asleep``、``resting`` 和 ``just_woke`` 标志；未绑定状态回调时
            返回三个标志均为 ``False`` 的默认值。
        """

        s = self._sleep_state() if self._sleep_state else None
        if s is None:
            return ScheduleSleepState(asleep=False)
        return ScheduleSleepState(
            asleep=s.asleep,
            just_woke=s.just_woke,
            resting=s.resting,
        )

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
        earlier_resting: bool = False,
    ) -> None:
        """结算指定人物自上次状态更新时间以来的作息影响。

        :param context: 已完成会话和人物归属解析的上下文。
        :param now: 可选的当前毫秒时间戳；省略时读取统一时钟。
        :param earlier_resting: 区间起点之前已休息时是否计入被截断的休息时间。

        副作用：
            owner 上会更新人格状态并保存每日快照；非 owner 只读取状态，不写入
            关系信号。
        """

        now = now or current_time()
        person_id = context.person.id
        # 结算区间的两端都不能想当然：
        # - 起点必须是全局结算游标，不能用 persona.get() 的 updated_at——后者取自
        #   persona_bond，每个对话回合都会把它推到当前时刻，两次对话之间的休息
        #   与睡眠会被整段丢弃（见 Persona.settled_at）。
        # - 终点必须是活动时间线已决策到的时刻，不能直接用 now——越过它的那段空缺
        #   由后台任务事后补写，此刻结算等于把它按离线前的活动算掉，之后补进来的
        #   真实活动（整夜睡眠是最大一笔）再也不会被读到（见 decided_until）。
        settled = now
        if self._schedule:
            settled = min(now, self._schedule.decided_until(now))
            effect = self._schedule.integrate_between(
                self.persona.settled_at(),
                settled,
                earlier_resting,
            )
        else:
            effect = None
        if context.relationship_signals_enabled:
            self.persona.apply_elapsed(person_id, settled, effect)
            self.persona.snapshot_daily(person_id, now)

    async def startup(self) -> None:
        """启动由入站消息唤醒、固定心跳兜底的聊天缓冲循环，并加载插件。

        插件的工具已在构造期登记；这里只跑它们的 ``on_load``——那一步允许读配置、
        建运行期对象，必须在事件循环里执行。
        """
        await self._plugins.load_all()
        self._stop.clear()
        self._wake.clear()
        self._poll_task = asyncio.create_task(self._poll_loop(), name='chat-poll')

    async def shutdown(self) -> None:
        """停止聊天缓冲轮询、终止仍在执行的回复，并卸载插件。"""
        await self._plugins.unload_all()
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
        # interrupt 只置取消事件；provider 在 SSE 行边界检查取消信号，被中断的
        # 回合正常远小于 1 秒就能收尾。先快照在飞任务再统一 interrupt，然后有界
        # 等待它们收尾，3 秒只是防止事件循环拆除时任务被直接销毁的兜底。
        inflight_tasks = [
            inflight.task
            for inflight in tuple(self._inflight.values())
            if not inflight.task.done()
        ]
        for stream_id in tuple(self._inflight):
            self.interrupt(stream_id)
        if inflight_tasks:
            _, pending = await asyncio.wait(inflight_tasks, timeout=3)
            for pending_task in pending:
                pending_task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

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
            # 等待中的 stream 只被新消息唤醒：无下文时保持等待是设计行为而非
            # 停滞，任何一条新消息都会解除等待并要求 Bot 表态。
            # 私聊例外：对面是一个在等回应的人，等到超时就必须对这半句话表态，
            # 否则 wait 会把私聊变成永久已读不回。
            waiting_at = self._waiting.get(stream_id)
            if waiting_at is not None and len(buffered) <= waiting_at.watermark:
                direct_hold = buffered[0].context.stream.kind == 'direct'
                within_window = (
                    now - waiting_at.since < DIRECT_WAIT_TIMEOUT_S * 1_000
                )
                if not direct_hold or within_window:
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
            插件改写器先于落库执行，落库与回合缓冲用的都是改写后的正文；
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
        # 改写必须先于落库：改后的正文要进历史、记忆与摘要。改写失败或改写为空时
        # 分发侧保留上一步正文，这里拿到的总是非空结果。
        text = await self._plugins.rewrite_inbound(inbound, trimmed)
        message_id = self.memory.append_message(
            stream_id,
            inbound.context.person.id,
            'user',
            text,
            accepted_at,
            inbound.external_message_id,
        )
        # 观察必须后于落库：它要拿与落库行一致的 message_id。
        self._plugins.observe_inbound(stream_id, message_id, inbound)
        image_task: asyncio.Task[str] | None = None
        if inbound.image_sources or inbound.emoji_sources:
            # 先以稳定占位符确认接收并返回；描述成功后后台回写同一行正文。
            # 回合启动时再等待该任务，避免图片下载/VLM 拖住 HTTP 入站响应。
            image_task = asyncio.create_task(self._describe_image_message(
                stream_id,
                message_id,
                text,
                inbound.image_sources,
                inbound.emoji_sources,
                inbound.emoji_sub_types,
            ))
            self._track_background_task(image_task)
        self._buffers.setdefault(stream_id, []).append(_BufferedMessage(
            text=text,
            context=inbound.context,
            mentioned_me=inbound.mentioned_me,
            external_message_id=inbound.external_message_id,
            bot_name=inbound.bot_name,
            message_id=message_id,
            previous_message_at=previous_message_at,
            accepted_at=accepted_at,
            image_description_task=image_task,
            poked_me=inbound.poked_me,
            pokes_in_window=inbound.pokes_in_window,
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
        # collect_enabled 关闭时入站图片只识别不入库：识别结果仍回写正文，
        # 但不再把新图收进可发送库。
        if self._emoji_library is not None and self._cfg.emoji.collect_enabled:
            for description, sub_type in zip(
                emoji_descriptions,
                emoji_sub_types,
                strict=True,
            ):
                if description is None:
                    continue
                try:
                    await self._emoji_library.register(
                        description.image_bytes,
                        description.emotion_tags,
                        description.media_type,
                        description.content_hash,
                        sub_type,
                    )
                except (EmojiBannedError, EmojiContentRejectedError) as exc:
                    # 封禁命中、超限或内容审查拒绝只影响这一张图，不应让整条
                    # 消息的图片描述任务失败；事件与告警已在 register 内落账。
                    logger.warning(
                        'emoji_inbound_register_rejected',
                        hash=description.content_hash,
                        error=str(exc),
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
        # 戳一戳的正文由适配器合成（形如「[揉了揉月璃]」），其中的 Bot 名字不是任何人
        # 说出的点名信号。名字匹配必须排除这些行，否则入口门控刚排除掉的合成点名会在
        # 批次门控原样复活，审计事件报告的门控态也会与入口对不上。
        name_match_text = '\n'.join(
            message.text for message in batch if not message.poked_me
        )
        inbound = InboundMessage(
            text=trimmed,
            context=context,
            mentioned_me=any(message.mentioned_me for message in batch),
            bot_name=next(
                (message.bot_name for message in reversed(batch) if message.bot_name is not None),
                None,
            ),
            poked_me=any(message.poked_me for message in batch),
            # 入口对每一次到达各记一次；批次里取最大值即「这一批最靠后的那次戳在
            # 窗口里排第几」，与入口对同一条消息的判定完全一致。
            pokes_in_window=max(
                (message.pokes_in_window for message in batch), default=0,
            ),
        )

        turn = self._next_turn()
        self._active_turns[stream_id] = turn
        mark_turn_start(turn)
        sender = self._sender_metadata(context)
        source = source_label(context.stream, direct_name=sender['senderNickname'])
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
            stream_kind=context.stream.kind,
            stream_external_id=context.stream.external_id,
            source_label=source,
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
            prepared_context: _PreparedTurnContext | None = None
            # 默认使用占位符正文；图片任务完成前的异常处理仍需可读的批次文本。
            trimmed = '\n'.join(message.text for message in batch)
            try:
                now = current_time()
                earlier_resting = self.current_sleep().asleep
                self.settle_elapsed(context, now, earlier_resting)
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
                impression = await self._conversation_impression(context, now)
                prepared_context = self._prepare_turn_context(
                    context,
                    trimmed,
                    now,
                    inbound.bot_name,
                    user_message_id_watermark=materialized_batch[-1].message_id,
                    batch_message_ids=tuple(
                        message.message_id for message in materialized_batch
                    ),
                    impression=impression,
                    turn_id=turn,
                )
                render_params: dict[str, dict[str, str]] = {}
                batch_gate = self._batch_gate(
                    context,
                    trimmed,
                    inbound.mentioned_me,
                    candidate_count=len(materialized_batch),
                    poked_me=inbound.poked_me,
                    pokes_in_window=inbound.pokes_in_window,
                    name_match_text=name_match_text,
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
                    source_label=source,
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
                # Bot 刚开过口，对话仍在 Bot 这边：与 Agent 路径同口径重新敞开自然回应窗口。
                self._follow_up_declined.discard(context.stream.id)
                asyncio.create_task(self._maybe_summarize(context.stream.id))
                asyncio.create_task(
                    self._maybe_extract_facts(context.stream.id, context.stream.kind)
                )
                asyncio.create_task(self._maybe_learn_expressions(context.stream.id))
                asyncio.create_task(self._maybe_refresh_profiles())
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
                    source_label=source,
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
                    source_label=source,
                    model_name=getattr(self._chat_provider, 'model', ''),
                )
                self._mark_stage(context, FAILED, str(exc), turn_id=turn)
                await self._emit(
                    stream_id,
                    'chat.error',
                    {'turnId': turn, 'kind': 'error', 'message': str(exc)},
                )
            finally:
                # 召回发生但本轮在门控或动作规划阶段结束时，同样留一条空提示词
                # 选择记录；这样每个实际执行过事实检索的回合都能一一重放。
                if prepared_context is not None:
                    prepared_context.retrieval_trace.emit_once([])

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
        """尝试为一个驱动源占用 stream，只在跨源抢占失败时记录竞争。

        :param stream_id: 待占用的会话流主键。
        :param source: 驱动源标识，当前为 ``reply`` 或 ``proactive``。
        :return: 占用成功为 ``True``；已被占用为 ``False``。
        副作用：占用成功时写入占用表；跨源抢占失败时发出 ``turn_competition`` 事件。
        """
        active_source = self._stream_claims.get(stream_id)
        if active_source is None:
            self._stream_claims[stream_id] = source
            return True
        # 同源抢占失败不是竞争，是系统循环按设计在等：回合在飞时缓冲区仍有消息，
        # _tick 每 CHAT_POLL_INTERVAL_S（0.1 秒）就会再试一次。
        # - 现象：改动前这里无条件发事件，真机 30 小时产出 14153 条 turn_competition，
        #   占全部控制台输出的 80%，且 activeSource 与 blockedSource 无一例外相同。
        # - 原因：轮询循环的每一次空转都被当成了一次值得上报的竞争。
        # - 后果：真正有价值的跨源竞争（reply 与 proactive 抢同一个 stream）被淹没在
        #   同源噪声里，30 小时内一条都没能被看见。
        if active_source == source:
            return False
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

        协议端在对方打字期间反复推送该通知，多数情况下不应响应：正常对话中对输入
        状态做出反应属于监控行为。唯一需要交给行动核心评估的情形：Bot 发言后
        对方长时间未回复，此刻才出现输入状态。满足时间条件仅创建一次
        ``reply / silent`` 决策机会，不保证发送。

        静默持续时长与此前已追问次数会先与历史一起交给独立的情景分析 Agent；
        Conversation Agent 再结合该画像选择动作。它选择 ``silent`` 时不产生消息，
        也不消耗本段静默的追问次数。

        :param context: 已完成归属解析的会话上下文。
        :return: 本次是否真的发了话。
        副作用：命中条件时调用 Conversation Agent；仅在其选择 ``reply`` 后投递。
        """
        stream_id = context.stream.id
        # 群聊不推送输入状态；即使将来推送，持续关注某个成员的输入状态也不合适。
        if context.stream.kind != 'direct':
            return False
        nudge = self._cfg.typing.nudge
        if not nudge.enabled or nudge.max_per_silence <= 0:
            return False
        if self._agent_scope(context, 'deliberate') != 'live':
            return False
        if self.current_sleep().asleep:
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
        if self.current_sleep().asleep:
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
            impression=await self._conversation_impression(context, now),
            turn_id=turn,
        )
        render_params: dict[str, dict[str, str]] = {}
        follow_up_context = '\n'.join((
            '# 当前主动跟进机会',
            situation,
            '这不是必须发言的通知。请结合上面的完整对话判断：只有话题明显未完、'
            '对方仍需要回应或支持、先前约定需要续上，或者此刻追问在你们的关系里'
            '确实自然时才选 reply；话题已经自然结束、只是礼貌收尾、没有新价值、'
            '继续追问会打扰时选 silent。不要因为时间到了就勉强找一句话说。',
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
        feedback_cfg = self._cfg.memory_feedback
        proactive_facts = _facts_for_prompt(
            self.memory,
            context.person.id,
            self.memory.top_facts(
                context.person.id, 5, now,
                stream_kind=context.stream.kind,
                private_in_group=self._private_facts_in_group,
            ),
            hard_filter_marked=feedback_cfg.enabled and feedback_cfg.hard_filter_enabled,
        )
        # 反馈纠错锚点：主动消息注入的事实同样进入观察；链路默认关闭，关闭时零写入。
        if feedback_cfg.enabled and proactive_facts:
            register_prompt_entries(
                self._db,
                [(item.fact_id, context.person.id)
                 for item in proactive_facts if item.fact_id],
                context.stream.id,
                now,
            )
        base_prompt = build_system_prompt(
            now=datetime.fromtimestamp(now / 1000),
            persona=persona_desc,
            acquaintance=acquaintance,
            facts=proactive_facts,
            episodes=[episode.summary for episode in self.memory.recent_episodes(
                context.stream.id, 2,
                exclude_pending_rebuild=(
                    feedback_cfg.enabled and feedback_cfg.episode_query_block_enabled
                ),
            )],
            schedule=schedule_desc,
            # 主动开口同样从 expressions 表挑贴合当前情境的说法；没有独立候选源。
            expression_habits=render_expression_habits(
                await self._pick_expression_habits(context, situation, [], None)
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

        :return: 包含主体精力、统一状态标签、日程、待处理消息数和会话参与人的
            可序列化字典；不展开单个人物的关系和事实。

        :raises ValueError: stream 不存在时由注册表抛出。
        """
        now = now or current_time()
        stream = self._registry.stream(stream_id)
        participants = [
            self._conversation_participant(person, stream)
            for person in self._registry.list_persons(stream.id)
        ]
        state = self.persona.inspect(self._desktop_context.person.id)
        sleep = self.current_sleep()
        return {
            'now': now,
            'selfState': {
                'energy': state.energy,
                'mood': state.mood,
                'statusLabel': status_label(
                    state,
                    asleep=sleep.asleep,
                    just_woke=sleep.just_woke,
                    resting=sleep.resting,
                ),
            },
            'schedule': _plan_to_dict(self._schedule.get(now)) if self._schedule else None,
            'conversation': {
                'workingMessages': self.memory.pending_count(stream.id),
                'participants': participants,
            },
        }

    def _next_turn(self) -> int:
        """分配单调递增的回合 ID，跨重启不与历史回合撞号。

        计数器本身仍在进程内，但起点由构造时的 :func:`max_turn_id` 从事件账本播种，
        因此新回合的编号一定大于账本里现存的任何一个。观察面板按 turnId 聚合，
        编号不重复是该聚合成立的前提。

        :return: 新分配的正整数回合 ID。

        副作用：
            更新进程内回合计数器；不会写入数据库。
        """

        self._turn_id += 1
        return self._turn_id

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
            仅观察决策口径，不产生额外模型往返。
        :param allow_wait: 本批是否还能等待；已经等过一次的批次传 False，
            此时 wait 不在动作集里，Bot 必须表态。

        speak（主动发起一个不回应任何人的话题）按配置开关进入动作集：它不需要独立触发，
        只是在已有候选里多一个选项，因此与 reply 共用同一条频率闸门。
        """
        react_enabled = self._react_available(context)
        capabilities = PlatformCapabilities(
            emoji=self._emoji_available(context),
            react=react_enabled,
            available_reactions=REACTION_IDS if react_enabled else (),
            poke=self._poke_available(context),
            plugin_capabilities=self._plugins.stream_capabilities(context.stream.id),
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

    def _cognitive_scope(
        self,
        frame: DecisionFrame,
        stream_id: int,
        stream_kind: StreamKind,
        person_kind: str,
    ) -> CognitiveScope:
        """按本回合水位冻结认知检索的会话与人物范围。

        范围在回合开始时定死：水位之后新到的发言者不进入检索范围，使 Bot 这一回合
        「能想起谁的事」不随批次外消息漂移。

        主动检索的人物范围只在「非群聊会话 + 当前对话者是 owner」时放开到跨在场者：
        群聊里放开会让 A 群能问出 B 群的事（两边都是 group 来源，可见性规则不拦，
        必须由这个门拦）；非 owner 的私聊里放开等于让任何人查任何人。被动注入
        不经过这里，仍按在场者取。

        :param frame: 本回合固定快照。
        :param stream_id: 当前会话 ID。
        :param stream_kind: 当前会话类型；群聊永不放开人物范围。
        :param person_kind: 当前对话者的人物类型；只有 ``owner`` 放开。
        :return: 供本回合全部认知动作共用的检索范围。
        """
        return CognitiveScope(
            stream_id=stream_id,
            person_ids=tuple(
                self.memory.recent_speakers(stream_id, frame.message_watermark)
            ),
            cross_person=stream_kind != 'group' and person_kind == 'owner',
        )

    def _present_person_ids(self, context: ConversationContext) -> list[int]:
        """给出本轮「在场者」的人物主键，供画像注入取数。

        口径与认知检索的 ``CognitiveScope`` 保持一致——最近开口过的人，加上当前
        这一位。两处若各定各的「在场」，同一轮里 Bot 检索得到的人和 Bot 有印象的人会
        对不上，而这种错位在输出上完全看不出来。

        :param context: 当前会话上下文。
        :return: 去重后的人物主键，当前说话人排在最前。
        :raises sqlite3.Error: 读取最近发言者失败。
        副作用：只读。
        """
        ids = [context.person.id]
        watermark = self.memory.latest_message_id(context.stream.id)
        for person_id in self.memory.recent_speakers(context.stream.id, watermark):
            if person_id not in ids:
                ids.append(person_id)
        return ids

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
                record_retrieval_trace=False,
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
                record_retrieval_trace=False,
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
        """执行一个对话回合，正常路径只运行第一轮。

        多轮回合已经停用，一个回合至多产出一次可见产物。原因是模型生成期间到达的
        新消息必须另开回合取得新快照；没有新消息时再问一次只会得到确定的收束
        答复，不值得一次模型往返。第一轮完成后因此无条件收束。

        ``MAX_TURN_ROUNDS`` 仅保留为防御上限，防止以后调整控制流时意外失控；当前
        正常路径永远不会进入第二轮，也不会撞到上限。

        第一轮的结果决定本回合副作用：
        - ``declined``（silent）：Bot 结束本轮，退出循环；
        - ``paused``（wait）：批次已退回缓冲等下文，退出循环；
        - ``failed``：模型或协议失败，退出循环；
        - ``acted``：已经产出可见产物，随后无条件收束。

        :param batch: 本回合的原始消息批次。
        :param allow_wait: 本回合是否还能等待；同一批只允许等一次。
        副作用：至多一次模型往返与一次可见产物投递；回合结束后结算后台副作用。
        """
        # 回合级副作用只在整个循环收束后结算一次。人格结算、摘要触发、场景观察
        # 都按整个回合记账，逐轮各记一遍会使多轮回合重复推进人格。
        acted = False
        exhausted = True
        for round_index in range(MAX_TURN_ROUNDS):
            if cancel_event.is_set():
                exhausted = False
                break
            if round_index > 0:
                # 第一轮之后无条件收束：新消息由下一次 _tick 取新快照；没有新消息
                # 则不为一句必然的「我说完了」再付一次模型往返。
                exhausted = False
                break
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
            )
            acted = acted or result.reason == 'acted'
            if result.reason != 'acted':
                exhausted = False
                break
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
        # 抽取必须与摘要同处收尾：多 Agent 路径是当前默认路径，回合从这里结束。
        # - 现象：接线只挂在旧单发路径的收尾处，真机上 episodes 涨到 1088 条，
        #   而 facts 停在 2 条、抽取游标 fact_extract_cursor 从未被创建。
        # - 原因：两条收尾路径只有摘要挂了两处，抽取只挂了旧那一处，而默认走的是这条。
        # - 后果：漏挂不会报错也不留日志（_maybe_extract_facts 的前置判断都是静默 return），
        #   表现为「功能已接线但永远不产出」，只能靠游标为空反推。
        asyncio.create_task(
            self._maybe_extract_facts(context.stream.id, context.stream.kind)
        )
        # 表达学习同理，与抽取同处收尾、同样两处都挂。
        asyncio.create_task(self._maybe_learn_expressions(context.stream.id))
        asyncio.create_task(self._maybe_refresh_profiles())
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
    ) -> _RoundResult:
        """执行一轮 Conversation Agent 调用并处理其结果。

        silent 只写行动决策事件，不产生任何用户可见输出；reply 复用既有 sink
        消费副作用与分句，随后持久化、人格结算与平台投递；模型/协议失败不
        流出任何正文，按失败状态呈现。

        :return: 本轮的收束原因，由 ``_run_conversation_turn`` 据此结算回合。
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
            # 省掉一整类浪费——Bot 选择 silent 时，表达选择那次模型调用根本
            # 不会发生，而合并调用时该成本无论如何都会先发生。
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
            """把认知动作与外部只读工具轮逐条显示到控制台。

            内部工具轮不产生任何用户可见产物，不渲染的话终端上只会看到
            「Bot 沉默了十几秒然后说了句话」，中间查了什么完全不可见。一轮
            允许执行多个工具，因此按明细逐条渲染：只渲染最后一条会让同轮的
            前几次检索在控制台上无任何展示。
            """
            for name, argument, observation in round_outcome.cognitive_steps:
                render_action_decision(
                    turn=turn,
                    agent_scope='live',
                    event_status=round_outcome.event_status,
                    action=name,
                    query=argument,
                    observation=observation,
                )

        # 关闭 ReAct 时连范围都不算：那是一次真实的数据库查询，
        # 为一个永远不会被消费的字段付账没有意义。
        cognitive_scope = (
            self._cognitive_scope(
                frame, context.stream.id, context.stream.kind, context.person.kind,
            )
            if self._cognitive_rounds > 0
            else None
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
            cognitive_scope=cognitive_scope,
            cognitive_rounds=self._cognitive_rounds,
            tool_context=(
                ToolContext(
                    stream_id=context.stream.id,
                    stream_kind=context.stream.kind,
                    frame=frame,
                    turn_id=frame.turn_id,
                    snapshot_id=frame.snapshot_id,
                    cross_person=(
                        cognitive_scope.cross_person if cognitive_scope else False
                    ),
                )
                if self._tool_calling
                else None
            ),
            on_events=on_events,
            on_chunk=on_chunk,
            on_round=on_round,
            replyer_messages=replyer_messages if self._split_replyer else None,
            signal=cancel_event,
        )
        raw_text = ''.join(assistant_raw)
        trace.emit('llm_final', turnId=turn, text=raw_text)
        # 终局动作一律先在控制台留一行「Bot 决定做什么、为什么」。此前只有 reply 会
        # 通过 render_turn 露面，silent / wait / react / poke 全是空白——终端上看不出
        # Bot 到底是在思考、在等、还是根本没被叫醒。
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
                # Bot 看过这一轮并决定不接，自然回应窗口就此关闭；后续普通群消息
                # 重新回到攒批判断，直到真信号或 Bot 自己再次开口把窗口打开。
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
                source_label=source_label(
                    context.stream,
                    direct_name=sender['senderNickname'],
                ),
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
            await self._apply_poke(
                context, batch, turn, outcome, frame, gate_inputs, batch_gate,
            )
            return _RoundResult('acted')
        if outcome.decision.action == 'react':
            await self._apply_reaction(
                context, turn, outcome, frame, gate_inputs, batch_gate,
            )
            return _RoundResult('acted')
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
            source_label=source_label(
                context.stream,
                direct_name=sender['senderNickname'],
            ),
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
        # Bot 刚开过口，对话仍在 Bot 这边：重新敞开自然回应窗口，让紧接着说的话
        # 不必再经过回复必要性评分就能进入 Bot 的视野。
        self._follow_up_declined.discard(context.stream.id)
        return _RoundResult('acted')

    def _hold_batch_for_wait(
        self,
        context: ConversationContext,
        batch: list[_BufferedMessage],
        turn: int,
        outcome: AgentOutcome,
    ) -> None:
        """把本批消息放回缓冲，等对方把话说完再决定。

        wait 与 silent 的区别是这批消息是否已消费：silent 表示不回应这批消息，
        批次就此消费；wait 表示话未说完、暂不表态，批次退回缓冲，下次与新到的
        消息合并成更大一批重新判断，届时 Bot 可同时看到前后文。

        三件事必须一起做，缺一则等待失效：

        1. 批次放回缓冲头部：后到的消息本来更晚，放头部使合并后仍为时间正序
           （与 `_tick` 异常回滚路径同一写法）。
        2. 累计器加回去：扩展触发口径在拿到候选时清零了累计，不加回去则这批
           退回后无法再达到触发条数，Bot 不再看到它们。
        3. 标记等待：`_tick` 据此在没有新消息之前不重开回合，否则轮询周期
           一到就会把同一批重新提交模型。

        群聊中无新消息则一直等待，这是有意语义：等待中的批次在无下文时保持
        无回应。任何一条新消息都会解除等待，且该轮 wait 已不在动作集里，Bot
        必须表态。私聊不沿用该语义：对方在等待回应，因此等待持有开始时刻，
        超过 ``DIRECT_WAIT_TIMEOUT_S`` 仍无下文时，``_tick`` 强制重开回合，
        该轮同样没有 wait，Bot 必须表态。

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
        self._waiting[stream_id] = _WaitHold(len(buffered), current_time())
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
    ) -> None:
        """戳一戳目标消息的发送者。

        目标沿用消息编号而不引入「人物编号」目标空间：跨人物选目标依赖的人物
        归属尚未解决（需连带重绑关系与事实），新增目标空间会扩大该未解问题的
        影响面。发送者由消息反查。

        :param context: 当前会话上下文。
        :param batch: 本回合批次，用于把目标消息反查回发送者。
        :param turn: 对话回合 ID。
        :param outcome: 已确认 action 为 poke 的 Agent 结果。
        :param frame: 本回合固定快照，用于组装投递失败事件。
        :param gate_inputs: 第 1 层确定性输入事实。
        :param batch_gate: 本批门控结果。

        :return: 无返回值。此方法只负责投递与落动作历史；原返回字符串只服务于
            已删除的跨轮摘要，保留它会制造不存在的消费契约。
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
                turn_id=turn,
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
        target_name = self._registry.stream_display_name(
            target.context.person.id, context.stream.id,
        )
        # 只有平台确认成功后才登记自己的动作；失败投递不能伪造成已经戳过。
        self.memory.append_message(
            context.stream.id,
            None,
            'assistant',
            format_assistant_poke_action(target_name),
            current_time(),
        )
        self._mark_stage(context, REPLIED, '戳了一下', turn_id=turn)

    async def _apply_reaction(
        self,
        context: ConversationContext,
        turn: int,
        outcome: AgentOutcome,
        frame: DecisionFrame,
        gate_inputs: GateInputFacts,
        batch_gate: _BatchGate,
    ) -> None:
        """投递一次表情回应，并按与回复相同的口径结算人格。

        表情回应虽然没有正文，也是一项已经发生的可见动作；成功后以动作事实写入
        助手历史，避免后续回合因看不到该动作而重复操作。

        人格结算沿用回复的权重，不为 react 单设更轻的系数：一轮只允许一个动作，
        react 已构成实际参与；额外常量会引入互相牵制的参数。

        :param context: 当前会话上下文。
        :param turn: 对话回合 ID。
        :param outcome: 已确认 action 为 react 的 Agent 结果。
        :param frame: 本回合固定快照，用于组装投递失败事件。
        :param gate_inputs: 第 1 层确定性输入事实。
        :param batch_gate: 本批门控结果。

        :return: 无返回值。此方法只负责投递、人格结算与落动作历史；原返回字符串
            只服务于已删除的跨轮摘要，保留它会制造不存在的消费契约。
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
                # 不退化成「改成发条消息」——那是替 Bot 改主意。
                raise RuntimeError(
                    f'消息 {target_id} 没有平台编号，无法贴表情回应'
                )
            receipt = await self._broker.dispatch_reaction(OutboundReaction(
                stream=context.stream,
                target_external_message_id=external_id,
                reaction=outcome.decision.reaction,
                turn_id=turn,
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
        # 动作事实只在平台确认成功后落库；目标使用稳定的消息主键，便于回看。
        self.memory.append_message(
            context.stream.id,
            None,
            'assistant',
            format_assistant_reaction_action(
                target_id,
                outcome.decision.reaction,
            ),
            current_time(),
        )
        self._mark_stage(
            context, REPLIED, f'贴了个「{outcome.decision.reaction}」', turn_id=turn,
        )

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
        """判断 Bot 上次开口之后群里聊过的话是否还没走远。

        用消息距离而不是时间：活跃群聊数秒内可产生十余条消息且话题切换快，冷清群聊
        数分钟仅数条消息且话题稳定。时限口径由 natural_reply_window 单独承担，两者
        在门控里取并集。

        :param stream_id: 目标群 stream ID。
        :param last_bot_reply_at: Bot 上一条回复的落库毫秒时间戳。
        :return: 从该时刻起（含 Bot 那条）累计消息不超过 ONGOING_TOPIC_MESSAGE_SPAN
            条时返回 True。
        :raises sqlite3.Error: 统计消息表失败时由记忆层抛出。
        副作用：只读 messages 表。
        """
        spanned = self.memory.message_count_since(stream_id, last_bot_reply_at)
        return spanned <= ONGOING_TOPIC_MESSAGE_SPAN

    def follow_up_declined(self, stream_id: int) -> bool:
        """返回 Bot 是否已在这个群的上一次跟进机会里主动选择了沉默。

        入口门控与批次门控必须读同一份事实，否则 reply_gate 审计事件会报告一个
        与实际生效判定不同的门控态，现场排查时无法据此还原真实路径。

        :param stream_id: 目标 stream ID。
        :return: Bot 放弃过且此后没有再开口时返回 True。
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

    async def _consume_events(self, events: Iterable[ParseEvent], sink: _TurnSink) -> None:
        """消费解析事件，应用副作用并按平台选择输出路径。

        :param events: 响应解析器产生的事件迭代器。
        :param sink: 当前回合的聚合状态。

        副作用：
            写入事实、人格和 promise 状态，推送桌面解析事件，或向非桌面 sink
            聚合按 ``<say>`` 边界切分的出站文本；取消信号会提前结束消费。
            分句收集不区分平台，控制台摘要面板始终能拿到剥掉标签后的可见正文。
            表情包命中与落空同样进 sink.side_effects，由轮末面板呈现那一行。
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
                    content_hash = _send_ref_content_hash(selection.send_ref)
                    # 面板行要能指认「选中了哪张」：哈希前 8 位定位记录，
                    # 请求词与库内标签并排，检索是否贴题一眼可判；候选数来自本次抽样。
                    tags = self._emoji_library.emotion_tags_for_hash(content_hash) or ''
                    sink.side_effects.append({
                        'kind': 'emoji_selected',
                        'hash': content_hash[:8],
                        'emotion': event.emotion,
                        'tags': tags,
                        'candidateCount': selection.candidate_count,
                        'useCountBefore': selection.use_count,
                    })
                    trace.emit(
                        'emoji_selected',
                        turnId=sink.turn,
                        streamId=context.stream.id,
                        emotion=event.emotion,
                        hash=content_hash,
                        tags=tags,
                        candidateCount=selection.candidate_count,
                        useCountBefore=selection.use_count,
                    )
                else:
                    # 落空此前完全静默：模型写了 <emoji> 但检索没有可用候选时，
                    # 终端与观察面板都看不到任何痕迹，现场只能表现为「发不出」。
                    # 这里留一条与命中对称的事件，便于区分「没写」和「写了没中」。
                    sink.side_effects.append({
                        'kind': 'emoji_selection_missed',
                        'emotion': event.emotion,
                    })
                    trace.emit(
                        'emoji_selection_missed',
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

