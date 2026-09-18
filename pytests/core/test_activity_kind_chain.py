"""连续同 kind 链长派生与两处时间输入措辞的守护用例。

此前两处决策输入文案都没有用例守护：``last_sleep_summary`` 的「正在睡」分支与
已结束分支在文本上不可区分，``刚才`` 行只给单段时长。真机上两者同时在场时
（09-14 05:04 的记录），模型看到的是「这一觉已经睡了 8 小时」与
「刚才：……已经持续 8 小时」并排，读起来像同一件事说了两遍，其中一句其实还没结束。

本文件锁五件事：

- ``continuous_kind_chain_minutes`` 在 ``(started_at, id)`` 一次有序查询的有限
  结果集上逐段回溯 activities 全表：单段、多段同 kind、异 kind 打断、段间缺口、
  长链（超过 ``recent_summary`` 条数上限）各自正确。零时长段是合法段：同 kind
  计入（贡献 0 分钟、链继续），异 kind 照常断链。只有真正的损坏才抛
  ``RuntimeError``：负时长、重叠、进行中的段后面还有段。
- 冷启动回归：空库走真实 ``_insert_cold_start`` 与 ``_advance``，连续多次决策
  不得再出现「活动决策失败」，时间线正常前进。冷启动第一段是零时长段——
  ``_insert_cold_start`` 写 ``expected_until=now``，同一毫秒的第一次决策把它
  结束成 [t, t]，时间线必须能带着它继续走。
- 链长右端只用 ``now``：``decided_until`` 裁的是积分右缘，共用同一右缘会把进行
  中那段裁掉，链长在每个边界上都偏短——而边界恰恰是唯一用到它的时刻。
- ``last_sleep_summary`` 的「正在睡」分支带「（还没醒）」，与已结束分支不再可混。
- ``刚才`` 行只在链长严格大于当前段时长时追加链长，单段不追加（追加只是噪声）。

链长只作为决策输入的信息注入，不携带任何门控判据；awake 链长同样只派生、
不封顶。依赖 ``src.core.schedule.timeline`` 与 ``src.core.db.schema``。
"""

from __future__ import annotations

import asyncio
import sqlite3

from structlog.testing import capture_logs

import pytest

from src.core.db import schema as db_schema
from src.core.schedule import timeline as timeline_module
from src.core.schedule.timeline import (
    Activity,
    ActivityDecisionContext,
    ActivityDraft,
    ActivityTimeline,
    ActivityTransition,
)

MINUTE_MS = 60_000
# 用整点毫秒做基准，链长断言全部按分钟精确计算，不经时区换算。
T0 = 1_800_000_000_000
# 渲染用例里「刚才」行追加的链长分句；旧版渲染没有这一分句。
CHAIN_CLAUSE = '算上首尾相接的之前几段'
# 决策失败重试的日志事件名；用例断言事件名与字段，不比对整句排版。
DECISION_FAILURE_EVENT = '活动决策失败，沿用当前活动并延后重试'

_KIND_PACE = {'awake': 0, 'rest': 1, 'sleep': 2}


def _database() -> sqlite3.Connection:
    """建一个只含当前 DDL 的内存库，不依赖迁移后的真实数据。"""

    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript(db_schema.DDL)
    db.executescript(db_schema.SEED)
    db.commit()
    return db


