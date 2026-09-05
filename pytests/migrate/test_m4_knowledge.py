"""W2-M4 知识迁移验收：知识正文 + 图谱结构。

对应 docs/memory-w2-migration.md 的 M-4 块与第五节断言：
- ★M-0：dry-run 计数报告与实际写入逐块一致；
- ★M-2：重复执行不产生重复行；
- ★M-4：迁入的 knowledge 行 embedding 全部为 NULL，一个旧向量数值都不搬；
- 块内要点：content_key 与 facts 同口径 exact_key()、图谱按 concept 名解析、
  端点缺失的边跳过并计数、memory_items 不导。
"""

from __future__ import annotations

import sqlite3

import pytest

from src.core.memory.similarity import exact_key
from src.core.memory.store import MemoryStore

from scripts.migrate.m4_knowledge import main, migrate_graph, migrate_knowledge


# 旧库三张先生表的形状，按 2025-07 真库勘察结果逐列复刻（含我们绝不读取的列）。
_OLD_DDL = '''
CREATE TABLE knowledges (
  id INTEGER NOT NULL PRIMARY KEY,
  content TEXT NOT NULL,
  embedding TEXT NOT NULL
);
CREATE TABLE graph_nodes (
  id INTEGER NOT NULL PRIMARY KEY,
  concept TEXT NOT NULL,
  memory_items TEXT NOT NULL,
  hash TEXT NOT NULL,
  created_time REAL NOT NULL,
  last_modified REAL NOT NULL
);
CREATE TABLE graph_edges (
  id INTEGER NOT NULL PRIMARY KEY,
  source TEXT NOT NULL,
  target TEXT NOT NULL,
  strength INTEGER NOT NULL,
  hash TEXT NOT NULL,
  created_time REAL NOT NULL,
  last_modified REAL NOT NULL
);
'''

_NOW = 1_750_000_000_000  # 固定的迁移时刻，毫秒


def _build_old_db(path) -> None:
    """造一个小样本旧库：覆盖正常行、字面重复、空内容、归一化后撞键、
    重复 concept 与端点缺失的边。"""
    db = sqlite3.connect(path)
    db.executescript(_OLD_DDL)
    # 旧向量是文本数组；这里只需要一个非空占位来证明「一个数值都不搬」。
    fake_vec = '[' + ', '.join(['0.01'] * 8) + ']'
    knowledges = [
        '光年之外的殖民船仍在航行',        # 正常 → 写入
        '光年之外的殖民船仍在航行',        # 字面重复 → 已存在
        '',                              # 空内容 → 跳过
        '   ',                           # 纯空白 → 跳过
        '！！！',                         # 归一化后为空 → 跳过
        '幻影坦克，是天启的克星！',        # 正常 → 写入
        '幻影坦克是天启的克星',            # 归一化后撞上一行 → 已存在
    ]
    for content in knowledges:
        db.execute('INSERT INTO knowledges (content, embedding) VALUES (?, ?)',
                   (content, fake_vec))
    nodes = [
        ('幻影坦克', 1744749455.0, 1751819164.0),
        ('天启坦克', 1744749456.0, 1751819165.0),
        ('光棱塔',   1744749457.0, 1751819166.0),
        ('幻影坦克', 1744749458.0, 1751819167.0),   # 重复 concept → 已存在
    ]
    for concept, created, modified in nodes:
        db.execute(
            'INSERT INTO graph_nodes (concept, memory_items, hash, created_time, last_modified)'
            ' VALUES (?, ?, ?, ?, ?)',
            (concept, '[]', 'h-' + concept, created, modified),
        )
    edges = [
        ('幻影坦克', '天启坦克', 18, 1751695692.0),
        ('天启坦克', '光棱塔',   5,  1751770095.0),
        ('光棱塔',   '幻影坦克', 261, 1751800000.0),
        ('幻影坦克', '不存在的概念', 7, 1751800001.0),   # 端点缺失 → 跳过
    ]
    for source, target, strength, modified in edges:
        db.execute(
            'INSERT INTO graph_edges (source, target, strength, hash, created_time, last_modified)'
            ' VALUES (?, ?, ?, ?, ?, ?)',
            (source, target, strength, 'e', modified, modified),
        )
    db.commit()
    db.close()


def _open_old(path) -> sqlite3.Connection:
    """与脚本同口径的只读连接：file:...?mode=ro。"""
    return sqlite3.connect(f'file:{path}?mode=ro', uri=True)


@pytest.fixture
def old_db_path(tmp_path):
    path = tmp_path / 'old.db'
    _build_old_db(path)
    return path


@pytest.fixture
def store(db) -> MemoryStore:
    """conftest 的 :memory: 库，迁移已跑过，直接当目标库用。"""
    return MemoryStore(db)


def test_migrate_knowledge_counts_and_embedding_null(old_db_path, store) -> None:
    """★M-4 + 块内要点：正文进库、content_key 同 facts 口径、embedding 全 NULL。"""
    old = _open_old(old_db_path)
    report = migrate_knowledge(old, store._db, now=_NOW, dry_run=False)

    assert report.read == 7
    assert report.inserted == 2
    assert report.existed == 2          # 字面重复 1 + 归一化撞键 1
    assert report.skipped == {'空内容': 3}

    rows = store._db.execute(
        'SELECT content, content_key, source, embedding, created_at FROM knowledge'
    ).fetchall()
    assert len(rows) == 2
    assert all(r['embedding'] is None for r in rows), '迁入行 embedding 必须为 NULL'
    assert {r['content'] for r in rows} == {'光年之外的殖民船仍在航行', '幻影坦克，是天启的克星！'}
    assert all(r['content_key'] == exact_key(r['content']) for r in rows)
    assert all(r['source'] for r in rows), 'source 必须标迁移'
    assert all(r['created_at'] == _NOW for r in rows)


