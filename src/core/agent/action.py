"""定义一轮对话在上下文完成后的可选动作决策。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Literal, Protocol, Tuple

import random

from src.core.common.clock import now as current_time


ActionKind = Literal['reply', 'silent']
ReplyLength = Literal['brief', 'long']


@dataclass(frozen=True)
class TurnAction:
    """一轮对话选择的动作及其可观测理由。"""

    action: ActionKind
    reason: str
    length: ReplyLength | None = None

    def __post_init__(self) -> None:
        """拒绝未声明动作、空理由和自相矛盾的篇幅。"""
        if self.action not in ('reply', 'silent'):
            raise ValueError(f'未知回合动作：{self.action}')
        if not self.reason.strip():
            raise ValueError('回合动作理由不能为空')
        if self.action == 'reply' and self.length not in ('brief', 'long'):
            raise ValueError('回复动作必须声明有效篇幅')
        if self.action == 'silent' and self.length is not None:
            raise ValueError('静默动作不能声明回复篇幅')


@dataclass(frozen=True)
class ActionContext:
    """动作策略可读取的已组织回合上下文。"""

    turn_id: int
    stream_id: int
    messages: Tuple[Dict[str, Any], ...]
    stream_kind: str = ''


class ActionPolicy(Protocol):
    """根据已组织上下文选择本轮动作的窄协议。"""

    async def decide(self, context: ActionContext) -> TurnAction:
        """返回本轮应执行的动作。"""
        ...


class AlwaysReplyPolicy:
    """保持现有行为的默认动作策略。"""

    async def decide(self, context: ActionContext) -> TurnAction:
        """始终选择回复，不读取或修改上下文。"""
        return TurnAction(action='reply', reason='默认策略始终回复', length='brief')


class PresenceActionPolicy:
    """根据最近窗口内主体发言占比平滑降低回复概率。"""

    def __init__(
        self,
        *,
        base_probability: float,
        decay_strength: float,
        window_minutes: int,
        assistant_reply_count_since: Callable[[int, int], int],
        message_count_since: Callable[[int, int], int],
        probability_draw: Callable[[], float] = random.random,
        clock: Callable[[], int] = current_time,
    ) -> None:
        """保存概率曲线及平台中立的窗口统计依赖。

        :raises ValueError: 概率、衰减强度或窗口长度超出约定范围。
        """
        if not 0.0 <= base_probability <= 1.0:
            raise ValueError('基础回复概率必须在 0 到 1 之间')
        if decay_strength < 0.0:
            raise ValueError('存在感衰减强度不能小于 0')
        if window_minutes < 1:
            raise ValueError('存在感统计窗口必须大于 0 分钟')
        self._base_probability = base_probability
        self._decay_strength = decay_strength
        self._window_ms = window_minutes * 60_000
        self._assistant_reply_count_since = assistant_reply_count_since
        self._message_count_since = message_count_since
        self._probability_draw = probability_draw
        self._clock = clock

    async def decide(self, context: ActionContext) -> TurnAction:
        """按 ``1 / (1 + k * presence)`` 计算实际回复概率。"""
        since = self._clock() - self._window_ms
        assistant_count = self._assistant_reply_count_since(context.stream_id, since)
        message_count = self._message_count_since(context.stream_id, since)
        presence = assistant_count / message_count if message_count > 0 else 0.0
        factor = 1.0 / (1.0 + self._decay_strength * presence)
        actual_probability = self._base_probability * factor
        draw = self._probability_draw()
        if not 0.0 <= draw < 1.0:
            raise ValueError('存在感策略随机值必须在 0 到 1 之间且不含 1')
        reason = (
            f'群聊存在感：占比={presence:.4f}，实际概率={actual_probability:.4f}，'
            f'抽样值={draw:.4f}'
        )
        return TurnAction(
            action='reply' if draw < actual_probability else 'silent',
            reason=reason,
            length='brief' if draw < actual_probability else None,
        )


class TurnPlanner:
    """复用动作策略，并按当前输入长度补全本轮回复篇幅。"""

    def __init__(self, action_policy: ActionPolicy, *, long_input_chars: int = 80) -> None:
        """保存动作判据与长输入阈值。

        :raises ValueError: 长输入阈值小于 1 时抛出。
        """
        if long_input_chars < 1:
            raise ValueError('长输入字符阈值必须大于 0')
        self._action_policy = action_policy
        self._long_input_chars = long_input_chars

    async def decide(self, context: ActionContext) -> TurnAction:
        """沿用既有动作结论，并以本批用户输入总长度选择篇幅。"""
        action = await self._action_policy.decide(context)
        if action.action == 'silent':
            return action
        user_texts = []
        for message in reversed(context.messages):
            if message.get('role') != 'user':
                break
            text = str(message.get('content', '')).strip()
            if context.stream_kind == 'group':
                # 群聊历史在读取时为每条正文添加“显示名: ”，篇幅只统计原始正文。
                text = '\n'.join(
                    line.partition(': ')[2] if ': ' in line else line
                    for line in text.splitlines()
                ).strip()
            user_texts.append(text)
        batch_input_chars = sum(len(text) for text in user_texts)
        length: ReplyLength = (
            'long'
            if batch_input_chars >= self._long_input_chars
            else 'brief'
        )
        return TurnAction(
            action='reply',
            reason=action.reason,
            length=length,
        )
