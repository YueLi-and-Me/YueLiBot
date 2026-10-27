"""管理独立的 plugin_market.toml，不参与四份主配置的整文件写入。

启动入口读取本模块的配置模型；bootstrap 复用模板生成首次配置和公开示例。
字段对账仅补缺项，保留用户注释、已有值和残留键，并完整验证已知字段。
"""

from ipaddress import ip_address
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Any, Dict, List
from urllib.parse import urlsplit

import tomllib

from src.core.config.upgrade import AddedField, apply_added_fields
from src.core.logging.logger import get_logger

logger = get_logger(__name__)
# 官方索引地址；只用于首次生成配置，已有 sources 不被重写。
OFFICIAL_INDEX_URL = 'https://plugins.yuelibot.org/index.json'


class MarketSource(BaseModel):
    """单个索引源；name 为非空显示名，index_url 为 HTTPS 或回环 HTTP 地址。"""

    model_config = ConfigDict(extra='ignore', strict=True)
    name: str = Field(min_length=1, description='索引源显示名称，非空字符串。')
    index_url: str = Field(description='索引文件地址，仅允许 HTTPS，回环调试允许 HTTP。')

    @field_validator('name')
    @classmethod
    def validate_name(cls, value: str) -> str:
        """拒绝仅含空白的名称，合法名称保留部署者原文。"""
        if not value.strip():
            raise ValueError('索引源 name 不能为空白')
        return value

    @field_validator('index_url')
    @classmethod
    def validate_url(cls, value: str) -> str:
        """限制索引传输协议；不解析 DNS，避免把外部域名误判为回环地址。"""
        parsed = urlsplit(value)
        if parsed.hostname and parsed.scheme == 'https':
            return value
        loopback = parsed.hostname == 'localhost'
        if parsed.hostname and not loopback:
            try:
                loopback = ip_address(parsed.hostname).is_loopback
            except ValueError:
                loopback = False
        if parsed.scheme == 'http' and loopback:
            return value
        raise ValueError('index_url 只接受 https://，回环地址允许 http://')


class DependencyConfig(BaseModel):
    """控制是否补装插件库；关闭时仍检查并提供受约束的手动命令。"""

    model_config = ConfigDict(extra='ignore', strict=True)
    auto_install: bool = Field(True, description='是否自动安装缺失 Python 库，默认 true。')


class PluginMarketConfig(BaseModel):
    """索引源按列表顺序优先；空列表关闭市场索引访问，不关闭依赖检查。"""

    model_config = ConfigDict(extra='ignore', strict=True)
    sources: List[MarketSource] = Field(default_factory=lambda: [MarketSource(name='月璃官方', index_url=OFFICIAL_INDEX_URL)])
    dependencies: DependencyConfig = Field(default_factory=DependencyConfig)


def plugin_market_template() -> str:
    """返回无本机值的默认 TOML，启动生成与 config.example 使用同一份文本。"""
    return '''# 插件市场的索引源与依赖安装方式。本文件由月璃生成，可以直接编辑。

[[sources]]
# 索引源名称，显示在插件市场页上
name = "月璃官方"
# 索引文件地址；可以添加多个索引源，同一插件出现在多个源时按顺序取前者
index_url = "https://plugins.yuelibot.org/index.json"

[dependencies]
# 是否由月璃自动安装插件声明的 Python 库（装进月璃自己的运行环境，只新增、不改动已有库的版本）；
# 关闭后只检查，缺什么按提示手动安装
auto_install = true
'''


def _report_unknown(raw: Dict[str, Any], fields: List[str], prefix: str) -> None:
    """报告当前层残留键；prefix 标识字段位置，保留磁盘原文供部署者自行处理。"""
    for key in raw.keys() - set(fields):
        logger.warning('插件市场配置存在残留字段，已保留原文', field=f'{prefix}{key}')


def load_plugin_market_config(directory: Path) -> PluginMarketConfig:
    """生成缺失配置，校验后仅追加缺失字段，残留键只报告。

    :param directory: 配置根目录，仅访问其中的 plugin_market.toml。
    :return: 已完成严格类型及地址校验的市场配置。
    :raises ValueError: TOML、字段类型、地址非法或对账后无法解析。
    :raises OSError: 读写失败，不掩盖真实配置错误。
    副作用：首次生成文件，后续对账保留已有值和注释。
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'plugin_market.toml'
    if not path.exists():
        path.write_text(plugin_market_template(), encoding='utf-8')
    raw = tomllib.loads(path.read_text(encoding='utf-8'))
    config = PluginMarketConfig.model_validate(raw)
    _report_unknown(raw, list(PluginMarketConfig.model_fields), '')
    for position, source in enumerate(raw.get('sources', [])):
        _report_unknown(source, list(MarketSource.model_fields), f'sources[{position}].')
    _report_unknown(raw.get('dependencies', {}), list(DependencyConfig.model_fields), 'dependencies.')
    added = []
    if 'sources' not in raw:
        added.append(AddedField(section='', key='sources', value='[{ name = "月璃官方", index_url = "' + OFFICIAL_INDEX_URL + '" }]'))
    if 'auto_install' not in raw.get('dependencies', {}):
        added.append(AddedField(section='dependencies', key='auto_install', value='true'))
    if added:
        if not apply_added_fields(path, added):
            raise ValueError('plugin_market.toml 缺失字段无法追加，配置保持原样')
        logger.info('插件市场配置已补齐缺失字段', fields=[item.path for item in added])
    return config
