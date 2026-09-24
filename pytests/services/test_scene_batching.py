"""群聊按场面回复：一批取出几个人的消息，回复途中被新内容立即打断并合并重来。

开关 ``conversation_agent.scene_batching`` 打开时：
- 群聊一批取出缓冲区里所有人的待回消息，静默等待按全场最后一条计时；
- 回合开始到动作定下之前，进来任何人的新内容（私聊即对方）立即取消模型调用，
  批次退回缓冲与新消息合并，不写助手历史、不保存半截回复、不做好感度结算；
- 只补表情包、QQ 表情、戳一戳、只 @，达到重来上限，已写过心情或约定，旧管线，
  桌面——都不打断；
- 这一轮的对方取主要对象（最后一条 @／叫名字／回复她的发送者），好感度只结算一次；
- 规划器协议列出本批全部发送者，人物画像优先取本批发送者。
关闭开关时的按人取批行为见 test_reply_superseded_by_follow_up.py。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Sequence

import asyncio

import pytest

from src.core.agent.profile import profiles_for_injection
from src.core.config.schema import Config
from src.core.llm_models.openai import LlmError
from src.core.observe.store import event_store
from src.core.platform_io.types import DeliveryReceipt, InboundMessage, OutboundMessage
from src.core.services.chat import ChatService
from src.core.services.chat import service as chat_service_module


class _Provider:
    """按调用序号返回脚本输出；指定的调用在产出任何内容之前挂起等取消。

    挂起的调用模拟模型还在等首字：只有取消信号到达才会结束，并按真实客户端的
    约定抛出 ``aborted``；两秒内等不到取消说明打断没有生效，直接判失败。
    ``mid_call`` 里的钩子在产出第一段之后执行，用来模拟「已经吐出心情标签之后才有人插话」。
    """

    def __init__(
        self,
        scripts: List[List[str]],
        *,
        hang_calls: Sequence[int] = (),
    ) -> None:
        self.scripts = scripts
        self.hang_calls = set(hang_calls)
        self.calls = 0
        self.messages: List[list] = []
        self.before_call: Dict[int, Callable[[], Awaitable[None]]] = {}
        self.mid_call: Dict[int, Callable[[], Awaitable[None]]] = {}
        self.provider = 'fake'
        self.model = 'fake'

    async def stream(
        self, messages=None, signal: asyncio.Event | None = None, **_kwargs: Any,
    ) -> AsyncIterator[dict[str, str]]:
        index = self.calls
        self.calls += 1
        self.messages.append(list(messages or []))
        hook = self.before_call.get(index)
        if hook is not None:
            await hook()
        if index in self.hang_calls:
            assert signal is not None, '模型调用没有收到取消信号'
            try:
                await asyncio.wait_for(signal.wait(), timeout=2)
            except asyncio.TimeoutError as exc:
                raise AssertionError('取消信号没有送到等首字的模型调用') from exc
            raise LlmError('aborted', '生成已中断')
        script = self.scripts[min(index, len(self.scripts) - 1)]
        for position, text in enumerate(script):
            yield {'text': text}
            mid = self.mid_call.get(index)
            if position == 0 and mid is not None:
                await mid()


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


def _config(*, mode: str = 'enabled', restarts: int = 1, quiet: float = 0.0) -> Config:
    config = Config()
    config.bot.name = '月璃'
    config.conversation_agent.mode = mode
    config.conversation_agent.max_cognitive_rounds = 0
    config.conversation_agent.max_reply_restarts = restarts
    config.conversation_agent.reply_quiet_seconds = quiet
    config.conversation_agent.scene_batching = True
    config.group_chat.scene_refresh_messages = 0
    return config


def _person(registry, sender: str, card: str, kind: str = 'group'):
    return registry.resolve_inbound(
        platform='qq',
        stream_kind=kind,
        stream_external_id='86420' if kind == 'group' else sender,
        sender_external_id=sender,
        sender_nickname=card,
        sender_group_card=card if kind == 'group' else '',
        first_seen_at=1_000_000,
    )


def _reply(text: str, extra: str = '') -> str:
    return (
        '<decision action="reply" targets="1" reasons="direct_question" length="brief"/>'
        f'{extra}<say emotion="normal">{text}</say>'
    )


def _mention(context, text: str = '@月璃 看这个') -> InboundMessage:
    return InboundMessage(text=text, context=context, mentioned_me=True, authored_text=' 看这个')


def _sticker(context) -> InboundMessage:
    return InboundMessage(
        text='[表情包]', context=context,
        emoji_sources=('https://example.invalid/s.gif',), emoji_sub_types=(1,),
        authored_text='',
    )


def _user_inputs_by_turn() -> Dict[int, List[str]]:
    grouped: Dict[int, List[str]] = {}
    for event in reversed(event_store.search(kinds=['user_input']).events):
        grouped.setdefault(event['turnId'], []).append(event['text'])
    return grouped


def _assistant_lines(chat: ChatService, stream_id: int) -> List[str]:
    return [
        message.content
        for message in chat.memory.working_memory(stream_id, 20)
        if message.role == 'assistant'
    ]


def _segments(broker: _RecordingBroker) -> List[List[str]]:
    return [message.segments for message in broker.dispatched]


def _buffer(chat: ChatService, stream_id: int) -> List[str]:
    return [message.text for message in chat._buffers.get(stream_id, [])]


async def _run_turn(chat: ChatService, stream_id: int) -> None:
    await chat._tick()
    await chat._inflight[stream_id].task


def _chat(db, provider, broker, **config: Any) -> ChatService:
    return ChatService(db, provider, None, None, _noop, cfg=_config(**config), broker=broker)


class TestSceneBatch:
    async def test_group_batch_takes_every_sender_and_protocol_names_them(self, db) -> None:
        provider = _Provider([[_reply('两个都看到了')]])
        broker = _RecordingBroker()
        chat = _chat(db, provider, broker)
        a = _person(chat._registry, '97531', '小李')
        b = _person(chat._registry, '24680', '小王')
        stream_id = a.stream.id

        await chat.send(InboundMessage(text='哈哈', context=a))
        await chat.send(_mention(b))
        await _run_turn(chat, stream_id)

        assert list(_user_inputs_by_turn().values())[-1] == ['哈哈', '@月璃 看这个']
        prompt = '\n'.join(str(item.get('content', '')) for item in provider.messages[0])
        assert '本轮处理小李、小王刚发送的这几条消息' in prompt
        assert _segments(broker) == [['两个都看到了']]

    async def test_primary_is_last_signal_sender_and_bond_settles_once(self, db, monkeypatch) -> None:
        provider = _Provider([[_reply('在呢')]])
        chat = _chat(db, provider, _RecordingBroker())
        a = _person(chat._registry, '97531', '小李')
        b = _person(chat._registry, '24680', '小王')
        c = _person(chat._registry, '13579', '小张')
        settled: List[int] = []
        original = chat.persona.apply_turn

        def record(person_id, *args, **kwargs):
            settled.append(person_id)
            return original(person_id, *args, **kwargs)

        monkeypatch.setattr(chat.persona, 'apply_turn', record)
        await chat.send(_mention(a, '@月璃 在吗'))
        await chat.send(_mention(b))
        await chat.send(InboundMessage(text='哈哈', context=c))
        await _run_turn(chat, a.stream.id)

        # 最后一条 @ 她的是小王（不是最后说话的小张）：好感度只结算一次、只记给小王，
        # 精力也只扣这一次。
        assert settled == [b.person.id]

    async def test_quiet_window_counts_from_last_message_of_anyone(self, db, monkeypatch) -> None:
        clock = {'now': 1_800_000_000_000}
        monkeypatch.setattr(chat_service_module, 'current_time', lambda: clock['now'])
        provider = _Provider([[_reply('两个都看到了')]])
        chat = _chat(db, provider, _RecordingBroker(), quiet=1.5)
        a = _person(chat._registry, '97531', '小李')
        b = _person(chat._registry, '24680', '小王')
        stream_id = a.stream.id

        await chat.send(_mention(a))
        clock['now'] += 1_000
        await chat.send(InboundMessage(text='我也想问', context=b))
        clock['now'] += 1_000
        await chat._tick()
        assert stream_id not in chat._inflight

        clock['now'] += 600
        await _run_turn(chat, stream_id)
        assert list(_user_inputs_by_turn().values())[-1] == ['@月璃 看这个', '我也想问']


class TestInterrupt:
    async def test_planning_is_interrupted_before_first_token(self, db) -> None:
        provider = _Provider([[], [_reply('两个都看到了')]], hang_calls=(0,))
        broker = _RecordingBroker()
        chat = _chat(db, provider, broker)
        a = _person(chat._registry, '97531', '小李')
        b = _person(chat._registry, '24680', '小王')
        stream_id = a.stream.id
        intimacy_before = chat.persona.get(a.person.id).intimacy

        async def b_speaks() -> None:
            await chat.send(InboundMessage(text='我也想问', context=b))

        provider.before_call[0] = b_speaks
        await chat.send(_mention(a))
        await _run_turn(chat, stream_id)

        # 被打断的回合什么都不留下：不投递、不写历史、不结算好感度。
        assert broker.dispatched == []
        assert _assistant_lines(chat, stream_id) == []
        assert chat.persona.get(a.person.id).intimacy == intimacy_before
        assert _buffer(chat, stream_id) == ['@月璃 看这个', '我也想问']
        assert chat._reply_restarts[stream_id] == 1

        await _run_turn(chat, stream_id)
        assert _segments(broker) == [['两个都看到了']]
        assert list(_user_inputs_by_turn().values())[-1] == ['@月璃 看这个', '我也想问']
        assert _assistant_lines(chat, stream_id) == ['<say>两个都看到了</say>']

    async def test_interrupt_while_preparing_skips_the_model(self, db) -> None:
        provider = _Provider([[_reply('两个都看到了')]])
        broker = _RecordingBroker()
        chat = _chat(db, provider, broker)
        a = _person(chat._registry, '97531', '小李')
        b = _person(chat._registry, '24680', '小王')
        stream_id = a.stream.id

        await chat.send(_mention(a))
        await chat._tick()
        # 回合任务已创建、还没走到模型调用，这时有人插话。
        await chat.send(InboundMessage(text='我也想问', context=b))
        await chat._inflight[stream_id].task

        assert provider.calls == 0
        assert _buffer(chat, stream_id) == ['@月璃 看这个', '我也想问']
        await _run_turn(chat, stream_id)
        assert _segments(broker) == [['两个都看到了']]

    async def test_replying_is_interrupted_in_split_mode(self, db) -> None:
        config = _config()
        config.conversation_agent.split_replyer = True
        config.conversation_agent.tool_calling = False
        planner = _Provider([[
            '<decision action="reply" targets="1" reasons="direct_question" '
            'length="brief" reference="回应一下"/>',
        ]])
        replyer = _Provider([[], ['<say>好的</say>']], hang_calls=(0,))
        broker = _RecordingBroker()
        chat = ChatService(
            db, planner, None, None, _noop, cfg=config, broker=broker,
            planner_provider=planner, replyer_provider=replyer,
        )
        a = _person(chat._registry, '97531', '小李')
        b = _person(chat._registry, '24680', '小王')
        stream_id = a.stream.id

        async def b_speaks() -> None:
            await chat.send(InboundMessage(text='我也想问', context=b))

        replyer.before_call[0] = b_speaks
        await chat.send(_mention(a))
        await _run_turn(chat, stream_id)
        assert broker.dispatched == []
        assert _buffer(chat, stream_id) == ['@月璃 看这个', '我也想问']

        await _run_turn(chat, stream_id)
        assert _segments(broker) == [['好的']]

    async def test_mood_written_before_interjection_keeps_the_reply(self, db) -> None:
        """单次调用路径边生成边放出事件：心情先落库、之后才有人插话。

        拆分路径的回复正文整段校验后才放出，插话发生时心情尚未落库，取消后也不会再落，
        不存在重复写入，因此这条保护只在单次调用路径上起作用。
        """
        provider = _Provider([[
            '<decision action="reply" targets="1" reasons="direct_question" length="brief"/>'
            '<mood favor="1"/>',
            '<say emotion="normal">在呢</say>',
        ]])
        broker = _RecordingBroker()
        chat = _chat(db, provider, broker)
        a = _person(chat._registry, '97531', '小李')
        b = _person(chat._registry, '24680', '小王')
        stream_id = a.stream.id

        async def b_speaks() -> None:
            await chat.send(InboundMessage(text='我也想问', context=b))

        provider.mid_call[0] = b_speaks
        await chat.send(_mention(a))
        await _run_turn(chat, stream_id)

        # 心情已经落库，重来会重复写入：照常发出，插话留给下一轮。
        assert _segments(broker) == [['在呢']]
        assert _buffer(chat, stream_id) == ['我也想问']

    @pytest.mark.parametrize(
        'follow_up',
        [
            _sticker,
            lambda context: InboundMessage(
                text='[戳了戳月璃]', context=context, poked_me=True, authored_text='',
            ),
            lambda context: InboundMessage(
                text='@月璃', context=context, mentioned_me=True, authored_text='',
            ),
        ],
        ids=['sticker', 'poke', 'bare_mention'],
    )
    async def test_reactions_do_not_interrupt(self, db, follow_up) -> None:
        provider = _Provider([[_reply('在呢')]])
        broker = _RecordingBroker()
        chat = _chat(db, provider, broker)
        a = _person(chat._registry, '97531', '小李')
        b = _person(chat._registry, '24680', '小王')

        async def b_reacts() -> None:
            await chat.send(follow_up(b))

        provider.before_call[0] = b_reacts
        await chat.send(_mention(a, '@月璃 在吗'))
        await _run_turn(chat, a.stream.id)
        assert _segments(broker) == [['在呢']]

    async def test_restart_limit_two_then_delivers(self, db) -> None:
        provider = _Provider([[], [], [_reply('三个都看到了')]], hang_calls=(0, 1))
        broker = _RecordingBroker()
        chat = _chat(db, provider, broker, restarts=2)
        a = _person(chat._registry, '97531', '小李')
        b = _person(chat._registry, '24680', '小王')
        c = _person(chat._registry, '13579', '小张')
        stream_id = a.stream.id

        async def b_speaks() -> None:
            await chat.send(InboundMessage(text='我也想问', context=b))

        async def c_speaks() -> None:
            await chat.send(InboundMessage(text='加一', context=c))

        provider.before_call[0] = b_speaks
        provider.before_call[1] = c_speaks
        await chat.send(_mention(a))
        await _run_turn(chat, stream_id)
        await _run_turn(chat, stream_id)
        assert broker.dispatched == []
        assert chat._reply_restarts[stream_id] == 2

        await _run_turn(chat, stream_id)
        assert _segments(broker) == [['三个都看到了']]
        assert list(_user_inputs_by_turn().values())[-1] == ['@月璃 看这个', '我也想问', '加一']

    async def test_restart_limit_reached_does_not_interrupt(self, db) -> None:
        provider = _Provider([[_reply('在呢')]])
        broker = _RecordingBroker()
        chat = _chat(db, provider, broker, restarts=0)
        a = _person(chat._registry, '97531', '小李')
        b = _person(chat._registry, '24680', '小王')

        async def b_speaks() -> None:
            await chat.send(InboundMessage(text='我也想问', context=b))

        provider.before_call[0] = b_speaks
        await chat.send(_mention(a))
        await _run_turn(chat, a.stream.id)
        assert _segments(broker) == [['在呢']]
        assert _buffer(chat, a.stream.id) == ['我也想问']

    async def test_legacy_pipeline_is_never_interrupted(self, db) -> None:
        provider = _Provider([['<say>在呢</say>']])
        broker = _RecordingBroker()
        chat = _chat(db, provider, broker, mode='off')
        a = _person(chat._registry, '97531', '小李')
        b = _person(chat._registry, '24680', '小王')

        async def b_speaks() -> None:
            await chat.send(InboundMessage(text='我也想问', context=b))

        provider.before_call[0] = b_speaks
        await chat.send(_mention(a))
        await _run_turn(chat, a.stream.id)
        assert _segments(broker) == [['在呢']]

    async def test_direct_chat_follow_up_interrupts(self, db) -> None:
        provider = _Provider([[], [_reply('问吧')]], hang_calls=(0,))
        broker = _RecordingBroker()
        chat = _chat(db, provider, broker)
        peer = _person(chat._registry, '97531', '小李', kind='direct')
        stream_id = peer.stream.id

        async def peer_continues() -> None:
            await chat.send(InboundMessage(text='问你个事', context=peer))

        provider.before_call[0] = peer_continues
        await chat.send(InboundMessage(text='在吗', context=peer))
        await _run_turn(chat, stream_id)
        assert broker.dispatched == []

        await _run_turn(chat, stream_id)
        assert _segments(broker) == [['问吧']]


class TestPresence:
    async def test_batch_senders_lead_present_persons(self, db) -> None:
        chat = _chat(db, _Provider([[]]), _RecordingBroker())
        a = _person(chat._registry, '97531', '小李')
        b = _person(chat._registry, '24680', '小王')

        ids = chat._present_person_ids(a, (b.person.id, a.person.id))
        assert ids[:2] == [b.person.id, a.person.id]

    async def test_profiles_prefer_batch_senders_over_intimacy(self, db) -> None:
        chat = _chat(db, _Provider([[]]), _RecordingBroker())
        people = [
            _person(chat._registry, sender, card)
            for sender, card in (('1', '甲'), ('2', '乙'), ('3', '丙'), ('4', '丁'))
        ]
        ids = [person.person.id for person in people]
        for person_id, intimacy in zip(ids, (90.0, 80.0, 70.0, 10.0)):
            db.execute(
                'INSERT OR REPLACE INTO persona_bond (person_id, intimacy, updated_at) VALUES (?, ?, 0)',
                (person_id, intimacy),
            )
            db.execute(
                "INSERT OR REPLACE INTO person_profile (person_id, summary) VALUES (?, '印象')",
                (person_id,),
            )
        db.commit()

        chosen = [profile.person_id for profile in profiles_for_injection(db, ids, limit=3)]
        assert chosen == ids[:3]
        # 好感度最低的丁在本批里：它先入选，其余按好感度补足。
        chosen = [
            profile.person_id
            for profile in profiles_for_injection(db, ids, limit=3, priority_ids=(ids[3],))
        ]
        assert chosen == [ids[3], ids[0], ids[1]]


def test_scene_batching_is_on_by_default() -> None:
    assert Config().conversation_agent.scene_batching is True
