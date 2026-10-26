"""协议层取用量：Chat、Responses、三种向量协议与重排，以及语音合成不按 token 计量。

全部用假 HTTP 传输驱动真实 provider，再经路由层落账，断言用量小时表里的结果；
不访问任何服务商，不读 config/。
"""

from __future__ import annotations

from typing import Any, Dict, List

import json

import httpx
import pytest

from src.core.config.schema import ModelCandidate
from src.core.llm_models.embeddings import EmbedInput, request_embeddings
from src.core.llm_models.openai import LlmError, OpenAiChatProvider
from src.core.llm_models.rerank import request_rerank
from src.core.llm_models.router import ModelRouter


def _chat_candidate(api_format: str = 'openai', **kwargs: Any) -> ModelCandidate:
    return ModelCandidate(
        name='对话模型', provider='厂商甲', kind='openai', base_url='https://example.invalid/v1',
        api_key='sk-fake', identifier='chat-model', api_format=api_format, **kwargs,
    )


def _install_sse(monkeypatch: pytest.MonkeyPatch, events: List[Any], captured: list | None = None) -> None:
    """安装内存 SSE 响应；事件为字典时序列化成 data 行，字符串原样写出。"""
    original = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        body = ''.join(
            f'data: {event if isinstance(event, str) else json.dumps(event)}\n\n' for event in events
        )
        return httpx.Response(200, text=body)

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))


def _install_json(monkeypatch: pytest.MonkeyPatch, payload: Dict[str, Any]) -> None:
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=json.dumps(payload)))
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=transport, **kw))


async def _collect(router: ModelRouter, **kwargs: Any) -> List[dict]:
    return [chunk async for chunk in router.stream([{'role': 'user', 'content': '在吗'}], **kwargs)]


def _delta(text: str, usage: Dict[str, int] | None = None) -> Dict[str, Any]:
    event: Dict[str, Any] = {'choices': [{'delta': {'content': text}}]}
    if usage is not None:
        event['usage'] = usage
    return event


_USAGE = {
    'prompt_tokens': 120, 'completion_tokens': 30, 'total_tokens': 150,
    'prompt_tokens_details': {'cached_tokens': 100},
    'completion_tokens_details': {'reasoning_tokens': 12},
}


# ---------------------------------------------------------------- B-1 Chat 取数


async def test_chat_usage_from_trailing_chunk_with_empty_choices(monkeypatch, usage_rows) -> None:
    """规范形状：末块 choices 为空、顶层带 usage；调用方收不到用量块。"""
    _install_sse(monkeypatch, [_delta('我'), _delta('在'), {'choices': [], 'usage': _USAGE}, '[DONE]'])
    chunks = await _collect(ModelRouter('chat', [_chat_candidate()]))

    assert chunks == [{'text': '我'}, {'text': '在'}]
    [row] = usage_rows()
    assert (row['calls'], row['unreported_calls'], row['failed_calls']) == (1, 0, 0)
    assert (row['prompt_tokens'], row['completion_tokens'], row['total_tokens']) == (120, 30, 150)
    assert (row['cached_tokens'], row['reasoning_tokens']) == (100, 12)
    assert (row['task'], row['model'], row['provider']) == ('chat', '对话模型', '厂商甲')


async def test_chat_usage_inside_first_choice(monkeypatch, usage_rows) -> None:
    """部分网关把用量放在 choices[0].usage 里。"""
    _install_sse(monkeypatch, [
        _delta('好'),
        {'choices': [{'delta': {}, 'finish_reason': 'stop', 'usage': {'prompt_tokens': 7, 'completion_tokens': 3}}]},
        '[DONE]',
    ])
    assert await _collect(ModelRouter('chat', [_chat_candidate()])) == [{'text': '好'}]

    [row] = usage_rows()
    # 服务商没给 total_tokens 时取前两项之和。
    assert (row['prompt_tokens'], row['completion_tokens'], row['total_tokens']) == (7, 3, 10)


async def test_chat_cumulative_usage_on_every_chunk_takes_the_last(monkeypatch, usage_rows) -> None:
    """有的网关每块都带累计值，以最后一次为准，不逐块相加。"""
    _install_sse(monkeypatch, [
        _delta('一', {'prompt_tokens': 50, 'completion_tokens': 1, 'total_tokens': 51}),
        _delta('二', {'prompt_tokens': 50, 'completion_tokens': 2, 'total_tokens': 52}),
        _delta('三', {'prompt_tokens': 50, 'completion_tokens': 3, 'total_tokens': 53}),
        '[DONE]',
    ])
    assert await _collect(ModelRouter('chat', [_chat_candidate()])) == [
        {'text': '一'}, {'text': '二'}, {'text': '三'},
    ]
    [row] = usage_rows()
    assert (row['prompt_tokens'], row['completion_tokens'], row['total_tokens']) == (50, 3, 53)


