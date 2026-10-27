"""读取并合并插件索引，缓存目录和下载计数，复用宿主的版本兼容判定。

市场操作与管理接口共享 IndexClient；网络仅访问配置中的索引与 stats_url，
下载归档由 download 模块负责。本模块不导入或执行第三方插件入口。
"""

from dataclasses import dataclass, field
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from typing import Any, Dict, List, Literal, Optional, Union
from urllib.parse import urlsplit

import json
import threading
import time

import httpx

from src.core.logging.logger import get_logger
from src.plugin_system.manifest import HostCompatibility, check_host_compatibility, manifest_from_payload

from .config import MarketSource

logger = get_logger(__name__)
# 索引及统计响应的字节上限，单位字节；流式读取时逐块检查。
MAX_INDEX_BYTES = 5 * 1024 * 1024
# 网络超时与进程内缓存有效期，单位秒，均为正数。
INDEX_TIMEOUT_SECONDS = 30
CACHE_SECONDS = 600


def _https(value: str) -> str:
    """验证下载和统计地址有 HTTPS 主机；非法值立即抛出 ValueError。"""
    if urlsplit(value).scheme != 'https' or not urlsplit(value).hostname:
        raise ValueError('地址必须为非空 https:// URL')
    return value


class VersionEntry(BaseModel):
    """固定提交对应的版本条目；大小单位字节，撤回版本不可安装。"""

    model_config = ConfigDict(strict=True)
    version: str = Field(pattern=r'^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$')
    commit: str = Field(pattern=r'^[0-9a-fA-F]{40}$')
    host_application: Dict[str, str]
    python_dependencies: List[str]
    downloads: List[str] = Field(min_length=1)
    sha256: str = Field(pattern=r'^[0-9a-fA-F]{64}$')
    size: int = Field(gt=0)
    published_at: str
    yanked: bool

    @field_validator('downloads')
    @classmethod
    def validate_downloads(cls, values: List[str]) -> List[str]:
        """保持镜像顺序并拒绝非 HTTPS 地址。"""
        return [_https(value) for value in values]

    @field_validator('published_at')
    @classmethod
    def validate_time(cls, value: str) -> str:
        """要求有效的 ISO 时间字符串，原文用于管理页展示。"""
        datetime.fromisoformat(value.replace('Z', '+00:00'))
        return value


class CatalogPlugin(BaseModel):
    """索引中的插件元数据；id 同时用于目录名，因此禁止路径分隔和点路径。"""

    model_config = ConfigDict(strict=True)
    id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    author: Union[str, Dict[str, str]]
    license: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    tags: List[str]
    versions: List[VersionEntry] = Field(min_length=1)


class CatalogIndex(BaseModel):
    """单源索引协议；schema_version 仅接受整数 1，统计接口可省略。"""

    model_config = ConfigDict(strict=True)
    schema_version: Literal[1]
    generated_at: str
    plugins: List[CatalogPlugin]
    stats_url: Optional[str] = None


def _manifest(plugin: CatalogPlugin, version: VersionEntry):
    """把索引条目转换为宿主清单；依赖语法和版本区间只由宿主实现判定。"""
    return manifest_from_payload(dict(manifest_version=2, id=plugin.id, plugin_type='tool', name=plugin.name,
        description=plugin.description, version=version.version, host_application=version.host_application,
        author=plugin.author, license=plugin.license, python_dependencies=version.python_dependencies))


def parse_index(payload: Any) -> CatalogIndex:
    """严格读取单源索引，任何错误包含插件 id 与字段位置。

    :param payload: HTTP JSON 解码得到的对象；不接受缺字段或类型转换。
    :return: 已验证的索引模型。
    :raises ValueError: 协议版本、字段、清单或版本条目非法。
    """
    if not isinstance(payload, dict) or type(payload.get('schema_version')) is not int or payload['schema_version'] != 1:
        raise ValueError('索引 schema_version 必须是整数 1')
    try:
        result = CatalogIndex.model_validate(payload)
    except ValidationError as exc:
        messages = []
        for error in exc.errors(include_input=False):
            location = error['loc']
            identity = '索引'
            raw_plugins = payload.get('plugins')
            if len(location) > 1 and location[0] == 'plugins' and isinstance(raw_plugins, list) and isinstance(location[1], int):
                raw = raw_plugins[location[1]]
                identity = str(raw.get('id', location[1])) if isinstance(raw, dict) else str(location[1])
            messages.append(f'{identity} 字段 {".".join(map(str, location))}：{error["msg"]}')
        raise ValueError('；'.join(messages)) from exc
    if result.stats_url is not None:
        _https(result.stats_url)
    seen = set()
    for plugin in result.plugins:
        if plugin.id in seen:
            raise ValueError(f'{plugin.id} 在同一索引中重复出现')
        seen.add(plugin.id)
        versions = set()
        for version in plugin.versions:
            if version.version in versions:
                raise ValueError(f'{plugin.id} versions.version 重复：{version.version}')
            versions.add(version.version)
            if not all(k in version.host_application for k in ('min_version', 'max_version')):
                raise ValueError(f'{plugin.id} host_application 必须同时声明 min_version 与 max_version')
            try:
                _manifest(plugin, version)
            except ValueError as exc:
                raise ValueError(f'{plugin.id} 清单字段错误：{exc}') from exc
    return result


