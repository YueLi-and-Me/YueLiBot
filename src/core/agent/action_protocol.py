"""Conversation Agent 的第一期行动协议与决策校验。

本模块定义「决策外壳 + reply 负载」两层协议：终局与认知两类动作枚举、封闭 reason_codes、
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
- ``available_actions``：按 stream、平台能力与**剩余认知轮次**动态收窄动作空间——
  剩余轮次归零时认知动作直接不在动作集里，模型再选就是越界，不存在「预算耗尽降级」路径；
- ``GateInputFacts`` / ``ActionDecisionEvent``：四层审计事件，
  ``to_dict`` 生成可供 trace 使用的可序列化字典。

依赖：仅标准库与 ``src.core.platform_io.types`` 的 ``StreamKind`` 别名；
被 Conversation Agent 与 ``src.core.services.chat`` 消费，不反向依赖。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from src.core.platform_io.types import StreamKind

# 终局动作：产出可见产物或明确结束本回合；react 仅当平台适配器已验证真实执行能力时开放。
# 认知动作：不产生任何可见产物，执行后把观察结果回灌给模型并再发起一轮（ReAct 回环）。
ConversationAction = Literal[
    'reply', 'silent', 'react', 'poke', 'wait', 'speak', 'recall', 'inspect',
]
TERMINAL_ACTIONS: frozenset[ConversationAction] = frozenset({
    'reply', 'silent', 'react', 'poke', 'wait', 'speak',
})
COGNITIVE_ACTIONS: frozenset[ConversationAction] = frozenset({'recall', 'inspect'})
ALL_ACTIONS: frozenset[ConversationAction] = TERMINAL_ACTIONS | COGNITIVE_ACTIONS
ReplyLength = Literal['brief', 'long']
# 三态门控的态；门控判定本身在 conversation_gate 模块实现。
GateDisposition = Literal['drop', 'force', 'deliberate']
# 行动事件的状态：区分「她考虑后选择行动」与各类失败，绝不允许混为一谈。
# cognitive_step 是认知轮的终态：既不是她定了要做什么，也不是失败，而是「她去查了一下」。
EventStatus = Literal[
    'committed',
    'silent_by_choice',
    'cognitive_step',
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

# 等待理由：封闭枚举。它与沉默分域是有意的——「我决定不接这茬」和「话还没说完，
# 我先不表态」是两件不同的事，混进同一个域会让账本再也分不出她是放弃了还是在等。
WAIT_REASON_CODES: frozenset[str] = frozenset({
    'unfinished_thought',
    'thread_developing',
})

# 主动开口的理由：封闭枚举。与回复分域，因为「有人跟我说话所以我接」和
# 「没人跟我说话但我想说」是两种完全不同的动机，混域会让账本分不出她是被
# 叫起来的还是自己起的念头。
SPEAK_REASON_CODES: frozenset[str] = frozenset({
    'noticed_activity',
    'remembered_something',
    'long_silence',
    'promise_due',
})

ALL_REASON_CODES: frozenset[str] = (
    REPLY_REASON_CODES
    | SILENT_REASON_CODES
    | WAIT_REASON_CODES
    | SPEAK_REASON_CODES
)

# 表情回应的语义词表：模型只写这些名字，平台侧的具体表情编号由适配器映射。
# 语义名而非平台编号进协议，是因为「贴哪个表情」是角色行为，「它在 QQ 上是几号」
# 是平台细节；把编号写进提示词等于让人格层去背协议表，换个平台就得重写人格。
#
# 【关键】名字必须逐字照抄平台自己的表情名，不许自己起近义词。
#
# - 现象：第一版凭印象写了「惊讶」「无语」两个名字，实测「惊讶」被配到 26 号
#   （那是「惊恐」），而「无语」在 QQ 的表情表里**根本不存在**。
# - 原因：语义名与平台编号是两张表，名字一旦自创就失去可逐条比对的基准，
#   错配只能靠人眼在群里发现。
# - 后果：贴错表情不报错，只会显示成另一个表情，是最难被发现的那类错。
#   照抄平台名之后，映射表可以直接对着平台的表情表逐条核。
#
# 只给六个，不给全表：真人在群里常用的反应就那么几个，给两百个只会让她挑得
# 又慢又乱。这六个覆盖六种不同的社交意图（认可 / 觉得好笑 / 服了 / 喜欢 /
# 没想到 / 围观），要加就明确加，别为了「更全」而全。
REACTION_IDS: tuple[str, ...] = ('赞', '笑哭', '无奈', '爱心', '惊讶', '吃瓜')


def _validate_reason_codes(
    action: ConversationAction,
    reason_codes: tuple[str, ...],
) -> None:
    """校验理由码的形状与动作分域，完整决策与动作头共用。

    认知动作（recall/inspect）不参与本校验：理由码是给「回不回」这个决策做审计的
    封闭枚举，而认知动作的审计信息是它的 query 本身。强行要求一个不承载信息的字段
    只会加重生成侧格式负担、抬高 parse_error 率——动作头合规率是花了一整轮 shadow
    才压到 0 的，不为此再赌一次。

    :param action: 已确认属于封闭动作集的当前动作。
    :param reason_codes: 模型声明的理由码元组。
    :raises IllegalActionError: 理由码为空、重复、未知或与动作分域矛盾。
    """
    if action in COGNITIVE_ACTIONS:
        return
    if not reason_codes:
        raise IllegalActionError('reason_codes 不能为空')
    if len(set(reason_codes)) != len(reason_codes):
        raise IllegalActionError('reason_codes 不允许重复')
    # react 与 poke 不单独说话，但都是「做出了回应」，其理由与回复同域；
    # 沉默与等待各有自己的域，三者不允许互串。
    if action == 'silent':
        domain = SILENT_REASON_CODES
    elif action == 'wait':
        domain = WAIT_REASON_CODES
    elif action == 'speak':
        domain = SPEAK_REASON_CODES
    else:
        domain = REPLY_REASON_CODES
    for code in reason_codes:
        if code not in ALL_REASON_CODES:
            raise IllegalActionError(
                f'未知 reason_code：{code}（封闭枚举，不允许自由字符串）'
            )
        if code not in domain:
            raise IllegalActionError(f'reason_code {code} 不能与动作 {action} 组合')


def _validate_cognitive_shape(
    action: ConversationAction,
    query: str | None,
    target_message_ids: tuple[int, ...],
    quote_message_id: int | None,
) -> None:
    """校验认知动作的形状：必须有检索词，且不得携带任何投递侧字段。

    认知动作只读、不产出可见产物，因此 targets / quote 这类「回给谁、引用哪条」
    的字段对它没有意义；携带即视为模型把两类动作混淆，按协议错误处理。

    :param action: 已确认属于认知动作集的当前动作。
    :param query: 检索词原文。
    :param target_message_ids: 目标消息 ID 元组，认知动作必须为空。
    :param quote_message_id: 引用消息 ID，认知动作必须为 None。
    :raises IllegalActionError: 检索词缺失或为空白，或携带了投递侧字段。
    """
    if query is None or not query.strip():
        raise IllegalActionError(f'{action} 动作必须携带非空 query')
    if target_message_ids:
        raise IllegalActionError(f'{action} 动作不能指定目标消息')
    if quote_message_id is not None:
        raise IllegalActionError(f'{action} 动作不能携带引用')


class IllegalActionError(ValueError):
    """决策违反行动协议或回合帧约束时抛出，代表模型协议错误。

    调用方必须把该异常记录为 ``illegal_action`` 事件状态，不得降级成普通
    回复，也不得记录成「她选择沉默」。
    """


@dataclass(frozen=True)
class PlatformCapabilities:
    """运行时按当前 stream 与平台适配器真实具备的能力。

    模型只能在这些真实能力内选择：``quote`` 关闭时决策不能携带
    ``quote_message_id``；平台未验证 reaction 执行能力时 ``react`` 不进入
    动作集，且可用反应标识封闭给出；``emoji`` 表示当前平台、表情包库和
    频率窗口共同允许产生表情包可见产物。

    ``quote`` 只管「模型能否自己指定引用目标」。QQ 群聊投递时按目标消息是否
    已被后续发言冲开自动挂引用，那条路径由代码强制，不受本开关影响——目标已经
    由模型选定，引用只是这个选择在平台上的呈现方式。
    """

    quote: bool = False
    react: bool = False
    available_reactions: tuple[str, ...] = ()
    emoji: bool = False
    poke: bool = False

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
        for action in self.available_actions:
            if action not in ALL_ACTIONS:
                raise ValueError(f'未知动作进入动作集：{action}')

    def with_available_actions(
        self,
        actions: frozenset[ConversationAction],
    ) -> 'DecisionFrame':
        """复制本帧并替换动作集，用于 ReAct 各轮按剩余预算收窄动作空间。

        除动作集之外的一切（水位、可选消息、门控态、平台能力）在整个回合内保持不变，
        因此「回合固定快照」这条性质不被多轮破坏：变的只有她这一轮还能选什么。

        :param actions: 本轮允许的动作集合。
        :return: 除动作集外与本帧完全相同的新帧。
        :raises ValueError: 新动作集与门控态矛盾或含未知动作。
        """
        return DecisionFrame(
            turn_id=self.turn_id,
            snapshot_id=self.snapshot_id,
            stream_kind=self.stream_kind,
            disposition=self.disposition,
            selectable_message_ids=self.selectable_message_ids,
            message_watermark=self.message_watermark,
            available_actions=actions,
            capabilities=self.capabilities,
        )


@dataclass(frozen=True)
class ReplyPayload:
    """reply 动作的正文负载。

    ``expression_intent`` 为后续表达增强预留，第一期只校验其非空性，不消费。
    """

    text: str
    length: ReplyLength
    expression_intent: str | None = None
    emoji_emotions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """拒绝无可见产物、未知篇幅、空表达意图和多表情包。"""
        if not self.text.strip() and not self.emoji_emotions:
            raise ValueError('回复必须包含正文或表情包')
        if self.length not in ('brief', 'long'):
            raise ValueError(f'未知回复篇幅：{self.length}')
        if self.expression_intent is not None and not self.expression_intent.strip():
            raise ValueError('表达意图不能为空字符串')
        if any(not emotion.strip() for emotion in self.emoji_emotions):
            raise ValueError('表情包目标情绪不能为空字符串')
        if len(self.emoji_emotions) > 1:
            raise ValueError('一轮回复最多发送一张表情包')


def _validate_frame_choice(
    action: ConversationAction,
    target_message_ids: tuple[int, ...],
    quote_message_id: int | None,
    reaction: str | None,
    frame: DecisionFrame,
) -> None:
    """校验动作、目标与引用在回合帧内的合法性，完整决策与动作头共用。

    目标必须属于本回合 selectable_message_ids 且不晚于水位；引用还必须
    具备平台能力。任何违反都按协议错误抛出，调用方不得静默降级。

    :param action: 当前动作。
    :param target_message_ids: 目标消息 ID 元组。
    :param quote_message_id: 可选的引用消息 ID。
    :param reaction: react 动作选中的表情回应标识；其他动作为 None。
    :param frame: 本回合固定快照。
    :raises IllegalActionError: DROP 帧带决策、动作超出动作空间、FORCE 场景
        silent、目标或引用越界、引用能力缺失、反应标识不在平台可用集内。
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
    if reaction is not None and reaction not in frame.capabilities.available_reactions:
        raise IllegalActionError(
            f'表情回应 {reaction} 不在本平台可用反应'
            f' {list(frame.capabilities.available_reactions)} 内'
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
    query: str | None = None
    reaction: str | None = None

    def __post_init__(self) -> None:
        """拒绝形状矛盾：未知动作、自由 reason_code、动作与负载不匹配。"""
        if self.action not in ALL_ACTIONS:
            raise IllegalActionError(f'未知动作：{self.action}')
        _validate_reason_codes(self.action, self.reason_codes)
        if self.action in COGNITIVE_ACTIONS:
            _validate_cognitive_shape(
                self.action,
                self.query,
                self.target_message_ids,
                self.quote_message_id,
            )
            if self.reply is not None:
                raise IllegalActionError(f'{self.action} 动作不能携带 reply 负载')
            return
        if self.query is not None:
            raise IllegalActionError(f'{self.action} 动作不能携带 query')
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
        if self.action == 'speak':
            if self.reply is None:
                raise IllegalActionError('speak 动作必须携带正文负载')
            if self.target_message_ids:
                raise IllegalActionError('speak 动作不能指定目标消息')
            if self.quote_message_id is not None:
                raise IllegalActionError('speak 动作不能携带引用')
        if self.action == 'wait':
            if self.reply is not None:
                raise IllegalActionError('wait 动作不能携带 reply 负载')
            if self.target_message_ids:
                raise IllegalActionError('wait 动作不能指定目标消息')
            if self.quote_message_id is not None:
                raise IllegalActionError('wait 动作不能携带引用')
        if self.action == 'poke':
            if self.reply is not None:
                raise IllegalActionError('poke 动作不能携带 reply 负载')
            if len(self.target_message_ids) != 1:
                raise IllegalActionError('poke 动作必须且只能指定一条目标消息')
            if self.quote_message_id is not None:
                raise IllegalActionError('poke 动作不能携带引用')
        if self.action == 'react':
            if self.reply is not None:
                raise IllegalActionError('react 动作不能携带 reply 负载')
            if len(self.target_message_ids) != 1:
                raise IllegalActionError('react 动作必须且只能指定一条目标消息')
            if self.quote_message_id is not None:
                raise IllegalActionError('react 动作不能携带引用')
            if self.reaction is None or not self.reaction.strip():
                raise IllegalActionError('react 动作必须声明非空 reaction')
        elif self.reaction is not None:
            raise IllegalActionError(f'{self.action} 动作不能携带 reaction')

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
            self.reaction,
            frame,
        )


