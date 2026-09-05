"""表达方式接线机检（对应派发文档第 7 节的机检项）。

覆盖：

1. E-1 修复脚本幂等，且不以「使用」开头的行不被改动；
2. 候选池为空时 `_pick_expression_habits` 仍 emit `source='disabled'`，不调用模型；
3. 候选池非空时事件为 `source='model'`，`pool` 等于实际候选数，`count <= limit`；
4. 加权抽样：count 1 与 count 10 两组，高频组显著高于但不垄断低频组；
5. 候选总数 < 10 时返回空池；
6. 候选池按 `stream_id` 隔离；
8. 只有被选中的行 `use_count` 增加、`last_used_at` 被写入；
9. 旧配置写出 `expression_habits` 时启动明确报错。
10. 批量删除：只删请求行、不存在的 id 静默跳过；候选跌破下限（含删空整条
   会话）如实回报而不拦截；已驳回的墓碑行不受牵连。
11. 复核是放行闸门：未复核（`checked = 0`）的行既不进候选池也不计入候选总数，
   整条会话都没复核过时池子为空。

第 7 项（选择提示词不含 style 文本）在 pytests/llm/test_expression_select.py 覆盖。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List

import asyncio
import json
import random
import sqlite3

import pytest

from scripts.fix_expression_style_prefix import fix as fix_style_prefix
from src.core.agent.expression import fetch_expression_pool
from src.core.api.http import _delete_expressions
from src.core.observe.store import search_events


def _seed_stream(db: sqlite3.Connection, stream_id: int) -> None:
    db.execute(
        'INSERT INTO streams (id, platform, kind, external_id) VALUES (?, ?, ?, ?)',
        (stream_id, 'test', 'group', f'test-{stream_id}'),
    )
    db.commit()


def _seed_expressions(
    db: sqlite3.Connection,
    stream_id: int,
    rows: List[tuple[str, str, int]],
) -> List[int]:
    """按 (situation, style, use_count) 种表达行，返回行 id 列表。

    统一种成 ``checked = 1``（人工确认）：候选池以复核为放行闸门，未复核的行
    根本不进池。这些用例考的是抽样、隔离与回写口径，需要的是「已经是候选」的
    行；未复核行被排除这件事由 :func:`test_unreviewed_rows_never_enter_pool`
    单独把关。
    """

    ids: List[int] = []
    for situation, style, use_count in rows:
        cursor = db.execute(
            'INSERT INTO expressions'
            ' (situation, style, stream_id, use_count, source, created_at, checked)'
            ' VALUES (?, ?, ?, ?, ?, 0, 1)',
            (situation, style, stream_id, use_count, '测试'),
        )
        ids.append(int(cursor.lastrowid))
    db.commit()
    return ids


# ── 1. E-1 数据修复脚本 ────────────────────────────────────────────────


def test_fix_script_is_idempotent_and_preserves_unmatched(db) -> None:
    stream_id = 7
    _seed_stream(db, stream_id)
    _seed_expressions(db, stream_id, [
        ('情境甲', '使用反问句加强语气', 3),
        ('情境乙', '使用 简单直接的疑问句式', 1),
        ('情境丙', '本来就正常的说法', 5),
    ])

    first = fix_style_prefix(db)
    assert first.matched == 2
    assert first.updated == 2

    rows_after_first = db.execute(
        'SELECT id, situation, style, use_count FROM expressions ORDER BY id'
    ).fetchall()
    assert [tuple(r)[2] for r in rows_after_first] == [
        '反问句加强语气',
        '简单直接的疑问句式',
        '本来就正常的说法',
    ]
    # 只动 style 文本：use_count 历史值一个都不许变。
    assert [tuple(r)[3] for r in rows_after_first] == [3, 1, 5]

    second = fix_style_prefix(db)
    assert second.matched == 0
    assert second.updated == 0
    rows_after_second = db.execute(
        'SELECT id, situation, style, use_count FROM expressions ORDER BY id'
    ).fetchall()
    assert rows_after_second == rows_after_first


def test_fix_script_skips_unique_key_collisions(db) -> None:
    """剥前缀后若与既有行撞 (situation, style, stream_id)，保持原样不合并不删除。"""

    stream_id = 7
    _seed_stream(db, stream_id)
    _seed_expressions(db, stream_id, [
        ('情境甲', '反问句加强语气', 1),
        ('情境甲', '使用反问句加强语气', 1),
    ])

    report = fix_style_prefix(db)
    assert report.skipped_conflict == 1
    assert db.execute('SELECT COUNT(*) FROM expressions').fetchone()[0] == 2
    # 重跑仍然幂等：撞键行依旧保持原样。
    assert fix_style_prefix(db).updated == 0


# ── 4/5/6. 候选池读取 ──────────────────────────────────────────────────


def test_pool_empty_below_min_candidates(db) -> None:
    stream_id = 7
    _seed_stream(db, stream_id)
    _seed_expressions(db, stream_id, [(f'情境{i}', f'说法{i}', 1) for i in range(9)])

    pool, total = fetch_expression_pool(db, stream_id, rng=random.Random(1))
    assert pool == []
    assert total == 9

    # 边界：补足第 10 条后立即可抽。
    _seed_expressions(db, stream_id, [('情境9', '说法9', 1)])
    pool, total = fetch_expression_pool(db, stream_id, rng=random.Random(1))
    assert pool and total == 10


def test_weighted_sampling_favors_but_does_not_monopolize(db) -> None:
    """count 线性映射到 [1, 5]：高频组被抽中次数显著更高，但低频组始终有非零概率。"""

    stream_id = 7
    _seed_stream(db, stream_id)
    low_ids = set(_seed_expressions(
        db, stream_id, [(f'低频{i}', f'低频说法{i}', 1) for i in range(10)]
    ))
    high_ids = set(_seed_expressions(
        db, stream_id, [(f'高频{i}', f'高频说法{i}', 10) for i in range(10)]
    ))

    rng = random.Random(20260825)
    low_hits = high_hits = 0
    runs = 1000
    for _ in range(runs):
        pool, _ = fetch_expression_pool(db, stream_id, rng=rng)
        for sample in pool:
            if sample.id in low_ids:
                low_hits += 1
            elif sample.id in high_ids:
                high_hits += 1

    # 高频子集满 10 条先独占一轮 5 抽，全量轮里高频还有 5 倍权重：高频必然显著更高。
    assert high_hits > low_hits * 3, f'高频未显著占优：high={high_hits} low={low_hits}'
    # 权重上限 5 的直接体现：低频组在 1000 次抽样里仍然稳定出现，不是被垄断。
    assert low_hits > 50, f'低频组被垄断：high={high_hits} low={low_hits}'


def test_pool_isolated_by_stream(db) -> None:
    _seed_stream(db, 7)
    _seed_stream(db, 8)
    ids_7 = set(_seed_expressions(db, 7, [(f'七群情境{i}', f'七群说法{i}', 1) for i in range(12)]))
    _seed_expressions(db, 8, [(f'八群情境{i}', f'八群说法{i}', 3) for i in range(15)])

    rng = random.Random(7)
    for _ in range(100):
        pool, total = fetch_expression_pool(db, 7, rng=rng)
        assert total == 12
        assert pool, '候选总数足够时池子不应为空'
        assert all(sample.id in ids_7 for sample in pool)
        assert all('八群' not in sample.situation for sample in pool)


def test_unreviewed_rows_never_enter_pool(db) -> None:
    """未复核（``checked = 0``）的行既不进候选池，也不计入候选总数。

    学习器抄的是她自己说过的话。不设放行闸门时，无意中形成的口癖会被原样转录
    成「表达习惯」再喂回去形成正反馈，因此复核必须是使用的前置条件。
    """

    _seed_stream(db, 21)
    reviewed = set(_seed_expressions(db, 21, [(f'已复核情境{i}', f'已复核说法{i}', 1)
                                              for i in range(12)]))
    db.executemany(
        'INSERT INTO expressions'
        ' (situation, style, stream_id, use_count, source, created_at, checked)'
        ' VALUES (?, ?, 21, 0, ?, 0, 0)',
        [(f'待复核情境{i}', f'待复核说法{i}', '测试') for i in range(30)],
    )
    db.commit()

    rng = random.Random(21)
    for _ in range(50):
        pool, total = fetch_expression_pool(db, 21, rng=rng)
        assert total == 12, '候选总数只数已复核的行'
        assert all(sample.id in reviewed for sample in pool)
        assert all('待复核' not in sample.style for sample in pool)


def test_pool_empty_when_nothing_reviewed(db) -> None:
    """整条会话都没复核过时候选池为空——宁可不注入，也不注入没人看过的东西。"""

    _seed_stream(db, 22)
    db.executemany(
        'INSERT INTO expressions'
        ' (situation, style, stream_id, use_count, source, created_at, checked)'
        ' VALUES (?, ?, 22, 0, ?, 0, 0)',
        [(f'情境{i}', f'说法{i}', '测试') for i in range(40)],
    )
    db.commit()

    pool, total = fetch_expression_pool(db, 22, rng=random.Random(22))
    assert pool == []
    assert total == 0


# ── 2/3/8. ChatService 挑选与回写 ─────────────────────────────────────


class _Provider:
    """按脚本吐一段文本的假 provider，同时记下收到的调用参数。"""

    def __init__(self, payload: str) -> None:
        self._payload = payload
        self.calls: List[Dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> AsyncIterator[dict]:
        self.calls.append(kwargs)
        payload = self._payload

        async def _gen() -> AsyncIterator[dict]:
            yield {'text': payload}

        return _gen()


def _chat(db, expression_provider) -> Any:
    from src.core.config.schema import Config
    from src.core.services.chat import ChatService

    return ChatService(
        db=db,
        chat_provider=None,
        proactive_provider=None,
        summary_provider=None,
        push_event=lambda *_: None,
        cfg=Config(),
        expression_provider=expression_provider,
    )


def _expression_select_events() -> List[Dict[str, Any]]:
    return search_events(kinds=['expression_select']).events


def test_empty_pool_emits_disabled_without_calling_model(db) -> None:
    provider = _Provider('{"selected": [1]}')
    chat = _chat(db, provider)
    context = chat.desktop_context

    picked = asyncio.run(chat._pick_expression_habits(context, '在吗', [], None))

    assert picked == []
    assert provider.calls == [], '候选池为空时不许调用模型'
    events = _expression_select_events()
    assert len(events) == 1
    assert events[0]['source'] == 'disabled'
    assert events[0]['count'] == 0
    assert events[0]['pool'] == 0
    assert events[0]['total'] == 0


def test_no_provider_keeps_legacy_disabled_event(db) -> None:
    """没有表达模型槽时维持旧形态：disabled，没有 pool/total 可报。"""

    chat = _chat(db, None)
    picked = asyncio.run(chat._pick_expression_habits(chat.desktop_context, '在吗', [], None))

    assert picked == []
    events = _expression_select_events()
    assert len(events) == 1
    assert events[0]['source'] == 'disabled'
    assert 'pool' not in events[0]


def test_nonempty_pool_emits_model_source_with_pool_and_total(db) -> None:
    provider = _Provider(json.dumps({'selected': [1, 2]}))
    chat = _chat(db, provider)
    context = chat.desktop_context
    # desktop 会话 id=1 由 SEED 自带；全部 use_count=1 时高频子集为空，只从全量抽 5 条。
    _seed_expressions(db, context.stream.id, [(f'情境{i}', f'说法{i}', 1) for i in range(15)])

    picked = asyncio.run(chat._pick_expression_habits(context, '在吗', [], None))

    assert len(picked) == 2
    assert len(provider.calls) == 1
    events = _expression_select_events()
    assert len(events) == 1
    event = events[0]
    assert event['source'] == 'model'
    assert event['pool'] == 5
    assert event['total'] == 15
    assert event['count'] == 2
    assert event['count'] <= 4
    assert all(habit.startswith('当“') for habit in event['habits'])


def test_only_selected_rows_get_use_count_and_last_used_at(db) -> None:
    provider = _Provider(json.dumps({'selected': [1]}))
    chat = _chat(db, provider)
    context = chat.desktop_context
    _seed_expressions(db, context.stream.id, [(f'情境{i}', f'说法{i}', 1) for i in range(15)])

    picked = asyncio.run(chat._pick_expression_habits(context, '在吗', [], None))
    assert len(picked) == 1

    changed = db.execute(
        'SELECT id, situation, style, use_count, last_used_at FROM expressions'
        ' WHERE use_count != 1 OR last_used_at IS NOT NULL'
    ).fetchall()
    assert len(changed) == 1, '只允许被选中的那行发生变化'
    row = changed[0]
    assert row['id'] == picked[0].id
    assert row['use_count'] == 2, '选中一次只加一'
    assert row['last_used_at'] is not None and row['last_used_at'] > 0

    untouched = db.execute(
        'SELECT COUNT(*) FROM expressions WHERE use_count = 1 AND last_used_at IS NULL'
    ).fetchone()[0]
    assert untouched == 14


# ── 9. 退休配置字段 ────────────────────────────────────────────────────


@pytest.mark.parametrize('field', ['expression_habits', 'proactive_expression_habits'])
def test_retired_expression_config_fields_fail_fast(field: str) -> None:
    from src.core.config.schema import PersonalityConfig

    raw = {
        'birthday': '',
        'personality': '人设',
        'reply_style': '表达',
        'tone_probability': 0.0,
        'tone_variants': [],
        field: ['旧配置里的手写表达习惯'],
    }
    with pytest.raises(ValueError, match=rf'personality\.{field} 已取消'):
        PersonalityConfig.model_validate(raw)


# ── 10. 批量删除 ───────────────────────────────────────────────────────


def test_batch_delete_removes_only_requested_rows(db) -> None:
    """批量删除只动请求的行，不存在的 id 静默跳过而不整批失败。

    并发下一部分 id 可能已被别处删掉，若为此回滚整批，界面上的批量操作会变
    得随机失败且无法自愈；因此实删数允许小于请求数。
    """

    _seed_stream(db, 3)
    ids = _seed_expressions(db, 3, [(f's{i}', f'y{i}', 1) for i in range(15)])

    deleted, low_pools = _delete_expressions(db, ids[:4] + [999999])

    assert deleted == 4
    assert low_pools == []
    remaining = {int(row[0]) for row in db.execute('SELECT id FROM expressions')}
    assert remaining == set(ids[4:])


def test_batch_delete_reports_pool_dropping_below_floor(db) -> None:
    """候选数跌破下限时如实回报，不拦截删除。"""

    _seed_stream(db, 3)
    ids = _seed_expressions(db, 3, [(f's{i}', f'y{i}', 1) for i in range(15)])

    deleted, low_pools = _delete_expressions(db, ids[:6])

    assert deleted == 6
    assert low_pools == [{'streamId': 3, 'candidates': 9}]
    # 回报归回报，删除照做：低于下限的后果是取池返回空，由界面告知使用者。
    pool, total = fetch_expression_pool(db, 3)
    assert total == 9
    assert pool == []


def test_batch_delete_reports_stream_emptied_to_zero(db) -> None:
    """整条会话被删空时同样要报出来。

    受影响会话必须在删除之前取：删空之后该会话在 expressions 里不再有任何行，
    事后按 ``GROUP BY stream_id`` 统计根本不会出现它，而候选归零恰恰是后果最
    重的一种。
    """

    _seed_stream(db, 7)
    ids = _seed_expressions(db, 7, [(f'u{i}', f'w{i}', 1) for i in range(3)])

    deleted, low_pools = _delete_expressions(db, ids)

    assert deleted == 3
    assert low_pools == [{'streamId': 7, 'candidates': 0}]


def test_batch_delete_keeps_rejected_tombstones(db) -> None:
    """已驳回的行不计入候选数，也不因为批量删除别的行而被顺手清掉。

    驳回行是「这条判过了」的记号，靠 ``UNIQUE(situation, style, stream_id)``
    挡住学习器重新学回同样的说法；被误删等于这道记号失效。
    """

    _seed_stream(db, 3)
    live = _seed_expressions(db, 3, [(f's{i}', f'y{i}', 1) for i in range(12)])
    tombs = _seed_expressions(db, 3, [(f'r{i}', f'q{i}', 1) for i in range(4)])
    db.executemany(
        'UPDATE expressions SET checked = -1 WHERE id = ?', [(i,) for i in tombs])
    db.commit()

    deleted, low_pools = _delete_expressions(db, live[:3])

    assert deleted == 3
    # 剩 9 条可用：4 条墓碑不算候选，所以照样跌破下限。
    assert low_pools == [{'streamId': 3, 'candidates': 9}]
    survived = {int(row[0]) for row in db.execute(
        'SELECT id FROM expressions WHERE checked = -1')}
    assert survived == set(tombs)
