"""管理接口的密钥边界、回环限制与前后端字段契约。"""

from pathlib import Path
from typing import Any, Dict
import json
import subprocess

import pytest

from src.core.mcp.config import McpConfig, McpDocument, McpServerConfig, McpToolConfig, render_mcp_config
from src.core.mcp.manager import McpManager, McpServerStatus, McpToolStatus
from src.core.mcp.snapshot import McpSnapshotChange
from src.core.config.schema import CONFIG_VERSION
from src.plugin_system.registry import PluginRegistry
from pytests.agent.test_plugin_compatibility import write_plugin


@pytest.fixture
def tools_fixture(tmp_path):
    """只构造本地配置与状态，不连接服务、不调用模型。"""
    source = '''from typing import Dict
from pydantic import Field
from src.plugin_system import PluginConfig, ToolPlugin, tool
class Settings(PluginConfig):
    api_key: str = Field('', description='密钥', json_schema_extra={'secret': True})
    headers: Dict[str, str] = Field(default_factory=dict, description='请求头')
    limit: int = Field(3, ge=1, le=10, description='每日上限')
class Example(ToolPlugin):
    config_model = Settings
    def stream_capabilities(self, stream_id):
        """配置有效时可见。"""
        return frozenset({'fixture'})
    @tool(name='example', description='离线工具', capabilities={'fixture'})
    async def execute(self, invocation, context):
        pass
'''
    root = tmp_path / 'plugins'
    directory = write_plugin(root, 'editable', source=source)
    (directory / 'config.toml').write_text('[plugin]\nenabled=true\napi_key="plugin-test-secret"\nheaders={Authorization="Bearer plugin-test-secret"}\nlimit=3\n', encoding='utf-8')
    registry = PluginRegistry()
    registry.discover([(root, False)])
    config_dir = tmp_path / 'config'
    config_dir.mkdir()
    server = McpServerConfig(name='demo', url='http://127.0.0.1:9/mcp', api_key='mcp-test-secret',
        env={'TOKEN': 'env-test-secret'}, headers={'Authorization': 'Bearer {api_key}', 'X-Key': 'literal-header-secret'},
        tools={'echo': McpToolConfig()})
    config = McpConfig(servers=[server])
    (config_dir / 'mcp.toml').write_text(render_mcp_config(McpDocument(mcp=config).model_dump()), encoding='utf-8')
    (config_dir / 'providers.toml').write_text(f'api_providers=[]\n[inner]\nversion="{CONFIG_VERSION}"\n', encoding='utf-8')
    manager = McpManager(config, data_dir=tmp_path, providers={})
    manager._states['demo'] = McpServerStatus('demo', '已连接', '2026-07-28', '测试服务', '1.0.0',
        changes=(McpSnapshotChange('说明变化', 'echo', '旧说明', '新说明'),),
        tools=(McpToolStatus('echo', '回显', '工具说明', {'type': 'object', 'properties': {}}, True,
            True, True, False, '', '', 50, 0, True, '已开放'),))
    return registry, manager, config_dir, directory


def test_star19_read_serializers_hide_all_secret_values(tools_fixture):
    """密钥、环境变量与含密钥的字段都不能随读取响应泄露。"""
    from src.core.config.mcp_webui import serialize_mcp_config, serialize_mcp_status, serialize_plugins
    registry, manager, config_dir, _directory = tools_fixture
    plugins = serialize_plugins(registry)
    config = serialize_mcp_config(manager.config, {})
    body = json.dumps([plugins, config, serialize_mcp_status(manager.statuses())])
    for secret in ('plugin-test-secret', 'mcp-test-secret', 'env-test-secret', 'literal-header-secret'):
        assert secret not in body
    assert plugins['plugins'][0]['values']['api_key'] == ''
    assert plugins['plugins'][0]['secretSet']['api_key'] is True
    assert config['servers'][0]['api_key'] == ''
    assert config['servers'][0]['apiKeySet'] is True
    assert config['servers'][0]['env'] == {'TOKEN': ''}
    assert config['servers'][0]['envSet'] == {'TOKEN': True}


