"""表情回应（react）动作的接线验收。

覆盖三层：能力门控（开关可关、只在 QQ 群聊开启）、核心投递（走
``dispatch_reaction`` 而不是发消息、成功后写动作历史、缺平台编号如实失败），
以及适配器侧的报文校验与表情编号映射。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, List

import pytest

from src.core.agent.action_protocol import (
    REACTION_IDS,
    PlatformCapabilities,
    available_actions,
)
from src.core.config.schema import Config
from src.core.observe.store import event_store
from src.core.platform_io.driver import DeliveryError
from src.core.platform_io.types import (
    DeliveryReceipt,
    InboundMessage,
    OutboundMessage,
    OutboundReaction,
)
from src.core.services.chat import ChatService


class _ScriptedProvider:
    """按调用顺序产出脚本分片并保留实际请求的替身提供方。"""

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


class _RecordingBroker:
    """分别记录消息投递与表情回应的 broker 替身。"""

    def __init__(self) -> None:
        self.dispatched: List[OutboundMessage] = []
        self.reactions: List[OutboundReaction] = []

    async def dispatch(self, message: OutboundMessage) -> DeliveryReceipt:
        self.dispatched.append(message)
        return DeliveryReceipt(
            platform=message.stream.platform,
            stream_id=message.stream.id,
            external_message_ids=['fake-1'],
        )

    async def dispatch_reaction(self, reaction: OutboundReaction) -> DeliveryReceipt:
        self.reactions.append(reaction)
        return DeliveryReceipt(
            platform=reaction.stream.platform,
            stream_id=reaction.stream.id,
            external_message_ids=[],
        )


async def _noop(_channel: str, _payload: Any, _stream_id: int) -> None:
    return None


def _config(*, reactions: bool) -> Config:
    config = Config()
    config.bot.name = '月璃'
    config.conversation_agent.mode = 'enabled'
    config.conversation_agent.max_cognitive_rounds = 0
    config.group_chat.reactions_enabled = reactions
    return config


def _group_context(registry, external_id: str = '86420'):
    return registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id=external_id,
        sender_external_id='97531',
        sender_nickname='账号昵称',
        sender_group_card='小李',
        first_seen_at=1_000_000,
    )


def _action_events() -> List[dict]:
    return list(reversed(event_store.search(kinds=['action_decision']).events))


_REACT = '<decision action="react" targets="1" reaction="赞" reasons="natural_reaction"/>'


class TestCapabilityGate:
    def test_switch_off_closes_the_action(self, db) -> None:
        """关掉开关就彻底没有这个动作，提示词与动作集里都不出现。"""
        chat = ChatService(
            db, _ScriptedProvider([['x']]), None, None, _noop,
            cfg=_config(reactions=False), broker=_RecordingBroker(),
        )
        assert chat._react_available(_group_context(chat._registry)) is False

    def test_enabled_only_for_qq_group(self, db) -> None:
        chat = ChatService(
            db, _ScriptedProvider([['x']]), None, None, _noop,
            cfg=_config(reactions=True), broker=_RecordingBroker(),
        )
        chat.set_platform_capabilities('qq', {'reaction', 'poke'})
        assert chat._react_available(_group_context(chat._registry)) is True
        # 私聊两个人贴表情没有「让别人看见我在回应谁」的意义。
        direct = chat._registry.resolve_inbound(
            platform='qq',
            stream_kind='direct',
            stream_external_id='900000001',
            sender_external_id='900000001',
            sender_nickname='凌白',
            sender_group_card='',
            first_seen_at=1_000_000,
        )
        assert chat._react_available(direct) is False
        # 桌面根本没有这个协议动作。
        assert chat._react_available(chat.desktop_context) is False

    def test_action_space_opens_only_with_capability(self) -> None:
        without = available_actions('group', 'deliberate', PlatformCapabilities())
        assert 'react' not in without
        caps = PlatformCapabilities(react=True, available_reactions=REACTION_IDS)
        assert 'react' in available_actions('group', 'deliberate', caps)


class TestReactDelivery:
    async def _run_turn(self, db, provider, broker, *, external_id: str | None):
        chat = ChatService(
            db, provider, None, None, _noop,
            cfg=_config(reactions=True), broker=broker,
        )
        # 等价于真实适配器连接成功后的能力上报：没有上报时表情回应不进动作集。
        chat.set_platform_capabilities('qq', {'reaction'})
        context = _group_context(chat._registry)
        await chat.send(InboundMessage(
            text='月璃你看这个',
            context=context,
            external_message_id=external_id,
        ))
        await chat._tick()
        await chat._inflight[context.stream.id].task
        return chat, context

    async def test_react_goes_through_reaction_channel(self, db) -> None:
        """react 走 dispatch_reaction，不发消息，并在成功后写自己的动作历史。"""
        broker = _RecordingBroker()
        chat, context = await self._run_turn(
            db, _ScriptedProvider([[_REACT]]), broker, external_id='9001',
        )

        assert len(broker.reactions) == 1
        assert broker.reactions[0].target_external_message_id == '9001'
        assert broker.reactions[0].reaction == '赞'
        assert broker.dispatched == [], 'react 不应该同时发一条消息'
        stored = chat.memory.working_memory(context.stream.id, 20)
        own_actions = [m.content for m in stored if m.role == 'assistant']
        assert own_actions == ['[给消息 1 贴了个「赞」]']
        rendered = chat._history_for_context(
            context, stored, label_message_ids=True, flatten=True,
        )
        own_line = next(item['content'] for item in rendered if '贴了个「赞」' in item['content'])
        assert own_line.startswith('[我] ')
        assert _action_events()[-1]['eventStatus'] == 'committed'
        assert _action_events()[-1]['decision']['reaction'] == '赞'

    async def test_missing_external_id_fails_loudly(self, db) -> None:
        """没有平台编号就贴不了表情，如实记 delivery_failed，不改成发条消息。

        异常本身由回合任务的失败出口接住并记日志（M3.9 定的口径），因此这里断言
        的是可观测结果：没有贴出任何表情，账本上留下 delivery_failed。
        """
        broker = _RecordingBroker()
        await self._run_turn(
            db, _ScriptedProvider([[_REACT]]), broker, external_id=None,
        )

        assert broker.reactions == []
        assert broker.dispatched == []
        stored_count = db.execute(
            "SELECT COUNT(*) FROM messages WHERE role = 'assistant'",
        ).fetchone()[0]
        assert stored_count == 0
        assert _action_events()[-1]['eventStatus'] == 'delivery_failed'

    async def test_prompt_lists_available_reactions(self, db) -> None:
        provider = _ScriptedProvider([[_REACT]])
        await self._run_turn(db, provider, _RecordingBroker(), external_id='9001')

        system = provider.messages[0][0]['content']
        assert 'action="react"' in system
        assert '赞 / 笑哭' in system

    async def test_reaction_outside_platform_set_is_illegal(self, db) -> None:
        """平台能力集是封闭的：写一个不在集合里的反应按协议失败处理。"""
        broker = _RecordingBroker()
        await self._run_turn(
            db,
            _ScriptedProvider([[
                '<decision action="react" targets="1" reaction="比心" '
                'reasons="natural_reaction"/>'
            ]]),
            broker,
            external_id='9001',
        )

        assert broker.reactions == []
        assert _action_events()[-1]['eventStatus'] == 'illegal_action'


class TestAdapterSide:
    def test_reaction_ids_are_derived_from_the_platform_table(self) -> None:
        """映射由平台表按名反查派生，「名字对但编号错」在结构上不可能发生。"""
        from src.platforms.onebot11.qq_faces import FACE_NAMES, face_id_by_name
        from src.platforms.onebot11.segments import REACTION_EMOJI_IDS, reaction_emoji_id

        assert set(REACTION_IDS) == set(REACTION_EMOJI_IDS)
        for name, face_id in REACTION_EMOJI_IDS.items():
            # 每个语义名都必须是平台表里真实存在的表情名，且编号来自那张表。
            assert FACE_NAMES[face_id] == name
            assert face_id_by_name(name) == face_id
        assert reaction_emoji_id('赞') == REACTION_EMOJI_IDS['赞']

    def test_unknown_face_name_blows_up_at_lookup(self) -> None:
        """名字对不上必须当场炸，不做近似匹配——贴错表情是不报错的错。"""
        from src.platforms.onebot11.qq_faces import face_id_by_name

        with pytest.raises(KeyError):
            face_id_by_name('无语')   # 这个名字平台表里没有，第一版曾错用过

    def test_duplicate_names_resolve_to_the_small_face_id(self) -> None:
        """十组重名对应「小黄脸 / Unicode」两套编号，按名反查取前者。"""
        from src.platforms.onebot11.qq_faces import face_id_by_name

        assert face_id_by_name('大哭') == '9'
        assert face_id_by_name('庆祝') == '320'

    def test_inbound_face_segments_render_with_names(self) -> None:
        """入站表情要还原成具体名字，否则她分不出「赞」和「裂开」。"""
        from src.platforms.onebot11.segments import segment_to_text

        assert segment_to_text({'type': 'face', 'data': {'id': '76'}}) == '[表情：赞]'
        assert segment_to_text({'type': 'face', 'data': {'id': '357'}}) == '[表情：裂开]'
        # 新增的未知编号保留无名占位：真的不知道就不许编一个名字出来。
        assert segment_to_text({'type': 'face', 'data': {'id': '999999'}}) == '[表情]'

    def test_unknown_reaction_raises(self) -> None:
        from src.platforms.onebot11.segments import reaction_emoji_id

        with pytest.raises(ValueError):
            reaction_emoji_id('不存在的反应')

    def test_parse_reaction_requires_all_fields(self) -> None:
        from src.platforms.onebot11.backend import _parse_reaction

        payload = {
            'channel': 'qq.react',
            'stream_id': 3,
            'payload': {
                'streamKind': 'group',
                'streamExternalId': '86420',
                'targetExternalMessageId': '9001',
                'reaction': '赞',
            },
        }
        parsed = _parse_reaction(payload)
        assert parsed.target_external_message_id == '9001'
        assert parsed.reaction == '赞'

        for missing in ('targetExternalMessageId', 'reaction', 'streamExternalId'):
            broken = {**payload, 'payload': {**payload['payload'], missing: ''}}
            with pytest.raises(ValueError):
                _parse_reaction(broken)


class TestDriver:
    async def test_qq_driver_pushes_react_channel(self) -> None:
        from src.core.platform_io.drivers.qq_ws import QqWebSocketDriver
        from src.core.platform_io.types import StreamRef

        pushed: List[tuple[int, str, Any]] = []

        async def push(stream_id: int, channel: str, payload: Any) -> int:
            pushed.append((stream_id, channel, payload))
            return 1

        stream = StreamRef(id=3, platform='qq', kind='group', external_id='86420')
        await QqWebSocketDriver(push).react(OutboundReaction(
            stream=stream, target_external_message_id='9001', reaction='赞',
        ))

        assert pushed[0][1] == 'qq.react'
        assert pushed[0][2]['targetExternalMessageId'] == '9001'

    async def test_driver_without_support_refuses(self) -> None:
        """能力判定说可用、驱动却不支持，属于不一致，必须当场报错。"""
        from src.core.platform_io.driver import PlatformDriver
        from src.core.platform_io.types import StreamRef

        class _Bare(PlatformDriver):
            platform = 'bare'

            async def start(self) -> None:
                return None

            async def stop(self) -> None:
                return None

            async def send(self, message: OutboundMessage) -> DeliveryReceipt:
                raise AssertionError('本用例不应发送消息')

        stream = StreamRef(id=3, platform='bare', kind='group', external_id='x')
        with pytest.raises(DeliveryError):
            await _Bare().react(OutboundReaction(
                stream=stream, target_external_message_id='9001', reaction='赞',
            ))
