"""
前台窗口 → 活动类别。直接移植自 src/core/awareness/classify.ts。

★ 隐私红线：绝不把原始窗口标题送进 LLM。
输出只有类别和时长，标题在这里被吃掉，不往外传。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional

Activity = Literal[
    'work', 'coding', 'gaming', 'video', 'music',
    'browsing', 'chat', 'reading', 'files', 'idle', 'other'
]
VisionContext = Literal['steam-library', 'gameplay', 'game-folder']


@dataclass
class ForegroundInfo:
    process: str
    title: str | None = None
    fullscreen: bool = False


@dataclass
class Classified:
    activity: Activity
    label: str
    silent: bool


_PROCESS_RULES: list[tuple[Activity, frozenset[str]]] = [
    ('coding', frozenset([
        'code', 'code - insiders', 'cursor', 'windsurf', 'devenv', 'idea64', 'pycharm64',
        'webstorm64', 'goland64', 'clion64', 'rider64', 'studio64', 'sublime_text',
        'rustrover64', 'zed', 'neovide',
    ])),
    ('work', frozenset([
        'winword', 'excel', 'powerpnt', 'onenote', 'wps', 'et', 'wpp',
        'acad', 'notion', 'obsidian', 'typora',
    ])),
    ('gaming', frozenset([
        'steam', 'steamwebhelper', 'epicgameslauncher', 'battle.net', 'league of legends',
        'genshinimpact', 'yuanshen', 'starrail', 'zenlesszonezero', 'dota2', 'csgo',
        'cs2', 'valorant', 'minecraft', 'javaw',
    ])),
    ('video', frozenset(['potplayermini64', 'vlc', 'mpv', 'bilibili', 'iqiyi', 'youku',
                          'tencentvideo', 'mpc-hc64'])),
    ('music', frozenset(['cloudmusic', 'qqmusic', 'spotify', 'foobar2000', 'kugou', 'kuwo', 'aimp'])),
    ('chat', frozenset(['wechat', 'weixin', 'qq', 'tim', 'dingtalk', 'feishu', 'lark',
                         'telegram', 'discord', 'slack', 'whatsapp'])),
    ('reading', frozenset(['sumatrapdf', 'acrobat', 'acrord32', 'foxitreader', 'calibre', 'neat reader'])),
    ('files', frozenset(['explorer'])),
    ('browsing', frozenset(['chrome', 'msedge', 'firefox', 'brave', 'opera', 'vivaldi',
                             'arc', '360se', 'qqbrowser'])),
    ('work', frozenset(['powershell', 'pwsh', 'cmd', 'windowsterminal', 'wt',
                         'alacritty', 'wezterm', 'mintty'])),
]

_MEETING = frozenset([
    'zoom', 'teams', 'msteams', 'tencentmeeting', 'wemeetapp',
    'voovmeeting', 'webexmta', 'dingtalk',
])

_LABELS: dict[Activity, str] = {
    'coding': '在写代码', 'work': '在处理文档或工作', 'gaming': '在打游戏',
    'video': '在看视频', 'music': '在听歌', 'browsing': '在上网',
    'chat': '在跟人聊天', 'reading': '在看书或文档', 'files': '在翻文件夹',
    'idle': '没在操作电脑', 'other': '在用电脑做别的事',
}

_STEAM = frozenset(['steam', 'steamwebhelper'])
_FILE_MGR = frozenset(['explorer'])

_BROWSER_HINTS: list[tuple[Activity, re.Pattern]] = [
    ('video', re.compile(r'bilibili|哔哩哔哩|youtube|爱奇艺|优酷|腾讯视频|netflix|抖音|douyin', re.IGNORECASE)),
    ('music', re.compile(r'网易云|spotify|qq音乐|音乐', re.IGNORECASE)),
    ('coding', re.compile(r'github|stack overflow|mdn|npm|文档|docs?\b', re.IGNORECASE)),
]

_BROWSERS = frozenset(['chrome', 'msedge', 'firefox', 'brave', 'opera', 'vivaldi', 'arc', '360se', 'qqbrowser'])


def _normalize(proc: str) -> str:
    return re.sub(r'\.exe$', '', proc.lower()).strip()


def classify(info: ForegroundInfo | None) -> Classified:
    if not info or not info.process:
        return Classified(activity='idle', label=_LABELS['idle'], silent=False)
    proc = _normalize(info.process)
    if proc in _MEETING:
        return Classified(activity='other', label=_LABELS['other'], silent=True)
    activity: Activity = 'other'
    for act, names in _PROCESS_RULES:
        if proc in names:
            activity = act
            break
    # 浏览器按标题细分（标题在此被消化，不出现在返回值里）
    if activity == 'browsing' and info.title:
        for act, pat in _BROWSER_HINTS:
            if pat.search(info.title):
                activity = act
                break
    return Classified(activity=activity, label=_LABELS[activity], silent=bool(info.fullscreen))


def vision_context_for(info: ForegroundInfo | None, classified: Classified) -> VisionContext | None:
    if not info or classified.silent:
        return None
    proc = _normalize(info.process)
    if proc in _STEAM:
        return 'steam-library'
    if proc in _FILE_MGR:
        return 'game-folder'
    if classified.activity == 'gaming':
        return 'gameplay'
    return None


LONG_SESSION_MINUTES = 110


def describe_activity(c: Classified, minutes: int) -> str:
    if c.activity == 'idle':
        return '他现在没在操作电脑。'
    if minutes < 20:
        span = ''
    elif minutes < 60:
        span = '有一会儿了'
    elif minutes < 120:
        span = '快一个小时了'
    elif minutes < 240:
        span = '两个多小时了'
    else:
        span = '很久了'
    return f'他{c.label}，已经{span}。' if span else f'他{c.label}。'
