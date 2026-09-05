from pathlib import Path
from typing import Callable, Tuple

import hashlib
import json
import sqlite3

import pytest

from scripts.migrate.common import MigrationReport
from scripts.migrate.episodes import migrate as migrate_episodes
from scripts.migrate.expressions import migrate as migrate_expressions
from scripts.migrate.jargon import migrate as migrate_jargon
from scripts.migrate.profile_seeds import migrate as migrate_profile_seeds
from src.core.db.schema import DDL, SEED


def _sha256(path: Path) -> str:
    """计算数据库文件摘要，用于证明 dry-run 和源库读取没有改文件。"""

    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _create_target(path: Path) -> None:
    """创建带固定会话、人物和身份映射的当前结构测试库。"""

    db = sqlite3.connect(path)
    db.executescript(DDL)
    db.executescript(SEED)
    db.executemany(
        "INSERT INTO persons (id, kind, first_seen_at) VALUES (?, 'human', 1)",
        [(10,), (11,), (12,)],
    )
    db.executemany(
        "INSERT INTO streams (id, platform, kind, external_id) VALUES (?, 'qq', ?, ?)",
        [
            (10, "group", "group-ok"),
            (11, "direct", "user-ok"),
        ],
    )
    db.executemany(
        """INSERT INTO identities (person_id, platform, external_id, display_name)
           VALUES (?, 'qq', ?, ?)""",
        [
            (10, "profile-new", "新画像"),
            (11, "profile-empty", "空画像"),
            (12, "profile-existing", "已有画像"),
        ],
    )
    db.executemany(
        """INSERT INTO person_profile
           (person_id, summary, evidence_count, refreshed_at, dirty)
           VALUES (?, ?, ?, ?, ?)""",
        [
            (11, "", 0, 0, 1),
            (12, "本地证据生成的画像", 8, 123, 0),
        ],
    )
    db.commit()
    db.close()


def _create_source(path: Path) -> None:
    """创建与四个历史资产入口相同字段形状的小样本源库。"""

    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE chat_streams (
          stream_id TEXT PRIMARY KEY,
          group_id TEXT,
          user_id TEXT
        );
        CREATE TABLE chat_history (
          id INTEGER PRIMARY KEY,
          chat_id TEXT,
          start_time REAL,
          end_time REAL,
          theme TEXT,
          keywords TEXT,
          summary TEXT
        );
        CREATE TABLE expression (
          id INTEGER PRIMARY KEY,
          situation TEXT,
          style TEXT,
          count INTEGER,
          chat_id TEXT,
          checked INTEGER,
          create_date REAL
        );
        CREATE TABLE jargon (
          id INTEGER PRIMARY KEY,
          content TEXT,
          meaning TEXT,
          chat_id TEXT,
          is_global INTEGER,
          is_jargon INTEGER
        );
        CREATE TABLE person_info (
          id INTEGER PRIMARY KEY,
          platform TEXT,
          user_id TEXT,
          impression TEXT
        );
        """
    )
    db.executemany(
        "INSERT INTO chat_streams (stream_id, group_id, user_id) VALUES (?, ?, ?)",
        [
            ("chat-group", "group-ok", "member-in-group"),
            ("chat-direct", None, "user-ok"),
            ("chat-missing", "group-missing", "member-missing"),
        ],
    )
    db.executemany(
        """INSERT INTO chat_history
           (id, chat_id, start_time, end_time, theme, keywords, summary)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                1,
                "chat-group",
                1735689600.25,
                1735689660.5,
                "项目进展",
                json.dumps(["迁移", "记忆", "迁移", ""], ensure_ascii=False),
                "讨论了迁移方案。",
            ),
            (
                2,
                "chat-missing",
                1735689700.0,
                1735689760.0,
                "无法映射",
                "[]",
                "这条不得落到默认会话。",
            ),
            (3, "chat-direct", 1735689800.0, 1735689860.0, "空概括", "[]", "  "),
        ],
    )
    db.executemany(
        """INSERT INTO expression
           (id, situation, style, count, chat_id, checked, create_date)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [
            (1, "被夸奖时", "先嘴硬再道谢", 7, "chat-group", 1, 1735689600.0),
            (2, "陌生群", "不能串味", 3, "chat-missing", 1, 1735689600.0),
            (3, "空风格", "  ", 1, "chat-direct", 1, 1735689600.0),
            (4, "未审核", "不迁移", 1, "chat-direct", 0, 1735689600.0),
        ],
    )
    db.executemany(
        """INSERT INTO jargon
           (id, content, meaning, chat_id, is_global, is_jargon)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [
            (1, "全局词", "所有会话都适用", "chat-missing", 1, 1),
            (2, "群内词", "只在这个群适用", "chat-group", 0, 1),
            (3, "错群词", "不得落到默认会话", "chat-missing", 0, 1),
            (4, "待定词", "尚未确认", "chat-group", 0, None),
            (5, "否定词", "确认不是黑话", "chat-group", 0, 0),
            (6, "空释义", "  ", "chat-group", 0, 1),
        ],
    )
    db.executemany(
        "INSERT INTO person_info (id, platform, user_id, impression) VALUES (?, ?, ?, ?)",
        [
            (1, "qq", "profile-new", "来自历史的画像种子"),
            (2, "qq", "profile-empty", "填入原本为空的画像行"),
            (3, "qq", "profile-existing", "不得覆盖本地画像"),
            (4, "qq", "profile-missing", "找不到本地人物"),
            (5, "qq", "profile-new", "  "),
        ],
    )
    db.commit()
    db.close()


