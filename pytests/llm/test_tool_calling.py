"""OpenAI 兼容工具调用的流式解析回归。

覆盖三件事：分片按 index 拼接、没有工具声明时行为逐字不变、无名分片被丢弃。
"""

from __future__ import annotations

import json

from src.core.llm_models.openai import _ToolCallAccumulator, _parse_sse_line


def _delta(payload: dict) -> str:
    return 'data: ' + json.dumps({'choices': [{'delta': payload}]}, ensure_ascii=False)


def test_parses_tool_call_deltas() -> None:
    """工具调用分片被原样透传，解析器本身不做拼接。"""
    chunk = _parse_sse_line(_delta({
        'tool_calls': [{'index': 0, 'id': 'call_1',
                        'function': {'name': 'reply', 'arguments': '{"ms'}}],
    }))

    assert chunk is not None and not isinstance(chunk, str)
    assert chunk['tool_call_deltas'][0]['function']['name'] == 'reply'


def test_text_only_delta_has_no_tool_field() -> None:
    """没有工具调用的增量不该多出字段，否则下游会误判为工具轮。"""
    chunk = _parse_sse_line(_delta({'content': '在的'}))

    assert chunk == {'text': '在的'}


def test_accumulator_joins_arguments_across_deltas() -> None:
    """name 只在首片出现、arguments 逐片拼接，这是兼容接口的实际形状。"""
    acc = _ToolCallAccumulator()
    acc.push([{'index': 0, 'id': 'call_1', 'function': {'name': 'reply', 'arguments': '{"msg'}}])
    acc.push([{'index': 0, 'function': {'arguments': '_id": 3}'}}])

    drained = acc.drain()

    assert drained == [{'id': 'call_1', 'name': 'reply', 'arguments': '{"msg_id": 3}'}]
    assert json.loads(drained[0]['arguments']) == {'msg_id': 3}
    # drain 之后状态清空：重试路径不能重复产出同一个调用。
    assert acc.drain() == []


def test_accumulator_orders_by_index_and_drops_nameless() -> None:
    """多工具按 index 升序；没有名字的分片是协议噪声，留着也调不动。"""
    acc = _ToolCallAccumulator()
    acc.push([
        {'index': 1, 'function': {'name': 'recall', 'arguments': '{}'}},
        {'index': 0, 'function': {'name': 'reply', 'arguments': '{}'}},
        {'index': 2, 'function': {'arguments': '{}'}},
    ])

    assert [call['name'] for call in acc.drain()] == ['reply', 'recall']
