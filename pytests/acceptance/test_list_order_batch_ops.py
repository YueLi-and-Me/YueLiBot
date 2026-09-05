"""列表排序与批量复核的验收用例。

覆盖三条线：
- 黑话 / 表达方式 / 表情包三个列表的四种排序口径与 time_desc 默认值；
- 黑话的逐条与批量复核（含推断阶梯锁定）、逐条与批量删除；
- 表达方式的批量复核；表情包的批量封禁 / 解封 / 删除。
"""

from __future__ import annotations

from pathlib import Path

import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

import pytest

from src.core.agent.jargon_mine import COMPLETE_SIGHTINGS
from src.core.api.auth import token_manager
from src.core.api.http import router
from src.core.api.state import app_state


def _client() -> TestClient:
    """构造带 Bearer 头的测试客户端。"""
    token_manager.configure('order-token')
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, client=('127.0.0.1', 50_000))
    client.headers['Authorization'] = 'Bearer order-token'
    return client


def _seed_jargon(db: sqlite3.Connection) -> None:
    """写三条黑话：入库时间递增、hits 递减，两个排序维度刚好相反。"""
    rows = [
        ('老词', '含义一', 'confirmed', 30, 1_000),
        ('中词', '含义二', 'confirmed', 20, 2_000),
        ('新词', '含义三', 'confirmed', 10, 3_000),
    ]
    db.executemany(
        'INSERT INTO jargon (term, meaning, stream_id, status, hits, source,'
        ' created_at, sightings, inferred_at_sightings)'
        " VALUES (?, ?, NULL, ?, ?, '测试', ?, 30, 25)",
        rows,
    )
    db.commit()


def _seed_expressions(db: sqlite3.Connection) -> None:
    """写三条表达方式：入库时间递增、use_count 递减。"""
    rows = [
        ('情境一', '说法一', 30, 1_000),
        ('情境二', '说法二', 20, 2_000),
        ('情境三', '说法三', 10, 3_000),
    ]
    db.executemany(
        'INSERT INTO expressions (situation, style, stream_id, use_count, source,'
        " created_at, checked, last_used_at) VALUES (?, ?, NULL, ?, '测试', ?, 0, NULL)",
        rows,
    )
    db.commit()


def test_jargon_order_defaults_to_newest_first(db: sqlite3.Connection) -> None:
    _seed_jargon(db)
    client = _client()

    default = client.get('/api/jargon').json()
    assert [row['term'] for row in default['entries']] == ['新词', '中词', '老词']

    oldest = client.get('/api/jargon', params={'order': 'time_asc'}).json()
    assert [row['term'] for row in oldest['entries']] == ['老词', '中词', '新词']

    most_used = client.get('/api/jargon', params={'order': 'use_desc'}).json()
    assert [row['term'] for row in most_used['entries']] == ['老词', '中词', '新词']

    least_used = client.get('/api/jargon', params={'order': 'use_asc'}).json()
    assert [row['term'] for row in least_used['entries']] == ['新词', '中词', '老词']

    assert client.get('/api/jargon', params={'order': 'bogus'}).status_code == 422


def test_jargon_reject_locks_inference_and_leaves_confirmed_list(
    db: sqlite3.Connection,
) -> None:
    _seed_jargon(db)
    client = _client()
    target = client.get('/api/jargon').json()['entries'][0]

    written = client.put(
        f'/api/jargon/{target["id"]}/status', json={'status': 'rejected'},
    )
    assert written.status_code == 200

    confirmed = client.get('/api/jargon').json()
    assert target['term'] not in [row['term'] for row in confirmed['entries']]

    rejected = client.get('/api/jargon', params={'status': 'rejected'}).json()
    assert [row['term'] for row in rejected['entries']] == [target['term']]
    # 锁定推断阶梯，否则证据继续增长时推断会把人工判定覆盖回 confirmed。
    assert rejected['entries'][0]['inferredAtSightings'] == COMPLETE_SIGHTINGS

    # 撤销驳回回到待定并解锁，条目重新交给自动判定。
    client.put(f'/api/jargon/{target["id"]}/status', json={'status': 'pending'})
    pending = client.get('/api/jargon', params={'status': 'pending'}).json()
    assert pending['entries'][0]['inferredAtSightings'] == 0


