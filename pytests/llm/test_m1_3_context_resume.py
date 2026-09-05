"""验证会话恢复提示按历史状态选择对应措辞档位。

本模块覆盖首次会话、短时间恢复、长时间未见和上下文信息缺失等输入，
确保生成提示不会凭空添加未验证的现实信息。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, AsyncIterator, Dict, List

from src.core.agent import prompt as prompt_module
from src.core.agent.prompt import build_system_prompt
from src.core.config.schema import Config
from src.core.services.chat import ChatService, InboundMessage


_RESUMPTION_MARKER = '距离你们上次说话'


def _prompt(**kwargs: Any) -> str:
    values = {
        'name': '测试角色',
        'birthday': '',
        'personality': '测试人设',
        'reply_style': '测试说话方式',
        'schedule': '',
    }
    values.update(kwargs)
    return build_system_prompt(**values)


class _RecordingProvider:
    def __init__(self) -> None:
        self.system_prompts: List[str] = []

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        **_kwargs: Any,
    ) -> AsyncIterator[Dict[str, str]]:
        self.system_prompts.append(messages[0]['content'])
        yield {'text': '<say>嗯</say>'}


async def _noop_push(_channel: str, _payload: Any, _stream_id: int = 1) -> None:
    return None


def _chat(db, provider: _RecordingProvider) -> ChatService:
    return ChatService(db, provider, provider, provider, _noop_push, cfg=Config())


async def _send_and_wait(chat: ChatService, text: str) -> None:
    context = chat.desktop_context
    await chat.send(InboundMessage(text=text, context=context))
    await chat._tick()
    inflight = chat._inflight.get(context.stream.id)
    assert inflight is not None
    await inflight.task


def _move_history_beyond_session_gap(db, chat: ChatService) -> None:
    db.execute(
        'UPDATE messages SET created_at = created_at - ?',
        (chat._session_gap_ms + 60_000,),
    )
    db.commit()


def test_prompt_without_resumption_is_stable_and_resumption_order_is_unchanged() -> None:
    now = datetime(2026, 8, 5, 12, 34)
    base_prompt = _prompt(now=now)
    resumption = '距离你们上次说话过了几个小时。'
    resumed_prompt = _prompt(
        now=now,
        persona='人格状态在这里。',
        resumption=resumption,
    )

    assert _prompt(now=now, resumption=None) == base_prompt
    assert resumed_prompt.index('# 此刻') < resumed_prompt.index(resumption) < resumed_prompt.index('人格状态在这里。')


async def test_same_session_does_not_add_resumption_context(db) -> None:
    provider = _RecordingProvider()
    chat = _chat(db, provider)

    assert hasattr(chat, '_sessions')
    await _send_and_wait(chat, '第一句')
    await _send_and_wait(chat, '紧接着第二句')

    assert _RESUMPTION_MARKER not in provider.system_prompts[-1]


async def test_only_a_gap_beyond_session_gap_adds_resumption_context(db) -> None:
    provider = _RecordingProvider()
    chat = _chat(db, provider)

    await _send_and_wait(chat, '第一句')
    _move_history_beyond_session_gap(db, chat)
    await _send_and_wait(chat, '隔了一段时间')

    assert _RESUMPTION_MARKER in provider.system_prompts[-1]


async def test_first_conversation_does_not_invent_a_previous_conversation(db) -> None:
    provider = _RecordingProvider()
    chat = _chat(db, provider)

    assert hasattr(chat, '_sessions')
    await _send_and_wait(chat, '第一次说话')

    assert _RESUMPTION_MARKER not in provider.system_prompts[-1]


async def test_resumption_context_is_consumed_after_one_turn(db) -> None:
    provider = _RecordingProvider()
    chat = _chat(db, provider)

    await _send_and_wait(chat, '第一句')
    _move_history_beyond_session_gap(db, chat)
    await _send_and_wait(chat, '久别后的第一句')
    prompt_after_gap = provider.system_prompts[-1]
    await _send_and_wait(chat, '久别后的第二句')

    assert _RESUMPTION_MARKER in prompt_after_gap
    assert _RESUMPTION_MARKER not in provider.system_prompts[-1]


async def test_proactive_prompt_receives_the_same_resumption_context(db) -> None:
    provider = _RecordingProvider()
    chat = _chat(db, provider)

    await _send_and_wait(chat, '第一句')
    _move_history_beyond_session_gap(db, chat)
    await chat.compose_proactive(chat.desktop_context, '正在写代码')

    assert _RESUMPTION_MARKER in provider.system_prompts[-1]


def test_resumption_tiers_cover_all_boundaries() -> None:
    assert hasattr(prompt_module, 'describe_resumption')
    describe = prompt_module.describe_resumption
    six_hours = 6 * 60 * 60_000
    one_day = 24 * 60 * 60_000

    assert describe(six_hours - 1) == '距离你们上次说话过了几个小时。'
    assert describe(six_hours) == '距离你们上次说话隔了一夜。'
    assert describe(one_day - 1) == '距离你们上次说话隔了一夜。'
    assert describe(one_day) == '距离你们上次说话已经过去 1 天。'
    assert describe(10 * 24 * 60 * 60_000) == '距离你们上次说话已经过去 10 天。'


def test_every_value_in_the_overnight_tier_is_literally_true() -> None:
    """中间档的每一个取值说出来都必须为真。

    第一版把上界写成 3 天，于是 71 小时的间隔也说「隔了一夜」——那已经快三天了。
    向提示词注入未经验证的信息会导致开场描述与现实不符。
    """
    describe = prompt_module.describe_resumption
    hour = 60 * 60_000

    for hours in (6, 12, 18, 23):
        assert describe(hours * hour) == '距离你们上次说话隔了一夜。', f'{hours} 小时'

    for hours in (25, 47, 71):
        assert describe(hours * hour).startswith('距离你们上次说话已经过去'), f'{hours} 小时'


def test_resumption_tier_thresholds_are_strictly_increasing() -> None:
    assert hasattr(prompt_module, 'RESUMPTION_TIERS')
    thresholds = [threshold for threshold, _ in prompt_module.RESUMPTION_TIERS]

    assert all(previous < current for previous, current in zip(thresholds, thresholds[1:]))


def test_resumption_text_never_directs_an_emotion() -> None:
    assert hasattr(prompt_module, 'describe_resumption')
    describe = prompt_module.describe_resumption
    outputs = [
        describe(60 * 60_000),
        describe(6 * 60 * 60_000),
        describe(3 * 24 * 60 * 60_000),
    ]

    for output in outputs:
        assert not any(word in output for word in ('想他', '闹别扭', '热情', '委屈'))
