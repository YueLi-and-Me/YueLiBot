"""工具管理页唯一的响应序列化与磁盘保存边界。

读取不执行插件入口、不建立连接；敏感字段只返回空值和设置标记。
保存先完整校验，再原子替换；运行中的插件和 MCP 管理器等待重启后重新加载。
"""

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from pydantic import ValidationError
import inspect
import tempfile
import tomllib

from src.core.mcp.compat import McpServerInfo, McpTool
from src.core.mcp.compat.types import redact
from src.core.mcp.config import McpConfig, McpConfigError, McpDocument, load_provider_keys, render_mcp_config, validate_mcp_config
from src.core.mcp.manager import McpServerStatus
from src.plugin_system.config import PluginConfig, plugin_config_path, read_enabled_flag, save_plugin_config, save_plugin_enabled, secret_fields
from src.plugin_system.registry import PluginRegistry


def _redact(value: Any, secrets: Sequence[str]) -> Any:
    """递归抹除响应中已知敏感值，包括错误、说明与内联容器中的引用。"""
    if isinstance(value, str):
        return redact(value, tuple(secrets))
    if isinstance(value, dict):
        return {key: _redact(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item, secrets) for item in value]
    return value


def serialize_plugins(registry: PluginRegistry) -> Dict[str, Any]:
    """从状态账本与已导入的配置模型生成插件响应，秘密字段的默认值也不返回。"""
    rows: List[Dict[str, Any]] = []
    secrets: List[str] = []
    for status in registry.statuses():
        manifest = status.manifest
        if manifest is not None and manifest.plugin_type != 'tool':
            continue
        plugin = registry.configuration_plugin(status.directory)
        model = type(plugin).config_model if plugin is not None else PluginConfig
        schema = deepcopy(model.model_json_schema())
        path = plugin_config_path(status.directory)
        values = tomllib.loads(path.read_text(encoding='utf-8'))['plugin'] if path.is_file() else model().model_dump()
        if plugin is None:
            # 停用、版本拒绝或入口损坏时没有配置声明，只编辑开关并保留原文件其余字段。
            values = {'enabled': read_enabled_flag(status.directory) is not False}
        else:
            values = {name: values[name] for name in model.model_fields if name in values}
        flags: Dict[str, bool] = {}
        for name in secret_fields(model):
            value = values.get(name, '')
            flags[name] = bool(value)
            if isinstance(value, str) and value:
                secrets.append(value)
            values[name] = ''
            schema['properties'][name].pop('default', None)
        author = manifest.author if manifest is not None else None
        tools = []
        if plugin is not None:
            visibility = inspect.getdoc(type(plugin).stream_capabilities) or ''
            for spec, _executor in plugin.tools():
                tools.append({'name': spec.name, 'description': spec.description,
                    'capabilities': sorted(spec.capabilities), 'visibility': visibility, 'timeout_ms': spec.timeout_ms})
        rows.append({'directory': str(status.directory), 'id': status.plugin_id, 'name': status.name,
            'version': status.version, 'description': manifest.description if manifest is not None else '',
            'author': author if isinstance(author, str) else author.get('name', '') if author is not None else '',
            'source': status.source, 'min_version': status.host_application.get('min_version', ''),
            'max_version': status.host_application.get('max_version', ''), 'decision': status.decision,
            'reason': status.reason, 'tools': tools, 'config_schema': schema, 'values': values,
            'secretSet': flags, 'configEditable': plugin is not None})
    return _redact({'plugins': rows}, secrets)


def serialize_mcp_config(config: McpConfig, providers: Mapping[str, str]) -> Dict[str, Any]:
    """连接设置保留结构，密钥、env 和非模板请求头只返回设置标记。"""
    result = config.model_dump()
    for row in result['servers']:
        row['original_name'] = row['name']
        row['apiKeySet'] = bool(row['api_key'])
        row['clear_api_key'] = False
        row['api_key'] = ''
        row['envSet'] = {key: bool(value) for key, value in row['env'].items()}
        row['env'] = {key: '' for key in row['env']}
        row['headerSet'] = {key: bool(value) and '{api_key}' not in value for key, value in row['headers'].items()}
        row['headers'] = {key: '' if row['headerSet'][key] else value for key, value in row['headers'].items()}
    result['providers'] = list(providers)
    return result


def serialize_mcp_status(statuses: Sequence[McpServerStatus]) -> Dict[str, Any]:
    """显式定义运行状态的字段树，不把连接对象或内部凭证混入响应。"""
    return {'servers': [{'name': s.name, 'state': s.state, 'protocol_version': s.protocol_version,
        'server_name': s.server_name, 'server_version': s.server_version, 'last_error': s.last_error,
        'changes': [{'kind': c.kind, 'tool': c.tool, 'old_head': c.old_head, 'new_head': c.new_head} for c in s.changes],
        'tools': [{'name': t.name, 'title': t.title, 'description': t.description, 'input_schema': t.input_schema,
            'read_only': t.read_only, 'listed': t.listed, 'expose': t.expose, 'readonly': t.readonly,
            'alias': t.alias, 'override_description': t.override_description, 'daily_limit': t.daily_limit,
            'used_today': t.used_today, 'exposed': t.exposed, 'reason': t.reason} for t in s.tools]} for s in statuses]}


