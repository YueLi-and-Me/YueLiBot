"""助手自身表达与动作伪消息不污染表达学习。"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List

import pytest

from src.core.agent.expression_learn import run_learning
from src.core.agent.fact_extract import render_dialogue
from src.core.memory.store import (
    MemoryStore,
    format_assistant_poke_action,
    format_assistant_reaction_action,
    is_assistant_action_message,
)

STREAM_ID = 1
NOW = 1_800_000_000_000


class _StubProvider:
    """返回合法空学习结果，并保留实际收到的模型消息。"""

    def __init__(self) -> None:
        self.requests: List[List[Dict[str, str]]] = []

    async def stream(self, **kwargs: Any) -> AsyncIterator[Dict[str, str]]:
        self.requests.append(kwargs['messages'])
        yield {'text': '{"expressions": []}'}


async def _run_learning(store: MemoryStore, provider: _StubProvider, db: Any) -> None:
    await run_learning(
        store,
        provider,
        db,
        stream_id=STREAM_ID,
        participants=(),
        bot_name='月璃',
        trigger_messages=1,
        batch_messages=10,
        temperature=0.1,
        max_tokens=128,
        now=NOW,
    )


@pytest.mark.asyncio
async def test_action_only_batch_does_not_call_expression_model(db: Any) -> None:
    """整批只有她的动作历史时不发起模型请求：没有别人的发言就没有可学的说法。"""

    store = MemoryStore(db)
    provider = _StubProvider()
    store.append_message(
        STREAM_ID,
        None,
        'assistant',
        format_assistant_poke_action('小明'),
        NOW,
    )
    store.append_message(
        STREAM_ID,
        None,
        'assistant',
        format_assistant_reaction_action(8203, '无奈'),
        NOW + 1,
    )

    await _run_learning(store, provider, db)

    assert provider.requests == []


@pytest.mark.asyncio
async def test_assistant_speech_only_batch_does_not_call_expression_model(db: Any) -> None:
    """整批只有月璃真实发言时也不调用学习模型，避免自我模仿闭环。"""

    store = MemoryStore(db)
    provider = _StubProvider()
    store.append_message(
        STREAM_ID,
        None,
        'assistant',
        '这是一段足够长的月璃回复，但无论内容多有辨识度，都不能成为表达学习语料。',
        NOW,
    )

    await _run_learning(store, provider, db)

    assert provider.requests == []


@pytest.mark.asyncio
async def test_real_speech_keeps_action_out_of_expression_dialogue(db: Any) -> None:
    """群友开口会触发学习，但月璃台词与动作历史都不进入模型对白。

    这条边界由代码过滤保证，不能只依赖提示词要求模型自行忽略月璃的说法。
    """

    store = MemoryStore(db)
    provider = _StubProvider()
    other_speech = (
        '你看那个片子了吗，最后那段我印象特别深，尤其是镜头安静下来以后，'
        '前面那些喧闹一下子都有了落点。'
    )
    real_speech = (
        '这次镜头的光影变化很有意思，我尤其喜欢最后安静下来的那一段。'
        '前面的喧闹和结尾形成了很自然的对照，情绪也跟着慢慢落了下来。'
    )
    action = format_assistant_reaction_action(8203, '无奈')
    store.append_message(STREAM_ID, 1, 'user', other_speech, NOW)
    store.append_message(STREAM_ID, None, 'assistant', real_speech, NOW + 1)
    store.append_message(STREAM_ID, None, 'assistant', action, NOW + 2)

    await _run_learning(store, provider, db)

    assert len(provider.requests) == 1
    dialogue = provider.requests[0][-1]['content']
    assert other_speech in dialogue
    assert real_speech not in dialogue
    assert action not in dialogue
    assert '月璃：' not in dialogue


def test_fact_dialogue_omits_assistant_action_messages(db: Any) -> None:
    """事实抽取只看到真正说出口的内容，不把平台动作当对白。"""

    store = MemoryStore(db)
    action = format_assistant_poke_action('小明')
    store.append_message(STREAM_ID, None, 'assistant', '我记得这件事。', NOW)
    store.append_message(STREAM_ID, None, 'assistant', action, NOW + 1)

    dialogue = render_dialogue(
        store.messages_after(STREAM_ID, 0, 10),
        (),
        '月璃',
    )

    assert dialogue == '月璃：我记得这件事。'
    assert action not in dialogue


def test_normal_bracketed_speech_is_not_an_action_message() -> None:
    """普通方括号发言不能因外形相似而被排除。"""

    assert is_assistant_action_message('[状态] 这个方案我觉得可以。') is False
    assert is_assistant_action_message('[戳了戳]') is False
    assert is_assistant_action_message(format_assistant_poke_action('小明')) is True
    assert is_assistant_action_message(
        format_assistant_reaction_action(8203, '无奈'),
    ) is True
