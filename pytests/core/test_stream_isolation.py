"""验证屏幕情境、会话恢复提示和记忆召回按 stream 隔离。

本模块覆盖 desktop、私聊和群聊出口的屏幕权限、会话身份和记忆范围，
确保一个 stream 的人物或历史不会泄漏到另一个 stream。
"""

from __future__ import annotations

from typing import List

import sqlite3

import pytest

from src.core.config.schema import Config
from src.core.memory.store import EpisodeInput, FactInput
from src.core.platform_io.types import ConversationContext, IdentityRef
from src.core.services.chat import ChatService


NOW = 1_800_000_000_000
FRONTEND_SENTINEL = '前台哨兵-VSCode'
SCREEN_SENTINEL = '屏幕哨兵-一段截图描述'
ACTIVITY_SENTINEL = f'{FRONTEND_SENTINEL}\n{SCREEN_SENTINEL}'


async def _noop(_channel: str, _payload: object, _stream_id: int = 1) -> None:
    return None


def _make_chat(db: sqlite3.Connection, surfaces: List[str]) -> ChatService:
    chat = ChatService(
        db=db,
        chat_provider=None,
        proactive_provider=None,
        summary_provider=None,
        push_event=_noop,
        cfg=Config(),
    )
    # 先写断言再改实现：旧代码没有这个属性，但仍会沿真实组装路径无条件注入 activity。
    chat._perception_surfaces = frozenset(surfaces)
    chat.set_activity_provider(lambda: ACTIVITY_SENTINEL)
    return chat


def _owner_context(chat: ChatService, stream_kind: str) -> ConversationContext:
    if stream_kind == 'desktop':
        return chat.desktop_context
    owner = chat._registry.owner_person()
    external_id = 'owner-qq'
    chat._registry.link_identity(owner, 'qq', external_id, '用户本人')
    stream = chat._registry.get_or_create_stream('qq', stream_kind, f'{stream_kind}-owner')
    return ConversationContext(
        stream=stream,
        person=owner,
        identity=IdentityRef(platform='qq', external_id=external_id, display_name='用户本人'),
        group_card='用户本人',
    )


def _contact_context(chat: ChatService, stream_kind: str) -> ConversationContext:
    return chat._registry.resolve_inbound(
        platform='qq',
        stream_kind=stream_kind,
        stream_external_id=f'{stream_kind}-contact',
        sender_external_id=f'{stream_kind}-contact-qq',
        sender_nickname='联系人',
        sender_group_card='联系人群名片' if stream_kind == 'group' else '',
        first_seen_at=NOW,
    )


async def _system_prompt(
    chat: ChatService,
    context: ConversationContext,
    query: str = '隔离测试',
) -> str:
    prepared = chat._prepare_turn_context(context, query, NOW)
    messages = chat._render_prepared_context(prepared)
    return messages[0]['content']


def _assert_activity_absent(system: str) -> None:
    assert FRONTEND_SENTINEL not in system
    assert SCREEN_SENTINEL not in system


def _assert_activity_present(system: str) -> None:
    assert FRONTEND_SENTINEL in system
    assert SCREEN_SENTINEL in system


async def test_group_never_receives_activity_even_when_direct_is_enabled(
    db: sqlite3.Connection,
) -> None:
    """group 不属于可配置出口，即使其他出口全开也不能出现屏幕情境。"""
    chat = _make_chat(db, ['desktop', 'direct'])

    system = await _system_prompt(chat, _contact_context(chat, 'group'))

    _assert_activity_absent(system)


async def test_group_owner_never_receives_activity(db: sqlite3.Connection) -> None:
    """owner 在场不改变 group 的出口属性。"""
    chat = _make_chat(db, ['desktop', 'direct'])

    system = await _system_prompt(chat, _owner_context(chat, 'group'))

    _assert_activity_absent(system)


async def test_direct_owner_has_no_activity_with_default_surface(
    db: sqlite3.Connection,
) -> None:
    """默认只允许 desktop，QQ 私聊不得获得屏幕情境。"""
    chat = _make_chat(db, ['desktop'])

    system = await _system_prompt(chat, _owner_context(chat, 'direct'))

    _assert_activity_absent(system)


