"""把聊天图片来源解析为中文客观描述，并缓存已识别结果。

服务在后台任务中按来源下载聊天图片并调用视觉模型；同一份图片按 SHA-256
复用描述，避免重复调用。下载、解码或模型调用失败时统一返回 ``None``，由
调用方保留 ``[图片]`` 占位符，不得猜测图片内容。同步入站路径只传来源引用，
不在这里完成任何下载或模型调用。
"""

from __future__ import annotations

from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Protocol, Sequence
from urllib.parse import unquote, urlsplit
import asyncio
import base64
import hashlib
import re

import httpx

from src.core.common.logger import get_logger
from src.core.config.schema import Config
from src.core.llm_models.openai import LlmError
from src.core.llm_models.snapshot import bind_render_params
from src.core.observe import events as trace
from src.core.prompts.registry import get_prompt, prompt_metadata

logger = get_logger(__name__)

# 单张图片识别的截止时间，避免聊天图片拖慢回合上下文构建。
IMAGE_DESCRIPTION_DEADLINE_S = 8.0
# 图片来源下载截止时间；来源不可达时保留占位符，不让后台任务永久挂起。
IMAGE_DOWNLOAD_TIMEOUT_S = 10.0
# 聊天图片通常远小于屏幕截图，但仍设置上限防止异常文件占满内存。
MAX_IMAGE_BYTES = 5 * 1024 * 1024
# 占位符保持稳定：描述成功时替换 [图片]，失败时原样保留。
IMAGE_PLACEHOLDER = '[图片]'
EMOJI_PLACEHOLDER = '[表情包]'
# QQ 图片 CDN 的防盗链要求：缺少 Referer 时返回 400「download url has expired」，
# 实际链接并未过期；必须带上浏览器 UA 与同域 Referer 才能下载。
_BROWSER_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0 Safari/537.36'
)
_QQ_IMAGE_HOST_SUFFIX = 'qpic.cn'