@pytest.fixture
def databases(tmp_path: Path) -> Tuple[Path, Path]:
    """返回隔离的源库和当前结构目标库。"""

    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _create_source(source)
    _create_target(target)
    return source, target


def _assert_read_only_dry_run(
    migrate: Callable[[Path, Path, bool], MigrationReport],
    source: Path,
    target: Path,
) -> MigrationReport:
    """执行 dry-run，并断言源库、目标库文件摘要都没有变化。"""

    source_before = _sha256(source)
    target_before = _sha256(target)
    report = migrate(source, target, True)
    assert _sha256(source) == source_before
    assert _sha256(target) == target_before
    return report


def test_m1_情节迁移的计数时间单位与fts对齐(databases: Tuple[Path, Path]) -> None:
    source, target = databases

    dry_run = _assert_read_only_dry_run(migrate_episodes, source, target)
    assert dry_run.source_rows == 3
    assert dry_run.write_count == 1
    assert dry_run.skip_count("unmapped_session") == 1
    assert dry_run.unmapped_count("unmapped_session") == 1
    assert dry_run.skip_count("empty_field") == 1

    source_before = _sha256(source)
    actual = migrate_episodes(source, target, False)
    assert actual.write_count == dry_run.write_count
    assert actual.skipped == dry_run.skipped
    assert _sha256(source) == source_before

    db = sqlite3.connect(target)
    episode = db.execute(
        "SELECT kind, summary, started_at, ended_at, created_at, stream_id FROM episodes"
    ).fetchone()
    assert episode == (
        "conversation",
        "主题：项目进展\n讨论了迁移方案。",
        1735689600250,
        1735689660500,
        1735689660500,
        10,
    )
    cues = db.execute("SELECT id, cue FROM episode_cues ORDER BY id").fetchall()
    assert [row[1] for row in cues] == ["迁移", "记忆"]
    assert db.execute("SELECT rowid FROM cues_fts ORDER BY rowid").fetchall() == [
        (row[0],) for row in cues
    ]
    db.close()

    repeated = migrate_episodes(source, target, False)
    assert repeated.write_count == 0
    assert repeated.skip_count("existing") == 1


