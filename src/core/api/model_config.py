"""WebUI 模型与厂商工作台接口。

提供厂商/模型/任务路由/生成参数的读取与保存，以及通过 API Key 探测上游
连通性和可用模型列表；写操作只允许本机回环访问，密钥不会原样返回给浏览器。
"""

from __future__ import annotations

from ipaddress import ip_address
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Request, status

from src.core.api.auth import SESSION_COOKIE_NAME, require_token
from src.core.api.state import app_state
from src.core.config import model_webui
from src.core.logging.logger import get_logger

router = APIRouter(prefix='/models', tags=['models'])
logger = get_logger(__name__)


def _auth(
    authorization: str | None = Header(default=None),
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> None:
    """复用主体鉴权：Bearer 或 HttpOnly Cookie 任一有效即可。

    :param authorization: FastAPI 注入的 ``Authorization`` 请求头，可为 ``None``。
    :param session_token: FastAPI 注入的会话 Cookie，可为 ``None``。
    :raises fastapi.HTTPException: 两种凭据都未通过校验时返回 401。
    """
    require_token(authorization, session_token)


def _require_loopback(request: Request) -> None:
    """写操作仅允许浏览器同机的回环地址访问。

    :param request: FastAPI 注入的请求对象，用于读取客户端地址。
    :raises fastapi.HTTPException: 客户端地址缺失、非法或非回环时返回 403。
    """
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
    """取得启动流程注入的配置目录。

    :return: 配置目录路径。
    :raises fastapi.HTTPException: 配置目录尚未注入时返回 503。
    """
    value = getattr(app_state, 'config_dir', None)
    if value is None:
        raise HTTPException(status_code=503, detail='配置目录尚未注入')
    return Path(value)


def _masked_snapshot(data: Dict[str, Any]) -> Dict[str, Any]:
    """复制快照并隐藏 API Key 明文，同时保留是否已设置密钥的标记。

    :param data: ``model_webui.snapshot`` 产出的完整配置快照。
    :return: 浅拷贝后的快照，``providers`` 逐项拷贝且 ``api_key`` 置空串。
    副作用：无。
    """
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
    """校验并保存模型配置；完整引用校验通过后才替换磁盘文件。

    :param payload: 浏览器提交的完整模型配置。
    :return: 保存结果（含 ``ok`` 标记与重载后的快照等字段）。
    :raises fastapi.HTTPException: 校验失败时返回 400，detail 携带原因。
    """
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
    """通过表单字段直接测试厂商端点连通性与 API Key。

    :param base_url: 厂商 API 基础地址。
    :param api_key: 表单里明文提交的 API Key，可为空串（未填时测连通性）。
    :param client_type: 客户端协议类型，默认 ``openai``。
    :param auth_type: 鉴权方式，默认 ``bearer``。
    :param auth_name: 自定义鉴权头名称，可为空串。
    :param model_list_endpoint: 模型列表端点路径，默认 ``/models``。
    :return: 连通性测试结果，含成功标记与耗时等字段。
    """
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
    """使用已保存厂商的密钥测试连通性，浏览器侧不会拿到明文 Key。

    :param provider_name: 已保存配置里的厂商名。
    :return: 连通性测试结果。
    :raises fastapi.HTTPException: 厂商名不存在时返回 404。
    """
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
    """使用已保存厂商的密钥获取可用模型列表。

    :param provider_name: 已保存配置里的厂商名。
    :return: 含 ``success``、``models`` 与 ``count`` 的结果。
    :raises fastapi.HTTPException: 厂商名不存在时返回 404；上游请求失败
        时返回 502，detail 携带上游错误。
    """
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
    """通过表单字段直接获取厂商可用模型列表。

    :param base_url: 厂商 API 基础地址。
    :param api_key: 表单里明文提交的 API Key，可为空串。
    :param client_type: 客户端协议类型，默认 ``openai``。
    :param auth_type: 鉴权方式，默认 ``bearer``。
    :param auth_name: 自定义鉴权头名称，可为空串。
    :param model_list_endpoint: 模型列表端点路径，默认 ``/models``。
    :return: 含 ``success``、``models`` 与 ``count`` 的结果。
    :raises fastapi.HTTPException: 上游请求失败时返回 502，detail 携带
        上游错误。
    """
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
