"""关系深度分档。"""

from __future__ import annotations


def relationship_tier(intimacy: float) -> str:
    """将好感度压成中立的关系深度标签，不推导表达风格。"""
    if not 0.0 <= intimacy <= 100.0:
        raise ValueError(f'好感度超出 0~100：{intimacy}')
    if intimacy < 20.0:
        return '初识'
    if intimacy < 45.0:
        return '熟悉'
    if intimacy < 70.0:
        return '信任'
    if intimacy < 88.0:
        return '重要'
    return '深厚'
