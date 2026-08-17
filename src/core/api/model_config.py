"""WebUI 模型与厂商工作台接口。

提供厂商/模型/任务路由/生成参数的读取与保存，以及通过 API Key 探测上游
连通性和可用模型列表；写操作只允许本机回环访问，密钥不会原样返回给浏览器。
"""

from __future__ import annotations

from ipaddress import ip_address
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Request, status

from src.core.api.auth import SESSION_COOKIE_NAME, require_token
from src.core.api.state import app_state
from src.core.config import model_webui
from src.core.common.logger import get_logger

router = APIRouter(prefix='/models', tags=['models'])
logger = get_logger(__name__)


def _auth(
    authorization: str | None = Header(default=None),
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> None:
    """复用主体鉴权：Bearer 或 HttpOnly Cookie 任一有效即可。"""
    require_token(authorization, session_token)


def _require_loopback(request: Request) -> None:
    """写操作仅允许浏览器同机的回环地址访问。"""
    host = request.client.host if request.client is not None else ''
    try:
        loopback = ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='模型配置编辑仅允许从本机回环地址访问',
        )


def _config_dir() -> Path:
    value = getattr(app_state, 'config_dir', None)
    if value is None:
        raise HTTPException(status_code=503, detail='配置目录尚未注入')
    return Path(value)


def _masked_snapshot(data: Dict[str, Any]) -> Dict[str, Any]:
    """复制快照并隐藏 API Key 明文，同时保留是否已设置密钥的标记。"""
    result = dict(data)
    providers = []
    for item in data.get('providers', []):
        row = dict(item)
        row['api_key'] = ''
        providers.append(row)
    result['providers'] = providers
    return result


@router.get('/config', dependencies=[Depends(_auth)])
async def model_config() -> Dict[str, Any]:
    """读取厂商、模型、任务路由与生成参数快照。"""
    return _masked_snapshot(model_webui.snapshot(_config_dir()))


@router.put('/config', dependencies=[Depends(_auth), Depends(_require_loopback)])
async def save_model_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    """校验并保存模型配置；完整引用校验通过后才替换磁盘文件。"""
    result = model_webui.save(_config_dir(), payload)
    if not result.get('ok'):
        raise HTTPException(status_code=400, detail=str(result.get('detail') or '保存失败'))
    return result


@router.get('/test-connection', dependencies=[Depends(_auth)])
async def test_provider_connection(
    base_url: str = Query(...),
    api_key: str = Query(''),
    client_type: str = Query('openai'),
    auth_type: str = Query('bearer'),
    auth_name: str = Query(''),
    model_list_endpoint: str = Query('/models'),
) -> Dict[str, Any]:
    """通过表单字段直接测试厂商端点连通性与 API Key。"""
    return await model_webui.test_connection(
        base_url=base_url,
        api_key=api_key,
        client_type=client_type,
        auth_type=auth_type,
        auth_name=auth_name,
        model_list_endpoint=model_list_endpoint,
    )


@router.get('/test-connection-by-name', dependencies=[Depends(_auth)])
async def test_provider_connection_by_name(
    provider_name: str = Query(...),
) -> Dict[str, Any]:
    """使用已保存厂商的密钥测试连通性，浏览器侧不会拿到明文 Key。"""
    snapshot = model_webui.snapshot(_config_dir())
    provider = next(
        (item for item in snapshot['providers'] if item.get('name') == provider_name),
        None,
    )
    if provider is None:
        raise HTTPException(status_code=404, detail=f'未找到厂商：{provider_name}')
    return await model_webui.test_connection(
        base_url=str(provider.get('base_url') or ''),
        api_key=str(provider.get('api_key') or ''),
        client_type=str(provider.get('client_type') or 'openai'),
        auth_type=str(provider.get('auth_type') or 'bearer'),
        auth_name=str(provider.get('auth_name') or ''),
        model_list_endpoint=str(provider.get('model_list_endpoint') or '/models'),
        default_headers=dict(provider.get('default_headers') or {}),
        default_query=dict(provider.get('default_query') or {}),
    )


@router.get('/list-by-name', dependencies=[Depends(_auth)])
async def provider_models_by_name(
    provider_name: str = Query(...),
) -> Dict[str, Any]:
    """使用已保存厂商的密钥获取可用模型列表。"""
    snapshot = model_webui.snapshot(_config_dir())
    provider = next(
        (item for item in snapshot['providers'] if item.get('name') == provider_name),
        None,
    )
    if provider is None:
        raise HTTPException(status_code=404, detail=f'未找到厂商：{provider_name}')
    try:
        models = await model_webui.list_models(
            base_url=str(provider.get('base_url') or ''),
            api_key=str(provider.get('api_key') or ''),
            client_type=str(provider.get('client_type') or 'openai'),
            auth_type=str(provider.get('auth_type') or 'bearer'),
            auth_name=str(provider.get('auth_name') or ''),
            model_list_endpoint=str(provider.get('model_list_endpoint') or '/models'),
            default_headers=dict(provider.get('default_headers') or {}),
            default_query=dict(provider.get('default_query') or {}),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'获取模型列表失败：{exc}') from exc
    return {'success': True, 'models': models, 'count': len(models)}


@router.get('/list', dependencies=[Depends(_auth)])
async def provider_models(
    base_url: str = Query(...),
    api_key: str = Query(''),
    client_type: str = Query('openai'),
    auth_type: str = Query('bearer'),
    auth_name: str = Query(''),
    model_list_endpoint: str = Query('/models'),
) -> Dict[str, Any]:
    """通过表单字段直接获取厂商可用模型列表。"""
    try:
        models = await model_webui.list_models(
            base_url=base_url,
            api_key=api_key,
            client_type=client_type,
            auth_type=auth_type,
            auth_name=auth_name,
            model_list_endpoint=model_list_endpoint,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'获取模型列表失败：{exc}') from exc
    return {'success': True, 'models': models, 'count': len(models)}
