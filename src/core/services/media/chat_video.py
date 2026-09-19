"""把聊天视频链接交给全模态模型理解（画面和声音），并按内容哈希缓存结果。

QQ 视频直链会过期且无法重取，所以入库后要立即决定是否观看：先用 HTTP Range
只读 MP4 盒子头拿到时长（不下载整段、不引入解码依赖），超过上限就不交给模型；
要看就把链接原样放进 ``video_url`` 块交给 ``video`` 路由的模型。下载、读时长或
模型调用失败时统一返回 ``None``，由调用方保留 ``[视频]`` 占位符，不让模型猜内容。
描述、时长判定与在途任务都按段里的 ``file``（内容 MD5）做进程内缓存，
同一个视频被转发多次只看一次；进程重启即丢，不持久化。
"""

from __future__ import annotations

from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Protocol, Sequence
import asyncio
import struct

import httpx

from src.core.logging.logger import get_logger
from src.core.config.schema import Config
from src.core.llm_models.openai import LlmError
from src.core.llm_models.snapshot import bind_render_params
from src.core.observe import events as trace
from src.core.platform_io.types import VideoSource
from src.core.prompts.registry import get_prompt, prompt_metadata

logger = get_logger(__name__)

# 单个视频理解至少保留的总截止时间；实际值还必须覆盖 video 路由的首字窗口。
MIN_VIDEO_DESCRIPTION_DEADLINE_S = 8.0
# 首字到达后生成一段描述所需的收尾时间，同时继续约束挂起的响应流。
VIDEO_DESCRIPTION_COMPLETION_GRACE_S = 5.0
# 读时长的每个 Range 请求的短超时；与模型截止分开计，不叠加等待。
VIDEO_DURATION_REQUEST_TIMEOUT_S = 10.0
# 沿盒子逐个读取的头部字节数：32 位长度 + 4 字节类型 + 可能的 64 位扩展长度。
_BOX_HEADER_BYTES = 16
# 盒子数上限：超过即按损坏处理，避免异常数据让盒子遍历无限走下去。
_BOX_WALK_LIMIT = 64
# 占位符保持稳定：理解成功时替换 [视频]，失败时原样保留。
VIDEO_PLACEHOLDER = '[视频]'
_VIDEO_PROMPT_ID = 'video.description'


class VideoDurationUnreadableError(Exception):
    """读不出视频时长：非 MP4、盒子损坏、链接过期或网络错误。"""


@dataclass(frozen=True)
class _VideoTooLong:
    """视频时长超过观看上限的事实结果；``seconds`` 是读出的实际时长。"""

    seconds: float


# 一个视频的处理结果：描述文本、超限事实，或 ``None``（保留占位符）。
VideoOutcome = str | _VideoTooLong | None


def message_concerns_her(
    stream_kind: str,
    mentioned_me: bool,
    name_mentioned: bool,
    replied_to_me: bool,
) -> bool:
    """「跟她有关」的唯一判定函数：视频理解范围与补看触发都读它。

    四项任一为真：私聊（stream kind 为 ``direct``）；@ 她；叫她名字；回复她的消息。
    戳一戳、贴表情不算。

    :param stream_kind: 会话类型（``direct`` 或 ``group``）。
    :param mentioned_me: 本条包含协议 @。
    :param name_mentioned: 本条正文叫了她的名字/别名。
    :param replied_to_me: 本条回复了她自己发的消息。
    :return: 该消息是否跟她有关。
    副作用：不读取任何外部状态。
    """
    return (
        stream_kind == 'direct'
        or mentioned_me
        or name_mentioned
        or replied_to_me
    )


def _format_too_long(seconds: float) -> str:
    """把超限时长写成只交代事实的占位文案；不足 60 秒写秒，否则按分钟四舍五入。"""
    if seconds < 60:
        return f'约 {round(seconds)} 秒'
    return f'约 {round(seconds / 60)} 分钟'


