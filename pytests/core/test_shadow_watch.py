"""shadow_watch 脚本的批量加载与一次性渲染回归。"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
import json
import sqlite3

from scripts.eval import shadow_watch


def _event_payload(model_task: str, prompt_hash: str, watermark: int) -> str:
    return json.dumps({
        'version': {
            'modelTask': model_task,
            'promptHash': prompt_hash,
        },
        'messageWatermark': watermark,
    })


def _create_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(':memory:')
    conn.executescript('''
        CREATE TABLE pipeline_events (
            seq INTEGER PRIMARY KEY,
            at INTEGER,
            stream_id INTEGER,
            turn_id INTEGER,
            kind TEXT,
            payload TEXT
        );
        CREATE TABLE messages (
            stream_id INTEGER,
            id INTEGER,
            content TEXT
        );
    ''')
    return conn


def test_load_samples_batches_text_and_legacy_action_queries() -> None:
    """每条样本不再逐行点查询，相关文本与旧动作按批取回。"""
    conn = _create_connection()
    conn.executemany(
        'INSERT INTO messages(stream_id, id, content) VALUES (?, ?, ?)',
        [(1, 101, '正文甲'), (1, 102, '正文乙'), (2, 201, '正文丙')],
    )
    conn.executemany(
        'INSERT INTO pipeline_events(seq, at, stream_id, turn_id, kind, payload) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        [
            (1, 1_000, 1, 7, 'action_decision', _event_payload('chat.conversation.shadow', 'h1', 101)),
            (2, 2_000, 1, 8, 'action_decision', _event_payload('chat.conversation.shadow', 'h1', 102)),
            (3, 3_000, 2, 9, 'action_decision', _event_payload('chat.conversation.shadow', 'old', 201)),
            (4, 4_000, 1, 7, 'turn_action', json.dumps({'action': 'silent'})),
            (5, 5_000, 1, 7, 'turn_action', json.dumps({'action': 'reply'})),
        ],
    )
    queries = 0

    def trace(statement: str) -> None:
        nonlocal queries
        if statement.lstrip().upper().startswith('SELECT'):
            queries += 1

    conn.set_trace_callback(trace)

    samples = shadow_watch._load_samples(
        conn,
        min_seq=0,
        prompt_hash='h1',
        model_task='chat.conversation.shadow',
    )

    assert [sample.seq for sample in samples] == [1, 2]
    assert samples[0].text == '正文甲'
    assert samples[0].legacy_action == 'reply'
    assert samples[1].text == '正文乙'
    assert samples[1].legacy_action == ''
    # 主查询 + 消息正文 + 旧动作各一次批量查询，而不是 2×N 次逐行点查询。
    assert queries == 3


def test_run_once_renders_report_once(monkeypatch, tmp_path, capsys) -> None:
    """_run_once 写文件与终端输出复用同一份渲染结果。"""
    class _FakeConn:
        def __enter__(self) -> sqlite3.Connection:
            return self  # type: ignore[return-value]

        def __exit__(self, *_args: object) -> bool:
            return False

    render_count = 0

    def _fake_render(report: shadow_watch.ShadowReport) -> str:
        nonlocal render_count
        render_count += 1
        return f'# 渲染次数={render_count}'

    monkeypatch.setattr(shadow_watch, '_connect', lambda _db: _FakeConn())
    monkeypatch.setattr(shadow_watch, '_load_samples', lambda *_args, **_kwargs: [])
    monkeypatch.setattr(shadow_watch, '_render_report', _fake_render)

    report_path = tmp_path / 'shadow-watch.md'
    args = Namespace(
        db=Path('memory.db'),
        report=report_path,
        hash='',
        model_task=shadow_watch.DEFAULT_MODEL_TASK,
    )

    assert shadow_watch._run_once(args) == 0
    assert render_count == 1
    assert report_path.read_text(encoding='utf-8') == '# 渲染次数=1'
    assert '# 渲染次数=1' in capsys.readouterr().out
