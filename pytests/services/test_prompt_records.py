"""分阶段调用记录读取侧回归：列表摘要、单份读取与路径穿越防护。

写入侧在 pytests/llm/test_router.py 覆盖；本文件只验证面板读取这一半。
"""

from __future__ import annotations

import json

import pytest

from src.core.llm_models import snapshot
from src.core.services.prompt_records import (
    RecordsDisabled,
    list_records,
    list_tasks,
    read_record,
)


def _write(root, task: str, name: str, payload: dict) -> None:
    directory = root / task
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(
        json.dumps(payload, ensure_ascii=False), encoding='utf-8',
    )


def _payload(task: str, text: str, total_ms: int = 100, error=None) -> dict:
    return {
        'at': '2026-08-21T02:00:00.000',
        'task': task,
        'stage': 'generating',
        'streamId': 3,
        'turnId': 41,
        'model': {'name': 'm1', 'provider': 'p1', 'candidate': {}},
        'timing': {'firstTokenMs': 10, 'totalMs': total_ms},
        'request': {'messages': [{'role': 'user', 'content': '在吗'}]},
        'response': {'text': text, 'reasoning': '', 'chunks': 2},
        'attempts': [],
        'error': error,
    }


@pytest.fixture
def records_root(tmp_path):
    snapshot.configure_exchanges(tmp_path, 50)
    yield tmp_path
    snapshot.configure_exchanges(None)


def test_lists_tasks_and_summaries(records_root) -> None:
    """摘要只取列表需要的字段，不把整段 messages 带进列表响应。"""
    _write(records_root, 'chat-conversation', '20260821_020000_000001_s3.json',
           _payload('chat.conversation', '我在'))
    _write(records_root, 'chat-summary', '20260821_020001_000002_s3.json',
           _payload('chat.summary', '摘要'))

    assert list_tasks() == ['chat-conversation', 'chat-summary']

    records = list_records(None, 10)
    assert len(records) == 2
    assert {item['task'] for item in records} == {'chat.conversation', 'chat.summary'}
    first = records[0]
    assert first['textLength'] == len('摘要') or first['textLength'] == len('我在')
    assert 'messages' not in first
    assert first['streamId'] == 3 and first['turnId'] == 41


def test_filters_by_task(records_root) -> None:
    """按任务筛选是面板区分 planner 与 replyer 的主要入口。"""
    _write(records_root, 'chat-conversation', 'a.json', _payload('chat.conversation', '甲'))
    _write(records_root, 'chat-summary', 'b.json', _payload('chat.summary', '乙'))

    records = list_records('chat-summary', 10)

    assert [item['task'] for item in records] == ['chat.summary']


def test_reads_single_record(records_root) -> None:
    """单份读取返回完整内容，含请求消息全文。"""
    _write(records_root, 'chat-conversation', 'a.json', _payload('chat.conversation', '我在'))

    record = read_record('chat-conversation', 'a.json')

    assert record['request']['messages'] == [{'role': 'user', 'content': '在吗'}]
    assert record['response']['text'] == '我在'


def test_rejects_path_traversal(records_root) -> None:
    """任务名与文件名都不接受分隔符与上跳，参数来自 HTTP 不可信。"""
    for task, name in (
        ('..', 'a.json'),
        ('chat-conversation', '../../secret.json'),
        ('chat/conversation', 'a.json'),
        ('chat-conversation', 'a.txt'),
    ):
        with pytest.raises(ValueError):
            read_record(task, name)


def test_broken_file_is_skipped_not_fatal(records_root) -> None:
    """一份损坏记录不能挡住其余记录——写入中途被读到是正常情况。"""
    _write(records_root, 'chat-conversation', 'good.json', _payload('chat.conversation', '我在'))
    (records_root / 'chat-conversation' / 'broken.json').write_text('{不是 JSON', encoding='utf-8')

    records = list_records('chat-conversation', 10)

    assert [item['name'] for item in records] == ['good.json']


def test_disabled_reports_distinctly(tmp_path) -> None:
    """未启用要能与「启用但还没有记录」区分开。"""
    snapshot.configure_exchanges(None)

    with pytest.raises(RecordsDisabled):
        list_records(None, 10)
