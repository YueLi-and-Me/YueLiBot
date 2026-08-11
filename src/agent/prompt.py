"""主对话与主动搭话提示词组装。"""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional, Tuple

from .expression import render_expression_habits, select_expression_habits
from .vocab import EXPRESSION_IDS, GESTURE_IDS

from src.common.clock import now as current_time
from src.prompts.registry import get_prompt

# 重逢措辞的档位。写成一张有序表而不是几个散落的常量：
# 它们同属一条曲线，单独调任何一个都要看着相邻档位，拆开放反而更难维护。
# 顺序由 pytests 断言守住，不靠人记。第三档携带具体天数，由函数动态生成。
RESUMPTION_TIERS: List[Tuple[int, str]] = [
    (6 * 60 * 60_000, '距离你们上次说话过了几个小时。'),
    # 上界是 24 小时而不是更久：「隔了一夜」必须在这一档的每一个取值上都为真。
    # 超过一天一律走下面的天数那一行——天数是算出来的，说多久就是多久。
    (24 * 60 * 60_000, '距离你们上次说话隔了一夜。'),
]


def _time_context(now: datetime, schedule: Optional[str] = None) -> str:
    hour = now.hour
    if hour < 5:
        period = '凌晨'
    elif hour < 9:
        period = '清晨'
    elif hour < 12:
        period = '上午'
    elif hour < 14:
        period = '中午'
    elif hour < 18:
        period = '下午'
    elif hour < 23:
        period = '晚上'
    else:
        period = '深夜'
    weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
    lines = [
        f'现在是 {now.year}年{now.month}月{now.day}日{weekdays[now.weekday()]}，{period}{hour}点{now.minute:02d}分。',
        '时间只是这段对话的背景。除非他正在聊作息、饭点或时间本身，否则不要像报时一样主动提起；'
        '真要关心也只自然带一句，不要每到固定时段重复问候。',
    ]
    if schedule:
        lines.extend(['', schedule])
    return '\n'.join(lines)


def _relationship_context(user_nickname: Optional[str], relationship: Optional[str]) -> str:
    lines: List[str] = []
    if user_nickname:
        lines.append(f'他希望你叫他「{user_nickname}」。只在自然需要称呼时用，不要每句话都带名字。')
    if relationship:
        lines.append(
            f'你把他当{relationship}看待。这会影响你的分寸和亲近感，但不要反复声明这层关系，'
            '也不要把称呼当成口头禅。'
        )
    return '\n'.join(lines)


def describe_resumption(gap_ms: int) -> str:
    """把静默时长翻译成一句陈述。只说过了多久，不说她该有什么情绪。"""
    for threshold, description in RESUMPTION_TIERS:
        if gap_ms < threshold:
            return description
    days = gap_ms // (24 * 60 * 60_000)
    return f'距离你们上次说话已经过去 {days} 天。'


def build_system_prompt(
    name: str,
    birthday: str,
    personality: str,
    reply_style: str,
    now: Optional[datetime] = None,
    persona: Optional[str] = None,
    acquaintance: Optional[str] = None,
    facts: Optional[List[str]] = None,
    episodes: Optional[List[str]] = None,
    activity: Optional[str] = None,
    schedule: Optional[str] = None,
    user_nickname: Optional[str] = None,
    relationship: Optional[str] = None,
    expression_habits: Optional[str] = None,
    tone: Optional[str] = None,
    resumption: Optional[str] = None,
    aliases: Optional[List[str]] = None,
    platform_name: Optional[str] = None,
) -> str:
    """组装主对话提示词，各段只承担一种职责。"""

    if now is None:
        now = datetime.fromtimestamp(current_time() / 1000)

    parts: List[str] = [
        f'你是「{name}」。',
        '',
        '# 你是谁',
        personality,
    ]
    parsed_birthday: date | None = None
    if birthday:
        parsed_birthday = date.fromisoformat(birthday)
        age = now.year - parsed_birthday.year - (
            (now.month, now.day) < (parsed_birthday.month, parsed_birthday.day)
        )
        parts.extend(['', f'你今年 {age} 岁。'])
    self_names = [value for value in [*(aliases or []), platform_name] if value and value != name]
    if self_names:
        unique_names = list(dict.fromkeys(self_names))
        parts.extend([
            '',
            f'别人也可能用这些名字叫你：{"、".join(unique_names)}。这些都是你的称呼。',
        ])
    if acquaintance:
        parts.extend(['', '# 你们的关系走到哪里了', acquaintance])
    if user_nickname or relationship:
        parts.extend(['', _relationship_context(user_nickname, relationship)])

    parts.extend(['', '# 此刻', _time_context(now, schedule)])
    if (
        parsed_birthday is not None
        and (parsed_birthday.month, parsed_birthday.day) == (now.month, now.day)
    ):
        parts.append('今天是你的生日。')
    if resumption:
        parts.extend(['', resumption])
    if persona:
        parts.extend(['', persona])
    if activity:
        parts.extend([
            '',
            '# 眼前的一点情境',
            activity,
            '这只是你顺眼得到的背景，不是监控报告。和当前话题无关就别提，也不要复述成「我看到你正在……」。',
        ])
    if facts:
        parts.extend([
            '',
            '# 你早就知道的事',
            *[f'- {fact}' for fact in facts],
            '把这些当成相处已久留下的常识。用得上时自然接住，用不上就放着；不要逐条复述给他听。',
        ])
    if episodes:
        parts.extend([
            '',
            '# 最近留下的聊天回想',
            *[f'- {episode}' for episode in episodes],
            '回想只用来理解没说完的话和关系变化，不要为了证明记得而主动翻旧账。',
        ])

    parts.extend([
        '',
        '# 说话的味道',
        reply_style,
    ])
    if tone:
        parts.extend(['', tone])

    # 表达样本放在靠近输出的位置：越贴近生成，模型越容易真的照着那个语感说话。
    if expression_habits:
        parts.extend(['', '# 她平时的说法', expression_habits])

    parts.extend([
        '',
        '# 有一说一',
        get_prompt('chat.discipline').text,
        '',
        '# 边界',
        get_prompt('chat.boundaries').text,
        '',
        '# 输出格式',
        get_prompt('chat.protocol').render(
            emotions=' / '.join(EXPRESSION_IDS),
            gestures=' / '.join(GESTURE_IDS),
        ),
    ])
    return '\n'.join(parts)


def build_proactive_prompt(base_prompt: str, situation: str) -> str:
    """在完整人设之上追加主动搭话场景，不重复另一套人格。"""

    return '\n\n'.join([
        base_prompt,
        get_prompt('chat.proactive').render(situation=situation),
    ])
