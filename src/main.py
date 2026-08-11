"""
YueLiBot Python 后端入口。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import argparse
import asyncio
import os
import socket
import sys

import uvicorn

from src.api.auth import token_manager
from src.common.backend_runtime import create_backend_runtime
from src.common.logger import get_logger, initialize_logging
from src.config.loader import load_config
from src.llm_models.protocol import LlmProvider
from src.observe import events as trace
from src.prompts.registry import prompt_metadata


DEFAULT_BACKEND_PORT = 7999


class _LLMGenerator:
    """把明确注入的日程路由适配为 DayPlanService 需要的生成接口。"""

    def __init__(
        self,
        schedule_provider: LlmProvider,
        temperature: float,
        max_tokens: int | None,
    ) -> None:
        self._schedule_provider = schedule_provider
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def generate(self, prompt: str) -> str:
        raw = ''
        reasoning_length = 0
        messages = [{'role': 'user', 'content': prompt}]
        trace.emit(
            'llm_request',
            messages=messages,
            temperature=self._temperature,
            maxTokens=self._max_tokens,
            **prompt_metadata('schedule', ('schedule',)),
        )
        async for chunk in self._schedule_provider.stream(
            messages=messages,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            response_format={'type': 'json_object'},
        ):
            text = chunk.get('text')
            if isinstance(text, str):
                raw += text
            reasoning = chunk.get('reasoning')
            if isinstance(reasoning, str):
                reasoning_length += len(reasoning)
        if not raw.strip():
            raise ValueError(
                '日程模型未返回正文'
                f'（正文字符={len(raw)}，推理字符={reasoning_length}）'
            )
        return raw


def _bind_backend_socket(port: int) -> socket.socket:
    """绑定并占住后端端口，端口已被占用时给出可执行的排查提示。"""
    # 要一直持有这个 socket，探完就放会被别人占走。
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", port))
    except OSError as exc:
        sock.close()
        if exc.errno in {98, 10048}:
            raise OSError(
                f'后端端口 {port} 已被占用，Bot 无法启动。'
                f'请运行 Get-NetTCPConnection -LocalPort {port} '
                f'查看占用进程，结束冲突进程后重试；'
                f'如需临时改用其他端口，可传入 --port <端口>。'
            ) from exc
        raise
    return sock


def _announce_port(port: int) -> None:
    print(f"YUELI_PORT={port}", flush=True)


def _announce_token(token: str) -> None:
    print(f"YUELI_TOKEN={token}", flush=True)


def _announce_ready() -> None:
    print("YUELI_READY=1", flush=True)


class _ReadyAnnouncingServer(uvicorn.Server):
    """在端口真正开始监听之后才打印就绪公告。"""

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        _announce_ready()


def main() -> None:
    parser = argparse.ArgumentParser(description="YueLiBot Python backend")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--config-path", required=True)
    parser.add_argument("--port", type=int, default=DEFAULT_BACKEND_PORT)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()

    os.environ["YUELI_DATA_DIR"] = args.data_dir

    cfg = load_config(Path(args.config_path))
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    initialize_logging(cfg.log, data_dir / 'logs')
    logger = get_logger("main")

    from src.prompts.registry import configure_prompts
    configure_prompts(data_dir)

    if args.selftest:
        # ★ 自检必须用独立临时目录，绝不能碰 --data-dir 指向的真实 memory.db——
        #   REFLECT 检查会塞假消息触发摘要，写进真实库就是污染用户数据。
        from src.selftest import run_selftest
        rc = asyncio.run(run_selftest(cfg))
        sys.exit(rc)

    db_path = data_dir / "memory.db"

    # 先占住端口再落盘连接信息。端口冲突时不得写出看似可用的新 token，
    # 更不能打印端口或就绪公告。
    sock = _bind_backend_socket(args.port)
    port = sock.getsockname()[1]
    backend_runtime = create_backend_runtime(data_dir, port)
    token_manager.configure(backend_runtime.token)
    _announce_port(port)
    _announce_token(backend_runtime.token)

    from src.llm_models.snapshot import configure as configure_snapshots
    configure_snapshots(
        data_dir / 'logs' / 'llm_request' if cfg.log.request_snapshots else None,
        cfg.log.max_snapshot_files,
    )

    from src.common.db.connection import open_db
    from src.common.db.migrations.manager import run_migrations
    from src.observe.store import configure as configure_event_store
    from src.platform_io.broker import PlatformBroker
    from src.platform_io.drivers.qq_ws import QqWebSocketDriver
    from src.platform_io.registry import StreamRegistry
    from src.platform_io.types import StreamRef
    db = open_db(db_path)
    run_migrations(db, db_path)
    configure_event_store(
        db_path,
        retention_count=cfg.log.event_retention_count,
        retention_hours=cfg.log.event_retention_hours,
    )
    logger.info("db_ready", path=str(db_path))

    # 初始化 ChatService
    from src.api.state import app_state
    from src.api.ws import push
    from src.services.chat import ChatService

    app_state.registry = StreamRegistry(db)
    app_state.group_chat_config = cfg.group_chat
    broker = PlatformBroker()
    qq_driver = QqWebSocketDriver(push)

    def _register_platform_stream(stream: StreamRef) -> None:
        if stream.platform != qq_driver.platform:
            return
        if not broker.has_driver(stream.id):
            broker.register(stream.id, qq_driver)

    app_state.broker = broker
    app_state.register_platform_stream = _register_platform_stream
    desktop_context = app_state.registry.desktop_context()
    logger.info(
        "stream_registry_ready",
        owner_person_id=desktop_context.person.id,
        desktop_stream_id=desktop_context.stream.id,
    )

    # 八个任务各自的候选序列。厂商挂了在这一层换下一条连接，业务侧无感。
    from src.llm_models.router import create_routers
    routers = create_routers(cfg)
    app_state.routers = routers

    chat_provider = routers.chat if routers.chat.ready else None
    proactive_provider = routers.proactive if routers.proactive.ready else None
    summary_provider = routers.summary if routers.summary.ready else None
    schedule_provider = routers.schedule if routers.schedule.ready else None
    if chat_provider:
        logger.info("llm_ready", model=chat_provider.model,
                    candidates=len(chat_provider.candidates), strategy=cfg.routing.chat.strategy)
    else:
        logger.warning("llm_init_failed",
                       error="model_tasks.chat.model_list 是空的，她这轮说不出话")

    vision_provider = None
    if cfg.vision.enabled:
        vision_provider = routers.vision
        logger.info("vision_model_ready", model=vision_provider.model,
                    candidates=len(vision_provider.candidates))

    async def _push_event(
        channel: str,
        payload: Any,
        stream_id: int = desktop_context.stream.id,
    ) -> int:
        return await push(stream_id, channel, payload)

    app_state.chat = ChatService(
        db=db,
        chat_provider=chat_provider,
        proactive_provider=proactive_provider,
        summary_provider=summary_provider,
        push_event=_push_event,
        cfg=cfg,
        broker=broker,
        expression_provider=routers.expression if routers.expression.ready else None,
    )

    # 初始化 VectorService（可选，默认关）——必须在 app_state.chat 建好之后，
    # 它要用 chat.memory；直接赋 _vector 属性，和下面 TTS 的 _speak_audio
    # 是同一种「服务建好后挂到 chat 私有属性上」的写法。
    if cfg.vector.enabled:
        try:
            from src.memory.embed import build_client
            from src.services.vector import VectorService
            embed_client = build_client(routers.embedding)
            app_state.chat._vector = VectorService(app_state.chat.memory, embed_client)
            logger.info("vector_recall_enabled", model=routers.embedding.model,
                        candidates=len(routers.embedding.candidates))
        except Exception as exc:
            logger.warning("vector_recall_init_failed", error=str(exc))

    # 初始化 TTS 服务（可选）
    if cfg.tts.enabled and routers.tts.ready:
        from src.services.tts import TtsService
        tts = TtsService(cfg, _push_event, routers.tts)
        app_state.chat._speak_audio = tts.speak
        app_state.chat._cancel_audio = tts.cancel
        app_state.tts = tts
        logger.info("tts_ready", voice=cfg.tts.voice,
                    candidates=len(routers.tts.candidates))

    # 初始化 DayPlanService（可选；失败则 schedule 留 None，AwarenessService 退到 fallback 日程）
    schedule = None
    try:
        from src.schedule.plan import DayPlanService, ScheduleSleepState
        from src.persona.state import describe_persona

        chat_svc = app_state.chat
        schedule_generation = cfg.generation.schedule
        schedule_generator = (
            _LLMGenerator(
                schedule_provider,
                schedule_generation.temperature,
                schedule_generation.token_limit,
            )
            if schedule_provider else None
        )
        schedule = DayPlanService(
            store=chat_svc.memory,
            persona_description=lambda: describe_persona(
                chat_svc.persona.get(desktop_context.person.id)
            ),
            interaction_density=lambda n: chat_svc.memory.interaction_density(
                desktop_context.stream.id,
                n,
            ),
            anniversary_at=lambda: chat_svc.memory.first_seen_at(desktop_context.person.id),
            energy=lambda: chat_svc.persona.get(desktop_context.person.id).energy,
            last_interaction_at=lambda: chat_svc.memory.last_message_at(desktop_context.stream.id),
            generator=schedule_generator,
            character_name=cfg.bot.name,
            character_personality=cfg.personality.personality,
            schedule_config=cfg.schedule,
        )
        chat_svc.set_schedule(schedule)
        logger.info("schedule_service_ready")
    except Exception as exc:
        logger.warning("schedule_init_failed", error=str(exc))

    # 初始化 AwarenessService：吃前台事件、驱动睡眠状态、决定主动搭话。
    # startup/shutdown 注册进 lifecycle，真正的调用发生在 uvicorn 拉起循环之后
    # （api/app.py 的 FastAPI lifespan），这里只是登记。
    from src.services.lifecycle import lifecycle
    from src.services.proactive import AwarenessService
    awareness = AwarenessService(
        chat=app_state.chat,
        schedule=schedule,
        cfg=cfg,
        push_event=_push_event,
        vision_provider=vision_provider,
    )
    app_state.awareness = awareness
    app_state.foreground_callback = awareness.on_foreground
    lifecycle.register("awareness", awareness.startup, awareness.shutdown)

    logger.info("backend_starting", port=port)

    from src.api.app import create_app
    config = uvicorn.Config(create_app(), log_level="warning", access_log=False)
    _ReadyAnnouncingServer(config).run(sockets=[sock])


if __name__ == "__main__":
    main()
