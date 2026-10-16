"""v35 -> v36：表情包新增画面描述列，并清洗混入标签的模型控制符号。

- ``emoji.description``：画面描述（角色、外观、物件、图上文字），供按内容查找表情包。
  迁移只建列，存量行为空串，由启动后的后台任务逐张识别补写。
- 标签清洗：部分视觉模型把 ``<|end_of_box|>``、``</think>`` 等控制符号写进输出，
  旧的标签规范化不识别它们，生产库有 101 行带着这类后缀入库。清洗后这些行的向量
  按带符号的标签算出，一并置空，由启动后的表情包向量补算重写；补算完成前这些行
  不参与按情绪的向量检索。
"""

from __future__ import annotations

from typing import List

import re
import sqlite3

from .registry import register

from src.core.logging.logger import get_logger

logger = get_logger(__name__)

FROM_VERSION = 35
# 清洗口径冻结在迁移内，不随 chat_image 的现行实现变化。
_THINK_BLOCK = re.compile(r'<think>.*?</think>', re.DOTALL)
_MODEL_MARKUP = re.compile(r'<\|[^|>]*\|>|</?think>')


def _columns(db: sqlite3.Connection, table: str) -> List[str]:
    """读取表的列名；表不存在时返回空列表。"""
    return [row[0] for row in db.execute('SELECT name FROM pragma_table_info(?)', (table,))]


def _clean_tags(tags: str) -> str:
    """去掉控制符号后按逗号重新拼接标签，保持原有顺序并去重。

    :raises RuntimeError: 清洗后一个标签都不剩；标签列不允许为空，必须人工处理。
    """
    cleaned = _MODEL_MARKUP.sub('', _THINK_BLOCK.sub('', tags))
    parts: List[str] = []
    for part in cleaned.split(','):
        tag = part.strip()
        if tag and tag not in parts:
            parts.append(tag)
    if not parts:
        raise RuntimeError(f'v35→v36 迁移：标签「{tags}」清洗后为空')
    return ','.join(parts)


@register(FROM_VERSION)
def migrate(db: sqlite3.Connection) -> None:
    """为 emoji 表补 description 列，清洗带控制符号的标签并置空其向量。"""
    columns = _columns(db, 'emoji')
    # 最小旧库可能尚未创建表情包表，缺表交给链尾当前 DDL 创建。
    if not columns:
        return
    if 'description' not in columns:
        db.execute("ALTER TABLE emoji ADD COLUMN description TEXT NOT NULL DEFAULT ''")
    cleaned = 0
    for content_hash, tags in db.execute(
        "SELECT hash, emotion_tags FROM emoji WHERE emotion_tags LIKE '%<%'"
    ).fetchall():
        new_tags = _clean_tags(str(tags))
        if new_tags == tags:
            continue
        db.execute(
            'UPDATE emoji SET emotion_tags = ?, emotion_vec = NULL WHERE hash = ?',
            (new_tags, content_hash),
        )
        cleaned += 1
    logger.info('v35_to_v36_done', cleanedTags=cleaned)
