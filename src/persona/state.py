"""
人格状态机。

关系只保留按人物隔离的好感度；精力是唯一的全局身体状态。
★ 连续数值绝不直接喂给模型，describe_persona() 只翻译成自然语言行为指令。
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlite3

from src.agent.relationship import relationship_tier
from src.common.clock import now as current_time, snapshot_date
from src.platform_io.registry import StreamRegistry
from src.platform_io.types import PersonRef


@dataclass
class PersonaState:
    intimacy: float    # 好感度 0~100
    energy: float      # 精力   0~100
    updated_at: int


@dataclass
class PersonaSnapshot(PersonaState):
    date: str = ''
    captured_at: int = 0


@dataclass
class MoodDelta:
    favor: float | None = None
    energy: float | None = None


_RANGE: dict[str, tuple[float, float]] = {
    'intimacy': (0.0, 100.0),
    'energy': (0.0, 100.0),
}


def _clamp(key: str, v: float) -> float:
    lo, hi = _RANGE[key]
    return max(lo, min(hi, v))


def _clamp_delta(v: float | None) -> float:
    if v is None or not (-1e9 < v < 1e9):
        return 0.0
    return max(-3.0, min(3.0, v))


class Persona:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self._registry = StreamRegistry(db)

    def get(self, person_id: int) -> PersonaState:
        """组合指定人物的好感度与全局身体精力。"""
        person = self._registry.person(person_id)
        bond = self._db.execute(
            'SELECT intimacy, updated_at FROM persona_bond WHERE person_id = ?',
            (person.id,),
        ).fetchone()
        if bond is None:
            if person.kind == 'owner':
                raise RuntimeError('owner persona_bond 不存在，确认 v7 迁移已完整执行')
            self._create_contact_bond(person)
            bond = self._db.execute(
                'SELECT intimacy, updated_at FROM persona_bond WHERE person_id = ?',
                (person.id,),
            ).fetchone()
        if bond is None:
            raise RuntimeError(f'person {person.id} 的 persona_bond 创建失败')
        self_state = self._db.execute(
            'SELECT energy FROM persona_self WHERE id = 1'
        ).fetchone()
        if self_state is None:
            raise RuntimeError('persona_self 行不存在，确认 v6 迁移已完整执行')
        return PersonaState(
            intimacy=bond[0],
            energy=self_state[0],
            updated_at=bond[1],
        )

    def inspect(self, person_id: int) -> PersonaState:
        """只读人物关系与全局精力，不因打开画像而写入默认关系行。"""
        person = self._registry.person(person_id)
        bond = self._db.execute(
            'SELECT intimacy, updated_at FROM persona_bond WHERE person_id = ?',
            (person.id,),
        ).fetchone()
        if bond is None:
            if person.kind == 'owner':
                raise RuntimeError('owner persona_bond 不存在，确认 v7 迁移已完整执行')
            bond = (12.0, person.first_seen_at)
        self_state = self._db.execute(
            'SELECT energy FROM persona_self WHERE id = 1'
        ).fetchone()
        if self_state is None:
            raise RuntimeError('persona_self 行不存在，确认 v6 迁移已完整执行')
        return PersonaState(
            intimacy=bond[0],
            energy=self_state[0],
            updated_at=bond[1],
        )

    def _create_contact_bond(self, person: PersonRef) -> None:
        if person.kind != 'contact':
            raise RuntimeError(f'不能为 kind={person.kind} 的 person 懒创建关系状态')
        self._db.execute(
            '''INSERT INTO persona_bond (person_id, intimacy, updated_at)
               VALUES (?, ?, ?)''',
            (person.id, 12.0, current_time()),
        )
        self._db.commit()

    def _write(self, person_id: int, state: PersonaState) -> None:
        self._db.execute(
            '''UPDATE persona_bond SET intimacy = ?, updated_at = ?
               WHERE person_id = ?''',
            (state.intimacy, state.updated_at, person_id),
        )
        self._db.execute(
            'UPDATE persona_self SET energy = ?, updated_at = ? WHERE id = 1',
            (state.energy, state.updated_at),
        )
        self._db.commit()

    def snapshot_daily(self, person_id: int, now: int | None = None) -> None:
        self._require_owner(person_id)
        now = now if now is not None else current_time()
        state = self.get(person_id)
        date = snapshot_date(now)
        self._db.execute(
            '''INSERT OR IGNORE INTO persona_snapshots (date, intimacy, energy, captured_at)
               VALUES (?, ?, ?, ?)''',
            (date, state.intimacy, state.energy, now),
        )
        self._db.commit()

    def latest_snapshot_before(
        self,
        person_id: int,
        now: int | None = None,
    ) -> PersonaSnapshot | None:
        self._require_owner(person_id)
        now = now if now is not None else current_time()
        row = self._db.execute(
            '''SELECT date, intimacy, energy, captured_at FROM persona_snapshots
               WHERE date < ? ORDER BY date DESC LIMIT 1''',
            (snapshot_date(now),),
        ).fetchone()
        if not row:
            return None
        return PersonaSnapshot(
            date=row[0],
            intimacy=row[1],
            energy=row[2],
            updated_at=row[3],
            captured_at=row[3],
        )

    def snapshots(self, person_id: int, limit: int = 90) -> list[PersonaSnapshot]:
        self._require_owner(person_id)
        rows = self._db.execute(
            '''SELECT date, intimacy, energy, captured_at FROM persona_snapshots
               ORDER BY date DESC LIMIT ?''',
            (limit,),
        ).fetchall()
        return [
            PersonaSnapshot(
                date=row[0],
                intimacy=row[1],
                energy=row[2],
                updated_at=row[3],
                captured_at=row[3],
            )
            for row in rows
        ]

    def apply_mood(
        self,
        person_id: int,
        delta: MoodDelta,
        now: int | None = None,
    ) -> PersonaState:
        now = now if now is not None else current_time()
        state = self.get(person_id)
        favor = _clamp_delta(delta.favor)
        energy = _clamp_delta(delta.energy)
        next_state = PersonaState(
            intimacy=_clamp('intimacy', state.intimacy + favor * 1.2),
            energy=_clamp('energy', state.energy + energy * 3),
            updated_at=now,
        )
        self._write(person_id, next_state)
        return next_state

    def apply_turn(self, person_id: int, now: int | None = None) -> PersonaState:
        now = now if now is not None else current_time()
        state = self.get(person_id)
        next_state = PersonaState(
            intimacy=_clamp('intimacy', state.intimacy + 0.35),
            energy=_clamp('energy', state.energy - 0.4),
            updated_at=now,
        )
        self._write(person_id, next_state)
        return next_state

    def apply_elapsed(
        self,
        person_id: int,
        now: int | None = None,
        asleep_hours: float = 0.0,
    ) -> PersonaState:
        now = now if now is not None else current_time()
        person = self._registry.person(person_id)
        state = self.get(person.id)
        if person.kind != 'owner':
            return state
        hours = max(0.0, (now - state.updated_at) / 3_600_000)
        if hours < 1:
            return state
        bounded_asleep = min(hours, max(0.0, asleep_hours))
        awake_hours = hours - bounded_asleep
        days = hours / 24
        next_state = PersonaState(
            intimacy=_clamp('intimacy', state.intimacy - days * 0.6),
            energy=_clamp('energy', state.energy + bounded_asleep * 4 - awake_hours * 2),
            updated_at=now,
        )
        self._write(person.id, next_state)
        return next_state

    def _require_owner(self, person_id: int) -> PersonRef:
        person = self._registry.person(person_id)
        if person.kind != 'owner':
            raise ValueError('persona snapshot 仅允许 owner person 读写')
        return person


def describe_persona(s: PersonaState) -> str:
    """只陈述关系深度和身体精力，具体表达交给关系决策与可配置人设。"""
    lines = [f'你和对方的关系深度：{relationship_tier(s.intimacy)}。']
    if s.energy < 20:
        lines.append('你困得厉害，句子会明显变短，反应也慢一点；除非话题正好相关，不要自动催他睡觉。')
    elif s.energy < 45:
        lines.append('你有点累，懒得把每句话说得很完整，也没力气维持过分热情。')
    elif s.energy > 85:
        lines.append('你现在精神很好，更容易接住玩笑或顺手多讲一个刚想到的细节，但不用因此变得吵闹。')
    return '\n'.join(lines)


def describe_acquaintance(first_seen_at: int, now: int | None = None) -> str:
    now = now if now is not None else current_time()
    days = (now - first_seen_at) // 86_400_000
    if days <= 0:
        return '你和他今天刚认识。'
    if days == 1:
        return '你和他昨天刚认识，还是新鲜的关系。'
    if days < 30:
        return f'你和他认识了 {days} 天。'
    months = days // 30
    return f'你和他认识了 {days} 天，差不多 {months} 个月了。'
