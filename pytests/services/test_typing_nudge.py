"""输入状态催促的触发条件验收。"""

from __future__ import annotations

from typing import Any, AsyncIterator, List

import asyncio
import json
import re

from src.core.api.http import PlatformTypingBody, platform_typing
from src.core.api.state import app_state
from src.core.awareness.sleep import SleepState
from src.core.config.schema import Config
from src.core.platform_io.broker import PlatformBroker
from src.core.platform_io.drivers.qq_ws import QqWebSocketDriver
from src.core.platform_io.registry import StreamRegistry
from src.core.platform_io.types import DeliveryReceipt, InboundMessage, OutboundMessage
from src.core.services.chat import ChatService
import src.core.memory.store as store_module
# 补丁必须打在实现模块 chat.service 上而不是 chat 包上：ChatService 的代码在
# service 的命名空间里解析 current_time 等名字，改包的属性对它不生效。
import src.core.services.chat.follow_up as follow_up_module
import src.core.services.chat.service as chat_module


class _DecisionProvider:
    """按脚本选择 reply 或 silent 的 Conversation Agent 模型替身。"""

    def __init__(self, actions: List[str] | None = None) -> None:
        self.actions = list(actions or ['reply'])
        self.calls: List[List[dict[str, Any]]] = []

    async def stream(
        self,
        messages: List[dict[str, Any]] | None = None,
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, str]]:
        self.calls.append(messages or [])
        action = self.actions.pop(0) if self.actions else 'reply'
        if action == 'silent':
            yield {'text': '<decision action="silent" reasons="topic_closed"/>'}
            return
        system = str((messages or [{}])[0].get('content', ''))
        match = re.search(r'<decision action="reply" targets="(\d+)"', system)
        assert match is not None
        yield {
            'text': (
                f'<decision action="reply" targets="{match.group(1)}" '
                'reasons="topic_continuation" length="brief"/>'
                '<say emotion="pout">快说呀，我还等着呢。</say>'
            ),
        }


class _SceneProvider:
    """固定返回当前话题与气氛的独立情景分析 Agent 替身。"""

    def __init__(self) -> None:
        self.calls: List[List[dict[str, Any]]] = []

    async def stream(
        self,
        messages: List[dict[str, Any]] | None = None,
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, str]]:
        self.calls.append(messages or [])
        yield {'text': '{"topic":"还在商量刚才的事","atmosphere":"平淡"}'}


class _FakeBroker:
    def __init__(self) -> None:
        self.dispatched: List[OutboundMessage] = []

    async def dispatch(self, message: OutboundMessage) -> DeliveryReceipt:
        self.dispatched.append(message)
        return DeliveryReceipt(
            platform=message.stream.platform,
            stream_id=message.stream.id,
            external_message_ids=['fake-1'],
        )


SILENCE_MS = int(Config().typing.nudge.peer_silence_minutes * 60_000)
MAX_NUDGES = Config().typing.nudge.max_per_silence


async def _noop(_channel: str, _payload: Any, _stream_id: int) -> None:
    return None


def _direct_context(registry: StreamRegistry):
    return registry.resolve_inbound(
        platform='qq',
        stream_kind='direct',
        stream_external_id='24680',
        sender_external_id='24680',
        sender_nickname='凌白',
        sender_group_card='',
        first_seen_at=1_000_000,
    )


def _service(
    db,
    decision: _DecisionProvider | None = None,
    broker: _FakeBroker | None = None,
    scene: _SceneProvider | None = None,
) -> ChatService:
    config = Config()
    config.bot.name = '月璃'
    config.conversation_agent.mode = 'enabled'
    return ChatService(
        db,
        decision,
        None,
        scene or _SceneProvider(),
        _noop,
        cfg=config,
        broker=broker or _FakeBroker(),
    )


