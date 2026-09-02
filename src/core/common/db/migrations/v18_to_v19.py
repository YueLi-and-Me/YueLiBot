"""v18 -> v19：收敛事实类别并按新类别重算衰减时间。

旧抽取提示允许模型自由生成 ``kind``，真机数据因此分裂成三十种文本值，
大部分事实落进不对应任何类别的默认半衰期。本迁移使用固定映射把存量类别
收拢到衰减模型已有的七类，并同步重算 ``half_life_hours`` 与 ``due_at``。

迁移不改 ``strength`` 与 ``updated_at``：前者是事实已经积累的留存强度，后者
是该强度的时间锚；只用二者重新推导下一次冻结评估时刻。
"""

from __future__ import annotations

from typing import Dict

import sqlite3

from .registry import register
from src.core.memory.decay import (
    DEFAULT_FACT_KIND,
    FACT_KINDS,
    freeze_due_at,
    half_life_for,
)

FROM_VERSION = 18

# 字面映射是本次数据修复的审计依据。表中没有出现的旧值统一归入 ``事件``，
# 不根据字符或关键词做启发式推断。
KIND_MAPPING: Dict[str, str] = {
    '身份': '身份',
    '昵称': '身份',
    '籍贯': '身份',
    '居住地': '身份',
    '职责': '身份',
    '技能': '身份',
    '硬件': '身份',
    '技术配置': '身份',
    '日期': '日期',
    '偏好': '偏好',
    '喜好': '偏好',
    '厌恶': '偏好',
    '评价': '偏好',
    '需求': '偏好',
    '判断': '偏好',
    '习惯': '习惯',
    '生活': '习惯',
    '工具使用': '习惯',
    '关系': '关系',
    '事件': '事件',
    '经历': '事件',
    '计划': '事件',
    '游戏经历': '事件',
    '游戏': '事件',
    '购物': '事件',
    '开销': '事件',
    '开发': '事件',
    '开发测试': '事件',
    '状态': '状态',
    '现状': '状态',
    '健康': '状态',
    '开发状态': '状态',
    '开发进度': '状态',
}


def _validate(db: sqlite3.Connection) -> None:
    """确认所有事实都属于枚举，且两项衰减派生值逐行一致。"""

    rows = db.execute(
        'SELECT id, kind, strength, half_life_hours, updated_at, due_at FROM facts'
    ).fetchall()
    for fact_id, kind, strength, half_life_hours, updated_at, due_at in rows:
        if kind not in FACT_KINDS:
            raise RuntimeError(f'v19 迁移自检失败：facts.id={fact_id} 的类别是 {kind!r}')
        expected_half_life = half_life_for(kind)
        if float(half_life_hours) != expected_half_life:
            raise RuntimeError(
                f'v19 迁移自检失败：facts.id={fact_id} 的半衰期是 '
                f'{half_life_hours!r}，预期 {expected_half_life!r}'
            )
        expected_due_at = freeze_due_at(float(strength), int(updated_at), expected_half_life)
        if int(due_at) != expected_due_at:
            raise RuntimeError(
                f'v19 迁移自检失败：facts.id={fact_id} 的 due_at 是 '
                f'{due_at!r}，预期 {expected_due_at!r}'
            )


def _facts_exists(db: sqlite3.Connection) -> bool:
    """判断历史库是否已经包含 ``facts``；部分早期最小库要到链尾 DDL 才建表。"""

    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'facts'"
    ).fetchone()
    return row is not None


@register(FROM_VERSION)
def migrate(db: sqlite3.Connection) -> None:
    """逐行应用固定类别映射，并重算半衰期与下一次冻结评估时刻。

    重放幂等：七个目标类别也在映射表中，再次执行会得到相同的三个字段值。
    """

    if not _facts_exists(db):
        return
    rows = db.execute(
        'SELECT id, kind, strength, updated_at FROM facts ORDER BY id'
    ).fetchall()
    for fact_id, old_kind, strength, updated_at in rows:
        source_kind = old_kind.strip() if isinstance(old_kind, str) else ''
        kind = KIND_MAPPING.get(source_kind, DEFAULT_FACT_KIND)
        half_life_hours = half_life_for(kind)
        due_at = freeze_due_at(float(strength), int(updated_at), half_life_hours)
        db.execute(
            '''UPDATE facts
               SET kind = ?, half_life_hours = ?, due_at = ?
               WHERE id = ?''',
            (kind, half_life_hours, due_at, int(fact_id)),
        )
    _validate(db)