def serialize_test_result(info: Optional[McpServerInfo], tools: Tuple[McpTool, ...], error: str) -> Dict[str, Any]:
    """临时连接与假后端共用同一响应，无工具登记或磁盘保存副作用。"""
    return {'ok': info is not None and not error, 'protocol_version': info.protocol_version if info is not None else '',
        'server_name': info.name if info is not None else '', 'server_version': info.version if info is not None else '',
        'tools': [{'name': t.name, 'title': t.title, 'description': t.description,
            'input_schema': t.input_schema, 'read_only': t.read_only} for t in tools], 'error': error}


def read_mcp_document(directory: Path) -> McpDocument:
    """只读磁盘文档；首次生成和升级由启动流程负责。"""
    path = directory / 'mcp.toml'
    return McpDocument.model_validate(tomllib.loads(path.read_text(encoding='utf-8'))) if path.is_file() else McpDocument()


def prepare_mcp_payload(directory: Path, payload: Dict[str, Any]) -> McpConfig:
    """移除显示标记并恢复留空的旧密钥；删除 env 键或服务仍按提交生效。"""
    candidate = deepcopy(payload)
    candidate.pop('providers', None)
    old = {s.name: s for s in read_mcp_document(directory).mcp.servers}
    servers = candidate.get('servers', [])
    if not isinstance(servers, list) or any(not isinstance(row, dict) for row in servers):
        raise McpConfigError(['servers 必须是服务对象列表'])
    for row in servers:
        previous = old.get(row.pop('original_name', row.get('name', '')))
        clear_key = row.pop('clear_api_key', False)
        if not isinstance(clear_key, bool):
            raise McpConfigError(['clear_api_key 必须是布尔值'])
        for flag in ('apiKeySet', 'envSet', 'headerSet'):
            row.pop(flag, None)
        if previous is not None:
            if row.get('api_key') == '' and not clear_key:
                row['api_key'] = previous.api_key
            for key in ('env', 'headers'):
                values = row.get(key, {})
                if isinstance(values, dict):
                    row[key] = {name: previous.model_dump()[key].get(name, '') if value == '' else value
                        for name, value in values.items()}
    return validate_mcp_config(candidate, load_provider_keys(directory))


def _replace(path: Path, rendered: str) -> None:
    """同目录临时文件保证原子替换，异常向调用者暴露且清理未替换文件。"""
    temporary: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(rendered)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_mcp_config(directory: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
    """完整校验与 TOML 回读均通过后才替换文件，失败不改变原文件。"""
    try:
        config = prepare_mcp_payload(directory, payload)
    except (McpConfigError, ValidationError) as exc:
        detail = str(exc) if isinstance(exc, McpConfigError) else '；'.join(e['msg'] for e in exc.errors(include_input=False))
        return {'ok': False, 'detail': detail}
    document = read_mcp_document(directory).model_dump()
    document['mcp'] = config.model_dump()
    rendered = render_mcp_config(document)
    McpDocument.model_validate(tomllib.loads(rendered))
    _replace(directory / 'mcp.toml', rendered)
    return {'ok': True, 'detail': '已保存配置，重启后端后生效'}


def save_plugin_settings(registry: PluginRegistry, plugin_id: str, values: Dict[str, Any]) -> Dict[str, Any]:
    """目录只从账本取；未导入的插件只更新 enabled，不执行入口也不丢其余配置。"""
    statuses = [s for s in registry.statuses() if s.plugin_id == plugin_id and s.manifest is not None and s.manifest.plugin_type == 'tool']
    if len(statuses) != 1:
        return {'ok': False, 'detail': '插件不存在或 id 重复，无法确定配置目录'}
    directory = statuses[0].directory
    plugin = registry.configuration_plugin(directory)
    if plugin is not None:
        model = type(plugin).config_model
        path = plugin_config_path(directory)
        original = tomllib.loads(path.read_text(encoding='utf-8'))['plugin'] if path.is_file() else {}
        secrets = [original[name] for name in secret_fields(model) if isinstance(original.get(name), str) and original[name]]
        candidate = deepcopy(values)
        # 密钥引用也会被读响应抹除；未编辑的脱敏容器原样恢复，避免保存隐藏占位符。
        for name, value in original.items():
            if name not in secret_fields(model) and name in candidate and candidate[name] == _redact(value, secrets):
                candidate[name] = value
        return save_plugin_config(directory, model, candidate)
    if set(values) != {'enabled'} or not isinstance(values['enabled'], bool):
        return {'ok': False, 'detail': '插件入口尚未导入，只能编辑 enabled 布尔开关'}
    return save_plugin_enabled(directory, values['enabled'])
