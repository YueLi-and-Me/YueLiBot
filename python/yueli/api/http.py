"""
HTTP 路由（已接通各 service）。
"""

from __future__ import annotations

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


@router.post("/platform/screenshot/chat", dependencies=[Depends(_auth)])
async def platform_screenshot_chat(request: Request) -> dict:
    """他问起屏幕时截的那一帧。

    ★ 这是视觉的**唯一**入口。曾经还有个 /platform/screenshot 走后台轮询，
      每 12s 推一张图进来做帧差和关键帧序列；整条链路连同它的九个时间常量
      一起删掉了——他不问，就不看。
    这里用 await 而不是 create_task：调用方要等它完成之后才发 /chat/send，
    好让这一轮的情境文本能读到刚生成的描述。
    """
    if not get_config().vision.ready or not app_state.awareness or not app_state.awareness.vision:
        return {"ok": True}
    jpeg_bytes = await request.body()
    if jpeg_bytes:
        # 带上前台程序名：视觉模型认不出界面时，「这是 PyCharm」这个先验
        # 比让它对着截图硬猜有用得多。窗口标题仍然不传。
        await app_state.awareness.vision.glance(jpeg_bytes, app=app_state.awareness.current_app())
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
