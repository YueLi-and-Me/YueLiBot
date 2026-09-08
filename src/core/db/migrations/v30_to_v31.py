"""v30 -> v31：回填表情包视觉身份，合并重编码副本并传播封禁。

迁移先核验全部文件并制定合并计划，再执行 DDL 和数据修改。删除的文件另存
到数据目录 backups/emoji-v30，SQL 或文件自检失败时回滚并恢复文件；备份也用于
进程意外退出后的人工恢复。迁移不创建独立 logger，沿用管理器的迁移步骤报告。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import hashlib
import sqlite3

from .registry import register

from src.core.services.media.emoji import (
    _delete_emoji_file,
    _file_ref_path,
    emoji_visual_key,
    same_emoji_visual,
)

FROM_VERSION = 30


def _columns(db: sqlite3.Connection, table: str) -> List[str]:
    """读取固定迁移表的列名，已有列在重放时不再执行 ALTER TABLE。"""
    return [row[0] for row in db.execute('SELECT name FROM pragma_table_info(?)', (table,))]


@register(FROM_VERSION)
def migrate(db: sqlite3.Connection) -> None:
    """保留每组最早记录及其标签、向量，累计计数并删除多余行与文件。

    持久化库只允许修改该库同目录的 emojis，防止库副本里的绝对引用误删原库
    文件。内存库从已有文件的父目录确定边界。图片缺失、路径越界、哈希错误、
    计数或文件数不守恒均完整抛错，禁止跳过坏行推进版本。
    """
    emoji_columns = _columns(db, 'emoji')
    banned_columns = _columns(db, 'emoji_banned')
    # 最小旧库可能尚未创建表情包表，没有存量图片需要回填；缺表交给链尾
    # 当前 DDL 创建。若只有独立封禁表，仍需补列，以免 CREATE IF NOT EXISTS
    # 留下旧结构。已有表内的坏数据仍由后续预检完整报错，不能按此路径跳过。
    if not emoji_columns:
        if banned_columns and 'visual_key' not in banned_columns:
            db.execute("ALTER TABLE emoji_banned ADD COLUMN visual_key TEXT NOT NULL DEFAULT ''")
            if 'visual_key' not in _columns(db, 'emoji_banned'):
                raise sqlite3.OperationalError('表情包封禁表迁移后缺少 visual_key')
        return
    # 当前结构的重放只读元数据，不能重新合并、刷新封禁时间或重复删文件。
    if 'visual_key' in emoji_columns and 'visual_key' in banned_columns:
        return

    cursor = db.execute('SELECT * FROM emoji ORDER BY first_seen_at, hash')
    names = [column[0] for column in cursor.description]
    rows: List[Dict[str, Any]] = [dict(zip(names, row)) for row in cursor.fetchall()]
    bans = (
        db.execute('SELECT hash, banned_at, reason FROM emoji_banned').fetchall()
        if banned_columns else []
    )
    totals = db.execute(
        'SELECT COALESCE(SUM(use_count), 0), COALESCE(SUM(seen_count), 0) FROM emoji'
    ).fetchone()
    database_file = next(row[2] for row in db.execute('PRAGMA database_list') if row[1] == 'main')
    directory = Path(database_file).resolve().parent / 'emojis' if database_file else None
    paths: Dict[str, Path] = {}
    features: Dict[str, str] = {}
    groups: List[List[Dict[str, Any]]] = []
    for row in rows:
        path = _file_ref_path(row['send_ref']).resolve(strict=True)
        if directory is None:
            directory = path.parent
        if not path.is_relative_to(directory) or path.stem.lower() != row['hash'].lower():
            raise ValueError(f'表情包迁移文件引用越界或文件名不是哈希：{row["hash"]}')
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != row['hash'].lower():
            raise ValueError(f'表情包迁移文件内容哈希不一致：{row["hash"]}')
        paths[row['hash']] = path
        key = emoji_visual_key(content)
        features[row['hash']] = key
        for group in groups:
            if same_emoji_visual(key, features[group[0]['hash']]):
                group.append(row)
                break
        else:
            groups.append([row])

    if directory is not None:
        file_count = sum(path.is_file() for path in directory.rglob('*'))
        if file_count != len(rows):
            raise ValueError(f'表情包迁移前文件数 {file_count} 与记录数 {len(rows)} 不一致')

    # 在数据库提交之前保留被删除文件的独立副本；不把备份放进 emojis，
    # 否则会成为孤儿文件并破坏目录与记录的一一对应。
    removed_files: Dict[Path, bytes] = {}
    for group in groups:
        for row in group[1:]:
            path = paths[row['hash']]
            removed_files[path] = path.read_bytes()
    if removed_files and directory is not None:
        backup_dir = directory.parent / 'backups' / 'emoji-v30'
        backup_dir.mkdir(parents=True, exist_ok=True)
        for path, content in removed_files.items():
            backup_path = backup_dir / path.name
            if backup_path.exists():
                if backup_path.read_bytes() != content:
                    raise ValueError(f'表情包迁移备份内容不一致：{path.name}')
            else:
                backup_path.write_bytes(content)

    db.execute('SAVEPOINT emoji_visual_migration')
    try:
        if 'visual_key' not in emoji_columns:
            db.execute("ALTER TABLE emoji ADD COLUMN visual_key TEXT NOT NULL DEFAULT ''")
        if not banned_columns:
            db.execute(
                '''CREATE TABLE emoji_banned (
                       hash TEXT PRIMARY KEY, visual_key TEXT NOT NULL DEFAULT '',
                       banned_at INTEGER NOT NULL, reason TEXT)'''
            )
        elif 'visual_key' not in banned_columns:
            db.execute("ALTER TABLE emoji_banned ADD COLUMN visual_key TEXT NOT NULL DEFAULT ''")

        canonical: Dict[str, str] = {}
        for group in groups:
            keep = group[0]
            key = features[keep['hash']]
            for row in group:
                canonical[row['hash'].lower()] = key
            last_used = [row['last_used_at'] for row in group if row['last_used_at'] is not None]
            db.execute(
                'UPDATE emoji SET visual_key = ?, use_count = ?, seen_count = ?, '
                'last_used_at = ? WHERE hash = ?',
                (key, sum(row['use_count'] for row in group),
                 sum(row['seen_count'] for row in group), max(last_used) if last_used else None,
                 keep['hash']),
            )
            for row in group[1:]:
                db.execute('DELETE FROM emoji WHERE hash = ?', (row['hash'],))

        # 保留每一条原始封禁及原因、时间；所有副本指向同一个视觉身份。
        # 删除 emoji 行不影响封禁证据，管理页通过统一 predicate 判断保留行。
        for digest, _banned_at, _reason in bans:
            if digest.lower() in canonical:
                db.execute(
                    'UPDATE emoji_banned SET visual_key = ? WHERE hash = ?',
                    (canonical[digest.lower()], digest),
                )

        after = db.execute(
            'SELECT COALESCE(SUM(use_count), 0), COALESCE(SUM(seen_count), 0) FROM emoji'
        ).fetchone()
        if tuple(after) != tuple(totals):
            raise sqlite3.IntegrityError('表情包合并前后使用次数或见到次数不守恒')
        for path in removed_files:
            _delete_emoji_file(path, directory)
            if path.exists():
                raise OSError(f'表情包合并文件删除失败：{path.name}')
        if directory is not None:
            file_count = sum(path.is_file() for path in directory.rglob('*'))
            count = db.execute('SELECT COUNT(*) FROM emoji').fetchone()[0]
            if file_count != count:
                raise sqlite3.IntegrityError(
                    f'表情包迁移后文件数 {file_count} 与记录数 {count} 不一致'
                )
        db.execute('RELEASE SAVEPOINT emoji_visual_migration')
    except BaseException:
        db.execute('ROLLBACK TO SAVEPOINT emoji_visual_migration')
        db.execute('RELEASE SAVEPOINT emoji_visual_migration')
        for path, content in removed_files.items():
            path.write_bytes(content)
        raise
