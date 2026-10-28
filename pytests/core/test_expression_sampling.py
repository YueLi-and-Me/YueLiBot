"""表达抽样口径：权重来自 learn_count，use_count 不参与；冷却按 6 小时窗口衰减。

对应任务书 T2 与 ★12 / ★13。用例直接调用 ``fetch_expression_pool``，注入固定
随机种子与固定毫秒时钟，统计口径与生产代码一致，不依赖真实时钟。
"""

from __future__ import annotations

from typing import Dict, Optional

import random
import sqlite3

import pytest

from src.core.agent.expression import EXPRESSION_COOLDOWN_MS, fetch_expression_pool

# 固定的「当前时间」毫秒时间戳；冷却系数全部相对它计算，避免真实时钟漂移。
_NOW_MS = 1_800_000_000_000
# 等权重候选出现次数的相对差容差：3000 轮抽样下抽样噪声约 1%，20% 足够排除误判。
_TOLERANCE = 0.20


def _seed_stream(db: sqlite3.Connection, stream_id: int) -> None:
    """插入一条测试会话行，供 expressions 的外键与按会话隔离使用。"""

    db.execute(
        'INSERT INTO streams (id, platform, kind, external_id) VALUES (?, ?, ?, ?)',
        (stream_id, 'test', 'group', f'test-{stream_id}'),
    )
    db.commit()


def _seed_expression(
    db: sqlite3.Connection,
    stream_id: int,
    situation: str,
    style: str,
    *,
    learn_count: int = 1,
    use_count: int = 0,
    last_used_at: Optional[int] = None,
) -> int:
    """插入一条 ``checked = 1`` 的表达行并返回主键。

    用例考的是抽样口径，闸门放行是前置条件；未复核行被排除由
    ``pytests/acceptance/test_expressions_wiring.py`` 单独把关。
    """

    cursor = db.execute(
        'INSERT INTO expressions'
        ' (situation, style, stream_id, learn_count, use_count, source, created_at,'
        ' checked, last_used_at)'
        ' VALUES (?, ?, ?, ?, ?, ?, 0, 1, ?)',
        (situation, style, stream_id, learn_count, use_count, '测试', last_used_at),
    )
    db.commit()
    return int(cursor.lastrowid)


# ── ★12 权重来自 learn_count，与 use_count 无关 ──────────────────────────


def test_sampling_weight_ignores_use_count(db) -> None:
    """use_count 一大一小的两条候选，抽中次数必须在抽样噪声内一致。

    use_count 同时被选中加一又当权重会构成正反馈，头部条目因此垄断提示词；
    改成 learn_count 后它只供审核页展示，不参与抽样。
    """

    stream_id = 41
    _seed_stream(db, stream_id)
    for index in range(10):
        _seed_expression(
            db, stream_id, f'中性情境{index}', f'中性说法{index}', learn_count=1,
        )
    low_use_id = _seed_expression(
        db, stream_id, '低使用情境', '低使用说法', learn_count=1, use_count=0,
    )
    high_use_id = _seed_expression(
        db, stream_id, '高使用情境', '高使用说法', learn_count=1, use_count=1000,
    )

    rng = random.Random(20261028)
    hits: Dict[int, int] = {low_use_id: 0, high_use_id: 0}
    for _ in range(3000):
        pool, total = fetch_expression_pool(db, stream_id, now=_NOW_MS, rng=rng)
        assert total == 12
        for sample in pool:
            if sample.id in hits:
                hits[sample.id] += 1

    low_hits, high_hits = hits[low_use_id], hits[high_use_id]
    relative_gap = abs(low_hits - high_hits) / max(low_hits, high_hits)
    assert relative_gap < _TOLERANCE, (
        f'use_count 影响了抽样：low_use={low_hits} high_use={high_hits}'
    )
    # 等权重抽样下两条都应稳定出现，排除「一条因某种原因被完全排除」的假通过。
    assert low_hits > 500 and high_hits > 500


def test_higher_learn_count_gets_more_picks(db) -> None:
    """对照：learn_count 高的组必须显著更常被抽中，证明权重确实换成了 learn_count。"""

    stream_id = 42
    _seed_stream(db, stream_id)
    low_ids = {
        _seed_expression(
            db, stream_id, f'低学习情境{index}', f'低学习说法{index}', learn_count=1,
        )
        for index in range(10)
    }
    high_ids = {
        _seed_expression(
            db, stream_id, f'高学习情境{index}', f'高学习说法{index}', learn_count=5,
        )
        for index in range(10)
    }

    rng = random.Random(20261028)
    low_hits = high_hits = 0
    for _ in range(1000):
        pool, _ = fetch_expression_pool(db, stream_id, now=_NOW_MS, rng=rng)
        for sample in pool:
            if sample.id in low_ids:
                low_hits += 1
            elif sample.id in high_ids:
                high_hits += 1

    # 高频子集（10 条）先独占一轮 5 抽，全量轮里高学习条目还有 5 倍权重。
    assert high_hits > low_hits * 3, (
        f'learn_count 未显著占优：high={high_hits} low={low_hits}'
    )
    assert low_hits > 50, f'低学习条目被完全垄断：high={high_hits} low={low_hits}'


