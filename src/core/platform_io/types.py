"""定义平台接入层共享的不可变引用、上下文、消息和投递回执。

这些数据类只描述已经完成归属解析的数据，不负责数据库读写、协议解析或消息
发送。``StreamRegistry`` 创建的引用由本模块的数据类承载，并在主体服务、平台
驱动和观察事件之间传递。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Literal


PersonKind = Literal['contact', 'owner']
StreamKind = Literal['desktop', 'direct', 'group']


@dataclass(frozen=True)
class PersonRef:
    """已存在人物的稳定数据库引用。

    ``kind`` 区分普通联系人和配置确定的 owner；``first_seen_at`` 使用统一的
    毫秒时间戳。
    """

    id: int
    kind: PersonKind
    first_seen_at: int


@dataclass(frozen=True)
class IdentityRef:
    """人物在某个平台上的外部身份和当前展示名。"""

    platform: str
    external_id: str
    display_name: str


@dataclass(frozen=True)
class GroupMembershipRef:
    """人物在一个 QQ 群会话中的当前群名片记录。"""

    stream_id: int
    group_external_id: str
    group_card: str
    updated_at: int


@dataclass(frozen=True)
class StreamRef:
    """已存在会话分区的稳定数据库引用。"""

    id: int
    platform: str
    kind: StreamKind
    external_id: str


@dataclass(frozen=True)
class ConversationContext:
    """一条入站消息的会话、人物、平台身份和关系信号判据。"""

    stream: StreamRef
    person: PersonRef
    identity: IdentityRef | None = None
    group_card: str = ''

    @property
    def relationship_signals_enabled(self) -> bool:
        """判断上下文是否允许应用 owner 关系信号。

        :return: ``person.kind`` 为 ``owner`` 时返回 ``True``，与消息来源平台无关。
        """
        return self.person.kind == 'owner'


@dataclass(frozen=True)
class InboundMessage:
    """已完成会话与人物归属解析的入站消息。"""

    text: str
    context: ConversationContext
    mentioned_me: bool = False
    external_message_id: str | None = None
    bot_name: str | None = None
    image_sources: tuple[str, ...] = ()
    emoji_sources: tuple[str, ...] = ()
    emoji_sub_types: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        """校验表情包来源与协议子类型逐项对齐。"""

        if len(self.emoji_sources) != len(self.emoji_sub_types):
            raise ValueError('入站表情包来源与 sub_type 数量必须一致')


@dataclass(frozen=True)
class OutboundMessage:
    """待投递的非桌面回复，包含文本分句和可选表情包图片引用。"""

    stream: StreamRef
    segments: List[str]
    emoji_refs: tuple[str, ...] = ()
    emoji_sub_types: tuple[int, ...] = ()
    # 每个发送批次（每条文字一批、每张表情包一批）发出前的停顿，按「文字在前、
    # 表情包在后」的顺序逐项对齐；首项恒为 0，因为模型生成本身已经占用了十几秒。
    # 节奏由主体按人格配置算好，适配器只负责照做，避免打字速度这类角色行为参数
    # 散落到各平台适配器里各算一套。
    batch_delays_ms: tuple[int, ...] = ()
    # 第一条气泡要引用的平台消息编号；为空表示不引用。
    # 群里消息滚动快、她生成又要十几秒，不带指向的回复落地时已经被别的话题冲开，
    # 旁观者看不出在回谁。引用与否由投递层按「目标之后是否已有人插话」判定，
    # 不进入模型的动作头，避免多一个可写错的协议字段。
    quote_external_message_id: str | None = None

    def __post_init__(self) -> None:
        """校验表情包引用与协议子类型对齐，以及停顿覆盖全部发送批次。"""

        if len(self.emoji_refs) != len(self.emoji_sub_types):
            raise ValueError('出站表情包引用与 sub_type 数量必须一致')
        if self.batch_delays_ms and (
            len(self.batch_delays_ms) != len(self.segments) + len(self.emoji_refs)
        ):
            raise ValueError('出站发送批次与打字停顿数量必须一致')


@dataclass(frozen=True)
class OutboundReaction:
    """待投递的一次表情回应：给某条已有消息贴一个表情，不产生新消息。

    与 :class:`OutboundMessage` 分开而不是塞进它的可选字段，是因为两者在平台上是
    完全不同的动作（发消息 vs 给消息贴表情），共用一个类型会让「segments 为空但
    reaction 非空」这种半合法状态成为常态，校验只能靠约定。

    :ivar stream: 目标会话；表情回应目前只在群聊有意义。
    :ivar target_external_message_id: 被回应消息的平台编号。内部消息 ID 发不出去，
        必须已经回填过平台编号；没有编号的历史消息不可被回应。
    :ivar reaction: 语义反应标识，取自协议的封闭词表；平台编号由适配器映射。
    """

    stream: StreamRef
    target_external_message_id: str
    reaction: str

    def __post_init__(self) -> None:
        """拒绝空目标编号与空反应标识，避免空洞进入平台调用。"""
        if not self.target_external_message_id.strip():
            raise ValueError('表情回应必须指定被回应消息的平台编号')
        if not self.reaction.strip():
            raise ValueError('表情回应标识不能为空')


@dataclass(frozen=True)
class OutboundPoke:
    """待投递的一次戳一戳：戳某个人，不产生消息也不贴在某条消息上。

    与 :class:`OutboundReaction` 分开的理由同样是「平台上是两个不同动作」：
    表情回应贴在**消息**上，戳一戳作用在**人**上，目标空间根本不同。

    :ivar stream: 目标会话；戳一戳目前只在群聊开放。
    :ivar target_external_id: 被戳者在该平台的外部标识（QQ 号）。
    """

    stream: StreamRef
    target_external_id: str

    def __post_init__(self) -> None:
        """拒绝空目标标识，避免空洞进入平台调用。"""
        if not self.target_external_id.strip():
            raise ValueError('戳一戳必须指定被戳者的平台标识')


@dataclass(frozen=True)
class DeliveryReceipt:
    """一次平台投递的可追踪结果。

    ``external_message_ids`` 允许具体驱动回填平台返回的消息编号；不支持编号
    的通道返回空列表。
    """

    platform: str
    stream_id: int
    external_message_ids: List[str]
