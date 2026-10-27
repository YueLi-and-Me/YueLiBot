"""串行执行插件安装、升级、卸载和接管，保护配置与可恢复的历史目录。

管理接口在线程池中调用 MarketOperations；索引、下载、解压和收据各自负责
输入边界，共享依赖模块的 INSTALL_LOCK 保证文件操作与包安装不交错。
"""

from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import os
import re
import shutil
import stat
import time

from src.core.logging.logger import get_logger
from src.plugin_system.dependencies import INSTALL_LOCK, ensure_plugin_dependencies_locked
from src.plugin_system.manifest import load_manifest
from src.plugin_system.registry import PluginRegistry

from .archive import extract_archive
from .download import download_archive
from .index import IndexClient, SourcedPlugin, VersionEntry, compatibility
from .receipt import modified_files, read_receipt, write_receipt

logger = get_logger(__name__)
# 暂存残留保留时间，单位秒；操作开始时只清理早于此阈值的项。
STAGING_MAX_AGE_SECONDS = 3600
# 每个插件最多保留的旧目录数量，正整数；仅清理匹配该插件完整备份名的目录。
BACKUP_LIMIT = 3


def _is_link(path: Path) -> bool:
    """同时识别符号链接和 Windows junction，避免清理操作跟随到边界外。"""
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or (os.name == 'nt' and bool(info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT))


def _remove_owned(path: Path, boundary: Path) -> None:
    """仅删除指定根下的普通条目；链接目录拒绝删除，错误立即暴露。"""
    if _is_link(path) or not path.resolve().is_relative_to(boundary.resolve()):
        raise ValueError(f'市场清理路径不在可删除边界内：{path}')
    if path.is_dir():
        for child in path.rglob('*'):
            if _is_link(child):
                raise ValueError(f'市场清理目录包含链接：{child}')
        shutil.rmtree(path)
    else:
        path.unlink()


