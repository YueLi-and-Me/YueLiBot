"""启动 YueLiBot Python 后端并组装数据库、模型、平台和观察服务。

命令行参数决定运行时数据目录、配置文件和监听端口；入口负责先校验端口，再创建
数据库迁移、模型路由、对话服务、可选向量/TTS/日程服务，并将生命周期回调交给
FastAPI。实际 HTTP/WebSocket 路由由 ``src.core.api`` 提供。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List

import argparse
import asyncio
import os
import socket
import sys
import time

import uvicorn

from src.core.agent.action import PresenceActionPolicy, TurnPlanner
from src.core.api.auth import token_manager
from src.core.common.backend_runtime import create_backend_runtime, runtime_file_path
from src.core.common.clock import now as current_time
from src.core.common.console_layout import print_box
from src.core.common.logger import get_logger, initialize_logging
from src.core.config.loader import load_config
from src.core.config.schema import (
    BotDocument,
    Config,
    FeatureDocument,
    ModelCatalog,
    ProviderCatalog,
)
from src.core.config.upgrade import upgrade_config_directory
from src.core.llm_models.protocol import LlmProvider
from src.core.llm_models.snapshot import current_render_params
from src.core.observe import events as trace
from src.core.services.chat_image import ChatImageDescriber
from src.core.services.emoji import EmojiLibrary, VisionEmojiContentFilter
from src.core.prompts.registry import prompt_metadata


DEFAULT_BACKEND_PORT = 7999

# 初始化计时起点，由 main() 在解析完参数后置位。
#
# 只覆盖「解析参数之后到监听建立」这一段，不含解释器启动与模块导入的约 0.5 秒——
# 那一段既不在本进程的控制范围内，也无法通过改代码缩短。就绪框里因此写「初始化」
# 而不是「启动」，避免给出一个我们并没有测量的数字。
_init_started_at: float | None = None


class _LLMGenerator:
    """把明确注入的调度路由适配为 JSON 生成接口。"""

    def __init__(
        self,
        schedule_provider: LlmProvider,
        temperature: float,
        max_tokens: int | None,
        *,
        prompt_id: str,
        template_id: str,
    ) -> None:
        """绑定日程模型提供者及生成参数。

        :param schedule_provider: 已选中的日程模型提供者。
        :param temperature: 模型采样温度。
        :param max_tokens: 最大输出 token 数；``None`` 表示由提供者决定。
        """

        self._schedule_provider = schedule_provider
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._prompt_id = prompt_id
        self._template_id = template_id

    async def generate(
        self,
        prompt: str,
    ) -> str:
        """流式生成并拼接一份调度 JSON 文本。

        :param prompt: 已渲染的日程生成提示词。

        :return: 模型返回的非空正文。

        :raises ValueError: 模型只返回推理内容或空正文。
        :raises Exception: 提供者连接、协议或流式迭代错误直接传播。

        副作用：
            写入模型请求观察事件；不会持久化结果，持久化由业务服务负责。
        """

        raw = ''
        reasoning_length = 0
        messages = [{'role': 'user', 'content': prompt}]
        # 调度请求要求 JSON object，推理字段只计数用于诊断，不混入返回正文。
        trace.emit(
            'llm_request',
            messages=messages,
            temperature=self._temperature,
            maxTokens=self._max_tokens,
            renderParams=current_render_params(),
            **prompt_metadata(self._prompt_id, (self._template_id,)),
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
                f'{self._prompt_id} 模型未返回正文'
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


def _announce_model_routing(cfg: Config) -> None:
    """在启动早期展示各模型任务解析到的候选，让配置改动可见。

    配置是四份 TOML 加一层默认值，「我改的那行到底生效没有」以前只能翻日志逐条找。
    任务路由是其中最容易出错也最容易被误改的一层：模型改名、厂商引用错、新任务槽
    没配而静默继承 chat，这三种都不会报错，只会在运行时表现为「换了个模型说话」。

    :param cfg: 已完成交叉校验的配置对象。
    :return: 无返回值。
    副作用：向 stdout 打印信息框；不写日志文件（同 WebUI 入口框的口径，避免与
        结构化日志重复刷屏）。
    """

    rows: List[str] = []
    routing = cfg.routing
    for task in type(routing).model_fields:
        entry = getattr(routing, task)
        candidates = entry.candidates
        if not candidates:
            rows.append(f'{task:<11}未配置候选')
            continue
        first = candidates[0]
        extra = f'（+{len(candidates) - 1} 个备选）' if len(candidates) > 1 else ''
        rows.append(f'{task:<11}{first.identifier}  ·  {first.provider}{extra}')
    print_box('模型任务路由', rows, width=96, source=__name__)


def _announce_webui_ready(port: int, token: str, runtime_path: Path) -> None:
    """在监听真正建立后打印唯一一次 WebUI 入口框。

    只打一次：早先还有一个「正在初始化」的预告框，但它出现在启动日志顶部、
    地址那一刻还连不上，用户照着点只会失败，而真正可用的时刻另有一个框——
    同一份信息出现两次，先出现的那次还是错的。

    :param port: 后端实际监听端口。
    :param token: 当前进程认证 token；每次启动重新生成。
    :param runtime_path: 运行时凭据文件路径，供用户事后再取一次 token。

    副作用：
        向标准输出写入包含 token 的信息框，不进日志文件与 WebUI 日志流。
    """

    rows = [
        f'地址：http://127.0.0.1:{port}',
        f'登录 token：{token}',
        f'token 每次启动重新生成，也可从 {runtime_path} 读取',
    ]
    if _init_started_at is not None:
        rows.append(f'初始化耗时：{time.perf_counter() - _init_started_at:.2f} 秒')
    print_box(
        'WebUI 已就绪',
        rows,
        # 路径和 64 位 token 都要保持单行，用户才能直接复制。
        width=112,
        # 含当前进程主凭据，不进 WebUI 日志流与 JSONL。
        publish=False,
    )


class _ReadyAnnouncingServer(uvicorn.Server):
    """在端口真正开始监听之后才打印就绪公告与 WebUI 状态框。"""

    def __init__(
        self,
        config: uvicorn.Config,
        port: int,
        token: str,
        runtime_path: Path,
    ) -> None:
        """记录就绪公告所需的监听端口、认证 token 与凭据文件路径。

        :param config: Uvicorn 配置。
        :param port: 后端实际监听端口。
        :param token: 当前进程认证 token。
        :param runtime_path: 运行时凭据文件路径。
        """

        super().__init__(config)
        self._entry_port = port
        self._entry_token = token
        self._entry_runtime_path = runtime_path

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        """完成 Uvicorn 启动后再输出就绪标记与 WebUI 状态框。

        :param sockets: 已绑定的监听 socket 列表；由 Uvicorn 传入。

        副作用：
            先执行父类启动流程，再向标准输出写入 ``YUELI_READY=1`` 与 WebUI 就绪框。

        入口框只在这里打一次：地址在监听建立之前是打不开的，提前预告等于给出
        一个当时点了会失败的地址。
        """

        await super().startup(sockets=sockets)
        _announce_ready()
        _announce_webui_ready(
            self._entry_port, self._entry_token, self._entry_runtime_path)


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

    global _init_started_at
    _init_started_at = time.perf_counter()

    # 先把数据目录暴露给依赖环境变量的后端组件，再加载配置和日志。
    os.environ["YUELI_DATA_DIR"] = args.data_dir

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    # 配置对账必须在解析之前：新增字段先补进文件再读，用户才能在文件里看到它们，
    # 而不是只看到一个「代码里有默认值」的隐形开关。写入前整目录备份。
    upgrade_config_directory(
        Path(args.config_path),
        {
            'providers.toml': ProviderCatalog,
            'models.toml': ModelCatalog,
            'bot.toml': BotDocument,
            'features.toml': FeatureDocument,
        },
        data_dir,
    )
    cfg = load_config(Path(args.config_path))
    initialize_logging(cfg.log, data_dir / 'logs')
    logger = get_logger("main")
    # 启动日志的开场白：没有它时第一行是模型路由框，读者不知道这份输出从哪开始，
    # 也不知道正在起的是哪个 bot。
    logger.info('startup_begin', bot=cfg.bot.name, dataDir=str(data_dir))
    _announce_model_routing(cfg)

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

    from src.core.llm_models.snapshot import (
        configure as configure_snapshots,
        configure_exchanges,
    )
    configure_snapshots(
        data_dir / 'logs' / 'llm_request' if cfg.log.request_snapshots else None,
        cfg.log.max_snapshot_files,
    )
    configure_exchanges(
        data_dir / 'logs' / 'prompt' if cfg.log.prompt_records else None,
        cfg.log.max_prompt_records_per_task,
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

    app_state.config_dir = Path(args.config_path)
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

    # 表情包语义检索与事实召回复用同一个 embedding 客户端；表情包本身即使
    # 未配置 embedding 也可按标签包含匹配，不影响收侧识别和登记。
    embed_client = None
    if routers.embedding.ready:
        try:
            from src.core.memory.embed import build_client
            embed_client = build_client(routers.embedding)
        except Exception as exc:
            logger.warning('embedding_client_init_failed', error=str(exc))

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
    vision_task_enabled = cfg.vision.enabled or cfg.vision.chat_image_enabled
    if vision_task_enabled:
        vision_provider = routers.vision
        logger.info("vision_model_ready", model=vision_provider.model,
                    candidates=len(vision_provider.candidates))
    image_describer = ChatImageDescriber(cfg, vision_provider)
    # 内容过滤只在配置开启时装配（决定五：走既有 vision 槽，不开新槽）；
    # 过滤关闭时该对象为 None，入库路径零模型调用。
    emoji_content_filter = (
        VisionEmojiContentFilter(
            vision_provider,
            temperature=cfg.generation.vision.temperature,
            max_tokens=cfg.generation.vision.token_limit or 32,
        )
        if cfg.emoji.content_filtration
        else None
    )
    emoji_library = EmojiLibrary(
        db,
        data_dir / 'emojis',
        embed_client,
        config=cfg.emoji,
        content_filter=emoji_content_filter,
    )
    verified_emoji_count = emoji_library.verify_integrity()
    logger.info('emoji_library_ready', count=verified_emoji_count)
    app_state.emoji_library = emoji_library

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
        planner_provider=routers.planner if routers.planner.ready else None,
        replyer_provider=routers.replyer if routers.replyer.ready else None,
        scene_provider=routers.scene if routers.scene.ready else None,
        memory_provider=routers.memory if routers.memory.ready else None,
        image_describer=image_describer,
        emoji_library=emoji_library,
    )
    app_state.chat.set_action_policy(
        'group',
        TurnPlanner(PresenceActionPolicy(
            base_probability=cfg.group_chat.name_mention_probability,
            decay_strength=cfg.group_chat.presence_decay_strength,
            window_minutes=cfg.group_chat.reply_window_minutes,
            assistant_reply_count_since=app_state.chat.memory.assistant_reply_count_since,
            message_count_since=app_state.chat.memory.message_count_since,
        )),
    )

    # 向量服务依赖 ChatService 已创建的 MemoryStore，因此必须在聊天服务之后装配。
    if cfg.vector.enabled:
        try:
            from src.core.services.vector import VectorService
            if embed_client is None:
                raise ValueError('model_tasks.embedding.model_list 是空的，无法启用向量召回')
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

    from src.core.schedule.timeline import ActivityTimeline
    timeline = ActivityTimeline(db)

    # 日程服务是可选依赖；活动时间线即使没有日程模型也保持可读。
    schedule = None
    try:
        from src.core.schedule.plan import DayPlanService

        chat_svc = app_state.chat
        schedule_generation = cfg.generation.schedule
        schedule_generator = (
            _LLMGenerator(
                schedule_provider,
                schedule_generation.temperature,
                schedule_generation.token_limit,
                prompt_id='schedule',
                template_id='schedule',
            )
            if schedule_provider else None
        )
        activity_generator = (
            _LLMGenerator(
                schedule_provider,
                schedule_generation.temperature,
                schedule_generation.token_limit,
                prompt_id='activity.next',
                template_id='activity.next',
            )
            if schedule_provider else None
        )
        schedule = DayPlanService(
            db=db,
            timeline=timeline,
            store=chat_svc.memory,
            persona_state=lambda: chat_svc.persona.get(desktop_context.person.id),
            interaction_density=lambda n: chat_svc.memory.interaction_density(
                desktop_context.stream.id,
                n,
            ),
            anniversary_at=lambda: chat_svc.memory.first_seen_at(desktop_context.person.id),
            last_interaction_at=lambda: chat_svc.memory.last_message_at(desktop_context.stream.id),
            generator=schedule_generator,
            activity_generator=activity_generator,
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
        timeline=timeline,
        cfg=cfg,
        push_event=_push_event,
        sensor=sensor,
    )
    app_state.awareness = awareness
    app_state.foreground_callback = awareness.on_foreground

    async def _auto_register_emojis() -> None:
        """在聊天服务启动前用视觉模型登记表情包目录中的新增图片。

        只有真的登记了新图片才重新校验完整性：构造 EmojiLibrary 时已经全量
        校验过一遍，扫描没有新增时库的形态没变，再算一遍是把每个文件的
        SHA-256 白算第二次。真机 369 个文件（97 MB）的一次全量校验约 0.3 秒，
        占整个启动的可观份额。
        """

        summary = await emoji_library.auto_register_directory(image_describer)
        verified_count = (
            emoji_library.verify_integrity() if summary.added else verified_emoji_count
        )
        logger.info(
            'emoji_directory_scanned',
            discovered=summary.discovered,
            added=summary.added,
            skipped=summary.skipped,
            failed=summary.failed,
            verified=verified_count,
            directory=str(data_dir / 'emojis'),
        )

    async def _stop_emoji_auto_register() -> None:
        """投放目录扫描不持有后台资源，关闭阶段无需处理。"""

    emoji_maintenance_stop = asyncio.Event()
    emoji_maintenance_task: asyncio.Task[None] | None = None

    async def _emoji_maintenance_loop() -> None:
        """按配置节奏在后台检查库容量并清理孤儿文件。

        淘汰与清理都不进入收表情的入站热路径（决定三）：容量按
        check_interval_minutes 检查，孤儿文件按 cleanup.check_interval_hours
        检查，两次检查共用同一个等待循环。max_count 为 0 表示不设限；
        auto_evict 关闭时只告警不删除。
        """
        emoji_cfg = cfg.emoji
        now_ms = current_time()
        next_evict_ms = now_ms + emoji_cfg.check_interval_minutes * 60_000
        next_cleanup_ms = now_ms + int(emoji_cfg.cleanup.check_interval_hours * 3_600_000)
        while not emoji_maintenance_stop.is_set():
            now_ms = current_time()
            if emoji_cfg.max_count > 0 and now_ms >= next_evict_ms:
                count = emoji_library.stats()['count']
                if count > emoji_cfg.max_count:
                    if emoji_cfg.auto_evict:
                        evicted = emoji_library.evict_to_limit(emoji_cfg.max_count)
                        logger.info(
                            'emoji_maintenance_evicted',
                            evictedCount=len(evicted),
                            maxCount=emoji_cfg.max_count,
                        )
                    else:
                        logger.warning(
                            'emoji_over_limit',
                            count=count,
                            maxCount=emoji_cfg.max_count,
                        )
                next_evict_ms = now_ms + emoji_cfg.check_interval_minutes * 60_000
            if emoji_cfg.cleanup.enabled and now_ms >= next_cleanup_ms:
                emoji_library.cleanup_orphans(
                    emoji_cfg.cleanup.orphan_retention_days,
                )
                next_cleanup_ms = now_ms + int(
                    emoji_cfg.cleanup.check_interval_hours * 3_600_000,
                )
            wait_ms = max(min(next_evict_ms, next_cleanup_ms) - current_time(), 500)
            try:
                await asyncio.wait_for(
                    emoji_maintenance_stop.wait(),
                    timeout=wait_ms / 1000,
                )
            except TimeoutError:
                continue

    async def _emoji_maintenance() -> None:
        """把维护循环挂成后台任务并立即返回。

        - 现象：把无限循环本身注册为 startup 钩子时，启动会永久停在
          「服务正在启动 名称：emoji_maintenance」这一行。
        - 原因：生命周期逐个 await 各服务的 startup 直到返回，循环永不返回。
        - 后果：其后的 chat、感知等服务全部起不来，进程看似挂死。
        """

        nonlocal emoji_maintenance_task
        emoji_maintenance_stop.clear()
        emoji_maintenance_task = asyncio.create_task(
            _emoji_maintenance_loop(), name='emoji-maintenance')

    async def _stop_emoji_maintenance() -> None:
        """置位停止事件，等待维护循环在下一个等待点退出。"""

        emoji_maintenance_stop.set()
        if emoji_maintenance_task is not None:
            try:
                await emoji_maintenance_task
            except asyncio.CancelledError:
                pass

    lifecycle.register(
        'emoji_auto_register',
        _auto_register_emojis,
        _stop_emoji_auto_register,
    )
    # 高频词表是黑话召回打分的输入，首轮全量重建必须在首个回合前完成，
    # 因此排在 chat 之前注册。
    from src.core.services.jargon_stats import JargonStatsService
    jargon_stats = JargonStatsService(db)
    lifecycle.register('jargon_stats', jargon_stats.startup, jargon_stats.shutdown)
    # 黑话学习走自己的游标旁路积累证据与推断词条，不进回合路径；挨着
    # jargon_stats 注册，两者共同构成黑话的「用」与「学」两侧。
    from src.core.services.jargon_learn import JargonLearnService
    if routers.memory.ready:
        jargon_learn = JargonLearnService(
            db,
            app_state.chat.memory,
            routers.memory,
            temperature=cfg.generation.memory.temperature,
            max_tokens=cfg.generation.memory.token_limit,
            bot_name=cfg.bot.name,
            bot_names=(cfg.bot.name, *cfg.bot.aliases, cfg.bot.user_nickname),
        )
        lifecycle.register('jargon_learn', jargon_learn.startup, jargon_learn.shutdown)
    else:
        # 没有 memory 路由时学习整条功能是关的，这句必须在启动时说出来：
        # 静默关掉在外部看来与「正常但这段对话没什么可学的」完全一样。
        logger.warning('jargon_learn_disabled', reason='memory 模型路由不可用')
    lifecycle.register(
        'emoji_maintenance',
        _emoji_maintenance,
        _stop_emoji_maintenance,
    )
    lifecycle.register('chat', app_state.chat.startup, app_state.chat.shutdown)
    lifecycle.register("awareness", awareness.startup, awareness.shutdown)

    # 配置热重载的持有方更新：第 1 类字段的使用点都通过下面这些引用读取，
    # 重载成功后统一换到新对象即生效（第 2/3 类的分类见 loader 的前缀表与
    # 交付报告）。这里写私有属性是权宜——chat.py 等归记忆线，等它空出再改
    # 造成公开 setter，届时本回调只换调用形式、语义不变。
    def _on_config_reloaded(previous: object, fresh: object) -> None:
        """把装配期创建的服务切到新配置对象上。

        :param previous: 旧配置（回调签名要求，本回调不使用）。
        :param fresh: 重载后的新配置。
        副作用：原地重绑各持有方的配置引用，不重建任何服务。
        """
        app_state.group_chat_config = fresh.group_chat
        app_state.chat._cfg = fresh
        app_state.chat._image_describer._cfg = fresh
        if app_state.tts is not None:
            app_state.tts._cfg = fresh
        app_state.awareness._cfg = fresh
        if schedule is not None:
            schedule._config = fresh.schedule
        sensor._cfg = fresh
        if getattr(sensor, '_vision', None) is not None:
            sensor._vision._cfg = fresh

    from src.core.config.loader import add_config_reload_listener
    add_config_reload_listener(_on_config_reloaded)

    logger.info("backend_starting", port=port)

    from src.core.api.app import create_app
    config = uvicorn.Config(create_app(), log_level="warning", access_log=False)
    _ReadyAnnouncingServer(
        config,
        port=port,
        token=backend_runtime.token,
        runtime_path=runtime_file_path(data_dir),
    ).run(sockets=[sock])


if __name__ == "__main__":
    main()
