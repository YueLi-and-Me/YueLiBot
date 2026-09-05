"""导入中心的机检断言（★I-1 ~ ★I-7）。

覆盖：批批次写入、去重计数、按批次删除的隔离性、预览一致性、
并发拒绝、检索可达、迁移对无批次存量的保全。
"""

from __future__ import annotations

import sqlite3

import pytest

from src.core.db.migrations.bootstrap import write_user_version
from src.core.db.migrations.manager import CURRENT_VERSION, get_user_version, run_migrations
from src.core.memory import import_center
from src.core.memory.knowledge import add_knowledge, search_knowledge

NOW = 1_800_000_000_000


def _reset_lock():
    import_center._release_import_lock()


@pytest.mark.asyncio
async def test_star_i1_import_creates_batched_knowledge(db):
    """★I-1：导入 N 条产出 N 条 knowledge，全部指向同一批次。"""

    _reset_lock()
    text = '月璃是一个 QQ 聊天机器人项目。\n\n她的检索层用 BM25 与向量混合排序。'
    result = await import_center.run_import(db, text, '测试资料', NOW)

    assert result['submitted'] == 2 and result['added'] == 2
    rows = db.execute(
        'SELECT id, import_batch_id, source FROM knowledge WHERE import_batch_id = ?',
        (result['batch_id'],),
    ).fetchall()
    assert len(rows) == 2
    assert {row[1] for row in rows} == {result['batch_id']}
    assert {row[2] for row in rows} == {import_center.IMPORT_SOURCE}


@pytest.mark.asyncio
async def test_star_i2_dedup_reflected_in_added_count(db):
    """★I-2：重复导入去重生效，批次计数是实际新增数而非提交数。"""

    _reset_lock()
    first = await import_center.run_import(db, '他喜欢喝冰美式。\n\n他养了一只猫。', '批一', NOW)
    second = await import_center.run_import(
        db, '他喜欢喝冰美式。\n\n他在成都工作。', '批二', NOW + 1,
    )

    assert first['added'] == 2
    assert second['submitted'] == 2
    assert second['added'] == 1
    assert second['duplicated'] == 1
    # 重复命中的既有行不回填新批次：条目仍属于第一批。
    dup_row = db.execute(
        'SELECT import_batch_id FROM knowledge WHERE content LIKE ?', ('%冰美式%',)
    ).fetchone()
    assert dup_row[0] == first['batch_id']


@pytest.mark.asyncio
async def test_star_i3_delete_only_touches_own_batch(db):
    """★I-3：按批次删除只删该批次，其他批次与无批次存量一条不动。"""

    _reset_lock()
    batch_a = await import_center.run_import(db, '第一批第一条。\n\n第一批第二条。', '批A', NOW)
    batch_b = await import_center.run_import(db, '第二批唯一一条。', '批B', NOW + 1)
    legacy_id = add_knowledge(db, '无批次的存量知识。', 'legacy', NOW)

    deleted = import_center.delete_batch(db, batch_a['batch_id'])

    assert deleted['deleted'] == 2
    remaining = {
        row[0] for row in db.execute('SELECT content FROM knowledge').fetchall()
    }
    assert remaining == {'第二批唯一一条。', '无批次的存量知识。'}
    assert db.execute(
        'SELECT import_batch_id FROM knowledge WHERE id = ?', (legacy_id,)
    ).fetchone()[0] is None
    assert import_center.batch_count(db, batch_b['batch_id']) == 1


@pytest.mark.asyncio
async def test_star_i4_preview_matches_actual(db):
    """★I-4：删除预览的条数与实际删除数一致。"""

    _reset_lock()
    created = await import_center.run_import(db, '预览条一。\n\n预览条二。\n\n预览条三。', '批', NOW)

    preview = import_center.delete_preview(db, created['batch_id'])
    actual = import_center.delete_batch(db, created['batch_id'])

    assert preview['to_delete'] == actual['deleted'] == 3
    assert preview['items'] and '预览条一' in preview['items'][0]['content']


@pytest.mark.asyncio
async def test_star_i5_concurrent_import_rejected(db):
    """★I-5：并发导入被明确拒绝，不是静默排队。"""

    import_center._acquire_import_lock()
    try:
        with pytest.raises(import_center.ImportBusyError, match='已有导入进行中'):
            await import_center.run_import(db, '不该被排队的内容。', '并发', NOW)
    finally:
        _reset_lock()
    # 闸释放后可以再次导入。
    result = await import_center.run_import(db, '闸释放后的正常导入。', '重试', NOW)
    assert result['added'] == 1


