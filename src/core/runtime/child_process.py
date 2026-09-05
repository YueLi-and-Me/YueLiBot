"""外部子进程监护：拉起、日志转发、异常重启与有界收尾。

Python 成为进程入口之后，QQ 适配器与桌面外壳（Electron）都由本进程负责拉起；这套监护
逻辑此前完整地长在 Electron 的进程监护器里（已随入口反转删除）。本模块只提供与业务无关
的进程原语：按 argv 启动、把子进程 stdout/stderr 按行加标签转发到本进程输出、异常退出后
按指数退避重启、停止时结束整棵进程树。

拉起谁由 ``src.core.services.host.adapter_host`` 与 ``src.core.services.host.desktop_shell``
决定；启动与收尾的时机由 ``src.main`` 在监听建立之后、服务器关闭之前编排。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Sequence

import asyncio
import os
import signal
import subprocess
import sys

from src.core.logging.logger import get_logger

logger = get_logger(__name__)

# 连续异常退出的重启上限；超过后停止监护，等用户处理。
MAX_RESTARTS = 5
# 退避基准秒数，第 n 次重启等待 BASE_BACKOFF * 2 ** (n - 1) 秒。
BASE_BACKOFF = 2.0
# 稳定运行超过这个秒数就认为上一次重启成功，连续重启计数清零。
STABLE_AFTER = 60.0
# 无换行输出的缓冲上限（字节）。子进程可能长时间不吐换行，缓冲不设上限会无限增长。
MAX_LINE_BUFFER = 64 * 1024
# taskkill 的等待秒数；超时后走直接终止兜底。
TREE_KILL_GRACE = 2.0


def _child_environment(extra: Dict[str, str] | None) -> Dict[str, str]:
    """在当前进程环境上叠加子进程所需的输出行为变量。

    :param extra: 调用方追加的环境变量；``None`` 表示只应用公共项。
    :return: 供 ``asyncio.create_subprocess_exec`` 使用的完整环境映射。
    副作用：不修改当前进程环境。
    """
    env = dict(os.environ)
    # stdout 经管道转发，子进程的 isatty() 为 False。三项分别解决：缓冲导致日志迟到、
    # Windows 管道默认编码不是 UTF-8 导致中文乱码、彩色输出被自动关闭。
    env['PYTHONUNBUFFERED'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    env['YUELI_FORCE_COLOR'] = '1'
    if extra:
        env.update(extra)
    return env


def _isolation_kwargs() -> Dict[str, Any]:
    """给出把子进程放进独立进程组所需的创建参数。

    - 现象：不隔离时适配器会在后端刚开始收尾时就自己抛 KeyboardInterrupt 退出。
    - 原因：终端的中断会送给整个前台进程组的所有成员——Windows 是
      ``CTRL_C_EVENT`` 广播，POSIX 是 SIGINT 发往前台进程组。
    - 后果：收尾顺序（先停适配器、再关后端）由本进程编排，子进程抢先响应会让顺序失效，
      日志里只剩半截 traceback。

    两个平台的开关名不同且互斥：``start_new_session`` 只有 POSIX 支持，在 Windows 上
    传它会直接抛 ``ValueError``，因此按平台给不同的键，不能合成一份公共参数。

    :return: 传给 ``asyncio.create_subprocess_exec`` 的平台相关关键字参数。
    """
    if sys.platform == 'win32':
        return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
    return {'start_new_session': True}


class ChildProcess:
    """一个受监护的外部子进程。

    负责单个子进程的完整生命周期：首次拉起失败直接抛给调用方（启动阶段就该暴露），
    运行期把输出按行贴标签转发，异常退出按退避重启，返回码 0 视为对方主动结束、
    不重启。
    """

    def __init__(
        self,
        *,
        name: str,
        tag: str,
        argv: Sequence[str],
        cwd: Path,
        env: Dict[str, str] | None = None,
        restart_on_failure: bool = True,
    ) -> None:
        """记录子进程的启动参数与重启策略。

        :param name: 用于日志的服务名，例如 ``qq_adapter``。
        :param tag: 转发输出时加在每行前面的短标签，例如 ``snowluma``。
        :param argv: 完整命令行；``argv[0]`` 为可执行文件路径或 PATH 中的名字。
        :param cwd: 子进程工作目录。
        :param env: 追加的环境变量；公共的编码与彩色变量由本类统一注入。
        :param restart_on_failure: 异常退出后是否按退避重启。
        """
        self._name = name
        self._tag = tag
        self._argv: List[str] = list(argv)
        self._cwd = cwd
        self._env = _child_environment(env)
        self._restart_on_failure = restart_on_failure
        self._process: asyncio.subprocess.Process | None = None
        self._pumps: List[asyncio.Task[None]] = []
        self._watcher: asyncio.Task[None] | None = None
        self._stopping = False
        self._restarts = 0

    @property
    def name(self) -> str:
        """返回用于日志的服务名。"""
        return self._name

    @property
    def alive(self) -> bool:
        """判断子进程当前是否存在且尚未退出。

        :return: 进程已创建且返回码尚未产生时为 ``True``。
        """
        process = self._process
        return process is not None and process.returncode is None

    async def start(self) -> None:
        """拉起子进程并进入监护循环。

        :return: ``None``；首次拉起完成后立即返回，后续退出与重启由后台任务处理。
        :raises OSError: 可执行文件不存在或权限不足。首次拉起失败不重试，由调用方
            决定是终止启动还是降级运行。
        副作用：创建子进程、两个输出转发任务和一个退出监视任务。
        """
        self._stopping = False
        self._restarts = 0
        await self._spawn()
        self._watcher = asyncio.create_task(self._watch(), name=f'child-watch:{self._name}')

    async def stop(self, grace: float = 5.0) -> None:
        """终止子进程并结束监护循环。

        :param grace: 等待子进程自行退出的秒数，超时后强制终止。
        :return: ``None``；进程已退出或从未启动时安全返回。
        副作用：终止子进程树，取消监视任务并等待转发任务写完剩余输出。
        """
        self._stopping = True
        process = self._process
        if process is not None and process.returncode is None:
            await self._terminate(process, grace)
            # 退出日志由这里打，不交给监视任务：stop() 紧接着就会取消它，
            # 它多半来不及写这一行，表现为「有 child_started、没有对应的收尾」。
            logger.info('child_stopped', child=self._name, code=process.returncode)
        watcher = self._watcher
        self._watcher = None
        if watcher is not None and not watcher.done():
            watcher.cancel()
            # 监视任务只做等待与日志，取消后无需回收结果。
            await asyncio.gather(watcher, return_exceptions=True)
        await self._drain_pumps()

    async def _spawn(self) -> None:
        """创建一次子进程并挂上输出转发任务。

        :return: ``None``。
        :raises OSError: 进程创建失败。
        副作用：写入 ``_process`` 与 ``_pumps``，并输出一行启动日志。
        """
        process = await asyncio.create_subprocess_exec(
            *self._argv,
            cwd=str(self._cwd),
            env=self._env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_isolation_kwargs(),
        )
        self._process = process
        self._pumps = [
            asyncio.create_task(self._pump(process.stdout, False), name=f'child-out:{self._name}'),
            asyncio.create_task(self._pump(process.stderr, True), name=f'child-err:{self._name}'),
        ]
        logger.info('child_started', child=self._name, pid=process.pid)

    async def _watch(self) -> None:
        """等待子进程退出，并按策略决定是否重启。

        :return: ``None``；停止监护、正常退出或超过重启上限时结束。
        副作用：输出退出与重启日志，可能重新创建子进程。
        """
        while True:
            process = self._process
            if process is None:
                return
            started_at = asyncio.get_running_loop().time()
            code = await process.wait()
            await self._drain_pumps()
            if self._stopping:
                # 收尾中的退出由 stop() 负责记录，这里不重复输出。
                return
            if code == 0:
                # 返回码 0 是对方主动结束：用户从托盘退出桌面外壳走的就是这一条。
                logger.info('child_exited_clean', child=self._name)
                self._process = None
                return
            if asyncio.get_running_loop().time() - started_at > STABLE_AFTER:
                self._restarts = 0
            if not self._restart_on_failure:
                logger.error('child_failed', child=self._name, code=code)
                self._process = None
                return
            if self._restarts >= MAX_RESTARTS:
                logger.error(
                    'child_restart_exhausted',
                    child=self._name,
                    code=code,
                    limit=MAX_RESTARTS,
                )
                self._process = None
                return
            delay = BASE_BACKOFF * 2 ** self._restarts
            self._restarts += 1
            logger.warning(
                'child_restarting',
                child=self._name,
                code=code,
                attempt=self._restarts,
                delaySeconds=delay,
            )
            await asyncio.sleep(delay)
            if self._stopping:
                return
            try:
                await self._spawn()
            except OSError as exc:
                logger.error('child_respawn_failed', child=self._name, error=str(exc))
                self._process = None
                return

    async def _pump(self, stream: asyncio.StreamReader | None, is_error: bool) -> None:
        """把子进程的一路输出按行贴标签转发到本进程输出。

        不使用 ``StreamReader.readline``：单行超过内部上限时它抛 ``ValueError``，
        而子进程完全可能吐出超长行（模型返回体、异常栈）。这里自行缓冲，超过
        ``MAX_LINE_BUFFER`` 丢弃未成行内容，行为可预期。

        :param stream: 子进程的 stdout 或 stderr；未建立管道时为 ``None``。
        :param is_error: 为 ``True`` 时写入本进程 stderr，否则写入 stdout。
        :return: ``None``；流结束时返回。
        副作用：向当前进程的标准输出流写入文本。
        """
        if stream is None:
            return
        buffer = b''
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            buffer += chunk
            while True:
                index = buffer.find(b'\n')
                if index == -1:
                    break
                self._write_line(buffer[:index], is_error)
                buffer = buffer[index + 1:]
            if len(buffer) > MAX_LINE_BUFFER:
                buffer = b''
        if buffer:
            self._write_line(buffer, is_error)

    def _write_line(self, raw: bytes, is_error: bool) -> None:
        """输出一行带标签的子进程日志。

        :param raw: 不含换行符的原始字节。
        :param is_error: 是否写入 stderr。
        :return: ``None``；空白行不输出。
        副作用：写入并刷新当前进程的标准输出流。
        """
        text = raw.decode('utf-8', errors='replace').rstrip()
        if not text:
            return
        target = sys.stderr if is_error else sys.stdout
        target.write(f'[{self._tag}] {text}\n')
        target.flush()

    async def _drain_pumps(self) -> None:
        """等待两路输出转发任务把剩余内容写完。

        :return: ``None``。
        副作用：清空转发任务列表。
        """
        pumps = self._pumps
        self._pumps = []
        if pumps:
            await asyncio.gather(*pumps, return_exceptions=True)

    async def _terminate(self, process: asyncio.subprocess.Process, grace: float) -> None:
        """结束子进程及其整棵进程树。

        - 现象：只终止直接子进程会留下孤儿——Windows 上是 Electron 与它的 GPU 进程，
          POSIX 上是 ``npm`` 派生的 node 与 electron。
        - 原因：外壳是 ``npm`` → ``node`` → ``electron`` 三层；Windows 没有真正的
          SIGTERM，``Process.terminate`` 退化为 ``TerminateProcess`` 只结束一层，
          POSIX 的 ``terminate`` 也只向直接子进程发信号。
        - 后果：改回直接终止会让残留进程继续占着运行时凭据与窗口，下一次启动表现为
          「桌宠出现两个」。

        两个平台各用各的树形终止：Windows 靠 ``taskkill /T``，POSIX 靠进程组——
        子进程创建时已经 ``start_new_session``，它的 pid 同时是新进程组的 pgid。

        :param process: 待终止的子进程。
        :param grace: 等待其自行退出的秒数。
        :return: ``None``。
        副作用：终止目标进程树，Windows 上额外启动一个 taskkill 子进程。
        """
        if sys.platform == 'win32':
            await self._taskkill_tree(process)
        else:
            self._signal_group(process, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=grace)
            return
        except asyncio.TimeoutError:
            logger.warning('child_kill_timeout', child=self._name, graceSeconds=grace)
        if sys.platform == 'win32':
            process.kill()
        else:
            self._signal_group(process, signal.SIGKILL)
        await process.wait()

    def _signal_group(self, process: asyncio.subprocess.Process, number: int) -> None:
        """在 POSIX 上把信号发给子进程所在的整个进程组。

        :param process: 目标子进程。
        :param number: 要发送的信号编号。
        :return: ``None``；进程已退出时安全返回。
        副作用：向进程组内的全部进程发送信号。
        """
        try:
            os.killpg(os.getpgid(process.pid), number)
        except ProcessLookupError:
            # 子进程在两次检查之间自己退出了，没有需要终止的对象。
            return
        except PermissionError as exc:
            # 组内出现了本进程无权终止的成员时，退回只终止直接子进程，并把原因留痕。
            logger.warning('child_killpg_denied', child=self._name, error=str(exc))
            process.send_signal(number)

    async def _taskkill_tree(self, process: asyncio.subprocess.Process) -> None:
        """在 Windows 上用 taskkill 结束目标进程树。

        :param process: 待终止的子进程。
        :return: ``None``；taskkill 不可用或迟迟不返回时静默返回，由调用方的直接
            终止兜底。
        副作用：启动一个 taskkill 子进程。
        """
        try:
            killer = await asyncio.create_subprocess_exec(
                'taskkill', '/T', '/F', '/pid', str(process.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            logger.warning('child_taskkill_unavailable', child=self._name, error=str(exc))
            return
        try:
            await asyncio.wait_for(killer.wait(), timeout=TREE_KILL_GRACE)
        except asyncio.TimeoutError:
            killer.kill()


__all__ = ['ChildProcess']
