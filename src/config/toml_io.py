"""版本化 TOML 读取。主体与 NapCat 适配器共用同一份 [inner] version 校验。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import tomllib


def read_versioned_toml(path: Path, expected_version: str, hint: str) -> Dict[str, Any]:
    """读取 TOML 并校验 [inner] version；版本不符抛 ValueError，hint 是给用户的修复指引。"""
    with open(path, 'rb') as file:
        document = tomllib.load(file)
    # 版本对不上就不要往下解释字段了：同一个 model_tasks 在 1.0.0 里是模型名、
    # 在 1.1.0 里是候选列表，硬读只会给出一堆看不懂的类型错误。
    version = document.get('inner', {}).get('version')
    if version != expected_version:
        raise ValueError(
            f'{path.name} 的配置版本是 {version!r}，当前需要 {expected_version}；{hint}'
        )
    return document
