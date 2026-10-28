"""v36 -> v37：表达学习三列与学习游标回填。

``expressions`` 新增三列，存量行取声明的默认值：

- ``learn_count``：该表达被学到的次数，单位次，取值 ≥ 1。新学到的行记 1，之后同一
  ``(situation, style, stream_id)`` 再次被学到时 +1；它是抽样权重的来源（见
  ``agent/expression.py``），与只记本机被选中次数的 ``use_count`` 语义不同。
- ``reading``：那条原话在对话里指代谁、指什么事（指代说明），长度上限 200 字符。
- ``quote``：从来源消息里原样摘出的那段话，长度上限 80 字符（学习侧口径见
  ``agent/expression_learn.py``）。

存量行 ``learn_count`` 一律为 1：旧条目没有逐次计数，按「至少被学过一次」记，
使新旧条目的抽样权重口径一致；``reading`` / ``quote`` 留空串，审核页对空值
整行不渲染。

同时回拨 ``meta.expression_learn_cursor`` 里每个会话的学习游标：游标改到「迁移
时刻往前 14 天」窗口内第一条消息的 id 之前，使后台学习在升级后重新处理近 14 天
对话，补齐新的六字段与来源校验口径下的学习结果（任务书设计决定 9）。

幂等口径：

- 列已存在时跳过对应的 ``ALTER TABLE``；本次没有执行任何 DDL 时不做自检断言，
  原因见 :func:``_verify_added_columns``。
- 游标回填只在目标值更早时写回，因此重复执行不会把游标推后，也不会改写已回填的
  值；窗口内没有消息的会话保持不动。
- 迁移不删除、不改写任何 ``expressions`` 行的 ``checked`` / ``situation`` /
  ``style`` / ``use_count`` / ``last_used_at`` / ``source`` / ``created_at``。
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

import json
import sqlite3

from .registry import register

from src.core.logging.logger import get_logger
from src.core.runtime.clock import now as current_time

logger = get_logger(__name__)

FROM_VERSION = 36
# 回填天数，单位天，取值 14：升级后重新学习的对话窗口，见任务书设计决定 9。
BACKFILL_DAYS = 14
# 一天的毫秒数；只用于把回填天数换算成与时间戳同单位的窗口宽度。
_MS_PER_DAY = 24 * 3600 * 1000
# 学习游标键，必须与 ``agent/expression_learn.CURSOR_KEY`` 一致。迁移不 import
# 该模块，避免把模型调用层拉进数据库迁移；两处一致性由 test_migration_v37 钉住。
CURSOR_KEY = 'expression_learn_cursor'


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    """判断迁移目标表是否已由历史结构创建。

    :param db: 已打开的 SQLite 连接。
    :param table: 表名；调用方只传本模块内的字面量。
    :return: 表存在时返回 ``True``，否则返回 ``False``。
    :raises sqlite3.Error: 元数据查询失败时传播数据库异常。
    副作用：只读 ``sqlite_master``。
    """

    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,),
    ).fetchone()
    return row is not None


def _column_info(db: sqlite3.Connection) -> Dict[str, Tuple[int, str | None]]:
    """读取 ``expressions`` 每列的 NOT NULL 标记与默认值声明。

    :param db: 已打开的 SQLite 连接；调用方须先确认 ``expressions`` 存在。
    :return: 列名到 ``(notnull, dflt_value)`` 的映射。``notnull`` 是 PRAGMA
        原样返回的 0/1 标记；``dflt_value`` 是默认值声明文本，无默认值时为
        ``None``。
    :raises sqlite3.Error: PRAGMA 查询失败时传播数据库异常。
    副作用：只读表结构。
    """

    return {
        str(row[1]): (int(row[3]), row[4])
        for row in db.execute('PRAGMA table_info(expressions)')
    }


def _verify_added_columns(db: sqlite3.Connection) -> None:
    """自检本次补列结果，失败抛错让整个迁移事务回滚。

    [关键] 只在本次真的执行过 ``ALTER TABLE`` 时调用。

    - 现象：升级后新学到的行 ``learn_count`` 会大于 1，而升级前的存量行全为 1。
    - 原因：下面的断言描述的是「补列那一刻的存量状态」——默认值刚填进去、学习
      尚未在新列上累加。学习一旦运行，这个前提就不成立。
    - 后果：迁移是幂等的，重放（例如备份恢复后的再次启动）若无条件断言，会在完全
      正常的库上报「自检失败」，把本可正常启动的进程拦成故障。

    :param db: 刚执行过补列的 SQLite 连接。
    :return: 无返回值；断言通过时静默返回。
    :raises RuntimeError: 新列缺失、默认值或非空声明与预期不符，或存量行的
        ``learn_count`` 不是 1。
    副作用：只读表结构与存量行。
    """

    declared = {
        'learn_count': (1, '1'),
        'reading': (1, "''"),
        'quote': (1, "''"),
    }
    columns = _column_info(db)
    for name, expected in declared.items():
        actual = columns.get(name)
        if actual is None:
            raise RuntimeError(f'v36→v37 迁移自检失败：expressions 缺少列 {name}')
        if actual != expected:
            raise RuntimeError(
                f'v36→v37 迁移自检失败：expressions.{name} 的 '
                f'(notnull, dflt_value) 是 {actual!r}，预期 {expected!r}'
            )
    stale = int(db.execute(
        'SELECT COUNT(*) FROM expressions WHERE learn_count != 1'
    ).fetchone()[0])
    if stale:
        raise RuntimeError(
            f'v36→v37 迁移自检失败：{stale} 条存量行的 learn_count 不是 1'
        )


def _backfill_cursors(db: sqlite3.Connection) -> int:
    """把学习游标回拨到回填窗口内第一条消息之前。

    逐会话处理 ``meta.expression_learn_cursor`` 的每个游标：取该会话在窗口内
    最早的一条消息，把游标设到它前一条消息的 id。窗口内没有消息的会话不动；当前
    游标已经更早的会话也不动——游标只允许被推早，不允许被推后，否则重放会把学习
    已经处理过的进度往前挪、跳过尚未学习的消息。

    :param db: 已打开的 SQLite 连接。
    :return: 实际回拨的会话数；``0`` 表示没有游标需要改。
    :raises sqlite3.Error: 读取 ``meta`` / ``messages`` 或写回 ``meta``
        失败。
    副作用：需要回拨时把整份游标 JSON 覆盖写回 ``meta``；不提交事务。
    """

    if not _table_exists(db, 'meta'):
        # 迁移链在 v5→v6 已要求 meta 存在；这里只防御手工构造的最小库。
        logger.info('v36_to_v37_cursor_skipped', reason='缺少 meta 表')
        return 0
    row = db.execute(
        'SELECT value FROM meta WHERE key = ?', (CURSOR_KEY,),
    ).fetchone()
    if row is None:
        return 0
    try:
        cursor_map = json.loads(row[0])
    except (TypeError, ValueError):
        logger.warning('v36_to_v37_cursor_unparsable', raw=repr(row[0]))
        return 0
    if not isinstance(cursor_map, dict):
        # 非对象说明该键被别的写入方改写过，无法判断哪些会话要回拨；按任务书
        # 口径只用告警暴露，不阻断启动。
        logger.warning(
            'v36_to_v37_cursor_not_object',
            valueType=type(cursor_map).__name__,
            value=repr(cursor_map),
        )
        return 0
    cutoff = current_time() - BACKFILL_DAYS * _MS_PER_DAY
    updated: Dict[str, Any] = dict(cursor_map)
    rewound = 0
    for key, cursor in cursor_map.items():
        if isinstance(cursor, bool) or not isinstance(cursor, int):
            logger.warning(
                'v36_to_v37_cursor_value_invalid', stream=key, value=repr(cursor),
            )
            continue
        try:
            stream_id = int(key)
        except (TypeError, ValueError):
            logger.warning('v36_to_v37_cursor_key_invalid', stream=repr(key))
            continue
        first = db.execute(
            'SELECT MIN(id) FROM messages WHERE stream_id = ? AND created_at >= ?',
            (stream_id, cutoff),
        ).fetchone()[0]
        if first is None:
            continue
        target = int(first) - 1
        if target < cursor:
            updated[key] = target
            rewound += 1
    if rewound:
        db.execute(
            'INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)',
            (CURSOR_KEY, json.dumps(updated, ensure_ascii=False)),
        )
    return rewound


@register(FROM_VERSION)
def migrate(db: sqlite3.Connection) -> None:
    """补齐表达学习三列并回拨学习游标；重放时只处理尚未执行的部分。

    迁移不提交事务，由迁移管理器统一提交或回滚。

    :param db: 当前迁移事务使用的 SQLite 连接。
    :raises RuntimeError: 本次补列后的结构自检失败。
    :raises sqlite3.Error: DDL 或游标读写失败。
    副作用：``expressions`` 增加三列并填入默认值；``meta`` 里的学习游标
        可能被回拨。
    """

    if not _table_exists(db, 'expressions'):
        # 最小旧库要到链尾的当前 DDL 才建表，此时没有列可补、也没有表达学习游标。
        logger.info('v36_to_v37_skipped', reason='缺少 expressions 表')
        return
    columns = _column_info(db)
    added = 0
    # 列名、类型与默认值均为静态字面量，不以字符串拼接生成 DDL（安全门禁口径）。
    # 判重靠 PRAGMA 而不是 try/except ALTER：后者会把磁盘、约束等真实 DDL 失败
    # 一并当成「列已存在」吞掉。
    if 'learn_count' not in columns:
        db.execute(
            'ALTER TABLE expressions '
            'ADD COLUMN learn_count INTEGER NOT NULL DEFAULT 1'
        )
        added += 1
    if 'reading' not in columns:
        db.execute(
            "ALTER TABLE expressions ADD COLUMN reading TEXT NOT NULL DEFAULT ''"
        )
        added += 1
    if 'quote' not in columns:
        db.execute(
            "ALTER TABLE expressions ADD COLUMN quote TEXT NOT NULL DEFAULT ''"
        )
        added += 1
    if added:
        _verify_added_columns(db)
    rewound = _backfill_cursors(db)
    logger.info('v36_to_v37_done', addedColumns=added, cursorsRewound=rewound)
