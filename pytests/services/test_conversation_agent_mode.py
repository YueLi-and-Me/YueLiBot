"""Conversation 行动核心灰度模式接线验收。"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List

import asyncio
import time

from src.core.agent.conversation_gate import ONGOING_TOPIC_MESSAGE_SPAN
from src.core.runtime.clock import now as current_time
from src.core.config.schema import Config
from src.core.observe.store import event_store
from src.core.platform_io.forward import (
    ForwardMessagePart,
    ForwardMessageTree,
    ForwardNode,
)
from src.core.platform_io.registry import StreamRegistry
from src.core.platform_io.types import DeliveryReceipt, InboundMessage, OutboundMessage
from src.core.services.chat import ChatService


class _ScriptedProvider:
    """按调用顺序轮换分片脚本并保留每次实际请求的替身提供方。"""

    def __init__(self, scripts: List[List[str]]) -> None:
        self.scripts = scripts
        self.calls = 0
        self.messages: List[List[dict[str, Any]]] = []

    async def stream(
        self,
        messages: List[dict[str, Any]] | None = None,
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, str]]:
        self.calls += 1
        if messages is not None:
            self.messages.append(messages)
        script = self.scripts[min(self.calls, len(self.scripts)) - 1]
        for text in script:
            yield {'text': text}


class _CrossedProvider:
    """首轮阻塞到测试放行，之后返回 silent；用于复现续跑竞态。"""

    def __init__(self) -> None:
        self.calls: List[List[dict[str, Any]]] = []
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()

    async def stream(
        self,
        messages: List[dict[str, Any]] | None = None,
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, str]]:
        self.calls.append(messages or [])
        if len(self.calls) == 1:
            self.first_started.set()
            await self.release_first.wait()
            yield {
                'text': (
                    '<decision action="reply" targets="1" reasons="can_add_value" length="brief"/>'
                    '<say>上一条回复正文</say>'
                )
            }
            return
        yield {'text': '<decision action="silent" reasons="duplicate_response"/>'}


class _FakeBroker:
    """记录并成功回执的非桌面出站路由替身。"""

    def __init__(self) -> None:
        self.dispatched: List[OutboundMessage] = []

    async def dispatch(self, message: OutboundMessage) -> DeliveryReceipt:
        self.dispatched.append(message)
        return DeliveryReceipt(
            platform=message.stream.platform,
            stream_id=message.stream.id,
            external_message_ids=["fake-1"],
        )


async def _noop(_channel: str, _payload: Any, _stream_id: int) -> None:
    return None


def _group_context(registry: StreamRegistry, external_id: str = "86420"):
    return registry.resolve_inbound(
        platform="qq",
        stream_kind="group",
        stream_external_id=external_id,
        sender_external_id="97531",
        sender_nickname="账号昵称",
        sender_group_card="小李",
        first_seen_at=1_000_000,
    )


def _direct_context(registry: StreamRegistry, external_id: str = "900000001"):
    return registry.resolve_inbound(
        platform="qq",
        stream_kind="direct",
        stream_external_id=external_id,
        sender_external_id=external_id,
        sender_nickname="凌白",
        sender_group_card="",
        first_seen_at=1_000_000,
    )


def _action_events() -> List[dict[str, Any]]:
    return [entry for entry in event_store.search(kinds=["action_decision"]).events]


async def test_shadow_records_decision_and_keeps_legacy_behavior(db) -> None:
    """shadow：DELIBERATE 候选付一次 Agent 调用只记录，旧管线行为不变。"""
    config = Config()
    config.bot.name = "月璃"
    config.conversation_agent.mode = "shadow"
    provider = _ScriptedProvider([
        [
            '<decision action="reply" targets="1" reasons="direct_question" length="brief"/>',
            '<say emotion="normal">Agent想说的话</say>',
        ],
        ['<say emotion="normal">旧管线回复</say>'],
    ])
    chat = ChatService(db, provider, None, None, _noop, cfg=config, broker=_FakeBroker())
    context = _group_context(chat._registry)
    await chat.send(InboundMessage(text="月璃在吗", context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    assert provider.calls == 2
    shadow_events = [
        entry for entry in _action_events()
        if entry["version"]["modelTask"] == "chat.conversation.shadow"
    ]
    assert len(shadow_events) == 1
    assert shadow_events[0]["eventStatus"] == "committed"
    assert shadow_events[0]["decision"]["action"] == "reply"
    # 旧管线照常决策并发出自己的回复，Agent 正文被丢弃。
    turn_actions = event_store.search(kinds=["turn_action"]).events
    assert turn_actions[-1]["decisionSource"] == "TurnPlanner(AlwaysReplyPolicy)"
    stored = [
        message.content
        for message in chat.memory.working_memory(context.stream.id, 20)
    ]
    assert any("旧管线回复" in content for content in stored)
    assert not any("Agent想说的话" in content for content in stored)


async def test_shadow_prompt_replaces_say_first_protocol(db) -> None:
    """shadow 系统提示词只教「先动作头后正文」，不再与旧协议竞争。"""
    config = Config()
    config.bot.name = "月璃"
    config.conversation_agent.mode = "shadow"
    provider = _ScriptedProvider([
        [
            '<decision action="reply" targets="1" reasons="direct_question" length="brief"/>',
            '<say emotion="normal">Agent想说的话</say>',
        ],
        ['<say emotion="normal">旧管线回复</say>'],
    ])
    chat = ChatService(db, provider, None, None, _noop, cfg=config, broker=_FakeBroker())
    context = _group_context(chat._registry)
    await chat.send(InboundMessage(text="月璃在吗", context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    assert provider.calls == 2
    agent_messages = provider.messages[0]
    legacy_system = provider.messages[1][0]['content']
    agent_system = agent_messages[0]['content']
    assert '# 输出格式' in agent_system
    assert '先决定「做什么」' in agent_system
    assert '<decision action="reply" targets="1"' in agent_system
    assert '<decision action="silent" reasons="others_conversation"/>' in agent_system
    assert '只输出下列标签' not in agent_system
    # 示例之前的真实历史 assistant 必须纯文本化；few-shot 示例自身允许含 <say>。
    example_index = next(
        i for i, message in enumerate(agent_messages)
        if '[输出格式示例]' in message['content']
    )
    history_assistant = [
        m for m in agent_messages[1:example_index]
        if m['role'] == 'assistant'
    ]
    assert not any('<say' in m['content'] for m in history_assistant)
    assert any('[输出格式示例]' in m['content'] for m in agent_messages)
    assert any('[输出要求]' in m['content'] for m in agent_messages)
    # 旧管线提示词不受 Agent 协议影响，仍保持直接输出 <say> 的格式。
    assert '只输出下列标签' in legacy_system


async def test_protocol_lists_selectable_messages_with_original_text(db) -> None:
    """可选消息编号必须带原文锚点，否则模型无法把 targets 填成合法数字。

    线上故障：协议块只给出孤立的消息主键，模型改写成「凌白最后一条」这类
    描述，整轮按 illegal_action 失败，用户侧表现为她收到点名却不回话。
    """
    config = Config()
    config.bot.name = "月璃"
    config.conversation_agent.mode = "enabled"
    provider = _ScriptedProvider([
        [
            '<decision action="reply" targets="1" reasons="directly_addressed" length="brief"/>',
            '<say emotion="normal">在的</say>',
        ],
    ])
    chat = ChatService(db, provider, None, None, _noop, cfg=config, broker=_FakeBroker())
    context = _group_context(chat._registry)
    await chat.send(InboundMessage(text="月璃出来", context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    system = provider.messages[0][0]['content']
    assert '本轮可选消息（编号 = 原文）：' in system
    # 群聊清单与历史行同口径带群名片，模型才能把编号对回具体那条消息。
    assert '  1 = 小李: 月璃出来' in system
    assert '写人名' in system
    assert '只填一个' in system
    # 历史行也必须带同一个编号，清单里的数字才有可指认的落点。
    # 编号与群名片之间夹着发言时刻，因此分开断言而不是拼成一整串。
    history_user = [m for m in provider.messages[0][1:] if m['role'] == 'user']
    assert any(
        m['content'].startswith('[1] ') and '小李: 月璃出来' in m['content']
        for m in history_user
    )


async def test_desktop_stream_prompt_hides_the_silent_example(db) -> None:
    """桌面交互不允许 silent，协议里就不能出现 silent 示例。

    桌面交互纳入 Agent 后，动作空间只剩 reply；示例是模型最容易照抄的部分，摆一个
    本回合非法的动作等于主动制造 illegal_action。
    """
    config = Config()
    config.bot.name = "月璃"
    config.conversation_agent.mode = "enabled"
    provider = _ScriptedProvider([
        ['<decision action="reply" targets="1" reasons="direct_question" length="brief"/>',
         '<say emotion="normal">在的</say>'],
    ])
    chat = ChatService(db, provider, None, None, _noop, cfg=config)
    context = chat.desktop_context
    await chat.send(InboundMessage(text="在吗", context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    system = provider.messages[0][0]['content']
    assert '# reply（回复）' in system
    assert '# silent（不回复）' not in system
    # 认知动作与 stream 类型无关（她在私聊里同样可以先想一下），因此动作集是
    # 「终局只有 reply」而不是「只有 reply」；断言按语义写，不钉死枚举顺序。
    assert 'action 只能写当前允许的动作之一：' in system
    header = system.split('action 只能写当前允许的动作之一：')[1].splitlines()[0]
    assert 'silent' not in header


async def test_user_started_direct_first_turn_must_reply_without_scene_gate(db) -> None:
    """用户主动发起的 QQ 私聊必须回复，不能被情景分析判成已读不回。"""
    config = Config()
    config.bot.name = "月璃"
    config.conversation_agent.mode = "enabled"
    decision = _ScriptedProvider([
        [
            '<decision action="reply" targets="1" reasons="directly_addressed" '
            'length="brief"/>',
            '<say emotion="normal">嗯嗯，你说。</say>',
        ],
    ])
    scene = _ScriptedProvider([
        ['{"topic":"对方只是礼貌收尾","atmosphere":"平淡"}'],
    ])
    broker = _FakeBroker()
    chat = ChatService(
        db,
        decision,
        None,
        scene,
        _noop,
        cfg=config,
        broker=broker,
    )
    context = _direct_context(chat._registry)

    await chat.send(InboundMessage(text="我想跟你说件事", context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    assert scene.calls == 0
    assert decision.calls == 1
    assert len(broker.dispatched) == 1
    system = decision.messages[0][0]['content']
    assert '# 私聊情景分析结果' not in system
    assert '# silent（不回复）' not in system
    events = _action_events()
    assert events[-1]["gate"]["disposition"] == "force"
    assert events[-1]["eventStatus"] == "committed"


async def test_long_say_is_delivered_as_several_bubbles(db) -> None:
    """一条长台词按打字习惯切成多条投递，历史与出站看到同一份气泡。

    线上故障：出站侧把全部分句 ''.join 成一条 QQ 消息，模型分好的 <say> 在最后
    一公里被还原成一大段，用户看到的永远是一整段。
    """
    config = Config()
    config.bot.name = "月璃"
    config.conversation_agent.mode = "enabled"
    broker = _FakeBroker()
    provider = _ScriptedProvider([
        [
            '<decision action="reply" targets="1" reasons="directly_addressed" length="brief"/>',
            '<say emotion="normal">不过某人不是连额度都没了吗，哪来的1314，梦里啥都有。</say>',
        ],
    ])
    chat = ChatService(db, provider, None, None, _noop, cfg=config, broker=broker)
    context = _group_context(chat._registry)
    await chat.send(InboundMessage(text="月璃出来", context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    assert len(broker.dispatched) == 1
    assert broker.dispatched[0].segments == [
        '不过某人不是连额度都没了吗',
        '哪来的1314，梦里啥都有',
    ]


async def test_typing_config_drives_bubbles_and_delays(db) -> None:
    """切分条数与打字停顿全部来自配置，关掉停顿就连续发。"""
    config = Config()
    config.bot.name = "月璃"
    config.conversation_agent.mode = "enabled"
    config.typing.bubble_target_chars = 6
    config.typing.delay_enabled = False
    broker = _FakeBroker()
    provider = _ScriptedProvider([
        [
            '<decision action="reply" targets="1" reasons="directly_addressed" length="brief"/>',
            '<say emotion="normal">在呢在呢，你说你说，我听着呢。</say>',
        ],
    ])
    chat = ChatService(db, provider, None, None, _noop, cfg=config, broker=broker)
    context = _group_context(chat._registry)
    await chat.send(InboundMessage(text="月璃出来", context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    outbound = broker.dispatched[0]
    # 目标字数调小，同一条台词就切得更碎。
    assert outbound.segments == ['在呢在呢', '你说你说', '我听着呢']
    # 停顿关闭时全为 0，多条气泡连续发出。
    assert outbound.batch_delays_ms == (0, 0, 0)


async def test_action_protocol_states_how_to_write_each_length(db) -> None:
    """length 必须在协议里带上正文写法，否则它是个写完没人读的字段。"""
    config = Config()
    config.bot.name = "月璃"
    config.conversation_agent.mode = "enabled"
    provider = _ScriptedProvider([
        ['<decision action="reply" targets="1" reasons="directly_addressed" length="brief"/>',
         '<say emotion="normal">在的</say>'],
    ])
    chat = ChatService(db, provider, None, None, _noop, cfg=config, broker=_FakeBroker())
    context = _group_context(chat._registry)
    await chat.send(InboundMessage(text="月璃出来", context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    system = provider.messages[0][0]['content']
    assert '绝大多数时候都该写 brief' in system
    assert '二三十个字' in system
    assert '不是写小作文' in system
    assert '会作为一条独立消息发出去' in system


async def test_legacy_pipeline_history_keeps_no_message_id_labels(db) -> None:
    """[编号] 前缀只服务动作头，旧管线历史必须保持原样。"""
    config = Config()
    config.bot.name = "月璃"
    config.conversation_agent.mode = "shadow"
    provider = _ScriptedProvider([
        [
            '<decision action="reply" targets="1" reasons="directly_addressed" length="brief"/>',
            '<say emotion="normal">在的</say>',
        ],
        ['<say emotion="normal">旧管线回复</say>'],
    ])
    chat = ChatService(db, provider, None, None, _noop, cfg=config, broker=_FakeBroker())
    context = _group_context(chat._registry)
    await chat.send(InboundMessage(text="月璃出来", context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    assert provider.calls == 2
    agent_user = [m for m in provider.messages[0][1:] if m['role'] == 'user']
    legacy_user = [m for m in provider.messages[1][1:] if m['role'] == 'user']
    assert any(
        m['content'].startswith('[1] ') and '小李: 月璃出来' in m['content']
        for m in agent_user
    )
    assert any('小李: 月璃出来' in m['content'] for m in legacy_user)
    assert not any('[1] ' in m['content'] for m in legacy_user)


async def test_enabled_prompt_also_uses_action_head_protocol(db) -> None:
    """enabled 路径与 shadow 使用同一套「动作头先于正文」系统提示词。"""
    config = Config()
    config.conversation_agent.mode = "enabled"
    provider = _ScriptedProvider([
        ['<decision action="reply" targets="1" reasons="direct_question" length="brief"/><say>在的</say>'],
    ])
    chat = ChatService(db, provider, None, None, _noop, cfg=config)
    context = chat.desktop_context
    await chat.send(InboundMessage(text="在吗", context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    assert provider.calls == 1
    system = provider.messages[0][0]['content']
    assert '<decision action=' in system
    assert '只输出下列标签' not in system


async def test_shadow_frequency_mode_observes_plain_group_batches(db) -> None:
    """frequency 口径：普通群消息攒够阈值后只产生 shadow 决策，不唤醒旧管线。"""
    config = Config()
    config.bot.name = "月璃"
    config.conversation_agent.mode = "shadow"
    config.conversation_agent.trigger_mode = "frequency"
    config.conversation_agent.frequency_talk_value = 0.6
    provider = _ScriptedProvider([
        ['<decision action="silent" reasons="others_conversation"/>'],
    ])
    chat = ChatService(db, provider, None, None, _noop, cfg=config, broker=_FakeBroker())
    context = _group_context(chat._registry)

    await chat.send(InboundMessage(text="第一条普通消息", context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task
    assert provider.calls == 0

    await chat.send(InboundMessage(text="第二条普通消息", context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    assert provider.calls == 1
    shadow_events = [
        entry for entry in _action_events()
        if entry["version"]["modelTask"] == "chat.conversation.shadow"
    ]
    assert len(shadow_events) == 1
    assert shadow_events[0]["eventStatus"] == "silent_by_choice"
    assert shadow_events[0]["gate"]["reasonCodes"] == ["frequency_budget"]
    # shadow 只观察：frequency_budget 不应把旧管线也唤醒。
    assert not [
        entry for entry in event_store.search(kinds=["turn_action"]).events
        if entry["turnId"] == shadow_events[0]["turnId"]
    ]
    stored = [
        message.content
        for message in chat.memory.working_memory(context.stream.id, 20)
    ]
    assert stored == ["第一条普通消息", "第二条普通消息"]


async def test_shadow_reply_necessity_worthy_content_triggers_without_backlog(db) -> None:
    """reply_necessity 口径：写清来意的无点名群消息无需积压即可进入 DELIBERATE。"""
    config = Config()
    config.bot.name = "月璃"
    config.conversation_agent.mode = "shadow"
    config.conversation_agent.trigger_mode = "reply_necessity"
    provider = _ScriptedProvider([
        ['<decision action="silent" reasons="others_conversation"/>'],
    ])
    chat = ChatService(db, provider, None, None, _noop, cfg=config, broker=_FakeBroker())
    context = _group_context(chat._registry)

    worthy_text = (
        "这个报错怎么解决，帮我看看具体步骤，"
        + "请把每一步都讲清楚。" * 12
    )
    await chat.send(InboundMessage(text=worthy_text, context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    assert provider.calls == 1
    shadow_events = [
        entry for entry in _action_events()
        if entry["version"]["modelTask"] == "chat.conversation.shadow"
    ]
    assert len(shadow_events) == 1
    assert shadow_events[0]["eventStatus"] == "silent_by_choice"
    assert shadow_events[0]["gate"]["reasonCodes"] == ["reply_necessity"]


def test_asleep_drop_preserves_extended_pending(db) -> None:
    """asleep 硬边界不消费 frequency 累计，醒来后继续原有预算。"""
    from types import SimpleNamespace

    config = Config()
    config.bot.name = "月璃"
    config.conversation_agent.mode = "shadow"
    config.conversation_agent.trigger_mode = "frequency"
    config.conversation_agent.frequency_talk_value = 0.6
    chat = ChatService(
        db,
        _ScriptedProvider([[]]),
        None,
        None,
        _noop,
        cfg=config,
        broker=_FakeBroker(),
    )
    context = _group_context(chat._registry)

    first = chat._batch_gate(context, "第一条普通消息", False, candidate_count=1)
    assert first.result.reason_codes == ('frequency_wait',)
    assert chat._extended_pending[context.stream.id] == 1

    chat.set_sleep_state_provider(
        lambda: SimpleNamespace(asleep=True, just_woke=False, resting=False, level='light')
    )
    asleep_drop = chat._batch_gate(context, "第二条普通消息", False, candidate_count=1)
    assert asleep_drop.result.reason_codes == ('light_sleep',)
    assert chat._extended_pending[context.stream.id] == 1

    chat.set_sleep_state_provider(
        lambda: SimpleNamespace(asleep=False, just_woke=False, resting=False, level='awake')
    )
    third = chat._batch_gate(context, "第三条普通消息", False, candidate_count=1)
    assert third.result.reason_codes == ('frequency_budget',)
    assert context.stream.id not in chat._extended_pending

async def test_shadow_skips_force_candidates(db) -> None:
    """shadow 只观察 DELIBERATE；@必回 FORCE 候选不重复付费。"""
    config = Config()
    config.bot.name = "月璃"
    config.conversation_agent.mode = "shadow"
    provider = _ScriptedProvider([["<say>必回回复</say>"]])
    chat = ChatService(db, provider, None, None, _noop, cfg=config, broker=_FakeBroker())
    context = _group_context(chat._registry)
    await chat.send(InboundMessage(text="@月璃 在吗", context=context, mentioned_me=True))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    assert provider.calls == 1
    assert not [
        entry for entry in _action_events()
        if entry["version"]["modelTask"] == "chat.conversation.shadow"
    ]


async def test_enabled_desktop_reply_commits_without_decision_tag_in_history(db) -> None:
    """enabled：桌面 FORCE 一次调用产出动作头与正文，历史只落可见正文。"""
    config = Config()
    config.conversation_agent.mode = "enabled"
    provider = _ScriptedProvider([
        ['<decision action="reply" targets="1" reasons="direct_question" length="brief"/><say>在的</say>'],
    ])
    chat = ChatService(db, provider, None, None, _noop, cfg=config)
    context = chat.desktop_context
    await chat.send(InboundMessage(text="在吗", context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    assert provider.calls == 1
    events = _action_events()
    assert events[-1]["eventStatus"] == "committed"
    assert events[-1]["gate"]["disposition"] == "force"
    # 桌面终局动作只有 reply；认知动作（recall/inspect）与 stream 类型无关，
    # 因此它们出现在动作集里不违反「桌面不允许无解释的 silent」这条约束。
    assert "silent" not in events[-1]["gate"]["availableActions"]
    assert "reply" in events[-1]["gate"]["availableActions"]
    assert events[-1]["decision"]["targetMessageIds"] == [1]
    assert events[-1]["decision"]["reply"]["text"] == "在的"
    stored = [message.content for message in chat.memory.working_memory(context.stream.id, 20)]
    assert any("在的" in content for content in stored)
    assert not any("<decision" in content for content in stored)


async def test_enabled_group_silent_writes_only_action_event(db) -> None:
    """enabled：群聊 Agent 自主沉默只写行动决策事件，无任何其他副作用。"""
    config = Config()
    config.bot.name = "月璃"
    config.conversation_agent.mode = "enabled"
    provider = _ScriptedProvider([['<decision action="silent" reasons="others_conversation"/>']])
    chat = ChatService(db, provider, None, None, _noop, cfg=config, broker=_FakeBroker())
    context = _group_context(chat._registry)
    await chat.send(InboundMessage(text="月璃你好", context=context))
    await chat._tick()
    turn = chat._active_turns[context.stream.id]
    await chat._inflight[context.stream.id].task

    assert provider.calls == 1
    events = _action_events()
    assert events[-1]["eventStatus"] == "silent_by_choice"
    assert events[-1]["decision"]["action"] == "silent"
    # 无观察事件、无助手历史、无投递。
    assert not [
        entry for entry in event_store.search(turn_id=turn, kinds=["observation"]).events
    ]
    stored = [message.content for message in chat.memory.working_memory(context.stream.id, 20)]
    assert [content for content in stored if content != "月璃你好"] == []
    assert chat._broker is not None and chat._broker.dispatched == []
    stage = event_store.current_stages()[0]
    assert stage["stage"] == "gated"
    assert "她选择沉默" in stage["detail"]


async def test_enabled_force_rejects_silent_as_protocol_error(db) -> None:
    """enabled：@必回 FORCE 返回 silent 判协议错误，不降级、无正文。"""
    config = Config()
    config.bot.name = "月璃"
    config.conversation_agent.mode = "enabled"
    provider = _ScriptedProvider([['<decision action="silent" reasons="low_relevance"/>']])
    chat = ChatService(db, provider, None, None, _noop, cfg=config, broker=_FakeBroker())
    context = _group_context(chat._registry)
    await chat.send(InboundMessage(text="@月璃 在吗", context=context, mentioned_me=True))
    await chat._tick()
    turn = chat._active_turns[context.stream.id]
    await chat._inflight[context.stream.id].task

    assert provider.calls == 1
    events = _action_events()
    assert events[-1]["eventStatus"] == "illegal_action"
    assert "不在本回合可用动作" in events[-1]["detail"]
    assert not [
        entry for entry in event_store.search(turn_id=turn, kinds=["stage"]).events
        if entry["stage"] == "replied"
    ]
    stored = [message.content for message in chat.memory.working_memory(context.stream.id, 20)]
    assert [content for content in stored if content != "@月璃 在吗"] == []


async def test_enabled_parse_error_releases_nothing(db) -> None:
    """enabled：正文先于动作头按解析失败处理，桌面收到错误事件。"""
    config = Config()
    config.conversation_agent.mode = "enabled"
    provider = _ScriptedProvider([["<say>没有动作头</say>"]])
    pushed: List[tuple[str, Any, int]] = []

    async def push(channel: str, payload: Any, stream_id: int) -> None:
        pushed.append((channel, payload, stream_id))

    chat = ChatService(db, provider, None, None, push, cfg=config)
    context = chat.desktop_context
    await chat.send(InboundMessage(text="在吗", context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    assert provider.calls == 1
    events = _action_events()
    assert events[-1]["eventStatus"] == "parse_error"
    assert any(channel == "chat.error" for channel, _, _ in pushed)
    stored = [message.content for message in chat.memory.working_memory(context.stream.id, 20)]
    assert [content for content in stored if content != "在吗"] == []


async def test_enabled_batch_drop_skips_agent_and_records_reason(db) -> None:
    """enabled：批次级门控 DROP（休眠）不调用 Agent，落 gate_dropped 事件。"""
    from types import SimpleNamespace

    config = Config()
    config.bot.name = "月璃"
    config.conversation_agent.mode = "enabled"
    provider = _ScriptedProvider([[]])
    chat = ChatService(db, provider, None, None, _noop, cfg=config, broker=_FakeBroker())
    context = _group_context(chat._registry)
    await chat.send(InboundMessage(text="月璃你好", context=context))
    # 入缓冲后、批次处理前进入休眠：批次级门控必须拦截。
    chat.set_sleep_state_provider(
        lambda: SimpleNamespace(asleep=True, just_woke=False, resting=False, level='light')
    )
    await chat._tick()
    turn = chat._active_turns[context.stream.id]
    assert context.stream.id not in chat._inflight

    assert provider.calls == 0
    events = _action_events()
    assert events[-1]["eventStatus"] == "gate_dropped"
    assert events[-1]["gate"]["reasonCodes"] == ["light_sleep"]
    assert events[-1]["inputs"]["asleep"] is True
    assert [
        entry for entry in event_store.search(turn_id=turn, kinds=["observation"]).events
    ]


async def test_selected_streams_only_routes_listed_streams(db) -> None:
    """selected_streams：清单内 stream 走 Agent，清单外保留旧管线。"""
    config = Config()
    config.bot.name = "月璃"
    config.conversation_agent.mode = "selected_streams"
    config.conversation_agent.selected_streams = ["86420"]
    provider = _ScriptedProvider([
        ['<decision action="silent" reasons="attention_elsewhere"/>'],
        ["<say>旧管线</say>"],
    ])
    chat = ChatService(db, provider, None, None, _noop, cfg=config, broker=_FakeBroker())
    listed = _group_context(chat._registry, "86420")
    unlisted = _group_context(chat._registry, "99999")
    await chat.send(InboundMessage(text="月璃你好", context=listed))
    await chat._tick()
    await chat._inflight[listed.stream.id].task
    await chat.send(InboundMessage(text="月璃你好", context=unlisted))
    await chat._tick()
    await chat._inflight[unlisted.stream.id].task

    assert provider.calls == 2
    events = _action_events()
    assert events[-1]["eventStatus"] == "silent_by_choice"
    turn_actions = event_store.search(kinds=["turn_action"]).events
    assert turn_actions[-1]["decisionSource"] == "TurnPlanner(AlwaysReplyPolicy)"


def test_conversation_agent_config_defaults_off() -> None:
    """默认灰度关闭：不持有 Agent，旧管线一字不改。"""
    config = Config()

    assert config.conversation_agent.mode == "off"
    assert config.conversation_agent.selected_streams == []

def test_batch_gate_natural_window_uses_last_reply_age(db) -> None:
    """自然回应窗口按上一条 Bot 回复的年龄判定，十分钟计数不再扩大触发面。"""
    config = Config()
    config.bot.name = "月璃"
    chat = ChatService(db, None, None, None, _noop, cfg=config)
    context = _group_context(chat._registry)
    now = current_time()
    chat.memory.append_message(
        context.stream.id, None, 'assistant', '<say>刚回复</say>', now - 30_000,
    )

    fresh = chat._batch_gate(context, '普通群消息', False, candidate_count=1)

    assert fresh.result.disposition == 'deliberate'
    assert 'natural_reply_window' in fresh.result.reason_codes

    # 时限过期但消息距离仍近时改由 ongoing_topic 接住：她开口之后群里没聊几句，
    # 话题没有走远。两条口径量纲不同，这里验证时限那条确实已经失效。
    context2 = _group_context(chat._registry, "86421")
    chat.memory.append_message(
        context2.stream.id, None, 'assistant', '<say>更早回复</say>', now - 120_000,
    )
    stale_time = chat._batch_gate(context2, '普通群消息', False, candidate_count=1)

    assert stale_time.result.reason_codes == ('ongoing_topic',)

    # 时限与消息距离双双过期，才回落到无信号过滤。
    for index in range(ONGOING_TOPIC_MESSAGE_SPAN):
        chat.memory.append_message(
            context2.stream.id, 1, 'user', f'后续第{index}条', now - 110_000 + index,
        )
    stale_both = chat._batch_gate(context2, '普通群消息', False, candidate_count=1)

    assert stale_both.result.disposition == 'drop'
    assert stale_both.result.reason_codes == ('attention_filtered',)


def test_batch_gate_natural_window_closes_after_she_declines(db) -> None:
    """她在跟进机会里选择沉默后窗口关闭，重新开口后再次敞开。

    关闭条件是她自己的终局动作，与十分钟窗口内回过几条无关；本用例直接操作
    服务持有的放弃集合，验证门控读取的是这一事实而不是回复计数。
    """
    config = Config()
    config.bot.name = "月璃"
    chat = ChatService(db, None, None, None, _noop, cfg=config)
    context = _group_context(chat._registry)
    now = current_time()
    chat.memory.append_message(
        context.stream.id, None, 'assistant', '<say>刚回复</say>', now - 30_000,
    )

    opened = chat._batch_gate(context, '普通群消息', False, candidate_count=1)
    assert 'natural_reply_window' in opened.result.reason_codes

    chat._follow_up_declined.add(context.stream.id)
    declined = chat._batch_gate(context, '普通群消息', False, candidate_count=1)
    assert declined.result.disposition == 'drop'
    assert declined.result.reason_codes == ('attention_filtered',)

    chat._follow_up_declined.discard(context.stream.id)
    reopened = chat._batch_gate(context, '普通群消息', False, candidate_count=1)
    assert 'natural_reply_window' in reopened.result.reason_codes


async def test_new_message_ends_continuation_and_starts_fresh_turn(
    db,
    monkeypatch,
) -> None:
    """模型生成期间到达的消息留在缓冲区，并由下一回合读取最新上下文。"""
    config = Config()
    config.bot.name = "月璃"
    config.conversation_agent.mode = "enabled"
    provider = _CrossedProvider()
    chat = ChatService(db, provider, None, None, _noop, cfg=config, broker=_FakeBroker())
    context = _group_context(chat._registry)

    await chat.send(InboundMessage(text="月璃你好", context=context))
    await chat._tick()
    first_turn = chat._inflight[context.stream.id]
    await provider.first_started.wait()
    await chat.send(InboundMessage(text="插队消息", context=context))
    provider.release_first.set()
    await first_turn.task
    await asyncio.sleep(0)

    # 第一回合不能把生成期间到达的消息拿去续跑；消息仍等待正常轮询。
    assert len(provider.calls) == 1
    assert [message.text for message in chat._buffers[context.stream.id]] == ['插队消息']

    await chat._tick()
    second_turn = chat._inflight[context.stream.id]
    assert second_turn is not first_turn
    await second_turn.task

    assert len(provider.calls) == 2
    second_history = provider.calls[1]
    previous_reply_index = next(
        index for index, message in enumerate(second_history)
        if message.get('role') == 'assistant' and '上一条回复正文' in message.get('content', '')
    )
    current_user_index = next(
        index for index, message in enumerate(second_history)
        if message.get('role') == 'user' and '插队消息' in message.get('content', '')
    )
    assert previous_reply_index < current_user_index
    current_user = second_history[current_user_index]
    assert '插队消息' in current_user.get('content', '')
    # 已经回复过的首条消息不应继续留在新回合的当前候选块里。
    assert '月璃你好' not in current_user.get('content', '')
    assert '[本回合续跑]' not in current_user.get('content', '')


class _TwoStageProvider:
    """记录每次调用的消息序列，并按调用次序返回决策头 / 正文。"""

    def __init__(self, script: List[str]) -> None:
        self.script = script
        self.messages: List[List[dict[str, Any]]] = []

    async def stream(
        self,
        messages: List[dict[str, Any]] | None = None,
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, str]]:
        self.messages.append(list(messages or []))
        index = min(len(self.messages), len(self.script)) - 1
        yield {'text': self.script[index]}


async def test_split_replyer_end_to_end(db) -> None:
    """开启拆分后：决策走 planner、正文走 replyer，发出去的是 replyer 那句。

    同时验证决策层写的 reference 确实出现在回复生成那一次的提示词里——这条
    自由文本通道是两级之间唯一的语义带宽，断了就等于让 replyer 盲写。
    """
    config = Config()
    config.bot.name = "月璃"
    config.conversation_agent.mode = "enabled"
    config.conversation_agent.split_replyer = True
    config.conversation_agent.tool_calling = False
    planner = _TwoStageProvider([
        '<decision action="reply" targets="1" reasons="direct_question" '
        'length="brief" reference="他问在不在，随口应一声"/>'
        '<say>决策模型写的正文，不许发出去</say>',
    ])
    replyer = _TwoStageProvider(['<say>在的</say>'])
    broker = _FakeBroker()
    chat = ChatService(
        db, planner, None, None, _noop, cfg=config, broker=broker,
        planner_provider=planner, replyer_provider=replyer,
    )
    context = _group_context(chat._registry)

    await chat.send(InboundMessage(text="月璃在吗", context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    # 两级各调用一次。
    assert len(planner.messages) == 1
    assert len(replyer.messages) == 1
    # 发出去的是回复生成模型那句。
    delivered = [segment for message in broker.dispatched for segment in message.segments]
    assert delivered == ['在的']
    # 决策层的背景说明进了回复生成的提示词。
    replyer_prompt = '\n'.join(
        item.get('content', '') for item in replyer.messages[0]
    )
    assert '他问在不在，随口应一声' in replyer_prompt
    # 回复生成那一次不该再带动作头协议。
    assert '只能写当前允许的动作之一' not in replyer_prompt


async def test_split_disabled_keeps_single_call(db) -> None:
    """开关关闭时仍是单次调用，正文来自同一次决策流。"""
    config = Config()
    config.bot.name = "月璃"
    config.conversation_agent.mode = "enabled"
    config.conversation_agent.split_replyer = False
    planner = _TwoStageProvider([
        '<decision action="reply" targets="1" reasons="direct_question" length="brief"/>'
        '<say>单次调用的正文</say>',
    ])
    replyer = _TwoStageProvider(['<say>不该被调用</say>'])
    broker = _FakeBroker()
    chat = ChatService(
        db, planner, None, None, _noop, cfg=config, broker=broker,
        planner_provider=planner, replyer_provider=replyer,
    )
    context = _group_context(chat._registry)

    await chat.send(InboundMessage(text="月璃在吗", context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    assert len(replyer.messages) == 0
    delivered = [segment for message in broker.dispatched for segment in message.segments]
    assert delivered == ['单次调用的正文']


async def test_split_layers_persona_between_stages(db) -> None:
    """人格分两层：决策那一次不带表达样本，回复生成那一次才补上。

    表达层三块（回复风格、语调、表达样本）只影响「话怎么说」，决策层用不上；
    留着既占上下文，也会诱导决策模型顺手把台词写了。
    """
    config = Config()
    config.bot.name = "月璃"
    config.personality.reply_style = "说话要短"
    config.conversation_agent.mode = "enabled"
    config.conversation_agent.split_replyer = True
    config.conversation_agent.tool_calling = False
    planner = _TwoStageProvider([
        '<decision action="reply" targets="1" reasons="direct_question" '
        'length="brief" reference="随口应一声"/>',
    ])
    replyer = _TwoStageProvider(['<say>在的</say>'])
    chat = ChatService(
        db, planner, None, None, _noop, cfg=config, broker=_FakeBroker(),
        planner_provider=planner, replyer_provider=replyer,
    )
    context = _group_context(chat._registry)

    await chat.send(InboundMessage(text="月璃在吗", context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    planner_system = planner.messages[0][0]['content']
    replyer_system = replyer.messages[0][0]['content']
    # 身份与人格两级都要有：判断「她这种人会不会这么做」靠的正是这些。
    assert '月璃' in planner_system and '月璃' in replyer_system
    # 回复风格只给会说话的那一级。
    assert '说话要短' not in planner_system
    assert '说话要短' in replyer_system


class _ToolCallProvider:
    """产出工具调用的决策替身，记录收到的工具声明与提示词。"""

    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments
        self.messages: List[List[dict[str, Any]]] = []
        self.tools: List[List[dict[str, Any]]] = []

    async def stream(
        self,
        messages: List[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        self.messages.append(list(messages or []))
        self.tools.append(list(kwargs.get('tools') or []))
        yield {'tool_calls': [{
            'id': 'call_1', 'name': self.name, 'arguments': self.arguments,
        }]}


class _ForwardToolPlanner:
    """先读取合并转发，再按同一消息编号选择回复。"""

    def __init__(self) -> None:
        self.message_id = 0
        self.messages: List[List[Dict[str, Any]]] = []
        self.tools: List[List[Dict[str, Any]]] = []

    async def stream(
        self,
        messages: List[Dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[Dict[str, Any]]:
        self.messages.append(list(messages or []))
        self.tools.append(list(kwargs.get('tools') or []))
        if len(self.messages) == 1:
            yield {'tool_calls': [{
                'id': 'read-forward',
                'name': 'read_forward_message',
                'arguments': f'{{"message_id": {self.message_id}}}',
            }]}
            return
        yield {'tool_calls': [{
            'id': 'reply-after-forward',
            'name': 'reply',
            'arguments': (
                f'{{"target": {self.message_id}, '
                '"reasons": ["direct_question"], "length": "brief", '
                '"reference": "已经读完转发正文"}'
            ),
        }]}


class _DirectSceneProvider:
    """为主动跟进测试返回固定私聊场景。"""

    async def stream(
        self,
        messages: List[dict[str, Any]] | None = None,
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, str]]:
        yield {'text': '{"topic":"上一个问题还没聊完","atmosphere":"平淡"}'}


async def test_tool_calling_end_to_end(db) -> None:
    """开启工具调用后：决策以工具下发与回收，正文仍由回复生成模型写。

    同时验证提示词里不再出现 XML 那套输出协议——两套协议同时在场时模型会在
    「写标签」和「调工具」之间摇摆。
    """
    config = Config()
    config.bot.name = "月璃"
    config.conversation_agent.mode = "enabled"
    config.conversation_agent.split_replyer = True
    assert config.conversation_agent.tool_calling is True
    planner = _ToolCallProvider(
        'reply',
        '{"target": 1, "reasons": ["direct_question"], "length": "brief", '
        '"reference": "他直接问了，应一声"}',
    )
    replyer = _TwoStageProvider(['<say>在的</say>'])
    broker = _FakeBroker()
    chat = ChatService(
        db, planner, None, None, _noop, cfg=config, broker=broker,
        planner_provider=planner, replyer_provider=replyer,
    )
    context = _group_context(chat._registry)

    await chat.send(InboundMessage(text="月璃在吗", context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    delivered = [segment for message in broker.dispatched for segment in message.segments]
    assert delivered == ['在的']
    # 工具声明真的下发了，且目标取值就是本轮可选消息。
    reply_tool = next(
        item['function'] for item in planner.tools[0]
        if item['function']['name'] == 'reply'
    )
    assert reply_tool['parameters']['properties']['target']['enum'] == ['1']
    planner_messages = planner.messages[0]
    assert planner_messages[0]['role'] == 'system'
    assert all(item['role'] == 'user' for item in planner_messages[1:])
    tool_protocol = planner_messages[-1]['content']
    assert '给 target 用的记号' in tool_protocol
    assert '由 target 决定' in tool_protocol
    assert 'targets' not in tool_protocol
    # 时间与人物画像各占一个 item，不再和稳定人格糊进同一个 system。
    assert len([
        item for item in planner_messages
        if item.get('content', '').startswith('[当前时间]')
    ]) == 1
    assert len([
        item for item in planner_messages
        if item.get('content', '').startswith('[人物画像]')
    ]) == 1
    assert '[当前时间]' not in planner_messages[0]['content']
    assert '[人物画像]' not in planner_messages[0]['content']
    # 工具协议独立留在最后，成为决策侧最近的一条约束。
    assert planner_messages[-1]['content'].startswith('你的任务是分析聊天和聊天中的互动情况')
    # 决策提示词里不再有 XML 输出协议与 few-shot。
    planner_prompt = '\n'.join(item.get('content', '') for item in planner_messages)
    assert '[输出要求]' not in planner_prompt
    assert '[输出格式示例]' not in planner_prompt
    assert '<decision' not in planner_prompt


async def test_forward_message_tool_is_wired_into_live_chat(db) -> None:
    """真实聊天装配应缓存入站树、逐层回灌观察，再正常产生回复。"""
    config = Config()
    config.bot.name = '月璃'
    config.conversation_agent.mode = 'enabled'
    config.conversation_agent.split_replyer = True
    planner = _ForwardToolPlanner()
    replyer = _TwoStageProvider(['<say>我看完转发了。</say>'])
    broker = _FakeBroker()
    chat = ChatService(
        db,
        planner,
        None,
        None,
        _noop,
        cfg=config,
        broker=broker,
        planner_provider=planner,
        replyer_provider=replyer,
    )
    context = _group_context(chat._registry)
    tree = ForwardMessageTree(nodes=(ForwardNode(
        sender_name='转发中的用户',
        parts=(ForwardMessagePart.text_part('需要模型读到的正文'),),
    ),))

    await chat.send(InboundMessage(
        text='月璃看一下这个[转发消息]',
        context=context,
        forward_messages=(tree,),
    ))
    planner.message_id = chat._buffers[context.stream.id][0].message_id
    await chat._tick()
    await chat._inflight[context.stream.id].task

    assert len(planner.messages) == 2
    first_tool_names = {
        item['function']['name'] for item in planner.tools[0]
    }
    assert 'read_forward_message' in first_tool_names
    forward_tool = next(
        item['function']
        for item in planner.tools[0]
        if item['function']['name'] == 'read_forward_message'
    )
    assert forward_tool['parameters']['properties']['depth']['minimum'] == 1
    assert forward_tool['parameters']['properties']['offset']['minimum'] == 0
    observation = planner.messages[1][-1]['content']
    assert '需要模型读到的正文' in observation
    assert f'"message_id": {planner.message_id}' in observation
    delivered = [
        segment for message in broker.dispatched for segment in message.segments
    ]
    assert delivered == ['我看完转发了']


def test_tool_item_history_keeps_storage_order_and_bot_marker(db) -> None:
    """拍平后保留事件落库顺序，不再为角色交替搬动跨轮回复。"""
    config = Config()
    config.bot.name = '月璃'
    config.conversation_agent.mode = 'enabled'
    config.conversation_agent.split_replyer = True
    config.conversation_agent.tool_calling = True
    planner = _ToolCallProvider('silent', '{"reasons": ["others_conversation"]}')
    replyer = _TwoStageProvider(['<say>不会调用</say>'])
    chat = ChatService(
        db, planner, None, None, _noop, cfg=config, broker=_FakeBroker(),
        planner_provider=planner, replyer_provider=replyer,
    )
    context = _group_context(chat._registry)
    now = 1_800_000_000_000
    chat.memory.append_message(
        context.stream.id, context.person.id, 'user', '上一轮问题', now,
    )
    current_id = chat.memory.append_message(
        context.stream.id, context.person.id, 'user', '插队消息', now + 1,
    )
    chat.memory.append_message(
        context.stream.id,
        None,
        'assistant',
        '<say>上一轮回复</say><mood favor="+1"/>',
        now + 2,
    )

    prepared = chat._prepare_turn_context(
        context,
        '插队消息',
        now + 3,
        user_message_id_watermark=current_id,
        batch_message_ids=(current_id,),
    )

    history = prepared.agent_history
    assert [item['role'] for item in history] == ['user', 'user', 'user']
    previous_index = next(i for i, item in enumerate(history) if '上一轮问题' in item['content'])
    current_index = next(i for i, item in enumerate(history) if '插队消息' in item['content'])
    bot_index = next(i for i, item in enumerate(history) if '月璃: 上一轮回复' in item['content'])
    assert previous_index < current_index < bot_index
    assert f'[{current_id}]' in history[current_index]['content']
    # 两类行使用对称且不混淆的标记：她自己固定为 [我]，别人只有消息编号。
    assert history[bot_index]['content'].startswith('[我] ')
    assert '[我]' not in history[previous_index]['content']
    assert '[我]' not in history[current_index]['content']
    assert '<say' not in history[bot_index]['content']
    assert '<mood' not in history[bot_index]['content']
    # 同一份 prepared 仍给 shadow 后的旧管线保留逻辑角色顺序。
    assert [item['role'] for item in prepared.raw_history] == [
        'user', 'assistant', 'user',
    ]
    assert '上一轮回复' in prepared.raw_history[1]['content']
    assert '插队消息' in prepared.raw_history[2]['content']


async def test_tool_calling_shadow_discards_generated_reply_body(db) -> None:
    """工具动作需由 replyer 补齐合法正文，但 shadow 不得投递或落库。"""
    config = Config()
    config.bot.name = '月璃'
    config.conversation_agent.mode = 'shadow'
    config.conversation_agent.split_replyer = True
    config.conversation_agent.tool_calling = True
    legacy = _TwoStageProvider(['<say>旧管线回复</say>'])
    planner = _ToolCallProvider(
        'reply',
        '{"target": 1, "reasons": ["direct_question"], "length": "brief", '
        '"reference": "影子模式也要形成完整规划"}',
    )
    replyer = _TwoStageProvider(['<say>不应落库的影子正文</say>'])
    chat = ChatService(
        db, legacy, None, None, _noop, cfg=config, broker=_FakeBroker(),
        planner_provider=planner, replyer_provider=replyer,
    )
    context = _group_context(chat._registry)

    await chat.send(InboundMessage(text='月璃在吗', context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    assert len(planner.messages) == 1
    assert len(replyer.messages) == 1
    assert len(legacy.messages) == 1
    shadow_events = [
        entry for entry in _action_events()
        if entry['version']['modelTask'] == 'chat.conversation.shadow'
    ]
    assert len(shadow_events) == 1
    assert shadow_events[0]['eventStatus'] == 'committed'
    assert shadow_events[0]['decision']['action'] == 'reply'
    stored = [
        message.content
        for message in chat.memory.working_memory(context.stream.id, 20)
    ]
    assert any('旧管线回复' in content for content in stored)
    assert not any('不应落库的影子正文' in content for content in stored)


async def test_tool_calling_direct_follow_up_uses_item_stream(db) -> None:
    """主动跟进的 planner/replyer 也必须走同一套扁平 item 协议。"""
    config = Config()
    config.bot.name = '月璃'
    config.conversation_agent.mode = 'enabled'
    config.conversation_agent.split_replyer = True
    config.conversation_agent.tool_calling = True
    planner = _ToolCallProvider('reply', '{}')
    replyer = _TwoStageProvider(['<say emotion="smile">我再问一句</say>'])
    chat = ChatService(
        db,
        planner,
        None,
        _DirectSceneProvider(),
        _noop,
        cfg=config,
        broker=_FakeBroker(),
        planner_provider=planner,
        replyer_provider=replyer,
    )
    context = _direct_context(chat._registry)
    now = current_time()
    target_id = chat.memory.append_message(
        context.stream.id,
        context.person.id,
        'user',
        '那然后呢？',
        now - 2_000,
    )
    chat.memory.append_message(
        context.stream.id,
        None,
        'assistant',
        '<say>我也在想</say>',
        now - 1_000,
    )
    planner.arguments = (
        f'{{"target": {target_id}, "reasons": ["topic_continuation"], '
        '"length": "brief", "reference": "上一段对话还没聊完"}'
    )
    target = next(
        message for message in chat.memory.working_memory(context.stream.id, 20)
        if message.message_id == target_id
    )

    result = await chat._decide_direct_follow_up(
        context,
        target,
        '对方静默了一会儿，这是一次可选的自然续接机会。',
    )

    assert result is not None
    assert result[1] == [{'text': '我再问一句', 'emotion': 'smile'}]
    assert len(planner.messages) == 1
    assert len(replyer.messages) == 1
    assert all(item['role'] == 'user' for item in planner.messages[0][1:])
    assert all(item['role'] == 'user' for item in replyer.messages[0][1:])
    assert any(
        '# 私聊情景分析结果' in item['content']
        for item in planner.messages[0]
    )
    assert any(
        '# 当前主动跟进机会' in item['content']
        for item in planner.messages[0]
    )
    assert planner.messages[0][-1]['content'].startswith('你的任务是分析聊天和聊天中的互动情况')
    assert replyer.messages[0][-1]['content'].startswith('本轮需要发言')


async def test_tool_calling_flattens_history_into_user_stream(db) -> None:
    """工具模式下历史拍平成 user 流，她自己的发言带显示名前缀。

    拍平的意义不在省事，而在拆掉一整条脆弱链路：角色必须交替这条端点要求，
    逼出了「把跨轮落库的回复重排到批次之前」，重排又可能把它顶到历史开头被
    normalize_history 丢掉。拍平之后不存在开头 assistant，也就没有那个丢法。
    """
    config = Config()
    config.bot.name = "月璃"
    config.conversation_agent.mode = "enabled"
    config.conversation_agent.split_replyer = True
    config.conversation_agent.tool_calling = True
    planner = _ToolCallProvider(
        'reply',
        '{"target": 3, "reasons": ["direct_question"], "length": "brief", '
        '"reference": "接着上一句"}',
    )
    replyer = _TwoStageProvider(['<say>好</say>'])
    chat = ChatService(
        db, planner, None, None, _noop, cfg=config, broker=_FakeBroker(),
        planner_provider=planner, replyer_provider=replyer,
    )
    context = _group_context(chat._registry)
    # 先造一段「用户说话 → 她回过话」的历史，再让她面对新消息做决策。
    chat.memory.append_message(context.stream.id, context.person.id, 'user', '月璃你好')
    chat.memory.append_message(context.stream.id, None, 'assistant', '<say>你好呀</say>')

    await chat.send(InboundMessage(text="在干嘛", context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    history = planner.messages[0][1:]
    # 决策提示词里除 system 外全是 user：没有 assistant 就没有「开头 assistant
    # 被丢弃」这个失败模式。
    assert {item['role'] for item in history} == {'user'}
    merged = '\n'.join(item['content'] for item in history)
    # 她自己说过的话仍然在，且能看出是谁说的。
    assert '月璃: 你好呀' in merged
    assert '月璃你好' in merged
