"""
前台窗口 → 活动类别。直接移植自 src/core/awareness/classify.ts。

★ 隐私红线：绝不把原始窗口标题送进 LLM。标题在这里被吃掉，不往外传——
  它带的是文档名、网页地址、聊天对象这些真正敏感的东西。
  进程名（app 字段）是另一回事，会送出去：它只是可执行文件名，而「他开的是
  PyCharm」这个先验能显著改善视觉模型对画面的判断——认不出界面时，知道这是
  什么程序比瞎猜强得多。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional

Activity = Literal[
    'work', 'coding', 'gaming', 'video', 'music',
    'browsing', 'chat', 'reading', 'files', 'idle', 'other'
]


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
    # 展示用的程序名，例如 'PyCharm'。未知程序退回进程名本身（去掉 .exe）。
    app: str = ''


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

# 进程名 → 人看得懂的程序名。没收录的程序退回进程名本身，因为"他开的是什么"
# 本身就是这里要传达的信息，写成「某个程序」等于什么都没说。
_APP_NAMES: dict[str, str] = {
    'code': 'VS Code', 'code - insiders': 'VS Code', 'cursor': 'Cursor',
    'devenv': 'Visual Studio', 'idea64': 'IntelliJ IDEA', 'pycharm64': 'PyCharm',
    'webstorm64': 'WebStorm', 'goland64': 'GoLand', 'clion64': 'CLion',
    'rider64': 'Rider', 'studio64': 'Android Studio', 'rustrover64': 'RustRover',
    'sublime_text': 'Sublime Text', 'windsurf': 'Windsurf', 'zed': 'Zed',
    'winword': 'Word', 'excel': 'Excel', 'powerpnt': 'PowerPoint',
    'onenote': 'OneNote', 'wps': 'WPS', 'et': 'WPS 表格', 'wpp': 'WPS 演示',
    'acad': 'AutoCAD', 'notion': 'Notion', 'obsidian': 'Obsidian', 'typora': 'Typora',
    'steam': 'Steam', 'steamwebhelper': 'Steam', 'epicgameslauncher': 'Epic Games',
    'battle.net': '战网', 'genshinimpact': '原神', 'yuanshen': '原神',
    'starrail': '崩坏：星穹铁道', 'zenlesszonezero': '绝区零', 'minecraft': 'Minecraft',
    'potplayermini64': 'PotPlayer', 'vlc': 'VLC', 'mpv': 'mpv', 'mpc-hc64': 'MPC-HC',
    'bilibili': '哔哩哔哩', 'iqiyi': '爱奇艺', 'youku': '优酷', 'tencentvideo': '腾讯视频',
    'cloudmusic': '网易云音乐', 'qqmusic': 'QQ 音乐', 'spotify': 'Spotify',
    'foobar2000': 'foobar2000', 'kugou': '酷狗音乐', 'kuwo': '酷我音乐', 'aimp': 'AIMP',
    'wechat': '微信', 'weixin': '微信', 'qq': 'QQ', 'tim': 'TIM',
    'dingtalk': '钉钉', 'feishu': '飞书', 'lark': '飞书', 'telegram': 'Telegram',
    'discord': 'Discord', 'slack': 'Slack', 'whatsapp': 'WhatsApp',
    'sumatrapdf': 'SumatraPDF', 'acrobat': 'Acrobat', 'acrord32': 'Acrobat Reader',
    'foxitreader': '福昕阅读器', 'calibre': 'calibre', 'neat reader': 'Neat Reader',
    'explorer': '文件资源管理器',
    'chrome': 'Chrome', 'msedge': 'Edge', 'firefox': 'Firefox', 'brave': 'Brave',
    'opera': 'Opera', 'vivaldi': 'Vivaldi', 'arc': 'Arc', '360se': '360 浏览器',
    'qqbrowser': 'QQ 浏览器',
    'powershell': 'PowerShell', 'pwsh': 'PowerShell', 'cmd': '命令提示符',
    'windowsterminal': 'Windows 终端', 'wt': 'Windows 终端',
    'alacritty': 'Alacritty', 'wezterm': 'WezTerm', 'mintty': 'mintty',
}

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
    app = _APP_NAMES.get(proc, proc)
    if proc in _MEETING:
        return Classified(activity='other', label=_LABELS['other'], silent=True, app=app)
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
    return Classified(activity=activity, label=_LABELS[activity],
                      silent=bool(info.fullscreen), app=app)


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
    # 带上程序名：「他在用 PyCharm 写代码」比「他在写代码」具体得多，
    # 视觉描述认不出界面时，这一句就是她唯一靠得住的依据。
    label = f'在用 {c.app} {c.label[1:]}' if c.app and c.label.startswith('在') else c.label
    return f'他{label}，已经{span}。' if span else f'他{label}。'