@pytest.mark.parametrize('path', ['/tools/plugins/test.editable/config', '/tools/mcp/config', '/tools/mcp/test'])
async def test_star20_mutations_and_test_require_loopback(path):
    """回环拦截应发生在任何配置或服务读取之前。"""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.core.api.tools_config import _auth, router
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[_auth] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app, client=('192.0.2.4', 1000)), base_url='http://test') as client:
        result = await client.request('POST' if path.endswith('/test') else 'PUT', path, json={})
    assert result.status_code == 403
    assert '回环' in result.json()['detail']


def test_star21_backend_response_trees_match_typescript(tools_fixture, tmp_path):
    """通过 TS 编译器展开引用、数组与动态字典，对照真实序列化结果。"""
    from src.core.config.mcp_webui import serialize_mcp_config, serialize_mcp_status, serialize_plugins, serialize_test_result
    from src.core.mcp.compat import McpServerInfo, McpTool
    registry, manager, _config_dir, _directory = tools_fixture
    responses = {
        'PluginsResponse': serialize_plugins(registry),
        'McpConfigResponse': serialize_mcp_config(manager.config, {'示例厂商': 'provider-secret'}),
        'McpStatusResponse': serialize_mcp_status(manager.statuses()),
        'McpTestResponse': serialize_test_result(McpServerInfo('测试服务', '1', '2026-07-28'),
            (McpTool('echo', '回显', '说明', {'type': 'object'}, True),), ''),
    }
    fixture = tmp_path / 'responses.json'
    fixture.write_text(json.dumps(responses, ensure_ascii=False), encoding='utf-8')
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(['node', str(root / 'tests/tools-fields.cjs'), str(fixture)], cwd=root,
        text=True, encoding='utf-8', capture_output=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'CONTRACT_OK=4' in result.stdout


def test_masked_mcp_save_preserves_secrets_and_rejects_invalid(tools_fixture):
    """空白密钥与 env 沿用磁盘；非法提交整体拒绝，运行实例不热重载。"""
    from src.core.config.mcp_webui import save_mcp_config, serialize_mcp_config
    registry, manager, config_dir, _directory = tools_fixture
    payload = serialize_mcp_config(manager.config, {})
    payload['servers'][0]['tools']['echo']['daily_limit'] = 2
    result = save_mcp_config(config_dir, payload)
    assert result['ok'], result
    import tomllib
    saved = tomllib.loads((config_dir / 'mcp.toml').read_text(encoding='utf-8'))['mcp']['servers'][0]
    assert saved['api_key'] == 'mcp-test-secret'
    assert saved['env']['TOKEN'] == 'env-test-secret'
    assert saved['headers']['X-Key'] == 'literal-header-secret'
    assert saved['tools']['echo']['daily_limit'] == 2
    assert manager.config.servers[0].tools['echo'].daily_limit == 50
    before = (config_dir / 'mcp.toml').read_bytes()
    payload['servers'][0]['tools']['echo']['daily_limit'] = 0
    assert not save_mcp_config(config_dir, payload)['ok']
    assert (config_dir / 'mcp.toml').read_bytes() == before


async def test_reads_require_auth_and_hide_secrets_on_real_routes(tools_fixture, monkeypatch):
    """走实际鉴权依赖与序列化路由，未鉴权不能读取配置。"""
    from types import SimpleNamespace
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.core.api.state import app_state
    from src.core.api.tools_config import _auth, router
    registry, manager, config_dir, _directory = tools_fixture
    monkeypatch.setattr(app_state, 'chat', SimpleNamespace(plugin_registry=registry))
    monkeypatch.setattr(app_state, 'mcp', manager)
    monkeypatch.setattr(app_state, 'config_dir', config_dir)
    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app, client=('127.0.0.1', 1000)), base_url='http://test') as client:
        for path in ('/tools/plugins', '/tools/mcp/config', '/tools/mcp/status'):
            assert (await client.get(path)).status_code == 401
        app.dependency_overrides[_auth] = lambda: None
        for path in ('/tools/plugins', '/tools/mcp/config', '/tools/mcp/status'):
            response = await client.get(path)
            assert response.status_code == 200
            for secret in ('plugin-test-secret', 'mcp-test-secret', 'env-test-secret', 'literal-header-secret'):
                assert secret not in response.text


