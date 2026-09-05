"""历史资产迁移入口共用的数据库安全、映射和计数设施。"""

from __future__ import annotations

from argparse import ArgumentParser
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from time import time_ns
from typing import Callable, Dict, Iterator, List, Optional, Set, Tuple

import sqlite3
import sys


SKIP_EMPTY_FIELD = "empty_field"
SKIP_EXISTING = "existing"
SKIP_PENDING = "pending"
SKIP_REJECTED = "rejected"
SKIP_UNCHECKED = "unchecked"
SKIP_UNMAPPED_IDENTITY = "unmapped_identity"
SKIP_UNMAPPED_SESSION = "unmapped_session"

_REASON_LABELS = {
    SKIP_EMPTY_FIELD: "空字段",
    SKIP_EXISTING: "已存在",
    SKIP_PENDING: "尚未确认",
    SKIP_REJECTED: "已判定为非目标数据",
    SKIP_UNCHECKED: "未审核",
    SKIP_UNMAPPED_IDENTITY: "无法映射的人物",
    SKIP_UNMAPPED_SESSION: "无法映射的会话",
}
_MIN_REASONABLE_SECONDS = 946684800.0
_MAX_REASONABLE_SECONDS = 4102444800.0


class MigrationError(RuntimeError):
    """表示源数据形状、目标结构或映射不满足迁移前提。"""


