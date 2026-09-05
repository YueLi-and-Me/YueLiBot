"""免费额度耗尽必须立即上报，不能在重试等待中被首字超时覆盖。"""

from typing import Any, Dict, List
import json

import httpx
import pytest

from src.core.config.schema import ModelCandidate
from src.core.llm_models import openai as openai_module
from src.core.llm_models import router as router_module
from src.core.llm_models.openai import LlmError, OpenAiChatProvider, error_hint
from src.core.llm_models.router import ModelRouter


_ERROR = {
    'code': 'AllocationQuota.FreeTierOnly',
    'message': 'Free quota exhausted. Disable the use free tier only mode.',
}


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    """保留真实 HTTP/SSE 解析，只替换传输层以避免访问外部服务。"""
    client_class = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        openai_module.httpx, 'AsyncClient',
        lambda **kwargs: client_class(transport=transport, **kwargs),
    )


@pytest.mark.parametrize('structured', [False, True])
@pytest.mark.parametrize('envelope', ['flat', 'nested', 'sse'])
async def test_free_quota_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, structured: bool, envelope: str,
) -> None:
    requests: List[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if envelope == 'sse':
            return httpx.Response(200, text='data: ' + json.dumps({'error': _ERROR}) + '\n\n')
        return httpx.Response(403, json=_ERROR if envelope == 'flat' else {'error': _ERROR})

    _install_transport(monkeypatch, respond)
    provider = OpenAiChatProvider(
        base_url='https://primary.example/v1', api_key='', model='test',
        max_retries=2, retry_interval_ms=0,
    )
    with pytest.raises(LlmError) as caught:
        _ = [chunk async for chunk in provider.stream(
            [], response_format={'type': 'json_object'} if structured else None,
        )]
    assert caught.value.kind == 'billing'
    assert 'Free quota exhausted' in str(caught.value)
    assert len(requests) == 1
    assert '免费额度' in error_hint(caught.value.kind)


@pytest.mark.parametrize('code', ['RateLimit', 'Throttling.RateQuota', ''])
async def test_temporary_rate_limit_still_retries(
    monkeypatch: pytest.MonkeyPatch, code: str,
) -> None:
    requests: List[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(429, json={'error': {'code': code, 'message': 'Too many requests'}})

    _install_transport(monkeypatch, respond)
    provider = OpenAiChatProvider(
        base_url='https://primary.example/v1', api_key='', model='test',
        max_retries=2, retry_interval_ms=0,
    )
    with pytest.raises(LlmError) as caught:
        _ = [chunk async for chunk in provider.stream([])]
    assert caught.value.kind == 'quota'
    assert len(requests) == 3


@pytest.mark.parametrize('has_backup', [False, True])
async def test_router_preserves_free_quota_failure_before_deadline(
    monkeypatch: pytest.MonkeyPatch, has_backup: bool,
) -> None:
    """重试间隔长于首字窗口时，也必须保留账务错误并立即尝试备用。"""
    requests: List[str] = []
    attempts: List[Dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.host)
        if request.url.host == 'primary.example':
            return httpx.Response(403, json={'error': _ERROR})
        delta = {'choices': [{'delta': {'content': '{"ok":true}'}}]}
        return httpx.Response(200, text='data: ' + json.dumps(delta) + '\n\ndata: [DONE]\n\n')

    _install_transport(monkeypatch, respond)
    monkeypatch.setattr(router_module, 'record_attempt', lambda **kwargs: attempts.append(kwargs))
    names = ['primary', 'backup'] if has_backup else ['primary']
    candidates = [ModelCandidate(
        name=name, provider=name, kind='openai', identifier='test',
        base_url=f'https://{name}.example/v1', api_key='',
        max_retries=2, retry_interval_ms=1_000,
    ) for name in names]
    router = ModelRouter('schedule', candidates, first_token_timeout_ms=100)
    stream = router.stream([], response_format={'type': 'json_object'})
    if has_backup:
        assert [chunk async for chunk in stream] == [{'text': '{"ok":true}'}]
    else:
        with pytest.raises(LlmError) as caught:
            _ = [chunk async for chunk in stream]
        assert caught.value.kind == 'billing'
        assert 'AllocationQuota.FreeTierOnly' in str(caught.value)
    assert requests == [f'{name}.example' for name in names]
    assert len(attempts) == 1
    assert attempts[0]['error_kind'] == 'billing'
    assert 'AllocationQuota.FreeTierOnly' in attempts[0]['message']
