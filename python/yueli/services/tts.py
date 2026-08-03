"""TTS 服务：调 OpenAI 兼容 /audio/speech，经 WS 推 base64 音频给 Electron。"""

from __future__ import annotations

import asyncio
import base64
from typing import Any, Callable

import httpx

from yueli.common.logger import get_logger

logger = get_logger(__name__)

GIVE_UP_AFTER = 3


class TtsService:
    def __init__(self, cfg: Any, push_event: Callable) -> None:
        self._cfg = cfg
        self._push_event = push_event
        self._failures = 0
        self._cache_hits = 0
        self._cache_misses = 0
        self._cache: dict[str, bytes] = {}   # 简单内存缓存

    @property
    def enabled(self) -> bool:
        return self._cfg.tts.ready and self._failures < GIVE_UP_AFTER

    def speak(self, text: str, turn_id: int) -> None:
        if not self.enabled or not text.strip():
            return
        asyncio.create_task(self._run(text.strip(), turn_id))

    def cancel(self, turn_id: int) -> None:
        asyncio.create_task(self._push_event('voice.play', {'turnId': turn_id, 'kind': 'stop'}))

    async def _run(self, text: str, turn_id: int) -> None:
        cache_key = f'{self._cfg.tts.model}:{self._cfg.tts.voice}:{text}'
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
        async with httpx.AsyncClient(timeout=30.0) as client:
            headers: dict = {'Content-Type': 'application/json'}
            if self._cfg.tts.api_key:
                headers['Authorization'] = f'Bearer {self._cfg.tts.api_key}'
            resp = await client.post(
                f'{self._cfg.tts.base_url.rstrip("/")}/audio/speech',
                headers=headers,
                json={
                    'model': self._cfg.tts.model,
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

    def inspect(self) -> dict:
        return {
            'enabled': self.enabled,
            'configured': self._cfg.tts.ready,
            'model': self._cfg.tts.model,
            'voice': self._cfg.tts.voice,
            'format': self._cfg.tts.format,
            'failures': self._failures,
            'cacheHits': self._cache_hits,
            'cacheMisses': self._cache_misses,
        }
