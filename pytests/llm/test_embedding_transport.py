"""向量传输与对话共用连接参数，所有请求由内存 HTTP 客户端截获。"""

import json

import httpx
import pytest

from src.core.config.schema import ModelCandidate
from src.core.llm_models.openai import LlmError
from src.core.llm_models.router import ModelRouter
from src.core.memory.embed import EmbeddingClient


@pytest.mark.parametrize('auth,name,key', [('header', 'X-Key', 'fake'), ('query', 'token', 'fake'), ('none', '', '')])
async def test_embedding_transport_options(monkeypatch, auth, name, key):
    requests, options = [], []
    original = httpx.AsyncClient

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [1.0, 0.0]}]})

    def client(**kw):
        options.append(kw)
        return original(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(httpx, 'AsyncClient', client)
    candidate = ModelCandidate(name='文本', provider='test', kind='openai', identifier='embedding',
        auth_type=auth, auth_name=name, api_key=key, default_headers={'X-Test': 'yes'},
        default_query={'region': 'test'}, timeout_ms=17000, embedding_dim=2)
    result = await EmbeddingClient(ModelRouter('embedding', [candidate]))._call(['内容'])
    assert result == [[1.0, 0.0]]
    request = requests[0]
    assert request.url.path == '/v1/embeddings'
    assert request.url.host == 'api.openai.com'
    assert request.url.params['region'] == 'test'
    assert request.headers['X-Test'] == 'yes'
    assert options[0]['timeout'] == 17.0
    assert json.loads(request.content) == {'input': ['内容'], 'model': 'embedding'}
    if auth == 'header':
        assert request.headers[name] == key
    if auth == 'query':
        assert request.url.params[name] == key
    if auth == 'none':
        assert 'Authorization' not in request.headers


async def test_embedding_http_error_classified(monkeypatch):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(503, json={'error': {'code': 'model_not_found', 'message': 'missing'}}))
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=transport, **kw))
    candidate = ModelCandidate(name='文本', provider='test', base_url='https://example.invalid/v1', identifier='embedding')
    with pytest.raises(LlmError) as exc:
        await EmbeddingClient(ModelRouter('embedding', [candidate]))._call(['内容'])
    assert exc.value.kind == 'model'