class MarketOperations:
    """市场文件操作服务；运行注册表仅用于身份确认，不进行插件热加载。"""

    def __init__(self, *, index: IndexClient, registry: PluginRegistry, plugins_dir: Path, data_dir: Path,
                 auto_install: bool, force: bool = False, proxy: str = '') -> None:
        """保存市场依赖和运行目录；force、auto_install 分别来自宿主及独立配置。"""
        self.index = index
        self.registry = registry
        self.plugins_dir = plugins_dir.absolute()
        self.data_dir = data_dir.absolute()
        self.auto_install = auto_install
        self.force = force
        self.proxy = proxy
        self.staging = self.plugins_dir / '.staging'
        # 备份必须与插件目录位于同一文件系统。
        # - 现象：备份放在数据目录时，`--data-dir` 指向另一块盘或挂载点后，os.replace 报
        #   EXDEV（Windows 为 WinError 17），升级、卸载与接管全部失败。
        # - 原因：跨文件系统不能原子改名，只能复制后删除，中途失败会留下半个插件目录。
        # - 后果：放回数据目录会让自定义数据目录的部署无法再用市场维护插件；点开头的目录
        #   已被插件发现与 installed() 跳过，不会被当成插件加载。
        self.backups = self.plugins_dir / '.backups'

    def _begin(self, plugin_id: str, action: str) -> None:
        """验证插件 id 并清理过期暂存项，必须在共享锁内调用。"""
        if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', plugin_id) is None:
            raise ValueError('插件 id 不能包含路径分隔符或点路径')
        self.staging.mkdir(parents=True, exist_ok=True)
        if _is_link(self.plugins_dir) or _is_link(self.staging):
            raise ValueError('插件根目录和暂存目录不能是链接')
        for path in self.staging.iterdir():
            if time.time() - path.lstat().st_mtime > STAGING_MAX_AGE_SECONDS:
                _remove_owned(path, self.staging)

    @contextmanager
    def _operation(self, plugin_id: str, action: str, version: str = '', source: str = '') -> Iterator[None]:
        """串行化完整操作并记录失败原文，异常继续交给接口处理。"""
        with INSTALL_LOCK:
            try:
                self._begin(plugin_id, action)
                logger.info('插件市场开始操作', plugin=plugin_id, version=version, source=source, action=action, result='开始')
                yield
            except Exception as exc:
                logger.error('插件市场操作失败', plugin=plugin_id, version=version, source=source, action=action, result=str(exc))
                raise

    def installed(self) -> Dict[str, Tuple[Path, str]]:
        """读取磁盘插件身份，合并注册表中的同 id 目录；不执行任何插件代码。"""
        result: Dict[str, Tuple[Path, str]] = {}
        for status in self.registry.statuses():
            if status.plugin_id and status.directory.is_dir():
                result[status.plugin_id] = (status.directory.absolute(), status.version)
        if self.plugins_dir.exists():
            for path in sorted(self.plugins_dir.iterdir()):
                if not path.is_dir() or path.name.startswith('.'):
                    continue
                try:
                    manifest = load_manifest(path / '_manifest.json')
                except (OSError, ValueError):
                    continue
                result.setdefault(manifest.plugin_id, (path, manifest.version))
        return result

    def _selected(self, plugin_id: str, version: str, *, source: str = '', index_url: str = '') -> Tuple[SourcedPlugin, VersionEntry]:
        """从合并索引中选择未撤回且可兼容的版本，来源不匹配时拒绝操作。"""
        for item in self.index.fetch().plugins:
            if item.plugin.id != plugin_id:
                continue
            if (source and item.source.name != source) or (index_url and item.source.index_url != index_url):
                raise ValueError('插件索引来源与已安装收据不符')
            for entry in item.plugin.versions:
                if entry.version != version:
                    continue
                if entry.yanked:
                    raise ValueError(f'插件 {plugin_id} 版本 {version} 已撤回')
                result = compatibility(item.plugin, entry, force=self.force)
                if result.decision == '不兼容':
                    raise ValueError(result.reason)
                logger.info('插件市场版本检查完成', plugin=plugin_id, version=version, source=item.source.name, result=result.decision)
                return item, entry
        raise ValueError(f'索引中没有插件版本：{plugin_id}@{version}')

    def _unpack(self, item: SourcedPlugin, entry: VersionEntry) -> Path:
        """下载并校验版本归档，下载件始终清理，解压目录交由调用方管理。"""
        archive = download_archive(entry, self.staging, proxy=self.proxy, transport=self.index.transport)
        try:
            directory = extract_archive(archive, self.staging, plugin_id=item.plugin.id, version=entry.version)
            logger.info('插件市场下载与解压完成', plugin=item.plugin.id, version=entry.version, source=item.source.name, result='成功')
            return directory
        finally:
            archive.unlink(missing_ok=True)

    def _receipt(self, directory: Path, item: SourcedPlugin, entry: VersionEntry) -> None:
        """记录本次安装的固定提交和逐文件指纹，不包含用户配置。"""
        write_receipt(directory, source=item.source.name, index_url=item.source.index_url, plugin_id=item.plugin.id,
                      version=entry.version, commit=entry.commit, sha256=entry.sha256)

    def _finish(self, plugin_id: str, directory: Path, entry: VersionEntry, source: str) -> Dict[str, Any]:
        """在现有锁内处理依赖；包安装失败保留插件文件并返回手动说明。"""
        notes = ensure_plugin_dependencies_locked([(self.plugins_dir, True)], auto_install=self.auto_install,
                                                   data_dir=self.data_dir, proxy=self.proxy, only=directory)
        logger.info('插件市场操作完成', plugin=plugin_id, version=entry.version, source=source, result='重启后生效')
        return dict(ok=True, detail='重启后生效', restart_required=True, dependency_note=notes.get(directory, ''))

    def install(self, *, source: str, plugin_id: str, version: str) -> Dict[str, Any]:
        """安装未存在的插件；成功后等待重启，冲突或校验失败抛出 ValueError。"""
        with self._operation(plugin_id, '安装', version, source):
            item, entry = self._selected(plugin_id, version, source=source)
            target = self.plugins_dir / plugin_id
            if target.exists() or plugin_id in self.installed():
                raise ValueError(f'插件 {plugin_id} 已存在，不能重复安装')
            directory = self._unpack(item, entry)
            try:
                self._receipt(directory, item, entry)
                os.replace(directory, target)
            finally:
                _remove_owned(directory.parent, self.staging)
            return self._finish(plugin_id, target, entry, item.source.name)

    def _backup_path(self, plugin_id: str, version: str) -> Path:
        """生成不会覆盖已有备份的秒级时间戳路径，目录名仅允许安全版本字符。"""
        if re.fullmatch(r'[A-Za-z0-9._-]+', version) is None:
            raise ValueError('旧插件版本不能用于备份目录名')
        self.backups.mkdir(parents=True, exist_ok=True)
        if _is_link(self.backups):
            raise ValueError('插件备份根目录不能是链接')
        stamp = datetime.now()
        while True:
            path = self.backups / f'{plugin_id}-{version}-{stamp:%Y%m%d-%H%M%S}'
            if not any(item.name.startswith(plugin_id + '-') and item.name.endswith(f'{stamp:%Y%m%d-%H%M%S}') for item in self.backups.iterdir()):
                return path
            stamp += timedelta(seconds=1)

    def _prune(self, plugin_id: str) -> None:
        """按备份名最后的时间字段保留三份，仅清理本插件的完整匹配目录。"""
        pattern = re.compile(re.escape(plugin_id) + r'-[A-Za-z0-9._-]+-([0-9]{8}-[0-9]{6})$')
        matches: List[Tuple[str, Path]] = []
        for path in self.backups.iterdir():
            match = pattern.fullmatch(path.name)
            if match and path.is_dir():
                if _is_link(path):
                    raise ValueError(f'插件备份不能是链接：{path}')
                if load_manifest(path / '_manifest.json').plugin_id != plugin_id:
                    continue
                matches.append((match[1], path))
        for _, path in sorted(matches)[:-BACKUP_LIMIT]:
            _remove_owned(path, self.backups)

    def _replace(self, plugin_id: str, version: str, takeover: bool) -> Dict[str, Any]:
        """持锁替换已安装插件；入口就位前失败恢复旧目录，配置跟随到新版本。"""
        installed = self.installed()
        if plugin_id not in installed:
            raise ValueError(f'插件 {plugin_id} 尚未安装')
        target, old_version = installed[plugin_id]
        if _is_link(target) or not target.resolve().is_relative_to(self.plugins_dir.resolve()):
            raise ValueError('插件目录不能是链接')
        receipt = read_receipt(target)
        if takeover and receipt is not None:
            raise ValueError('插件已由市场管理，无需接管')
        if not takeover and receipt is None:
            raise ValueError('手动安装插件须先交给市场管理')
        if receipt is not None:
            changed = modified_files(target)
            if changed:
                raise ValueError('插件代码已被修改，拒绝覆盖：' + '、'.join(changed))
        item, entry = self._selected(plugin_id, version, index_url=receipt['index_url'] if receipt else '')
        directory = self._unpack(item, entry)
        backup = self._backup_path(plugin_id, old_version)
        moved = False
        try:
            os.replace(target, backup)
            moved = True
            os.replace(directory, target)
            if (backup / 'config.toml').is_file():
                shutil.copy2(backup / 'config.toml', target / 'config.toml')
            self._receipt(target, item, entry)
        except Exception:
            # 现象：旧目录已移走而新目录就位失败会导致插件消失；原因：替换涉及两次改名；
            # 后果：任一文件准备步骤失败都须恢复原目录，不能留下半次升级。
            if moved:
                if target.exists():
                    _remove_owned(target, self.plugins_dir)
                os.replace(backup, target)
            raise
        finally:
            _remove_owned(directory.parent, self.staging)
        self._prune(plugin_id)
        return self._finish(plugin_id, target, entry, item.source.name)

    def upgrade(self, *, plugin_id: str, version: str) -> Dict[str, Any]:
        """升级有收据的插件，保护代码改动并保留配置；返回依赖说明与重启提示。"""
        with self._operation(plugin_id, '升级', version):
            return self._replace(plugin_id, version, False)

    def takeover(self, *, plugin_id: str, version: str) -> Dict[str, Any]:
        """以所选索引版本接管手动插件，原目录进入备份，不进行无依据的指纹比较。"""
        with self._operation(plugin_id, '交给市场管理', version):
            return self._replace(plugin_id, version, True)

    def uninstall(self, *, plugin_id: str) -> Dict[str, Any]:
        """将市场插件连同配置移入备份，不删除其宿主数据目录中的内容。"""
        with self._operation(plugin_id, '卸载'):
            installed = self.installed()
            if plugin_id not in installed:
                raise ValueError(f'插件 {plugin_id} 尚未安装')
            target, version = installed[plugin_id]
            receipt = read_receipt(target)
            if receipt is None:
                raise ValueError('手动安装插件不能直接从市场卸载')
            if _is_link(target) or not target.resolve().is_relative_to(self.plugins_dir.resolve()):
                raise ValueError('插件目录不能是链接')
            backup = self._backup_path(plugin_id, version)
            os.replace(target, backup)
            self._prune(plugin_id)
            logger.info('插件市场卸载完成', plugin=plugin_id, version=version, source=receipt['source'], result=str(backup))
            return dict(ok=True, detail=f'插件已移到 {backup}，重启后生效', restart_required=True, dependency_note='')
