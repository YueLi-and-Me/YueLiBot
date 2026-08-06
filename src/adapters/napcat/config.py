"""QQ 适配器专用配置读取与严格校验。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import sys

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.config.toml_io import read_versioned_toml


NAPCAT_CONFIG_VERSION = '0.1.0'
_CONFIG_HINT = (
    '桌宠托管启动时会自动创建停用模板；若手动运行，请创建 config/napcat.toml，'
    '按 M3 QQ 私聊接入说明填写 enabled、host、port、owner.qq，'
    '协议端没有 token 时也必须保留 token = ""'
)


class InnerConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')

    version: Literal['0.1.0']


class NapcatConnectionConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')

    # 旧版手写配置没有这个字段时保持原有连接行为；自动生成的模板会明确写 false。
    enabled: bool = True
    host: str
    port: int = Field(gt=0, le=65535)
    token: str
    reconnect_interval_sec: float = Field(gt=0)
    action_timeout_sec: float = Field(gt=0)

    @field_validator('host')
    @classmethod
    def _validate_host(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError('napcat.host 不能为空')
        return value


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

    @model_validator(mode='after')
    def _require_owner_when_enabled(self) -> 'NapcatDocument':
        if self.napcat.enabled and not self.owner.qq:
            raise ValueError('启用 QQ 适配器时 owner.qq 不能为空')
        return self


def read_config(path: Path) -> NapcatDocument:
    """读取并校验一份完整的 napcat.toml。"""
    document = read_versioned_toml(path, NAPCAT_CONFIG_VERSION, _CONFIG_HINT)
    return NapcatDocument.model_validate(document)


def load_config(path: Path) -> NapcatDocument:
    """读取适配器配置，错误打印修复指引后以非零状态退出。"""
    try:
        return read_config(path)
    except Exception as exc:
        print(f'[napcat] 配置错误：{path}\n{exc}\n{_CONFIG_HINT}', file=sys.stderr)
        raise SystemExit(1) from exc
