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
    """读取当前系统时间对应的 Unix 毫秒时间戳。

    Returns:
        自 1970-01-01 UTC 起经过的整数毫秒数。

    Side Effects:
        读取系统时钟；不修改应用状态。
    """
    return int(time.time() * 1000)


def from_ms(ms: int) -> datetime:
    """将 Unix 毫秒时间戳转换为带 UTC 时区的 ``datetime``。

    Args:
        ms: Unix 毫秒时间戳。

    Returns:
        与时间戳对应的 UTC 感知 ``datetime``。

    Raises:
        OverflowError: 时间戳超出平台 ``datetime`` 支持范围。
        TypeError: 时间戳不是可进行除法运算的数值。
    """
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def snapshot_date(ms: int | None = None) -> str:
    """将时间戳转换为本地自然日键，用于人格快照去重。

    Args:
        ms: 可选 Unix 毫秒时间戳；省略或传入 ``None`` 时读取当前系统时间。

    Returns:
        ``YYYY-MM-DD`` 格式的本地日期字符串。

    Raises:
        OverflowError: 时间戳超出平台 ``datetime`` 支持范围。
        TypeError: 时间戳不是可进行除法运算的数值。

    Side Effects:
        ``ms`` 省略时读取系统时钟；不修改业务状态。
    """
    dt = datetime.fromtimestamp((ms or now()) / 1000)
    return dt.strftime("%Y-%m-%d")
