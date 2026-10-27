"""解析并严格校验插件清单 ``_manifest.json``。

清单是插件对主体的全部自述：身份、类型，以及该类型额外要求的字段。它在加载期
一次性校验完毕，任何缺失或矛盾都当场抛出——清单错误若拖到运行期，表现形式是「某个
动作永远不生效」，那种现场无法区分是声明写错还是协议端不支持。

依赖 ``capabilities`` 的封闭枚举；被 ``adapter`` 基类持有，被插件宿主用于发现与登记。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, FrozenSet, Literal, Mapping, Optional, Set, Tuple, Union

import json
import re

from src.core.app_meta import APP_VERSION
from src.core.logging.logger import get_logger

from .capabilities import AdapterCapability, parse_capabilities

logger = get_logger(__name__)

# 同一插件、同一兼容判定在一个进程内只提示一次，避免多条加载路径重复告警。
_warned_compatibility: Set[Tuple[str, str]] = set()

# 插件门面、清单或上下文最近一次不兼容变化所在的主程序版本，x.y.z 三段。
# 当前版本高于插件声明的 max_version 时：上界不低于此版本的插件按兼容模式加载，
# 低于此版本说明插件按旧门面编写，判为不兼容。只在门面出现不兼容变化的那次发版上调；
# 不随普通发版改动，否则每次升级主程序都会让全部第三方插件失效、逼作者跟着发版。
PLUGIN_INTERFACE_FLOOR = '0.2.0'


@dataclass(frozen=True)
class HostCompatibility:
    """保存宿主版本判定与可直接展示的中文原因；格式校验由清单解析负责。"""

    decision: Literal['正常', '兼容模式', '强制兼容', '不兼容']
    reason: str


def check_host_compatibility(
    manifest: 'PluginManifest', *, third_party: bool, force: bool,
) -> HostCompatibility:
    """判定已解析清单是否支持当前宿主，不执行入口或记录日志。

    :param manifest: 已完成格式校验的插件清单。
    :param third_party: 调用方明确提供的来源标记，不依据路径猜测。
    :param force: 是否允许版本不符或声明缺失的插件应急加载。
    :return: 正常、兼容模式、强制兼容或不兼容及其原因原文。
    """
    bounds = manifest.host_application
    minimum = bounds.get('min_version')
    maximum = bounds.get('max_version')
    current = _version_tuple(APP_VERSION, '宿主版本')
    missing = minimum is None or maximum is None
    if third_party and missing:
        result = HostCompatibility('不兼容',
            f'插件 {manifest.name} 未声明支持的主程序版本区间（min_version 与 max_version 均必填），已跳过')
    elif minimum is not None and current < _version_tuple(minimum, 'min_version'):
        result = HostCompatibility('不兼容',
            f'插件 {manifest.name} 不支持当前版本 {APP_VERSION}（声明支持 {minimum}～{maximum or "不限"}），已跳过')
    elif maximum is not None and current > _version_tuple(maximum, 'max_version'):
        if _version_tuple(maximum, 'max_version') >= _version_tuple(PLUGIN_INTERFACE_FLOOR, '插件接口下限'):
            return HostCompatibility('兼容模式',
                f'插件 {manifest.name} 声明最高支持 {maximum}，当前 {APP_VERSION}，按兼容模式加载')
        result = HostCompatibility('不兼容',
            f'插件 {manifest.name} 按 {PLUGIN_INTERFACE_FLOOR} 之前的插件接口编写（声明支持 '
            f'{minimum or "不限"}～{maximum}），与当前版本 {APP_VERSION} 不兼容，已跳过')
    else:
        return HostCompatibility('正常', '')
    if force:
        declared = '未声明' if missing else f'{minimum}～{maximum}'
        return HostCompatibility('强制兼容',
            f'已开启强制兼容：插件 {manifest.name} 声明 {declared}，当前 {APP_VERSION}，仍然加载')
    return result


def report_host_compatibility(manifest: 'PluginManifest', result: HostCompatibility) -> None:
    """展示版本拒绝原因，兼容模式与强制兼容按插件去重告警。

    :param manifest: 日志关联的插件身份。
    :param result: 纯判定函数的结果；正常判定不输出日志。
    :return: 无；更新进程内告警去重集合。
    """
    if result.decision == '不兼容':
        logger.error(result.reason)
    elif result.decision in ('兼容模式', '强制兼容'):
        key = (manifest.plugin_id, result.decision)
        if key not in _warned_compatibility:
            _warned_compatibility.add(key)
            logger.warning(result.reason)


def require_host_compatibility(manifest: 'PluginManifest', *, third_party: bool, force: bool) -> None:
    """在入口执行前实施版本判定；拒绝时抛出含中文原因的 ManifestError。"""
    result = check_host_compatibility(manifest, third_party=third_party, force=force)
    report_host_compatibility(manifest, result)
    if result.decision == '不兼容':
        raise ManifestError(result.reason)

# 当前支持的清单格式版本。宿主与清单必须逐字相等而不是「大于等于」：
# 格式变更意味着字段语义变化，静默接受旧版本会让插件按旧语义运行而无人察觉。
SUPPORTED_MANIFEST_VERSION = 2


class ManifestError(ValueError):
    """清单结构非法；加载期抛出，不允许带着残缺清单进入运行期。"""


# 插件类型。决定用哪个基类、走哪条发现路径，也决定清单还要求哪些额外字段。
# 适配器互斥且跑在独立进程，由进程入口按名字选中一个；工具插件同进程、可共存，
# 由宿主扫目录全部加载。两条发现路径共用清单格式，但不合并。
PluginType = Literal['adapter', 'tool']
ALL_PLUGIN_TYPES: FrozenSet[PluginType] = frozenset({'adapter', 'tool'})


@dataclass(frozen=True)
class PluginManifest:
    """任何插件都必须自述的公共部分。

    :ivar plugin_id: 全局唯一标识，形如 ``yueli.napcat-adapter``，用于登记与去重。
    :ivar plugin_type: 插件类型，决定基类与发现路径。
    :ivar name: 人类可读名称，出现在日志与控制台。
    :ivar version: 插件自身版本，与主体版本无关。
    :ivar description: 一句话说明这个插件是做什么的。
    :ivar host_application: 版本闭区间；解析校验格式，加载前另行判定当前版本。
    :ivar author: 作者字符串或含 name、可选 url 的只读映射；未声明时为 None。
    :ivar license: SPDX 标识字符串；未声明时为 None，不内置许可证目录。
    :ivar urls: 可选 repository 地址的只读映射。
    :ivar python_dependencies: 索引包的 PEP 508 声明；缺省为空，加载前检查当前环境。
    """

    plugin_id: str
    plugin_type: PluginType
    name: str
    version: str
    description: str
    # 元数据只接受关键字参数，保持 AdapterManifest 原有位置参数的含义不变。
    host_application: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({}), kw_only=True,
    )
    author: Optional[Union[str, Mapping[str, str]]] = field(default=None, kw_only=True)
    license: Optional[str] = field(default=None, kw_only=True)
    urls: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}), kw_only=True)
    python_dependencies: Tuple[str, ...] = field(default=(), kw_only=True)


@dataclass(frozen=True)
class AdapterManifest(PluginManifest):
    """适配器插件在公共部分之外的自述。

    :ivar protocol: 协议族标识，当前只有 ``onebot11``。同族的适配器共用协议实现。
    :ivar config_section: 该适配器读取的配置段名；两个适配器不得相同，否则无法区分
        各自的连接参数。
    :ivar static_capabilities: 连上就一定可用、无需探测的能力。
    :ivar probed_capabilities: 必须实测才能确定的能力，例如依赖协议端可选组件的动作。
    """

    protocol: str = ''
    config_section: str = ''
    static_capabilities: FrozenSet[AdapterCapability] = frozenset()
    probed_capabilities: FrozenSet[AdapterCapability] = frozenset()

    @property
    def declared_capabilities(self) -> FrozenSet[AdapterCapability]:
        """返回清单声明的全部能力，即静态与待探测两部分之并。

        :return: 能力集合；它是能力结算结果的上界，探测不可能扩大这个范围。
        """
        return self.static_capabilities | self.probed_capabilities


def _require_text(payload: Mapping[str, Any], key: str) -> str:
    """取出清单里的一个非空字符串字段。

    :param payload: 清单顶层映射。
    :param key: 字段名。
    :return: 去除首尾空白后的值。
    :raises ManifestError: 字段缺失、类型不对或为空白。
    """
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f'清单缺少非空字符串字段 {key}')
    return value.strip()


def _parse_plugin_type(payload: Mapping[str, Any]) -> PluginType:
    """校验并取出插件类型。

    :param payload: 清单顶层映射。
    :return: 封闭枚举内的插件类型。
    :raises ManifestError: 字段缺失或取值不在枚举内。
    """
    value = _require_text(payload, 'plugin_type')
    if value not in ALL_PLUGIN_TYPES:
        allowed = '、'.join(sorted(ALL_PLUGIN_TYPES))
        raise ManifestError(f'plugin_type 不是合法类型：{value}；可用值为 {allowed}')
    # 上一步已确认取值属于封闭集合，此处窄化安全。
    return value  # type: ignore[return-value]


def _parse_adapter_capabilities(
    payload: Mapping[str, Any],
) -> Tuple[FrozenSet[AdapterCapability], FrozenSet[AdapterCapability]]:
    """解析适配器清单的能力声明。

    :param payload: 清单顶层映射。
    :return: ``(静态能力, 待探测能力)``。
    :raises ManifestError: capabilities 结构非法，或两个列表有交集。
    :raises CapabilityError: 能力标识不在封闭枚举内。
    """
    raw = payload.get('capabilities')
    if not isinstance(raw, Mapping):
        raise ManifestError('清单的 capabilities 必须是对象')
    for key in ('static', 'probed'):
        if not isinstance(raw.get(key, []), list):
            raise ManifestError(f'清单的 capabilities.{key} 必须是数组')
    static = parse_capabilities(raw.get('static', []), 'capabilities.static')
    probed = parse_capabilities(raw.get('probed', []), 'capabilities.probed')
    overlap = static & probed
    if overlap:
        # 同一能力既声明为静态又声明为待探测，说明作者自己也没确定它是否可靠；
        # 放过去只会让探测结果被静态声明覆盖，等于探测白做。
        raise ManifestError(
            f'能力不能同时出现在 static 与 probed：{"、".join(sorted(overlap))}'
        )
    return static, probed


def _version_tuple(value: str, field_name: str) -> Tuple[int, int, int]:
    """把严格的 x.y.z 版本号变成可比较的非负整数三元组。

    :param value: 不带前缀、前导零、预发布或构建后缀的三个十进制分量。
    :param field_name: 错误信息中的字段路径。
    :return: (主版本, 次版本, 补丁版本)。
    :raises ManifestError: 不满足上述格式；不猜测 SemVer 或 PEP 440 后缀的顺序。
    宿主版本契约仅允许三段整数；依赖库使用的 PEP 440 语法不改变此契约。
    """
    if re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', value) is None:
        raise ManifestError(f'{field_name} 只支持 x.y.z 三段非负整数版本号，实际为 {value!r}')
    major, minor, patch = value.split('.')
    return int(major), int(minor), int(patch)


def _optional_text_map(payload: Mapping[str, Any], key: str) -> Mapping[str, str]:
    """复制可选元数据对象为只读字符串映射，拒绝显式的空值或非法字段类型。

    :param payload: 清单顶层映射。
    :param key: host_application、author 或 urls。
    :return: 缺失时为空映射，存在时各字段为非空字符串。
    :raises ManifestError: 对象或其字段类型不符。
    """
    raw = payload.get(key, {})
    if not isinstance(raw, Mapping):
        raise ManifestError(f'清单的 {key} 必须是对象')
    result: Dict[str, str] = {}
    for name in raw:
        try:
            result[name] = _require_text(raw, name)
        except ManifestError as exc:
            raise ManifestError(f'清单的 {key}.{name} 必须是非空字符串') from exc
    return MappingProxyType(result)


def _host_compatibility(payload: Mapping[str, Any]) -> Mapping[str, str]:
    """只校验宿主版本闭区间格式，当前宿主版本由加载入口另行比较。

    :param payload: 可选含 host_application 的清单映射。
    :return: 已复制且不可变的版本声明。
    :raises ManifestError: 版本格式非法或上下界倒置。
    """
    bounds = _optional_text_map(payload, 'host_application')
    minimum = (
        _version_tuple(bounds['min_version'], 'host_application.min_version')
        if 'min_version' in bounds else None
    )
    maximum = (
        _version_tuple(bounds['max_version'], 'host_application.max_version')
        if 'max_version' in bounds else None
    )
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ManifestError('host_application 版本区间非法：min_version 高于 max_version')
    return bounds


def _parse_author(payload: Mapping[str, Any]) -> Optional[Union[str, Mapping[str, str]]]:
    """校验两种作者声明，避免原始可变 JSON 对象进入清单。

    :param payload: 清单顶层映射，author 可缺失。
    :return: 作者字符串或含 name、可选 url 的只读映射；未声明时为 None。
    :raises ManifestError: 作者不是非空字符串或合法作者对象。
    """
    if 'author' not in payload:
        return None
    if isinstance(payload['author'], str):
        return _require_text(payload, 'author')
    author = _optional_text_map(payload, 'author')
    if 'name' not in author:
        raise ManifestError('清单的 author.name 必须是非空字符串')
    return author


def _parse_python_dependencies(payload: Mapping[str, Any]) -> Tuple[str, ...]:
    """解析可选的索引包声明，保留原文供安装与错误提示使用。

    :param payload: 清单映射；未声明依赖时返回空元组。
    :return: 按声明顺序保存的 PEP 508 字符串。
    :raises ManifestError: 非数组、空项、非法写法、URL 或规范化后的重复包名。
    """
    raw = payload.get('python_dependencies', [])
    if not isinstance(raw, list):
        raise ManifestError(f'python_dependencies 必须是数组，实际为 {raw!r}')
    names: Set[str] = set()
    for position, value in enumerate(raw):
        location = f'python_dependencies[{position}]'
        if not isinstance(value, str) or not value.strip():
            raise ManifestError(f'{location} 必须是非空字符串，实际为 {value!r}')
        try:
            requirement = Requirement(value)
        except InvalidRequirement as exc:
            raise ManifestError(f'{location} 不是合法 PEP 508 依赖：{value!r}；{exc}') from exc
        if requirement.url:
            raise ManifestError(f'{location} 只接受索引上发布的包，不接受 URL 依赖：{value!r}')
        name = canonicalize_name(requirement.name)
        if name in names:
            raise ManifestError(f'{location} 重复声明包名：{value!r}')
        names.add(name)
    return tuple(raw)


def manifest_from_payload(payload: Mapping[str, Any]) -> PluginManifest:
    """把已解析的清单映射校验并转换为对应类型的清单对象。

    按 ``plugin_type`` 分派：适配器额外要求协议、配置段与能力声明；工具插件的
    能力写在各条 ToolSpec 上，清单里不重复声明。

    :param payload: 已解析的清单顶层映射。
    :return: 适配器返回 :class:`AdapterManifest`，其余返回 :class:`PluginManifest`。
    :raises ManifestError: 版本不符、必填字段缺失、类型非法或能力声明矛盾。
    :raises CapabilityError: 能力标识不在封闭枚举内。
    """
    version = payload.get('manifest_version')
    if type(version) is not int or version != SUPPORTED_MANIFEST_VERSION:
        raise ManifestError(
            f'清单格式版本必须是 {SUPPORTED_MANIFEST_VERSION}，实际为 {version!r}'
        )

    plugin_type = _parse_plugin_type(payload)
    common = {
        'plugin_id': _require_text(payload, 'id'),
        'plugin_type': plugin_type,
        'name': _require_text(payload, 'name'),
        'version': _require_text(payload, 'version'),
        'description': _require_text(payload, 'description'),
        'host_application': _host_compatibility(payload),
        'author': _parse_author(payload),
        'license': _require_text(payload, 'license') if 'license' in payload else None,
        'urls': _optional_text_map(payload, 'urls'),
        'python_dependencies': _parse_python_dependencies(payload),
    }
    if plugin_type != 'adapter':
        return PluginManifest(**common)

    static, probed = _parse_adapter_capabilities(payload)
    return AdapterManifest(
        **common,
        protocol=_require_text(payload, 'protocol'),
        config_section=_require_text(payload, 'config_section'),
        static_capabilities=static,
        probed_capabilities=probed,
    )


def load_manifest(path: Path) -> PluginManifest:
    """从磁盘读取并校验一份 ``_manifest.json``。

    :param path: 清单文件路径。
    :return: 校验通过的清单。
    :raises ManifestError: 文件不存在、不是合法 JSON 对象，或内容校验失败。
    :raises CapabilityError: 能力标识不在封闭枚举内。
    副作用：读取一次磁盘文件。
    """
    if not path.is_file():
        raise ManifestError(f'清单文件不存在：{path}')
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise ManifestError(f'清单不是合法 JSON：{path}（{exc.msg}）') from exc
    if not isinstance(payload, dict):
        raise ManifestError(f'清单顶层必须是对象：{path}')
    return manifest_from_payload(payload)