class ImageDescriptionProvider(Protocol):
    """聊天图片描述依赖的最小异步模型接口。"""

    model: str

    def stream(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.85,
        max_tokens: int | None = None,
        signal: asyncio.Event | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """按增量块产生视觉模型响应。"""

        ...


@dataclass(frozen=True)
class DescribedEmoji:
    """一张已成功读取并获得情绪标签的表情包。"""

    content_hash: str
    emotion_tags: str
    image_bytes: bytes
    media_type: str


def merge_image_descriptions(
    text: str,
    descriptions: Sequence[str | None],
) -> str:
    """按顺序把图片描述合并回已含 ``[图片]`` 占位符的正文。

    :param text: 已由消息段转换得到的正文，普通图片位置为 ``[图片]``。
    :param descriptions: 与普通图片顺序一致的描述；``None`` 或空串表示保留
        对应占位符，并继续处理下一张图片。
    :return: 普通图片占位符按顺序升级为 ``[图片：<描述>]`` 后的正文。
    """
    merged = text
    position = 0
    placeholder = IMAGE_PLACEHOLDER
    for description in descriptions:
        index = merged.find(placeholder, position)
        if index < 0:
            break
        replacement = placeholder
        if description and description.strip():
            replacement = f'[图片：{description.strip()}]'
        merged = merged[:index] + replacement + merged[index + len(placeholder):]
        position = index + len(replacement)
    return merged


def merge_emoji_descriptions(
    text: str,
    descriptions: Sequence[DescribedEmoji | None],
) -> str:
    """按消息段顺序把表情包情绪标签合并回正文占位符。

    失败项保留 ``[表情包]``，绝不根据上下文猜测未识别图片表达的情绪。
    """

    merged = text
    position = 0
    for description in descriptions:
        index = merged.find(EMOJI_PLACEHOLDER, position)
        if index < 0:
            break
        replacement = EMOJI_PLACEHOLDER
        if description is not None:
            replacement = f'[表情包：{description.emotion_tags}]'
        merged = merged[:index] + replacement + merged[index + len(EMOJI_PLACEHOLDER):]
        position = index + len(replacement)
    return merged


class ChatImageDescriber:
    """按来源下载并描述聊天图片，在进程内按内容哈希缓存结果。"""

    def __init__(
        self,
        cfg: Config,
        provider: ImageDescriptionProvider | None,
    ) -> None:
        """绑定视觉模型配置与提供者。

        :param cfg: 提供视觉开关和生成参数的运行时配置。
        :param provider: 可选的视觉模型提供者；为 ``None`` 时所有描述返回 ``None``。
        副作用：只保存配置和空缓存，不建立网络连接。
        """
        self._cfg = cfg
        self._provider = provider
        self._cache: dict[tuple[str, str], str] = {}
        self._pending: dict[tuple[str, str], asyncio.Task[str | None]] = {}
        self._protocol_error: str | None = None

    async def describe(
        self,
        image_bytes: bytes,
        media_type: str = 'image/jpeg',
        image_hash: str | None = None,
    ) -> str | None:
        """获取图片的中文客观描述，优先命中缓存。

        :param image_bytes: 图片原始字节；空字节视为无图片。
        :param media_type: 图片 MIME 类型，默认 ``image/jpeg``。
        :param image_hash: 可选的调用方预先计算的 SHA-256；省略时按字节计算。
        :return: 非空描述；功能禁用、字节无效、模型失败或空响应时返回 ``None``。
        """
        if not image_bytes or not self._cfg.vision.chat_image_enabled:
            return None
        if len(image_bytes) > MAX_IMAGE_BYTES:
            logger.warning(
                'chat_image_too_large',
                bytes=len(image_bytes),
                limit=MAX_IMAGE_BYTES,
            )
            return None
        digest = image_hash or hashlib.sha256(image_bytes).hexdigest()
        return await self._describe_with_prompt(
            image_bytes,
            media_type,
            digest,
            'image.description',
        )

    async def _describe_with_prompt(
        self,
        image_bytes: bytes,
        media_type: str,
        digest: str,
        prompt_id: str,
    ) -> str | None:
        """按提示词类型复用同一内容的描述缓存和并发任务。"""

        cache_key = (prompt_id, digest)
        cached = self._cache.get(cache_key)
        if cached is not None:
            trace.emit('image_description', result='cached', hash=digest, promptId=prompt_id)
            return cached
        pending = self._pending.get(cache_key)
        if pending is not None:
            return await pending

        task = asyncio.create_task(self._describe_uncached(
            digest,
            image_bytes,
            media_type,
            prompt_id,
        ))
        self._pending[cache_key] = task
        task.add_done_callback(
            lambda finished, key=cache_key: self._finalize_pending(key, finished)
        )
        return await task

    def _finalize_pending(
        self,
        key: tuple[str, str],
        task: asyncio.Task[str | None],
    ) -> None:
        """消化完成的图片描述任务并读取异常。"""
        self._pending.pop(key, None)
        if task.cancelled():
            return
        try:
            task.exception()
        except Exception:
            pass

    async def describe_attachments(
        self,
        attachments: Sequence[dict[str, Any]],
    ) -> list[str | None]:
        """批量描述入站附件中的图片。

        :param attachments: 每项包含 ``data``（Base64 图片字节）、可选 ``mime``
            与可选 ``sha256`` 的字典列表。
        :return: 与输入等长的描述列表，任一解码或识别失败项为 ``None``。
        """
        results: list[str | None] = []
        for attachment in attachments:
            data = str(attachment.get('data') or '')
            try:
                image_bytes = base64.b64decode(data, validate=True)
            except (ValueError, TypeError):
                results.append(None)
                continue
            results.append(await self.describe(
                image_bytes,
                media_type=str(attachment.get('mime') or 'image/jpeg'),
                image_hash=str(attachment.get('sha256') or '') or None,
            ))
        return results

    async def describe_sources(
        self,
        sources: Sequence[str],
    ) -> list[str | None]:
        """并发下载并描述一组图片来源，结果顺序与输入一致。

        :param sources: 普通图片来源列表；支持 ``base64://``、``file://``、
            ``http(s)://`` 和本地路径，空字符串会作为失败项保留对齐顺序。
        :return: 与输入等长的描述列表；功能禁用、下载失败或识别失败项为 ``None``。
        副作用：仅在后台任务中被调用，下载和模型请求都并发执行；同一内容
            仍由 :meth:`describe` 按 SHA-256 去重。
        """
        if not sources or not self._cfg.vision.chat_image_enabled or self._provider is None:
            return [None for _ in sources]
        if self._protocol_error:
            return [None for _ in sources]

        async def _describe_one(source: str, http: httpx.AsyncClient) -> str | None:
            if not source:
                return None
            try:
                image_bytes = await _read_image_source(source, http)
                if not image_bytes:
                    return None
                if len(image_bytes) > MAX_IMAGE_BYTES:
                    logger.warning(
                        'chat_image_too_large',
                        bytes=len(image_bytes),
                        limit=MAX_IMAGE_BYTES,
                    )
                    return None
                return await self.describe(
                    image_bytes,
                    media_type=_guess_media_type(image_bytes),
                )
            except Exception as exc:
                logger.warning('chat_image_source_failed', error=str(exc))
                return None

        # 一条消息内的多张图共用同一个短超时客户端并发下载，避免串行
        # 8s/10s 累加，也不把连接挂在进程级客户端上影响其它请求。
        async with httpx.AsyncClient(timeout=IMAGE_DOWNLOAD_TIMEOUT_S) as http:
            return list(await asyncio.gather(
                *(_describe_one(source, http) for source in sources)
            ))

    async def describe_emoji_sources(
        self,
        sources: Sequence[str],
    ) -> list[DescribedEmoji | None]:
        """并发读取表情包，并只生成最多五个情绪或语气标签。

        :param sources: 与正文 ``[表情包]`` 顺序一致的图片来源。
        :return: 成功项包含内容哈希、规范标签和原始字节；任一步骤失败为 ``None``。
        副作用：下载来源并调用视觉模型；不写入表情包库。
        """

        if not sources or not self._cfg.vision.chat_image_enabled or self._provider is None:
            return [None for _ in sources]
        if self._protocol_error:
            return [None for _ in sources]

        async def _describe_one(
            source: str,
            http: httpx.AsyncClient,
        ) -> DescribedEmoji | None:
            if not source:
                return None
            try:
                image_bytes = await _read_image_source(source, http)
                if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
                    if image_bytes:
                        logger.warning(
                            'chat_emoji_too_large',
                            bytes=len(image_bytes),
                            limit=MAX_IMAGE_BYTES,
                        )
                    return None
                digest = hashlib.sha256(image_bytes).hexdigest()
                media_type = _guess_media_type(image_bytes)
                raw_tags = await self._describe_with_prompt(
                    image_bytes,
                    media_type,
                    digest,
                    'emoji.description',
                )
                tags = _normalize_emotion_tags(raw_tags)
                if tags is None:
                    return None
                return DescribedEmoji(
                    content_hash=digest,
                    emotion_tags=tags,
                    image_bytes=image_bytes,
                    media_type=media_type,
                )
            except Exception as exc:
                logger.warning('chat_emoji_source_failed', error=str(exc))
                return None

        async with httpx.AsyncClient(timeout=IMAGE_DOWNLOAD_TIMEOUT_S) as http:
            return list(await asyncio.gather(
                *(_describe_one(source, http) for source in sources)
            ))

    async def _describe_uncached(
        self,
        digest: str,
        image_bytes: bytes,
        media_type: str,
        prompt_id: str,
    ) -> str | None:
        """执行一次未命中缓存的实际图片识别。"""
        if self._provider is None:
            return None
        if self._protocol_error:
            trace.emit('image_description', result='skipped', hash=digest, error=self._protocol_error)
            return None
        try:
            description = await asyncio.wait_for(
                self._call_model(image_bytes, media_type, prompt_id),
                timeout=IMAGE_DESCRIPTION_DEADLINE_S,
            )
        except asyncio.TimeoutError:
            logger.warning(
                'chat_image_description_timeout',
                seconds=IMAGE_DESCRIPTION_DEADLINE_S,
                hash=digest,
            )
            trace.emit('image_description', result='timeout', hash=digest)
            return None
        if description:
            self._cache[(prompt_id, digest)] = description
            logger.info(
                'chat_image_description',
                chars=len(description),
                hash=digest,
                promptId=prompt_id,
            )
            trace.emit(
                'image_description',
                result='ok',
                hash=digest,
                text=description,
                promptId=prompt_id,
            )
            return description
        trace.emit('image_description', result='empty', hash=digest)
        return None

    async def _call_model(
        self,
        image_bytes: bytes,
        media_type: str,
        prompt_id: str,
    ) -> str | None:
        """构造多模态请求并消费视觉模型流式响应。"""
        try:
            encoded = base64.b64encode(image_bytes).decode('ascii')
            prompt = get_prompt(prompt_id).render()
            content = [
                {'type': 'text', 'text': prompt},
                {
                    'type': 'image_url',
                    'image_url': {'url': f'data:{media_type};base64,{encoded}', 'detail': 'low'},
                },
            ]
            generation = self._cfg.generation.vision
            trace.emit(
                'llm_request',
                messages=[{
                    'role': 'user',
                    'content': [
                        {'type': 'text', 'text': prompt},
                        {'type': 'image', 'bytes': len(image_bytes), 'detail': 'low'},
                    ],
                }],
                temperature=generation.temperature,
                maxTokens=generation.token_limit,
                renderParams={},
                **prompt_metadata(prompt_id, (prompt_id,)),
            )
            bind_render_params({})
            raw = ''
            stream = self._provider.stream(
                messages=[{'role': 'user', 'content': content}],
                temperature=generation.temperature,
                max_tokens=generation.token_limit,
            )
            async with aclosing(stream) as model_stream:
                async for chunk in model_stream:
                    if chunk.get('text'):
                        raw += chunk['text']
            description = raw.strip()
            if description:
                return description
            return None
        except LlmError as exc:
            message = str(exc)
            if 'unknown variant `image_url`' in message or 'expected `text`' in message:
                self._protocol_error = (
                    '当前视觉接口不接受图片消息块，请检查模型与接口协议'
                )
                logger.warning('chat_image_model_not_multimodal', error=self._protocol_error)
            else:
                logger.warning('chat_image_description_failed', error=message)
            return None
        except Exception as exc:
            logger.warning('chat_image_description_failed', error=str(exc))
            return None


def _image_download_headers(source: str) -> dict[str, str]:
    """为 QQ 图片 CDN 构造防盗链下载请求头。

    :param source: 图片来源 URL。
    :return: 仅当 host 属于 ``*.qpic.cn`` 时返回浏览器 UA、图片 Accept 与同域
        Referer；其他域名返回空字典，保持原有请求行为。
    副作用：只解析 URL，不发起网络请求。
    """
    parsed = urlsplit(source)
    host = (parsed.hostname or '').lower()
    if host != _QQ_IMAGE_HOST_SUFFIX and not host.endswith(f'.{_QQ_IMAGE_HOST_SUFFIX}'):
        return {}
    scheme = parsed.scheme or 'https'
    return {
        'User-Agent': _BROWSER_USER_AGENT,
        'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
        'Referer': f'{scheme}://{host}/',
    }


async def _read_image_source(
    source: str,
    http: httpx.AsyncClient | None = None,
) -> bytes:
    """读取 base64、本地文件或 HTTP 图片来源的原始字节。

    :param source: OneBot 图片段的 ``url`` / ``file`` 值；也兼容裸本地路径。
    :param http: 可复用的短超时 HTTP 客户端；为 ``None`` 且来源是 HTTP 时
        创建一个临时客户端。
    :return: 图片原始字节；无法读取或内容为空时返回 ``b''``。
    :raises Exception: 网络失败、状态码错误或文件读取失败时向上传播，由调用方
        决定按占位符处理；本函数不猜测内容。
    副作用：HTTP 来源使用调用方提供的客户端，或按下载超时创建独立客户端。
    """
    if source.startswith('base64://'):
        decoded = base64.b64decode(source[len('base64://'):])
        return decoded if isinstance(decoded, bytes) else b''
    if source.startswith('file://'):
        # Path.as_uri() 会把空格等字符转成百分号编码；先解码再交给文件系统，
        # 否则带空格的 QQ 数据目录路径无法命中本地图片。
        raw_path = unquote(source[len('file://'):])
        # OneBot 在 Windows 上常给出 file:///C:/... 形式；去掉第三根斜杠
        # 才能被 pathlib 识别为盘符路径，而不是当前盘下的 C: 目录。
        if len(raw_path) >= 3 and raw_path[0] == '/' and raw_path[2] == ':':
            raw_path = raw_path[1:]
        return Path(raw_path).read_bytes()
    if source.startswith(('http://', 'https://')):
        # qpic.cn 裸请求会被防盗链拒绝并伪报链接过期；请求头仅在 QQ CDN 域名生效，
        # 不改变其他图片源的下载行为。
        headers = _image_download_headers(source)
        if http is not None:
            response = await http.get(source, headers=headers)
            response.raise_for_status()
            return response.content
        async with httpx.AsyncClient(timeout=IMAGE_DOWNLOAD_TIMEOUT_S) as client:
            response = await client.get(source, headers=headers)
            response.raise_for_status()
            return response.content
    return Path(source).read_bytes()


def _guess_media_type(image_bytes: bytes) -> str:
    """按文件签名判断常见聊天图片 MIME，无法识别时按 JPEG 处理。

    :param image_bytes: 图片文件头至少 12 字节；空字节仍返回 ``image/jpeg``。
    :return: ``image/png``、``image/gif``、``image/webp`` 或默认 ``image/jpeg``。
    """
    if image_bytes.startswith(bytes([0x89]) + b"PNG"):
        return 'image/png'
    if image_bytes.startswith(b"GIF8"):
        return 'image/gif'
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return 'image/webp'
    return 'image/jpeg'


def _normalize_emotion_tags(raw: str | None) -> str | None:
    """把视觉模型输出收窄为最多五个短情绪标签。"""

    if raw is None:
        return None
    normalized = raw.strip()
    if any(
        marker in normalized
        for marker in ('无法判断', '内容不清晰', '看不清', '不确定')
    ):
        return None
    parts = re.split(r'[,，、;；\n]+', normalized)
    tags: list[str] = []
    for part in parts:
        tag = part.strip().strip('[]【】。.！!？?：:')
        if not tag or len(tag) > 16 or tag in tags:
            continue
        tags.append(tag)
        if len(tags) == 5:
            break
    return ','.join(tags) if tags else None
