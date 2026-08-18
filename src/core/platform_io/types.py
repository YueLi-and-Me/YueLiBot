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

    def __post_init__(self) -> None:
        """校验表情包引用与协议子类型逐项对齐。"""

        if len(self.emoji_refs) != len(self.emoji_sub_types):
            raise ValueError('出站表情包引用与 sub_type 数量必须一致')


@dataclass(frozen=True)
class DeliveryReceipt:
    """一次平台投递的可追踪结果。

    ``external_message_ids`` 允许具体驱动回填平台返回的消息编号；不支持编号
    的通道返回空列表。
    """

    platform: str
    stream_id: int
    external_message_ids: List[str]
