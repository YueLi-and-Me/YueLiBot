"""计费：基础单价、缓存命中价、分时段单价的选取与加载期校验。

时刻一律由本机时区的本地时间构造，与 ``prices_at`` 的口径一致，因此在任何时区
运行结果相同。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import pytest

import src.core.llm_models.usage as usage_module
from pytests.conftest import render_loadable_config
from src.core.config.loader import read_config
from src.core.config.schema import ModelCandidate, PricePeriodConfig
from src.core.llm_models.router import ModelRouter
from src.core.llm_models.usage import PriceSet, TokenUsage, compute_cost, prices_at

# 100 万输入（其中 40 万命中缓存）、20 万输出。
USAGE = TokenUsage(prompt_tokens=1_000_000, completion_tokens=200_000, total_tokens=1_200_000, cached_tokens=400_000)


def _local_ms(hour: int, minute: int, second: int = 0) -> int:
    return int(datetime(2026, 10, 26, hour, minute, second).timestamp() * 1000)


def _candidate(**kwargs: Any) -> ModelCandidate:
    return ModelCandidate(name='峰谷模型', provider='厂商甲', identifier='peak', **kwargs)


def _period(start: str, end: str, price_in: float, price_out: float, cache: float | None = None) -> PricePeriodConfig:
    return PricePeriodConfig(start=start, end=end, price_in=price_in, price_out=price_out, cache_price_in=cache)


def test_base_price() -> None:
    # 60 万未命中 × 2 + 40 万命中 × 2 + 20 万输出 × 8，单位元／百万 token。
    assert compute_cost(USAGE, PriceSet(2.0, 8.0, None)) == pytest.approx(0.6 * 2 + 0.4 * 2 + 0.2 * 8)


def test_blank_cache_price_falls_back_to_input_price() -> None:
    assert compute_cost(USAGE, PriceSet(2.0, 8.0, None)) == compute_cost(USAGE, PriceSet(2.0, 8.0, 2.0))


def test_zero_cache_price_makes_cache_hits_free() -> None:
    assert compute_cost(USAGE, PriceSet(2.0, 8.0, 0.0)) == pytest.approx(0.6 * 2 + 0.2 * 8)


def test_cache_price_discount() -> None:
    assert compute_cost(USAGE, PriceSet(2.0, 8.0, 0.5)) == pytest.approx(0.6 * 2 + 0.4 * 0.5 + 0.2 * 8)


def test_zero_price_model_costs_nothing() -> None:
    assert compute_cost(USAGE, PriceSet(0.0, 0.0, None)) == 0.0


def test_period_boundaries_include_start_and_exclude_end() -> None:
    candidate = _candidate(price_in=2.0, price_out=8.0, price_periods=[_period('08:00', '12:00', 4.0, 16.0)])
    assert prices_at(candidate, _local_ms(8, 0)) == PriceSet(4.0, 16.0, None)
    assert prices_at(candidate, _local_ms(11, 59, 59)) == PriceSet(4.0, 16.0, None)
    assert prices_at(candidate, _local_ms(12, 0)) == PriceSet(2.0, 8.0, None)
    assert prices_at(candidate, _local_ms(7, 59, 59)) == PriceSet(2.0, 8.0, None)


def test_period_across_midnight() -> None:
    candidate = _candidate(price_in=2.0, price_out=8.0, cache_price_in=0.5,
                           price_periods=[_period('23:00', '07:00', 1.0, 4.0, 0.25)])
    for hour, minute in ((23, 0), (23, 59), (0, 0), (3, 30), (6, 59)):
        assert prices_at(candidate, _local_ms(hour, minute)) == PriceSet(1.0, 4.0, 0.25)
    assert prices_at(candidate, _local_ms(7, 0)) == PriceSet(2.0, 8.0, 0.5)
    assert prices_at(candidate, _local_ms(22, 59)) == PriceSet(2.0, 8.0, 0.5)


def test_outside_every_period_uses_base_price() -> None:
    candidate = _candidate(price_in=3.0, price_out=9.0, price_periods=[
        _period('00:30', '08:30', 1.0, 3.0), _period('20:00', '22:00', 5.0, 15.0),
    ])
    assert prices_at(candidate, _local_ms(12, 0)) == PriceSet(3.0, 9.0, None)
    assert prices_at(candidate, _local_ms(0, 15)) == PriceSet(3.0, 9.0, None)


class _CrossingClient:
    """一次跨越时段边界的调用：开始在谷段，结束时已进入平段。"""

    async def stream(self, messages, temperature=0.85, max_tokens=None, signal=None, **_options: Any) -> AsyncIterator[dict]:
        yield {'text': '好'}
        yield {'usage': USAGE}


async def test_attempt_start_time_selects_the_price_group(monkeypatch, usage_rows) -> None:
    """单价组按尝试开始时刻选：07:59:59 开始、08:00:30 结束的调用按谷段计费。"""
    moments: Iterator[int] = iter([_local_ms(7, 59, 59)] + [_local_ms(8, 0, 30)] * 50)
    monkeypatch.setattr(usage_module, 'current_time', lambda: next(moments))
    candidate = _candidate(price_in=2.0, price_out=8.0, price_periods=[_period('23:00', '08:00', 1.0, 4.0)])
    router = ModelRouter('chat', [candidate])
    client = _CrossingClient()
    router.client = lambda item: client  # type: ignore[method-assign]

    assert [chunk async for chunk in router.stream([{'role': 'user', 'content': '在吗'}])] == [{'text': '好'}]

    [row] = usage_rows()
    assert row['cost'] == pytest.approx(compute_cost(USAGE, PriceSet(1.0, 4.0, None)))
    # 小时桶同样按开始时刻归入 07 点那一桶。
    assert row['hour_start'] == _local_ms(7, 59, 59) - _local_ms(7, 59, 59) % 3_600_000


def _config_with_periods(tmp_path: Path, periods: str) -> Path:
    """在模板配置最后一个模型（qwen-embedding）后追加分时段单价子表。"""
    directory = render_loadable_config(tmp_path / 'config')
    models = directory / 'models.toml'
    models.write_text(models.read_text(encoding='utf-8') + periods, encoding='utf-8')
    return directory


@pytest.mark.parametrize('periods,message', [
    (
        '[[models.price_periods]]\nstart = "22:00"\nend = "02:00"\nprice_in = 1.0\nprice_out = 1.0\n'
        '[[models.price_periods]]\nstart = "01:00"\nend = "03:00"\nprice_in = 1.0\nprice_out = 1.0\n',
        '模型 qwen-embedding 的 price_periods 第 1 段（22:00～02:00）与第 2 段（01:00～03:00）时间重叠',
    ),
    (
        '[[models.price_periods]]\nstart = "08:00"\nend = "08:00"\nprice_in = 1.0\nprice_out = 1.0\n',
        '模型 qwen-embedding 的 price_periods 第 1 段的开始与结束相同（08:00）',
    ),
    (
        '[[models.price_periods]]\nstart = "8:00"\nend = "09:00"\nprice_in = 1.0\nprice_out = 1.0\n',
        "模型 qwen-embedding 的 price_periods 第 1 段的 start '8:00' 不是合法的 HH:MM 时刻",
    ),
    (
        '[[models.price_periods]]\nstart = "08:00"\nend = "24:00"\nprice_in = 1.0\nprice_out = 1.0\n',
        "模型 qwen-embedding 的 price_periods 第 1 段的 end '24:00' 不是合法的 HH:MM 时刻",
    ),
])
def test_invalid_periods_fail_at_load_with_model_name(tmp_path: Path, periods: str, message: str) -> None:
    with pytest.raises(ValueError) as exc:
        read_config(_config_with_periods(tmp_path, periods))
    assert message in str(exc.value)


def test_valid_periods_reach_the_candidate(tmp_path: Path) -> None:
    """模型的缓存命中价与分时段单价随候选交给路由层。"""
    directory = render_loadable_config(tmp_path / 'config')
    models = directory / 'models.toml'
    text = models.read_text(encoding='utf-8').replace(
        'name = "deepseek-pro"', 'cache_price_in = 0.5\nname = "deepseek-pro"', 1,
    )
    models.write_text(text, encoding='utf-8')
    config = read_config(directory)
    [candidate] = [item for item in config.routing.chat.candidates if item.name == 'deepseek-pro']
    assert candidate.cache_price_in == 0.5
    assert candidate.price_periods == []
