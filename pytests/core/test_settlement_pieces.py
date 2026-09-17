"""结算片段与逐片截断的守护用例。

人格结算的精力增量原先按「整窗求和 → 加进 E → 整窗回归 → 末尾截断」计算，回归
作用在未截断的值上：一段把整个窗口顶出 [0,100] 的活动，其溢出部分仍参与回归。
改成逐片累加后，越界溢出在片末当场烧掉，回归输入随之不同。本文件锁：

- 三个合同数值例（单片上越界、单片下越界、中途越界后落回）经真实
  ``ChatService.settle_time`` 入口的新旧结果；
- W2/W3 两片顺序例（速率不在速率表内，直接喂合成片段给逐片累加的私有纯函数）；
- 净截断量为 0 时新旧逐位等价（固定种子随机不触界序列 ≥ 200 组 + 触界抵消序列）；
- mood 求和与现行算法逐位相等（``==``，不用 approx）；
- ``iter_pieces`` 的行序、求交、零长度跳过、进行中段右端与字段契约；
- 一次结算只查一次活动窗口；精力关闭、回退与早退、结算右缘的边界不变。

依赖 ``src.core.persona.state``、``src.core.schedule.timeline``、
``src.core.services.chat`` 与 ``src.core.db.schema``。
"""

from __future__ import annotations

import random
import sqlite3

from datetime import datetime
from math import exp
from typing import Any, Sequence, Tuple

import pytest

from src.core.config.schema import Config
from src.core.persona import state as state_module
from src.core.persona.state import (
    ENERGY_BASELINE,
    ENERGY_RATES,
    ENERGY_TAU,
    MOOD_RATE,
    Persona,
)
from src.core.schedule.plan import DayPlanService
from src.core.schedule.timeline import ActivityTimeline
from src.core.services.chat import ChatService

MINUTE_MS = 60_000
HOUR_MS = 3_600_000
T0 = int(datetime(2032, 7, 15, 23).timestamp() * 1000)


async def _push(*_args: Any) -> None:
    pass


def _services(db: sqlite3.Connection) -> ChatService:
    """使用真实人格、日程和活动存储，模型不装配——与既有的心跳结算用例同形。"""

    cfg = Config()
    chat = ChatService(db, None, None, None, _push, cfg=cfg)
    timeline = ActivityTimeline(db)
    schedule = DayPlanService(
        chat.memory, lambda: chat.persona.get(chat.desktop_context.person.id),
        lambda _now: '', lambda: 0, lambda: None, '测试角色', '安静',
        timeline=timeline,
    )
    chat.set_schedule(schedule)
    return chat


def _reset_persona(db: sqlite3.Connection, energy: float, at: int) -> None:
    """把精力与结算游标放到窗口起点，mood 固定 50 不干扰精力断言。"""

    db.execute(
        'UPDATE persona_self SET energy = ?, mood = 50, updated_at = ? WHERE id = 1',
        (energy, at),
    )
    db.execute('UPDATE persona_bond SET updated_at = ? WHERE person_id = 1', (at,))
    db.execute('DELETE FROM persona_snapshots')
    db.commit()


def _insert_activity(
    db: sqlite3.Connection,
    *,
    kind: str,
    energy_pace: int,
    mood_pace: int = 0,
    started_at: int,
    expected_until: int,
    ended_at: int | None,
    source: str = 'decided',
) -> int:
    """直接写一行活动。"""

    cursor = db.execute(
        """INSERT INTO activities
           (kind, doing, mood, energy_pace, mood_pace, advances,
            started_at, expected_until, ended_at, source)
           VALUES (?, '测试活动', '平静', ?, ?, NULL, ?, ?, ?, ?)""",
        (kind, energy_pace, mood_pace, started_at, expected_until, ended_at, source),
    )
    db.commit()
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _piece(
    activity_id: int,
    kind: str,
    energy_rate: float,
    duration_ms: int,
    *,
    mood_rate: float = 0.0,
    started_at: int = 0,
) -> state_module.SettlementPiece:
    """按毫秒时长构造一段合成片段；hours 由起止差派生，与调用方计算逐位一致。"""

    return state_module.SettlementPiece(
        activity_id=activity_id,
        kind=kind,
        source='decided',
        started_at=started_at,
        ended_at=started_at + duration_ms,
        energy_rate=energy_rate,
        mood_rate=mood_rate,
    )


