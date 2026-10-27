"""工具与 MCP 管理接口，读操作鉴权，写入和临时建连进一步限制为回环访问。"""

from ipaddress import ip_address
from pathlib import Path
from typing import Any, Dict
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request
from pydantic import ValidationError
import asyncio

from src.core.config import mcp_webui
from src.core.mcp.compat import McpFailure, connect, connection_secrets
from src.core.mcp.config import McpConfigError, load_provider_keys
from src.plugin_system.registry import PluginRegistry

from .auth import SESSION_COOKIE_NAME, require_token
from .state import app_state

router = APIRouter(prefix='/tools', tags=['tools'])


def _auth(authorization: str | None = Header(default=None),
          session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME)) -> None:
    """复用后端的 Bearer 与会话 Cookie 鉴权。"""
    require_token(authorization, session_token)


def _require_loopback(request: Request) -> None:
    """测试连接可能启动本机程序，必须在读取配置和建连前拒绝非回环请求。"""
    try:
        allowed = request.client is not None and ip_address(request.client.host).is_loopback
    except ValueError:
        allowed = False
    if not allowed:
        raise HTTPException(status_code=403, detail='工具与 MCP 编辑及测试连接仅允许从本机回环地址访问')


def _directory() -> Path:
    """启动流程未注入配置目录时明确返回不可用。"""
    if app_state.config_dir is None:
        raise HTTPException(status_code=503, detail='配置目录尚未注入')
    return Path(app_state.config_dir)


def _plugins() -> PluginRegistry:
    """取得真实聊天服务的注册表，不重新发现插件。"""
    if app_state.chat is None:
        raise HTTPException(status_code=503, detail='聊天服务尚未启动')
    return app_state.chat.plugin_registry


@router.get('/plugins', dependencies=[Depends(_auth)])
async def plugins() -> Dict[str, Any]:
    """读取全部工具插件的清单、状态和已导入的配置声明。"""
    market_ids = set()
    if app_state.plugin_market is not None:
        snapshot = await asyncio.to_thread(app_state.plugin_market.index.fetch)
        market_ids = {item.plugin.id for item in snapshot.plugins}
    return await asyncio.to_thread(mcp_webui.serialize_plugins, _plugins(), market_ids=market_ids)


@router.put('/plugins/{plugin_id}/config', dependencies=[Depends(_auth), Depends(_require_loopback)])
async def save_plugin(plugin_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """按账本定位插件目录，完整校验并保存，重启后生效。"""
    result = mcp_webui.save_plugin_settings(_plugins(), plugin_id, payload)
    if not result['ok']:
        raise HTTPException(status_code=400, detail=result['detail'])
    return {'ok': True, 'detail': '已保存配置，重启后端后生效'}


@router.get('/mcp/config', dependencies=[Depends(_auth)])
async def mcp_config() -> Dict[str, Any]:
    """只读磁盘快照并屏蔽密钥和环境变量值。"""
    directory = _directory()
    return mcp_webui.serialize_mcp_config(mcp_webui.read_mcp_document(directory).mcp, load_provider_keys(directory))


@router.put('/mcp/config', dependencies=[Depends(_auth), Depends(_require_loopback)])
async def save_mcp(payload: Dict[str, Any]) -> Dict[str, Any]:
    """整份保存 MCP 设置，运行实例不热重载。"""
    result = mcp_webui.save_mcp_config(_directory(), payload)
    if not result['ok']:
        raise HTTPException(status_code=400, detail=result['detail'])
    return result


@router.get('/mcp/status', dependencies=[Depends(_auth)])
async def mcp_status() -> Dict[str, Any]:
    """取得连接状态、工具授权理由与共享日计数。"""
    if app_state.mcp is None:
        raise HTTPException(status_code=503, detail='MCP 管理器尚未启动')
    manager = app_state.mcp
    secrets = [secret for server in manager.config.servers for secret in connection_secrets(
        server.api_key or manager.providers.get(server.api_key_provider, ''), headers=server.headers, env=server.env)]
    return mcp_webui._redact(mcp_webui.serialize_mcp_status(manager.statuses()), secrets)


@router.post('/mcp/test', dependencies=[Depends(_auth), Depends(_require_loopback)])
async def test_mcp(payload: Dict[str, Any]) -> Dict[str, Any]:
    """临时连接单个服务并在 20 秒内清理，不登记工具、不保存配置。"""
    directory = _directory()
    try:
        config = mcp_webui.prepare_mcp_payload(directory, {'enabled': True, 'servers': [payload]})
    except (McpConfigError, ValidationError) as exc:
        detail = str(exc) if isinstance(exc, McpConfigError) else '；'.join(e['msg'] for e in exc.errors(include_input=False))
        raise HTTPException(status_code=400, detail=detail) from exc
    server = config.servers[0]
    providers = load_provider_keys(directory)
    api_key = providers[server.api_key_provider] if server.api_key_provider else server.api_key
    manager = app_state.mcp
    if manager is None:
        raise HTTPException(status_code=503, detail='MCP 管理器尚未启动')
    connection = None
    result: Dict[str, Any]
    try:
        async with asyncio.timeout(20):
            connection = await connect(server, api_key=api_key,
                proxy=manager.proxy, force=manager.force)
            result = mcp_webui.serialize_test_result(connection.info, connection.tools, '')
    except McpFailure as exc:
        result = mcp_webui.serialize_test_result(None, (), str(exc))
    except TimeoutError:
        result = mcp_webui.serialize_test_result(None, (), '测试连接超过 20 秒')
    finally:
        if connection is not None:
            await connection.close()
    secrets = connection_secrets(api_key, headers=server.headers, env=server.env)
    return mcp_webui._redact(result, secrets)
