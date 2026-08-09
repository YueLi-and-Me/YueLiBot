"""关系分寸词汇：关系深度分档与受限动作白名单。

只回答「当前关系事实允许哪些动作」。规划调用与计划容器在
`src.agent.turn_plan`；拆开是因为 persona 只需要分档，不需要规划器。
"""

from __future__ import annotations

from typing import Dict, Literal, Tuple


RelationshipAction = Literal[
    'ignore',
    'keep_distance',
    'be_natural',
    'take_seriously',
    'hold_boundary',
]

_ACTION_INSTRUCTIONS: Dict[RelationshipAction, str] = {
    'ignore': '本轮不刻意表现关系远近。',
    'keep_distance': '本轮保持初识分寸。',
    'be_natural': '本轮按已有熟悉程度自然回应。',
    'take_seriously': '本轮认真对待对方处境，但表达方式服从你的人设。',
    'hold_boundary': '本轮优先守住你的身份和边界。',
}


def action_instruction(action: RelationshipAction) -> str:
    """查出关系动作对应的固定指令。"""
    return _ACTION_INSTRUCTIONS[action]


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


def allowed_relationship_actions(intimacy: float) -> Tuple[RelationshipAction, ...]:
    """按关系深度给规划器动作白名单，避免它越过当前关系事实。"""
    tier = relationship_tier(intimacy)
    actions: list[RelationshipAction] = ['ignore']
    if tier == '初识':
        actions.append('keep_distance')
    else:
        actions.append('be_natural')
    if tier in ('重要', '深厚'):
        actions.append('take_seriously')
    actions.append('hold_boundary')
    return tuple(actions)
