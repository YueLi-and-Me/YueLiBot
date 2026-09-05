"""键鼠信号在 Python 侧的分类验收。"""

from __future__ import annotations

from src.desktop.classify import AWAY_SECONDS, BUSY_KEYS_PER_MIN, classify_input


def test_input_intensity_boundaries() -> None:
    assert classify_input(0, 0, 0, idle_seconds=600, span_ms=8_000) == 'away'
    assert classify_input(0, 0, 0, idle_seconds=AWAY_SECONDS - 1, span_ms=8_000) != 'away'
    assert classify_input(BUSY_KEYS_PER_MIN - 1, 0, 0, idle_seconds=0, span_ms=60_000) == 'light'
    assert classify_input(BUSY_KEYS_PER_MIN, 0, 0, idle_seconds=0, span_ms=60_000) == 'busy'
