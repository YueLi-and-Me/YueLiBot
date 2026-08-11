"""将文本转换为音频并通过事件通道推送给桌面客户端。

服务从 ``ModelRouter`` 取得 TTS 候选，支持 OpenAI 兼容协议和单独的厂商协议，
对相同音色与文本使用进程内缓存。连续失败达到 ``GIVE_UP_AFTER`` 后停止接受
新的合成任务，以避免错误配置导致每轮重复等待；音频数据只在内存中处理。
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from typing import Any, Callable

import httpx

from src.common.logger import get_logger
from src.config.schema import ModelCandidate
from src.llm_models.router import ModelRouter

logger = get_logger(__name__)

GIVE_UP_AFTER = 3


class TtsService:
    """管理 TTS 合成任务、失败熔断、缓存和音频事件推送。"""

    def __init__(self, cfg: Any, push_event: Callable, router: ModelRouter) -> None:
        """初始化 TTS 服务。

        Args:
            cfg: 提供 ``tts`` 配置的运行时配置对象。
            push_event: 异步事件推送回调，接收事件名称和载荷字典。
            router: 提供候选模型轮询的 ``ModelRouter``。
        """

        self._cfg = cfg
        self._push_event = push_event
        self._router = router
        self._failures = 0
        self._cache_hits = 0
        self._cache_misses = 0
        self._cache: dict[str, bytes] = {}

    @property
    def enabled(self) -> bool:
        """判断当前是否允许创建新的 TTS 合成任务。

        Returns:
            TTS 配置启用、路由器就绪且连续失败次数未达到阈值时返回 ``True``。
        """

        return self._cfg.tts.enabled and self._router.ready and self._failures < GIVE_UP_AFTER

    def speak(self, text: str, turn_id: int) -> None:
        """异步合成一条文本并推送播放事件。

        Args:
            text: 待合成文本；去除首尾空白后为空时忽略。
            turn_id: 关联对话回合 ID。

        Side Effects:
            服务启用且文本非空时创建后台 asyncio task；任务可能调用远端 TTS
            接口并推送音频事件。
        """

        if not self.enabled or not text.strip():
            return
        asyncio.create_task(self._run(text.strip(), turn_id))

    def cancel(self, turn_id: int) -> None:
        """向客户端发送停止当前回合语音播放的事件。

        Args:
            turn_id: 要停止的对话回合 ID。

        Side Effects:
            创建后台事件推送任务；不取消已经创建的远端合成请求。
        """

        asyncio.create_task(self._push_event('voice.play', {'turnId': turn_id, 'kind': 'stop'}))

    async def _run(self, text: str, turn_id: int) -> None:
        """执行缓存查找、语音合成和音频事件推送。

        Args:
            text: 已去除首尾空白的待合成文本。
            turn_id: 关联对话回合 ID。

        Side Effects:
            更新缓存命中统计和连续失败计数，可能调用远端服务并推送 base64 音频；
            合成失败只记录日志，不向后台 task 调度方抛出。
        """

        # 缓存键不包含具体候选模型，避免路由切换后为相同音色和文本重复合成。
        cache_key = f'{self._cfg.tts.voice}:{text}'
        if cache_key in self._cache:
            self._cache_hits += 1
            data = self._cache[cache_key]
        else:
            self._cache_misses += 1
            try:
                data = await self._synth(text)
                self._cache[cache_key] = data
                self._failures = 0
            except Exception as exc:
                self._failures += 1
                logger.warning('tts_failed', error=str(exc), failures=self._failures)
                return

        b64 = base64.b64encode(data).decode('ascii')
        await self._push_event('voice.play', {
            'turnId': turn_id, 'kind': 'audio',
            'format': self._cfg.tts.format, 'data': b64,
        })

    async def _synth(self, text: str) -> bytes:
        """通过模型路由器选择候选并生成音频。

        Args:
            text: 待合成文本。

        Returns:
            远端接口返回的非空音频字节。

        Raises:
            Exception: 所有候选均失败或返回空音频时由路由器直接传播。
        """

        async def call(candidate: ModelCandidate) -> bytes:
            """根据候选协议类型调用对应的语音合成实现。

            Args:
                candidate: 当前路由尝试的模型候选配置。

            Returns:
                当前候选生成的非空音频字节。

            Raises:
                Exception: 候选协议调用、HTTP 请求或音频解码失败时传播。
            """

            if candidate.client_type == 'volcengine':
                return await self._synth_volcengine(candidate, text)
            return await self._synth_openai(candidate, text)

        return await self._router.run(call)

    async def _synth_openai(self, candidate: ModelCandidate, text: str) -> bytes:
        """调用 OpenAI 兼容的 ``/audio/speech`` 接口。

        Args:
            candidate: 提供基础 URL、模型标识和可选 API 密钥的候选配置。
            text: 待合成文本。

        Returns:
            非空音频响应体。

        Raises:
            RuntimeError: HTTP 状态非成功或响应体为空。
            httpx.HTTPError: 网络请求失败时由客户端抛出。
        """

        async with httpx.AsyncClient(timeout=30.0) as client:
            headers: dict = {'Content-Type': 'application/json'}
            if candidate.api_key:
                # 仅为当前候选组装认证头，避免路由切换时复用其他提供者的凭据。
                headers['Authorization'] = f'Bearer {candidate.api_key}'
            resp = await client.post(
                f'{candidate.base_url.rstrip("/")}/audio/speech',
                headers=headers,
                json={
                    'model': candidate.identifier,
                    'voice': self._cfg.tts.voice,
                    'input': text,
                    'response_format': self._cfg.tts.format,
                    'speed': self._cfg.tts.speed,
                },
            )
            if not resp.is_success:
                raise RuntimeError(f'TTS HTTP {resp.status_code}')
            data = resp.content
            if not data:
                # 空响应不能进入缓存或播放器，否则失败会表现为静默而非可诊断错误。
                raise RuntimeError('TTS returned empty audio')
            return data

    async def _synth_volcengine(self, candidate: ModelCandidate, text: str) -> bytes:
        """调用火山语音协议并解码响应中的 base64 音频。

        Args:
            candidate: 提供 App ID、访问令牌、基础 URL 和模型标识的候选配置。
            text: 待合成文本。

        Returns:
            解码后的非空音频字节。

        Raises:
            RuntimeError: HTTP 请求失败、返回码不是 3000 或响应缺少音频数据。
            ValueError: base64 音频字段格式非法。
            httpx.HTTPError: 网络请求失败时由客户端抛出。
        """
        cfg = self._cfg.tts
        base = (candidate.base_url or 'https://openspeech.bytedance.com').rstrip('/')
        payload = {
            'app': {
                'appid': candidate.app_id,
                'token': candidate.api_key,
                'cluster': cfg.cluster,
            },
            'user': {'uid': 'yueli'},
            'audio': {
                'voice_type': cfg.voice,
                'encoding': cfg.format,
                'speed_ratio': cfg.speed,
            },
            'request': {'reqid': str(uuid.uuid4()), 'text': text, 'operation': 'query'},
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f'{base}/api/v1/tts',
                headers={
                    'Content-Type': 'application/json',
                    # 该协议要求 Bearer 与令牌之间使用分号，空格会导致认证失败。
                    'Authorization': f'Bearer;{candidate.api_key}',
                },
                json=payload,
            )
        if not resp.is_success:
            raise RuntimeError(f'豆包语音 HTTP {resp.status_code}: {resp.text[:200]}')
        body = resp.json()
        # 仅 code=3000 表示成功；保留服务端 code/message 以定位协议字段错误。
        if body.get('code') != 3000:
            raise RuntimeError(
                f'豆包语音返回 code={body.get("code")} message={body.get("message")!r}'
            )
        encoded = body.get('data')
        if not encoded:
            raise RuntimeError(f'豆包语音未返回音频，原始响应键：{sorted(body)}')
        return base64.b64decode(encoded)

    def inspect(self) -> dict:
        """返回 TTS 配置、失败计数、缓存规模和路由状态。

        Returns:
            可序列化的诊断字典；不包含音频内容或认证密钥。
        """

        return {
            'enabled': self.enabled,
            'configured': self._cfg.tts.enabled and self._router.ready,
            'model': self._router.model,
            'voice': self._cfg.tts.voice,
            'format': self._cfg.tts.format,
            'failures': self._failures,
            'cacheHits': self._cache_hits,
            'cacheMisses': self._cache_misses,
            'cache': {
                'files': len(self._cache),
                'bytes': sum(len(data) for data in self._cache.values()),
            },
            'routing': self._router.inspect(),
        }
