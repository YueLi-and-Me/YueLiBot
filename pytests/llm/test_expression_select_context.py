"""表达选择提示词的回应对象与背景输入，以及注入文本的新渲染格式。

对应任务书 T6 与 ★15 / ★16：

- ★15 有回应对象或决策背景时，选择提示词给出「这次要回应：…」「回复背景：…」；
  两项都没有时退回「对方刚说的这句：…」；任何情况下 style 都不进提示词。
- ★16 ``render_expression_habits`` 的条目改用「- 当{situation}时，可以套用：{style}」，
  选中条目里至少一条 style 带〈角色〉占位时追加换算说明行。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List

import asyncio
import json

import pytest

from src.core.agent.expression import ExpressionSample, render_expression_habits
from src.core.agent.expression_select import ExpressionSelector, build_selection_prompt
from src.core.observe.store import search_events

# situation 与 style 刻意写成完全不同的文本，才能断言提示词只带了情境。
_CANDIDATES = [
    ExpressionSample(id=1, situation='对方分享顺利的事时', style='先接事情本身再夸'),
    ExpressionSample(id=2, situation='对方讲的事很离谱时', style='先愣一下再反问'),
    ExpressionSample(id=3, situation='没听懂时', style='直接承认不知道'),
    ExpressionSample(id=4, situation='对方认真问事时', style='先给结论再解释'),
    ExpressionSample(id=5, situation='话题自然结束时', style='短回应收尾'),
]

_RESPONDED = ('小明', '你桌面都有什么')
_REFERENCE = '他刚分享完桌面截图，想听她的评价'


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


def _assert_styles_absent(prompt: str) -> None:
    """style 进提示词会让模型按表述好感挑，而不是按情境匹配挑。"""

    for sample in _CANDIDATES:
        assert sample.style not in prompt


# ── ★15 提示词携带回应对象与决策背景 ────────────────────────────────────


def test_prompt_carries_responded_message_and_reference() -> None:
    """两项输入都在时，提示词描述「这次要回应谁、为什么接」，不再复述当前消息。"""

    prompt = build_selection_prompt(
        _CANDIDATES,
        '你桌面都有什么',
        [],
        limit=4,
        responded=_RESPONDED,
        reference=_REFERENCE,
    )

    assert f'这次要回应：{_RESPONDED[0]}：{_RESPONDED[1]}' in prompt
    assert f'回复背景：{_REFERENCE}' in prompt
    assert '对方刚说的这句' not in prompt
    _assert_styles_absent(prompt)


@pytest.mark.parametrize('responded, reference, present, absent', [
    (_RESPONDED, None, f'这次要回应：{_RESPONDED[0]}：{_RESPONDED[1]}', '回复背景：'),
    (None, _REFERENCE, f'回复背景：{_REFERENCE}', '这次要回应：'),
])
def test_prompt_carries_each_input_independently(
    responded: tuple[str, str] | None,
    reference: str | None,
    present: str,
    absent: str,
) -> None:
    """只有回应对象或只有背景时，按给出项拼接，不丢也不补编另一项。"""

    prompt = build_selection_prompt(
        _CANDIDATES, '你桌面都有什么', [], limit=4,
        responded=responded, reference=reference,
    )

    assert present in prompt
    assert absent not in prompt
    assert '对方刚说的这句' not in prompt
    _assert_styles_absent(prompt)


def test_prompt_falls_back_to_user_text_without_context() -> None:
    """两项输入都没有时保持旧口径，与改动前完全一致。"""

    prompt = build_selection_prompt(_CANDIDATES, '你桌面都有什么', [], limit=4)

    assert '对方刚说的这句：你桌面都有什么' in prompt
    assert '这次要回应：' not in prompt
    assert '回复背景：' not in prompt
    _assert_styles_absent(prompt)


def test_selector_render_params_match_prompt_placeholders() -> None:
    """观察面板回放用的渲染参数必须与提示词占位符同源，能还原实际提示词。"""

    provider = _Provider(json.dumps({'selected': [1]}))
    selector = ExpressionSelector(provider, temperature=0.1, max_tokens=2048)
    asyncio.run(selector.select(
        '你桌面都有什么', [], _CANDIDATES, limit=4,
        responded=_RESPONDED, reference=_REFERENCE,
    ))

    events = search_events(kinds=['llm_request']).events
    assert len(events) == 1
    render = events[0]['renderParams']['expression.select']
    assert set(render) == {'history', 'response_context', 'options', 'limit'}
    assert render['response_context'] == (
        f'这次要回应：{_RESPONDED[0]}：{_RESPONDED[1]}\n回复背景：{_REFERENCE}'
    )


# ── ★16 注入文本新格式 ──────────────────────────────────────────────────


def test_render_appends_placeholder_hint_when_any_style_has_roles() -> None:
    """只要有一条带〈角色〉，就要提示把占位换算成本次对话里的人和事。"""

    block = render_expression_habits([
        ExpressionSample(
            id=1,
            situation='朋友提出荒唐设想、而这设想成真对自己有好处',
            style='〈提出设想的人〉真是害人不浅',
        ),
        ExpressionSample(id=2, situation='对方抱怨具体麻烦', style='先接住那件麻烦'),
    ])

    assert block.splitlines() == [
        '【表达习惯参考，请视情况自然的使用】',
        '- 当朋友提出荒唐设想、而这设想成真对自己有好处时，可以套用：'
        '〈提出设想的人〉真是害人不浅',
        '- 当对方抱怨具体麻烦时，可以套用：先接住那件麻烦',
        '〈〉里换成这次对话里对应的人和事；只借说法的结构，不要照搬原话。',
    ]


def test_render_without_roles_has_only_header_and_entries() -> None:
    """旧条目没有〈角色〉，原样渲染，不追加末行说明。"""

    block = render_expression_habits([
        ExpressionSample(id=1, situation='对方抱怨具体麻烦', style='先接住那件麻烦'),
    ])

    assert block.splitlines() == [
        '【表达习惯参考，请视情况自然的使用】',
        '- 当对方抱怨具体麻烦时，可以套用：先接住那件麻烦',
    ]


def test_render_strips_time_suffix_before_appending() -> None:
    """学习侧 situation 以「时 / 的时候」收尾：渲染前去掉尾缀，只保留模板的一个「时」。

    否则「…对自己有好处时」套上模板会渲染成「…对自己有好处时时」。
    """

    block = render_expression_habits([
        ExpressionSample(
            id=1,
            situation='朋友提出荒唐设想、而这设想成真对自己有好处时',
            style='〈提出设想的人〉真是害人不浅',
        ),
        ExpressionSample(id=2, situation='对方抱怨具体麻烦的时候', style='先接住那件麻烦'),
    ])

    lines = block.splitlines()
    assert lines[1] == (
        '- 当朋友提出荒唐设想、而这设想成真对自己有好处时，可以套用：'
        '〈提出设想的人〉真是害人不浅'
    )
    assert lines[2] == '- 当对方抱怨具体麻烦时，可以套用：先接住那件麻烦'
    assert '时时' not in block


def test_render_strips_sentence_punctuation_before_appending_time() -> None:
    """★6：旧条目 situation 常以句末标点收尾；先去掉「。！？.!?…」与空白，再补模板的「时」。

    否则「被对方可爱发言打动时的反应。」会渲染成「…反应。时」。
    """

    block = render_expression_habits([
        ExpressionSample(id=1, situation='被对方可爱发言打动时的反应。', style='哈！'),
        ExpressionSample(id=2, situation='被问周末怎么过时？', style='先反问回去'),
        ExpressionSample(id=3, situation='对方讲了冷笑话的时候', style='配合笑一下'),
    ])

    assert '。时' not in block
    assert '？时' not in block
    lines = block.splitlines()
    assert lines[1] == '- 当被对方可爱发言打动时的反应时，可以套用：哈！'
    assert lines[2] == '- 当被问周末怎么过时，可以套用：先反问回去'
    assert lines[3] == '- 当对方讲了冷笑话时，可以套用：配合笑一下'


def test_render_empty_input_returns_empty_string() -> None:
    """没有选中条目时不注入该提示词块：空串而不是只有说明行。"""

    assert render_expression_habits([]) == ''
