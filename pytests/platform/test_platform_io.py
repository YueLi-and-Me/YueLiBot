"""验证平台出站契约和 PlatformBroker 的回复门控行为。

本模块覆盖目标出口、睡眠状态、称呼命中和投递失败的观察记录，
确保平台驱动只接收符合当前会话规则的消息。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, List

import pytest

from src.core.config.schema import GroupChatConfig
from src.core.platform_io.broker import PlatformBroker
from src.core.platform_io.driver import DeliveryError, PlatformDriver
from src.core.platform_io.types import DeliveryReceipt, OutboundMessage, StreamRef

class _Driver(PlatformDriver):
    platform = 'qq'

    def __init__(self) -> None:
        self.messages: List[OutboundMessage] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        self.messages.append(message)
        return DeliveryReceipt(
            platform=self.platform,
            stream_id=message.stream.id,
            external_message_ids=['message-1'],
        )


def _stream(stream_id: int = 2) -> StreamRef:
    return StreamRef(id=stream_id, platform='qq', kind='group', external_id='group-1')


async def test_broker_unicasts_to_the_registered_stream_driver() -> None:
    broker = PlatformBroker()
    driver = _Driver()
    message = OutboundMessage(stream=_stream(), segments=['第一句', '第二句'])
    broker.register(message.stream.id, driver)

    receipt = await broker.dispatch(message)

    assert driver.messages == [message]
    assert receipt.stream_id == message.stream.id
    assert receipt.external_message_ids == ['message-1']


async def test_broker_rejects_an_unregistered_stream() -> None:
    with pytest.raises(DeliveryError, match='没有已注册'):
        await PlatformBroker().dispatch(OutboundMessage(stream=_stream(), segments=['不会丢失']))


def test_broker_rejects_duplicate_stream_registration() -> None:
    broker = PlatformBroker()
    broker.register(2, _Driver())

    with pytest.raises(ValueError, match='已注册'):
        broker.register(2, _Driver())


class _Memory:
    def __init__(self, reply_count: int) -> None:
        self._reply_count = reply_count

    def assistant_reply_count_since(self, _stream_id: int, _since: int) -> int:
        return self._reply_count

    def last_assistant_reply_at(self, _stream_id: int) -> int | None:
        return None


class _Chat:
    def __init__(
        self,
        reply_count: int = 0,
        asleep: bool = False,
        at_mention_must_reply: bool = True,
    ) -> None:
        self.memory = _Memory(reply_count)
        self._sleep = SimpleNamespace(asleep=asleep)
        self.at_mention_must_reply = at_mention_must_reply
        self.name_mention_probability = 1.0
        self.inbound: List[Any] = []
        self.observed: List[Any] = []
        # 入口门控读取的跟进事实；用例默认「没放弃过、话题已走远」，
        # 需要验证跟进口径的用例自行改写这两个属性。
        self.declined = False
        self.topic_hers = False

    def bot_names(self) -> tuple[str, ...]:
        return ('配置名称', '配置别名')

    def follow_up_declined(self, stream_id: int) -> bool:
        return self.declined

    def topic_still_hers(self, stream_id: int, last_bot_reply_at: int) -> bool:
        return self.topic_hers

    def current_sleep(self) -> SimpleNamespace:
        return self._sleep

    async def send(self, inbound: Any) -> int:
        self.inbound.append(inbound)
        return 42

    def record_group_observation(self, inbound: Any, reason: str = '') -> int:
        self.observed.append((inbound, reason))
        return 1


def _inbound_body(**overrides: Any) -> Any:
    from src.core.api.http import PlatformInboundBody

    fields = {
        'platform': 'qq',
        'streamKind': 'group',
        'streamExternalId': 'group-2',
        'senderExternalId': 'sender-9',
        'senderNickname': '账号昵称',
        'senderGroupCard': '小李',
        'text': '在吗',
        'mentionedMe': True,
        'externalMessageId': 'message-8',
    }
    fields.update(overrides)
    return PlatformInboundBody(**fields)


async def test_platform_inbound_merges_at_chat_service_and_keeps_full_context(db) -> None:
    from src.core.api.http import platform_inbound
    from src.core.api.state import app_state
    from src.core.platform_io.registry import StreamRegistry

    original_chat = app_state.chat
    original_registry = app_state.registry
    original_group_chat = app_state.group_chat_config
    chat = _Chat()
    try:
        app_state.chat = chat
        app_state.registry = StreamRegistry(db)
        app_state.group_chat_config = GroupChatConfig()

        response = await platform_inbound(_inbound_body())

        assert json.loads(response.body) == {
            'streamId': 2,
            'accepted': True,
            'reason': 'at_mention_must_reply',
        }
        assert len(chat.inbound) == 1
        assert chat.inbound[0].context.stream.kind == 'group'
        assert chat.inbound[0].mentioned_me is True
        assert chat.inbound[0].external_message_id == 'message-8'
    finally:
        app_state.chat = original_chat
        app_state.registry = original_registry
        app_state.group_chat_config = original_group_chat


async def test_platform_inbound_rejects_unmentioned_or_rate_limited_groups(db) -> None:
    from src.core.api.http import platform_inbound
    from src.core.api.state import app_state
    from src.core.platform_io.registry import StreamRegistry

    original_chat = app_state.chat
    original_registry = app_state.registry
    original_group_chat = app_state.group_chat_config
    chat = _Chat(reply_count=3, at_mention_must_reply=False)
    try:
        app_state.chat = chat
        app_state.registry = StreamRegistry(db)
        app_state.group_chat_config = GroupChatConfig()

        # 频率硬上限只约束「没人点名的自发参与」，因此这里必须用一条既没有 @
        # 也没有称呼的消息；被 @ 或被叫名字时越过上限由门控用例单独覆盖。
        response = await platform_inbound(_inbound_body(mentionedMe=False))

        assert json.loads(response.body)['accepted'] is False
        assert json.loads(response.body)['reason'] == 'rate_limited'
        assert chat.inbound == []
        assert len(chat.observed) == 1
        # 拦截理由要一路传到观察记录，控制台和面板才说得清为什么没回
        assert chat.observed[0][1] == 'rate_limited'
    finally:
        app_state.chat = original_chat
        app_state.registry = original_registry
        app_state.group_chat_config = original_group_chat


async def test_reply_gate_does_not_draw_or_report_unused_probability(db, monkeypatch) -> None:
    import random

    import src.core.api.http as http_module
    from src.core.api.state import app_state
    from src.core.observe.store import event_store
    from src.core.platform_io.registry import StreamRegistry

    original_chat = app_state.chat
    original_registry = app_state.registry
    original_group_chat = app_state.group_chat_config
    chat = _Chat()
    random_calls = 0

    def count_random() -> float:
        nonlocal random_calls
        random_calls += 1
        return 0.5

    monkeypatch.setattr(random, 'random', count_random)
    try:
        app_state.chat = chat
        app_state.registry = StreamRegistry(db)
        app_state.group_chat_config = GroupChatConfig()
        await http_module.platform_inbound(_inbound_body(
            text=f'{chat.bot_names()[0]}，在吗',
            mentionedMe=False,
        ))
    finally:
        app_state.chat = original_chat
        app_state.registry = original_registry
        app_state.group_chat_config = original_group_chat

    gate = event_store.search(kinds=['reply_gate'], limit=1).events[0]
    assert random_calls == 0
    assert 'probabilityDraw' not in gate
    assert 'nameMentionProbability' not in gate
    assert gate['disposition'] == 'deliberate'
    assert gate['reasonCodes'] == ['name_mention']
    assert gate['reason'] == 'name_mention'


def test_gate_decision_carries_every_input_it_judged_on() -> None:
    """决策结果必须包含睡眠状态、称呼命中和已回复次数等全部输入。"""
    from src.core.agent.conversation_gate import GateRequest, decide_disposition

    result = decide_disposition(GateRequest(
        stream_kind='group',
        mentioned_me=False,
        name_mentioned=True,
        asleep=False,
        at_mention_must_reply=True,
        replies_in_window=1,
        max_replies_in_window=3,
        last_bot_reply_elapsed_ms=0,
    ))

    # 距上一条回复足够近，自然回应窗口与称呼两个注意力信号同时命中。
    assert result.disposition == 'deliberate'
    assert result.reason_codes == ('name_mention', 'natural_reply_window')
    assert result.as_trace() == {
        'disposition': 'deliberate',
        'reasonCodes': ['name_mention', 'natural_reply_window'],
    }


def test_gate_records_sleep_even_when_name_was_called() -> None:
    from src.core.agent.conversation_gate import GateRequest, decide_disposition

    result = decide_disposition(GateRequest(
        stream_kind='group',
        mentioned_me=False,
        name_mentioned=True,
        asleep=True,
        at_mention_must_reply=True,
        replies_in_window=0,
        max_replies_in_window=3,
    ))

    assert result.reason_codes == ('asleep',)
    # 称呼命中与睡眠状态是独立条件，入口处仍会把称呼命中写入 reply_gate 事件。
    assert result.disposition == 'drop'