def _piece_hours(duration_ms: int) -> float:
    """与 SettlementPiece.hours 同一除法的时长（小时）。"""

    return duration_ms / 3_600_000


def _old_algorithm(energy: float, deltas: Sequence[float], hours: float) -> float:
    """用例内复刻的现行算法对照：整窗求和 → 加进 E → 整窗回归 → 末尾截断。"""

    value = energy + sum(deltas)
    value = value + (ENERGY_BASELINE - value) * (1.0 - exp(-hours / ENERGY_TAU))
    return min(100.0, max(0.0, value))


def _new_algorithm(energy: float, pieces: Sequence[Any], hours: float) -> float:
    """现行入口内部同序：逐片累加并截断 → 整窗回归 → 末尾截断。"""

    walked = state_module._accumulate_energy_piecewise(energy, pieces)
    walked = walked + (ENERGY_BASELINE - walked) * (1.0 - exp(-hours / ENERGY_TAU))
    return min(100.0, max(0.0, walked))


# ---------------------------------------------------------------- 三个合同数值例（真实结算入口）


def test_settle_burns_upper_overflow_at_piece_end() -> None:
    """单片上越界：E=95 睡 pace=3 共 8 小时，新结果 94.6269，不再是 100。"""

    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    try:
        from src.core.db import schema as db_schema
        db.executescript(db_schema.DDL)
        db.executescript(db_schema.SEED)
        db.commit()
        chat = _services(db)
        _reset_persona(db, 95.0, T0)
        _insert_activity(
            db, kind='sleep', energy_pace=3,
            started_at=T0, expected_until=T0 + 8 * HOUR_MS, ended_at=T0 + 8 * HOUR_MS,
        )

        chat.settle_time(T0 + 8 * HOUR_MS)

        energy = chat.persona.get(1).energy
        assert energy == pytest.approx(94.6269, abs=1e-4)
        assert abs(energy - 100.0) > 0.1, '新旧结果必须可区分：旧算法在本窗口钉 100'
        assert chat.persona.settled_at() == T0 + 8 * HOUR_MS
    finally:
        db.close()


def test_settle_raises_lower_overflow_at_piece_end() -> None:
    """单片下越界：E=5 醒 pace=-3 共 2 小时，新结果 2.6527，不再是 0。"""

    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    try:
        from src.core.db import schema as db_schema
        db.executescript(db_schema.DDL)
        db.executescript(db_schema.SEED)
        db.commit()
        chat = _services(db)
        _reset_persona(db, 5.0, T0)
        _insert_activity(
            db, kind='awake', energy_pace=-3,
            started_at=T0, expected_until=T0 + 2 * HOUR_MS, ended_at=T0 + 2 * HOUR_MS,
        )

        chat.settle_time(T0 + 2 * HOUR_MS)

        energy = chat.persona.get(1).energy
        assert energy == pytest.approx(2.6527, abs=1e-4)
        assert abs(energy - 0.0) > 0.1, '新旧结果必须可区分：旧算法在本窗口钉 0'
    finally:
        db.close()


def test_settle_burns_mid_window_overflow_even_when_net_lands_in_range() -> None:
    """中途越界后落回界内：E=80 先 rest pace=2 共 10 小时、后 awake pace=-1 共 10 小时。"""

    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    try:
        from src.core.db import schema as db_schema
        db.executescript(db_schema.DDL)
        db.executescript(db_schema.SEED)
        db.commit()
        chat = _services(db)
        _reset_persona(db, 80.0, T0)
        _insert_activity(
            db, kind='rest', energy_pace=2,
            started_at=T0, expected_until=T0 + 10 * HOUR_MS, ended_at=T0 + 10 * HOUR_MS,
        )
        _insert_activity(
            db, kind='awake', energy_pace=-1,
            started_at=T0 + 10 * HOUR_MS, expected_until=T0 + 20 * HOUR_MS,
            ended_at=T0 + 20 * HOUR_MS,
        )

        chat.settle_time(T0 + 20 * HOUR_MS)

        energy = chat.persona.get(1).energy
        assert energy == pytest.approx(48.5190, abs=1e-4)
        assert abs(energy - 55.1114) > 0.1, '新旧结果必须可区分：旧算法把越界带进回归'
    finally:
        db.close()


