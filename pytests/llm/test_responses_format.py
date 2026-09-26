"""Responses 协议契约：仅使用假 HTTP 传输，覆盖正文、终止事件与错误分类。"""

from typing import Dict, List
import json

import httpx
import pytest

from src.core.llm_models.openai import LlmError, OpenAiChatProvider


def install_stream(monkeypatch, events: List[Dict], captured=None):
    """安装内存 SSE 响应；captured 可检查实际发送的字节。"""
    original = httpx.AsyncClient

    def handler(request):
        if captured is not None:
            captured.append(request)
        body = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events)
        return httpx.Response(200, text=body)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=transport, **kw))


def provider(**kw):
    return OpenAiChatProvider('https://example.invalid/v1', 'fake', 'model',
                              api_format='responses', max_retries=0, **kw)


async def collect(client, messages=None, **kw):
    return [chunk async for chunk in client.stream(messages or [], **kw)]


async def test_responses_request_body(monkeypatch):
    requests = []
    install_stream(monkeypatch, [{'type': 'response.completed'}], requests)
    messages = [
        {'role': 'system', 'content': '系统'},
        {'role': 'user', 'content': [
            {'type': 'text', 'text': '图片和视频'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA==', 'detail': 'low'}},
            {'type': 'video_url', 'video_url': {'url': 'https://example.invalid/a.mp4'}},
        ]},
        {'role': 'assistant', 'content': [{'type': 'text', 'text': '好'}]},
    ]
    tool = {'type': 'function', 'function': {'name': 'send', 'description': '发送', 'parameters': {'type': 'object'}}}
    await collect(provider(), messages, temperature=0.4, max_tokens=80,
                  tools=[tool], response_format={'type': 'json_object'})
    assert str(requests[0].url) == 'https://example.invalid/v1/responses'
    assert json.loads(requests[0].content) == {
        'model': 'model', 'input': [messages[0], {'role': 'user', 'content': [
            {'type': 'input_text', 'text': '图片和视频'},
            {'type': 'input_image', 'image_url': 'data:image/png;base64,AA==', 'detail': 'low'},
            {'type': 'input_video', 'video_url': 'https://example.invalid/a.mp4'},
        ]}, {'role': 'assistant', 'content': [{'type': 'output_text', 'text': '好'}]}],
        'stream': True, 'store': False, 'temperature': 0.4, 'max_output_tokens': 80,
        'tools': [{'type': 'function', **tool['function'], 'strict': False}],
        'text': {'format': {'type': 'json_object'}},
    }


@pytest.mark.parametrize('kind,key', [
    ('response.output_text.delta', 'text'),
    ('response.reasoning_text.delta', 'reasoning'),
    ('response.reasoning_summary_text.delta', 'reasoning'),
])
async def test_responses_deltas(monkeypatch, kind, key):
    install_stream(monkeypatch, [{'type': kind, 'delta': '内容'}, {'type': 'response.completed'}])
    assert await collect(provider()) == [{key: '内容'}]


async def test_responses_tool_call_id(monkeypatch):
    install_stream(monkeypatch, [
        {'type': 'response.output_item.done', 'item': {'type': 'function_call', 'id': 'msg_1', 'call_id': 'call_1', 'name': 'send', 'arguments': '{}'}},
        {'type': 'response.completed'},
    ])
    assert await collect(provider(), tools=[{'type': 'function', 'function': {'name': 'send'}}]) == [
        {'tool_calls': [{'id': 'call_1', 'name': 'send', 'arguments': '{}'}]},
    ]


@pytest.mark.parametrize('event,kind', [
    ({'type': 'response.incomplete', 'response': {'incomplete_details': {'reason': 'content_filter'}}}, 'blocked'),
    ({'type': 'response.incomplete', 'response': {'incomplete_details': {'reason': 'max_output_tokens'}}}, 'format'),
    ({'type': 'response.failed', 'response': {'error': {'code': 'model_not_found', 'message': 'missing'}}}, 'model'),
    ({'type': 'error', 'code': 'invalid_api_key', 'message': 'bad'}, 'auth'),
    ({'code': 'AllocationQuota.FreeTierOnly', 'message': 'empty'}, 'billing'),
    ({'type': 'response.failed', 'response': {'error': {'code': 'server_error', 'message': 'Free quota exhausted. test'}}}, 'billing'),
    ({'type': 'response.refusal.delta', 'delta': '拒绝'}, 'blocked'),
])
async def test_responses_errors(monkeypatch, event, kind):
    install_stream(monkeypatch, [event])
    with pytest.raises(LlmError) as exc:
        await collect(provider())
    assert exc.value.kind == kind


async def test_responses_incomplete_with_text(monkeypatch):
    install_stream(monkeypatch, [
        {'type': 'response.output_text.delta', 'delta': '正文'},
        {'type': 'response.incomplete', 'response': {'incomplete_details': {'reason': 'max_output_tokens'}}},
    ])
    assert await collect(provider()) == [{'text': '正文'}]


async def test_responses_disconnect_retries_reasoning(monkeypatch):
    requests = []
    install_stream(monkeypatch, [{'type': 'response.reasoning_text.delta', 'delta': '思考'}], requests)
    client = OpenAiChatProvider('https://example.invalid', 'fake', 'model', api_format='responses', max_retries=1, retry_interval_ms=0)
    with pytest.raises(LlmError, match='完成事件前断开') as exc:
        await collect(client)
    assert exc.value.kind == 'network'
    assert len(requests) == 2


@pytest.mark.parametrize('extra', [{'store': True}, {'input': []}, {'model': 'other'}, {'stream': False}, {'previous_response_id': 'x'}])
async def test_responses_extra_body_rejected(extra):
    with pytest.raises(LlmError) as exc:
        await collect(provider(extra_body=extra))
    assert exc.value.kind == 'model'


async def test_responses_unknown_block():
    with pytest.raises(ValueError, match='不支持'):
        await collect(provider(), [{'role': 'user', 'content': [{'type': 'unknown'}]}])
