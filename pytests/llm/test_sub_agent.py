"""一次性模型子任务执行器的回归测试。"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List

import asyncio

import pytest

from src.core.agent.sub_agent import (
    DEFAULT_USER_PROMPT,
    SubAgentCall,
    run_sub_agent,
)


class _RecordingProvider:
    """按脚本输出文本与推理增量，并记录收到的调用参数。"""

    def __init__(self, text: str, reasoning: str = '') -> None:
        self._text = text
        self._reasoning = reasoning
        self.calls: List[Dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> AsyncIterator[dict]:
        self.calls.append(kwargs)

        async def _gen() -> AsyncIterator[dict]:
            if self._reasoning:
                yield {'reasoning': self._reasoning}
            if self._text:
                yield {'text': self._text}

        return _gen()


@pytest.mark.asyncio
async def test_run_sub_agent_appends_user_turn_for_system_only_request() -> None:
    """兼容接口要求 user 消息，执行器必须在发起前补齐。"""
    provider = _RecordingProvider('{"ok":true}', reasoning='想想')
    result = await run_sub_agent(SubAgentCall(
        task='utility',
        provider=provider,
        messages=[{'role': 'system', 'content': '只输出 JSON'}],
    ))

    assert result.text == '{"ok":true}'
    assert result.reasoning_chars == 2
    sent = provider.calls[0]['messages']
    assert [message['role'] for message in sent] == ['system', 'user']
    assert sent[-1]['content'] == DEFAULT_USER_PROMPT


@pytest.mark.asyncio
async def test_run_sub_agent_keeps_existing_user_message() -> None:
    """调用方已经给出 user 消息时不得重复追加。"""
    provider = _RecordingProvider('ok')
    await run_sub_agent(SubAgentCall(
        task='utility',
        provider=provider,
        messages=[
            {'role': 'system', 'content': '规则'},
            {'role': 'user', 'content': '实际输入'},
        ],
    ))

    sent = provider.calls[0]['messages']
    assert [message['role'] for message in sent] == ['system', 'user']
    assert sent[-1]['content'] == '实际输入'


@pytest.mark.asyncio
async def test_run_sub_agent_forwards_request_options() -> None:
    """采样参数、结构化输出与取消信号必须原样到达 provider。"""
    signal = asyncio.Event()
    provider = _RecordingProvider('ok')
    render_params = {'utility': {'input': 'x'}}
    trace_extra = {'promptId': 'utility.select', 'promptHash': 'deadbeef'}

    def response_validator(_raw: str) -> None:
        return None

    await run_sub_agent(SubAgentCall(
        task='utility',
        provider=provider,
        messages=[{'role': 'user', 'content': '开始'}],
        temperature=0.1,
        max_tokens=128,
        response_format={'type': 'json_object'},
        response_validator=response_validator,
        signal=signal,
        render_params=render_params,
        trace_extra=trace_extra,
    ))

    call = provider.calls[0]
    assert call['temperature'] == 0.1
    assert call['max_tokens'] == 128
    assert call['response_format'] == {'type': 'json_object'}
    assert call['response_validator'] is response_validator
    assert call['signal'] is signal


@pytest.mark.asyncio
@pytest.mark.parametrize('call', [
    SubAgentCall(task=' ', provider=_RecordingProvider('ok'), messages=[{'role': 'user', 'content': 'x'}]),
    SubAgentCall(task='utility', provider=_RecordingProvider('ok'), messages=[]),
])
async def test_run_sub_agent_rejects_invalid_input(call: SubAgentCall) -> None:
    """任务名与消息列表的硬约束必须在请求前暴露。"""
    with pytest.raises(ValueError):
        await run_sub_agent(call)
