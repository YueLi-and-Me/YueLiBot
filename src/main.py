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

import uvicorn

from src.core.agent.action import PresenceActionPolicy, TurnPlanner
from src.core.api.auth import token_manager
from src.core.common.backend_runtime import create_backend_runtime, runtime_file_path
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
from src.core.services.emoji import EmojiLibrary
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
    print_box('模型任务路由', rows, width=96)


def _announce_webui_entry(port: int, token: str, runtime_path: Path) -> None:
    """在启动早期用醒目的信息框输出 WebUI 地址与登录 token。

    :param port: 后端实际监听端口。
    :param token: 当前进程认证 token；每次启动重新生成。
    :param runtime_path: 运行时凭据文件路径，供用户事后再取一次 token。

    副作用：
        向标准输出写入包含 token 的启动信息框；应用不会主动把它写入 JSONL 或 WebUI
        日志流，但外部启动器仍可能记录标准输出。

    这里刻意用 ``print`` 而不是 logger，两个原因缺一不可：

    1. **token 不能进文件日志。** logger 会同时写 ``data/logs`` 下的 JSONL 并推给
       WebUI 日志流；而 token 的落盘位置 ``data/runtime/`` 由
       ``_restrict_runtime_directory()`` 限制了权限，日志目录没有。写进日志等于绕开
       那道限制，而日志文件长期留存、又经常被整份复制去排障。
    2. **stdout 在两种启动方式下都看得见。** supervisor 只吞掉 ``YUELI_`` 前缀的协议行
       （`supervisor.ts` 的 `_onLine`），其余 stdout 原样转发到 Electron 控制台。
       反过来，这个信息框**不能**加 ``YUELI_`` 前缀，否则会被当成协议行吃掉。

    token 不拼进 URL：URL 会进浏览器历史和 referrer，而 token 是当前进程的主凭据。
    """

    print_box(
        'YueLiBot · WebUI 入口',
        [
            f'WebUI 观察面板：http://127.0.0.1:{port}',
            f'登录 token：{token}',
            f'token 每次启动重新生成，也可从 {runtime_path} 读取',
            '状态：后端正在初始化，完成后会显示“WebUI 已就绪”',
        ],
        # 路径和 64 位 token 都需要保持在单行，启动时才能直接复制。
        width=112,
    )


def _announce_webui_ready(port: int, token: str) -> None:
    """在监听真正建立后再次给出短的 WebUI 就绪确认。"""

    print_box(
        'WebUI 已就绪',
        [
            f'地址：http://127.0.0.1:{port}',
            f'登录 token：{token}',
            '现在可以在浏览器中打开上面的地址',
        ],
        # 64 位 token 不能在确认框里折行，否则用户复制时容易漏字符。
        width=88,
    )


class _ReadyAnnouncingServer(uvicorn.Server):
    """在端口真正开始监听之后才打印就绪公告与 WebUI 状态框。"""

    def __init__(self, config: uvicorn.Config, port: int, token: str) -> None:
        """记录就绪公告所需的监听端口和认证 token。

        :param config: Uvicorn 配置。
        :param port: 后端实际监听端口。
        :param token: 当前进程认证 token。
        """

        super().__init__(config)
        self._entry_port = port
        self._entry_token = token

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        """完成 Uvicorn 启动后再输出就绪标记与 WebUI 状态框。

        :param sockets: 已绑定的监听 socket 列表；由 Uvicorn 传入。

        副作用：
            先执行父类启动流程，再向标准输出写入 ``YUELI_READY=1`` 与 WebUI 就绪框。

        启动早期已经输出带“正在初始化”状态的入口框；这里仅在监听真正建立后补上
        “WebUI 已就绪”确认，避免用户误把预告地址当成已经可访问的服务。
        """

        await super().startup(sockets=sockets)
        _announce_ready()
        _announce_webui_ready(self._entry_port, self._entry_token)


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
    # 端口已经被当前进程占住，先把入口放在启动日志顶部；真正监听后还会再打印一次
    # “WebUI 已就绪”框，避免用户把初始化中的地址误认为服务已经可访问。
    _announce_webui_entry(
        port,
        backend_runtime.token,
        runtime_file_path(data_dir),
    )

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
    emoji_library = EmojiLibrary(db, data_dir / 'emojis', embed_client)
    verified_emoji_count = emoji_library.verify_integrity()
    logger.info('emoji_library_ready', count=verified_emoji_count)

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

    async def _auto_register_emojis() -> None:
        """在聊天服务启动前用视觉模型登记表情包目录中的新增图片。"""

        summary = await emoji_library.auto_register_directory(image_describer)
        verified_count = emoji_library.verify_integrity()
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

    lifecycle.register(
        'emoji_auto_register',
        _auto_register_emojis,
        _stop_emoji_auto_register,
    )
    lifecycle.register('chat', app_state.chat.startup, app_state.chat.shutdown)
    lifecycle.register("awareness", awareness.startup, awareness.shutdown)

    logger.info("backend_starting", port=port)

    from src.core.api.app import create_app
    config = uvicorn.Config(create_app(), log_level="warning", access_log=False)
    _ReadyAnnouncingServer(
        config,
        port=port,
        token=backend_runtime.token,
    ).run(sockets=[sock])


if __name__ == "__main__":
    main()
