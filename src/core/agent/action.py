"""定义一轮对话在上下文完成后的可选动作决策。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Literal, Protocol, Tuple


ActionKind = Literal['reply', 'silent']


@dataclass(frozen=True)
class TurnAction:
    """一轮对话选择的动作及其可观测理由。"""

    action: ActionKind
    reason: str

    def __post_init__(self) -> None:
        """拒绝未声明动作和不可观测的空理由。"""
        if self.action not in ('reply', 'silent'):
            raise ValueError(f'未知回合动作：{self.action}')
        if not self.reason.strip():
            raise ValueError('回合动作理由不能为空')


@dataclass(frozen=True)
class ActionContext:
    """动作策略可读取的已组织回合上下文。"""

    turn_id: int
    stream_id: int
    messages: Tuple[Dict[str, Any], ...]


class ActionPolicy(Protocol):
    """根据已组织上下文选择本轮动作的窄协议。"""

    async def decide(self, context: ActionContext) -> TurnAction:
        """返回本轮应执行的动作。"""
        ...


class AlwaysReplyPolicy:
    """保持现有行为的默认动作策略。"""

    async def decide(self, context: ActionContext) -> TurnAction:
        """始终选择回复，不读取或修改上下文。"""
        return TurnAction(action='reply', reason='默认策略始终回复')
