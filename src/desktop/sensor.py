"""汇总桌面前台、输入强度与视觉信号，向主动感知内核提供窄接口。"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from src.core.awareness.signals import Classified
from src.core.common.clock import now as current_time
from src.core.common.logger import get_logger
from src.core.observe import events as trace

from .classify import ForegroundInfo, classify, classify_input, describe_activity
from .monitor import ForegroundProcessMonitor
from .vision import VisionProvider, VisionService


logger = get_logger(__name__)


class DesktopSensor:
    """摄入 Electron 桌面快照并维护可供内核读取的低敏感度信号。"""

    def __init__(
        self,
        cfg: Any,
        push_event: Callable[[str, dict[str, Any]], Awaitable[None]],
        vision_provider: VisionProvider | None = None,
    ) -> None:
        """初始化桌面传感器，但不在构造阶段创建视觉服务。"""
        self._cfg = cfg
        self._push_event = push_event
        self._vision_provider = vision_provider
        self._monitor = ForegroundProcessMonitor()
        self._last_classified: Classified | None = None
        self._last_activity_since = current_time()
        self._last_visible = True
        self._vision: VisionService | None = None
        self._vision_spoke_count = 0

    def startup(self) -> None:
        """按配置创建视觉服务。"""
        if self._cfg.vision.ready and self._vision_provider:
            self._vision = VisionService(self._cfg, self._push_event, self._vision_provider)
            logger.info('vision_service_ready', model=self._vision_provider.model)
        elif self._cfg.vision.ready:
            logger.warning('vision_service_disabled', reason='视觉模型配置无效，请检查后端启动日志')

    def ingest(self, body: dict[str, Any]) -> tuple[Classified, bool]:
        """摄入一次 Electron 快照并返回分类信号与窗口变化标志。"""
        now = current_time()
        info = ForegroundInfo(
            process=str(body.get('process') or ''),
            title=body.get('title'),
            fullscreen=bool(body.get('fullscreen')),
        )
        if 'visible' in body:
            self._last_visible = bool(body.get('visible'))

        input_snapshot = body.get('input')
        if isinstance(input_snapshot, dict):
            intensity = classify_input(
                int(input_snapshot.get('keys', 0)),
                int(input_snapshot.get('clicks', 0)),
                float(input_snapshot.get('mouseDistance', 0)),
                int(input_snapshot.get('idleSeconds', 0)),
                int(input_snapshot.get('spanMs', 8_000)),
            )
        else:
            intensity = 'light'

        observation = self._monitor.observe(info)
        classified = classify(info)
        classified.intensity = intensity
        if (
            observation.process_changed
            or self._last_classified is None
            or classified.activity != self._last_classified.activity
        ):
            self._last_activity_since = now
        self._last_classified = classified

        # 窗口标题只用于本地分类和指纹比较，不写入观察事件。
        trace.emit(
            'foreground',
            process=info.process,
            activity=classified.activity,
            intensity=classified.intensity,
            silent=classified.silent,
            windowChanged=observation.window_changed,
        )
        return classified, observation.window_changed

    @property
    def signal(self) -> Classified | None:
        """返回最近一次分类信号。"""
        return self._last_classified

    @property
    def visible(self) -> bool:
        """返回桌面外壳最近报告的可见状态。"""
        return self._last_visible

    @property
    def vision(self) -> VisionService | None:
        """返回已创建的视觉服务。"""
        return self._vision

    def minutes(self, now: int) -> int:
        """返回当前活动已持续的整分钟数。"""
        return max(0, (now - self._last_activity_since) // 60_000)

    def activity_text(self, now: int | None = None) -> str:
        """返回不含视觉信息的当前桌面活动文本。"""
        if self._last_classified is None:
            return ''
        sampled_at = current_time() if now is None else now
        return describe_activity(self._last_classified, self.minutes(sampled_at))

    def with_vision(self, situation: str) -> str:
        """把当前有效视觉描述附加到一段情境文本。"""
        if self._vision is None:
            return situation
        description = self._vision.chat_glance()
        if not description:
            return f'{situation}\n（你这会儿看不到他的屏幕。他要是问起，就直说这会儿没看清，别拿以前看到过的界面充数。）'
        return (
            f'{situation}\n（你刚瞥了一眼屏幕，看到的就是这些：{description}。'
            '问到屏幕上有什么，只能依据这一句；以前看到过的界面、文件夹属于回忆，'
            '别当成现在还在那儿。）'
        )

    def has_vision_description(self) -> bool:
        """判断视觉服务当前是否持有有效描述。"""
        return bool(self._vision and self._vision.chat_glance())

    def note_vision_spoke(self) -> None:
        """记录一次实际使用视觉描述的主动搭话。"""
        self._vision_spoke_count += 1

    def vision_stats(self) -> dict[str, Any]:
        """返回不含截图和模型原文的视觉调用统计。"""
        if self._vision is None:
            return {'enabled': False, 'looks': 0, 'spoke': 0}
        return {**self._vision.stats(), 'spoke': self._vision_spoke_count}

    def current_app(self) -> str:
        """返回最近分类信号中的程序名。"""
        return self._last_classified.app if self._last_classified else ''
