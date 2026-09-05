"""W4 知识索引与向量重算验收。

对应 docs/memory-w4-knowledge.md 第二节：
- ★W4-1：重算后 embedding 字节长度与当前向量模型维度一致，不存在旧维度长度的行；
- ★W4-2：中断后重跑自动接上（待办集合就是 embedding IS NULL），已完成的行不重复计算；
- ★W4-3：向量服务整批失败时那批仍为 NULL、记了事件，不抛异常、不中断调用方；
- ★W4-6：[vector].enabled = false 时不发起任何向量请求；
- knowledge_fts 是 content='' 的外部内容表，rowid 必须与 knowledge.id 显式对齐。
"""

from __future__ import annotations

import struct
import sqlite3

import pytest

from src.core.memory.knowledge import (
    index_knowledge,
    knowledge_without_embedding,
    store_knowledge_embedding,
)
from src.core.memory.similarity import exact_key
from src.core.memory.store import MemoryStore

from scripts.maintain.knowledge_reindex import recompute_embeddings, reindex

_NOW = 1_750_000_000_000


def _seed(db: sqlite3.Connection, contents: list[str]) -> list[int]:
    """以迁移落库后的形状播种：正文在库、FTS 未建、embedding 为 NULL。"""
    ids = []
    for content in contents:
        cur = db.execute(
            'INSERT INTO knowledge (content, content_key, source, created_at)'
            ' VALUES (?, ?, ?, ?)',
            (content, exact_key(content), 'migrate-m4', _NOW),
        )
        ids.append(cur.lastrowid)
    db.commit()
    return ids


class _FakeEmbedClient:
    """复刻 EmbeddingClient.embed 的既有口径：按 96 条分批，失败的批整批留 None。

    :param dim: 当前向量模型维度。
    :param failing: 要模拟失败的批号集合（从 0 起）。
    """

    def __init__(self, dim: int = 4, failing: frozenset[int] = frozenset()) -> None:
        self.dim = dim
        self.failing = failing
        self.batches: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[bytes | None]:
        results: list[bytes | None] = [None] * len(texts)
        for start in range(0, len(texts), 96):
            batch = texts[start:start + 96]
            self.batches.append(list(batch))
            if start // 96 in self.failing:
                continue
            for i, _ in enumerate(batch):
                results[start + i] = struct.pack(f'{self.dim}f', *([0.5] * self.dim))
        return results


def test_index_builds_fts_with_aligned_rowid(db) -> None:
    """knowledge_fts 的 rowid 与 knowledge.id 显式对齐，且重复索引不重复建行。"""
    store = MemoryStore(db)
    del store
    ids = _seed(db, ['光年之外的殖民船仍在航行', '幻影坦克是天启的克星', '勾股定理'])

    indexed = index_knowledge(db, limit=1000)
    assert indexed == 3
    assert index_knowledge(db, limit=1000) == 0, '已建索引的行不应重复建'

    # content='' 的外部内容表不存列值，查 tokens 只会得到 NULL；
    # 能验证的是 rowid 对齐与 MATCH 命中，可读的分词文本在 knowledge.tokens_v2。
    fts_rows = db.execute('SELECT rowid FROM knowledge_fts').fetchall()
    assert sorted(r[0] for r in fts_rows) == sorted(ids), 'rowid 必须显式对齐主键'
    stored = db.execute('SELECT id, tokens_v2 FROM knowledge').fetchall()
    assert all(r[1].strip() for r in stored), 'tokens_v2 应与 FTS 同口径落库'
    # 词面命中验证：按第二条的关键词查，命中的必须是第二条。
    hit = db.execute(
        "SELECT rowid FROM knowledge_fts WHERE knowledge_fts MATCH '幻影'"
    ).fetchall()
    assert [r[0] for r in hit] == [ids[1]]


