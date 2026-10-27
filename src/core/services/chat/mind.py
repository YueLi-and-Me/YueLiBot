"""规划器心理活动的跨回合记录。

规划器的每个终局动作都带一两句心理活动（``mind``）：这几条是谁在对谁说、她怎么
看、打算怎么做。本模块把真正生效的那些按会话存进 ``meta`` 表（键
``mind:{stream_id}``），供后续回合按时间插回决策层看到的聊天记录，使她能看到自己
前几轮的判断，而不是每批消息都从零猜一遍。

记录只存在 ``meta`` 表，不进 ``messages`` 表：事实抽取、摘要、表达学习、黑话挖掘、
人物画像与场景观察都只读 ``messages`` 表，心理活动由此在构造上不会流入这些后台任务。

由 ``ChatService`` 继承，依赖它的 ``memory``、``_working_memory_messages`` 与
``_impression_name``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Mapping

from src.core.agent.conversation import AgentOutcome
from src.core.platform_io.types import ConversationContext

# 生效后是在回应某条消息的动作；它们的目标消息记作「她已经回应过的消息」。
_RESPONDING_ACTIONS = frozenset({'reply', 'react', 'poke'})


def mind_key(stream_id: int) -> str:
    """返回某会话心理活动记录在 meta 表里的键名。

    :param stream_id: 会话 ID。
    :return: 形如 ``mind:{stream_id}`` 的键名。
    """
    return f'mind:{stream_id}'


@dataclass(frozen=True)
class MindEntry:
    """一次已生效终局动作的心理活动。

    :ivar at: 动作头通过校验的 Unix 毫秒时刻，用于按时间插回聊天记录。
    :ivar mind: 心理活动原文。
    :ivar action: 终局动作名。
    :ivar target_message_id: 目标消息 ID；speak / silent / wait 没有目标，为 None。
    :ivar target_name: 目标消息发送者在本会话的显示名；没有目标时为 None。
    :ivar turn_id: 所属回合 ID。
    """

    at: int
    mind: str
    action: str
    target_message_id: int | None
    target_name: str | None
    turn_id: int

    def to_dict(self) -> Dict[str, Any]:
        """转换为写入 meta 表的驼峰字段字典。"""
        return {
            'at': self.at,
            'mind': self.mind,
            'action': self.action,
            'targetMessageId': self.target_message_id,
            'targetName': self.target_name,
            'turnId': self.turn_id,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> 'MindEntry':
        """从 meta 表里的字典还原一条记录。

        :param raw: ``to_dict`` 写入的字典。
        :return: 还原后的记录。
        :raises KeyError: 缺少字段，说明记录不是本模块写入的形状。
        """
        return cls(
            at=raw['at'],
            mind=raw['mind'],
            action=raw['action'],
            target_message_id=raw['targetMessageId'],
            target_name=raw['targetName'],
            turn_id=raw['turnId'],
        )

    def action_label(self) -> str:
        """给出聊天记录里心理活动行末尾括号内的动作描述。

        :return: 例如「回复了凌白」「没有说话」。
        :raises ValueError: 动作不属于终局动作。
        """
        if self.action == 'reply':
            return f'回复了{self.target_name}'
        if self.action == 'speak':
            return '主动说了一句'
        if self.action == 'silent':
            return '没有说话'
        if self.action == 'wait':
            return '先等等'
        if self.action == 'react':
            return f'给{self.target_name}的消息贴了表情'
        if self.action == 'poke':
            return f'戳了{self.target_name}'
        raise ValueError(f'心理活动记录里出现了非终局动作：{self.action}')


def render_mind_line(entry: MindEntry, stamp: str, bot_name: str) -> str:
    """把一条心理活动渲染成决策层聊天记录里的一行。

    行首用 ``[我·心里]`` 而不是消息编号：它不是可回应的消息，不能被当成目标；
    时间戳与消息行同一格式，由调用方按同一套跨天规则给出。

    :param entry: 心理活动记录。
    :param stamp: 已按聊天记录口径格式化的时间戳，例如 ``11:47``。
    :param bot_name: Bot 在聊天记录里的显示名。
    :return: 形如 ``[我·心里] 11:47 月璃心里想：……（没有说话）`` 的一行。
    """
    return f'[我·心里] {stamp} {bot_name}心里想：{entry.mind}（{entry.action_label()}）'


class MindLogMixin:

    def _record_mind(
        self,
        context: ConversationContext,
        turn: int,
        outcome: AgentOutcome,
        senders: Mapping[int, int],
    ) -> None:
        """在终局动作真正生效后写入一条心理活动。

        只在生效之后调用：reply / speak / react / poke 投递成功、silent 落定、wait
        退回批次。模型或协议失败、shadow 决策、被打断作废的轮次与认知轮都不调用，
        它们不是她这一轮实际做了的事。

        列表上限取 ``conversation.working_memory_messages``：渲染只取落在工作记忆
        时间跨度内的条目，每条至少对应一批消息，超出这个数的旧条目必然落在窗口外。

        :param context: 当前会话上下文。
        :param turn: 回合 ID。
        :param outcome: 已生效的 Agent 结果，必须带终局决策与校验时刻。
        :param senders: 本回合可选消息 ID 到发送者人物 ID 的映射，用于反查目标
            消息的发送者显示名。
        :raises KeyError: 目标消息不在 ``senders`` 内。
        :raises ValueError: 目标消息的发送者在本会话没有显示名。
        副作用：读写 meta 表 ``mind:{stream_id}``。
        """
        decision = outcome.decision
        assert decision is not None and decision.mind is not None
        assert outcome.decided_at is not None
        target_id = (
            decision.target_message_ids[0] if decision.target_message_ids else None
        )
        entry = MindEntry(
            at=outcome.decided_at,
            mind=decision.mind,
            action=decision.action,
            target_message_id=target_id,
            target_name=(
                self._impression_name(senders[target_id], context)
                if target_id is not None
                else None
            ),
            turn_id=turn,
        )
        key = mind_key(context.stream.id)
        entries: List[Dict[str, Any]] = [*self.memory.read_json(key, []), entry.to_dict()]
        self.memory.write_json(key, entries[-self._working_memory_messages:])

    def _minds_since(self, stream_id: int, since: int) -> List[MindEntry]:
        """读取某会话不早于给定时刻的心理活动，按时间正序。

        :param stream_id: 会话 ID。
        :param since: Unix 毫秒时刻，通常是本回合工作记忆最早一条消息的时间。
        :return: ``at`` 不早于 ``since`` 的记录。
        :raises KeyError: meta 表里的记录形状不对。
        副作用：只读 meta 表。
        """
        entries = [
            MindEntry.from_dict(raw)
            for raw in self.memory.read_json(mind_key(stream_id), [])
        ]
        return sorted(
            (entry for entry in entries if entry.at >= since),
            key=lambda entry: entry.at,
        )

    def _responded_message_ids(self, stream_id: int) -> FrozenSet[int]:
        """返回她在本会话已生效地回应过的消息 ID（回复、贴表情、戳一戳的目标）。

        :param stream_id: 会话 ID。
        :return: 心理活动记录覆盖范围内被回应过的消息 ID 集合。
        :raises KeyError: meta 表里的记录形状不对。
        副作用：只读 meta 表。
        """
        return frozenset(
            raw['targetMessageId']
            for raw in self.memory.read_json(mind_key(stream_id), [])
            if raw['action'] in _RESPONDING_ACTIONS
        )
