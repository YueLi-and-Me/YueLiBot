"""按当前对话情境从 Bot 配置候选中挑选表达样本。

本模块构造受限编号选择提示词，调用 `LlmProvider` 获取 JSON 对象，并严格把
模型返回的编号映射回候选表达习惯；解析失败不会静默生成新文本或扩展候选集合。
"""

from __future__ import annotations

from typing import Dict, List, Sequence
import asyncio
import json

from src.agent.expression import ExpressionSample
from src.llm_models.protocol import LlmProvider
from src.observe import events as trace
from src.prompts.registry import get_prompt, prompt_metadata

_SELECTION_KEY = 'selected'
# 限制模型夹带正文。
_MAX_SELECTION_CHARS = 256


def candidate_count(candidates: Sequence[ExpressionSample]) -> int:
    """候选样本总数。"""
    return len(candidates)


def build_selection_prompt(
    candidates: Sequence[ExpressionSample],
    user_text: str,
    history: Sequence[dict[str, str]],
    limit: int,
) -> str:
    """组装挑选提示词：先说职责边界，再给候选，最后限定输出。"""
    indexed: Dict[int, ExpressionSample] = {
        index: sample for index, sample in enumerate(candidates, start=1)
    }
    options = '\n'.join(
        f'{index}. {sample}' for index, sample in indexed.items()
    )
    context = json.dumps(list(history), ensure_ascii=False)
    return get_prompt('expression.select').render(
        history=context,
        user_text=user_text,
        options=options,
        limit=str(limit),
    )


def parse_selection(
    raw: str,
    candidates: Sequence[ExpressionSample],
    limit: int,
) -> List[ExpressionSample]:
    """严格解析编号数组；结构或编号不合法时直接暴露错误。"""
    if len(raw) > _MAX_SELECTION_CHARS:
        raise ValueError(f'表达挑选输出超过 {_MAX_SELECTION_CHARS} 字符')
    try:
        payload = json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        raise ValueError('表达挑选结果不是合法 JSON') from exc
    if not isinstance(payload, dict) or set(payload) != {_SELECTION_KEY}:
        raise ValueError(f'表达挑选结果必须且只能包含 {_SELECTION_KEY} 字段')

    indices = payload[_SELECTION_KEY]
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
    """一次独立的挑选调用，输出受限于固定候选编号。"""

    def __init__(
        self,
        provider: LlmProvider,
        temperature: float,
        max_tokens: int | None,
        candidates: Sequence[ExpressionSample],
    ) -> None:
        """创建一次独立的表达习惯选择器。

        :param provider: 提供流式文本生成能力的模型客户端。
        :param temperature: 传给模型的采样温度，具体范围由 provider 实现约束。
        :param max_tokens: 单次选择请求的最大输出 token 数；`None` 表示不额外指定。
        :param candidates: 可供模型选择的候选表达，不能为空。
        :raises ValueError: `candidates` 为空。
        :side_effects: 保存候选的不可变副本，不执行模型请求。
        """
        if not candidates:
            raise ValueError('表达选择候选不能为空')
        self._provider = provider
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._candidates = tuple(candidates)

    async def select(
        self,
        user_text: str,
        history: Sequence[dict[str, str]],
        limit: int = 4,
        signal: asyncio.Event | None = None,
    ) -> List[ExpressionSample]:
        """请求模型从候选表达中选择不超过上限的样本。

        :param user_text: 当前用户消息，用于判断表达习惯是否贴合语境。
        :param history: 最近对话历史，每项包含 `role` 与 `content` 字段。
        :param limit: 最多允许返回的候选数量，默认值为 4。
        :param signal: 可选取消事件；触发后由 provider 终止流式请求。
        :return: 按模型选择顺序排列的候选表达列表。
        :raises ValueError: 模型输出不是限定 JSON、编号越界、重复或超过 `limit`。
        :raises Exception: provider 的网络、鉴权或流式读取错误向调用方传播。
        :side_effects: 发起一次模型请求并记录 `llm_request` 观测事件；不修改候选。
        :performance: 输出解析按候选数量线性构造索引，模型请求耗时占主要成本。
        """
        prompt = build_selection_prompt(self._candidates, user_text, history, limit)
        raw = ''
        reasoning_length = 0
        messages = [{'role': 'system', 'content': prompt}]
        trace.emit(
            'llm_request',
            messages=messages,
            temperature=self._temperature,
            maxTokens=self._max_tokens,
            **prompt_metadata('expression.select', ('expression.select',)),
        )
        async for chunk in self._provider.stream(
            messages=messages,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            response_format={'type': 'json_object'},
            signal=signal,
        ):
            text = chunk.get('text')
            if text:
                raw += text
            reasoning = chunk.get('reasoning')
            if isinstance(reasoning, str):
                reasoning_length += len(reasoning)
        try:
            return parse_selection(raw, self._candidates, limit)
        except ValueError as exc:
            raise ValueError(
                f'{exc}（正文字符={len(raw)}，推理字符={reasoning_length}）'
            ) from exc
