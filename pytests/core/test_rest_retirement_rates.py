"""rest 退出恢复源、清醒速率压缩与结算日志的守护用例。

速率表重定后只有 sleep 回精力；rest 只是比平常清醒掉得慢（同 kind 内档位越大
对精力越好），清醒各档整体压缩。未装配日程服务时的回退消耗引用速率表的清醒
pace=-1 档，不再单独写死。每次结算在状态事务提交之后写一条「精力结算完成」
日志，逐片截断的净截断量一并入账。

本文件锁：

- 速率表逐格数值、「除 sleep 外无正格」与「同 kind 内档位越大速率越大」；
- 经真实 ``ChatService.settle_time`` 入口的 rest／awake 结算，期望按新表速率
  字面量手算（逐片累加不触界，再向基线回归）；
- 未装配日程的回退路径按清醒 pace=-1 档（-3.5/h）消耗；
- 四处提示词文案的双向断言（新原文在、旧原文不在），含休息中的对话行为提示；
- 结算日志的条数与字段口径，以及早退、回退、精力关闭三种边界。

依赖 ``src.core.persona.state``、``src.core.schedule.timeline``、
``src.core.services.chat`` 与 ``src.core.db.schema``。
"""

from __future__ import annotations

import sqlite3

from datetime import datetime
from math import exp
from pathlib import Path

import pytest

from structlog.testing import capture_logs

from src.core.config.schema import Config
from src.core.persona.state import (
    ENERGY_BASELINE,
    ENERGY_FALLBACK_RATE,
    ENERGY_RATES,
    ENERGY_TAU,
    PersonaState,
    describe_persona_for_planning,
)
from src.core.schedule import timeline as timeline_module
from src.core.schedule.plan import DayPlanService, ScheduleSleepState
from src.core.schedule.timeline import (
    Activity,
    ActivityDecisionContext,
    ActivityTimeline,
)
from src.core.services.chat import ChatService

HOUR_MS = 3_600_000
MINUTE_MS = 60_000
T0 = int(datetime(2032, 7, 15, 23).timestamp() * 1000)
SETTLE_EVENT = '精力结算完成'


async def _push(*_args) -> None:
    pass


def _services(db: sqlite3.Connection) -> ChatService:
    """真实人格、日程与活动存储，模型不装配——与既有的心跳结算用例同形。"""

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
    started_at: int,
    hours: float,
) -> None:
    """写一段覆盖 [started_at, started_at+hours] 的已结束活动。"""

    ended_at = started_at + int(hours * HOUR_MS)
    db.execute(
        """INSERT INTO activities
           (kind, doing, mood, energy_pace, mood_pace, advances,
            started_at, expected_until, ended_at, source)
           VALUES (?, '测试活动', '平静', ?, 0, NULL, ?, ?, ?, 'decided')""",
        (kind, energy_pace, started_at, ended_at, ended_at),
    )
    db.commit()


def _regress(energy: float, hours: float) -> float:
    """与结算入口同口径的基线回归：活动积分之后向 ENERGY_BASELINE 收敛。"""

    return energy + (ENERGY_BASELINE - energy) * (1.0 - exp(-hours / ENERGY_TAU))


def _settle_logs(logs: list) -> list:
    return [entry for entry in logs if entry.get('event') == SETTLE_EVENT]


# ---------------------------------------------------------------- 速率表


def test_energy_rate_table_pins_every_cell() -> None:
    """逐格锁定速率表；除 sleep 外任何格都不得为正。"""

    assert ENERGY_RATES == {
        ('sleep', 2): 4.0,
        ('sleep', 3): 6.5,
        ('rest', 1): -1.5,
        ('rest', 2): -0.75,
        ('awake', 1): 0.0,
        ('awake', 0): -1.5,
        ('awake', -1): -3.5,
        ('awake', -2): -5.5,
        ('awake', -3): -7.5,
    }
    assert all(
        rate <= 0 for (kind, _pace), rate in ENERGY_RATES.items() if kind != 'sleep'
    ), '只有 sleep 允许回精力'
    # 回退消耗与速率表的清醒 pace=-1 档同源，速率表再改时两者不分叉。
    assert ENERGY_FALLBACK_RATE == ENERGY_RATES[('awake', -1)] == -3.5


def test_rates_increase_with_pace_within_each_kind() -> None:
    """同一 kind 内档位数字越大速率越大（对精力越好）。"""

    for kind in ('awake', 'rest', 'sleep'):
        ordered = sorted(
            (pace, rate) for (k, pace), rate in ENERGY_RATES.items() if k == kind
        )
        assert len(ordered) > 1, f'{kind} 至少应有两档'
        assert all(
            higher[1] > lower[1] for lower, higher in zip(ordered, ordered[1:])
        ), f'{kind} 的速率必须随档位严格上升：{ordered}'


# ---------------------------------------------------------------- 真实结算入口


