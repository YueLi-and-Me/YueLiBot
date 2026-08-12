"""提供 `python -m src.platforms.napcat` 的命令行入口。

本模块解析配置文件路径和主体运行时信息，初始化日志后启动
`NapcatRunner`；适配器业务逻辑和连接重试由同包的配置、运行器模块负责。
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import argparse
import asyncio
import sys

from src.core.common.logger import initialize_logging
from src.core.common.backend_runtime import read_backend_runtime

from .config import load_config
from .runner import NapcatRunner


def main(argv: Sequence[str] | None = None) -> int:
    """解析命令行参数并运行 QQ 适配器。

    :param argv: 可选参数序列；为 `None` 时读取进程命令行，默认值为 `None`。
    :return: 正常退出或用户中断返回 `0`，配置/运行错误返回 `1`。
    :raises SystemExit: `argparse` 参数错误时由解析器抛出；适配器加载配置失败时
        由 `load_config` 抛出并在本函数中转为返回码。
    副作用：初始化日志、读取配置与运行时 token，并在校验通过后启动异步适配器。
    """
    parser = argparse.ArgumentParser(description='YueLiBot QQ 私聊适配器')
    parser.add_argument(
        '--config-path',
        type=Path,
        default=Path('config/napcat.toml'),
        help='NapCat 连接配置路径',
    )
    parser.add_argument(
        '--runtime-path',
        type=Path,
        default=Path('data/runtime/backend.json'),
        help='主体 Python backend 运行时信息文件',
    )
    args = parser.parse_args(argv)

    initialize_logging()
    try:
        # 先加载适配器配置，再读取主体端口和令牌，避免使用不完整运行时信息建立连接。
        config = load_config(args.config_path)
        runtime = read_backend_runtime(args.runtime_path)
        asyncio.run(NapcatRunner(config, runtime.port, runtime.token).run())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f'[napcat] 适配器退出：{exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
