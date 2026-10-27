"""插件市场管理接口，读取须鉴权，目录写入额外限制回环访问。

同步网络与文件操作在线程池执行，事件循环继续处理其他会话；业务交给市场服务。
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from typing import Any, Dict

import asyncio

from src.core.plugin_market.operations import MarketOperations
from src.core.plugin_market.webui import serialize_market

from .state import app_state
from .tools_config import _auth, _require_loopback

router = APIRouter(prefix='/plugin-market', tags=['plugin-market'])


class PluginRequest(BaseModel):
    """插件操作的身份字段，拒绝未知字段与非字符串值。"""

    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(min_length=1)


class VersionRequest(PluginRequest):
    """升级或接管的目标版本，版本存在性与兼容性由市场服务验证。"""

    version: str = Field(min_length=1)


class InstallRequest(VersionRequest):
    """首次安装指定来源，避免同 id 的不同来源产生误选。"""

    source: str = Field(min_length=1)


def _market() -> MarketOperations:
    """取得组合根注入的市场服务，未启动时返回明确的 503。"""
    if app_state.plugin_market is None:
        raise HTTPException(status_code=503, detail='插件市场尚未启动')
    return app_state.plugin_market


@router.get('/index', dependencies=[Depends(_auth)])
async def index(refresh: int = 0) -> Dict[str, Any]:
    """读取合并目录，网络失败逐源保留在响应中。

    :param refresh: 0 使用缓存，1 强制刷新，默认 0。
    :return: 来源、插件、兼容性和磁盘安装状态组成的快照。
    :raises HTTPException: 参数或收据非法返回 400；服务未启动返回 503。
    """
    if refresh not in (0, 1):
        raise HTTPException(status_code=400, detail='refresh 只允许 0 或 1')
    try:
        return await asyncio.to_thread(serialize_market, _market(), refresh=bool(refresh))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post('/install', dependencies=[Depends(_auth), Depends(_require_loopback)])
async def install(request: InstallRequest) -> Dict[str, Any]:
    """在线程池中安装指定来源和版本。

    :param request: 来源名称、插件 id 与目标版本，均为非空字符串。
    :return: 安装结果、依赖说明与重启提示，不执行热加载。
    :raises HTTPException: 完整性或目录冲突原样返回 400。
    """
    try:
        return await asyncio.to_thread(_market().install, source=request.source, plugin_id=request.id, version=request.version)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post('/upgrade', dependencies=[Depends(_auth), Depends(_require_loopback)])
async def upgrade(request: VersionRequest) -> Dict[str, Any]:
    """升级已有收据的插件，保留用户配置并备份旧目录。

    :param request: 已安装插件 id 和索引中的目标版本。
    :return: 操作结果、依赖说明与重启提示。
    :raises HTTPException: 文件改动、下载或替换失败返回 400，服务负责目录恢复。
    """
    try:
        return await asyncio.to_thread(_market().upgrade, plugin_id=request.id, version=request.version)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post('/takeover', dependencies=[Depends(_auth), Depends(_require_loopback)])
async def takeover(request: VersionRequest) -> Dict[str, Any]:
    """接管索引中存在的手动安装插件，原目录备份后再替换。

    :param request: 手动安装插件 id 和索引中的目标版本。
    :return: 操作结果、依赖说明与重启提示。
    :raises HTTPException: 身份、完整性或磁盘操作失败返回 400。
    """
    try:
        return await asyncio.to_thread(_market().takeover, plugin_id=request.id, version=request.version)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post('/uninstall', dependencies=[Depends(_auth), Depends(_require_loopback)])
async def uninstall(request: PluginRequest) -> Dict[str, Any]:
    """把市场插件移到备份，不删除宿主数据。

    :param request: 已有市场收据的插件 id。
    :return: 实际备份路径和重启提示。
    :raises HTTPException: 身份或移动失败返回 400。
    """
    try:
        return await asyncio.to_thread(_market().uninstall, plugin_id=request.id)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
