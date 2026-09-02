"""为已迁移数据库的事实与知识原向量补算自解释 SQ8 列。

用法：

    python scripts/vector_quantize.py data/memory.db

脚本只处理 ``embedding IS NOT NULL AND embedding_q8 IS NULL`` 的行，可中断重跑；
不会创建数据库、推进迁移或改写原 ``embedding`` 列。
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import argparse
import asyncio
import sqlite3
import sys

from src.core.common.db.migrations.manager import CURRENT_VERSION, get_user_version
from src.core.memory.quantize import (
    backfill_quantized_embeddings,
    pending_quantization_counts,
)


def _require_q8_columns(db: sqlite3.Connection) -> None:
    """确认目标库已通过 v20 迁移，避免脚本掩盖版本错误。"""

    for table in ('facts', 'knowledge'):
        if table == 'facts':
            rows = db.execute("SELECT * FROM pragma_table_info('facts')").fetchall()
        else:
            rows = db.execute("SELECT * FROM pragma_table_info('knowledge')").fetchall()
        columns = {str(row[1]) for row in rows}
        if 'embedding_q8' not in columns:
            raise RuntimeError(f'目标库缺少 {table}.embedding_q8，请先完成 v20 迁移')


def main(argv: Sequence[str] | None = None) -> int:
    """打开现有数据库，补算两张表的 SQ8 列并给出执行后查库结果。"""

    parser = argparse.ArgumentParser(description='补算事实与知识的 SQ8 向量列')
    parser.add_argument('target_db', type=Path, help='已迁移到当前版本的数据库文件')
    parser.add_argument('--batch-size', type=int, default=256, help='每批事务行数')
    args = parser.parse_args(argv)
    if not args.target_db.is_file():
        parser.error(f'目标数据库不存在：{args.target_db}')

    db = sqlite3.connect(str(args.target_db))
    try:
        version = get_user_version(db)
        if version != CURRENT_VERSION:
            raise RuntimeError(
                f'目标库版本为 v{version}，当前代码要求 v{CURRENT_VERSION}；请先迁移'
            )
        _require_q8_columns(db)
        before = pending_quantization_counts(db)
        report = asyncio.run(
            backfill_quantized_embeddings(db, batch_size=args.batch_size)
        )
        after = pending_quantization_counts(db)
        quick_check = str(db.execute('PRAGMA quick_check').fetchone()[0])
    finally:
        db.close()

    print(
        'SQ8 补算完成：'
        f'事实 {report.facts} 行、知识 {report.knowledge} 行；'
        f'执行前待办 {before["facts"] + before["knowledge"]} 行，'
        f'执行后待办 {after["facts"] + after["knowledge"]} 行；'
        f'数据库检查 {quick_check}'
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
