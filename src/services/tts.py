"""TTS 服务：调 OpenAI 兼容 /audio/speech，经 WS 推 base64 音频给 Electron。

语音同样走轮询：model_tasks.tts 里写几条候选，前面的厂商挂了就换后面的。
连续失败到 GIVE_UP_AFTER 之后整个服务停掉——那时问题多半不在某一家厂商，
而是音色名或协议配错了，继续每轮重试只会白等。
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
    def __init__(self, cfg: Any, push_event: Callable, router: ModelRouter) -> None:
        self._cfg = cfg
        self._push_event = push_event
        self._router = router
        self._failures = 0
        self._cache_hits = 0
        self._cache_misses = 0
        self._cache: dict[str, bytes] = {}   # 简单内存缓存

    @property
    def enabled(self) -> bool:
        return self._cfg.tts.enabled and self._router.ready and self._failures < GIVE_UP_AFTER

    def speak(self, text: str, turn_id: int) -> None:
        if not self.enabled or not text.strip():
            return
        asyncio.create_task(self._run(text.strip(), turn_id))

    def cancel(self, turn_id: int) -> None:
        asyncio.create_task(self._push_event('voice.play', {'turnId': turn_id, 'kind': 'stop'}))

    async def _run(self, text: str, turn_id: int) -> None:
        # 缓存按音色而不是按候选模型——同一句话换厂商合成出来仍然是同一个人在说，
        # 没必要因为主力挂过一次就重合成一遍。
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
        """交给路由器挑候选；某个厂商失败就换下一条连接再合成一次。"""
        async def call(candidate: ModelCandidate) -> bytes:
            if candidate.client_type == 'volcengine':
                return await self._synth_volcengine(candidate, text)
            return await self._synth_openai(candidate, text)

        return await self._router.run(call)

    async def _synth_openai(self, candidate: ModelCandidate, text: str) -> bytes:
        async with httpx.AsyncClient(timeout=30.0) as client:
            headers: dict = {'Content-Type': 'application/json'}
            if candidate.api_key:
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
                raise RuntimeError('TTS returned empty audio')
            return data

    async def _synth_volcengine(self, candidate: ModelCandidate, text: str) -> bytes:
        """豆包语音（火山引擎）合成。

        和 OpenAI 兼容接口完全是两套东西：
          · 认证是 App ID + Access Token，且 header 写成 `Bearer;{token}`
            —— 分隔符是分号不是空格，这是它特有的格式，写成空格会 401；
          · 请求体是 app/user/audio/request 四段嵌套，不是 OpenAI 那五个平铺字段；
          · 音频不在 body 里，而是 JSON 的 data 字段，base64 编码。

        ⚠ 火山的文档站是 JS 渲染的，抓不到逐字段的官方定义，下面的字段名来自
          公开资料与社区实现。所以这里把接口返回的 code/message 原样抛出去，
          万一某个字段对不上，日志里直接能看到是哪个字段的问题，而不是静默失败。
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
                    # ★ 分号，不是空格。
                    'Authorization': f'Bearer;{candidate.api_key}',
                },
                json=payload,
            )
        if not resp.is_success:
            raise RuntimeError(f'豆包语音 HTTP {resp.status_code}: {resp.text[:200]}')
        body = resp.json()
        # 3000 = 成功。其余一律把原始 code/message 带出去，便于对字段。
        if body.get('code') != 3000:
            raise RuntimeError(
                f'豆包语音返回 code={body.get("code")} message={body.get("message")!r}'
            )
        encoded = body.get('data')
        if not encoded:
            raise RuntimeError(f'豆包语音未返回音频，原始响应键：{sorted(body)}')
        return base64.b64decode(encoded)

    def inspect(self) -> dict:
        return {
            'enabled': self.enabled,
            'configured': self._cfg.tts.enabled and self._router.ready,
            'voice': self._cfg.tts.voice,
            'format': self._cfg.tts.format,
            'failures': self._failures,
            'cacheHits': self._cache_hits,
            'cacheMisses': self._cache_misses,
            'routing': self._router.inspect(),
        }
