"""按桌宠开关决定是否拉起 Electron 桌面外壳，并解析它的启动命令行。

入口反转之后 Python 是进程入口，桌面外壳退化成一个可选的前端：
``bot.toml`` 的 ``[desktop_pet] enabled = false`` 时本模块不产出任何进程，后端、
WebUI 与 QQ 适配器照常运行——这正是无头部署所需要的形态，服务器上不该为了拉起一个
Python 子进程而先装一套图形环境。

外壳的启动形态有两种，按仓库里实际存在的东西判定，不做猜测：

- 开发外壳：``node_modules/electron-vite`` 在场时走 ``npm run dev``，源码改动即时生效。
- 构建外壳：只有构建产物 ``out/main/index.js`` 时，直接用本地 Electron 可执行文件。

被 ``src.main`` 在监听建立之后调用；进程行为由
``src.core.common.child_process.ChildProcess`` 提供。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

import shutil
import sys

from src.core.common.child_process import ChildProcess
from src.core.common.logger import get_logger

logger = get_logger(__name__)

# 开发外壳的判据与命令：electron-vite 在场即认为这是一份开发签出。
DEV_SHELL_MARKER = Path('node_modules') / 'electron-vite' / 'package.json'
DEV_SHELL_SCRIPT = 'dev'
# 构建外壳的判据与可执行文件：package.json 的 main 指向前者，Electron 以仓库根为应用目录。
BUILT_SHELL_ENTRY = Path('out') / 'main' / 'index.js'
ELECTRON_BINARY = Path('node_modules') / '.bin' / ('electron.cmd' if sys.platform == 'win32' else 'electron')


def resolve_shell_command(project_root: Path) -> List[str]:
    """给出拉起桌面外壳的完整命令行。

    开发外壳优先于构建外壳：``out/`` 是 gitignore 的构建产物，可能早已落后于源码，
    优先用它会让人对着一个旧界面排查新代码的问题。两者都不可用时直接报错，不静默
    跳过——用户明确把桌宠开着，拉不起来必须当场知道原因。

    :param project_root: 仓库根目录。
    :return: 可直接交给 ``ChildProcess`` 的命令行。
    :raises RuntimeError: 既没有开发依赖也没有构建产物，或缺少 npm / Electron 可执行文件。
    副作用：只做文件存在性判断，不创建进程。
    """
    if (project_root / DEV_SHELL_MARKER).is_file():
        npm = shutil.which('npm')
        if npm is None:
            raise RuntimeError(
                '桌宠已开启，但 PATH 里找不到 npm，无法启动开发外壳。'
                '安装 Node.js 后重试，或执行 npm run build 改用构建外壳。'
            )
        return [npm, 'run', DEV_SHELL_SCRIPT]

    if (project_root / BUILT_SHELL_ENTRY).is_file():
        electron = project_root / ELECTRON_BINARY
        if not electron.is_file():
            raise RuntimeError(
                f'桌宠已开启，构建产物在 {BUILT_SHELL_ENTRY} 但找不到 {electron}。'
                '先执行 npm install 安装 Electron。'
            )
        # 参数 "." 让 Electron 以仓库根为应用目录，入口取 package.json 的 main。
        return [str(electron), '.']

    raise RuntimeError(
        '桌宠已开启，但既没有 node_modules（开发外壳）也没有 out/main/index.js（构建外壳）。'
        '执行 npm install 后用 npm run dev，或执行 npm run build 生成构建产物；'
        '若本机不需要桌宠，把 bot.toml 的 [desktop_pet] enabled 置为 false。'
    )


def build_desktop_shell_process(
    enabled: bool,
    project_root: Path,
    data_dir: Path,
    config_dir: Path,
) -> ChildProcess | None:
    """按桌宠开关组装桌面外壳子进程；关闭时返回 ``None``。

    :param enabled: ``bot.toml`` 的 ``[desktop_pet] enabled``。
    :param project_root: 仓库根目录，作为外壳的工作目录与应用目录。
    :param data_dir: 后端实际使用的数据目录，注入给外壳以避免两侧各自推导。
    :param config_dir: 后端实际使用的配置目录，注入理由同上。
    :return: 可启动的 ``ChildProcess``；桌宠关闭时返回 ``None``。
    :raises RuntimeError: 桌宠开启但外壳启动方式无法解析。
    副作用：输出一行说明本次是否拉起外壳的日志；不创建进程。
    """
    if not enabled:
        logger.info(
            'desktop_shell_disabled',
            hint='bot.toml 的 [desktop_pet] enabled 为 false，本次不拉起桌面外壳',
        )
        return None

    command = resolve_shell_command(project_root)
    env: Dict[str, str] = {
        # 两侧各自推导目录会在后端用了自定义 --data-dir / --config-path 时分叉：
        # 数据目录分叉出两个 memory.db（双方都「工作正常」，只是记忆对不上），
        # 配置目录分叉则让 [desktop_pet] enabled 各读一份。这里由后端单向注入。
        'YUELI_PROJECT_ROOT': str(project_root),
        'YUELI_DATA_DIR': str(data_dir.resolve()),
        'YUELI_CONFIG_DIR': str(config_dir.resolve()),
        # 告诉外壳它是被后端拉起的：托盘的「退出」因此代表退出整个应用，
        # 而不是只关掉一个连上了别人后端的客户端。
        'YUELI_SHELL_MANAGED': '1',
    }
    logger.info('desktop_shell_launching', command=' '.join(command))
    return ChildProcess(
        name='desktop_shell',
        tag='shell',
        argv=command,
        cwd=project_root,
        env=env,
    )


__all__ = ['build_desktop_shell_process', 'resolve_shell_command']
