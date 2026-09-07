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
- ``available_actions``：按 stream、平台能力与剩余认知轮次动态收窄动作空间——
  剩余轮次归零时认知动作直接不在动作集里，模型再选就是越界，不存在「预算耗尽降级」路径；
- ``GateInputFacts`` / ``ActionDecisionEvent``：四层审计事件，
  ``to_dict`` 生成可供 trace 使用的可序列化字典。

依赖：仅标准库与 ``src.core.platform_io.types`` 的 ``StreamKind`` 别名；
被 Conversation Agent 与 ``src.core.services.chat`` 消费，不反向依赖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Literal, Tuple

from src.core.platform_io.types import StreamKind

# 终局动作：产出可见产物或明确结束本回合；react 仅当平台适配器已验证真实执行能力时开放。
# 认知动作：不产生任何可见产物，执行后把观察结果回灌给模型并再发起一轮（ReAct 回环）。
ConversationAction = Literal[
    'reply', 'silent', 'react', 'poke', 'wait', 'speak', 'recall', 'inspect', 'consult',
]
TERMINAL_ACTIONS: frozenset[ConversationAction] = frozenset({
    'reply', 'silent', 'react', 'poke', 'wait', 'speak',
})
COGNITIVE_ACTIONS: frozenset[ConversationAction] = frozenset({'recall', 'inspect', 'consult'})
# 需要产出可见正文的终局动作。决策与表达拆分后，仅这两个动作需要二次调用回复生成模型；
# silent / wait / react / poke 在决策调用内结束。
SPEAKING_ACTIONS: frozenset[ConversationAction] = frozenset({'reply', 'speak'})
ALL_ACTIONS: frozenset[ConversationAction] = TERMINAL_ACTIONS | COGNITIVE_ACTIONS
ReplyLength = Literal['brief', 'long']
# 三态门控的态；门控判定本身在 conversation_gate 模块实现。
GateDisposition = Literal['drop', 'force', 'deliberate']
# 行动事件状态。committed / silent_by_choice / cognitive_step 为 Agent 自主结论，
# gate_dropped 及其余为失败或拦截状态；两类禁止合并。
# cognitive_step 表示本回合以一次认知检索结束，尚未给出终局动作。
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

# 回复理由码：封闭枚举。扩充时必须同步修改本表与校验逻辑。
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

# 沉默理由码：封闭枚举。
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

# 等待理由码：封闭枚举。与沉默分域：silent 表示放弃本批消息，wait 表示等待后续消息
# 后合并处理；分域保证审计事件可区分两种行为。
WAIT_REASON_CODES: frozenset[str] = frozenset({
    'unfinished_thought',
    'thread_developing',
})

# 主动发言理由码：封闭枚举。与回复分域：reply 由入站消息触发，speak 为自主发起；
# 分域保证审计事件可区分两类动机。
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

# 表情回应语义词表：模型只输出语义名，平台编号由适配器映射。语义名进入协议而非
# 平台编号：表情选择属于角色行为，编号属于平台实现，二者解耦后更换平台无需修改提示词。
#
# 名称必须逐字取自平台表情表，禁止自创近义词。
#
# - 现象：首版按印象命名，「惊讶」被映射到 26 号（实际为「惊恐」），
#   「无语」在平台表情表中不存在。
# - 原因：语义名与平台编号需保持逐字对应，自创名称即失去比对基准。
# - 后果：错配不报错，仅表现为发送了错误表情。
#
# 仅保留六个条目，覆盖六种社交意图（认可 / 觉得好笑 / 服了 / 喜欢 / 没想到 / 围观）；
# 候选过多会降低模型选择的速度与准确性。扩充时必须显式修改本表。
REACTION_IDS: tuple[str, ...] = ('赞', '笑哭', '无奈', '爱心', '惊讶', '吃瓜')

# 非法取值进错误信息时的保留长度（字符）。取值本该是十余字符的标识符，超出即
# 说明模型写的是自由文本；截断只影响错误信息展示，原值仍在请求存档中。
_CODE_QUOTE_LIMIT = 24


