"""Conversation Agent：在 ReAct 回环里完成行动决策与发声。

本模块是行动核心中唯一真正调用模型的 Agent。一个回合由若干轮组成：每轮流式消费
一次模型输出，解析器在动作头（<decision>）完整且通过回合帧校验之前，不向调用方
放出任何正文事件。

- 选到终局动作（reply / silent / react）时回合结束：单模型 reply 的正文与副作用
  事件逐批流出；拆分 replyer 的产物通过整轮协议校验后统一放出；silent 与 react
  不产生任何用户可见内容。
- 选到认知动作（recall / inspect / consult）时本轮结束、回合继续：执行检索、把观察结果
  追加进消息序列，再发起下一轮。认知轮不放出任何事件，用户侧不可见。

轮次预算通过动作空间表达：调用方给出的动作集是权威的，本模块只在预算耗尽时
从中减去认知动作，模型再选认知动作即触发既有的动作空间校验，记为
``illegal_action``。不存在「预算耗尽自动 reply」的降级路径，也不在本模块重算
动作空间——该判据只有 ``action_protocol.available_actions`` 一处。

失败语义（event_status 与自主沉默是互斥的两类状态）：
- 正文先于动作头 / 缺失动作头 → parse_error；工具调用模式下模型不走工具通道
  （整条响应只有正文，或既无正文也无工具调用）同属此类，先按
  ``_ACTION_CALL_REPAIR_LIMIT`` 纠错重发，重发仍不调工具才落此状态；
- 动作头违反协议或回合帧（非法枚举、自由理由码、目标越界、引用能力缺失、
  FORCE 禁默、认知动作缺 query、动作头之后没有正文）→ illegal_action；
  工具调用模式下的此类错误先按 ``_ACTION_CALL_REPAIR_LIMIT`` 纠错重试，
  把拒绝原因回灌给模型重发，重试仍不合法才落此状态；
- LlmError(kind=timeout) → timeout，其余 LlmError 与未知异常 → provider_error；
- LlmError(kind=aborted) 原样上抛且不写行动决策事件：用户主动中断不属于
  八种行动事件状态，由调用方沿用既有中断语义处理；
- 认知动作执行本身失败（数据库错误等）原样上抛，不转成模型失败状态：
  那是本机故障而非模型协议问题，混入 provider_error 会被当作服务商波动忽略。

依赖：action_protocol（协议与校验）、cognition（认知动作执行）、tooling
（认知动作与外部只读工具的登记与执行）、parser（流式解析）、llm_models 协议与
LlmError、observe.events（行动决策事件落账）；被 src.core.services.chat 在
DELIBERATE / FORCE 候选上调用。
"""

from __future__ import annotations

from contextlib import aclosing
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, List, Mapping, Sequence, cast

import asyncio
import json
import time

