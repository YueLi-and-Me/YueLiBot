"""提供主体后端的 HTTP API 路由和请求模型。

路由覆盖登录会话、桌面及平台入站消息、聊天控制、前台活动、截图、日记、
观测面板和运行追踪；鉴权依赖来自 `src.core.api.auth`，业务状态通过模块级
`app_state` 连接到聊天、注册表、感知和语音服务。
"""

from __future__ import annotations

from ipaddress import ip_address
from typing import Any, Dict, List, Literal

import asyncio
import sqlite3

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
from src.core.agent.expression import MIN_POOL_CANDIDATES
from src.core.agent.jargon import jargon_use_enabled, set_jargon_use
from src.core.common.clock import now as current_time
from src.core.common.console_layout import print_box
from src.core.common.db.connection import get_db, run_in_thread
from src.core.common.logger import get_logger
from src.core.config.loader import get_config, reload_config
from src.core.memory.association import EDGE_HALF_LIFE_HOURS, HOPS, SPREAD_LIMIT, spread
from src.core.memory.decay import retention
from src.core.observe import events as trace
from src.core.observe.events import enter_stage
from src.core.observe.stages import GATED, RECEIVED
from src.core.observe.store import current_stages, event_store, search_events
from src.core.platform_io.forward import forward_tree_from_payload
from src.core.platform_io.types import InboundMessage, StreamRef
from src.core.prompts.registry import (
    delete_prompt_override,
    list_prompts,
    prompt_detail,
    prompt_history,
    update_prompt,
)
from src.core.services.chat_image import merge_image_descriptions
from src.core.services.prompt_records import (
    RecordsDisabled,
    list_records as list_prompt_records,
    list_tasks as list_record_tasks,
    read_record as read_prompt_record,
)
from src.core.services.replay import replay_event, replay_task_for_seq
from src.core.services.trace_console import render_action_decision

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
    # 平台消息编号。允许为空：戳一戳这类 notice 通道在协议上就没有消息编号，
    # 下游引用逻辑已按「不带编号的通道」处理，不会拿内部 ID 冒充平台编号发出去。
    external_message_id: str = Field(alias='externalMessageId')
    # 本条是「有人戳了 Bot」。戳一戳没有正文也没有 @，正文里没有任何门控能识别的
    # 信号，因此由适配器把这件事作为独立事实提交，门控据此抬入 DELIBERATE。
    poked_me: bool = Field(default=False, alias='pokedMe')
    # 本条是「有人给 Bot 发的消息贴了表情回应」。与戳一戳同口径：只抬入
    # DELIBERATE，绝不 FORCE——群里贴表情非常频繁，每次都唤醒会造成大量
    # 无意义回合。
    emoji_liked_me: bool = Field(default=False, alias='emojiLikedMe')
    image_sources: List[str] = Field(default_factory=list, alias='imageSources')
    emoji_sources: List[str] = Field(default_factory=list, alias='emojiSources')
    emoji_sub_types: List[int] = Field(default_factory=list, alias='emojiSubTypes')
    forward_messages: List[Dict[str, Any]] = Field(
        default_factory=list,
        alias='forwardMessages',
    )
    # 旧协议字段：已下载的 Base64 附件；新适配器只应提交 imageSources。
    images: List[InboundImageBody] = Field(default_factory=list, alias='imageSegments')

    @field_validator('image_sources', 'emoji_sources')
    @classmethod
    def _normalize_image_sources(cls, values: List[str]) -> List[str]:
        """规范图片来源字符串，空项保留以对齐正文占位符。"""
        return [str(value).strip() for value in values]

    @field_validator('emoji_sub_types')
    @classmethod
    def _validate_emoji_sub_types(cls, values: List[int]) -> List[int]:
        """拒绝普通图片子类型，保证后续登记内容都可作为表情包发送。"""

        if any(
            isinstance(value, bool) or value < 0 or value in {0, 4, 9}
            for value in values
        ):
            raise ValueError('emojiSubTypes 必须只包含表情包子类型整数')
        return values

    @field_validator('forward_messages')
    @classmethod
    def _validate_forward_messages(
        cls,
        values: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """在进入业务副作用前严格验证每棵合并转发消息树。"""
        for index, payload in enumerate(values):
            try:
                forward_tree_from_payload(payload)
            except ValueError as exc:
                raise ValueError(
                    f'forwardMessages[{index}] 结构非法：{exc}'
                ) from exc
        return values

    @model_validator(mode='after')
    def _validate_emoji_metadata_alignment(self) -> 'PlatformInboundBody':
        """保证每个表情包来源都携带同位置的 ``sub_type``。"""

        if len(self.emoji_sources) != len(self.emoji_sub_types):
            raise ValueError('emojiSources 与 emojiSubTypes 数量必须一致')
        return self

    @field_validator(
        'platform',
        'stream_external_id',
        'sender_external_id',
        'sender_nickname',
        'text',
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


class InboundImageBody(BaseModel):
    """平台适配器随入站消息提交的普通图片附件。"""

    model_config = ConfigDict(extra='forbid')

    sha256: str = ''
    data: str = ''
    mime: str = 'image/jpeg'

    @field_validator('mime')
    @classmethod
    def _normalize_mime(cls, value: str) -> str:
        """规范化图片 MIME 类型。"""
        value = value.strip()
        return value or 'image/jpeg'


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


@router.post('/auth/auto', dependencies=[Depends(_require_loopback)])
async def web_auto_login(response: Response) -> dict:
    """本机浏览器免手输 token：回环地址直接创建登录会话。"""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=create_session(),
        httponly=True,
        samesite='strict',
        path='/',
    )
    response.headers['Cache-Control'] = 'no-store'
    return {'ok': True}


@router.post('/runtime/shutdown', dependencies=[Depends(_auth), Depends(_require_loopback)])
async def runtime_shutdown() -> JSONResponse:
    """请求后端进程优雅退出；无 server 句柄（测试挂载场景）时返回 503。

    置位 ``should_exit`` 后 uvicorn 走与 SIGINT 完全相同的优雅路径：停监听、
    对在飞请求只关 keep-alive 并等其完成、跑 lifespan 逆序关闭链后以 0 退出。
    因此本响应仍能正常发出，Electron supervisor 只需等待子进程退出事件。
    """
    server = app_state.uvicorn_server
    if server is None:
        return JSONResponse({'detail': '关机句柄未注入'}, status_code=503)
    logger.info('shutdown_requested')
    server.should_exit = True
    return JSONResponse({'ok': True})


@router.post('/system/restart', dependencies=[Depends(_auth), Depends(_require_loopback)])
async def system_restart() -> dict:
    """重启当前 Python 后端进程；Electron supervisor 会重新拉起。

    与 ``/runtime/shutdown`` 同一条优雅路径：进程退出后 supervisor 照旧按
    ``_scheduleRestart`` 拉起，重启语义不变，只是关闭链有机会收尾。
    """
    server = app_state.uvicorn_server
    if server is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='关机句柄未注入',
        )
    logger.info('restart_requested')
    server.should_exit = True
    return {'ok': True}


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
        或启动一轮聊天生成；接受的消息如带 ``imageSources``，只先以 ``[图片]``
        占位符落库并创建后台描述任务，不在此请求内下载图片。
    """
    # 服务未完成装配时返回 503，避免把平台消息误判为已处理。
    if app_state.chat is None or app_state.registry is None:
        return JSONResponse(
            {'detail': '对话服务未初始化'},
            status_code=503,
        )

    forward_messages = tuple(
        forward_tree_from_payload(payload) for payload in body.forward_messages
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
    last_bot_reply_elapsed_ms: int | None = None
    current_topic_available = False
    if context.stream.kind == 'group':
        # 频率窗口只统计当前 group stream 的助手消息，不跨群或跨平台共享配额。
        reply_count = app_state.chat.memory.assistant_reply_count_since(
            context.stream.id,
            now - group_chat.reply_window_minutes * 60_000,
        )
        last_bot_reply_at = app_state.chat.memory.last_assistant_reply_at(context.stream.id)
        if last_bot_reply_at is not None:
            last_bot_reply_elapsed_ms = now - last_bot_reply_at
            current_topic_available = app_state.chat.topic_still_hers(
                context.stream.id, last_bot_reply_at,
            )
    # 文本称呼只读取 bot.toml；协议登录昵称仅用于上下文展示，不能旁路配置触发回合。
    bot_names = app_state.chat.bot_names()
    asleep = app_state.chat.current_sleep().asleep
    # poke 正文由适配器合成，里面的 Bot 名字不是用户说出的点名信号，不能参与匹配。
    # 名称匹配也只在群聊门控中有意义；直接对话不读取名称，避免空名称配置报错。
    name_mentioned = (
        mentions_bot_name(body.text, bot_names)
        if context.stream.kind == 'group' and not body.poked_me
        else False
    )
    pokes_in_window = (
        app_state.chat.record_poke_arrival(context.stream.id, now)
        if body.poked_me
        else 0
    )
    gate_result = decide_disposition(GateRequest(
        stream_kind=context.stream.kind,
        mentioned_me=body.mentioned_me,
        name_mentioned=name_mentioned,
        asleep=asleep,
        at_mention_must_reply=app_state.chat.at_mention_must_reply,
        replies_in_window=reply_count,
        max_replies_in_window=group_chat.max_replies_in_window,
        last_bot_reply_elapsed_ms=last_bot_reply_elapsed_ms,
        current_topic_available=current_topic_available,
        poked_me=body.poked_me,
        pokes_in_window=pokes_in_window,
        emoji_liked_me=body.emoji_liked_me,
        # 入口与批次两个门控必须读同一份跟进事实，否则 reply_gate 审计事件报告的
        # 门控态会与真正生效的批次判定不一致，现场无法据事件还原真实路径。
        follow_up_declined=app_state.chat.follow_up_declined(context.stream.id),
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
        pokedMe=body.poked_me,
        pokesInWindow=pokes_in_window,
        repliesInWindow=reply_count,
        maxRepliesInWindow=group_chat.max_replies_in_window,
        naturalReplyElapsedMs=last_bot_reply_elapsed_ms,
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
                image_sources=tuple(body.image_sources),
                emoji_sources=tuple(body.emoji_sources),
                emoji_sub_types=tuple(body.emoji_sub_types),
                forward_messages=forward_messages,
            ),
            reason,
        )
        # DROP 不调用模型，但必须落一条可审计行动事件，回答「代码根本没让 Bot 考虑」。
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

    # 只有私聊与协议 @ 会在睡着时得到 force；先保留门控时的 asleep 审计事实，
    # 再打断时间线里的 sleep 段，避免名字命中或戳一戳旁路既有丢弃优先级。
    if asleep and gate_result.disposition == 'force':
        app_state.chat.wake_from_inbound(now)

    image_sources = tuple(body.image_sources)
    emoji_sources = tuple(body.emoji_sources)
    emoji_sub_types = tuple(body.emoji_sub_types)
    legacy_attachments = [image.model_dump() for image in body.images]
    if legacy_attachments and not image_sources:
        # 兼容旧协议：已带 Base64 的入站消息只能同步描述，新适配器不再走此分支。
        descriptions = await app_state.chat.describe_inbound_images(legacy_attachments)
        outbound_text = merge_image_descriptions(body.text, descriptions)
    else:
        # 新协议：先以 [图片] 占位符接收落库并返回，下载和 VLM 描述
        # 由聊天服务的后台任务完成，不阻塞 NapCat 的串行入站循环。
        outbound_text = body.text

    await app_state.chat.send(InboundMessage(
        text=outbound_text,
        context=context,
        mentioned_me=body.mentioned_me,
        external_message_id=body.external_message_id,
        bot_name=body.bot_name,
        image_sources=image_sources if not legacy_attachments else (),
        emoji_sources=emoji_sources,
        emoji_sub_types=emoji_sub_types,
        forward_messages=forward_messages,
        poked_me=body.poked_me,
        pokes_in_window=pokes_in_window,
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


class GroupBackfillMessageBody(BaseModel):
    """一条只观察不回复的群历史消息。"""

    model_config = ConfigDict(extra='forbid')

    external_message_id: str = Field(alias='externalMessageId')
    message_seq: int = Field(default=0, alias='messageSeq')
    created_at: int = Field(default=0, alias='createdAt')
    sender_external_id: str = Field(alias='senderExternalId')
    sender_nickname: str = Field(alias='senderNickname')
    sender_group_card: str = Field(default='', alias='senderGroupCard')
    text: str
    mentioned_me: bool = Field(default=False, alias='mentionedMe')


class GroupBackfillBody(BaseModel):
    """NapCat 启动/重连后提交的群历史回填批次。"""

    model_config = ConfigDict(extra='forbid')

    platform: str
    stream_external_id: str = Field(alias='streamExternalId')
    messages: List[GroupBackfillMessageBody]


class PlatformTypingBody(BaseModel):
    """平台适配器上报的一条「对方正在输入」通知。"""

    model_config = ConfigDict(extra='forbid')

    platform: str
    stream_kind: Literal['direct', 'group'] = Field(alias='streamKind')
    stream_external_id: str = Field(alias='streamExternalId')
    sender_external_id: str = Field(alias='senderExternalId')


class PlatformDeliveryFailureBody(BaseModel):
    """平台适配器上报的一次出站动作在协议端的失败。

    主体的投递回执只证明出站报文送到了适配器，真正的平台调用在适配器进程里才
    发出。没有这条回报时，协议端拒绝只留在适配器日志里，主体侧照常记
    outbound_delivered，控制台显示动作成功——现场表现为「她做了动作但对方什么
    都没收到」，且无法从账本查出。
    """

    model_config = ConfigDict(extra='forbid')

    platform: str
    stream_id: int = Field(alias='streamId')
    # 发起该投递的回合编号；0 表示投递没有回合上下文（如后台补发）。
    turn_id: int = Field(default=0, alias='turnId')
    action: Literal['send', 'react', 'poke']
    # 目标平台标识（QQ 号或消息编号）；发消息一类没有单独目标时为空串。
    target: str = ''
    error: str


@router.post('/platform/typing', dependencies=[Depends(_auth)])
async def platform_typing(body: PlatformTypingBody) -> JSONResponse:
    """接收对方正在输入的通知，交由聊天服务判断是否据此开口。

    协议端在对方打字期间会反复推送该通知，因此本路由必须保持廉价：只做归属
    解析并交给聊天服务，是否开口、开口说什么全部由服务层与模型决定。

    :param body: 已通过 Pydantic 校验的输入状态通知。
    :return: JSON 响应；服务未初始化时返回 503，其余情况返回是否触发了发言。
    :raises fastapi.HTTPException: 路由鉴权失败时由依赖项返回 401。
    :raises ValueError: 注册表归属解析输入不一致时抛出。
    副作用：可能触发一次主动消息的模型调用与平台投递。
    """
    if app_state.chat is None or app_state.registry is None:
        return JSONResponse({'detail': '对话服务未初始化'}, status_code=503)

    context = app_state.registry.resolve_existing_context(
        platform=body.platform,
        stream_kind=body.stream_kind,
        stream_external_id=body.stream_external_id,
        sender_external_id=body.sender_external_id,
    )
    if context is None:
        # 输入状态没有昵称等建档信息；必须先由一条真实私聊建立归属，不能凭瞬时
        # 通知创建空身份。正常会话里该分支只会出现在适配器先于消息恢复连接时。
        return JSONResponse({
            'accepted': False,
            'spoke': False,
            'reason': '会话或发送者身份尚未建立',
        })
    # 后端重启后 broker 是空的；输入状态可以在新的普通消息到达前命中一条已有
    # 私聊。此时必须像 platform_inbound 一样恢复 stream -> QQ driver 绑定，
    # 否则决策器即使选择 reply，也会在最终投递时因没有出站 driver 返回 500。
    if app_state.register_platform_stream is not None:
        app_state.register_platform_stream(context.stream)
    spoke = await app_state.chat.note_peer_typing(context)
    return JSONResponse({'accepted': True, 'spoke': spoke})


@router.post('/platform/delivery/failed', dependencies=[Depends(_auth)])
async def platform_delivery_failed(body: PlatformDeliveryFailureBody) -> JSONResponse:
    """接收适配器回报的出站动作失败，写入观察账本并渲染到控制台。

    只落账与展示，不做任何补偿投递：这类失败的原因基本都在平台侧（发包能力不可
    用、目标失效、频率限制），自动重发只会把一次可见失败变成对用户的反复骚扰。

    :param body: 已通过 Pydantic 校验的投递失败回报。
    :return: JSON 响应，恒为已接收。
    :raises fastapi.HTTPException: 路由鉴权失败时由依赖项返回 401。
    副作用：写入一条 delivery_failed 观察事件，并渲染一行控制台决策摘要。
    """
    trace.emit(
        'delivery_failed',
        turnId=body.turn_id,
        platform=body.platform,
        streamId=body.stream_id,
        action=body.action,
        target=body.target,
        detail=body.error,
    )
    render_action_decision(
        turn=body.turn_id,
        agent_scope='live',
        event_status='delivery_failed',
        detail=body.error,
        action=body.action,
    )
    return JSONResponse({'accepted': True})


@router.post('/platform/group/backfill', dependencies=[Depends(_auth)])
async def platform_group_backfill(body: GroupBackfillBody) -> JSONResponse:
    """接收停机期间错过的群历史，只写观察上下文不触发回复。

    :param body: 已通过 Pydantic 校验的群 ID 与历史消息列表。
    :return: 实际写入条数的 JSON 响应；服务未初始化时返回 503。
    """
    if app_state.chat is None or app_state.registry is None:
        return JSONResponse({'detail': '对话服务未初始化'}, status_code=503)
    if not body.messages:
        return JSONResponse({'written': 0})
    first = body.messages[0]
    context = app_state.registry.resolve_inbound(
        platform=body.platform,
        stream_kind='group',
        stream_external_id=body.stream_external_id,
        sender_external_id=first.sender_external_id,
        sender_nickname=first.sender_nickname,
        sender_group_card=first.sender_group_card,
        first_seen_at=first.created_at or current_time(),
    )
    written = app_state.chat.record_group_backfill(
        context,
        [message.model_dump(by_alias=True) for message in body.messages],
    )
    return JSONResponse({'written': written})


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


@router.get('/prompt-records', dependencies=[Depends(_auth)])
async def prompt_records(
    task: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    """列出分阶段调用记录的摘要，最近的在前。

    记录按模型任务分目录，多级 Agent 下同一回合会产生多份；``tasks`` 一并返回
    当前有记录的任务名，供面板做筛选而不必再发一次请求。

    :param task: 只看某个任务；省略时合并全部任务按时间排序。
    :param limit: 摘要条数上限，默认 50。
    :return: 含 ``records``、``tasks`` 与 ``enabled`` 的字典。
    """
    try:
        return {
            'enabled': True,
            'tasks': list_record_tasks(),
            'records': list_prompt_records(task, limit),
        }
    except RecordsDisabled:
        # 未启用与「启用但还没有记录」对使用者含义不同，用 enabled 区分开，
        # 不要都回空列表让人对着空面板等。
        return {'enabled': False, 'tasks': [], 'records': []}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get('/prompt-records/{task}/{name}', dependencies=[Depends(_auth)])
async def prompt_record(task: str, name: str) -> dict:
    """读取单份调用记录的完整内容，含全部请求消息与模型产出。"""
    try:
        return read_prompt_record(task, name)
    except RecordsDisabled as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


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


# ------------------------------------------------------- 黑话与表达方式（只读浏览）
# 以下两条路由只做 SELECT。两张表当前都是「只出不进」：条目全部来自一次性历史
# 迁移脚本，运行时只消费不新增——黑话的消费在 agent/jargon.py 的查表命中，表达
# 方式的消费在 services/chat.py 的候选池抽样与选择模型，两条链路都会回写使用
# 计数，但都不会 INSERT 新词条。表达方式行因此同时返回 use_count 与
# last_used_at：前者是累计量（含迁移带来的历史值），只有后者能区分「这条在本
# 部署真的被用过」和「这条只是迁移数据里频次高」。
# SQL 为完全静态文本：全部筛选值一律参数绑定，可选条件用 ``? IS NULL``
# 参数开关表达；LIKE 关键词先转义 %、_ 与 \，排序方向来自 Literal 枚举、
# 只决定执行哪一条静态语句，杜绝任何外部输入进 SQL 文本。


def _like_keyword(keyword: str) -> str:
    """把用户关键词转成转义后的 LIKE 模式。

    :param keyword: 原始关键词，调用方已去空白。
    :return: ``%关键词%`` 模式，其中 ``%``、``_``、``\\`` 已按 ESCAPE 子句转义。
    """
    escaped = (
        keyword
        .replace('\\', '\\\\')
        .replace('%', r'\%')
        .replace('_', r'\_')
    )
    return f'%{escaped}%'


def _list_jargon_rows(
    db: sqlite3.Connection,
    entry_status: str,
    stream_id: int | None,
    global_only: bool,
    keyword: str | None,
    limit: int,
    offset: int,
) -> tuple[list[dict], int]:
    """同步查询黑话词条一页与符合条件的总数。

    :param db: 进程级 SQLite 连接，由路由层取得后传入。
    :param entry_status: 只取该状态的词条（confirmed / pending）。
    :param stream_id: 只取该会话专属词条；``None`` 表示不限。
    :param global_only: 为真时只取全局词条（``stream_id IS NULL``）。
    :param keyword: 关键词，同时匹配词与含义；``None`` 表示不过滤。
    :param limit: 页大小。
    :param offset: 偏移量。
    :return: ``(词条字典列表, 总数)``。
    :raises sqlite3.Error: 查询失败时抛出，由路由层转换。
    """
    # 与查询文本占位符一一对应的绑定参数：NULL / 0 即关闭对应可选条件。
    keyword_pattern = _like_keyword(keyword) if keyword else None
    filters = [
        entry_status,               # status = ?
        stream_id, stream_id,       # ? IS NULL OR stream_id = ?
        1 if global_only else 0,    # ? = 0 OR stream_id IS NULL
        keyword_pattern, keyword_pattern, keyword_pattern,  # LIKE 开关与两个模式
    ]
    total = int(db.execute(
        '''SELECT COUNT(*) FROM jargon
           WHERE status = ?
             AND (? IS NULL OR stream_id = ?)
             AND (? = 0 OR stream_id IS NULL)
             AND (? IS NULL OR term LIKE ? ESCAPE '\\' OR meaning LIKE ? ESCAPE '\\')''',
        filters,
    ).fetchone()[0])
    rows = db.execute(
        '''SELECT id, term, meaning, stream_id, status, hits, source, created_at,
                  sightings, inferred_at_sightings
           FROM jargon
           WHERE status = ?
             AND (? IS NULL OR stream_id = ?)
             AND (? = 0 OR stream_id IS NULL)
             AND (? IS NULL OR term LIKE ? ESCAPE '\\' OR meaning LIKE ? ESCAPE '\\')
           ORDER BY id
           LIMIT ? OFFSET ?''',
        [*filters, limit, offset],
    ).fetchall()
    entries = [
        {
            'id': row['id'],
            'term': row['term'],
            'meaning': row['meaning'],
            'streamId': row['stream_id'],
            'status': row['status'],
            'hits': row['hits'],
            'source': row['source'],
            'createdAt': row['created_at'],
            'sightings': row['sightings'],
            'inferredAtSightings': row['inferred_at_sightings'],
        }
        for row in rows
    ]
    return entries, total


def _list_expression_rows(
    db: sqlite3.Connection,
    stream_id: int | None,
    checked: int | None,
    use_desc: bool,
    limit: int,
    offset: int,
) -> tuple[list[dict], int]:
    """同步查询表达方式一页与总数，按使用次数排序、id 作稳定次序。

    :param db: 进程级 SQLite 连接，由路由层取得后传入。
    :param stream_id: 只取该会话的表达；``None`` 表示不限。
    :param checked: 只取该复核状态的表达（0 未复核 / 1 已确认 / -1 已驳回）；
        ``None`` 表示不限。
    :param use_desc: 为真按使用次数降序，否则升序。
    :param limit: 页大小。
    :param offset: 偏移量。
    :return: ``(表达字典列表, 总数)``；每行含 ``useCount`` 累计次数、
        ``lastUsedAt`` 最近一次被选中的毫秒时间戳（从未被选中时为 ``None``）
        与 ``checked`` 复核状态。
    :raises sqlite3.Error: 查询失败时抛出，由路由层转换。
    """
    # ? IS NULL 参数开关：传 NULL 即关闭对应过滤，SQL 文本保持完全静态。
    filters = [stream_id, stream_id, checked, checked]
    total = int(db.execute(
        'SELECT COUNT(*) FROM expressions'
        ' WHERE (? IS NULL OR stream_id = ?) AND (? IS NULL OR checked = ?)',
        filters,
    ).fetchone()[0])
    if use_desc:
        rows = db.execute(
            '''SELECT id, situation, style, stream_id, use_count, source,
                      created_at, last_used_at, checked
               FROM expressions
               WHERE (? IS NULL OR stream_id = ?) AND (? IS NULL OR checked = ?)
               ORDER BY use_count DESC, id DESC
               LIMIT ? OFFSET ?''',
            [*filters, limit, offset],
        ).fetchall()
    else:
        rows = db.execute(
            '''SELECT id, situation, style, stream_id, use_count, source,
                      created_at, last_used_at, checked
               FROM expressions
               WHERE (? IS NULL OR stream_id = ?) AND (? IS NULL OR checked = ?)
               ORDER BY use_count ASC, id ASC
               LIMIT ? OFFSET ?''',
            [*filters, limit, offset],
        ).fetchall()
    entries = [
        {
            'id': row['id'],
            'situation': row['situation'],
            'style': row['style'],
            'streamId': row['stream_id'],
            'useCount': row['use_count'],
            'source': row['source'],
            'createdAt': row['created_at'],
            'lastUsedAt': row['last_used_at'],
            'checked': row['checked'],
        }
        for row in rows
    ]
    return entries, total


def _unreadable_db() -> HTTPException:
    """构造数据库未初始化的 503 异常，供两条只读路由复用。"""
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail='数据库未初始化',
    )


def _read_db_or_503() -> sqlite3.Connection:
    """取进程级数据库连接，把「未初始化」精确圈定在 get_db 这一步。

    RuntimeError 在查询路径里还可能有别的来源，那些不属于「未初始化」；
    因此只有这里捕获转换，路由层的其余异常一律按查询失败记录并返回 500。

    :return: 进程级 SQLite 连接。
    :raises fastapi.HTTPException: 尚未调用 ``open_db`` 时 503。
    """
    try:
        return get_db()
    except RuntimeError as exc:
        raise _unreadable_db() from exc


@router.get('/api/jargon', dependencies=[Depends(_auth)])
async def jargon_entries(
    entry_status: Literal['confirmed', 'pending'] = Query(
        default='confirmed', alias='status',
    ),
    stream_id: int | None = Query(default=None, alias='streamId', ge=1),
    global_only: bool = Query(default=False, alias='globalOnly'),
    keyword: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """分页浏览黑话词表，只读。

    ``streamId`` 选中某会话的专属词条，``globalOnly`` 只看全局词条，两者都不传
    则全部返回——词条响应里的 ``streamId`` 为 ``null`` 即全局，前端据此区分
    「全局通用」与「只在某个群成立」。默认只给已确认词条，待定候选需显式传
    ``status=pending``。

    :param entry_status: 词条状态过滤，默认 ``confirmed``。
    :param stream_id: 会话 ID 过滤；``None`` 表示不限。
    :param global_only: 为真时只返回全局词条。
    :param keyword: 关键词，同时匹配词条与含义。
    :param limit: 页大小，1 到 200，默认 50。
    :param offset: 偏移量，从 0 起。

    :return: ``entries`` 词条列表与 ``total`` 符合条件总数。
    :raises fastapi.HTTPException: 数据库未初始化时 503；查询失败时 500，
        完整 traceback 以 ``jargon_query_failed`` 事件落日志。

    副作用：
        仅读取 jargon 表，不写任何列，不影响运行时的提示词注入路径。
    """
    cleaned_keyword = keyword.strip() if keyword else None
    if cleaned_keyword == '':
        cleaned_keyword = None
    db = _read_db_or_503()
    try:
        entries, total = await run_in_thread(
            _list_jargon_rows,
            db, entry_status, stream_id, global_only, cleaned_keyword, limit, offset,
        )
    except Exception as exc:
        logger.exception('jargon_query_failed')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'黑话词表查询失败：{exc}',
        ) from exc
    return {'entries': entries, 'total': total, 'limit': limit, 'offset': offset}


class JargonUseBody(BaseModel):
    """会话级黑话 use 开关的写入体。"""

    enabled: bool


@router.get('/api/streams/{stream_id}/jargon/use', dependencies=[Depends(_auth)])
async def jargon_use_status(stream_id: int) -> dict:
    """读取一个会话的黑话 use 开关。

    :param stream_id: 会话 ID。
    :return: ``enabled`` 布尔状态；缺省为开。
    :raises fastapi.HTTPException: 数据库未初始化时 503。
    副作用：只读 ``meta`` 表。
    """
    db = _read_db_or_503()
    enabled = await run_in_thread(jargon_use_enabled, db, stream_id)
    return {'streamId': stream_id, 'enabled': enabled}


@router.put('/api/streams/{stream_id}/jargon/use', dependencies=[Depends(_auth)])
async def jargon_use_update(stream_id: int, body: JargonUseBody) -> dict:
    """写一个会话的黑话 use 开关。

    :param stream_id: 会话 ID。
    :param body: 目标状态。
    :return: 写入后的 ``enabled`` 实际状态。
    :raises fastapi.HTTPException: 数据库未初始化时 503；写入失败时 500，
        完整 traceback 以 ``jargon_use_write_failed`` 事件落日志。
    副作用：写 ``meta`` 表的 ``jargon:use:{stream_id}`` 键；下一回合的
        召回立即生效。
    """
    db = _read_db_or_503()
    try:
        enabled = await run_in_thread(set_jargon_use, db, stream_id, body.enabled)
    except Exception as exc:
        logger.exception('jargon_use_write_failed')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'黑话开关写入失败：{exc}',
        ) from exc
    return {'streamId': stream_id, 'enabled': enabled}


@router.get('/api/expressions', dependencies=[Depends(_auth)])
async def expression_entries(
    stream_id: int | None = Query(default=None, alias='streamId', ge=1),
    checked: int | None = Query(default=None, ge=-1, le=1),
    order: Literal['use_desc', 'use_asc'] = Query(default='use_desc'),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """分页浏览表达方式词表，只读。

    表达方式已接入回复生成（候选池抽样 → 选择模型），词表由回合收尾处的
    后台学习任务增补（source 为「本机学习」），人工复核只负责剔除与保护：
    确认（checked=1）的永不自动淘汰，驳回（checked=-1）的退出候选池。

    :param stream_id: 会话 ID 过滤；``None`` 表示不限。
    :param checked: 复核状态过滤；``None`` 表示不限。
    :param order: ``use_desc`` 按使用次数降序（默认），``use_asc`` 升序。
    :param limit: 页大小，1 到 200，默认 50。
    :param offset: 偏移量，从 0 起。

    :return: ``entries`` 表达列表与 ``total`` 符合条件总数。
    :raises fastapi.HTTPException: 数据库未初始化时 503；查询失败时 500，
        完整 traceback 以 ``expression_query_failed`` 事件落日志。

    副作用：
        仅读取 expressions 表，不写任何列。
    """
    db = _read_db_or_503()
    try:
        entries, total = await run_in_thread(
            _list_expression_rows,
            db, stream_id, checked, order == 'use_desc', limit, offset,
        )
    except Exception as exc:
        logger.exception('expression_query_failed')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'表达方式查询失败：{exc}',
        ) from exc
    return {'entries': entries, 'total': total, 'limit': limit, 'offset': offset}


class ExpressionCheckedBody(BaseModel):
    """表达方式人工复核写入体。"""

    model_config = ConfigDict(extra='forbid')

    checked: Literal[-1, 0, 1]


def _set_expression_checked(
    db: sqlite3.Connection,
    expression_id: int,
    checked: int,
) -> bool:
    """同步写一条表达方式的复核状态，只动 ``checked`` 一列。

    :param db: 进程级 SQLite 连接，由路由层取得后传入。
    :param expression_id: 表达方式行 ID。
    :param checked: 目标复核状态（0 未复核 / 1 已确认 / -1 已驳回）。
    :return: 目标行存在且已写入为 ``True``；行不存在为 ``False``。
    :raises sqlite3.Error: 写入失败时抛出，由路由层转换。
    """
    cursor = db.execute(
        'UPDATE expressions SET checked = ? WHERE id = ?',
        (checked, expression_id),
    )
    db.commit()
    return cursor.rowcount > 0


@router.put('/api/expressions/{expression_id}/checked', dependencies=[Depends(_auth)])
async def expression_checked_update(expression_id: int, body: ExpressionCheckedBody) -> dict:
    """人工复核一条表达方式，只写 ``checked``。

    复核不是使用的前置条件（未复核照常进候选池），它的职责是剔除与保护：
    确认（1）的永不参与自动淘汰，驳回（-1）的立刻退出候选池但不删除——
    删掉的表达学习器下次可能又学回来，-1 是「这条判过了」的记号。

    :param expression_id: 表达方式行 ID。
    :param body: 目标复核状态。
    :return: 写入后的 ``id`` 与 ``checked`` 实际状态。
    :raises fastapi.HTTPException: 数据库未初始化时 503；行不存在时 404；
        写入失败时 500，完整 traceback 以 ``expression_checked_write_failed``
        事件落日志。
    副作用：写 expressions 一行的 ``checked`` 列；候选池下一次取池即生效。
    """
    db = _read_db_or_503()
    try:
        found = await run_in_thread(
            _set_expression_checked, db, expression_id, body.checked,
        )
    except Exception as exc:
        logger.exception('expression_checked_write_failed')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'表达方式复核写入失败：{exc}',
        ) from exc
    if not found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='表达方式不存在',
        )
    return {'id': expression_id, 'checked': body.checked}


def _delete_expression(db: sqlite3.Connection, expression_id: int) -> bool:
    """同步删除一条表达方式。

    删除只由人在界面上发起。后台淘汰任务的范围被限定在本机学习产出上
    （见 ``agent/expression_learn.py`` 的 ``eliminate_stale``），迁移带来的存量
    一条都不自动删——存量里有整个会话的行从未被本机选中过，自动清理会让该
    会话候选池归零、表达选择停摆。存量的取舍因此走这条人工路径。

    :param db: 进程级 SQLite 连接，由路由层取得后传入。
    :param expression_id: 表达方式行 ID。
    :return: 目标行存在且已删除为 ``True``；行不存在为 ``False``。
    :raises sqlite3.Error: 删除失败时抛出，由路由层转换。
    """
    cursor = db.execute('DELETE FROM expressions WHERE id = ?', (expression_id,))
    db.commit()
    return cursor.rowcount > 0


@router.delete('/api/expressions/{expression_id}', dependencies=[Depends(_auth)])
async def expression_delete(expression_id: int) -> dict:
    """删除一条表达方式。

    与复核（``checked = -1``）的区别：驳回是可逆的记号，行还在、只是退出候选池，
    学习器再学到同样的说法时 ``UNIQUE`` 约束会撞上它、不会重复插入；删除是不可逆
    的，同样的说法以后可以被重新学回来。清理迁移存量用删除，压制某条说法用驳回。

    :param expression_id: 表达方式行 ID。
    :return: 被删除的 ``id``。
    :raises fastapi.HTTPException: 数据库未初始化时 503；行不存在时 404；
        删除失败时 500，完整 traceback 以 ``expression_delete_failed``
        事件落日志。
    副作用：从 expressions 表删除一行；候选池下一次取池即生效。
    """
    db = _read_db_or_503()
    try:
        found = await run_in_thread(_delete_expression, db, expression_id)
    except Exception as exc:
        logger.exception('expression_delete_failed')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'表达方式删除失败：{exc}',
        ) from exc
    if not found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='表达方式不存在',
        )
    return {'id': expression_id}


class ExpressionBatchDeleteBody(BaseModel):
    """批量删除表达方式的请求体。"""

    ids: List[int] = Field(min_length=1, max_length=200)


def _delete_expressions(
    db: sqlite3.Connection,
    ids: List[int],
) -> tuple[int, list[dict]]:
    """同步批量删除表达方式，并回报因此跌破下限的会话候选池。

    受影响的会话必须在删除之前取：整条会话的表达被删光时它在 expressions 里
    不再有行，事后的 ``GROUP BY stream_id`` 不会出现该会话，而候选池归零正是
    最需要上报的情形。

    候选池只做如实回报、不做拦截。跌破 :data:`MIN_POOL_CANDIDATES` 时
    ``fetch_expression_pool`` 直接返回空池，表达注入静默停摆——这件事必须让
    人在界面上立刻看见，而不是由后端替人把删除拦下来。

    :param db: 进程级 SQLite 连接，由路由层取得后传入。
    :param ids: 待删除的行 ID 列表；不存在的 ID 静默跳过，只计入实际删除数。
    :return: ``(实际删除行数, 低于下限的候选池列表)``；列表元素含 ``streamId``
        与删除后剩余的 ``candidates``，无一跌破时为空列表。
    :raises sqlite3.Error: 删除失败时抛出，由路由层转换。
    副作用：从 expressions 表删除若干行并提交。
    """
    # 占位符由 '?' 拼成、数量取自列表长度，参数仍走绑定，不存在注入面。
    marks = ','.join('?' * len(ids))
    affected = [
        int(row[0])
        for row in db.execute(
            f'SELECT DISTINCT stream_id FROM expressions'
            f' WHERE id IN ({marks}) AND stream_id IS NOT NULL',
            ids,
        )
    ]
    cursor = db.execute(f'DELETE FROM expressions WHERE id IN ({marks})', ids)
    db.commit()
    low_pools = []
    for stream_id in affected:
        candidates = int(db.execute(
            'SELECT COUNT(*) FROM expressions WHERE stream_id = ? AND checked = 1',
            (stream_id,),
        ).fetchone()[0])
        if candidates < MIN_POOL_CANDIDATES:
            low_pools.append({'streamId': stream_id, 'candidates': candidates})
    return cursor.rowcount, low_pools


@router.post('/api/expressions/batch-delete', dependencies=[Depends(_auth)])
async def expression_batch_delete(body: ExpressionBatchDeleteBody) -> dict:
    """批量删除表达方式，一次事务删完并回报候选池风险。

    与逐条删除同语义，只是省去反复往返：删除不可逆，同样的说法日后可以被重新
    学到；要让某条永久停止生效用「驳回」。

    单次上限 200 条，与列表路由的 ``limit`` 上限同值。界面的选择集跨页累积、
    没有条数上限，因此由前端按这个值分批发出；上限留在这里是为了给单次请求的
    SQL 占位符数量和事务时长封顶，不作为业务约束。

    :param body: 含 ``ids`` 的请求体；ID 不存在时静默跳过，不报 404——批量场景
        下并发删除造成的部分失效属正常，整批因此回滚反而更难用。
    :return: ``deleted`` 实际删除行数、``requested`` 请求条数，以及 ``lowPools``
        删除后候选数跌破下限的会话（含 ``streamId`` 与剩余 ``candidates``）。
    :raises fastapi.HTTPException: 数据库未初始化时 503；删除失败时 500，完整
        traceback 以 ``expression_batch_delete_failed`` 事件落日志。
    副作用：从 expressions 表删除若干行；候选池下一次取池即生效。
    """
    db = _read_db_or_503()
    try:
        deleted, low_pools = await run_in_thread(_delete_expressions, db, body.ids)
    except Exception as exc:
        logger.exception('expression_batch_delete_failed')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'表达方式批量删除失败：{exc}',
        ) from exc
    logger.info(
        'expression_batch_deleted',
        deleted=deleted,
        requested=len(body.ids),
        lowPools=len(low_pools),
    )
    return {'deleted': deleted, 'requested': len(body.ids), 'lowPools': low_pools}


# --------------------------------------------------------------- 联想网络只读

def _memory_payloads(
    db: sqlite3.Connection,
    refs: dict[int, tuple[str, int]],
    now: int,
) -> dict[int, dict]:
    """按层批量取回节点指向的记忆正文与留存度。

    指针表只存 ``(ref_kind, ref_id)``，正文分散在 facts / episodes / knowledge
    三张表。这里按层各查一次而不是逐节点查，是因为节点数随边一起增长，逐节点
    查会让整张图的加载退化成 O(节点数) 次往返。

    留存度口径与联想层内部的节点打分保持一致：facts 走衰减曲线，episodes 与
    knowledge 不衰减恒为 ``1.0``。三层都可能出现指针还在、被指向的行已经删掉的
    情况，那种节点以 ``alive=False`` 如实返回，不静默丢弃——图上少一个点比画一个
    灰点更难排查。

    :param db: 当前库连接。
    :param refs: ``{节点 ID: (ref_kind, ref_id)}``。
    :param now: 当前毫秒时间戳，用于计算 facts 的留存度。
    :return: ``{节点 ID: 节点字典}``，字段见 :func:`memory_graph` 的响应说明。
    :raises sqlite3.Error: 查询失败。
    副作用：只读。
    """
    by_kind: dict[str, dict[int, list[int]]] = {'fact': {}, 'episode': {}, 'knowledge': {}}
    for node, (ref_kind, ref_id) in refs.items():
        by_kind.setdefault(ref_kind, {}).setdefault(ref_id, []).append(node)

    out: dict[int, dict] = {}

    fact_ids = list(by_kind['fact'])
    if fact_ids:
        marks = ','.join('?' * len(fact_ids))
        for row in db.execute(
            f'SELECT id, person_id, kind, content, strength, half_life_hours, updated_at, '
            f'hit_count, active FROM facts WHERE id IN ({marks})',
            fact_ids,
        ).fetchall():
            keep = retention(
                float(row['strength']), int(row['updated_at']),
                float(row['half_life_hours']), now,
            )
            for node in by_kind['fact'][int(row['id'])]:
                out[node] = {
                    'text': row['content'],
                    'label': row['kind'],
                    'personId': row['person_id'],
                    'retention': round(keep, 4),
                    'updatedAt': row['updated_at'],
                    'hits': row['hit_count'],
                    # 事实被冻结时 active=0，但节点仍留在图上：遗忘不等于抹除。
                    'alive': bool(row['active']),
                }

    episode_ids = list(by_kind['episode'])
    if episode_ids:
        marks = ','.join('?' * len(episode_ids))
        for row in db.execute(
            f'SELECT id, summary, kind, ended_at FROM episodes WHERE id IN ({marks})',
            episode_ids,
        ).fetchall():
            for node in by_kind['episode'][int(row['id'])]:
                out[node] = {
                    'text': row['summary'],
                    'label': row['kind'],
                    'personId': None,
                    'retention': 1.0,
                    'updatedAt': row['ended_at'],
                    'hits': None,
                    'alive': True,
                }

    knowledge_ids = list(by_kind['knowledge'])
    if knowledge_ids:
        marks = ','.join('?' * len(knowledge_ids))
        for row in db.execute(
            f'SELECT id, content, source, created_at, hit_count FROM knowledge WHERE id IN ({marks})',
            knowledge_ids,
        ).fetchall():
            for node in by_kind['knowledge'][int(row['id'])]:
                out[node] = {
                    'text': row['content'],
                    'label': row['source'],
                    'personId': None,
                    'retention': 1.0,
                    'updatedAt': row['created_at'],
                    'hits': row['hit_count'],
                    'alive': True,
                }

    for node, (ref_kind, ref_id) in refs.items():
        if node in out:
            continue
        # 指针指向的行已经不在了。保留节点并标注，便于发现「谁删了记忆没清指针」。
        out[node] = {
            'text': f'（{ref_kind} #{ref_id} 已不存在）',
            'label': '', 'personId': None, 'retention': 0.0,
            'updatedAt': 0, 'hits': None, 'alive': False,
        }
    return out


def _memory_graph_rows(
    db: sqlite3.Connection,
    limit: int,
    include_frozen: bool,
    now: int,
) -> dict:
    """读出联想网络的节点、边与统计，供观察面板绘图。

    只返回有边的节点：孤立节点在图上无法解释，「记忆尚未与任何内容关联」由
    统计中的 ``isolated`` 计数表达。

    节点超过 ``limit`` 时按度数降序截断，保留连接最密的核心；随后丢弃任一端点
    被截断的边，避免出现指向图外的悬空线。截断与否由 ``stats.truncated`` 标注。

    :param db: 当前库连接。
    :param limit: 节点数上限。
    :param include_frozen: 是否把已冻结（``active=0``）的边一并返回。
    :param now: 当前毫秒时间戳。
    :return: ``nodes`` / ``edges`` / ``stats`` 三段的字典。
    :raises sqlite3.Error: 查询失败。
    副作用：只读。
    """
    edge_rows = db.execute(
        'SELECT id, source_id, target_id, strength, updated_at, active FROM memory_edges'
    ).fetchall()

    kept: list[dict] = []
    frozen_total = 0
    for row in edge_rows:
        active = bool(row['active'])
        if not active:
            frozen_total += 1
        if not active and not include_frozen:
            continue
        keep = retention(
            float(row['strength']), int(row['updated_at']), EDGE_HALF_LIFE_HOURS, now,
        )
        kept.append({
            'id': row['id'],
            'source': row['source_id'],
            'target': row['target_id'],
            'strength': round(float(row['strength']), 4),
            'retention': round(keep, 4),
            'updatedAt': row['updated_at'],
            'active': active,
        })

    degree: dict[int, int] = {}
    for edge in kept:
        degree[edge['source']] = degree.get(edge['source'], 0) + 1
        degree[edge['target']] = degree.get(edge['target'], 0) + 1

    ordered = sorted(degree, key=lambda node: (-degree[node], node))
    selected = set(ordered[:limit])
    truncated = len(ordered) > limit
    if truncated:
        kept = [e for e in kept if e['source'] in selected and e['target'] in selected]

    refs: dict[int, tuple[str, int]] = {}
    if selected:
        marks = ','.join('?' * len(selected))
        for row in db.execute(
            f'SELECT id, ref_kind, ref_id FROM memory_nodes WHERE id IN ({marks})',
            list(selected),
        ).fetchall():
            refs[int(row['id'])] = (str(row['ref_kind']), int(row['ref_id']))

    payloads = _memory_payloads(db, refs, now)
    nodes = [
        {
            'id': node,
            'kind': refs[node][0],
            'refId': refs[node][1],
            'degree': degree.get(node, 0),
            **payloads[node],
        }
        for node in sorted(refs)
    ]

    node_total = int(db.execute('SELECT COUNT(*) FROM memory_nodes').fetchone()[0])
    edge_total = len(edge_rows)
    spread_runs = int(db.execute(
        "SELECT COUNT(*) FROM pipeline_events WHERE kind = 'memory_spread'"
    ).fetchone()[0])
    return {
        'nodes': nodes,
        'edges': kept,
        'stats': {
            'nodeTotal': node_total,
            'edgeTotal': edge_total,
            'activeEdges': edge_total - frozen_total,
            'frozenEdges': frozen_total,
            # 有节点却一条边都没有，等于还没和别的记忆一起被点亮过。
            'isolated': node_total - len(degree),
            # 扩散实际跑过几次。为 0 说明建边在跑但从没被读过，这条只能从账本看出来。
            'spreadRuns': spread_runs,
            'truncated': truncated,
        },
    }


def _memory_spread_rows(
    db: sqlite3.Connection,
    ref_kind: str,
    ref_id: int,
    hops: int,
    limit: int,
    now: int,
) -> list[dict]:
    """从指定节点出发跑一次扩散，返回带正文的命中列表。

    直接调用运行时的 :func:`~src.core.memory.association.spread`，不另写一份预览
    实现：面板要回答的是「Bot 真的会想起什么」，重写一遍就只能回答「我以为会想起
    什么」。该函数已声明只读，不建边也不加强，因此面板反复点不会污染边权。

    与真机的唯一差别是不传短期激活表——面板没有对话上下文，也就没有「刚才聊到
    过」这回事；调用方须在界面上说明这一点。

    :param db: 当前库连接。
    :param ref_kind: 种子所在层。
    :param ref_id: 种子在该层内的主键。
    :param hops: 最多走几跳。
    :param limit: 结果条数上限。
    :param now: 当前毫秒时间戳。
    :return: 按 score 降序的命中字典列表。
    :raises ValueError: ``ref_kind`` 不在允许集合内，由 spread 内部抛出。
    :raises sqlite3.Error: 查询失败。
    副作用：只读。
    """
    hits = spread(db, [(ref_kind, ref_id, 1.0)], now, hops=hops, limit=limit, activation=None)
    refs = {hit.node_id: (hit.ref_kind, hit.ref_id) for hit in hits}
    payloads = _memory_payloads(db, refs, now)
    return [
        {
            'id': hit.node_id,
            'kind': hit.ref_kind,
            'refId': hit.ref_id,
            'score': round(hit.score, 4),
            'hops': hit.hops,
            **payloads[hit.node_id],
        }
        for hit in hits
    ]


@router.get('/api/memory/graph', dependencies=[Depends(_auth)])
async def memory_graph(
    limit: int = Query(default=200, ge=10, le=1000),
    include_frozen: bool = Query(default=False, alias='includeFrozen'),
) -> dict:
    """读出整张联想网络，只读。

    节点字段：``id`` 指针表主键、``kind`` 所在层、``refId`` 层内主键、``text``
    正文、``label`` 分类（fact 取 kind，knowledge 取 source）、``personId``、
    ``retention`` 当前留存度、``degree`` 度数、``hits`` 命中次数、``alive``
    指向的记忆是否仍存在。边字段：``strength`` 存量强度与 ``retention`` 折算到
    此刻的实际强度——两者分开给，是因为「这条边有多强」与「它多久没被用了」在
    图上要用不同的视觉通道表达。

    :param limit: 节点数上限，10 到 1000，默认 200；超出按度数降序截断。
    :param include_frozen: 为真时把已冻结的边一并返回，用于观察被遗忘的连接。

    :return: ``nodes`` / ``edges`` / ``stats`` 三段。
    :raises fastapi.HTTPException: 数据库未初始化时 503；查询失败时 500，
        完整 traceback 以 ``memory_graph_failed`` 事件落日志。

    副作用：
        仅读取 memory_nodes / memory_edges 与三层记忆表，不建边也不加强。
    """
    db = _read_db_or_503()
    try:
        return await run_in_thread(
            _memory_graph_rows, db, limit, include_frozen, current_time(),
        )
    except Exception as exc:
        logger.exception('memory_graph_failed')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'联想网络查询失败：{exc}',
        ) from exc


@router.get('/api/memory/spread', dependencies=[Depends(_auth)])
async def memory_spread_preview(
    kind: Literal['fact', 'episode', 'knowledge'] = Query(...),
    ref_id: int = Query(..., alias='refId', ge=1),
    hops: int = Query(default=HOPS, ge=1, le=3),
    limit: int = Query(default=SPREAD_LIMIT, ge=1, le=20),
) -> dict:
    """以指定记忆为种子跑一次扩散预览，只读。

    面板据此回答「从这里出发 Bot 会顺带想起什么」。走的是运行时同一份 spread
    实现，因此结果与 Bot 真实的联想一致；唯一差别是没有短期激活加成（面板没有
    对话上下文），界面上标注为「不含刚才聊到过的加成」。

    :param kind: 种子所在层。
    :param ref_id: 种子在该层内的主键。
    :param hops: 最多走几跳，1 到 3，默认取运行时的 ``HOPS``。
    :param limit: 结果条数上限，1 到 20，默认取运行时的 ``SPREAD_LIMIT``。

    :return: ``hits`` 命中列表、``seed`` 种子标识与本次使用的 ``hops`` / ``limit``。
    :raises fastapi.HTTPException: 数据库未初始化时 503；查询失败时 500，
        完整 traceback 以 ``memory_spread_failed`` 事件落日志。

    副作用：
        只读，不建边也不加强边——建边只发生在真实召回里「被采用」之后。
    """
    db = _read_db_or_503()
    try:
        hits = await run_in_thread(
            _memory_spread_rows, db, kind, ref_id, hops, limit, current_time(),
        )
    except Exception as exc:
        logger.exception('memory_spread_failed')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'扩散预览失败：{exc}',
        ) from exc
    return {'hits': hits, 'seed': {'kind': kind, 'refId': ref_id}, 'hops': hops, 'limit': limit}


def _emoji_library_or_503() -> Any:
    """返回已装配的表情包库，未初始化时按 503 拒绝。"""

    library = app_state.emoji_library
    if library is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='表情包库尚未初始化',
        )
    return library


class EmojiBanBody(BaseModel):
    """封禁请求体：只带可选原因。"""

    reason: str = ''


@router.get('/api/emojis', dependencies=[Depends(_auth)])
async def emoji_entries(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    banned: bool | None = Query(default=None),
) -> dict:
    """分页浏览表情包库，附带库容量总览。

    排序与后台淘汰同口径（use_count 升序、last_used_at 升序），页面看到的
    先后就是真的会先被淘汰的先后；未使用过的记录 last_used_at 为 null。

    :param limit: 页大小，1 到 200，默认 50。
    :param offset: 偏移量，从 0 起。
    :param banned: ``true`` 只看已封禁、``false`` 只看未封禁、省略则不筛选。
    :return: entries 表情包记录列表、total 当前筛选下的条数与 stats 容量
        总览。``total`` 跟着筛选走（否则翻页会翻出空白页），而 stats 里的数
        始终是全库口径。
    :raises fastapi.HTTPException: 服务未初始化 503；查询失败 500。

    副作用：
        只读 emoji 表、封禁表与目录元数据，不修改任何内容。
    """
    library = _emoji_library_or_503()
    try:
        entries = await run_in_thread(library.page, limit, offset, banned)
        total = await run_in_thread(library.count_entries, banned)
        stats = await run_in_thread(library.stats)
    except Exception as exc:
        logger.exception('emoji_query_failed')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'表情包列表查询失败：{exc}',
        ) from exc
    return {
        'entries': entries,
        'total': total,
        'stats': stats,
        'limit': limit,
        'offset': offset,
    }


@router.get('/api/emojis/{content_hash}/thumbnail', dependencies=[Depends(_auth)])
async def emoji_thumbnail(content_hash: str) -> Response:
    """返回一张表情包的原图字节，供管理页缩略图使用。

    :param content_hash: 64 位十六进制内容哈希。
    :return: 按扩展名给出 MIME 类型的图片响应。
    :raises fastapi.HTTPException: 服务未初始化 503；哈希非法 400；记录或
        文件不存在 404。
    """
    library = _emoji_library_or_503()
    try:
        path = await run_in_thread(library.file_path, content_hash)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='表情包记录或文件不存在',
        )
    try:
        data = await run_in_thread(path.read_bytes)
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'表情包文件读取失败：{exc}',
        ) from exc
    media_type = {
        '.gif': 'image/gif',
        '.png': 'image/png',
        '.webp': 'image/webp',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
    }.get(path.suffix.lower(), 'image/jpeg')
    return Response(content=data, media_type=media_type)


@router.post('/api/emojis/{content_hash}/ban', dependencies=[Depends(_auth)])
async def emoji_ban(content_hash: str, body: EmojiBanBody) -> dict:
    """按内容哈希封禁一张图；封禁独立于 emoji 行存在。

    :param content_hash: 64 位十六进制内容哈希。
    :param body: 可选封禁原因。
    :return: ok 与本次是否新增封禁。
    :raises fastapi.HTTPException: 服务未初始化 503；哈希非法 400。
    """
    library = _emoji_library_or_503()
    try:
        banned = await run_in_thread(library.ban, content_hash, body.reason)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {'ok': True, 'banned': banned}


@router.post('/api/emojis/{content_hash}/unban', dependencies=[Depends(_auth)])
async def emoji_unban(content_hash: str) -> dict:
    """解除一条封禁记录。

    :param content_hash: 64 位十六进制内容哈希。
    :return: ok 与本次是否确实删除了封禁。
    :raises fastapi.HTTPException: 服务未初始化 503；哈希非法 400。
    """
    library = _emoji_library_or_503()
    try:
        unbanned = await run_in_thread(library.unban, content_hash)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {'ok': True, 'unbanned': unbanned}


@router.delete('/api/emojis/{content_hash}', dependencies=[Depends(_auth)])
async def emoji_delete(content_hash: str) -> dict:
    """删除一条表情包记录及其磁盘文件。

    :param content_hash: 64 位十六进制内容哈希。
    :return: ok 与本次是否确实删除了记录。
    :raises fastapi.HTTPException: 服务未初始化 503；哈希非法 400。
    """
    library = _emoji_library_or_503()
    try:
        removed = await run_in_thread(library.remove, content_hash)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {'ok': True, 'removed': removed}


@router.post('/system/config/reload', dependencies=[Depends(_auth), Depends(_require_loopback)])
async def system_config_reload() -> dict:
    """重读配置目录并热应用第 1 类字段；失败时保持原配置并完整报错。

    与重启的区别：模型客户端、日志管道、可选服务装配等启动期形态（第 2 类）
    不受影响，变更清单里会逐字段标注「需要重启才生效」；被拷进实例属性的
    第 3 类同样只标注不生效。不做「失败就用旧配置继续跑」的静默兜底——
    校验不过直接报错，进程保持原配置，日志与 WebUI 都能看到原因。

    :return: ``ok`` 恒为真；``changedFields`` 为标注后的变更字段行，
        空列表表示配置没有变化。

    :raises fastapi.HTTPException: 配置读取或校验失败时 400，detail 带完整原因；
        此时全局配置保持原状，本次重载没有生效。

    副作用：
        成功路径替换进程级配置单例并通知持有方（控制台信息框加
        ``config_reloaded`` 日志事件）；失败路径以 ``config_reload_failed``
        落完整 traceback。
    """
    try:
        _, summary = await asyncio.to_thread(reload_config)
    except Exception as exc:
        logger.exception('config_reload_failed')
        print_box('配置热重载失败', [
            '已保持原配置继续运行，本次重载没有生效。',
            f'原因：{exc}',
        ])
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'配置重载失败，已保持原配置：{exc}',
        ) from exc
    print_box('配置热重载完成', summary if summary else ['没有字段发生变化。'])
    return {'ok': True, 'changedFields': summary}
