"""状态板从持久化阶段事件恢复。"""

from __future__ import annotations

import time

import pytest

from src.core.observe import store
from src.core.observe.events import enter_stage
from src.core.observe.stages import GATED, GENERATING, RECEIVED
from src.core.observe.store import current_stages


def test_board_reports_stage_id_and_label() -> None:
    enter_stage(RECEIVED, 3, 'QQ 群聊 123', '今天天气不错')
    enter_stage(GENERATING, 1, '桌面', turn_id=7)

    rows = {row['streamId']: row for row in current_stages()}
    assert rows[3]['stage'] == 'received'
    assert rows[3]['stageLabel'] == '已收到'
    assert rows[3]['detail'] == '今天天气不错'
    assert rows[1]['turnId'] == 7


def test_gated_is_a_named_stage() -> None:
    enter_stage(GATED, 3, 'QQ 群聊 123', '未回复：group_not_mentioned')
    row = current_stages()[0]
    assert row['stage'] == 'gated'
    assert row['stageLabel'] == '静默接收'
    assert 'group_not_mentioned' in row['detail']


def test_same_stage_does_not_restart_clock() -> None:
    enter_stage(GATED, 3, 'QQ 群聊 123', '未回复')
    time.sleep(0.03)
    enter_stage(GATED, 3, 'QQ 群聊 123', '未回复')
    assert current_stages()[0]['stageElapsedMs'] >= 20


def test_changing_stage_restarts_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """切换阶段会重置计时：新阶段的已用时长从切换那一刻起算，不继承上一阶段。

    原写法是 `sleep(0.03)` 之后断言「新阶段已用 < 20 毫秒」，用的是真实墙钟。
    机器一忙，从 enter_stage 到取快照之间就可能超过 20 毫秒——两轮全量各红过一次，
    每次复跑又转绿，是典型的时间敏感断言而不是真回归。

    写入与读取都走 store.current_time（enter_stage 落库时不传 at，由 store 取时钟），
    所以钉死这一个名字就能让整条链路脱离墙钟，断言也随之从「小于某个阈值」
    变成精确值——那才是这条用例真正要证的东西。
    """
    clock = {'now': 1_000_000}
    monkeypatch.setattr(store, 'current_time', lambda: clock['now'])

    enter_stage(GATED, 3, 'QQ 群聊 123')
    clock['now'] += 30_000
    assert current_stages()[0]['stageElapsedMs'] == 30_000

    enter_stage(RECEIVED, 3, 'QQ 群聊 123')
    clock['now'] += 5
    assert current_stages()[0]['stageElapsedMs'] == 5


def test_snapshot_is_newest_first() -> None:
    enter_stage(GATED, 3, 'QQ 群聊 123')
    time.sleep(0.01)
    enter_stage(GENERATING, 1, '桌面')
    assert [row['streamId'] for row in current_stages()] == [1, 3]
