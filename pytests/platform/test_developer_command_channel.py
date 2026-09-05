"""开发者命令通道的隔离、鉴权与隐藏性验收。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, List, Tuple
from unittest.mock import AsyncMock

from structlog.testing import capture_logs

import json
import sqlite3

import pytest

from src.core.agent.fact_extract import advance_cursor, read_cursor
from src.core.api.http import PlatformInboundBody, platform_inbound
from src.core.api.state import app_state
from src.core.commands import register_command, registered_commands
from src.core.config.schema import Config, DeveloperConfig, GroupChatConfig
from src.core.memory.store import MemoryStore
from src.core.observe.store import event_store
from src.core.platform_io.registry import StreamRegistry
from src.core.platform_io.driver import DeliveryError
from src.core.platform_io.types import DeliveryReceipt, OutboundMessage
from src.core.services.chat import ChatService, _DirectFollowUpState


class _ChatSpy:
    """记录普通聊天入口；命令命中时该列表必须保持为空。"""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.memory = MemoryStore(db)
        self.inbound: List[Any] = []
        self.observed: List[Tuple[Any, str]] = []
        self.at_mention_must_reply = True
        self.conversation_trigger_mode = 'signal'

    def bot_names(self) -> Tuple[str, ...]:
        return ('月璃',)

    def current_sleep(self) -> SimpleNamespace:
        return SimpleNamespace(asleep=False)

    def follow_up_declined(self, stream_id: int) -> bool:
        del stream_id
        return False

    def topic_still_hers(self, stream_id: int, last_bot_reply_at: int) -> bool:
        del stream_id, last_bot_reply_at
        return False

    def record_poke_arrival(self, stream_id: int, now: int) -> int:
        del stream_id, now
        return 1

    def extended_trigger_enabled(self, context: Any) -> bool:
        del context
        return False

    async def send(self, inbound: Any) -> None:
        self.inbound.append(inbound)

    def record_group_observation(self, inbound: Any, reason: str = '') -> int:
        self.observed.append((inbound, reason))
        return 1


class _BrokerSpy:
    """记录命令通道直接投递的平台消息。"""

    def __init__(self) -> None:
        self.dispatched: List[OutboundMessage] = []

    async def dispatch(self, message: OutboundMessage) -> DeliveryReceipt:
        self.dispatched.append(message)
        return DeliveryReceipt(
            platform=message.stream.platform,
            stream_id=message.stream.id,
            external_message_ids=['command-message'],
        )


@pytest.fixture
def command_app(
    db: sqlite3.Connection,
) -> Iterator[Tuple[StreamRegistry, _ChatSpy, _BrokerSpy]]:
    """装配开启态命令通道，并在用例结束后完整恢复全局 API 状态。"""
    previous = (
        app_state.chat,
        app_state.registry,
        app_state.group_chat_config,
        app_state.developer_config,
        app_state.broker,
        app_state.register_platform_stream,
    )
    registry = StreamRegistry(db)
    registry.set_sole_identity(
        registry.owner_person(),
        platform='qq',
        external_id='owner-qq',
        display_name='主人',
    )
    chat = _ChatSpy(db)
    broker = _BrokerSpy()
    app_state.chat = chat
    app_state.registry = registry
    app_state.group_chat_config = GroupChatConfig()
    app_state.developer_config = DeveloperConfig(enabled=True)
    app_state.broker = broker
    app_state.register_platform_stream = None
    try:
        yield registry, chat, broker
    finally:
        (
            app_state.chat,
            app_state.registry,
            app_state.group_chat_config,
            app_state.developer_config,
            app_state.broker,
            app_state.register_platform_stream,
        ) = previous


def _body(
    *,
    sender: str = 'owner-qq',
    stream_kind: str = 'direct',
    mentioned_me: bool = False,
) -> PlatformInboundBody:
    """构造命令文本的 QQ 入站请求。"""
    return PlatformInboundBody(
        platform='qq',
        streamKind=stream_kind,
        streamExternalId='owner-qq' if stream_kind == 'direct' else 'group-1',
        senderExternalId=sender,
        senderNickname='发送者',
        senderGroupCard='群名片' if stream_kind == 'group' else '',
        text='/help',
        mentionedMe=mentioned_me,
        externalMessageId='incoming-1',
    )


def test_只注册_help_并公开后续注册入口(monkeypatch: pytest.MonkeyPatch) -> None:
    """通道自带命令只有 /help；其余命令由各自的包通过公共装饰器登记。

    合流前这里断言目录**等于** ['/help']。合流后 /git /version /stat 由 D2、D3
    在启动期注册，而注册表是进程内全局状态，跨用例串味会让全等断言随执行顺序时红时绿。
    改成断言通道**源码里**只有一处 @register_command——那才是「通道自带几条」
    这个作用域约束的真正载体，与别的包注册了什么无关。
    """
    import src.core.commands.registry as registry_module

    catalog = {item.name: item for item in registered_commands()}
    assert '/help' in catalog
    assert catalog['/help'].description == '列出当前已注册的开发者命令'

    source = Path(registry_module.__file__).read_text(encoding='utf-8')
    assert source.count('@register_command') == 1

    monkeypatch.setattr(registry_module, '_commands', [])
    monkeypatch.setattr(registry_module, '_command_names', set())

    @register_command('/probe', r'/probe', '测试注册入口')
    def probe_handler(context: Any) -> str:
        del context
        return 'ok'

    assert probe_handler is not None
    assert [item.name for item in registered_commands()] == ['/probe']


async def test_V1_V3_owner私聊命中补历史但不触发对话(
    db: sqlite3.Connection,
    command_app: Tuple[StreamRegistry, _ChatSpy, _BrokerSpy],
) -> None:
    """V-1/V-3：命令直投后仅补两条历史，事件、回合和抽取游标不动。"""
    registry, chat, broker = command_app
    context = registry.resolve_inbound(
        platform='qq',
        stream_kind='direct',
        stream_external_id='owner-qq',
        sender_external_id='owner-qq',
        sender_nickname='主人',
        sender_group_card='',
        first_seen_at=1,
    )
    store = MemoryStore(db)
    advance_cursor(store, context.stream.id, 7)
    messages_before = db.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
    events_before = len(event_store.search(limit=1000).events)

    response = await platform_inbound(_body())

    payload = json.loads(response.body)
    assert payload['accepted'] is True
    assert payload['reason'] == 'developer_command'
    assert payload['command'] == '/help'
    assert chat.inbound == []
    assert len(broker.dispatched) == 1
    # 帮助文本随注册表增长，不做全等断言（合流后 /git /version /stat 都会出现在里面）；
    # 本用例保留直投和游标隔离约束，仅把消息不落库更新为成功后净增两条。
    help_text = broker.dispatched[0].segments[0]
    assert help_text.startswith('开发者命令：')
    assert '/help — 列出当前已注册的开发者命令' in help_text
    assert db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == messages_before + 2
    assert len(event_store.search(limit=1000).events) == events_before
    assert read_cursor(store, context.stream.id) == 7


async def test_A2_非owner按普通聊天处理且不泄露权限(
    command_app: Tuple[StreamRegistry, _ChatSpy, _BrokerSpy],
) -> None:
    """A-2：同样的正文不返回权限信息，只进入既有聊天入口。"""
    _, chat, broker = command_app

    response = await platform_inbound(_body(sender='other-qq'))

    payload = json.loads(response.body)
    assert payload['accepted'] is True
    assert payload['reason'] != 'developer_command'
    assert [item.text for item in chat.inbound] == ['/help']
    assert broker.dispatched == []
    assert '权限' not in response.body.decode('utf-8')
    assert len(event_store.search(kinds=['reply_gate']).events) == 1


async def test_A3_关闭时owner也按普通聊天处理(
    command_app: Tuple[StreamRegistry, _ChatSpy, _BrokerSpy],
) -> None:
    """A-3：默认关闭态不能消费任何命令文本。"""
    _, chat, broker = command_app
    app_state.developer_config = DeveloperConfig(enabled=False)

    response = await platform_inbound(_body())

    assert json.loads(response.body)['reason'] != 'developer_command'
    assert [item.text for item in chat.inbound] == ['/help']
    assert broker.dispatched == []


async def test_A5_owner在群聊中同样命中且不进对话(
    command_app: Tuple[StreamRegistry, _ChatSpy, _BrokerSpy],
) -> None:
    """A-5：会话面不设限，群聊里 owner 同样命中，且与私聊一样绕过对话链路。

    原用例断言的是「群聊不命中」——2026-09-05 按用户裁定放开了私聊限制，
    代价是回复会被整群看到，能触发的仍只有 owner 一人。这里改为正向断言，
    并保留「不进 ChatService」这一条：它才是命令通道与普通消息的分界。
    """
    _, chat, broker = command_app

    response = await platform_inbound(_body(stream_kind='group', mentioned_me=True))

    payload = json.loads(response.body)
    assert payload['accepted'] is True
    assert payload['reason'] == 'developer_command'
    assert payload['command'] == '/help'
    assert chat.inbound == []
    assert len(broker.dispatched) == 1
    assert broker.dispatched[0].segments[0].startswith('开发者命令：')


@pytest.mark.parametrize('stream_kind', ['direct', 'group'])
async def test_V1_V2_V3_V5_V6_成功直投后历史可见(
    db: sqlite3.Connection,
    command_app: Tuple[StreamRegistry, _ChatSpy, _BrokerSpy],
    stream_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实存储与历史渲染覆盖角色、原文、顺序、水位及命令被算作新发言。"""
    registry, chat, broker = command_app
    body = _body(stream_kind=stream_kind)
    body.text = '  /help  '
    context = registry.resolve_inbound(
        platform=body.platform, stream_kind=body.stream_kind,
        stream_external_id=body.stream_external_id,
        sender_external_id=body.sender_external_id,
        sender_nickname=body.sender_nickname, sender_group_card=body.sender_group_card,
        first_seen_at=1,
    )
    previous_id = chat.memory.append_message(context.stream.id, context.person.id, 'user', '上一批消息', now=10)
    before = db.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
    events_before = len(event_store.search(limit=1000).events)
    assert chat.memory.has_user_messages_after(context.stream.id, previous_id) is False
    # 模拟时钟在投递期间回拨；回答的时间仍不得早于命令。
    ticks = iter([200, 100])
    monkeypatch.setattr('src.core.api.http.current_time', lambda: next(ticks))
    with capture_logs() as logs:
        await platform_inbound(body)
    rows = db.execute('SELECT * FROM messages WHERE id > ? ORDER BY id', (previous_id,)).fetchall()
    assert db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == before + 2
    assert [r['role'] for r in rows] == ['user', 'assistant']
    assert [r['content'] for r in rows] == [body.text, broker.dispatched[0].segments[0]]
    assert rows[0]['sender_person_id'] == registry.owner_person().id
    assert rows[0]['external_message_id'] == body.external_message_id
    assert rows[1]['sender_person_id'] is None
    assert rows[0]['created_at'] <= rows[1]['created_at']
    assert chat.inbound == [] and chat.observed == []
    assert len(event_store.search(limit=1000).events) == events_before
    assert event_store.search(kinds=['user_input']).events == []
    executed = [e for e in logs if e['event'] == 'developer_command_executed']
    assert len(executed) == 1
    assert executed[0]['user_message_id'] == rows[0]['id']
    assert executed[0]['assistant_message_id'] == rows[1]['id']
    # V-6：命令是普通 user 消息，必须阻止把旧一轮当作仍在等待对方发言。
    assert chat.memory.has_user_messages_after(context.stream.id, previous_id) is True
    # 旧批水位仍排除后来的命令 user；下一批自己的末条 user 允许完整历史进入。
    old_window = chat.memory.working_memory(context.stream.id, user_message_id_watermark=previous_id)
    assert rows[0]['id'] not in [m.message_id for m in old_window]
    next_id = chat.memory.append_message(context.stream.id, context.person.id, 'user', '刚才这段是什么意思？', now=300)
    history = chat.memory.working_memory(context.stream.id, user_message_id_watermark=next_id)
    renderer = ChatService.__new__(ChatService)
    renderer._registry = registry
    renderer._bot_display_name = '月璃'
    for flatten in (False, True):
        rendered = renderer._history_for_context(context, history, label_message_ids=True, flatten=flatten)
        assert body.text in rendered[1]['content']
        assert rows[1]['content'] in rendered[2]['content']


