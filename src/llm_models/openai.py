"""
OpenAI 兼容的异步流式对话客户端。直接移植自 src/core/llm/openai.ts。

方舟、DeepSeek、Qwen、月之暗面、智谱、Ollama、OpenAI 本身都提供
`POST {baseUrl}/chat/completions`，所以一个实现打通全部。
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
    def __init__(self, kind: str, message: str, detail: str = '') -> None:
        super().__init__(message)
        self.kind = kind   # auth | quota | model | network | blocked | aborted | unknown
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
    if status in (401, 403): return 'auth'
    if status == 429: return 'quota'
    if status == 404: return 'model'
    if status >= 500: return 'network'
    return 'unknown'


def _classify_code(code: str) -> str:
    if re.search(r'SensitiveContent|Sensitive|Risk|Policy|content_filter', code, re.I): return 'blocked'
    if re.search(r'Quota|RateLimit|TPM|RPM|Throttl|insufficient', code, re.I): return 'quota'
    if re.search(r'Auth|ApiKey|Credential|Permission|invalid_api_key', code, re.I): return 'auth'
    if re.search(r'Model|Endpoint|model_not_found', code, re.I): return 'model'
    return 'unknown'


class _ReasoningTagParser:
    _OPEN = '<think>'
    _CLOSE = '</think>'

    def __init__(self) -> None:
        self._inside = False
        self._buffer = ''

    def push(self, text: str) -> list[dict[str, str]]:
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
        chunks: list[dict[str, str]] = []
        self._append(chunks, self._buffer)
        self._buffer = ''
        return chunks

    def _append(self, chunks: list[dict[str, str]], value: str) -> None:
        if value:
            key = 'reasoning' if self._inside else 'text'
            chunks.append({key: value})

    def _pending_suffix(self, marker: str) -> int:
        maximum = min(len(self._buffer), len(marker) - 1)
        for size in range(maximum, 0, -1):
            if marker.startswith(self._buffer[-size:]):
                return size
        return 0


class OpenAiChatProvider:
    def __init__(self, base_url: str, api_key: str, model: str,
                 headers: dict | None = None, extra_body: dict | None = None,
                 auth_type: Literal['bearer', 'header', 'query', 'none'] = 'bearer',
                 auth_name: str = '',
                 reasoning_parse_mode: ReasoningParseMode = 'field',
                 timeout_ms: int = 120_000, max_retries: int = 2,
                 retry_interval_ms: int = 800) -> None:
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
        """发起流式请求；只在尚未输出内容时重试可恢复错误。

        流已经交给上层后再重放请求会产生重复文本和重复副作用，因此无论错误
        类型如何，一旦 yield 过内容就立即向上抛出。
        """
        for attempt in range(self._max_retries + 1):
            yielded_content = False
            try:
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
    line = line.strip()
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
    result: dict[str, Any] = {}
    if text is not None:
        result['text'] = text
    if reasoning is not None:
        result['reasoning'] = reasoning
    return result


def resolve_base_url(kind: str, base_url: str) -> str:
    """厂商地址：填了就用填的，留空才回落到预设里的官方地址。

    两者都没有说明 kind 是个没见过的名字，直接报出来——静默拼一个空地址
    只会让后面的请求报「连不上 /chat/completions」，那时已经看不出根因了。
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
