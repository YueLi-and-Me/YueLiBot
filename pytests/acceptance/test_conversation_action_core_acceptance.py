"""Conversation 行动核心 14 条验收标准对照。"""

from __future__ import annotations

from typing import Any, AsyncIterator, List

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.agent.action_protocol import (
    ActionDecisionEvent,
    ConversationDecision,
    GateInputFacts,
    PlatformCapabilities,
    available_actions,
)
from src.core.api.auth import token_manager
from src.core.api.http import PlatformInboundBody, platform_inbound, router
from src.core.api.state import app_state
from src.core.config.schema import Config
from src.core.observe.store import event_store
from src.core.platform_io.registry import StreamRegistry
from src.core.platform_io.types import DeliveryReceipt, InboundMessage, OutboundMessage
from src.core.services.chat import ChatService


class _ScriptedProvider:
    """按调用顺序轮换分片脚本的替身提供方。"""

    def __init__(self, scripts: List[List[str]]) -> None:
        self.scripts = scripts
        self.calls = 0

    async def stream(self, **_kwargs: Any) -> AsyncIterator[dict[str, str]]:
        self.calls += 1
        script = self.scripts[min(self.calls, len(self.scripts)) - 1]
        for text in script:
            yield {'text': text}


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


def _caps(**overrides: Any) -> PlatformCapabilities:
    return PlatformCapabilities(**overrides)


def _group(registry: StreamRegistry, external_id: str = "86420"):
    return registry.resolve_inbound(
        platform="qq",
        stream_kind="group",
        stream_external_id=external_id,
        sender_external_id="97531",
        sender_nickname="账号昵称",
        sender_group_card="小李",
        first_seen_at=1_000_000,
    )


def _enabled_config() -> Config:
    config = Config()
    config.bot.name = "月璃"
    config.conversation_agent.mode = "enabled"
    return config


# 1. @必回=true 且真实 @ 时，动作集合中没有 silent。
def test_1_at_must_reply_action_set_has_no_silent() -> None:
    assert available_actions("group", "force", _caps()) == frozenset({"reply"})


async def test_1_at_must_reply_force_rejects_silent_head(db) -> None:
    config = _enabled_config()
    provider = _ScriptedProvider([['<decision action="silent" reasons="low_relevance"/>']])
    chat = ChatService(db, provider, None, None, _noop, cfg=config, broker=_FakeBroker())
    context = _group(chat._registry)
    await chat.send(InboundMessage(text="@月璃 在吗", context=context, mentioned_me=True))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    events = [e for e in event_store.search(kinds=["action_decision"]).events]
    assert events[-1]["eventStatus"] == "illegal_action"
    # FORCE 的硬约束是「不许 silent」，不是「只许 reply」：先检索再回复不违反
    # @必回契约，因此认知动作允许出现在 FORCE 的动作集里。
    assert "silent" not in events[-1]["gate"]["availableActions"]
    assert "reply" in events[-1]["gate"]["availableActions"]