@pytest.mark.parametrize('enabled,sender', [(False, 'owner-qq'), (True, 'other-qq')])
async def test_V4_未命中不由命令通道补消息(
    db: sqlite3.Connection,
    command_app: Tuple[StreamRegistry, _ChatSpy, _BrokerSpy],
    enabled: bool, sender: str,
) -> None:
    """普通聊天入口用 spy 固定边界，断言命令层不增加任何一行。"""
    _, chat, broker = command_app
    app_state.developer_config = DeveloperConfig(enabled=enabled)
    before = db.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
    await platform_inbound(_body(sender=sender))
    assert db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == before
    assert len(chat.inbound) == 1
    assert broker.dispatched == []


@pytest.mark.parametrize('failure', ['exception', 'empty', 'invalid_type', 'delivery', 'missing_broker'])
async def test_V4_执行或投递失败不补消息(
    db: sqlite3.Connection,
    command_app: Tuple[StreamRegistry, _ChatSpy, _BrokerSpy],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """失败提示仍按旧行为直投，投递异常仍暴露；两者均不能冒充成功历史。"""
    import src.core.commands.registry as registry_module
    _, chat, broker = command_app
    before = db.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
    if failure in ('exception', 'empty', 'invalid_type'):
        monkeypatch.setattr(registry_module, '_commands', [])
        monkeypatch.setattr(registry_module, '_command_names', set())
        @register_command('/help', r'/help', '失败路径测试')
        def broken(context: Any) -> str:
            if failure == 'exception':
                raise RuntimeError('处理器原始错误')
            return '' if failure == 'empty' else None
    if failure == 'delivery':
        monkeypatch.setattr(broker, 'dispatch', AsyncMock(side_effect=DeliveryError('投递失败')))
    if failure == 'missing_broker':
        app_state.broker = None
    with capture_logs() as logs:
        if failure in ('delivery', 'missing_broker'):
            with pytest.raises((DeliveryError, RuntimeError)):
                await platform_inbound(_body())
        else:
            await platform_inbound(_body())
            assert '执行失败' in broker.dispatched[0].segments[0]
    assert db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == before
    assert not any(e['event'] == 'developer_command_executed' for e in logs)
    assert chat.inbound == []
    assert event_store.search(kinds=['user_input']).events == []


async def test_V6_追问生成期间收到命令阻止追问投递(
    db: sqlite3.Connection,
    command_app: Tuple[StreamRegistry, _ChatSpy, _BrokerSpy],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """走真实定时追问方法，在生成期间完成命令直投，钉住第二次消息主键检查。"""
    registry, _, broker = command_app
    cfg = Config()
    cfg.bot.name = '月璃'
    chat = ChatService(db, None, None, None, lambda *_: None, cfg=cfg, broker=broker)
    app_state.chat = chat
    context = registry.resolve_inbound(
        platform='qq', stream_kind='direct', stream_external_id='owner-qq',
        sender_external_id='owner-qq', sender_nickname='主人', sender_group_card='', first_seen_at=1,
    )
    target_id = chat.memory.append_message(context.stream.id, context.person.id, 'user', '我先去忙了', now=100)
    chat.memory.append_message(context.stream.id, None, 'assistant', '好呀', now=200)
    state = _DirectFollowUpState(context, 200, target_id, 200)
    chat._direct_follow_ups[context.stream.id] = state
    monkeypatch.setattr(chat, 'current_sleep', lambda: SimpleNamespace(asleep=False))
    async def deciding(*_args: Any) -> Tuple[int, List[str]]:
        await platform_inbound(_body())
        return 42, ['忙完了吗？']
    decide = AsyncMock(side_effect=deciding)
    speak = AsyncMock()
    monkeypatch.setattr(chat, '_decide_direct_follow_up', decide)
    monkeypatch.setattr(chat, '_speak_claimed_external', speak)
    assert await chat._attempt_direct_follow_up(state) is False
    decide.assert_awaited_once()
    speak.assert_not_awaited()
    assert len(broker.dispatched) == 1
    assert chat.memory.has_user_messages_after(context.stream.id, target_id) is True
    assert chat._buffers == {}
    # 命令后再次尝试同一追问也应被抑制，既有状态不生成新回合。
    assert await chat._attempt_direct_follow_up(state) is False
    decide.assert_awaited_once()
