"""主动搭话的小本本：只记意图与时间，不冻结旧情境或视觉描述。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class IntentType(IntEnum):
    """值即投放优先级；数值越大，越应先被尝试投放。"""

    Idle = 0
    Scene = 1
    Plan = 2
    # 保留原始持久化值，避免已写入 SQLite 的约定在升级后被当成畸形数据丢弃。
    Promise = 5

    @property
    def ttl_ms(self) -> int:
        """每种念头的新鲜期；与轮询间隔不存在大小依赖。"""
        match self:
            case IntentType.Idle:
                return 15 * 60_000
            case IntentType.Scene:
                return 5 * 60_000
            case IntentType.Plan:
                return 20 * 60_000
            case IntentType.Promise:
                return 2 * 60 * 60_000
        raise ValueError(f'未知意图类型：{self}')


@dataclass(frozen=True)
class PendingIntent:
    intent_type: IntentType
    earliest_at: int
    expires_at: int
    activity: str
    wants_vision: bool
    subject: str = ''


def stash(pending: list[PendingIntent], intent: PendingIntent) -> list[PendingIntent]:
    """同类型只留最早的那一条，避免一个瞬间的反复事件攒成连发。"""
    for existing in pending:
        if existing.intent_type == intent.intent_type:
            if existing.earliest_at <= intent.earliest_at:
                return pending
            return [intent if item is existing else item for item in pending]
    return [*pending, intent]


def eligible_intents(pending: list[PendingIntent], now: int) -> tuple[
    list[PendingIntent], list[PendingIntent], list[PendingIntent],
]:
    """剔除过期项并筛出已到点候选；调用者再按闸门决定是否真正投放。"""
    remaining: list[PendingIntent] = []
    expired: list[PendingIntent] = []
    candidates: list[PendingIntent] = []
    for intent in pending:
        if now > intent.expires_at:
            expired.append(intent)
            continue
        remaining.append(intent)
        if now >= intent.earliest_at:
            candidates.append(intent)
    candidates.sort(key=lambda item: item.intent_type, reverse=True)
    return remaining, candidates, expired
