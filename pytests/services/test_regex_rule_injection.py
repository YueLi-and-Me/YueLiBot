"""正则规则接入回复生成：只进写正文的提示词，决策层看不到。

对应任务书 ★4（没有回应对象时不匹配、不发事件）与 ★8（决策层不出现规则；
写正文的提示词格式逐字一致；「〈〉里换成…」那行整块只出现一次）。
用例走 ``ChatService`` 的真实组装与渲染路径，对话内容全部是虚构的。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import sqlite3

import pytest

from src.core.agent.expression import ExpressionSample, render_expression_habits
from src.core.config.schema import Config, PersonalityConfig, RegexRuleConfig
from src.core.services.chat import ChatService

NOW = 1_800_000_000_000
QUERY = '男人是这样的'
_RULE = RegexRuleConfig(
    pattern=r'^(?P<n>\S{1,20})是这样的[。！!]*$',
    reaction='{n}是这样的，〈某类人〉只要〈做一件简单的事〉就可以',
)
_HEADER = '【对方这句话的一种接法，接不接由你判断，不合适就当没看到】'
_ANGLE_NOTE = '〈〉里换成这次对话里对应的人和事；只借说法的结构，不要照搬原话。'
_EXPECTED_BLOCK = (
    f'{_HEADER}\n'
    '- 男人是这样的，〈某类人〉只要〈做一件简单的事〉就可以\n'
    f'{_ANGLE_NOTE}'
)


async def _noop_push(_channel: str, _payload: object, _stream_id: int = 1) -> None:
    return None


def _chat(db: sqlite3.Connection, rules: List[RegexRuleConfig]) -> ChatService:
    personality = PersonalityConfig(
        birthday='',
        personality='虚构人设',
        reply_style='',
        tone_probability=0.0,
        tone_variants=[],
        regex_rules=rules,
    )
    return ChatService(
        db=db,
        chat_provider=None,
        proactive_provider=None,
        summary_provider=None,
        push_event=_noop_push,
        cfg=Config(personality=personality),
    )


@pytest.fixture()
def emitted(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[str, Dict[str, Any]]]:
    """截下全部观测事件，只记录事件名与字段。"""

    events: List[Tuple[str, Dict[str, Any]]] = []
    monkeypatch.setattr(
        'src.core.observe.events.emit',
        lambda kind, **fields: events.append((kind, fields)) or {},
    )
    return events


def _system(messages: List[dict]) -> str:
    assert messages[0]['role'] == 'system'
    return str(messages[0]['content'])


class TestNoRespondedMessage:
    """★4：没有回应对象（主动开口、旧管线）时不匹配、不发事件。"""

    async def test_enrich_without_responded_message_skips_rules(self, db, emitted) -> None:
        chat = _chat(db, [_RULE])
        prepared = chat._prepare_turn_context(chat.desktop_context, QUERY, NOW, turn_id=7)

        messages = await chat._enrich_prepared_context(prepared, None, {}, None)

        assert _HEADER not in _system(messages)
        assert [kind for kind, _fields in emitted if kind == 'regex_rule'] == []
        assert chat._regex_rule_cooldowns == {}


class TestReplyerOnly:
    """★8：规则只进写正文的提示词，格式与设计决定 9 逐字一致。"""

    @pytest.mark.parametrize('protocol_text', [None, '（回复协议）'])
    async def test_replyer_prompt_carries_rule_block_verbatim(
        self, db, emitted, protocol_text: str | None,
    ) -> None:
        chat = _chat(db, [_RULE])
        prepared = chat._prepare_turn_context(chat.desktop_context, QUERY, NOW, turn_id=7)

        messages = await chat._enrich_prepared_context(
            prepared, None, {}, None,
            protocol_text=protocol_text,
            responded_message=('小雨', QUERY),
        )

        system = _system(messages)
        assert f'# 表达方式参考\n{_EXPECTED_BLOCK}' in system
        assert system.count(_ANGLE_NOTE) == 1
        assert '优先参考' not in system
        events = [fields for kind, fields in emitted if kind == 'regex_rule']
        assert events == [{'streamId': chat.desktop_context.stream.id, 'rule': 1, 'result': 'injected'}]
        assert set(chat._regex_rule_cooldowns) == {(_RULE.pattern, chat.desktop_context.stream.id)}

    async def test_decision_prompt_never_carries_rule(self, db, emitted) -> None:
        chat = _chat(db, [_RULE])
        prepared = chat._prepare_turn_context(chat.desktop_context, QUERY, NOW, turn_id=7)

        decision = chat._render_prepared_context(prepared, decision_only=True)
        await chat._enrich_prepared_context(
            prepared, None, {}, None, responded_message=('小雨', QUERY),
        )
        decision_after = chat._render_prepared_context(
            prepared, protocol_text='（决策协议）', decision_only=True,
        )

        for messages in (decision, decision_after):
            text = '\n'.join(str(message['content']) for message in messages)
            assert _HEADER not in text
            assert '男人是这样的，' not in text

    async def test_rule_text_never_carries_message_body_into_event(self, db, emitted) -> None:
        chat = _chat(db, [_RULE])
        prepared = chat._prepare_turn_context(chat.desktop_context, QUERY, NOW, turn_id=7)

        await chat._enrich_prepared_context(
            prepared, None, {}, None, responded_message=('小雨', QUERY),
        )

        for kind, fields in emitted:
            if kind == 'regex_rule':
                assert set(fields) == {'streamId', 'rule', 'result'}

    async def test_second_turn_right_after_is_cooldown(self, db, emitted) -> None:
        chat = _chat(db, [_RULE])
        prepared = chat._prepare_turn_context(chat.desktop_context, QUERY, NOW, turn_id=7)

        await chat._enrich_prepared_context(
            prepared, None, {}, None, responded_message=('小雨', QUERY),
        )
        messages = await chat._enrich_prepared_context(
            prepared, None, {}, None, responded_message=('小雨', QUERY),
        )

        assert _HEADER not in _system(messages)
        results = [fields['result'] for kind, fields in emitted if kind == 'regex_rule']
        assert results == ['injected', 'cooldown']


class TestRenderBlock:
    """★8：表达方式与规则拼在同一块里，「〈〉」说明行只出现一次。"""

    def test_expression_and_rule_share_one_angle_note(self) -> None:
        samples = [ExpressionSample(id=1, situation='有人逞强时', style='〈逞强的人〉又开始了')]

        block = render_expression_habits(samples, reaction='男人是这样的，〈某类人〉只要…')

        assert block == (
            '【表达习惯参考，请视情况自然的使用】\n'
            '- 当有人逞强时，可以套用：〈逞强的人〉又开始了\n'
            f'{_HEADER}\n'
            '- 男人是这样的，〈某类人〉只要…\n'
            f'{_ANGLE_NOTE}'
        )

    def test_angle_note_follows_rule_even_when_expression_has_none(self) -> None:
        samples = [ExpressionSample(id=1, situation='有人逞强时', style='又开始了')]

        block = render_expression_habits(samples, reaction='〈谁〉都这样')

        assert block.count(_ANGLE_NOTE) == 1
        assert block.endswith(_ANGLE_NOTE)

    def test_rule_without_angle_brackets_has_no_note(self) -> None:
        assert render_expression_habits([], reaction='晚安') == f'{_HEADER}\n- 晚安'

    def test_expression_only_is_unchanged(self) -> None:
        samples = [ExpressionSample(id=1, situation='有人逞强时', style='〈逞强的人〉又开始了')]

        assert render_expression_habits(samples) == (
            '【表达习惯参考，请视情况自然的使用】\n'
            '- 当有人逞强时，可以套用：〈逞强的人〉又开始了\n'
            f'{_ANGLE_NOTE}'
        )
