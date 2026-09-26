"""登记向量空间来源，供启动装配识别模型、协议、维度或配方变化。

只创建 vector_space，不对旧向量作来源推断；首次接管由运行时装配登记。
重放遇到已有表直接返回，自检仅针对本次执行的 DDL。
"""

import sqlite3

from .registry import register

FROM_VERSION = 32


@register(FROM_VERSION)
def migrate(db: sqlite3.Connection) -> None:
    """创建空间登记表；已有表保持原样，DDL 失败向迁移管理器传播。"""
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='vector_space'").fetchone():
        return
    db.execute('''CREATE TABLE vector_space (
        consumer TEXT PRIMARY KEY,
        model TEXT NOT NULL,
        api_format TEXT NOT NULL,
        dim INTEGER NOT NULL,
        recipe TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    )''')
    columns = {row[1] for row in db.execute('PRAGMA table_info(vector_space)')}
    if columns != {'consumer', 'model', 'api_format', 'dim', 'recipe', 'updated_at'}:
        raise RuntimeError('向量空间登记表迁移自检失败')
