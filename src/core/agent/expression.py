"""表达习惯候选抽取逻辑。

本模块仅从配置提供的表达习惯序列中无放回抽样，并返回有限数量的原始文本；表达选择
服务负责将抽样结果组合进模型提示词。
"""

from __future__ import annotations

from typing import List, Optional, Sequence
import random

ExpressionSample = str


def sample_expression_habits(
    candidates: Sequence[ExpressionSample],
    limit: int,
    rng: Optional[random.Random] = None,
) -> List[ExpressionSample]:
    """从配置候选中无放回抽取有限数量的表达习惯文本。

    :param candidates: 配置提供的表达习惯序列；函数只返回其中已有文本。
    :param limit: 最大抽取数量；小于等于 ``0`` 时返回空列表，超过候选数时按候选数截断。
    :param rng: 可选的随机数生成器；省略时使用模块级随机源。

    :return: 按随机顺序排列且不重复的表达习惯列表。

    :raises TypeError: 候选序列不可转换为列表或随机源不支持抽样时抛出。
    :raises ValueError: 自定义随机源拒绝给定抽样范围时抛出。

    副作用：
        读取随机源状态；不修改输入序列。
    """

    if limit <= 0 or not candidates:
        return []
    picker = rng or random
    return picker.sample(list(candidates), min(limit, len(candidates)))


def render_expression_habits(samples: Sequence[ExpressionSample]) -> str:
    """将表达习惯样本渲染为模型提示词中的独立文本块。

    :param samples: 已选中的表达习惯文本序列；空序列表示不生成该提示词块。

    :return: 以说明行和项目列表组成的提示词文本；输入为空时返回空字符串。

    :raises TypeError: 样本元素不是可格式化文本时由字符串格式化操作触发。
    """

    if not samples:
        return ''
    return '\n'.join([
        '下面是配置中与这一轮最贴合的表达习惯，只借语感，不要逐句照抄：',
        *[f'- {sample}' for sample in samples],
    ])
