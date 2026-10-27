"""启动后在后台检查市场插件是否可升级，仅提示，不下载包或自动升级。

组合根将本服务登记到生命周期；目录与状态来自 IndexClient 和安装收据。
"""

from packaging.version import Version
from typing import Optional

import asyncio

from src.core.logging.logger import get_logger

from .index import SourcedPlugin, compatibility
from .operations import MarketOperations
from .receipt import read_receipt

logger = get_logger(__name__)


def available_update(item: SourcedPlugin, installed_version: str, *, force: bool) -> Optional[str]:
    """返回高于当前版本的最新可安装版本；撤回或不兼容条目均不参与选择。"""
    candidates = [entry.version for entry in item.plugin.versions if not entry.yanked
                  and Version(entry.version) > Version(installed_version)
                  and compatibility(item.plugin, entry, force=force).decision != '不兼容']
    return max(candidates, key=Version) if candidates else None


class MarketUpdateNotifier:
    """一次性后台检查，不占用启动等待链，不创建轮询或自动升级任务。"""

    def __init__(self, operations: MarketOperations) -> None:
        """保存市场服务引用；构造时不访问网络或文件系统。"""
        self.operations = operations
        self._task: Optional[asyncio.Task] = None

    async def startup(self) -> None:
        """创建后台任务后立即返回，实际同步网络和磁盘读取在线程中执行。"""
        self._task = asyncio.create_task(asyncio.to_thread(self._check), name='plugin-market-updates')

    async def shutdown(self) -> None:
        """取消未完成的后台等待并取回异常；HTTP 自身受三十秒超时约束。"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _check(self) -> None:
        """只比较有收据且来源一致的插件；拉取与读取失败仅记录 warning。"""
        try:
            snapshot = self.operations.index.fetch()
            installed = self.operations.installed()
            updates = []
            for item in snapshot.plugins:
                if item.plugin.id not in installed:
                    continue
                directory, version = installed[item.plugin.id]
                receipt = read_receipt(directory)
                if receipt is None or receipt['index_url'] != item.source.index_url:
                    continue
                target = available_update(item, version, force=self.operations.force)
                if target is not None:
                    updates.append(f'{item.plugin.id} → {target}')
            if updates:
                logger.info('插件有可升级版本', plugins=updates)
        except Exception as exc:
            logger.warning('启动时检查插件升级失败', reason=str(exc))
