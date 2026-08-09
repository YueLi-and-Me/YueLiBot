"""平台接入层共享的不可变消息与投递类型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Literal


PersonKind = Literal['contact', 'owner']
StreamKind = Literal['desktop', 'direct', 'group']


@dataclass(frozen=True)
class PersonRef:
    """已存在 person 的稳定引用。"""

    id: int
    kind: PersonKind
    first_seen_at: int


@dataclass(frozen=True)
class IdentityRef:
    """person 在某个平台上的稳定身份与展示名。"""

    platform: str
    external_id: str
    display_name: str


@dataclass(frozen=True)
class GroupMembershipRef:
    """person 在一个 QQ 群中的当前群名片。"""

    stream_id: int
    group_external_id: str
    group_card: str
    updated_at: int


@dataclass(frozen=True)
class StreamRef:
    """已存在 stream 的稳定引用。"""

    id: int
    platform: str
    kind: StreamKind
    external_id: str


@dataclass(frozen=True)
class ConversationContext:
    """一条消息的说话场所、发送人与关系性信号判据。"""

    stream: StreamRef
    person: PersonRef
    identity: IdentityRef | None = None
    group_card: str = ''

    @property
    def relationship_signals_enabled(self) -> bool:
        """关系性信号只面向 owner，与消息来自哪个平台无关。"""
        return self.person.kind == 'owner'


@dataclass(frozen=True)
class InboundMessage:
    """已完成归属解析的一条入站消息。"""

    text: str
    context: ConversationContext
    mentioned_me: bool = False
    external_message_id: str | None = None
    bot_name: str | None = None


@dataclass(frozen=True)
class OutboundMessage:
    """待投递的非桌面回复，保留按 <say> 切好的原始分句。"""

    stream: StreamRef
    segments: List[str]


@dataclass(frozen=True)
class DeliveryReceipt:
    """一次平台投递的可追踪结果。"""

    platform: str
    stream_id: int
    external_message_ids: List[str]
