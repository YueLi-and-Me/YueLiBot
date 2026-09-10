"""活动单段决策时长的上限：清醒与休息按 kind 收紧，睡眠维持原样。

一次决策给出的时长直接决定这一段活动的能量代价。真机上出现过 360 分钟的清醒决策
（按 awake pace=-1 的 -6.0/h 折算，一段扣 36 点精力），把当天精力打到归零；而近 14
天 106 次 awake 决策里，超过 240 分钟的只有 2 段，长段属于尾部离群值而非常态。

本文件锁三件事：

- 写入层按 kind 限幅，越界只告警不回滚，并把模型给出的原始值留在告警里；
- 解析层在同一张上限表上工作，而长缺口补叙那种「只表示相对占比」的 minutes 不受影响；
- 延续决策同样按 kind 限幅，否则清醒段仍能被一次决策延到数小时。

依赖 ``src.core.schedule.timeline`` 与 ``src.core.db.schema``。
"""

from __future__ import annotations

import json
import sqlite3

from structlog.testing import capture_logs

import pytest

from src.core.db import schema as db_schema
from src.core.schedule import timeline as timeline_module
from src.core.schedule.timeline import (
    Activity,
    ActivityDraft,
    ActivityTimeline,
    ActivityTransition,
)

MINUTE_MS = 60_000
STARTED_AT = 1_700_000_000_000
# 限幅告警的事件名。用例断言事件名与字段，而不是中文提示的整句排版。
CLAMP_EVENT = '活动 minutes 越界，已按 kind 限幅'


def _database() -> sqlite3.Connection:
    """建一个只含当前 DDL 的内存库，不依赖迁移后的真实数据。"""

    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript(db_schema.DDL)
    db.executescript(db_schema.SEED)
    db.commit()
    return db


def _draft(kind: str, minutes: int) -> ActivityDraft:
    """构造一份字段齐全、只有 kind 与时长不同的活动草案。"""

    return ActivityDraft(
        kind=kind,
        doing='整理手头的东西',
        mood='专注但仍然会回应',
        energy_pace=0,
        mood_pace=0,
        minutes=minutes,
    )


def _until_of(db: sqlite3.Connection, activity_id: int) -> int:
    """读回一段活动的预期结束时刻。"""

    row = db.execute(
        'SELECT expected_until FROM activities WHERE id = ?',
        (activity_id,),
    ).fetchone()
    assert row is not None, '活动写入后必须能读回'
    return int(row['expected_until'])


def _clamped_entries(logs: list[dict]) -> list[dict]:
    """从捕获到的日志里挑出限幅告警。"""

    return [entry for entry in logs if entry.get('event') == CLAMP_EVENT]


@pytest.mark.parametrize('kind', ['awake', 'rest', 'sleep'])
def test_insert_clamps_decision_minutes_at_kind_limit(kind: str) -> None:
    """每个 kind 的草案写入时都被限幅到自己的上限，并留下原始值告警。"""

    limit = timeline_module._DECISION_MINUTE_LIMITS[kind]
    raw_minutes = limit + 1
    db = _database()
    try:
        with capture_logs() as logs:
            activity_id = ActivityTimeline(db)._insert_draft(
                _draft(kind, raw_minutes),
                started_at=STARTED_AT,
                ended_at=None,
                source='decided',
            )

        assert _until_of(db, activity_id) == STARTED_AT + limit * MINUTE_MS
        clamped = _clamped_entries(logs)
        assert len(clamped) == 1
        assert clamped[0]['kind'] == kind
        assert clamped[0]['raw'] == raw_minutes
        assert clamped[0]['clamped'] == limit
    finally:
        db.close()


def test_six_hour_awake_draft_is_clamped_while_sleep_is_untouched() -> None:
    """同一份 360 分钟的草案：清醒被压到上限，睡眠原样写入。"""

    awake_limit = timeline_module._DECISION_MINUTE_LIMITS['awake']
    sleep_limit = timeline_module._DECISION_MINUTE_LIMITS['sleep']
    raw_minutes = 360
    assert raw_minutes > awake_limit, '这条用例的前提是 360 分钟超出清醒上限'
    assert raw_minutes <= sleep_limit, '睡眠上限必须容得下整夜睡眠'
    db = _database()
    try:
        with capture_logs() as awake_logs:
            awake_id = ActivityTimeline(db)._insert_draft(
                _draft('awake', raw_minutes),
                started_at=STARTED_AT,
                ended_at=None,
                source='decided',
            )
        with capture_logs() as sleep_logs:
            sleep_id = ActivityTimeline(db)._insert_draft(
                _draft('sleep', raw_minutes),
                started_at=STARTED_AT,
                ended_at=None,
                source='decided',
            )

        assert _until_of(db, awake_id) == STARTED_AT + awake_limit * MINUTE_MS
        assert _until_of(db, sleep_id) == STARTED_AT + raw_minutes * MINUTE_MS
        assert len(_clamped_entries(awake_logs)) == 1
        assert _clamped_entries(sleep_logs) == []
    finally:
        db.close()