async def test_chat_without_usage_counts_as_unreported(monkeypatch, usage_rows) -> None:
    """完全没有用量：计入未返回次数，token 与费用都是 0，不估算。"""
    _install_sse(monkeypatch, [_delta('嗯'), {'choices': [], 'usage': None}, '[DONE]'])
    assert await _collect(ModelRouter('chat', [_chat_candidate(price_in=2.0, price_out=8.0)])) == [{'text': '嗯'}]

    [row] = usage_rows()
    assert (row['calls'], row['unreported_calls']) == (1, 1)
    assert (row['prompt_tokens'], row['completion_tokens'], row['cost']) == (0, 0, 0.0)


async def test_chat_total_only_usage_counts_as_unreported(monkeypatch, usage_rows) -> None:
    """对话类只给 total_tokens 时分不清输入与输出，按没返回处理，不推测拆分。"""
    _install_sse(monkeypatch, [_delta('嗯'), {'choices': [], 'usage': {'total_tokens': 50}}, '[DONE]'])
    assert await _collect(ModelRouter('chat', [_chat_candidate()])) == [{'text': '嗯'}]

    [row] = usage_rows()
    assert (row['calls'], row['unreported_calls'], row['total_tokens']) == (1, 1, 0)


# ---------------------------------------------------------------- B-2 请求体


async def test_chat_body_requests_usage_by_default(monkeypatch) -> None:
    requests: list = []
    _install_sse(monkeypatch, ['[DONE]'], requests)
    client = OpenAiChatProvider('https://example.invalid/v1', 'fake', 'model')
    assert [chunk async for chunk in client.stream([{'role': 'user', 'content': 'hi'}])] == []

    body = json.loads(requests[0].content)
    assert body == {
        'model': 'model', 'messages': [{'role': 'user', 'content': 'hi'}], 'stream': True,
        'stream_options': {'include_usage': True}, 'temperature': 0.85,
    }
    assert list(body) == ['model', 'messages', 'stream', 'stream_options', 'temperature']


async def test_chat_body_keeps_user_stream_options_from_extra_body(monkeypatch) -> None:
    requests: list = []
    _install_sse(monkeypatch, ['[DONE]'], requests)
    client = OpenAiChatProvider(
        'https://example.invalid/v1', 'fake', 'model',
        extra_body={'stream_options': {'include_usage': False, 'chunk_size': 8}},
    )
    assert [chunk async for chunk in client.stream([{'role': 'user', 'content': 'hi'}])] == []

    assert json.loads(requests[0].content)['stream_options'] == {'include_usage': False, 'chunk_size': 8}


async def test_responses_body_has_no_stream_options(monkeypatch) -> None:
    requests: list = []
    _install_sse(monkeypatch, [{'type': 'response.completed', 'response': {}}], requests)
    client = OpenAiChatProvider('https://example.invalid/v1', 'fake', 'model', api_format='responses')
    assert [chunk async for chunk in client.stream([{'role': 'user', 'content': 'hi'}])] == []

    body = json.loads(requests[0].content)
    assert 'stream_options' not in body
    assert body == {
        'model': 'model', 'input': [{'role': 'user', 'content': 'hi'}],
        'stream': True, 'store': False, 'temperature': 0.85,
    }


# ---------------------------------------------------------------- B-3 Responses


async def test_responses_usage_from_completed_event(monkeypatch, usage_rows) -> None:
    _install_sse(monkeypatch, [
        {'type': 'response.reasoning_text.delta', 'delta': '想想'},
        {'type': 'response.output_text.delta', 'delta': '好的'},
        {'type': 'response.completed', 'response': {'usage': {
            'input_tokens': 900, 'output_tokens': 60, 'total_tokens': 960,
            'input_tokens_details': {'cached_tokens': 512},
            'output_tokens_details': {'reasoning_tokens': 40},
        }}},
    ])
    chunks = await _collect(ModelRouter('chat', [_chat_candidate('responses')]))

    assert chunks == [{'reasoning': '想想'}, {'text': '好的'}]
    [row] = usage_rows()
    assert (row['prompt_tokens'], row['completion_tokens'], row['total_tokens']) == (900, 60, 960)
    assert (row['cached_tokens'], row['reasoning_tokens']) == (512, 40)
    assert row['unreported_calls'] == 0


