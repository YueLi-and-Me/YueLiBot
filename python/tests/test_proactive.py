"""
AwarenessService 编排测试。

只测编排是否正确调用了已经被 test_phase3.py 覆盖的 classify/budget/sleep
纯函数，不重复测这些函数本身。真实 LLM 一律不打——compose_proactive/speak
换成记录调用的 stub。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import yueli.services.proactive as proactive_module
from yueli.awareness.sleep import SleepState
from yueli.config.schema import Config
from yueli.services.chat import ChatService
from yueli.services.proactive import AwarenessService


async def _noop_push(channel, payload):
    return None


def _make_chat(db) -> ChatService:
    return ChatService(db=db, provider=None, push_event=_noop_push)


def _make_ready_chat(db) -> ChatService:
    """chat.ready=True，但 compose_proactive/speak 是记录调用的 stub，不打真实 LLM。"""
    chat = _make_chat(db)
    chat._provider = object()
    chat.compose_proactive = AsyncMock(return_value=[{'text': '在干嘛呢'}])
    chat.speak = MagicMock(return_value=1)
    return chat


async def test_on_foreground_updates_classification(db):
    """window_changed() 那几条断言随后台截图链路一起删了——它只有那个端点在读。
    窗口切换本身仍然驱动主动搭话，走的是 monitor 的 obs.window_changed。"""
    svc = AwarenessService(chat=_make_chat(db), schedule=None, cfg=Config())

    svc.on_foreground({'process': 'Code.exe', 'title': 'a.py', 'fullscreen': False})
    await asyncio.sleep(0.02)
    assert svc._last_classified.activity == 'coding'
    assert svc.current_app() == 'VS Code'

    svc.on_foreground({'process': 'chrome.exe', 'title': '某网页', 'fullscreen': False})
    await asyncio.sleep(0.02)
    assert svc._last_classified.activity == 'browsing'


async def test_meeting_process_does_not_trigger_speak(db):
    chat = _make_ready_chat(db)
    svc = AwarenessService(chat=chat, schedule=None, cfg=Config())

    svc.on_foreground({'process': 'Zoom.exe', 'title': 'meeting', 'fullscreen': False})
    await asyncio.sleep(0.05)

    chat.compose_proactive.assert_not_awaited()
    chat.speak.assert_not_called()


async def test_window_change_triggers_scene_speak_when_allowed(db):
    chat = _make_ready_chat(db)
    svc = AwarenessService(chat=chat, schedule=None, cfg=Config())

    svc.on_foreground({'process': 'Code.exe', 'title': 'main.py', 'fullscreen': False})
    await asyncio.sleep(0.05)

    chat.compose_proactive.assert_awaited_once()
    chat.speak.assert_called_once()
    assert svc._budget.used == 1


async def test_cooldown_prevents_repeat_scene_speak(db):
    chat = _make_ready_chat(db)
    svc = AwarenessService(chat=chat, schedule=None, cfg=Config())

    svc.on_foreground({'process': 'Code.exe', 'title': 'a', 'fullscreen': False})
    await asyncio.sleep(0.05)
    assert chat.compose_proactive.await_count == 1

    # 立刻切到另一个窗口——真实挂钟时间远小于 30 分钟冷却，应该被挡住
    svc.on_foreground({'process': 'chrome.exe', 'title': 'b', 'fullscreen': False})
    await asyncio.sleep(0.05)
    assert chat.compose_proactive.await_count == 1


async def test_sleep_state_pushed_only_on_transition(db, monkeypatch):
    chat = _make_chat(db)
    svc = AwarenessService(chat=chat, schedule=None, cfg=Config())

    calls: list[tuple[str, dict]] = []

    async def fake_push(channel, payload):
        calls.append((channel, payload))

    monkeypatch.setattr(proactive_module, 'push', fake_push)

    svc._sleep.current = MagicMock(side_effect=[
        SleepState(asleep=False, drowsy=False, just_woke=False, probability=0.1),
        SleepState(asleep=False, drowsy=False, just_woke=False, probability=0.2),
        SleepState(asleep=True, drowsy=False, just_woke=False, probability=0.9),
    ])

    await svc._refresh_sleep(1_000)
    await svc._refresh_sleep(2_000)
    await svc._refresh_sleep(3_000)

    assert len(calls) == 2   # 初次上报 + 一次真实翻转；中间那次状态没变，不重复推
    assert calls[0][1]['asleep'] is False
    assert calls[1][1]['asleep'] is True


async def test_startup_wires_providers_and_shutdown_stops_poll_task(db):
    chat = _make_chat(db)
    svc = AwarenessService(chat=chat, schedule=None, cfg=Config())

    await svc.startup()
    await asyncio.sleep(0.05)
    assert chat._activity is not None
    assert chat._sleep_state is not None
    assert svc._poll_task is not None and not svc._poll_task.done()

    await svc.shutdown()
    assert svc._poll_task is None