def test_migrate_graph_resolves_concepts_and_skips_dangling(old_db_path, store) -> None:
    """图谱结构：concept 唯一键去重，边按 concept 名解析，端点缺失跳过并计数。"""
    old = _open_old(old_db_path)
    report = migrate_graph(old, store._db, now=_NOW, dry_run=False)

    assert report.read == 8             # 节点 4 + 边 4
    assert report.inserted == 3 + 3
    assert report.existed == 1          # 重复 concept
    assert report.skipped == {'端点缺失': 1}

    nodes = store._db.execute('SELECT concept, created_at FROM knowledge_nodes').fetchall()
    assert {r['concept'] for r in nodes} == {'幻影坦克', '天启坦克', '光棱塔'}
    by_concept = {r['concept']: r['created_at'] for r in nodes}
    assert by_concept['幻影坦克'] == int(1744749455.0 * 1000), '节点时间戳应从秒转毫秒'

    edges = store._db.execute(
        '''SELECT s.concept AS src, t.concept AS tgt, e.strength, e.updated_at
           FROM knowledge_edges e
           JOIN knowledge_nodes s ON s.id = e.source_id
           JOIN knowledge_nodes t ON t.id = e.target_id'''
    ).fetchall()
    assert {(r['src'], r['tgt']) for r in edges} == {
        ('幻影坦克', '天启坦克'), ('天启坦克', '光棱塔'), ('光棱塔', '幻影坦克'),
    }
    strength = {(r['src'], r['tgt']): r['strength'] for r in edges}
    assert strength[('光棱塔', '幻影坦克')] == 261.0, 'strength 原样保留'
    assert all(r['updated_at'] > 0 for r in edges)


def test_dry_run_report_matches_actual(old_db_path, store) -> None:
    """★M-0：dry-run 的计数与随后的实际写入完全一致，且 dry-run 不落任何行。"""
    old = _open_old(old_db_path)
    dry_k = migrate_knowledge(old, store._db, now=_NOW, dry_run=True)
    dry_g = migrate_graph(old, store._db, now=_NOW, dry_run=True)
    assert store._db.execute('SELECT COUNT(*) FROM knowledge').fetchone()[0] == 0
    assert store._db.execute('SELECT COUNT(*) FROM knowledge_nodes').fetchone()[0] == 0
    assert store._db.execute('SELECT COUNT(*) FROM knowledge_edges').fetchone()[0] == 0

    real_k = migrate_knowledge(old, store._db, now=_NOW, dry_run=False)
    real_g = migrate_graph(old, store._db, now=_NOW, dry_run=False)
    assert dry_k == real_k
    assert dry_g == real_g


def test_rerun_is_idempotent(old_db_path, store) -> None:
    """★M-2：重复执行不新增行，第二次全部计入「已存在」。"""
    old = _open_old(old_db_path)
    migrate_knowledge(old, store._db, now=_NOW, dry_run=False)
    migrate_graph(old, store._db, now=_NOW, dry_run=False)
    counts_before = _counts(store._db)

    second_k = migrate_knowledge(old, store._db, now=_NOW + 1000, dry_run=False)
    second_g = migrate_graph(old, store._db, now=_NOW + 1000, dry_run=False)

    assert _counts(store._db) == counts_before
    # 重跑时 4 条有效正文与 4 个节点全部撞「已存在」，边 3 条已存在、1 条端点缺失。
    assert second_k.inserted == 0 and second_k.existed == 4
    assert second_g.inserted == 0
    assert second_g.existed == 4 + 3
    assert second_g.skipped == {'端点缺失': 1}


def _counts(db: sqlite3.Connection) -> tuple[int, int, int]:
    return (
        db.execute('SELECT COUNT(*) FROM knowledge').fetchone()[0],
        db.execute('SELECT COUNT(*) FROM knowledge_nodes').fetchone()[0],
        db.execute('SELECT COUNT(*) FROM knowledge_edges').fetchone()[0],
    )


def test_old_db_is_not_modified(old_db_path, store) -> None:
    """只读旧库：迁移前后旧库三张表的行数不变。"""
    old = _open_old(old_db_path)
    before = [old.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
              for t in ('knowledges', 'graph_nodes', 'graph_edges')]
    migrate_knowledge(old, store._db, now=_NOW, dry_run=False)
    migrate_graph(old, store._db, now=_NOW, dry_run=False)
    after = [old.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
             for t in ('knowledges', 'graph_nodes', 'graph_edges')]
    assert before == after


def test_main_cli_writes_and_reports(old_db_path, tmp_path, capsys) -> None:
    """CLI 入口：旧库与目标库都走路径参数，报告打印到 stdout。"""
    target = tmp_path / 'target.db'
    rc = main([str(old_db_path), str(target)])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'knowledge' in out and 'knowledge_edges' in out
    db = sqlite3.connect(target)
    assert db.execute('SELECT COUNT(*) FROM knowledge').fetchone()[0] == 2
    db.close()

    rc = main([str(old_db_path), str(target), '--dry-run'])
    assert rc == 0
    out = capsys.readouterr().out
    assert '已存在' in out
