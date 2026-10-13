"""独立拥有 mcp.toml 的生成、字段对账、完整校验与密钥来源解析。

复用主配置的差异模型与整目录备份；动态服务和工具逐条对账，桌面写入器不参与。
"""

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Literal, Type
from pydantic import BaseModel, ConfigDict, Field, ValidationError
import json
import tomllib

from src.core.config.schema import CONFIG_VERSION, InnerConfig, ProviderCatalog
from src.core.config.upgrade import AddedField, FileDiff, backup_config_directory, diff_document, report_config_changes


class McpToolConfig(BaseModel):
    """显式列入配置的工具授权；expose 仅控制模型可见性，不放宽只读限制。"""

    model_config = ConfigDict(extra='forbid', strict=True)
    expose: bool = Field(True, description='开放给她；关闭后仅供插件调用。')
    readonly: bool = Field(False, description='部署者确认该工具只读。')
    alias: str = Field('', description='模型可见别名，留空使用服务名和工具名。')
    description: str = Field('', description='覆盖服务器说明，建议写明使用时机与隐私边界。')
    daily_limit: int = Field(50, ge=1, le=1000, description='每日调用上限，1～1000 次，重启清零。')


class McpServerConfig(BaseModel):
    """单个服务的连接与工具白名单，所有调用超时范围为 1～120 秒。"""

    model_config = ConfigDict(extra='forbid', strict=True)
    name: str = Field(pattern=r'^[a-z0-9_]{1,20}$', description='唯一服务名，1～20 个小写字母、数字或下划线。')
    enabled: bool = True
    transport: Literal['http', 'stdio'] = 'http'
    url: str = ''
    api_key: str = ''
    api_key_provider: str = ''
    headers: Dict[str, str] = Field(default_factory=dict)
    use_proxy: bool = False
    command: str = ''
    args: List[str] = Field(default_factory=list)
    env: Dict[str, str] = Field(default_factory=dict)
    cwd: str = ''
    call_timeout_seconds: int = Field(15, ge=1, le=120, description='单次工具调用超时，单位秒，1～120。')
    tools: Dict[str, McpToolConfig] = Field(default_factory=dict)


class McpConfig(BaseModel):
    """MCP 总开关与服务列表；没有服务时不连接任何外部程序。"""

    model_config = ConfigDict(extra='forbid', strict=True)
    enabled: bool = True
    servers: List[McpServerConfig] = Field(default_factory=list)


class McpDocument(BaseModel):
    """磁盘文档，版本与主配置共用字段对账机制，但不进入四份主配置写入器。"""

    model_config = ConfigDict(extra='forbid')
    inner: InnerConfig = Field(default_factory=InnerConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)


class McpConfigError(ValueError):
    """整份配置无效；errors 只含字段路径与原因，禁止带 Pydantic 输入值。"""

    def __init__(self, errors: List[str]) -> None:
        """保存所有错误，交由启动入口逐条报告并整体停用 MCP。"""
        self.errors = errors
        super().__init__('；'.join(errors))


def load_provider_keys(directory: Path) -> Dict[str, str]:
    """读取厂商目录的名称与密钥，只供连接时引用，不写入快照或管理页响应。"""
    path = directory / 'providers.toml'
    catalog = ProviderCatalog.model_validate(tomllib.loads(path.read_text(encoding='utf-8')))
    return {provider.name: provider.api_key for provider in catalog.api_providers}


def validate_mcp_config(payload: Dict[str, Any], providers: Dict[str, str]) -> McpConfig:
    """先验证全部字段，再汇总连接关系错误；任一错误拒绝整份文档。

    :param payload: mcp 段，不含 inner。
    :param providers: 已校验的厂商名与密钥映射。
    :return: 完整可用配置，保留 disabled 服务的声明。
    :raises McpConfigError: 结构或交叉字段非法，消息不含凭证原值。
    """
    try:
        config = McpConfig.model_validate(payload)
    except ValidationError as exc:
        raise McpConfigError(['.'.join(map(str, e['loc'])) + '：' + e['msg'] for e in exc.errors(include_input=False)]) from exc
    errors: List[str] = []
    names = set()
    for server in config.servers:
        prefix = f'服务 {server.name} '
        if server.name in names:
            errors.append(prefix + 'name 必须唯一')
        names.add(server.name)
        if server.transport == 'http' and not server.url.startswith(('http://', 'https://')):
            errors.append(prefix + 'url 必须以 http:// 或 https:// 开头')
        if server.transport == 'stdio' and not server.command.strip():
            errors.append(prefix + 'stdio 必须填写 command')
        if server.api_key and server.api_key_provider:
            errors.append(prefix + 'api_key 与 api_key_provider 至多填写一个')
        if server.api_key_provider and server.api_key_provider not in providers:
            errors.append(prefix + 'api_key_provider 引用的厂商不存在')
        if any('{api_key}' in v for v in server.headers.values()) and not (server.api_key or server.api_key_provider):
            errors.append(prefix + '请求头用了 {api_key}，但没有密钥来源')
    if errors:
        raise McpConfigError(errors)
    return config


def _toml_value(value: Any) -> str:
    """编码本模块字段允许的 TOML 标量和内联容器，不接受未知对象。"""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return '[' + ', '.join(_toml_value(v) for v in value) + ']'
    if isinstance(value, dict):
        return '{ ' + ', '.join(f'{_toml_value(k)} = {_toml_value(v)}' for k, v in value.items()) + ' }'
    raise TypeError(f'MCP 配置字段类型无法写入 TOML：{type(value).__name__}')


