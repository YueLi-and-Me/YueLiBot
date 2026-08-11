"""
视觉服务：他问起屏幕时截一帧，交给视觉模型描述。

★ 只有一条路径：**他主动问，才看**。
  曾经还有一条后台轮询链路（每 12s 截图 + 帧差 + 关键帧序列 + 全局瞥视冷却
  + 按 context 缓存描述），目标是「全时态感知」。它带来的复杂度远超收益——
  光时间常量就有九个横跨两种语言、互相之间还有隐式顺序依赖，排错时要同时
  在脑子里放着九个数字，实际表现却是她拿着几分钟前的旧描述当现在讲。
  现在整条删掉：他不问，就不看。

隐私约束：
  · 截图绝不落盘，用完即弃
  · 截多大范围由 vision.capture_mode 决定（window / screen），见 capture.ts
  · 窗口标题永不外传；进程名会（见 awareness/classify.py 的说明）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol

import asyncio
import base64
import re

from src.common.clock import now as current_time
from src.common.logger import get_logger
from src.config.schema import Config
from src.llm_models.openai import LlmError
from src.observe import events as trace
from src.prompts.registry import get_prompt, prompt_metadata

logger = get_logger(__name__)

# 复用窗口：这段时间内重复提问直接返回上一次的描述，不再打模型。
# 防的是「看看我屏幕」「现在呢」这种连着问，每句都摊一次视觉调用。
CHAT_GLANCE_COOLDOWN_MS = 20_000
# 描述的有效期。★ 必须 >= 复用冷却，否则会出现「过期了但还不允许重新调用」
# 的死窗口。两者管的是不同的事：冷却管「要不要重新花钱看」，TTL 管「这条
# 描述还能不能算数」。缺了 TTL 就会拿旧描述冒充当前画面，她会理直气壮地
# 描述一个早就关掉的界面。
CHAT_GLANCE_TTL_MS = 60_000
# 单次调用的截止时间。★ 视觉 provider 用的是 api_provider 那套参数
# （timeout_ms 默认 120s + max_retries 2），最坏能跑好几分钟——那是给后台
# 任务用的，而这里他正等着回话。也必须明显小于 Electron 侧的
# CHAT_GLANCE_TIMEOUT，否则那边先 abort，请求被掐断会让这里抛
# CancelledError，在 uvicorn 里表现成一整屏 ASGI 报错。
CHAT_GLANCE_DEADLINE_S = 8.0


@dataclass(frozen=True)
class VisionFailure:
    """一次视觉调用没有得到描述时的可回放诊断。"""

    error_type: str
    error_kind: str = ''
    status_code: int | None = None
    response_excerpt: str = ''

    def as_trace(self) -> dict[str, str | int | None]:
        return {
            'errorType': self.error_type,
            'errorKind': self.error_kind or None,
            'statusCode': self.status_code,
            'responseExcerpt': self.response_excerpt or None,
        }


@dataclass(frozen=True)
class VisionCallResult:
    """模型输出与失败诊断必须成对返回，不能靠 logger 猜原因。"""

    description: str | None
    failure: VisionFailure | None = None


def _safe_response_excerpt(value: str) -> str:
    """错误响应进 trace 前去掉可能出现的凭证，并限制体积。"""
    redacted = re.sub(
        r'(?i)(authorization["\']?\s*[:=]\s*["\']?bearer\s+|bearer\s+)[^\s,;"\']+',
        r'\1[REDACTED_SECRET]',
        value.strip(),
    )
    return redacted[:400]


def _llm_failure(exc: LlmError) -> VisionFailure:
    """把客户端已分类的错误带进 trace，供现场排障而不是猜测。"""
    status_match = re.search(r'HTTP\s+(\d{3})', str(exc))
    return VisionFailure(
        error_type=type(exc).__name__,
        error_kind=exc.kind,
        status_code=int(status_match.group(1)) if status_match else None,
        response_excerpt=_safe_response_excerpt(exc.detail or str(exc)),
    )


class VisionProvider(Protocol):
    """视觉服务实际依赖的最小模型接口。"""

    model: str

    def stream(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.85,
        max_tokens: int | None = None,
        signal: asyncio.Event | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        ...


class VisionService:
    def __init__(self, cfg: Config,
                 push_event: Callable[[str, dict[str, Any]], Awaitable[None]],
                 provider: VisionProvider | None) -> None:
        self._cfg = cfg
        self._push_event = push_event
        self._provider = provider
        self._chat_glance: tuple[str, int] | None = None   # (描述, 生成时刻)
        self._protocol_error: str | None = None
        self._glances = 0

    def stats(self) -> dict[str, Any]:
        return {
            'enabled': self._cfg.vision.enabled,
            'available': self._protocol_error is None,
            'error': self._protocol_error,
            'looks': self._glances,
        }

    def chat_glance(self, ttl_ms: int = CHAT_GLANCE_TTL_MS) -> str | None:
        """最近一次屏幕描述，过期返回 None。

        ★ 必须判 TTL。宁可没有，也不能拿旧的冒充现在的——截图失败时旧描述
          继续被当成「你刚瞥了一眼屏幕」，她就会描述一个早就关掉的界面。
        """
        if not self._chat_glance:
            return None
        text, at = self._chat_glance
        if current_time() - at > ttl_ms:
            return None
        return text

    async def glance(self, jpeg_bytes: bytes, app: str = '') -> str | None:
        """看一眼当前画面。只在他问起屏幕时被调用（见 Electron 侧 screenIntent）。"""
        if not self._cfg.vision.enabled or not jpeg_bytes or self._protocol_error:
            trace.emit('vision_glance', result='skipped',
                       enabled=self._cfg.vision.enabled, bytes=len(jpeg_bytes),
                       error=self._protocol_error)
            return self.chat_glance()

        now = current_time()
        if self._chat_glance and now - self._chat_glance[1] < CHAT_GLANCE_COOLDOWN_MS:
            trace.emit('vision_glance', result='cached',
                       ageMs=now - self._chat_glance[1], app=app)
            return self._chat_glance[0]

        await self._push_event('vision.watching', {'watching': True})
        try:
            result = await asyncio.wait_for(
                self._call_vision_model(jpeg_bytes, app),
                timeout=CHAT_GLANCE_DEADLINE_S,
            )
        except asyncio.TimeoutError:
            logger.warning('chat_glance_timeout', seconds=CHAT_GLANCE_DEADLINE_S,
                           hint='视觉接口太慢，这一轮 Bot 会如实说看不到；持续出现就换视觉模型')
            trace.emit('vision_glance', result='timeout', seconds=CHAT_GLANCE_DEADLINE_S, app=app)
            result = VisionCallResult(
                description=None,
                failure=VisionFailure(
                    error_type='TimeoutError',
                    error_kind='network',
                    response_excerpt=f'视觉调用超过 {CHAT_GLANCE_DEADLINE_S:g} 秒截止时间',
                ),
            )
        finally:
            await self._push_event('vision.watching', {'watching': False})

        description = result.description
        if description:
            self._chat_glance = (description, now)
            self._glances += 1
            logger.info('chat_glance_description', chars=len(description))
            trace.emit('vision_glance', result='ok', app=app, text=description)
        else:
            logger.info('chat_glance_empty', reason='模型返回空描述或调用失败，详见上一条 vision_call_failed')
            failure = result.failure or VisionFailure(error_type='EmptyResponse')
            trace.emit('vision_glance', result='empty', app=app, **failure.as_trace())
        # ★ 失败时返回 None 而不是退回旧缓存——调用方需要知道这次没看成。
        return description

    async def _call_vision_model(self, jpeg_bytes: bytes, app: str = '') -> VisionCallResult:
        if not self._provider:
            return VisionCallResult(None, VisionFailure(error_type='ProviderUnavailable'))
        if self._protocol_error:
            return VisionCallResult(
                None,
                VisionFailure(
                    error_type='ProtocolError',
                    response_excerpt=_safe_response_excerpt(self._protocol_error),
                ),
            )
        try:
            b64 = base64.b64encode(jpeg_bytes).decode('ascii')
            prompt = self._build_vision_prompt(app)
            content: list[dict] = [
                {'type': 'text', 'text': prompt},
                {'type': 'image_url',
                 'image_url': {'url': f'data:image/jpeg;base64,{b64}', 'detail': 'low'}},
            ]
            raw = ''
            generation = self._cfg.generation.vision
            trace.emit(
                'llm_request',
                messages=[{
                    'role': 'user',
                    'content': [
                        {'type': 'text', 'text': prompt},
                        {'type': 'image', 'bytes': len(jpeg_bytes), 'detail': 'low'},
                    ],
                }],
                temperature=generation.temperature,
                maxTokens=generation.token_limit,
                **prompt_metadata('vision.glance', ('vision.glance',)),
            )
            async for chunk in self._provider.stream(
                messages=[{'role': 'user', 'content': content}],
                temperature=generation.temperature,
                max_tokens=generation.token_limit,
            ):
                if chunk.get('text'):
                    raw += chunk['text']
            description = raw.strip()
            if description:
                return VisionCallResult(description)
            return VisionCallResult(None, VisionFailure(error_type='EmptyResponse'))
        except LlmError as exc:
            failure = _llm_failure(exc)
            message = str(exc)
            if 'unknown variant `image_url`' in message or 'expected `text`' in message:
                self._protocol_error = (
                    '当前模型接口不接受 OpenAI image_url 消息块；'
                    '请配置支持图片输入的视觉 API，或为该接口实现专用协议适配器'
                )
                logger.warning('vision_model_not_multimodal',
                               model=self._provider.model, error=self._protocol_error)
                return VisionCallResult(None, failure)
            logger.warning('vision_call_failed', model=self._provider.model, error=message)
            return VisionCallResult(None, failure)
        except Exception as exc:
            logger.warning('vision_call_failed', error=str(exc))
            return VisionCallResult(
                None,
                VisionFailure(
                    error_type=type(exc).__name__,
                    response_excerpt=_safe_response_excerpt(str(exc)),
                ),
            )

    @staticmethod
    def _build_vision_prompt(app: str = '') -> str:
        """★ app 是前台程序名。模型经常认不出小众界面，先告诉它这是什么程序，
        描述质量立刻不一样——这是整条链路里最便宜的一个先验。
        """
        hint = f'画面里他开着的是 {app}。' if app else ''
        return get_prompt('vision.glance').render(app_hint=hint)
