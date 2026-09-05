"""speak（起一个不接任何人的话头）验收。

关键性质是**它不是一条并行的唤起路径**：扩展触发口径本来就会在「群里热闹但没人
理她」时给出候选，speak 只是让那个候选里多一个选项，因此与 reply 共用同一条
频率闸门，不会额外增加她开口的次数。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, List

import pytest

from src.core.agent.action_protocol import (
    SPEAK_REASON_CODES,
    DecisionHead,
    IllegalActionError,
    PlatformCapabilities,
    available_actions,
)
from src.core.config.schema import Config
from src.core.observe.store import event_store
from src.core.platform_io.types import DeliveryReceipt, InboundMessage, OutboundMessage
from src.core.services.chat import ChatService


class _ScriptedProvider:
    def __init__(self, scripts: List[List[str]]) -> None:
        self.scripts = scripts
        self.calls = 0
        self.messages: List[List[dict]] = []

    async def stream(self, messages=None, **_kwargs: Any) -> AsyncIterator[dict[str, str]]:
        index = self.calls
        self.calls += 1
        self.messages.append(list(messages or []))
        for text in self.scripts[min(index, len(self.scripts) - 1)]:
            yield {'text': text}


class _Broker:
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


def _config(*, speak: bool = True) -> Config:
    config = Config()
    config.bot.name = '月璃'
    config.conversation_agent.mode = 'enabled'
    config.conversation_agent.max_cognitive_rounds = 0
    config.group_chat.self_started_topics = speak
    config.group_chat.scene_refresh_messages = 0
    return config


def _group(registry):
    return registry.resolve_inbound(
        platform='qq', stream_kind='group', stream_external_id='86420',
        sender_external_id='97531', sender_nickname='账号昵称',
        sender_group_card='小李', first_seen_at=1_000_000,
    )


def _events() -> List[dict]:
    return list(reversed(event_store.search(kinds=['action_decision']).events))


_SPEAK = (
    '<decision action="speak" reasons="remembered_something"/>'
    '<say emotion="smile">刚想起来那家店今天不开</say>'
)


class TestSpeakProtocol:
    def test_speak_takes_no_target(self) -> None:
        """没有哪条消息是她在回的，所以不许有 targets/quote/length。"""
        head = DecisionHead(
            action='speak', target_message_ids=(), quote_message_id=None,
            reason_codes=('long_silence',),
        )
        assert head.action == 'speak'
        for kwargs in (
            {'target_message_ids': (1,)},
            {'quote_message_id': 1},
            {'length': 'brief'},
        ):
            base: dict[str, Any] = dict(
                action='speak', target_message_ids=(), quote_message_id=None,
                reason_codes=('long_silence',),
            )
            base.update(kwargs)
            with pytest.raises(IllegalActionError):
                DecisionHead(**base)

    def test_speak_reasons_are_their_own_domain(self) -> None:
        """「有人叫我所以我接」和「没人叫我但我想说」是两种动机，不许混域。"""
        with pytest.raises(IllegalActionError):
            DecisionHead(
                action='speak', target_message_ids=(), quote_message_id=None,
                reason_codes=('direct_question',),
            )
        with pytest.raises(IllegalActionError):
            DecisionHead(
                action='reply', target_message_ids=(1,), quote_message_id=None,
                reason_codes=('long_silence',), length='brief',
            )
        for code in SPEAK_REASON_CODES:
            assert DecisionHead(
                action='speak', target_message_ids=(), quote_message_id=None,
                reason_codes=(code,),
            ).reason_codes == (code,)

    def test_speak_requires_visible_body(self) -> None:
        head = DecisionHead(
            action='speak', target_message_ids=(), quote_message_id=None,
            reason_codes=('long_silence',),
        )
        with pytest.raises(IllegalActionError):
            head.to_decision('')

    def test_speak_is_not_a_parallel_wake_path(self) -> None:
        """它只出现在已有的 DELIBERATE 候选里，不给 FORCE、不自成一态。"""
        caps = PlatformCapabilities()
        assert 'speak' in available_actions('group', 'deliberate', caps, allow_speak=True)
        assert 'speak' not in available_actions('group', 'deliberate', caps, allow_speak=False)
        # @必回场景的契约是回答对方，不是换个话头。
        assert 'speak' not in available_actions('group', 'force', caps, allow_speak=True)
        assert 'drop' and not available_actions('group', 'drop', caps, allow_speak=True)


class TestSpeakDelivery:
    async def _run(self, db, provider, broker, *, speak: bool = True):
        chat = ChatService(
            db, provider, None, None, _noop, cfg=_config(speak=speak), broker=broker,
        )
        context = _group(chat._registry)
        await chat.send(InboundMessage(
            text='月璃你看', context=context, external_message_id='9001',
        ))
        await chat._tick()
        await chat._inflight[context.stream.id].task
        return chat, context

    async def test_speak_delivers_without_quote(self, db) -> None:
        broker = _Broker()
        chat, context = await self._run(db, _ScriptedProvider([[_SPEAK]]), broker)

        assert len(broker.dispatched) == 1
        assert broker.dispatched[0].segments == ['刚想起来那家店今天不开']
        # 没有目标消息，自然不挂引用。
        assert broker.dispatched[0].quote_external_message_id is None
        assert _events()[-1]['eventStatus'] == 'committed'
        assert _events()[-1]['decision']['action'] == 'speak'
        assert _events()[-1]['decision']['targetMessageIds'] == []
        # 与 reply 同款：可见正文进助手历史。
        stored = [m.content for m in chat.memory.working_memory(context.stream.id, 20)]
        assert any('刚想起来那家店今天不开' in item for item in stored)

    async def test_switch_off_removes_the_option(self, db) -> None:
        broker = _Broker()
        await self._run(db, _ScriptedProvider([[_SPEAK]]), broker, speak=False)

        assert broker.dispatched == []
        assert _events()[-1]['eventStatus'] == 'illegal_action'

    async def test_prompt_discourages_empty_small_talk(self, db) -> None:
        """效果不好的根因是没料硬开口，提示词必须把这条压住。"""
        provider = _ScriptedProvider([[_SPEAK]])
        await self._run(db, provider, _Broker())

        system = provider.messages[0][0]['content']
        assert 'action="speak"' in system
        assert '绝大多数时候都应选择 silent' in system
        assert '无实际内容的搭话' in system


class TestConsoleLabels:
    """控制台必须认得全部协议枚举，漏一个就在终端里显示成英文原文。"""

    def test_every_action_status_and_reason_has_a_label(self) -> None:
        from src.core.agent.action_protocol import (
            ALL_ACTIONS, ALL_REASON_CODES, EventStatus,
        )
        from src.core.common.log_display import VALUE_LABELS

        assert [a for a in sorted(ALL_ACTIONS) if a not in VALUE_LABELS] == []
        assert [s for s in sorted(EventStatus.__args__) if s not in VALUE_LABELS] == []
        assert [c for c in sorted(ALL_REASON_CODES) if c not in VALUE_LABELS] == []
