"""固定基线实际请求字节，保护 Chat 字段顺序及 extra_body 的覆盖语义。"""

import httpx

from src.core.llm_models.openai import OpenAiChatProvider


async def test_chat_request_bytes_unchanged(monkeypatch):
    original = httpx.AsyncClient
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, text='data: [DONE]\n\n')

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=transport, **kw))
    client = OpenAiChatProvider(
        'https://example.invalid/v1', 'fake', 'original',
        extra_body={'model': 'override', 'temperature': 0.7, 'max_tokens': 99, 'custom': True},
    )
    chunks = [chunk async for chunk in client.stream(
        [{'role': 'user', 'content': 'hello'}], temperature=0.3, max_tokens=80,
        response_format={'type': 'json_object'},
        tools=[{'type': 'function', 'function': {'name': 'send'}}],
    )]
    assert chunks == []
    assert requests[0].content == (
        b'{"model":"override","messages":[{"role":"user","content":"hello"}],'
        b'"stream":true,"temperature":0.7,"max_tokens":80,"custom":true,'
        b'"response_format":{"type":"json_object"},'
        b'"tools":[{"type":"function","function":{"name":"send"}}]}'
    )
