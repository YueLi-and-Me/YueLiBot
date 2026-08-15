"""提供主体后端的 HTTP API 路由和请求模型。

路由覆盖登录会话、桌面及平台入站消息、聊天控制、前台活动、截图、日记、
观测面板和运行追踪；鉴权依赖来自 `src.core.api.auth`，业务状态通过模块级
`app_state` 连接到聊天、注册表、感知和语音服务。
"""

from __future__ import annotations

from ipaddress import ip_address
from typing import List, Literal

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import (
    SESSION_COOKIE_NAME,
    create_session,
    extract_bearer,
    require_token,
    revoke_session,
    verify_session,
    verify_token,
)
from .state import app_state   # 全局服务状态

from src.core.agent.action_protocol import ActionDecisionEvent, GateInputFacts
from src.core.agent.conversation_gate import GateRequest, decide_disposition, mentions_bot_name
from src.core.common.clock import now as current_time
from src.core.common.logger import get_logger
from src.core.config.loader import get_config
from src.core.observe import events as trace
from src.core.observe.events import enter_stage
from src.core.observe.stages import GATED, RECEIVED
from src.core.observe.store import current_stages, event_store, search_events
from src.core.platform_io.types import InboundMessage, StreamRef
from src.core.prompts.registry import (
    delete_prompt_override,
    list_prompts,
    prompt_detail,
    prompt_history,
    update_prompt,
)
from src.core.services.replay import replay_event, replay_task_for_seq

logger = get_logger(__name__)
router = APIRouter()


class PlatformInboundBody(BaseModel):
    """平台适配器提交的一条完整入站消息。"""

    model_config = ConfigDict(extra='forbid')

    platform: str
    stream_kind: Literal['direct', 'group'] = Field(alias='streamKind')
    stream_external_id: str = Field(alias='streamExternalId')
    sender_external_id: str = Field(alias='senderExternalId')
    sender_nickname: str = Field(alias='senderNickname')
    sender_group_card: str = Field(alias='senderGroupCard')
    bot_name: str | None = Field(default=None, alias='botName')
    text: str
    mentioned_me: bool = Field(alias='mentionedMe')
    external_message_id: str = Field(alias='externalMessageId')

    @field_validator(
        'platform',
        'stream_external_id',
        'sender_external_id',
        'sender_nickname',
        'text',
        'external_message_id',
    )
    @classmethod
    def _require_text(cls, value: str) -> str:
        """去除字符串首尾空白并拒绝空字符串字段。

        :param value: Pydantic 待校验的字符串字段。
        :return: 去除首尾空白后的非空字符串。
        :raises ValueError: 规范化结果为空。
        副作用：不修改原字符串或模型状态。
        """
        value = value.strip()
        if not value:
            raise ValueError('字符串字段不能为空')
        return value

    @field_validator('sender_group_card')
    @classmethod
    def _normalize_group_card(cls, value: str) -> str:
        """规范化群名片，允许其为空。

        :param value: 原始群名片字符串。
        :return: 去除首尾空白后的字符串。
        副作用：不执行外部查询。
        """
        return value.strip()

    @field_validator('bot_name')
    @classmethod
    def _require_optional_bot_name(cls, value: str | None) -> str | None:
        """校验可选的 Bot 名称，区分缺失和空白配置。

        :param value: 入站请求中的 Bot 名称，可以为 `None`。
        :return: `None` 或去除首尾空白后的非空名称。
        :raises ValueError: 显式提供但只包含空白的名称。
        副作用：不修改请求对象。
        """
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError('botName 不能为空字符串')
        return normalized


class PlatformIdentityLinkBody(BaseModel):
    """平台适配器启动时，把已声明的 owner 身份绑定到既有 person。"""

    model_config = ConfigDict(extra='forbid')

    platform: Literal['qq']
    external_id: str = Field(alias='externalId')
    display_name: str = Field(alias='displayName')

    @field_validator('external_id')
    @classmethod
    def _require_qq(cls, value: str) -> str:
        """规范化并校验 owner 的 QQ identity 外部 ID。

        :param value: 平台提交的外部身份标识。
        :return: 去除首尾空白后的数字字符串。
        :raises ValueError: 标识为空或含非数字字符。
        副作用：不查询平台，也不修改模型外的身份数据。
        """
        value = value.strip()
        if not value or not value.isdigit():
            raise ValueError('QQ identity 必须是非空数字')
        return value

    @field_validator('display_name')
    @classmethod
    def _require_display_name(cls, value: str) -> str:
        """规范化 owner identity 的显示名称。

        :param value: 平台提交的显示名称。
        :return: 去除首尾空白后的非空字符串。
        :raises ValueError: 名称为空或只包含空白。
        副作用：不执行身份写入。
        """
        value = value.strip()
        if not value:
            raise ValueError('字符串字段不能为空')
        return value


