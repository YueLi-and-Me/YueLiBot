"""合成桌面和 QQ 共用的音频，并为桌面推送播放事件。

服务从 ``ModelRouter`` 取得 TTS 候选，支持 OpenAI 兼容协议和单独的厂商协议，
按音色、格式、语速和文本缓存音频。桌面连续失败达到 ``GIVE_UP_AFTER`` 后停止
接受后台播放任务；QQ 调用可等待的 ``synthesize``，失败直接交给投递链路。
"""

from __future__ import annotations

from typing import Any, Callable, Dict

import asyncio
import base64
import uuid

import httpx

from src.core.logging.logger import get_logger
from src.core.config.schema import ModelCandidate
from src.core.llm_models.router import ModelRouter
from src.core.platform_io.voice import MAX_VOICE_BYTES

logger = get_logger(__name__)

GIVE_UP_AFTER = 3


class TtsService:
    """管理 TTS 合成任务、失败熔断、缓存和音频事件推送。"""

    def __init__(self, cfg: Any, push_event: Callable, router: ModelRouter) -> None:
        """初始化 TTS 服务。

        :param cfg: 提供 ``tts`` 配置的运行时配置对象。
        :param push_event: 异步事件推送回调，接收事件名称和载荷字典。
        :param router: 提供候选模型轮询的 ``ModelRouter``。
        """

        self._cfg = cfg
        self._push_event = push_event
        self._router = router
        self._failures = 0
        self._cache_hits = 0
        self._cache_misses = 0
        self._cache: Dict[str, bytes] = {}

    def apply_config(self, cfg: Any) -> None:
        """把本对象持有的配置引用切到重载后的新对象上。

        配置热重载不重建服务，只把各持有方的引用换掉；引用一换，所有读取点
        下次读到的就是新值。熔断计数跨重载保留，音频缓存清空，以免复用旧参数产物。

        :param cfg: 重载后的运行时配置。
        :return: ``None``。
        副作用：重绑配置引用；不重置熔断状态。
        """

        self._cfg = cfg
        # 热更新音色、语速或格式后不复用旧字节，避免音频格式与报文元数据不一致。
        self._cache.clear()

    @property
    def enabled(self) -> bool:
        """判断当前是否允许创建新的 TTS 合成任务。

        :return: TTS 配置启用、路由器就绪且连续失败次数未达到阈值时返回 ``True``。
        """

        return self._cfg.tts.enabled and self._router.ready and self._failures < GIVE_UP_AFTER

    def speak(self, text: str, turn_id: int) -> None:
        """异步合成一条文本并推送播放事件。

        :param text: 待合成文本；去除首尾空白后为空时忽略。
        :param turn_id: 关联对话回合 ID。

        副作用：
            服务启用且文本非空时创建后台 asyncio task；任务可能调用远端 TTS
            接口并推送音频事件。
        """

        if not self.enabled or not text.strip():
            return
        asyncio.create_task(self._run(text.strip(), turn_id))

    def cancel(self, turn_id: int) -> None:
        """向客户端发送停止当前回合语音播放的事件。

        :param turn_id: 要停止的对话回合 ID。

        副作用：
            创建后台事件推送任务；不取消已经创建的远端合成请求。
        """

        asyncio.create_task(self._push_event('voice.play', {'turnId': turn_id, 'kind': 'stop'}))

    async def _run(self, text: str, turn_id: int) -> None:
        """执行缓存查找、语音合成和音频事件推送。

        :param text: 已去除首尾空白的待合成文本。
        :param turn_id: 关联对话回合 ID。

        副作用：
            更新缓存命中统计和连续失败计数，可能调用远端服务并推送 base64 音频；
            合成失败只记录日志，不向后台 task 调度方抛出。
        """

        try:
            data = await self.synthesize(text)
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

    async def synthesize(self, text: str) -> bytes:
        """返回完整音频供 QQ 投递或桌面播放；空文本、未启用、超限及厂商错误直接抛出。

        :param text: 一条完整台词，去除首尾空白后必须非空。
        :return: 不超过 5 MiB 的音频字节；只缓存成功结果，不发送平台消息。
        """
        if not (self._cfg.tts.enabled or self._cfg.tts.qq_enabled) or not self._router.ready:
            raise RuntimeError('语音合成未启用或未配置模型')
        text = text.strip()
        if not text:
            raise ValueError('语音合成文本不能为空')
        if not self._cfg.tts.voice.strip():
            raise ValueError('语音合成缺少音色 ID')
        cfg = self._cfg.tts
        cache_key = repr((cfg.voice, cfg.format, cfg.speed, cfg.cluster, text))
        if cache_key in self._cache:
            self._cache_hits += 1
            return self._cache[cache_key]
        self._cache_misses += 1
        data = await self._synth(text)
        if not data:
            raise RuntimeError('语音合成返回空音频')
        if len(data) > MAX_VOICE_BYTES:
            raise ValueError('语音合成结果超过 5 MiB')
        # 有界缓存避免长期运行积累全部历史音频；超限前按插入顺序淘汰。
        while self._cache and sum(len(item) for item in self._cache.values()) + len(data) > 32 * 1024 * 1024:
            self._cache.pop(next(iter(self._cache)))
        self._cache[cache_key] = data
        return data

    async def _synth(self, text: str) -> bytes:
        """通过模型路由器选择候选并生成音频。

        :param text: 待合成文本。

        :return: 远端接口返回的非空音频字节。

        :raises Exception: 所有候选均失败或返回空音频时由路由器直接传播。
        """

        async def call(candidate: ModelCandidate) -> bytes:
            """根据候选协议类型调用对应的语音合成实现。

            :param candidate: 当前路由尝试的模型候选配置。

            :return: 当前候选生成的非空音频字节。

            :raises Exception: 候选协议调用、HTTP 请求或音频解码失败时传播。
            """

            if candidate.client_type == 'volcengine':
                return await self._synth_volcengine(candidate, text)
            return await self._synth_openai(candidate, text)

        return await self._router.run(call)

    async def _synth_openai(self, candidate: ModelCandidate, text: str) -> bytes:
        """调用 OpenAI 兼容的 ``/audio/speech`` 接口。

        :param candidate: 提供基础 URL、模型标识和可选 API 密钥的候选配置。
        :param text: 待合成文本。

        :return: 非空音频响应体。

        :raises RuntimeError: HTTP 状态非成功或响应体为空。
        :raises httpx.HTTPError: 网络请求失败时由客户端抛出。
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
                raise RuntimeError('语音合成返回空音频')
            return data

    async def _synth_volcengine(self, candidate: ModelCandidate, text: str) -> bytes:
        """调用火山语音协议并解码响应中的 base64 音频。

        :param candidate: 提供 App ID、访问令牌、基础 URL 和模型标识的候选配置。
        :param text: 待合成文本。

        :return: 解码后的非空音频字节。

        :raises RuntimeError: HTTP 请求失败、返回码不是 3000 或响应缺少音频数据。
        :raises ValueError: base64 音频字段格式非法。
        :raises httpx.HTTPError: 网络请求失败时由客户端抛出。
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
        return base64.b64decode(encoded, validate=True)

    def inspect(self) -> dict:
        """返回 TTS 配置、失败计数、缓存规模和路由状态。

        :return: 可序列化的诊断字典；不包含音频内容或认证密钥。
        """

        return {
            'enabled': self.enabled,
            'configured': (self._cfg.tts.enabled or self._cfg.tts.qq_enabled) and self._router.ready,
            'qqEnabled': self._cfg.tts.qq_enabled and self._router.ready,
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
