"""QQ 适配器专用配置读取与严格校验。"""

from __future__ import annotations

from pathlib import Path
from typing import List, Literal

import sys

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from src.config.toml_io import read_versioned_toml


NAPCAT_CONFIG_VERSION = '0.1.0'
_CONFIG_HINT = (
    'napcat.self_qq 填月璃的号（NapCat 登录的那个），owner.qq 填你自己的号，两个不能一样。'
    '桌宠启动时会自动创建 config/napcat.toml，每项都有注释。'
)


class InnerConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')

    version: Literal['0.1.0']


class NapcatConnectionConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')

    # 必填：漏写就等于让程序替用户决定要不要连 QQ
    enabled: bool
    # 月璃自己的号，用来认出她自己发的消息
    self_qq: str = ''
    host: str
    port: int = Field(gt=0, le=65535)
    token: str
    reconnect_interval_sec: float = Field(gt=0)
    action_timeout_sec: float = Field(gt=0)

    @field_validator('self_qq', mode='before')
    @classmethod
    def _normalize_self_qq(cls, value: object) -> object:
        # 写成数字也认
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return value

    @field_validator('self_qq')
    @classmethod
    def _validate_self_qq(cls, value: str) -> str:
        value = value.strip()
        if value and not value.isdigit():
            raise ValueError('napcat.self_qq 必须是数字 QQ 号')
        return value

    @field_validator('host')
    @classmethod
    def _validate_host(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError('napcat.host 不能为空')
        return value


class PrivateAccessConfig(BaseModel):
    """私聊名单策略。"""

    model_config = ConfigDict(extra='forbid')

    # 默认白名单：配错的后果是朋友没收到回复，而不是谁都能消耗额度
    mode: Literal['whitelist', 'blacklist'] = 'whitelist'
    list: List[str] = Field(default_factory=list)

    @field_validator('list', mode='before')
    @classmethod
    def _normalize_numeric_list(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        return [
            str(item) if isinstance(item, int) and not isinstance(item, bool) else item
            for item in value
        ]

    @field_validator('list')
    @classmethod
    def _validate_list(cls, value: List[str]) -> List[str]:
        normalized: List[str] = []
        for item in value:
            item = item.strip()
            if not item or not item.isdigit():
                raise ValueError('private.list 必须是数字 QQ 号列表')
            normalized.append(item)
        return normalized

    def allows(self, sender_qq: str, owner_qq: str) -> bool:
        """判断私聊是否放行；owner 无论模式和名单内容都自动放行。"""
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
        if not isinstance(value, list):
            return value
        return [
            str(item) if isinstance(item, int) and not isinstance(item, bool) else item
            for item in value
        ]

    @field_validator('list')
    @classmethod
    def _validate_list(cls, value: List[str]) -> List[str]:
        normalized: List[str] = []
        for item in value:
            item = item.strip()
            if not item or not item.isdigit():
                raise ValueError('group.list 必须是数字 QQ 群号列表')
            normalized.append(item)
        return normalized

    def allows(self, group_qq: str) -> bool:
        """仅放行白名单群；调用方无需也不得传入 owner 身份。"""
        return group_qq in self.list


class OwnerConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')

    qq: str = ''

    @field_validator('qq')
    @classmethod
    def _validate_qq(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return value
        if not value.isdigit():
            raise ValueError('owner.qq 必须是数字 QQ 号')
        return value


class NapcatDocument(BaseModel):
    model_config = ConfigDict(extra='forbid')

    inner: InnerConfig
    napcat: NapcatConnectionConfig
    owner: OwnerConfig
    private: PrivateAccessConfig = Field(default_factory=PrivateAccessConfig)
    group: GroupAccessConfig = Field(default_factory=GroupAccessConfig)

    @model_validator(mode='after')
    def _require_two_distinct_qq_numbers(self) -> 'NapcatDocument':
        """校验启用时两个 QQ 号都填了且不相同。"""
        if not self.napcat.enabled:
            return self
        if not self.napcat.self_qq:
            raise ValueError('启用 QQ 适配器时 napcat.self_qq 不能为空，填月璃登录的那个 QQ 号')
        if not self.owner.qq:
            raise ValueError('启用 QQ 适配器时 owner.qq 不能为空，填你自己的 QQ 号')
        if self.napcat.self_qq == self.owner.qq:
            raise ValueError(
                f'napcat.self_qq 和 owner.qq 都填成了 {self.owner.qq}，这两个必须是不同的号：'
                'napcat.self_qq 填月璃登录的机器人号，owner.qq 填你自己的号'
            )
        return self


def read_config(path: Path) -> NapcatDocument:
    """读取并校验一份完整的 napcat.toml。"""
    document = read_versioned_toml(path, NAPCAT_CONFIG_VERSION, _CONFIG_HINT)
    return NapcatDocument.model_validate(document)


def _readable_error(exc: Exception) -> str:
    """把校验错误压成「字段：原因」几行。"""
    if not isinstance(exc, ValidationError):
        return str(exc)
    lines = []
    for error in exc.errors():
        field = '.'.join(str(part) for part in error['loc'])
        message = error['msg'].removeprefix('Value error, ')
        lines.append(f'  {field}：{message}' if field else f'  {message}')
    return '\n'.join(lines)


def load_config(path: Path) -> NapcatDocument:
    """读取配置，失败时打印修复指引并退出。"""
    try:
        return read_config(path)
    except Exception as exc:
        print(
            f'[napcat] {path} 有问题：\n{_readable_error(exc)}\n\n{_CONFIG_HINT}',
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
