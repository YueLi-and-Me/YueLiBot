"""WebUI 月璃设置页的配置读取与保存接口。

读取时返回 ``settings_schema.json`` 的字段映射和五份配置的业务值；保存沿用
模型工作台的鉴权与回环限制，并复用 ``settings_webui`` 的完整校验与原子写。
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, status

from .model_config import _auth, _config_dir, _require_loopback
from src.core.config import settings_webui

router = APIRouter(prefix='/settings', tags=['settings'])


@router.get('/config', dependencies=[Depends(_auth)])
async def settings_config() -> Dict[str, Any]:
    """返回五份配置文件的 schema 与当前值。"""
    return settings_webui.snapshot(_config_dir())


@router.put('/config', dependencies=[Depends(_auth), Depends(_require_loopback)])
async def save_settings_config(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    """校验并保存五份配置；任一文件不完整时整体回滚。"""
    values = payload.get('values') if isinstance(payload.get('values'), dict) else payload
    result = settings_webui.save(_config_dir(), values)
    if not result.get('ok'):
        raise HTTPException(status_code=400, detail=str(result.get('detail') or '保存失败'))
    return result