def _quote_code(code: str) -> str:
    """把模型给出的取值渲染成适合进错误信息的短引用。

    自由字符串常是一整句理由散文，原样拼进错误信息会挤爆控制台错误框，也让
    纠错回灌里「哪一段是非法取值」失去边界；完整原值仍保留在 data/logs/prompt
    的请求存档中，诊断不受影响。

    :param code: 模型给出的原始取值。
    :return: 折叠空白并在超长时截断的带引号文本。
    """
    flat = ' '.join(code.split())
    return f'「{flat}」' if len(flat) <= _CODE_QUOTE_LIMIT else f'「{flat[:_CODE_QUOTE_LIMIT]}…」'


def _validate_reason_codes(
    action: ConversationAction,
    reason_codes: tuple[str, ...],
) -> None:
    """校验理由码的形状与动作分域，完整决策与动作头共用。

    认知动作（recall/inspect/consult）不参与本校验：理由码服务于回复/沉默决策的审计，
    认知动作的审计信息是 query 本身；强制要求不承载信息的字段会增加生成侧格式
    负担并抬高 parse_error 率，该格式经过完整影子阶段验证后才收敛为此形态。

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
    # react 与 poke 属于回应行为，理由码与回复同域；沉默与等待各自独立分域。
    if action == 'silent':
        domain = SILENT_REASON_CODES
    elif action == 'wait':
        domain = WAIT_REASON_CODES
    elif action == 'speak':
        domain = SPEAK_REASON_CODES
    else:
        domain = REPLY_REASON_CODES
    # 错误信息必须带上本动作的可用取值：这条文案会被工具调用纠错原样回灌给模型，
    # 只说「不允许自由字符串」等于让它再猜一次，而模型这类错误恰恰是不知道该填什么。
    available = f'{action} 可用取值：{"、".join(sorted(domain))}'
    for code in reason_codes:
        if code not in ALL_REASON_CODES:
            raise IllegalActionError(
                f'未知 reason_code：{_quote_code(code)}'
                f'（封闭枚举，不允许自由字符串）；{available}'
            )
        if code not in domain:
            raise IllegalActionError(
                f'reason_code {_quote_code(code)} 不能与动作 {action} 组合；{available}'
            )


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
    回复，也不得记录成「Bot 选择沉默」。
    """


