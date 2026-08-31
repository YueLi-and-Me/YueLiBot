"""定义适配器能力的封闭枚举。

能力描述的是「协议端实际能不能做这件事」，与「用户想不想开」是两回事：后者仍由
``config/bot.toml`` 的开关表达，两者是与的关系——能力不可用时，配置开着也不进动作集。

本模块只有数据定义与校验，不含任何协议调用；被 ``manifest`` 用于清单校验、被
``adapter`` 用于结算最终能力集合、被聊天服务用于收窄动作集。
"""

from __future__ import annotations

from typing import FrozenSet, Iterable, Literal


# 适配器能力标识。取值封闭：新增能力必须同时改这里和 ALL_CAPABILITIES，
# 让「协议端支持什么」这件事只有一处判据。
AdapterCapability = Literal[
    'send_message',
    'quote_reply',
    'reaction',
    'poke',
    'forward_message',
    'group_history',
    'member_info',
]

# 全部合法能力标识。清单校验据此拒绝拼错的名字——拼错若被放过，
# 表现为该能力永远不可用，而现场看不出是拼写问题还是协议端不支持。
ALL_CAPABILITIES: FrozenSet[AdapterCapability] = frozenset({
    'send_message',
    'quote_reply',
    'reaction',
    'poke',
    'forward_message',
    'group_history',
    'member_info',
})


class CapabilityError(ValueError):
    """能力标识非法；清单加载期即抛出，不允许带着错误标识进入运行期。"""


def parse_capabilities(
    values: Iterable[object],
    label: str,
) -> FrozenSet[AdapterCapability]:
    """把清单里的一组能力标识校验并转换为封闭集合。

    :param values: 清单中的原始标识序列，元素类型未经校验。
    :param label: 出错信息里用于定位的字段名，例如 ``capabilities.static``。
    :return: 去重后的能力集合；输入为空时返回空集合。
    :raises CapabilityError: 元素不是字符串、为空白、重复出现，或不在
        :data:`ALL_CAPABILITIES` 内。
    """
    parsed: set[AdapterCapability] = set()
    for index, value in enumerate(values):
        if not isinstance(value, str):
            raise CapabilityError(f'{label}[{index}] 必须是字符串')
        name = value.strip()
        if not name:
            raise CapabilityError(f'{label}[{index}] 不能为空')
        if name not in ALL_CAPABILITIES:
            allowed = '、'.join(sorted(ALL_CAPABILITIES))
            raise CapabilityError(f'{label}[{index}] 不是合法能力：{name}；可用值为 {allowed}')
        if name in parsed:
            raise CapabilityError(f'{label} 出现重复能力：{name}')
        # 上一步已确认 name 属于封闭集合，此处的窄化是安全的。
        parsed.add(name)  # type: ignore[arg-type]
    return frozenset(parsed)
