"""wait 与 poke 两个动作的验收。

wait 的核心是**这批消息算不算处理过**：silent 消费批次，wait 把批次退回缓冲、
把累计器加回去、并在没有新消息之前不再重开回合。poke 与 react 同构，只是目标
空间从「某条消息」变成「某个人」。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, List

import pytest

from src.core.agent.action_protocol import (
    WAIT_REASON_CODES,
    IllegalActionError,
    PlatformCapabilities,
    available_actions,
)
from src.core.agent.conversation_gate import (
    POKE_SIGNAL_LIMIT,
    GateRequest,
    decide_disposition,
)
from src.core.config.schema import Config
from src.core.observe.store import event_store
from src.core.platform_io.types import (
    DeliveryReceipt,
    InboundMessage,
    OutboundMessage,
    OutboundPoke,
)
from src.core.services.chat import ChatService, _WaitHold


class _ScriptedProvider:
    def __init__(self, scripts: List[List[str]]) -> None:
        self.scripts = scripts
        self.calls = 0

    async def stream(self, messages=None, **_kwargs: Any) -> AsyncIterator[dict[str, str]]:
        index = self.calls
        self.calls += 1
        for text in self.scripts[min(index, len(self.scripts) - 1)]:
            yield {'text': text}


class _RecordingBroker:
    def __init__(self) -> None:
        self.dispatched: List[OutboundMessage] = []
        self.pokes: List[OutboundPoke] = []

    async def dispatch(self, message: OutboundMessage) -> DeliveryReceipt:
        self.dispatched.append(message)
        return DeliveryReceipt(
            platform=message.stream.platform,
            stream_id=message.stream.id,
            external_message_ids=['x'],
        )

    async def dispatch_poke(self, poke: OutboundPoke) -> DeliveryReceipt:
        self.pokes.append(poke)
        return DeliveryReceipt(
            platform=poke.stream.platform,
            stream_id=poke.stream.id,
            external_message_ids=[],
        )


async def _noop(_channel: str, _payload: Any, _stream_id: int) -> None:
    return None


def _config(*, pokes: bool = False) -> Config:
    config = Config()
    config.bot.name = '月璃'
    config.conversation_agent.mode = 'enabled'
    config.conversation_agent.max_cognitive_rounds = 0
    config.group_chat.pokes_enabled = pokes
    config.group_chat.scene_refresh_messages = 0
    return config


def _group(registry):
    return registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id='86420',
        sender_external_id='97531',
        sender_nickname='账号昵称',
        sender_group_card='小李',
        first_seen_at=1_000_000,
    )


def _direct(registry):
    return registry.resolve_inbound(
        platform='qq',
        stream_kind='direct',
        stream_external_id='97531',
        sender_external_id='97531',
        sender_nickname='账号昵称',
        sender_group_card='',
        first_seen_at=1_000_000,
    )


def _events() -> List[dict]:
    return list(reversed(event_store.search(kinds=['action_decision']).events))


_WAIT = '<decision action="wait" reasons="unfinished_thought"/>'
_REPLY = (
    '<decision action="reply" targets="1" reasons="direct_question" length="brief"/>'
    '<say emotion="normal">说吧。</say>'
)
_POKE = '<decision action="poke" targets="1" reasons="relationship_impulse"/>'


class TestWaitProtocol:
    def test_wait_reasons_are_their_own_domain(self) -> None:
        """等待与沉默分域：混用会让账本分不出她是放弃了还是在等。"""
        from src.core.agent.action_protocol import DecisionHead

        head = DecisionHead(
            action='wait',
            target_message_ids=(),
            quote_message_id=None,
            reason_codes=('unfinished_thought',),
        )
        assert head.action == 'wait'

        with pytest.raises(IllegalActionError):
            DecisionHead(
                action='wait',
                target_message_ids=(),
                quote_message_id=None,
                reason_codes=('no_new_value',),   # 沉默域的码
            )
        with pytest.raises(IllegalActionError):
            DecisionHead(
                action='silent',
                target_message_ids=(),
                quote_message_id=None,
                reason_codes=('unfinished_thought',),   # 等待域的码
            )

    def test_wait_takes_no_target(self) -> None:
        from src.core.agent.action_protocol import DecisionHead

        with pytest.raises(IllegalActionError):
            DecisionHead(
                action='wait',
                target_message_ids=(1,),
                quote_message_id=None,
                reason_codes=('unfinished_thought',),
            )

    def test_wait_only_when_allowed(self) -> None:
        """约束写在动作空间：等过一次之后 wait 直接不在集合里。"""
        caps = PlatformCapabilities()
        assert 'wait' in available_actions('group', 'deliberate', caps, allow_wait=True)
        assert 'wait' not in available_actions('group', 'deliberate', caps, allow_wait=False)
        # 私聊门控恒为必回，但仍可等对方把话说完（服务层有超时兜底）；
        # 桌面是即时交互界面，群聊 @必回是明确点名，都不允许等。
        assert 'wait' in available_actions('direct', 'force', caps, allow_wait=True)
        assert 'wait' not in available_actions('desktop', 'deliberate', caps, allow_wait=True)
        assert 'wait' not in available_actions('group', 'force', caps, allow_wait=True)

    def test_every_wait_reason_parses(self) -> None:
        from src.core.agent.action_protocol import DecisionHead

        for code in WAIT_REASON_CODES:
            assert DecisionHead(
                action='wait',
                target_message_ids=(),
                quote_message_id=None,
                reason_codes=(code,),
            ).reason_codes == (code,)


class TestWaitBehaviour:
    async def _chat(self, db, provider) -> ChatService:
        return ChatService(
            db, provider, None, None, _noop,
            cfg=_config(), broker=_RecordingBroker(),
        )

    async def test_wait_returns_batch_and_holds_stream(self, db) -> None:
        provider = _ScriptedProvider([[_WAIT]])
        chat = await self._chat(db, provider)
        context = _group(chat._registry)
        await chat.send(InboundMessage(text='月璃我跟你讲', context=context))
        await chat._tick()
        await chat._inflight[context.stream.id].task

        stream_id = context.stream.id
        # 批次退回缓冲，累计器加回去，等待标记就位。
        assert len(chat._buffers.get(stream_id, [])) == 1
        assert chat._extended_pending.get(stream_id) == 1
        assert stream_id in chat._waiting
        assert _events()[-1]['eventStatus'] == 'committed'
        assert _events()[-1]['decision']['action'] == 'wait'

        # 没有新消息时不再重开回合，否则轮询一到就把同一批重问一遍模型。
        await chat._tick()
        assert provider.calls == 1

    async def test_new_message_releases_the_wait_and_forbids_waiting_again(self, db) -> None:
        provider = _ScriptedProvider([[_WAIT], [_REPLY]])
        chat = await self._chat(db, provider)
        context = _group(chat._registry)
        await chat.send(InboundMessage(text='月璃我跟你讲', context=context))
        await chat._tick()
        await chat._inflight[context.stream.id].task

        await chat.send(InboundMessage(text='就是那个事', context=context))
        await chat._tick()
        await chat._inflight[context.stream.id].task

        assert provider.calls == 2
        assert context.stream.id not in chat._waiting
        # 第二轮已经等过一次，动作集里不能再有 wait。
        assert 'wait' not in _events()[-1]['gate']['availableActions']
        assert _events()[-1]['decision']['action'] == 'reply'

    async def test_wait_produces_no_visible_output(self, db) -> None:
        provider = _ScriptedProvider([[_WAIT]])
        broker = _RecordingBroker()
        chat = ChatService(
            db, provider, None, None, _noop, cfg=_config(), broker=broker,
        )
        context = _group(chat._registry)
        await chat.send(InboundMessage(text='月璃我跟你讲', context=context))
        await chat._tick()
        await chat._inflight[context.stream.id].task

        assert broker.dispatched == []
        stored = [m.role for m in chat.memory.working_memory(context.stream.id, 20)]
        assert 'assistant' not in stored

    async def test_direct_wait_times_out_and_forces_a_reply(self, db) -> None:
        """私聊等待有界：超时仍无下文时强制重开回合，那一轮没有 wait。"""
        provider = _ScriptedProvider([[_WAIT], [_REPLY]])
        chat = await self._chat(db, provider)
        context = _direct(chat._registry)
        await chat.send(InboundMessage(text='月璃我跟你讲', context=context))
        await chat._tick()
        await chat._inflight[context.stream.id].task
        assert provider.calls == 1
        assert context.stream.id in chat._waiting

        # 未超时：等待继续持有，轮询不重开回合。
        await chat._tick()
        assert provider.calls == 1

        # 把等待开始时刻拨回超时窗口之前，模拟十分钟也没有下文。
        hold = chat._waiting[context.stream.id]
        chat._waiting[context.stream.id] = _WaitHold(
            hold.watermark, hold.since - 11_000,
        )
        await chat._tick()
        await chat._inflight[context.stream.id].task

        assert provider.calls == 2
        assert context.stream.id not in chat._waiting
        assert 'wait' not in _events()[-1]['gate']['availableActions']
        assert _events()[-1]['decision']['action'] == 'reply'

    async def test_direct_wait_merges_follow_up_before_timeout(self, db) -> None:
        """超时之前到达的下文走既有合并路径：一批重判且不许再等。"""
        provider = _ScriptedProvider([[_WAIT], [_REPLY]])
        chat = await self._chat(db, provider)
        context = _direct(chat._registry)
        await chat.send(InboundMessage(text='月璃我跟你讲', context=context))
        await chat._tick()
        await chat._inflight[context.stream.id].task

        await chat.send(InboundMessage(text='就是那个事', context=context))
        await chat._tick()
        await chat._inflight[context.stream.id].task

        assert provider.calls == 2
        assert context.stream.id not in chat._waiting
        assert 'wait' not in _events()[-1]['gate']['availableActions']
        assert _events()[-1]['decision']['action'] == 'reply'


class TestPoke:
    async def _run(self, db, provider, broker, *, pokes: bool):
        chat = ChatService(
            db, provider, None, None, _noop,
            cfg=_config(pokes=pokes), broker=broker,
        )
        # 等价于真实适配器连接成功、探测到发包能力可用后的上报；未上报时
        # 戳一戳不进动作集，这正是能力门控要保证的行为。
        chat.set_platform_capabilities('qq', {'poke'})
        context = _group(chat._registry)
        await chat.send(InboundMessage(text='月璃在吗', context=context))
        await chat._tick()
        await chat._inflight[context.stream.id].task
        return chat, context

    async def test_default_is_off(self, db) -> None:
        """戳一戳会给对方推提醒，比贴表情吵，默认不开。"""
        chat = ChatService(
            db, _ScriptedProvider([[_REPLY]]), None, None, _noop,
            cfg=_config(), broker=_RecordingBroker(),
        )
        assert chat._poke_available(_group(chat._registry)) is False

    async def test_poke_resolves_target_sender(self, db) -> None:
        broker = _RecordingBroker()
        chat, context = await self._run(
            db, _ScriptedProvider([[_POKE]]), broker, pokes=True,
        )

        assert len(broker.pokes) == 1
        # 目标沿用消息编号，发送者由消息反查，不新开一套人物目标空间。
        assert broker.pokes[0].target_external_id == '97531'
        assert broker.dispatched == []
        stored = chat.memory.working_memory(context.stream.id, 20)
        own_actions = [m.content for m in stored if m.role == 'assistant']
        assert own_actions == ['[戳了戳 小李]']
        rendered = chat._history_for_context(
            context, stored, label_message_ids=True, flatten=True,
        )
        own_line = next(item['content'] for item in rendered if '戳了戳 小李' in item['content'])
        assert own_line.startswith('[我] ')
        assert _events()[-1]['eventStatus'] == 'committed'

    async def test_poke_without_capability_is_illegal(self, db) -> None:
        broker = _RecordingBroker()
        await self._run(db, _ScriptedProvider([[_POKE]]), broker, pokes=False)

        assert broker.pokes == []
        assert _events()[-1]['eventStatus'] == 'illegal_action'

    def test_parse_poke_requires_all_fields(self) -> None:
        from src.platforms.onebot11.backend import _parse_poke

        payload = {
            'channel': 'qq.poke',
            'stream_id': 3,
            'payload': {
                'streamKind': 'group',
                'streamExternalId': '86420',
                'targetExternalId': '97531',
            },
        }
        assert _parse_poke(payload).target_external_id == '97531'
        for missing in ('targetExternalId', 'streamExternalId'):
            broken = {**payload, 'payload': {**payload['payload'], missing: ''}}
            with pytest.raises(ValueError):
                _parse_poke(broken)


class Test私聊连戳不再打崩入站:
    """门控把私聊连戳判成 drop 之后，落库这一步必须撑得住。

    门控次序是有意的：poke_repeat 排在私聊 FORCE 之前（见
    conversation_gate.decide_disposition 的次序说明），所以私聊会拿到 drop。
    早先落库走的是群聊专用方法，开头就拒绝非群聊，于是对方连戳几下就能让
    /platform/inbound 抛 ValueError 返 500。这两条断言分别盯住那条链的两端。
    """

    def test_私聊连戳的门控结果是丢弃(self) -> None:
        """poke_repeat 先于私聊 FORCE 生效，这是设计次序，不是缺陷。"""
        result = decide_disposition(GateRequest(
            stream_kind='direct',
            mentioned_me=False,
            name_mentioned=False,
            asleep=False,
            at_mention_must_reply=True,
            replies_in_window=0,
            max_replies_in_window=3,
            poked_me=True,
            pokes_in_window=POKE_SIGNAL_LIMIT + 1,
        ))

        assert result.disposition == 'drop'
        assert result.reason_codes == ('poke_repeat',)

    async def test_私聊的静默消息照样落库(self, db) -> None:
        """非群聊出口写入不得抛异常，也不得走群聊专属的观察事件。"""
        chat = ChatService(
            db, _ScriptedProvider([['嗯']]), None, None, _noop,
            cfg=_config(), broker=_RecordingBroker(),
        )
        context = _direct(chat._registry)

        message_id = await chat.record_silent_inbound(
            InboundMessage(text='[戳了戳月璃]', context=context),
            'poke_repeat',
        )

        assert message_id > 0
        history = chat.memory.working_memory(context.stream.id, 10)
        assert any(item.content == '[戳了戳月璃]' for item in history), '静默消息必须进历史'
