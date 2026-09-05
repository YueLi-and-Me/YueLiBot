"""知识层（L3）的索引维护与向量重算读写。

knowledge 不是「Bot 的记忆」而是「Bot 知道的事」：没有衰减，不参与遗忘曲线。
本模块的写侧服务于两件事——

- 全文索引补齐：``knowledge_fts`` 是 ``content=''`` 的外部内容表，``rowid``
  必须与 ``knowledge.id`` 显式对齐（这个项目在 ``facts_fts`` 上踩过一次：
  对不齐会让检索结果指向错误的行，而且行数看起来完全正常）；
- 向量重算：待办集合就是 ``embedding IS NULL``，无需游标表即可断点续跑。

检索侧（``search_knowledge`` / ``related_concepts`` / ``touch_knowledge``）
在同模块的检索一节。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import sqlite3

from .decay import relevance_from_bm25, retention_weight, score
from .similarity import exact_key
from .tokenize import index_tokens, match_query


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


def add_knowledge(
    db: sqlite3.Connection,
    content: str,
    source: str,
    now: int,
    batch_id: int | None = None,
) -> int:
    """写入一条知识并同步建好全文索引，返回其主键。

    索引必须当场建，不能留给离线重算：``search_knowledge`` 是
    ``knowledge_fts JOIN knowledge`` 的形态，没有 FTS 行的知识不是排名靠后，
    而是整行检索不到，且该缺失不易察觉。

    向量由上层写入链路在本函数返回 ID 后生成；启动期补算与
    ``scripts/maintain/knowledge_reindex.py`` 负责历史缺口。缺向量时检索仍可使用 BM25。

    去重靠 ``content_key`` 唯一约束，与 ``facts.content_key`` 同口径
    （``similarity.exact_key``）。重复内容直接返回既有行 ID，不新增、不报错——
    同一件事被不同批次的对话反复提到是常态，不是异常。

    :param db: 当前库连接。
    :param content: 知识正文；首尾空白会去掉，规范化后为空时返回 0。
    :param source: 来源标识，例如 ``fact_extract``。
    :param now: 当前毫秒时间戳。
    :param batch_id: 导入来源批次 ID；仅新建行时落库，重复命中既有行不回填——
        已存在的知识不属于后来导入它的那个批次。
    :return: 新建或既有知识行的 ID；``content`` 为空时返回 0。
    :raises sqlite3.Error: 写入或提交失败。
    副作用：写入 knowledge 与 knowledge_fts 并提交事务。
    """
    text = (content or '').strip()
    if not text:
        return 0
    key = exact_key(text)
    row = db.execute('SELECT id FROM knowledge WHERE content_key = ?', (key,)).fetchone()
    if row is not None:
        return int(row[0])
    cursor = db.execute(
        '''INSERT INTO knowledge (content, content_key, source, created_at, import_batch_id)
           VALUES (?, ?, ?, ?, ?)''',
        (text, key, source, now, batch_id),
    )
    kid = int(cursor.lastrowid)
    tokens = index_tokens(text)
    # 外部内容表不感知主表行，rowid 必须显式对齐主键——这个项目在 facts_fts 上
    # 踩过一次：对不齐会让检索结果指向错误的行，而行数看起来完全正常。
    db.execute('INSERT INTO knowledge_fts (rowid, tokens) VALUES (?, ?)', (kid, tokens))
    db.execute('UPDATE knowledge SET tokens_v2 = ? WHERE id = ?', (tokens, kid))
    db.commit()
    return kid


def knowledge_without_embedding(db: sqlite3.Connection, limit: int) -> list[tuple[int, str]]:
    """读取尚未计算向量的知识行，供启动期或离线重算。

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


# ---------------------------------------------------------------- 检索


@dataclass
class KnowledgeHit:
    """一条知识检索命中。

    :ivar id: ``knowledge`` 表主键。
    :ivar content: 知识正文。
    :ivar score: 排序分数；词面与向量融合后的相关度。知识没有衰减（它不是
        「Bot 的记忆」而是「Bot 知道的事」），``retention`` 项恒取 1。
    """

    id: int
    content: str
    score: float