class WebLoginBody(BaseModel):
    """浏览器登录页提交的一次性后端 token。"""

    model_config = ConfigDict(extra='forbid')

    token: str

    @field_validator('token')
    @classmethod
    def _require_token(cls, value: str) -> str:
        """去除登录 token 首尾空白并拒绝空值。

        :param value: 浏览器登录请求中的 token。
        :return: 去除首尾空白后的 token。
        :raises ValueError: token 为空。
        副作用：不执行 token 比较。
        """
        value = value.strip()
        if not value:
            raise ValueError('token 不能为空')
        return value


class PromptWriteBody(BaseModel):
    """提示词编辑器提交的完整模板文本。"""

    model_config = ConfigDict(extra='forbid')

    content: str


class ReplayBody(BaseModel):
    """单次隔离重放请求。"""

    model_config = ConfigDict(extra='forbid')

    seq: int = Field(ge=1)


def _require_loopback(request: Request) -> None:
    """拒绝来自非回环地址的提示词写请求。

    :param request: FastAPI 请求对象。
    :raises fastapi.HTTPException: 客户端地址缺失、非法或不是回环地址时返回 403。
    副作用：仅读取连接地址。
    """
    host = request.client.host if request.client is not None else ''
    try:
        loopback = ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='提示词编辑仅允许从本机回环地址访问',
        )


def _auth(
    authorization: str | None = Header(default=None),
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> None:
    """作为 FastAPI 依赖校验 Bearer 或 HttpOnly Cookie 鉴权。

    :param authorization: 可选 Authorization 请求头，由 FastAPI 注入。
    :param session_token: 可选会话 Cookie，由 FastAPI 注入。
    :return: 鉴权通过时返回 `None`。
    :raises fastapi.HTTPException: 两种凭据都无法通过校验时返回 401。
    副作用：只读取请求凭据，不修改会话状态。
    """
    require_token(authorization, session_token)


@router.get("/health")
async def health() -> dict:
    """返回不需要鉴权的进程存活探针。

    :return: 固定返回 `{'ok': True}` 的 JSON 可序列化字典。
    副作用：不访问服务状态或外部系统。
    """
    return {"ok": True}


@router.post('/auth/login')
async def web_login(body: WebLoginBody, response: Response) -> dict:
    """校验浏览器提交的 token，并写入 HttpOnly 会话 Cookie。

    :param body: 包含用户输入认证 token 的请求模型。
    :param response: FastAPI 响应对象，用于设置会话 Cookie 和禁止缓存。

    :return: ``{'ok': True}``。

    :raises fastapi.HTTPException: token 不匹配当前进程 token 时返回 401。

    副作用：
        在响应中写入 ``yueli_session`` HttpOnly、Strict Cookie；浏览器脚本无法读取
        Cookie，响应同时设置 ``Cache-Control: no-store``。
    """
    if not verify_token(body.token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='token 不正确')
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=create_session(),
        httponly=True,
        samesite='strict',
        path='/',
    )
    response.headers['Cache-Control'] = 'no-store'
    return {'ok': True}


@router.post('/auth/logout')
async def web_logout(
    response: Response,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> dict:
    """作废当前浏览器会话并清除会话 Cookie。

    :param response: FastAPI 响应对象，用于清除会话 Cookie。
    :param session_token: 当前浏览器提交的会话凭据。
    :return: ``{'ok': True}``。
    :raises fastapi.HTTPException: Cookie 缺失或会话已经失效时返回 401。
    副作用：仅撤销当前会话，不影响其他浏览器会话或后端主 token。
    """
    if not revoke_session(session_token or ''):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='认证失败')
    response.delete_cookie(key=SESSION_COOKIE_NAME, path='/', samesite='strict')
    response.headers['Cache-Control'] = 'no-store'
    return {'ok': True}


