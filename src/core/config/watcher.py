"""监测四份主体 TOML 的内容变化，稳定保存后自动执行配置热重载。

由主程序注册生命周期；复用 loader 的完整校验和服务通知，不另建一条应用路径。
连续两次观测到相同的新内容才重载，避免编辑器分段写入和多文件保存的中间态。
非法配置会完整记录错误，修正文件后自动重新校验；相同的错误内容只尝试一次。
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import List, Optional, Tuple

import asyncio

from .bootstrap import MAIN_CONFIG_FILES
from .loader import reload_config
from .schema import Config
from src.core.logging.console_layout import print_box
from src.core.logging.logger import get_logger

logger = get_logger(__name__)

ConfigSignature = Tuple[Optional[bytes], ...]


class ConfigWatcher:
    """按文件内容检测变化，不依赖编辑器是否保留修改时间或使用原子替换。"""

    def __init__(self, directory: Path, *, interval_s: float = 1.0) -> None:
        """保存监测目录与轮询间隔，不启动后台任务。

        :param directory: 已完成启动校验的主体配置目录。
        :param interval_s: 扫描间隔，单位秒，必须大于零；稳定两轮后应用。
        :raises ValueError: 间隔不大于零。
        副作用：读取文件内容摘要；不修改配置或运行服务。
        """
        if interval_s <= 0:
            raise ValueError('配置监测间隔必须大于零')
        self._directory = directory
        self._interval_s = interval_s
        self._seen = self._signature()
        self._attempted = self._seen
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None

    def _signature(self) -> ConfigSignature:
        """读取四份文件的内容摘要；暂时缺失也作为一种明确的变更状态。"""
        signatures: List[Optional[bytes]] = []
        for name in MAIN_CONFIG_FILES:
            try:
                content = (self._directory / name).read_bytes()
            except FileNotFoundError:
                signatures.append(None)
            else:
                signatures.append(sha256(content).digest())
        return tuple(signatures)

    async def check_once(self) -> Optional[Tuple[Config, List[str]]]:
        """扫描一次，稳定的新内容走完整重载；校验或服务应用错误直接抛出。

        :return: 实际重载时返回新配置与变更清单；内容未稳定或未变化时返回 None。
        副作用：有效变更通知运行服务；不写文件，失败内容修正后才重新尝试。
        """
        signature = await asyncio.to_thread(self._signature)
        if signature != self._seen:
            self._seen = signature
            return None
        if signature == self._attempted:
            return None
        self._attempted = signature
        return await asyncio.to_thread(reload_config)

    async def startup(self) -> None:
        """启动唯一的监测任务；重复启动直接报错。"""
        if self._task is not None:
            raise RuntimeError('配置监测已经启动')
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name='config-watch')
        logger.info('config_watch_started', intervalSeconds=self._interval_s)

    async def shutdown(self) -> None:
        """停止扫描并等待在途重载完成，避免服务关闭后仍回写其配置引用。"""
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self) -> None:
        """周期扫描并完整记录重载失败；修正文件后继续处理后续变更。"""
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_s)
            except TimeoutError:
                try:
                    result = await self.check_once()
                    if result is not None:
                        _, summary = result
                        print_box('配置自动重载完成', summary if summary else ['没有业务字段变化。'])
                except Exception:
                    logger.exception('config_reload_failed')
