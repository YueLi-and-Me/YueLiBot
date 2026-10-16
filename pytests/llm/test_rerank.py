"""重排线格式与任务路由验收：假 HTTP 响应，不访问模型或本机配置。"""

from pathlib import Path

import json
import math

import httpx
import pytest

from pytests.conftest import render_loadable_config
from src.core.config.loader import read_config
from src.core.config.schema import ModelCandidate
from src.core.llm_models.openai import LlmError
from src.core.llm_models.rerank import request_rerank


def _candidate(base_url: str = 'https://example.invalid/v1', **kwargs) -> ModelCandidate:
    return ModelCandidate(name='重排', provider='test', identifier='rerank-model', base_url=base_url, **kwargs)


def _fake_http(monkeypatch, payload) -> list[httpx.Request]:
    requests: list[httpx.Request] = []
    original = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=json.dumps(payload))

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    return requests


async def test_scores_follow_input_order(monkeypatch) -> None:
    """服务端按分数排序返回时，按 index 还原成输入顺序。"""
    requests = _fake_http(monkeypatch, {'results': [
        {'index': 1, 'relevance_score': 0.9}, {'index': 0, 'relevance_score': 0.2},
    ]})

    scores = await request_rerank(_candidate(), 'S16', ['今天天气不错', 'S16 总决赛'])

    assert scores == [0.2, 0.9]
    assert requests[0].url.path == '/v1/rerank'
    assert json.loads(requests[0].content) == {
        'model': 'rerank-model', 'query': 'S16', 'documents': ['今天天气不错', 'S16 总决赛'],
    }


@pytest.mark.parametrize('base_url', [
    'https://example.invalid/compatible-api/v1/reranks',
    'https://example.invalid/v1/rerank/',
])
async def test_full_endpoint_is_used_as_is(monkeypatch, base_url: str) -> None:
    """base_url 已是完整重排端点时不再补路径，兼容路径写法不同的厂商。"""
    requests = _fake_http(monkeypatch, {'results': [{'index': 0, 'relevance_score': 0.5}]})

    await request_rerank(_candidate(base_url), 'q', ['d'])

    assert requests[0].url.path == base_url.split('example.invalid')[1].rstrip('/')


async def test_empty_documents_send_nothing(monkeypatch) -> None:
    requests = _fake_http(monkeypatch, {'results': []})

    assert await request_rerank(_candidate(), 'q', []) == []
    assert requests == []


@pytest.mark.parametrize('results', [
    [{'index': 0, 'relevance_score': 0.5}],
    [{'index': 0, 'relevance_score': 0.5}, {'index': 0, 'relevance_score': 0.4}],
    [{'index': 0, 'relevance_score': 0.5}, {'index': 2, 'relevance_score': 0.4}],
    [{'index': 0, 'relevance_score': math.nan}, {'index': 1, 'relevance_score': 0.4}],
    [{'index': 0, 'relevance_score': True}, {'index': 1, 'relevance_score': 0.4}],
    [{'index': 0}, {'index': 1, 'relevance_score': 0.4}],
])
async def test_malformed_results_raise(monkeypatch, results) -> None:
    """缺条、重号、越界或分数非法时整批报错，不把错位的分数交给检索。"""
    _fake_http(monkeypatch, {'results': results})

    with pytest.raises(LlmError):
        await request_rerank(_candidate(), 'q', ['a', 'b'])


async def test_extra_body_cannot_override_protocol_fields(monkeypatch) -> None:
    requests = _fake_http(monkeypatch, {'results': []})

    with pytest.raises(LlmError, match='documents'):
        await request_rerank(_candidate(extra_body={'documents': []}), 'q', ['a'])
    assert requests == []


def _with_rerank_model(directory: Path, api_format: str) -> None:
    """往模板配置里加一个重排模型，挂到模板里第一个模型所用的厂商上。"""
    models = directory / 'models.toml'
    lines = models.read_text(encoding='utf-8').splitlines()
    start = lines.index(next(line for line in lines if line.startswith('[model_tasks.rerank]')))
    lines[start + 1] = 'model_list = ["测试重排"]'
    provider = next(
        line.split('=', 1)[1].strip().split('#')[0].strip()
        for line in lines if line.startswith('api_provider')
    )
    lines.extend([
        '',
        '[[models]]',
        'name = "测试重排"',
        'model_identifier = "rerank-model"',
        f'api_provider = {provider}',
        f'api_format = "{api_format}"',
    ])
    models.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def test_rerank_task_is_empty_by_default(tmp_path: Path) -> None:
    """全新安装不配重排：回想与查知识照原样排序。"""
    config = read_config(render_loadable_config(tmp_path / 'config'))

    assert config.routing.rerank.ready is False


def test_rerank_task_loads_openai_format(tmp_path: Path) -> None:
    directory = render_loadable_config(tmp_path / 'config')
    _with_rerank_model(directory, 'openai')

    config = read_config(directory)

    assert [candidate.identifier for candidate in config.routing.rerank.candidates] == ['rerank-model']


def test_rerank_task_rejects_responses_format(tmp_path: Path) -> None:
    directory = render_loadable_config(tmp_path / 'config')
    _with_rerank_model(directory, 'responses')

    with pytest.raises(ValueError, match='model_tasks.rerank'):
        read_config(directory)