def _freeze(monkeypatch, clock: dict) -> None:
    """冻结聊天服务、私聊跟进与记忆层共同使用的时钟。

    三处都是 ``from ... import now as current_time`` 的绑定式导入，各自持有独立的
    函数引用，打补丁必须逐个模块打：漏掉任何一处，那一处就仍读真实时间，助手消息
    的时间戳与静默时长计算随即失真，表现为定时追问不触发（``proactive.calls`` 为空）。
    ``follow_up`` 这一处是 ChatService 拆包后新增的——跟进逻辑从 ``service`` 搬进
    ``chat.follow_up`` 时带走了它自己的绑定。
    """
    monkeypatch.setattr(chat_module, 'current_time', lambda: clock['now'])
    monkeypatch.setattr(follow_up_module, 'current_time', lambda: clock['now'])
    monkeypatch.setattr(store_module, 'current_time', lambda: clock['now'])


def _silence(chat: ChatService, stream_id: int, elapsed_ms: int, now: int) -> None:
    """伪造「她在 elapsed_ms 之前说过话，之后对方没再开口」的历史。"""
    context = _direct_context(chat._registry)
    chat.memory.append_message(stream_id, context.person.id, 'user', '那然后呢？')
    chat.memory.append_message(stream_id, None, 'assistant', '<say>在的</say>')
    chat.memory._db.execute(
        "UPDATE messages SET created_at = ? WHERE role = 'assistant' AND stream_id = ?",
        (now - elapsed_ms, stream_id),
    )
    chat.memory._db.execute(
        "UPDATE messages SET created_at = ? WHERE role = 'user' AND stream_id = ?",
        (now - elapsed_ms - 1_000, stream_id),
    )


async def test_recent_reply_does_not_trigger_a_nudge(db, monkeypatch) -> None:
    """一来一回的正常节奏里看到对方打字就开口，是监控不是聊天。"""
    now = 1_700_000_000_000
    _freeze(monkeypatch, {'now': now})
    proactive = _DecisionProvider()
    chat = _service(db, proactive)
    context = _direct_context(chat._registry)
    _silence(chat, context.stream.id, SILENCE_MS - 1_000, now)

    assert await chat.note_peer_typing(context) is False
    assert proactive.calls == []


async def test_long_silence_then_typing_triggers_one_nudge(db, monkeypatch) -> None:
    """她说完后对方久久不回，直到看见对方开始打字，才值得催一句。"""
    now = 1_700_000_000_000
    _freeze(monkeypatch, {'now': now})
    proactive = _DecisionProvider()
    scene = _SceneProvider()
    broker = _FakeBroker()
    chat = _service(db, proactive, broker, scene)
    context = _direct_context(chat._registry)
    _silence(chat, context.stream.id, SILENCE_MS + 60_000, now)

    assert await chat.note_peer_typing(context) is True
    assert len(proactive.calls) == 1
    assert len(scene.calls) == 1
    assert len(broker.dispatched) == 1
    # 情景分析 Agent 先看到明确的双方标签；决策器随后读到它产出的场景块。
    assert '凌白: 那然后呢？' in scene.calls[0][0]['content']
    assert '月璃: 在的' in scene.calls[0][0]['content']
    situation = proactive.calls[0][0]['content']
    assert '# 私聊情景分析结果' in situation
    assert '还在商量刚才的事' in situation
    # 情境只给事实，语气交给模型；协议词汇不许泄漏到她眼前。
    assert '他一直没回' in situation
    assert '开始打字' in situation
    assert 'input_status' not in situation


