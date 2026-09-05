"""记录模型调用的分层、脱敏 JSON 记录。

本模块有两条独立的落盘链路，共用同一份上下文快照与脱敏逻辑：

- 失败快照（``dump``）：只在调用失败时写 ``logs/llm_request/``，用于事后
  排查一次具体故障，按总文件数轮转。
- 分阶段调用记录（``dump_exchange``）：每次模型调用结束都写
  ``logs/prompt/<任务>/<stream>/<毫秒时间戳>.json``，成功与失败都留，用于逐
  阶段回看「这一次到底发给模型什么、它回了什么」。按任务分目录各自轮转，
  一个高频任务连续写入不会冲掉其他任务的记录。

上下文变量保存内部请求、当前候选、实际 provider 请求和候选尝试；写入前会移除
常见密钥字段。调用方只需在路由层收尾时调用一次 ``dump_exchange``。
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
_render_params: ContextVar[Dict[str, Dict[str, str]] | None] = ContextVar(
    'llm_render_params',
    default=None,
)
_directory: Path | None = None
_max_files = 50
_exchange_directory: Path | None = None
_exchange_max_files = 200


def configure(directory: Path | None, max_files: int = 50) -> None:
    """配置失败快照目录和最大文件数，并清空当前请求上下文。

    :param directory: 快照目录；`None` 表示禁用文件写入。
    :param max_files: 最多保留的 JSON 快照数，默认值为 50。
    副作用：修改模块级目录/上限并重置当前 ContextVar。
    """
    global _directory, _max_files
    _directory = directory
    _max_files = max_files
    _current.set(None)
    _render_params.set(None)


def configure_exchanges(directory: Path | None, max_files_per_task: int = 200) -> None:
    """配置分阶段调用记录的根目录与每个任务的保留份数。

    :param directory: 记录根目录；``None`` 表示关闭该链路，只保留失败快照。
    :param max_files_per_task: 每个任务子目录最多保留的记录数，默认 200。
        按任务分别计数，回复这类高频任务不会把日程、总结那类低频任务的记录挤掉。
    副作用：只修改模块级配置，不触碰当前 ContextVar——失败快照与调用记录共用
        同一份上下文，重置会让先配置的那条链路丢掉已记录的请求。
    """
    global _exchange_directory, _exchange_max_files
    _exchange_directory = directory
    _exchange_max_files = max_files_per_task


def bind_render_params(render_params: Dict[str, Dict[str, str]]) -> None:
    """把本次模型调用的提示词渲染参数绑定到当前异步上下文。"""
    _render_params.set(deepcopy(render_params))


def current_render_params() -> Dict[str, Dict[str, str]] | None:
    """返回当前异步上下文绑定的提示词渲染参数副本。"""
    values = _render_params.get()
    return deepcopy(values) if values is not None else None


def _redact(value: Any) -> Any:
    """递归删除映射和列表中的敏感字段值。

    :param value: 任意 JSON 风格值。
    :return: 脱敏后的新映射/列表；标量原样返回。
    副作用：不修改输入容器。
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
    render_params: Dict[str, Dict[str, str]] | None,
) -> None:
    """在候选循环开始前记录调用方的模型请求快照。

    :param task: 模型任务名称。
    :param stage: 当前观测阶段名称。
    :param turn_id: 当前聊天轮次 ID；无聊天轮次时为 ``None``。
    :param stream_id: 当前 stream ID；无 stream 上下文时为 ``None``。
    :param messages: 发送给路由器的消息列表。
    :param temperature: 请求采样温度；无配置时为 ``None``。
    :param max_tokens: 请求最大 token 数；无配置时为 ``None``。
    :param response_format: 可选响应格式字典。

    副作用：
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
            'renderParams': deepcopy(render_params),
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
    副作用：更新当前异步上下文的候选字段。
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
    副作用：不修改上下文状态。
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

    :param url: 实际请求 URL。
    :param headers: 实际请求头映射。
    :param body: 实际请求体映射。
    :param candidate: 可选模型候选快照；省略时使用当前上下文候选。
    :param secret_header_name: 额外需要脱敏的请求头名称，默认为空字符串。
    :param secret_query_name: 需要脱敏的 URL 查询参数名称，默认为空字符串。

    副作用：
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
    副作用：不执行网络请求。
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

    :param url: 实际请求 URL。
    :param headers: 实际请求头映射。
    :param body: 实际请求体映射。

    副作用：
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
    副作用：修改当前异步上下文的尝试列表。
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
    副作用：创建 JSON 文件并删除超出保留上限的旧文件。
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
    副作用：首次调用时向 ContextVar 写入空状态。
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


def exchange_directory() -> Path | None:
    """返回分阶段调用记录的根目录。

    观察面板据此列出与读取记录文件；关闭记录时返回 ``None``，调用方应据此
    回报「未启用」而不是空列表——两者对使用者的含义完全不同。

    :return: 记录根目录；未启用时为 ``None``。
    """
    return _exchange_directory


def dump_exchange(
    *,
    task: str,
    model: str,
    provider: str,
    output_text: str,
    reasoning_text: str,
    tool_calls: list[dict] | None,
    chunk_count: int,
    first_token_ms: int | None,
    total_ms: int,
    error_type: str = '',
    error: str = '',
) -> Path | None:
    """把一次模型调用的请求与产出写成分阶段 JSON 记录。

    与 ``dump`` 的区别是成功时也写入：分阶段记录的用途不是排查故障，而是回看
    每一级 Agent 各自看到了什么、说了什么。planner / replyer 拆分之后同一个回合
    会有多次模型调用，只靠控制台无法还原是哪一级出的问题。

    :param task: 模型任务名，同时作为子目录名（``.`` 会被替换成 ``-``）。
    :param model: 实际产出本次结果的模型名称。
    :param provider: 实际使用的服务商名称。
    :param output_text: 聚合后的可见输出文本。
    :param reasoning_text: 聚合后的推理文本；模型未提供时为空串。
    :param tool_calls: 模型选中的工具调用；工具模式下这就是这一级唯一的产出，
        不记等于整份记录看不出它决定了什么。非工具调用时为 ``None``。
    :param chunk_count: 本次流式响应的增量条数，用于判断是否被中途截断。
    :param first_token_ms: 首字耗时毫秒；一个 chunk 都没拿到时为 ``None``。
    :param total_ms: 从发起请求到本次调用收尾的总毫秒数。
    :param error_type: 失败类型；成功时为空串。
    :param error: 失败消息；成功时为空串。
    :return: 新建记录路径；未启用记录目录或上下文里没有请求时返回 ``None``。
    :raises OSError: 目录创建或文件写入失败。

    副作用：创建 JSON 文件并裁剪该任务目录下超出保留份数的旧记录。
    """
    if _exchange_directory is None:
        return None
    state = _current.get()
    if state is None:
        return None
    internal_request = state.get('internal_request')
    if internal_request is None:
        return None

    payload: Dict[str, Any] = {
        'at': datetime.now().isoformat(timespec='milliseconds'),
        'task': task,
        'stage': internal_request.get('stage'),
        'streamId': internal_request.get('streamId'),
        'turnId': internal_request.get('turnId'),
        'model': {
            'name': model,
            'provider': provider,
            # 候选切换后 candidate 记的是最后一次选中的模型，与上面两项一致；
            # 保留完整候选字段是为了能看出 kind（厂商协议类型）。
            'candidate': deepcopy(state.get('candidate')),
        },
        'timing': {
            'firstTokenMs': first_token_ms,
            'totalMs': total_ms,
        },
        'request': {
            'messages': internal_request.get('messages'),
            'temperature': internal_request.get('temperature'),
            'maxTokens': internal_request.get('maxTokens'),
            'responseFormat': internal_request.get('responseFormat'),
            'renderParams': internal_request.get('renderParams'),
        },
        'response': {
            'text': output_text,
            'reasoning': reasoning_text,
            'toolCalls': deepcopy(tool_calls) if tool_calls else None,
            'chunks': chunk_count,
        },
        # 候选切换过程；成功记录里非空说明这次是换了候选才成的。
        'attempts': deepcopy(state.get('attempts') or []),
        'providerRequest': deepcopy(state.get('provider_request')),
        'error': {'type': error_type, 'message': error} if error_type else None,
    }

    directory = _exchange_directory / _task_directory_name(task)
    directory.mkdir(parents=True, exist_ok=True)
    stream_id = internal_request.get('streamId')
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    name = f'{stamp}_s{stream_id}.json' if stream_id is not None else f'{stamp}.json'
    path = directory / name
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding='utf-8',
    )
    _prune_exchanges(directory)
    return path


def _task_directory_name(task: str) -> str:
    """把模型任务名转成可用作目录名的形式。

    任务名形如 ``chat.conversation.shadow``，点号在 Windows 上虽然合法，但会让
    目录看起来像文件；统一换成连字符，同时挡掉路径分隔符防止越级写入。
    """
    safe = task.strip() or 'unknown'
    for char in ('/', '\\', ':', '.'):
        safe = safe.replace(char, '-')
    return safe


def _prune_exchanges(directory: Path) -> None:
    """裁剪单个任务目录下超出保留份数的旧记录。

    :param directory: 某个任务的记录目录。
    副作用：删除该目录中最早的 JSON 文件。
    :raises OSError: 删除文件失败时传播异常。
    """
    files = sorted(
        directory.glob('*.json'),
        key=lambda item: (item.stat().st_mtime, item.name),
    )
    while len(files) > _exchange_max_files:
        files.pop(0).unlink(missing_ok=True)


def _prune() -> None:
    """删除超过最大保留数量的旧快照文件。

    副作用：可能删除快照目录中最早的 JSON 文件；目录未启用时无操作。
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
    副作用：只读取目录和文件元数据。
    """
    if _directory is None or not _directory.exists():
        return []
    return sorted(_directory.glob('*.json'), key=lambda path: (path.stat().st_mtime, path.name))
