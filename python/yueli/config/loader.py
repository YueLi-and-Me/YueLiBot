"""加载配置，启动时统一校验，字段缺失/类型错立即报错。"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from .schema import Config

_config: Config | None = None


def load_config(path: Path) -> Config:
    """
    从显式路径加载并返回全局配置单例。

    路径必传，不再猜 CWD——同一进程内只加载一次，后续调用直接返回缓存。
    """
    global _config
    if _config is not None:
        return _config

    try:
        with open(path, 'rb') as fh:
            data = tomllib.load(fh)
        _config = Config.model_validate(data)
    except Exception as exc:
        print(f"[yueli] 配置错误，请检查 {path}：\n{exc}", file=sys.stderr)
        sys.exit(1)

    return _config


def get_config() -> Config:
    """获取已加载的配置。须在 load_config(path) 之后调用。"""
    if _config is None:
        raise RuntimeError('配置未初始化，请先调用 load_config(path)')
    return _config


def reset_config() -> None:
    """清空缓存的单例——仅供测试用，让不同测试用例能加载不同的配置文件。"""
    global _config
    _config = None
