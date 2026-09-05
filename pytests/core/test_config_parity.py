"""配置写入器对齐校验的 pytest 包装。

真正的校验逻辑在仓库内的 scripts/check/config_parity.py（提交物）；
这里只是把它挂进 pytest，让「schema 加字段忘了同步 TS 模板」
在正常测试跑里就红灯。脚本不在当前检出状态时跳过——
例如在尚未合入该脚本的工作树上。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PARITY_SCRIPT = REPO_ROOT / 'scripts' / 'check' / 'config_parity.py'


def test_config_writer_matches_python_schema() -> None:
    """Electron 模板产出的字段集必须与 schema.py 完全一致。"""
    if not PARITY_SCRIPT.exists():
        pytest.skip('scripts/check/config_parity.py 不在当前检出中（对齐校验尚未合入）')
    result = subprocess.run(
        [sys.executable, str(PARITY_SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f'配置写入器与 schema 漂移：\n{result.stdout}\n{result.stderr}'
    )
