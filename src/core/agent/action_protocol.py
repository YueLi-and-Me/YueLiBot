"""Conversation Agent 的第一期行动协议与决策校验。

本模块定义「决策外壳 + reply 负载」两层协议：动作枚举、封闭 reason_codes、
平台能力、回合固定快照（``DecisionFrame``）与 ``ConversationDecision`` 的
合法性校验，以及四层可审计行动事件（``ActionDecisionEvent``）。全部为纯
确定性逻辑，不调用模型、不读写数据库；模型产出决策后必须先通过
``ConversationDecision.validate`` 完成硬边界校验，校验失败按协议错误处理，
不允许静默降级成普通回复。

对外暴露：
- ``ConversationAction`` / ``ReplyLength`` / ``GateDisposition`` / ``EventStatus``
  以及回复、沉默两套封闭 reason_codes；
- ``PlatformCapabilities``：运行时按当前 stream 与平台适配器真实具备的能力；
- ``DecisionFrame``：一次注意力候选窗口的回合固定快照；
- ``ReplyPayload`` / ``ConversationDecision``：模型决策的数据形态与自检；
- ``available_actions``：按 stream 与平台能力动态收窄动作空间；
- ``GateInputFacts`` / ``ActionDecisionEvent``：四层审计事件，
  ``to_dict`` 生成可供 trace 使用的可序列化字典。

依赖：仅标准库与 ``src.core.platform_io.types`` 的 ``StreamKind`` 别名；
被 Conversation Agent 与 ``src.core.services.chat`` 消费，不反向依赖。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from src.core.platform_io.types import StreamKind

# 第一期基础动作：react 仅当平台适配器已验证真实执行能力时开放。
ConversationAction = Literal['reply', 'silent', 'react']
ReplyLength = Literal['brief', 'long']
# 三态门控的态；门控判定本身在 conversation_gate 模块实现。
GateDisposition = Literal['drop', 'force', 'deliberate']
# 行动事件的状态：区分「她考虑后选择行动」与各类失败，绝不允许混为一谈。
EventStatus = Literal[
    'committed',
    'silent_by_choice',
    'gate_dropped',
    'timeout',
    'provider_error',
    'parse_error',
    'illegal_action',
    'delivery_failed',
]

# 回复理由：封闭枚举，不允许模型自由编造；后续扩充必须显式改这里并同步校验。
REPLY_REASON_CODES: frozenset[str] = frozenset({
    'directly_addressed',
    'direct_question',
    'topic_continuation',
    'emotional_support',
    'pending_thread',
    'can_add_value',
    'relationship_impulse',
    'natural_reaction',
})

# 沉默理由：封闭枚举，同样不允许自由文本思维链进入事件。
SILENT_REASON_CODES: frozenset[str] = frozenset({
    'others_conversation',
    'would_interrupt',
    'no_new_value',
    'topic_closed',
    'duplicate_response',
    'not_addressed',
    'attention_elsewhere',
    'low_relevance',
})

ALL_REASON_CODES: frozenset[str] = REPLY_REASON_CODES | SILENT_REASON_CODES


def _validate_reason_codes(
    action: ConversationAction,
    reason_codes: tuple[str, ...],
) -> None:
    """校验理由码的形状与动作分域，完整决策与动作头共用。

    :param action: 已确认属于封闭动作集的当前动作。
    :param reason_codes: 模型声明的理由码元组。
    :raises IllegalActionError: 理由码为空、重复、未知或与动作分域矛盾。
    """
    if not reason_codes:
        raise IllegalActionError('reason_codes 不能为空')
    if len(set(reason_codes)) != len(reason_codes):
        raise IllegalActionError('reason_codes 不允许重复')
    # react 不单独说话，其理由与回复同域；沉默理由不能与回复动作混用。
    domain = REPLY_REASON_CODES if action != 'silent' else SILENT_REASON_CODES
    for code in reason_codes:
        if code not in ALL_REASON_CODES:
            raise IllegalActionError(
                f'未知 reason_code：{code}（封闭枚举，不允许自由字符串）'
            )
        if code not in domain:
            raise IllegalActionError(f'reason_code {code} 不能与动作 {action} 组合')


class IllegalActionError(ValueError):
    """决策违反行动协议或回合帧约束时抛出，代表模型协议错误。

    调用方必须把该异常记录为 ``illegal_action`` 事件状态，不得降级成普通
    回复，也不得记录成「她选择沉默」。
    """


@dataclass(frozen=True)
class PlatformCapabilities:
    """运行时按当前 stream 与平台适配器真实具备的能力。

    模型只能在这些真实能力内选择：平台不支持引用时决策不能携带
    ``quote_message_id``；平台未验证 reaction 执行能力时 ``react`` 不进入
    动作集，且可用反应标识封闭给出，模型不能自由生成资源 ID。
    """

    quote: bool = False
    react: bool = False
    available_reactions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """拒绝空反应标识，防止资源 ID 空洞进入动作集。"""
        if self.react and not self.available_reactions:
            raise ValueError('react 能力开启时必须给出可用的反应标识')
        for reaction_id in self.available_reactions:
            if not reaction_id.strip():
                raise ValueError('可用反应标识不能为空字符串')


@dataclass(frozen=True)
class DecisionFrame:
    """一次注意力候选窗口的回合固定快照。

    ``selectable_message_ids`` 只包含当前同一人物批次内的消息；水位之后的
    未来消息与其他人物的历史都不可选。``available_actions`` 由运行时按
    stream 与平台能力计算，模型只能在其内选择。
    """

    turn_id: int
    snapshot_id: str
    stream_kind: StreamKind
    disposition: GateDisposition
    selectable_message_ids: tuple[int, ...]
    message_watermark: int
    available_actions: frozenset[ConversationAction]
    capabilities: PlatformCapabilities

    def __post_init__(self) -> None:
        """拒绝自相矛盾的帧：可选项必须不晚于水位，动作集与门控态必须一致。"""
        if not self.snapshot_id:
            raise ValueError('snapshot_id 不能为空')
        for message_id in self.selectable_message_ids:
            if message_id > self.message_watermark:
                raise ValueError(
                    f'可选消息 {message_id} 晚于回合消息水位 {self.message_watermark}'
                )
        if self.disposition == 'drop' and self.available_actions:
            raise ValueError('DROP 候选不调用模型，动作集必须为空')
        if self.disposition != 'drop' and not self.available_actions:
            raise ValueError(f'{self.disposition} 候选的动作集不能为空')


@dataclass(frozen=True)
class ReplyPayload:
    """reply 动作的正文负载。

    ``expression_intent`` 为后续表达增强预留，第一期只校验其非空性，不消费。
    """

    text: str
    length: ReplyLength
    expression_intent: str | None = None

    def __post_init__(self) -> None:
        """拒绝空正文、未知篇幅与空表达意图。"""
        if not self.text.strip():
            raise ValueError('回复正文不能为空')
        if self.length not in ('brief', 'long'):
            raise ValueError(f'未知回复篇幅：{self.length}')
        if self.expression_intent is not None and not self.expression_intent.strip():
            raise ValueError('表达意图不能为空字符串')


def _validate_frame_choice(
    action: ConversationAction,
    target_message_ids: tuple[int, ...],
    quote_message_id: int | None,
    frame: DecisionFrame,
) -> None:
    """校验动作、目标与引用在回合帧内的合法性，完整决策与动作头共用。

    目标必须属于本回合 selectable_message_ids 且不晚于水位；引用还必须
    具备平台能力。任何违反都按协议错误抛出，调用方不得静默降级。

    :param action: 当前动作。
    :param target_message_ids: 目标消息 ID 元组。
    :param quote_message_id: 可选的引用消息 ID。
    :param frame: 本回合固定快照。
    :raises IllegalActionError: DROP 帧带决策、动作超出动作空间、FORCE 场景
        silent、目标或引用越界、引用能力缺失。
    """
    if frame.disposition == 'drop':
        raise IllegalActionError('DROP 候选不调用模型，不存在合法决策')
    if action not in frame.available_actions:
        raise IllegalActionError(
            f'动作 {action} 不在本回合可用动作'
            f' {sorted(frame.available_actions)} 中'
        )
    if frame.disposition == 'force' and action == 'silent':
        raise IllegalActionError('FORCE 场景不允许 silent')
    selectable = frozenset(frame.selectable_message_ids)
    for target in target_message_ids:
        if target not in selectable:
            raise IllegalActionError(
                f'目标消息 {target} 不在本回合 selectable_message_ids 内'
            )
        if target > frame.message_watermark:
            raise IllegalActionError(
                f'目标消息 {target} 晚于回合消息水位 {frame.message_watermark}'
            )
    if quote_message_id is not None:
        if not frame.capabilities.quote:
            raise IllegalActionError('平台不支持引用时不能携带 quote_message_id')
        if quote_message_id not in selectable:
            raise IllegalActionError(
                f'引用消息 {quote_message_id} 不在本回合 selectable_message_ids 内'
            )
        if quote_message_id > frame.message_watermark:
            raise IllegalActionError(
                f'引用消息 {quote_message_id} 晚于回合消息水位'
                f' {frame.message_watermark}'
            )


@dataclass(frozen=True)
class ConversationDecision:
    """一次模型调用产出的行动决策外壳及可选 reply 负载。

    结构自检（``__post_init__``）只拒绝形状矛盾；与回合帧相关的硬边界
    （动作空间、目标范围、引用能力、FORCE 禁默）由 ``validate`` 校验。
    """

    action: ConversationAction
    target_message_ids: tuple[int, ...]
    quote_message_id: int | None
    reason_codes: tuple[str, ...]
    reply: ReplyPayload | None = None

    def __post_init__(self) -> None:
        """拒绝形状矛盾：未知动作、自由 reason_code、动作与负载不匹配。"""
        if self.action not in ('reply', 'silent', 'react'):
            raise IllegalActionError(f'未知动作：{self.action}')
        _validate_reason_codes(self.action, self.reason_codes)
        if self.action == 'reply':
            if self.reply is None:
                raise IllegalActionError('reply 动作必须携带 reply 负载')
            if not self.target_message_ids:
                raise IllegalActionError('reply 动作必须指定至少一条目标消息')
        if self.action == 'silent':
            if self.reply is not None:
                raise IllegalActionError('silent 动作不能携带 reply 负载')
            if self.target_message_ids:
                raise IllegalActionError('silent 动作不能指定目标消息')
            if self.quote_message_id is not None:
                raise IllegalActionError('silent 动作不能携带引用')
        if self.action == 'react':
            if self.reply is not None:
                raise IllegalActionError('react 动作不能携带 reply 负载')
            if len(self.target_message_ids) != 1:
                raise IllegalActionError('react 动作必须且只能指定一条目标消息')
            if self.quote_message_id is not None:
                raise IllegalActionError('react 动作不能携带引用')

    def validate(self, frame: DecisionFrame) -> None:
        """按回合帧校验决策的硬边界，违反时抛出协议错误。

        :param frame: 本回合固定快照；目标、引用、动作空间与门控态约束均
            以它为准。
        :raises IllegalActionError: 目标超出可选集、引用能力缺失、动作不在
            动作空间或 FORCE 场景返回 silent 时抛出。
        """
        _validate_frame_choice(
            self.action,
            self.target_message_ids,
            self.quote_message_id,
            frame,
        )


@dataclass(frozen=True)
class DecisionHead:
    """动作头：正文流式输出前必须完整且通过校验的决策外壳。

    与 ConversationDecision 的区别是 reply 的正文此刻尚未产生：reply 动作
    用 length 声明篇幅，正文随后以 <say> 流式输出；silent 只存在动作
    头本身，其后不允许任何正文。
    """

    action: ConversationAction
    target_message_ids: tuple[int, ...]
    quote_message_id: int | None
    reason_codes: tuple[str, ...]
    length: ReplyLength | None = None

    def __post_init__(self) -> None:
        """拒绝形状矛盾：未知动作、自由 reason_code、篇幅与动作不匹配。"""
        if self.action not in ('reply', 'silent', 'react'):
            raise IllegalActionError(f'未知动作：{self.action}')
        _validate_reason_codes(self.action, self.reason_codes)
        if self.action == 'reply':
            if not self.target_message_ids:
                raise IllegalActionError('reply 动作必须指定至少一条目标消息')
            if self.length not in ('brief', 'long'):
                raise IllegalActionError('reply 动作头必须声明 brief 或 long 篇幅')
        if self.action == 'silent':
            if self.target_message_ids:
                raise IllegalActionError('silent 动作不能指定目标消息')
            if self.quote_message_id is not None:
                raise IllegalActionError('silent 动作不能携带引用')
            if self.length is not None:
                raise IllegalActionError('silent 动作不能声明回复篇幅')
        if self.action == 'react':
            if len(self.target_message_ids) != 1:
                raise IllegalActionError('react 动作必须且只能指定一条目标消息')
            if self.quote_message_id is not None:
                raise IllegalActionError('react 动作不能携带引用')
            if self.length is not None:
                raise IllegalActionError('react 动作不能声明回复篇幅')

    def validate(self, frame: DecisionFrame) -> None:
        """按回合帧校验动作头，违反时抛出协议错误。

        :param frame: 本回合固定快照。
        :raises IllegalActionError: 目标越界、引用能力缺失、动作超出动作
            空间或 FORCE 场景 silent。
        """
        _validate_frame_choice(
            self.action,
            self.target_message_ids,
            self.quote_message_id,
            frame,
        )

    def to_decision(self, body_text: str) -> ConversationDecision:
        """结合流式正文组装完整决策。

        :param body_text: reply 动作的完整可见正文；silent 与 react 忽略该
            参数，传入非空值视为协议错误。
        :return: 通过结构自检的 ConversationDecision。
        :raises IllegalActionError: silent/react 传入正文或组装结果结构非法。
        """
        if self.action == 'silent':
            if body_text.strip():
                raise IllegalActionError('silent 动作头之后不能有正文')
            return ConversationDecision(
                action='silent',
                target_message_ids=(),
                quote_message_id=None,
                reason_codes=self.reason_codes,
                reply=None,
            )
        if self.action == 'react':
            if body_text.strip():
                raise IllegalActionError('react 动作头之后不能有正文')
            return ConversationDecision(
                action='react',
                target_message_ids=self.target_message_ids,
                quote_message_id=None,
                reason_codes=self.reason_codes,
                reply=None,
            )
        if not body_text.strip():
            raise IllegalActionError('reply 动作头之后没有正文')
        return ConversationDecision(
            action='reply',
            target_message_ids=self.target_message_ids,
            quote_message_id=self.quote_message_id,
            reason_codes=self.reason_codes,
            reply=ReplyPayload(text=body_text.strip(), length=self.length or 'brief'),
        )


def available_actions(
    stream_kind: StreamKind,
    disposition: GateDisposition,
    capabilities: PlatformCapabilities,
) -> frozenset[ConversationAction]:
    """按 stream 与平台能力动态收窄第一版动作空间。

    :param stream_kind: 会话类型；私聊与桌面第一版不允许无解释的 silent，
        那里的交互契约是用户直接对她说话。
    :param disposition: 门控态；DROP 不进入模型，动作集为空；群聊 FORCE
        （@必回）只允许 reply。
    :param capabilities: 运行时真实具备的平台能力；只有已验证的 reaction
        支持才会让 react 进入动作集。

    :return: 本回合允许模型选择的动作集合。
    """
    if disposition == 'drop':
        return frozenset()
    if stream_kind in ('desktop', 'direct'):
        return frozenset({'reply'})
    if disposition == 'force':
        return frozenset({'reply'})
    actions: set[ConversationAction] = {'reply', 'silent'}
    if capabilities.react:
        actions.add('react')
    return frozenset(actions)


@dataclass(frozen=True)
class GateInputFacts:
    """行动事件第 1 层：决策时刻的确定性输入事实快照。

    字段与门控判据一一对应，全部由代码按事实填充，不含任何模型自由文本；
    后续块引入人物作用域与未决线索时继续扩充。
    """

    stream_kind: str
    mentioned_me: bool
    name_mentioned: bool
    must_reply: bool
    asleep: bool
    rate_limited: bool
    recent_bot_replies: int
    candidate_message_ids: tuple[int, ...]
    selectable_message_ids: tuple[int, ...]
    reply_to_bot: bool = False
    current_topic_available: bool = False
    pending_thread_available: bool = False

    def to_dict(self) -> dict[str, Any]:
        """转换为 trace 使用的驼峰字段字典。"""
        return {
            'streamKind': self.stream_kind,
            'mentionedMe': self.mentioned_me,
            'nameMentioned': self.name_mentioned,
            'mustReply': self.must_reply,
            'asleep': self.asleep,
            'rateLimited': self.rate_limited,
            'recentBotReplies': self.recent_bot_replies,
            'candidateMessageIds': list(self.candidate_message_ids),
            'selectableMessageIds': list(self.selectable_message_ids),
            'replyToBot': self.reply_to_bot,
            'currentTopicAvailable': self.current_topic_available,
            'pendingThreadAvailable': self.pending_thread_available,
        }


@dataclass(frozen=True)
class ActionDecisionEvent:
    """四层可审计行动事件：输入事实 / 门控结果 / Agent 决策 / 版本信息。

    ``event_status`` 必须区分：``committed``（她考虑后选择行动）、
    ``silent_by_choice``（她考虑后选择沉默）、``gate_dropped``（代码根本没
    让她考虑）、``timeout`` / ``provider_error`` / ``parse_error`` /
    ``illegal_action`` / ``delivery_failed``（模型或投递故障）。模型失败与
    自主沉默绝不能混进同一个状态。
    """

    # DROP 发生在任何回合之前，该层没有回合编号，因此允许为 None。
    turn_id: int | None
    snapshot_id: str
    turn_message_watermark: int
    gate_inputs: GateInputFacts
    gate_disposition: GateDisposition
    gate_reason_codes: tuple[str, ...]
    available_actions: tuple[str, ...]
    decision: ConversationDecision | None
    event_status: EventStatus
    detail: str = ''
    prompt_hash: str = ''
    model_task: str = ''
    provider: str = ''
    model: str = ''
    latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        """组装四层审计字典，供 ``trace.emit('action_decision', ...)`` 使用。

        :return: 含 ``turnId`` / ``snapshotId`` / ``eventStatus`` 与
            ``inputs`` / ``gate`` / ``decision`` / ``version`` 四层的字典。
        """
        decision = None
        if self.decision is not None:
            decision = {
                'action': self.decision.action,
                'targetMessageIds': list(self.decision.target_message_ids),
                'quoteMessageId': self.decision.quote_message_id,
                'reasonCodes': list(self.decision.reason_codes),
                'reply': (
                    {
                        'text': self.decision.reply.text,
                        'length': self.decision.reply.length,
                        **(
                            {'expressionIntent': self.decision.reply.expression_intent}
                            if self.decision.reply.expression_intent is not None
                            else {}
                        ),
                    }
                    if self.decision.reply is not None
                    else None
                ),
            }
        return {
            'turnId': self.turn_id,
            'snapshotId': self.snapshot_id,
            'messageWatermark': self.turn_message_watermark,
            'eventStatus': self.event_status,
            'detail': self.detail,
            'inputs': self.gate_inputs.to_dict(),
            'gate': {
                'disposition': self.gate_disposition,
                'reasonCodes': list(self.gate_reason_codes),
                'availableActions': list(self.available_actions),
            },
            'decision': decision,
            'version': {
                'promptHash': self.prompt_hash,
                'modelTask': self.model_task,
                'provider': self.provider,
                'model': self.model,
                'latencyMs': self.latency_ms,
            },
        }