async def test_responses_incomplete_with_text_still_records_usage(monkeypatch, usage_rows) -> None:
    """输出上限截断但已有正文时正常结束，用量照记。"""
    _install_sse(monkeypatch, [
        {'type': 'response.output_text.delta', 'delta': '说到一半'},
        {'type': 'response.incomplete', 'response': {
            'incomplete_details': {'reason': 'max_output_tokens'},
            'usage': {'input_tokens': 300, 'output_tokens': 80},
        }},
    ])
    assert await _collect(ModelRouter('chat', [_chat_candidate('responses')])) == [{'text': '说到一半'}]

    [row] = usage_rows()
    assert (row['calls'], row['failed_calls']) == (1, 0)
    assert (row['prompt_tokens'], row['completion_tokens'], row['total_tokens']) == (300, 80, 380)


async def test_responses_reasoning_exhausted_still_records_usage(monkeypatch, usage_rows) -> None:
    """思考耗尽输出上限、没有正文：调用失败，但服务商已结算的用量与费用照记。"""
    _install_sse(monkeypatch, [
        {'type': 'response.reasoning_text.delta', 'delta': '想了很久'},
        {'type': 'response.incomplete', 'response': {
            'incomplete_details': {'reason': 'max_output_tokens'},
            'usage': {'input_tokens': 200, 'output_tokens': 4096,
                      'output_tokens_details': {'reasoning_tokens': 4096}},
        }},
    ])
    router = ModelRouter('chat', [_chat_candidate('responses', price_in=1.0, price_out=4.0)])

    with pytest.raises(LlmError) as raised:
        await _collect(router)

    assert raised.value.kind == 'format'
    [row] = usage_rows()
    assert (row['calls'], row['failed_calls'], row['unreported_calls']) == (1, 1, 0)
    assert (row['prompt_tokens'], row['completion_tokens'], row['reasoning_tokens']) == (200, 4096, 4096)
    assert row['cost'] == pytest.approx((200 * 1.0 + 4096 * 4.0) / 1_000_000)


async def test_responses_content_filter_still_records_usage(monkeypatch, usage_rows) -> None:
    """输出被内容策略截断：按内容拦截失败，已结算的用量照记。"""
    _install_sse(monkeypatch, [
        {'type': 'response.incomplete', 'response': {
            'incomplete_details': {'reason': 'content_filter'},
            'usage': {'input_tokens': 150, 'output_tokens': 20},
        }},
    ])

    with pytest.raises(LlmError) as raised:
        await _collect(ModelRouter('chat', [_chat_candidate('responses')]))

    assert raised.value.kind == 'blocked'
    [row] = usage_rows()
    assert (row['failed_calls'], row['prompt_tokens'], row['completion_tokens']) == (1, 150, 20)


async def test_responses_failed_event_still_records_usage(monkeypatch, usage_rows) -> None:
    """失败事件带着用量时同样照记。"""
    _install_sse(monkeypatch, [
        {'type': 'response.failed', 'response': {
            'error': {'code': 'server_error', 'message': '上游出错'},
            'usage': {'input_tokens': 80, 'output_tokens': 5},
        }},
    ])

    with pytest.raises(LlmError):
        await _collect(ModelRouter('chat', [_chat_candidate('responses')]))

    [row] = usage_rows()
    assert (row['failed_calls'], row['prompt_tokens'], row['completion_tokens']) == (1, 80, 5)


# ---------------------------------------------------------------- B-5 非流式


def _embedding_candidate(api_format: str) -> ModelCandidate:
    return ModelCandidate(
        name='向量模型', provider='厂商乙', identifier='embedding', embedding_dim=2,
        api_format=api_format, base_url='https://example.invalid/api/plan/v3',
    )


