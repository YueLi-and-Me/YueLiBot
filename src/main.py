"""启动 YueLiBot Python 后端并组装数据库、模型、平台和观察服务。

命令行参数决定运行时数据目录、配置文件和监听端口；入口负责先校验端口，再创建
数据库迁移、模型路由、对话服务、可选向量/TTS/日程服务，并将生命周期回调交给
FastAPI。实际 HTTP/WebSocket 路由由 ``src.core.api`` 提供。

本模块同时是整个应用的进程入口：监听建立之后按配置拉起 QQ 适配器与 Electron
桌面外壳（``[desktop_pet] enabled`` 为 false 时不拉外壳，进程保持无头形态），
退出时按相反顺序收走它们。子进程行为见 ``src.core.common.child_process``。
"""

from __future__ import annotations

from pathlib import Path
from types import FrameType
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
from src.core.api.state import app_state
from src.core.common.backend_runtime import create_backend_runtime, runtime_file_path
from src.core.common.child_process import ChildProcess
from src.core.common.clock import now as current_time
from src.core.common.console_layout import print_box
from src.core.common.logger import get_logger, initialize_logging
from src.core.common.self_check import announce_startup_self_check
from src.core.config.bootstrap import (
    MAIN_CONFIG_FILES,
    bootstrap_config_directory,
    missing_startup_requirements,
)
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
from src.core.services.adapter_host import build_adapter_process
from src.core.services.chat_image import ChatImageDescriber
from src.core.services.desktop_shell import build_desktop_shell_process
from src.core.services.emoji import EmojiLibrary, VisionEmojiContentFilter
from src.core.prompts.registry import prompt_metadata


DEFAULT_BACKEND_PORT = 7999

# 仓库根目录：``src/main.py`` 的上两层。子进程的工作目录与外壳的应用目录都以它为准，
# 不用 os.getcwd()——入口反转后进程可能从任意目录启动，用当前工作目录会解析到别处。
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 停止单个子进程的等待秒数，超时后强制终止。适配器与外壳都没有需要落盘的状态，
# 这里只需覆盖「进程收到终止信号到真正消失」的时间。
CHILD_STOP_GRACE_SECONDS = 5.0

