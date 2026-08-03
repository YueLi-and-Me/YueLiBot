"""
人格状态机。直接移植自 src/core/persona/state.ts。

四条连续数值轴，不用离散路线 —— 连续轴让变化渐进没有明确分界。
★ 数值绝不直接喂给模型，describePersona() 翻译成自然语言行为指令。
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlite3

from yueli.common.clock import now as current_time, snapshot_date


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

    def get(self) -> PersonaState:
        row = self._db.execute(
            'SELECT intimacy, tsundere, reliance, energy, updated_at FROM persona WHERE id = 1'
        ).fetchone()
        if not row:
            raise RuntimeError('persona 行不存在，确认 DDL 和 SEED 已执行')
        return PersonaState(intimacy=row[0], tsundere=row[1], reliance=row[2],
                            energy=row[3], updated_at=row[4])

    def _write(self, s: PersonaState) -> None:
        self._db.execute(
            'UPDATE persona SET intimacy=?, tsundere=?, reliance=?, energy=?, updated_at=? WHERE id=1',
            (s.intimacy, s.tsundere, s.reliance, s.energy, s.updated_at)
        )
        self._db.commit()

    def snapshot_daily(self, now: int | None = None) -> None:
        now = now if now is not None else current_time()
        s = self.get()
        date = snapshot_date(now)
        self._db.execute(
            '''INSERT OR IGNORE INTO persona_snapshots (date, intimacy, tsundere, reliance, energy, captured_at)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (date, s.intimacy, s.tsundere, s.reliance, s.energy, now)
        )
        self._db.commit()

    def latest_snapshot_before(self, now: int | None = None) -> PersonaSnapshot | None:
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

    def snapshots(self, limit: int = 90) -> list[PersonaSnapshot]:
        rows = self._db.execute(
            '''SELECT date, intimacy, tsundere, reliance, energy, captured_at FROM persona_snapshots
               ORDER BY date DESC LIMIT ?''', (limit,)
        ).fetchall()
        return [PersonaSnapshot(date=r[0], intimacy=r[1], tsundere=r[2], reliance=r[3],
                                energy=r[4], updated_at=r[5], captured_at=r[5]) for r in rows]

    def apply_mood(self, delta: MoodDelta, now: int | None = None) -> PersonaState:
        now = now if now is not None else current_time()
        s = self.get()
        favor = _clamp_delta(delta.favor)
        energy = _clamp_delta(delta.energy)
        nxt = PersonaState(
            intimacy=_clamp('intimacy', s.intimacy + favor * 1.2),
            tsundere=_clamp('tsundere', s.tsundere - favor * 0.4),
            reliance=_clamp('reliance', s.reliance + favor * 0.5),
            energy=_clamp('energy', s.energy + energy * 3),
            updated_at=now,
        )
        self._write(nxt)
        return nxt

    def apply_turn(self, now: int | None = None) -> PersonaState:
        now = now if now is not None else current_time()
        s = self.get()
        nxt = PersonaState(
            intimacy=_clamp('intimacy', s.intimacy + 0.35),
            tsundere=_clamp('tsundere', s.tsundere - 0.08),
            reliance=_clamp('reliance', s.reliance + 0.15),
            energy=_clamp('energy', s.energy - 0.4),
            updated_at=now,
        )
        self._write(nxt)
        return nxt

    def apply_elapsed(self, now: int | None = None, asleep_hours: float = 0.0) -> PersonaState:
        now = now if now is not None else current_time()
        s = self.get()
        hours = max(0.0, (now - s.updated_at) / 3_600_000)
        if hours < 1:
            return s
        bounded_asleep = min(hours, max(0.0, asleep_hours))
        awake_hours = hours - bounded_asleep
        days = hours / 24
        nxt = PersonaState(
            intimacy=_clamp('intimacy', s.intimacy - days * 0.6),
            tsundere=_clamp('tsundere', s.tsundere + days * 1.5),
            reliance=_clamp('reliance', s.reliance - days * 1.2),
            energy=_clamp('energy', s.energy + bounded_asleep * 4 - awake_hours * 2),
            updated_at=now,
        )
        self._write(nxt)
        return nxt


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
