"""路由层的用量归集：剥离用量块、按候选尝试落账、小时账本累加、调用记录与控制台面板。

假 provider 直接产出标准化后的 ``{'usage': TokenUsage}`` 增量，覆盖调用方可见性、
首字判定、结构化校验、打断与提前关流；账本用例直接写入落账记录，覆盖分桶与耗时
累加；写库失败时回复照常完成。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Dict, List

import asyncio
import json
import sqlite3

import pytest
from structlog.testing import capture_logs

import src.core.llm_models.router as router_module
import src.core.llm_models.usage as usage_module
from src.core.config.schema import ModelCandidate
from src.core.llm_models.openai import LlmError
from src.core.llm_models.router import ModelRouter
from src.core.llm_models.usage import HOUR_MS, TokenUsage, UsageRecord, usage_ledger
from src.core.observe.events import enter_stage
from src.core.observe.stages import RECEIVED
from src.core.services.console.turn_panel import ModelCall, render_stage_panel

_USAGE = TokenUsage(prompt_tokens=200, completion_tokens=40, total_tokens=240, cached_tokens=50, reasoning_tokens=10)


def _candidate(name: str = '主力', provider: str = '厂商甲', **kwargs: Any) -> ModelCandidate:
    return ModelCandidate(name=name, provider=provider, identifier=f'{name}-model', **kwargs)


class _ScriptClient:
    """按脚本依次产出增量，可在末尾抛错，或在某处停住等待被关闭。"""

    def __init__(self, chunks: List[Dict[str, Any]], error: LlmError | None = None, hang: bool = False) -> None:
        self._chunks = chunks
        self._error = error
        self._hang = hang
        self.closed = False

    async def stream(self, messages, temperature=0.85, max_tokens=None, signal=None, **_options: Any) -> AsyncIterator[dict]:
        try:
            for chunk in self._chunks:
                yield chunk
            if self._hang:
                await asyncio.Event().wait()
            if self._error is not None:
                raise self._error
        finally:
            self.closed = True


def _router(*pairs: tuple[ModelCandidate, _ScriptClient], task: str = 'chat') -> ModelRouter:
    router = ModelRouter(task, [candidate for candidate, _ in pairs])
    clients = {candidate.name: client for candidate, client in pairs}
    router.client = lambda candidate: clients[candidate.name]  # type: ignore[method-assign]
    return router


async def _collect(router: ModelRouter, **kwargs: Any) -> List[dict]:
    return [chunk async for chunk in router.stream([{'role': 'user', 'content': '在吗'}], **kwargs)]


# ---------------------------------------------------------------- B-4 路由剥离


@pytest.mark.parametrize('options', [
    {},
    {'require_text': True},
    {'response_format': {'type': 'json_object'}},
    {'tool_call_validator': lambda calls: None},
])
async def test_caller_never_sees_the_usage_chunk(usage_rows, options: Dict[str, Any]) -> None:
    """普通、要求正文、结构化校验、工具调用校验四条路径都收不到用量块。"""
    client = _ScriptClient([{'text': '{"ok": true}'}, {'usage': _USAGE}])
    chunks = await _collect(_router((_candidate(), client)), **options)

    assert chunks == [{'text': '{"ok": true}'}]
    [row] = usage_rows()
    assert (row['prompt_tokens'], row['completion_tokens'], row['cached_tokens']) == (200, 40, 50)


async def test_usage_only_stream_is_not_a_first_token(monkeypatch, usage_rows) -> None:
    """只有用量、没有正文的流不算出了首字：不要求正文时按空流处理。"""
    rendered: List[ModelCall] = []
    monkeypatch.setattr(router_module, 'render_model_call', rendered.append)

    assert await _collect(_router((_candidate(), _ScriptClient([{'usage': _USAGE}])))) == []

    assert rendered[0].first_token_ms is None
    assert rendered[0].usage == _USAGE
    [row] = usage_rows()
    assert (row['calls'], row['failed_calls'], row['prompt_tokens']) == (1, 0, 200)


async def test_usage_only_stream_fails_require_text_and_switches(monkeypatch, usage_rows) -> None:
    """要求正文时，只有用量的候选判失败（用量照记），换下一个候选。"""
    rendered: List[ModelCall] = []
    monkeypatch.setattr(router_module, 'render_model_call', rendered.append)
    router = _router(
        (_candidate('主力'), _ScriptClient([{'usage': _USAGE}])),
        (_candidate('备用', '厂商乙'), _ScriptClient([{'text': '看到了'}])),
    )

    assert await _collect(router, require_text=True) == [{'text': '看到了'}]

    rows = {row['model']: row for row in usage_rows()}
    assert (rows['主力']['failed_calls'], rows['主力']['prompt_tokens']) == (1, 200)
    assert (rows['备用']['failed_calls'], rows['备用']['unreported_calls']) == (0, 1)
    # 最终交给调用记录与面板的是最终候选的用量：备用没返回。
    assert rendered[0].model == '备用'
    assert rendered[0].usage is None


async def test_usage_only_stream_alone_fails_require_text(usage_rows) -> None:
    with pytest.raises(LlmError) as exc:
        await _collect(_router((_candidate(), _ScriptClient([{'usage': _USAGE}]))), require_text=True)

    assert exc.value.kind == 'model'
    [row] = usage_rows()
    assert (row['calls'], row['failed_calls'], row['prompt_tokens']) == (1, 1, 200)


async def test_format_failure_keeps_usage_and_counts_as_failed(usage_rows) -> None:
    """结构化校验失败的候选已生成完整输出，服务商照样计费：用量照记、结局为失败。"""
    router = _router(
        (_candidate('主力'), _ScriptClient([{'text': '不是 JSON'}, {'usage': _USAGE}])),
        (_candidate('备用', '厂商乙'), _ScriptClient([{'text': '{"a": 1}'}, {'usage': TokenUsage(5, 3, 8)}])),
    )

    assert await _collect(router, response_format={'type': 'json_object'}) == [{'text': '{"a": 1}'}]

    rows = {row['model']: row for row in usage_rows()}
    assert (rows['主力']['calls'], rows['主力']['failed_calls'], rows['主力']['total_tokens']) == (1, 1, 240)
    assert (rows['备用']['calls'], rows['备用']['failed_calls'], rows['备用']['total_tokens']) == (1, 0, 8)


async def test_user_abort_is_interrupted_and_settled_once(usage_rows) -> None:
    client = _ScriptClient([{'text': '我'}], error=LlmError('aborted', '生成已中断'))

    with pytest.raises(LlmError):
        await _collect(_router((_candidate(), client)))

    [row] = usage_rows()
    assert (row['calls'], row['interrupted_calls'], row['failed_calls']) == (1, 1, 0)


async def test_caller_closing_early_is_interrupted_and_settled_once(usage_rows) -> None:
    """调用方读到动作头就关流：尝试在收尾处落一次，结局为被打断。"""
    client = _ScriptClient([{'text': '第一片'}], hang=True)
    stream = _router((_candidate(), client)).stream([{'role': 'user', 'content': '在吗'}])

    assert await anext(stream) == {'text': '第一片'}
    await stream.aclose()

    assert client.closed
    [row] = usage_rows()
    assert (row['calls'], row['interrupted_calls'], row['failed_calls'], row['unreported_calls']) == (1, 1, 0, 0)


_VALIDATED = [
    ({'tool_call_validator': lambda calls: None},
     {'tool_calls': [{'id': 'c1', 'name': 'reply', 'arguments': '{}'}]}),
    ({'response_format': {'type': 'json_object'}}, {'text': '{"ok": true}'}),
]


@pytest.mark.parametrize('options,result', _VALIDATED)
async def test_closing_after_a_validated_response_is_ok(usage_rows, options, result) -> None:
    """缓冲校验的路径交付前已读完整条流：调用方拿到结果就关流，结局仍为正常。"""
    client = _ScriptClient([result, {'usage': TokenUsage(500, 20, 520)}])
    stream = _router((_candidate(), client)).stream([{'role': 'user', 'content': '在吗'}], **options)

    assert await anext(stream) == result
    await stream.aclose()

    [row] = usage_rows()
    assert (row['calls'], row['interrupted_calls'], row['failed_calls'], row['unreported_calls']) == (1, 0, 0, 0)
    assert row['prompt_tokens'] == 500


@pytest.mark.parametrize('options,result', _VALIDATED)
async def test_validated_response_without_usage_is_unreported_even_if_closed_early(usage_rows, options, result) -> None:
    """同一路径下服务商没返回用量：计入未返回，不被提前关流遮成中断。"""
    client = _ScriptClient([result])
    stream = _router((_candidate(), client)).stream([{'role': 'user', 'content': '在吗'}], **options)

    assert await anext(stream) == result
    await stream.aclose()

    [row] = usage_rows()
    assert (row['calls'], row['interrupted_calls'], row['unreported_calls']) == (1, 0, 1)


async def test_skipped_candidates_are_not_recorded(usage_rows) -> None:
    """同族跳过规则跳过的候选没有发请求，不记账。"""
    router = _router(
        (_candidate('gemini-a', '网关'), _ScriptClient([], error=LlmError('blocked', '内容被拦截'))),
        (_candidate('gemini-b', '网关'), _ScriptClient([{'text': '不该到这'}])),
        (_candidate('备用', '厂商乙'), _ScriptClient([{'text': '好'}])),
    )

    assert await _collect(router) == [{'text': '好'}]
    assert sorted(row['model'] for row in usage_rows()) == ['gemini-a', '备用']


# ---------------------------------------------------------------- B-8 账本


def _record(started_at: int, *, stream_id: int = 0, duration_ms: int = 100, cost: float = 0.0) -> UsageRecord:
    return UsageRecord(
        started_at=started_at, task='planner', model='主力', provider='厂商甲',
        stream_id=stream_id, plugin_id='', plugin_third_party=False, outcome='ok', metered=True,
        usage=TokenUsage(10, 2, 12), cost=cost, duration_ms=duration_ms,
    )


def test_same_hour_same_key_accumulates_and_hours_stay_apart(usage_rows) -> None:
    hour = 1_793_000_000_000 - 1_793_000_000_000 % HOUR_MS
    usage_ledger.record(_record(hour + 5_000, duration_ms=100, cost=0.25))
    usage_ledger.record(_record(hour + HOUR_MS - 1, duration_ms=300, cost=0.5))
    usage_ledger.record(_record(hour + HOUR_MS, duration_ms=50))

    first, second = usage_rows()
    assert first['hour_start'] == hour and second['hour_start'] == hour + HOUR_MS
    assert (first['calls'], first['prompt_tokens'], first['total_tokens']) == (2, 20, 24)
    assert first['cost'] == pytest.approx(0.75)
    # 耗时之和与平方之和，供算平均与标准差。
    assert (first['duration_ms_sum'], first['duration_ms_sq_sum']) == (400, 100 * 100 + 300 * 300)
    assert (second['calls'], second['duration_ms_sum'], second['duration_ms_sq_sum']) == (1, 50, 2500)


async def test_stream_id_defaults_to_zero_and_follows_the_session(usage_rows) -> None:
    router = _router((_candidate(), _ScriptClient([{'text': '好'}, {'usage': _USAGE}])))
    await _collect(router)
    enter_stage(RECEIVED, 7, '测试会话')
    await _collect(router)

    assert sorted(row['stream_id'] for row in usage_rows()) == [0, 7]
    assert all(row['plugin_id'] == '' for row in usage_rows())


async def test_ledger_write_failure_does_not_break_the_reply(usage_db, monkeypatch) -> None:
    """写库失败只记一条带异常的 warning，回复照常完成。"""

    class _BrokenConnection:
        def execute(self, *_args: Any) -> None:
            raise sqlite3.OperationalError('database is locked')

    # 坏连接只在这个块里生效，块结束即还原真实连接，再由 usage_db 正常关闭。
    with monkeypatch.context() as patch, capture_logs() as logs:
        patch.setattr(usage_ledger, '_connection', _BrokenConnection())
        chunks = await _collect(_router((_candidate(), _ScriptClient([{'text': '照常'}, {'usage': _USAGE}]))))

    assert chunks == [{'text': '照常'}]
    failures = [entry for entry in logs if entry['event'] == 'llm_usage_write_failed']
    assert len(failures) == 1
    assert failures[0]['log_level'] == 'warning'
    assert failures[0]['exc_info'] is True


async def test_unreported_warning_is_logged_once_per_model(monkeypatch, usage_rows) -> None:
    monkeypatch.setattr(usage_module, '_unreported_warned', set())
    router = _router((_candidate(), _ScriptClient([{'text': '好'}])))
    with capture_logs() as logs:
        await _collect(router)
        await _collect(router)

    warnings = [entry for entry in logs if entry['event'] == 'llm_usage_unreported']
    assert len(warnings) == 1
    assert (warnings[0]['model'], warnings[0]['provider']) == ('主力', '厂商甲')
    [row] = usage_rows()
    assert (row['calls'], row['unreported_calls']) == (2, 2)


# ---------------------------------------------------------------- B-12 调用记录与面板


async def test_exchange_record_carries_final_candidate_usage(tmp_path: Path, usage_rows) -> None:
    from src.core.llm_models import snapshot

    snapshot.configure_exchanges(tmp_path, 5)
    try:
        await _collect(_router((_candidate(), _ScriptClient([{'text': '好'}, {'usage': _USAGE}]))))
        await _collect(_router((_candidate(), _ScriptClient([{'text': '嗯'}]))))
    finally:
        snapshot.configure_exchanges(None)

    records = [
        json.loads(path.read_text(encoding='utf-8'))
        for path in sorted((tmp_path / 'chat').glob('*.json'))
    ]
    assert [record['usage'] for record in records] == [
        {'promptTokens': 200, 'completionTokens': 40, 'totalTokens': 240, 'cachedTokens': 50, 'reasoningTokens': 10},
        None,
    ]


def test_console_model_panel_shows_usage() -> None:
    from rich.console import Console

    call = ModelCall(
        task='planner', model='主力', provider='厂商甲', first_token_ms=800, total_ms=2100,
        text='好', usage=TokenUsage(12345, 678, 13023),
    )
    console = Console(width=100, record=True, color_system=None)
    console.print(render_stage_panel(call))
    assert '用量：输入 12,345 · 输出 678' in console.export_text()

    without = ModelCall(task='planner', model='主力', provider='厂商甲', first_token_ms=800, total_ms=2100, text='好')
    console = Console(width=100, record=True, color_system=None)
    console.print(render_stage_panel(without))
    assert '用量' not in console.export_text()
