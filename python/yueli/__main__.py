"""
YueLiBot Python 后端入口。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
from pathlib import Path

import uvicorn

from yueli.common.logger import get_logger, initialize_logging
from yueli.config.loader import load_config


def _pick_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _announce_port(port: int) -> None:
    print(f"YUELI_PORT={port}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="YueLiBot Python backend")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--config-path", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()

    os.environ["YUELI_TOKEN"] = args.token
    os.environ["YUELI_DATA_DIR"] = args.data_dir

    cfg = load_config(Path(args.config_path))
    initialize_logging(cfg.advanced.log_level)
    logger = get_logger("main")

    if args.selftest:
        # ★ 自检必须用独立临时目录，绝不能碰 --data-dir 指向的真实 memory.db——
        #   REFLECT 检查会塞假消息触发摘要，写进真实库就是污染用户数据。
        from yueli.selftest import run_selftest
        rc = asyncio.run(run_selftest(cfg))
        sys.exit(rc)

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "memory.db"

    from yueli.services.trace import trace
    trace.configure(
        data_dir / "logs" / "trace.jsonl",
        record_content=cfg.advanced.trace_content,
        max_bytes=cfg.advanced.trace_max_bytes,
    )
    if cfg.advanced.trace_content:
        logger.warning("trace_content_enabled",
                       note="trace.jsonl 会明文记录对话正文与完整提示词，排查完请关掉")

    from yueli.common.db.connection import open_db
    from yueli.common.db.migrations.manager import run_migrations
    db = open_db(db_path)
    run_migrations(db, db_path)
    logger.info("db_ready", path=str(db_path))

    # 初始化 ChatService
    from yueli.api.state import app_state
    from yueli.api.ws import push
    from yueli.services.chat import ChatService

    from yueli.llm.openai import create_chat_provider, create_vision_provider

    provider = None
    try:
        provider = create_chat_provider(cfg)
        logger.info("llm_ready", model=provider.model)
    except Exception as exc:
        logger.warning("llm_init_failed", error=str(exc))

    vision_provider = None
    if cfg.vision.enabled:
        try:
            vision_provider = create_vision_provider(cfg)
            logger.info("vision_model_ready", model=vision_provider.model)
        except Exception as exc:
            logger.warning("vision_model_init_failed", error=str(exc))

    async def _push_event(channel: str, payload) -> None:
        await push(channel, payload)

    app_state.chat = ChatService(
        db=db,
        provider=provider,
        push_event=_push_event,
        cfg=cfg,
    )

    # 初始化 VectorService（可选，默认关）——必须在 app_state.chat 建好之后，
    # 它要用 chat.memory；直接赋 _vector 属性，和下面 TTS 的 _speak_audio
    # 是同一种「服务建好后挂到 chat 私有属性上」的写法。
    if cfg.vector.enabled:
        try:
            from yueli.memory.embed import build_client_from_config
            from yueli.services.vector import VectorService
            embed_client = build_client_from_config(cfg)
            app_state.chat._vector = VectorService(app_state.chat.memory, embed_client)
            logger.info("vector_recall_enabled", model=cfg.vector.embedding_model)
        except Exception as exc:
            logger.warning("vector_recall_init_failed", error=str(exc))

    # 初始化 TTS 服务（可选）
    if cfg.tts.ready:
        from yueli.services.tts import TtsService
        tts = TtsService(cfg, _push_event)
        app_state.chat._speak_audio = tts.speak
        app_state.tts = tts
        logger.info("tts_ready", model=cfg.tts.model)

    # 初始化 DayPlanService（可选；失败则 schedule 留 None，AwarenessService 退到 fallback 日程）
    schedule = None
    try:
        from yueli.schedule.plan import DayPlanService, ScheduleSleepState
        from yueli.persona.state import describe_persona

        class _LLMGenerator:
            async def generate(self, prompt: str) -> str:
                raw = ''
                async for chunk in provider.stream(
                    messages=[{'role': 'user', 'content': prompt}],
                    temperature=0.95, max_tokens=700,
                ):
                    if chunk.get('text'):
                        raw += chunk['text']
                return raw

        chat_svc = app_state.chat
        schedule = DayPlanService(
            store=chat_svc.memory,
            persona_description=lambda: describe_persona(chat_svc.persona.get()),
            interaction_density=lambda n: chat_svc.memory.interaction_density(n),
            anniversary_at=lambda: chat_svc.memory.first_seen_at,
            energy=lambda: chat_svc.persona.get().energy,
            last_interaction_at=lambda: chat_svc.memory.last_message_at(),
            generator=_LLMGenerator() if provider else None,
        )
        chat_svc.set_schedule(schedule)
        logger.info("schedule_service_ready")
    except Exception as exc:
        logger.warning("schedule_init_failed", error=str(exc))

    # 初始化 AwarenessService：吃前台事件、驱动睡眠状态、决定主动搭话。
    # startup/shutdown 注册进 lifecycle，真正的调用发生在 uvicorn 拉起循环之后
    # （api/app.py 的 FastAPI lifespan），这里只是登记。
    from yueli.services.lifecycle import lifecycle
    from yueli.services.proactive import AwarenessService
    awareness = AwarenessService(
        chat=app_state.chat,
        schedule=schedule,
        cfg=cfg,
        vision_provider=vision_provider,
    )
    app_state.awareness = awareness
    app_state.foreground_callback = awareness.on_foreground
    lifecycle.register("awareness", awareness.startup, awareness.shutdown)

    port = args.port or _pick_port()
    _announce_port(port)
    logger.info("backend_starting", port=port)

    from yueli.api.app import create_app
    app = create_app()

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
