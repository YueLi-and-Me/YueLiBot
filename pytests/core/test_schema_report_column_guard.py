"""schema_report 统计助手对缺列库的存在性护栏。

v20→v21 给 facts 补 origin_kind、v21→v22 补 slot / superseded_by，而
report_schema_changes 在 run_migrations 收尾对任意形态的库都会调用：
本文件用停在 v18 的库钉住「统计助手不得打断迁移收尾」这一约束。
"""

from __future__ import annotations

import sqlite3
from typing import List

from src.core.db import schema_report
from src.core.db.migrations.bootstrap import write_user_version
from src.core.db.schema import DDL
from src.core.db.schema_report import (
    SchemaChanges,
    describe_schema_changes,
    report_schema_changes,
    snapshot_shape,
)


class _LoggerRecorder:
    """按事件名记录 info/error 调用，替代 schema_report 的模块级 logger。"""

    def __init__(self) -> None:
        self.events: List[str] = []

    def info(self, event: str, **kwargs: object) -> None:
        self.events.append(event)

    def error(self, event: str, **kwargs: object) -> None:
        self.events.append(event)


def _make_v18_shape_database() -> sqlite3.Connection:
    """创建停在 v18 的最小库：facts 有 kind，没有 v21 / v22 引入的三列。"""

    db = sqlite3.connect(':memory:')
    db.executescript(
        """
        CREATE TABLE facts (
          id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL DEFAULT 1,
          kind TEXT NOT NULL DEFAULT '事件', content TEXT NOT NULL,
          content_key TEXT NOT NULL, strength REAL NOT NULL,
          half_life_hours REAL NOT NULL, updated_at INTEGER NOT NULL,
          created_at INTEGER NOT NULL, hit_count INTEGER NOT NULL DEFAULT 0,
          last_hit_at INTEGER, due_at INTEGER NOT NULL,
          active INTEGER NOT NULL DEFAULT 1, tokens_v2 TEXT NOT NULL DEFAULT '',
          embedding BLOB, UNIQUE(person_id, content_key)
        );
        """
    )
    write_user_version(db, 18)
    return db


def _insert_fact(db: sqlite3.Connection, key: str, kind: str) -> None:
    db.execute(
        '''INSERT INTO facts (id, person_id, kind, content, content_key, strength,
                              half_life_hours, updated_at, created_at, due_at)
           VALUES (1, 1, ?, ?, ?, 0.8, 2160, 1, 1, 2)''',
        (kind, f'内容-{key}', f'key-{key}'),
    )
    db.commit()


def test_report_on_v18_shape_database_does_not_raise() -> None:
    """停在 v18 的库调用 report_schema_changes 不抛：迁移收尾不被观测打断。"""

    db = _make_v18_shape_database()
    _insert_fact(db, 'one', '身份')

    report_schema_changes(
        describe_schema_changes({}, snapshot_shape(db)),
        18,
        db,
    )
    db.close()


def test_missing_columns_skip_lines_and_logs(
    monkeypatch, capsys
) -> None:
    """缺列统计不进信息框、不发日志；有列的统计照常输出。"""

    db = _make_v18_shape_database()
    _insert_fact(db, 'one', '身份')

    recorder = _LoggerRecorder()
    monkeypatch.setattr(schema_report, 'logger', recorder)

    report_schema_changes(SchemaChanges(created=['probe_table']), 18, db)

    out = capsys.readouterr().out
    assert '事实 kind 分布：身份 1' in out
    assert 'origin_kind 分布' not in out
    assert '带槽位的事实' not in out
    assert '已被取代的事实' not in out
    assert 'db_fact_kind_distribution' in recorder.events
    assert 'db_fact_origin_distribution' not in recorder.events
    db.close()


def test_db_none_keeps_legacy_path(capsys) -> None:
    """db=None 的老路径：只输出结构行，无任何事实统计。"""

    report_schema_changes(SchemaChanges(created=['probe_table']), 18, None)

    out = capsys.readouterr().out
    assert '本次新建的表' in out
    assert '事实 kind 分布' not in out
    assert '事实 origin_kind 分布' not in out
    assert '带槽位的事实' not in out

    report_schema_changes(SchemaChanges(), 18, None)
    assert capsys.readouterr().out == ''


def test_current_shape_keeps_all_statistics(monkeypatch, capsys) -> None:
    """最新形态的库四个统计行齐全，列存在时行为与合流前一致。"""

    db = sqlite3.connect(':memory:')
    db.executescript(DDL)
    _insert_fact(db, 'one', '身份')

    recorder = _LoggerRecorder()
    monkeypatch.setattr(schema_report, 'logger', recorder)

    report_schema_changes(SchemaChanges(created=['probe_table']), 22, db)

    out = capsys.readouterr().out
    assert '事实 kind 分布：身份 1' in out
    assert '事实 origin_kind 分布：legacy 1' in out
    assert '带槽位的事实：0' in out
    assert '已被取代的事实：0' in out
    assert 'db_fact_kind_distribution' in recorder.events
    assert 'db_fact_origin_distribution' in recorder.events
    db.close()
