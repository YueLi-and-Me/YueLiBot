"""
维护人物关系、主体精力与心情，并将连续状态转换为模型可执行的行为约束。

本模块使用 ``persona_bond`` 保存按人物隔离的亲密度，使用
``persona_self`` 保存主体共享的精力和心情，并通过 ``persona_snapshots`` 记录
owner 的每日状态。``StreamRegistry`` 负责校验人物归属；关系等级由
``src.core.agent.relationship`` 计算，数据库连接由调用方创建并注入。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import exp

import sqlite3

from src.core.agent.relationship import relationship_tier
from src.core.common.clock import now as current_time, snapshot_date
from src.core.platform_io.registry import StreamRegistry
from src.core.platform_io.types import PersonRef


@dataclass
class PersonaState:
    """某个人物当前的关系状态与主体精力、心情。

    ``intimacy``、``energy`` 和 ``mood`` 的有效范围均为 0 至 100；
    ``updated_at`` 使用项目统一的毫秒时间戳，表示本次状态写入时间。
    """

    intimacy: float    # 好感度 0~100
    energy: float      # 精力   0~100
    mood: float        # 心情   0~100
    updated_at: int


@dataclass
class ElapsedEffect:
    """日程服务预先积分得到的精力与心情变化。

    两个增量均为目标时间区间内的事件合计值；人格服务只负责应用增量并执行心情
    回归，不反向读取日程或睡眠配置。
    """

    energy_delta: float
    mood_delta: float


@dataclass
class PersonaSnapshot(PersonaState):
    """owner 在某个自然日保存的关系状态快照。"""

    date: str = ''
    captured_at: int = 0


@dataclass
class EventDelta:
    """一次事件对亲密度和精力的增量。

    ``None`` 表示该维度不调整；实际增量会在应用前限制到 [-3, 3]，
    以防止单个事件覆盖长期累积状态。
    """

    favor: float | None = None
    energy: float | None = None


class EnergyTier(Enum):
    """由主体精力派生的对外状态档位。"""

    HIGH = '精神很好'
    NORMAL = '清醒'
    TIRED = '疲惫'
    SPENT = '精疲力尽'


class MoodTier(Enum):
    """由主体心情派生的对外状态档位。"""

    GOOD = '心情不错'
    FLAT = '心情平稳'
    LOW = '心情低落'


_RANGE: dict[str, tuple[float, float]] = {
    'intimacy': (0.0, 100.0),
    'energy': (0.0, 100.0),
    'mood': (0.0, 100.0),
}

MOOD_RATE = 2.0
MOOD_TAU = 6.0


def _clamp(key: str, v: float) -> float:
    """将状态值限制在指定维度的 [0, 100] 范围内。

    :param key: ``_RANGE`` 中的状态字段名，只接受 ``intimacy``、``energy`` 或 ``mood``。
    :param v: 待限制的数值。

    :return: 限制后的浮点数。

    :raises KeyError: ``key`` 不属于已知状态字段时抛出。
    """

    lo, hi = _RANGE[key]
    return max(lo, min(hi, v))


def _clamp_delta(v: float | None) -> float:
    """限制一次事件的增量并将无效输入归零。

    :param v: 原始增量；``None``、非有限值或绝对值不小于 1e9 的值视为无效。

    :return: 位于 [-3, 3] 的增量；无效输入返回 0.0。
    """

    if v is None or not (-1e9 < v < 1e9):
        return 0.0
    return max(-3.0, min(3.0, v))


class Persona:
    """通过统一数据库连接读写关系、精力、心情和 owner 快照。"""

    def __init__(self, db: sqlite3.Connection) -> None:
        """初始化状态服务。

        :param db: 已完成迁移的 SQLite 连接。调用方负责连接生命周期和事务边界。
        """

        self._db = db
        self._registry = StreamRegistry(db)

    def get(self, person_id: int) -> PersonaState:
        """读取人物亲密度并合并主体当前精力。

        :param person_id: ``persons.id`` 稳定主键。

        :return: 指定人物的 ``PersonaState``。首次读取 contact 时会创建默认亲密度
            记录；owner 缺少关系记录则视为迁移损坏。

        :raises ValueError: 人物不存在时由注册表抛出。
        :raises RuntimeError: owner 关系记录或主体精力记录缺失，或默认记录写入失败。

        副作用：
            首次读取 contact 会向 ``persona_bond`` 写入默认值并提交事务。
        """
        # contact 允许惰性创建关系行，owner 缺失则必须暴露迁移损坏而不能补默认值。
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
        # 精力和心情属于主体而非人物，所有关系查询共享同一 persona_self 行。
        self_state = self._db.execute(
            'SELECT energy, mood FROM persona_self WHERE id = 1'
        ).fetchone()
        if self_state is None:
            raise RuntimeError('persona_self 行不存在，确认 v6 迁移已完整执行')
        return PersonaState(
            intimacy=bond[0],
            energy=self_state[0],
            mood=self_state[1],
            updated_at=bond[1],
        )

    def inspect(self, person_id: int) -> PersonaState:
        """只读人物关系与主体状态，不创建缺失的 contact 关系记录。

        :param person_id: ``persons.id`` 稳定主键。

        :return: 指定人物的当前状态；缺少 contact 关系时使用未持久化的默认亲密度 12.0。

        :raises ValueError: 人物不存在时由注册表抛出。
        :raises RuntimeError: owner 关系记录或主体状态记录缺失。
        """
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
            'SELECT energy, mood FROM persona_self WHERE id = 1'
        ).fetchone()
        if self_state is None:
            raise RuntimeError('persona_self 行不存在，确认 v6 迁移已完整执行')
        return PersonaState(
            intimacy=bond[0],
            energy=self_state[0],
            mood=self_state[1],
            updated_at=bond[1],
        )

    def _create_contact_bond(self, person: PersonRef) -> None:
        """为 contact 创建初始亲密度记录。

        :param person: 已由注册表解析的人物引用，``kind`` 必须为 ``contact``。

        :raises RuntimeError: 传入 owner 或其他不支持的人物类型。

        副作用：
            向 ``persona_bond`` 写入亲密度 12.0 并提交事务。
        """

        if person.kind != 'contact':
            raise RuntimeError(f'不能为 kind={person.kind} 的 person 懒创建关系状态')
        self._db.execute(
            '''INSERT INTO persona_bond (person_id, intimacy, updated_at)
               VALUES (?, ?, ?)''',
            (person.id, 12.0, current_time()),
        )
        self._db.commit()

    def _write(self, person_id: int, state: PersonaState) -> None:
        """原子更新人物亲密度和主体精力、心情。

        :param person_id: ``persons.id`` 稳定主键。
        :param state: 已完成范围限制、准备持久化的新状态。

        副作用：
            更新 ``persona_bond`` 与 ``persona_self``，随后提交 SQLite 事务。
            数据库约束或连接错误会直接向调用方传播。
        """

        self._db.execute(
            '''UPDATE persona_bond SET intimacy = ?, updated_at = ?
               WHERE person_id = ?''',
            (state.intimacy, state.updated_at, person_id),
        )
        self._db.execute(
            '''UPDATE persona_self SET energy = ?, mood = ?, updated_at = ?
               WHERE id = 1''',
            (state.energy, state.mood, state.updated_at),
        )
        self._db.commit()

    def snapshot_daily(self, person_id: int, now: int | None = None) -> None:
        """保存 owner 当日首次状态快照。

        :param person_id: 必须为 owner 的 ``persons.id``。
        :param now: 可选的当前毫秒时间戳；省略时读取统一时钟。

        :raises ValueError: ``person_id`` 不是 owner 时抛出。
        :raises RuntimeError: owner 状态缺失时抛出。

        副作用：
            通过 ``INSERT OR IGNORE`` 写入当天快照并提交事务；重复调用不会覆盖
            当天已经捕获的值。
        """

        self._require_owner(person_id)
        now = now if now is not None else current_time()
        state = self.get(person_id)
        date = snapshot_date(now)
        self._db.execute(
            '''INSERT OR IGNORE INTO persona_snapshots
               (date, intimacy, energy, mood, captured_at)
               VALUES (?, ?, ?, ?, ?)''',
            (date, state.intimacy, state.energy, state.mood, now),
        )
        self._db.commit()

    def latest_snapshot_before(
        self,
        person_id: int,
        now: int | None = None,
    ) -> PersonaSnapshot | None:
        """读取当前自然日之前最近的一条 owner 快照。

        :param person_id: 必须为 owner 的 ``persons.id``。
        :param now: 可选的当前毫秒时间戳；省略时读取统一时钟。

        :return: 最近的历史快照；没有更早快照时返回 ``None``。

        :raises ValueError: ``person_id`` 不是 owner 时抛出。
        """

        self._require_owner(person_id)
        now = now if now is not None else current_time()
        row = self._db.execute(
            '''SELECT date, intimacy, energy, mood, captured_at FROM persona_snapshots
               WHERE date < ? ORDER BY date DESC LIMIT 1''',
            (snapshot_date(now),),
        ).fetchone()
        if not row:
            return None
        return PersonaSnapshot(
            date=row[0],
            intimacy=row[1],
            energy=row[2],
            mood=row[3],
            updated_at=row[4],
            captured_at=row[4],
        )

    def snapshots(self, person_id: int, limit: int = 90) -> list[PersonaSnapshot]:
        """按时间倒序读取 owner 的历史快照。

        :param person_id: 必须为 owner 的 ``persons.id``。
        :param limit: 最多返回的快照数量，默认 90 条；由 SQLite 执行限制。

        :return: 按日期从新到旧排列的快照列表。

        :raises ValueError: ``person_id`` 不是 owner 时抛出。
        """

        self._require_owner(person_id)
        rows = self._db.execute(
            '''SELECT date, intimacy, energy, mood, captured_at FROM persona_snapshots
               ORDER BY date DESC LIMIT ?''',
            (limit,),
        ).fetchall()
        return [
            PersonaSnapshot(
                date=row[0],
                intimacy=row[1],
                energy=row[2],
                mood=row[3],
                updated_at=row[4],
                captured_at=row[4],
            )
            for row in rows
        ]

    def apply_event(
        self,
        person_id: int,
        delta: EventDelta,
        now: int | None = None,
        *,
        weight: float,
    ) -> PersonaState:
        """将事件转换为亲密度和精力变化并持久化。

        :param person_id: ``persons.id`` 稳定主键。
        :param delta: 事件提供的亲密度和精力原始增量。
        :param now: 可选的本次写入毫秒时间戳；省略时读取统一时钟。
        :param weight: 事件权重，仅限关键字且必填；同时作用于亲密度和精力两个维度。
            不设默认值：漏传会让群聊按全速消耗全局精力且测试无感，故要求调用点显式打折。

        :return: 应用增量并限制到 [0, 100] 后的新状态。

        :raises ValueError: 人物不存在时由注册表抛出。
        :raises RuntimeError: 关系或主体精力记录缺失，或数据库写入失败。

        副作用：
            更新 ``persona_bond`` 和 ``persona_self`` 并提交事务。
        """

        now = now if now is not None else current_time()
        state = self.get(person_id)
        # 单次事件先限制原始增量，再应用权重，防止异常输入跳过状态边界。
        favor = _clamp_delta(delta.favor)
        energy = _clamp_delta(delta.energy)
        next_state = PersonaState(
            intimacy=_clamp('intimacy', state.intimacy + favor * 1.2 * weight),
            energy=_clamp('energy', state.energy + energy * 3 * weight),
            mood=state.mood,
            updated_at=now,
        )
        self._write(person_id, next_state)
        return next_state

    def apply_turn(
        self,
        person_id: int,
        now: int | None = None,
        *,
        weight: float,
    ) -> PersonaState:
        """应用一次对话回合的固定亲密度收益与精力消耗。

        :param person_id: ``persons.id`` 稳定主键。
        :param now: 可选的本次写入毫秒时间戳；省略时读取统一时钟。
        :param weight: 回合权重，仅限关键字且必填；同时缩放亲密度收益和精力消耗。
            不设默认值：漏传会让群聊按全速消耗全局精力且测试无感，故要求调用点显式打折。

        :return: 应用变化并限制到 [0, 100] 后的新状态。

        :raises ValueError: 人物不存在时由注册表抛出。
        :raises RuntimeError: 关系或主体精力记录缺失，或数据库写入失败。

        副作用：
            更新两张状态表并提交事务。
        """

        now = now if now is not None else current_time()
        state = self.get(person_id)
        next_state = PersonaState(
            intimacy=_clamp('intimacy', state.intimacy + 0.35 * weight),
            energy=_clamp('energy', state.energy - 0.4 * weight),
            mood=state.mood,
            updated_at=now,
        )
        self._write(person_id, next_state)
        return next_state

    def apply_elapsed(
        self,
        person_id: int,
        now: int | None = None,
        effect: ElapsedEffect | None = None,
    ) -> PersonaState:
        """按经过的时间衰减关系，并应用 owner 在此期间的精力、心情变化。

        :param person_id: ``persons.id`` 稳定主键。
        :param now: 可选的当前毫秒时间戳；省略时读取统一时钟。
        :param effect: 调用方按日程积分得到的精力与心情事件变化；未提供时将全部
            经过时间按清醒状态每小时消耗两点精力处理，心情只向基线回归。

        :return: 调整后的状态；非 owner 或经过时间不足一小时则返回原状态。

        :raises ValueError: 人物不存在时由注册表抛出。
        :raises RuntimeError: 状态记录缺失或数据库写入失败。

        副作用：
            owner 经过至少一小时后更新两张状态表并提交事务。
        """

        now = now if now is not None else current_time()
        person = self._registry.person(person_id)
        state = self.get(person.id)
        # 只有 owner 的共享精力和心情参与时间结算，contact 的状态只随交互事件变化。
        if person.kind != 'owner':
            return state
        hours = max(0.0, (now - state.updated_at) / 3_600_000)
        if hours < 1:
            return state
        # 日程层决定精力曲线的形状；未装配日程时才退回原有的全清醒线性消耗。
        energy_delta = effect.energy_delta if effect is not None else -hours * 2
        mood_delta = effect.mood_delta if effect is not None else 0.0
        mood = state.mood + mood_delta
        mood += (50.0 - mood) * (1.0 - exp(-hours / MOOD_TAU))
        days = hours / 24
        next_state = PersonaState(
            intimacy=_clamp('intimacy', state.intimacy - days * 0.6),
            energy=_clamp('energy', state.energy + energy_delta),
            mood=_clamp('mood', mood),
            updated_at=now,
        )
        self._write(person.id, next_state)
        return next_state

    def _require_owner(self, person_id: int) -> PersonRef:
        """校验人物必须是 owner。

        :param person_id: ``persons.id`` 稳定主键。

        :return: 已解析的 owner 引用。

        :raises ValueError: 人物不存在或人物类型不是 owner。
        """

        person = self._registry.person(person_id)
        if person.kind != 'owner':
            raise ValueError('persona snapshot 仅允许 owner person 读写')
        return person


def energy_tier(s: PersonaState) -> EnergyTier:
    """按既有阈值把连续精力转换为唯一的派生档位。

    :param s: 待判断的人物状态。
    :return: 精力见底、疲惫、正常或充沛档位。
    """

    if s.energy < 20:
        return EnergyTier.SPENT
    if s.energy < 45:
        return EnergyTier.TIRED
    if s.energy > 85:
        return EnergyTier.HIGH
    return EnergyTier.NORMAL


def mood_tier(s: PersonaState) -> MoodTier:
    """按心情轴阈值把连续值转换为派生档位。

    :param s: 待判断的人物状态。
    :return: 心情不错、平稳或低落档位。
    """

    if s.mood > 65:
        return MoodTier.GOOD
    if s.mood < 35:
        return MoodTier.LOW
    return MoodTier.FLAT


def status_label(
    s: PersonaState,
    *,
    asleep: bool,
    just_woke: bool,
    drowsy: bool,
) -> str:
    """合成睡眠状态与精力档位的唯一对外状态标签。

    :param s: 待描述的人物状态。
    :param asleep: 当前是否已经睡着。
    :param just_woke: 当前是否处于刚醒阶段。
    :param drowsy: 当前是否正在犯困。
    :return: 按睡着、刚醒、犯困、精力档的固定优先级生成的中文标签。
    """

    if asleep:
        return '睡着'
    if just_woke:
        return '刚醒'
    if drowsy:
        return '犯困'
    return energy_tier(s).value


def describe_persona(s: PersonaState) -> str:
    """将连续关系状态转换为不含状态重复信息的关系描述。

    :param s: 待描述的人物状态。

    :return: 只包含关系等级的中文描述；不会暴露原始数值。
    """
    return f'你和对方的关系深度：{relationship_tier(s.intimacy)}。'


def describe_persona_for_planning(s: PersonaState) -> str:
    """把当前精力与心情改写成供日程模型使用的安排口径。

    :param s: 昨日结束时的人物状态。
    :return: 不含原始数值、不会把单一状态铺满全天的中文规划约束。
    """

    tier = energy_tier(s)
    if tier is EnergyTier.SPENT:
        energy_guidance = (
            '昨天结束时精力已经见底。今天的安排要明显轻一些，并且必须至少有两段是明确能回精力的'
            '（吃饭、午睡、洗澡、发呆这类），不要把一整天都写成没劲。'
        )
    elif tier is EnergyTier.TIRED:
        energy_guidance = (
            '昨天结束时精力偏低。今天的安排要轻一些，并且必须至少有两段是明确能回精力的'
            '（吃饭、午睡、洗澡、发呆这类），不要把一整天都写成没劲。'
        )
    elif tier is EnergyTier.HIGH:
        energy_guidance = (
            '昨天结束时精力很好。今天可以安排一些更费精力的事，但要保留消耗和恢复的起伏，'
            '不要把一整天都写成亢奋。'
        )
    else:
        energy_guidance = '昨天结束时精力平稳。今天按她自己的节奏安排，让消耗和恢复自然交替。'

    mood = mood_tier(s)
    if mood is MoodTier.GOOD:
        mood_guidance = '昨天结束时心情不错。今天可以安排一两件她自己期待的小事。'
    elif mood is MoodTier.LOW:
        mood_guidance = '昨天结束时心情偏低。今天至少安排一两件她自己喜欢、能让心情回升的小事。'
    else:
        mood_guidance = ''
    return '\n'.join(part for part in (energy_guidance, mood_guidance) if part)


def describe_acquaintance(first_seen_at: int, now: int | None = None) -> str:
    """根据首次出现时间生成相识时长提示。

    :param first_seen_at: 首次发现人物的毫秒时间戳。
    :param now: 可选的当前毫秒时间戳；省略时读取统一时钟。

    :return: 按天数或近似月份表达的中文相识时长。
    """

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
