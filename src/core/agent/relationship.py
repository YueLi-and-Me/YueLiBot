"""根据好感度划分关系深度标签。

本模块把 0~100 的数值好感度映射为固定的关系等级，供提示词和观察面板使用；它不
推导人物性格、语气或额外行为。
"""

from __future__ import annotations


def relationship_tier(intimacy: float) -> str:
    """将 0 到 100 的好感度映射为固定的关系深度标签。

    :param intimacy: 好感度数值，允许范围为 ``0.0`` 到 ``100.0``，包含边界。

    :return: ``初识``、``熟悉``、``信任``、``重要`` 或 ``深厚`` 之一；区间按
        ``20``、``45``、``70`` 和 ``88`` 分界。

    :raises ValueError: 好感度不在 ``0.0`` 到 ``100.0`` 范围内。
    :raises TypeError: 好感度不支持数值比较时抛出。
    """
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
