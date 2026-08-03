"""前台进程监控器。直接移植自 src/core/awareness/monitor.ts。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .classify import ForegroundInfo


@dataclass
class ForegroundObservation:
    foreground: ForegroundInfo | None
    process_changed: bool
    window_changed: bool


def _normalize_process(process: str | None) -> str:
    import re
    return re.sub(r'\.exe$', '', (process or '').lower()).strip()


def _fingerprint(value: str) -> int:
    """FNV-1a 小指纹。只判断标题有没有变，不保存原文。"""
    h = 0x811c9dc5
    for ch in value:
        h ^= ord(ch)
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


class ForegroundProcessMonitor:
    def __init__(self) -> None:
        self._initialized = False
        self._last_process = ''
        self._last_window_fingerprint = 0

    def observe(self, foreground: ForegroundInfo | None) -> ForegroundObservation:
        process = _normalize_process(foreground.process if foreground else None)
        fp = _fingerprint(f'{process}\x00{foreground.title if foreground and foreground.title else ""}')
        process_changed = not self._initialized or process != self._last_process
        window_changed = not self._initialized or process_changed or fp != self._last_window_fingerprint
        self._initialized = True
        self._last_process = process
        self._last_window_fingerprint = fp
        return ForegroundObservation(foreground=foreground, process_changed=process_changed, window_changed=window_changed)
