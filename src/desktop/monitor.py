"""监控前台进程和窗口标题变化，同时避免保存原始标题。

`ForegroundProcessMonitor` 只输出首次观察、进程变化和标题指纹变化标志；标题
通过 FNV-1a 指纹参与比较，原文不会写入状态或传递给后续模型层。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .classify import ForegroundInfo


@dataclass
class ForegroundObservation:
    """表示一次前台信息观察与上次观察的差异。

    :ivar foreground: 本次前台信息；可以为 `None`。
    :ivar process_changed: 进程名是否变化，首次观察也为 `True`。
    :ivar window_changed: 窗口指纹是否变化，首次观察也为 `True`。
    """

    foreground: ForegroundInfo | None
    process_changed: bool
    window_changed: bool


def _normalize_process(process: str | None) -> str:
    """规范化可选进程名。

    :param process: 原始进程名，可以为 `None`。
    :return: 小写、移除 `.exe` 后缀并去除空白的进程名。
    副作用：不执行系统进程查询。
    """
    import re
    return re.sub(r'\.exe$', '', (process or '').lower()).strip()


def _fingerprint(value: str) -> int:
    """计算用于变化检测的 32 位 FNV-1a 指纹。

    :param value: 进程名与窗口标题组成的内部字符串。
    :return: 32 位无符号整数指纹；只用于比较，不可还原原文。
    副作用：不保存输入字符串。
    :performance: 时间复杂度与字符串长度线性相关。
    """
    h = 0x811c9dc5
    for ch in value:
        h ^= ord(ch)
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


class ForegroundProcessMonitor:
    """保存前台变化检测所需的最小状态。

    实例只持有规范化进程名和窗口标题指纹，不保留窗口标题原文。
    """

    def __init__(self) -> None:
        """创建尚未观察过前台窗口的监控器。

        :return: 无返回值。
        副作用：初始化首次观察标志和上一次比较值。
        """
        self._initialized = False
        self._last_process = ''
        self._last_window_fingerprint = 0

    def observe(self, foreground: ForegroundInfo | None) -> ForegroundObservation:
        """比较当前前台信息并更新监控器状态。

        :param foreground: 当前前台窗口信息，可以为 `None`。
        :return: 包含原始本次对象引用和两个变化标志的观察结果。
        副作用：保存规范化进程名和标题指纹，不保存标题原文。
        """
        process = _normalize_process(foreground.process if foreground else None)
        fp = _fingerprint(f'{process}\x00{foreground.title if foreground and foreground.title else ""}')
        process_changed = not self._initialized or process != self._last_process
        window_changed = not self._initialized or process_changed or fp != self._last_window_fingerprint
        self._initialized = True
        self._last_process = process
        self._last_window_fingerprint = fp
        return ForegroundObservation(foreground=foreground, process_changed=process_changed, window_changed=window_changed)
