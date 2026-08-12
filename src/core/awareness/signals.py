"""桌面前台信号的类型契约。

本模块只定义内核与信号提供方之间的数据形状，不含任何采集或分类逻辑。
``src.desktop.classify`` 负责把 Electron 上报的前台进程和键鼠快照产出为
``Classified``；``src.core.awareness`` 的兴趣累积与每日预算、以及主动搭话服务
负责消费它。

把词表放在内核一侧，是为了让依赖保持单向：内核给出可选的活动标签集合，桌面
外壳按这套标签汇报，将来换成别的信号源（例如协议侧的在线状态）也只需实现同一
组标签，不需要内核反向依赖任何采集实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Activity = Literal[
    'work', 'coding', 'gaming', 'video', 'music',
    'browsing', 'chat', 'reading', 'files', 'idle', 'other'
]
InputIntensity = Literal['away', 'light', 'busy']


@dataclass
class Classified:
    """表示脱敏后的活动类别和输入强度。

    :ivar activity: 规范化后的活动枚举值。
    :ivar label: 面向提示词的中文活动描述。
    :ivar silent: 当前环境是否应阻止主动搭话。
    :ivar app: 面向用户的程序名，默认值为空字符串。
    :ivar intensity: 键鼠输入强度，默认值为 `light`。
    """

    activity: Activity
    label: str
    silent: bool
    # 展示用的程序名，例如 'PyCharm'。未知程序退回进程名本身（去掉 .exe）。
    app: str = ''
    # 人在不在、手头忙不忙。与 activity 分工，不复刻进程名分类。
    intensity: InputIntensity = 'light'
