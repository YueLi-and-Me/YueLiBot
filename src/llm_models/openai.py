"""实现 OpenAI 兼容协议的异步流式对话客户端。

方舟、DeepSeek、Qwen、月之暗面、智谱、Ollama、OpenAI 本身都提供
`POST {baseUrl}/chat/completions`，所以一个实现打通全部。

客户端负责鉴权参数组装、SSE 增量解析、思考字段/标签分流和请求级重试；
在已经向上游产生正文后不重放请求，以避免重复输出和副作用。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Literal
from urllib.parse import urlencode
import asyncio
import json
import re

import httpx

from .snapshot import current_candidate, record_provider_request

from src.common.logger import get_logger

logger = get_logger(__name__)

ReasoningParseMode = Literal['field', 'tag', 'none']


class LlmError(Exception):
    """表示模型请求失败及其可供路由层判断的错误类别。

    :ivar kind: 错误类别，例如 `auth`、`quota`、`network`、`timeout` 或 `aborted`。
    :ivar detail: 可选的原始错误详情，默认值为空字符串。
    """

    def __init__(self, kind: str, message: str, detail: str = '') -> None:
        """创建带分类和原始详情的模型错误。

        :param kind: 供重试和候选切换判断的错误类别。
        :param message: 面向日志或用户的错误消息。
        :param detail: 可选的服务端原始响应片段，默认值为空字符串。
        :side_effects: 初始化异常属性，不执行网络操作。
        """
        super().__init__(message)
        self.kind = kind   # auth | quota | model | network | timeout | blocked | aborted | unknown
        self.detail = detail


_PRESETS: dict[str, dict[str, Any]] = {
    'ark': {'base_url': 'https://ark.cn-beijing.volces.com/api/v3', 'default_model': None},
    'deepseek': {'base_url': 'https://api.deepseek.com/v1', 'default_model': 'deepseek-chat'},
    'dashscope': {'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1', 'default_model': 'qwen-plus'},
    'moonshot': {'base_url': 'https://api.moonshot.cn/v1', 'default_model': 'moonshot-v1-8k'},
    'openai': {'base_url': 'https://api.openai.com/v1', 'default_model': 'gpt-4o-mini'},
    'ollama': {'base_url': 'http://127.0.0.1:11434/v1', 'default_model': None},
}


def _classify_status(status: int) -> str:
    """根据 HTTP 状态码归类模型请求错误。

    :param status: 模型接口返回的 HTTP 状态码。
    :return: `auth`、`quota`、`model`、`network` 或 `unknown` 类别。
    :side_effects: 不访问网络。
    """
    if status in (401, 403): return 'auth'
    if status == 429: return 'quota'
    if status == 404: return 'model'
    if status >= 500: return 'network'
    return 'unknown'


def _classify_code(code: str) -> str:
    """根据服务端错误码文本归类模型请求错误。

    :param code: 服务端返回的 code 或 type 文本。
    :return: `blocked`、`quota`、`auth`、`model` 或 `unknown` 类别。
    :side_effects: 不修改输入文本。
    """
    if re.search(r'SensitiveContent|Sensitive|Risk|Policy|content_filter', code, re.I): return 'blocked'
    if re.search(r'Quota|RateLimit|TPM|RPM|Throttl|insufficient', code, re.I): return 'quota'
    if re.search(r'Auth|ApiKey|Credential|Permission|invalid_api_key', code, re.I): return 'auth'
    if re.search(r'Model|Endpoint|model_not_found', code, re.I): return 'model'
    return 'unknown'


class _ReasoningTagParser:
    """增量拆分 `<think>` 标签中的推理文本和正文。

    标签可能跨越多个网络分片；解析器保留未完成标记的后缀，直到下一次追加或
    `flush` 才决定其归属。
    """
    _OPEN = '<think>'
    _CLOSE = '</think>'

    def __init__(self) -> None:
        """创建处于正文状态的空标签解析器。

        :side_effects: 初始化缓冲区和标签嵌套状态，不执行 I/O。
        """
        self._inside = False
        self._buffer = ''

    def push(self, text: str) -> list[dict[str, str]]:
        """追加文本分片并输出当前可确定的正文/推理块。

        :param text: 新收到的文本分片。
        :return: 每项含 `text` 或 `reasoning` 键的增量块列表。
        :side_effects: 修改内部缓冲区和 `<think>` 状态。
        :performance: 处理量与已消费分片长度线性相关。
        """
        self._buffer += text
        chunks: list[dict[str, str]] = []
        while self._buffer:
            marker = self._CLOSE if self._inside else self._OPEN
            index = self._buffer.find(marker)
            if index >= 0:
                self._append(chunks, self._buffer[:index])
                self._buffer = self._buffer[index + len(marker):]
                self._inside = not self._inside
                continue

            pending = self._pending_suffix(marker)
            ready_length = len(self._buffer) - pending
            if ready_length:
                self._append(chunks, self._buffer[:ready_length])
                self._buffer = self._buffer[ready_length:]
            break
        return chunks

    def flush(self) -> list[dict[str, str]]:
        """结束流并把剩余缓冲区作为当前状态的文本块输出。

        :return: 未完成标签之外的剩余增量块。
        :side_effects: 清空内部缓冲区，不重置实例的 `_inside` 标志。
        """
        chunks: list[dict[str, str]] = []
        self._append(chunks, self._buffer)
        self._buffer = ''
        return chunks

    def _append(self, chunks: list[dict[str, str]], value: str) -> None:
        """按当前标签状态把非空片段追加到输出列表。

        :param chunks: 当前输出块列表。
        :param value: 待追加文本。
        :side_effects: 仅在 `value` 非空时修改 `chunks`。
        """
        if value:
            key = 'reasoning' if self._inside else 'text'
            chunks.append({key: value})

    def _pending_suffix(self, marker: str) -> int:
        """计算可能是未完整标签标记的缓冲区后缀长度。

        :param marker: 当前期待的 `<think>` 或 `</think>` 标记。
        :return: 应保留等待下个分片的后缀字符数。
        :side_effects: 不修改缓冲区。
        """
        maximum = min(len(self._buffer), len(marker) - 1)
        for size in range(maximum, 0, -1):
            if marker.startswith(self._buffer[-size:]):
                return size
        return 0


class OpenAiChatProvider:
    """调用 OpenAI 兼容 `/chat/completions` 端点的异步流式 provider。

    实例保存请求级配置和鉴权方式；同一 provider 的重试仅发生在尚未产出内容时，
    具体的跨厂商切换由上层 `ModelRouter` 负责。
    """

    def __init__(self, base_url: str, api_key: str, model: str,
                 headers: dict | None = None, extra_body: dict | None = None,
                 auth_type: Literal['bearer', 'header', 'query', 'none'] = 'bearer',
                 auth_name: str = '',
                 reasoning_parse_mode: ReasoningParseMode = 'field',
                 timeout_ms: int = 120_000, max_retries: int = 2,
                 retry_interval_ms: int = 800) -> None:
        """创建一个模型接口客户端。

        :param base_url: 兼容接口根地址，不应以 `/` 结尾。
        :param api_key: 鉴权凭据；`none` 鉴权类型可为空。
        :param model: 非空模型 ID。
        :param headers: 每次请求附加的请求头，默认值为 `None`。
        :param extra_body: 每次请求附加的 JSON 字段，默认值为 `None`。
        :param auth_type: `bearer`、`header`、`query` 或 `none`，默认值为 `bearer`。
        :param auth_name: header/query 鉴权使用的字段名，默认值为空字符串。
        :param reasoning_parse_mode: 推理字段解析模式，默认值为 `field`。
        :param timeout_ms: 单次 HTTP 超时毫秒数，默认值为 120000。
        :param max_retries: 尚未产出内容时的内部重试次数，默认值为 2。
        :param retry_interval_ms: 重试间隔毫秒数，默认值为 800。
        :raises LlmError: `model` 为空。
        :side_effects: 保存配置，不在构造阶段建立 HTTP 连接。
        """
        # 连接延迟到首次 stream 调用，允许路由器在启动期先完成候选装配。
        if not model.strip():
            raise LlmError('model', '未指定模型 ID，请检查 models.toml')
        self.model = model.strip()
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key.strip()
        self._auth_type = auth_type
        self._auth_name = auth_name.strip()
        self._headers = headers or {}
        self._extra_body = extra_body or {}
        self._reasoning_parse_mode = reasoning_parse_mode
        self._timeout = timeout_ms / 1000
        self._max_retries = max_retries
        self._retry_interval = retry_interval_ms / 1000

    async def stream(self, messages: list[dict], temperature: float = 0.85,
                     max_tokens: int | None = None,
                     signal: asyncio.Event | None = None,
                     response_format: dict[str, str] | None = None,
                     ) -> AsyncIterator[dict]:
        """发起流式请求，并仅在尚未输出内容时重试可恢复错误。

        流已经交给上层后再重放请求会产生重复文本和重复副作用，因此无论错误
        类型如何，一旦 yield 过内容就立即向上抛出。

        Args:
            messages: OpenAI 兼容消息列表。
            temperature: 采样温度，默认 ``0.85``。
            max_tokens: 可选最大输出 token 数。
            signal: 可选取消事件；重试间隔期间触发时转换为 ``aborted`` 错误。
            response_format: 可选结构化响应格式；当前支持 JSON object 格式。

        Yields:
            解析后的增量字典，顺序与服务端流式响应一致。

        Raises:
            LlmError: 网络、配额、HTTP、协议或主动取消错误；输出产生后不再重试。
            asyncio.CancelledError: 调用方取消异步生成器时传播。

        Side Effects:
            发起一次或多次 HTTP 流式请求，记录请求快照和重试日志；输出后失败不会重放。
        """
        for attempt in range(self._max_retries + 1):
            yielded_content = False
            try:
                # 结构化输出和普通文本共用 HTTP/SSE 解析，仅在请求体中切换 response_format。
                if response_format is None:
                    chunks = self._stream_once(messages, temperature, max_tokens, signal)
                else:
                    chunks = self._stream_once_structured(
                        messages,
                        temperature,
                        max_tokens,
                        signal,
                        response_format,
                    )
                async for chunk in chunks:
                    yielded_content = True
                    yield chunk
                return
            except LlmError as exc:
                retryable = exc.kind in ('network', 'quota')
                # 已产生内容后禁止重放，否则上层会收到重复文本和重复副作用。
                if yielded_content or not retryable or attempt >= self._max_retries:
                    raise
                logger.warning(
                    'llm_request_retry',
                    attempt=attempt + 1,
                    max_retries=self._max_retries,
                    reason=str(exc),
                )
                if self._retry_interval:
                    await asyncio.sleep(self._retry_interval)
                if signal and signal.is_set():
                    raise LlmError('aborted', '生成已中断')

    async def _stream_once(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int | None,
        signal: asyncio.Event | None,
    ) -> AsyncIterator[dict]:
        """以普通文本模式执行一次 HTTP 流式请求。

        :param messages: OpenAI 消息列表。
        :param temperature: 采样温度。
        :param max_tokens: 可选最大输出 token 数。
        :param signal: 可选取消事件。
        :return: 下游 SSE 解析得到的增量字典。
        :raises LlmError: 网络、HTTP、服务端或取消错误。
        :side_effects: 发起一次 HTTP 流式请求。
        """
        async for chunk in self._stream_http(
            messages,
            temperature,
            max_tokens,
            signal,
            None,
        ):
            yield chunk

    async def _stream_once_structured(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int | None,
        signal: asyncio.Event | None,
        response_format: dict[str, str],
    ) -> AsyncIterator[dict]:
        """以 JSON object 响应格式执行一次 HTTP 流式请求。

        :param messages: OpenAI 消息列表。
        :param temperature: 采样温度。
        :param max_tokens: 可选最大输出 token 数。
        :param signal: 可选取消事件。
        :param response_format: 当前只接受 `{'type': 'json_object'}`。
        :return: 下游 SSE 解析得到的增量字典。
        :raises ValueError: response_format 不是支持的 JSON object 结构。
        :raises LlmError: 网络、HTTP、服务端或取消错误。
        :side_effects: 发起一次 HTTP 流式请求。
        """
        if response_format != {'type': 'json_object'}:
            raise ValueError(f'不支持的结构化输出格式：{response_format!r}')
        async for chunk in self._stream_http(
            messages,
            temperature,
            max_tokens,
            signal,
            response_format,
        ):
            yield chunk

    async def _stream_http(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int | None,
        signal: asyncio.Event | None,
        response_format: dict[str, str] | None,
    ) -> AsyncIterator[dict]:
        """组装鉴权请求并解析兼容接口的 SSE 流。

        :param messages: OpenAI 消息列表。
        :param temperature: 采样温度。
        :param max_tokens: 可选最大输出 token 数。
        :param signal: 可选取消事件；设置后中断生成。
        :param response_format: 可选响应格式字段。
        :return: 文本、推理或结束标记前的增量字典。
        :raises LlmError: HTTP 状态错误、服务端错误、超时、网络错误或主动中断。
        :side_effects: 记录脱敏请求快照并建立一次 HTTP 流式连接。
        :performance: 流式消费响应，不缓存完整模型输出。
        """
        # 认证类型只影响 URL/header 组装，其他请求字段保持同一协议结构。
        auth_headers: dict[str, str] = {}
        if self._auth_type == 'bearer':
            auth_headers['Authorization'] = f'Bearer {self.api_key}'
        elif self._auth_type == 'header':
            auth_headers[self._auth_name] = self.api_key
        headers = {
            'Content-Type': 'application/json',
            **auth_headers,
            **self._headers,
        }
        body: dict[str, Any] = {
            'model': self.model, 'messages': messages, 'stream': True,
            'temperature': temperature,
            **self._extra_body,
        }
        if max_tokens is not None:
            body['max_tokens'] = max_tokens
        if response_format is not None:
            body['response_format'] = response_format

        # 记录脱敏请求后再建立连接，保证失败快照包含实际发送的任务参数。
        url = f'{self.base_url}/chat/completions'
        if self._auth_type == 'query':
            url = f'{url}?{urlencode({self._auth_name: self.api_key})}'
        record_provider_request(
            url,
            headers,
            body,
            candidate=current_candidate(),
            secret_header_name=self._auth_name if self._auth_type == 'header' else '',
            secret_query_name=self._auth_name if self._auth_type == 'query' else '',
        )
        tag_parser = (
            _ReasoningTagParser()
            if self._reasoning_parse_mode == 'tag'
            else None
        )

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                async with client.stream('POST', url, headers=headers, json=body) as resp:
                    if resp.status_code != 200:
                        # 错误响应只读取有限正文用于分类和诊断，不把完整响应缓存到内存。
                        text = await resp.aread()
                        body_text = text.decode('utf-8', errors='replace')
                        code = ''
                        msg = ''
                        try:
                            j = json.loads(body_text)
                            error = j.get('error', {})
                            code = error.get('code', '') or error.get('type', '')
                            msg = error.get('message', '')
                        except Exception:
                            pass
                        code_kind = _classify_code(code) if code else 'unknown'
                        kind = (
                            _classify_status(resp.status_code)
                            if code_kind == 'unknown'
                            else code_kind
                        )
                        suffix = f'：{msg}' if msg else ''
                        raise LlmError(kind, f'模型接口返回 HTTP {resp.status_code}{suffix}', body_text[:400])

                    async for line in resp.aiter_lines():
                        if signal and signal.is_set():
                            raise LlmError('aborted', '生成已中断')
                        # SSE 解析器同时识别文本、推理字段和 [DONE] 标记。
                        chunk = _parse_sse_line(line, self._reasoning_parse_mode)
                        if chunk == 'done':
                            if tag_parser is not None:
                                for parsed in tag_parser.flush():
                                    yield parsed
                            return
                        if chunk:
                            if tag_parser is None:
                                yield chunk
                            else:
                                for parsed in tag_parser.push(chunk['text']):
                                    yield parsed
                    if tag_parser is not None:
                        for parsed in tag_parser.flush():
                            yield parsed
            except httpx.TimeoutException:
                raise LlmError('network', f'请求超时（{self._timeout}s）')
            except httpx.RequestError as exc:
                raise LlmError('network', f'连不上 {self.base_url}', str(exc))


def _parse_sse_line(
    line: str,
    reasoning_parse_mode: ReasoningParseMode = 'field',
) -> dict | str | None:
    """解析一行 SSE `data:` 负载。

    :param line: 原始 SSE 文本行。
    :param reasoning_parse_mode: 推理字段解析模式，默认值为 `field`。
    :return: 增量字典、字符串 `done` 或不可处理行对应的 `None`。
    :raises LlmError: 负载包含模型服务端错误。
    :side_effects: 不修改输入行或 provider 状态。
    """
    line = line.strip()
    # 注释行、空行和非 data 行不产生模型事件，保持 SSE 心跳透明。
    if not line or line.startswith(':') or not line.startswith('data:'):
        return None
    payload = line[5:].strip()
    if payload == '[DONE]':
        return 'done'
    try:
        j = json.loads(payload)
    except Exception:
        return None
    if j.get('error'):
        # 服务端错误必须转换为统一 LlmError，路由器才能按类别决定是否重试。
        code = j['error'].get('code', '') or j['error'].get('type', '')
        msg = j['error'].get('message', '未知')
        raise LlmError(_classify_code(str(code)), f'模型返回错误：{msg}')
    delta = (j.get('choices') or [{}])[0].get('delta', {})
    if not delta:
        return None
    text = delta.get('content')
    reasoning = None
    if reasoning_parse_mode == 'field':
        reasoning = delta.get('reasoning_content')
        if reasoning is None:
            reasoning = delta.get('reasoning')
    if text is None and reasoning is None:
        return None
    # 只输出实际存在的增量字段，避免下游将空字段当作有效正文。
    result: dict[str, Any] = {}
    if text is not None:
        result['text'] = text
    if reasoning is not None:
        result['reasoning'] = reasoning
    return result


def resolve_base_url(kind: str, base_url: str) -> str:
    """解析模型提供者的请求基地址，并在配置不完整时立即报告原因。

    Args:
        kind: 提供者类型名称，用于查找内置基地址。
        base_url: 配置中的显式基地址；去除首尾空白后非空时优先使用。

    Returns:
        可用于拼接模型接口路径的非空基地址。

    Raises:
        LlmError: ``base_url`` 为空且 ``kind`` 没有对应内置地址。

    Side Effects:
        不执行网络请求，不修改提供者配置。
    """
    explicit = base_url.strip()
    if explicit:
        return explicit
    preset_url = _PRESETS.get(kind.strip().lower(), {}).get('base_url', '')
    if not preset_url:
        raise LlmError(
            'unknown',
            f'厂商 kind={kind} 没有内置官方地址，请在 providers.toml 里填 base_url',
        )
    return preset_url