def render_mcp_config(document: Dict[str, Any]) -> str:
    """按固定模型字段写出带说明的文档；调用方须在保存前完成完整校验。"""
    mcp = document['mcp']
    lines = ['# MCP 工具默认不开放；配置改动保存后重启生效。', '[inner]',
             f'version = {_toml_value(CONFIG_VERSION)}', '', '[mcp]',
             f'enabled = {_toml_value(mcp.get("enabled", True))} # 总开关，默认开启。']
    servers = mcp.get('servers', [])
    if not isinstance(servers, list):
        raise McpConfigError(['mcp.servers 必须是服务表数组'])
    for server in servers:
        lines.extend(['', '[[mcp.servers]]'])
        for key, value in server.items():
            if key != 'tools':
                description = McpServerConfig.model_fields[key].description if key in McpServerConfig.model_fields else None
                lines.append(f'{key} = {_toml_value(value)}' + (f' # {description}' if description else ''))
        for tool, values in server.get('tools', {}).items():
            lines.extend(['', f'[mcp.servers.tools.{_toml_value(tool)}]'])
            for key, value in values.items():
                description = McpToolConfig.model_fields[key].description if key in McpToolConfig.model_fields else None
                lines.append(f'{key} = {_toml_value(value)}' + (f' # {description}' if description else ''))
    return '\n'.join(lines) + '\n'


def mcp_template() -> str:
    """返回不含服务、无密钥的首次安装模板，注释示例不会自动连接。"""
    return render_mcp_config({'mcp': {'enabled': True}}) + '''
# 注释示例：联网搜索增强版，只给插件调用；开放前确认来源与只读性质。
# [[mcp.servers]]
# name = "bailian"
# enabled = true
# transport = "http"
# url = "https://dashscope.aliyuncs.com/api/v1/mcps/EnhancedSearch/mcp"
# api_key_provider = "dashscope"
# headers = { Authorization = "Bearer {api_key}" }
# use_proxy = false
# call_timeout_seconds = 15
# [mcp.servers.tools.search_pro]
# expose = false
# readonly = true
# daily_limit = 3
'''


def _reconcile(raw: Dict[str, Any], model: Type[BaseModel], name: str) -> FileDiff:
    """复用主配置的字段差异判据，原地补缺项与删除退休字段。

    主升级器把服务数组当用户数据；这里逐条取出后对账，不生成虚构服务。
    空字典默认值由本模块编码，因为主升级器的标量编码器不处理字典。
    """
    diff = diff_document(raw, model, name)
    # 空服务列表不写入模板，避免随后追加 [[mcp.servers]] 与 servers = [] 冲突。
    diff.added = [item for item in diff.added if item.path != 'mcp.servers']
    for key, info in model.model_fields.items():
        if key not in raw and not info.is_required():
            default = info.get_default(call_default_factory=True)
            if isinstance(default, dict):
                diff.added.append(AddedField(section='', key=key, value=_toml_value(default)))
    for item in diff.added:
        target = raw
        for part in item.section.split('.') if item.section else ():
            target = target[part]
        target[item.key] = tomllib.loads('value = ' + item.value)['value']
    for removed in diff.removed:
        parts = removed.split('.')
        target = raw
        for part in parts[:-1]:
            target = target[part]
        del target[parts[-1]]
    return diff


def load_mcp_config(directory: Path, *, data_dir: Path, providers: Dict[str, str]) -> McpConfig:
    """首次生成模板，逐服务补缺项并报告退休字段，随后完整校验整份配置。

    :param directory: 配置目录，仅拥有其中的 mcp.toml。
    :param data_dir: 备份目录所在的数据根目录。
    :param providers: 厂商名称映射，供引用合法性检查。
    :return: 校验后的 MCP 总配置。
    :raises McpConfigError: TOML、字段或连接关系非法，宿主须整体停用。
    :raises OSError: 文件或备份失败，不继续写入。
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'mcp.toml'
    if not path.exists():
        path.write_text(mcp_template(), encoding='utf-8')
    try:
        raw = tomllib.loads(path.read_text(encoding='utf-8'))
    except tomllib.TOMLDecodeError as exc:
        raise McpConfigError(['mcp.toml 不是合法 TOML']) from exc
    working = deepcopy(raw)
    if 'inner' in working and not isinstance(working['inner'], dict):
        raise McpConfigError(['inner 必须是版本表'])
    diffs = [_reconcile(working, McpDocument, 'mcp.toml')]
    diffs[0].version_from = raw.get('inner', {}).get('version')
    diffs[0].version_to = CONFIG_VERSION
    mcp = working.get('mcp')
    if not isinstance(mcp, dict):
        raise McpConfigError(['mcp 段必须存在且为表'])
    servers = mcp.get('servers', [])
    if not isinstance(servers, list):
        raise McpConfigError(['mcp.servers 必须是服务表数组'])
    for server in servers:
        if not isinstance(server, dict):
            raise McpConfigError(['mcp.servers 必须是服务表数组'])
        diffs.append(_reconcile(server, McpServerConfig, f'mcp.toml 服务 {server.get("name", "未命名")}'))
        tools = server.get('tools', {})
        if not isinstance(tools, dict):
            raise McpConfigError(['mcp.servers.tools 必须是工具表'])
        for name, tool in tools.items():
            if not isinstance(tool, dict):
                raise McpConfigError([f'工具 {name} 配置必须是表'])
            diffs.append(_reconcile(tool, McpToolConfig, f'mcp.toml 工具 {name}'))
    config = validate_mcp_config(mcp, providers)
    version_changed = working.get('inner', {}).get('version') != CONFIG_VERSION
    if working != raw or version_changed:
        backup_config_directory(directory, data_dir)
        temporary = path.with_suffix('.toml.tmp')
        temporary.write_text(render_mcp_config({'mcp': config.model_dump()}), encoding='utf-8')
        temporary.replace(path)
        report_config_changes(diffs)
    return config
