"""日志落盘：等级分档、JSONL 形状、换文件与清理。

日志除 stdout 外还必须写入文件，确保终端滚动或进程异常退出后仍可进行离线排查。
"""

from __future__ import annotations

from pathlib import Path

import json
import time

import pytest

from src.core.logging.log_sink import JsonlFileSink, render_json_line
from src.core.logging.logger import current_log_file, get_logger, initialize_logging
from src.core.config.schema import LogConfig


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def test_json_line_keeps_reserved_keys_and_collects_the_rest() -> None:
    payload = render_json_line({
        'timestamp': '08-09 12:00:00',
        'level': 'error',
        'logger': 'src.core.services.chat',
        'event': 'turn_failed',
        'turnId': 5,
        'error': '中文错误',
    })
    assert payload['level'] == 'error'
    assert payload['event'] == 'turn_failed'
    assert payload['fields'] == {'turnId': 5, 'error': '中文错误'}


def test_json_line_omits_fields_when_no_kwargs() -> None:
    payload = render_json_line({'level': 'info', 'event': 'ready'})
    assert 'fields' not in payload


def test_sink_rotates_when_file_is_full(tmp_path: Path) -> None:
    sink = JsonlFileSink(tmp_path, max_bytes=200, max_files=10, cleanup_days=0)
    for i in range(20):
        sink.write({'event': f'line-{i}', 'padding': 'x' * 40})
    files = sorted(tmp_path.glob('app_*.log.jsonl'))
    assert len(files) > 1
    for path in files:
        assert path.stat().st_size <= 400


def test_sink_keeps_at_most_max_files(tmp_path: Path) -> None:
    sink = JsonlFileSink(tmp_path, max_bytes=120, max_files=3, cleanup_days=0)
    for i in range(40):
        sink.write({'event': f'line-{i}', 'padding': 'y' * 40})
    assert len(list(tmp_path.glob('app_*.log.jsonl'))) <= 3


def test_sink_deletes_files_past_cleanup_days(tmp_path: Path) -> None:
    stale = tmp_path / 'app_20200101_000000.log.jsonl'
    tmp_path.mkdir(parents=True, exist_ok=True)
    stale.write_text('{"event": "old"}\n', encoding='utf-8')
    old_time = time.time() - 30 * 86_400
    import os
    os.utime(stale, (old_time, old_time))

    sink = JsonlFileSink(tmp_path, max_bytes=1024, max_files=10, cleanup_days=7)
    sink.write({'event': 'fresh'})
    assert not stale.exists()


def test_console_and_file_levels_are_independent(tmp_path: Path, capsys) -> None:
    """终端只看 WARNING，文件留 DEBUG——排障不必改配置再复现一遍。"""
    initialize_logging(
        LogConfig(level='DEBUG', console_level='WARNING', file_level='DEBUG', color_scope='none'),
        tmp_path,
    )
    log = get_logger('src.core.services.chat')
    log.debug('debug_line')
    log.info('info_line')
    log.warning('warning_line')

    path = current_log_file()
    assert path is not None
    events = [entry['event'] for entry in _lines(path)]
    assert events == ['debug_line', 'info_line', 'warning_line']

    console = capsys.readouterr().out
    assert 'debug_line' not in console
    assert 'info_line' not in console
    assert 'warning_line' in console


def test_no_file_written_when_to_file_is_off(tmp_path: Path) -> None:
    initialize_logging(LogConfig(to_file=False), tmp_path)
    get_logger('src.core.services.chat').error('boom')
    assert current_log_file() is None
    assert list(tmp_path.glob('app_*.log.jsonl')) == []


def test_unknown_level_fails_loudly(tmp_path: Path) -> None:
    """等级名称错误必须在初始化时抛出，不能降级为 INFO。"""
    with pytest.raises(ValueError, match='未知的日志等级'):
        initialize_logging(LogConfig(level='INF0'), tmp_path)
    with pytest.raises(ValueError, match='未知的日志等级'):
        initialize_logging(LogConfig(library_levels={'httpx': 'WARN'}), tmp_path)
