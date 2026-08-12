"""定义主动搭话的待办意图及其新鲜期管理。

意图只保存类型、时间、活动和主题，不保存旧的屏幕描述；调用方在真正投放前
必须重新读取当前情境并完成预算校验。
"""

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
        """返回当前意图类型的有效期，单位为毫秒。

        Returns:
            ``Idle`` 为 15 分钟、``Scene`` 为 5 分钟、``Plan`` 为 20 分钟、
            ``Promise`` 为 2 小时对应的毫秒数。

        Raises:
            ValueError: 枚举实例不属于已支持的意图类型。

        Side Effects:
            仅读取枚举值，不修改意图或全局状态。
        """
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
    """表示一条等待到期或等待预算允许的主动搭话意图。

    :ivar intent_type: 意图类型及其投放优先级。
    :ivar earliest_at: 最早可投放的 Unix 毫秒时间戳。
    :ivar expires_at: 超过此时间后意图失效的 Unix 毫秒时间戳。
    :ivar activity: 产生意图时的活动类别。
    :ivar wants_vision: 是否需要在投放前重新获取视觉信息。
    :ivar subject: 可选主题文本，默认值为空字符串。
    """

    intent_type: IntentType
    earliest_at: int
    expires_at: int
    activity: str
    wants_vision: bool
    subject: str = ''


def stash(pending: list[PendingIntent], intent: PendingIntent) -> list[PendingIntent]:
    """将一条待投放意图加入列表，并确保同类型只保留最早的候选。

    Args:
        pending: 当前待处理意图列表。
        intent: 待加入的新意图。

    Returns:
        不含同类型较晚意图的新列表；若现有同类型意图更早，则返回原列表对象。

    Raises:
        TypeError: 列表项不是 ``PendingIntent`` 或时间字段不可比较时抛出。

    Side Effects:
        不修改列表中的现有对象；在新意图更早时创建替换后的列表。
    """
    for existing in pending:
        if existing.intent_type == intent.intent_type:
            if existing.earliest_at <= intent.earliest_at:
                return pending
            return [intent if item is existing else item for item in pending]
    return [*pending, intent]


def eligible_intents(pending: list[PendingIntent], now: int) -> tuple[
    list[PendingIntent], list[PendingIntent], list[PendingIntent],
]:
    """剔除过期项并筛出已到点候选；调用方随后执行投放资格校验。

    Args:
        pending: 当前等待处理的意图列表。
        now: 当前 Unix 毫秒时间戳。

    Returns:
        依次为未过期意图、已到最早投放时间的候选和本次剔除的过期意图。

    Side Effects:
        不修改输入列表；候选列表按 ``IntentType`` 数值从高到低排序。
    """
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