@dataclass(frozen=True)
class DecisionHead:
    """动作头：正文流式输出前必须完整且通过校验的决策外壳。

    与 ConversationDecision 的区别是 reply 的正文此刻尚未产生：reply 动作
    用 length 声明篇幅，正文随后以 <say> 流式输出；silent 只存在动作
    头本身，其后不允许任何正文。认知动作（recall/inspect）同样只存在动作头，
    其后不允许任何正文——它产出的是回灌给模型的观察，不是可见产物。
    """

    action: ConversationAction
    target_message_ids: tuple[int, ...]
    quote_message_id: int | None
    reason_codes: tuple[str, ...]
    length: ReplyLength | None = None
    query: str | None = None
    reaction: str | None = None

    def __post_init__(self) -> None:
        """拒绝形状矛盾：未知动作、自由 reason_code、篇幅与动作不匹配。"""
        if self.action not in ALL_ACTIONS:
            raise IllegalActionError(f'未知动作：{self.action}')
        _validate_reason_codes(self.action, self.reason_codes)
        if self.action in COGNITIVE_ACTIONS:
            _validate_cognitive_shape(
                self.action,
                self.query,
                self.target_message_ids,
                self.quote_message_id,
            )
            if self.length is not None:
                raise IllegalActionError(f'{self.action} 动作不能声明回复篇幅')
            return
        if self.query is not None:
            raise IllegalActionError(f'{self.action} 动作不能携带 query')
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
        if self.action == 'speak':
            # 自主开口没有可回的消息，因此没有目标、没有引用；篇幅也不给：
            # 主动搭话本来就该短，多一个字段只多一种写错的方式。
            if self.target_message_ids:
                raise IllegalActionError('speak 动作不能指定目标消息')
            if self.quote_message_id is not None:
                raise IllegalActionError('speak 动作不能携带引用')
            if self.length is not None:
                raise IllegalActionError('speak 动作不能声明回复篇幅')
        if self.action == 'wait':
            if self.target_message_ids:
                raise IllegalActionError('wait 动作不能指定目标消息')
            if self.quote_message_id is not None:
                raise IllegalActionError('wait 动作不能携带引用')
            if self.length is not None:
                raise IllegalActionError('wait 动作不能声明回复篇幅')
        if self.action == 'poke':
            if len(self.target_message_ids) != 1:
                raise IllegalActionError('poke 动作必须且只能指定一条目标消息')
            if self.quote_message_id is not None:
                raise IllegalActionError('poke 动作不能携带引用')
            if self.length is not None:
                raise IllegalActionError('poke 动作不能声明回复篇幅')
        if self.action == 'react':
            if len(self.target_message_ids) != 1:
                raise IllegalActionError('react 动作必须且只能指定一条目标消息')
            if self.quote_message_id is not None:
                raise IllegalActionError('react 动作不能携带引用')
            if self.length is not None:
                raise IllegalActionError('react 动作不能声明回复篇幅')
            if self.reaction is None or not self.reaction.strip():
                raise IllegalActionError('react 动作必须声明非空 reaction')
        elif self.reaction is not None:
            raise IllegalActionError(f'{self.action} 动作不能携带 reaction')

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
            self.reaction,
            frame,
        )

    def to_decision(
        self,
        body_text: str,
        emoji_emotions: tuple[str, ...] = (),
    ) -> ConversationDecision:
        """结合流式正文组装完整决策。

        :param body_text: reply 动作的完整可见正文；silent、react 与认知动作
            忽略该参数，传入非空值视为协议错误。
        :return: 通过结构自检的 ConversationDecision。
        :raises IllegalActionError: silent/react/认知动作传入正文，或组装结果结构非法。
        """
        if self.action in COGNITIVE_ACTIONS:
            if body_text.strip() or emoji_emotions:
                raise IllegalActionError(f'{self.action} 动作头之后不能有正文或表情包')
            return ConversationDecision(
                action=self.action,
                target_message_ids=(),
                quote_message_id=None,
                reason_codes=self.reason_codes,
                reply=None,
                query=self.query,
            )
        if self.action == 'silent':
            if body_text.strip() or emoji_emotions:
                raise IllegalActionError('silent 动作头之后不能有正文或表情包')
            return ConversationDecision(
                action='silent',
                target_message_ids=(),
                quote_message_id=None,
                reason_codes=self.reason_codes,
                reply=None,
            )
        if self.action in ('react', 'poke', 'wait'):
            if body_text.strip() or emoji_emotions:
                raise IllegalActionError(f'{self.action} 动作头之后不能有正文或表情包')
            return ConversationDecision(
                action=self.action,
                target_message_ids=self.target_message_ids,
                quote_message_id=None,
                reason_codes=self.reason_codes,
                reply=None,
                reaction=self.reaction,
            )
        if not body_text.strip() and not emoji_emotions:
            raise IllegalActionError(f'{self.action} 动作头之后没有可见正文或表情包')
        return ConversationDecision(
            action=self.action,
            target_message_ids=self.target_message_ids,
            quote_message_id=self.quote_message_id,
            reason_codes=self.reason_codes,
            reply=ReplyPayload(
                text=body_text.strip(),
                length=self.length or 'brief',
                emoji_emotions=emoji_emotions,
            ),
        )