async def test_typing_opportunity_can_choose_silent_without_dispatch(
    db,
    monkeypatch,
) -> None:
    """达到输入阈值也不是必发；决策器认为话题已结束时保持沉默且不计次。"""
    now = 1_700_000_000_000
    _freeze(monkeypatch, {'now': now})
    decision = _DecisionProvider(['silent'])
    scene = _SceneProvider()
    broker = _FakeBroker()
    chat = _service(db, decision, broker, scene)
    context = _direct_context(chat._registry)
    _silence(chat, context.stream.id, SILENCE_MS + 60_000, now)

    assert await chat.note_peer_typing(context) is False
    assert len(scene.calls) == 1
    assert len(decision.calls) == 1
    assert broker.dispatched == []
    assert context.stream.id not in chat._typing_nudges
    # 即使没有正常运行期的定时状态（例如刚重启），连续输入通知也不能重新抽决策。
    assert await chat.note_peer_typing(context) is False
    assert len(scene.calls) == 1
    assert len(decision.calls) == 1


async def test_a_nudge_itself_restarts_the_silence_window(db, monkeypatch) -> None:
    """催完立刻再看到打字不会再催：她刚说完，静默重新开始计时。"""
    clock = {'now': 1_700_000_000_000}
    _freeze(monkeypatch, clock)
    proactive = _DecisionProvider()
    chat = _service(db, proactive)
    context = _direct_context(chat._registry)
    _silence(chat, context.stream.id, SILENCE_MS + 60_000, clock['now'])

    assert await chat.note_peer_typing(context) is True
    assert await chat.note_peer_typing(context) is False
    assert len(proactive.calls) == 1


async def test_nudges_stop_after_the_cap(db, monkeypatch) -> None:
    """催到上限就闭嘴：再催下去就从「等急了」变成缠人。"""
    clock = {'now': 1_700_000_000_000}
    _freeze(monkeypatch, clock)
    proactive = _DecisionProvider()
    chat = _service(db, proactive)
    context = _direct_context(chat._registry)
    _silence(chat, context.stream.id, SILENCE_MS + 60_000, clock['now'])

    for _ in range(MAX_NUDGES):
        assert await chat.note_peer_typing(context) is True
        # 每次催完都要重新熬过完整的静默窗口，才轮到下一次。
        clock['now'] += SILENCE_MS + 60_000
    assert await chat.note_peer_typing(context) is False
    assert len(proactive.calls) == MAX_NUDGES
    # 第二次的情境必须带上「已经催过」，她才可能换个说法而不是复读。
    assert '已经催过 1 次' in proactive.calls[1][0]['content']


async def test_peer_reply_clears_the_nudge_budget(db, monkeypatch) -> None:
    """对方一开口就算静默结束，下一段静默重新计数。"""
    now = 1_700_000_000_000
    _freeze(monkeypatch, {'now': now})
    chat = _service(db, _DecisionProvider())
    context = _direct_context(chat._registry)
    chat._typing_nudges[context.stream.id] = MAX_NUDGES

    await chat.send(InboundMessage(text='来了来了', context=context))

    assert context.stream.id not in chat._typing_nudges


async def test_peer_already_replied_is_not_a_silence(db, monkeypatch) -> None:
    """她说完之后对方已经回过话，这条通知只是正常对话的一部分。"""
    now = 1_700_000_000_000
    _freeze(monkeypatch, {'now': now})
    proactive = _DecisionProvider()
    chat = _service(db, proactive)
    context = _direct_context(chat._registry)
    stream_id = context.stream.id
    _silence(chat, stream_id, SILENCE_MS + 60_000, now)
    chat.memory.append_message(stream_id, context.person.id, 'user', '在的')

    assert await chat.note_peer_typing(context) is False
    assert proactive.calls == []


async def test_group_stream_never_nudges(db, monkeypatch) -> None:
    """群里盯着某个人打字不合适，即使协议将来推送也不接。"""
    now = 1_700_000_000_000
    _freeze(monkeypatch, {'now': now})
    proactive = _DecisionProvider()
    chat = _service(db, proactive)
    context = chat._registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id='86420',
        sender_external_id='97531',
        sender_nickname='群友',
        sender_group_card='小李',
        first_seen_at=1_000_000,
    )
    _silence(chat, context.stream.id, SILENCE_MS + 60_000, now)

    assert await chat.note_peer_typing(context) is False
    assert proactive.calls == []


