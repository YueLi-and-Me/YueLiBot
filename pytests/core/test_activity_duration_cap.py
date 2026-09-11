"""活动单段决策时长的上限：清醒与休息按 kind 收紧，睡眠维持原样。

一次决策给出的时长直接决定这一段活动的能量代价。真机上出现过 360 分钟的清醒决策
（按 awake pace=-1 的 -6.0/h 折算，一段扣 36 点精力），把当天精力打到归零；而近 14
天 106 次 awake 决策里，超过 240 分钟的只有 2 段，长段属于尾部离群值而非常态。

本文件锁四件事：

- 写入层按 kind 限幅，越界只告警不回滚，并把模型给出的原始值留在告警里；
- 解析层在同一张上限表上工作，而长缺口补叙那种「只表示相对占比」的 minutes 不受影响；
- 延续决策同样按 kind 限幅，否则清醒段仍能被一次决策延到数小时；
- 上限封的是**一段活动的累计时长**，不只是单次决策：累计达上限后提示词不再给出
  继续选项，写入层也拒绝这次 continue，否则一次次各自合规的 continue 能叠出 8 小时段。

依赖 ``src.core.schedule.timeline`` 与 ``src.core.db.schema``。
"""

from __future__ import annotations

import asyncio
import json
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
STARTED_AT = 1_700_000_000_000
# 限幅告警的事件名。用例断言事件名与字段，而不是中文提示的整句排版。
CLAMP_EVENT = '活动 minutes 越界，已按 kind 限幅'
# 续期截断告警的事件名，与单次决策越界分开，便于定位是哪条判据生效。
TRUNCATE_EVENT = '延续时长超过这一段剩余的可用时长，已截断到累计上限'
# 短缺口分支给出的两个选项原文；累计达上限后继续选项必须整条消失。
CONTINUE_OPTION = '{"decision":"continue","minutes":45}'
SWITCH_OPTION = '{"decision":"switch","activity":'


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


def _open_activity(db: sqlite3.Connection) -> Activity:
    """读回唯一进行中的活动，让被拒用例断言的是库里的真实状态。

    ``started_at`` 不会被 continue 改写，因此它就是累计时长的起点。
    """

    row = db.execute(
        'SELECT * FROM activities WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1'
    ).fetchone()
    assert row is not None, '用例前提是存在一条进行中的活动'
    return timeline_module._activity_from_row(row)


def _context() -> ActivityDecisionContext:
    """构造一份字段齐全的决策上下文，让提示词用例只突出被验证的差异。"""

    return ActivityDecisionContext(
        character_name='测试角色',
        character_personality='按自己的节奏安排一天。',
        persona='精力 40，心情 55。',
        sleep_history='昨晚睡了八小时',
        intentions='1. 把第三章大纲收尾',
        intention_count=1,
        rough_rhythm='今天想早点睡',
        recent_activities='吃午饭 → 在书桌前写大纲',
        interaction='还没有互动记录',
    )


