"""
维护人物关系与主体精力，并将连续状态转换为模型可执行的行为约束。

本模块使用 ``persona_bond`` 保存按人物隔离的亲密度，使用
``persona_self`` 保存主体共享的精力，并通过 ``persona_snapshots`` 记录
owner 的每日状态。``StreamRegistry`` 负责校验人物归属；关系等级由
``src.core.agent.relationship`` 计算，数据库连接由调用方创建并注入。
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlite3

from src.core.agent.relationship import relationship_tier
from src.core.common.clock import now as current_time, snapshot_date
from src.core.platform_io.registry import StreamRegistry
from src.core.platform_io.types import PersonRef


@dataclass
class PersonaState:
    """某个人物当前的关系状态与主体精力。

    ``intimacy`` 和 ``energy`` 的有效范围均为 0 至 100；``updated_at``
    使用项目统一的毫秒时间戳，表示本次状态写入时间。
    """

    intimacy: float    # 好感度 0~100
    energy: float      # 精力   0~100
    updated_at: int


@dataclass
class PersonaSnapshot(PersonaState):
    """owner 在某个自然日保存的关系状态快照。"""

    date: str = ''
    captured_at: int = 0


@dataclass
class MoodDelta:
    """一次情绪事件对亲密度和精力的增量。

    ``None`` 表示该维度不调整；实际增量会在应用前限制到 [-3, 3]，
    以防止单个事件覆盖长期累积状态。
    """

    favor: float | None = None
    energy: float | None = None


_RANGE: dict[str, tuple[float, float]] = {
    'intimacy': (0.0, 100.0),
    'energy': (0.0, 100.0),
}


def _clamp(key: str, v: float) -> float:
    """将状态值限制在指定维度的 [0, 100] 范围内。

    Args:
        key: ``_RANGE`` 中的状态字段名，只接受 ``intimacy`` 或 ``energy``。
        v: 待限制的数值。

    Returns:
        限制后的浮点数。

    Raises:
        KeyError: ``key`` 不属于已知状态字段时抛出。
    """

    lo, hi = _RANGE[key]
    return max(lo, min(hi, v))


def _clamp_delta(v: float | None) -> float:
    """限制一次情绪事件的增量并将无效输入归零。

    Args:
        v: 原始增量；``None``、非有限值或绝对值不小于 1e9 的值视为无效。

    Returns:
        位于 [-3, 3] 的增量；无效输入返回 0.0。
    """

    if v is None or not (-1e9 < v < 1e9):
        return 0.0
    return max(-3.0, min(3.0, v))


class Persona:
    """通过统一数据库连接读写关系、精力和 owner 快照。"""

    def __init__(self, db: sqlite3.Connection) -> None:
        """初始化状态服务。

        Args:
            db: 已完成迁移的 SQLite 连接。调用方负责连接生命周期和事务边界。
        """

        self._db = db
        self._registry = StreamRegistry(db)

    def get(self, person_id: int) -> PersonaState:
        """读取人物亲密度并合并主体当前精力。

        Args:
            person_id: ``persons.id`` 稳定主键。

        Returns:
            指定人物的 ``PersonaState``。首次读取 contact 时会创建默认亲密度
            记录；owner 缺少关系记录则视为迁移损坏。

        Raises:
            ValueError: 人物不存在时由注册表抛出。
            RuntimeError: owner 关系记录或主体精力记录缺失，或默认记录写入失败。

        Side Effects:
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
        # 精力属于主体而非人物，所有关系查询共享同一 persona_self 行。
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
        """只读人物关系与主体精力，不创建缺失的 contact 关系记录。

        Args:
            person_id: ``persons.id`` 稳定主键。

        Returns:
            指定人物的当前状态；缺少 contact 关系时使用未持久化的默认亲密度 12.0。

        Raises:
            ValueError: 人物不存在时由注册表抛出。
            RuntimeError: owner 关系记录或主体精力记录缺失。
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
        """为 contact 创建初始亲密度记录。

        Args:
            person: 已由注册表解析的人物引用，``kind`` 必须为 ``contact``。

        Raises:
            RuntimeError: 传入 owner 或其他不支持的人物类型。

        Side Effects:
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
        """原子更新人物亲密度和主体精力。

        Args:
            person_id: ``persons.id`` 稳定主键。
            state: 已完成范围限制、准备持久化的新状态。

        Side Effects:
            更新 ``persona_bond`` 与 ``persona_self``，随后提交 SQLite 事务。
            数据库约束或连接错误会直接向调用方传播。
        """

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
        """保存 owner 当日首次状态快照。

        Args:
            person_id: 必须为 owner 的 ``persons.id``。
            now: 可选的当前毫秒时间戳；省略时读取统一时钟。

        Raises:
            ValueError: ``person_id`` 不是 owner 时抛出。
            RuntimeError: owner 状态缺失时抛出。

        Side Effects:
            通过 ``INSERT OR IGNORE`` 写入当天快照并提交事务；重复调用不会覆盖
            当天已经捕获的值。
        """

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
        """读取当前自然日之前最近的一条 owner 快照。

        Args:
            person_id: 必须为 owner 的 ``persons.id``。
            now: 可选的当前毫秒时间戳；省略时读取统一时钟。

        Returns:
            最近的历史快照；没有更早快照时返回 ``None``。

        Raises:
            ValueError: ``person_id`` 不是 owner 时抛出。
        """

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
        """按时间倒序读取 owner 的历史快照。

        Args:
            person_id: 必须为 owner 的 ``persons.id``。
            limit: 最多返回的快照数量，默认 90 条；由 SQLite 执行限制。

        Returns:
            按日期从新到旧排列的快照列表。

        Raises:
            ValueError: ``person_id`` 不是 owner 时抛出。
        """

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
        weight: float = 1.0,
    ) -> PersonaState:
        """将情绪事件转换为亲密度和精力变化并持久化。

        Args:
            person_id: ``persons.id`` 稳定主键。
            delta: 情绪事件提供的亲密度和精力原始增量。
            now: 可选的本次写入毫秒时间戳；省略时读取统一时钟。
            weight: 事件权重，默认 1.0；同时作用于两个维度。

        Returns:
            应用增量并限制到 [0, 100] 后的新状态。

        Raises:
            ValueError: 人物不存在时由注册表抛出。
            RuntimeError: 关系或主体精力记录缺失，或数据库写入失败。

        Side Effects:
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
            updated_at=now,
        )
        self._write(person_id, next_state)
        return next_state

    def apply_turn(
        self,
        person_id: int,
        now: int | None = None,
        weight: float = 1.0,
    ) -> PersonaState:
        """应用一次对话回合的固定亲密度收益与精力消耗。

        Args:
            person_id: ``persons.id`` 稳定主键。
            now: 可选的本次写入毫秒时间戳；省略时读取统一时钟。
            weight: 回合权重，默认 1.0；同时缩放亲密度收益和精力消耗。

        Returns:
            应用变化并限制到 [0, 100] 后的新状态。

        Raises:
            ValueError: 人物不存在时由注册表抛出。
            RuntimeError: 关系或主体精力记录缺失，或数据库写入失败。

        Side Effects:
            更新两张状态表并提交事务。
        """

        now = now if now is not None else current_time()
        state = self.get(person_id)
        next_state = PersonaState(
            intimacy=_clamp('intimacy', state.intimacy + 0.35 * weight),
            energy=_clamp('energy', state.energy - 0.4 * weight),
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
        """按经过的时间衰减关系并恢复或消耗 owner 精力。

        Args:
            person_id: ``persons.id`` 稳定主键。
            now: 可选的当前毫秒时间戳；省略时读取统一时钟。
            asleep_hours: 在经过时间内处于睡眠的小时数，负值按 0 处理，且不会
                超过实际经过小时数。

        Returns:
            调整后的状态；非 owner 或经过时间不足一小时则返回原状态。

        Raises:
            ValueError: 人物不存在时由注册表抛出。
            RuntimeError: 状态记录缺失或数据库写入失败。

        Side Effects:
            owner 经过至少一小时后更新两张状态表并提交事务。
        """

        now = now if now is not None else current_time()
        person = self._registry.person(person_id)
        state = self.get(person.id)
        # 只有 owner 的共享精力参与时间结算，contact 的状态只随交互事件变化。
        if person.kind != 'owner':
            return state
        hours = max(0.0, (now - state.updated_at) / 3_600_000)
        if hours < 1:
            return state
        # 睡眠小时数限制在实际经过时长内，剩余时长按清醒状态计算精力消耗。
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
        """校验人物必须是 owner。

        Args:
            person_id: ``persons.id`` 稳定主键。

        Returns:
            已解析的 owner 引用。

        Raises:
            ValueError: 人物不存在或人物类型不是 owner。
        """

        person = self._registry.person(person_id)
        if person.kind != 'owner':
            raise ValueError('persona snapshot 仅允许 owner person 读写')
        return person


def describe_persona(s: PersonaState) -> str:
    """将连续关系状态转换为有限的自然语言行为约束。

    Args:
        s: 待描述的人物状态。

    Returns:
        包含关系等级和精力区间提示的中文指令文本；不会暴露原始数值。
    """
    lines = [f'你和对方的关系深度：{relationship_tier(s.intimacy)}。']
    if s.energy < 20:
        lines.append('你困得厉害，句子会明显变短，反应也慢一点；除非话题正好相关，不要自动催他睡觉。')
    elif s.energy < 45:
        lines.append('你有点累，懒得把每句话说得很完整，也没力气维持过分热情。')
    elif s.energy > 85:
        lines.append('你现在精神很好，更容易接住玩笑或顺手多讲一个刚想到的细节，但不用因此变得吵闹。')
    return '\n'.join(lines)


def describe_acquaintance(first_seen_at: int, now: int | None = None) -> str:
    """根据首次出现时间生成相识时长提示。

    Args:
        first_seen_at: 首次发现人物的毫秒时间戳。
        now: 可选的当前毫秒时间戳；省略时读取统一时钟。

    Returns:
        按天数或近似月份表达的中文相识时长。
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
