"""将历史全文索引从手写 bigram 预分词迁移到 jieba 分词结果。

迁移为 ``facts`` 增加 ``tokens_v2``，重新处理事实文本，并重建 facts 与
episode_cues 的 contentless FTS 索引。由于索引不保存原文，重建时必须同时写入
原始 rowid 和新的分词字符串，才能保持查询结果与事实记录一一对应。
"""

from __future__ import annotations

import sqlite3

from src.core.common.logger import get_logger
from .registry import register

logger = get_logger(__name__)


# 延迟导入：jieba 首次 import 会加载词典（约 1s），
# 放在迁移函数里而非模块顶层，避免影响进程冷启动
def _tokenize(text: str) -> str:
    """使用 jieba 对记忆文本进行精确模式分词。

    :param text: 待分词的事实或召回线索文本。
    :return: 以单个空格连接的非空分词结果。
    副作用：首次调用可能加载 jieba 词典；不修改数据库。
    :performance: 首次调用包含词典加载成本，后续复杂度与文本长度相关。
    """
    import jieba
    tokens = list(jieba.cut(text, cut_all=False))
    return " ".join(t for t in tokens if t.strip())


@register(3)
def v3_to_v4(db: sqlite3.Connection) -> None:
    """将全文索引预分词列切换为 jieba 精确模式结果并重建 FTS 表。

    :param db: 当前迁移事务使用的 SQLite 连接。

    :raises sqlite3.Error: 列添加、索引重建或批量写入失败。
    :raises ImportError: 运行环境未安装 jieba。

    副作用：
        为 ``facts`` 添加 ``tokens_v2`` 列，更新事实和情节线索分词结果，删除并重建
        ``facts_fts`` 和 ``cues_fts``；不提交事务，由迁移管理器统一提交。

    性能：
        首次分词可能加载词典，整体耗时与事实和线索文本总长度线性相关。
    """

    # 1. facts 加新分词列（若不存在）
    cols = {row[1] for row in db.execute("PRAGMA table_info(facts)").fetchall()}
    if "tokens_v2" not in cols:
        db.execute("ALTER TABLE facts ADD COLUMN tokens_v2 TEXT NOT NULL DEFAULT ''")

    # 2. 重新分词所有 facts content
    rows = db.execute("SELECT id, content FROM facts").fetchall()
    logger.info("v3_to_v4_retokenize_facts", count=len(rows))
    for row in rows:
        tokens = _tokenize(row["content"])
        db.execute("UPDATE facts SET tokens_v2 = ? WHERE id = ?", (tokens, row["id"]))

    # 3. 重建 facts_fts（删旧表 → 新建 → 批量插入）
    db.execute("DROP TABLE IF EXISTS facts_fts")
    db.execute(
        "CREATE VIRTUAL TABLE facts_fts USING fts5(tokens, content='', tokenize='unicode61')"
    )
    db.executemany(
        "INSERT INTO facts_fts(rowid, tokens) VALUES (?, ?)",
        [(row["id"], _tokenize(row["content"])) for row in rows],
    )

    # 4. 重建 cues_fts
    cue_rows = db.execute("SELECT id, cue FROM episode_cues").fetchall()
    logger.info("v3_to_v4_retokenize_cues", count=len(cue_rows))
    db.execute("DROP TABLE IF EXISTS cues_fts")
    db.execute(
        "CREATE VIRTUAL TABLE cues_fts USING fts5(tokens, content='', tokenize='unicode61')"
    )
    db.executemany(
        "INSERT INTO cues_fts(rowid, tokens) VALUES (?, ?)",
        [(row["id"], _tokenize(row["cue"])) for row in cue_rows],
    )

    logger.info("v3_to_v4_done", facts=len(rows), cues=len(cue_rows))
