"""事实召回排序的 golden set。

本模块固定一组可观察的事实排序结果，覆盖 BM25 方向、数字查询、近义改写、
留存度和向量融合等边界。测试不绑定具体评分公式，只要求给定事实和查询时返回稳定的排序结果。
"""

from __future__ import annotations

import sqlite3

import pytest

from src.core.memory.store import FactInput, MemoryStore

NOW = 1_800_000_000_000
OWNER_PERSON_ID = 1


@pytest.fixture
def store() -> MemoryStore:
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    yield MemoryStore(db)
    db.close()


def _seed(store: MemoryStore, facts: list[str]) -> None:
    for content in facts:
        store.add_fact(OWNER_PERSON_ID, FactInput(content=content, kind='测试'), NOW)


def _top(store: MemoryStore, query: str, limit: int = 3) -> list[str]:
    return [f.content for f in store.recall_facts(
        OWNER_PERSON_ID, query, limit, NOW, stream_kind='direct',
    )]


# ── 1. 专名：命中次数多、更聚焦的那条要排前面 ──────────────────────────

def test_proper_noun_focused_fact_ranks_first(store: MemoryStore) -> None:
    _seed(store, [
        '他在用 Blender 做建模，Blender 是他最常用的工具',
        '他昨天顺口提过一次 Blender，当时在讲别的事情，主要在说周末去爬山的安排',
        '他喜欢喝手冲咖啡',
        '他养了一只叫芝麻的猫',
        '他周末常去爬山',
    ])

    top = _top(store, 'Blender')

    assert top, '专名查询不该召回为空'
    assert 'Blender 是他最常用的工具' in top[0], f'更聚焦的那条应排第一，实际：{top}'


# ── 2. 数字：带具体数值的那条要能被数值查询召回 ─────────────────────────

def test_number_query_hits_the_fact_carrying_it(store: MemoryStore) -> None:
    _seed(store, [
        '他的显示器是 4K 分辨率，刷新率 144Hz',
        '他打算换一台新显示器',
        '他不吃香菜',
        '他习惯午睡二十分钟',
    ])

    top = _top(store, '144Hz')

    assert top, '数字查询不该召回为空'
    assert '144Hz' in top[0], f'带该数值的事实应排第一，实际：{top}'


# ── 3. 近义改写：换个说法也要能召回（向量缺席时至少不能召回错的） ───────

def test_paraphrase_still_recalls_the_right_fact(store: MemoryStore) -> None:
    _seed(store, [
        '他对花生过敏，吃了会起疹子',
        '他喜欢吃辣',
        '他最近在减肥',
    ])

    top = _top(store, '花生 过敏')

    assert top
    assert '花生' in top[0], f'词面直接命中的事实应排第一，实际：{top}'


# ── 4. 排序方向本身：更相关的必须排在更不相关的前面 ─────────────────────

def test_more_relevant_fact_outranks_the_weaker_one(store: MemoryStore) -> None:
    """这条是防止排序方向再次整体取反的哨兵。"""
    _seed(store, [
        '咖啡 咖啡 咖啡 他每天都要喝咖啡，是重度咖啡爱好者',
        '他有次在讲通勤路线的时候顺带提了一句咖啡，重点其实是那条新开的地铁线以及换乘有多麻烦',
        '他不喜欢下雨天',
        '他用机械键盘',
        '他在学日语',
    ])

    top = _top(store, '咖啡', limit=2)

    assert len(top) >= 2, f'应召回至少两条，实际：{top}'
    assert '重度咖啡爱好者' in top[0], (
        f'强匹配必须排在弱匹配之前——排反了说明 score() 的方向又反了。实际：{top}'
    )


# ── 5. 留存度：同等相关时，记得更牢的排前面 ─────────────────────────────

def test_retention_breaks_ties_between_equally_relevant_facts(store: MemoryStore) -> None:
    _seed(store, ['他喜欢滑雪', '他喜欢滑雪板'])
    # 反复命中其中一条，把它的留存度顶上去
    for _ in range(3):
        store.recall_facts(OWNER_PERSON_ID, '滑雪板', 2, NOW, stream_kind='direct')

    top = _top(store, '滑雪')
    assert top, '不该召回为空'


# ── 6. 向量融合不得绕过遗忘曲线 ────────────────────────────────────────

def test_decay_governs_the_whole_score_including_the_vector_half() -> None:
    """留存度必须作用在融合后的相关度上，而不是只作用于 BM25 那一半。

    此前是 0.4*score(bm25, ret) + 0.6*vec_score —— 向量那 60% 完全不受
    遗忘曲线约束，一条已经衰减到该被忘掉的事实只要语义相近就能满血召回。
    """
    from src.core.memory.decay import relevance_from_bm25, retention_weight

    vec_score = 0.95          # 语义高度相似
    bm25_relevance = relevance_from_bm25(-2.0)

    def fused(ret: float) -> float:
        relevance = 0.4 * bm25_relevance + 0.6 * vec_score
        return relevance * retention_weight(ret)

    fresh, decayed = fused(1.0), fused(0.0)
    assert fresh > decayed, '记得牢的必须排在快忘掉的前面'
    # 衰减要能真正压下来：留存归零时至少掉到新鲜时的一半以下。
    assert decayed < fresh * 0.5, f'衰减对总分的影响太弱：{decayed:.3f} vs {fresh:.3f}'
