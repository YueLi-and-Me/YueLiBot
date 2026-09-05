"""验证屏幕意图触发、视觉描述时效和视觉服务失败处理。

本模块覆盖明确屏幕意图时的即时采集、旧描述过期后的拒绝复用、
前台窗口情境注入以及 provider 超时边界，确保视觉上下文只来自当前有效描述。
"""

from __future__ import annotations

import asyncio
from io import BytesIO
from typing import Any, AsyncIterator, List

from PIL import Image

from src.core.config.schema import Config
from src.desktop.vision import VisionService


def _frame(shade: int = 100) -> bytes:
    buf = BytesIO()
    Image.new('RGB', (64, 64), (shade, shade, shade)).save(buf, format='JPEG')
    return buf.getvalue()


class _Recorder:
    model = 'vision-test'

    def __init__(self) -> None:
        self.messages: List[dict[str, Any]] = []
        self.calls = 0

    async def stream(self, messages, temperature=0.85, max_tokens=None, signal=None) -> AsyncIterator[dict]:
        self.messages = messages
        self.calls += 1
        yield {'text': 'PyCharm 里在改一个报错的函数'}


async def _noop_push(_channel: str, _payload: Any, _stream_id: int = 1) -> None:
    return None


def _cfg() -> Config:
    cfg = Config()
    cfg.vision.enabled = True
    return cfg


def _images(provider: _Recorder) -> List[dict]:
    return [c for c in provider.messages[0]['content'] if c['type'] == 'image_url']


def _prompt(provider: _Recorder) -> str:
    return provider.messages[0]['content'][0]['text']


# ── 请求形状 ───────────────────────────────────────────────────────────

async def test_glance_sends_one_image_and_asks_what_is_happening() -> None:
    provider = _Recorder()
    service = VisionService(_cfg(), _noop_push, provider)

    await service.glance(_frame())

    assert len(_images(provider)) == 1
    assert '正在做什么' in _prompt(provider)


async def test_process_name_is_given_to_the_model_as_a_prior() -> None:
    """模型经常认不出小众界面，先告诉它这是什么程序——最便宜的一个先验。"""
    provider = _Recorder()
    service = VisionService(_cfg(), _noop_push, provider)

    await service.glance(_frame(), app='PyCharm')

    assert 'PyCharm' in _prompt(provider)


async def test_prompt_allows_admitting_it_cannot_tell() -> None:
    """不给「看不清」这个出口，模型就会硬编一个界面出来。"""
    provider = _Recorder()
    await VisionService(_cfg(), _noop_push, provider).glance(_frame())
    assert '看不清' in _prompt(provider)


# ── 缓存、冷却与过期 ───────────────────────────────────────────────────

async def test_glance_returns_description_and_caches_it() -> None:
    provider = _Recorder()
    service = VisionService(_cfg(), _noop_push, provider)

    description = await service.glance(_frame())

    assert description == 'PyCharm 里在改一个报错的函数'
    assert service.chat_glance() == description
    assert provider.calls == 1


async def test_glance_reuses_cache_within_cooldown_window() -> None:
    """连着问「看看我屏幕」「现在呢」不该每句都摊一次视觉调用。"""
    provider = _Recorder()
    service = VisionService(_cfg(), _noop_push, provider)

    first = await service.glance(_frame(100))
    second = await service.glance(_frame(200))   # 画面变了也不重新调

    assert first == second
    assert provider.calls == 1


async def test_glance_recalls_model_after_cooldown_expires(monkeypatch) -> None:
    from src.desktop import vision as vision_module

    provider = _Recorder()
    service = VisionService(_cfg(), _noop_push, provider)
    fake_now = [1_000_000]
    monkeypatch.setattr(vision_module, 'current_time', lambda: fake_now[0])

    await service.glance(_frame(100))
    fake_now[0] += vision_module.CHAT_GLANCE_COOLDOWN_MS + 1
    await service.glance(_frame(200))

    assert provider.calls == 2


async def test_stale_description_expires_instead_of_posing_as_current(monkeypatch) -> None:
    """过期描述不得继续作为当前屏幕内容，必须在 TTL 到期后清除。"""
    from src.desktop import vision as vision_module

    service = VisionService(_cfg(), _noop_push, _Recorder())
    fake_now = [1_000_000]
    monkeypatch.setattr(vision_module, 'current_time', lambda: fake_now[0])

    await service.glance(_frame())
    assert service.chat_glance() is not None

    fake_now[0] += vision_module.CHAT_GLANCE_TTL_MS + 1
    assert service.chat_glance() is None, '过期描述必须消失，不能继续冒充当前画面'


