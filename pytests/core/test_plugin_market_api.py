"""验证市场接口的鉴权、回环限制、响应字段和真实插件状态序列化。

使用内存 ASGI 与 HTTP 传输；插件源码来自测试归档，不访问真实外部服务。
"""

from fastapi import FastAPI
from importlib import import_module

import httpx
import pytest

from src.core.api.state import app_state
from src.core.config.mcp_webui import serialize_plugins
from pytests.core.test_plugin_market import market


@pytest.mark.parametrize('action,payload', [
    ('install', {'source':'测试','id':'test.market','version':'1.0.0'}),
    ('upgrade', {'id':'test.market','version':'2.0.0'}),
    ('uninstall', {'id':'test.market'}),
    ('takeover', {'id':'test.market','version':'1.0.0'}),
])
async def test_market_writes_require_auth_and_loopback(tmp_path, monkeypatch, action, payload):
    """所有写操作在读取插件、索引或调用子进程之前完成两道访问检查。"""
    router = import_module('src.core.api.plugin_market').router
    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr(app_state, 'plugin_market', market(tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app, client=('203.0.113.10',1234)), base_url='http://test') as client:
        assert (await client.post(f'/plugin-market/{action}', json=payload)).status_code == 401
        response = await client.post(f'/plugin-market/{action}', json=payload, headers={'Authorization':'Bearer test-token-fixture'})
        assert response.status_code == 403
    assert not (tmp_path/'plugins').exists()


async def test_market_index_and_operation_response(tmp_path, monkeypatch):
    """实际安装经过服务层，GET 同时展示目录、兼容信息和安装状态。"""
    router = import_module('src.core.api.plugin_market').router
    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr(app_state, 'plugin_market', market(tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app, client=('127.0.0.1',1234)), base_url='http://test', headers={'Authorization':'Bearer test-token-fixture'}) as client:
        response = await client.get('/plugin-market/index?refresh=1')
        assert response.status_code == 200
        raw = response.json()
        assert {'plugins','sources','conflicts','errors','auto_install','backup_root'} <= raw.keys()
        row = raw['plugins'][0]
        assert row['installed']['kind'] == '未装'
        assert {'decision','reason','host_application','yanked','python_dependencies'} <= row['versions'][0].keys()
        response = await client.post('/plugin-market/install', json={'source':'测试','id':'test.market','version':'1.0.0'})
        assert response.status_code == 200
        assert response.json() == dict(ok=True, detail='重启后生效', restart_required=True, dependency_note='')
        row = (await client.get('/plugin-market/index')).json()['plugins'][0]
        assert row['installed'] == dict(kind='市场安装', version='1.0.0', upgrade_to='5.0.0', market_id_available=False)
        response = await client.post('/plugin-market/install', json={'source':'测试','id':'test.market','version':'1.0.0'})
        assert response.status_code == 400 and '已存在' in response.json()['detail']


def test_tools_serializes_market_ownership(tmp_path):
    """收据和手动安装状态由后端读取，新增三个字段始终存在。"""
    ops = market(tmp_path)
    ops.install(source='测试', plugin_id='test.market', version='1.0.0')
    ops.registry.discover([(tmp_path/'plugins', True)])
    row = serialize_plugins(ops.registry, market_ids={'test.market'})['plugins'][0]
    assert row['managed'] is True and row['installed_version'] == '1.0.0'
    assert row['market_id_available'] is False
    (tmp_path/'plugins/test.market/.yueli-install.json').unlink()
    row = serialize_plugins(ops.registry, market_ids={'test.market'})['plugins'][0]
    assert row['managed'] is False and row['market_id_available'] is True
