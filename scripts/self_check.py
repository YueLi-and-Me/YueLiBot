"""主动执行 YueLiBot 的只读运行时健康检查。"""

from __future__ import annotations

from pathlib import Path
from typing import List

import argparse

from src.core.runtime.self_check import render_report, run_self_check


def main(argv: List[str] | None = None) -> int:
    """解析可选路径并把自检健康结论作为进程退出码返回。"""
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description='只读检查 YueLiBot 迁移、数据库、向量、配置和后台积压',
    )
    parser.add_argument(
        '--database',
        type=Path,
        default=project_root / 'data' / 'memory.db',
        help='SQLite 数据库路径（默认：项目 data/memory.db）',
    )
    parser.add_argument(
        '--config-dir',
        type=Path,
        default=project_root / 'config',
        help='主体配置目录（默认：项目 config）',
    )
    parser.add_argument(
        '--adapters-dir',
        type=Path,
        default=project_root / 'adapters',
        help='适配器插件根目录（默认：项目 adapters）',
    )
    args = parser.parse_args(argv)

    report = run_self_check(
        args.database,
        args.config_dir,
        args.adapters_dir,
    )
    print(render_report(report), flush=True)
    return report.exit_code


if __name__ == '__main__':
    raise SystemExit(main())
