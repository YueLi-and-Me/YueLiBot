"""读取并校验主体与 QQ 适配器共用的版本化 TOML 文件。

调用方提供期望版本和面向用户的修复提示；版本不匹配时在解析业务字段前直接失败。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import tomllib


def read_versioned_toml(path: Path, expected_version: str, hint: str) -> Dict[str, Any]:
    """读取 TOML 并校验 `[inner].version`。

    :param path: TOML 文件路径。
    :param expected_version: 当前代码支持的配置版本。
    :param hint: 版本不匹配时附加给用户的修复指引。
    :return: 解析后的顶层字典。
    :raises OSError: 文件无法打开时抛出。
    :raises tomllib.TOMLDecodeError: 文件内容不是合法 TOML。
    :raises ValueError: 顶层缺少匹配的 `[inner].version`。
    副作用：只读取文件，不写入配置。
    """
    with open(path, 'rb') as file:
        document = tomllib.load(file)
    # 版本对不上就不要往下解释字段了：同一个 model_tasks 在 1.0.0 里是模型名、
    # 版本不匹配时停止解析字段，避免用旧结构解释新字段并产生误导性的类型错误。
    version = document.get('inner', {}).get('version')
    if version != expected_version:
        raise ValueError(
            f'{path.name} 的配置版本是 {version!r}，当前需要 {expected_version}；{hint}'
        )
    return document