def compatibility(plugin: CatalogPlugin, version: VersionEntry, *, force: bool) -> HostCompatibility:
    """返回与注册表完全相同的版本判定和原因；force 为宿主强制兼容开关。"""
    return check_host_compatibility(_manifest(plugin, version), third_party=True, force=force)


@dataclass(frozen=True)
class SourcedPlugin:
    """保存优先源提供的插件及可选统计，不跨来源混合版本。"""

    plugin: CatalogPlugin
    source: MarketSource
    downloads_count: Optional[int] = None
    version_downloads: Dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class IndexSnapshot:
    """保存合并结果、来源冲突与逐源错误，失败源不隐藏其他源的有效结果。"""

    plugins: List[SourcedPlugin]
    conflicts: List[Dict[str, str]]
    errors: List[Dict[str, str]]


class IndexClient:
    """共享十分钟的内存缓存；同步网络供后台线程调用，不写磁盘。"""

    def __init__(self, sources: List[MarketSource], *, proxy: str = '', transport: Optional[httpx.BaseTransport] = None) -> None:
        """保存有序源、代理和可选 HTTP 传输；transport 供离线测试注入。"""
        self.sources = sources
        self.proxy = proxy
        self.transport = transport
        self._snapshot: Optional[IndexSnapshot] = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def _json(self, url: str) -> Any:
        """限流式响应体到 MAX_INDEX_BYTES；非 200、网络和 JSON 错误原样抛出。"""
        with httpx.Client(proxy=self.proxy or None, transport=self.transport, timeout=INDEX_TIMEOUT_SECONDS, follow_redirects=True) as client:
            with client.stream('GET', url) as response:
                if response.status_code != 200:
                    raise ValueError(f'HTTP {response.status_code}：{url}')
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_INDEX_BYTES:
                        raise ValueError(f'索引响应超过 {MAX_INDEX_BYTES} 字节上限：{url}')
                return json.loads(data)

    def fetch(self, *, refresh: bool = False) -> IndexSnapshot:
        """按优先级合并源；refresh 强制重新请求，统计失败仅告警不影响目录。

        :return: 当前内存快照；逐源错误包含来源名、地址与原始原因。
        副作用：缓存过期或刷新时访问索引和可选统计地址，同步等待网络。
        """
        with self._lock:
            if not refresh and self._snapshot is not None and time.monotonic() < self._expires:
                return self._snapshot
            merged: Dict[str, SourcedPlugin] = {}
            conflicts, errors = [], []
            for source in self.sources:
                try:
                    index = parse_index(self._json(source.index_url))
                except (ValueError, httpx.HTTPError) as exc:
                    errors.append(dict(source=source.name, index_url=source.index_url, reason=str(exc)))
                    logger.warning('插件索引读取失败', source=source.name, result=str(exc))
                    continue
                stats = {}
                if index.stats_url:
                    try:
                        stats = self._json(index.stats_url)['plugins']
                        if not isinstance(stats, dict):
                            raise ValueError('下载统计 plugins 必须是对象')
                    except (ValueError, KeyError, TypeError, httpx.HTTPError) as exc:
                        logger.warning('插件下载统计读取失败', source=source.name, result=str(exc))
                        stats = {}
                for plugin in index.plugins:
                    if plugin.id in merged:
                        conflicts.append(dict(id=plugin.id, kept=merged[plugin.id].source.name, ignored=source.name))
                        continue
                    item = stats.get(plugin.id, {})
                    total = item.get('total') if isinstance(item, dict) else None
                    versions = item.get('versions', {}) if isinstance(item, dict) else {}
                    valid = type(total) is int and total >= 0 and isinstance(versions, dict) and all(type(v) is int and v >= 0 for v in versions.values())
                    merged[plugin.id] = SourcedPlugin(plugin, source, total if valid else None, versions if valid else {})
            self._snapshot = IndexSnapshot(list(merged.values()), conflicts, errors)
            self._expires = time.monotonic() + CACHE_SECONDS
            return self._snapshot
