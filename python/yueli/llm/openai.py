"""
OpenAI 兼容的异步流式对话客户端。直接移植自 src/core/llm/openai.ts。

方舟、DeepSeek、Qwen、月之暗面、智谱、Ollama、OpenAI 本身都提供
`POST {baseUrl}/chat/completions`，所以一个实现打通全部。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from yueli.common.logger import get_logger

logger = get_logger(__name__)


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


class OpenAiChatProvider:
    def __init__(self, base_url: str, api_key: str, model: str,
                 headers: dict | None = None, extra_body: dict | None = None,
                 timeout_ms: int = 120_000, max_retries: int = 2,
                 retry_interval_ms: int = 800) -> None:
        if not model.strip():
            raise LlmError('model', '未指定模型 ID，请检查 models.toml')
        self.model = model.strip()
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key.strip()
        self._headers = headers or {}
        self._extra_body = extra_body or {}
        self._timeout = timeout_ms / 1000
        self._max_retries = max_retries
        self._retry_interval = retry_interval_ms / 1000

    async def stream(self, messages: list[dict], temperature: float = 0.85,
                     max_tokens: int | None = None,
                     signal: asyncio.Event | None = None) -> AsyncIterator[dict]:
        """发起流式请求；只在尚未输出内容时重试可恢复错误。

        流已经交给上层后再重放请求会产生重复文本和重复副作用，因此无论错误
        类型如何，一旦 yield 过内容就立即向上抛出。
        """
        for attempt in range(self._max_retries + 1):
            yielded_content = False
            try:
                async for chunk in self._stream_once(messages, temperature, max_tokens, signal):
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
        headers = {
            'Content-Type': 'application/json',
            **({'Authorization': f'Bearer {self.api_key}'} if self.api_key else {}),
            **self._headers,
        }
        body: dict[str, Any] = {
            'model': self.model, 'messages': messages, 'stream': True,
            'temperature': temperature,
            **self._extra_body,
        }
        if max_tokens is not None:
            body['max_tokens'] = max_tokens

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                async with client.stream('POST', f'{self.base_url}/chat/completions',
                                          headers=headers, json=body) as resp:
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
                        chunk = _parse_sse_line(line)
                        if chunk == 'done':
                            return
                        if chunk:
                            yield chunk
            except httpx.TimeoutException:
                raise LlmError('network', f'请求超时（{self._timeout}s）')
            except httpx.RequestError as exc:
                raise LlmError('network', f'连不上 {self.base_url}', str(exc))


def _parse_sse_line(line: str) -> dict | str | None:
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
    reasoning = delta.get('reasoning_content') or delta.get('reasoning')
    if text is None and reasoning is None:
        return None
    result: dict[str, Any] = {}
    if text is not None:
        result['text'] = text
    if reasoning is not None:
        result['reasoning'] = reasoning
    return result


def create_chat_provider(config: Any) -> OpenAiChatProvider:
    """从 pydantic Config 对象构造 OpenAI 兼容客户端。"""
    llm = config.llm
    preset = _PRESETS.get(llm.provider, {})
    base_url = llm.base_url or preset.get('base_url', '')
    if not base_url:
        raise LlmError('unknown', f'未知的 llm.provider：{llm.provider}')
    api_key = llm.api_key or ''
    model = llm.model or preset.get('default_model', '')
    if not model:
        raise LlmError('model', '未指定模型 ID，在设置窗口里填 llm.model')

    extra: dict = {}
    if llm.provider == 'ark':
        mode = (llm.thinking or 'disabled').lower()
        if mode in ('enabled', 'auto'):
            extra['thinking'] = {'type': mode}
        else:
            extra['thinking'] = {'type': 'disabled'}

    return OpenAiChatProvider(
        base_url=base_url, api_key=api_key, model=model,
        extra_body=extra if extra else None,
        timeout_ms=llm.timeout_ms,
        max_retries=llm.max_retries,
        retry_interval_ms=llm.retry_interval_ms,
    )


def create_vision_provider(config: Any) -> OpenAiChatProvider:
    """构造视觉客户端；vision.* 留空的字段才复用 llm.*。"""
    llm = config.llm
    vision = config.vision
    preset = _PRESETS.get(llm.provider, {})

    base_url = vision.base_url.strip() or llm.base_url.strip() or preset.get('base_url', '')
    if not base_url:
        raise LlmError('unknown', '视觉模型没有可用的 API 地址，请填写 vision.base_url')
    if vision.base_url.strip() and not vision.model.strip():
        raise LlmError('model', '填写 vision.base_url 时必须同时填写 vision.model')

    model = vision.model.strip() or llm.model.strip() or preset.get('default_model', '')
    if not model:
        raise LlmError('model', '未指定视觉模型，请填写 vision.model')

    api_key = vision.api_key.strip() or llm.api_key.strip()
    return OpenAiChatProvider(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout_ms=vision.timeout_ms,
        max_retries=vision.max_retries,
        retry_interval_ms=vision.retry_interval_ms,
    )