logger = get_logger('main')

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
    # 该 socket 需持续持有：探测后立即释放会被其他进程抢占。
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", port))
    except OSError as exc:
        sock.close()
        if exc.errno in {98, 10048}:
            # 排查命令按平台给：无头部署跑在 Linux 上，给一条 PowerShell 命令
            # 等于没给。
            inspect = (
                f'Get-NetTCPConnection -LocalPort {port}' if sys.platform == 'win32'
                else f'ss -lptn "sport = :{port}"'
            )
            raise OSError(
                f'后端端口 {port} 已被占用，Bot 无法启动。'
                f'请运行 {inspect} '
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

    配置是四份 TOML 加一层默认值，配置改动是否生效此前只能翻日志逐条确认。
    任务路由是其中最容易出错也最容易被误改的一层：模型改名、厂商引用错、新任务槽
    没配而静默继承 chat，这三种都不会报错，只会在运行时表现为「换了个模型说话」。

    :param cfg: 已完成交叉校验的配置对象。
    :return: 无返回值。
    副作用：向 stdout 打印信息框；不写日志文件（同 WebUI 入口框的口径，避免与
        结构化日志重复输出占满控制台）。
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


def _bootstrap_config(config_dir: Path) -> bool:
    """补齐缺失的配置文件，并判断本次是否属于「刚生成、还没填」的首次安装。

    :param config_dir: 主体配置目录。
    :return: 本次创建了主体配置文件时为 ``True``，调用方应当停止启动。
    :raises KeyError: schema 新增了必填字段但初始配置模块没有给出初值。
    :raises OSError: 配置目录或文件无法写入。
    副作用：创建缺失的配置文件；首次安装时向标准输出打印待填清单。
    """
    created = bootstrap_config_directory(config_dir)
    if not created:
        return False
    # 只补出了适配器那两份文件时不算首次安装：QQ 是可选组件，它的连接配置默认
    # 停用，主体照常能跑。停下来只会拦住一个本来就不接 QQ 的部署。
    main_created = [path for path in created if path.name in MAIN_CONFIG_FILES]
    if not main_created:
        return False
    rows = [f'配置目录：{config_dir.resolve()}', '']
    # 配置目录内的文件只写文件名：绝对路径逐行重复一遍会把框撑到换行，反而看不清
    # 到底生成了哪几份。适配器连接配置不在配置目录里，仍给完整路径。
    rows.extend(f'已生成：{_display_path(path, config_dir)}' for path in created)
    rows.append('')
    rows.extend(f'待填写：{item}' for item in missing_startup_requirements(config_dir))
    rows.append('')
    rows.append('填好上面这些之后重新启动；其余选项都有默认值，可以先不动。')
    print_box('首次启动 · 配置已生成', rows, width=112, publish=False)
    return True


def _display_path(path: Path, config_dir: Path) -> str:
    """把配置目录内的文件缩写为文件名，目录外的保留完整路径。

    :param path: 待展示的文件路径。
    :param config_dir: 主体配置目录。
    :return: 供信息框展示的路径文本。
    """
    try:
        return str(path.resolve().relative_to(config_dir.resolve()))
    except ValueError:
        return str(path.resolve())


def _announce_unfilled_config(config_dir: Path) -> None:
    """在配置加载失败时，补一条「哪些东西还没填」的人话提示。

    :param config_dir: 主体配置目录。
    :return: ``None``；没有可报告的缺项时不输出。
    副作用：向标准输出打印待填清单；不抑制调用方要抛出的原始异常。
    """
    try:
        missing = missing_startup_requirements(config_dir)
    except Exception:
        # 连读都读不了时说明是更靠前的问题（文件缺失、TOML 语法错），原始异常
        # 已经说清楚了，这里不必再叠一层。
        return
    if not missing:
        return
    rows = [f'配置目录：{config_dir.resolve()}', '']
    rows.extend(f'待填写：{item}' for item in missing)
    rows.append('')
    rows.append('上面的报错是同一件事的结构化形式。')
    print_box('配置还没填完', rows, width=112, publish=False)


def _build_children(
    cfg: Config,
    config_dir: Path,
    data_dir: Path,
    launch_shell: bool,
) -> List[ChildProcess]:
    """按配置组装本进程要监护的子进程列表。

    两个组件都是可选的，缺失方式不同：适配器缺配置属于「这台机器不接 QQ」，只告警；
    外壳解析失败属于「用户开着桌宠却起不来」，打 error 但同样不阻断后端启动——
    为一个界面组件让 QQ 和 WebUI 一起停掉不成比例。

    :param cfg: 已完成交叉校验的配置对象。
    :param config_dir: 主体配置目录。
    :param data_dir: 运行时数据目录。
    :param launch_shell: 是否允许拉起桌面外壳；``--no-shell`` 时为 ``False``。
    :return: 按启动顺序排列的子进程列表，可能为空。
    副作用：读取适配器声明与连接配置，并输出说明本次拉起了什么的日志。
    """
    children: List[ChildProcess] = []
    adapter = build_adapter_process(PROJECT_ROOT, config_dir, data_dir)
    if adapter is not None:
        children.append(adapter)

    if not launch_shell:
        # 开发时两个终端各跑一边（一边 python bot.py，一边 npm run dev）是既有工作流，
        # 此时外壳已经在别处运行，再拉一个只会出现两个桌宠。
        logger.info('desktop_shell_suppressed', reason='--no-shell')
        return children
    try:
        shell = build_desktop_shell_process(
            cfg.desktop_pet.enabled, PROJECT_ROOT, data_dir, config_dir)
    except RuntimeError as exc:
        logger.error('desktop_shell_unavailable', error=str(exc))
        return children
    if shell is not None:
        children.append(shell)
    return children


def _reexec_process() -> None:
    """用同一份命令行重新执行本进程，实现 ``/system/restart``。

    入口反转之前重启由 Electron 的监护器完成：Python 退出后它再拉一个。现在没有
    外部监护者，重启只能由本进程接手。

    - 现象：Windows 上 ``os.execv`` 之后终端会立刻回到提示符，而新进程仍在往同一个
      控制台输出。
    - 原因：Windows 的 exec 语义是「结束当前进程、另起一个新进程」，进程 ID 不保留，
      等待原进程的 shell 因此认为命令已经结束。
    - 后果：这只影响终端观感，新进程的监听、子进程与日志都正常；换成先 spawn 再退出
      也是同一个结果，不值得为此引入一个常驻的父进程。

    :return: 不返回；调用成功后当前进程映像被替换。
    :raises OSError: 解释器路径不可执行时由 ``os.execv`` 抛出。
    副作用：刷新标准输出后替换当前进程。
    """
    logger.info('backend_reexec', argv=' '.join(sys.argv))
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, *sys.argv])