async def test_temporary_connection_uses_real_sdk_and_never_registers(tools_fixture, monkeypatch):
    """临时测试使用真实 SDK 生命周期，退出后运行管理器与磁盘保持原状。"""
    from mcp import Client
    from src.core.api.state import app_state
    from src.core.api.tools_config import test_mcp
    from src.core.config.mcp_webui import serialize_mcp_config
    from src.core.mcp.compat import sdk
    from pytests.mcp.test_mcp import memory_server
    _registry, manager, config_dir, _directory = tools_fixture
    monkeypatch.setattr(app_state, 'mcp', manager)
    monkeypatch.setattr(app_state, 'config_dir', config_dir)
    server = memory_server()
    monkeypatch.setattr(sdk, '_client_factory', lambda _transport, **kwargs: Client(server, **kwargs))
    before = (config_dir / 'mcp.toml').read_bytes()
    result = await test_mcp(serialize_mcp_config(manager.config, {})['servers'][0])
    assert result['ok'], result
    assert result['protocol_version']
    assert any(tool['name'] == 'echo' for tool in result['tools'])
    assert not manager._connections
    assert (config_dir / 'mcp.toml').read_bytes() == before


async def test_temporary_connection_total_timeout_is_twenty_seconds(tools_fixture, monkeypatch):
    """缩短测试时钟，不实际等待二十秒；断言接口期限及连接取消传递。"""
    import asyncio
    from src.core.api.state import app_state
    from src.core.api import tools_config
    from src.core.config.mcp_webui import serialize_mcp_config
    _registry, manager, config_dir, _directory = tools_fixture
    monkeypatch.setattr(app_state, 'mcp', manager)
    monkeypatch.setattr(app_state, 'config_dir', config_dir)
    cancelled = asyncio.Event()
    async def blocked(*args, **kwargs):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
    deadlines = []
    original = asyncio.timeout
    def clock(seconds):
        deadlines.append(seconds)
        return original(0.01)
    monkeypatch.setattr(tools_config, 'connect', blocked)
    monkeypatch.setattr(tools_config.asyncio, 'timeout', clock)
    result = await tools_config.test_mcp(serialize_mcp_config(manager.config, {})['servers'][0])
    assert not result['ok'] and '20 秒' in result['error']
    assert deadlines == [20] and cancelled.is_set()


def test_disabled_plugin_toggle_keeps_other_fields_without_import(tmp_path):
    """入口即使损坏仍能开关；配置字段与注释不应被管理页清除。"""
    from src.core.config.mcp_webui import save_plugin_settings, serialize_plugins
    directory = write_plugin(tmp_path, 'disabled', source='raise RuntimeError("入口不可执行")')
    (directory / 'config.toml').write_text('[plugin]\n# 保留说明\nenabled=false\nurl="示例地址"\n', encoding='utf-8')
    registry = PluginRegistry()
    registry.discover([(tmp_path, False)])
    assert serialize_plugins(registry)['plugins'][0]['configEditable'] is False
    assert save_plugin_settings(registry, 'test.disabled', {'enabled': True})['ok']
    text = (directory / 'config.toml').read_text(encoding='utf-8')
    assert '示例地址' in text and '# 保留说明' in text