# 2. 名字/别名出现时进入 DELIBERATE，但代码里不写死回复。
async def test_2_name_mention_enters_deliberate_and_agent_may_stay_silent(db) -> None:
    from src.core.agent.conversation_gate import GateRequest, decide_disposition

    result = decide_disposition(GateRequest(
        stream_kind="group",
        mentioned_me=False,
        name_mentioned=True,
        sleep_level='awake',
        at_mention_must_reply=True,
        replies_in_window=0,
        max_replies_in_window=3,
    ))
    assert result.disposition == "deliberate"

    config = _enabled_config()
    provider = _ScriptedProvider([['<decision action="silent" reasons="attention_elsewhere"/>']])
    chat = ChatService(db, provider, None, None, _noop, cfg=config, broker=_FakeBroker())
    context = _group(chat._registry)
    await chat.send(InboundMessage(text="月璃你好", context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    events = [e for e in event_store.search(kinds=["action_decision"]).events]
    assert events[-1]["eventStatus"] == "silent_by_choice"
    assert chat._broker is not None and chat._broker.dispatched == []


# 2b. DELIBERATE 枚举封闭到实际会 emit 的码，不留死枚举。
def test_2b_deliberate_enum_has_no_unemitted_codes() -> None:
    from src.core.agent.conversation_gate import (
        DELIBERATE_GATE_CODES,
        GateRequest,
        decide_disposition,
    )

    assert 'ordinary_group_message' not in DELIBERATE_GATE_CODES
    result = decide_disposition(GateRequest(
        stream_kind='group',
        mentioned_me=False,
        name_mentioned=False,
        sleep_level='awake',
        at_mention_must_reply=True,
        replies_in_window=0,
        max_replies_in_window=3,
    ))
    assert result.disposition == 'drop'
    assert result.reason_codes == ('attention_filtered',)


# 3. 名字、别名、符号全部从 Bot 配置动态读取。
def test_3_names_come_from_config() -> None:
    from src.core.agent.conversation_gate import mentions_bot_name

    names = ("Bot#7", "星", "NOVA!")
    assert mentions_bot_name("xbot#7y", names) is True
    assert mentions_bot_name("关于星河的故事", names) is True
    assert mentions_bot_name("xnova!y", names) is True
    assert mentions_bot_name("平台登录昵称，在吗", names) is False


# 4. 私聊、桌面动作集合不含 silent。
def test_4_direct_and_desktop_have_no_silent() -> None:
    assert available_actions("desktop", "force", _caps()) == frozenset({"reply"})
    assert available_actions("direct", "force", _caps()) == frozenset({"reply"})


# 5. DROP 场景不调用模型，并记录确定性原因。
async def test_5_drop_does_not_call_model_and_records_reason(db) -> None:
    previous = (app_state.chat, app_state.registry,
                app_state.register_platform_stream, app_state.group_chat_config)
    config = Config()
    config.bot.name = "月璃"
    provider = _ScriptedProvider([[]])
    chat = ChatService(db, provider, None, None, _noop, cfg=config)
    registry = StreamRegistry(db)
    app_state.chat = chat
    app_state.registry = registry
    app_state.register_platform_stream = None
    app_state.group_chat_config = config.group_chat
    try:
        response = await platform_inbound(PlatformInboundBody(
            platform="qq",
            streamKind="group",
            streamExternalId="86420",
            senderExternalId="97531",
            senderNickname="账号昵称",
            senderGroupCard="小李",
            text="这句不带称呼",
            mentionedMe=False,
            externalMessageId="6",
        ))
    finally:
        (app_state.chat, app_state.registry,
         app_state.register_platform_stream, app_state.group_chat_config) = previous

    assert '"accepted":false' in response.body.decode("utf-8")
    assert provider.calls == 0
    events = [e for e in event_store.search(kinds=["action_decision"]).events]
    assert events[-1]["eventStatus"] == "gate_dropped"
    assert events[-1]["gate"]["reasonCodes"] == ["attention_filtered"]


# 6. DELIBERATE 场景一次模型调用同时产出行动决策与正文。
async def test_6_deliberate_one_call_produces_decision_and_text(db) -> None:
    config = _enabled_config()
    provider = _ScriptedProvider([
        ['<decision action="reply" targets="1" reasons="direct_question" length="brief"/><say>在的</say>'],
    ])
    broker = _FakeBroker()
    chat = ChatService(db, provider, None, None, _noop, cfg=config, broker=broker)
    context = _group(chat._registry)
    await chat.send(InboundMessage(text="月璃在吗", context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    assert provider.calls == 1
    events = [e for e in event_store.search(kinds=["action_decision"]).events]
    assert events[-1]["eventStatus"] == "committed"
    assert events[-1]["decision"]["action"] == "reply"
    assert events[-1]["decision"]["reply"]["text"] == "在的"
    assert len(broker.dispatched) == 1


# 7. silent 不产生用户消息 / TTS / facts / mood / promise 副作用，只写一条行动决策事件。
async def test_7_silent_has_no_side_effects(db) -> None:
    config = _enabled_config()
    provider = _ScriptedProvider([['<decision action="silent" reasons="others_conversation"/>']])
    broker = _FakeBroker()
    chat = ChatService(db, provider, None, None, _noop, cfg=config, broker=broker)
    context = _group(chat._registry)
    await chat.send(InboundMessage(text="月璃你好", context=context))
    await chat._tick()
    turn = chat._active_turns[context.stream.id]
    await chat._inflight[context.stream.id].task

    action_events = [e for e in event_store.search(turn_id=turn, kinds=["action_decision"]).events]
    assert len(action_events) == 1
    assert action_events[0]["eventStatus"] == "silent_by_choice"
    assert not event_store.search(turn_id=turn, kinds=["memory_fact"]).events
    assert not event_store.search(turn_id=turn, kinds=["mood_delta"]).events
    assert not event_store.search(turn_id=turn, kinds=["promise_stashed"]).events
    assert not event_store.search(turn_id=turn, kinds=["outbound_delivered"]).events
    assert broker.dispatched == []
    stored = [m.content for m in chat.memory.working_memory(context.stream.id, 20)]
    assert [c for c in stored if c != "月璃你好"] == []


# 8. silent（自主）与模型失败 / 投递失败是不同的 event_status。
def test_8_event_status_distinguishes_silence_from_failures() -> None:
    from src.core.agent.action_protocol import EventStatus

    distinct = {
        "silent_by_choice", "gate_dropped", "timeout", "provider_error",
        "parse_error", "illegal_action", "delivery_failed", "committed",
        # 认知轮的终态：她去查了一下，本回合尚未结束。既不是她定了要做什么，
        # 也不是失败，必须与上面八种都分开。
        "cognitive_step",
    }
    assert distinct == set(EventStatus.__args__)


# 9. target_message_ids 不能超出 selectable_message_ids。
async def test_9_target_outside_selectable_is_illegal(db) -> None:
    config = _enabled_config()
    provider = _ScriptedProvider([
        ['<decision action="reply" targets="103" reasons="direct_question" length="brief"/><say>在</say>'],
    ])
    chat = ChatService(db, provider, None, None, _noop, cfg=config, broker=_FakeBroker())
    context = _group(chat._registry)
    await chat.send(InboundMessage(text="月璃在吗", context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    events = [e for e in event_store.search(kinds=["action_decision"]).events]
    assert events[-1]["eventStatus"] == "illegal_action"
    assert "不在本回合 selectable_message_ids 内" in events[-1]["detail"]


# 10. 引用目标不能晚于当前消息水位；目标不能指向另一人物历史。
def test_10_quote_bounded_by_selectable_snapshot() -> None:
    from src.core.agent.action_protocol import DecisionFrame, DecisionHead, IllegalActionError

    frame = DecisionFrame(
        turn_id=1,
        snapshot_id="s",
        stream_kind="group",
        disposition="deliberate",
        selectable_message_ids=(101, 102),
        message_watermark=102,
        available_actions=frozenset({"reply"}),
        capabilities=_caps(quote=True),
    )
    head = DecisionHead(
        action="reply",
        target_message_ids=(101,),
        quote_message_id=102,
        reason_codes=("direct_question",),
        length="brief",
    )
    head.validate(frame)
    try:
        DecisionHead(
            action="reply",
            target_message_ids=(101,),
            quote_message_id=103,
            reason_codes=("direct_question",),
            length="brief",
        ).validate(frame)
    except IllegalActionError as exc:
        assert "不在本回合 selectable_message_ids 内" in str(exc)
    else:
        raise AssertionError("引用 103 应被拒绝")


# 11. 平台不支持引用 / reaction 时，模型看不到对应能力。
def test_11_platform_without_capabilities_hides_them() -> None:
    from src.core.agent.prompt import render_action_protocol

    assert available_actions("group", "deliberate", _caps()) == frozenset({"reply", "silent"})
    text = render_action_protocol(
        ["reply", "silent"], [(101, "凌白: 在吗")], quote_supported=False
    )
    assert "不要写 quote 属性" in text
    # 动作清单里只有 reply / silent，react 未获得平台能力不会出现。
    assert "reply / silent" in text
    assert "reply / silent / react" not in text


# 12. reply 动作头校验完成后才允许正文流出。
async def test_12_body_is_held_until_head_is_valid(db) -> None:
    from src.core.agent.conversation import ConversationAgent
    from src.core.agent.parser import DecisionEvent, ParseEvent, TextEvent
    from src.core.agent.action_protocol import DecisionFrame, GateInputFacts

    provider = _ScriptedProvider([
        ['<decision action="reply" targets="101" reasons="direct_question" length="brief"/><say>在</say>'],
    ])
    agent = ConversationAgent(provider, temperature=0.7)
    frame = DecisionFrame(
        turn_id=9,
        snapshot_id="s9",
        stream_kind="group",
        disposition="deliberate",
        selectable_message_ids=(101,),
        message_watermark=101,
        available_actions=frozenset({"reply", "silent"}),
        capabilities=_caps(),
    )
    inputs = GateInputFacts(
        stream_kind="group",
        mentioned_me=False,
        name_mentioned=True,
        must_reply=False,
        asleep=False,
        rate_limited=False,
        recent_bot_replies=0,
        candidate_message_ids=(101,),
        selectable_message_ids=(101,),
    )
    collected: List[ParseEvent] = []

    async def collect(events: List[ParseEvent]) -> None:
        collected.extend(events)

    outcome = await agent.run(frame, [{"role": "system", "content": "s"}], inputs,
                              ("name_mention",), on_events=collect)

    assert outcome.event_status == "committed"
    # 动作头校验通过后正文事件才放出：无回调时正文进入结果事件列表。
    assert collected
    assert not any(isinstance(e, DecisionEvent) for e in collected)


# 13. 非法动作不静默降级成普通回复；FORCE 场景返回 silent 视为协议错误。
async def test_13_illegal_action_never_downgrades_to_plain_reply(db) -> None:
    config = _enabled_config()
    provider = _ScriptedProvider([['<decision action="dance" targets="1" reasons="direct_question" length="brief"/><say>在</say>']])
    broker = _FakeBroker()
    chat = ChatService(db, provider, None, None, _noop, cfg=config, broker=broker)
    context = _group(chat._registry)
    await chat.send(InboundMessage(text="月璃在吗", context=context))
    await chat._tick()
    turn = chat._active_turns[context.stream.id]
    await chat._inflight[context.stream.id].task

    events = [e for e in event_store.search(kinds=["action_decision"]).events]
    assert events[-1]["eventStatus"] == "illegal_action"
    assert "未知动作：dance" in events[-1]["detail"]
    assert broker.dispatched == []
    assert not [
        entry for entry in event_store.search(turn_id=turn, kinds=["stage"]).events
        if entry["stage"] == "replied"
    ]


# 14. reason_codes、门控输入、available_actions、snapshot、prompt hash、模型信息可查询。
async def test_14_action_decision_is_queryable_via_http(db) -> None:
    config = _enabled_config()
    provider = _ScriptedProvider([
        ['<decision action="reply" targets="1" reasons="direct_question" length="brief"/><say>在的</say>'],
    ])
    chat = ChatService(db, provider, None, None, _noop, cfg=config, broker=_FakeBroker())
    context = _group(chat._registry)
    await chat.send(InboundMessage(text="月璃在吗", context=context))
    await chat._tick()
    turn = chat._active_turns[context.stream.id]
    await chat._inflight[context.stream.id].task

    token_manager.configure("action-core-token")
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer action-core-token"
        response = client.get(f"/events?turnId={turn}&kind=action_decision")

    assert response.status_code == 200
    entries = [entry for entry in response.json()["events"]
               if entry["kind"] == "action_decision"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["eventStatus"] == "committed"
    assert entry["inputs"]["nameMentioned"] is True
    assert {"reply", "silent"} <= set(entry["gate"]["availableActions"])
    assert entry["decision"]["reasonCodes"] == ["direct_question"]
    assert entry["snapshotId"] == f"turn-{turn}"
    assert entry["version"]["promptHash"]
    assert entry["version"]["modelTask"] == "chat.conversation"
