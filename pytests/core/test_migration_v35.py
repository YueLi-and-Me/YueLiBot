"""存量知识升级重切迁移（v34 -> v35）回归。

钉六件事：导入批次按现行切块规则重切且仍归原批次、一次性迁移的切片拼回后重切、
全文索引随之替换、其他来源的知识不动、重复条目只留先写入的那条、缺表的最小库直接
跳过；另有停在 v34 的库走完迁移链的端到端用例。
"""

from __future__ import annotations

import sqlite3

from src.core.db.migrations.bootstrap import write_user_version
from src.core.db.migrations.manager import (
    CURRENT_VERSION,
    get_user_version,
    run_migrations,
)
from src.core.db.migrations.v34_to_v35 import FROM_VERSION, MIGRATED_SOURCE, migrate
from src.core.db.schema import DDL, SEED
from src.core.memory.chunking import split_into_chunks
from src.core.memory.knowledge import add_knowledge, index_knowledge, search_knowledge
from src.core.memory.similarity import exact_key

NOW = 1_800_000_000_000
HEADING = '进补最好先排毒'
BODY = '冬季进补之前先排毒，血流滞缓时多种毒素容易留存体内，直接进补反而加重负担，所以要先调理。'
EXTRACTED = '凌白喜欢喝冰美式'


def _v34_shaped_db() -> sqlite3.Connection:
    """构造 v34 形态的库：结构随当前 DDL，版本号停在 v34，导入行按旧规则一段一条。"""

    db = sqlite3.connect(':memory:')
    db.executescript(DDL)
    db.executescript(SEED)
    write_user_version(db, FROM_VERSION)
    return db


def _batch(db: sqlite3.Connection, paragraphs: list[str]) -> int:
    """登记一个导入批次并按旧规则一段一条写入，返回批次 ID。"""

    batch_id = int(db.execute(
        '''INSERT INTO import_batches (kind, origin_name, submitted, added, status, created_at)
           VALUES ('paste', '粘贴', ?, ?, 'done', ?)''',
        (len(paragraphs), len(paragraphs), NOW),
    ).lastrowid)
    for paragraph in paragraphs:
        add_knowledge(db, paragraph, 'import_center', NOW, batch_id=batch_id)
    return batch_id


def _batch_rows(db: sqlite3.Connection, batch_id: int) -> list[str]:
    return [row[0] for row in db.execute(
        'SELECT content FROM knowledge WHERE import_batch_id = ? ORDER BY id', (batch_id,)
    )]


def test_v35_rechunks_import_batches_with_current_rules() -> None:
    db = _v34_shaped_db()
    batch_id = _batch(db, [HEADING, BODY])
    assert _batch_rows(db, batch_id) == [HEADING, BODY]

    migrate(db)

    assert _batch_rows(db, batch_id) == split_into_chunks(f'{HEADING}\n\n{BODY}') == [f'{HEADING}\n{BODY}']
    assert db.execute(
        'SELECT submitted, added FROM import_batches WHERE id = ?', (batch_id,)
    ).fetchone() == (1, 1)
    assert db.execute(
        'SELECT embedding IS NULL, source, created_at FROM knowledge WHERE import_batch_id = ?', (batch_id,)
    ).fetchone() == (1, 'import_center', NOW)
    db.close()


def test_v35_replaces_fulltext_index() -> None:
    """旧行的全文索引同步删除：标题词只命中新的一条，不再命中只有标题的旧行。"""

    db = _v34_shaped_db()
    _batch(db, [HEADING, BODY])

    migrate(db)

    assert [hit.content for hit in search_knowledge(db, '排毒', 5)] == [f'{HEADING}\n{BODY}']
    db.close()


def test_v35_rejoins_migrated_slices() -> None:
    """一次性迁移的切片以换行拼回：段首标记分段，行宽处断开的句子拼回一句，先于导入批次写入。"""

    db = _v34_shaped_db()
    for content in (f'<☆>{HEADING}', f'<☆>{BODY[:30]}', BODY[30:]):
        db.execute(
            '''INSERT INTO knowledge (content, content_key, source, created_at)
               VALUES (?, ?, ?, ?)''',
            (content, exact_key(content), MIGRATED_SOURCE, NOW),
        )
    # 迁移脚本写入后由离线索引补建全文索引；未建索引的行不在真实库里出现。
    index_knowledge(db)
    batch_id = _batch(db, [f'{HEADING}\n{BODY}'])
    assert _batch_rows(db, batch_id) == [f'{HEADING}\n{BODY}']

    migrate(db)

    assert db.execute(
        'SELECT content, import_batch_id FROM knowledge WHERE source = ?', (MIGRATED_SOURCE,)
    ).fetchall() == [(f'{HEADING}\n{BODY}', None)]
    assert _batch_rows(db, batch_id) == [], '与迁移条目重复的导入条目按去重键跳过'
    db.close()


def test_v35_leaves_other_sources_untouched() -> None:
    db = _v34_shaped_db()
    extracted_id = add_knowledge(db, EXTRACTED, 'fact_extract', NOW)
    db.execute('UPDATE knowledge SET embedding = ? WHERE id = ?', (b'\x00' * 8, extracted_id))
    _batch(db, [HEADING, BODY])

    migrate(db)

    assert db.execute(
        'SELECT content, embedding FROM knowledge WHERE id = ?', (extracted_id,)
    ).fetchone() == (EXTRACTED, b'\x00' * 8)
    db.close()


def test_v35_keeps_duplicate_in_earlier_batch() -> None:
    db = _v34_shaped_db()
    first = _batch(db, [HEADING, BODY])
    # 旧规则下两批的条目各不相同（第二批粘贴时标题与正文之间没有空行），
    # 按现行规则重切后两批得到同一条。
    second = _batch(db, [f'{HEADING}\n{BODY}'])
    assert _batch_rows(db, second) == [f'{HEADING}\n{BODY}']

    migrate(db)

    assert _batch_rows(db, first) == [f'{HEADING}\n{BODY}']
    assert _batch_rows(db, second) == []
    assert db.execute(
        'SELECT submitted, added FROM import_batches WHERE id = ?', (second,)
    ).fetchone() == (1, 0)
    db.close()


def test_v35_skips_database_without_tables() -> None:
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE probe (id INTEGER PRIMARY KEY)')

    migrate(db)

    assert db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name IN ('knowledge', 'import_batches')"
    ).fetchone()[0] == 0
    db.close()


def test_chain_from_v34_reaches_current_version() -> None:
    db = _v34_shaped_db()
    batch_id = _batch(db, [HEADING, BODY])

    run_migrations(db)

    assert get_user_version(db) == CURRENT_VERSION
    assert _batch_rows(db, batch_id) == [f'{HEADING}\n{BODY}']
    db.close()