@pytest.mark.asyncio
async def test_star_i6_imported_items_retrievable(db):
    """★I-6：导入的条目能被真实检索命中。"""

    _reset_lock()
    text = (
        '月璃的检索层用 BM25 与向量混合排序，事实按在场者召回。\n\n'
        '知识层的去重键是 content_key，与事实层同口径。'
    )
    await import_center.run_import(db, text, '检索验证', NOW)

    hits = search_knowledge(db, 'BM25 向量 混合排序', 5)
    assert hits
    assert any('BM25' in hit.content for hit in hits)


def test_star_i7_migration_preserves_unbatched_knowledge():
    """★I-7：v24 库迁移到 v25 后，无批次存量仍在且仍可检索。"""

    db = sqlite3.connect(':memory:')
    # messages 表是「是否全新库」的探针：缺了它会被当成空库走 DDL 初始化，
    # 而不是走 v24→v25 迁移。真实 v24 库必有该表。
    db.execute(
        'CREATE TABLE messages ('
        '  id INTEGER PRIMARY KEY, role TEXT NOT NULL, content TEXT NOT NULL,'
        '  created_at INTEGER NOT NULL, episode_id INTEGER, stream_id INTEGER NOT NULL,'
        '  sender_person_id INTEGER, external_message_id TEXT)'
    )
    db.execute(
        '''CREATE TABLE knowledge (
             id INTEGER PRIMARY KEY, content TEXT NOT NULL,
             content_key TEXT NOT NULL UNIQUE, source TEXT NOT NULL DEFAULT '',
             tokens_v2 TEXT NOT NULL DEFAULT '', embedding BLOB, embedding_q8 BLOB,
             created_at INTEGER NOT NULL, hit_count INTEGER NOT NULL DEFAULT 0,
             last_hit_at INTEGER
           )'''
    )
    db.execute(
        "INSERT INTO knowledge (content, content_key, source, created_at)"
        " VALUES ('存量知识正文。', 'legacy-key-1', 'legacy', 1)"
    )
    write_user_version(db, 24)

    run_migrations(db)

    assert get_user_version(db) == CURRENT_VERSION
    row = db.execute(
        'SELECT content, import_batch_id FROM knowledge WHERE content_key = ?',
        ('legacy-key-1',),
    ).fetchone()
    assert row is not None and row[0] == '存量知识正文。' and row[1] is None
    total = db.execute(
        'SELECT COUNT(*) FROM knowledge WHERE import_batch_id IS NULL'
    ).fetchone()[0]
    assert total == 1
    db.close()


class TestChunking:
    """分块：段落优先、超长段按句切、空段丢弃、无标点硬切。"""

    def test_paragraphs_become_chunks(self):
        text = '第一段。\n\n第二段。\n\n\n第三段。'
        assert import_center.split_into_chunks(text) == ['第一段。', '第二段。', '第三段。']

    def test_long_paragraph_split_by_sentence(self):
        sentence = '这是一个完整的句子。'
        paragraph = sentence * 150
        chunks = import_center.split_into_chunks(paragraph)
        assert len(chunks) > 1
        assert all(len(chunk) <= import_center.CHUNK_MAX_CHARS for chunk in chunks)
        assert ''.join(chunk.replace(' ', '') for chunk in chunks) == paragraph

    def test_unpunctuated_wall_is_hard_cut(self):
        wall = '字' * (import_center.CHUNK_MAX_CHARS * 2 + 10)
        chunks = import_center.split_into_chunks(wall)
        assert len(chunks) == 3
        assert all(len(chunk) <= import_center.CHUNK_MAX_CHARS for chunk in chunks)


@pytest.mark.asyncio
async def test_paste_limit_rejected(db):
    """粘贴超上限被拒绝，且不建批次。"""

    _reset_lock()
    with pytest.raises(ValueError, match='超过上限'):
        await import_center.run_import(db, 'x' * (import_center.MAX_PASTE_CHARS + 1), '超限', NOW)
    assert db.execute('SELECT COUNT(*) FROM import_batches').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_batch_item_limit_rejected(db):
    """分块超过单批条目上限被拒绝。"""

    _reset_lock()
    text = '\n\n'.join(f'独立条目 {i}。' for i in range(import_center.MAX_BATCH_ITEMS + 1))
    with pytest.raises(ValueError, match='单批上限'):
        await import_center.run_import(db, text, '超条数', NOW)


@pytest.mark.asyncio
async def test_import_events_emitted(db, monkeypatch):
    """导入开始与完成事件各一条，字段带批次与条数。"""

    _reset_lock()
    emitted = []
    monkeypatch.setattr(
        'src.core.memory.import_center.trace.emit',
        lambda event, **fields: emitted.append((event, fields)),
    )

    await import_center.run_import(db, '事件验证条目。', '事件', NOW)

    kinds = [event for event, _ in emitted]
    assert 'import_started' in kinds and 'import_done' in kinds
    done = next(fields for event, fields in emitted if event == 'import_done')
    assert done['added'] == 1 and done['submitted'] == 1
