"""把前台进程和用户输入状态转换为低敏感度活动描述。

本模块是桌宠外壳侧的信号提供方：输入来自 Electron 通过 ``/platform/foreground``
推送的前台快照，输出是 ``src.core.awareness.signals`` 定义的 ``Classified``，
供内核的兴趣累积、每日预算和主动搭话服务消费。活动标签的取值集合由内核定义，
本模块只负责判定，不扩充词表。

隐私约束：原始窗口标题只参与浏览器活动细分，不进入返回值，也不会送给模型；
进程名经过规范化和有限映射后保留为程序名，帮助视觉描述识别应用类型。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.core.awareness.signals import Activity, Classified, InputIntensity

__all__ = [
    'AWAY_SECONDS', 'BUSY_KEYS_PER_MIN', 'LONG_SESSION_MINUTES',
    'Activity', 'Classified', 'ForegroundInfo', 'InputIntensity',
    'classify', 'classify_input', 'describe_activity',
]

# 这两个值各自只回答一个问题：多久无输入算离开、每分钟敲多少键算手头正忙。
# 不从键鼠去猜活动类型，进程名已经能更准确地回答那件事。
AWAY_SECONDS = 5 * 60
BUSY_KEYS_PER_MIN = 120


@dataclass
class ForegroundInfo:
    """描述 Electron 上报的当前前台窗口信息。

    :ivar process: 进程名，通常含可选 `.exe` 后缀。
    :ivar title: 原始窗口标题；只在本模块内部用于浏览器细分，默认值为 `None`。
    :ivar fullscreen: 是否全屏，默认值为 `False`，全屏时活动会被标记为静默。
    """

    process: str
    title: str | None = None
    fullscreen: bool = False


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

# 进程名映射为可读的程序名；未收录的程序保留原进程名，避免丢失活动识别信息。
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
    """规范化进程名以便与规则表匹配。

    :param proc: 原始进程名。
    :return: 转为小写、移除末尾 `.exe` 并去除首尾空白后的名称。
    副作用：不修改输入字符串。
    """
    return re.sub(r'\.exe$', '', proc.lower()).strip()


def classify(info: ForegroundInfo | None) -> Classified:
    """根据前台进程和标题推断脱敏活动类别。

    :param info: 前台窗口信息；`None` 或空进程表示用户当前空闲。
    :return: 活动类别、中文标签、静默标志、程序名和默认输入强度。
    副作用：读取 `info.title` 仅用于本地匹配，不在返回结构中保留原始标题。
    :performance: 按固定规则表线性扫描，规则规模与输入无关。
    """
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


def classify_input(keys: int, clicks: int, distance: float, idle_seconds: int,
                   span_ms: int) -> InputIntensity:
    """把 Electron 汇总的键鼠数量与系统空闲时间归成三档。

    clicks / distance 只随快照一并传递，方便保持采集口径完整；忙碌阈值刻意只看
    键盘速率，避免再引入一套鼠标距离、点击次数等活动类型猜测规则。

    :param keys: 采样窗口内的键盘按键数。
    :param clicks: 采样窗口内的鼠标点击数；当前分类规则不直接使用该值。
    :param distance: 采样窗口内的鼠标移动距离；当前分类规则不直接使用该值。
    :param idle_seconds: 系统连续空闲秒数。
    :param span_ms: 采样窗口长度，单位为毫秒，必须大于 ``0``。

    :return: ``away``、``busy`` 或 ``light`` 三档输入强度。

    :raises ValueError: ``span_ms`` 小于等于 ``0``。

    副作用：
        不执行进程查询或持久化；输入值仅用于本次分类。
    """
    if span_ms <= 0:
        raise ValueError('键鼠采样窗口必须大于 0')
    if idle_seconds >= AWAY_SECONDS:
        return 'away'
    keys_per_minute = keys * 60_000 / span_ms
    if keys_per_minute >= BUSY_KEYS_PER_MIN:
        return 'busy'
    return 'light'


LONG_SESSION_MINUTES = 110


def describe_activity(c: Classified, minutes: int) -> str:
    """把活动分类和持续时间渲染为自然语言情境描述。

    :param c: 已脱敏的活动分类结果。
    :param minutes: 当前活动持续分钟数，负值会按短时活动处理。
    :return: 中文活动描述；忙碌输入时追加“手头正忙”。
    副作用：不执行进程查询或模型调用。
    """
    if c.intensity == 'away':
        return '当前用户不在电脑前。'
    if c.activity == 'idle':
        return '当前用户现在没在操作电脑。'
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
    # 活动描述同时保留程序名和行为，视觉描述缺失时仍能提供可追踪的前台上下文。
    label = f'在用 {c.app} {c.label[1:]}' if c.app and c.label.startswith('在') else c.label
    text = f'当前用户{label}，已经{span}。' if span else f'当前用户{label}。'
    return f'{text}手头正忙。' if c.intensity == 'busy' else text
