"""从 Bot 配置中抽取并渲染表达习惯。"""

from __future__ import annotations

from typing import List, Optional, Sequence
import random

ExpressionSample = str


def sample_expression_habits(
    candidates: Sequence[ExpressionSample],
    limit: int,
    rng: Optional[random.Random] = None,
) -> List[ExpressionSample]:
    """从配置候选中无放回抽取；代码不补充任何表达内容。"""

    if limit <= 0 or not candidates:
        return []
    picker = rng or random
    return picker.sample(list(candidates), min(limit, len(candidates)))


def render_expression_habits(samples: Sequence[ExpressionSample]) -> str:
    """渲染成提示词块。空样本返回空串，让调用方直接跳过这一段。"""

    if not samples:
        return ''
    return '\n'.join([
        '下面是配置中与这一轮最贴合的表达习惯，只借语感，不要逐句照抄：',
        *[f'- {sample}' for sample in samples],
    ])
