"""解析并严格校验插件清单 ``_manifest.json``。

清单是插件对主体的全部自述：身份、类型，以及该类型额外要求的字段。它在加载期
一次性校验完毕，任何缺失或矛盾都当场抛出——清单错误若拖到运行期，表现形式是「某个
动作永远不生效」，那种现场无法区分是声明写错还是协议端不支持。

依赖 ``capabilities`` 的封闭枚举；被 ``adapter`` 基类持有，被插件宿主用于发现与登记。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, FrozenSet, Literal, Mapping

import json

from .capabilities import AdapterCapability, parse_capabilities


# 当前支持的清单格式版本。宿主与清单必须逐字相等而不是「大于等于」：
# 格式变更意味着字段语义变化，静默接受旧版本会让插件按旧语义运行而无人察觉。
SUPPORTED_MANIFEST_VERSION = 1


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
    """

    plugin_id: str
    plugin_type: PluginType
    name: str
    version: str
    description: str


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
) -> tuple[FrozenSet[AdapterCapability], FrozenSet[AdapterCapability]]:
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
    if version != SUPPORTED_MANIFEST_VERSION:
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
