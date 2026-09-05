"""表达方式挑选：候选受限、解析严格、非法输出必须炸。

候选来自 expressions 表的按会话候选池；模型只能从当轮候选的编号中选择。
选择提示词只列情境（situation），说法示例（style）是选中之后的载荷。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List

import asyncio
import json

import pytest

from src.core.agent.expression import ExpressionSample
from src.core.agent.expression_select import (
    ExpressionSelector,
    build_selection_prompt,
    parse_selection,
)


# situation 与 style 刻意写成完全不同的文本，才能断言提示词只带了情境。
_CANDIDATES = [
    ExpressionSample(id=1, situation='对方分享顺利的事时', style='先接事情本身'),
    ExpressionSample(id=2, situation='对方讲的事很离谱时', style='可以先愣一下'),
    ExpressionSample(id=3, situation='没听懂时', style='直接承认不知道'),
    ExpressionSample(id=4, situation='对方认真问事时', style='直接回答'),
    ExpressionSample(id=5, situation='话题自然结束时', style='短回应就够'),
]


class _Provider:
    """按脚本吐一段文本的假 provider，同时记下收到的调用参数。"""

    def __init__(self, payload: str) -> None:
        self._payload = payload
        self.calls: List[Dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> AsyncIterator[dict]:
        self.calls.append(kwargs)
        payload = self._payload

        async def _gen() -> AsyncIterator[dict]:
            yield {'text': payload}

        return _gen()


def test_prompt_lists_every_candidate_situation_with_index() -> None:
    prompt = build_selection_prompt(_CANDIDATES, '你桌面都有什么', [], limit=4)
    for index in range(1, len(_CANDIDATES) + 1):
        assert f'\n{index}. ' in f'\n{prompt}'
    # 职责边界、上下文不完整声明、判断依据三段都必须在
    assert '不负责代替角色说话' in prompt
    assert '这里没有全部展现出来' in prompt
    assert '判断依据' in prompt


def test_prompt_lists_situations_without_styles() -> None:
    """选择模型的任务是「我现在处在哪个情境」；style 进提示词会让它按好听程度挑。"""
    prompt = build_selection_prompt(_CANDIDATES, '你桌面都有什么', [], limit=4)
    for sample in _CANDIDATES:
        assert sample.situation in prompt
        assert sample.style not in prompt


def test_prompt_describes_empty_selection_as_json_object() -> None:
    """空选择也必须用 {"selected": []}，避免模型把「空数组」理解成裸数组。"""
    prompt = build_selection_prompt(_CANDIDATES, '你桌面都有什么', [], limit=4)
    assert '{"selected": []}' in prompt
    assert '裸数组' in prompt


def test_parse_returns_samples_in_requested_order() -> None:
    picked = parse_selection('{"selected": [2, 1]}', _CANDIDATES, limit=4)
    assert len(picked) == 2
    assert picked[0] != picked[1]
    assert picked[0] is _CANDIDATES[1]
    assert picked[1] is _CANDIDATES[0]


def test_parse_accepts_empty_selection() -> None:
    """没有贴合的就该空手而归，不许凑数。"""
    assert parse_selection('{"selected": []}', _CANDIDATES, limit=4) == []


@pytest.mark.parametrize('raw, reason', [
    ('这不是 JSON', '不是合法 JSON'),
    ('{"selected": [1], "why": "x"}', '多了字段'),
    ('{"picked": [1]}', '键名不对'),
    ('{"selected": "1"}', '不是数组'),
    ('{"selected": [1, 1]}', '编号重复'),
    ('{"selected": [0]}', '编号越界'),
    ('{"selected": [999]}', '编号越界'),
    ('{"selected": ["1"]}', '编号不是整数'),
    ('{"selected": [true]}', '布尔不算整数'),
])
def test_parse_rejects_malformed_output(raw: str, reason: str) -> None:
    with pytest.raises(ValueError):
        parse_selection(raw, _CANDIDATES, limit=4)


def test_parse_rejects_more_than_limit() -> None:
    with pytest.raises(ValueError):
        parse_selection('{"selected": [1, 2, 3, 4, 5]}', _CANDIDATES, limit=4)


def test_selector_passes_structured_params_and_returns_samples() -> None:
    provider = _Provider(json.dumps({'selected': [3, 5]}))
    selector = ExpressionSelector(provider, temperature=0.1, max_tokens=2048)
    picked = asyncio.run(selector.select('你桌面都有什么', [], _CANDIDATES, limit=4))

    assert picked == [_CANDIDATES[2], _CANDIDATES[4]]
    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call['temperature'] == 0.1
    assert call['max_tokens'] == 2048
    assert call['response_format'] == {'type': 'json_object'}
    assert 'thinking' not in call
    # 统一执行器保证请求里至少有一条 user 消息，兼容 SiliconFlow 等接口。
    sent = call['messages']
    assert [message['role'] for message in sent] == ['system', 'user']
    assert sent[0]['content'].startswith('你在为一次回复挑选表达情境')
    assert sent[-1]['content'] == '请按上述要求输出结果。'


def test_selector_rejects_empty_candidates() -> None:
    """候选为空时由调用方跳过本轮选择；漏到这里就是调用方的 bug，必须炸。"""
    selector = ExpressionSelector(_Provider('{"selected": []}'), temperature=0.1, max_tokens=2048)
    with pytest.raises(ValueError, match='候选不能为空'):
        asyncio.run(selector.select('你桌面都有什么', [], [], limit=4))


def test_selector_error_message_carries_token_counts() -> None:
    """解析失败时要能看出模型把预算花在哪了，否则没法判断是不是推理吃光了。"""
    provider = _Provider('{"selected": [999]}')
    selector = ExpressionSelector(provider, temperature=0.1, max_tokens=2048)
    with pytest.raises(ValueError, match='正文字符='):
        asyncio.run(selector.select('随便说点什么', [], _CANDIDATES, limit=4))


class _BrokenProvider:
    """吐不出合法 JSON 的 provider，模拟推理吃光预算后正文为空。"""

    def stream(self, **kwargs: Any) -> AsyncIterator[dict]:
        async def _gen() -> AsyncIterator[dict]:
            yield {'reasoning': '嗯……让我想想应该选哪几条'}

        return _gen()


def _seed_expressions(db, stream_id: int, count: int = 10) -> None:
    """给指定会话种 count 条表达方式，让候选池达到可选规模。"""

    db.executemany(
        'INSERT INTO expressions (situation, style, stream_id, use_count, source, created_at)'
        ' VALUES (?, ?, ?, 1, ?, 0)',
        [
            (f'情境{i}', f'说法{i}', stream_id, '测试')
            for i in range(count)
        ],
    )
    db.commit()


def test_selection_failure_does_not_kill_the_reply(db) -> None:
    """表达样本挑选失败时只丢弃样本，不得阻断整轮回复。"""
    from src.core.config.schema import Config
    from src.core.services.chat import ChatService

    chat = ChatService(
        db=db,
        chat_provider=None,
        proactive_provider=None,
        summary_provider=None,
        push_event=lambda *_: None,
        cfg=Config(),
        expression_provider=_BrokenProvider(),
    )
    context = chat.desktop_context
    _seed_expressions(db, context.stream.id)
    picked = asyncio.run(chat._pick_expression_habits(context, '小璃说话！', [], None))
    assert picked == []
