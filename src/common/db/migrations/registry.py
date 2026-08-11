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
    """返回把迁移函数注册到指定起始版本的装饰器。

    :param from_version: 迁移开始前的 `user_version` 整数。
    :return: 接受迁移函数并返回原函数的装饰器。
    :raises ValueError: 目标版本已经存在注册函数。
    :side_effects: 调用返回的装饰器时修改模块级迁移注册表。
    """
    def decorator(fn: MigrationFn) -> MigrationFn:
        """登记一个迁移函数并保持其原始调用签名。

        :param fn: 接受 SQLite 连接并在当前事务中执行迁移的函数。
        :return: 原样返回 `fn`，供函数定义继续绑定。
        :raises ValueError: 外层指定的版本已经注册。
        :side_effects: 修改模块级 `_registry`。
        """
        if from_version in _registry:
            raise ValueError(f"迁移版本 {from_version} 已注册：{_registry[from_version].__name__}")
        _registry[from_version] = fn
        return fn
    return decorator


def get_registry() -> dict[int, MigrationFn]:
    """返回当前迁移注册表的浅复制。

    :return: 起始版本到迁移函数的字典副本。
    :side_effects: 不修改内部注册表，调用方修改返回值不会影响全局注册。
    """
    return dict(_registry)
