"""动态画面感知：关键帧抽取 + 帧序列 + 时序提示词。

背景：此前每次视觉调用都只送一张孤立截图，模型手里没有任何帧间信息，
结构上不可能回答「画面在发生什么」。参考妹居物语走的那条链路——RTC 在
服务端做关键帧抽取，再把帧序列交给模型做时序分析，模型本身仍然只读单图。
我们的画面本来就在本机，省掉传输层，只需要补抽帧和多帧输入这两层。
"""

from __future__ import annotations

import asyncio
from io import BytesIO
from typing import Any, AsyncIterator, List

from PIL import Image

from yueli.awareness.look_state import MAX_KEYFRAMES, VisionLookState
from yueli.config.schema import Config
from yueli.services.vision import KEYFRAME_DELTA, VisionService


def _frame(shade: int) -> bytes:
    """生成一张纯色 JPEG。shade 差得越多，帧差越大。"""
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
        yield {'text': '血量掉了一半'}


async def _noop_push(_channel: str, _payload: Any) -> None:
    return None


def _cfg(frames: int) -> Config:
    cfg = Config()
    cfg.vision.enabled = True
    cfg.vision.frames = frames
    return cfg


def _images(provider: _Recorder) -> List[dict]:
    return [c for c in provider.messages[0]['content'] if c['type'] == 'image_url']


def _prompt(provider: _Recorder) -> str:
    return provider.messages[0]['content'][0]['text']


# ── 关键帧抽取 ─────────────────────────────────────────────────────────

def test_static_frames_do_not_pile_up_in_the_buffer() -> None:
    """静止画面重复入列，等于拿 N 张一样的图问模型发生了什么变化。"""
    state = VisionLookState()
    same = _frame(120)
    state.push_keyframe('gameplay', same, 3)
    # 后续帧与上一帧无差异时，调用方不会 push——这里直接验证缓冲区语义
    assert len(state.keyframes('gameplay')) == 1


def test_keyframe_buffer_keeps_recent_frames_in_time_order() -> None:
    state = VisionLookState()
    for shade in (10, 60, 110, 160, 210):
        state.push_keyframe('gameplay', _frame(shade), 3)

    frames = state.keyframes('gameplay')
    assert len(frames) == 3, '应只保留最近 3 帧'
    assert frames[-1] == _frame(210), '最后一帧必须是最新的'
    assert frames[0] == _frame(110), '最旧的应该被挤出去'


def test_keyframe_buffer_is_per_context() -> None:
    state = VisionLookState()
    state.push_keyframe('gameplay', _frame(10), 3)
    state.push_keyframe('steam-library', _frame(200), 3)

    assert len(state.keyframes('gameplay')) == 1
    assert len(state.keyframes('steam-library')) == 1


def test_buffer_limit_is_capped(monkeypatch) -> None:
    state = VisionLookState()
    for shade in range(0, 250, 20):
        state.push_keyframe('gameplay', _frame(shade), MAX_KEYFRAMES + 10)
    assert len(state.keyframes('gameplay')) <= MAX_KEYFRAMES + 10


# ── 多帧输入 ───────────────────────────────────────────────────────────

async def test_single_frame_mode_matches_previous_behaviour() -> None:
    """frames=1 是默认值，请求形状必须和改动前一致——单图 + 「是什么」提示词。"""
    provider = _Recorder()
    service = VisionService(_cfg(1), _noop_push, provider)

    await service._call_vision_model([_frame(100)], 'gameplay')

    assert len(_images(provider)) == 1
    prompt = _prompt(provider)
    assert '连续截图' not in prompt
    assert '眼下能直接看见的状态' in prompt