@router.get('/auth/session')
async def web_session(
    authorization: str | None = Header(default=None),
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> dict:
    """返回当前请求是否已通过 Bearer 或会话 Cookie 鉴权。

    :param authorization: 可选 Authorization 请求头，由 FastAPI 注入。
    :param session_token: 可选 HttpOnly 会话 Cookie，由 FastAPI 注入。

    :return: 包含 ``authenticated`` 布尔字段的字典；不返回 token 或观察数据。

    副作用：
        仅读取请求凭据，不修改会话状态。
    """
    authenticated = (
        verify_token(extract_bearer(authorization))
        or verify_session(session_token or '')
    )
    return {'authenticated': authenticated}


@router.get('/runtime/health', dependencies=[Depends(_auth)])
async def runtime_health() -> dict:
    """在 Electron 连接独立后端前确认认证链路和进程实例可达。

    :return: 固定返回 ``{'ok': True}``。

    :raises fastapi.HTTPException: 鉴权依赖未通过时由 ``_auth`` 返回 401。

    副作用：
        不读取业务状态，不执行外部 I/O；路由级鉴权已先验证当前 token。
    """
    return {'ok': True}


@router.post("/chat/send", dependencies=[Depends(_auth)])
async def chat_send(request: Request) -> JSONResponse:
    """接收桌面端聊天文本并把它提交给当前桌面 stream。

    :param request: FastAPI 请求对象；JSON body 需要包含字符串字段 `text`。
    :return: 明确表示消息是否进入缓冲的 JSON 响应。
    :raises Exception: 请求体不是合法 JSON，或聊天服务发送失败时传播原始异常。
    副作用：读取注册表桌面上下文并将非空消息放入聊天缓冲区。
    """
    body = await request.json()
    text = str(body.get("text", "")).strip()
    if not text or app_state.chat is None or app_state.registry is None:
        return JSONResponse({'accepted': False})
    context = app_state.registry.desktop_context()
    await app_state.chat.send(InboundMessage(text=text, context=context))
    return JSONResponse({'accepted': True})


@router.post('/platform/inbound', dependencies=[Depends(_auth)])
async def platform_inbound(body: PlatformInboundBody) -> JSONResponse:
    """接收平台入站消息，完成 stream/person 归属解析和回复门控后提交聊天服务。

    :param body: 已通过 Pydantic 校验的平台入站消息，包含平台、会话、发送者和正文信息。

    :return: JSON 响应；服务未初始化时返回 503，门控拒绝时返回 ``accepted=False``，
        接受时返回 stream ID 与门控原因；消息入缓冲时尚未创建回合。

    :raises fastapi.HTTPException: 路由鉴权失败时由依赖项返回 401。
    :raises ValueError: 注册表归属解析、记忆查询或聊天服务发现输入不一致时抛出。
    :raises Exception: 聊天轮次创建或持久化失败且未被服务层处理时传播。

    副作用：
        可能创建或更新人物、身份和 stream，写入接收/门控观测事件，记录被拒消息，
        或启动一轮聊天生成。
    """
    # 服务未完成装配时返回 503，避免把平台消息误判为已处理。
    if app_state.chat is None or app_state.registry is None:
        return JSONResponse(
            {'detail': '对话服务未初始化'},
            status_code=503,
        )

    now = current_time()
    # 归属解析必须先于门控，后续 trace、记忆和出站路由都依赖稳定 stream/person 引用。
    context = app_state.registry.resolve_inbound(
        platform=body.platform,
        stream_kind=body.stream_kind,
        stream_external_id=body.stream_external_id,
        sender_external_id=body.sender_external_id,
        sender_nickname=body.sender_nickname,
        sender_group_card=body.sender_group_card,
        first_seen_at=now,
    )
    if app_state.register_platform_stream is not None:
        app_state.register_platform_stream(context.stream)
    stream_name = _stream_label(context.stream)
    enter_stage(
        RECEIVED, context.stream.id, stream_name, body.text[:40],
    )
    group_chat = app_state.group_chat_config
    reply_count = 0
    if context.stream.kind == 'group':
        # 频率窗口只统计当前 group stream 的助手消息，不跨群或跨平台共享配额。
        reply_count = app_state.chat.memory.assistant_reply_count_since(
            context.stream.id,
            now - group_chat.reply_window_minutes * 60_000,
        )
    # 文本称呼只读取 bot.toml；协议登录昵称仅用于上下文展示，不能旁路配置触发回合。
    bot_names = app_state.chat.bot_names()
    asleep = app_state.chat.current_sleep().asleep
    # 名称匹配只在群聊门控中有意义；直接对话不读取名称，避免空名称配置报错。
    name_mentioned = (
        mentions_bot_name(body.text, bot_names)
        if context.stream.kind == 'group'
        else False
    )
    gate_result = decide_disposition(GateRequest(
        stream_kind=context.stream.kind,
        mentioned_me=body.mentioned_me,
        name_mentioned=name_mentioned,
        asleep=asleep,
        at_mention_must_reply=app_state.chat.at_mention_must_reply,
        replies_in_window=reply_count,
        max_replies_in_window=group_chat.max_replies_in_window,
    ))
    plain_group_deferred = (
        context.stream.kind == 'group'
        and gate_result.disposition == 'drop'
        and gate_result.reason_codes == ('attention_filtered',)
        and app_state.chat.conversation_trigger_mode != 'signal'
        and app_state.chat.extended_trigger_enabled(context)
    )
    trace.emit(
        'reply_gate',
        streamId=context.stream.id,
        personId=context.person.id,
        text=body.text,
        botNames=list(bot_names),
        accepted=gate_result.disposition != 'drop' or plain_group_deferred,
        reason=(
            'deferred_to_trigger_mode'
            if plain_group_deferred
            else gate_result.reason_codes[0]
        ),
        asleep=asleep,
        mentionedMe=body.mentioned_me,
        nameMentioned=name_mentioned,
        repliesInWindow=reply_count,
        maxRepliesInWindow=group_chat.max_replies_in_window,
        **gate_result.as_trace(),
    )
    if gate_result.disposition == 'drop' and not plain_group_deferred:
        reason = gate_result.reason_codes[0]
        # 静默消息仍写入历史和观察事件，确保下一轮上下文知道该消息已经出现。
        enter_stage(
            GATED, context.stream.id, stream_name,
            f'未回复：{reason}',
        )
        message_id = app_state.chat.record_group_observation(
            InboundMessage(
                text=body.text,
                context=context,
                mentioned_me=body.mentioned_me,
                external_message_id=body.external_message_id,
                bot_name=body.bot_name,
            ),
            reason,
        )
        # DROP 不调用模型，但必须落一条可审计行动事件，回答「代码根本没让她考虑」。
        gate_event = ActionDecisionEvent(
            turn_id=None,
            snapshot_id=f'gate-{message_id}',
            turn_message_watermark=message_id,
            gate_inputs=GateInputFacts(
                stream_kind=context.stream.kind,
                mentioned_me=body.mentioned_me,
                name_mentioned=name_mentioned,
                must_reply=False,
                asleep=asleep,
                rate_limited=reason == 'rate_limited',
                recent_bot_replies=reply_count,
                candidate_message_ids=(message_id,),
                selectable_message_ids=(),
            ),
            gate_disposition=gate_result.disposition,
            gate_reason_codes=gate_result.reason_codes,
            available_actions=(),
            decision=None,
            event_status='gate_dropped',
        )
        trace.emit('action_decision', **gate_event.to_dict())
        return JSONResponse({
            'streamId': context.stream.id,
            'accepted': False,
            'reason': reason,
        })

    await app_state.chat.send(InboundMessage(
        text=body.text,
        context=context,
        mentioned_me=body.mentioned_me,
        external_message_id=body.external_message_id,
        bot_name=body.bot_name,
    ))
    return JSONResponse({
        'streamId': context.stream.id,
        'accepted': True,
        'reason': (
            'deferred_to_trigger_mode'
            if plain_group_deferred
            else gate_result.reason_codes[0]
        ),
    })


@router.post('/platform/identity/link', dependencies=[Depends(_auth)])
async def platform_identity_link(body: PlatformIdentityLinkBody) -> dict:
    """在平台消息进入归属解析前绑定适配器声明的 owner 外部身份。

    :param body: 包含平台、owner 外部 ID 和显示名的已校验请求模型。

    :return: 绑定成功时返回 ``ok=True`` 和 owner person ID；注册表未初始化时返回失败详情。

    :raises fastapi.HTTPException: 路由鉴权失败时由依赖项返回 401。
    :raises ValueError: 外部身份已由注册表校验为非法时抛出。

    副作用：
        修改 owner person 的指定平台唯一身份绑定；同平台旧绑定会被解除。
    """
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
    """中断当前桌面 stream 上正在生成的聊天轮次。

    :return: 固定返回 `{'ok': True}`。
    副作用：若聊天和注册表已初始化，向桌面 stream 的 ChatService 发出中断请求。
    """
    if app_state.chat and app_state.registry:
        app_state.chat.interrupt(app_state.registry.desktop_stream().id)
    return {"ok": True}


@router.post("/platform/foreground", dependencies=[Depends(_auth)])
async def platform_foreground(request: Request) -> dict:
    """接收 Electron 主进程定期上报的前台进程信息。

    :param request: 已通过鉴权的 FastAPI 请求；请求体应为可解析的 JSON 对象。

    :return: 固定返回 ``{'ok': True}``。

    :raises json.JSONDecodeError: 请求体不是合法 JSON 时由框架请求解析逻辑抛出。
    :raises Exception: 前台活动回调处理请求体失败时传播原始异常。

    副作用：
        若已注册前台活动回调，则将请求体交给回调更新当前前台上下文。
    """
    body = await request.json()
    if app_state.foreground_callback:
        app_state.foreground_callback(body)
    return {"ok": True}


@router.post("/platform/screenshot/chat", dependencies=[Depends(_auth)])
async def platform_screenshot_chat(request: Request) -> dict:
    """接收聊天上下文所需的单帧 JPEG，并在请求内完成视觉描述更新。

    :param request: 已通过认证的 HTTP 请求；请求体应为 JPEG 二进制数据，空请求体
            表示本轮不更新视觉缓存。

    :return: ``{"ok": True}``；视觉功能未就绪时也返回成功，以保持前台采集端协议稳定。

    :raises RuntimeError: 感知服务或视觉提供者在处理图片时报告运行时错误。
    :raises StarletteHTTPException: 请求体读取失败时由框架传播。

    副作用：
        可能调用视觉模型并更新当前会话的最新描述。函数等待描述完成后才返回，
        确保随后发送的聊天请求能够读取本帧结果；不保存窗口标题或原始截图。
    """
    if not get_config().vision.ready or not app_state.awareness or not app_state.awareness.vision:
        return {"ok": True}
    jpeg_bytes = await request.body()
    if jpeg_bytes:
        # 前台程序名作为低敏感度上下文补充视觉提示，窗口标题不进入模型请求。
        await app_state.awareness.vision.glance(jpeg_bytes, app=app_state.awareness.current_app())
    return {"ok": True}


@router.get("/diary", dependencies=[Depends(_auth)])
async def diary() -> JSONResponse:
    """返回聊天服务生成的日记面板数据。

    :return: 聊天服务的日记 JSON；服务未初始化时返回 `{'_stub': True}`。
    副作用：只读取当前聊天服务状态，不触发模型调用。
    """
    if app_state.chat is None:
        return JSONResponse({"_stub": True})
    return JSONResponse(app_state.chat.diary_payload())


@router.get("/observability", dependencies=[Depends(_auth)])
async def observability(stream_id: int = Query(alias='streamId')) -> JSONResponse:
    """返回指定 stream 的观测快照及可选感知、语音状态。

    :param stream_id: 查询参数 `streamId`，目标 stream 的正整数数据库 ID。
    :return: 可序列化的观测快照 JSON。
    :raises fastapi.HTTPException: 服务未初始化时返回 503，stream 不存在时返回 404。
    副作用：读取注册表、聊天、感知和 TTS 服务状态，不修改业务数据。
    """
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


def _stream_label(stream: StreamRef) -> str:
    """生成观察面板使用的 stream 可读短名称。

    :param stream: 已解析的 stream 引用，包含平台、会话类型和外部 ID。

    :return: 桌面 stream 返回 ``桌面``；其他 stream 返回平台大写名称、私聊或群聊类型
        以及外部 ID 组成的短名称。

    副作用：
        仅读取 stream 字段，不修改注册表或业务状态。
    """
    if stream.platform == 'desktop':
        return '桌面'
    kind = '群聊' if stream.kind == 'group' else '私聊'
    return f'{stream.platform.upper()} {kind} {stream.external_id}'


@router.get('/stages', dependencies=[Depends(_auth)])
async def stages() -> dict:
    """返回每条 stream 当前观测阶段，供观察面板轮询。

    :return: 包含 ``stages`` 字段的可序列化阶段快照。

    副作用：
        仅读取阶段看板，不触发业务处理或模型调用。
    """
    return {'stages': current_stages()}


@router.get('/events', dependencies=[Depends(_auth)])
async def event_history(
    stream_id: int | None = Query(default=None, alias='streamId', ge=1),
    turn_id: int | None = Query(default=None, alias='turnId', ge=1),
    kind: List[str] | None = Query(default=None),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    limit: int = Query(default=200, ge=1, le=1_000),
    cursor: int | None = Query(default=None, ge=1),
) -> dict:
    """按组合条件检索持久化管线事件。

    返回结果按 ``seq`` 倒序；``nextCursor`` 非空时可继续向前翻页。时间区间
    使用左闭右开语义，重复 ``kind`` 参数表示多选。
    """
    try:
        page = search_events(
            stream_id=stream_id,
            turn_id=turn_id,
            kinds=kind,
            since_at=since,
            until_at=until,
            limit=limit,
            cursor=cursor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {'events': page.events, 'nextCursor': page.next_cursor}


@router.get('/prompts', dependencies=[Depends(_auth)])
async def prompts() -> dict:
    """列出全部提示词模板的来源、哈希与占位符。"""
    return {'prompts': list_prompts()}


@router.get('/prompts/{prompt_id}', dependencies=[Depends(_auth)])
async def prompt(prompt_id: str) -> dict:
    """返回一份提示词的生效文本与内置文本。"""
    try:
        return prompt_detail(prompt_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.put(
    '/prompts/{prompt_id}',
    dependencies=[Depends(_auth), Depends(_require_loopback)],
)
async def put_prompt(prompt_id: str, body: PromptWriteBody) -> dict:
    """校验、写入并热重载一份提示词覆盖。"""
    try:
        return update_prompt(prompt_id, body.content)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@router.delete(
    '/prompts/{prompt_id}',
    dependencies=[Depends(_auth), Depends(_require_loopback)],
)
async def delete_prompt(prompt_id: str) -> dict:
    """删除一份提示词覆盖并热重载内置版本。"""
    try:
        return delete_prompt_override(prompt_id)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@router.get('/prompts/{prompt_id}/history', dependencies=[Depends(_auth)])
async def prompt_versions(prompt_id: str) -> dict:
    """列出一份提示词的有限历史归档。"""
    try:
        return {'history': prompt_history(prompt_id)}
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post('/replay', dependencies=[Depends(_auth)])
async def replay(body: ReplayBody) -> dict:
    """使用当前模板隔离重放一条历史模型请求。"""
    try:
        task = replay_task_for_seq(event_store, body.seq)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if app_state.routers is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f'{task} 模型路由尚未就绪',
        )
    provider = app_state.routers.for_task(task)
    if not provider.ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f'{task} 模型路由尚未就绪',
        )
    try:
        return await replay_event(event_store, provider, body.seq)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get('/streams', dependencies=[Depends(_auth)])
async def streams() -> dict:
    """列出只读观察面板可选择的全部 stream。

    :return: 包含每个 stream 的数据库 ID、平台、会话类型和外部 ID 的字典；注册表未就绪
        时返回空列表。

    副作用：
        仅读取注册表，不创建 stream 或修改业务数据。
    """
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


@router.get('/api/persons', dependencies=[Depends(_auth)])
async def persons() -> dict:
    """列出人物画像入口，不将人物关系详情嵌入会话观测快照。

    :return: 包含人物画像摘要列表的 ``persons`` 字段。

    :raises fastapi.HTTPException: 聊天服务未初始化时返回 503。

    副作用：
        仅读取聊天服务状态，不触发模型调用或人物数据写入。
    """
    if app_state.chat is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='人物画像服务未初始化',
        )
    return {'persons': app_state.chat.list_person_profiles()}


@router.get('/api/persons/{person_id}', dependencies=[Depends(_auth)])
async def person_detail(person_id: int) -> dict:
    """按数据库 ID 读取单个人物画像，禁止将无效 ID 回退到 owner。

    :param person_id: 路径参数中的人物数据库 ID；必须为正整数。

    :return: 指定人物的可序列化画像数据。

    :raises fastapi.HTTPException: 服务未初始化时返回 503，人物 ID 不存在时返回 404。
    :raises ValueError: 聊天服务将人物 ID 判定为非法时转换为 404。

    副作用：
        仅读取人物画像，不创建人物或更新关系状态。
    """
    if app_state.chat is None or app_state.registry is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='人物画像服务未初始化',
        )
    try:
        return app_state.chat.person_profile(person_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
