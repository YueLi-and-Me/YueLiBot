"""定义 QQ 适配器配置模型，并负责版本化 TOML 的读取与校验。

本模块把 `config/napcat.toml` 解析为 Pydantic 模型，统一校验协议端连接、
机器人与 owner 的 QQ 号、私聊访问策略和群聊白名单；`load_config` 负责把
结构化校验错误转换为命令行可读的修复提示。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Literal

import sys

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from src.core.config.toml_io import read_versioned_toml


NAPCAT_CONFIG_VERSION = '0.1.0'
_CONFIG_HINT = (
    'napcat.self_qq 填机器人的号（NapCat 登录的那个），owner.qq 填你自己的号，两个不能一样。'
    '桌宠启动时会自动创建 config/napcat.toml，每项都有注释。'
)


class InnerConfig(BaseModel):
    """描述 NapCat 配置文件的内部版本字段。

    :ivar version: 当前支持的配置版本，固定为 `0.1.0`。
    :raises pydantic.ValidationError: 版本缺失、版本不受支持或包含额外字段。
    """

    model_config = ConfigDict(extra='forbid')

    version: Literal['0.1.0']


class ProtocolConnectionConfig(BaseModel):
    """描述协议端 WebSocket 的连接参数。

    :ivar enabled: 是否启用 QQ 适配器。
    :ivar self_qq: NapCat 实际登录的机器人 QQ 号；允许空字符串表示未启用时不校验。
    :ivar host: 协议端主机名或 IP 地址，默认值由配置文件提供。
    :ivar port: 正向 WebSocket 端口，范围为 1 到 65535。
    :ivar token: 协议端访问令牌，可以为空。
    :ivar reconnect_interval_sec: 重连基础间隔，单位为秒，必须为正数。
    :ivar action_timeout_sec: action 响应超时时间，单位为秒，必须为正数。
    :raises pydantic.ValidationError: 字段类型、端口、主机或 QQ 号不符合约束。
    """

    model_config = ConfigDict(extra='forbid')

    # 该字段必须显式配置，避免程序在用户未确认时自动建立 QQ 连接。
    enabled: bool
    # 机器人登录的 QQ 号，用于识别自身发送的消息。
    self_qq: str = ''
    host: str
    port: int = Field(gt=0, le=65535)
    token: str
    reconnect_interval_sec: float = Field(gt=0)
    action_timeout_sec: float = Field(gt=0)

    @field_validator('self_qq', mode='before')
    @classmethod
    def _normalize_self_qq(cls, value: object) -> object:
        """把 TOML 中的整数 QQ 号转换为字符串后再进行统一校验。

        :param value: 原始配置值，通常为字符串或整数。
        :return: 整数转成的十进制字符串；其他值原样返回给 Pydantic。
        :raises TypeError: 不由本方法主动抛出，非法类型交给模型字段校验处理。
        副作用：不修改传入对象。
        """
        # TOML 数字值统一转换为字符串，使后续校验只处理一种字段类型。
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return value

    @field_validator('self_qq')
    @classmethod
    def _validate_self_qq(cls, value: str) -> str:
        """去除机器人 QQ 号首尾空白并验证其为数字字符串。

        :param value: Pydantic 已转换的机器人 QQ 号。
        :return: 规范化后的数字字符串；允许空字符串表示未配置。
        :raises ValueError: 非空值包含非数字字符。
        副作用：不修改模型状态。
        """
        value = value.strip()
        if value and not value.isdigit():
            raise ValueError('napcat.self_qq 必须是数字 QQ 号')
        return value

    @field_validator('host')
    @classmethod
    def _validate_host(cls, value: str) -> str:
        """规范化协议端主机名并拒绝空白地址。

        :param value: 配置文件中的主机名或 IP 地址。
        :return: 去除首尾空白后的主机名。
        :raises ValueError: 地址为空或只包含空白。
        副作用：不执行 DNS 或网络检查。
        """
        value = value.strip()
        if not value:
            raise ValueError('napcat.host 不能为空')
        return value


class PrivateAccessConfig(BaseModel):
    """私聊名单策略。"""

    model_config = ConfigDict(extra='forbid')

    # 默认使用白名单，未配置名单时不放行普通私聊请求。
    mode: Literal['whitelist', 'blacklist'] = 'whitelist'
    list: List[str] = Field(default_factory=list)

    @field_validator('list', mode='before')
    @classmethod
    def _normalize_numeric_list(cls, value: object) -> object:
        """把访问名单中的整数 QQ 号转换为字符串。

        :param value: 原始名单值；非列表值交给 Pydantic 处理。
        :return: 列表中的整数转为字符串后的新列表，或原始非列表值。
        副作用：不修改传入列表，转换列表时创建新对象。
        """
        if not isinstance(value, list):
            return value
        return [
            str(item) if isinstance(item, int) and not isinstance(item, bool) else item
            for item in value
        ]

    @field_validator('list')
    @classmethod
    def _validate_list(cls, value: List[str]) -> List[str]:
        """规范化并校验私聊访问名单中的 QQ 号。

        :param value: Pydantic 转换后的字符串列表。
        :return: 去除每项首尾空白后的数字 QQ 号列表。
        :raises ValueError: 列表项为空或包含非数字字符。
        副作用：创建并返回新列表，不修改原列表。
        """
        normalized: List[str] = []
        for item in value:
            item = item.strip()
            if not item or not item.isdigit():
                raise ValueError('private.list 必须是数字 QQ 号列表')
            normalized.append(item)
        return normalized

    def allows(self, sender_qq: str, owner_qq: str) -> bool:
        """根据私聊访问策略判断发送者是否可以进入消息处理流程。

        :param sender_qq: 当前消息发送者的 QQ 号；应为已规范化的数字字符串。
        :param owner_qq: 配置的 owner QQ 号；owner 始终具有访问权限。

        :return: owner 请求始终返回 ``True``；其他发送者在白名单模式下仅名单成员
            返回 ``True``，黑名单模式下仅非名单成员返回 ``True``。

        :raises TypeError: 参数不是可比较字符串时抛出。

        副作用：
            仅读取当前策略和名单，不修改配置对象。
        """
        if sender_qq == owner_qq:
            return True
        listed = sender_qq in self.list
        return listed if self.mode == 'whitelist' else not listed


class GroupAccessConfig(BaseModel):
    """群聊白名单；群准入只由群 ID 决定，不接受任何人物豁免。"""

    model_config = ConfigDict(extra='forbid')

    mode: Literal['whitelist'] = 'whitelist'
    list: List[str] = Field(default_factory=list)

    @field_validator('list', mode='before')
    @classmethod
    def _normalize_numeric_list(cls, value: object) -> object:
        """把群聊白名单中的整数群号转换为字符串。

        :param value: 原始名单值；非列表值交给 Pydantic 处理。
        :return: 列表中的整数转为字符串后的新列表，或原始非列表值。
        副作用：不修改传入列表。
        """
        if not isinstance(value, list):
            return value
        return [
            str(item) if isinstance(item, int) and not isinstance(item, bool) else item
            for item in value
        ]

    @field_validator('list')
    @classmethod
    def _validate_list(cls, value: List[str]) -> List[str]:
        """规范化并校验群聊白名单中的群号。

        :param value: Pydantic 转换后的字符串列表。
        :return: 去除每项首尾空白后的数字群号列表。
        :raises ValueError: 列表项为空或包含非数字字符。
        副作用：创建并返回新列表，不修改原列表。
        """
        normalized: List[str] = []
        for item in value:
            item = item.strip()
            if not item or not item.isdigit():
                raise ValueError('group.list 必须是数字 QQ 群号列表')
            normalized.append(item)
        return normalized

    def allows(self, group_qq: str) -> bool:
        """判断群号是否存在于群聊白名单中。

        :param group_qq: 当前消息所属群的 QQ 群号；应为已规范化的数字字符串。

        :return: 群号在 ``list`` 中时返回 ``True``，否则返回 ``False``。

        :raises TypeError: 参数不是可用于成员判断的值时抛出。

        副作用：
            仅读取白名单，不执行 owner 或人物级别的额外豁免判断。
        """
        return group_qq in self.list


class OwnerConfig(BaseModel):
    """保存主体 owner 的 QQ 号，并限制其只能由数字组成。

    :ivar qq: owner QQ 号；默认值为空字符串，启用适配器时由文档级校验禁止为空。
    :raises pydantic.ValidationError: `qq` 非空但包含非数字字符。
    """

    model_config = ConfigDict(extra='forbid')

    qq: str = ''

    @field_validator('qq')
    @classmethod
    def _validate_qq(cls, value: str) -> str:
        """规范化 owner QQ 号并验证其格式。

        :param value: 配置文件中的 owner QQ 号。
        :return: 去除首尾空白后的数字字符串，空字符串保留给未启用配置。
        :raises ValueError: 非空值包含非数字字符。
        副作用：不执行网络或身份查询。
        """
        value = value.strip()
        if not value:
            return value
        if not value.isdigit():
            raise ValueError('owner.qq 必须是数字 QQ 号')
        return value


class AdapterDocument(BaseModel):
    """组合 QQ 适配器所需的全部配置段。

    :ivar inner: 配置版本信息。
    :ivar napcat: 协议端连接参数。
    :ivar owner: 主体 owner 身份。
    :ivar private: 私聊访问策略，默认使用空白名单。
    :ivar group: 群聊访问策略，默认使用空白白名单。
    :raises pydantic.ValidationError: 适配器启用时机器人 QQ 号或 owner QQ 号缺失、
        两者相同，或任一子模型校验失败。
    """

    model_config = ConfigDict(extra='forbid')

    inner: InnerConfig
    napcat: ProtocolConnectionConfig
    owner: OwnerConfig
    private: PrivateAccessConfig = Field(default_factory=PrivateAccessConfig)
    group: GroupAccessConfig = Field(default_factory=GroupAccessConfig)

    @model_validator(mode='after')
    def _require_two_distinct_qq_numbers(self) -> 'AdapterDocument':
        """校验启用适配器时机器人与 owner 使用两个不同的完整 QQ 号。

        :return: 当前已校验的配置模型实例。

        :raises ValueError: 适配器启用但机器人 QQ 号或 owner QQ 号为空，或两者相同。

        副作用：
            仅读取模型字段，不修改配置值。
        """
        if not self.napcat.enabled:
            return self
        if not self.napcat.self_qq:
            raise ValueError('启用 QQ 适配器时 napcat.self_qq 不能为空，填机器人登录的那个 QQ 号')
        if not self.owner.qq:
            raise ValueError('启用 QQ 适配器时 owner.qq 不能为空，填你自己的 QQ 号')
        if self.napcat.self_qq == self.owner.qq:
            raise ValueError(
                f'napcat.self_qq 和 owner.qq 都填成了 {self.owner.qq}，这两个必须是不同的号：'
                'napcat.self_qq 填机器人登录的 QQ 号，owner.qq 填你自己的号'
            )
        return self


def read_config(path: Path) -> AdapterDocument:
    """读取并校验一份完整的 NapCat TOML 配置文件。

    :param path: 配置文件路径；文件必须包含当前支持的版本字段和完整配置结构。

    :return: 校验通过的 ``AdapterDocument`` 实例。

    :raises OSError: 配置文件无法读取时抛出。
    :raises ValueError: 版本字段不匹配或 TOML 结构不合法时抛出。
    :raises pydantic.ValidationError: 配置字段类型、范围或跨字段约束校验失败。
    """
    document = read_versioned_toml(path, NAPCAT_CONFIG_VERSION, _CONFIG_HINT)
    return AdapterDocument.model_validate(document)


def _readable_error(exc: Exception) -> str:
    """将 Pydantic 校验错误格式化为逐字段的中文诊断文本。

    :param exc: 待格式化的异常；非 ``ValidationError`` 按字符串直接转换。

    :return: 每行包含字段路径和错误原因的文本；字段路径为空时仅显示原因。

    :raises TypeError: 异常对象无法转换为字符串时由 ``str`` 操作触发。
    """
    if not isinstance(exc, ValidationError):
        return str(exc)
    lines = []
    for error in exc.errors():
        field = '.'.join(str(part) for part in error['loc'])
        message = error['msg'].removeprefix('Value error, ')
        lines.append(f'  {field}：{message}' if field else f'  {message}')
    return '\n'.join(lines)


def load_config(path: Path) -> AdapterDocument:
    """读取 NapCat 配置；校验失败时输出诊断信息并以状态码 1 终止进程。

    :param path: NapCat TOML 配置文件路径。

    :return: 校验通过的 ``AdapterDocument`` 实例。

    :raises SystemExit: 文件读取、版本解析或字段校验失败时以状态码 ``1`` 退出。
    """
    try:
        return read_config(path)
    except Exception as exc:
        print(
            f'[QQ 适配器] {path} 配置校验失败：\n{_readable_error(exc)}\n\n{_CONFIG_HINT}',
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