def merge_video_descriptions(
    text: str,
    outcomes: Sequence[Any],
) -> str:
    """把各视频结果按顺序合并进正文，与 ``merge_image_descriptions`` 同形。

    描述文本替换为 ``[视频：描述]``；超限替换为只交代事实的
    ``[视频：约 N 秒/分钟，超出观看时长上限，未看]``；``None`` 保留 ``[视频]``。

    :param text: 含 ``[视频]`` 占位符的正文。
    :param outcomes: 与占位符顺序一致的结果序列。
    :return: 合并后的正文；占位符个数与结果数不一致时按较短者处理。
    副作用：不修改输入。
    """
    merged = text
    position = 0
    for outcome in outcomes:
        index = merged.find(VIDEO_PLACEHOLDER, position)
        if index < 0:
            break
        position = index + len(VIDEO_PLACEHOLDER)
        if outcome is None:
            continue
        if isinstance(outcome, _VideoTooLong):
            replacement = (
                f'[视频：{_format_too_long(outcome.seconds)}，超出观看时长上限，未看]'
            )
        else:
            description = str(outcome).strip()
            if not description:
                continue
            replacement = f'[视频：{description}]'
        merged = merged[:index] + replacement + merged[position:]
        position = index + len(replacement)
    return merged


async def _range_get(
    http: httpx.AsyncClient,
    url: str,
    start: int,
    length: int,
) -> tuple[bytes, int | None]:
    """读取 ``[start, start+length)`` 区间，并尽力从 Content-Range 取出文件总长度。

    :raises VideoDurationUnreadableError: 链接过期、网络错误或状态码不是 200/206。
    """
    try:
        response = await http.get(url, headers={'Range': f'bytes={start}-{start + length - 1}'})
    except httpx.HTTPError as exc:
        raise VideoDurationUnreadableError(f'Range 请求失败：{exc}') from exc
    if response.status_code not in (200, 206):
        raise VideoDurationUnreadableError(f'HTTP {response.status_code}')
    total: int | None = None
    content_range = response.headers.get('content-range', '')
    if '/' in content_range:
        tail = content_range.rsplit('/', 1)[-1].strip()
        if tail.isdigit():
            total = int(tail)
    return response.content, total


def _mvhd_duration_seconds(moov: bytes) -> float:
    """在 ``moov`` 盒子的直接子盒里找 ``mvhd`` 并解析时长（version 0 与 1 两种布局）。

    :raises VideoDurationUnreadableError: 找不到 ``mvhd`` 或时间基准为 0。
    """
    index = 8
    while index + 8 <= len(moov):
        size, kind = struct.unpack('>I4s', moov[index:index + 8])
        if kind == b'mvhd':
            version = moov[index + 8]
            if version == 1:
                timescale, duration = struct.unpack('>IQ', moov[index + 28:index + 40])
            else:
                timescale, duration = struct.unpack('>II', moov[index + 20:index + 28])
            if timescale == 0:
                raise VideoDurationUnreadableError('mvhd 的 timescale 为 0')
            return duration / timescale
        if size < 8:
            raise VideoDurationUnreadableError('moov 内盒子损坏')
        index += size
    raise VideoDurationUnreadableError('moov 里没有 mvhd')


async def read_mp4_duration_seconds(url: str, http: httpx.AsyncClient) -> float:
    """用 Range 请求沿 MP4 盒子逐个读 16 字节头，遇 ``moov`` 整块读下解析 ``mvhd``。

    不下载整段：请求数只随盒子个数走（通常 3–5 个），与文件大小无关。
    ``size == 1`` 取 64 位扩展长度，``size == 0`` 表示盒子一直到文件尾。

    :param url: 视频直链（QQ 多媒体链接会过期，过期按读不出处理）。
    :param http: 已带短超时的异步客户端。
    :return: 视频时长（秒）。
    :raises VideoDurationUnreadableError: 非 MP4、盒子损坏、链接过期或网络错误。
    """
    offset = 0
    total: int | None = None
    for _ in range(_BOX_WALK_LIMIT):
        head, reported = await _range_get(http, url, offset, _BOX_HEADER_BYTES)
        if len(head) < 8:
            raise VideoDurationUnreadableError('盒子头不足 8 字节')
        total = total if total is not None else reported
        size, kind = struct.unpack('>I4s', head[:8])
        try:
            kind.decode('ascii')
        except UnicodeDecodeError as exc:
            raise VideoDurationUnreadableError('盒子类型不可读') from exc
        if size == 1:
            if len(head) < _BOX_HEADER_BYTES:
                raise VideoDurationUnreadableError('64 位盒子长度缺失')
            size = struct.unpack('>Q', head[8:16])[0]
        elif size == 0:
            if total is None:
                raise VideoDurationUnreadableError('size==0 盒子且总长未知')
            size = total - offset
        if size < 8:
            raise VideoDurationUnreadableError('盒子长度损坏')
        if kind == b'moov':
            moov, _ = await _range_get(http, url, offset, size)
            return _mvhd_duration_seconds(moov)
        offset += size
        if total is not None and offset >= total:
            break
    raise VideoDurationUnreadableError('没找到 moov')


