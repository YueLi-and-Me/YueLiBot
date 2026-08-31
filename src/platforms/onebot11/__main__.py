"""提供 `python -m src.platforms.onebot11` 的命令行入口，并充当最小插件宿主。

本模块按 ``--adapter`` 选中一个适配器插件目录，加载清单与实现，然后驱动
``on_load`` → ``on_start`` 生命周期；协议端能力的探测与上报挂在运行器的连接序列上，
每次重连都会重新执行，因此不由本模块调度。

停机时先取消 ``on_start`` 任务再调 ``on_stop``：运行器把连接断开视为可重试故障，
顺序反过来会让它在停机过程中重连。

依赖 ``src.plugin_system`` 的加载器与生命周期契约、同包的配置与运行器模块。
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import Sequence

import argparse
import asyncio
import sys

from src.core.common.logger import initialize_logging
from src.plugin_system import AdapterPlugin, load_adapter_plugin


def main(argv: Sequence[str] | None = None) -> int:
    """解析命令行参数并运行 QQ 适配器。

    :param argv: 可选参数序列；为 `None` 时读取进程命令行，默认值为 `None`。
    :return: 正常退出或用户中断返回 `0`，配置/运行错误返回 `1`。
    :raises SystemExit: `argparse` 参数错误时由解析器抛出；适配器加载配置失败时
        由 `load_config` 抛出并在本函数中转为返回码。
    副作用：初始化日志、读取配置与运行时 token，并在校验通过后启动异步适配器。
    """
    parser = argparse.ArgumentParser(description='YueLiBot QQ 适配器宿主')
    parser.add_argument(
        '--adapter',
        default='yueli-napcat-adapter',
        help='适配器插件目录名，位于 adapters/ 下；两个协议端后端互斥，同时只能选一个',
    )
    parser.add_argument(
        '--adapters-dir',
        type=Path,
        default=Path('adapters'),
        help='适配器插件根目录',
    )
    parser.add_argument(
        '--runtime-path',
        type=Path,
        default=Path('data/runtime/backend.json'),
        help='主体 Python backend 运行时信息文件；数据目录可配置，故由宿主注入',
    )
    args = parser.parse_args(argv)

    initialize_logging()
    try:
        # 连接参数读哪个配置段归插件的 on_load：不同适配器读不同段，宿主不替它们
        # 决定。只有运行时信息路径由宿主注入——它随数据目录配置变化，插件的默认值
        # 仅在数据目录取默认位置时成立。
        plugin = load_adapter_plugin(
            args.adapters_dir / args.adapter,
            runtime_path=args.runtime_path,
        )
        asyncio.run(_serve(plugin))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f'[适配器] 退出：{exc}', file=sys.stderr)
        return 1
    return 0


async def _serve(plugin: AdapterPlugin) -> None:
    """驱动一个适配器插件的完整生命周期。

    :param plugin: 已按清单构造、尚未加载的插件实例。
    :return: ``None``；正常情况下直到停机才返回。
    :raises Exception: ``on_load`` 或 ``on_start`` 的不可重试错误原样上抛。
    副作用：读取插件所需配置、建立协议端与主体连接并进入收发循环。
    """
    await plugin.on_load()
    task = asyncio.create_task(plugin.on_start())
    try:
        await task
    finally:
        # 先取消收发任务再关连接。顺序反过来时运行器会把断开当作可重试故障，
        # 在停机过程中重新连上协议端。
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await plugin.on_stop()


if __name__ == '__main__':
    raise SystemExit(main())