# ---------------------------------------------------------------- W2 / W3（合成片段喂私有纯函数）


def test_w2_w3_piecewise_clamp_examples() -> None:
    """W2：E=95 先 +10/h 后 −10/h 各 1 小时；W3：E=5 先 −10/h 后 +10/h 各 1 小时。

    速率 ±10/h 不在速率表里，经真实入口构造不出来，改为直接喂合成片段；
    回归与末尾截断按同一公式收尾。
    """

    w2_pieces = (
        _piece(1, 'rest', 10.0, HOUR_MS),
        _piece(2, 'awake', -10.0, HOUR_MS, started_at=HOUR_MS),
    )
    w3_pieces = (
        _piece(1, 'awake', -10.0, HOUR_MS),
        _piece(2, 'rest', 10.0, HOUR_MS, started_at=HOUR_MS),
    )

    w2_new = _new_algorithm(95.0, w2_pieces, 2.0)
    w3_new = _new_algorithm(5.0, w3_pieces, 2.0)
    assert w2_new == pytest.approx(88.9797, abs=1e-4)
    assert w3_new == pytest.approx(12.2446, abs=1e-4)
    assert _old_algorithm(95.0, [10.0, -10.0], 2.0) == pytest.approx(93.7757, abs=1e-4)
    assert _old_algorithm(5.0, [-10.0, 10.0], 2.0) == pytest.approx(7.4486, abs=1e-4)
    assert abs(w2_new - 93.7757) > 0.1
    assert abs(w3_new - 7.4486) > 0.1


# ---------------------------------------------------------------- 净截断量为 0 的等价


def _random_untouched_sequences(
    count: int,
    *,
    seed: int,
) -> list[tuple[float, list[tuple[float, int]]]]:
    """固定种子生成不触界的（初值, [(速率, 毫秒时长)…]）序列：步行过程不出 (0,100)。"""

    rng = random.Random(seed)
    rates = sorted(set(ENERGY_RATES.values()))
    sequences: list[tuple[float, list[tuple[float, int]]]] = []
    while len(sequences) < count:
        energy = rng.uniform(5.0, 95.0)
        steps: list[tuple[float, int]] = []
        walk = energy
        ok = True
        for _ in range(rng.randint(1, 6)):
            rate = rng.choice(rates)
            duration_ms = rng.randint(6 * MINUTE_MS, 3 * HOUR_MS)
            walk += rate * _piece_hours(duration_ms)
            if not 0.5 < walk < 99.5:
                ok = False
                break
            steps.append((rate, duration_ms))
        if ok:
            sequences.append((energy, steps))
    return sequences


def test_untouched_windows_match_the_old_algorithm_bit_close() -> None:
    """净截断量为 0：不触界的随机序列新旧两条算法逐位接近（≤ 1e-9）。"""

    sequences = _random_untouched_sequences(200, seed=20260917)
    assert len(sequences) == 200
    for index, (energy, steps) in enumerate(sequences):
        pieces = tuple(
            _piece(index + 1, 'rest', rate, duration_ms)
            for index, (rate, duration_ms) in enumerate(steps)
        )
        total_hours = sum(_piece_hours(duration_ms) for _, duration_ms in steps)
        new_value = _new_algorithm(energy, pieces, total_hours)
        old_value = _old_algorithm(
            energy,
            [rate * _piece_hours(duration_ms) for rate, duration_ms in steps],
            total_hours,
        )
        assert abs(new_value - old_value) <= 1e-9, f'第 {index} 组不等：{steps}'


