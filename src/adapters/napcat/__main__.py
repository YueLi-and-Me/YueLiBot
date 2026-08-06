"""`python -m src.adapters.napcat` 入口。"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import argparse
import asyncio
import sys

from src.common.logger import initialize_logging

from .config import load_config
from .runner import NapcatRunner


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='YueLiBot QQ 私聊适配器')
    parser.add_argument(
        '--config-path',
        type=Path,
        default=Path('config/napcat.toml'),
        help='NapCat 连接配置路径',
    )
    parser.add_argument('--backend-port', type=int, required=True, help='主体 Python backend 端口')
    parser.add_argument('--token', required=True, help='主体 Python backend 一次性 token')
    args = parser.parse_args(argv)

    initialize_logging()
    config = load_config(args.config_path)
    try:
        asyncio.run(NapcatRunner(config, args.backend_port, args.token).run())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f'[napcat] 适配器退出：{exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