class _ReadyAnnouncingServer(uvicorn.Server):
    """在端口开始监听之后打印就绪公告，并持有本进程拉起的子进程。

    子进程（QQ 适配器、桌面外壳）的启停时机绑在监听的建立与关闭上，而不是挂进
    ``lifecycle``：它们要在监听建立**之后**才起（外壳与适配器一上来就要连后端），
    要在服务器开始收尾**之前**就停（适配器停了才不会再有新的入站消息进来）。
    ``lifecycle`` 的两个边界都在这个区间之内，装不下它们。
    """

    def __init__(
        self,
        config: uvicorn.Config,
        port: int,
        token: str,
        runtime_path: Path,
        children: List[ChildProcess],
    ) -> None:
        """记录就绪公告所需的连接信息与待监护的子进程。

        :param config: Uvicorn 配置。
        :param port: 后端实际监听端口。
        :param token: 当前进程认证 token。
        :param runtime_path: 运行时凭据文件路径。
        :param children: 监听建立后按顺序拉起的子进程；停止时按相反顺序收走。
        """

        super().__init__(config)
        self._entry_port = port
        self._entry_token = token
        self._entry_runtime_path = runtime_path
        self._children = children

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        """完成 Uvicorn 启动后输出就绪标记，并拉起受监护的子进程。

        :param sockets: 已绑定的监听 socket 列表；由 Uvicorn 传入。

        副作用：
            先执行父类启动流程，再向标准输出写入 ``YUELI_READY=1`` 与 WebUI 就绪框，
            最后按顺序创建子进程。

        入口框只在这里打一次：地址在监听建立之前是打不开的，提前预告等于给出
        一个当时点了会失败的地址。
        """

        await super().startup(sockets=sockets)
        _announce_ready()
        _announce_webui_ready(
            self._entry_port, self._entry_token, self._entry_runtime_path)
        for child in self._children:
            try:
                await child.start()
            except OSError as exc:
                # 单个子进程拉不起来不该拖垮后端：QQ 适配器起不来时桌宠与 WebUI 仍
                # 可用，反之亦然。错误整条打出来，不做重试也不静默。
                logger.error('child_start_failed', child=child.name, error=str(exc))

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        """先收走子进程，再执行 Uvicorn 的优雅关闭。

        :param sockets: 已绑定的监听 socket 列表；由 Uvicorn 传入并在父类中关闭。
        :return: ``None``。
        副作用：终止全部子进程树，随后停止监听、等待在飞请求并执行 lifespan 关闭链。
        """

        for child in reversed(self._children):
            await child.stop(CHILD_STOP_GRACE_SECONDS)
        await super().shutdown(sockets=sockets)

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        """处理终止信号；收尾开始之后的重复信号不再升级为强制退出。

        - 现象：Uvicorn 默认的第二次 SIGINT 会置位 ``force_exit``，跳过在飞请求的
          等待与 lifespan 关闭链。
        - 原因：收尾链上挂着八个服务与子进程终止，最坏要走完
          ``timeout_graceful_shutdown`` 的 15 秒，用户很可能在这期间再按一次 Ctrl+C。
        - 后果：允许升级会让「多按一次」变成丢状态——未落库的回合、未写完的事件账本
          都在这条链上。收尾本身是有界的，这里只提示，不中断。

        :param sig: 收到的信号编号。
        :param frame: 信号发生时的栈帧；本实现不使用。
        :return: ``None``。
        副作用：首次信号置位 ``should_exit``；重复信号只输出一行提示。
        """

        if self.should_exit:
            print('正在收尾，请稍候；强制结束请从任务管理器结束该进程。', flush=True)
            return
        super().handle_exit(sig, frame)


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
    # 两个目录默认取仓库根下的同名目录，而不是当前工作目录下的：入口反转后
    # 进程可能从任意目录启动（systemd、快捷方式、另一个终端），按 cwd 解析会在
    # 别处建出第二份 data/ 与 config/，两边都「工作正常」只是记忆和配置对不上。
    parser.add_argument(
        "--data-dir",
        default=str(PROJECT_ROOT / 'data'),
        help="运行时数据目录，默认为仓库根的 data/",
    )
    parser.add_argument(
        "--config-path",
        default=str(PROJECT_ROOT / 'config'),
        help="主体配置目录，默认为仓库根的 config/",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_BACKEND_PORT)
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument(
        "--no-shell",
        action="store_true",
        help="即使桌宠已开启也不拉起 Electron 外壳；开发时外壳单独跑 npm run dev 用",
    )
    args = parser.parse_args()

    global _init_started_at
    _init_started_at = time.perf_counter()

    # 先把数据目录暴露给依赖环境变量的后端组件，再加载配置和日志。
    os.environ["YUELI_DATA_DIR"] = args.data_dir

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    config_dir = Path(args.config_path)
    # 配置文件缺失时先生成一份初始配置，再往下走。这一段此前只有 Electron 有：
    # 它首次启动弹设置窗口，填完才落盘。桌宠可以整个不在场之后，无头形态下没有
    # 任何进程会去创建 config/，而它不入版本库，全新签出里就是没有。
    if _bootstrap_config(config_dir):
        sys.exit(1)
    # 配置对账必须在解析之前：新增字段先补进文件再读，用户才能在文件里看到它们，
    # 而不是只看到一个「代码里有默认值」的隐形开关。写入前整目录备份。
    upgrade_config_directory(
        config_dir,
        {
            'providers.toml': ProviderCatalog,
            'models.toml': ModelCatalog,
            'bot.toml': BotDocument,
            'features.toml': FeatureDocument,
        },
        data_dir,
    )
    try:
        cfg = load_config(config_dir)
    except SystemExit:
        # load_config 自己把诊断打到 stderr 后 sys.exit(1)，抛的是 SystemExit 而不是
        # Exception。这里只在它后面补一句人话——「格式都对、就是还没填」这一类在
        # pydantic 的字段路径里看不出来；原始诊断原样保留，不吞不改。
        _announce_unfilled_config(config_dir)
        raise
    initialize_logging(cfg.log, data_dir / 'logs')
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
    announce_startup_self_check(db_path, config_dir, PROJECT_ROOT / 'adapters', cfg)
    configure_event_store(
        db_path,
        retention_count=cfg.log.event_retention_count,
        retention_hours=cfg.log.event_retention_hours,
    )
    logger.info("db_ready", path=str(db_path))

    # 检索调优的生效 profile 必须在启动横幅里可见：参数何时被谁换过，
    # 事后只能从这里对账；bootstrap 同时把覆盖表恢复进进程。
    from src.core.memory import tuning as retrieval_tuning
    active_profile = retrieval_tuning.bootstrap_active(db)
    overrides = retrieval_tuning.active_overrides()
    print_box(
        '检索调优',
        [
            f'当前生效 profile：{active_profile}',
            f'覆盖参数：{len(overrides)} 项'
            + (f'（{"、".join(sorted(overrides))}）' if overrides else ''),
        ],
        width=96,
        source=__name__,
    )

    # 先装配归属注册表和 broker，随后创建的聊天服务才能解析并投递外部 stream。
    from src.core.api.ws import push
    from src.core.services.chat import ChatService

    app_state.config_dir = config_dir
    app_state.registry = StreamRegistry(db)
    app_state.group_chat_config = cfg.group_chat
    app_state.developer_config = cfg.developer
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
    embedding_client_disabled_reason = 'model_tasks.embedding.model_list 是空的'
    if routers.embedding.ready:
        try:
            from src.core.memory.embed import build_client
            embed_client = build_client(routers.embedding)
        except Exception as exc:
            embedding_client_disabled_reason = '向量客户端构造失败'
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
    # 内容过滤仅在配置开启时装配（复用既有 vision 槽，不新增槽）；
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
    image_describer = ChatImageDescriber(
        cfg,
        vision_provider,
        emoji_tag_lookup=emoji_library.emotion_tags_for_hash,
    )

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
    # 无论开关状态都创建并注册服务：关闭或候选缺失必须在生命周期启动期明确说出来。
    from src.core.services.vector import VectorService
    vector_client = embed_client if cfg.vector.enabled else None
    vector_disabled_reason = (
        'vector.enabled=false'
        if not cfg.vector.enabled
        else embedding_client_disabled_reason
    )
    vector_service = VectorService(
        app_state.chat.memory,
        vector_client,
        disabled_reason=vector_disabled_reason,
        db=db,
    )
    app_state.chat._vector = vector_service
    # 导入中心需要给新写入的知识补向量；公开引用避免 API 层伸进聊天服务的私有字段。
    app_state.vector = vector_service
    if vector_service.enabled:
        logger.info(
            "vector_recall_enabled",
            model=routers.embedding.model,
            candidates=len(routers.embedding.candidates),
        )

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

    # 存储连接此前从不关闭：注册最早的服务，逆序关闭时最后执行，
    # 保证 chat/awareness 等关闭期间仍能写库。
    async def _storage_startup() -> None:
        """存储服务无启动动作；注册仅为挂接关闭钩子。"""

    async def _storage_shutdown() -> None:
        """关闭观察账本与主数据库连接（两者均已实现幂等关闭）。"""

        from src.core.observe.store import close as close_event_store
        close_event_store()
        from src.core.common.db.connection import close_db
        close_db()

    lifecycle.register('storage', _storage_startup, _storage_shutdown)

    # 开发者命令的注册集中在这里：/version 要拿本次运行的实际路径，模块导入期拿不到
    # （自定义 --data-dir / --config-path 时会读到另一份）。
    # 注册只是往进程内目录里追加条目，是否响应由通道按 [developer] 与 owner 判定。
    from src.core.services.dev_commands import register_dev_commands
    register_dev_commands(db_path, config_dir)
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
        SHA-256 重复计算第二次。真机 369 个文件（97 MB）的一次全量校验约 0.3 秒，
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

        淘汰与清理不进入表情收集的入站路径：容量按
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
    # 联想层的边衰减单独走低频任务，不挂回合路径：边没有 due_at 列，冻结判据是
    # 一次全表扫描，而它的时间尺度以月计（半衰期 720 小时，约 54 天才跌破阈值）。
    from src.core.services.edge_decay import EdgeDecayService
    edge_decay = EdgeDecayService(db)
    lifecycle.register('edge_decay', edge_decay.startup, edge_decay.shutdown)
    lifecycle.register('vector', vector_service.startup, vector_service.shutdown)
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
    # 反馈纠错（N4）整条链路默认关闭；开启时才装配服务。判定走 memory 路由，
    # 情节重建重摘要走 summary 路由，路由不可用时对应循环空转而不是报错。
    if cfg.memory_feedback.enabled:
        from src.core.services.memory_feedback import MemoryFeedbackService
        memory_feedback = MemoryFeedbackService(
            db,
            cfg.memory_feedback,
            judge_provider=routers.memory if routers.memory.ready else None,
            summary_provider=routers.summary if routers.summary.ready else None,
            bot_name=cfg.bot.name,
            bot_personality=cfg.personality.personality,
            judge_temperature=cfg.generation.memory.temperature,
            judge_max_tokens=cfg.generation.memory.token_limit,
            summary_temperature=cfg.generation.summary.temperature,
            summary_max_tokens=cfg.generation.summary.token_limit,
        )
        lifecycle.register('memory_feedback', memory_feedback.startup, memory_feedback.shutdown)
        if not routers.memory.ready:
            logger.warning('memory_feedback_disabled', reason='memory 模型路由不可用')
    lifecycle.register(
        'emoji_maintenance',
        _emoji_maintenance,
        _stop_emoji_maintenance,
    )
    lifecycle.register('chat', app_state.chat.startup, app_state.chat.shutdown)
    lifecycle.register("awareness", awareness.startup, awareness.shutdown)

    # 配置热重载的持有方更新：第 1 类字段的使用点都通过下面这些引用读取，
    # 重载成功后统一换到新对象即生效（第 2/3 类的分类见 loader 的前缀表）。
    # 每个持有方各自提供 apply_config，本回调只负责按装配关系逐个调用；
    # 各自持有的下级对象（聊天的图片描述器、传感器的视觉服务）由持有方级联，
    # 这里不再向下伸手。
    def _on_config_reloaded(previous: object, fresh: object) -> None:
        """把装配期创建的服务切到新配置对象上。

        :param previous: 旧配置（回调签名要求，本回调不使用）。
        :param fresh: 重载后的新配置。
        副作用：原地重绑各持有方的配置引用，不重建任何服务。
        """
        app_state.group_chat_config = fresh.group_chat
        app_state.developer_config = fresh.developer
        app_state.chat.apply_config(fresh)
        if app_state.tts is not None:
            app_state.tts.apply_config(fresh)
        app_state.awareness.apply_config(fresh)
        if schedule is not None:
            schedule.apply_config(fresh.schedule)
        sensor.apply_config(fresh)

    from src.core.config.loader import add_config_reload_listener
    add_config_reload_listener(_on_config_reloaded)

    logger.info("backend_starting", port=port)

    from src.core.api.app import create_app
    config = uvicorn.Config(
        create_app(),
        log_level="warning",
        access_log=False,
        # 给「等在飞 HTTP 任务」加 15 秒上界；超时由 uvicorn 取消残留任务，
        # 保证优雅退出有界。
        timeout_graceful_shutdown=15,
    )
    server = _ReadyAnnouncingServer(
        config,
        port=port,
        token=backend_runtime.token,
        runtime_path=runtime_file_path(data_dir),
        children=_build_children(cfg, config_dir, data_dir, not args.no_shell),
    )
    # 关机端点据这句柄置位 should_exit，走与 SIGINT 相同的优雅路径。
    app_state.uvicorn_server = server
    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        # Uvicorn 的 capture_signals 在优雅收尾跑完之后，会用原处理器重放捕获到的
        # SIGINT，于是 run() 抛出 KeyboardInterrupt。收尾此时已经全部完成，再让它
        # 冒泡只会在终端里留下一段与故障无关的 traceback。
        pass
    if app_state.restart_requested:
        _reexec_process()


if __name__ == "__main__":
    main()
