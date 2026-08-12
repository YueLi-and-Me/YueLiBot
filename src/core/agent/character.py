"""会话级语调抽取逻辑。

本模块根据配置概率从候选语调中最多选择一项，不生成或修改人格文本；聊天服务在创建
新会话时调用该函数，并将结果写入会话上下文。
"""

from __future__ import annotations

from typing import List, Optional
import random


def pick_tone(
    probability: float,
    variants: List[str],
    rng: Optional[random.Random] = None,
) -> Optional[str]:
    """按配置概率从候选语调中选择一项，仅影响当前会话轮次。

    :param probability: 命中概率，建议范围为 ``0.0`` 到 ``1.0``；小于等于 ``0``
            时始终不选择，大于 ``1.0`` 时按始终命中处理。
    :param variants: 可供选择的非空语调文本列表；空列表始终返回 ``None``。
    :param rng: 可选的随机数生成器；省略时使用模块级随机源，测试场景可传入带种子的实例。

    :return: 命中的候选语调文本；未命中或没有候选时返回 ``None``。

    :raises IndexError: 自定义随机源返回了不适用于 ``variants`` 的索引时由 ``choice`` 抛出。
    :raises TypeError: 参数类型不支持比较、随机采样或选择操作时抛出。

    副作用：
        读取随机源状态；不修改候选列表和持久化数据。
    """

    if probability <= 0 or not variants:
        return None
    picker = rng or random
    if picker.random() > probability:
        return None
    return picker.choice(variants)
