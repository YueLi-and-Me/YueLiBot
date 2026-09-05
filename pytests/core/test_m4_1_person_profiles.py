"""验证会话信息与人物画像的拆分及 HTTP 读取边界。

本模块覆盖 owner、联系人和群成员的关系轴、事实记录与不存在人物的 404 响应，
依赖 FastAPI 只读接口和真实数据库模型。
"""

from __future__ import annotations

from typing import Any

import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.api.auth import token_manager
from src.core.api.http import router as http_router
from src.core.api.state import app_state
from src.core.memory.store import FactInput
from src.core.persona.state import EventDelta
from src.core.platform_io.registry import StreamRegistry
from src.core.config.schema import Config
from src.core.services.chat import ChatService


NOW = 1_700_000_100_000


async def _noop(_channel: str, _payload: Any, _stream_id: int = 1) -> None:
    return None


def _add_group_message(
    chat: ChatService,
    registry: StreamRegistry,
    external_id: str,
    display_name: str,
    text: str,
    created_at: int,
) -> tuple[int, int]:
    context = registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id='20001',
        sender_external_id=external_id,
        sender_nickname=display_name,
        sender_group_card=display_name,
        first_seen_at=created_at,
    )
    chat.memory.append_message(
        context.stream.id,
        context.person.id,
        'user',
        text,
        created_at,
    )
    return context.stream.id, context.person.id


def test_conversation_snapshot_contains_participants_without_person_fields(
    db: sqlite3.Connection,
) -> None:
    """会话页只保留会话数据，并列出群里出现过的所有人。"""
    registry = StreamRegistry(db)
    chat = ChatService(db, None, None, None, _noop, cfg=Config())
    stream_id, first_person_id = _add_group_message(
        chat,
        registry,
        '10001',
        '小李',
        '第一句',
        NOW,
    )
    same_stream_id, second_person_id = _add_group_message(
        chat,
        registry,
        '10002',
        '小王',
        '第二句',
        NOW + 1,
    )

    payload = chat.observability_snapshot(stream_id, now=NOW + 2)

    assert same_stream_id == stream_id
    assert first_person_id != second_person_id
    assert 'persona' not in payload
    assert 'semantic' not in payload.get('memory', {})
    assert payload['conversation']['workingMessages'] == 2
    assert all(
        set(participant) == {
            'id', 'kind', 'displayName', 'externalId', 'nickname', 'groupCard',
        }
        for participant in payload['conversation']['participants']
    )
    assert {
        (participant['id'], participant['displayName'])
        for participant in payload['conversation']['participants']
    } == {
        (first_person_id, '小李'),
        (second_person_id, '小王'),
    }


def test_person_profiles_keep_bonds_and_facts_isolated(db: sqlite3.Connection) -> None:
    """owner 与联系人分别读取自己的关系轴和事实，不得共享画像。"""
    registry = StreamRegistry(db)
    owner = registry.owner_person()
    registry.link_identity(owner, 'qq', '90001', '本人账号')
    contact = registry.create_person('contact', first_seen_at=NOW - 86_400_000)
    registry.link_identity(contact, 'qq', '10001', '小李')
    chat = ChatService(db, None, None, None, _noop, cfg=Config())

    chat.persona.apply_event(contact.id, EventDelta(favor=3), NOW, weight=1.0)
    chat.memory.add_fact(owner.id, FactInput(kind='偏好', content='用户本人喜欢咖啡'), NOW)
    chat.memory.add_fact(contact.id, FactInput(kind='偏好', content='小李喜欢红茶'), NOW)

    owner_profile = chat.person_profile(owner.id, now=NOW + 1)
    contact_profile = chat.person_profile(contact.id, now=NOW + 1)

    assert owner_profile['displayName'] == '本人账号'
    assert contact_profile['displayName'] == '小李'
    assert owner_profile['bond'] != contact_profile['bond']
    assert [fact['content'] for fact in owner_profile['facts']] == ['用户本人喜欢咖啡']
    assert [fact['content'] for fact in contact_profile['facts']] == ['小李喜欢红茶']
    assert owner_profile['streams'] == []
    assert contact_profile['streams'] == []


