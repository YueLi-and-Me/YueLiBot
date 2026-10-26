"""用量统计口径：时间范围、按天归并、每条回复平均、四种分组与总览相符、接口鉴权。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.api.http import router as http_router
from src.core.api.state import app_state
from src.core.config.schema import ModelCandidate
from src.core.llm_models.usage import HOUR_MS, TokenUsage, UsageRecord, usage_ledger
from src.core.llm_models.usage_stats import (
    DAY_MS,
    ModelPrice,
    model_prices,
    range_bounds,
    summarize_usage,
    task_labels,
)

# 2026-10-26 15:00 UTC = 东八区 23:00；之后一小时就跨到 10-27。
EVENING_UTC = 1_793_026_800_000
EAST_8 = 480


def _record(at: int, **overrides: Any) -> UsageRecord:
    fields: Dict[str, Any] = dict(
        started_at=at, task='planner', model='主力', provider='厂商甲', stream_id=0,
        plugin_id='', plugin_third_party=False, outcome='ok', metered=True,
        usage=TokenUsage(100, 20, 120, 30, 5), cost=0.01, duration_ms=1000,
    )
    fields.update(overrides)
    return UsageRecord(**fields)


def _summary(start: int, end: int, now: int, prices: Dict[str, ModelPrice] | None = None) -> Dict[str, Any]:
    return usage_ledger.read(lambda connection: summarize_usage(
        connection, start_ms=start, end_ms=end, tz_offset_minutes=EAST_8, now_ms=now, prices=prices or {},
    ))


def _add_messages(path: Path, rows: List[tuple]) -> None:
    connection = sqlite3.connect(path)
    connection.executemany('INSERT INTO messages (role, content, created_at, stream_id) VALUES (?, ?, ?, ?)', rows)
    connection.commit()
    connection.close()


def test_east_8_day_boundary(usage_db: Path) -> None:
    """东八区 23:00 与次日 00:30 的调用归在不同的天，「今天」只含零点之后。"""
    usage_ledger.record(_record(EVENING_UTC, cost=1.0))
    usage_ledger.record(_record(EVENING_UTC + HOUR_MS + 30 * 60_000, cost=2.0))
    now = EVENING_UTC + 2 * HOUR_MS

    start, end = range_bounds('today', now, EAST_8)
    assert start == EVENING_UTC + HOUR_MS  # 东八区 10-27 零点
    today = _summary(start, end, now)
    assert (today['overview']['calls'], today['overview']['cost']) == (1, pytest.approx(2.0))

    daily = _summary(*range_bounds('all', now, EAST_8), now)['trend']['daily']
    assert len(daily) == 30
    assert [point['cost'] for point in daily[-2:]] == [pytest.approx(1.0), pytest.approx(2.0)]
    assert daily[-1]['start'] == start and daily[-2]['start'] == start - DAY_MS
    hourly = _summary(*range_bounds('all', now, EAST_8), now)['trend']['hourly']
    assert len(hourly) == 48 and hourly[-1]['start'] == now - now % HOUR_MS


def test_per_reply_average_and_zero_replies(usage_db: Path) -> None:
    usage_ledger.record(_record(EVENING_UTC, cost=0.3))
    usage_ledger.record(_record(EVENING_UTC + 1000, cost=0.3))
    start, end = EVENING_UTC, EVENING_UTC + HOUR_MS

    empty = _summary(start, end, end)
    assert empty['overview']['replies'] == 0
    assert empty['perReply'] == {'cost': None, 'tokens': None, 'calls': None}

    _add_messages(usage_db, [
        ('assistant', '甲', EVENING_UTC + 10, 5), ('assistant', '乙', EVENING_UTC + 20, 5),
        ('assistant', '丙', EVENING_UTC + 30, 6), ('user', '用户说的不算', EVENING_UTC + 40, 5),
        ('assistant', '范围外', EVENING_UTC + HOUR_MS, 5),
    ])
    summary = _summary(start, end, end)
    assert summary['overview']['replies'] == 3
    assert summary['perReply']['cost'] == pytest.approx(0.6 / 3)
    assert summary['perReply']['tokens'] == pytest.approx(240 / 3)
    assert summary['perReply']['calls'] == pytest.approx(2 / 3)


def test_every_grouping_adds_up_to_the_overview(usage_db: Path) -> None:
    connection = sqlite3.connect(usage_db)
    connection.execute(
        "INSERT INTO streams (id, platform, kind, external_id, display_name) VALUES (5, 'qq', 'group', '123', '月璃粉丝群')"
    )
    connection.commit()
    connection.close()
    usage_ledger.record(_record(EVENING_UTC, stream_id=5, cost=0.11, duration_ms=800))
    usage_ledger.record(_record(EVENING_UTC + 1, task='vision', model='视觉', provider='厂商乙', stream_id=5,
                                plugin_id='forward-message', cost=0.07, duration_ms=4000))
    usage_ledger.record(_record(EVENING_UTC + 2, task='memory', plugin_id='weather', plugin_third_party=True,
                                outcome='failed', usage=None, cost=0.0, duration_ms=30000))
    usage_ledger.record(_record(EVENING_UTC + 3, task='summary', outcome='interrupted', cost=0.02))
    usage_ledger.record(_record(EVENING_UTC + 4, model='免费模型', usage=None, cost=0.0))
    prices = {'主力': ModelPrice('厂商甲', 2.0, 8.0, 0.5, 1), '免费模型': ModelPrice('厂商甲', 0.0, 0.0, None, 0)}

    summary = _summary(EVENING_UTC, EVENING_UTC + HOUR_MS, EVENING_UTC + HOUR_MS, prices)
    overview = summary['overview']
    assert (overview['calls'], overview['failedCalls'], overview['interruptedCalls'], overview['unreportedCalls']) == (5, 1, 1, 1)
    for kind in ('source', 'task', 'model', 'stream'):
        groups = summary['groups'][kind]
        for field in ('calls', 'failedCalls', 'interruptedCalls', 'unreportedCalls', 'promptTokens',
                      'completionTokens', 'totalTokens', 'cachedTokens', 'reasoningTokens'):
            assert sum(group[field] for group in groups) == overview[field], (kind, field)
        assert sum(group['cost'] for group in groups) == pytest.approx(overview['cost'])

    sources = {group['key']: group for group in summary['groups']['source']}
    assert {key: group['kind'] for key, group in sources.items()} == {
        'main': 'main', 'forward-message': 'builtin', 'weather': 'third_party',
    }
    streams = {group['streamId']: group['label'] for group in summary['groups']['stream']}
    assert streams == {0: '后台任务', 5: '群聊 · 月璃粉丝群'}
    tasks = {group['key']: group['label'] for group in summary['groups']['task']}
    assert tasks['planner'] == task_labels()['planner'] == '行动决策'
    models = {group['model']: group for group in summary['groups']['model']}
    assert models['主力']['price'] == {'priceIn': 2.0, 'priceOut': 8.0, 'cachePriceIn': 0.5, 'periods': 1, 'unpriced': False}
    assert models['免费模型']['price']['unpriced'] is True
    assert models['视觉']['price'] is None
    assert summary['unreported'] == [{'model': '免费模型', 'provider': '厂商甲', 'calls': 1}]
    planner = next(group for group in summary['groups']['task'] if group['key'] == 'planner')
    assert planner['avgDurationMs'] == pytest.approx(900.0)
    assert planner['durationStdMs'] == pytest.approx(100.0)


def test_model_prices_take_the_first_candidate() -> None:
    candidates = [
        ModelCandidate(name='主力', provider='甲', price_in=2.0, price_out=8.0, cache_price_in=0.5),
        ModelCandidate(name='主力', provider='甲', price_in=9.0, price_out=9.0),
    ]
    assert model_prices(candidates) == {'主力': ModelPrice('甲', 2.0, 8.0, 0.5, 0)}


@pytest.fixture
def api_client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(app_state, 'routers', SimpleNamespace(all_candidates=lambda: []))
    app = FastAPI()
    app.include_router(http_router)
    with TestClient(app) as client:
        yield client


def test_usage_api_requires_login(api_client: TestClient, usage_db: Path) -> None:
    assert api_client.get('/api/usage', params={'start': 0, 'end': 1}).status_code == 401


def test_usage_api_returns_the_summary(api_client: TestClient, usage_db: Path) -> None:
    usage_ledger.record(_record(EVENING_UTC))
    response = api_client.get(
        '/api/usage',
        params={'start': EVENING_UTC, 'end': EVENING_UTC + HOUR_MS, 'tzOffset': EAST_8},
        headers={'Authorization': 'Bearer test-token-fixture'},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload['overview']['calls'] == 1
    assert payload['range'] == {'start': EVENING_UTC, 'end': EVENING_UTC + HOUR_MS, 'tzOffsetMinutes': EAST_8}

    bad = api_client.get(
        '/api/usage', params={'start': 10, 'end': 5}, headers={'Authorization': 'Bearer test-token-fixture'},
    )
    assert bad.status_code == 400
