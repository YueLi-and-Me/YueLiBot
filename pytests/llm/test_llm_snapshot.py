"""失败调用快照：记实际请求体，隐去密钥，按数量保留。"""

from __future__ import annotations

from pathlib import Path

import json

from src.core.llm_models.snapshot import configure, dump, record_request

_SECRET = 'sk-do-not-write-this-anywhere'


def _record(tmp_path: Path, body: dict) -> None:
    configure(tmp_path, max_files=50)
    record_request(
        'https://api.example.com/v1/chat/completions',
        {'Content-Type': 'application/json', 'Authorization': f'Bearer {_SECRET}'},
        body,
    )


def test_snapshot_keeps_the_body_that_was_actually_sent(tmp_path: Path) -> None:
    _record(tmp_path, {
        'model': 'm',
        'messages': [],
        'temperature': 0.1,
        'thinking': {'type': 'disabled'},
    })
    path = dump('expression', 'ValueError', '不是合法 JSON')
    assert path is not None

    payload = json.loads(path.read_text(encoding='utf-8'))
    assert payload['task'] == 'expression'
    assert payload['error'] == {'type': 'ValueError', 'message': '不是合法 JSON'}
    assert payload['provider_request']['body']['thinking'] == {'type': 'disabled'}
    assert payload['provider_request']['body']['temperature'] == 0.1


def test_snapshot_never_writes_credentials(tmp_path: Path) -> None:
    _record(tmp_path, {'model': 'm', 'messages': [], 'api_key': _SECRET})
    path = dump('chat', 'LlmError', 'auth')
    assert path is not None
    assert _SECRET not in path.read_text(encoding='utf-8')
    assert payload_auth(path) == '[已隐去]'


def payload_auth(path: Path) -> str:
    return json.loads(path.read_text(encoding='utf-8'))['provider_request']['headers']['Authorization']


def test_extra_fields_are_carried(tmp_path: Path) -> None:
    _record(tmp_path, {'model': 'm', 'messages': []})
    path = dump('chat', 'LlmError', 'quota', {'turnId': 7})
    assert path is not None
    assert json.loads(path.read_text(encoding='utf-8'))['extra'] == {'turnId': 7}


def test_snapshot_keeps_at_most_max_files(tmp_path: Path) -> None:
    configure(tmp_path, max_files=3)
    for i in range(10):
        record_request('https://api.example.com/v1/chat/completions', {}, {'seq': i})
        dump('chat', 'LlmError', f'error-{i}')
    assert len(list(tmp_path.glob('*.json'))) <= 3


def test_disabled_snapshots_write_nothing(tmp_path: Path) -> None:
    configure(None)
    record_request('https://api.example.com/v1/chat/completions', {}, {'model': 'm'})
    assert dump('chat', 'LlmError', 'boom') is None
    assert list(tmp_path.glob('*.json')) == []


def test_dump_without_a_recorded_request_is_a_noop(tmp_path: Path) -> None:
    """没发出过请求就没有现场可留，不该凭空造一份空快照。"""
    configure(tmp_path, max_files=50)
    from src.core.llm_models import snapshot
    snapshot._current.set(None)
    assert dump('chat', 'LlmError', 'boom') is None