def search_knowledge(
    db: sqlite3.Connection,
    query: str,
    limit: int,
    *,
    query_embedding: bytes | None = None,
) -> list[KnowledgeHit]:
    """按 BM25 + 向量联合打分检索知识。

    打分口径与 ``MemoryStore.recall_facts`` 完全一致：FTS 按 BM25 取候选，
    查询向量与行向量都在时按 0.4/0.6 融合语义相关度——复用
    ``decay.relevance_from_bm25()`` 与 ``decay.score()``，不另造公式。唯一的
    差别是知识不衰减，``retention`` 恒取 1（此时 ``score(bm25, 1.0)`` 就是
    词面相关度本身）。任一向量缺失或坏掉时退回 BM25，不抛异常。

    :param db: 当前库连接。
    :param query: 待检索的自然语言查询。
    :param limit: 最多返回的条数。
    :param query_embedding: 查询文本的小端 float32 packed 向量；``None`` 时
        仅使用 BM25。
    :return: 按相关度降序的命中列表；查询无有效词时返回空列表。
    :raises sqlite3.Error: FTS 查询失败。
    副作用：只读 knowledge_fts 与 knowledge；不写任何列、不提交事务。
        命中计数由调用方在确认采用后经 :func:`touch_knowledge` 单独记录。
    """
    match = match_query(query)
    if not match or limit < 1:
        return []
    rows = db.execute(
        '''SELECT k.id, k.content, bm25(knowledge_fts) AS bm, k.embedding
           FROM knowledge_fts JOIN knowledge k ON k.id = knowledge_fts.rowid
           WHERE knowledge_fts MATCH ?
           ORDER BY bm ASC LIMIT ?''',
        (match, limit * 3),
    ).fetchall()
    scored: list[KnowledgeHit] = []
    for r in rows:
        final = score(r[2], 1.0)
        embedding = r[3]
        if query_embedding is not None and embedding is not None:
            try:
                from .embed import cosine
                # 查询向量按 float32 打包，每个分量占 4 字节；维度必须与解包结果一致。
                dim = len(query_embedding) // 4
                cos = cosine(query_embedding, embedding, dim)
                # 余弦 [-1, 1] 映射到 [0, 1]，与 BM25 同一分数域后 0.4/0.6 融合。
                relevance = 0.4 * relevance_from_bm25(r[2]) + 0.6 * ((cos + 1) / 2)
                final = relevance * retention_weight(1.0)
            except Exception:
                # 向量计算失败时保留 BM25 相关度，单条坏向量不阻断整批召回。
                pass
        scored.append(KnowledgeHit(id=r[0], content=r[1], score=final))
    scored.sort(key=lambda hit: hit.score, reverse=True)
    return scored[:limit]


def related_concepts(db: sqlite3.Connection, concept: str, limit: int) -> list[str]:
    """沿 ``knowledge_edges`` 走一跳，返回与概念直接相连的相邻概念。

    边在库里是有向存储，但「相关」的语义不看方向——A→B 与 B→A 都让两者相邻，
    因此两个方向都走，同一邻居按最强的一条边计。多跳扩散由联想层负责，这里不做。

    :param db: 当前库连接。
    :param concept: 起点概念名；不在 ``knowledge_nodes`` 里时返回空列表。
    :param limit: 最多返回的概念数。
    :return: 按边 ``strength`` 降序的概念名列表。
    :raises sqlite3.Error: 查询失败。
    副作用：只读 knowledge_nodes 与 knowledge_edges。
    """
    name = concept.strip()
    if not name or limit < 1:
        return []
    row = db.execute(
        'SELECT id FROM knowledge_nodes WHERE concept = ?', (name,)
    ).fetchone()
    if row is None:
        return []
    node_id = row[0]
    rows = db.execute(
        '''SELECT concept, MAX(strength) AS s FROM (
               SELECT n.concept AS concept, e.strength AS strength
               FROM knowledge_edges e JOIN knowledge_nodes n ON n.id = e.target_id
               WHERE e.source_id = ? AND e.target_id != ?
               UNION ALL
               SELECT n.concept AS concept, e.strength AS strength
               FROM knowledge_edges e JOIN knowledge_nodes n ON n.id = e.source_id
               WHERE e.target_id = ? AND e.source_id != ?
           ) GROUP BY concept ORDER BY s DESC, concept LIMIT ?''',
        (node_id, node_id, node_id, node_id, limit),
    ).fetchall()
    return [r[0] for r in rows]


def touch_knowledge(db: sqlite3.Connection, ids: Sequence[int], now: int) -> None:
    """记录一批知识被命中：``hit_count`` 加一、``last_hit_at`` 更新。

    只落数据供后续检索调优，不参与本轮打分：知识没有衰减曲线可改写，
    计数仅用于观测。与 facts 的命中回补不同源：那边的写回驱动遗忘曲线，
    这边只是账本。

    :param db: 当前库连接。
    :param ids: 被命中的 ``knowledge.id`` 序列；同一次调用里的重复 id 只计一次。
    :param now: 命中时刻的 Unix 毫秒时间戳。
    :raises sqlite3.Error: 更新或提交失败。
    副作用：更新 knowledge 表并提交事务；不存在的 id 被静默忽略。
    """
    unique = list(dict.fromkeys(ids))
    if not unique:
        return
    db.executemany(
        'UPDATE knowledge SET hit_count = hit_count + 1, last_hit_at = ? WHERE id = ?',
        [(now, kid) for kid in unique],
    )
    db.commit()
