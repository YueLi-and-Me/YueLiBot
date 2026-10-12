"""v33 -> v34：按新口径重算 facts 与 knowledge 的严格去重键。

``exact_key`` 原先删掉全部标点，「0.1.5」与「0.15」、「1.5万」与「15万」生成同一个
键；新口径把夹在两个数字之间的标点收成一个「.」。存量 ``content_key`` 不重算时，
同一条正文重写一遍会算出新键、查不到旧行，在 ``UNIQUE`` 约束之外另起一行。

只改写「存量键等于旧口径、且新旧口径不同」的行。键与旧口径本就不符的行来自更早的
写入路径，不属于这次口径变化，原样保留。在这个范围内重算不会撞键：新键去掉「.」
即为旧键，旧键两两不同则新键也两两不同；旧键里不含「.」，逐行更新途中也不会与
尚未更新的行相撞。重放时这些行的键已是新口径，命中 0 行。
"""

from __future__ import annotations

import re
import sqlite3

from .registry import register

from src.core.logging.logger import get_logger
from src.core.memory.similarity import exact_key

logger = get_logger(__name__)

FROM_VERSION = 33
_TABLES = ('facts', 'knowledge')


def _legacy_key(text: str) -> str:
    """迁移前的去重键口径：删除全部标点与符号后转小写。冻结在此，不随现行实现变化。"""

    return re.sub(r'[^\w一-鿿㐀-䶿豈-﫿]', '', text).lower()


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    """判断历史库是否已经包含指定表；部分早期最小库要到链尾 DDL 才建表。"""

    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,),
    ).fetchone()
    return row is not None


def _rekey(db: sqlite3.Connection, table: str) -> int:
    """把一张表里按旧口径写入的 ``content_key`` 改写为新口径，返回改写的行数。

    :raises RuntimeError: 改写后仍有按旧口径写入、且新口径不同的行。
    :raises sqlite3.IntegrityError: 改写撞上 ``UNIQUE`` 约束；按上文推导不应发生。
    """

    changed = 0
    for row_id, content, key in db.execute(
        f'SELECT id, content, content_key FROM {table} ORDER BY id'
    ).fetchall():
        new_key = exact_key(str(content))
        if key != _legacy_key(str(content)) or new_key == key:
            continue
        db.execute(f'UPDATE {table} SET content_key = ? WHERE id = ?', (new_key, int(row_id)))
        changed += 1
    remaining = sum(
        1 for content, key in db.execute(f'SELECT content, content_key FROM {table}')
        if key == _legacy_key(str(content)) != exact_key(str(content))
    )
    if remaining:
        raise RuntimeError(f'v33→v34 迁移自检失败：{table} 仍有 {remaining} 行按旧口径写入')
    return changed


@register(FROM_VERSION)
def migrate(db: sqlite3.Connection) -> None:
    """按新口径重算 facts 与 knowledge 的 ``content_key``；缺表的库跳过该表。"""

    changed = {table: _rekey(db, table) for table in _TABLES if _table_exists(db, table)}
    logger.info('v33_to_v34_done', **changed)
