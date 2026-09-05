"""按 ``config/adapter.toml`` 的声明拉起 QQ 适配器进程。

适配器此前由 Electron 的进程监护器拉起，于是「没有图形环境就没有 QQ」。
入口反转到 Python 之后，协议端进程的归属跟着状态所有权走：谁持有数据库和事件账本，
谁负责拉起并收走适配器。本模块只做「解析声明 → 组装命令行」，进程行为由
``src.core.runtime.child_process.ChildProcess`` 提供。

适配器是可选组件：没有声明文件或连接配置时告警并跳过，主体后端与 WebUI 照常运行。
被 ``src.main`` 在监听建立之后调用。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import sys

from src.core.runtime.backend_runtime import runtime_file_path
from src.core.runtime.child_process import ChildProcess
from src.core.logging.logger import get_logger
from src.core.config.adapter_selection import (
    ADAPTER_SELECTION_FILENAME,
    adapter_config_path,
    read_active_adapter,
)

logger = get_logger(__name__)


def adapter_log_tag(plugin_dir: str) -> str:
    """由插件目录名派生转发适配器输出时使用的短标签。

    标签必须跟着实际拉起的那个插件走：写死一个协议端名字，换适配器后就会出现
    「拉起的是 A、日志写着 B」，只能靠翻代码才发现。

    :param plugin_dir: ``adapters/`` 下的插件目录名。
    :return: 去掉固定前后缀后的协议端名字。
    """
    return plugin_dir.removeprefix('yueli-').removesuffix('-adapter')


def build_adapter_process(project_root: Path, config_dir: Path, data_dir: Path) -> ChildProcess | None:
    """按当前声明组装 QQ 适配器子进程；未配置时返回 ``None``。

    :param project_root: 仓库根目录，适配器以 ``python -m`` 方式启动，需要它解析包路径。
    :param config_dir: 主体配置目录，``adapter.toml`` 位于其中。
    :param data_dir: 运行时数据目录，用于定位后端连接凭据文件。
    :return: 可启动的 ``ChildProcess``；缺少适配器声明或连接配置时返回 ``None``。
    :raises ValueError: 声明文件存在但内容非法（缺字段、指向不存在的插件目录）。
        这类错误不跳过：用户确实配了适配器，只是配错了，静默降级会让「QQ 不上线」
        变成一个无处可查的现象。
    :raises tomllib.TOMLDecodeError: 声明文件不是合法 TOML。
    副作用：只读取配置文件，不创建进程。
    """
    selection_path = config_dir / ADAPTER_SELECTION_FILENAME
    if not selection_path.is_file():
        logger.warning(
            'adapter_selection_missing',
            path=str(selection_path),
            hint='没有适配器声明，本次只启动主体后端与 WebUI',
        )
        return None
    plugin_dir = read_active_adapter(config_dir)
    connection_path = adapter_config_path(plugin_dir)
    if not connection_path.is_file():
        logger.warning(
            'adapter_config_missing',
            adapter=plugin_dir,
            path=str(connection_path),
            hint='适配器缺少连接配置，本次不拉起适配器',
        )
        return None

    argv: List[str] = [
        # 用当前解释器而不是 PATH 里的 python：后端跑在虚拟环境里时，PATH 上的那个
        # 解释器往往装不到项目依赖，表现为适配器一起来就 ImportError。
        sys.executable,
        '-m', 'src.platforms.onebot11',
        '--adapter', plugin_dir,
        '--runtime-path', str(runtime_file_path(data_dir)),
    ]
    return ChildProcess(
        name='qq_adapter',
        tag=adapter_log_tag(plugin_dir),
        argv=argv,
        cwd=project_root,
    )


__all__ = ['adapter_log_tag', 'build_adapter_process']