@pytest.mark.parametrize(
    'kind,pace,rate,final',
    [
        # 期望按新表字面量手算：60 + rate×2 逐片累加（不触界），再向基线回归 2 小时。
        # rest 两档都从 60 慢慢往下掉，pace2（58.765269）比 pace1（57.326484）掉得少；
        # 清醒 pace=-1 掉得更多（53.489727）。
        ('rest', 2, -0.75, 58.765269),
        ('rest', 1, -1.5, 57.326484),
        ('awake', -1, -3.5, 53.489727),
    ],
)
def test_settle_time_applies_the_compressed_rates(
    db: sqlite3.Connection,
    kind: str,
    pace: int,
    rate: float,
    final: float,
) -> None:
    chat = _services(db)
    _reset_persona(db, 60.0, T0)
    _insert_activity(db, kind=kind, energy_pace=pace, started_at=T0, hours=2)

    chat.settle_time(T0 + 2 * HOUR_MS)

    assert ENERGY_RATES[(kind, pace)] == rate, '手算前提：速率表已是新表'
    assert chat.persona.get(1).energy == pytest.approx(final, abs=1e-4)
    assert chat.persona.settled_at() == T0 + 2 * HOUR_MS


def test_unassembled_schedule_falls_back_to_awake_pace_minus_one(
    db: sqlite3.Connection,
) -> None:
    """未装配日程服务：2 小时按 -3.5/h 消耗，60 → 53.0 再回归得 53.489727。"""

    cfg = Config()
    chat = ChatService(db, None, None, None, _push, cfg=cfg)
    _reset_persona(db, 60.0, T0)

    chat.settle_time(T0 + 2 * HOUR_MS)

    assert chat.persona.get(1).energy == pytest.approx(
        _regress(60.0 - 3.5 * 2, 2.0), abs=1e-4
    )


# ---------------------------------------------------------------- 提示词文案


def test_activity_next_rule1_states_rest_does_not_recover() -> None:
    """决策规则 1：rest 不回精力，只是比平常清醒掉得慢。"""

    prompt_path = (
        Path(timeline_module.__file__).resolve().parents[1]
        / 'prompts'
        / 'activity.next.md'
    )
    content = prompt_path.read_text(encoding='utf-8')
    assert (
        'awake 的 energyPace 只能是 -3 到 1；rest 只能是 1 或 2'
        '（1 是边歇边做点事，2 是真正放松下来）；sleep 只能是 2 或 3。'
    ) in content
    assert (
        'energyPace 越大对精力越好。清醒时最好的状态是不掉精力；'
        'rest 不回精力，只是比平常清醒掉得慢；真正回精力只能靠 sleep。'
    ) in content
    assert '真正回精力只能靠 rest 或 sleep' not in content, '旧文案必须退场'


def _awake_current(now: int) -> Activity:
    """构造一段进行中的 awake 活动。"""

    return Activity(
        id=7,
        kind='awake',
        doing='窝在书桌前刷视频',
        mood='放松，被问到仍会回应',
        energy_pace=0,
        mood_pace=0,
        advances=None,
        started_at=now - 30 * MINUTE_MS,
        expected_until=now + 60 * MINUTE_MS,
        ended_at=None,
        source='decided',
    )


def _decision_context() -> ActivityDecisionContext:
    """构造一份走「允许选择 sleep」分支的决策上下文。"""

    return ActivityDecisionContext(
        character_name='测试角色',
        character_personality='安静',
        persona='此刻精力平稳。',
        sleep_history='没有睡眠记录。',
        intentions='今天没有特别想做的事。',
        intention_count=0,
        rough_rhythm='作息随意。',
        recent_activities='无。',
        interaction='最近偶尔说话。',
    )


def test_sleep_rule_allowed_branch_states_rest_does_not_recover() -> None:
    """sleep_rule 允许分支：rest 仍会回应消息，也不回精力。"""

    now = T0 + 600 * MINUTE_MS
    prompt = timeline_module.build_activity_prompt(
        _awake_current(now), now, 0, _decision_context(),
    )

    assert (
        '允许选择 sleep。按这一段的打算选：打算真的睡着用 sleep，只是闭眼缓一缓用 rest。'
        'rest 期间每条消息仍会把你叫来回应，也不回精力。'
    ) in prompt
    assert '精力恢复也只有 sleep 的一半上下' not in prompt, '旧文案必须退场'


def test_planning_guidance_drops_recovery_wording() -> None:
    """日方向口径：低精力两档不再要求排「明确能回精力」的段落。"""

    spent = describe_persona_for_planning(PersonaState(50.0, 10.0, 50.0, T0))
    assert spent == (
        '昨天结束时精力已经见底。今天的安排要明显轻一些，'
        '多排不费劲的事（吃饭、洗澡、发呆这类），需要的话留出午睡，'
        '不要把一整天都写成没劲。'
    )
    tired = describe_persona_for_planning(PersonaState(50.0, 30.0, 50.0, T0))
    assert tired == (
        '昨天结束时精力偏低。今天的安排要轻一些，'
        '多排不费劲的事（吃饭、洗澡、发呆这类），需要的话留出午睡，'
        '不要把一整天都写成没劲。'
    )
    assert '明确能回精力' not in spent and '明确能回精力' not in tired


