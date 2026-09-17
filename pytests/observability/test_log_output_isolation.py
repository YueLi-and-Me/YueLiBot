"""日志输出目的地失效时的支路隔离与业务接缝测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, MutableMapping

import asyncio
import json
import sqlite3
import sys

import pytest

from src.core.config.schema import LogConfig
from src.core.logging.logger import emit_console_trace, get_logger, initialize_logging
from src.core.schedule.timeline import (
    DECISION_RETRY_MS,
    MINUTE_MS,
    Activity,
    ActivityTimeline,
    ActivityTransition,
)
from src.core.webui.logs import webui_logs

import src.core.logging.logger as logger_module


_CONSOLE_FAILURE_EVENT = '控制台日志输出失败，已停用控制台输出'
_FILE_FAILURE_EVENT = '文件日志写入失败，已停用文件日志'


class _FailingStdout:
    """记录 stdout 写入次数，并在每次写入时抛出指定的 OSError。"""

    def __init__(self, error: OSError) -> None:
        self.error = error
        self.write_attempts = 0

    def write(self, _text: str) -> int:
        self.write_attempts += 1
        raise self.error

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False


@pytest.fixture(autouse=True)
def _reset_output_branches(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """每条用例显式复位进程级熔断状态，不为生产代码增加测试开关。"""

    monkeypatch.setattr(logger_module, '_console_output_disabled', False, raising=False)
    monkeypatch.setattr(logger_module, '_file_output_disabled', False, raising=False)
    webui_logs.clear()
    yield
    webui_logs.clear()


def _jsonl_entries(directory: Path) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for path in sorted(directory.rglob('app_*.log.jsonl')):
        entries.extend(
            json.loads(line)
            for line in path.read_text(encoding='utf-8').splitlines()
        )
    return entries


def _webui_lines() -> List[str]:
    async def collect() -> List[str]:
        subscriber, backlog = webui_logs.subscribe()
        webui_logs.unsubscribe(subscriber)
        return [str(item['line']) for item in backlog]

    return asyncio.run(collect())


def _initialize_file_logging(directory: Path) -> None:
    initialize_logging(
        LogConfig(level='DEBUG', color_scope='none', to_file=True),
        directory,
    )


def _insert_open_awake(
    db: sqlite3.Connection,
    *,
    started_at: int,
    expected_until: int,
) -> int:
    with db:
        cursor = db.execute(
            """INSERT INTO activities
                 (kind, doing, mood, energy_pace, mood_pace, advances,
                  started_at, expected_until, ended_at, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                'awake',
                '继续处理手头的事情',
                '状态平稳',
                0,
                0,
                None,
                started_at,
                expected_until,
                None,
                'decided',
            ),
        )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


@pytest.mark.parametrize(
    'error_factory',
    [
        pytest.param(lambda: OSError(22, 'Invalid argument'), id='oserror-22'),
        pytest.param(lambda: BrokenPipeError(32, 'Broken pipe'), id='broken-pipe-32'),
    ],
)
def test_控制台写失败只记录一次并跨重新初始化保持停用(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_factory: Callable[[], OSError],
) -> None:
    _initialize_file_logging(tmp_path)
    failing = _FailingStdout(error_factory())
    monkeypatch.setattr(sys, 'stdout', failing)
    log = get_logger('src.core.logging.logger')

    log.warning('触发控制台失败', marker='first')
    log.warning('控制台已经停用', marker='second')
    _initialize_file_logging(tmp_path / 'after-reinitialize')
    log.warning('重新初始化后仍停用', marker='third')

    assert failing.write_attempts == 1
    entries = _jsonl_entries(tmp_path)
    failures = [entry for entry in entries if entry['event'] == _CONSOLE_FAILURE_EVENT]
    assert len(failures) == 1
    assert failures[0]['level'] == 'error'
    assert failures[0]['logger'] == 'src.core.logging.logger'
    assert failures[0]['fields'] == {
        'errno': failing.error.errno,
        'error': str(failing.error),
        'stream': 'stdout',
    }
    assert sorted(
        entry['event']
        for entry in entries
        if entry['event'] != _CONSOLE_FAILURE_EVENT
    ) == sorted(
        [
            '触发控制台失败',
            '控制台已经停用',
            '重新初始化后仍停用',
        ]
    )


