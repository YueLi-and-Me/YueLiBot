"""三种向量线格式使用假 HTTP 响应验证，不访问模型或本机配置。"""

import asyncio
import json
import math

import httpx
import pytest

from src.core.config.schema import ModelCandidate
from src.core.llm_models.openai import LlmError


def candidate(api_format='openai', **kwargs):
    return ModelCandidate(name='测试', provider='test', identifier='embedding', embedding_dim=2,
                          api_format=api_format, base_url='https://example.invalid/api/plan/v3', **kwargs)


def fake_http(monkeypatch, payload):
    requests = []
    original = httpx.AsyncClient

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    return requests


@pytest.mark.parametrize('protocol', ['openai', 'dashscope_multimodal', 'ark_multimodal'])
async def test_embedding_protocol_request_and_normalize(monkeypatch, protocol):
    from src.core.llm_models.embeddings import EmbedInput, request_embeddings
    payload = {'data': [{'index': 0, 'embedding': [3, 4]}]}
    if protocol == 'dashscope_multimodal':
        payload = {'output': {'embeddings': [{'index': 0, 'type': 'fused', 'embedding': [3, 4]}]}}
    elif protocol == 'ark_multimodal':
        payload = {'data': {'embedding': [3, 4]}}
    requests = fake_http(monkeypatch, payload)
    entry = EmbedInput(text='开心', image=None if protocol == 'openai' else b'png', media_type='image/png')
    assert await request_embeddings(candidate(protocol), [entry]) == [[0.6, 0.8]]
    body = json.loads(requests[0].content)
    if protocol == 'openai':
        assert body == {'input': ['开心'], 'model': 'embedding'}
        assert requests[0].url.path == '/api/plan/v3/embeddings'
    elif protocol == 'dashscope_multimodal':
        assert body == {'model': 'embedding', 'input': {'contents': [{'text': '开心', 'image': 'data:image/png;base64,cG5n'}]}, 'parameters': {'dimension': 2}}
        assert requests[0].url.path == '/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding'
    else:
        assert body == {'model': 'embedding', 'input': [{'type': 'text', 'text': '开心'}, {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,cG5n'}}], 'encoding_format': 'float'}
        assert requests[0].url.path == '/api/plan/v3/embeddings/multimodal'


@pytest.mark.parametrize('vectors', [[], [[1, 0], [1, 0]], [[]], [[1]], [[0, 0]], [[math.inf, 1]], [[math.nan, 1]], [['bad', 1]], [[True, 1]]])
async def test_invalid_vectors_reject_whole_batch(monkeypatch, vectors):
    from src.core.llm_models.embeddings import EmbedInput, request_embeddings
    # 非有限 JSON 是供应商错误响应，由自定义序列化保留原样供解析校验。
    original = httpx.AsyncClient
    payload = {'data': [{'index': i, 'embedding': vec} for i, vec in enumerate(vectors)]}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=json.dumps(payload)))
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=transport, **kw))
    with pytest.raises(LlmError) as exc:
        await request_embeddings(candidate(), [EmbedInput(text='文本')])
    assert exc.value.kind == 'format'


@pytest.mark.parametrize('text,image,kind', [('文本', None, 'text'), ('', b'png', 'image'), ('文本', b'png', 'fused')])
async def test_dashscope_checks_each_input_type(monkeypatch, text, image, kind):
    from src.core.llm_models.embeddings import EmbedInput, request_embeddings
    wrong = 'image' if kind != 'image' else 'text'
    fake_http(monkeypatch, {'output': {'embeddings': [{'index': 0, 'type': wrong, 'embedding': [1, 0]}]}})
    with pytest.raises(LlmError, match='2026-03-06') as exc:
        await request_embeddings(candidate('dashscope_multimodal'), [EmbedInput(text, image, 'image/png')])
    assert exc.value.kind == 'format'


@pytest.mark.parametrize('protocol,key', [('dashscope_multimodal', 'model'), ('dashscope_multimodal', 'input'), ('ark_multimodal', 'model'), ('ark_multimodal', 'input'), ('ark_multimodal', 'encoding_format')])
async def test_multimodal_reserved_fields(protocol, key):
    from src.core.llm_models.embeddings import EmbedInput, request_embeddings
    with pytest.raises(LlmError) as exc:
        await request_embeddings(candidate(protocol, extra_body={key: 'override'}), [EmbedInput('文本')])
    assert exc.value.kind == 'model'


async def test_openai_rejects_image():
    from src.core.llm_models.embeddings import EmbedInput, request_embeddings
    with pytest.raises(LlmError) as exc:
        await request_embeddings(candidate(), [EmbedInput(image=b'png', media_type='image/png')])
    assert exc.value.kind == 'model'


async def test_snapshot_omits_image(monkeypatch):
    from src.core.llm_models.embeddings import EmbedInput, request_embeddings
    snapshots = []
    fake_http(monkeypatch, {'data': {'embedding': [1, 0]}})
    monkeypatch.setattr('src.core.llm_models.openai.record_provider_request', lambda url, headers, body, **kw: snapshots.append(body))
    await request_embeddings(candidate('ark_multimodal'), [EmbedInput(image=b'png', media_type='image/png')])
    value = snapshots[0]['input'][0]['image_url']['url']
    assert value == '<data URI 已省略，原始长度 26>'


async def test_ark_concurrency_limit(monkeypatch):
    from src.core.llm_models.embeddings import ARK_CONCURRENCY, EmbedInput, request_embeddings
    current = maximum = 0
    original = httpx.AsyncClient

    async def handler(request):
        nonlocal current, maximum
        current += 1
        maximum = max(maximum, current)
        await asyncio.sleep(0.01)
        current -= 1
        return httpx.Response(200, json={'data': {'embedding': [1, 0]}})

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    result = await request_embeddings(candidate('ark_multimodal'), [EmbedInput(str(i)) for i in range(11)])
    assert len(result) == 11
    assert maximum == ARK_CONCURRENCY == 4


async def test_client_batches_and_keeps_failed_batch_null(monkeypatch):
    from src.core.llm_models.embeddings import EmbedInput, _BATCH
    from src.core.llm_models.router import ModelRouter
    from src.core.memory.embed import EmbeddingClient, _unpack
    calls = []
    original = httpx.AsyncClient

    def handler(request):
        contents = json.loads(request.content)['input']['contents']
        calls.append(contents)
        entries = [{'index': i, 'type': 'text', 'embedding': [3, 4]} for i in range(len(contents))]
        if len(calls) == 1:
            entries[-1]['embedding'] = [0, 0]
        return httpx.Response(200, json={'output': {'embeddings': list(reversed(entries))}})

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    client = EmbeddingClient(ModelRouter('multimodal_embedding', [candidate('dashscope_multimodal')]))
    assert client.accepts_images
    result = await client.embed_inputs([EmbedInput(str(i)) for i in range(_BATCH + 1)])
    assert [len(batch) for batch in calls] == [_BATCH, 1]
    assert result[:_BATCH] == [None] * _BATCH
    assert _unpack(result[-1], 2) == pytest.approx([0.6, 0.8])
