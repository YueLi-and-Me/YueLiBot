"""连续同 kind 链长派生与两处时间输入措辞的守护用例。

对应精力系统重设计阶段 1（时间输入诚实化）。此前两处文案都没有用例守护：
``last_sleep_summary`` 的「正在睡」分支与已结束分支文本上不可区分，``刚才`` 行
只给单段时长。真机上两者同时在场时（09-14 05:04 的记录），模型看到的是
「这一觉已经睡了 8 小时」与「刚才：……已经持续 8 小时」并排，读起来像同一件事
说了两遍，其中一句其实还没结束。

本文件锁四件事：

- ``continuous_kind_chain_minutes`` 逐段回溯 activities 全表：单段、多段同 kind、
  异 kind 打断、段间缺口、长链（超过 ``recent_summary`` 条数上限）各自正确；
  回溯到「前段起点不早于后段」时抛 ``RuntimeError`` 暴露时间线损坏。
- 链长右端只用 ``now``：``decided_until`` 裁的是积分右缘，共用同一右缘会把进行
  中那段裁掉，链长在每个边界上都偏短——而边界恰恰是唯一用到它的时刻。
- ``last_sleep_summary`` 的「正在睡」分支带「（还没醒）」，与已结束分支不再可混。
- ``刚才`` 行只在链长严格大于当前段时长时追加链长，单段不追加（追加只是噪声）。

链长在本阶段只是注入决策输入的信息，不是任何门控判据；awake 链长同样只派生、
不封顶。依赖 ``src.core.schedule.timeline`` 与 ``src.core.db.schema``。
"""

from __future__ import annotations

import sqlite3

import pytest

from src.core.db import schema as db_schema
from src.core.schedule import timeline as timeline_module
from src.core.schedule.timeline import (
    Activity,
    ActivityDecisionContext,
    ActivityDraft,
    ActivityTimeline,
)

MINUTE_MS = 60_000
# 用整点毫秒做基准，链长断言全部按分钟精确计算，不经时区换算。
T0 = 1_800_000_000_000
# 渲染用例里「刚才」行追加的链长分句；旧版渲染没有这一分句。
CHAIN_CLAUSE = '算上首尾相接的之前几段'

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
        assert timeline.continuous_kind_chain_minutes(current, now) == 300
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
        assert timeline.continuous_kind_chain_minutes(current, now) == 480
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

        assert timeline.continuous_kind_chain_minutes(_open_activity(db), now) == 260
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

        assert timeline.continuous_kind_chain_minutes(_open_activity(db), now) == 170
    finally:
        db.close()


def test_chain_raises_when_predecessor_does_not_start_earlier() -> None:
    """回溯找到的相邻前段起点不早于后段起点时抛 RuntimeError，不静默跳出。

    合法时间线里每段都是正时长，``前段.started_at < 前段.ended_at == 后段.started_at``
    必然成立；违反它即时间线损坏（例如出现起点不早于终点的段），必须暴露而不是
    让链长停在一个看似合理实则错误的值上。
    """

    db = _database()
    try:
        timeline = ActivityTimeline(db)
        _insert_segment(timeline, 'sleep', T0 + 300 * MINUTE_MS, None, T0 + 600 * MINUTE_MS)
        # 损坏行：起点（T0+320）不早于它自己的终点（T0+300），而终点又恰好接上
        # 进行中那段的起点，回溯一定会踩到它。用裸 SQL 写入，因为这条行本来就不
        # 该能通过正常写入路径产生。
        db.execute(
            """INSERT INTO activities
                 (kind, doing, mood, energy_pace, mood_pace, advances,
                  started_at, expected_until, ended_at, source)
               VALUES ('sleep', '损坏段', '损坏段', 2, 0, NULL, ?, ?, ?, 'decided')""",
            (T0 + 320 * MINUTE_MS, T0 + 300 * MINUTE_MS, T0 + 300 * MINUTE_MS),
        )
        db.commit()

        with pytest.raises(RuntimeError, match='前段起点不早于后段起点'):
            timeline.continuous_kind_chain_minutes(
                _open_activity(db), T0 + 400 * MINUTE_MS,
            )
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
        assert chain == 1076
        assert chain > recent_six, '链长必须超过摘要里最近 6 段的合计，否则等同按摘要截断'
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
        assert timeline.continuous_kind_chain_minutes(_open_activity(db), now) == 180
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