def test_touching_but_canceling_windows_stay_equivalent() -> None:
    """触界但上下界截断恰好抵消（净截断量 0）的窗口归入等价组。"""

    canceling = (
        _piece(1, 'rest', 60.0, HOUR_MS),     # 50 + 60 = 110 → 片末截到 100（烧掉 10）
        _piece(2, 'awake', -110.0, HOUR_MS, started_at=HOUR_MS),  # 100 − 110 → 截到 0（抬升 10）
    )
    new_value = _new_algorithm(50.0, canceling, 2.0)
    old_value = _old_algorithm(50.0, [60.0, -110.0], 2.0)
    assert abs(new_value - old_value) <= 1e-9

    two_sided = (
        _piece(1, 'rest', 60.0, HOUR_MS),
        _piece(2, 'awake', -70.0, HOUR_MS, started_at=HOUR_MS),
        _piece(3, 'awake', -40.0, HOUR_MS, started_at=2 * HOUR_MS),
        _piece(4, 'rest', 50.0, HOUR_MS, started_at=3 * HOUR_MS),
    )
    new_value = _new_algorithm(50.0, two_sided, 4.0)
    old_value = _old_algorithm(50.0, [60.0, -70.0, -40.0, 50.0], 4.0)
    assert abs(new_value - old_value) <= 1e-9


def test_w1_unchanged_when_nothing_touches() -> None:
    """W1 对照：E=40 睡 pace=3 共 8 小时，未触界，新旧同为 87.8550（经真实入口）。"""

    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    try:
        from src.core.db import schema as db_schema
        db.executescript(db_schema.DDL)
        db.executescript(db_schema.SEED)
        db.commit()
        chat = _services(db)
        _reset_persona(db, 40.0, T0)
        _insert_activity(
            db, kind='sleep', energy_pace=3,
            started_at=T0, expected_until=T0 + 8 * HOUR_MS, ended_at=T0 + 8 * HOUR_MS,
        )

        chat.settle_time(T0 + 8 * HOUR_MS)

        assert chat.persona.get(1).energy == pytest.approx(87.8550, abs=1e-4)
    finally:
        db.close()


# ---------------------------------------------------------------- mood 逐位相等


def test_mood_sum_is_bit_identical_to_the_old_computation(db: sqlite3.Connection) -> None:
    """同一组活动：片段求和与现行算法的 mood_delta 用 == 比较。"""

    paces = (2, -1, 3, 0)
    for index, mood_pace in enumerate(paces):
        start = T0 + index * 90 * MINUTE_MS
        _insert_activity(
            db, kind='rest', energy_pace=1, mood_pace=mood_pace,
            started_at=start, expected_until=start + 90 * MINUTE_MS,
            ended_at=start + 90 * MINUTE_MS,
        )
    timeline = ActivityTimeline(db)

    pieces = timeline.iter_pieces(T0 - 45 * MINUTE_MS, T0 + 6 * HOUR_MS)
    assert pieces, '夹具前提：窗口内有片段'
    total = 0.0
    for piece in pieces:
        total += piece.mood_rate * piece.hours
    # 用例内复刻被取代的整窗求和算法的 mood 部分：同一查询、同一行序、同一乘法。
    reference = 0.0
    rows = db.execute(
        """SELECT mood_pace, started_at, ended_at FROM activities
           WHERE started_at < ? AND COALESCE(ended_at, ?) > ?
           ORDER BY started_at, id""",
        (T0 + 6 * HOUR_MS, T0 + 6 * HOUR_MS, T0 - 45 * MINUTE_MS),
    ).fetchall()
    for row in rows:
        segment_start = max(T0 - 45 * MINUTE_MS, int(row['started_at']))
        segment_end = min(T0 + 6 * HOUR_MS, int(row['ended_at']))
        if segment_end <= segment_start:
            continue
        reference += MOOD_RATE * int(row['mood_pace']) * (
            (segment_end - segment_start) / HOUR_MS
        )
    assert total == reference


# ---------------------------------------------------------------- iter_pieces 契约


