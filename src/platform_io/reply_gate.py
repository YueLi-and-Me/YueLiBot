"""群聊回复门控：纯函数，供入口在接线时记录决策。"""

from __future__ import annotations

from dataclasses import dataclass

from src.platform_io.types import StreamKind


@dataclass(frozen=True)
class ReplyGateDecision:
    """一次确定性的群聊回复决策。"""

    accepted: bool
    reason: str


def decide_reply(
    stream_kind: StreamKind,
    asleep: bool,
    mentioned_me: bool,
    my_replies_in_window: int,
    max_replies_in_window: int,
) -> ReplyGateDecision:
    """只在群聊被叫到且未超过硬频率上限时回复。"""
    if stream_kind != 'group':
        return ReplyGateDecision(accepted=True, reason='not_group')
    if asleep and not mentioned_me:
        return ReplyGateDecision(accepted=False, reason='asleep_without_mention')
    if my_replies_in_window >= max_replies_in_window:
        return ReplyGateDecision(accepted=False, reason='window_limit')
    if mentioned_me:
        return ReplyGateDecision(accepted=True, reason='mentioned')
    return ReplyGateDecision(accepted=False, reason='group_not_mentioned')
