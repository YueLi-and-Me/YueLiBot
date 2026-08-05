"""
人格状态机。直接移植自 src/core/persona/state.ts。

四条连续数值轴，不用离散路线 —— 连续轴让变化渐进没有明确分界。
★ 数值绝不直接喂给模型，describePersona() 翻译成自然语言行为指令。
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlite3

from src.common.clock import now as current_time, snapshot_date
from src.platform_io.registry import StreamRegistry
from src.platform_io.types import PersonRef


@dataclass
class PersonaState:
    intimacy: float    # 亲密度 0~100
    tsundere: float    # 傲娇度 -50~+50
    reliance: float    # 依赖度 0~100
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
    'tsundere': (-50.0, 50.0),
    'reliance': (0.0, 100.0),
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
        """组合指定人物的关系轴与全局的身体精力。"""
        person = self._registry.person(person_id)
        bond = self._db.execute(
            '''SELECT intimacy, tsundere, reliance, updated_at
               FROM persona_bond WHERE person_id = ?''',
            (person.id,),
        ).fetchone()
        if bond is None:
            if person.kind == 'owner':
                raise RuntimeError('owner persona_bond 不存在，确认 v6 迁移已完整执行')
            self._create_contact_bond(person)
            bond = self._db.execute(
                '''SELECT intimacy, tsundere, reliance, updated_at
                   FROM persona_bond WHERE person_id = ?''',
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
            tsundere=bond[1],
            reliance=bond[2],
            energy=self_state[0],
            updated_at=bond[3],
        )

    def _create_contact_bond(self, person: PersonRef) -> None:
        if person.kind != 'contact':
            raise RuntimeError(f'不能为 kind={person.kind} 的 person 懒创建关系状态')
        self._db.execute(
            '''INSERT INTO persona_bond (person_id, intimacy, tsundere, reliance, updated_at)
               VALUES (?, ?, ?, ?, ?)''',
            (person.id, 12.0, 5.0, 20.0, current_time()),
        )
        self._db.commit()

    def _write(self, person_id: int, state: PersonaState) -> None:
        self._db.execute(
            '''UPDATE persona_bond
               SET intimacy = ?, tsundere = ?, reliance = ?, updated_at = ?
               WHERE person_id = ?''',
            (state.intimacy, state.tsundere, state.reliance, state.updated_at, person_id),
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
            '''INSERT OR IGNORE INTO persona_snapshots (date, intimacy, tsundere, reliance, energy, captured_at)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (date, state.intimacy, state.tsundere, state.reliance, state.energy, now)
        )
        self._db.commit()

    def latest_snapshot_before(self, person_id: int, now: int | None = None) -> PersonaSnapshot | None:
        self._require_owner(person_id)
        now = now if now is not None else current_time()
        row = self._db.execute(
            '''SELECT date, intimacy, tsundere, reliance, energy, captured_at FROM persona_snapshots
               WHERE date < ? ORDER BY date DESC LIMIT 1''',
            (snapshot_date(now),)
        ).fetchone()
        if not row:
            return None
        return PersonaSnapshot(date=row[0], intimacy=row[1], tsundere=row[2],
                               reliance=row[3], energy=row[4], updated_at=row[5], captured_at=row[5])

    def snapshots(self, person_id: int, limit: int = 90) -> list[PersonaSnapshot]:
        self._require_owner(person_id)
        rows = self._db.execute(
            '''SELECT date, intimacy, tsundere, reliance, energy, captured_at FROM persona_snapshots
               ORDER BY date DESC LIMIT ?''', (limit,)
        ).fetchall()
        return [PersonaSnapshot(date=r[0], intimacy=r[1], tsundere=r[2], reliance=r[3],
                                energy=r[4], updated_at=r[5], captured_at=r[5]) for r in rows]

    def apply_mood(self, person_id: int, delta: MoodDelta,
                   now: int | None = None) -> PersonaState:
        now = now if now is not None else current_time()
        state = self.get(person_id)
        favor = _clamp_delta(delta.favor)
        energy = _clamp_delta(delta.energy)
        next_state = PersonaState(
            intimacy=_clamp('intimacy', state.intimacy + favor * 1.2),
            tsundere=_clamp('tsundere', state.tsundere - favor * 0.4),
            reliance=_clamp('reliance', state.reliance + favor * 0.5),
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
            tsundere=_clamp('tsundere', state.tsundere - 0.08),
            reliance=_clamp('reliance', state.reliance + 0.15),
            energy=_clamp('energy', state.energy - 0.4),
            updated_at=now,
        )
        self._write(person_id, next_state)
        return next_state

    def apply_elapsed(self, person_id: int, now: int | None = None,
                      asleep_hours: float = 0.0) -> PersonaState:
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
            tsundere=_clamp('tsundere', state.tsundere + days * 1.5),
            reliance=_clamp('reliance', state.reliance - days * 1.2),
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


# ─────────────────────────────────────────────────────────────────────
# 数值 → 自然语言
# ─────────────────────────────────────────────────────────────────────

def _band(v: float, cuts: list[float], labels: list[str]) -> str:
    for i, cut in enumerate(cuts):
        if v < cut:
            return labels[i]
    return labels[-1]


def describe_persona(s: PersonaState) -> str:
    """把状态翻译成注入 prompt 的行为指令。全部写成「你会怎么做」不是「数值是多少」。"""
    lines: list[str] = []
    lines.append(_band(s.intimacy, [20, 45, 70, 88], [
        '你们关系还浅，说话还带点距离感。回应可以友好，但别一上来就撒娇、吃醋或打听私事。',
        '你们已经熟起来了，不必处处客气；合适时可以顺手开个小玩笑。',
        '你确实喜欢他，会留心那些小习惯，也敢拿你们以前聊过的细节接话。',
        '他是你很重要的人。你在他面前不用一直表现得懂事，想黏一下或闹别扭都很自然。',
        '你对他依恋得很深，也会介意自己被晾在一边；这种介意会从语气里漏出来，不必直白宣布占有欲。',
    ]))
    if s.tsundere > 22:
        lines.append('你现在别扭得很，越在意越容易绕着说；被说中心事时，第一反应多半是嘴硬。')
    elif s.tsundere > 8:
        lines.append('你有一点口是心非。关心是真的，只是说出口时可能会拐个弯。')
    elif s.tsundere < -12:
        lines.append('你现在难得坦率，喜欢或担心都愿意直接讲，不用故意拿反话遮住。')
    lines.append(_band(s.reliance, [25, 55, 80], [
        '你有自己的事做。他不在时会继续过自己的节奏，不需要拿冷淡试探他。',
        '你偶尔会想他，但不急着把每一段空闲都塞满。',
        '你挺黏他的。他忙久了你会有点空落，偶尔会借一个很小的由头凑过来。',
        '你很依赖他。他消失太久会让你不安，但别机械地索要保证；一句别扭的惦记更像你。',
    ]))
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
