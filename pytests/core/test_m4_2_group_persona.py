"""验证群聊人格增量的折算和睡眠状态计算。

本模块比较群聊与桌面会话的能量影响，并确认折算后的能量输入睡眠状态机时保持参数一致性。
"""

from __future__ import annotations

from typing import Callable

import sqlite3

import pytest

from src.core.awareness.sleep import SleepStateController
from src.core.config.schema import Config
from src.core.persona.state import EventDelta, Persona, PersonaState
from src.core.platform_io.types import ConversationContext, IdentityRef
from src.core.services.chat import ChatService
from src.core.schedule.timeline import ActivityTimeline


NOW = 1_800_000_000_000
GROUP_WEIGHT = 0.05


async def _noop(_channel: str, _payload: object, _stream_id: int = 1) -> None:
    return None


def _owner_group_context(chat: ChatService) -> ConversationContext:
    owner = chat._registry.owner_person()
    chat._registry.link_identity(owner, 'qq', 'owner-qq', '用户本人')
    return ConversationContext(
        stream=chat._registry.get_or_create_stream('qq', 'group', 'group-persona'),
        person=owner,
        identity=IdentityRef(
            platform='qq',
            external_id='owner-qq',
            display_name='用户本人',
        ),
        group_card='用户本人',
    )


def _restore_state(
    db: sqlite3.Connection,
    state: PersonaState,
    person_id: int,
) -> None:
    db.execute(
        'UPDATE persona_bond SET intimacy = ?, updated_at = ? WHERE person_id = ?',
        (state.intimacy, state.updated_at, person_id),
    )
    db.execute(
        'UPDATE persona_self SET energy = ?, mood = ?, updated_at = ? WHERE id = 1',
        (state.energy, state.mood, state.updated_at),
    )
    db.commit()


def _apply_turns(
    persona: Persona,
    person_id: int,
    count: int,
    weight: float,
) -> PersonaState:
    state = persona.get(person_id)
    for index in range(count):
        state = persona.apply_turn(
            person_id,
            NOW + index * 60_000,
            weight=weight,
        )
    return state


def test_group_weight_is_selected_only_for_group_stream(
    db: sqlite3.Connection,
) -> None:
    config = Config()
    chat = ChatService(db, None, None, None, _noop, cfg=config)
    group = _owner_group_context(chat)

    assert chat._persona_weight(group) == GROUP_WEIGHT
    assert chat._persona_weight(chat.desktop_context) == 1.0


def test_two_hundred_group_turns_cost_less_energy_than_twenty_desktop_turns(
    db: sqlite3.Connection,
) -> None:
    """群聊 200 轮的能量下降量必须小于桌面 20 轮。"""
    persona = Persona(db)
    person_id = 1
    before = persona.get(person_id)
    group_after = _apply_turns(persona, person_id, 200, GROUP_WEIGHT)
    group_drop = before.energy - group_after.energy
    _restore_state(db, before, person_id)
    desktop_after = _apply_turns(persona, person_id, 20, 1.0)
    desktop_drop = before.energy - desktop_after.energy

    print(f'群聊 200 轮 energy 掉幅={group_drop:.6f}')
    print(f'桌面 20 轮 energy 掉幅={desktop_drop:.6f}')
    assert group_drop < desktop_drop


def test_two_hundred_group_turns_do_not_trigger_sleep(
    db: sqlite3.Connection,
) -> None:
    """群聊折算只改变人格数值，不能凭精力自行制造睡眠事实。"""
    persona = Persona(db)
    _apply_turns(persona, 1, 200, GROUP_WEIGHT)
    db.execute(
        '''INSERT INTO activities
             (kind, doing, mood, energy_pace, mood_pace, advances,
              started_at, expected_until, ended_at, source)
           VALUES ('awake', '在处理消息', '状态平稳', 0, 0, NULL, ?, ?, NULL, 'decided')''',
        (NOW, NOW + 60_000),
    )
    db.commit()
    sleep = SleepStateController(ActivityTimeline(db)).current(NOW)

    assert sleep.asleep is False


def test_weight_is_required_keyword_only(db: sqlite3.Connection) -> None:
    """weight 无默认值：漏传须立即抛 TypeError，而非静默按全速结算。

    遗留②的硬化——防止未来新增群聊结算路径忘记打折，静默掏空全局精力且测试无感。
    """
    persona = Persona(db)

    with pytest.raises(TypeError):
        persona.apply_turn(1, NOW)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        persona.apply_event(1, EventDelta(favor=3.0, energy=-3.0), NOW)  # type: ignore[call-arg]


def test_group_intimacy_still_increases(db: sqlite3.Connection) -> None:
    """4：群聊关系积累变慢但不归零。"""
    persona = Persona(db)
    before = persona.get(1)

    after = _apply_turns(persona, 1, 200, GROUP_WEIGHT)

    assert after.intimacy > before.intimacy


def test_weight_one_matches_documented_deltas(db: sqlite3.Connection) -> None:
    """weight=1.0 时 turn 与 mood 都产出未打折的既有增量。"""
    persona = Persona(db)
    person_id = 1
    before = persona.get(person_id)

    turn = persona.apply_turn(person_id, NOW, weight=1.0)
    assert turn.intimacy - before.intimacy == pytest.approx(0.35)
    assert turn.energy - before.energy == pytest.approx(-0.4)

    _restore_state(db, before, person_id)
    mood = persona.apply_event(
        person_id,
        EventDelta(favor=3.0, energy=-3.0),
        NOW,
        weight=1.0,
    )
    assert mood.intimacy - before.intimacy == pytest.approx(3.0 * 1.2)
    assert mood.energy - before.energy == pytest.approx(-3.0 * 3.0)


@pytest.mark.parametrize(
    ('apply_delta', 'expected_intimacy_delta', 'expected_energy_delta'),
    [
        (
            lambda persona, person_id: persona.apply_turn(
                person_id,
                NOW,
                weight=GROUP_WEIGHT,
            ),
            0.35 * GROUP_WEIGHT,
            -0.4 * GROUP_WEIGHT,
        ),
        (
            lambda persona, person_id: persona.apply_event(
                person_id,
                EventDelta(favor=3.0, energy=-3.0),
                NOW,
                weight=GROUP_WEIGHT,
            ),
            3.0 * 1.2 * GROUP_WEIGHT,
            -3.0 * 3.0 * GROUP_WEIGHT,
        ),
    ],
)
def test_same_group_weight_applies_to_every_persona_delta(
    db: sqlite3.Connection,
    apply_delta: Callable[[Persona, int], PersonaState],
    expected_intimacy_delta: float,
    expected_energy_delta: float,
) -> None:
    persona = Persona(db)
    before = persona.get(1)

    after = apply_delta(persona, 1)

    assert after.intimacy - before.intimacy == pytest.approx(expected_intimacy_delta)
    assert after.energy - before.energy == pytest.approx(expected_energy_delta)


def test_group_chat_configuration_has_documented_defaults() -> None:
    group_chat = Config().group_chat

    assert group_chat.persona_weight == GROUP_WEIGHT
    assert group_chat.reply_window_minutes == 10
    assert group_chat.max_replies_in_window == 3
