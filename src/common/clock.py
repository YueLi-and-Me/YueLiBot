"""
统一的毫秒时钟。

与 src/core/time/clock.ts 保持相同语义：
  now()        → 当前 Unix 毫秒时间戳（int）
  from_ms(ms)  → datetime（UTC 感知）

整个后端只通过这里取时间，方便测试时 mock。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone


def now() -> int:
    """当前 Unix 毫秒时间戳。"""
    return int(time.time() * 1000)


def from_ms(ms: int) -> datetime:
    """毫秒时间戳 → UTC datetime。"""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def snapshot_date(ms: int | None = None) -> str:
    """本地自然日字符串，格式 YYYY-MM-DD。用于人格快照去重键。"""
    dt = datetime.fromtimestamp((ms or now()) / 1000)
    return dt.strftime("%Y-%m-%d")
