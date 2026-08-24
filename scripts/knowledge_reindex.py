"""W4：把迁进来的知识变成可检索的——全文索引补齐 + 向量重算（可断点续跑）。

两个阶段，顺序执行：

1. **FTS 索引**（总是执行）：把 ``knowledge`` 里尚未进入 ``knowledge_fts`` 的行
   补进索引，rowid 与主键显式对齐。向量开关关着时，检索全靠这条 BM25 路径，
   所以这一阶段不受开关影响。
2. **向量重算**（只在 ``[vector].enabled = true`` 时执行）：旧库的 embedding 是
   2025-07 用当时模型算的文本数组，与当前向量模型不同源，一个数值都不许搬，
   全部用当前模型重算。待办集合就是 ``embedding IS NULL``，中断后重跑自动接上；
   走 ``memory/embed.py`` 的批量入口（单批 96，既有上限不改）；向量服务失败的
   批次留 ``NULL``、记事件，下次重跑再补——失败的批不阻断其他批。

用法示例：

    python scripts/knowledge_reindex.py data/memory.db
    python scripts/knowledge_reindex.py data/memory.db --config config
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from src.core.common.logger import get_logger
from src.core.memory.knowledge import (
    index_knowledge,
    knowledge_without_embedding,
    store_knowledge_embedding,
)
from src.core.memory.store import MemoryStore

logger = get_logger(__name__)

# 内存中一次攒多少个 96 条小批再落一轮盘；只影响提交粒度，不影响 embed.py 的
# 单批上限。
_CHUNK = 96 * 10


@dataclass
class ReindexReport:
    """一次重建的计数报告。

    :ivar indexed: 本次补进 FTS 索引的行数。
    :ivar embedded: 本次成功写入向量的行数。
    :ivar failed: 本次向量计算失败、仍留 ``NULL`` 待下次重跑的行数。
    """

    indexed: int = 0
    embedded: int = 0
    failed: int = 0


async def recompute_embeddings(
    db: sqlite3.Connection,
    client: Any,
) -> ReindexReport:
    """对待办集合（``embedding IS NULL``）做一次完整重算。

    失败批次由 ``embed.py`` 记为 ``embed_batch_failed`` 事件并留 ``NULL``，
    本函数继续推进其余批次；待办集合本身是断点，重跑自动接上。

    :param db: 当前库连接。
    :param client: 提供 ``embed(texts)`` 的向量客户端（``EmbeddingClient`` 口径：
        返回与输入等长的列表，失败项为 ``None``）。
    :return: 计数报告；``embedded`` 与 ``failed`` 分别计入成功与失败行数。
    :raises sqlite3.Error: 写库失败时原样上抛——那是本机故障，不是向量服务故障。
    """
    # 一次性快照全部待办：两万余条正文只占几 MB，换来「失败项在本次运行内
    # 不会被重复选中」的简单性。
    pending = knowledge_without_embedding(db, limit=10_000_000)
    report = ReindexReport()
    for start in range(0, len(pending), _CHUNK):
        part = pending[start:start + _CHUNK]
        vecs = await client.embed([content for _, content in part])
        for (kid, _), vec in zip(part, vecs):
            if vec is None:
                report.failed += 1
                continue
            store_knowledge_embedding(db, kid, vec)
            report.embedded += 1
        # 批次之间主动让出事件循环，与 VectorService.backfill 同口径。
        await asyncio.sleep(0)
    if report.failed:
        logger.warning('knowledge_embed_left_null', failed=report.failed)
    logger.info('knowledge_embed_done', embedded=report.embedded, failed=report.failed)
    return report


async def reindex(
    db: sqlite3.Connection,
    *,
    vector_enabled: bool,
    client: Any = None,
) -> ReindexReport:
    """执行一次完整重建：先补 FTS 索引，再按开关决定是否重算向量。

    :param db: 当前库连接。
    :param vector_enabled: ``[vector].enabled`` 的取值；为 ``False`` 时不发起
        任何向量请求，检索退回 BM25。
    :param client: 向量客户端；``vector_enabled=True`` 时必填。
    :return: 计数报告。
    :raises ValueError: 开关打开但客户端未装配。
    """
    report = ReindexReport()
    while True:
        done = index_knowledge(db, limit=_CHUNK)
        report.indexed += done
        if done == 0:
            break
    if not vector_enabled:
        return report
    if client is None:
        raise ValueError('vector.enabled=true 但 embedding 客户端未装配')
    sub = await recompute_embeddings(db, client)
    report.embedded = sub.embedded
    report.failed = sub.failed
    return report


def main(argv: Sequence[str] | None = None) -> int:
    """重建入口：打开目标库、确保表结构，按配置开关执行两个阶段。

    :param argv: 命令行参数；``目标库 [--config 配置目录]``。
    :return: 进程退出码；正常完成为 0。
    :raises SystemExit: 向量开关打开但 embedding 任务没有可用候选时直接报错。
    """
    parser = argparse.ArgumentParser(
        description='W4：知识全文索引补齐与向量重算（可断点续跑）',
    )
    parser.add_argument('target_db', type=Path, help='当前库文件路径（不存在则创建）')
    parser.add_argument(
        '--config', type=Path, default=Path('config'), help='配置目录，默认 ./config'
    )
    args = parser.parse_args(argv)

    db = sqlite3.connect(args.target_db)
    db.row_factory = sqlite3.Row
    # 确保当前表结构存在；DDL 全部是 CREATE IF NOT EXISTS，对已有库无副作用。
    MemoryStore(db)

    from src.core.config.loader import load_config
    from src.core.llm_models.router import create_routers

    cfg = load_config(args.config)
    client = None
    if cfg.vector.enabled:
        from src.core.memory.embed import build_client

        routers = create_routers(cfg)
        try:
            client = build_client(routers.embedding)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc

    report = asyncio.run(reindex(db, vector_enabled=cfg.vector.enabled, client=client))
    db.close()

    vector_line = (
        f'，向量重算 {report.embedded} 行、失败留 NULL {report.failed} 行'
        if cfg.vector.enabled else '，向量开关未开，跳过重算（检索走 BM25）'
    )
    print(f'知识索引重建完成：FTS 补建 {report.indexed} 行{vector_line}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
