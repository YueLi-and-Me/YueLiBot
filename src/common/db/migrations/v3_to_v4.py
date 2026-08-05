"""
v3 → v4 迁移：预分词列从 Intl.Segmenter+bigram 换成 jieba。

原因：TS 侧手写 bigram 是因为无法分发编译型分词 .dll；
Python 侧 jieba 是纯 Python 包，这个约束不存在了。

步骤：
  1. facts 表加 tokens_v2 列（新预分词）
  2. 用 jieba 重新分词所有 content，填入 tokens_v2
  3. 删旧 facts_fts / cues_fts，重建，用 tokens_v2 驱动
  4. episode_cues 同理重建索引

注意：facts_fts 是 contentless 表（content=''），只存索引不存原文，
重建时必须重新插入 rowid + tokens 对。
"""

from __future__ import annotations

import sqlite3

from yueli.common.logger import get_logger
from .registry import register

logger = get_logger(__name__)


# 延迟导入：jieba 首次 import 会加载词典（约 1s），
# 放在迁移函数里而非模块顶层，避免影响进程冷启动
def _tokenize(text: str) -> str:
    import jieba
    tokens = list(jieba.cut(text, cut_all=False))
    return " ".join(t for t in tokens if t.strip())


@register(3)
def v3_to_v4(db: sqlite3.Connection) -> None:
    """将全文索引的预分词列从手写 bigram 切换到 jieba。"""

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