from .action_protocol import (
    COGNITIVE_ACTIONS,
    SPEAKING_ACTIONS,
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
from .tool_schema import build_tool_definitions, decision_head_from_tool_call
from .cognition import (
    OBSERVATION_EVENT_MAX_CHARS,
    OBSERVATION_MAX_CHARS,
    CognitiveScope,
)
from .parser import (
    DecisionEvent,
    EmojiEvent,
    ParseEvent,
    ResponseParser,
    ResponseProtocolError,
    SayEvent,
    TextEvent,
)

from src.core.llm_models.openai import LlmError
from src.core.llm_models.protocol import LlmProvider
from src.core.observe import events as trace
from src.core.tooling.executor import ToolExecutor
from src.core.tooling.registry import ToolRegistry
from src.core.tooling.spec import ToolContext, ToolExecutionResult, ToolInvocation


# 每一轮都追加在末条消息之后的输出起点指令。系统提示词末尾的协议离生成位置较远，
# 在生成位置附近重复该指令可降低模型先输出 <say> 的概率；这与
# ChatService 给首轮末条用户消息追加的指令是同一条约束，只是作用在后续轮次。
_OUTPUT_REQUIREMENT = (
    '[输出要求] 你下一条回复必须先输出 <decision> 动作标签；'
    '正文只能放在其后的 <say> 里，禁止在 <decision> 之前输出 <say>、普通文字或解释。'
)
# 认知轮次用尽时追加的收束指令。它仅复述动作空间的既有约束，实际约束在
# available_actions；两处口径必须一致，修改时须同步。
_FINAL_ROUND_NOTICE = (
    '你已经用完这一轮可以查东西的次数，接下来必须直接给出最终动作，不能再检索。'
)
# 工具调用协议错误的纠错重试上限，两类模型侧错误共用同一份预算：
# - 兼容网关把工具参数整段丢弃，到达校验层的是空对象（illegal_action）；
# - 模型完全不走工具通道，直接输出正文或什么都不给（parse_error）。
# 两类都把原因回灌重发一次即可恢复；重发仍不合法才按对应状态失败。重试只重发
# 模型调用，不替模型补写任何参数。
_ACTION_CALL_REPAIR_LIMIT = 1


class _LocalToolExecutionError(RuntimeError):
    """标记外部执行器的本机故障，使其越过模型错误归因分支。"""

    def __init__(self, error: Exception) -> None:
        """保存原始异常，调用层会按既有本机故障语义原样抛出。"""
        self.error = error
        super().__init__(str(error))


class _MissingActionCall(Exception):
    """工具调用模式下模型没有通过工具给出动作。

    - 现象：声明了 tools 的决策请求里，模型流出的是台词正文（观测到的都是回复
      开头的一两个字），整条响应没有任何工具调用；偶尔正文也没有。
    - 原因：tools 只是可选项，服务端默认的 tool_choice=auto 允许模型改用正文
      作答，提示词里「只调用工具」的约束对部分模型并不成立。
    - 后果：正文在工具模式下不被承认为动作表达，该轮没有可执行的决策；直接
      终局会把一次可自愈的协议失效变成用户侧的无声失败，因此先纠错重发一次。
    """


class _RepairableCallFault(Exception):
    """还有纠错预算的工具调用协议错误，携带回灌用的纠错消息。

    由单次尝试在 ``IllegalActionError`` 分支抛出、回合驱动层捕获；预算耗尽时
    尝试自身直接按 ``illegal_action`` 终局，不会抛出本类。
    """

    def __init__(self, correction: list[dict[str, str]]) -> None:
        """保存纠错消息，由回合驱动层追加进下一次尝试的消息序列。"""
        self.correction = correction
        super().__init__('工具调用协议错误，等待纠错重试')


async def _execute_external_tool(
    executor: ToolExecutor,
    invocation: ToolInvocation,
    context: ToolContext,
) -> ToolExecutionResult:
    """执行工具并标记本机异常，使调用层只把真实 wait_for 超时归为超时。"""
    try:
        return await executor.execute(invocation, context)
    except Exception as exc:
        raise _LocalToolExecutionError(exc) from exc


def _truncate(text: str, limit: int) -> str:
    """按字符上限截断事件账本里的观察摘要。

    :param text: 观察正文。
    :param limit: 字符上限。
    :return: 未超限时原样返回，超限时返回截断后加省略号的文本。
    """
    if len(text) <= limit:
        return text
    return f'{text[:limit]}…'





def _clip_total(text: str, limit: int) -> str:
    """按总预算截断回灌文本，并显式标注截断。

    多条工具观察共享同一预算：整段拼接后一次性截断，而不是逐条各自截断——
    逐条截断会让先执行的工具占满全部预算，后续观察无法进入回灌。

    :param text: 待截断的完整文本。
    :param limit: 总字符预算。
    :return: 未超限时原样返回，超限时返回截断后标注的文本。
    """
    if len(text) <= limit:
        return text
    return f'{text[:limit]}…（已截断）'


def _merge_observation_text(
    steps: Sequence[tuple[str, str, str]],
    terminal_observation: str = '',
) -> str:
    """把一轮执行的工具观察合并为单段文本，供事件账本使用。

    按执行顺序逐条拼接「工具名 + 入参 + 结果」；认知动作与外部只读工具共用
    同一份明细，前者的入参是检索词，后者是 JSON 参数。预算截断发生在回灌消息的
    渲染处（_observation_messages），这里只拼文本不截断，账本字段的截断由
    finish 按 OBSERVATION_EVENT_MAX_CHARS 统一处理。

    :param steps: 按执行顺序排列的（工具名、入参文本、观察正文）明细。
    :param terminal_observation: 可选；认知工具之后跟随 silent 等终局动作时，
        终局结算携带的观察文本一并并入。
    :return: 拼接后的单段观察文本；无任何内容时为空串。
    """
    blocks = [
        f'{action}：{query}\n{observation}'
        for action, query, observation in steps
    ]
    if terminal_observation.strip():
        blocks.append(terminal_observation.strip())
    return '\n\n'.join(blocks)


def _emit_tool_execution(
    call: dict[str, Any],
    frame: DecisionFrame,
    round_index: int,
    *,
    event_status: str,
    duration_ms: int,
    observation: str = '',
    tool_kind: str = 'cognitive',
) -> None:
    """把一次已执行工具的账目写进观察账本。

    与 action_decision 同口径挂回合编号：同一轮执行多个工具时，roundIndex
    相同的多条本事件构成一条完整链路。

    :param call: 模型侧的工具调用原文，name 与 arguments 直接落账。
    :param frame: 本回合固定快照。
    :param round_index: 轮次序号。
    :param event_status: committed / failed；截断的调用走 discarded 专用函数。
    :param duration_ms: 执行耗时。
    :param observation: 观察摘要，按账本上限截断。
    :param tool_kind: 工具类别；内置认知动作为 cognitive，外部只读工具为
        readonly。账本据此区分 Bot 查询记忆与读取会话内容。
    """
    trace.emit(
        'tool_execution',
        turnId=frame.turn_id,
        snapshotId=frame.snapshot_id,
        roundIndex=round_index,
        toolName=call.get('name', ''),
        toolKind=tool_kind,
        arguments=call.get('arguments', ''),
        durationMs=duration_ms,
        eventStatus=event_status,
        observation=_truncate(observation, OBSERVATION_EVENT_MAX_CHARS),
    )


def _emit_discarded_tool_call(
    call: dict[str, Any],
    frame: DecisionFrame,
    round_index: int,
) -> None:
    """给终局动作之后被截断的工具调用记一条轻量审计事件。

    截断的调用不执行、不计为错误：它表示模型在终局动作之后多选了工具，
    不属于协议越界，账本须与协议越界区分记录。

    :param call: 被截断的工具调用原文。
    :param frame: 本回合固定快照。
    :param round_index: 轮次序号。
    """
    trace.emit(
        'tool_execution',
        turnId=frame.turn_id,
        snapshotId=frame.snapshot_id,
        roundIndex=round_index,
        toolName=call.get('name', ''),
        toolKind='',
        arguments=call.get('arguments', ''),
        durationMs=0,
        eventStatus='discarded',
        observation='',
    )


@dataclass(frozen=True)
class AgentOutcome:
    """一次 Conversation Agent 调用的完整结果。

    decision 仅当状态为 committed / silent_by_choice / cognitive_step 时非空；
    失败状态（timeout / provider_error / parse_error / illegal_action）下为 None，
    原因见 action_event.detail。cognitive_step 且本轮只执行了外部只读工具时
    decision 同样为 None——外部工具没有 ConversationDecision，明细在
    cognitive_steps 与 tool_invocation 里。

    :ivar observation: 认知轮的观察正文；非认知轮为空串。
    :ivar cognitive_rounds_used: 本回合实际用掉的认知轮次数，供调用方记账与观察。
    :ivar cognitive_steps: 本轮实际执行的工具明细（工具名、入参文本、观察正文），
        按执行顺序排列；一轮响应允许执行多个工具，调用方按条数扣减预算。认知
        动作与外部只读工具同列其中，共用一份预算。
    :ivar tool_invocation: 本轮最后一次外部只读工具调用；没有外部工具时为 None，
        供调用方在缺少 decision 时仍能渲染该轮做了什么。
    """

    decision: ConversationDecision | None
    event_status: EventStatus
    action_event: ActionDecisionEvent
    body_text: str = ""
    body_events: tuple[ParseEvent, ...] = ()
    observation: str = ""
    cognitive_rounds_used: int = 0
    cognitive_steps: tuple[tuple[str, str, str], ...] = ()
    tool_invocation: ToolInvocation | None = None


def _parse_id_list(raw: str | None) -> tuple[int, ...]:
    """把逗号分隔的目标 ID 原文解析为整数元组。

    提示词只要求写单个编号，解析侧仍接受多个合法编号：改判为协议失败会整轮
    丢弃合法回复。格式面由提示词收窄，不新增拒绝规则。

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
    """唯一能产出用户可见内容的模型 Agent。

    可见正文永远从一条已经通过校验的动作头派生，这一条不受调用次数影响：

    - 未注入 replyer：同一次模型调用内先出动作头再发声，与拆分前逐字相同。
    - 注入 replyer：动作头一解析完就结束决策流，正文改由第二次调用产出。
      决策模型此后写的任何字都不解析、不流出，其职责到动作头为止。

    ReAct 回环只在动作头层面展开：认知动作不产出可见内容，因此不影响上述约束。
    只有 ``SPEAKING_ACTIONS`` 会触发第二次调用；silent / wait / react / poke
    在决策那一次就结束，不额外付一次模型往返。
    """

    def __init__(
        self,
        provider: LlmProvider,
        *,
        temperature: float,
        max_tokens: int | None = None,
        replyer: LlmProvider | None = None,
        replyer_temperature: float | None = None,
        replyer_max_tokens: int | None = None,
        tool_calling: bool = False,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        """保存模型提供方与采样参数。

        :param provider: 已配置的决策模型流式提供方；未注入 ``replyer`` 时它同时
            负责产出正文。
        :param temperature: 采样温度。
        :param max_tokens: 可选的输出 token 上限。
        :param replyer: 可选的回复生成模型。注入后决策与表达分离：本 Agent 只从
            决策流里取动作头，正文由它产出。省略即保持单次调用的既有行为。
        :param replyer_temperature: 回复生成的采样温度；省略时沿用决策那一档。
            两级分开取值：决策要求输出稳定，表达要求自然。
        :param replyer_max_tokens: 回复生成的输出上限；省略时沿用决策那一档。
        :param tool_calling: 决策层是否改用工具调用表达动作。为真时动作空间以
            函数签名下发、决策以 ``tool_calls`` 回来，不再解析 XML 动作头。
            必须与 ``replyer`` 一起启用：工具调用只产出决策，没有正文来源。
        :param tool_registry: 可选的工具注册表。注入后工具调用模式的声明改由
            注册表生成；省略时退回直接生成，两者输出逐字一致——动作声明的
            判据只有 ``tool_schema`` 一份，注册表只是它的统一入口。
        :raises ValueError: 启用工具调用却没有注入回复生成模型。
        """
        if tool_calling and replyer is None:
            raise ValueError('工具调用模式必须同时注入 replyer，否则没有正文来源')
        self._provider = provider
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._replyer = replyer
        self._replyer_temperature = (
            temperature if replyer_temperature is None else replyer_temperature
        )
        self._replyer_max_tokens = (
            max_tokens if replyer_max_tokens is None else replyer_max_tokens
        )
        self._tool_calling = tool_calling
        self._tool_registry = tool_registry

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
        cognitive_scope: CognitiveScope | None = None,
        cognitive_rounds: int = 0,
        tool_context: ToolContext | None = None,
        on_events: Callable[[list[ParseEvent]], Awaitable[None]] | None = None,
        on_chunk: Callable[[dict[str, Any]], None] | None = None,
        on_round: Callable[[AgentOutcome], None] | None = None,
        replyer_messages: Callable[[DecisionHead], Awaitable[list[dict]]] | None = None,
        signal: asyncio.Event | None = None,
    ) -> AgentOutcome:
        """执行一个回合：若干认知轮之后给出终局动作，并逐轮落账。

        :param frame: 本回合固定快照；每轮只有动作集按剩余预算变化，其余字段
            （水位、可选消息、门控态、平台能力）在整个回合内不变。
        :param messages: 已组装、可直接提交模型的角色/内容消息列表；认知轮的
            观察追加在其副本尾部，调用方传入的列表不被修改。
        :param gate_inputs: 第 1 层确定性输入事实。
        :param gate_reason_codes: 第 2 层门控原因码。
        :param prompt_hash: 第 4 层提示词指纹；由调用方按模板组合计算。
        :param model_task: 第 4 层模型任务标识。
        :param provider_name: 第 4 层提供方标识。
        :param model_name: 第 4 层模型标识。
        :param cognitive_scope: 认知检索的会话与人物范围；省略时退化为单轮，
            行为与引入 ReAct 之前逐字相同。
        :param cognitive_rounds: 本回合最多允许几次认知动作；0 表示关闭 ReAct。
            一轮响应执行多个认知工具时按工具条数扣减。
        :param tool_context: 外部只读工具可读取的会话上下文；省略时注册表里的
            外部工具不下发，只执行内置动作工具。
        :param on_events: 动作头校验通过后逐批接收正文与副作用事件的回调；
            省略时事件聚合到返回结果中，适合测试与重放。认知轮不会调用它。
        :param on_chunk: 可选的原生分片回调，供调用方转发流式观测事件。
        :param on_round: 可选的逐轮回调，每个认知轮结束后以该轮结果调用一次；
            终局轮不调用（调用方可直接取得返回值）。用于向调用方展示认知轮的
            中间过程；认知轮不产生任何用户可见产物。
        :param replyer_messages: 按动作头组装回复生成消息序列的异步回调。提示词、
            人格与历史都属于调用方，Agent 不自行拼装；组装本身可能包含向量检索与
            表达选择这类模型往返，因此是异步的。省略它（或未注入 replyer）即退回
            单次调用，正文仍从决策流里取。
        :param signal: 可选的取消事件，透传给模型提供方，跨轮持续有效。

        :return: 携带终局决策、事件状态与完整审计事件的 AgentOutcome。
        :raises LlmError: kind 为 aborted 时原样上抛，表示用户主动中断。
        :raises Exception: 认知动作执行失败（如数据库错误）原样上抛；那是本机
            故障，不转成模型失败状态。

        副作用：每轮写入一条 action_decision 观察事件；模型调用次数等于实际轮数。
        """
        # 认知检索与外部只读工具都可以启用多轮：只配置了外部工具、未给认知
        # 范围的会话同样进入多轮，否则工具声明下发了却没有轮次可用。
        external_tool_enabled = (
            tool_context is not None
            and self._tool_registry is not None
            and self._tool_registry.has_registered_tools()
        )
        react_enabled = cognitive_rounds > 0 and (
            cognitive_scope is not None or external_tool_enabled
        )
        rounds_left = cognitive_rounds if react_enabled else 0
        working_messages = list(messages)
        round_index = 0
        while True:
            # 调用方给的动作集是权威的：它已经按 stream、门控态、平台能力与初始
            # 预算算过一次。Agent 唯一多知道的是剩余轮数，因此这里只做减法，
            # 不重算：重算会产生第二份动作空间判据，两份会不一致。
            round_frame = (
                frame
                if rounds_left > 0
                else frame.with_available_actions(
                    frame.available_actions - COGNITIVE_ACTIONS
                )
            )
            outcome = await self._run_round(
                round_frame,
                working_messages,
                gate_inputs,
                gate_reason_codes,
                round_index=round_index,
                cognitive_rounds_used=round_index,
                prompt_hash=prompt_hash,
                model_task=model_task,
                provider_name=provider_name,
                model_name=model_name,
                rounds_left=rounds_left,
                cognitive_scope=cognitive_scope,
                tool_context=tool_context,
                on_events=on_events,
                on_chunk=on_chunk,
                replyer_messages=replyer_messages,
                signal=signal,
            )
            if outcome.event_status != 'cognitive_step':
                return outcome
            # 只执行了外部只读工具的轮没有 ConversationDecision，明细在
            # cognitive_steps 里；这里只断言该轮确实做了事。
            assert outcome.cognitive_steps
            if on_round is not None:
                on_round(outcome)
            # 一轮响应可以执行多个认知工具：预算按工具条数扣减，不允许先超支
            # 再补终局；扣减后为负的情况已在 _run_round 内按「预算耗尽未给出
            # 终局动作」记 illegal_action，不会走到这里。
            rounds_left -= len(outcome.cognitive_steps)
            round_index += 1
            working_messages.extend(
                _observation_messages(
                    outcome.cognitive_steps,
                    final_round=rounds_left <= 0,
                    flattened=self._tool_calling,
                )
            )

    async def _run_round(
        self,
        frame: DecisionFrame,
        messages: list[dict],
        gate_inputs: GateInputFacts,
        gate_reason_codes: tuple[str, ...],
        *,
        round_index: int,
        cognitive_rounds_used: int,
        prompt_hash: str,
        model_task: str,
        provider_name: str,
        model_name: str,
        rounds_left: int,
        cognitive_scope: CognitiveScope | None,
        tool_context: ToolContext | None,
        on_events: Callable[[list[ParseEvent]], Awaitable[None]] | None,
        on_chunk: Callable[[dict[str, Any]], None] | None,
        replyer_messages: Callable[[DecisionHead], Awaitable[list[dict]]] | None,
        signal: asyncio.Event | None,
    ) -> AgentOutcome:
        """执行一轮模型调用，并在选到工具时就地完成执行。

        执行放在本轮之内而不是交回 run()，是为了让事件的 ``latency_ms`` 覆盖
        「模型想 + 实际查」的完整耗时，也让观察摘要能与它所属的那一轮写进同一条事件。

        工具调用被判协议错误时不直接终局：网关会把工具参数整段丢弃，这类错误
        回灌拒绝原因重发一次即可恢复。驱动层只重发模型调用，不补写参数；预算
        见 ``_ACTION_CALL_REPAIR_LIMIT``，由 ``_run_attempt`` 自身执行并终局。

        :param frame: 已按本轮剩余预算收窄动作集的回合帧。
        :param messages: 本轮实际提交模型的消息序列；纠错消息追加在其副本尾部，
            调用方传入的列表不被修改。
        :param round_index: 轮次序号，从 0 开始。
        :param cognitive_rounds_used: 进入本轮之前已用掉的认知轮次数。
        :param rounds_left: 进入本轮时的剩余认知预算；一轮响应执行多个认知工具
            后若预算为负且未给出终局动作，按协议错误记账。
        :return: 本轮结果；工具轮返回 ``cognitive_step`` 并带上观察正文。
        """
        started = time.monotonic()
        correction: list[dict[str, str]] = []
        repairs_used = 0
        while True:
            try:
                return await self._run_attempt(
                    [*messages, *correction],
                    frame,
                    gate_inputs,
                    gate_reason_codes,
                    round_index=round_index,
                    cognitive_rounds_used=cognitive_rounds_used,
                    prompt_hash=prompt_hash,
                    model_task=model_task,
                    provider_name=provider_name,
                    model_name=model_name,
                    rounds_left=rounds_left,
                    cognitive_scope=cognitive_scope,
                    tool_context=tool_context,
                    on_events=on_events,
                    on_chunk=on_chunk,
                    replyer_messages=replyer_messages,
                    signal=signal,
                    started=started,
                    repairs_used=repairs_used,
                )
            except _RepairableCallFault as fault:
                correction.extend(fault.correction)
                repairs_used += 1

    async def _run_attempt(
        self,
        messages: list[dict],
        frame: DecisionFrame,
        gate_inputs: GateInputFacts,
        gate_reason_codes: tuple[str, ...],
        *,
        round_index: int,
        cognitive_rounds_used: int,
        prompt_hash: str,
        model_task: str,
        provider_name: str,
        model_name: str,
        rounds_left: int,
        cognitive_scope: CognitiveScope | None,
        tool_context: ToolContext | None,
        on_events: Callable[[list[ParseEvent]], Awaitable[None]] | None,
        on_chunk: Callable[[dict[str, Any]], None] | None,
        replyer_messages: Callable[[DecisionHead], Awaitable[list[dict]]] | None,
        signal: asyncio.Event | None,
        started: float,
        repairs_used: int,
    ) -> AgentOutcome:
        """执行一次模型调用；计时起点与纠错预算由 ``_run_round`` 给出。

        :param messages: 本次尝试实际提交模型的消息序列，已含纠错回灌。
        :param frame: 已按本轮剩余预算收窄动作集的回合帧。
        :param round_index: 轮次序号，从 0 开始。
        :param cognitive_rounds_used: 进入本轮之前已用掉的认知轮次数。
        :param started: 本轮计时起点，跨纠错尝试不变，事件耗时因此覆盖重试。
        :param repairs_used: 进入本次尝试前已用掉的纠错次数，用于预算判断与
            审计事件中的纠错记数。
        :return: 本次尝试的结果。
        :raises _RepairableCallFault: 工具调用协议错误且还有纠错预算。
        """
        parser = ResponseParser()
        # 决策与表达分离是否在本轮生效。两个条件缺一不可：注入了回复生成模型，
        # 且调用方给得出它的消息序列——提示词属于调用方，Agent 不自行拼装。
        split_reply = self._replyer is not None and replyer_messages is not None
        # 决策流里出现发言动作头后置位，据此跳出决策流并转入回复生成。
        planned_head: DecisionHead | None = None
        head: DecisionHead | None = None
        body_events: list[ParseEvent] = []
        body_parts: list[str] = []
        emoji_emotions: list[str] = []
        decision: ConversationDecision | None = None
        tool_invocation: ToolInvocation | None = None
        status: EventStatus = "committed"
        detail = ""
        observation = ""
        # 本轮已执行的工具明细；一轮响应可以含多个工具，按执行顺序排列。
        # 认知动作与外部只读工具同列其中，共用同一份预算。
        steps: list[tuple[str, str, str]] = []
        # 认知工具的本机故障标记：只用于让异常原样穿过外层分类器，不参与账本。
        tool_crash: BaseException | None = None
        # 本轮工具调用解析是否发生模型侧协议错误。只有这类错误允许纠错重试，
        # XML 动作头与本机接线故障保持既有的直接终局语义。
        call_fault = False
        # 工具模式下是否收到过正文。仅用于让缺失工具调用的失败原因区分「输出了
        # 台词」与「整条响应为空」，两者的纠错回灌措辞不同。
        prose_only = False

        async def release(events: list[ParseEvent]) -> None:
            """放出已通过动作头校验的事件；无回调时仅聚合到结果。"""
            if on_events is not None:
                await on_events(list(events))
            else:
                body_events.extend(events)

        def finish() -> AgentOutcome:
            """组装审计事件、写入观察账本并返回本轮结果。"""
            latency_ms = int((time.monotonic() - started) * 1000)
            # 纠错重试覆盖了一次本会终局的协议错误，审计事件必须记录纠错次数，
            # 否则该轮只能凭延迟异常反推发生过重试。
            repair_note = (
                f'含 {repairs_used} 次工具调用纠错' if repairs_used else ''
            )
            audited_detail = '；'.join(
                part for part in (detail, repair_note) if part
            )
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
                detail=audited_detail,
                prompt_hash=prompt_hash,
                model_task=model_task,
                provider=provider_name,
                model=model_name,
                latency_ms=latency_ms,
                round_index=round_index,
                observation=_truncate(observation, OBSERVATION_EVENT_MAX_CHARS),
                tool_name=(
                    tool_invocation.tool_name
                    if tool_invocation is not None
                    else ''
                ),
                tool_call_id=(
                    tool_invocation.call_id
                    if tool_invocation is not None
                    else ''
                ),
                tool_arguments=(
                    tool_invocation.arguments
                    if tool_invocation is not None
                    else {}
                ),
                available_tools=(
                    self._tool_registry.available_tool_names(frame)
                    if self._tool_calling and self._tool_registry is not None
                    else ()
                ),
            )
            trace.emit('action_decision', **action_event.to_dict())
            return AgentOutcome(
                decision=decision,
                event_status=status,
                action_event=action_event,
                body_text=''.join(body_parts),
                body_events=tuple(body_events),
                observation=observation,
                cognitive_rounds_used=cognitive_rounds_used,
                cognitive_steps=tuple(steps),
                tool_invocation=tool_invocation,
            )

        try:
            # aclosing 保证提前 return（silent / 认知动作都会提前结束本轮）时
            # 生成器立即收到 GeneratorExit，httpx 的流式连接随即释放；靠 GC 回收
            # 会把连接按不确定的时机挂着，而认知动作让提前结束从罕见变成常态。
            # 工具声明按本轮动作集生成：动作空间已经被剩余预算收窄过，
            # 声明一个本回合非法的工具等于主动制造 illegal_action。
            # 声明来源已收编进注册表；未注入注册表时退回直接生成。两条路径
            # 共用 tool_schema 同一份判据，输出必须逐字一致，不允许各算一套。
            if self._tool_calling:
                tools = (
                    self._tool_registry.build_tool_definitions(frame)
                    if self._tool_registry is not None
                    else build_tool_definitions(frame)
                )
            else:
                tools = None
            async with aclosing(
                self._provider.stream(
                    messages=messages,
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                    signal=signal,
                    **({'tools': tools} if tools else {}),
                )
            ) as stream:
                async for chunk in stream:
                    # 拆分模式下动作头一到手就不再读决策流。放在循环开头判断是
                    # 因为动作头是在内层事件循环里解析出来的，那里 break 只能跳出
                    # 内层；靠标志位在这里再断一次，才真正停止消费决策流。
                    if planned_head is not None:
                        break
                    if on_chunk is not None:
                        on_chunk(chunk)
                    tool_calls = chunk.get('tool_calls')
                    if tool_calls and head is None:
                        # 一轮响应允许选择多个工具：按返回顺序逐个结算。
                        # 认知工具执行后把观察回灌，终局动作至多一个且必须排在
                        # 最后，出现即截断其后调用——截断的调用不执行、不计为
                        # 错误，只记 discarded 轻量事件用于审计。
                        terminal_seen = False
                        last_cognitive_decision: ConversationDecision | None = None
                        for call in tool_calls:
                            if terminal_seen:
                                _emit_discarded_tool_call(call, frame, round_index)
                                continue
                            if not isinstance(call, Mapping):
                                call_fault = True
                                raise IllegalActionError('模型工具调用必须是对象')
                            call_name = call.get('name')
                            if not isinstance(call_name, str) or not call_name.strip():
                                call_fault = True
                                raise IllegalActionError('模型工具调用缺少非空 name')
                            raw_arguments = call.get('arguments', '')
                            resolved = (
                                self._tool_registry.resolve(call_name)
                                if self._tool_registry is not None
                                else None
                            )
                            if resolved is not None and resolved.kind == 'tool':
                                # 外部只读工具：执行后与认知工具同列 steps，共用
                                # 预算与回灌通道，因此不会自成一条并行的轮次语义。
                                if tool_context is None:
                                    # 装配缺失属于本机故障，不给纠错重试：重发
                                    # 调用也不会使上下文出现。
                                    raise IllegalActionError(
                                        f'工具 {call_name} 缺少执行上下文'
                                    )
                                try:
                                    invocation, tool_observation = (
                                        await self._run_readonly_tool(
                                            call,
                                            call_name,
                                            raw_arguments,
                                            frame,
                                            tool_context,
                                            round_index,
                                        )
                                    )
                                except IllegalActionError:
                                    call_fault = True
                                    raise
                                tool_invocation = invocation
                                steps.append((
                                    invocation.tool_name,
                                    json.dumps(
                                        invocation.arguments,
                                        ensure_ascii=False,
                                        sort_keys=True,
                                    ),
                                    tool_observation,
                                ))
                                continue
                            if not isinstance(raw_arguments, str):
                                call_fault = True
                                raise IllegalActionError(
                                    f'动作工具 {call_name} 的 arguments 必须是 JSON 文本'
                                )
                            try:
                                call_head = decision_head_from_tool_call(
                                    call_name, raw_arguments, frame,
                                )
                            except IllegalActionError:
                                call_fault = True
                                raise
                            tool_started_at = time.monotonic()
                            try:
                                settled = await self._settle_head(
                                    call_head, frame, cognitive_scope,
                                )
                            except Exception as tool_exc:
                                _emit_tool_execution(
                                    call, frame, round_index,
                                    event_status='failed',
                                    duration_ms=int((time.monotonic() - tool_started_at) * 1000),
                                )
                                # 内置认知工具的本机故障原样上抛：它会被外层
                                # 异常分类捕获，这里用标记让它原样穿过，不转成
                                # 模型失败——本机 bug 混进 provider_error 会被
                                # 当成服务商波动忽略掉。
                                tool_crash = tool_exc
                                raise
                            tool_duration_ms = int((time.monotonic() - tool_started_at) * 1000)
                            if settled is not None:
                                step_status, step_decision, step_observation = settled
                                if call_head.action in COGNITIVE_ACTIONS:
                                    _emit_tool_execution(
                                        call, frame, round_index,
                                        event_status='committed',
                                        duration_ms=tool_duration_ms,
                                        observation=step_observation,
                                    )
                                    steps.append(
                                        (
                                            call_head.action,
                                            step_decision.query or '',
                                            step_observation,
                                        )
                                    )
                                    last_cognitive_decision = step_decision
                                    continue
                                # silent 也是终局动作：置位后循环继续，剩余调用
                                # 全部按截断记账。
                                terminal_seen = True
                                status, decision, observation = step_status, step_decision, step_observation
                                observation = _merge_observation_text(steps, observation)
                                continue
                            # reply / react / poke / wait / speak：终局动作。
                            terminal_seen = True
                            head = call_head
                            if head.action in SPEAKING_ACTIONS:
                                # 工具调用只携带动作头；只有真正需要正文的动作才交给
                                # replyer。react / poke / wait 已经是完整终局动作。
                                planned_head = head
                            # 置位后循环继续，剩余调用全部按截断记账。
                        if not terminal_seen:
                            # 整个响应都是查东西的工具：预算按条数扣减，超支即
                            # 协议错误，不存在「预算耗尽就降级成别的动作」的路径。
                            # 只调外部只读工具的轮没有认知决策，decision 保持
                            # None，明细由 steps 承载。
                            assert steps
                            if rounds_left - len(steps) < 0:
                                status = 'illegal_action'
                                detail = (
                                    f'预算耗尽未给出终局动作：本轮执行了 {len(steps)} 个'
                                    f'工具，剩余预算 {rounds_left}'
                                )
                                return finish()
                            status = 'cognitive_step'
                            decision = last_cognitive_decision
                            observation = _merge_observation_text(steps)
                            return finish()
                        if head is None:
                            # silent 终局已结算，其余调用已按截断记账。
                            return finish()
                        break
                    text = chunk.get('text')
                    if not text:
                        continue
                    if self._tool_calling:
                        # 工具模式只接受函数调用这一种动作表达：正文既不解析也
                        # 不放出，只记一笔用于区分「输出了正文」与「什么都没给」。
                        #
                        # 收到正文不就地终局，是因为部分模型会先流出一小段正文
                        # 再发工具调用；在首个正文分片上关流会把本可用的调用一并
                        # 丢掉。缺失工具调用的判定因此推迟到整条流结束之后。
                        prose_only = True
                        continue
                    for event in parser.push(text):
                        if head is None:
                            if isinstance(event, DecisionEvent):
                                head = self._parse_head(event, frame)
                                settled = await self._settle_head(
                                    head, frame, cognitive_scope,
                                )
                                if settled is not None:
                                    # 认知动作与静默都只有动作头：立即返回，其后
                                    # 若还有正文一律不解析、不流出、不计入。
                                    status, decision, observation = settled
                                    if status == 'cognitive_step':
                                        # XML 路径一轮只有一个动作头，认知明细
                                        # 恒为单条；与工具路径共用同一份记账。
                                        steps.append(
                                            (head.action, decision.query or '', observation)
                                        )
                                    return finish()
                                if split_reply and head.action in SPEAKING_ACTIONS:
                                    # 决策模型的职责到此为止。它此后写的正文一律
                                    # 丢弃：两份正文来源并存时，无法确定实际流出
                                    # 的是哪一份。
                                    planned_head = head
                                    break
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
            if planned_head is not None:
                # 决策流已在 aclosing 退出时关闭，这里才发起回复生成，两条流不重叠。
                assert replyer_messages is not None
                body_parts.clear()
                emoji_emotions.clear()
                await self._stream_body(
                    await replyer_messages(planned_head),
                    release=release,
                    on_chunk=on_chunk,
                    body_parts=body_parts,
                    emoji_emotions=emoji_emotions,
                    signal=signal,
                )
                decision = planned_head.to_decision(
                    ''.join(body_parts), tuple(emoji_emotions),
                )
                return finish()
            if head is None:
                if self._tool_calling:
                    raise _MissingActionCall(
                        '工具调用模式收到正文，模型没有通过工具选择动作'
                        if prose_only
                        else '模型没有选择任何动作'
                    )
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
        except ResponseProtocolError as exc:
            status = 'parse_error'
            detail = str(exc)
            return finish()
        except IllegalActionError as exc:
            if call_fault and repairs_used < _ACTION_CALL_REPAIR_LIMIT:
                # 协议错误回灌重试；预算判断放在抛出侧，耗尽时走下方终局路径，
                # 驱动层因此无需再处理预算用尽的分支。
                raise _RepairableCallFault(
                    _call_fault_messages(str(exc)),
                ) from exc
            status = 'illegal_action'
            detail = str(exc)
            return finish()
        except _MissingActionCall as exc:
            # 与非法工具调用同属模型侧协议错误，共用同一份纠错预算；预算判断放在
            # 抛出侧，耗尽时就地终局，驱动层因此无需再处理预算用尽的分支。
            if repairs_used < _ACTION_CALL_REPAIR_LIMIT:
                raise _RepairableCallFault(
                    _missing_call_messages(str(exc)),
                ) from exc
            status = 'parse_error'
            detail = str(exc)
            return finish()
        except _LocalToolExecutionError as exc:
            raise exc.error
        except Exception as exc:
            if tool_crash is not None and exc is tool_crash:
                # 认知工具的本机故障原样上抛：见 tool_crash 声明处的说明。
                raise
            status = 'provider_error'
            detail = f'{type(exc).__name__}：{exc}'
            return finish()
        return finish()

    async def _run_readonly_tool(
        self,
        call: Mapping[str, Any],
        call_name: str,
        raw_arguments: Any,
        frame: DecisionFrame,
        tool_context: ToolContext,
        round_index: int,
    ) -> tuple[ToolInvocation, str]:
        """解析并执行一次外部只读工具，返回调用记录与回灌用的观察正文。

        与认知动作的差别只在执行协议：外部工具的参数按 ToolSpec 的 Schema 校验，
        执行有独立超时，失败不上抛而是把失败原因作为观察回灌——只读工具查不到
        内容是正常结果，不应使整轮按失败处理。

        :param call: 模型侧的工具调用原文，用于落账。
        :param call_name: 已校验非空的工具名。
        :param raw_arguments: 模型给出的参数原文，JSON 文本或对象。
        :param frame: 本回合固定快照，同时用于可用性过滤。
        :param tool_context: 执行上下文；调用方保证非空。
        :param round_index: 轮次序号，用于工具执行事件挂链路。
        :return: ``(调用记录, 观察正文)``。
        :raises IllegalActionError: 参数不合法或工具在本回合不可用；属于模型侧
            协议错误，调用方据此启动纠错重试。
        :raises _LocalToolExecutionError: 执行器本机故障，或返回结果的工具名与
            调用不一致——后者说明注册表接线错乱，必须暴露而不是当作模型问题。
        """
        assert self._tool_registry is not None
        resolved = self._tool_registry.resolve(call_name)
        assert resolved is not None and resolved.spec is not None
        assert resolved.executor is not None
        invocation = self._tool_registry.parse_invocation(
            call_name,
            raw_arguments,
            frame,
            call_id=str(call.get('id') or ''),
        )
        execution_context = replace(
            tool_context,
            stream_kind=frame.stream_kind,
            frame=frame,
            turn_id=frame.turn_id,
            snapshot_id=frame.snapshot_id,
        )
        started_at = time.monotonic()
        try:
            result = await asyncio.wait_for(
                _execute_external_tool(
                    resolved.executor,
                    invocation,
                    execution_context,
                ),
                timeout=resolved.spec.timeout_ms / 1000,
            )
        except asyncio.TimeoutError:
            result = ToolExecutionResult(
                tool_name=invocation.tool_name,
                success=False,
                error_message=f'执行超过 {resolved.spec.timeout_ms} 毫秒',
            )
        duration_ms = int((time.monotonic() - started_at) * 1000)
        if result.tool_name != invocation.tool_name:
            error = RuntimeError(
                f'工具执行结果名称不一致：期望 '
                f'{invocation.tool_name}，实际 {result.tool_name}'
            )
            raise _LocalToolExecutionError(error) from error
        _emit_tool_execution(
            dict(call),
            frame,
            round_index,
            event_status='committed' if result.success else 'failed',
            duration_ms=duration_ms,
            observation=result.observation if result.success else '',
            tool_kind='readonly',
        )
        observation = (
            result.observation
            if result.success
            else f'工具执行失败：{result.error_message}'
        )
        return invocation, observation

    async def _settle_head(
        self,
        head: DecisionHead,
        frame: DecisionFrame,
        cognitive_scope: CognitiveScope | None,
    ) -> tuple[EventStatus, ConversationDecision, str] | None:
        """结算不产出正文的那两类动作头。

        认知工具经注册表统一执行、静默直接定案；两者都在动作头处终止本轮，
        其后不可能再有可见产物。抽出来是因为 XML 动作头与工具调用是同一套
        语义的两种表达，判据只能有一份。

        :param head: 已通过帧校验的动作头。
        :param frame: 本回合固定快照。
        :param cognitive_scope: 认知检索范围；选到认知动作时必须存在。
        :return: ``(状态, 决策, 观察正文)``；发言类动作返回 ``None``，表示调用方
            还要继续取正文。
        :raises Exception: 认知工具执行失败原样上抛——那是本机故障，不转成模型
            失败状态。
        """
        if head.action in COGNITIVE_ACTIONS:
            decision = head.to_decision('')
            assert cognitive_scope is not None
            assert decision.query is not None
            assert self._tool_registry is not None, '认知执行必须注入工具注册表'
            resolved = self._tool_registry.resolve(decision.action)
            if resolved is None or resolved.executor is None:
                # 动作空间与注册表不同步属于装配错误，不允许降级成任何
                # 其它动作或模型失败。
                raise KeyError(f'认知动作 {decision.action} 未在注册表绑定执行器')
            result = await resolved.executor.execute(
                ToolInvocation(
                    tool_name=decision.action,
                    arguments={'query': decision.query},
                ),
                ToolContext(
                    stream_id=cognitive_scope.stream_id,
                    stream_kind=frame.stream_kind,
                    frame=frame,
                    turn_id=frame.turn_id,
                    snapshot_id=frame.snapshot_id,
                    person_ids=cognitive_scope.person_ids,
                ),
            )
            if not result.success:
                raise RuntimeError(
                    f'{decision.action} 执行失败：{result.error_message}'
                )
            return 'cognitive_step', decision, result.observation
        if head.action == 'silent':
            return 'silent_by_choice', head.to_decision(''), ''
        return None

    async def _stream_body(
        self,
        messages: list[dict],
        *,
        release: Callable[[list[ParseEvent]], Awaitable[None]],
        on_chunk: Callable[[dict[str, Any]], None] | None,
        body_parts: list[str],
        emoji_emotions: list[str],
        signal: asyncio.Event | None,
    ) -> None:
        """调用回复生成模型，校验完整协议后放出正文与副作用事件。

        与决策流共用 ``release``，因此分句、表情包与副作用标签的下游处理口径
        完全一致。replyer 事件先在本函数内暂存，只有完整响应含非空 ``<say>`` 且
        标签外没有裸正文时才统一放出，避免后段协议错误发生前已经发送台词、TTS
        或写入记忆。

        回复生成模型不允许再出动作头：动作已定，其职责仅为产出正文。
        出现的动作头一律忽略，不覆盖已通过校验的决策。

        :param messages: 调用方组装好的回复生成消息序列。
        :param release: 事件放行回调，与决策流同一个。
        :param on_chunk: 可选的原生分片回调，供调用方转发流式观测事件。
        :param body_parts: 正文累积列表，就地追加。
        :param emoji_emotions: 表情包目标情绪累积列表，就地追加。
        :param signal: 可选取消事件。
        :raises LlmError: 由调用处的既有分支转成失败状态；aborted 原样上抛。
        :raises ResponseProtocolError: 回复缺少非空 ``<say>`` 或出现标签外正文。
        副作用：一次模型往返；协议完整有效时通过 release 放出暂存事件。
        """
        parser = ResponseParser(implicit_say=False)
        staged_events: List[ParseEvent] = []
        staged_body_parts: List[str] = []
        staged_emoji_emotions: List[str] = []

        def stage(events: list[ParseEvent]) -> None:
            for event in events:
                if isinstance(event, DecisionEvent):
                    continue
                staged_events.append(event)
                if isinstance(event, TextEvent):
                    staged_body_parts.append(event.value)
                elif isinstance(event, EmojiEvent):
                    staged_emoji_emotions.append(event.emotion)

        assert self._replyer is not None
        async with aclosing(
            self._replyer.stream(
                messages=messages,
                temperature=self._replyer_temperature,
                max_tokens=self._replyer_max_tokens,
                signal=signal,
            )
        ) as stream:
            async for chunk in stream:
                if on_chunk is not None:
                    on_chunk(chunk)
                text = chunk.get('text')
                if not text:
                    continue
                stage(parser.push(text))
        # 先完整校验 replyer 协议，再一次性放行事件。这样即使末尾才出现裸文本，
        # 前面的台词、TTS、记忆和情绪副作用也不会已经对外生效。
        stage(parser.flush())
        if (
            not any(isinstance(event, SayEvent) for event in staged_events)
            or not ''.join(staged_body_parts).strip()
        ):
            raise ResponseProtocolError('回复生成没有产生非空 <say> 正文')
        await release(staged_events)
        body_parts.extend(staged_body_parts)
        emoji_emotions.extend(staged_emoji_emotions)

    def _parse_head(self, event: DecisionEvent, frame: DecisionFrame) -> DecisionHead:
        """把解析器动作头转换为已通过帧校验的 DecisionHead。

        认知动作不要求 reasons：理由码是回复决策的审计封闭枚举，认知动作的
        审计信息是它的 query。因此先看动作类别再决定要不要强制解析 reasons，
        否则模型只写 ``<decision action="recall" query="…"/>`` 会被误判为协议失败。

        :param event: 解析器产出的动作头原始属性。
        :param frame: 本回合固定快照。
        :return: 已完成结构与帧校验的 DecisionHead。
        :raises IllegalActionError: 属性缺失、枚举非法、目标越界或引用能力缺失。
        """
        if event.action is None:
            raise IllegalActionError('动作头缺少 action 属性')
        action = cast(ConversationAction, event.action.strip())
        reason_codes = (
            () if action in COGNITIVE_ACTIONS else _parse_code_list(event.reasons)
        )
        head = DecisionHead(
            action=action,
            target_message_ids=_parse_id_list(event.targets),
            quote_message_id=_parse_optional_id(event.quote),
            reason_codes=reason_codes,
            length=_parse_length(event.length),
            query=event.query,
            reaction=event.reaction.strip() if event.reaction is not None else None,
            reference=(
                event.reference.strip()
                if event.reference is not None and event.reference.strip()
                else None
            ),
        )
        head.validate(frame)
        return head


def _observation_messages(
    steps: Sequence[tuple[str, str, str]],
    *,
    final_round: bool,
    flattened: bool = False,
) -> list[dict[str, str]]:
    """把一轮执行的工具及其观察渲染为下一轮可读的消息。

    一条工具一块，保持因果清晰；多块共享 OBSERVATION_MAX_CHARS 总预算，
    超出部分在最后一块截断并显式标注。XML 角色模式保留 assistant 动作头与
    user 结果两条消息；工具模式已经由函数调用表达动作，不再回灌 XML：
    把调用与结果折叠成一个 user item，不重新引入 assistant 角色与第二套协议。

    认知动作与外部只读工具共用同一份明细，仅回灌措辞按名字区分：认知动作的
    入参是检索词，写作「动作 / 查询」；外部工具的入参是 JSON 对象，写作
    「工具 / 参数」。措辞若混用，模型会把 JSON 参数当自然语言检索词照抄。

    :param steps: 按执行顺序排列的（工具名、入参文本、观察正文）明细；
        XML 路径解析器只认第一个动作头，因此恒为单条。
    :param final_round: 下一轮是否已经没有认知机会；为真时追加收束指令。
    :param flattened: 是否使用工具模式的单 item 回灌。
    :return: 追加到消息序列尾部的一条或两条消息。
    """
    notice = f'\n\n{_FINAL_ROUND_NOTICE}' if final_round else ''
    if flattened:
        blocks = [
            (
                f'[已完成的工具调用]\n动作：{name}\n查询：{argument}\n\n'
                f'[工具返回]\n{observation}'
                if name in COGNITIVE_ACTIONS
                else
                f'[已完成的工具调用]\n工具：{name}\n参数：{argument}\n\n'
                f'[工具返回]\n{observation}'
            )
            for name, argument, observation in steps
        ]
        content = _clip_total('\n\n'.join(blocks), OBSERVATION_MAX_CHARS) + notice
        return [{'role': 'user', 'content': content}]
    action, query, observation = steps[0]
    return [
        {
            'role': 'assistant',
            'content': f'<decision action="{action}" query="{query}"/>',
        },
        {
            'role': 'user',
            'content': f'[检索结果] {observation}{notice}\n\n{_OUTPUT_REQUIREMENT}',
        },
    ]


def _missing_call_messages(reason: str) -> list[dict[str, str]]:
    """把一次没有工具调用的响应渲染为纠错回灌消息。

    只陈述这一轮的输出不成立并要求改用工具调用，不提示该调哪个工具：动作选择
    仍然由模型自己做，回灌一个具体动作等于替它决策。

    :param reason: 判定缺失工具调用的原因，与终局状态的 detail 同一措辞。
    :return: 追加到消息序列尾部的一条 user 消息，与观察回灌同一扁平格式。
    """
    return [{
        'role': 'user',
        'content': (
            '[无效的响应]\n'
            f'原因：{reason}\n\n'
            '这一轮的动作只能通过调用工具给出，正文不会被采纳，'
            '你刚才的输出没有生效。请重新发起一次工具调用；'
            '只输出工具调用，不要输出台词、解释或格式说明。'
        ),
    }]


def _call_fault_messages(reason: str) -> list[dict[str, str]]:
    """把一次被拒绝的工具调用渲染为纠错回灌消息。

    网关会把工具参数整段丢弃，校验层收到的可能是空对象；这类协议错误直接
    终局会把一次可自愈的故障变成用户可见的沉默。回灌只陈述拒绝原因并要求
    重新调用，不替模型补写参数，参数内容由模型重新给出。

    :param reason: 校验层给出的拒绝原因。
    :return: 追加到消息序列尾部的一条 user 消息，与观察回灌同一扁平格式。
    """
    return [{
        'role': 'user',
        'content': (
            '[被拒绝的工具调用]\n'
            f'原因：{reason}\n\n'
            '这次调用没有被执行。请重新发起工具调用，完整填写全部必填字段；'
            '只输出工具调用，不要输出解释文字。'
        ),
    }]
