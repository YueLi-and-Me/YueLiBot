"""联想边衰减后台服务回归。

钉住三件事：冻结判据与 ``decay_edges`` 一致、启动时先扫一次（停机期间边照常衰减，
长期不开机的部署重启后应立刻收敛）、以及停止后不再留下后台任务。

背景：``decay_edges`` 自 W6 联想层落地起一直没有调用方，边只增不减。本服务是它的
调度点；边的时间尺度以月计（半衰期 720 小时，首次建边约 54 天才跌破阈值），所以
用低频轮询而不是挂在回合路径上。
"""

from __future__ import annotations

import asyncio
import math
import sqlite3

import pytest

from src.core.runtime.clock import now as current_time
from src.core.db.schema import DDL, SEED
from src.core.memory.association import EDGE_BOOST, EDGE_HALF_LIFE_HOURS
from src.core.memory.decay import FREEZE
from src.core.services.edge_decay import EdgeDecayService

MS_PER_HOUR = 3_600_000


def _database() -> sqlite3.Connection:
    """建一个带完整 schema 的内存库。

    :return: 已建表并写入种子数据的连接。
    副作用：创建内存数据库。
    """
    db = sqlite3.connect(':memory:')
    db.executescript(DDL)
    db.executescript(SEED)
    return db


def _edge(db: sqlite3.Connection, *, strength: float, age_hours: float, now: int) -> int:
    """插入一条指定强度与年龄的边。

    :param db: 目标连接。
    :param strength: 边强度。
    :param age_hours: 距今小时数，用于构造衰减程度。
    :param now: 当前毫秒时间戳。
    :return: 新边的主键。
    副作用：写入 memory_nodes 与 memory_edges。
    """
    seq = db.execute('SELECT COUNT(*) FROM memory_nodes').fetchone()[0]
    source = db.execute(
        "INSERT INTO memory_nodes(ref_kind, ref_id) VALUES ('fact', ?)", (seq + 1,),
    ).lastrowid
    target = db.execute(
        "INSERT INTO memory_nodes(ref_kind, ref_id) VALUES ('fact', ?)", (seq + 2,),
    ).lastrowid
    cur = db.execute(
        '''INSERT INTO memory_edges(source_id, target_id, strength, updated_at, active)
           VALUES (?, ?, ?, ?, 1)''',
        (source, target, strength, now - int(age_hours * MS_PER_HOUR)),
    )
    db.commit()
    return int(cur.lastrowid)


@pytest.mark.asyncio
async def test_startup_freezes_stale_edges_before_first_interval() -> None:
    """启动时先扫一次：跌破阈值的边立刻冻结，新鲜的边不动。"""
    db = _database()
    now = current_time()
    # 首次建边强度 0.35、半衰期 720 小时，跌破 0.1 需要约 1301 小时。
    stale = _edge(db, strength=EDGE_BOOST, age_hours=2000, now=now)
    fresh = _edge(db, strength=EDGE_BOOST, age_hours=10, now=now)

    service = EdgeDecayService(db)
    try:
        await service.startup()
    finally:
        await service.shutdown()

    assert db.execute('SELECT active FROM memory_edges WHERE id = ?', (stale,)).fetchone()[0] == 0
    assert db.execute('SELECT active FROM memory_edges WHERE id = ?', (fresh,)).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_threshold_matches_decay_edges_boundary() -> None:
    """冻结判据不额外收紧：留存度刚好在阈值之上的边保持活跃。"""
    db = _database()
    now = current_time()
    # 留存度 = strength * 2 ** (-hours / 半衰期)；取阈值前后各一天，避免踩在边界上。
    boundary_hours = EDGE_HALF_LIFE_HOURS * math.log2(EDGE_BOOST / FREEZE)
    just_above = _edge(db, strength=EDGE_BOOST, age_hours=boundary_hours - 24, now=now)
    just_below = _edge(db, strength=EDGE_BOOST, age_hours=boundary_hours + 24, now=now)

    service = EdgeDecayService(db)
    try:
        await service.startup()
    finally:
        await service.shutdown()

    assert db.execute('SELECT active FROM memory_edges WHERE id = ?', (just_above,)).fetchone()[0] == 1
    assert db.execute('SELECT active FROM memory_edges WHERE id = ?', (just_below,)).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_shutdown_leaves_no_pending_task() -> None:
    """停止后轮询任务已结束，不会在事件循环里留下悬挂任务。"""
    db = _database()
    service = EdgeDecayService(db)
    await service.startup()
    running = [t for t in asyncio.all_tasks() if t.get_name() == 'edge-decay-sweep']
    assert running, '启动后应存在名为 edge-decay-sweep 的轮询任务'

    await service.shutdown()

    assert all(t.done() for t in running)
    assert not [t for t in asyncio.all_tasks()
                if t.get_name() == 'edge-decay-sweep' and not t.done()]
