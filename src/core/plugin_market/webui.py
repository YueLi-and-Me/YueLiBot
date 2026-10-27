"""把市场索引和磁盘安装状态序列化为管理页响应，不渲染第三方 HTML。

API 与本机预览共用本模块，版本判定复用 index，升级提示复用 updates。
"""

from packaging.version import Version
from typing import Any, Dict

from .config import OFFICIAL_INDEX_URL
from .index import compatibility
from .operations import MarketOperations
from .receipt import read_receipt
from .updates import available_update


def serialize_market(operations: MarketOperations, *, refresh: bool = False) -> Dict[str, Any]:
    """组合来源、兼容性、安装所有权和可升级版本，供接口线程或预览生成器调用。

    :param operations: 当前宿主的市场服务，包含注册表、缓存、配置与数据路径。
    :param refresh: 是否强制刷新网络索引；默认使用十分钟缓存。
    :return: 可直接编码为 JSON 的市场快照，错误来源和冲突独立展示。
    :raises ValueError: 已安装收据损坏；不伪装成未安装。
    """
    snapshot = operations.index.fetch(refresh=refresh)
    installed = operations.installed()
    plugins = []
    for item in snapshot.plugins:
        plugin = item.plugin
        versions = []
        for entry in sorted(plugin.versions, key=lambda value: Version(value.version), reverse=True):
            verdict = compatibility(plugin, entry, force=operations.force)
            versions.append(dict(**entry.model_dump(), decision=verdict.decision, reason=verdict.reason,
                                 downloads_count=item.version_downloads.get(entry.version)))
        installation = dict(kind='未装', version='', upgrade_to=None, market_id_available=False)
        if plugin.id in installed:
            directory, version = installed[plugin.id]
            receipt = read_receipt(directory)
            managed = receipt is not None
            upgrade = available_update(item, version, force=operations.force) if managed and receipt['index_url'] == item.source.index_url else None
            installation = dict(kind='市场安装' if managed else '手动安装', version=version,
                                upgrade_to=upgrade, market_id_available=not managed)
        plugins.append(dict(id=plugin.id, name=plugin.name, description=plugin.description,
                            author=plugin.author if isinstance(plugin.author, str) else plugin.author['name'],
                            license=plugin.license, repository=plugin.repository, tags=plugin.tags,
                            source=item.source.model_dump(), versions=versions, installed=installation,
                            downloads_count=item.downloads_count,
                            store_url=f'https://plugins.yuelibot.org/plugins/{plugin.id}/' if item.source.index_url == OFFICIAL_INDEX_URL else None))
    return dict(plugins=plugins, sources=[source.model_dump() for source in operations.index.sources],
                conflicts=snapshot.conflicts, errors=snapshot.errors, auto_install=operations.auto_install,
                backup_root=str(operations.backups))
