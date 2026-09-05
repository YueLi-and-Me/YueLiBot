"""气泡末尾句号处理。

中文聊天里几乎没人用句号收尾，它在 IM 语境下读作「冷淡、有距离」，而模型写散文的
默认习惯让每条消息都带上。这里钉住「删什么、不删什么」的边界。
"""

from __future__ import annotations

from src.core.agent.segmentation import split_into_bubbles
from src.core.config.schema import TypingConfig


def _bubbles(text: str) -> list[str]:
    return split_into_bubbles(text, TypingConfig())


def test_trailing_period_is_dropped() -> None:
    assert _bubbles('今天没事。') == ['今天没事']
    assert _bubbles('嗯，我看看。') == ['嗯，我看看']


def test_other_terminal_marks_are_kept() -> None:
    """问号、感叹号、省略号在聊天里完全正常，属于真语气，不能一起删掉。"""
    assert _bubbles('行吧！') == ['行吧！']
    assert _bubbles('真的？') == ['真的？']
    assert _bubbles('等等…') == ['等等…']
    assert _bubbles('等等...') == ['等等...']


def test_repeated_periods_are_kept() -> None:
    """「。。。」是「无语」的常用写法，删成「。。」反而更怪。"""
    assert _bubbles('无语。。。') == ['无语。。。']


def test_sentence_internal_period_survives() -> None:
    """刺眼的是每条消息都以句号收尾，句中的那个并不刺眼。"""
    assert _bubbles('好的。你呢？') == ['好的。你呢？']
    assert _bubbles('我去了。他没来。') == ['我去了。他没来']


def test_ascii_period_is_untouched() -> None:
    """ASCII 句点整个不碰：省略号语气与版本号都靠它。"""
    assert _bubbles('v4.0 挺好的。') == ['v4.0 挺好的']


def test_prompts_state_the_rule() -> None:
    """代码只兜底，规矩本身写在提示词里——两处都要有。"""
    from src.core.prompts.registry import get_prompt

    for template_id in ('chat.protocol', 'chat.action.protocol'):
        assert '句尾不要使用句号' in get_prompt(template_id).text
