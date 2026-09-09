"""对话回合与会话的内部数据结构。

本模块只放不依赖 ``ChatService`` 实例的状态载体：批次失败计数、缓冲消息、
会话语气与 nonce、私聊跟进锚点、单轮流式解析状态、事实召回留痕、组装完成的
回合上下文、批次门控结果、单轮收束原因与 wait 持有状态。

这些类型全部由 ``src.core.services.chat.service`` 使用，对包外不构成接口；
名称保留下划线前缀，是因为若干测试按原名引用（拆包前它们与 ChatService
同在一个模块里）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Sequence, Tuple

import asyncio

from src.core.agent.conversation_gate import GateResult
from src.core.agent.parser import ParseEvent
from src.core.memory.store import RecalledFact
from src.core.observe import events as trace
from src.core.platform_io.types import ConversationContext


class _BatchFailureTracker:
    """按会话记录「同一批输入」连续失败的次数。

    后台队列以批为单位推进，批次由其首条消息 ID 标识：队列没前进时，下一轮取到
    的仍是同一批、首条 ID 不变；队列一旦前进，计数自然从头开始，因此不需要额外
    的失效逻辑。

    :ivar _limit: 判定这一批无法处理所需的连续失败次数。
    :ivar _state: 会话 ID 到 ``(批次首条消息 ID, 连续失败次数)`` 的映射。
    """

    def __init__(self, limit: int) -> None:
        """创建一个尚未记录任何失败的计数器。

        :param limit: 连续失败达到该次数即视为这一批无法处理。
        """
        self._limit = limit
        self._state: dict[int, tuple[int, int]] = {}

    def record(self, stream_id: int, head_id: int) -> int:
        """记一次失败，返回这一批已连续失败的次数。

        :param stream_id: 失败所属的会话 ID。
        :param head_id: 本批首条消息的 ID，用于识别是否仍是同一批。
        :return: 含本次在内的连续失败次数；批次头变化时从 1 重新计。
        副作用：更新内部计数表。
        """
        previous_head, count = self._state.get(stream_id, (0, 0))
        count = count + 1 if previous_head == head_id else 1
        self._state[stream_id] = (head_id, count)
        return count

    def exhausted(self, count: int) -> bool:
        """判断连续失败次数是否已达放弃这一批的门槛。

        :param count: :meth:`record` 返回的连续失败次数。
        :return: 达到或超过上限时为 ``True``。
        副作用：无。
        """
        return count >= self._limit

    def clear(self, stream_id: int) -> None:
        """在该会话的队列成功前进后清除计数。

        :param stream_id: 已推进队列的会话 ID。
        :return: 无返回值。
        副作用：删除该会话的计数记录。
        """
        self._state.pop(stream_id, None)


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
    # 入口门控判定过的戳一戳事实，批次门控据此读同一份事实：不重算信号窗口，
    # 也不拿适配器合成的正文做名字匹配。
    poked_me: bool = False
    pokes_in_window: int = 0


@dataclass
class _SessionState:
    """一个 stream 内稳定的语气、会话 nonce 和单次重逢上下文。"""

    started_at: int | None = None
    tone: str | None = None
    # 会话 nonce：每次新会话重摇，供测试观测「跨过静默间隔后会话真的重开了」。
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
    # 完整 say 通过护栏后才进入桌面、语音及气泡切分；历史保留原始台词边界。
    pending_say: List[ParseEvent] = field(default_factory=list)
    say_markup: List[str] = field(default_factory=list)
    say_texts: List[str] = field(default_factory=list)
    interrupted: bool = False
    # 已按模型目标情绪选中的可发送引用；只有真实命中才进入出站和历史。
    emoji_items: list[tuple[str, str, int]] = field(default_factory=list)


@dataclass
class _RetrievalTrace:
    """保存一轮事实召回的无损输入、候选池与单次落账状态。

    会话印象不能截断：截断后的文本会改变分词、召回集合与得分，使事件失去重放
    价值。体积控制留给事件保留策略，不在检索输入上做有损处理。
    """

    turn_id: int | None
    stream_id: int
    current_text: str
    conversation_impression: str
    candidate_pool: Tuple[Tuple[int, float], ...]
    emitted: bool = False

    def emit_once(self, prompt_fact_ids: Sequence[int]) -> None:
        """记录本轮首份真实生产提示词使用的事实，并保证同轮只写一条。"""

        if self.emitted:
            return
        trace.emit(
            'memory_retrieval_trace',
            turnId=self.turn_id,
            streamId=self.stream_id,
            currentText=self.current_text,
            conversationImpression=self.conversation_impression,
            currentTextChars=len(self.current_text),
            impressionChars=len(self.conversation_impression),
            candidateCount=len(self.candidate_pool),
            candidatePool=[
                {'factId': fact_id, 'score': score}
                for fact_id, score in self.candidate_pool
            ],
            promptFactIds=list(prompt_fact_ids),
        )
        self.emitted = True


@dataclass(frozen=True)
class _PreparedTurnContext:
    """保存一次性组装完成、可继续附加模型增强的回合上下文。"""

    context: ConversationContext
    query: str
    now: int
    platform_bot_name: str | None
    fact_candidates: list[RecalledFact]
    retrieval_trace: _RetrievalTrace
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
    # 本回合的黑话命中（词, 压缩后的含义）。查表带着 hits 写库与去重登记
    # 两类副作用，而组装好的上下文会被决策与回复两次渲染共用——查表放在
    # 组装期执行一次，结果作为不可变字段带下去，渲染期只读。
    jargon: tuple[tuple[str, str], ...]
    # 当前对话者与 Bot 的共处群（对方显示名, 群标签元组）；仅非群聊会话组装，
    # 无共处群时为 None。与 jargon 同一纪律：组装期取数一次，两次渲染只读。
    shared_groups: tuple[str, tuple[str, ...]] | None = None


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
    """回合内一轮的收束原因。"""

    # acted：产出了可见产物；declined：Bot 表示这轮做完了；
    # paused：批次退回缓冲等下文；failed：模型或协议失败。
    reason: str


@dataclass(frozen=True)
class _WaitHold:
    """一次 wait 的持有状态。

    :ivar watermark: 批次退回缓冲后的消息总数；缓冲长度超过它说明有新消息到达。
    :ivar since: 开始等待的毫秒时间戳，供私聊的下文超时兜底判断使用。
    """

    watermark: int
    since: int
