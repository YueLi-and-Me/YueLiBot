"""按对话情境从固定候选中挑选表达样本。"""

from __future__ import annotations

from typing import Dict, List, Sequence

import asyncio
import json

from src.agent.expression import EXPRESSION_HABITS, ExpressionSample
from src.llm_models.protocol import LlmProvider
from src.observe import events as trace
from src.prompts.registry import get_prompt, prompt_metadata

# proactive 仅用于主动搭话，不参与回复挑选。
_CANDIDATES: List[ExpressionSample] = [
    sample
    for bucket, samples in EXPRESSION_HABITS.items()
    if bucket != 'proactive'
    for sample in samples
]

# 编号与提示词保持一致。
_INDEXED: Dict[int, ExpressionSample] = {i: s for i, s in enumerate(_CANDIDATES, start=1)}

_SELECTION_KEY = 'selected'
# 限制模型夹带正文。
_MAX_SELECTION_CHARS = 256


def candidate_count() -> int:
    """候选样本总数。"""
    return len(_CANDIDATES)


def build_selection_prompt(
    user_text: str,
    history: Sequence[dict[str, str]],
    limit: int,
) -> str:
    """组装挑选提示词：先说职责边界，再给候选，最后限定输出。"""
    options = '\n'.join(
        f'{index}. 当「{situation}」时，{style}'
        for index, (situation, style) in _INDEXED.items()
    )
    context = json.dumps(list(history), ensure_ascii=False)
    return get_prompt('expression.select').render(
        history=context,
        user_text=user_text,
        options=options,
        limit=str(limit),
    )


def parse_selection(raw: str, limit: int) -> List[ExpressionSample]:
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
    for index in indices:
        if not isinstance(index, int) or isinstance(index, bool):
            raise ValueError(f'selected 只能是整数编号，收到 {index!r}')
        if index not in _INDEXED:
            raise ValueError(f'编号 {index} 不在 1~{len(_CANDIDATES)} 范围内')
        if index in seen:
            raise ValueError(f'编号 {index} 重复')
        seen.add(index)
        picked.append(_INDEXED[index])
    return picked


class ExpressionSelector:
    """一次独立的挑选调用，输出受限于固定候选编号。"""

    def __init__(
        self,
        provider: LlmProvider,
        temperature: float,
        max_tokens: int | None,
    ) -> None:
        self._provider = provider
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def select(
        self,
        user_text: str,
        history: Sequence[dict[str, str]],
        limit: int = 4,
        signal: asyncio.Event | None = None,
    ) -> List[ExpressionSample]:
        prompt = build_selection_prompt(user_text, history, limit)
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
            return parse_selection(raw, limit)
        except ValueError as exc:
            raise ValueError(
                f'{exc}（正文字符={len(raw)}，推理字符={reasoning_length}）'
            ) from exc