async def test_recompute_writes_current_dim_vectors(db) -> None:
    """★W4-1：重算后的字节长度 == 当前模型维度 × 4，不存在旧维度长度的行。"""
    MemoryStore(db)
    ids = _seed(db, [f'第 {i} 条知识' for i in range(5)])
    client = _FakeEmbedClient(dim=4)

    report = await recompute_embeddings(db, client)

    assert report.embedded == 5 and report.failed == 0
    rows = db.execute('SELECT embedding FROM knowledge').fetchall()
    assert all(r[0] is not None for r in rows)
    assert all(len(r[0]) == 4 * 4 for r in rows), 'packed float32 长度必须等于维度×4'
    assert not any(len(r[0]) == 8 * 4 for r in rows), '不允许存在旧库维度的行'
    assert client.batches, '确实发起了向量请求'


async def test_recompute_resumes_after_partial_failure(db) -> None:
    """★W4-2：整批失败后重跑自动接上；已完成的行不重复计算，最终总数正确。"""
    MemoryStore(db)
    _seed(db, [f'编号 {i} 的知识条目' for i in range(250)])

    first = _FakeEmbedClient(dim=4, failing=frozenset({1, 2}))
    report1 = await recompute_embeddings(db, first)
    assert report1.embedded == 96 and report1.failed == 154
    remaining = db.execute(
        'SELECT COUNT(*) FROM knowledge WHERE embedding IS NULL'
    ).fetchone()[0]
    assert remaining == 154

    done_contents = {
        r[0] for r in db.execute(
            'SELECT content FROM knowledge WHERE embedding IS NOT NULL'
        ).fetchall()
    }
    second = _FakeEmbedClient(dim=4)
    report2 = await recompute_embeddings(db, second)
    assert report2.embedded == 154 and report2.failed == 0
    retried = {text for batch in second.batches for text in batch}
    assert not (retried & done_contents), '已完成的行不允许重复计算'
    assert db.execute(
        'SELECT COUNT(*) FROM knowledge WHERE embedding IS NULL'
    ).fetchone()[0] == 0


async def test_recompute_total_failure_leaves_null_and_continues(db) -> None:
    """★W4-3：向量服务整批失败时那批仍为 NULL、记入报告，不抛异常。"""
    MemoryStore(db)
    _seed(db, [f'会失败的第 {i} 条' for i in range(200)])
    client = _FakeEmbedClient(dim=4, failing=frozenset({0, 1, 2}))

    report = await recompute_embeddings(db, client)   # 不抛异常

    assert report.embedded == 0 and report.failed == 200
    assert db.execute(
        'SELECT COUNT(*) FROM knowledge WHERE embedding IS NOT NULL'
    ).fetchone()[0] == 0


async def test_reindex_skips_vectors_when_disabled(db) -> None:
    """★W4-6：向量开关关闭时不发起任何向量请求；FTS 索引仍照常补齐。"""
    MemoryStore(db)
    _seed(db, ['开关关闭时也要能按词面搜到的知识'])

    class _Spy:
        called = False

        async def embed(self, texts):
            _Spy.called = True
            return []

    report = await reindex(db, vector_enabled=False, client=_Spy())

    assert not _Spy.called, 'vector.enabled=false 时不允许发起向量请求'
    assert report.embedded == 0
    assert db.execute('SELECT COUNT(*) FROM knowledge_fts').fetchone()[0] == 1
    assert db.execute(
        'SELECT COUNT(*) FROM knowledge WHERE embedding IS NULL'
    ).fetchone()[0] == 1


async def test_reindex_full_path(db) -> None:
    """开关打开时：先补 FTS，再重算向量，两步都体现在报告里。"""
    MemoryStore(db)
    _seed(db, [f'全链路第 {i} 条' for i in range(3)])
    report = await reindex(db, vector_enabled=True, client=_FakeEmbedClient(dim=4))
    assert report.indexed == 3 and report.embedded == 3 and report.failed == 0
    assert db.execute(
        'SELECT COUNT(*) FROM knowledge WHERE embedding IS NULL'
    ).fetchone()[0] == 0