def test_iter_pieces_contract(db: sqlite3.Connection) -> None:
    """行序、求交、零长度跳过、进行中段右端与字段逐项核验。"""

    first_id = _insert_activity(
        db, kind='awake', energy_pace=-1, mood_pace=1,
        started_at=T0, expected_until=T0 + 30 * MINUTE_MS, ended_at=T0 + 30 * MINUTE_MS,
    )
    # 零时长行（冷启动形态）：不出片。
    db.execute(
        """INSERT INTO activities
           (kind, doing, mood, energy_pace, mood_pace, advances,
            started_at, expected_until, ended_at, source)
           VALUES ('awake', '零时长段', '平静', 0, 0, NULL, ?, ?, ?, 'decided')""",
        (T0 + 30 * MINUTE_MS, T0 + 30 * MINUTE_MS, T0 + 30 * MINUTE_MS),
    )
    db.commit()
    sleep_id = _insert_activity(
        db, kind='sleep', energy_pace=3, mood_pace=-1,
        started_at=T0 + 30 * MINUTE_MS, expected_until=T0 + 90 * MINUTE_MS,
        ended_at=T0 + 90 * MINUTE_MS,
    )
    open_id = _insert_activity(
        db, kind='rest', energy_pace=2, mood_pace=2,
        started_at=T0 + 90 * MINUTE_MS, expected_until=T0 + 150 * MINUTE_MS,
        ended_at=None,
    )
    timeline = ActivityTimeline(db)

    pieces = timeline.iter_pieces(T0, T0 + 150 * MINUTE_MS)
    assert [piece.activity_id for piece in pieces] == [first_id, sleep_id, open_id], (
        '行序必须是 (started_at, id)，零时长行不出片'
    )
    assert pieces[0].kind == 'awake' and pieces[0].source == 'decided'
    assert pieces[0].energy_rate == ENERGY_RATES[('awake', -1)]
    assert pieces[0].mood_rate == MOOD_RATE * 1
    assert pieces[0].hours == pytest.approx(0.5)
    # 进行中段的右端取窗口右缘 to_ms。
    assert pieces[2].ended_at == T0 + 150 * MINUTE_MS
    assert pieces[2].hours == pytest.approx(1.0)

    # 求交：窗口两端裁切片段。
    clipped = timeline.iter_pieces(T0 + 15 * MINUTE_MS, T0 + 45 * MINUTE_MS)
    assert [piece.activity_id for piece in clipped] == [first_id, sleep_id]
    assert clipped[0].started_at == T0 + 15 * MINUTE_MS
    assert clipped[0].ended_at == T0 + 30 * MINUTE_MS

    # to_ms <= from_ms 返回空元组。
    assert timeline.iter_pieces(T0 + 60 * MINUTE_MS, T0 + 60 * MINUTE_MS) == ()
    assert timeline.iter_pieces(T0 + 90 * MINUTE_MS, T0 + 30 * MINUTE_MS) == ()
    # 空元组 ≠ None：装配了日程但窗口内没有覆盖，与「未装配走回退」是两种语义。
    # 注意进行中段的右端取 to_ms——窗口落在所有活动开始之前才没有片段。
    assert timeline.iter_pieces(T0 - 2 * HOUR_MS, T0 - HOUR_MS) == ()


# ---------------------------------------------------------------- 单一数据源与边界不变


def test_settle_queries_the_activity_window_exactly_once(db: sqlite3.Connection) -> None:
    """一次 settle_time 期间，对活动表的窗口查询只执行一次。"""

    chat = _services(db)
    _reset_persona(db, 60.0, T0)
    _insert_activity(
        db, kind='rest', energy_pace=2,
        started_at=T0, expected_until=T0 + 2 * HOUR_MS, ended_at=T0 + 2 * HOUR_MS,
    )
    schedule = chat._schedule
    assert schedule is not None
    calls = 0
    original = schedule.iter_pieces

    def counting(from_ms: int, to_ms: int, *rest: Any) -> Any:
        nonlocal calls
        calls += 1
        return original(from_ms, to_ms)

    schedule.iter_pieces = counting  # type: ignore[method-assign]
    chat.settle_time(T0 + 2 * HOUR_MS)
    assert calls == 1