def available_actions(
    stream_kind: StreamKind,
    disposition: GateDisposition,
    capabilities: PlatformCapabilities,
    *,
    cognitive_rounds_left: int = 0,
    allow_wait: bool = False,
    allow_speak: bool = False,
) -> frozenset[ConversationAction]:
    """按 stream、平台能力与剩余认知轮次动态收窄动作空间。

    认知动作的预算完全由本函数表达：``cognitive_rounds_left`` 归零时它们直接
    不在返回集合里，模型再选就撞上 ``_validate_frame_choice`` 的动作空间校验，
    记为 ``illegal_action``。**不存在「预算耗尽就当 reply」这类降级路径**——
    末轮的约束写在动作集里，不写在异常处理里。

    :param stream_kind: 会话类型；用户发起的私聊与桌面交互都不允许 silent，
        主动追问使用独立的 ``reply / silent`` 决策帧。
    :param disposition: 门控态；DROP 不进入模型，动作集为空；群聊 FORCE
        （@必回）只允许 reply 作为终局动作，但仍可先检索再回。
    :param capabilities: 运行时真实具备的平台能力；只有已验证的 reaction
        支持才会让 react 进入动作集。
    :param cognitive_rounds_left: 本回合还剩几次认知动作机会；小于等于 0
        表示只能给出终局动作。
    :param allow_speak: 是否允许她起一个**不接任何人**的话头。它与 reply 的区别
        只在有没有目标：reply 是接某条消息，speak 是她自己想说点什么。
        **不需要独立的触发机制**——扩展触发口径（frequency / reply_necessity）
        本来就会在「群里热闹但没人理她」时给出候选，speak 只是让那个候选里多一个
        选项，而不是再造一条并行的唤起路径。
    :param allow_wait: 本批是否还可以「先等等」。同一批消息只允许等一次——
        第二次进来时 wait 直接不在动作集里，模型再选就是越界。**约束写在动作
        空间而不是循环计数器里**，与认知轮次预算同一种表达方式，因此不需要
        「连续等待上限」这类会与其它数互相牵制的常量。

    :return: 本轮允许模型选择的动作集合。
    """
    if disposition == 'drop':
        return frozenset()
    actions: set[ConversationAction]
    if stream_kind in ('desktop', 'direct') or disposition == 'force':
        actions = {'reply'}
    else:
        actions = {'reply', 'silent'}
        if capabilities.react:
            actions.add('react')
        if capabilities.poke:
            actions.add('poke')
        if allow_speak:
            actions.add('speak')
        # 等待只在群聊有意义；用户发起的私聊必须回复，主动追问则使用独立的
        # reply / silent 决策帧，两条路径都不能把同一条私聊重新排队。
        if allow_wait and stream_kind == 'group':
            actions.add('wait')
    # 认知动作与 stream 类型、门控态都无关：无论她最终要不要开口，
    # 「先想一下再决定」这件事在任何出口都成立，只受轮次预算约束。
    if cognitive_rounds_left > 0:
        actions.update(COGNITIVE_ACTIONS)
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
    ``silent_by_choice``（她考虑后选择沉默）、``cognitive_step``（她先去查了
    一下，本回合尚未结束）、``gate_dropped``（代码根本没让她考虑）、
    ``timeout`` / ``provider_error`` / ``parse_error`` / ``illegal_action`` /
    ``delivery_failed``（模型或投递故障）。模型失败与自主沉默绝不能混进同一个状态。

    一个回合可能落多条本事件：``snapshot_id`` 相同、``round_index`` 从 0 递增，
    前面若干条为 ``cognitive_step``，最后一条必为终局或失败状态。观察面板据此
    把一次思考链串起来，``latency_ms`` 逐轮记账、总时长由各轮相加得到。
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
    # ReAct 轮次序号，从 0 开始；单轮回合恒为 0，与本期之前的事件形状兼容。
    round_index: int = 0
    # 认知动作的观察结果摘要，已按 OBSERVATION_EVENT_MAX_CHARS 截断后写入账本。
    observation: str = ''

    def to_dict(self) -> dict[str, Any]:
        """组装四层审计字典，供 ``trace.emit('action_decision', ...)`` 使用。

        :return: 含 ``turnId`` / ``snapshotId`` / ``roundIndex`` / ``eventStatus`` /
            ``observation`` 与 ``inputs`` / ``gate`` / ``decision`` / ``version``
            四层的字典。
        """
        decision = None
        if self.decision is not None:
            decision = {
                'action': self.decision.action,
                'targetMessageIds': list(self.decision.target_message_ids),
                'quoteMessageId': self.decision.quote_message_id,
                'reasonCodes': list(self.decision.reason_codes),
                **(
                    {'query': self.decision.query}
                    if self.decision.query is not None
                    else {}
                ),
                **(
                    {'reaction': self.decision.reaction}
                    if self.decision.reaction is not None
                    else {}
                ),
                'reply': (
                    {
                        'text': self.decision.reply.text,
                        'length': self.decision.reply.length,
                        **(
                            {'emojiEmotions': list(self.decision.reply.emoji_emotions)}
                            if self.decision.reply.emoji_emotions
                            else {}
                        ),
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
            'roundIndex': self.round_index,
            'messageWatermark': self.turn_message_watermark,
            'eventStatus': self.event_status,
            'detail': self.detail,
            'observation': self.observation,
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
