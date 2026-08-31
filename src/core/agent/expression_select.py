"""按当前对话情境从 expressions 表候选池中挑选表达样本。

本模块构造受限编号选择提示词，调用 `LlmProvider` 获取 JSON 对象，并严格将
模型返回的编号映射回候选表达样本；解析失败时不生成新文本、不扩展候选集合。

选择提示词只向模型列出候选的情境描述，模型返回命中的情境编号；说法示例
（style）不进选择提示词，是选中之后才拼进注入文本的载荷。若将 style 一并提供
给选择模型，模型会偏向表述的表面质量，偏离情境匹配的目标。
"""

from __future__ import annotations

from typing import Dict, List, Sequence
import asyncio
import json

from src.core.agent.expression import ExpressionSample
from src.core.agent.sub_agent import SubAgentCall, run_sub_agent
from src.core.llm_models.protocol import LlmProvider
from src.core.prompts.registry import get_prompt, prompt_metadata

_SELECTION_KEY = 'selected'
# 限制模型输出额外正文。
_MAX_SELECTION_CHARS = 256


def _options_text(candidates: Sequence[ExpressionSample]) -> str:
    """把候选池映射为从 1 开始编号的情境列表，供提示词与渲染参数共用。"""

    return '\n'.join(
        f'{index}. {sample.situation}'
        for index, sample in enumerate(candidates, start=1)
    )


def _history_text(history: Sequence[dict[str, str]]) -> str:
    """序列化最近对话历史，供提示词与渲染参数共用。"""

    return json.dumps(list(history), ensure_ascii=False)


def build_selection_prompt(
    candidates: Sequence[ExpressionSample],
    user_text: str,
    history: Sequence[dict[str, str]],
    limit: int,
) -> str:
    """组装受限表达选择提示词，候选以从 1 开始的编号情境列出。

    :param candidates: 按展示顺序排列的候选表达样本；只有 situation 进入提示词。
    :param user_text: 当前用户消息，用于模型判断语境匹配度。
    :param history: 最近对话历史；每项应包含 ``role`` 和 ``content`` 字段。
    :param limit: 模型最多可以返回的候选数量；由调用方负责传入有效上限。

    :return: 包含对话上下文、编号情境和输出上限约束的完整提示词。

    :raises KeyError: 表达选择提示词未在提示词目录中注册时抛出。
    :raises TypeError: 历史消息或候选文本无法序列化、格式化时抛出。
    """
    return get_prompt('expression.select').render(
        history=_history_text(history),
        user_text=user_text,
        options=_options_text(candidates),
        limit=str(limit),
    )


def parse_selection(
    raw: str,
    candidates: Sequence[ExpressionSample],
    limit: int,
) -> List[ExpressionSample]:
    """严格解析模型返回的编号数组，并映射回候选表达样本。

    :param raw: 模型返回的 JSON 文本，长度不得超过内部协议上限。
    :param candidates: 与提示词编号顺序一致的候选表达样本序列。
    :param limit: 允许选择的最大数量；必须为非负整数。

    :return: 按模型返回顺序排列的候选表达样本列表。

    :raises ValueError: 响应超长、JSON 结构不符、编号非整数、编号越界、编号重复或超过数量上限。
    :raises json.JSONDecodeError: 不直接向上抛出，解析错误会转换为 ``ValueError``。
    """
    # 先限制原始响应长度，防止额外正文占用解析与日志空间。
    if len(raw) > _MAX_SELECTION_CHARS:
        raise ValueError(f'表达挑选输出超过 {_MAX_SELECTION_CHARS} 字符')
    try:
        payload = json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        raise ValueError('表达挑选结果不是合法 JSON') from exc
    if not isinstance(payload, dict) or set(payload) != {_SELECTION_KEY}:
        raise ValueError(f'表达挑选结果必须且只能包含 {_SELECTION_KEY} 字段')

    indices = payload[_SELECTION_KEY]
    # 只接受固定字段和整数编号，禁止模型通过额外结构扩展选择协议。
    if not isinstance(indices, list):
        raise ValueError('selected 必须是数组')
    if len(indices) > limit:
        raise ValueError(f'挑选了 {len(indices)} 条，超过上限 {limit}')

    picked: List[ExpressionSample] = []
    seen: set[int] = set()
    indexed: Dict[int, ExpressionSample] = {
        candidate_index: sample
        for candidate_index, sample in enumerate(candidates, start=1)
    }
    for index in indices:
        # 编号从 1 开始且不可重复，保持候选顺序与提示词展示一致。
        if not isinstance(index, int) or isinstance(index, bool):
            raise ValueError(f'selected 只能是整数编号，收到 {index!r}')
        if index not in indexed:
            raise ValueError(f'编号 {index} 不在 1~{len(candidates)} 范围内')
        if index in seen:
            raise ValueError(f'编号 {index} 重复')
        seen.add(index)
        picked.append(indexed[index])
    return picked