def test_ttl_covers_the_reuse_cooldown() -> None:
    """TTL 必须 >= 复用冷却，否则会出现「过期了但还不允许重新调用」的死窗口。"""
    from src.desktop.vision import CHAT_GLANCE_COOLDOWN_MS, CHAT_GLANCE_TTL_MS
    assert CHAT_GLANCE_TTL_MS >= CHAT_GLANCE_COOLDOWN_MS


# ── 失败要暴露，不要兜底 ───────────────────────────────────────────────

async def test_failed_glance_does_not_resurrect_old_description() -> None:
    class _Failing:
        model = 'vision-test'

        async def stream(self, messages, temperature=0.85, max_tokens=None, signal=None):
            return
            yield {}   # pragma: no cover —— 让它是个 async generator

    service = VisionService(_cfg(), _noop_push, _Failing())
    assert await service.glance(_frame()) is None


async def test_slow_model_hits_the_deadline_instead_of_hanging(monkeypatch) -> None:
    """对话期间的视觉请求必须使用独立截止时间，不能沿用后台 provider 的长超时和重试预算。"""
    from src.desktop import vision as vision_module

    class _Hanging:
        model = 'vision-test'

        async def stream(self, messages, temperature=0.85, max_tokens=None, signal=None):
            await asyncio.sleep(60)
            yield {'text': '来不及了'}   # pragma: no cover

    monkeypatch.setattr(vision_module, 'CHAT_GLANCE_DEADLINE_S', 0.05)
    service = VisionService(_cfg(), _noop_push, _Hanging())

    assert await service.glance(_frame()) is None


def test_deadline_is_shorter_than_the_electron_abort() -> None:
    """必须明显小于 Electron 侧 CHAT_GLANCE_TIMEOUT（20s），否则那边先 abort。"""
    from src.desktop.vision import CHAT_GLANCE_DEADLINE_S
    assert CHAT_GLANCE_DEADLINE_S < 20


async def test_disabled_vision_never_calls_the_model() -> None:
    provider = _Recorder()
    cfg = Config()
    cfg.vision.enabled = False

    assert await VisionService(cfg, _noop_push, provider).glance(_frame()) is None
    assert provider.calls == 0


# ── 情境文本拼接 ───────────────────────────────────────────────────────

def _awareness():
    import sqlite3
    from src.core.memory.store import MemoryStore
    from src.core.services.chat import ChatService
    from src.core.services.proactive import AwarenessService
    from src.core.schedule.timeline import ActivityTimeline
    from src.desktop.sensor import DesktopSensor

    async def _push(_c, _p, _stream_id=1):
        return None

    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    MemoryStore(db)
    chat = ChatService(
        db=db,
        chat_provider=None,
        proactive_provider=None,
        summary_provider=None,
        push_event=_push,
        cfg=Config(),
    )
    cfg = Config()
    # 主动感知在桌宠开关关闭时整体停用；这里构造的是桌面场景，显式打开。
    cfg.desktop_pet.enabled = True
    sensor = DesktopSensor(cfg, _noop_push)
    return AwarenessService(
        chat=chat,
        schedule=None,
        timeline=ActivityTimeline(db),
        cfg=cfg,
        push_event=_noop_push,
        sensor=sensor,
    ), db


def test_no_description_tells_her_she_cannot_see() -> None:
    """没有有效屏幕描述时，提示词不得把历史或旧记忆中的界面当作当前视觉证据。"""
    svc, db = _awareness()

    class _Blind:
        def chat_glance(self):
            return None

    svc._sensor._vision = _Blind()
    text = svc._with_vision('他在写代码。')
    assert '看不到他的屏幕' in text
    assert '别拿以前看到过的界面充数' in text
    db.close()


def test_description_is_marked_authoritative_for_the_current_screen() -> None:
    """当前屏幕描述必须标记为唯一依据，避免被历史内容覆盖。"""
    svc, db = _awareness()

    class _Vision:
        def chat_glance(self):
            return '流程图编辑界面'

    svc._sensor._vision = _Vision()
    text = svc._with_vision('他在处理文档或工作。')
    assert '流程图编辑界面' in text
    assert '只能依据这一句' in text
    assert '属于回忆' in text, '必须点明旧界面是回忆，不是现在'
    db.close()


async def test_activity_text_carries_the_description_into_normal_chat() -> None:
    """视觉描述必须能进正常对话，而不是只在主动搭话时才用得上。"""
    svc, db = _awareness()
    svc.on_foreground({'process': 'pycharm64.exe', 'title': 'main.py', 'fullscreen': False})
    await asyncio.sleep(0.02)

    class _Vision:
        def chat_glance(self):
            return 'PyCharm 里在改一个报错的函数'

    svc._sensor._vision = _Vision()
    text = svc._activity_text()

    assert 'PyCharm 里在改一个报错的函数' in text
    assert '瞥了一眼屏幕' in text
    db.close()
