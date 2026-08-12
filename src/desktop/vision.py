"""在明确请求时调用视觉模型描述当前屏幕，并管理描述缓存与失败诊断。

服务只接受调用方主动提交的一帧 JPEG，不执行后台截图轮询；截图仅编码到当前
模型请求中，不写入磁盘。应用程序名可作为视觉提示先验，窗口标题等敏感上下文
由上游捕获层控制，不在本模块外传。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol

import asyncio
import base64
import re

from src.core.common.clock import now as current_time
from src.core.common.logger import get_logger
from src.core.config.schema import Config
from src.core.llm_models.openai import LlmError
from src.core.observe import events as trace
from src.core.prompts.registry import get_prompt, prompt_metadata

logger = get_logger(__name__)

# 重复请求在冷却窗口内复用最近描述，避免短时间内重复调用视觉模型。
CHAT_GLANCE_COOLDOWN_MS = 20_000
# TTL 必须覆盖冷却窗口，避免缓存刚允许复用却已经过期。
CHAT_GLANCE_TTL_MS = 60_000
# 请求截止时间应短于上游客户端超时，避免客户端先取消而产生未分类的异步异常。
CHAT_GLANCE_DEADLINE_S = 8.0


@dataclass(frozen=True)
class VisionFailure:
    """一次视觉调用没有得到描述时的可回放诊断。"""

    error_type: str
    error_kind: str = ''
    status_code: int | None = None
    response_excerpt: str = ''

    def as_trace(self) -> dict[str, str | int | None]:
        """转换为观察事件使用的可序列化错误字段。

        Returns:
            使用 camelCase 键名并将空字符串转换为 ``None`` 的字典。
        """

        return {
            'errorType': self.error_type,
            'errorKind': self.error_kind or None,
            'statusCode': self.status_code,
            'responseExcerpt': self.response_excerpt or None,
        }


@dataclass(frozen=True)
class VisionCallResult:
    """封装视觉描述结果及可选失败诊断。"""

    description: str | None
    failure: VisionFailure | None = None


def _safe_response_excerpt(value: str) -> str:
    """在错误响应进入 trace 前脱敏凭证并限制长度。

    Args:
        value: 原始错误文本或接口响应片段。

    Returns:
        将 Bearer 令牌替换为 ``[REDACTED_SECRET]`` 且最多 400 字符的文本。
    """
    redacted = re.sub(
        r'(?i)(authorization["\']?\s*[:=]\s*["\']?bearer\s+|bearer\s+)[^\s,;"\']+',
        r'\1[REDACTED_SECRET]',
        value.strip(),
    )
    return redacted[:400]


def _llm_failure(exc: LlmError) -> VisionFailure:
    """将已分类的模型错误转换为视觉失败诊断。

    Args:
        exc: 由模型客户端分类的 ``LlmError``。

    Returns:
        包含错误类型、错误分类、可解析 HTTP 状态码和脱敏响应摘要的诊断对象。
    """
    status_match = re.search(r'HTTP\s+(\d{3})', str(exc))
    return VisionFailure(
        error_type=type(exc).__name__,
        error_kind=exc.kind,
        status_code=int(status_match.group(1)) if status_match else None,
        response_excerpt=_safe_response_excerpt(exc.detail or str(exc)),
    )


class VisionProvider(Protocol):
    """视觉服务依赖的最小异步流式模型接口。"""

    model: str

    def stream(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.85,
        max_tokens: int | None = None,
        signal: asyncio.Event | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """按增量块产生视觉模型响应。

        Args:
            messages: OpenAI 兼容的多模态消息列表。
            temperature: 采样温度，默认 0.85。
            max_tokens: 最大输出 token 数；``None`` 表示由提供者决定。
            signal: 可选的取消信号。

        Returns:
            异步迭代器，每个元素包含可选的 ``text`` 字段。
        """

        ...


class VisionService:
    """协调主动视觉请求、短期描述缓存和事件诊断。"""

    def __init__(self, cfg: Config,
                 push_event: Callable[[str, dict[str, Any]], Awaitable[None]],
                 provider: VisionProvider | None) -> None:
        """初始化视觉服务。

        Args:
            cfg: 提供视觉开关和生成参数的运行时配置。
            push_event: 异步推送视觉观看状态的回调。
            provider: 可选的流式视觉模型提供者；为 ``None`` 时视为不可用。
        """

        self._cfg = cfg
        self._push_event = push_event
        self._provider = provider
        self._chat_glance: tuple[str, int] | None = None   # (描述, 生成时刻)
        self._protocol_error: str | None = None
        self._glances = 0

    def stats(self) -> dict[str, Any]:
        """返回视觉功能的配置状态和调用次数。

        Returns:
            不包含截图和凭证的诊断字典。
        """

        return {
            'enabled': self._cfg.vision.enabled,
            'available': self._protocol_error is None,
            'error': self._protocol_error,
            'looks': self._glances,
        }

    def chat_glance(self, ttl_ms: int = CHAT_GLANCE_TTL_MS) -> str | None:
        """读取未超过有效期的最近屏幕描述。

        Args:
            ttl_ms: 描述有效期，单位毫秒，默认使用 ``CHAT_GLANCE_TTL_MS``。

        Returns:
            未过期的缓存描述；没有缓存或已过期时返回 ``None``。
        """
        if not self._chat_glance:
            return None
        text, at = self._chat_glance
        if current_time() - at > ttl_ms:
            return None
        return text

    async def glance(self, jpeg_bytes: bytes, app: str = '') -> str | None:
        """调用视觉模型描述一帧当前画面。

        Args:
            jpeg_bytes: 当前画面的 JPEG 字节；空字节表示没有可用截图。
            app: 可选的前台程序名，作为模型识别界面的先验提示。

        Returns:
            模型生成的描述；功能禁用、缓存冷却命中、模型失败或响应为空时返回
            ``None``，禁用分支可能返回仍在有效期内的缓存描述。

        Side Effects:
            可能推送 ``vision.watching`` 开始/结束事件，调用视觉模型并更新最近
            描述缓存、调用计数和观察事件。
        """
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
                    # 厂商已连通，只是没在限时内出字，与连不上区分开。
                    error_kind='timeout',
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
        # 失败不回退到旧缓存，调用方必须能区分本次未获得新描述。
        return description

    async def _call_vision_model(self, jpeg_bytes: bytes, app: str = '') -> VisionCallResult:
        """构造多模态请求并消费视觉模型流式响应。

        Args:
            jpeg_bytes: 待发送的 JPEG 图像字节。
            app: 可选的前台程序名提示。

        Returns:
            包含清理后文本或结构化失败原因的 ``VisionCallResult``。

        Side Effects:
            发出模型请求并写入请求、成功或失败观察事件；检测到不支持多模态协议
            时会将服务标记为协议不可用，后续请求直接跳过。
        """

        if not self._provider:
            return VisionCallResult(None, VisionFailure(error_type='ProviderUnavailable'))
        if self._protocol_error:
            # 不重复请求已确认不兼容的接口，避免每次前台事件都产生相同失败。
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
            # 观察事件只记录图片大小和消息结构，不持久化原始图像或 base64 内容。
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
                    # 视觉提供者可能分多块返回描述，必须在结束后统一清理空白。
                    raw += chunk['text']
            description = raw.strip()
            if description:
                return VisionCallResult(description)
            return VisionCallResult(None, VisionFailure(error_type='EmptyResponse'))
        except LlmError as exc:
            failure = _llm_failure(exc)
            message = str(exc)
            if 'unknown variant `image_url`' in message or 'expected `text`' in message:
                # 将协议不兼容转换为持久状态，后续调用直接返回可观察的失败类型。
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
        """根据前台程序名构造视觉提示词。

        Args:
            app: 可选的前台程序名；为空时不追加应用提示。

        Returns:
            使用 ``vision.glance`` 模板渲染的提示词。

        Raises:
            KeyError, ValueError: 视觉提示词模板不存在或占位符不匹配。
        """
        hint = f'画面里他开着的是 {app}。' if app else ''
        return get_prompt('vision.glance').render(app_hint=hint)