def test_kind_limits_keep_sleep_looser_than_the_waking_hours() -> None:
    """上限必须保持 awake 最紧、rest 居中、sleep 最宽，否则封顶筛选不出尾部。"""

    limits = timeline_module._DECISION_MINUTE_LIMITS
    assert limits['awake'] < limits['rest'] < limits['sleep']


def test_parsed_awake_decision_is_clamped_before_it_reaches_the_timeline() -> None:
    """解析层同样按 kind 取上限：模型给出的原始值直接进告警，不等写入层。"""

    limit = timeline_module._DECISION_MINUTE_LIMITS['awake']
    raw_minutes = limit + 240
    payload = json.dumps({
        'decision': 'switch',
        'activity': {
            'kind': 'awake',
            'doing': '拿着手机窝在床上刷视频缓一缓',
            'mood': '还有点迷糊，但会回应',
            'energyPace': -1,
            'moodPace': 0,
            'minutes': raw_minutes,
            'advances': None,
        },
    }, ensure_ascii=False)

    with capture_logs() as logs:
        transition = timeline_module.parse_activity_decision(
            payload,
            intention_count=3,
            require_backfill=False,
        )

    assert transition is not None
    assert transition.next_activity is not None
    assert transition.next_activity.minutes == limit
    clamped = _clamped_entries(logs)
    assert len(clamped) == 1
    assert clamped[0]['kind'] == 'awake'
    assert clamped[0]['raw'] == raw_minutes
    assert clamped[0]['clamped'] == limit


def test_backfill_minutes_stay_relative_shares_above_the_kind_limit() -> None:
    """长缺口补叙的 minutes 只表示相对占比，不得按单段上限限幅。"""

    limit = timeline_module._DECISION_MINUTE_LIMITS['awake']
    share = limit + 60
    payload = json.dumps({
        'backfill': [
            {
                'kind': 'awake',
                'doing': '在客厅收拾东西',
                'mood': '慢慢进入状态',
                'energyPace': 0,
                'moodPace': 0,
                'minutes': share,
                'advances': None,
            },
            {
                'kind': 'rest',
                'doing': '靠着沙发闭目养神',
                'mood': '放松下来但仍会回应',
                'energyPace': 1,
                'moodPace': 0,
                'minutes': share,
                'advances': None,
            },
        ],
        'next': {
            'kind': 'awake',
            'doing': '回到书桌前坐下',
            'mood': '平静下来',
            'energyPace': 0,
            'moodPace': 0,
            'minutes': 30,
            'advances': None,
        },
    }, ensure_ascii=False)

    with capture_logs() as logs:
        transition = timeline_module.parse_activity_decision(
            payload,
            intention_count=3,
            require_backfill=True,
        )

    assert transition is not None
    assert [draft.minutes for draft in transition.backfilled] == [share, share]
    assert _clamped_entries(logs) == []


def test_continuation_decision_is_clamped_to_the_kind_limit() -> None:
    """延续同样是一次决策：它能把清醒段延长到的时长受同一张上限表约束。"""

    limit = timeline_module._DECISION_MINUTE_LIMITS['awake']
    raw_minutes = limit + 180
    db = _database()
    try:
        timeline = ActivityTimeline(db)
        activity_id = timeline._insert_draft(
            _draft('awake', 30),
            started_at=STARTED_AT,
            ended_at=None,
            source='decided',
        )
        now = STARTED_AT + 30 * MINUTE_MS
        previous = Activity(
            id=activity_id,
            kind='awake',
            doing='整理手头的东西',
            mood='专注但仍然会回应',
            energy_pace=0,
            mood_pace=0,
            advances=None,
            started_at=STARTED_AT,
            expected_until=now,
            ended_at=None,
            source='decided',
        )

        with capture_logs() as logs:
            timeline._apply_transition(
                previous,
                ActivityTransition(continuation_minutes=raw_minutes),
                now,
                0,
            )

        assert _until_of(db, activity_id) == now + limit * MINUTE_MS
        clamped = _clamped_entries(logs)
        assert len(clamped) == 1
        assert clamped[0]['kind'] == 'awake'
        assert clamped[0]['raw'] == raw_minutes
        assert clamped[0]['clamped'] == limit
        timeline.assert_invariants()
    finally:
        db.close()
