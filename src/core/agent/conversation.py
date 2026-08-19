"""Conversation Agent：一次模型调用同时完成行动决策与发声。

本模块是行动核心中唯一真正调用模型的 Agent。它接收已组装的消息与回合固定
快照，流式消费模型输出：解析器在动作头（<decision>）完整且通过回合帧校验
之前，不向调用方放出任何正文事件；校验通过后正文与副作用事件才逐批流出。
silent 只产出动作头并写一条行动决策事件，不产生任何用户可见内容。

失败语义（event_status 与自主沉默绝不允许混淆）：
- 正文先于动作头 / 缺失动作头 → parse_error；
- 动作头违反协议或回合帧（非法枚举、自由理由码、目标越界、引用能力缺失、
  FORCE 禁默、动作头之后没有正文）→ illegal_action；
- LlmError(kind=timeout) → timeout，其余 LlmError 与未知异常 → provider_error；
- LlmError(kind=aborted) 原样上抛且不写行动决策事件：用户主动中断不属于
  八种行动事件状态，由调用方沿用既有中断语义处理。

依赖：action_protocol（协议与校验）、parser（流式解析）、llm_models 协议
与 LlmError、observe.events（行动决策事件落账）；被 src.core.services.chat
在 DELIBERATE / FORCE 候选上调用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, cast

import asyncio
import time

from .action_protocol import (
    ActionDecisionEvent,
    ConversationAction,
    ConversationDecision,
    DecisionFrame,
    DecisionHead,
    EventStatus,
    GateInputFacts,
    IllegalActionError,
    ReplyLength,
)
from .parser import DecisionEvent, EmojiEvent, ParseEvent, ResponseParser, TextEvent

from src.core.llm_models.openai import LlmError
from src.core.llm_models.protocol import LlmProvider
from src.core.observe import events as trace


@dataclass(frozen=True)
class AgentOutcome:
    """一次 Conversation Agent 调用的完整结果。

    decision 仅当状态为 committed / silent_by_choice 时非空；失败状态
    （timeout / provider_error / parse_error / illegal_action）下为 None，
    原因见 action_event.detail。
    """

    decision: ConversationDecision | None
    event_status: EventStatus
    action_event: ActionDecisionEvent
    body_text: str = ""
    body_events: tuple[ParseEvent, ...] = ()


def _parse_id_list(raw: str | None) -> tuple[int, ...]:
    """把逗号分隔的目标 ID 原文解析为整数元组。

    提示词只要求写单个编号（多目标是历史上最主要的格式偏离来源），但解析侧
    仍接受多个合法编号：多写一个仍在可选集内的编号是一次真实且可审计的选择，
    把它改判为协议失败只会平白丢掉一整轮回复。格式面靠提示词收窄，不靠新增
    拒绝规则。

    :param raw: 动作头 targets 属性原文；None 表示未携带。
    :return: 已去除空项的整数元组。
    :raises IllegalActionError: 存在无法解析为整数的项。
    """
    if raw is None:
        return ()
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    try:
        return tuple(int(part) for part in parts)
    except ValueError as exc:
        raise IllegalActionError(f'目标消息 ID 无法解析：{raw}') from exc


def _parse_optional_id(raw: str | None) -> int | None:
    """把单个引用消息 ID 原文解析为可空整数。

    :param raw: 动作头 quote 属性原文；None 表示未携带。
    :return: 解析后的整数；未携带时为 None。
    :raises IllegalActionError: 原文存在但无法解析为整数。
    """
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise IllegalActionError(f'引用消息 ID 无法解析：{raw}') from exc


def _parse_code_list(raw: str | None) -> tuple[str, ...]:
    """把逗号分隔的理由码原文解析为非空字符串元组。

    :param raw: 动作头 reasons 属性原文；None 表示未携带。
    :return: 已去除空项与首尾空白的字符串元组；语义分域校验在 DecisionHead。
    :raises IllegalActionError: reasons 属性整体缺失。
    """
    if raw is None:
        raise IllegalActionError('动作头缺少 reasons 属性')
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _parse_length(raw: str | None) -> ReplyLength | None:
    """把篇幅原文解析为封闭枚举值。

    :param raw: 动作头 length 属性原文；None 表示未携带。
    :return: brief 或 long；未携带时为 None。
    :raises IllegalActionError: 原文存在但不属于封闭枚举。
    """
    if raw is None:
        return None
    value = raw.strip()
    if value in ('brief', 'long'):
        return cast(ReplyLength, value)
    raise IllegalActionError(f'未知回复篇幅：{raw}')


class ConversationAgent:
    """唯一能产出用户可见内容的模型 Agent（项目全局不变量之一）。

    同一次模型调用内先产出动作头再发声，不拆 Planner/Replyer，不引入多轮
    ReAct。silent 不触发任何正文副作用，只写一条结构化行动决策事件。
    """

    def __init__(
        self,
        provider: LlmProvider,
        *,
        temperature: float,
        max_tokens: int | None = None,
    ) -> None:
        """保存模型提供方与采样参数。

        :param provider: 已配置的对话模型流式提供方。
        :param temperature: 采样温度。
        :param max_tokens: 可选的输出 token 上限。
        """
        self._provider = provider
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def run(
        self,
        frame: DecisionFrame,
        messages: list[dict],
        gate_inputs: GateInputFacts,
        gate_reason_codes: tuple[str, ...],
        *,
        prompt_hash: str = "",
        model_task: str = "chat.conversation",
        provider_name: str = "",
        model_name: str = "",
        on_events: Callable[[list[ParseEvent]], Awaitable[None]] | None = None,
        on_chunk: Callable[[dict[str, Any]], None] | None = None,
        signal: asyncio.Event | None = None,
    ) -> AgentOutcome:
        """执行一次行动决策与发声，并落账四层行动决策事件。

        :param frame: 本回合固定快照；动作头必须在其边界内合法。
        :param messages: 已组装、可直接提交模型的角色/内容消息列表。
        :param gate_inputs: 第 1 层确定性输入事实。
        :param gate_reason_codes: 第 2 层门控原因码。
        :param prompt_hash: 第 4 层提示词指纹；由调用方按模板组合计算。
        :param model_task: 第 4 层模型任务标识。
        :param provider_name: 第 4 层提供方标识。
        :param model_name: 第 4 层模型标识。
        :param on_events: 动作头校验通过后逐批接收正文与副作用事件的回调；
            省略时事件聚合到返回结果中，适合测试与重放。
        :param on_chunk: 可选的原生分片回调，供调用方转发流式观测事件。
        :param signal: 可选的取消事件，透传给模型提供方。

        :return: 携带决策、事件状态与完整审计事件的 AgentOutcome。
        :raises LlmError: kind 为 aborted 时原样上抛，表示用户主动中断。

        副作用：写入一条 action_decision 观察事件；模型调用与事件数量线性相关。
        """
        started = time.monotonic()
        parser = ResponseParser()
        head: DecisionHead | None = None
        body_events: list[ParseEvent] = []
        body_parts: list[str] = []
        emoji_emotions: list[str] = []
        decision: ConversationDecision | None = None
        status: EventStatus = "committed"
        detail = ""

        async def release(events: list[ParseEvent]) -> None:
            """放出已通过动作头校验的事件；无回调时仅聚合到结果。"""
            if on_events is not None:
                await on_events(list(events))
            else:
                body_events.extend(events)

        def finish() -> AgentOutcome:
            """组装审计事件、写入观察账本并返回最终结果。"""
            latency_ms = int((time.monotonic() - started) * 1000)
            action_event = ActionDecisionEvent(
                turn_id=frame.turn_id,
                snapshot_id=frame.snapshot_id,
                turn_message_watermark=frame.message_watermark,
                gate_inputs=gate_inputs,
                gate_disposition=frame.disposition,
                gate_reason_codes=gate_reason_codes,
                available_actions=tuple(sorted(frame.available_actions)),
                decision=decision,
                event_status=status,
                detail=detail,
                prompt_hash=prompt_hash,
                model_task=model_task,
                provider=provider_name,
                model=model_name,
                latency_ms=latency_ms,
            )
            trace.emit('action_decision', **action_event.to_dict())
            return AgentOutcome(
                decision=decision,
                event_status=status,
                action_event=action_event,
                body_text=''.join(body_parts),
                body_events=tuple(body_events),
            )

        try:
            async for chunk in self._provider.stream(
                messages=messages,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                signal=signal,
            ):
                if on_chunk is not None:
                    on_chunk(chunk)
                text = chunk.get('text')
                if not text:
                    continue
                for event in parser.push(text):
                    if head is None:
                        if isinstance(event, DecisionEvent):
                            head = self._parse_head(event, frame)
                            if head.action == 'silent':
                                decision = head.to_decision('')
                                status = 'silent_by_choice'
                                # 静默只有动作头：立即返回，之后任何正文都不解析不流出。
                                return finish()
                            continue
                        status = 'parse_error'
                        detail = (
                            f'动作头之前出现了 {type(event).__name__}，正文被整体丢弃'
                        )
                        return finish()
                    if isinstance(event, DecisionEvent):
                        # 首个动作头之后重复出现视为模型噪声，忽略且不放出。
                        continue
                    await release([event])
                    if isinstance(event, TextEvent):
                        body_parts.append(event.value)
                    elif isinstance(event, EmojiEvent):
                        emoji_emotions.append(event.emotion)
            if head is None:
                status = 'parse_error'
                detail = '模型输出中没有动作头'
                return finish()
            # 冲刷未闭合标签，与既有流式管线保持一致的宽容度。
            for event in parser.flush():
                if isinstance(event, DecisionEvent):
                    continue
                await release([event])
                if isinstance(event, TextEvent):
                    body_parts.append(event.value)
                elif isinstance(event, EmojiEvent):
                    emoji_emotions.append(event.emotion)
            decision = head.to_decision(''.join(body_parts), tuple(emoji_emotions))
        except LlmError as exc:
            if exc.kind == 'aborted':
                # 用户主动中断不属于八种行动事件状态，原样上抛由调用方处理。
                raise
            status = 'timeout' if exc.kind == 'timeout' else 'provider_error'
            detail = str(exc)
            return finish()
        except IllegalActionError as exc:
            status = 'illegal_action'
            detail = str(exc)
            return finish()
        except Exception as exc:
            status = 'provider_error'
            detail = f'{type(exc).__name__}：{exc}'
            return finish()
        return finish()

    def _parse_head(self, event: DecisionEvent, frame: DecisionFrame) -> DecisionHead:
        """把解析器动作头转换为已通过帧校验的 DecisionHead。

        :param event: 解析器产出的动作头原始属性。
        :param frame: 本回合固定快照。
        :return: 已完成结构与帧校验的 DecisionHead。
        :raises IllegalActionError: 属性缺失、枚举非法、目标越界或引用能力缺失。
        """
        if event.action is None:
            raise IllegalActionError('动作头缺少 action 属性')
        action = cast(ConversationAction, event.action)
        head = DecisionHead(
            action=action,
            target_message_ids=_parse_id_list(event.targets),
            quote_message_id=_parse_optional_id(event.quote),
            reason_codes=_parse_code_list(event.reasons),
            length=_parse_length(event.length),
        )
        head.validate(frame)
        return head