class ExpressionSelector:
    """一次独立的挑选调用，输出受限于固定候选编号。

    候选池按会话、按轮变化，因此不再是构造期固定值，而是 ``select()`` 的入参；
    选择器本身只持有模型调用参数，可跨轮复用。
    """

    def __init__(
        self,
        provider: LlmProvider,
        temperature: float,
        max_tokens: int | None,
    ) -> None:
        """创建表达选择器。

        :param provider: 提供流式文本生成能力的模型客户端。
        :param temperature: 传给模型的采样温度，具体范围由 provider 实现约束。
        :param max_tokens: 单次选择请求的最大输出 token 数；`None` 表示不额外指定。
        副作用：保存模型调用参数，不执行模型请求。
        """
        self._provider = provider
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def select(
        self,
        user_text: str,
        history: Sequence[dict[str, str]],
        candidates: Sequence[ExpressionSample],
        limit: int = 4,
        signal: asyncio.Event | None = None,
    ) -> List[ExpressionSample]:
        """请求模型从候选池中选择不超过上限的表达样本。

        :param user_text: 当前用户消息，用于判断表达样本是否贴合语境。
        :param history: 最近对话历史，每项包含 `role` 与 `content` 字段。
        :param candidates: 本轮候选池；不能为空，由调用方在候选不足时跳过本轮选择。
        :param limit: 最多允许返回的候选数量，默认值为 4。
        :param signal: 可选取消事件；触发后由 provider 终止流式请求。
        :return: 按模型选择顺序排列的候选表达样本列表。
        :raises ValueError: 候选为空，或模型输出不是限定 JSON、编号越界、重复或超过 `limit`。
        :raises Exception: provider 的网络、鉴权或流式读取错误向调用方传播。
        副作用：发起一次模型请求并记录 `llm_request` 观测事件；不修改候选。
        :performance: 输出解析按候选数量线性构造索引，模型请求耗时占主要成本。
        """
        if not candidates:
            raise ValueError('表达选择候选不能为空')
        # 提示词只携带候选情境的编号，避免模型重新生成表达文本破坏候选约束。
        prompt = build_selection_prompt(candidates, user_text, history, limit)
        render_params = {
            'expression.select': {
                'history': _history_text(history),
                'user_text': user_text,
                'options': _options_text(candidates),
                'limit': str(limit),
            },
        }
        # 只发系统提示词会被部分服务商拒绝；统一执行器会补齐最小 user 消息。
        result = await run_sub_agent(SubAgentCall(
            task='expression',
            provider=self._provider,
            messages=[{'role': 'system', 'content': prompt}],
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            response_format={'type': 'json_object'},
            signal=signal,
            render_params=render_params,
            trace_extra=prompt_metadata('expression.select', ('expression.select',)),
        ))
        raw = result.text
        reasoning_length = result.reasoning_chars
        try:
            # 解析失败附带正文和推理长度，便于区分协议错误与模型输出过长。
            return parse_selection(raw, candidates, limit)
        except ValueError as exc:
            raise ValueError(
                f'{exc}（正文字符={len(raw)}，推理字符={reasoning_length}）'
            ) from exc
