"""角色表情与动作的固定词表。

本模块集中定义后端可识别的表情和动作标识，供表达选择、提示词校验和渲染层状态同步
使用；列表内容必须与角色视图支持的标识保持一致。
"""

EMOTIONS = [
    'normal', 'happy', 'smile', 'shy', 'sad', 'cry', 'angry', 'pout',
    'surprised', 'sleepy', 'speechless', 'dizzy', 'starry_eyes',
    'heart_eyes', 'smug', 'thinking',
]
EXPRESSION_IDS = [e for e in EMOTIONS if e != 'thinking'] + ['thinking']

GESTURES = ['heart', 'hold_star', 'clutch_chest', 'pray', 'tongue_out', 'head_fly']
GESTURE_IDS = GESTURES
