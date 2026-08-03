"""
TraceBuffer 基本行为 + 隐私红线（trace 里不许出现窗口标题原文）+
chat.send() 走一遍假 provider，断言 user_input/llm_request/llm_chunk/llm_final
都出现且 turnId 对得上。
"""

from __future__ import annotations

import asyncio
import json

from yueli.config.schema import Config
from yueli.services.chat import ChatService
from yueli.services.proactive import AwarenessService
from yueli.services.trace import trace


class _FakeProvider:
    model = 'fake-model'

    def __init__(self, chunks: list[dict]) -> None:
        self._chunks = chunks

    async def stream(self, messages, temperature=0.85, max_tokens=None, signal=None):
        for chunk in self._chunks:
            yield chunk


class _FailingProvider:
    model = 'fake-model'

    async def stream(self, messages, temperature=0.85, max_tokens=None, signal=None):
        raise RuntimeError('模拟网络故障')
        yield  # pragma: no cover — 让这个方法仍然是一个 async generator


async def _noop_push(channel, payload):
    return None


def test_buffer_seq_and_since_filters():
    trace.clear()
    e1 = trace.emit('a', x=1)
    e2 = trace.emit('b', x=2)
    assert e1['seq'] == 1 and e2['seq'] == 2
    assert trace.since(0) == [e1, e2]
    assert trace.since(1) == [e2]
    assert trace.since(2) == []


async def test_foreground_trace_never_contains_title(db):
    trace.clear()
    chat = ChatService(db=db, provider=None, push_event=_noop_push)
    svc = AwarenessService(chat=chat, schedule=None, cfg=Config())

    sensitive = '工资表_2026Q3_客户名单.xlsx'
    svc.on_foreground({'process': 'EXCEL.EXE', 'title': sensitive, 'fullscreen': False})
    await asyncio.sleep(0.02)   # 让 on_foreground 里起的后台 task 跑完，避免残留 pending task 警告

    entries = trace.since(0)
    dumped = json.dumps(entries, ensure_ascii=False)
    assert '工资' not in dumped
    assert '客户名单' not in dumped
    assert any(e['kind'] == 'foreground' for e in entries)


async def test_chat_send_emits_full_trace(db):
    trace.clear()
    provider = _FakeProvider([{'text': '你好'}, {'text': '呀', 'reasoning': '想了想要不要打招呼'}])
    chat = ChatService(db=db, provider=provider, push_event=_noop_push)

    turn = await chat.send('在干嘛')
    task = chat._inflight
    if task is not None:
        await asyncio.wait_for(task, timeout=5)

    entries = trace.since(0)
    kinds = [e['kind'] for e in entries]
    assert 'user_input' in kinds
    assert 'llm_request' in kinds
    assert kinds.count('llm_chunk') == 2
    assert 'llm_final' in kinds

    user_entry = next(e for e in entries if e['kind'] == 'user_input')
    assert user_entry['turnId'] == turn
    assert user_entry['text'] == '在干嘛'

    request_entry = next(e for e in entries if e['kind'] == 'llm_request')
    assert request_entry['turnId'] == turn
    assert isinstance(request_entry['messages'], list) and request_entry['messages']

    chunk_entries = [e for e in entries if e['kind'] == 'llm_chunk']
    assert chunk_entries[0]['text'] == '你好'
    assert chunk_entries[1]['reasoning'] == '想了想要不要打招呼'

    final_entry = next(e for e in entries if e['kind'] == 'llm_final')
    assert final_entry['turnId'] == turn
    assert '你好' in final_entry['text'] and '呀' in final_entry['text']


async def test_chat_send_memory_event_does_not_crash(db):
    """回归：trace.emit('memory_fact', ..., kind=...) 曾经因为 kind 关键字和位置参数撞车而 TypeError。"""
    trace.clear()
    provider = _FakeProvider([{'text': '<memory type="偏好">玩家喜欢深夜写代码</memory>好的'}])
    events: list[tuple[str, dict]] = []

    async def push(channel, payload):
        events.append((channel, payload))

    chat = ChatService(db=db, provider=provider, push_event=push)
    turn = await chat.send('记一下')
    task = chat._inflight
    if task is not None:
        await asyncio.wait_for(task, timeout=5)

    assert not any(channel == 'chat.error' for channel, _ in events)
    entries = trace.since(0)
    fact_entry = next(e for e in entries if e['kind'] == 'memory_fact')
    assert fact_entry['turnId'] == turn
    assert fact_entry['content'] == '玩家喜欢深夜写代码'
    assert fact_entry['memoryKind'] == '偏好'


async def test_chat_send_llm_error_does_not_crash(db):
    """回归：trace.emit('llm_error', ..., kind=...) 曾经因为同样的原因 TypeError。"""
    trace.clear()
    chat = ChatService(db=db, provider=_FailingProvider(), push_event=_noop_push)
    turn = await chat.send('在吗')
    task = chat._inflight
    if task is not None:
        await asyncio.wait_for(task, timeout=5)

    entries = trace.since(0)
    error_entry = next(e for e in entries if e['kind'] == 'llm_error')
    assert error_entry['turnId'] == turn
    assert error_entry['errorKind'] == 'unknown'
    assert '模拟网络故障' in error_entry['message']
