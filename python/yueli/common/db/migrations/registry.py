"""
迁移注册表。

每个迁移是一个函数 (db: sqlite3.Connection) -> None，
在单事务内执行，失败自动回滚。

版本号规则：`from_version` 是迁移前的 user_version 整数。
"""

from __future__ import annotations

import sqlite3
from typing import Callable

MigrationFn = Callable[[sqlite3.Connection], None]

# 版本 → 迁移函数的有序映射。manager 按 from_version 升序应用。
_registry: dict[int, MigrationFn] = {}


def register(from_version: int) -> Callable[[MigrationFn], MigrationFn]:
    """装饰器：将迁移函数注册到指定版本。"""
    def decorator(fn: MigrationFn) -> MigrationFn:
        if from_version in _registry:
            raise ValueError(f"迁移版本 {from_version} 已注册：{_registry[from_version].__name__}")
        _registry[from_version] = fn
        return fn
    return decorator


def get_registry() -> dict[int, MigrationFn]:
    return dict(_registry)