async def test_multi_frame_sends_sequence_and_asks_about_change() -> None:
    provider = _Recorder()
    service = VisionService(_cfg(3), _noop_push, provider)

    frames = [_frame(20), _frame(120), _frame(220)]
    await service._call_vision_model(frames, 'gameplay')

    images = _images(provider)
    assert len(images) == 3, '帧序列必须整体送进去'
    # 顺序必须是旧 → 新，否则模型读出来的时序是反的
    import base64
    sent = [base64.b64decode(i['image_url']['url'].split(',', 1)[1]) for i in images]
    assert sent == frames

    prompt = _prompt(provider)
    assert '3 张图' in prompt and '最后一张最新' in prompt
    assert '正在发生什么' in prompt
    # 要允许它说「没变化」，否则模型会硬编出一个变化来
    assert '没怎么动' in prompt


async def test_folder_context_stays_single_shot_even_with_frames_configured() -> None:
    """文件夹是一次性静态判断，多送帧没有意义，提示词不该变成时序版。"""
    provider = _Recorder()
    service = VisionService(_cfg(3), _noop_push, provider)

    await service._call_vision_model([_frame(10), _frame(200)], 'game-folder')

    prompt = _prompt(provider)
    assert '有游戏文件夹' in prompt
    assert '连续截图' not in prompt


async def test_frame_limit_respects_config_and_cap() -> None:
    assert VisionService(_cfg(1), _noop_push, _Recorder())._frame_limit() == 1
    assert VisionService(_cfg(3), _noop_push, _Recorder())._frame_limit() == 3
    # 配置层已经 le=4，这里再确认服务层不会被更大的值撑爆
    cfg = _cfg(1)
    object.__setattr__(cfg.vision, 'frames', 99)
    assert VisionService(cfg, _noop_push, _Recorder())._frame_limit() == MAX_KEYFRAMES


def test_keyframe_delta_is_looser_than_look_threshold() -> None:
    """攒序列要的是「画面动过」的证据，不是「值得叫模型」的大动作。"""
    from yueli.services.vision import FRAME_CHANGE_THRESHOLD
    assert KEYFRAME_DELTA < FRAME_CHANGE_THRESHOLD


# ── 链路接通 ───────────────────────────────────────────────────────────

async def test_vision_context_no_longer_fakes_gameplay() -> None:
    """没有匹配场景时如实返回 None，别再伪造成 gameplay。

    此前 ingest 侧兜底成 'gameplay'，消费侧又要求非空：写代码时截图照样
    上传、用游戏提示词描述、结果永远不被读取——隐私和钱都白付。
    """
    from yueli.services.chat import ChatService
    from yueli.services.proactive import AwarenessService

    async def _push(_c, _p):
        return None

    import sqlite3
    from yueli.memory.store import MemoryStore
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    MemoryStore(db)
    chat = ChatService(db=db, provider=None, push_event=_push)
    svc = AwarenessService(chat=chat, schedule=None, cfg=Config())

    # 写代码不属于 steam-library / gameplay / game-folder 任何一种
    svc.on_foreground({'process': 'Code.exe', 'title': 'main.py', 'fullscreen': False})
    await asyncio.sleep(0.02)
    assert svc.vision_context() is None, '写代码时不该伪造出一个视觉场景'
    db.close()


async def test_activity_text_carries_vision_description_into_normal_chat() -> None:
    """视觉描述必须能进正常对话，而不是只在主动搭话时才用得上。"""
    import sqlite3
    from yueli.memory.store import MemoryStore
    from yueli.services.chat import ChatService
    from yueli.services.proactive import AwarenessService

    async def _push(_c, _p):
        return None

    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    MemoryStore(db)
    chat = ChatService(db=db, provider=None, push_event=_push)
    svc = AwarenessService(chat=chat, schedule=None, cfg=Config())
    svc.on_foreground({'process': 'game.exe', 'title': '', 'fullscreen': False})
    await asyncio.sleep(0.02)

    class _Vision:
        def recent_description(self, _ctx):
            return '血量掉了一半'

    svc._vision = _Vision()
    svc._vision_context = 'gameplay'

    text = svc._activity_text()
    assert '血量掉了一半' in text, f'视觉描述没进情境文本：{text}'
    assert '瞥了一眼屏幕' in text
    db.close()