def test_resting_behavior_hint_says_energy_drains_slower(db: sqlite3.Connection) -> None:
    """对话行为提示：休息中不再说精力不会下降，改为比平常清醒掉得慢。"""

    chat = _services(db)
    schedule = DayPlanService(
        chat.memory, lambda: chat.persona.get(chat.desktop_context.person.id),
        lambda _now: '', lambda: 0, lambda: None, '测试角色', '安静',
        timeline=ActivityTimeline(db),
    )

    prompt = schedule.describe(T0, ScheduleSleepState(asleep=False, resting=True))

    assert '你正在休息，精力比平常清醒掉得慢，但仍然清醒并会正常回应。' in prompt
    assert '精力不会继续下降' not in prompt, '旧文案必须退场'


# ---------------------------------------------------------------- 结算日志


def test_settlement_writes_one_completion_log_with_full_fields(
    db: sqlite3.Connection,
) -> None:
    """一次真实结算恰好写一条日志，窗口、速率路径与前后状态字段齐全。"""

    chat = _services(db)
    _reset_persona(db, 60.0, T0)
    _insert_activity(db, kind='rest', energy_pace=2, started_at=T0, hours=2)

    with capture_logs() as logs:
        chat.settle_time(T0 + 2 * HOUR_MS)

    entries = _settle_logs(logs)
    assert len(entries) == 1, '一次结算只能写一条'
    entry = entries[0]
    assert entry['log_level'] == 'info'
    assert entry['window_started_at'] == T0
    assert entry['window_ended_at'] == T0 + 2 * HOUR_MS
    assert entry['hours'] == pytest.approx(2.0)
    assert entry['energy_enabled'] is True
    assert entry['fallback'] is False
    assert entry['energy_before'] == pytest.approx(60.0)
    # 60 − 0.75×2 = 58.5，不触界；回归 2 小时后 58.765269。
    assert entry['energy_piecewise'] == pytest.approx(58.5)
    assert entry['energy_after'] == pytest.approx(58.765269, abs=1e-4)
    assert entry['c_clip'] == pytest.approx(0.0)
    assert entry['mood_before'] == pytest.approx(50.0)
    assert entry['mood_after'] == pytest.approx(50.0)


def test_settlement_log_reports_net_clip(db: sqlite3.Connection) -> None:
    """95 + 6.5×8 = 147：上界烧掉 47 记入 c_clip，回归后 94.6269。"""

    chat = _services(db)
    _reset_persona(db, 95.0, T0)
    _insert_activity(db, kind='sleep', energy_pace=3, started_at=T0, hours=8)

    with capture_logs() as logs:
        chat.settle_time(T0 + 8 * HOUR_MS)

    entries = _settle_logs(logs)
    assert len(entries) == 1
    entry = entries[0]
    assert entry['energy_piecewise'] == pytest.approx(100.0)
    assert entry['c_clip'] == pytest.approx(47.0)
    assert entry['energy_after'] == pytest.approx(94.6269, abs=1e-4)


def test_early_return_under_an_hour_writes_no_log(db: sqlite3.Connection) -> None:
    """不足一小时早退：不结算也不写日志。"""

    chat = _services(db)
    _reset_persona(db, 60.0, T0)
    _insert_activity(db, kind='rest', energy_pace=2, started_at=T0, hours=2)

    with capture_logs() as logs:
        chat.settle_time(T0 + 30 * MINUTE_MS)

    assert _settle_logs(logs) == []
    assert chat.persona.settled_at() == T0


def test_fallback_path_logs_empty_piecewise_and_clip(db: sqlite3.Connection) -> None:
    """回退路径没有逐片过程：截断后精力与净截断量记为空。"""

    cfg = Config()
    chat = ChatService(db, None, None, None, _push, cfg=cfg)
    _reset_persona(db, 60.0, T0)

    with capture_logs() as logs:
        chat.settle_time(T0 + 2 * HOUR_MS)

    entries = _settle_logs(logs)
    assert len(entries) == 1
    entry = entries[0]
    assert entry['fallback'] is True
    assert entry['energy_enabled'] is True
    assert entry['energy_piecewise'] is None
    assert entry['c_clip'] is None
    assert entry['energy_after'] == pytest.approx(53.489727, abs=1e-4)


def test_energy_disabled_logs_empty_piecewise_and_clip(db: sqlite3.Connection) -> None:
    """精力关闭时不做活动积分：截断后精力与净截断量记为空。"""

    chat = _services(db)
    _reset_persona(db, 60.0, T0)
    _insert_activity(db, kind='rest', energy_pace=2, started_at=T0, hours=2)
    chat.persona.set_energy_enabled(False, T0)

    with capture_logs() as logs:
        chat.settle_time(T0 + 2 * HOUR_MS)

    entries = _settle_logs(logs)
    assert len(entries) == 1
    entry = entries[0]
    assert entry['energy_enabled'] is False
    assert entry['fallback'] is False
    assert entry['energy_piecewise'] is None
    assert entry['c_clip'] is None
    assert entry['energy_after'] == pytest.approx(60.0)
