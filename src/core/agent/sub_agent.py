"""可复用的一次性模型子任务执行器。

表达选择、表情包选择、回复必要性判断、工具决策等小任务都只需要「独立提示词 +
流式收完文本 + 返回给调用方解析」，不应进入主对话循环。本模块提供统一执行器，
集中处理消息校验、``llm_request`` 观测事件、渲染参数绑定与文本聚合；上下文截取、
输出解析、失败降级仍由各任务调用方负责，避免把任务特有语义混入执行层。

依赖：``LlmProvider`` 流式协议、快照上下文绑定与 trace 事件；被
``ExpressionSelector`` 等一次性模型任务调用，不反向依赖聊天服务。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence
import asyncio

from src.core.llm_models.protocol import LlmProvider
from src.core.llm_models.snapshot import bind_render_params
from src.core.observe import events as trace


# 兼容接口普遍要求请求中至少存在一条 user 消息；调用方只给 system 提示词时，
# 执行器补上这条最小指令，避免服务商以「No user query found」拒绝请求。
DEFAULT_USER_PROMPT = '请按上述要求输出结果。'

_ResponseFormat = Dict[str, str]
_RenderParams = Dict[str, Dict[str, str]]


@dataclass(frozen=True)
class SubAgentResult:
    """一次子代理调用的原始文本输出。

    :ivar text: 模型产生的完整正文，不包含推理字段。
    :ivar reasoning_chars: ``reasoning`` 增量累计字符数，供解析失败诊断区分
        预算是否被思考内容占用。
    """

    text: str
    reasoning_chars: int = 0


@dataclass(frozen=True)
class SubAgentCall:
    """描述一次待执行的子代理调用。

    :ivar task: 子任务名，例如 ``expression`` / ``emoji`` / ``reply_necessity``；
        写入 trace 供观察面板按任务过滤，不能为空。
    :ivar provider: 提供流式文本输出的模型客户端；通常为任务级 ``ModelRouter``。
    :ivar messages: OpenAI 风格消息列表；至少一条，允许只有 system，
        执行时会自动补齐 user 消息。
    :ivar temperature: 采样温度，默认 ``0.85``。
    :ivar max_tokens: 最大输出 token 数，``None`` 表示不额外限制。
    :ivar response_format: 可选结构化输出格式。
    :ivar signal: 可选的取消事件，原样传给 provider。
    :ivar render_params: 提示词渲染参数，进入失败快照与观察面板。
    :ivar trace_extra: 追加到 ``llm_request`` 事件的额外字段，通常放置
        ``promptId`` / ``promptHash`` 等任务级元数据。
    """

    task: str
    provider: LlmProvider
    messages: Sequence[Dict[str, Any]]
    temperature: float = 0.85
    max_tokens: int | None = None
    response_format: _ResponseFormat | None = None
    signal: asyncio.Event | None = None
    render_params: _RenderParams | None = None
    trace_extra: Dict[str, Any] = field(default_factory=dict)


def _messages_with_user_turn(
    messages: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """复制消息并在缺少 user 角色时补齐最小指令。

    :param messages: 调用方准备的消息列表。
    :return: 保证至少包含一条 ``role=user`` 消息的新列表。
    副作用：只复制映射，不修改调用方传入的字典。
    """
    normalized = [dict(message) for message in messages]
    if not any(str(message.get('role') or '').strip() == 'user' for message in normalized):
        normalized.append({'role': 'user', 'content': DEFAULT_USER_PROMPT})
    return normalized


async def run_sub_agent(call: SubAgentCall) -> SubAgentResult:
    """执行一次流式子代理调用并聚合完整文本。

    :param call: 已组装的子代理调用描述。
    :return: 含完整正文与推理字符数的结果。
    :raises ValueError: 任务名为空或消息列表为空。
    :raises Exception: provider 的网络、鉴权、协议或取消错误原样向调用方传播，
        由调用方决定记录快照、降级或中断。
    副作用：登记 ``llm_request`` 观测事件、绑定渲染参数并消费模型流；不解析输出、
        不写数据库。
    :performance: 文本按增量拼接，空间开销与输出长度线性相关。
    """
    task = call.task.strip()
    if not task:
        raise ValueError('子代理任务名不能为空')
    if not call.messages:
        raise ValueError('子代理 messages 不能为空')

    # 在发起前补齐 user 消息并登记观测，使失败快照与实际发送内容一致。
    messages = _messages_with_user_turn(call.messages)
    render_params = call.render_params or {}
    trace_fields = dict(call.trace_extra)
    trace_fields.update({
        'task': task,
        'messages': messages,
        'temperature': call.temperature,
        'maxTokens': call.max_tokens,
        'renderParams': render_params,
    })
    trace.emit('llm_request', **trace_fields)
    bind_render_params(render_params)

    text_parts: List[str] = []
    reasoning_chars = 0
    async for chunk in call.provider.stream(
        messages=messages,
        temperature=call.temperature,
        max_tokens=call.max_tokens,
        response_format=call.response_format,
        signal=call.signal,
    ):
        text = chunk.get('text')
        if isinstance(text, str) and text:
            text_parts.append(text)
        reasoning = chunk.get('reasoning')
        if isinstance(reasoning, str):
            reasoning_chars += len(reasoning)

    return SubAgentResult(
        text=''.join(text_parts),
        reasoning_chars=reasoning_chars,
    )
