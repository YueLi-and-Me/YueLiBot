"""v22 -> v23：为四类存量事实回填单值槽位。

W9 的事实账本只对增量生效：``slot`` 由抽取模型在写入时给出，存量行全部空槽，
「同一 ``(person_id, slot)`` 下的异值冲突」对存量记忆不可见。本迁移按正文内容
为 身份 / 状态 / 关系 / 日期 四类存量事实回填 ``slot``——只有正文明确表达了
某个单值维度（住在某地、是某职业、昵称是某名）时才填，其余保持空槽。

不改正文、不判取代、不动多值类（偏好 / 事件 / 习惯）：这三类天然一人多值，
空槽是正确状态。分类依据全部来自事实自己的正文，与来源无关，因此可以诚实做。
槽位名与抽取提示词同口径（2～6 字名词：居住地、职业、生日……），
此后新写的事实会与回填行落进同一槽位，冲突检测对存量与增量一视同仁。
"""

from __future__ import annotations

from typing import Tuple

import re
import sqlite3

from .registry import register

FROM_VERSION = 22

# 只回填这四类：它们承载单值维度；偏好 / 事件 / 习惯天然一人多值，不动。
_BACKFILL_KINDS = ('身份', '状态', '关系', '日期')

# 归类规则按序匹配，先中先得。模式只认正文明说出来的单值维度：
# 「被称为『群里唯一的猪』」这类别人起的外号不算昵称，「有对象」这类关系状态
# 措辞太松也不填——填错了槽比空槽更糟，空槽只是暂时不参与冲突检测。
_OCCUPATIONS = (
    '学生', '程序员', '工程师', '老师', '教师', '医生', '护士', '律师',
    '设计师', '会计', '销售', '运营', '产品经理', '公务员', '自由职业',
)
SLOT_RULES: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ('居住地', re.compile(r'住在|现居|居住在|定居在?|家住')),
    ('职业', re.compile(
        r'(?:是|作为|当了)(?:一名|一个|一位)?(?:' + '|'.join(_OCCUPATIONS) + r')'
        r'|在校学生|在校生'
    )),
    ('昵称', re.compile(r'昵称(?:是|叫)|名为|名字叫')),
    ('年级', re.compile(r'(?:读|在|上)(?:大[一二三四五六]|研[一二三]|高[一二三]|初[一二三])')),
    ('生日', re.compile(r'生日(?:是|在)|出生于|生于\d')),
)


def classify_slot(content: str) -> str:
    """按正文内容判断事实属于哪个单值槽位；没有明确命中时返回空串。

    :param content: 事实正文。
    :return: 槽位名；正文没有明确表达任何单值维度时为 ``''``。
    副作用：纯函数，不读外部状态。
    """

    for slot, pattern in SLOT_RULES:
        if pattern.search(content):
            return slot
    return ''


def _facts_exists(db: sqlite3.Connection) -> bool:
    """判断历史库是否已经包含 ``facts``；部分早期最小库要到链尾 DDL 才建表。"""

    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'facts'"
    ).fetchone()
    return row is not None


@register(FROM_VERSION)
def migrate(db: sqlite3.Connection) -> None:
    """为四类存量事实回填 ``slot``，重放幂等。

    只读四类中仍为空槽的行；已填槽的行（含抽取写入的）一律不动。
    自检重算一遍归类：四类里不允许再存在「正文可归类却仍是空槽」的行。
    """

    if not _facts_exists(db):
        return
    placeholders = ', '.join('?' for _ in _BACKFILL_KINDS)
    rows = db.execute(
        f'''SELECT id, content FROM facts
            WHERE kind IN ({placeholders}) AND (slot IS NULL OR slot = '')''',
        _BACKFILL_KINDS,
    ).fetchall()
    for fact_id, content in rows:
        slot = classify_slot(str(content))
        if slot:
            db.execute(
                "UPDATE facts SET slot = ? WHERE id = ? AND (slot IS NULL OR slot = '')",
                (slot, int(fact_id)),
            )

    leftovers = db.execute(
        f'''SELECT id, content FROM facts
            WHERE kind IN ({placeholders}) AND (slot IS NULL OR slot = '')''',
        _BACKFILL_KINDS,
    ).fetchall()
    for fact_id, content in leftovers:
        if classify_slot(str(content)):
            raise RuntimeError(
                f'v24 迁移自检失败：facts.id={fact_id} 正文可归类却仍为空槽'
            )