async def test_asleep_never_nudges(db, monkeypatch) -> None:
    """睡着的时候不会盯着对方的输入框。"""
    now = 1_700_000_000_000
    _freeze(monkeypatch, {'now': now})
    proactive = _DecisionProvider(['reply', 'reply'])
    chat = _service(db, proactive)
    chat.set_sleep_state_provider(
        lambda: SleepState(asleep=True, just_woke=False, resting=False)
    )
    context = _direct_context(chat._registry)
    _silence(chat, context.stream.id, SILENCE_MS + 60_000, now)

    assert await chat.note_peer_typing(context) is False
    assert proactive.calls == []


class _TypingChat:
    """记录主体路由交来的上下文，隔离模型和真实平台投递。"""

    def __init__(self) -> None:
        self.contexts = []

    async def note_peer_typing(self, context) -> bool:
        self.contexts.append(context)
        return True


async def test_platform_typing_reuses_the_existing_direct_context(db) -> None:
    """输入状态复用既有身份，并在重启后的空 broker 中恢复出站驱动绑定。"""
    original_chat = app_state.chat
    original_registry = app_state.registry
    original_register = app_state.register_platform_stream
    registry = StreamRegistry(db)
    registry.resolve_inbound(
        platform='qq',
        stream_kind='direct',
        stream_external_id='24680',
        sender_external_id='24680',
        sender_nickname='凌白',
        sender_group_card='',
        first_seen_at=1_000_000,
    )
    chat = _TypingChat()
    registered_streams = []
    try:
        app_state.chat = chat
        app_state.registry = registry
        app_state.register_platform_stream = registered_streams.append

        response = await platform_typing(PlatformTypingBody(
            platform='qq',
            streamKind='direct',
            streamExternalId='24680',
            senderExternalId='24680',
        ))

        assert json.loads(response.body) == {'accepted': True, 'spoke': True}
        assert len(chat.contexts) == 1
        assert chat.contexts[0].identity.display_name == '凌白'
        assert registered_streams == [chat.contexts[0].stream]
    finally:
        app_state.chat = original_chat
        app_state.registry = original_registry
        app_state.register_platform_stream = original_register


async def test_platform_typing_after_restart_restores_driver_before_reply(
    db,
    monkeypatch,
) -> None:
    """重启后第一条事件只有输入状态时，主动回复也必须能投递到 QQ。"""
    now = 1_700_000_000_000
    _freeze(monkeypatch, {'now': now})
    original_chat = app_state.chat
    original_registry = app_state.registry
    original_register = app_state.register_platform_stream
    registry = StreamRegistry(db)
    context = _direct_context(registry)
    broker = PlatformBroker()
    pushed = []

    async def push(stream_id: int, channel: str, payload: Any) -> int:
        pushed.append((stream_id, channel, payload))
        return 1

    driver = QqWebSocketDriver(push)

    def register(stream) -> None:
        if not broker.has_driver(stream.id):
            broker.register(stream.id, driver)

    chat = _service(db, _DecisionProvider(), broker)
    _silence(chat, context.stream.id, SILENCE_MS + 60_000, now)
    try:
        app_state.chat = chat
        app_state.registry = registry
        app_state.register_platform_stream = register

        response = await platform_typing(PlatformTypingBody(
            platform='qq',
            streamKind='direct',
            streamExternalId='24680',
            senderExternalId='24680',
        ))

        assert json.loads(response.body) == {'accepted': True, 'spoke': True}
        assert broker.has_driver(context.stream.id)
        assert len(pushed) == 1
        assert pushed[0][0] == context.stream.id
        assert pushed[0][1] == 'qq.send'
    finally:
        app_state.chat = original_chat
        app_state.registry = original_registry
        app_state.register_platform_stream = original_register


