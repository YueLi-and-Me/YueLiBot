"""QQ 适配器专用配置读取与严格校验。"""

from __future__ import annotations

from pathlib import Path
from typing import List, Literal

import sys

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from src.config.toml_io import read_versioned_toml


NAPCAT_CONFIG_VERSION = '0.1.0'
_CONFIG_HINT = """config/napcat.toml 该怎么填：

  [napcat]
  enabled  = true          改成 true 才会去连 QQ
  self_qq  = "月璃的号"     NapCat 里登录的那个机器人账号
  host     = "127.0.0.1"   NapCat 装在本机就填这个
  port     = 3001          和 NapCat 里那条「正向 WebSocket」的端口一致
  token    = ""            那条连接设了令牌就填一样的，没设就留空

  [owner]
  qq       = "你的号"       你自己平时用的 QQ，不是上面那个机器人号

self_qq 和 owner.qq 是两个不同的号，填反了她会把你当成她自己，你发什么她都不理。
桌宠正常启动时会自动创建这个文件，里面每一项都有说明。"""


class InnerConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')

    version: Literal['0.1.0']


class NapcatConnectionConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')

    # 必填，不给默认值：漏写它就等于让程序替用户决定要不要去连 QQ，
    # 而这个决定的两个方向后果完全不对称。自动生成的模板里明确写着 false。
    enabled: bool
    # 月璃自己的 QQ 号，也就是协议端登录的那个机器人账号。
    # 用来认出「这条消息是她自己发的」，避免她回复自己。
    self_qq: str = ''
    host: str
    port: int = Field(gt=0, le=65535)
    token: str
    reconnect_interval_sec: float = Field(gt=0)
    action_timeout_sec: float = Field(gt=0)

    @field_validator('self_qq', mode='before')
    @classmethod
    def _normalize_self_qq(cls, value: object) -> object:
        # TOML 里写成数字也认，省得为了引号来回折腾。
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
    """私聊名单策略；群聊访问控制不属于本配置。"""

    model_config = ConfigDict(extra='forbid')

    # 默认白名单：配错时最多是朋友没收到回复，不会静默消耗模型额度。
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

    @model_validator(mode='after')
    def _require_two_distinct_qq_numbers(self) -> 'NapcatDocument':
        """启用后必须两个号都填，且不能是同一个。

        填成同一个号是最容易犯的错：配 NapCat 的时候满眼都是机器人账号，
        很容易顺手把它填进 owner.qq。真那样了她会把你当成她自己，
        你发的每条消息都会被当作「自己发的」丢掉，而且不报任何错。
        """
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
    """把 pydantic 的报错压成人能读的几行。

    默认的 ValidationError 会把整份配置和文档链接一起打出来，
    真正有用的那句话反而埋在中间。这里只留字段名和原因。
    """
    if not isinstance(exc, ValidationError):
        return str(exc)
    lines = []
    for error in exc.errors():
        field = '.'.join(str(part) for part in error['loc'])
        message = error['msg'].removeprefix('Value error, ')
        lines.append(f'  {field}：{message}' if field else f'  {message}')
    return '\n'.join(lines)


def load_config(path: Path) -> NapcatDocument:
    """读取适配器配置，错误打印修复指引后以非零状态退出。"""
    try:
        return read_config(path)
    except Exception as exc:
        print(
            f'[napcat] {path} 有问题：\n{_readable_error(exc)}\n\n{_CONFIG_HINT}',
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