def _activity(
    kind: str,
    *,
    started_at: int,
    expected_until: int,
) -> Activity:
    """构造一段进行中的活动，只保留上限判据关心的两个时间点。"""

    return Activity(
        id=1,
        kind=kind,
        doing='在书桌前写大纲',
        mood='写得有点烦，但被问到仍会回应',
        energy_pace=-1,
        mood_pace=0,
        advances=None,
        started_at=started_at,
        expected_until=expected_until,
        ended_at=None,
        source='decided',
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


def _truncated_entries(logs: list[dict]) -> list[dict]:
    """从捕获到的日志里挑出按剩余可用时长截断的告警。"""

    return [entry for entry in logs if entry.get('event') == TRUNCATE_EVENT]


async def _let_background_task_run() -> None:
    """给 ``current()`` 创建的后台决策任务两轮事件循环执行机会。"""

    await asyncio.sleep(0)
    await asyncio.sleep(0)


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
    """延续同样是一次决策：它能把清醒段延长到的时长受同一张上限表约束。

    这一段已经持续 30 分钟，所以除单次决策上限外还要按剩余可用时长截断；两条判据
    落在同一段上，最终写入的 ``expected_until`` 是两者中更紧的那条。
    """

    limit = timeline_module._DECISION_MINUTE_LIMITS['awake']
    raw_minutes = limit + 180
    elapsed_before = 30
    db = _database()
    try:
        timeline = ActivityTimeline(db)
        activity_id = timeline._insert_draft(
            _draft('awake', elapsed_before),
            started_at=STARTED_AT,
            ended_at=None,
            source='decided',
        )
        now = STARTED_AT + elapsed_before * MINUTE_MS
        previous = _open_activity(db)

        with capture_logs() as logs:
            timeline._apply_transition(
                previous,
                ActivityTransition(continuation_minutes=raw_minutes),
                now,
                0,
            )

        remaining = limit - elapsed_before
        assert _until_of(db, activity_id) == now + remaining * MINUTE_MS
        clamped = _clamped_entries(logs)
        assert len(clamped) == 1
        assert clamped[0]['kind'] == 'awake'
        assert clamped[0]['raw'] == raw_minutes
        assert clamped[0]['clamped'] == limit
        truncated = _truncated_entries(logs)
        assert len(truncated) == 1
        assert truncated[0]['elapsed_minutes'] == elapsed_before
        assert truncated[0]['clamped'] == remaining
        timeline.assert_invariants()
    finally:
        db.close()


@pytest.mark.parametrize('kind', ['awake', 'rest', 'sleep'])
def test_prompt_withholds_continuation_once_elapsed_reaches_kind_limit(kind: str) -> None:
    """累计达到该 kind 的单段上限后，提示词只给 switch；未达上限时两个选项都在。"""

    limit = timeline_module._DECISION_MINUTE_LIMITS[kind]
    now = STARTED_AT + 40 * 60 * MINUTE_MS
    capped = _activity(
        kind,
        started_at=now - (limit + 10) * MINUTE_MS,
        expected_until=now,
    )
    ongoing = _activity(
        kind,
        started_at=now - 30 * MINUTE_MS,
        expected_until=now,
    )

    capped_prompt = timeline_module.build_activity_prompt(capped, now, 0, _context())
    ongoing_prompt = timeline_module.build_activity_prompt(ongoing, now, 0, _context())

    # 达上限：继续选项整条消失，规则改成必须切换，并把上限值写进提示词。
    assert CONTINUE_OPTION not in capped_prompt
    assert SWITCH_OPTION in capped_prompt
    assert '必须切换核心对象' in capped_prompt
    assert str(limit) in capped_prompt
    # 未达上限：两个选项都还在，否则正常延续会被误伤。
    assert CONTINUE_OPTION in ongoing_prompt
    assert SWITCH_OPTION in ongoing_prompt
    assert '必须切换核心对象' not in ongoing_prompt


def test_continuation_beyond_cumulative_limit_is_rejected() -> None:
    """累计已达上限的 continue 被拒绝，异常信息带上 kind、已持续分钟数与上限。"""

    limit = timeline_module._DECISION_MINUTE_LIMITS['awake']
    elapsed = limit + 10
    db = _database()
    try:
        timeline = ActivityTimeline(db)
        initial_minutes = 30
        activity_id = timeline._insert_draft(
            _draft('awake', initial_minutes),
            started_at=STARTED_AT,
            ended_at=None,
            source='decided',
        )
        now = STARTED_AT + elapsed * MINUTE_MS
        # 先提交这段活动：真机上它是在很久以前写入的，被拒的 continue 只应影响本次决策。
        db.commit()
        before = _until_of(db, activity_id)

        with pytest.raises(ValueError) as error:
            timeline._apply_transition(
                _open_activity(db),
                ActivityTransition(continuation_minutes=30),
                now,
                0,
            )

        message = str(error.value)
        assert '累计超限' in message
        assert 'awake' in message
        assert str(elapsed) in message
        assert str(limit) in message
        # 被拒绝时不动 expected_until，续期由 _advance 的失败分支负责。
        assert _until_of(db, activity_id) == before
        assert db.execute('SELECT COUNT(*) FROM activities').fetchone()[0] == 1
        timeline.assert_invariants()
    finally:
        db.close()


def test_continuation_below_cumulative_limit_only_extends_the_same_row() -> None:
    """未达上限的 continue 照常 UPDATE expected_until，不新增时间线段。"""

    limit = timeline_module._DECISION_MINUTE_LIMITS['awake']
    elapsed = 30
    assert elapsed < limit, '这条用例的前提是尚未达到累计上限'
    continue_minutes = 45
    db = _database()
    try:
        timeline = ActivityTimeline(db)
        activity_id = timeline._insert_draft(
            _draft('awake', elapsed),
            started_at=STARTED_AT,
            ended_at=None,
            source='decided',
        )
        now = STARTED_AT + elapsed * MINUTE_MS

        timeline._apply_transition(
            _open_activity(db),
            ActivityTransition(continuation_minutes=continue_minutes),
            now,
            0,
        )

        assert _until_of(db, activity_id) == now + continue_minutes * MINUTE_MS
        row = db.execute(
            'SELECT COUNT(*), MIN(started_at), MAX(ended_at) FROM activities'
        ).fetchone()
        assert row[0] == 1, '延续不得新增时间线段'
        assert row[1] == STARTED_AT, '延续不得改写 started_at，累计时长以它为起点'
        assert row[2] is None
        timeline.assert_invariants()
    finally:
        db.close()


@pytest.mark.parametrize('divisor', [4, 3])
def test_repeated_continuations_are_rejected_once_the_segment_reaches_the_limit(
    divisor: int,
) -> None:
    """真机形态：每一次 continue 单独都合规，叠到累计上限后这一次被拒绝。"""

    limit = timeline_module._DECISION_MINUTE_LIMITS['awake']
    step = limit // divisor
    assert 10 <= step < limit, '每一步都必须是模型能给出的、单独合规的延续时长'
    db = _database()
    try:
        timeline = ActivityTimeline(db)
        activity_id = timeline._insert_draft(
            _draft('awake', step),
            started_at=STARTED_AT,
            ended_at=None,
            source='decided',
        )
        accepted = 0
        now = STARTED_AT
        db.commit()
        # 有界循环而不是 while True：上限一旦失效，这条用例必须判失败而不是空转——
        # 无上界的叠加正是本次要修的那个故障，用它来验它会挂死整个测试进程。
        for _ in range(limit // step + 2):
            now += step * MINUTE_MS
            try:
                timeline._apply_transition(
                    _open_activity(db),
                    ActivityTransition(continuation_minutes=step),
                    now,
                    0,
                )
            except ValueError as exc:
                assert '累计超限' in str(exc)
                break
            accepted += 1
        else:
            pytest.fail('累计达上限后仍一直允许延续，实际段长就没有上界')

        assert accepted >= 2, '这条用例要的是「多次叠加」，不是一次就被拦下'
        # 拒绝正好落在累计上限之后的第一次边界上，且这次失败没有改写 expected_until。
        assert now - STARTED_AT >= limit * MINUTE_MS
        assert now - STARTED_AT - step * MINUTE_MS < limit * MINUTE_MS
        assert _until_of(db, activity_id) == now
        assert db.execute('SELECT COUNT(*) FROM activities').fetchone()[0] == 1
        # 实际段长：从 started_at 到最后一次成功延续定下的终点，不超过累计上限。
        assert _until_of(db, activity_id) - STARTED_AT <= limit * MINUTE_MS
        timeline.assert_invariants()
    finally:
        db.close()


def test_continuation_is_truncated_to_the_remaining_budget_of_the_segment() -> None:
    """未达上限但延续时长会顶穿上限时，按剩余可用时长截断，实际段长不越界。"""

    limit = timeline_module._DECISION_MINUTE_LIMITS['awake']
    elapsed = limit - 10
    db = _database()
    try:
        timeline = ActivityTimeline(db)
        activity_id = timeline._insert_draft(
            _draft('awake', elapsed),
            started_at=STARTED_AT,
            ended_at=None,
            source='decided',
        )
        now = STARTED_AT + elapsed * MINUTE_MS

        with capture_logs() as logs:
            timeline._apply_transition(
                _open_activity(db),
                ActivityTransition(continuation_minutes=limit),
                now,
                0,
            )

        assert _until_of(db, activity_id) == STARTED_AT + limit * MINUTE_MS
        truncated = _truncated_entries(logs)
        assert len(truncated) == 1
        assert truncated[0]['elapsed_minutes'] == elapsed
        assert truncated[0]['clamped'] == 10
        timeline.assert_invariants()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_rejected_continuation_retries_on_the_retry_window_not_every_poll() -> None:
    """累计超限被拒后走失败分支续期：重试按 DECISION_RETRY_MS，不是每分钟一次。

    周期推进每 60 秒调用一次 ``current()``。若失败续期挡不住它，重试会退化成每分钟
    一次，模型每轮白跑一次调用，活动在此期间继续按原 pace 扣精力。这里锁住续期窗口
    内的静默，以及窗口结束后重试确实重新发起。
    """

    limit = timeline_module._DECISION_MINUTE_LIMITS['awake']
    elapsed = limit + 10
    decided: list[int] = []
    db = _database()
    try:
        timeline = ActivityTimeline(db)
        # 这一段一开始就写满上限，因此 now 只是越过边界 10 分钟：缺口仍是短缺口，
        # 拒绝的理由只可能是累计超限，不会混进「长缺口不能延续」。
        activity_id = timeline._insert_draft(
            _draft('awake', limit),
            started_at=STARTED_AT,
            ended_at=None,
            source='decided',
        )
        db.commit()
        now = STARTED_AT + elapsed * MINUTE_MS

        async def stubborn(_activity: Activity, at: int, _gap_ms: int) -> ActivityTransition:
            decided.append(at)
            return ActivityTransition(continuation_minutes=30)

        timeline.set_decider(stubborn)
        with capture_logs() as logs:
            timeline.current(now)
            await _let_background_task_run()
            assert decided == [now]
            # 续期窗口内重复轮询：每一次都不该再创建后台决策任务。
            for step in range(1, timeline_module.DECISION_RETRY_MS // MINUTE_MS):
                timeline.current(now + step * MINUTE_MS)
                await _let_background_task_run()
            assert decided == [now]
            # 窗口一到，周期推进重新触发一次决策。
            timeline.current(now + timeline_module.DECISION_RETRY_MS)
            await _let_background_task_run()
            assert decided == [now, now + timeline_module.DECISION_RETRY_MS]

            failures = [
                entry for entry in logs
                if entry.get('event') == '活动决策失败，沿用当前活动并延后重试'
            ]
            # 一共轮询 11 次，只发起两次决策：每个续期窗口各一次。
            assert len(failures) == 2
            assert all('累计超限' in str(entry['error']) for entry in failures)
            assert str(elapsed) in str(failures[0]['error'])
            assert str(elapsed + timeline_module.DECISION_RETRY_MS // MINUTE_MS) in str(
                failures[1]['error']
            )

        row = db.execute(
            'SELECT expected_until, ended_at FROM activities WHERE id = ?',
            (activity_id,),
        ).fetchone()
        assert row is not None
        assert row['expected_until'] == now + 2 * timeline_module.DECISION_RETRY_MS
        assert row['ended_at'] is None
        assert db.execute('SELECT COUNT(*) FROM activities').fetchone()[0] == 1
        timeline.assert_invariants()
    finally:
        db.close()
