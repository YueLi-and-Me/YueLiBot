"""
Python CLI 自检：`python bot.py --selftest`。

替换掉原来那个只打一行假日志的桩实现——CHAT/REFLECT/AWARE 必须真的跑一遍，
不能只是「进程起来了」就算过。原来 TS 侧的 `SELFTEST-CHAT`/`REFLECT`/`AWARE`
逻辑随 main/chat.ts 等文件一起删掉了，这是它们在 Python 侧的等价物
（docs/python-rework.md 阶段 6 的验收标准）。

★ 全程用独立临时目录建库，绝不碰 --data-dir 指向的真实 memory.db——
  REFLECT 检查会塞假消息触发摘要，写进真实库就是污染用户数据。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable

from src.common.clock import now as current_time
from src.common.db.connection import open_db
from src.common.db.migrations.manager import run_migrations
from src.common.logger import get_logger
from src.services.chat import SUMMARIZE_AT, ChatService
from src.services.proactive import AwarenessService

logger = get_logger(__name__)

_CHAT_TIMEOUT_S = 20.0


def _report(tag: str, payload: dict) -> None:
    print(f"{tag} {json.dumps(payload, ensure_ascii=False)}", flush=True)


async def run_selftest(cfg: Any) -> int:
    """三项检查全 ok（含 skip）才返回 0。"""
    tmp = Path(tempfile.mkdtemp(prefix="yueli_selftest_"))
    try:
        db_path = tmp / "memory.db"
        db = open_db(db_path)
        run_migrations(db, db_path)

        events: list[dict] = []

        async def _push_event(channel: str, payload: Any) -> None:
            events.append({"channel": channel, "payload": payload})

        from src.llm_models.router import create_routers
        routers = create_routers(cfg)
        provider = routers.chat if routers.chat.ready else None
        if provider is None:
            logger.info("selftest_no_provider", reason="model_tasks.chat.model_list 是空的")

        chat = ChatService(db=db, provider=provider, push_event=_push_event, cfg=cfg)

        chat_ok = await _check_chat(chat, provider, events)
        reflect_ok = await _check_reflect(chat, provider)
        aware_ok = await _check_aware(chat, cfg, _push_event)
        return 0 if (chat_ok and reflect_ok and aware_ok) else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def _check_chat(chat: ChatService, provider: Any, events: list[dict]) -> bool:
    if provider is None:
        _report("SELFTEST-CHAT", {"ok": True, "skipped": True, "reason": "no_provider"})
        return True
    try:
        turn = await chat.send("自检：请用一个字回复我")
        task = chat._inflight
        if task is not None:
            await asyncio.wait_for(task, timeout=_CHAT_TIMEOUT_S)
        turn_events = [e for e in events if e["payload"].get("turnId") == turn]
        done = any(e["channel"] == "chat.done" for e in turn_events)
        error = next((e for e in turn_events if e["channel"] == "chat.error"), None)
        result: dict[str, Any] = {"ok": done and error is None, "turnId": turn, "events": len(turn_events)}
        if error:
            result["error"] = error["payload"].get("message")
        _report("SELFTEST-CHAT", result)
        return bool(result["ok"])
    except Exception as exc:
        _report("SELFTEST-CHAT", {"ok": False, "reason": str(exc)})
        return False


async def _check_reflect(chat: ChatService, provider: Any) -> bool:
    if provider is None:
        _report("SELFTEST-REFLECT", {"ok": True, "skipped": True, "reason": "no_provider"})
        return True
    try:
        before = len(chat.memory.all_episodes())
        base = current_time() - 60 * 60_000
        for i in range(SUMMARIZE_AT):
            role = "user" if i % 2 == 0 else "assistant"
            chat.memory.append_message(role, f"自检消息 {i}", base + i * 1000)
        await chat._maybe_summarize()
        after = len(chat.memory.all_episodes())
        ok = after > before
        _report("SELFTEST-REFLECT", {"ok": ok, "episodesBefore": before, "episodesAfter": after})
        return ok
    except Exception as exc:
        _report("SELFTEST-REFLECT", {"ok": False, "reason": str(exc)})
        return False


async def _check_aware(
    chat: ChatService,
    cfg: Any,
    push_event: Callable[[str, dict[str, Any]], Awaitable[None]],
) -> bool:
    """不需要 LLM——纯编排检查，白盒读内部状态。"""
    try:
        awareness = AwarenessService(chat=chat, schedule=None, cfg=cfg, push_event=push_event)

        awareness.on_foreground({"process": "Code.exe", "title": "main.py - test", "fullscreen": False})
        activity = awareness._last_classified.activity if awareness._last_classified else None
        activity_ok = activity == "coding"

        # 视觉 context 那套判定随后台截图链路一起删了（现在只在他问起时看一眼），
        # 这里改查程序名——它是喂给视觉模型的先验，也是情境文本的一部分。
        awareness.on_foreground({"process": "steam.exe", "title": "Steam", "fullscreen": False})
        app = awareness.current_app()
        app_ok = app == "Steam"

        sleep_state = awareness._sleep.current(current_time())
        sleep_ok = sleep_state is not None and isinstance(sleep_state.asleep, bool)

        await asyncio.sleep(0.05)   # 让 on_foreground 里起的后台 task 收尾，避免残留 pending task 警告

        ok = activity_ok and app_ok and sleep_ok
        _report("SELFTEST-AWARE", {
            "ok": ok, "activity": activity, "app": app,
            "asleep": sleep_state.asleep if sleep_state else None,
        })
        return ok
    except Exception as exc:
        _report("SELFTEST-AWARE", {"ok": False, "reason": str(exc)})
        return False
