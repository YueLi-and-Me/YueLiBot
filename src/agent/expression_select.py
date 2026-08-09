"""按对话情境从固定候选中挑选表达样本。"""

from __future__ import annotations

from typing import Dict, List, Sequence

import asyncio
import json

from src.agent.expression import EXPRESSION_HABITS, ExpressionSample
from src.llm_models.protocol import LlmProvider

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
    return '\n'.join([
        '你在为一次回复挑选表达方式。你不是她本人，不要替她说话，也不要写回复内容——'
        '写回复是另一个环节的事，你只输出编号。',
        '',
        '下面这段聊天记录只是最近的片段。他们之间还有更早的经历和更多了解，'
        '这里没有全部展现出来。不要因为记录里没提到，就当作没发生过。',
        '',
        '最近的对话：',
        context,
        '',
        f'他刚说的这句：{user_text}',
        '',
        '可选的表达方式：',
        options,
        '',
        f'挑最贴合他这句话的，最多 {limit} 条。判断依据：',
        '1. 他这句话在做什么——分享、抱怨、开玩笑、认真问事、还是在问她本人',
        '2. 他此刻的情绪',
        '3. 这句话给了多少可接的内容，短促的一句不需要长回应',
        '',
        '宁可少选也不要凑数：选进来一条不贴合的，她就会照着它说出不合时宜的话。'
        '实在没有贴合的，输出空数组。',
        '',
        '只输出严格 JSON，例如 {"selected": [3, 11]}；不要输出原因或其他字段。',
    ])


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
        async for chunk in self._provider.stream(
            messages=[{'role': 'system', 'content': prompt}],
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
