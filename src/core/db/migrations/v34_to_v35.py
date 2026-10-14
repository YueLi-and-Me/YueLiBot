"""v34 -> v35：按现行切块规则重切成篇导入的存量知识。

切块规则改写后（``memory/chunking.py``），此后的导入按新规则切；本迁移把升级前已经
写入的成篇资料按同一规则重切一次，使新旧知识口径一致。成篇资料有两种来源，导入时
都没有保留原文，但条目按主键升序即原文顺序，可以拼回后重切：

- 导入中心的批次：旧规则一段一条，同一批次的条目以空行拼接即还原出分段文本。
  短段原先各自成条，小标题与正文分在两条，PDF 页码与页眉单独成条。
- 一次性迁移（``scripts/migrate/knowledge.py``，``source = 'migrate-m4'``）：旧库的
  切片，边界多落在 PDF 行宽处，一句话常被切进两条。条目以换行拼接，交给切块器的
  硬换行规则拼回同一句；``<☆>`` 是旧库 PDF 转换留下的段首标记，转为显式段落分隔符。
  全部迁移条目视为一篇按序处理，不区分来源文档。

范围之外的知识一条不碰：运行期抽取与版本消息写入的知识每条是一件事，没有可拼回的
原文。

- 先删光范围内的旧行再按序写入：重切后的条目与已写入的条目重复时按去重键跳过，
  与 ``add_knowledge`` 一致。迁移条目先于导入批次写入。
- 导入批次的新条目归回原批次，按批次撤销照常有效；批次的 ``submitted`` 与 ``added``
  改为重切后的条数。升级前被逐条删过的批次，重切后不再能从计数上看出这一点。
- 新条目的向量为空，由启动后的知识向量补算写入，补算完成前这部分只走 BM25；命中
  计数不保留。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import sqlite3

from .registry import register

from src.core.logging.logger import get_logger
from src.core.memory.chunking import PARAGRAPH_BREAK, split_into_chunks
from src.core.memory.similarity import exact_key
from src.core.memory.tokenize import index_tokens

logger = get_logger(__name__)

FROM_VERSION = 34
# 一次性迁移写入的来源标识，与 scripts/migrate/knowledge.py 的 MIGRATION_SOURCE 一致。
MIGRATED_SOURCE = 'migrate-m4'
# 旧库 PDF 转换留下的段首标记。
_PARAGRAPH_MARK = '<☆>'


@dataclass
class _Document:
    """一篇待重切的资料。

    :ivar batch_id: 导入批次 ID；一次性迁移的资料为 ``None``。
    :ivar source: 新条目沿用的来源标识。
    :ivar created_at: 新条目沿用的写入时间（毫秒），取首条旧行的值。
    :ivar text: 拼回的原文。
    :ivar rows: 旧行数。
    """

    batch_id: Optional[int]
    source: str
    created_at: int
    text: str
    rows: int


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    """判断历史库是否已经包含指定表；部分早期最小库要到链尾 DDL 才建表。"""

    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ?", (table,),
    ).fetchone()
    return row is not None


def _read_documents(db: sqlite3.Connection) -> Tuple[List[Tuple[int, str]], List[_Document]]:
    """读出范围内的全部旧行，拼成待重切的资料。

    :return: ``(旧行的 (id, tokens_v2) 列表, 资料列表)``；资料按写入顺序排列，
        迁移资料在前，导入批次按批次 ID 升序在后。
    """

    old: List[Tuple[int, str]] = []
    documents: List[_Document] = []
    migrated = db.execute(
        '''SELECT id, content, tokens_v2, created_at FROM knowledge
           WHERE source = ? AND import_batch_id IS NULL ORDER BY id''',
        (MIGRATED_SOURCE,),
    ).fetchall()
    if migrated:
        old.extend((int(row[0]), str(row[2])) for row in migrated)
        marker = f'\n{PARAGRAPH_BREAK}\n'
        text = '\n'.join(str(row[1]).replace(_PARAGRAPH_MARK, marker) for row in migrated)
        documents.append(_Document(None, MIGRATED_SOURCE, int(migrated[0][3]), text, len(migrated)))

    batches: Dict[int, List[Tuple[int, str, str, str, int]]] = {}
    for row in db.execute(
        '''SELECT import_batch_id, id, content, tokens_v2, source, created_at FROM knowledge
           WHERE import_batch_id IS NOT NULL ORDER BY import_batch_id, id'''
    ):
        batches.setdefault(int(row[0]), []).append(
            (int(row[1]), str(row[2]), str(row[3]), str(row[4]), int(row[5]))
        )
    for batch_id, rows in batches.items():
        old.extend((kid, tokens) for kid, _, tokens, _, _ in rows)
        text = '\n\n'.join(content for _, content, _, _, _ in rows)
        documents.append(_Document(batch_id, rows[0][3], rows[0][4], text, len(rows)))
    return old, documents


def _write_chunks(db: sqlite3.Connection, document: _Document, chunks: List[str]) -> int:
    """写入一篇资料的新条目并建全文索引，返回实际新增条数（重复的跳过）。"""

    added = 0
    for chunk in chunks:
        key = exact_key(chunk)
        if db.execute('SELECT 1 FROM knowledge WHERE content_key = ?', (key,)).fetchone():
            continue
        tokens = index_tokens(chunk)
        kid = db.execute(
            '''INSERT INTO knowledge
                   (content, content_key, source, tokens_v2, created_at, import_batch_id)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (chunk, key, document.source, tokens, document.created_at, document.batch_id),
        ).lastrowid
        # 外部内容表不感知主表行，rowid 必须显式对齐主键，否则检索结果指向错误的行。
        db.execute('INSERT INTO knowledge_fts (rowid, tokens) VALUES (?, ?)', (kid, tokens))
        added += 1
    return added


@register(FROM_VERSION)
def migrate(db: sqlite3.Connection) -> None:
    """重切一次性迁移与导入批次的知识；缺少知识表或导入批次表的库跳过。

    :raises sqlite3.Error: 读写失败；由迁移管理器整体回滚。
    副作用：删改 knowledge、knowledge_fts 与 import_batches。
    """

    if not all(_table_exists(db, t) for t in ('knowledge', 'knowledge_fts', 'import_batches')):
        logger.info('v34_to_v35_skipped', reason='缺少知识表或导入批次表')
        return
    old, documents = _read_documents(db)
    # content='' 的 FTS5 外部内容表不能直接 DELETE，必须用 'delete' 命令喂回写入时的 token。
    for kid, tokens in old:
        db.execute(
            "INSERT INTO knowledge_fts(knowledge_fts, rowid, tokens) VALUES('delete', ?, ?)",
            (kid, tokens),
        )
    db.executemany('DELETE FROM knowledge WHERE id = ?', [(kid,) for kid, _ in old])

    counts = {'migrated_before': 0, 'migrated_after': 0, 'imported_before': 0, 'imported_after': 0}
    for document in documents:
        chunks = split_into_chunks(document.text)
        added = _write_chunks(db, document, chunks)
        if document.batch_id is None:
            counts['migrated_before'] += document.rows
            counts['migrated_after'] += added
            continue
        db.execute(
            'UPDATE import_batches SET submitted = ?, added = ? WHERE id = ?',
            (len(chunks), added, document.batch_id),
        )
        counts['imported_before'] += document.rows
        counts['imported_after'] += added
    batches = sum(1 for document in documents if document.batch_id is not None)
    logger.info('v34_to_v35_done', batches=batches, **counts)
