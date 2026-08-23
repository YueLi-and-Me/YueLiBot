"""知识层（L3）的索引维护与向量重算读写。

knowledge 不是「她的记忆」而是「她知道的事」：没有衰减，不参与遗忘曲线。
本模块的写侧服务于两件事——

- 全文索引补齐：``knowledge_fts`` 是 ``content=''`` 的外部内容表，``rowid``
  必须与 ``knowledge.id`` 显式对齐（这个项目在 ``facts_fts`` 上踩过一次：
  对不齐会让检索结果指向错误的行，而且行数看起来完全正常）；
- 向量重算：待办集合就是 ``embedding IS NULL``，天然断点续跑，不需要游标表。

检索侧（``search_knowledge`` / ``related_concepts`` / ``touch_knowledge``）
在同模块的检索一节。
"""

from __future__ import annotations

import sqlite3

from .tokenize import index_tokens

from src.core.common.logger import get_logger

logger = get_logger(__name__)


def knowledge_without_fts(db: sqlite3.Connection, limit: int) -> list[tuple[int, str]]:
    """读取尚未进入 ``knowledge_fts`` 的知识行，供索引补齐。

    :param db: 当前库连接。
    :param limit: 最多返回的行数。
    :return: ``(id, content)`` 列表，按主键正序。
    :raises sqlite3.Error: 查询失败。
    副作用：只读 knowledge 与 knowledge_fts。
    """
    rows = db.execute(
        '''SELECT k.id, k.content FROM knowledge k
           WHERE NOT EXISTS (SELECT 1 FROM knowledge_fts WHERE knowledge_fts.rowid = k.id)
           ORDER BY k.id LIMIT ?''',
        (limit,),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def index_knowledge(db: sqlite3.Connection, limit: int = 1000) -> int:
    """为尚未索引的知识行补建 FTS 索引，``rowid`` 显式对齐主键。

    同时回填 ``knowledge.tokens_v2``，与 facts 的口径一致（索引文本与预分词
    列内容相同）。可反复调用：已索引的行不会被选中，天然幂等。

    :param db: 当前库连接。
    :param limit: 单次最多处理的行数；调用方循环调用直至返回 0。
    :return: 本次建入索引的行数。
    :raises sqlite3.Error: 查询、写入或提交失败。
    副作用：写入 knowledge_fts 与 knowledge.tokens_v2 并提交事务。
    """
    rows = knowledge_without_fts(db, limit)
    for kid, content in rows:
        tokens = index_tokens(content)
        # 外部内容表不会感知主表行的存在，rowid 必须显式给足，漏对齐会让
        # 检索结果指向错误的行。
        db.execute(
            'INSERT INTO knowledge_fts (rowid, tokens) VALUES (?, ?)', (kid, tokens)
        )
        db.execute('UPDATE knowledge SET tokens_v2 = ? WHERE id = ?', (tokens, kid))
    if rows:
        db.commit()
    return len(rows)


def knowledge_without_embedding(db: sqlite3.Connection, limit: int) -> list[tuple[int, str]]:
    """读取尚未计算向量的知识行，供离线重算。

    :param db: 当前库连接。
    :param limit: 最多返回的行数。
    :return: ``(id, content)`` 列表，按主键正序。
    :raises sqlite3.Error: 查询失败。
    副作用：只读 knowledge 表。
    """
    rows = db.execute(
        'SELECT id, content FROM knowledge WHERE embedding IS NULL ORDER BY id LIMIT ?',
        (limit,),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def store_knowledge_embedding(
    db: sqlite3.Connection, knowledge_id: int, embedding: bytes
) -> None:
    """把一条重算出的向量写回知识行。

    :param db: 当前库连接。
    :param knowledge_id: ``knowledge.id`` 稳定主键。
    :param embedding: 小端 float32 packed 字节串；维度由调用方保证与当前
        向量模型一致——不同模型的向量空间不可比，旧值一个数值都不许搬。
    :raises sqlite3.Error: 更新或提交失败。
    副作用：更新 ``knowledge.embedding`` 并提交事务；不存在的 id 不会新增记录。
    """
    db.execute('UPDATE knowledge SET embedding = ? WHERE id = ?', (embedding, knowledge_id))
    db.commit()
