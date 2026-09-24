"""聊天提示词不再常驻注入「关系深度」与「认识多久」，好感度数值照常结算。

好感度只按回话次数涨跌、档位没有配套的说话差异，常驻那一句几乎不提供信息；
认识时长按本系统首次见到对方的时刻计算，迁移来的记忆会被算短。两句都从聊天
提示词中移除，数值与首次出现时间照常记录，供以后重新设计时使用。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, List

import pytest

from src.core.config.schema import Config
from src.core.platform_io.types import DeliveryReceipt, InboundMessage, OutboundMessage
from src.core.services.chat import ChatService


_FORBIDDEN = ('关系深度', '认识了', '刚认识', '关系走到哪里')


class _RecordingProvider:
    """记录每次调用收到的完整消息序列，并返回固定回复。"""

    def __init__(self, script: List[str]) -> None:
        self.script = script
        self.calls: List[list] = []
        self.provider = 'fake'
        self.model = 'fake'

    async def stream(self, messages=None, **_kwargs: Any) -> AsyncIterator[dict[str, str]]:
        self.calls.append(list(messages or []))
        for text in self.script:
            yield {'text': text}


class _RecordingBroker:
    def __init__(self) -> None:
        self.dispatched: List[OutboundMessage] = []

    async def dispatch(self, message: OutboundMessage) -> DeliveryReceipt:
        self.dispatched.append(message)
        return DeliveryReceipt(
            platform=message.stream.platform,
            stream_id=message.stream.id,
            external_message_ids=['x'],
        )


async def _noop(_channel: str, _payload: Any, _stream_id: int) -> None:
    return None


def _config() -> Config:
    config = Config()
    config.bot.name = '月璃'
    config.conversation_agent.mode = 'enabled'
    config.conversation_agent.max_cognitive_rounds = 0
    config.group_chat.scene_refresh_messages = 0
    return config


def _resolve(registry, kind: str):
    return registry.resolve_inbound(
        platform='qq',
        stream_kind=kind,
        stream_external_id='86420' if kind == 'group' else '97531',
        sender_external_id='97531',
        sender_nickname='小李',
        sender_group_card='小李' if kind == 'group' else '',
        first_seen_at=1_000_000,
    )


def _all_text(calls: List[list]) -> str:
    return '\n'.join(
        str(message.get('content', '')) for call in calls for message in call
    )


@pytest.mark.parametrize('kind', ['group', 'direct'])
async def test_chat_prompt_omits_relationship_depth_and_acquaintance(db, kind) -> None:
    provider = _RecordingProvider([
        '<decision action="reply" targets="1" reasons="direct_question" length="brief"/>'
        '<say emotion="normal">在呢</say>',
    ])
    broker = _RecordingBroker()
    chat = ChatService(db, provider, None, None, _noop, cfg=_config(), broker=broker)
    context = _resolve(chat._registry, kind)
    before = chat.persona.get(context.person.id).intimacy

    await chat.send(InboundMessage(
        text='月璃在吗', context=context, mentioned_me=True, name_mentioned=True,
    ))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    assert provider.calls, '回合没有调用模型'
    text = _all_text(provider.calls)
    for phrase in _FORBIDDEN:
        assert phrase not in text
    assert [message.segments for message in broker.dispatched] == [['在呢']]
    # 数值照常结算：提示词不再使用，不等于停止记录。
    assert chat.persona.get(context.person.id).intimacy > before


async def test_owner_prompt_omits_acquaintance(db) -> None:
    """认识时长原先只对主人注入；桌面会话的对方就是主人。"""
    provider = _RecordingProvider(['<say>在呢</say>'])
    chat = ChatService(db, provider, None, None, _noop, cfg=_config(), broker=_RecordingBroker())
    context = chat._registry.desktop_context()
    assert context.person.kind == 'owner'

    await chat.send(InboundMessage(text='在吗', context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    assert provider.calls, '回合没有调用模型'
    text = _all_text(provider.calls)
    for phrase in _FORBIDDEN:
        assert phrase not in text


async def test_itemized_prompt_has_no_relationship_depth_item(db) -> None:
    """工具调用路径把运行时上下文拆成独立项，同样不再出现那一项。"""
    from src.core.agent.prompt import build_itemized_system_prompt

    system, items = build_itemized_system_prompt(
        name='月璃', birthday='', personality='测试人设', reply_style='测试说话方式',
    )
    text = system + '\n'.join(items)
    for phrase in _FORBIDDEN:
        assert phrase not in text
    assert '人物画像' not in text
