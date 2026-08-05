"""角色词表（从 src/shared/character-vocab.ts 移植）。"""

EMOTIONS = [
    'normal', 'happy', 'smile', 'shy', 'sad', 'cry', 'angry', 'pout',
    'surprised', 'sleepy', 'speechless', 'dizzy', 'starry_eyes',
    'heart_eyes', 'smug', 'thinking',
]
EXPRESSION_IDS = [e for e in EMOTIONS if e != 'thinking'] + ['thinking']

GESTURES = ['heart', 'hold_star', 'clutch_chest', 'pray', 'tongue_out', 'head_fly']
GESTURE_IDS = GESTURES
