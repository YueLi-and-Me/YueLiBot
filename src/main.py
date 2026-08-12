"""启动 YueLiBot Python 后端并组装数据库、模型、平台和观察服务。

命令行参数决定运行时数据目录、配置文件和监听端口；入口负责先校验端口，再创建
数据库迁移、模型路由、对话服务、可选向量/TTS/日程服务，并将生命周期回调交给
FastAPI。实际 HTTP/WebSocket 路由由 ``src.core.api`` 提供。
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

from src.core.agent.action import PresenceActionPolicy
from src.core.api.auth import token_manager
from src.core.common.backend_runtime import create_backend_runtime
from src.core.common.logger import get_logger, initialize_logging
from src.core.config.loader import load_config
from src.core.llm_models.protocol import LlmProvider
from src.core.llm_models.snapshot import current_render_params
from src.core.observe import events as trace
from src.core.prompts.registry import prompt_metadata


DEFAULT_BACKEND_PORT = 7999


class _LLMGenerator:
    """把明确注入的日程路由适配为 DayPlanService 需要的生成接口。"""

    def __init__(
        self,
        schedule_provider: LlmProvider,
        temperature: float,
        max_tokens: int | None,
    ) -> None:
        """绑定日程模型提供者及生成参数。

        :param schedule_provider: 已选中的日程模型提供者。
        :param temperature: 模型采样温度。
        :param max_tokens: 最大输出 token 数；``None`` 表示由提供者决定。
        """

        self._schedule_provider = schedule_provider
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def generate(
        self,
        prompt: str,
    ) -> str:
        """流式生成并拼接一份日程 JSON 文本。

        :param prompt: 已渲染的日程生成提示词。

        :return: 模型返回的非空正文。

        :raises ValueError: 模型只返回推理内容或空正文。
        :raises Exception: 提供者连接、协议或流式迭代错误直接传播。

        副作用：
            写入模型请求观察事件；不会持久化日程，持久化由日程服务负责。
        """

        raw = ''
        reasoning_length = 0
        messages = [{'role': 'user', 'content': prompt}]
        # 日程请求要求 JSON object，推理字段只计数用于诊断，不混入返回正文。
        trace.emit(
            'llm_request',
            messages=messages,
            temperature=self._temperature,
            maxTokens=self._max_tokens,
            renderParams=current_render_params(),
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
        # 空正文通常表示 provider 只返回推理或响应协议不匹配，必须显式暴露。
        if not raw.strip():
            raise ValueError(
                '日程模型未返回正文'
                f'（正文字符={len(raw)}，推理字符={reasoning_length}）'
            )
        return raw


def _bind_backend_socket(port: int) -> socket.socket:
    """绑定并持有本地后端端口，避免探测完成后被其他进程抢占。

    :param port: 监听端口，范围为 ``1`` 到 ``65535``。

    :return: 已绑定到 ``127.0.0.1`` 的 TCP socket；调用方负责在服务器接管后管理其生命周期。

    :raises OSError: 端口被占用时抛出带排查提示的异常，其他绑定失败传播原始异常。

    副作用：
        成功时占用本地端口；绑定失败时关闭临时 socket。
    """
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
    """向父进程以固定键值格式输出实际监听端口。

    :param port: 后端实际绑定的端口。

    副作用：
        向标准输出写入并立即刷新 ``YUELI_PORT=<port>``。
    """

    print(f"YUELI_PORT={port}", flush=True)


def _announce_token(token: str) -> None:
    """向父进程以固定键值格式输出当前后端认证令牌。

    :param token: 当前进程认证 token。

    副作用：
        向标准输出写入并立即刷新 ``YUELI_TOKEN=<token>``；调用方必须确保输出通道受信任。
    """

    print(f"YUELI_TOKEN={token}", flush=True)


def _announce_ready() -> None:
    """向父进程输出后端已完成监听初始化的就绪标记。

    副作用：
        向标准输出写入并立即刷新 ``YUELI_READY=1``。
    """

    print("YUELI_READY=1", flush=True)


class _ReadyAnnouncingServer(uvicorn.Server):
    """在端口真正开始监听之后才打印就绪公告。"""

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        """完成 Uvicorn 启动后再输出就绪标记。

        :param sockets: 已绑定的监听 socket 列表；由 Uvicorn 传入。

        副作用：
            先执行父类启动流程，再向标准输出写入 ``YUELI_READY=1``。
        """

        await super().startup(sockets=sockets)
        _announce_ready()


def main() -> None:
    """解析命令行参数并启动后端进程。

    :return: ``None``；服务由 Uvicorn 事件循环持续运行。

    副作用：
        创建运行时目录、数据库和日志，初始化模型与可选服务，绑定本地端口并启动
        FastAPI。使用 ``--selftest`` 时改为在隔离临时目录执行自检并通过进程退出码
        返回结果。

    :raises OSError: 端口占用、目录创建或数据库初始化失败。
    :raises Exception: 配置读取、服务装配或 Uvicorn 启动错误直接传播。
    """

    parser = argparse.ArgumentParser(description="YueLiBot Python backend")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--config-path", required=True)
    parser.add_argument("--port", type=int, default=DEFAULT_BACKEND_PORT)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()

    # 先把数据目录暴露给依赖环境变量的后端组件，再加载配置和日志。
    os.environ["YUELI_DATA_DIR"] = args.data_dir

    cfg = load_config(Path(args.config_path))
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    initialize_logging(cfg.log, data_dir / 'logs')
    logger = get_logger("main")

    from src.core.prompts.registry import configure_prompts
    configure_prompts(data_dir)

    if args.selftest:
        # 自检会写入测试消息和摘要，必须在自身的临时数据库中运行，不能污染运行数据。
        from src.selftest import run_selftest
        rc = asyncio.run(run_selftest(cfg))
        sys.exit(rc)

    db_path = data_dir / "memory.db"

    # 先占住端口再生成并公告运行时凭证，避免端口冲突时留下看似可用的连接信息。
    sock = _bind_backend_socket(args.port)
    port = sock.getsockname()[1]
    backend_runtime = create_backend_runtime(data_dir, port)
    token_manager.configure(backend_runtime.token)
    _announce_port(port)
    _announce_token(backend_runtime.token)

    from src.core.llm_models.snapshot import configure as configure_snapshots
    configure_snapshots(
        data_dir / 'logs' / 'llm_request' if cfg.log.request_snapshots else None,
        cfg.log.max_snapshot_files,
    )

    from src.core.common.db.connection import open_db
    from src.core.common.db.migrations.manager import run_migrations
    from src.core.observe.store import configure as configure_event_store
    from src.core.platform_io.broker import PlatformBroker
    from src.core.platform_io.drivers.qq_ws import QqWebSocketDriver
    from src.core.platform_io.registry import StreamRegistry
    from src.core.platform_io.types import StreamRef
    db = open_db(db_path)
    run_migrations(db, db_path)
    configure_event_store(
        db_path,
        retention_count=cfg.log.event_retention_count,
        retention_hours=cfg.log.event_retention_hours,
    )
    logger.info("db_ready", path=str(db_path))

    # 先装配归属注册表和 broker，随后创建的聊天服务才能解析并投递外部 stream。
    from src.core.api.state import app_state
    from src.core.api.ws import push
    from src.core.services.chat import ChatService

    app_state.registry = StreamRegistry(db)
    app_state.group_chat_config = cfg.group_chat
    broker = PlatformBroker()
    qq_driver = QqWebSocketDriver(push)

    def _register_platform_stream(stream: StreamRef) -> None:
        """为 QQ stream 注册唯一的 WebSocket 出站驱动。

        :param stream: 已由注册表解析的 stream 引用。

        副作用：
            当 stream 属于 QQ 平台且尚未注册驱动时，向平台 broker 写入该 stream 的
            驱动绑定；重复调用不会重复注册。
        """

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

    # 路由器为各模型任务维护候选序列，业务服务只接收已经选择好的 provider。
    from src.core.llm_models.router import create_routers
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
                       error="model_tasks.chat.model_list 是空的，Bot 这轮无法回复")

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
        """将服务事件转发到指定 stream 的 WebSocket 订阅者。

        :param channel: 事件频道名称。
        :param payload: 事件载荷。
        :param stream_id: 目标 stream ID，默认使用 desktop stream。

        :return: 实际收到事件的订阅者数量。
        """

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
    app_state.chat.set_action_policy(
        'group',
        PresenceActionPolicy(
            base_probability=cfg.group_chat.name_mention_probability,
            decay_strength=cfg.group_chat.presence_decay_strength,
            window_minutes=cfg.group_chat.reply_window_minutes,
            assistant_reply_count_since=app_state.chat.memory.assistant_reply_count_since,
            message_count_since=app_state.chat.memory.message_count_since,
        ),
    )

    # 向量服务依赖 ChatService 已创建的 MemoryStore，因此必须在聊天服务之后装配。
    if cfg.vector.enabled:
        try:
            from src.core.memory.embed import build_client
            from src.core.services.vector import VectorService
            embed_client = build_client(routers.embedding)
            app_state.chat._vector = VectorService(app_state.chat.memory, embed_client)
            logger.info("vector_recall_enabled", model=routers.embedding.model,
                        candidates=len(routers.embedding.candidates))
        except Exception as exc:
            logger.warning("vector_recall_init_failed", error=str(exc))

    # TTS 只在配置启用且至少有一个可用候选时装配，避免创建永远失败的后台任务。
    if cfg.tts.enabled and routers.tts.ready:
        from src.core.services.tts import TtsService
        tts = TtsService(cfg, _push_event, routers.tts)
        app_state.chat._speak_audio = tts.speak
        app_state.chat._cancel_audio = tts.cancel
        app_state.tts = tts
        logger.info("tts_ready", voice=cfg.tts.voice,
                    candidates=len(routers.tts.candidates))

    # 日程服务是可选依赖；未成功装配时由 AwarenessService 使用配置备用日程。
    schedule = None
    try:
        from src.core.schedule.plan import DayPlanService, ScheduleSleepState
        from src.core.persona.state import describe_persona

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

    # 主动感知依赖聊天、日程和视觉服务；这里只登记生命周期回调，实际启动在
    # FastAPI lifespan 的事件循环中执行。
    from src.core.services.lifecycle import lifecycle
    from src.core.services.proactive import AwarenessService
    from src.desktop.sensor import DesktopSensor
    sensor = DesktopSensor(cfg, _push_event, vision_provider)
    awareness = AwarenessService(
        chat=app_state.chat,
        schedule=schedule,
        cfg=cfg,
        push_event=_push_event,
        sensor=sensor,
    )
    app_state.awareness = awareness
    app_state.foreground_callback = awareness.on_foreground
    lifecycle.register("awareness", awareness.startup, awareness.shutdown)

    logger.info("backend_starting", port=port)

    from src.core.api.app import create_app
    config = uvicorn.Config(create_app(), log_level="warning", access_log=False)
    _ReadyAnnouncingServer(config).run(sockets=[sock])


if __name__ == "__main__":
    main()
