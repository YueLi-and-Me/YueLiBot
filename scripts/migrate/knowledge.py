"""把旧库的知识正文与图谱结构迁进当前库。

三张子步骤，必须按顺序：正文入 ``knowledge``（``embedding`` 一律留空，重算另有脚本）、``graph_nodes`` 按 concept 去重入 ``knowledge_nodes``、``graph_edges`` 按
concept 名解析成新 id 后入 ``knowledge_edges``。旧节点上的 ``memory_items``
第一版不导——联想边会重新长出来，不依赖旧库那一列。

纪律（与迁移总规格一致）：

- **只读旧库**：以 ``file:...?mode=ro`` 打开，任何情况下不写旧库；
- **可 dry-run**：``--dry-run`` 只统计不写入，计数报告与实际写入完全一致——
  两种模式走同一条「先查存在性再决定」的计数路径，差别只在 INSERT 是否执行；
- **可断点续跑**：幂等靠内容键（``content_key`` / ``concept`` / 边端点对），
  不靠「跑到第几条」的游标，中断后重跑不产生重复行；
- **旧库路径是命令行参数**，不写死。

两点刻意的口径：

- 旧边的 ``strength`` 是 1~261 的整数（共现强度），**原样保留**进
  ``knowledge_edges.strength``；它只用于相关概念排序，相对序不变即可；
- 旧 ``knowledges`` 表没有时间戳列，迁入行的 ``created_at`` 取迁移时刻；
  节点与边沿用旧库自己的 ``created_time`` / ``last_modified``（秒转毫秒）。

用法示例：

    python scripts/migrate/knowledge.py 旧库.db data/memory.db
    python scripts/migrate/knowledge.py 旧库.db data/memory.db --dry-run
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from src.core.runtime.clock import now as current_time
from src.core.memory.similarity import exact_key
from src.core.memory.store import MemoryStore

# 迁移来源标记，写进 knowledge.source，便于后续按来源统计与排查。
MIGRATION_SOURCE = 'migrate-m4'

# 单事务提交粒度：旧库两万余行，一次一个大事务会把 WAL 撑得过长。
_COMMIT_EVERY = 1000

# 旧库必须具备的源表；缺失说明拿错了库，直接拒绝而不是迁出半份。
_REQUIRED_TABLES = ('knowledges', 'graph_nodes', 'graph_edges')


@dataclass
class BlockReport:
    """一块迁移的计数报告；dry-run 与实际写入必须逐字段相等。

    :ivar read: 从旧库读出的行数。
    :ivar inserted: 实际写入（或 dry-run 时预计写入）的行数。
    :ivar existed: 因内容键已存在而跳过的行数（含旧库内部重复）。
    :ivar skipped: 按原因分类的跳过计数（空内容 / 端点缺失）。
    """

    read: int = 0
    inserted: int = 0
    existed: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        """按原因累计一次跳过。"""
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def _open_readonly(path: Path) -> sqlite3.Connection:
    """以只读 URI 打开旧库；文件不存在或缺表时给出明确错误。

    :param path: 旧库文件路径。
    :return: 只读 SQLite 连接。
    :raises SystemExit: 文件不存在，或三张源表不全（大概率拿错了库）。
    """
    if not path.is_file():
        raise SystemExit(f'旧库不存在：{path}')
    db = sqlite3.connect(f'{path.absolute().as_uri()}?mode=ro', uri=True)
    tables = {
        row[0]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    missing = [t for t in _REQUIRED_TABLES if t not in tables]
    if missing:
        db.close()
        raise SystemExit(f'旧库缺少源表 {missing}，确认一下是不是拿错了库：{path}')
    return db


def migrate_knowledge(
    old: sqlite3.Connection,
    db: sqlite3.Connection,
    *,
    now: int,
    dry_run: bool,
) -> BlockReport:
    """把旧 ``knowledges`` 正文迁入 ``knowledge``，``embedding`` 一律留空。

    去重键 ``content_key`` 与 facts 同口径（``exact_key``）：旧库内部的字面重复
    与归一化后撞键都计入「已存在」，重复执行本函数不新增行。空内容（含归一化后
    为空的纯标点行）跳过并计数。旧库的 ``embedding`` 文本列一个数值都不读。

    :param old: 只读打开的旧库连接。
    :param db: 当前库连接（表结构由调用方保证已建好）。
    :param now: 迁移时刻的 Unix 毫秒时间戳，作为迁入行的 ``created_at``。
    :param dry_run: 为 ``True`` 时只统计不写入。
    :return: 计数报告。
    """
    report = BlockReport()
    seen: set[str] = set()
    pending = 0
    for row in old.execute('SELECT content FROM knowledges ORDER BY id'):
        report.read += 1
        content = (row[0] or '').strip()
        key = exact_key(content) if content else ''
        if not key:
            report.skip('空内容')
            continue
        if key in seen:
            report.existed += 1
            continue
        seen.add(key)
        exists = db.execute(
            'SELECT 1 FROM knowledge WHERE content_key = ?', (key,)
        ).fetchone()
        if exists:
            report.existed += 1
            continue
        report.inserted += 1
        if not dry_run:
            db.execute(
                '''INSERT INTO knowledge (content, content_key, source, created_at)
                   VALUES (?, ?, ?, ?)''',
                (content, key, MIGRATION_SOURCE, now),
            )
            pending += 1
            if pending % _COMMIT_EVERY == 0:
                db.commit()
    if not dry_run:
        db.commit()
    return report


def migrate_graph(
    old: sqlite3.Connection,
    db: sqlite3.Connection,
    *,
    now: int,
    dry_run: bool,
) -> BlockReport:
    """把旧图谱结构迁入 ``knowledge_nodes`` / ``knowledge_edges``。

    节点按 ``concept`` 唯一键去重；旧边的 ``source`` / ``target`` 是 concept 名，
    解析成新库节点 id 后写边，两端有一端解析不到就跳过并计数。``strength``
    原样保留。先迁节点再迁边，顺序不能反。

    :param old: 只读打开的旧库连接。
    :param db: 当前库连接。
    :param now: 迁移时刻；仅在旧行缺时间戳时兜底（本块用不到，保留对称签名）。
    :param dry_run: 为 ``True`` 时只统计不写入。
    :return: 覆盖节点与边两张表的合并计数报告。
    """
    del now  # 节点与边都有旧库时间戳，迁移时刻没有用途；保留参数位与知识块对称。
    report = BlockReport()
    seen_concepts: set[str] = set()
    pending = 0
    for row in old.execute('SELECT concept, created_time FROM graph_nodes ORDER BY id'):
        report.read += 1
        concept = (row[0] or '').strip()
        if not concept:
            report.skip('空concept')
            continue
        if concept in seen_concepts:
            report.existed += 1
            continue
        seen_concepts.add(concept)
        exists = db.execute(
            'SELECT 1 FROM knowledge_nodes WHERE concept = ?', (concept,)
        ).fetchone()
        if exists:
            report.existed += 1
            continue
        report.inserted += 1
        if not dry_run:
            db.execute(
                'INSERT INTO knowledge_nodes (concept, created_at) VALUES (?, ?)',
                (concept, int(row[1] * 1000)),
            )
            pending += 1
            if pending % _COMMIT_EVERY == 0:
                db.commit()
    if not dry_run:
        db.commit()

    # 边的端点按 concept 名解析成新库 id；映射表在节点迁入之后现查，
    # 因此重跑时旧边依然能解析（节点早已在库里），幂等由端点对唯一键保证。
    # 「能否解析」看 concept 名集合（已入库 ∪ 本次新迁），「是否已存在」按
    # concept 对去重后再落 id 查库——dry-run 时新节点还没有 id，这套口径让
    # 两种模式的计数完全一致。
    id_by_concept = {
        row[1]: row[0] for row in db.execute('SELECT id, concept FROM knowledge_nodes')
    }
    resolvable = set(id_by_concept) | seen_concepts
    seen_pairs: set[tuple[str, str]] = set()
    for row in old.execute(
        'SELECT source, target, strength, last_modified FROM graph_edges ORDER BY id'
    ):
        report.read += 1
        source_name = (row[0] or '').strip()
        target_name = (row[1] or '').strip()
        if source_name not in resolvable or target_name not in resolvable:
            report.skip('端点缺失')
            continue
        pair = (source_name, target_name)
        if pair in seen_pairs:
            report.existed += 1
            continue
        seen_pairs.add(pair)
        source_id = id_by_concept.get(source_name)
        target_id = id_by_concept.get(target_name)
        if source_id is not None and target_id is not None:
            exists = db.execute(
                'SELECT 1 FROM knowledge_edges WHERE source_id = ? AND target_id = ?',
                (source_id, target_id),
            ).fetchone()
            if exists:
                report.existed += 1
                continue
        report.inserted += 1
        if not dry_run:
            # 写入模式下可解析的 concept 必定已有新库 id（节点阶段刚迁入或本就在库）。
            assert source_id is not None and target_id is not None
            db.execute(
                '''INSERT INTO knowledge_edges (source_id, target_id, strength, updated_at)
                   VALUES (?, ?, ?, ?)''',
                (source_id, target_id, float(row[2]), int(row[3] * 1000)),
            )
            pending += 1
            if pending % _COMMIT_EVERY == 0:
                db.commit()
    if not dry_run:
        db.commit()
    return report


def _render(report: BlockReport, label: str) -> str:
    """把一块计数报告渲染成一行；dry-run 与实际写入共用同一格式。"""
    skipped = (
        '，跳过 ' + str(sum(report.skipped.values()))
        + '（' + '，'.join(f'{k} {v}' for k, v in sorted(report.skipped.items())) + '）'
        if report.skipped else ''
    )
    return (
        f'{label}: 读取 {report.read}，写入 {report.inserted}，'
        f'已存在 {report.existed}{skipped}'
    )


def main(argv: Sequence[str] | None = None) -> int:
    """迁移入口：旧库只读、目标库确保当前表结构，按正文→图谱顺序执行。

    :param argv: 命令行参数；``旧库 目标库 [--dry-run]``。
    :return: 进程退出码；正常完成为 0。
    """
    parser = argparse.ArgumentParser(
        description='旧库知识正文与图谱结构迁移（只读旧库，可 dry-run）',
    )
    parser.add_argument('old_db', type=Path, help='旧库文件路径（只读打开）')
    parser.add_argument('target_db', type=Path, help='当前库文件路径（不存在则创建）')
    parser.add_argument('--dry-run', action='store_true', help='只统计不写入')
    args = parser.parse_args(argv)

    old = _open_readonly(args.old_db)
    target = sqlite3.connect(args.target_db)
    target.row_factory = sqlite3.Row
    # 确保当前表结构存在；DDL 全部是 CREATE IF NOT EXISTS，对已有库无副作用。
    MemoryStore(target)

    now = current_time()
    knowledge_report = migrate_knowledge(old, target, now=now, dry_run=args.dry_run)
    graph_report = migrate_graph(old, target, now=now, dry_run=args.dry_run)
    old.close()
    target.close()

    mode = 'dry-run，未写入' if args.dry_run else '实际写入'
    print(f'知识与图谱迁移报告（{mode}）')
    print(_render(knowledge_report, 'knowledge'))
    print(_render(graph_report, 'knowledge_nodes + knowledge_edges'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