@dataclass(frozen=True)
class PlatformCapabilities:
    """运行时按当前 stream 与平台适配器真实具备的能力。

    模型只能在这些真实能力内选择：``quote`` 关闭时决策不能携带
    ``quote_message_id``；平台未验证 reaction 执行能力时 ``react`` 不进入
    动作集，且可用反应标识封闭给出；``emoji`` 表示当前平台、表情包库和
    频率窗口共同允许产生表情包可见产物；``plugin_capabilities`` 汇总工具插件
    按会话贡献的能力名（例如 ``forward_message`` 表示当前会话有可供只读工具
    逐层展开的合并转发缓存），不改变动作集。

    ``quote`` 只管「模型能否自己指定引用目标」。QQ 群聊投递时按目标消息是否
    已被后续发言冲开自动挂引用，那条路径由代码强制，不受本开关影响——目标已经
    由模型选定，引用只是这个选择在平台上的呈现方式。
    """

    quote: bool = False
    react: bool = False
    available_reactions: tuple[str, ...] = ()
    emoji: bool = False
    poke: bool = False
    # 插件按会话贡献的能力名集合。它只控制外部只读工具声明，不改变终局动作集，
    # 也不表示平台本身具备同名协议能力。
    plugin_capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        """拒绝空反应标识，防止资源 ID 空洞进入动作集。"""
        if self.react and not self.available_reactions:
            raise ValueError('react 能力开启时必须给出可用的反应标识')
        for reaction_id in self.available_reactions:
            if not reaction_id.strip():
                raise ValueError('可用反应标识不能是空字符串')

    def tool_capabilities(self) -> FrozenSet[str]:
        """返回可用于外部工具过滤的显式能力名集合。"""
        return self.plugin_capabilities


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
        因此「回合固定快照」这条性质不被多轮破坏：变的只有 Bot 这一轮还能选什么。

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
    头本身，其后不允许任何正文。认知动作（recall/inspect/consult）同样只存在动作头，
    其后不允许任何正文——它产出的是回灌给模型的观察，不是可见产物。
    """

    action: ConversationAction
    target_message_ids: tuple[int, ...]
    quote_message_id: int | None
    reason_codes: tuple[str, ...]
    length: ReplyLength | None = None
    query: str | None = None
    reaction: str | None = None
    # 决策层写给回复生成层的背景说明：发言动机、方向与相关前因。封闭理由码仅用于
    # 审计，信息量不足以为独立回复生成调用提供上下文，因此保留此自由文本字段。
    # 该字段不是正文，不得作为正文输出。
    reference: str | None = None

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
            # 自主发言没有可回应的消息，不携带 targets 与 quote；篇幅固定为短，
            # 不提供 length 字段以减少出错面。
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
    记为 ``illegal_action``。不存在「预算耗尽自动 reply」的降级路径：
    末轮的约束写在动作集里，不写在异常处理里。

    :param stream_kind: 会话类型；用户发起的私聊与桌面交互都不允许 silent，
        主动追问使用独立的 ``reply / silent`` 决策帧。
    :param disposition: 门控态；DROP 不进入模型，动作集为空；群聊 FORCE
        （@必回）只允许 reply 作为终局动作，但仍可先检索再回。
    :param capabilities: 运行时真实具备的平台能力；只有已验证的 reaction
        支持才会让 react 进入动作集。
    :param cognitive_rounds_left: 本回合还剩几次认知动作机会；小于等于 0
        表示只能给出终局动作。
    :param allow_speak: 是否允许 Bot 主动发起一个不回应任何人的话题。它与 reply 的区别
        只在有没有目标：reply 是回应某条消息，speak 是 Bot 自己想说点什么。
        不需要独立的触发机制：扩展触发口径（frequency / reply_necessity）
        本来就会在「群里热闹但没人理 Bot」时给出候选，speak 只是让那个候选里多一个
        选项，而不是再造一条并行的唤起路径。
    :param allow_wait: 本批是否还可以「先等等」。同一批消息只允许等一次——
        第二次进来时 wait 直接不在动作集里，模型再选就是越界。约束写在动作
        空间而不是循环计数器里，与认知轮次预算同一种表达方式，因此不需要
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
    # 等待在群聊与私聊均可用：对方将一句话拆成多条连发时，等待合并后统一回应
    # 比逐条回复更合理。三条边界：群聊 @必回要求立即回应；私聊门控只约束
    # 「最终必须表态」，等待由服务层超时兜底；桌面为即时交互，不排队。
    if allow_wait and stream_kind != 'desktop':
        if not (disposition == 'force' and stream_kind == 'group'):
            actions.add('wait')
    # 认知动作与 stream 类型、门控态无关：任一会话出口都允许先检索再决策，
    # 仅受轮次预算约束。
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

    ``event_status`` 必须区分：``committed``（Bot 考虑后选择行动）、
    ``silent_by_choice``（Bot 考虑后选择沉默）、``cognitive_step``（Bot 先去查了
    一下，本回合尚未结束）、``gate_dropped``（代码根本没让 Bot 考虑）、
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
    # 外部工具调用与动作决策互斥；仅工具轮填写，保持既有动作事件形状不变。
    tool_name: str = ''
    tool_call_id: str = ''
    tool_arguments: Dict[str, Any] = field(default_factory=dict)
    # 当前轮真实下发给模型的外部工具，便于判断“模型没用”还是“根本没声明”。
    available_tools: Tuple[str, ...] = ()

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
        payload: Dict[str, Any] = {
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
        if self.available_tools:
            payload['gate']['availableTools'] = list(self.available_tools)
        if self.tool_name:
            payload['toolInvocation'] = {
                'name': self.tool_name,
                'callId': self.tool_call_id,
                'arguments': dict(self.tool_arguments),
            }
        return payload
