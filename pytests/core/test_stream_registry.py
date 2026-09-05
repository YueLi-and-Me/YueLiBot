"""StreamRegistry 的单桌面种子与身份绑定边界。"""

from __future__ import annotations

import sqlite3

import pytest

from src.core.platform_io.registry import StreamRegistry


FIRST_SEEN_AT = 1_700_000_000_000


def test_desktop_context_uses_seeded_owner_and_stream(db: sqlite3.Connection) -> None:
    registry = StreamRegistry(db)

    context = registry.desktop_context()

    assert context.person.id == 1
    assert context.person.kind == "owner"
    assert context.stream.id == 1
    assert (context.stream.platform, context.stream.kind, context.stream.external_id) == (
        "desktop",
        "desktop",
        "desktop",
    )


def test_identity_can_link_to_owner_but_cannot_cross_person(db: sqlite3.Connection) -> None:
    registry = StreamRegistry(db)
    owner = registry.owner_person()
    contact = registry.create_person("contact", FIRST_SEEN_AT)

    registry.link_identity(owner, "qq", "10001", "桌面主人")

    assert registry.find_person_by_identity("qq", "10001") == owner
    with pytest.raises(ValueError, match="已绑定"):
        registry.link_identity(contact, "qq", "10001", "另一个人")


def test_stream_is_created_once_by_its_external_identity(db: sqlite3.Connection) -> None:
    registry = StreamRegistry(db)

    first = registry.get_or_create_stream("qq", "group", "20001")
    second = registry.get_or_create_stream("qq", "group", "20001")

    assert first == second
    assert first.id != registry.desktop_context().stream.id


def test_sole_identity_replaces_the_previous_number(db: sqlite3.Connection) -> None:
    """替换 sole identity 时必须解绑旧账号，避免旧账号继续解析为 owner。"""
    registry = StreamRegistry(db)
    owner = registry.owner_person()

    registry.set_sole_identity(owner, 'qq', '111111', '111111')
    registry.set_sole_identity(owner, 'qq', '222222', '222222')

    rows = db.execute(
        'SELECT external_id FROM identities WHERE person_id = ? AND platform = ?',
        (owner.id, 'qq'),
    ).fetchall()
    assert [row[0] for row in rows] == ['222222']
    assert registry.find_person_by_identity('qq', '111111') is None
    assert registry.find_person_by_identity('qq', '222222').id == owner.id


def test_sole_identity_leaves_contacts_alone(db: sqlite3.Connection) -> None:
    """解绑只作用于这一个 person，联系人的身份不受影响。"""
    registry = StreamRegistry(db)
    owner = registry.owner_person()
    contact = registry.create_person('contact', FIRST_SEEN_AT)
    registry.link_identity(contact, 'qq', '900000001', '凌白')

    registry.set_sole_identity(owner, 'qq', '333333', '333333')

    assert registry.find_person_by_identity('qq', '900000001').id == contact.id