def test_person_profile_exposes_qq_nickname_and_current_group_card(
    db: sqlite3.Connection,
) -> None:
    """人物画像用 QQ 号稳定定位，并把当前昵称、当前群名片分栏返回。"""
    registry = StreamRegistry(db)
    context = registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id='20001',
        sender_external_id='10001',
        sender_nickname='真实昵称',
        sender_group_card='当前群名片',
        first_seen_at=NOW,
    )
    chat = ChatService(db, None, None, None, _noop, cfg=Config())
    chat.memory.append_message(
        context.stream.id,
        context.person.id,
        'user',
        '在群里说话',
        NOW,
    )

    profile = chat.person_profile(context.person.id, now=NOW + 1)
    snapshot = chat.observability_snapshot(context.stream.id, now=NOW + 1)

    assert profile['identities'] == [{
        'platform': 'qq',
        'externalId': '10001',
        'displayName': '真实昵称',
    }]
    assert profile['groupMemberships'] == [{
        'streamId': context.stream.id,
        'groupExternalId': '20001',
        'groupCard': '当前群名片',
    }]
    assert snapshot['conversation']['participants'] == [{
        'id': context.person.id,
        'kind': 'contact',
        'displayName': '当前群名片',
        'externalId': '10001',
        'nickname': '真实昵称',
        'groupCard': '当前群名片',
    }]


def test_opening_contact_profile_does_not_write_default_bond(
    db: sqlite3.Connection,
) -> None:
    """只读人物页不能因为联系人尚未互动就写入一行默认关系状态。"""
    registry = StreamRegistry(db)
    contact = registry.create_person('contact', first_seen_at=NOW)
    registry.link_identity(contact, 'qq', '10003', '小张')
    chat = ChatService(db, None, None, None, _noop, cfg=Config())

    profile = chat.person_profile(contact.id, now=NOW + 1)
    stored = db.execute(
        'SELECT person_id FROM persona_bond WHERE person_id = ?',
        (contact.id,),
    ).fetchone()

    assert profile['bond']['intimacy'] == 12.0
    assert stored is None


def test_person_detail_api_returns_404_without_owner_fallback(
    db: sqlite3.Connection,
) -> None:
    """不存在的人物必须返回 404，不得回退到 owner 画像。"""
    previous_registry = app_state.registry
    previous_chat = app_state.chat
    token_manager.configure('m4-person-test-token')
    app_state.registry = StreamRegistry(db)
    app_state.chat = ChatService(db, None, None, None, _noop, cfg=Config())
    app = FastAPI()
    app.include_router(http_router)

    try:
        with TestClient(app) as client:
            response = client.get(
                '/api/persons/99999',
                headers={'Authorization': 'Bearer m4-person-test-token'},
            )
    finally:
        app_state.registry = previous_registry
        app_state.chat = previous_chat

    assert response.status_code == 404
    assert 'person 99999 不存在' in response.json()['detail']


def test_person_list_api_returns_503_when_service_is_uninitialized() -> None:
    """人物画像服务未初始化必须暴露错误，不能伪装成没有人物。"""
    previous_chat = app_state.chat
    token_manager.configure('m4-person-test-token')
    app_state.chat = None
    app = FastAPI()
    app.include_router(http_router)

    try:
        with TestClient(app) as client:
            response = client.get(
                '/api/persons',
                headers={'Authorization': 'Bearer m4-person-test-token'},
            )
    finally:
        app_state.chat = previous_chat

    assert response.status_code == 503
    assert response.json()['detail'] == '人物画像服务未初始化'


def test_人物画像的会话条目必须带显示名(db: sqlite3.Connection) -> None:
    """streams 里每一项都要有 displayName，群名为空时给空串而不是缺键。

    前端契约把 displayName 声明成必填字符串，streamLabel() 对群会话直接
    .trim()。漏发这一项时 TypeScript 查不出来（类型说有、运行时没有），
    人物详情页在渲染时抛 TypeError 整树卸载，表现为白屏——只要这个人在
    任何群里出现过就必现。这条断言盯的就是「键必须在」。
    """
    registry = StreamRegistry(db)
    chat = ChatService(db, None, None, None, _noop, cfg=Config())
    _stream_id, person_id = _add_group_message(
        chat, registry, '10086', '群友甲', '在群里说了句话', NOW,
    )

    profile = chat.person_profile(person_id, now=NOW + 1)

    assert profile['streams'], '这个人在群里发过言，会话列表不该为空'
    for stream in profile['streams']:
        assert 'displayName' in stream, f'会话 {stream["id"]} 缺 displayName'
        assert isinstance(stream['displayName'], str)