def _insert_segment(
    timeline: ActivityTimeline,
    kind: str,
    started_at: int,
    ended_at: int | None,
    expected_until: int,
) -> int:
    """按精确起止写入一段活动；显式传入 expected_until，minutes 不参与段长。"""

    duration_minutes = max(1, ((ended_at or expected_until) - started_at) // MINUTE_MS)
    return timeline._insert_draft(
        ActivityDraft(
            kind=kind,
            doing='闭着眼睛躺着',
            mood='安静，但被问到仍会回应',
            energy_pace=_KIND_PACE[kind],
            mood_pace=0,
            minutes=int(duration_minutes),
        ),
        started_at=started_at,
        ended_at=ended_at,
        source='decided',
        expected_until=expected_until,
    )


def _open_activity(db: sqlite3.Connection) -> Activity:
    """读回唯一进行中的活动，让链长用例从库里的真实状态起算。"""

    row = db.execute(
        'SELECT * FROM activities WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1'
    ).fetchone()
    assert row is not None, '用例前提是存在一条进行中的活动'
    return timeline_module._activity_from_row(row)


def _context(chain_minutes: int | None) -> ActivityDecisionContext:
    """构造一份字段齐全的决策上下文，只让链长字段随用例变化。"""

    return ActivityDecisionContext(
        character_name='测试角色',
        character_personality='按自己的节奏安排一天。',
        persona='精力 40，心情 55。',
        sleep_history='这一觉已经睡了 2 小时（还没醒）',
        intentions='1. 把第三章大纲收尾',
        intention_count=1,
        rough_rhythm='今天想早点睡',
        recent_activities='吃午饭 → 闭着眼睛躺着',
        interaction='还没有互动记录',
        current_kind_chain_minutes=chain_minutes,
    )


def test_single_segment_chain_equals_its_own_elapsed() -> None:
    """单段时链长就是这一段自 started_at 起的时长，两数相等。"""

    db = _database()
    try:
        timeline = ActivityTimeline(db)
        _insert_segment(timeline, 'sleep', T0, None, T0 + 480 * MINUTE_MS)
        now = T0 + 300 * MINUTE_MS

        current = _open_activity(db)
        assert timeline.continuous_kind_chain_minutes(current, now).minutes == 300
        assert timeline_module._elapsed_minutes(current, now) == 300
        timeline.assert_invariants()
    finally:
        db.close()


@pytest.mark.parametrize('kind', ['awake', 'rest', 'sleep'])
def test_chain_accumulates_contiguous_same_kind_segments(kind: str) -> None:
    """对全部 kind 都派生：首尾相接的同 kind 段累计成一条链，awake 也不例外。"""

    db = _database()
    try:
        timeline = ActivityTimeline(db)
        _insert_segment(timeline, kind, T0, T0 + 180 * MINUTE_MS, T0 + 180 * MINUTE_MS)
        _insert_segment(
            timeline, kind,
            T0 + 180 * MINUTE_MS, T0 + 360 * MINUTE_MS, T0 + 360 * MINUTE_MS,
        )
        _insert_segment(timeline, kind, T0 + 360 * MINUTE_MS, None, T0 + 660 * MINUTE_MS)
        now = T0 + 480 * MINUTE_MS

        current = _open_activity(db)
        assert timeline_module._elapsed_minutes(current, now) == 120
        assert timeline.continuous_kind_chain_minutes(current, now).minutes == 480
        timeline.assert_invariants()
    finally:
        db.close()


def test_chain_stops_at_different_kind() -> None:
    """链被异 kind 段打断：rest 不计入 sleep 链，反之亦然。"""

    db = _database()
    try:
        timeline = ActivityTimeline(db)
        _insert_segment(timeline, 'rest', T0, T0 + 200 * MINUTE_MS, T0 + 200 * MINUTE_MS)
        _insert_segment(
            timeline, 'sleep',
            T0 + 200 * MINUTE_MS, T0 + 400 * MINUTE_MS, T0 + 400 * MINUTE_MS,
        )
        _insert_segment(timeline, 'sleep', T0 + 400 * MINUTE_MS, None, T0 + 700 * MINUTE_MS)
        now = T0 + 460 * MINUTE_MS

        assert timeline.continuous_kind_chain_minutes(_open_activity(db), now).minutes == 260
        timeline.assert_invariants()
    finally:
        db.close()


def test_chain_stops_at_gap() -> None:
    """段间有真实缺口时链在缺口处终止，缺口之前的同 kind 段不计入。"""

    db = _database()
    try:
        timeline = ActivityTimeline(db)
        _insert_segment(timeline, 'sleep', T0, T0 + 100 * MINUTE_MS, T0 + 100 * MINUTE_MS)
        # T0+100 到 T0+160 之间是缺口：前一段 ended_at 不等于后一段 started_at。
        _insert_segment(
            timeline, 'sleep',
            T0 + 160 * MINUTE_MS, T0 + 300 * MINUTE_MS, T0 + 300 * MINUTE_MS,
        )
        _insert_segment(timeline, 'sleep', T0 + 300 * MINUTE_MS, None, T0 + 600 * MINUTE_MS)
        now = T0 + 330 * MINUTE_MS

        assert timeline.continuous_kind_chain_minutes(_open_activity(db), now).minutes == 170
    finally:
        db.close()


def test_chain_raises_on_negative_duration_segment() -> None:
    """负时长段（``ended_at < started_at``）是时间线损坏，抛 RuntimeError 暴露。

    不留洞与不重叠都拦不住它：起点不早于终点的段仍可能与前后邻居首尾相接。
    损坏行与当前链之间隔着缺口、回溯根本踩不到它，校验也必须把它认出来——
    链长是在一条被断言完好的时间线上计算的，而不是「能用就行」。
    """

    db = _database()
    try:
        timeline = ActivityTimeline(db)
        _insert_segment(timeline, 'sleep', T0, T0 + 100 * MINUTE_MS, T0 + 100 * MINUTE_MS)
        # 损坏行：起点 T0+200 晚于自己的终点 T0+100，前后都留缺口，唯一的异常
        # 就是负时长本身。用裸 SQL 写入，因为正常写入路径不产生这种行。
        db.execute(
            """INSERT INTO activities
                 (kind, doing, mood, energy_pace, mood_pace, advances,
                  started_at, expected_until, ended_at, source)
               VALUES ('sleep', '损坏段', '损坏段', 2, 0, NULL, ?, ?, ?, 'decided')""",
            (T0 + 200 * MINUTE_MS, T0 + 100 * MINUTE_MS, T0 + 100 * MINUTE_MS),
        )
        _insert_segment(timeline, 'sleep', T0 + 300 * MINUTE_MS, None, T0 + 600 * MINUTE_MS)
        db.commit()

        with pytest.raises(RuntimeError, match='负时长'):
            timeline.continuous_kind_chain_minutes(
                _open_activity(db), T0 + 400 * MINUTE_MS,
            )
    finally:
        db.close()


def test_chain_raises_on_overlapping_segments() -> None:
    """前段终点晚于后段起点（重叠）是时间线损坏，抛 RuntimeError 暴露。"""

    db = _database()
    try:
        timeline = ActivityTimeline(db)
        _insert_segment(timeline, 'sleep', T0, T0 + 200 * MINUTE_MS, T0 + 200 * MINUTE_MS)
        # 进行中这段的起点 T0+100 落在上一段内部：两段重叠 100 分钟。
        _insert_segment(timeline, 'sleep', T0 + 100 * MINUTE_MS, None, T0 + 400 * MINUTE_MS)
        db.commit()

        with pytest.raises(RuntimeError, match='重叠'):
            timeline.continuous_kind_chain_minutes(
                _open_activity(db), T0 + 300 * MINUTE_MS,
            )
    finally:
        db.close()


def test_chain_raises_when_open_segment_is_not_last() -> None:
    """进行中的段后面还有段是时间线损坏，抛 RuntimeError 暴露。"""

    db = _database()
    try:
        timeline = ActivityTimeline(db)
        # 先写进行中段，再补一条起点更晚的已结束段：开放行不再是最后一行。
        _insert_segment(timeline, 'sleep', T0, None, T0 + 60 * MINUTE_MS)
        _insert_segment(
            timeline, 'sleep',
            T0 + 100 * MINUTE_MS, T0 + 200 * MINUTE_MS, T0 + 200 * MINUTE_MS,
        )
        db.commit()

        with pytest.raises(RuntimeError, match='后面仍有活动'):
            timeline.continuous_kind_chain_minutes(
                _open_activity(db), T0 + 300 * MINUTE_MS,
            )
    finally:
        db.close()


def test_zero_length_same_kind_segment_extends_chain_without_minutes() -> None:
    """零时长段合法：同 kind 时计入——贡献 0 分钟、链继续向前延伸，不抛错。"""

    db = _database()
    try:
        timeline = ActivityTimeline(db)
        _insert_segment(timeline, 'sleep', T0, T0 + 120 * MINUTE_MS, T0 + 120 * MINUTE_MS)
        # 零时长段 [T0+120, T0+120]：首尾与前后两段都相接，自身不占时长。
        _insert_segment(
            timeline, 'sleep',
            T0 + 120 * MINUTE_MS, T0 + 120 * MINUTE_MS, T0 + 120 * MINUTE_MS,
        )
        _insert_segment(timeline, 'sleep', T0 + 120 * MINUTE_MS, None, T0 + 300 * MINUTE_MS)
        now = T0 + 180 * MINUTE_MS

        current = _open_activity(db)
        assert timeline_module._elapsed_minutes(current, now) == 60
        assert timeline.continuous_kind_chain_minutes(current, now).minutes == 180
        timeline.assert_invariants()
    finally:
        db.close()


def test_zero_length_different_kind_segment_breaks_chain() -> None:
    """零时长段合法：异 kind 时照常断链，不抛错。"""

    db = _database()
    try:
        timeline = ActivityTimeline(db)
        _insert_segment(timeline, 'sleep', T0, T0 + 120 * MINUTE_MS, T0 + 120 * MINUTE_MS)
        _insert_segment(
            timeline, 'rest',
            T0 + 120 * MINUTE_MS, T0 + 120 * MINUTE_MS, T0 + 120 * MINUTE_MS,
        )
        _insert_segment(timeline, 'sleep', T0 + 120 * MINUTE_MS, None, T0 + 300 * MINUTE_MS)
        now = T0 + 180 * MINUTE_MS

        assert timeline.continuous_kind_chain_minutes(_open_activity(db), now).minutes == 60
        timeline.assert_invariants()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_cold_start_then_repeated_decisions_keep_moving() -> None:
    """冷启动回归：零时长冷启动段在场时，第二次起的每次决策都正常推进。

    空时间线由 ``_insert_cold_start`` 写入 ``expected_until=now`` 的中性清醒段；
    同一毫秒的第一次决策（switch）把它结束成零时长段 [t, t]——生产库 id=1 即此
    形态。决策器内按 ``plan.py`` 同形重新读当前段并计算链长；此后每次决策都不得
    抛错续期，日志不得出现「活动决策失败」。
    """

    db = _database()
    try:
        timeline = ActivityTimeline(db)
        calls: list[int] = []
        chains: list[int] = []

        async def decider(_activity: Activity, at: int, _gap_ms: int) -> ActivityTransition:
            current = timeline.current(at)
            calls.append(at)
            chains.append(timeline.continuous_kind_chain_minutes(current, at).minutes)
            if len(calls) == 1:
                # 第一次决策：从冷启动的中性段切换成一件真事，把前者结束成零时长段。
                return ActivityTransition(
                    next_activity=ActivityDraft(
                        kind='awake',
                        doing='窝在书桌前刷视频',
                        mood='放松，被问到仍会回应',
                        energy_pace=0,
                        mood_pace=0,
                        minutes=30,
                    ),
                )
            return ActivityTransition(continuation_minutes=30)

        timeline.set_decider(decider)
        with capture_logs() as logs:
            timeline.current(T0)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            timeline.current(T0 + 30 * MINUTE_MS)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            timeline.current(T0 + 60 * MINUTE_MS)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        failures = [
            entry for entry in logs if entry.get('event') == DECISION_FAILURE_EVENT
        ]
        assert failures == [], f'第二次决策起不得再失败续期：{failures}'
        assert len(calls) == 3, '三次边界都应真的发起决策'
        assert chains == [0, 30, 60], '链长把零时长冷启动段计入（0 分钟、链继续）'
        rows = db.execute(
            'SELECT started_at, expected_until, ended_at FROM activities ORDER BY id'
        ).fetchall()
        assert len(rows) == 2, '延续只延长进行中段，不新增时间线段'
        assert rows[0]['started_at'] == rows[0]['ended_at'] == T0, (
            '第一段是被第一次决策结束的零时长冷启动段'
        )
        assert rows[1]['ended_at'] is None
        assert rows[1]['expected_until'] == T0 + 90 * MINUTE_MS, '时间线必须正常前进'
        timeline.assert_invariants()
    finally:
        db.close()


def test_long_chain_covers_more_than_the_recent_summary_limit() -> None:
    """长链用例：9 段相接的 sleep 链，链长是整条链而不是摘要截断后的长度。

    真机出现过连续 9 段 rest+sleep 合计 17h56m 而单段均未越界。这里按同样的段数
    与总时长构造；``recent_summary`` 默认只装 6 段，链长若从摘要派生会少算 3 段。
    """

    segment_minutes = [131, 109, 122, 117, 125, 114, 128, 111, 119]
    assert len(segment_minutes) == 9
    assert sum(segment_minutes) == 1076, '这条用例复刻的是真机那条 17h56m 的链'
    db = _database()
    try:
        timeline = ActivityTimeline(db)
        cursor = T0
        for minutes in segment_minutes[:-1]:
            end = cursor + minutes * MINUTE_MS
            _insert_segment(timeline, 'sleep', cursor, end, end)
            cursor = end
        _insert_segment(
            timeline, 'sleep', cursor, None, cursor + segment_minutes[-1] * MINUTE_MS,
        )
        now = T0 + 1076 * MINUTE_MS

        summary = timeline.recent_summary(now)
        assert len(summary.split(' → ')) == 6, '前提是摘要条数上限仍在 6，截断生效'
        recent_six = sum(segment_minutes[-6:])
        chain = timeline.continuous_kind_chain_minutes(_open_activity(db), now)
        assert chain.minutes == 1076
        assert chain.minutes > recent_six, '链长必须超过摘要里最近 6 段的合计，否则等同按摘要截断'
        timeline.assert_invariants()
    finally:
        db.close()


def test_chain_right_edge_is_now_not_the_settlement_horizon() -> None:
    """窗口裁剪用例：链长不受 decided_until／结算视界裁剪影响。

    边界时刻 ``now`` 越过进行中那段的 ``expected_until`` 时，``decided_until``
    把积分右缘裁到 ``expected_until``；链长若共用同一右缘，进行中那段会被裁短，
    而边界恰恰是唯一用到链长的时刻。这里断言两者确实分叉，且链长量到 ``now``。
    """

    db = _database()
    try:
        timeline = ActivityTimeline(db)
        _insert_segment(timeline, 'sleep', T0, T0 + 120 * MINUTE_MS, T0 + 120 * MINUTE_MS)
        _insert_segment(
            timeline, 'sleep', T0 + 120 * MINUTE_MS, None, T0 + 150 * MINUTE_MS,
        )
        db.commit()
        now = T0 + 180 * MINUTE_MS

        assert timeline.decided_until(now) == T0 + 150 * MINUTE_MS, (
            '前提是结算右缘确实被裁到 expected_until，否则这条用例没有验到分叉'
        )
        assert timeline.continuous_kind_chain_minutes(_open_activity(db), now).minutes == 180
        timeline.assert_invariants()
    finally:
        db.close()


def test_last_sleep_summary_marks_unfinished_sleep() -> None:
    """双向断言：正在睡的摘要带「（还没醒）」，旧的无标记表述不再出现。"""

    db = _database()
    try:
        timeline = ActivityTimeline(db)
        _insert_segment(timeline, 'sleep', T0, None, T0 + 600 * MINUTE_MS)
        db.commit()
        now = T0 + 480 * MINUTE_MS

        summary = timeline.last_sleep_summary(now)
        assert summary == '这一觉已经睡了 8 小时（还没醒）'
        assert '（还没醒）' in summary
        assert summary != '这一觉已经睡了 8 小时', '旧表述必须退场，两分支才不再可混'
    finally:
        db.close()


def test_last_sleep_summary_keeps_finished_sleep_unmarked() -> None:
    """已结束的睡眠摘要维持原样：有结束距离，没有「（还没醒）」。"""

    db = _database()
    try:
        timeline = ActivityTimeline(db)
        _insert_segment(timeline, 'sleep', T0, T0 + 480 * MINUTE_MS, T0 + 480 * MINUTE_MS)
        _insert_segment(
            timeline, 'awake',
            T0 + 480 * MINUTE_MS, None, T0 + 660 * MINUTE_MS + 120 * MINUTE_MS,
        )
        db.commit()
        now = T0 + 660 * MINUTE_MS

        summary = timeline.last_sleep_summary(now)
        assert summary == '3 小时前结束，睡了 8 小时'
        assert '（还没醒）' not in summary
    finally:
        db.close()


def test_current_activity_appends_chain_only_when_longer_than_segment() -> None:
    """双向断言：链长大于当前段时长时「刚才」行追加链长分句，单段时维持旧渲染。"""

    now = T0 + 480 * MINUTE_MS
    current = Activity(
        id=1,
        kind='sleep',
        doing='闭上眼睛慢慢睡着',
        mood='睡得还算安稳',
        energy_pace=2,
        mood_pace=0,
        advances=None,
        started_at=T0 + 360 * MINUTE_MS,
        expected_until=T0 + 660 * MINUTE_MS,
        ended_at=None,
        source='decided',
    )

    chained = timeline_module.build_activity_prompt(current, now, 0, _context(480))
    assert (
        '刚才：闭上眼睛慢慢睡着，已经持续 2 小时；'
        '算上首尾相接的之前几段，这种 sleep 状态已经连续 8 小时；'
        '原本打算持续到'
    ) in chained
    assert '已经持续 2 小时；原本打算持续到' not in chained, '旧表述必须让位给链长分句'

    single = timeline_module.build_activity_prompt(current, now, 0, _context(120))
    assert CHAIN_CLAUSE not in single, '链长等于当前段时长时追加只是噪声'
    assert '刚才：闭上眼睛慢慢睡着，已经持续 2 小时；原本打算持续到' in single

    absent = timeline_module.build_activity_prompt(current, now, 0, _context(None))
    assert CHAIN_CLAUSE not in absent, '调用方没有提供链长时不得凭空追加'


def test_open_segment_future_expected_until_does_not_extend_chain() -> None:
    """进行中段只计到 now：expected_until 的未来部分不进链长。"""

    db = _database()
    try:
        timeline = ActivityTimeline(db)
        _insert_segment(timeline, 'sleep', T0, T0 + 120 * MINUTE_MS, T0 + 120 * MINUTE_MS)
        # 进行中这段的预期结束在两小时之后：链长只能量到 now，不能量到预期终点。
        _insert_segment(
            timeline, 'sleep', T0 + 120 * MINUTE_MS, None, T0 + 300 * MINUTE_MS,
        )
        db.commit()
        now = T0 + 180 * MINUTE_MS

        current = _open_activity(db)
        assert timeline_module._elapsed_minutes(current, now) == 60
        assert timeline.continuous_kind_chain_minutes(current, now).minutes == 180
        timeline.assert_invariants()
    finally:
        db.close()


def test_chain_reports_whether_it_reached_the_first_row() -> None:
    """链起点是表中第一行时标记 reached_record_start；被异 kind 挡住时不标。"""

    db = _database()
    try:
        timeline = ActivityTimeline(db)
        # 表中第一行就是 sleep 链起点：回溯抵达记录起点，更早的历史不可知。
        _insert_segment(timeline, 'sleep', T0, T0 + 120 * MINUTE_MS, T0 + 120 * MINUTE_MS)
        _insert_segment(timeline, 'sleep', T0 + 120 * MINUTE_MS, None, T0 + 300 * MINUTE_MS)
        now = T0 + 180 * MINUTE_MS

        chain = timeline.continuous_kind_chain_minutes(_open_activity(db), now)
        assert chain.minutes == 180
        assert chain.start_ms == T0
        assert chain.reached_record_start is True

        # 链前再补一段异 kind：第一行不再是链的一部分，回溯被它挡住。
        db.execute(
            """INSERT INTO activities
                 (kind, doing, mood, energy_pace, mood_pace, advances,
                  started_at, expected_until, ended_at, source)
               VALUES ('awake', '更早的清醒段', '安静', 0, 0, NULL, ?, ?, ?, 'decided')""",
            (T0 - 60 * MINUTE_MS, T0, T0),
        )
        db.commit()
        chain = timeline.continuous_kind_chain_minutes(_open_activity(db), now)
        assert chain.minutes == 180
        assert chain.reached_record_start is False
        timeline.assert_invariants()
    finally:
        db.close()


def test_chain_raises_when_current_is_not_in_table() -> None:
    """链尾不在活动时间线中也是损坏：抛 RuntimeError，不算出任何链长。"""

    db = _database()
    try:
        timeline = ActivityTimeline(db)
        _insert_segment(timeline, 'awake', T0, None, T0 + 60 * MINUTE_MS)
        db.commit()
        ghost = Activity(
            id=0,
            kind='awake',
            doing='刚停下来，还没决定接下来做什么',
            mood='状态平稳，仍会正常回应',
            energy_pace=0,
            mood_pace=0,
            advances=None,
            started_at=T0,
            expected_until=T0 + 10 * MINUTE_MS,
            ended_at=None,
            source='decided',
        )

        with pytest.raises(RuntimeError, match='不在活动时间线中'):
            timeline.continuous_kind_chain_minutes(ghost, T0 + 30 * MINUTE_MS)
    finally:
        db.close()