# ── ★13 冷却：刚用过不进池，τ/2 权重减半，τ 之后恢复 ────────────────────


def test_cooldown_scales_weight_and_excludes_just_used(db) -> None:
    """冷却系数 min(1, Δt / τ)：刚用过为 0，τ/2 时约为一半，τ 之后恢复满权重。"""

    stream_id = 43
    _seed_stream(db, stream_id)
    normal_ids = {
        _seed_expression(
            db, stream_id, f'未使用情境{index}', f'未使用说法{index}', learn_count=1,
        )
        for index in range(10)
    }
    cold_id = _seed_expression(
        db, stream_id, '刚用过情境', '刚用过说法',
        learn_count=1, last_used_at=_NOW_MS,
    )
    half_id = _seed_expression(
        db, stream_id, '半冷情境', '半冷说法',
        learn_count=1, last_used_at=_NOW_MS - EXPRESSION_COOLDOWN_MS // 2,
    )
    full_id = _seed_expression(
        db, stream_id, '冷却结束情境', '冷却结束说法',
        learn_count=1, last_used_at=_NOW_MS - EXPRESSION_COOLDOWN_MS,
    )

    rng = random.Random(20261028)
    normal_hits = {sample_id: 0 for sample_id in normal_ids}
    half_hits = full_hits = 0
    runs = 3000
    for _ in range(runs):
        pool, total = fetch_expression_pool(db, stream_id, now=_NOW_MS, rng=rng)
        assert total == 13, '候选总数不受冷却过滤影响'
        picked_ids = {sample.id for sample in pool}
        assert cold_id not in picked_ids, '冷却系数为 0 的条目不进抽样'
        for sample in pool:
            if sample.id == half_id:
                half_hits += 1
            elif sample.id == full_id:
                full_hits += 1
            elif sample.id in normal_hits:
                normal_hits[sample.id] += 1

    average_normal = sum(normal_hits.values()) / len(normal_hits)
    # τ/2 的条目权重减半：稳定出现，但显著少于同 learn_count 的未使用条目。
    assert half_hits > 100, f'半冷条目从未出现：half={half_hits}'
    assert half_hits < average_normal * 0.75, (
        f'半冷条目没有明显降温：half={half_hits} normal_avg={average_normal:.0f}'
    )
    # τ 之后恢复满权重：与未使用条目的出现次数在容差内一致。
    assert abs(full_hits - average_normal) / average_normal < _TOLERANCE, (
        f'冷却结束后权重未恢复：full={full_hits} normal_avg={average_normal:.0f}'
    )


def test_all_rows_cooling_returns_empty_pool_with_honest_total(db) -> None:
    """全部条目都在冷却时按候选不足处理：返回空池，不抛错，总数如实。"""

    stream_id = 44
    _seed_stream(db, stream_id)
    for index in range(12):
        _seed_expression(
            db, stream_id, f'全冷情境{index}', f'全冷说法{index}',
            learn_count=1, last_used_at=_NOW_MS,
        )

    pool, total = fetch_expression_pool(db, stream_id, now=_NOW_MS)
    assert pool == []
    assert total == 12


def test_cooling_drops_eligible_below_floor_returns_empty_pool(db) -> None:
    """冷却过滤后不足 MIN_POOL_CANDIDATES 时沿用现有规则：本轮不选，不报错。"""

    stream_id = 45
    _seed_stream(db, stream_id)
    for index in range(9):
        _seed_expression(
            db, stream_id, f'可用情境{index}', f'可用说法{index}', learn_count=1,
        )
    for index in range(3):
        _seed_expression(
            db, stream_id, f'冷却情境{index}', f'冷却说法{index}',
            learn_count=1, last_used_at=_NOW_MS,
        )

    pool, total = fetch_expression_pool(db, stream_id, now=_NOW_MS)
    assert pool == []
    assert total == 12, 'total 统计 checked = 1 的全部行，不受冷却过滤影响'


# ── 新签名：now 为必填关键字参数 ────────────────────────────────────────


def test_fetch_requires_now_as_keyword(db) -> None:
    """统一时钟由调用方传入：now 必填且只接受关键字，漏传是调用方 bug 要炸。"""

    stream_id = 46
    _seed_stream(db, stream_id)
    with pytest.raises(TypeError):
        fetch_expression_pool(db, stream_id)
    with pytest.raises(TypeError):
        fetch_expression_pool(db, stream_id, _NOW_MS)
