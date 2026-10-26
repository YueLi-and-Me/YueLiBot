"""用量报告文件与控制台汇总：五个时间范围、不引外部资源、原子替换、没有新调用不打印。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import os
import re

import pytest

import src.core.services.maintenance.usage_report as report_module
from src.core.llm_models.usage import TokenUsage, UsageRecord, usage_ledger
from src.core.llm_models.usage_stats import RANGE_KEYS, RANGE_LABELS
from src.core.logging.console_layout import render_box
from src.core.runtime.clock import now as current_time
from src.core.services.maintenance.usage_report import (
    REPORT_RELATIVE_PATH,
    UsageReportService,
    format_cost,
    render_console_rows,
)


def _record(**overrides: Any) -> UsageRecord:
    fields: Dict[str, Any] = dict(
        started_at=current_time(), task='planner', model='主力', provider='厂商甲', stream_id=0,
        plugin_id='', plugin_third_party=False, outcome='ok', metered=True,
        usage=TokenUsage(1200, 80, 1280, 600, 20), cost=0.0123, duration_ms=2100,
    )
    fields.update(overrides)
    return UsageRecord(**fields)


@pytest.fixture
def service(tmp_path: Path, usage_db: Path, monkeypatch: pytest.MonkeyPatch) -> Tuple[UsageReportService, List[Any]]:
    printed: List[Any] = []
    monkeypatch.setattr(report_module, 'print_usage_box', lambda hour, today: printed.append((hour, today)))
    routers = SimpleNamespace(all_candidates=lambda: [])
    return UsageReportService(tmp_path / 'data', routers), printed


async def test_report_has_five_ranges_and_no_external_resources(service) -> None:
    report, _printed = service
    usage_ledger.record(_record(stream_id=0))
    usage_ledger.record(_record(task='vision', model='视觉', plugin_id='forward-message', usage=None, cost=0.0))
    await report.run_once()

    assert report.path == report.path.parents[1] / REPORT_RELATIVE_PATH
    html = report.path.read_text(encoding='utf-8')
    for key in RANGE_KEYS:
        assert f'id="panel-{key}"' in html
        assert f'<label for="range-{key}">{RANGE_LABELS[key]}</label>' in html
    assert '后台任务' in html and 'forward-message' in html
    assert '成功调用中有 1 次服务商没返回用量' in html
    # 单文件自带样式与 SVG：不引任何外部 URL、外链样式或脚本。
    assert not re.search(r'https?://|//[a-z0-9.-]+\.[a-z]', html, re.IGNORECASE)
    assert '<link' not in html and '<script' not in html and ' src=' not in html
    assert '<svg' in html and 'prefers-color-scheme: dark' in html


async def test_report_is_written_to_a_temporary_file_then_replaced(service, monkeypatch) -> None:
    report, _printed = service
    usage_ledger.record(_record())
    replaced: List[Tuple[Path, Path, str]] = []
    original_replace = os.replace

    def spy(source: Any, target: Any) -> None:
        source_path, target_path = Path(source), Path(target)
        replaced.append((source_path, target_path, source_path.read_text(encoding='utf-8')))
        original_replace(source, target)

    monkeypatch.setattr(report_module.os, 'replace', spy)
    await report.run_once()

    [(source, target, content)] = replaced
    assert target == report.path
    assert source != target and source.parent == target.parent
    assert content.endswith('</html>\n')
    assert not source.exists()
    assert target.read_text(encoding='utf-8') == content


async def test_console_box_prints_only_when_there_are_new_calls(service) -> None:
    report, printed = service
    await report.run_once()
    await report.run_once()
    assert printed == [], '没有新调用时不打印空框'

    usage_ledger.record(_record())
    await report.run_once()
    assert len(printed) == 1
    hour, today = printed[0]
    assert hour['overview']['calls'] == 1 and today['overview']['calls'] == 1

    await report.run_once()
    assert len(printed) == 1, '距上次打印没有新调用就不再打印'
    assert report.path.exists(), '报告文件每个周期都照常重写'


async def test_service_starts_and_stops_cleanly(service) -> None:
    report, _printed = service
    await report.startup()
    assert report.path.exists()
    await report.shutdown()


def test_console_rows_render_as_one_box() -> None:
    summary = {
        'overview': {
            'calls': 12, 'failedCalls': 1, 'interruptedCalls': 0, 'unreportedCalls': 2,
            'promptTokens': 23400, 'completionTokens': 530, 'cachedTokens': 14000, 'reasoningTokens': 120,
            'cost': 0.0406, 'replies': 6,
        },
        'perReply': {'cost': 0.0068, 'tokens': 3988.0, 'calls': 2.0},
        'groups': {
            'source': [{'label': '主程序', 'cost': 0.0364}, {'label': 'forward-message', 'cost': 0.0042}],
            'model': [{'model': 'deepseek-pro', 'cost': 0.0364}],
        },
    }
    box = render_box('模型用量', render_console_rows(summary, summary), width=report_module.CONSOLE_BOX_WIDTH)
    assert box.startswith('╭─ 模型用量')
    assert '23,400（缓存 14,000）' in box
    assert '今天按来源：主程序 ¥0.0364 · forward-message ¥0.0042' in box
    assert format_cost(0) == '¥0' and format_cost(0.00004) == '<¥0.0001' and format_cost(1234.5) == '¥1,234.50'
