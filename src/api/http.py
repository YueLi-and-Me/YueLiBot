"""
HTTP 路由（已接通各 service）。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import require_token
from .state import app_state   # 全局服务状态

from src.common.clock import now as current_time
from src.common.logger import get_logger
from src.config.loader import get_config
from src.platform_io.reply_gate import decide_reply
from src.platform_io.types import InboundMessage
from src.services.trace import trace

logger = get_logger(__name__)
router = APIRouter()

_GROUP_REPLY_WINDOW_MS = 10 * 60_000
_MAX_GROUP_REPLIES_IN_WINDOW = 3


class PlatformInboundBody(BaseModel):
    """平台适配器提交的一条完整入站消息。"""

    model_config = ConfigDict(extra='forbid')

    platform: str
    stream_kind: Literal['direct', 'group'] = Field(alias='streamKind')
    stream_external_id: str = Field(alias='streamExternalId')
    sender_external_id: str = Field(alias='senderExternalId')
    sender_name: str = Field(alias='senderName')
    text: str
    mentioned_me: bool = Field(alias='mentionedMe')
    external_message_id: str = Field(alias='externalMessageId')

    @field_validator(
        'platform',
        'stream_external_id',
        'sender_external_id',
        'sender_name',
        'text',
        'external_message_id',
    )
    @classmethod
    def _require_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError('字符串字段不能为空')
        return value


class PlatformIdentityLinkBody(BaseModel):
    """平台适配器启动时，把已声明的 owner 身份绑定到既有 person。"""

    model_config = ConfigDict(extra='forbid')

    platform: Literal['qq']
    external_id: str = Field(alias='externalId')
    display_name: str = Field(alias='displayName')

    @field_validator('external_id')
    @classmethod
    def _require_qq(cls, value: str) -> str:
        value = value.strip()
        if not value or not value.isdigit():
            raise ValueError('QQ identity 必须是非空数字')
        return value

    @field_validator('display_name')
    @classmethod
    def _require_display_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError('字符串字段不能为空')
        return value


def _auth(authorization: str | None = Header(default=None)) -> None:
    require_token(authorization)


@router.get("/health")
async def health() -> dict:
    return {"ok": True}


@router.get('/runtime/health', dependencies=[Depends(_auth)])
async def runtime_health() -> dict:
    """Electron 连接独立后端前，用 token 验证运行时文件与进程相匹配。"""
    return {'ok': True}


@router.post("/chat/send", dependencies=[Depends(_auth)])
async def chat_send(request: Request) -> JSONResponse:
    body = await request.json()
    text = str(body.get("text", "")).strip()
    if not text or app_state.chat is None or app_state.registry is None:
        return JSONResponse({"turnId": 0})
    context = app_state.registry.desktop_context()
    turn_id = await app_state.chat.send(InboundMessage(text=text, context=context))
    return JSONResponse({"turnId": turn_id})


@router.post('/platform/inbound', dependencies=[Depends(_auth)])
async def platform_inbound(body: PlatformInboundBody) -> JSONResponse:
    """接收非桌面平台消息，完成归属解析和群聊门控后汇入 ChatService。"""
    if app_state.chat is None or app_state.registry is None:
        return JSONResponse(
            {'detail': '对话服务未初始化'},
            status_code=503,
        )

    now = current_time()
    context = app_state.registry.resolve_inbound(
        platform=body.platform,
        stream_kind=body.stream_kind,
        stream_external_id=body.stream_external_id,
        sender_external_id=body.sender_external_id,
        sender_name=body.sender_name,
        first_seen_at=now,
    )
    if app_state.register_platform_stream is not None:
        app_state.register_platform_stream(context.stream)
    reply_count = 0
    if context.stream.kind == 'group':
        reply_count = app_state.chat.memory.assistant_reply_count_since(
            context.stream.id,
            now - _GROUP_REPLY_WINDOW_MS,
        )
    decision = decide_reply(
        stream_kind=context.stream.kind,
        asleep=app_state.chat.current_sleep().asleep,
        mentioned_me=body.mentioned_me,
        my_replies_in_window=reply_count,
        max_replies_in_window=_MAX_GROUP_REPLIES_IN_WINDOW,
    )
    trace.emit(
        'reply_gate',
        streamId=context.stream.id,
        accepted=decision.accepted,
        reason=decision.reason,
    )
    if not decision.accepted:
        return JSONResponse({
            'turnId': 0,
            'streamId': context.stream.id,
            'accepted': False,
            'reason': decision.reason,
        })

    turn_id = await app_state.chat.send(InboundMessage(
        text=body.text,
        context=context,
        mentioned_me=body.mentioned_me,
        external_message_id=body.external_message_id,
    ))
    return JSONResponse({
        'turnId': turn_id,
        'streamId': context.stream.id,
        'accepted': True,
        'reason': decision.reason,
    })


@router.post('/platform/identity/link', dependencies=[Depends(_auth)])
async def platform_identity_link(body: PlatformIdentityLinkBody) -> dict:
    """在平台消息进入归属解析前，绑定适配器声明的 owner 身份。"""
    if app_state.registry is None:
        return {'ok': False, 'detail': '身份注册表未初始化'}

    owner = app_state.registry.owner_person()
    # owner.qq 是单值配置项，换号时旧号要解绑，不能越攒越多
    app_state.registry.set_sole_identity(
        owner,
        body.platform,
        body.external_id,
        body.display_name,
    )
    return {'ok': True, 'personId': owner.id}


@router.post("/chat/interrupt", dependencies=[Depends(_auth)])
async def chat_interrupt() -> dict:
    if app_state.chat and app_state.registry:
        app_state.chat.interrupt(app_state.registry.desktop_stream().id)
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
async def observability(stream_id: int = Query(alias='streamId')) -> JSONResponse:
    if app_state.chat is None:
        return JSONResponse({"_stub": True})
    if app_state.registry is None:
        return JSONResponse({'detail': 'stream 注册表未初始化'}, status_code=503)
    try:
        app_state.registry.stream(stream_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    payload = app_state.chat.observability_snapshot(stream_id)
    if app_state.awareness:
        payload.update(app_state.awareness.observability_fields())
    if app_state.tts:
        payload["voice"] = app_state.tts.inspect()
    return JSONResponse(payload)


@router.get('/streams', dependencies=[Depends(_auth)])
async def streams() -> dict:
    """列出只读观察面板可选择的全部 stream。"""
    if app_state.registry is None:
        return {'streams': []}
    return {
        'streams': [
            {
                'id': stream.id,
                'platform': stream.platform,
                'kind': stream.kind,
                'externalId': stream.external_id,
            }
            for stream in app_state.registry.list_streams()
        ]
    }


@router.get("/debug/trace", dependencies=[Depends(_auth)])
async def debug_trace(since: int = 0) -> JSONResponse:
    """用户输入 / LLM 请求-流式增量-最终响应 / 记忆写入 / 感知决策的运行时追踪。

    增量拉取：seq 之后的条目，配合前端轮询，不用每次全量搬。
    """
    return JSONResponse(trace.since(since))
