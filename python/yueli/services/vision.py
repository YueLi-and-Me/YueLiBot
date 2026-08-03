"""
视觉服务：接收 Electron 推来的截图 JPEG，做帧差，决策是否调视觉模型。
直接移植 src/core/vision/index.ts 的核心逻辑。

隐私约束（与 TS 版一致）：
  · 只处理 Electron 截下来的那一个前台窗口，不截整屏
  · 截图绝不落盘，用完即弃
  · 只有经过结构化脱敏的文字线索才会进 context
"""

from __future__ import annotations

from collections import Counter
from io import BytesIO
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol

from PIL import Image

import asyncio
import base64
import hashlib

from yueli.awareness.look import within_look_cooldown
from yueli.awareness.look_state import VisionLookState
from yueli.common.clock import now as current_time
from yueli.common.logger import get_logger
from yueli.config.schema import Config
from yueli.llm.openai import LlmError

logger = get_logger(__name__)

# 与 look.ts 保持一致
FRAME_CHANGE_THRESHOLD = 0.18
# 视觉描述缓存的有效期——超过这个时长的描述不再喂进主动搭话的情境文本
DESCRIPTION_TTL_MS = 5 * 60_000


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


def _frame_delta(a: bytes | None, b: bytes) -> float:
    """两帧之间的变化程度（0~1）。移植自 src/main/platform/capture.ts frameDelta。"""
    if a is None:
        return 1.0
    # 简单 pixel-level comparison using pillow
    try:
        img_a = Image.open(BytesIO(a)).convert('L').resize((32, 32))
        img_b = Image.open(BytesIO(b)).convert('L').resize((32, 32))
        pa = list(img_a.getdata())
        pb = list(img_b.getdata())
        diff = sum(abs(x - y) for x, y in zip(pa, pb))
        return min(1.0, diff / (255 * len(pa)))
    except Exception:
        # Fallback: hash comparison
        return 0.0 if hashlib.sha256(a).digest() == hashlib.sha256(b).digest() else 1.0


class VisionService:
    def __init__(self, cfg: Config,
                 push_event: Callable[[str, dict[str, Any]], Awaitable[None]],
                 provider: VisionProvider | None,
                 look_state: VisionLookState | None = None) -> None:
        self._cfg = cfg
        self._push_event = push_event
        self._provider = provider
        self._last_frames: dict[str, bytes] = {}
        # ★ 必须是持久实例，跨请求保留冷却/帧差状态。
        #   传 None 只在单测里发生；生产路径由 AwarenessService 注入同一个
        #   实例，这样 on_foreground() 里 enter() 记的 context_since 才对得上。
        self._look = look_state or VisionLookState()
        self._descriptions: dict[str, tuple[str, int]] = {}   # context -> (text, at)
        self._protocol_error: str | None = None
        # 观察面板用：看了几次、每次 should_look() 判定的原因分布——
        # 排查"为什么她从来不看屏幕"时，byReason 常年卡在 cooldown/no-change
        # 就是阈值该调的信号（docs/observability.md 里提过这条）。
        self._looks = 0
        self._reason_counts: Counter[str] = Counter()

    def stats(self) -> dict[str, Any]:
        return {
            'enabled': self._cfg.vision.enabled,
            'available': self._protocol_error is None,
            'error': self._protocol_error,
            'looks': self._looks,
            'byReason': dict(self._reason_counts),
        }

    def recent_description(self, context: str, ttl_ms: int = DESCRIPTION_TTL_MS) -> str | None:
        """最近一次视觉描述，过期或没有则返回 None。供 AwarenessService 组装主动搭话情境用。"""
        entry = self._descriptions.get(context)
        if not entry:
            return None
        text, at = entry
        if current_time() - at > ttl_ms:
            return None
        return text

    async def process_screenshot(self, jpeg_bytes: bytes, context: str, window_changed: bool) -> None:
        """
        接收 Electron 截图，决定是否调视觉模型并生成情境描述。
        context: 'steam-library' | 'gameplay' | 'game-folder'
        """
        if not self._cfg.vision.enabled or not jpeg_bytes or self._protocol_error:
            return

        prev = self._last_frames.get(context)
        delta = _frame_delta(prev, jpeg_bytes)
        self._last_frames[context] = jpeg_bytes

        now = current_time()
        self._look.enter(context, now)
        # game-folder 是文件夹切换这种低频真实动作，不受全局瞥视冷却限制；
        # 其余场景先过一道全局冷却（LOOK_COOLDOWN_MS），避免看得太勤。
        # ★ 这条早退在 should_look() 之外，它自己不产出 reason——'cooldown' 这个
        #   分类专门留给这里用（look.py 的 LookReason 类型里有它，但
        #   should_look() 从不返回它，因为冷却判断本来就不归它管）。
        if context != 'game-folder' and within_look_cooldown(now, self._look.last_look_at):
            self._reason_counts['cooldown'] += 1
            return
        decision = self._look.evaluate(context, now, delta, window_changed)
        self._reason_counts[decision.reason] += 1
        if not decision.look:
            return
        self._looks += 1
        self._look.note_call(context, now)
        self._look.note_look(now)

        await self._push_event('vision.watching', {'watching': True})
        try:
            description = await self._call_vision_model(jpeg_bytes, context)
            if description:
                self._descriptions[context] = (description, current_time())
                logger.info('vision_description', context=context, chars=len(description))
        finally:
            await self._push_event('vision.watching', {'watching': False})

    async def _call_vision_model(self, jpeg_bytes: bytes, context: str) -> str | None:
        if not self._provider or self._protocol_error:
            return None
        try:
            b64 = base64.b64encode(jpeg_bytes).decode('ascii')
            prompt = self._build_vision_prompt(context)
            raw = ''
            async for chunk in self._provider.stream(
                messages=[{
                    'role': 'user',
                    'content': [
                        {'type': 'text', 'text': prompt},
                        {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64}', 'detail': 'low'}},
                    ],
                }],
                temperature=0.3, max_tokens=120,
            ):
                if chunk.get('text'):
                    raw += chunk['text']
            return raw.strip() or None
        except LlmError as exc:
            message = str(exc)
            if 'unknown variant `image_url`' in message or 'expected `text`' in message:
                self._protocol_error = (
                    '当前模型接口不接受 OpenAI image_url 消息块；'
                    '请配置支持图片输入的视觉 API，或为该接口实现专用协议适配器'
                )
                logger.warning(
                    'vision_model_not_multimodal',
                    model=self._provider.model,
                    error=self._protocol_error,
                )
                return None
            logger.warning('vision_call_failed', model=self._provider.model, error=message)
            return None
        except Exception as exc:
            logger.warning('vision_call_failed', error=str(exc))
            return None

    @staticmethod
    def _build_vision_prompt(context: str) -> str:
        if context == 'steam-library':
            return (
                '给月璃提取一条客观情境线索。这是 Steam 库页面：看得清时只写一到两个最显眼的游戏名，'
                '总共不超过十个字；看不清就只写「看不清」。不要描述界面，不要猜。'
            )
        if context == 'gameplay':
            return (
                '给月璃提取一条客观情境线索。这是游戏画面：用不超过十五个字写眼下能直接看见的状态，'
                '例如「正在打首领」或「停在装备菜单」。不要猜游戏名、剧情或玩家感受。'
            )
        if context == 'game-folder':
            return '这是文件夹截图。只输出「有游戏文件夹」或「没有游戏文件夹」，不要写文件名、路径或其他内容。'
        return '用不超过十五个字写一条从截图中直接看见的情境事实。不要推测，不要加开场白。'