def test_trace复用受保护入口且不重复记录控制台失败(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialize_file_logging(tmp_path)
    failing = _FailingStdout(OSError(22, 'Invalid argument'))
    monkeypatch.setattr(sys, 'stdout', failing)

    emit_console_trace({'at': 1_789_000_000_000, 'kind': 'probe', 'value': 1})
    emit_console_trace({'at': 1_789_000_000_001, 'kind': 'probe', 'value': 2})

    assert failing.write_attempts == 1
    failures = [
        entry for entry in _jsonl_entries(tmp_path)
        if entry['event'] == _CONSOLE_FAILURE_EVENT
    ]
    assert len(failures) == 1
    assert sum(_CONSOLE_FAILURE_EVENT in line for line in _webui_lines()) == 1


def test_文件写失败后控制台和webui各记录一次且不再写文件(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _initialize_file_logging(tmp_path)
    log = get_logger('src.core.logging.logger')
    log.info('先创建日志文件')
    capsys.readouterr()
    sink = logger_module._file_sink
    assert sink is not None
    path = sink.current_path()
    assert path is not None
    attempts = 0

    def fail_write(_payload: Dict[str, Any]) -> None:
        nonlocal attempts
        attempts += 1
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr(sink, 'write', fail_write)

    log.error('触发文件失败')
    log.error('文件已经停用')

    assert attempts == 1
    console_entries = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
    ]
    console_failures = [
        entry for entry in console_entries
        if entry['event'] == _FILE_FAILURE_EVENT
    ]
    assert console_failures == [
        {
            'timestamp': console_failures[0]['timestamp'],
            'level': 'error',
            'logger': 'src.core.logging.logger',
            'event': _FILE_FAILURE_EVENT,
            'errno': 28,
            'error': '[Errno 28] No space left on device',
            'path': str(path),
        }
    ]
    lines = _webui_lines()
    assert sum(_FILE_FAILURE_EVENT in line for line in lines) == 1
    failure_line = next(line for line in lines if _FILE_FAILURE_EVENT in line)
    assert '28' in failure_line
    assert 'No space left on device' in failure_line
    assert str(path) in failure_line

    _initialize_file_logging(tmp_path / 'after-reinitialize')
    replacement = logger_module._file_sink
    assert replacement is not None
    replacement_attempts = 0

    def count_replacement_write(_payload: Dict[str, Any]) -> None:
        nonlocal replacement_attempts
        replacement_attempts += 1

    monkeypatch.setattr(replacement, 'write', count_replacement_write)
    log.error('重新初始化后文件仍停用')
    assert replacement_attempts == 0


def test_控制台与文件先后失效时两条失败记录都只进入webui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialize_file_logging(tmp_path)
    log = get_logger('src.core.logging.logger')
    log.info('先创建日志文件')
    sink = logger_module._file_sink
    assert sink is not None
    file_attempts = 0

    def fail_file(_payload: Dict[str, Any]) -> None:
        nonlocal file_attempts
        file_attempts += 1
        raise OSError(28, 'No space left on device')

    failing_stdout = _FailingStdout(BrokenPipeError(32, 'Broken pipe'))
    monkeypatch.setattr(sink, 'write', fail_file)
    monkeypatch.setattr(sys, 'stdout', failing_stdout)

    log.error('两个输出目的地一起失效')
    log.error('业务仍继续')

    assert file_attempts == 1
    assert failing_stdout.write_attempts == 1
    lines = _webui_lines()
    assert sum(_FILE_FAILURE_EVENT in line for line in lines) == 1
    assert sum(_CONSOLE_FAILURE_EVENT in line for line in lines) == 1


def test_关闭文件日志时控制台失败记录进入webui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_logging(LogConfig(level='DEBUG', color_scope='none', to_file=False))
    failing = _FailingStdout(OSError(22, 'Invalid argument'))
    monkeypatch.setattr(sys, 'stdout', failing)

    get_logger('src.core.logging.logger').warning('没有文件支路')

    assert failing.write_attempts == 1
    assert sum(_CONSOLE_FAILURE_EVENT in line for line in _webui_lines()) == 1


def test_render_json_line的TypeError继续外抛(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialize_file_logging(tmp_path)

    def fail_render(_event_dict: MutableMapping[str, Any]) -> Dict[str, Any]:
        raise TypeError('render failed')

    monkeypatch.setattr(logger_module, 'render_json_line', fail_render)
    with pytest.raises(TypeError, match='render failed'):
        get_logger('src.core.logging.logger').error('渲染失败')


def test_webui渲染器的TypeError继续外抛(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_logging(LogConfig(level='DEBUG', color_scope='none', to_file=False))

    def fail_renderer(
        _logger: Any,
        _method_name: str,
        _event_dict: MutableMapping[str, Any],
    ) -> str:
        raise TypeError('renderer failed')

    monkeypatch.setattr(logger_module._webui_log_handler, '_renderer', fail_renderer)
    with pytest.raises(TypeError, match='renderer failed'):
        get_logger('src.core.logging.logger').error('渲染器失败')


@pytest.mark.asyncio
async def test_控制台失效不回滚awake累计时长截断(
    db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_789_000_000_000
    started_at = now - 100 * MINUTE_MS
    activity_id = _insert_open_awake(
        db,
        started_at=started_at,
        expected_until=now,
    )

    async def continue_for_sixty(
        _activity: Activity,
        _now: int,
        _gap_ms: int,
    ) -> ActivityTransition:
        return ActivityTransition(continuation_minutes=60)

    initialize_logging(LogConfig(level='DEBUG', color_scope='none', to_file=False))
    monkeypatch.setattr(sys, 'stdout', _FailingStdout(OSError(22, 'Invalid argument')))
    timeline = ActivityTimeline(db, decider=continue_for_sixty)
    activity = timeline._open_activity()
    assert activity is not None

    await timeline._advance(activity, now)

    row = db.execute(
        'SELECT expected_until FROM activities WHERE id = ?',
        (activity_id,),
    ).fetchone()
    assert row is not None
    assert int(row['expected_until']) == started_at + 120 * MINUTE_MS


@pytest.mark.asyncio
async def test_控制台失效不阻断决策失败后的续期(
    db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_789_000_000_000
    activity_id = _insert_open_awake(
        db,
        started_at=now - 30 * MINUTE_MS,
        expected_until=now,
    )

    async def fail_decision(
        _activity: Activity,
        _now: int,
        _gap_ms: int,
    ) -> ActivityTransition:
        raise ValueError('决策器测试失败')

    initialize_logging(LogConfig(level='DEBUG', color_scope='none', to_file=False))
    monkeypatch.setattr(sys, 'stdout', _FailingStdout(OSError(22, 'Invalid argument')))
    timeline = ActivityTimeline(db, decider=fail_decision)
    activity = timeline._open_activity()
    assert activity is not None

    await timeline._advance(activity, now)

    row = db.execute(
        'SELECT expected_until FROM activities WHERE id = ?',
        (activity_id,),
    ).fetchone()
    assert row is not None
    assert int(row['expected_until']) == now + DECISION_RETRY_MS
