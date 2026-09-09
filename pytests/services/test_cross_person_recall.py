"""主动检索跨在场者（K2）验收。

「在不在场」不再是主动检索的边界：非群聊会话且当前对话者是 owner 时，
recall 的人物范围放开到全库，可见性规则逐条照旧；其余会话类型与其他
对话者照旧收窄。被动注入不经过这条链，不受影响。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, List

import sqlite3

from src.core.agent.action_protocol import (
    DecisionFrame,
    GateInputFacts,
    PlatformCapabilities,
    available_actions,
)
from src.core.agent.cognition import CognitiveRequest, CognitiveScope, RecallAction
from src.core.agent.conversation import ConversationAgent
from src.core.config.schema import Config
from src.core.memory.scope import ORIGIN_GROUP
from src.core.memory.store import FactInput
from src.core.observe.store import event_store
from src.core.platform_io.types import DeliveryReceipt, InboundMessage
from src.core.services.chat import ChatService
from src.core.tooling.cognitive import CognitiveToolExecutor
from src.core.tooling.registry import build_builtin_action_registry

NOW = 1_800_000_000_000


def _absent_qq_person(chat: ChatService) -> int:
    """构造一个只在群里出现过、只有 QQ 身份的人物，并落一条 group 来源事实。"""

    context = chat._registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id='629201002',
        sender_external_id='3209184542',
        sender_nickname='不思量デス',
        sender_group_card='不思量',
        first_seen_at=NOW,
    )
    chat.memory.add_fact(
        context.person.id,
        FactInput(
            kind='事件',
            content='3209184542 玩《原神》这款游戏',
            origin_kind=ORIGIN_GROUP,
        ),
        NOW,
    )
    return context.person.id


def _make_chat(db: sqlite3.Connection) -> ChatService:
    async def _noop(_channel: str, _payload: object, _stream_id: int = 1) -> None:
        return None

    return ChatService(
        db=db,
        chat_provider=None,
        proactive_provider=None,
        summary_provider=None,
        push_event=_noop,
        cfg=Config(),
    )


def _frame(stream_kind: str) -> DecisionFrame:
    caps = PlatformCapabilities()
    return DecisionFrame(
        turn_id=7,
        snapshot_id='snap-7',
        stream_kind=stream_kind,
        disposition='deliberate',
        selectable_message_ids=(101,),
        message_watermark=101,
        available_actions=available_actions(
            stream_kind, 'deliberate', caps, cognitive_rounds_left=2,
        ),
        capabilities=caps,
    )


async def test_recall_action_cross_person_hits_absent_person(db: sqlite3.Connection) -> None:
    """★K2-1：放开人物范围后，不在场人物的 group 来源事实能被 recall 命中。"""

    chat = _make_chat(db)
    person_id = _absent_qq_person(chat)
    action = RecallAction(chat.memory, chat._registry.stream_display_name_or_any, db)

    result = await action.execute(CognitiveRequest(
        action='recall',
        query='原神',
        stream_id=1,
        stream_kind='direct',
        person_ids=(1,),
        message_watermark=0,
        cross_person=True,
    ))

    assert result.hit_count > 0
    assert '不思量デス' in result.text
    assert '原神' in result.text
    assert person_id != 1


async def test_recall_action_default_scope_misses_absent_person(db: sqlite3.Connection) -> None:
    """★K2-1 对照：同一构造不开跨人物时命中数为 0。"""

    chat = _make_chat(db)
    _absent_qq_person(chat)
    action = RecallAction(chat.memory, chat._registry.stream_display_name_or_any, db)

    result = await action.execute(CognitiveRequest(
        action='recall',
        query='原神',
        stream_id=1,
        stream_kind='direct',
        person_ids=(1,),
        message_watermark=0,
    ))

    assert result.hit_count == 0
    assert '没有想起' in result.text


def test_cognitive_scope_gate_truth_table(db: sqlite3.Connection) -> None:
    """两个条件是与关系：非群聊会话且当前对话者是 owner，缺一不可。"""

    chat = _make_chat(db)
    cases = [
        ('direct', 'owner', True),
        ('desktop', 'owner', True),
        ('direct', 'contact', False),
        ('desktop', 'contact', False),
        ('group', 'owner', False),
        ('group', 'contact', False),
    ]
    for stream_kind, person_kind, expected in cases:
        scope = chat._cognitive_scope(
            _frame(stream_kind), 1, stream_kind, person_kind,
        )
        assert scope.cross_person is expected, (stream_kind, person_kind)


class _ChatProvider:
    """按脚本逐轮产出 XML 动作的替身聊天模型。"""

    def __init__(self, scripts: List[List[str]]) -> None:
        self.scripts = scripts
        self.calls = 0
        self.messages: List[List[dict]] = []

    async def stream(self, messages=None, **_kwargs: Any):
        index = self.calls
        self.calls += 1
        self.messages.append(list(messages or []))
        for text in self.scripts[min(index, len(self.scripts) - 1)]:
            yield {'text': text}


class _Broker:
    def __init__(self) -> None:
        self.dispatched: List[Any] = []

    async def dispatch(self, message: Any) -> Any:
        self.dispatched.append(message)
        return DeliveryReceipt(
            platform=message.stream.platform,
            stream_id=message.stream.id,
            external_message_ids=['fake-1'],
        )


async def test_desktop_recall_of_qq_only_person_resolves_display_name(
    db: sqlite3.Connection,
) -> None:
    """★K2-3：桌面端问到只有 QQ 身份的人物时不抛异常，观察里显示名非空。

    桌面 stream 的平台是 ``desktop``，只有 QQ 身份的人物在本平台查不到显示名，
    严格解析会让整次检索按本机故障失败；宽容变体退回任一平台身份名。
    """

    async def _noop(_channel: str, _payload: Any, _stream_id: int) -> None:
        return None

    config = Config()
    config.bot.name = '月璃'
    config.conversation_agent.mode = 'enabled'
    config.conversation_agent.max_cognitive_rounds = 2
    provider = _ChatProvider([
        ['<decision action="recall" query="原神"/>'],
        [
            '<decision action="reply" targets="1" reasons="pending_thread" length="brief"/>',
            '<say emotion="smile">知道，他玩原神。</say>',
        ],
    ])
    chat = ChatService(db, provider, None, None, _noop, cfg=config, broker=_Broker())
    _absent_qq_person(chat)

    desktop = chat.desktop_context
    await chat.send(InboundMessage(text='你知道3209184542这个人吗', context=desktop))
    await chat._tick()
    await chat._inflight[desktop.stream.id].task

    assert provider.calls == 2
    observation = provider.messages[1][-1]['content']
    assert '不思量デス' in observation
    assert '原神' in observation


class _ScriptedToolProvider:
    """工具模式替身：逐轮返回脚本里的工具调用，并保留实际收到的消息流。"""

    def __init__(self, scripts: List[List[tuple[str, str]]]) -> None:
        self.scripts = scripts
        self.calls_made = 0

    async def stream(self, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        index = self.calls_made
        self.calls_made += 1
        if index >= len(self.scripts):
            raise AssertionError(f'工具脚本只准备了 {len(self.scripts)} 轮')
        yield {'tool_calls': [
            {'id': f'call_{i + 1}', 'name': name, 'arguments': arguments}
            for i, (name, arguments) in enumerate(self.scripts[index])
        ]}


class _Replyer:
    async def stream(self, **kwargs: Any) -> AsyncIterator[dict[str, str]]:
        del kwargs
        yield {'text': '<say>好。</say>'}


class _FakeAction:
    """返回固定观察的替身认知动作。"""

    name = 'recall'

    async def execute(self, request: Any) -> Any:
        from src.core.agent.cognition import CognitiveObservation

        return CognitiveObservation(text='查到了。', hit_count=1)


def _gate_inputs() -> GateInputFacts:
    return GateInputFacts(
        stream_kind='group',
        mentioned_me=False,
        name_mentioned=True,
        must_reply=False,
        asleep=False,
        rate_limited=False,
        recent_bot_replies=0,
        candidate_message_ids=(101,),
        selectable_message_ids=(101,),
    )


async def _run_tool_round(cross_person: bool) -> None:
    registry = build_builtin_action_registry()
    registry.bind_action_executor('recall', CognitiveToolExecutor(_FakeAction()))
    agent = ConversationAgent(
        _ScriptedToolProvider([
            [('recall', '{"query": "原神"}')],
            [(
                'reply',
                '{"target": 101, "reasons": ["pending_thread"], "length": "brief", '
                '"reference": "接着回答"}',
            )],
        ]),
        temperature=0.7,
        replyer=_Replyer(),
        tool_calling=True,
        tool_registry=registry,
    )

    async def replyer_messages(_head: Any) -> list[dict]:
        return [
            {'role': 'system', 'content': '只负责把话说出来'},
            {'role': 'user', 'content': '请输出 say'},
        ]

    await agent.run(
        _frame('group'),
        [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': '在么'}],
        _gate_inputs(),
        ('name_mentioned',),
        cognitive_scope=CognitiveScope(
            stream_id=3, person_ids=(11, 12), cross_person=cross_person,
        ),
        cognitive_rounds=2,
        replyer_messages=replyer_messages,
    )


async def test_tool_execution_carries_cross_person_flag() -> None:
    """每次认知工具执行都把「门开没开」落账，真机零命中时才能区分两种情形。"""

    await _run_tool_round(cross_person=True)
    await _run_tool_round(cross_person=False)

    events = list(reversed(event_store.search(kinds=['tool_execution']).events))
    recalls = [event for event in events if event['toolName'] == 'recall']
    assert [event['crossPerson'] for event in recalls] == [True, False]
