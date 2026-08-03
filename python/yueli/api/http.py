"""
HTTP 路由（已接通各 service）。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse

from yueli.common.logger import get_logger
from yueli.config.loader import get_config
from yueli.services.trace import trace
from .auth import require_token
from .state import app_state   # 全局服务状态

logger = get_logger(__name__)
router = APIRouter()


def _auth(authorization: str | None = Header(default=None)) -> None:
    require_token(authorization)


@router.get("/health")
async def health() -> dict:
    return {"ok": True}


@router.post("/chat/send", dependencies=[Depends(_auth)])
async def chat_send(request: Request) -> JSONResponse:
    body = await request.json()
    text = str(body.get("text", "")).strip()
    if not text or app_state.chat is None:
        return JSONResponse({"turnId": 0})
    turn_id = await app_state.chat.send(text)
    return JSONResponse({"turnId": turn_id})


@router.post("/chat/interrupt", dependencies=[Depends(_auth)])
async def chat_interrupt() -> dict:
    if app_state.chat:
        app_state.chat.interrupt()
    return {"ok": True}


@router.post("/platform/foreground", dependencies=[Depends(_auth)])
async def platform_foreground(request: Request) -> dict:
    """前台进程信息（从 Electron 主进程定期推来）。"""
    body = await request.json()
    if app_state.foreground_callback:
        app_state.foreground_callback(body)
    return {"ok": True}


@router.post("/platform/screenshot", dependencies=[Depends(_auth)])
async def platform_screenshot(request: Request) -> dict:
    """截图 JPEG（Electron 缩放压缩后推来，供帧差与视觉调用）。"""
    if not get_config().vision.ready or not app_state.awareness or not app_state.awareness.vision:
        return {"ok": True}
    jpeg_bytes = await request.body()
    # 真实的视觉上下文由 AwarenessService 根据最近一次前台分类算出——
    # Electron 没有 classify() 逻辑，不该指望它填对请求头。
    ctx = app_state.awareness.vision_context()
    window_changed = app_state.awareness.window_changed()
    asyncio.create_task(app_state.awareness.vision.process_screenshot(jpeg_bytes, ctx, window_changed))
    return {"ok": True}


@router.get("/diary", dependencies=[Depends(_auth)])
async def diary() -> JSONResponse:
    if app_state.chat is None:
        return JSONResponse({"_stub": True})
    return JSONResponse(app_state.chat.diary_payload())


@router.get("/observability", dependencies=[Depends(_auth)])
async def observability() -> JSONResponse:
    if app_state.chat is None:
        return JSONResponse({"_stub": True})
    payload = app_state.chat.observability_snapshot()
    if app_state.awareness:
        payload.update(app_state.awareness.observability_fields())
    if app_state.tts:
        payload["voice"] = app_state.tts.inspect()
    return JSONResponse(payload)


@router.get("/debug/trace", dependencies=[Depends(_auth)])
async def debug_trace(since: int = 0) -> JSONResponse:
    """用户输入 / LLM 请求-流式增量-最终响应 / 记忆写入 / 感知决策的运行时追踪。

    增量拉取：seq 之后的条目，配合前端轮询，不用每次全量搬。
    """
    return JSONResponse(trace.since(since))