async def test_star23_short_env_config_roundtrips_on_real_routes(tools_fixture, monkeypatch):
    """两服务配置读取不扫描普通字段，原样保存不能损坏 URL、程序参数和说明。"""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.core.api.state import app_state
    from src.core.api.tools_config import _auth, router
    import tomllib
    _registry, manager, config_dir, _directory = tools_fixture
    url = 'https://mcp.example/api/v1/mcps/EnhancedSearch/mcp'
    config = McpConfig(servers=[
        McpServerConfig(name='reader', transport='stdio', command='reader-v1', args=['server-v1.js'],
            env={'DEBUG': '1'}, tools={'read': McpToolConfig(description='read v1 file')}),
        McpServerConfig(name='bailian', url=url),
    ])
    (config_dir / 'mcp.toml').write_text(render_mcp_config(McpDocument(mcp=config).model_dump()), encoding='utf-8')
    monkeypatch.setattr(app_state, 'mcp', manager)
    monkeypatch.setattr(app_state, 'config_dir', config_dir)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[_auth] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app, client=('127.0.0.1', 1000)), base_url='http://test') as client:
        response = await client.get('/tools/mcp/config')
        assert response.status_code == 200
        payload = response.json()
        reader, bailian = payload['servers']
        assert reader['command'] == 'reader-v1'
        assert reader['args'] == ['server-v1.js']
        assert reader['tools']['read']['description'] == 'read v1 file'
        assert reader['env'] == {'DEBUG': ''} and reader['envSet'] == {'DEBUG': True}
        assert bailian['url'] == url
        saved = await client.put('/tools/mcp/config', json=payload)
        assert saved.status_code == 200, saved.text
    disk = tomllib.loads((config_dir / 'mcp.toml').read_text(encoding='utf-8'))['mcp']['servers']
    assert disk[1]['url'] == url
    assert disk[0]['args'] == ['server-v1.js'] and disk[0]['env'] == {'DEBUG': '1'}


async def test_star23_status_preserves_protocol_and_masks_long_credentials(tools_fixture, monkeypatch):
    """状态响应保留短值及模板原文，长环境凭证在工具说明和错误中均被抹除。"""
    from dataclasses import replace
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.core.api.state import app_state
    from src.core.api.tools_config import _auth, router
    _registry, manager, _config_dir, _directory = tools_fixture
    token = 'abcdefgh12345'
    manager.config.servers[0].env = {'DEBUG': '1', 'TOKEN': token}
    state = manager._states['demo']
    manager._states['demo'] = replace(state, protocol_version='2024-11-05', last_error=f'错误 {token}，1 次',
        tools=(replace(state.tools[0], description=f'read v1 file {token} Bearer {{api_key}}'),))
    monkeypatch.setattr(app_state, 'mcp', manager)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[_auth] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/tools/mcp/status')
    assert response.status_code == 200
    status = response.json()['servers'][0]
    assert status['protocol_version'] == '2024-11-05'
    assert status['last_error'] == '错误 [已隐藏]，1 次'
    assert status['tools'][0]['description'] == 'read v1 file [已隐藏] Bearer {api_key}'
    assert token not in response.text


async def test_star23_connection_test_uses_same_credential_rules(tools_fixture, monkeypatch):
    """临时连接同样保留协议和模板原文，只遮长凭证。"""
    from dataclasses import replace
    from mcp import Client
    from src.core.api.state import app_state
    from src.core.api.tools_config import test_mcp
    from src.core.config.mcp_webui import serialize_mcp_config
    from src.core.mcp.compat import sdk
    from pytests.mcp.test_mcp import memory_server
    _registry, manager, config_dir, _directory = tools_fixture
    manager.config.servers[0].env = {'DEBUG': '1', 'TOKEN': 'abcdefgh12345'}
    (config_dir / 'mcp.toml').write_text(render_mcp_config(McpDocument(mcp=manager.config).model_dump()), encoding='utf-8')
    monkeypatch.setattr(app_state, 'mcp', manager)
    monkeypatch.setattr(app_state, 'config_dir', config_dir)
    monkeypatch.setattr(Client, 'protocol_version', property(lambda _self: '2024-11-05'))
    server = memory_server()
    monkeypatch.setattr(sdk, '_client_factory', lambda _transport, **kwargs: Client(server, **kwargs))
    original = sdk.convert_tool
    monkeypatch.setattr(sdk, 'convert_tool', lambda raw: replace(original(raw),
        description='read v1 file abcdefgh12345 Bearer {api_key}'))
    result = await test_mcp(serialize_mcp_config(manager.config, {})['servers'][0])
    assert result['ok'], result
    assert result['protocol_version'] == '2024-11-05'
    assert result['tools'][0]['description'] == 'read v1 file [已隐藏] Bearer {api_key}'
