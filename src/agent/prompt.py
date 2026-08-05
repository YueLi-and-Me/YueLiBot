"""主对话与主动搭话提示词组装。"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Tuple

from .character import (
    ATTENTION_PROMPT,
    BEHAVIOR_PROMPT,
    BOUNDARIES_PROMPT,
    CHARACTER_NAME,
    IDENTITY_PROMPT,
    REPLY_STYLE_PROMPT,
)
from .expression import render_expression_habits, select_expression_habits
from .vocab import EXPRESSION_IDS, GESTURE_IDS

from src.common.clock import now as current_time
from src.schedule.daily import describe_schedule

_PROTOCOL = f"""只输出下列标签，不要在标签外写正文：

<say emotion="表情" gesture="动作">真正让他看到的台词</say>

- emotion 必填，只能选：{' / '.join(EXPRESSION_IDS)}
- gesture 选填，只能选：{' / '.join(GESTURE_IDS)}
- 一次自然发言通常只用一个 <say>。确实需要停顿或情绪转折时，才拆成两个
- <say> 里面只放台词，不放动作旁白、分析过程或格式说明

下面两个标签按需追加在发言之后，两个都不写是常态：

<memory type="类别">一句完整、客观的事实</memory>
只在这一轮第一次得知值得长期记住的稳定事实时写：喜好、习惯、身份、关系、重要日期或长期计划。
临时情绪、随口玩笑、你的推测和已经记过的内容都不要写。

<mood favor="+1" energy="-1"/>
只在这一轮确实改变了你对他的亲近感、或消耗了明显精力时写。favor 与 energy 都在 -3 到 +3，
没变化的属性可以省略；普通寒暄不用硬凑 mood 标签。

<promise at="2026-08-08 20:00" what="一起打游戏"/>
只有他明确提出一个未来安排、且你确实答应了，才可以追加。at 必须是确切的本地日期和时间，
what 只写他提议的事；不确定日期、他只是随口说说、你没有答应时都不写。绝不编造约定。

标签只是外壳。先自然地把话说出来，再套上标签，别为了填标签改掉你本来想说的那句话。"""

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
    schedule_text = schedule if schedule is not None else describe_schedule(now)
    if schedule_text:
        lines.extend(['', schedule_text])
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
    name: str = CHARACTER_NAME,
    now: Optional[datetime] = None,
    persona: Optional[str] = None,
    acquaintance: Optional[str] = None,
    facts: Optional[List[str]] = None,
    episodes: Optional[List[str]] = None,
    activity: Optional[str] = None,
    schedule: Optional[str] = None,
    user_nickname: Optional[str] = None,
    relationship: Optional[str] = None,
    identity: str = IDENTITY_PROMPT,
    behavior: str = BEHAVIOR_PROMPT,
    reply_style: str = REPLY_STYLE_PROMPT,
    attention: str = ATTENTION_PROMPT,
    boundaries: str = BOUNDARIES_PROMPT,
    expression_habits: Optional[str] = None,
    tone: Optional[str] = None,
    resumption: Optional[str] = None,
) -> str:
    """组装主对话提示词，各段只承担一种职责。"""

    if now is None:
        now = datetime.fromtimestamp(current_time() / 1000)

    parts: List[str] = [
        f'你是「{name}」。',
        '',
        '# 你是谁',
        identity,
    ]
    if acquaintance:
        parts.extend(['', '# 你们的关系走到哪里了', acquaintance])
    if user_nickname or relationship:
        parts.extend(['', _relationship_context(user_nickname, relationship)])

    parts.extend(['', '# 此刻', _time_context(now, schedule)])
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
        '# 这一刻怎么接话',
        behavior,
        '',
        '# 说话的味道',
        reply_style,
        '',
        attention,
    ])
    if tone:
        parts.extend(['', tone])

    # 表达样本放在靠近输出的位置：越贴近生成，模型越容易真的照着那个语感说话。
    if expression_habits:
        parts.extend(['', '# 她平时的说法', expression_habits])

    parts.extend([
        '',
        '# 边界',
        boundaries,
        '',
        '# 输出格式',
        _PROTOCOL,
    ])
    return '\n'.join(parts)


def build_proactive_prompt(base_prompt: str, situation: str) -> str:
    """在完整人设之上追加主动搭话场景，不重复另一套人格。"""

    return '\n'.join([
        base_prompt,
        '',
        '# 这次由你先开口',
        f'你顺手留意到：{situation}',
        '',
        '这不是系统通知，也不是关怀任务。挑一个你真的会有反应的细节开口：可以接一句吐槽、好奇、共鸣，'
        '也可以只是很轻地陪一下。不要把情境原样播报给他。',
        '只说一句，最多两句。不要用「我注意到」「检测到」「你似乎正在」开头，不要用空泛问题硬拉话题，'
        '也不要固定落到休息、喝水或早点睡。',
        '',
        '大概是这种起头方式：「这局打挺久了吧」「你这个报错我刚才瞄到了，看着就烦」「……你还在啊」。'
        '也可以从你自己那边起头，说你刚在琢磨的一件小事，不用绕回他身上。',
    ])