async def test_platform_typing_does_not_create_an_unknown_contact(db) -> None:
    """只有状态通知而没有真实消息时，不能创建空昵称联系人或幽灵会话。"""
    original_chat = app_state.chat
    original_registry = app_state.registry
    original_register = app_state.register_platform_stream
    registry = StreamRegistry(db)
    chat = _TypingChat()
    try:
        app_state.chat = chat
        app_state.registry = registry
        app_state.register_platform_stream = None

        response = await platform_typing(PlatformTypingBody(
            platform='qq',
            streamKind='direct',
            streamExternalId='13579',
            senderExternalId='13579',
        ))

        payload = json.loads(response.body)
        assert payload['accepted'] is False
        assert payload['spoke'] is False
        assert chat.contexts == []
        assert registry.find_person_by_identity('qq', '13579') is None
        assert all(stream.external_id != '13579' for stream in registry.list_streams())
    finally:
        app_state.chat = original_chat
        app_state.registry = original_registry
        app_state.register_platform_stream = original_register


async def test_scheduled_follow_up_keeps_the_five_minute_typing_anchor(
    db,
    monkeypatch,
) -> None:
    """一分钟主动追问后，输入检测仍在原回复后第三分钟开放，不推迟到第四分钟。"""
    clock = {'now': 1_700_000_000_000}
    _freeze(monkeypatch, clock)
    proactive = _DecisionProvider()
    chat = _service(db, proactive)
    chat._cfg.typing.follow_up.peer_silence_minutes = 0.001
    context = _direct_context(chat._registry)
    chat.memory.append_message(context.stream.id, context.person.id, 'user', '我有点犹豫')
    chat.memory.append_message(context.stream.id, None, 'assistant', '<say>那你呢？</say>')

    await chat.startup()
    try:
        chat._arm_direct_follow_up(context)
        clock['now'] += 1 * 60_000
        await asyncio.sleep(0.1)

        assert len(proactive.calls) == 1
        assert chat._typing_nudges[context.stream.id] == 1

        clock['now'] += 2 * 60_000
        assert await chat.note_peer_typing(context) is True
        assert len(proactive.calls) == 2
        assert chat._typing_nudges[context.stream.id] == 2
        # 同一次输入过程会收到多条通知，但三分钟节点只创建这一轮机会。
        assert await chat.note_peer_typing(context) is False
        assert len(proactive.calls) == 2
    finally:
        await chat.shutdown()


async def test_scheduled_follow_up_can_choose_silent(
    db,
    monkeypatch,
) -> None:
    """一分钟到点只创建动作机会，情景不适合续接时不向 QQ 追问。"""
    clock = {'now': 1_700_000_000_000}
    _freeze(monkeypatch, clock)
    decision = _DecisionProvider(['silent', 'silent'])
    scene = _SceneProvider()
    broker = _FakeBroker()
    chat = _service(db, decision, broker, scene)
    chat._cfg.typing.follow_up.peer_silence_minutes = 0.001
    context = _direct_context(chat._registry)
    chat.memory.append_message(context.stream.id, context.person.id, 'user', '好啦先这样')
    chat.memory.append_message(context.stream.id, None, 'assistant', '<say>嗯嗯</say>')

    await chat.startup()
    try:
        chat._arm_direct_follow_up(context)
        clock['now'] += 1 * 60_000
        await asyncio.sleep(0.1)

        assert len(scene.calls) == 1
        assert len(decision.calls) == 1
        assert broker.dispatched == []
        assert context.stream.id not in chat._typing_nudges

        clock['now'] += 2 * 60_000
        assert await chat.note_peer_typing(context) is False
        assert len(decision.calls) == 2
        # 输入机会选择 silent 后也已完成评估，连续状态通知不得反复调用模型。
        assert await chat.note_peer_typing(context) is False
        assert len(decision.calls) == 2
        assert broker.dispatched == []
    finally:
        await chat.shutdown()