class VideoDescriptionProvider(Protocol):
    """聊天视频理解依赖的最小异步模型接口。"""

    model: str

    def stream(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.85,
        max_tokens: int | None = None,
        signal: asyncio.Event | None = None,
        require_text: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        ...


class ChatVideoDescriber:
    """聊天视频理解服务：先读时长再决定看不看，结果与在途任务按 ``file`` 合并。

    与 ``ChatImageDescriber`` 同形：``apply_config`` 只重新绑定配置以支持热重载，
    缓存与在途任务不清空；总截止为 ``max(下限, video 路由首字窗口 + 收尾时间)``。
    """

    def __init__(
        self,
        cfg: Config,
        provider: VideoDescriptionProvider | None,
        http_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        """创建聊天视频理解服务。

        :param cfg: 运行时配置；读取 ``vision.chat_video_*`` 与 ``routing.video``。
        :param provider: ``video`` 路由的模型路由；为 ``None`` 时一律保留占位符。
        :param http_factory: 读时长用的异步客户端工厂；测试用它注入假传输层。
        副作用：不发起任何网络或模型请求。
        """
        self._cfg = cfg
        self._provider = provider
        self._http_factory = http_factory or (
            lambda: httpx.AsyncClient(timeout=VIDEO_DURATION_REQUEST_TIMEOUT_S)
        )
        self._description_deadline_s = max(
            MIN_VIDEO_DESCRIPTION_DEADLINE_S,
            cfg.routing.video.first_token_timeout_ms / 1000
            + VIDEO_DESCRIPTION_COMPLETION_GRACE_S,
        )
        # 成功与超限的最终结果；失败不缓存，下一次同名视频仍可重试。
        self._cache: dict[str, Any] = {}
        # 读出的时长（秒），按 file 缓存；超限判定因此也只算一次。
        self._durations: dict[str, float] = {}
        self._pending: dict[str, asyncio.Task[Any]] = {}

    def apply_config(self, cfg: Config) -> None:
        """在配置热重载时替换配置对象；缓存与在途任务不清空。

        :param cfg: 新的运行时配置。
        副作用：只重新绑定配置引用。
        """
        self._cfg = cfg

    async def describe_sources(
        self,
        sources: Sequence[VideoSource],
    ) -> list[Any]:
        """并发理解一组视频来源，结果顺序与 ``[视频]`` 占位符一致。

        :param sources: 与占位符顺序一致的视频来源；``url`` 为空的项结果为 ``None``。
        :return: 每个来源的 ``VideoOutcome``；开关关闭或无模型时全为 ``None``。
        副作用：读时长的 Range 请求与模型调用都在本任务内完成。
        """
        if not sources or not self._cfg.vision.chat_video_enabled or self._provider is None:
            return [None] * len(sources)

        async def _describe_one(source: VideoSource) -> Any:
            if not source.url:
                return None
            key = source.file.strip() or source.url
            cached = self._cache.get(key)
            if cached is not None:
                trace.emit('video_description', result='cached', file=key)
                return cached
            pending = self._pending.get(key)
            if pending is not None:
                return await pending
            task = asyncio.create_task(self._describe_uncached(key, source))
            self._pending[key] = task
            task.add_done_callback(lambda finished, k=key: self._finalize_pending(k, finished))
            return await task

        return await asyncio.gather(*(_describe_one(source) for source in sources))

    def _finalize_pending(self, key: str, task: asyncio.Task[Any]) -> None:
        """消化完成的视频理解任务并读取异常。"""
        self._pending.pop(key, None)
        if task.cancelled():
            return
        try:
            task.exception()
        except Exception:
            pass

    async def _read_duration(self, url: str) -> float:
        """读取视频时长；独立成方法以便测试替换网络层。

        :raises VideoDurationUnreadableError: 读不出时抛出，由调用方按保留占位处理。
        """
        async with self._http_factory() as http:
            return await read_mp4_duration_seconds(url, http)

    async def _describe_uncached(self, key: str, source: VideoSource) -> Any:
        """执行一次未命中缓存的实际视频理解。"""
        try:
            # 在当前视频任务内施加截止时间，避免 wait_for 再创建一层取消任务；
            # 读时长的每个请求另有短超时，不与模型截止叠加成第二套等待。
            async with asyncio.timeout(self._description_deadline_s):
                duration = self._durations.get(key)
                if duration is None:
                    duration = await self._read_duration(source.url)
                    self._durations[key] = duration
                if duration > self._cfg.vision.chat_video_max_seconds:
                    outcome: Any = _VideoTooLong(duration)
                    self._cache[key] = outcome
                    logger.info(
                        'chat_video_too_long',
                        file=key,
                        durationSec=round(duration, 1),
                        maxSeconds=self._cfg.vision.chat_video_max_seconds,
                    )
                    trace.emit(
                        'video_description',
                        result='too_long',
                        file=key,
                        durationSec=round(duration, 1),
                    )
                    return outcome
                description = await self._call_model(source.url, key, duration)
        except TimeoutError:
            logger.warning(
                'chat_video_description_timeout',
                seconds=self._description_deadline_s,
                file=key,
            )
            trace.emit('video_description', result='timeout', file=key)
            return None
        except VideoDurationUnreadableError as exc:
            logger.warning('chat_video_duration_unreadable', file=key, error=str(exc))
            trace.emit('video_description', result='unreadable', file=key, error=str(exc))
            return None
        if description:
            self._cache[key] = description
            logger.info('chat_video_description', chars=len(description), file=key)
            trace.emit(
                'video_description',
                result='ok',
                file=key,
                durationSec=round(duration, 1),
                text=description,
                promptId=_VIDEO_PROMPT_ID,
            )
            return description
        trace.emit('video_description', result='empty', file=key, durationSec=round(duration, 1))
        return None

    async def _call_model(self, url: str, key: str, duration: float) -> str | None:
        """把 QQ 链接原样放进 video_url 块交给全模态模型，并消费流式响应。"""
        try:
            prompt = get_prompt(_VIDEO_PROMPT_ID).render()
            content = [
                {'type': 'video_url', 'video_url': {'url': url}},
                {'type': 'text', 'text': prompt},
            ]
            generation = self._cfg.generation.video
            trace.emit(
                'llm_request',
                messages=[{
                    'role': 'user',
                    'content': [
                        {'type': 'video', 'url': url},
                        {'type': 'text', 'text': prompt},
                    ],
                }],
                temperature=generation.temperature,
                maxTokens=generation.token_limit,
                renderParams={},
                **prompt_metadata(_VIDEO_PROMPT_ID, (_VIDEO_PROMPT_ID,)),
            )
            bind_render_params({})
            raw = ''
            stream = self._provider.stream(
                messages=[{'role': 'user', 'content': content}],
                temperature=generation.temperature,
                max_tokens=generation.token_limit,
                require_text=True,
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
            logger.warning('chat_video_description_failed', file=key, error=str(exc))
            trace.emit(
                'video_description',
                result='failed',
                file=key,
                durationSec=round(duration, 1),
                error=str(exc),
            )
            return None
        except Exception as exc:
            logger.warning('chat_video_description_failed', file=key, error=str(exc))
            trace.emit(
                'video_description',
                result='failed',
                file=key,
                durationSec=round(duration, 1),
                error=str(exc),
            )
            return None
