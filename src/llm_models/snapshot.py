"""记录失败模型调用的分层、脱敏 JSON 快照。

上下文变量保存内部请求、当前候选、实际 provider 请求和候选尝试；写入快照前
会移除常见密钥字段，并按配置的最大文件数清理旧快照。
"""

from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
    """配置快照目录和最大文件数，并清空当前请求上下文。

    :param directory: 快照目录；`None` 表示禁用文件写入。
    :param max_files: 最多保留的 JSON 快照数，默认值为 50。
    :side_effects: 修改模块级目录/上限并重置当前 ContextVar。
    """
    global _directory, _max_files
    _directory = directory
    _max_files = max_files
    _current.set(None)


def _redact(value: Any) -> Any:
    """递归删除映射和列表中的敏感字段值。

    :param value: 任意 JSON 风格值。
    :return: 脱敏后的新映射/列表；标量原样返回。
    :side_effects: 不修改输入容器。
    :performance: 按容器元素数量递归遍历。
    """
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
    """在候选循环开始前记录调用方的模型请求快照。

    Args:
        task: 模型任务名称。
        stage: 当前观测阶段名称。
        turn_id: 当前聊天轮次 ID；无聊天轮次时为 ``None``。
        stream_id: 当前 stream ID；无 stream 上下文时为 ``None``。
        messages: 发送给路由器的消息列表。
        temperature: 请求采样温度；无配置时为 ``None``。
        max_tokens: 请求最大 token 数；无配置时为 ``None``。
        response_format: 可选响应格式字典。

    Side Effects:
        覆盖当前异步上下文中的快照状态，并深复制消息和响应格式；不写入磁盘。
    """
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
    """记录当前任务实际选择的模型候选。

    :param model: 模型内部名称。
    :param provider: API 厂商名称。
    :param kind: 厂商类型。
    :side_effects: 更新当前异步上下文的候选字段。
    """
    state = _state()
    state['candidate'] = {
        'model': model,
        'provider': provider,
        'kind': kind,
    }


def current_candidate() -> Dict[str, Any]:
    """返回当前异步上下文中的候选快照。

    :return: 候选字典的深复制；未选择候选时返回空字典。
    :side_effects: 不修改上下文状态。
    """
    candidate = _state().get('candidate')
    return deepcopy(candidate) if isinstance(candidate, dict) else {}


def record_provider_request(
    url: str,
    headers: Dict[str, str],
    body: Dict[str, Any],
    *,
    candidate: Dict[str, Any] | None = None,
    secret_header_name: str = '',
    secret_query_name: str = '',
) -> None:
    """记录实际 provider 请求，并对 URL、请求头和请求体执行脱敏。

    Args:
        url: 实际请求 URL。
        headers: 实际请求头映射。
        body: 实际请求体映射。
        candidate: 可选模型候选快照；省略时使用当前上下文候选。
        secret_header_name: 额外需要脱敏的请求头名称，默认为空字符串。
        secret_query_name: 需要脱敏的 URL 查询参数名称，默认为空字符串。

    Side Effects:
        更新当前异步上下文中的 provider 请求字段；不发起网络请求、不修改输入映射。
    """
    state = _state()
    selected = current_candidate() if candidate is None else deepcopy(candidate)
    secret_headers = _SECRET_HEADERS | frozenset({secret_header_name.lower()})
    state['provider_request'] = {
        'url': _redact_url(url, secret_query_name),
        'headers': {
            key: (_REDACTED if key.lower() in secret_headers else value)
            for key, value in headers.items()
        },
        'body': _redact(body),
        'candidate': selected,
    }


def _redact_url(url: str, secret_name: str) -> str:
    """对 URL 中指定的查询参数值执行脱敏。

    :param url: 原始请求 URL。
    :param secret_name: 需要替换的查询参数名；为空时返回原 URL。
    :return: 查询参数值被替换为脱敏标记后的 URL。
    :side_effects: 不执行网络请求。
    """
    if not secret_name:
        return url
    parts = urlsplit(url)
    query = urlencode([
        (key, _REDACTED if key == secret_name else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def record_request(url: str, headers: Dict[str, str], body: Dict[str, Any]) -> None:
    """记录没有额外候选信息的 provider 请求。

    Args:
        url: 实际请求 URL。
        headers: 实际请求头映射。
        body: 实际请求体映射。

    Side Effects:
        委托 ``record_provider_request`` 更新当前异步上下文；不发起网络请求。
    """
    record_provider_request(url, headers, body)


def record_attempt(
    *,
    model: str,
    provider: str,
    error_kind: str,
    message: str,
) -> None:
    """把一次候选失败追加到当前请求的尝试列表。

    :param model: 失败模型名称。
    :param provider: 失败厂商名称。
    :param error_kind: 错误类别。
    :param message: 可读错误消息。
    :side_effects: 修改当前异步上下文的尝试列表。
    """
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
    """将当前请求和失败信息写入脱敏 JSON 快照。

    :param task: 任务名称。
    :param error_type: 最终错误类型。
    :param error: 最终错误消息。
    :param extra: 可选附加观测字段。
    :return: 新建快照路径；未启用目录、没有上下文或没有请求记录时返回 `None`。
    :raises OSError: 目录创建或文件写入失败。
    :side_effects: 创建 JSON 文件并删除超出保留上限的旧文件。
    """
    # 未启用快照或当前上下文没有请求时不创建空诊断文件。
    if _directory is None:
        return None
    state = _current.get()
    if state is None:
        return None
    internal_request = state.get('internal_request')
    provider_request = state.get('provider_request')
    if internal_request is None and provider_request is None:
        return None

    # 复制附加字段，避免调用方后续修改可变对象而改变待写入快照内容。
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
    # 写入后统一裁剪旧文件，确保失败高峰不会无限增长日志目录。
    _prune()
    return path


def _state() -> Dict[str, Any]:
    """获取或初始化当前异步上下文的快照状态。

    :return: 当前上下文中的可变状态字典。
    :side_effects: 首次调用时向 ContextVar 写入空状态。
    """
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
    """删除超过最大保留数量的旧快照文件。

    :side_effects: 可能删除快照目录中最早的 JSON 文件；目录未启用时无操作。
    :raises OSError: 删除文件失败时传播异常。
    """
    if _directory is None:
        return
    files = _existing_files()
    while len(files) > _max_files:
        files.pop(0).unlink(missing_ok=True)


def _existing_files() -> List[Path]:
    """返回快照目录中按修改时间和文件名排序的 JSON 文件。

    :return: 由旧到新的快照路径列表；目录不存在时返回空列表。
    :side_effects: 只读取目录和文件元数据。
    """
    if _directory is None or not _directory.exists():
        return []
    return sorted(_directory.glob('*.json'), key=lambda path: (path.stat().st_mtime, path.name))