@dataclass
class MigrationReport:
    """保存单个迁移入口的可核对计数。

    ``write_count`` 在 dry-run 中表示预计写入数，在正式模式中表示实际写入数。两种模式
    使用完全相同的筛选和内容键，因此目标库未发生并发变化时两者应相等。
    """

    task: str
    dry_run: bool
    reasons: Tuple[str, ...]
    source_rows: int = 0
    write_count: int = 0
    skipped: Dict[str, int] = field(default_factory=dict)
    _unmapped_keys: Dict[str, Set[str]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """补齐所有入口声明过的跳过分类，使零计数也会出现在报告中。"""

        for reason in self.reasons:
            self.skipped.setdefault(reason, 0)
            if reason in (SKIP_UNMAPPED_IDENTITY, SKIP_UNMAPPED_SESSION):
                self._unmapped_keys.setdefault(reason, set())

    def record_skip(self, reason: str, key: Optional[str] = None) -> None:
        """记录一条被跳过的数据及其可选映射键。"""

        if reason not in self.skipped:
            raise MigrationError(f"迁移报告未声明跳过分类：{reason}")
        self.skipped[reason] += 1
        if key is not None:
            self._unmapped_keys.setdefault(reason, set()).add(key)

    def record_write(self) -> None:
        """记录一条预计写入或已经写入的数据。"""

        self.write_count += 1

    def skip_count(self, reason: str) -> int:
        """返回指定原因的跳过行数。"""

        return self.skipped.get(reason, 0)

    def unmapped_count(self, reason: str) -> int:
        """返回指定映射失败分类涉及的不同键数量。"""

        return len(self._unmapped_keys.get(reason, set()))

    def render(self) -> str:
        """渲染简体中文计数报告，显式显示所有零值分类。"""

        mode = "dry-run" if self.dry_run else "正式写入"
        write_label = "预计写入" if self.dry_run else "实际写入"
        lines = [
            f"{self.task}（{mode}）",
            f"源数据：{self.source_rows} 条",
            f"{write_label}：{self.write_count} 条",
            "跳过分类：",
        ]
        for reason in self.reasons:
            label = _REASON_LABELS[reason]
            rows = self.skip_count(reason)
            if reason == SKIP_UNMAPPED_SESSION:
                lines.append(
                    f"  - {label}：{rows} 条，来自 {self.unmapped_count(reason)} 个会话"
                )
            elif reason == SKIP_UNMAPPED_IDENTITY:
                lines.append(
                    f"  - {label}：{rows} 条，涉及 {self.unmapped_count(reason)} 个人物标识"
                )
            else:
                lines.append(f"  - {label}：{rows} 条")
        return "\n".join(lines)


@contextmanager
def open_databases(
    source_path: Path,
    target_path: Path,
    dry_run: bool,
) -> Iterator[Tuple[sqlite3.Connection, sqlite3.Connection]]:
    """以只读模式打开源库，并按运行模式只读或读写打开已存在的目标库。

    源连接固定使用 SQLite URI ``mode=ro``，不执行任何 PRAGMA。目标库也使用 URI，
    正式模式指定 ``mode=rw``，从而在路径写错时直接失败而不是创建一份空库。
    """

    source = _resolve_database_file(source_path, "源库")
    target = _resolve_database_file(target_path, "目标库")
    if source == target:
        raise MigrationError("源库与目标库不能是同一个文件")

    source_db: Optional[sqlite3.Connection] = None
    target_db: Optional[sqlite3.Connection] = None
    try:
        source_db = _connect_uri(source, "ro")
        target_db = _connect_uri(target, "ro" if dry_run else "rw")
        yield source_db, target_db
    except Exception:
        if target_db is not None and not dry_run:
            target_db.rollback()
        raise
    finally:
        if target_db is not None:
            target_db.close()
        if source_db is not None:
            source_db.close()


def validate_query(db: sqlite3.Connection, sql: str, label: str) -> None:
    """执行一条零行查询，确认所需表和字段真实存在。"""

    try:
        db.execute(sql).fetchone()
    except sqlite3.Error as exc:
        raise MigrationError(f"{label}结构不符合迁移规格：{exc}") from exc


def load_stream_mapping(
    source_db: sqlite3.Connection,
    target_db: sqlite3.Connection,
) -> Dict[str, Optional[int]]:
    """把源库哈希会话映射为当前库既有 group/direct stream。

    映射只使用迁移规格已拍板的 ``kind + external_id`` 关系，不创建 stream。若当前库在不同
    平台存在同 kind、同 external_id 的歧义，直接报错，避免把历史挂错会话。
    """

    validate_query(
        source_db,
        "SELECT stream_id, group_id, user_id FROM chat_streams LIMIT 0",
        "源库 chat_streams ",
    )
    validate_query(
        target_db,
        "SELECT id, kind, external_id FROM streams LIMIT 0",
        "目标库 streams ",
    )

    target_index: Dict[Tuple[str, str], int] = {}
    for row in target_db.execute(
        "SELECT id, kind, external_id FROM streams WHERE kind IN ('group', 'direct')"
    ):
        kind = text_value(row["kind"], "streams.kind", int(row["id"]))
        external_id = text_value(row["external_id"], "streams.external_id", int(row["id"]))
        key = (kind, external_id)
        if key in target_index:
            raise MigrationError(
                f"目标库存在歧义会话：kind={kind!r}, external_id={external_id!r}"
            )
        target_index[key] = int(row["id"])

    mapping: Dict[str, Optional[int]] = {}
    for row in source_db.execute(
        "SELECT stream_id, group_id, user_id FROM chat_streams ORDER BY stream_id"
    ):
        source_id = text_value(row["stream_id"], "chat_streams.stream_id", 0)
        if not source_id:
            continue
        group_id = text_value(row["group_id"], "chat_streams.group_id", 0)
        user_id = text_value(row["user_id"], "chat_streams.user_id", 0)
        target_id: Optional[int]
        if group_id:
            target_id = target_index.get(("group", group_id))
        elif user_id:
            target_id = target_index.get(("direct", user_id))
        else:
            target_id = None
        if source_id in mapping and mapping[source_id] != target_id:
            raise MigrationError(f"源库会话 {source_id!r} 对应多个不同目标")
        mapping[source_id] = target_id
    return mapping


def load_qq_identity_mapping(target_db: sqlite3.Connection) -> Dict[str, int]:
    """读取当前库 QQ external_id 到 person_id 的既有映射。"""

    validate_query(
        target_db,
        "SELECT person_id, platform, external_id FROM identities LIMIT 0",
        "目标库 identities ",
    )
    mapping: Dict[str, int] = {}
    for row in target_db.execute(
        "SELECT person_id, external_id FROM identities WHERE lower(platform) = 'qq'"
    ):
        person_id = int(row["person_id"])
        external_id = text_value(row["external_id"], "identities.external_id", person_id)
        if external_id in mapping and mapping[external_id] != person_id:
            raise MigrationError(f"目标库 QQ 标识 {external_id!r} 对应多个人物")
        mapping[external_id] = person_id
    return mapping


def text_value(value: object, field_name: str, row_id: int) -> str:
    """读取 SQLite 文本字段；空值归为空串，非文本类型立即暴露。"""

    if value is None:
        return ""
    if not isinstance(value, str):
        raise MigrationError(
            f"第 {row_id} 行字段 {field_name} 应为文本，实际为 {type(value).__name__}"
        )
    return value.strip()


def non_negative_integer(value: object, field_name: str, row_id: int) -> int:
    """读取必须为非负整数的计数字段。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MigrationError(f"第 {row_id} 行字段 {field_name} 必须是非负整数")
    return value


def sqlite_boolean(value: object, field_name: str, row_id: int) -> Optional[bool]:
    """把 SQLite 的 0/1/NULL 布尔值转成 Python 值，其他值视为源数据错误。"""

    if value is None:
        return None
    if value == 0:
        return False
    if value == 1:
        return True
    raise MigrationError(f"第 {row_id} 行字段 {field_name} 只能是 0、1 或 NULL")


def unix_milliseconds(value: object, field_name: str, row_id: int) -> int:
    """把合理范围内的 Unix 秒或毫秒统一为毫秒，并拒绝异常年代。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MigrationError(f"第 {row_id} 行字段 {field_name} 必须是 Unix 时间数值")
    numeric = float(value)
    if _MIN_REASONABLE_SECONDS <= numeric <= _MAX_REASONABLE_SECONDS:
        return int(round(numeric * 1000))
    if _MIN_REASONABLE_SECONDS * 1000 <= numeric <= _MAX_REASONABLE_SECONDS * 1000:
        return int(round(numeric))
    raise MigrationError(
        f"第 {row_id} 行字段 {field_name} 不在 2000—2100 年合理范围：{value!r}"
    )


def now_milliseconds() -> int:
    """返回当前 Unix 毫秒时间戳。"""

    return time_ns() // 1_000_000


def unmapped_key(value: str) -> str:
    """为缺失会话或人物标识提供稳定计数键，不把数据挂到默认对象。"""

    return value if value else "<空标识>"


def run_cli(
    description: str,
    migrate: Callable[[Path, Path, bool], MigrationReport],
    argv: Optional[List[str]] = None,
) -> int:
    """解析四个入口共用参数，运行迁移并输出中文报告。"""

    parser = ArgumentParser(description=description)
    parser.add_argument("--source-db", required=True, type=Path, help="只读历史源库路径")
    parser.add_argument("--target-db", required=True, type=Path, help="已初始化的当前记忆库路径")
    parser.add_argument("--dry-run", action="store_true", help="只统计预计结果，不修改目标库")
    args = parser.parse_args(argv)
    try:
        report = migrate(args.source_db, args.target_db, args.dry_run)
    except (MigrationError, OSError, sqlite3.Error) as exc:
        print(f"迁移失败：{exc}", file=sys.stderr)
        return 1
    print(report.render())
    return 0


def _resolve_database_file(path: Path, label: str) -> Path:
    """解析并验证数据库路径，拒绝目录、缺失文件和隐式新建。"""

    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise MigrationError(f"{label}不存在：{path}") from exc
    if not resolved.is_file():
        raise MigrationError(f"{label}不是文件：{resolved}")
    return resolved


def _connect_uri(path: Path, mode: str) -> sqlite3.Connection:
    """按明确的 ro/rw URI 模式打开 SQLite，不执行隐式建库。"""

    uri = f"{path.as_uri()}?mode={mode}"
    db = sqlite3.connect(uri, uri=True, timeout=5.0)
    db.row_factory = sqlite3.Row
    return db