def test_jargon_batch_status_and_delete(db: sqlite3.Connection) -> None:
    _seed_jargon(db)
    client = _client()
    ids = [row['id'] for row in client.get('/api/jargon').json()['entries']]

    batch = client.post(
        '/api/jargon/batch-status', json={'ids': ids, 'status': 'rejected'},
    ).json()
    assert batch == {'updated': 3, 'requested': 3, 'status': 'rejected'}
    assert client.get('/api/jargon').json()['total'] == 0

    # 不存在的 ID 静默跳过，不报 404。
    deleted = client.post(
        '/api/jargon/batch-delete', json={'ids': [*ids, 9_999]},
    ).json()
    assert deleted == {'deleted': 3, 'requested': 4}
    assert client.get('/api/jargon', params={'status': 'rejected'}).json()['total'] == 0
    assert client.delete('/api/jargon/9999').status_code == 404


def test_expression_order_and_batch_checked(db: sqlite3.Connection) -> None:
    _seed_expressions(db)
    client = _client()

    default = client.get('/api/expressions').json()
    assert [row['style'] for row in default['entries']] == ['说法三', '说法二', '说法一']

    most_used = client.get('/api/expressions', params={'order': 'use_desc'}).json()
    assert [row['style'] for row in most_used['entries']] == ['说法一', '说法二', '说法三']

    ids = [row['id'] for row in default['entries']]
    batch = client.post(
        '/api/expressions/batch-checked', json={'ids': ids, 'checked': -1},
    ).json()
    assert batch == {'updated': 3, 'requested': 3, 'checked': -1}
    rejected = client.get('/api/expressions', params={'checked': -1}).json()
    assert rejected['total'] == 3


def test_emoji_order_and_batch_actions(
    db: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core.config.schema import EmojiConfig
    from src.core.services.media.emoji import EmojiLibrary

    directory = tmp_path / 'emoji'
    directory.mkdir()
    library = EmojiLibrary(db, directory, config=EmojiConfig())
    digests = [f'{index:064x}' for index in (1, 2, 3)]
    for index, digest in enumerate(digests):
        target = directory / f'{digest}.png'
        target.write_bytes(b'x')
        db.execute(
            'INSERT INTO emoji (hash, send_ref, emotion_tags, sub_type, seen_count,'
            ' use_count, last_used_at, first_seen_at) VALUES (?, ?, ?, 1, 1, ?, NULL, ?)',
            (digest, target.resolve().as_uri(), '开心', 30 - index * 10, 1_000 + index),
        )
    db.commit()

    monkeypatch.setattr(app_state, 'emoji_library', library)
    client = _client()

    default = client.get('/api/emojis').json()
    assert [row['hash'] for row in default['entries']] == list(reversed(digests))

    oldest = client.get('/api/emojis', params={'order': 'time_asc'}).json()
    assert [row['hash'] for row in oldest['entries']] == digests

    # use_asc 是淘汰口径：用得最少的排最前，未用过的（last_used_at 为 NULL）打头。
    eviction = client.get('/api/emojis', params={'order': 'use_asc'}).json()
    assert [row['hash'] for row in eviction['entries']] == list(reversed(digests))

    most_used = client.get('/api/emojis', params={'order': 'use_desc'}).json()
    assert [row['hash'] for row in most_used['entries']] == digests

    banned = client.post(
        '/api/emojis/batch-ban', json={'hashes': digests, 'reason': '测试'},
    ).json()
    assert banned == {'ok': True, 'banned': 3, 'requested': 3}
    assert client.get('/api/emojis', params={'banned': True}).json()['total'] == 3

    unbanned = client.post(
        '/api/emojis/batch-unban', json={'hashes': digests[:1]},
    ).json()
    assert unbanned == {'ok': True, 'unbanned': 1, 'requested': 1}

    removed = client.post(
        '/api/emojis/batch-delete', json={'hashes': digests},
    ).json()
    assert removed == {'ok': True, 'removed': 3, 'requested': 3}
    assert client.get('/api/emojis').json()['total'] == 0
    # 删除记录不解除封禁：封禁按内容哈希独立保存，与 emoji 行无关。
    assert client.get('/api/emojis').json()['stats']['bannedCount'] == 2
