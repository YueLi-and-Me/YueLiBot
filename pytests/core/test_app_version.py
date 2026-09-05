"""应用版本三处一致的机检（任务书 B-1）。

单一来源是 src/core/app_meta.py 的 ``APP_VERSION``；pyproject.toml 与
package.json 的 ``version`` 字段必须与它相等。改动任何一处而不同步其余
两处，都会让本文件红灯。
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from src.core.app_meta import APP_VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]


def _pyproject_version() -> str:
    """读取 pyproject.toml 声明的项目版本。"""
    with open(REPO_ROOT / 'pyproject.toml', 'rb') as file:
        document = tomllib.load(file)
    return document['project']['version']


def _package_json_version() -> str:
    """读取 package.json 声明的应用版本。"""
    with open(REPO_ROOT / 'package.json', 'rb') as file:
        document = json.load(file)
    return document['version']


def test_single_source_matches_pyproject() -> None:
    assert _pyproject_version() == APP_VERSION, (
        'pyproject.toml 的 version 与 src/core/app_meta.py 的 APP_VERSION 漂移；'
        '应用版本的单一来源是 app_meta.py，先改它再同步本文件'
    )


def test_single_source_matches_package_json() -> None:
    assert _package_json_version() == APP_VERSION, (
        'package.json 的 version 与 src/core/app_meta.py 的 APP_VERSION 漂移；'
        '应用版本的单一来源是 app_meta.py，先改它再同步本文件'
    )
