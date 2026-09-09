"""v31 -> v32：退役「状态」事实类别，存量一律重判为「事件」。

「状态」的 12 小时半衰期对「读大三」（以年计）太短、对「正在测某个功能」
（以天计）太长：同一类别里成员寿命差两个数量级，说明它不是一个类别。退役后
枚举六类：身份 / 日期 / 偏好 / 习惯 / 关系 / 事件。

存量统一归「事件」而不按正文分流：迁移必须离线确定、不能调模型；按内容写
正则分流填错直接改的是遗忘曲线——v22 -> v23 那次槽位回填填错只损失一个空槽，
这次的风险不对等。统一归事件对最差一条也是 42 倍半衰期改善，之后的自然纠正
由抽取提示词承担：memory.extract.md 已把在读年级、就业休学写进「身份」的描述，
把近期开发进度、身体状况写进「事件」的描述。新写入按提示词分流、存量统一归
事件，两者口径不同是有意的。

重算按新半衰期推导 due_at，并把 active 直接置为「新留存度 >= REVIVE」：
换曲线之后原活跃位表达的是旧曲线下的结论，按新留存度复活该复活的行
（如「目前读大三」），避免被 12 小时曲线冻住的事实在新曲线下继续沉底。

人工置顶（半衰期达 ``PIN_HALF_LIFE_HOURS``）的行只改类别，不动它的半衰期
与衰减派生值。重放幂等：没有「状态」行时命中 0 行，正常结束。
"""

from __future__ import annotations

import sqlite3

from .registry import register

from src.core.logging.logger import get_logger
from src.core.memory.decay import (
    REVIVE,
    freeze_due_at,
    half_life_for,
    is_pinned,
    retention,
)
from src.core.runtime.clock import now as current_time

logger = get_logger(__name__)

FROM_VERSION = 31
_TARGET_KIND = '事件'
_SOURCE_KIND = '状态'


def _facts_exists(db: sqlite3.Connection) -> bool:
    """判断历史库是否已经包含 ``facts``；部分早期最小库要到链尾 DDL 才建表。"""

    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'facts'"
    ).fetchone()
    return row is not None


@register(FROM_VERSION)
def migrate(db: sqlite3.Connection) -> None:
    """把全部「状态」事实重判为「事件」，按新半衰期重算衰减派生值。"""

    if not _facts_exists(db):
        return
    now = current_time()
    rows = db.execute(
        'SELECT id, strength, updated_at, half_life_hours FROM facts'
        ' WHERE kind = ? ORDER BY id',
        (_SOURCE_KIND,),
    ).fetchall()
    pinned = 0
    for fact_id, strength, updated_at, old_half_life in rows:
        if is_pinned(float(old_half_life)):
            # 人工置顶是一种编码而不是状态：半衰期推到百年量级的行不参与
            # 自然衰减，只改类别，其余字段一律不动。
            db.execute(
                'UPDATE facts SET kind = ? WHERE id = ?', (_TARGET_KIND, int(fact_id)),
            )
            pinned += 1
            continue
        half_life = half_life_for(_TARGET_KIND)
        due_at = freeze_due_at(float(strength), int(updated_at), half_life)
        active = 1 if retention(
            float(strength), int(updated_at), half_life, now,
        ) >= REVIVE else 0
        db.execute(
            'UPDATE facts SET kind = ?, half_life_hours = ?, due_at = ?, active = ?'
            ' WHERE id = ?',
            (_TARGET_KIND, half_life, due_at, active, int(fact_id)),
        )
    remaining = db.execute(
        'SELECT COUNT(*) FROM facts WHERE kind = ?', (_SOURCE_KIND,),
    ).fetchone()[0]
    if remaining:
        raise RuntimeError(f'v31→v32 迁移自检失败：仍有 {remaining} 条「状态」类事实')
    logger.info('v31_to_v32_done', rejudged=len(rows), pinned=pinned)
