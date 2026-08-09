"""失败模型调用的分层快照。"""

from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import json


_REDACTED = '[已隐去]'
_SECRET_HEADERS = frozenset({'authorization', 'x-api-key', 'api-key'})
_SECRET_FIELDS = frozenset({
    'api_key', 'apikey', 'access_token', 'app_key', 'app_secret',
    'authorization', 'password', 'secret', 'token',
})

_current: ContextVar[Dict[str, Any] | None] = ContextVar('llm_snapshot', default=None)
_directory: Path | None = None
_max_files = 50


def configure(directory: Path | None, max_files: int = 50) -> None:
    global _directory, _max_files
    _directory = directory
    _max_files = max_files
    _current.set(None)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (_REDACTED if str(key).lower() in _SECRET_FIELDS else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def record_internal_request(
    *,
    task: str,
    stage: str,
    turn_id: int | None,
    stream_id: int | None,
    messages: List[dict],
    temperature: float | None,
    max_tokens: int | None,
    response_format: Dict[str, str] | None,
) -> None:
    """在候选循环前记录调用方要求。"""
    _current.set({
        'internal_request': {
            'task': task,
            'stage': stage,
            'turnId': turn_id,
            'streamId': stream_id,
            'messages': deepcopy(messages),
            'temperature': temperature,
            'maxTokens': max_tokens,
            'responseFormat': deepcopy(response_format),
        },
        'provider_request': None,
        'attempts': [],
        'candidate': None,
    })


def select_candidate(
    *,
    model: str,
    provider: str,
    kind: str,
) -> None:
    state = _state()
    state['candidate'] = {
        'model': model,
        'provider': provider,
        'kind': kind,
    }


def current_candidate() -> Dict[str, Any]:
    candidate = _state().get('candidate')
    return deepcopy(candidate) if isinstance(candidate, dict) else {}


def record_provider_request(
    url: str,
    headers: Dict[str, str],
    body: Dict[str, Any],
    *,
    candidate: Dict[str, Any] | None = None,
) -> None:
    """记录实际请求，并摘除敏感字段。"""
    state = _state()
    selected = current_candidate() if candidate is None else deepcopy(candidate)
    state['provider_request'] = {
        'url': url,
        'headers': {
            key: (_REDACTED if key.lower() in _SECRET_HEADERS else value)
            for key, value in headers.items()
        },
        'body': _redact(body),
        'candidate': selected,
    }


def record_request(url: str, headers: Dict[str, str], body: Dict[str, Any]) -> None:
    """兼容直接调用 provider 的测试入口。"""
    record_provider_request(url, headers, body)


def record_attempt(
    *,
    model: str,
    provider: str,
    error_kind: str,
    message: str,
) -> None:
    attempts = _state()['attempts']
    attempts.append({
        'model': model,
        'provider': provider,
        'errorKind': error_kind,
        'message': message,
    })


def dump(
    task: str,
    error_type: str,
    error: str,
    extra: Dict[str, Any] | None = None,
) -> Path | None:
    if _directory is None:
        return None
    state = _current.get()
    if state is None:
        return None
    internal_request = state.get('internal_request')
    provider_request = state.get('provider_request')
    if internal_request is None and provider_request is None:
        return None

    payload: Dict[str, Any] = {
        'at': datetime.now().isoformat(timespec='seconds'),
        'task': task,
        'error': {'type': error_type, 'message': error},
        'internal_request': internal_request,
        'provider_request': provider_request,
        'attempts': deepcopy(state['attempts']),
    }
    if extra:
        payload['extra'] = deepcopy(extra)

    _directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    path = _directory / f'{stamp}_{task}.json'
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding='utf-8',
    )
    _prune()
    return path


def _state() -> Dict[str, Any]:
    state = _current.get()
    if state is None:
        state = {
            'internal_request': None,
            'provider_request': None,
            'attempts': [],
            'candidate': None,
        }
        _current.set(state)
    return state


def _prune() -> None:
    if _directory is None:
        return
    files = _existing_files()
    while len(files) > _max_files:
        files.pop(0).unlink(missing_ok=True)


def _existing_files() -> List[Path]:
    if _directory is None or not _directory.exists():
        return []
    return sorted(_directory.glob('*.json'), key=lambda path: (path.stat().st_mtime, path.name))