@pytest.mark.parametrize('protocol,payload', [
    ('openai', {'data': [{'index': 0, 'embedding': [3, 4]}],
                'usage': {'prompt_tokens': 6, 'total_tokens': 6}}),
    ('dashscope_multimodal', {'output': {'embeddings': [{'index': 0, 'type': 'fused', 'embedding': [3, 4]}]},
                              'usage': {'input_tokens': 9}}),
    ('ark_multimodal', {'data': {'embedding': [3, 4]},
                        'usage': {'prompt_tokens': 11, 'total_tokens': 11}}),
])
async def test_embedding_protocols_report_usage(monkeypatch, usage_rows, protocol, payload) -> None:
    _install_json(monkeypatch, payload)
    entry = EmbedInput(text='开心', image=None if protocol == 'openai' else b'png', media_type='image/png')
    router = ModelRouter('embedding', [_embedding_candidate(protocol)])

    assert await router.run(lambda candidate: request_embeddings(candidate, [entry])) == [[0.6, 0.8]]

    [row] = usage_rows()
    expected = {'openai': 6, 'dashscope_multimodal': 9, 'ark_multimodal': 11}[protocol]
    assert (row['task'], row['calls'], row['unreported_calls']) == ('embedding', 1, 0)
    assert (row['prompt_tokens'], row['completion_tokens'], row['total_tokens']) == (expected, 0, expected)


async def test_ark_requests_within_one_attempt_are_summed(monkeypatch, usage_rows) -> None:
    """方舟按条请求：一次尝试内的多次上报累加进同一笔账。"""
    _install_json(monkeypatch, {'data': {'embedding': [3, 4]}, 'usage': {'prompt_tokens': 5}})
    entries = [EmbedInput(text=f'第{i}条', image=b'png', media_type='image/png') for i in range(3)]
    router = ModelRouter('multimodal_embedding', [_embedding_candidate('ark_multimodal')])

    await router.run(lambda candidate: request_embeddings(candidate, entries))

    [row] = usage_rows()
    assert (row['calls'], row['prompt_tokens'], row['total_tokens']) == (1, 15, 15)


async def test_rerank_reports_usage(monkeypatch, usage_rows) -> None:
    _install_json(monkeypatch, {
        'results': [{'index': 0, 'relevance_score': 0.3}, {'index': 1, 'relevance_score': 0.8}],
        'usage': {'prompt_tokens': 42, 'total_tokens': 42},
    })
    candidate = ModelCandidate(name='重排模型', provider='厂商丙', identifier='rerank', base_url='https://example.invalid/v1')
    router = ModelRouter('rerank', [candidate])

    assert await router.run(lambda item: request_rerank(item, 'q', ['a', 'b'])) == [0.3, 0.8]

    [row] = usage_rows()
    assert (row['task'], row['prompt_tokens'], row['unreported_calls']) == ('rerank', 42, 0)


async def test_rerank_total_only_usage_counts_as_input(monkeypatch, usage_rows) -> None:
    """重排用量只有 total_tokens：重排没有输出，总数全部按输入记并按输入价算费。"""
    _install_json(monkeypatch, {
        'results': [{'index': 0, 'relevance_score': 0.9}],
        'usage': {'total_tokens': 37},
    })
    candidate = ModelCandidate(
        name='重排模型', provider='厂商丙', identifier='rerank', base_url='https://example.invalid/v1',
        price_in=2.0,
    )

    await ModelRouter('rerank', [candidate]).run(lambda item: request_rerank(item, 'q', ['a']))

    [row] = usage_rows()
    assert (row['unreported_calls'], row['prompt_tokens'], row['completion_tokens'], row['total_tokens']) == (0, 37, 0, 37)
    assert row['cost'] == pytest.approx(37 * 2.0 / 1_000_000)


async def test_rerank_without_usage_counts_as_unreported(monkeypatch, usage_rows) -> None:
    _install_json(monkeypatch, {'results': [{'index': 0, 'relevance_score': 0.5}]})
    candidate = ModelCandidate(name='重排模型', provider='厂商丙', identifier='rerank', base_url='https://example.invalid/v1')

    await ModelRouter('rerank', [candidate]).run(lambda item: request_rerank(item, 'q', ['a']))

    [row] = usage_rows()
    assert (row['calls'], row['unreported_calls'], row['total_tokens']) == (1, 1, 0)


async def test_tts_counts_calls_and_duration_without_tokens(usage_rows) -> None:
    """语音合成不按 token 计量：只记次数与耗时，不算服务商没返回用量。"""
    candidate = ModelCandidate(name='语音模型', provider='厂商丁', identifier='tts')

    async def synthesize(_candidate: ModelCandidate) -> bytes:
        return b'audio'

    assert await ModelRouter('tts', [candidate]).run(synthesize) == b'audio'

    [row] = usage_rows()
    assert (row['task'], row['calls'], row['unreported_calls'], row['failed_calls']) == ('tts', 1, 0, 0)
    assert (row['prompt_tokens'], row['completion_tokens'], row['total_tokens'], row['cost']) == (0, 0, 0, 0.0)
    assert row['duration_ms_sum'] >= 0
