"""执行 Python 服务的离线自检并输出机器可解析的检查结果。

自检在独立临时目录中创建并迁移 SQLite 数据库，依次覆盖对话编排、摘要写入
和感知状态检查。模型提供者未配置时，依赖模型的检查报告为跳过并视为通过；
临时数据库和观察事件存储在函数结束时清理，不访问运行时数据目录。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable

import asyncio
import json
import shutil
import tempfile

from src.core.common.clock import now as current_time
from src.core.common.db.connection import open_db
from src.core.common.db.migrations.manager import run_migrations
from src.core.common.logger import get_logger
from src.core.llm_models.protocol import LlmProvider
from src.core.observe.store import close as close_event_store
from src.core.observe.store import configure as configure_event_store
from src.core.services.chat import ChatService, InboundMessage
from src.core.services.proactive import AwarenessService

logger = get_logger(__name__)

_CHAT_TIMEOUT_S = 20.0


def _report(tag: str, payload: dict) -> None:
    """以单行 JSON 输出一项自检结果。

    :param tag: 稳定的检查名称。
    :param payload: 可 JSON 序列化的结果字段。

    副作用：
        向标准输出写入一行并立即刷新，便于 CLI 调用方实时消费。
    """

    print(f"{tag} {json.dumps(payload, ensure_ascii=False)}", flush=True)


async def run_selftest(cfg: Any) -> int:
    """在隔离数据库中执行完整自检流程。

    :param cfg: 已加载的运行时配置，用于构建模型路由和服务。

    :return: 三项检查均通过或跳过时返回 0，否则返回 1。

    副作用：
        创建临时数据库、注册观察事件存储并输出三项检查结果；函数结束时关闭
        存储并删除临时目录。
    """
    tmp = Path(tempfile.mkdtemp(prefix="yueli_selftest_"))
    try:
        # 自检数据库与观察账本都绑定到临时目录，避免测试消息进入运行时数据。
        db_path = tmp / "memory.db"
        db = open_db(db_path)
        run_migrations(db, db_path)
        configure_event_store(db_path)

        events: list[dict] = []

        async def _push_event(channel: str, payload: Any, stream_id: int = 1) -> None:
            """将自检事件保存到内存列表，不连接真实客户端。

            :param channel: 事件通道名称。
            :param payload: 通道负载对象。
            :param stream_id: 事件所属 stream ID，默认 ``1``。

            副作用：
                向当前自检作用域的事件列表追加一条事件记录。
            """

            events.append({"stream_id": stream_id, "channel": channel, "payload": payload})

        from src.core.llm_models.router import create_routers
        routers = create_routers(cfg)
        chat_provider = routers.chat if routers.chat.ready else None
        proactive_provider = routers.proactive if routers.proactive.ready else None
        summary_provider = routers.summary if routers.summary.ready else None
        if chat_provider is None:
            logger.info("selftest_no_provider", reason="model_tasks.chat.model_list 是空的")

        chat = ChatService(
            db=db,
            chat_provider=chat_provider,
            proactive_provider=proactive_provider,
            summary_provider=summary_provider,
            push_event=_push_event,
            cfg=cfg,
        )

        # 三条链路分别验证对话、摘要和纯编排状态；缺少模型时由子检查报告跳过。
        chat_ok = await _check_chat(chat, chat_provider, events)
        reflect_ok = await _check_reflect(chat, summary_provider)
        aware_ok = await _check_aware(chat, cfg, _push_event)
        return 0 if (chat_ok and reflect_ok and aware_ok) else 1
    finally:
        close_event_store()
        shutil.rmtree(tmp, ignore_errors=True)


async def _check_chat(chat: ChatService, provider: LlmProvider | None, events: list[dict]) -> bool:
    """验证一次真实对话请求能够完成并产生结束事件。

    :param chat: 已绑定临时数据库和事件推送回调的聊天服务。
    :param provider: 已选中的聊天模型提供者；为 ``None`` 时跳过检查。
    :param events: 接收聊天事件的内存列表。

    :return: 对话完成且没有 ``chat.error`` 时返回 ``True``；无提供者时按跳过处理。

    副作用：
        可能调用模型提供者并向临时事件列表追加聊天事件。
    """

    if provider is None:
        _report("SELFTEST-CHAT", {"ok": True, "skipped": True, "reason": "no_provider"})
        return True
    try:
        # 发送后等待同 stream 的 inflight task，确保事件统计覆盖完整回合。
        context = chat.desktop_context
        turn = await chat.send(InboundMessage(text="自检：请用一个字回复我", context=context))
        inflight = chat._inflight.get(context.stream.id)
        if inflight is not None:
            await asyncio.wait_for(inflight.task, timeout=_CHAT_TIMEOUT_S)
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


async def _check_reflect(chat: ChatService, provider: LlmProvider | None) -> bool:
    """验证摘要阈值触发后能够写入新的 episode。

    :param chat: 已初始化的聊天服务。
    :param provider: 已选中的摘要模型提供者；为 ``None`` 时跳过检查。

    :return: 摘要数量增加时返回 ``True``；无提供者时按跳过处理。

    副作用：
        向临时数据库写入交替的用户和助手消息，并可能调用摘要模型。
    """

    if provider is None:
        _report("SELFTEST-REFLECT", {"ok": True, "skipped": True, "reason": "no_provider"})
        return True
    try:
        before = len(chat.memory.all_episodes())
        base = current_time() - 60 * 60_000
        desktop_context = chat.desktop_context
        # 写入达到摘要阈值的消息数量，验证摘要服务是否真正消费了这批数据。
        for i in range(chat._summarize_trigger_messages):
            role = "user" if i % 2 == 0 else "assistant"
            sender_person_id = desktop_context.person.id if role == "user" else None
            chat.memory.append_message(
                desktop_context.stream.id,
                sender_person_id,
                role,
                f"自检消息 {i}",
                base + i * 1000,
            )
        await chat._maybe_summarize(desktop_context.stream.id)
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
    """在不调用模型的情况下验证前台活动和睡眠状态编排。

    :param chat: 已初始化的聊天服务，用于提供状态依赖。
    :param cfg: 运行时配置。
    :param push_event: 异步事件推送回调。

    :return: 前台活动分类、应用名解析和睡眠状态类型均符合预期时返回 ``True``。

    副作用：
        更新感知服务的内存状态，并短暂等待后台前台事件任务完成。
    """

    try:
        awareness = AwarenessService(chat=chat, schedule=None, cfg=cfg, push_event=push_event)

        awareness.on_foreground({"process": "Code.exe", "title": "main.py - test", "fullscreen": False})
        activity = awareness._last_classified.activity if awareness._last_classified else None
        activity_ok = activity == "coding"

        # 以程序名作为应用上下文输入，验证当前感知链路的确定性字段解析。
        awareness.on_foreground({"process": "steam.exe", "title": "Steam", "fullscreen": False})
        app = awareness.current_app()
        app_ok = app == "Steam"

        sleep_state = awareness._sleep.current(current_time())
        sleep_ok = sleep_state is not None and isinstance(sleep_state.asleep, bool)

        # 等待前台事件启动的短任务收尾，避免自检结束时留下 pending task。
        await asyncio.sleep(0.05)

        ok = activity_ok and app_ok and sleep_ok
        _report("SELFTEST-AWARE", {
            "ok": ok, "activity": activity, "app": app,
            "asleep": sleep_state.asleep if sleep_state else None,
        })
        return ok
    except Exception as exc:
        _report("SELFTEST-AWARE", {"ok": False, "reason": str(exc)})
        return False