def test_unassembled_schedule_uses_the_legacy_fallback_unchanged(db: sqlite3.Connection) -> None:
    """pieces 为 None（未装配日程）走原回退公式：不逐片截断，结果与现行完全一致。"""

    cfg = Config()
    chat = ChatService(db, None, None, None, _push, cfg=cfg)
    _reset_persona(db, 60.0, T0)

    chat.settle_time(T0 + 3 * HOUR_MS)

    expected = 60.0 + 3.0 * -6.0
    expected = expected + (ENERGY_BASELINE - expected) * (1.0 - exp(-3.0 / ENERGY_TAU))
    assert chat.persona.get(1).energy == pytest.approx(min(100.0, max(0.0, expected)))
    assert chat.persona.settled_at() == T0 + 3 * HOUR_MS


def test_assembled_but_empty_window_is_not_the_fallback(db: sqlite3.Connection) -> None:
    """装配日程但窗口内没有活动覆盖：增量为 0，与回退公式的 −6/h 不是一回事。"""

    chat = _services(db)
    _reset_persona(db, 60.0, T0)
    db.execute('DELETE FROM activities')
    db.commit()

    chat.settle_time(T0 + 3 * HOUR_MS)

    expected = 60.0 + (ENERGY_BASELINE - 60.0) * (1.0 - exp(-3.0 / ENERGY_TAU))
    assert chat.persona.get(1).energy == pytest.approx(min(100.0, max(0.0, expected)))


def test_early_return_keeps_cursor_and_energy(db: sqlite3.Connection) -> None:
    """不足一小时早退：不推进游标、不写状态。"""

    chat = _services(db)
    _reset_persona(db, 60.0, T0)
    _insert_activity(
        db, kind='rest', energy_pace=2,
        started_at=T0, expected_until=T0 + 2 * HOUR_MS, ended_at=T0 + 2 * HOUR_MS,
    )

    chat.settle_time(T0 + 30 * MINUTE_MS)

    assert chat.persona.get(1).energy == 60.0
    assert chat.persona.settled_at() == T0


def test_settle_right_edge_stays_clipped_at_decided_until(db: sqlite3.Connection) -> None:
    """结算右缘仍是 min(now, decided_until(now))：进行中段之后的时间不结算。"""

    chat = _services(db)
    _reset_persona(db, 60.0, T0)
    _insert_activity(
        db, kind='awake', energy_pace=0,
        started_at=T0, expected_until=T0 + HOUR_MS, ended_at=None,
    )

    chat.settle_time(T0 + 3 * HOUR_MS)

    assert chat.persona.settled_at() == T0 + HOUR_MS
    expected = 60.0 + ENERGY_RATES[('awake', 0)]
    expected = expected + (ENERGY_BASELINE - expected) * (1.0 - exp(-1.0 / ENERGY_TAU))
    assert chat.persona.get(1).energy == pytest.approx(min(100.0, max(0.0, expected)))


def test_energy_disabled_freezes_energy_but_not_mood(db: sqlite3.Connection) -> None:
    """精力关闭：E 不变、mood 照常结算；与开关语义逐字一致。"""

    chat = _services(db)
    _reset_persona(db, 60.0, T0)
    db.execute('UPDATE persona_self SET mood = 40 WHERE id = 1')
    db.commit()
    _insert_activity(
        db, kind='rest', energy_pace=2, mood_pace=2,
        started_at=T0, expected_until=T0 + 2 * HOUR_MS, ended_at=T0 + 2 * HOUR_MS,
    )
    chat.persona.set_energy_enabled(False, T0)

    chat.settle_time(T0 + 2 * HOUR_MS)

    state = chat.persona.get(1)
    assert state.energy == 60.0
    expected_mood = 40.0 + MOOD_RATE * 2 * 2.0
    expected_mood = expected_mood + (50.0 - expected_mood) * (1.0 - exp(-2.0 / 6.0))
    assert state.mood == pytest.approx(min(100.0, max(0.0, expected_mood)))
    assert chat.persona.settled_at() == T0 + 2 * HOUR_MS
