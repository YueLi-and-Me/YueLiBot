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
class ReplyDecision:
    """内层回复判据只返回是否回复及其可观测理由。"""

    should_reply: bool
    reason: str

    def __post_init__(self) -> None:
        """拒绝非布尔结论与空理由。"""
        if not isinstance(self.should_reply, bool):
            raise TypeError('回复判据必须返回布尔结论')
        if not self.reason.strip():
            raise ValueError('回复判据理由不能为空')


@dataclass(frozen=True)
class ActionContext:
    """动作策略可读取的已组织回合上下文。

    ``messages`` 是未拼系统提示词、未按模型字符预算裁剪的会话历史；群聊
    user 条目已在读取时带上说话人前缀。策略由此直接理解“刚才说了什么”，
    不依赖最终提交给模型的消息形态。
    """

    turn_id: int
    stream_id: int
    messages: Tuple[Dict[str, Any], ...]
    batch_text: str


class ActionPolicy(Protocol):
    """根据已组织上下文选择本轮动作的窄协议。"""

    @property
    def decision_source(self) -> str:
        """返回写入动作事件的稳定策略来源。"""
        ...

    async def decide(self, context: ActionContext) -> TurnAction:
        """返回本轮应执行的动作。"""
        ...


class ReplyPolicy(Protocol):
    """仅判断本轮是否回复的内层窄协议。"""

    async def decide(self, context: ActionContext) -> ReplyDecision:
        """返回是否回复及其理由。"""
        ...


class AlwaysReplyPolicy:
    """保持现有行为的默认回复判据。"""

    async def decide(self, context: ActionContext) -> ReplyDecision:
        """始终选择回复，不读取或修改上下文。"""
        return ReplyDecision(should_reply=True, reason='默认策略始终回复')


class PresenceActionPolicy:
    """根据最近窗口内主体发言占比平滑降低回复概率。

    Conversation 行动核心落地后，本策略已退化为流量控制器：仅在灰度
    off / shadow 及 selected_streams 清单外的 stream 上参与决定是否进入
    回复（保留旧行为）；Agent 灰度 live 的 stream 不再调用本策略，最终
    reply/silent 全部由 Conversation Agent 决定，硬上限由三态门控执行，
    最近发言频次作为 Agent 的确定性输入事实。
    """

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

    async def decide(self, context: ActionContext) -> ReplyDecision:
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
        return ReplyDecision(
            should_reply=draw < actual_probability,
            reason=reason,
        )


class TurnPlanner:
    """复用动作策略，并按当前输入长度补全本轮回复篇幅。

    仅服务于灰度 off 的旧管线；Conversation Agent live 路径由模型在
    动作头中自选篇幅，不再经过本规划器。
    """

    def __init__(self, reply_policy: ReplyPolicy, *, long_input_chars: int = 80) -> None:
        """保存动作判据与长输入阈值。

        :raises ValueError: 长输入阈值小于 1 时抛出。
        """
        if long_input_chars < 1:
            raise ValueError('长输入字符阈值必须大于 0')
        self._reply_policy = reply_policy
        self._long_input_chars = long_input_chars

    @property
    def decision_source(self) -> str:
        """返回同时包含规划器与内层回复判据的事件来源。"""
        return f'{type(self).__name__}({type(self._reply_policy).__name__})'

    async def decide(self, context: ActionContext) -> TurnAction:
        """沿用既有动作结论，并以显式传入的本批原文长度选择篇幅。"""
        decision = await self._reply_policy.decide(context)
        batch_input_chars = len(context.batch_text.strip())
        reason = f'{decision.reason}；本批输入字符数={batch_input_chars}'
        if not decision.should_reply:
            return TurnAction(action='silent', reason=reason)
        length: ReplyLength = (
            'long'
            if batch_input_chars >= self._long_input_chars
            else 'brief'
        )
        return TurnAction(
            action='reply',
            reason=reason,
            length=length,
        )