def test_m2_表达迁移可预测写入并可重复执行(databases: Tuple[Path, Path]) -> None:
    source, target = databases

    dry_run = _assert_read_only_dry_run(migrate_expressions, source, target)
    assert dry_run.source_rows == 4
    assert dry_run.write_count == 1
    assert dry_run.skip_count("unmapped_session") == 1
    assert dry_run.skip_count("empty_field") == 1
    assert dry_run.skip_count("unchecked") == 1

    actual = migrate_expressions(source, target, False)
    assert actual.write_count == dry_run.write_count
    assert actual.skipped == dry_run.skipped

    db = sqlite3.connect(target)
    assert db.execute(
        """SELECT situation, style, stream_id, use_count, source, created_at
           FROM expressions"""
    ).fetchone() == (
        "被夸奖时",
        "先嘴硬再道谢",
        10,
        7,
        "历史迁移:M-2",
        1735689600000,
    )
    db.close()

    repeated = migrate_expressions(source, target, False)
    assert repeated.write_count == 0
    assert repeated.skip_count("existing") == 1


def test_m3_黑话只迁移已确认且有释义的记录(databases: Tuple[Path, Path]) -> None:
    source, target = databases

    dry_run = _assert_read_only_dry_run(migrate_jargon, source, target)
    assert dry_run.source_rows == 6
    assert dry_run.write_count == 2
    assert dry_run.skip_count("unmapped_session") == 1
    assert dry_run.skip_count("pending") == 1
    assert dry_run.skip_count("rejected") == 1
    assert dry_run.skip_count("empty_field") == 1

    actual = migrate_jargon(source, target, False)
    assert actual.write_count == dry_run.write_count
    assert actual.skipped == dry_run.skipped

    db = sqlite3.connect(target)
    rows = db.execute(
        """SELECT term, meaning, stream_id, status, hits, source
           FROM jargon ORDER BY term"""
    ).fetchall()
    assert rows == [
        ("全局词", "所有会话都适用", None, "confirmed", 0, "历史迁移:M-3"),
        ("群内词", "只在这个群适用", 10, "confirmed", 0, "历史迁移:M-3"),
    ]
    db.close()

    repeated = migrate_jargon(source, target, False)
    assert repeated.write_count == 0
    assert repeated.skip_count("existing") == 2
    assert repeated.skip_count("pending") == 1


def test_m5_画像种子标脏且不覆盖已有画像(databases: Tuple[Path, Path]) -> None:
    source, target = databases

    dry_run = _assert_read_only_dry_run(migrate_profile_seeds, source, target)
    assert dry_run.source_rows == 5
    assert dry_run.write_count == 2
    assert dry_run.skip_count("unmapped_identity") == 1
    assert dry_run.unmapped_count("unmapped_identity") == 1
    assert dry_run.skip_count("empty_field") == 1
    assert dry_run.skip_count("existing") == 1

    actual = migrate_profile_seeds(source, target, False)
    assert actual.write_count == dry_run.write_count
    assert actual.skipped == dry_run.skipped

    db = sqlite3.connect(target)
    rows = db.execute(
        """SELECT person_id, summary, evidence_count, refreshed_at, dirty
           FROM person_profile ORDER BY person_id"""
    ).fetchall()
    assert rows == [
        (10, "来自历史的画像种子", 0, 0, 1),
        (11, "填入原本为空的画像行", 0, 0, 1),
        (12, "本地证据生成的画像", 8, 123, 0),
    ]
    db.close()

    repeated = migrate_profile_seeds(source, target, False)
    assert repeated.write_count == 0
    assert repeated.skip_count("existing") == 3


def test_四个入口报告都显式呈现映射和空字段分类(
    databases: Tuple[Path, Path],
) -> None:
    source, target = databases

    for migrate in (
        migrate_episodes,
        migrate_expressions,
        migrate_jargon,
        migrate_profile_seeds,
    ):
        report = migrate(source, target, True)
        rendered = report.render()
        assert "空字段" in rendered
        assert "已存在" in rendered
        assert "无法映射" in rendered