async def test_direct_owner_receives_activity_when_enabled(db: sqlite3.Connection) -> None:
    """用户显式开放 direct 后，owner 私聊可以获得屏幕情境。"""
    chat = _make_chat(db, ['desktop', 'direct'])

    system = await _system_prompt(chat, _owner_context(chat, 'direct'))

    _assert_activity_present(system)


async def test_direct_contact_never_receives_activity(db: sqlite3.Connection) -> None:
    """私聊出口开放后仍只对 owner 生效，联系人不得获得屏幕情境。"""
    chat = _make_chat(db, ['desktop', 'direct'])

    system = await _system_prompt(chat, _contact_context(chat, 'direct'))

    _assert_activity_absent(system)


async def test_desktop_owner_keeps_activity(db: sqlite3.Connection) -> None:
    """默认 desktop 链路保持屏幕情境能力。"""
    chat = _make_chat(db, ['desktop'])

    system = await _system_prompt(chat, _owner_context(chat, 'desktop'))

    _assert_activity_present(system)


def test_group_surface_is_rejected_in_chinese_at_load_time() -> None:
    """group 不是默认关闭，而是在配置结构上不可选。"""
    with pytest.raises(ValueError, match='群聊.*屏幕'):
        Config.model_validate({'perception': {'surfaces': ['desktop', 'group']}})


@pytest.mark.parametrize(
    ('stream_kind', 'expects_resumption'),
    [('group', False), ('direct', True), ('desktop', True)],
)
async def test_owner_resumption_wording_depends_on_conversation_surface(
    db: sqlite3.Connection,
    stream_kind: str,
    expects_resumption: bool,
) -> None:
    """会话恢复措辞依据当前 stream，owner 关系判据不能让群聊绕过限制。"""
    chat = _make_chat(db, ['desktop'])
    context = _owner_context(chat, stream_kind)
    chat.memory.append_message(
        context.stream.id,
        context.person.id,
        'user',
        '三天前的旧消息',
        NOW - 3 * 24 * 60 * 60_000,
    )
    chat._refresh_session(context, NOW)

    system = await _system_prompt(chat, context)

    assert ('距离你们上次' in system) is expects_resumption


async def test_group_recall_is_scoped_to_current_person_and_stream(
    db: sqlite3.Connection,
) -> None:
    """10：facts 跟 person，episodes 跟 stream；这是既有正确行为的钉死测试。"""
    chat = _make_chat(db, ['desktop'])
    current = _contact_context(chat, 'group')
    other_person = chat._registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id=current.stream.external_id,
        sender_external_id='other-person-qq',
        sender_nickname='另一人',
        sender_group_card='另一人群名片',
        first_seen_at=NOW,
    )
    other_stream = chat._registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id='other-group',
        sender_external_id=current.identity.external_id,
        sender_nickname='联系人',
        sender_group_card='别群名片',
        first_seen_at=NOW,
    )
    chat.memory.add_fact(
        current.person.id,
        FactInput(content='m4isolation 当前说话人的事实'),
        NOW,
    )
    chat.memory.add_fact(
        other_person.person.id,
        FactInput(content='m4isolation 另一人的事实'),
        NOW,
    )
    chat.memory.add_episode(
        current.stream.id,
        EpisodeInput(
            summary='m4isolation 当前群的情节',
            cues=['m4isolation'],
            started_at=NOW - 2,
            ended_at=NOW - 1,
            message_ids=[],
        ),
        NOW,
    )
    chat.memory.add_episode(
        other_stream.stream.id,
        EpisodeInput(
            summary='m4isolation 另一个群的情节',
            cues=['m4isolation'],
            started_at=NOW - 2,
            ended_at=NOW - 1,
            message_ids=[],
        ),
        NOW,
    )

    system = await _system_prompt(chat, current, 'm4isolation')

    assert '当前说话人的事实' in system
    assert '另一人的事实' not in system
    assert '当前群的情节' in system
    assert '另一个群的情节' not in system
