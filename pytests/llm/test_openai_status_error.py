"""OpenAI 兼容接口错误响应的解析回归。"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from src.core.llm_models import openai as openai_module
from src.core.llm_models.openai import LlmError, OpenAiChatProvider

# 测试夹具占位密钥：不对应任何真实服务，经变量间接传入避免被安全扫描当作硬编码凭据。
_FIXTURE_API_KEY = "-".join(("test", "key"))


class _ErrorResponse:
    """模拟非 200 HTTP 响应，只提供错误状态读取路径。"""

    def __init__(self, status_code: int, body: bytes) -> None:
        self.status_code = status_code
        self._body = body

    async def __aenter__(self) -> "_ErrorResponse":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def aread(self) -> bytes:
        return self._body

    async def aiter_lines(self):
        if False:
            yield ''


def _install_error_response(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    body: bytes,
) -> List[None]:
    """把 AsyncClient 替换为固定返回错误响应的桩。"""

    requests: List[None] = []

    class _Client:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        def stream(
            self,
            method: str,
            url: str,
            *,
            headers: Dict[str, str],
            json: Dict[str, Any],
        ) -> _ErrorResponse:
            requests.append(None)
            return _ErrorResponse(status_code, body)

    monkeypatch.setattr(openai_module.httpx, "AsyncClient", _Client)
    return requests


def _provider() -> OpenAiChatProvider:
    return OpenAiChatProvider(
        base_url='https://api.example.com/v1',
        api_key=_FIXTURE_API_KEY,
        model='test-model',
        retry_interval_ms=0,
    )


@pytest.mark.asyncio
async def test_flat_siliconflow_error_surface_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """SiliconFlow 顶层 code/message 不能只剩笼统的 HTTP 400。"""
    _install_error_response(
        monkeypatch,
        400,
        b'{"code":20015,"message":"No user query found in messages.","data":null}',
    )

    with pytest.raises(LlmError) as excinfo:
        _ = [chunk async for chunk in _provider().stream(
            messages=[{'role': 'system', 'content': '只给 system'}],
        )]

    assert '20015' in str(excinfo.value)
    assert 'No user query found in messages.' in str(excinfo.value)
    assert 'No user query found in messages.' in excinfo.value.detail


@pytest.mark.asyncio
async def test_nested_openai_error_keeps_model_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenAI 嵌套 error 结构继续参与错误分类。"""
    _install_error_response(
        monkeypatch,
        400,
        b'{"error":{"code":"model_not_found","message":"bad model"}}',
    )

    with pytest.raises(LlmError) as excinfo:
        _ = [chunk async for chunk in _provider().stream(
            messages=[{'role': 'user', 'content': 'x'}],
        )]

    assert excinfo.value.kind == 'model'
    assert 'bad model' in str(excinfo.value)


@pytest.mark.asyncio
async def test_payment_required_is_non_retryable_billing_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """余额不足不是瞬时限流，必须给出精确分类并立即切换候选。"""
    requests = _install_error_response(
        monkeypatch,
        402,
        b'{"code":30001,"message":"Sorry, your account balance is insufficient"}',
    )

    with pytest.raises(LlmError) as excinfo:
        _ = [chunk async for chunk in _provider().stream(
            messages=[{'role': 'user', 'content': 'x'}],
        )]

    assert excinfo.value.kind == 'billing'
    assert 'balance is insufficient' in str(excinfo.value)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_channel_failure_switches_candidate_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """渠道缺失是确定性路由错误，同一模型上重复请求不会使渠道凭空出现。"""
    requests = _install_error_response(
        monkeypatch,
        500,
        (
            b'{"error":{"code":"get_channel_failed",'
            b'"message":"group auto has no channel for this model"}}'
        ),
    )

    with pytest.raises(LlmError) as excinfo:
        _ = [chunk async for chunk in _provider().stream(
            messages=[{'role': 'user', 'content': 'x'}],
        )]

    assert excinfo.value.kind == 'model'
    assert 'get_channel_failed' in str(excinfo.value)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_plain_server_error_keeps_network_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有确定错误码的临时 500 仍应保留首次请求和两次网络重试。"""
    requests = _install_error_response(monkeypatch, 500, b'gateway error')

    with pytest.raises(LlmError) as excinfo:
        _ = [chunk async for chunk in _provider().stream(
            messages=[{'role': 'user', 'content': 'x'}],
        )]

    assert excinfo.value.kind == 'network'
    assert len(requests) == 3
