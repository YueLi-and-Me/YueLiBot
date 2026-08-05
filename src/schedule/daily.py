"""
24 小时固定日程表（兜底用）。直接移植自 src/core/schedule/daily.ts。

纯函数，不碰数据库也不碰时钟——时间一律从参数传入。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List


@dataclass
class Slot:
    from_hour: int   # 起始小时（含）
    activity: str
    doing: str
    mood: str


SCHEDULE: List[Slot] = [
    Slot(0,  'sleep',     '在睡觉',                 '被叫醒会迷迷糊糊，带点起床气'),
    Slot(7,  'wake',      '刚醒，还在发呆',          '声音带点刚睡醒的沙沙感'),
    Slot(8,  'breakfast', '在吃早饭',               '还没完全清醒，偶尔会走神'),
    Slot(9,  'study',     '在看书、写东西',           '专注，但很乐意被打断'),
    Slot(12, 'lunch',     '在吃午饭',               '暂时把上午没想通的事放到一边'),
    Slot(13, 'afternoon', '在做自己的事，偶尔发呆',   '午后有点懒洋洋'),
    Slot(18, 'dinner',    '在吃晚饭',               '慢慢从白天的节奏里松下来'),
    Slot(20, 'bath',      '刚洗完澡，头发还湿着',     '放松，语气软'),
    Slot(21, 'relax',     '窝着刷手机、听歌',         '最闲最想聊天的时段'),
    Slot(23, 'winddown',  '准备睡了，还在赖一会儿',    '有点困，也有点舍不得结束聊到一半的话'),
]

SLEEP_FROM = 0
SLEEP_UNTIL = 7


def slot_at(now: datetime) -> Slot:
    h = now.hour
    for s in reversed(SCHEDULE):
        if h >= s.from_hour:
            return s
    return SCHEDULE[0]


def is_asleep(now: datetime) -> bool:
    return SLEEP_FROM <= now.hour < SLEEP_UNTIL


def minutes_until_sleep(now: datetime) -> int:
    if is_asleep(now):
        return 0
    # 次日 00:00
    bed = now.replace(hour=0, minute=0, second=0, microsecond=0)
    bed = bed + timedelta(days=1)
    return round((bed.timestamp() - now.timestamp()) / 60)


def describe_schedule(now: datetime) -> str:
    slot = slot_at(now)
    lines = [f'此刻你{slot.doing}。{slot.mood}。']
    if is_asleep(now):
        lines.append('你正在睡觉，如果他现在找你说话，你是被他叫醒的 —— 反应要符合刚被叫醒的状态。')
    else:
        left = minutes_until_sleep(now)
        if left <= 60:
            lines.append('已经很晚了。困意可以自然落在句子里，但除非正聊到作息，不要固定催他睡觉。')
    return '\n'.join(lines)


def activities_between(from_dt: datetime, to_dt: datetime) -> List[str]:
    out: List[str] = []
    cursor = from_dt.replace(minute=0, second=0, microsecond=0)
    last_activity = None
    for _ in range(48):
        if cursor > to_dt:
            break
        s = slot_at(cursor)
        if s.activity != last_activity:
            out.append(f'{cursor.hour}点左右{s.doing}')
            last_activity = s.activity
        cursor += timedelta(hours=1)
    return out
