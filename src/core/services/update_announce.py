"""发布后向指定群公告新版本。

服务端在每次发版时把版本号写到公开的广播端点（见 ``telemetry-server/``），本模块
定时读取它，发现比自己新的版本就把该版本的更新内容发到配置指定的群，并只公告一次。

为什么要绕这一圈，而不是让发布流程直接调本机的接口：

- 现象：后端 HTTP 只监听回环地址，协议端也只监听本机，公网无处可推。
- 原因：把服务端或协议端其中一个开到公网，才能让云上的发布流程"推送"进来。
- 后果：本模块走"发布流程写、本机读"的形态，把暴露面留在零——多等几分钟换掉一个
  对公网开放的可控接口，这个交换是有意的，不要为了省那几分钟把端口开出去。

**默认关闭**：配置段缺失或 ``enabled`` 为假时整条链路惰性。把它发布出去也不会让
别人的 bot 跟着发公告——别人的配置里没有这个群号。

公告发出后还要进她自己的记忆，否则她在群里说过的话自己不知道：

- 现象：0.1.5 公告发进群后，在私聊和别的群问她「0.1.5 更新了什么」，她答不上来，
  还把旧版本的内容当成新版本说。
- 原因：公告直接走出站 broker，既不写消息历史也不进任何记忆层；而情节按设计不跨会话，
  事实只记关于人的事，关于版本的内容在别的会话里没有任何通道能被找到。
- 后果：因此两处都要写——公告原文作为她的发言落进该群的消息历史，更新条目逐条写进
  全局知识库，供任何会话里的 ``consult`` 查到。

任何失败都只记 warning，不影响任何功能路径；与 :mod:`src.core.runtime.telemetry`
同一取向。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, List, Tuple

import asyncio
import json
import sqlite3

import httpx

from src.core.agent.fact_extract import EmbedKnowledgeFn, persist_knowledge
from src.core.app_meta import APP_VERSION
from src.core.config.schema import UpdateAnnounceConfig
from src.core.logging.logger import get_logger
from src.core.memory.store import MemoryStore
from src.core.platform_io.types import OutboundMessage, StreamRef
from src.core.runtime.clock import now as current_time
from src.core.runtime.telemetry import REQUEST_TIMEOUT_S
from src.core.runtime.update_notes import read_release_notes, read_state, write_state

logger = get_logger(__name__)

# 广播端点的路径，与 telemetry-server 的路由一致。
LATEST_VERSION_PATH = '/update/latest'
# 状态文件里记录「已公告到哪个版本」的字段名。与更新内容报告共用同一份文件。
ANNOUNCED_FIELD = 'announced_version'
# 出站投递使用的平台标识。公告只走 QQ。
ANNOUNCE_PLATFORM = 'qq'
# 公告正文的首行。
_HEADING = '月璃更新到 {version}'
# 更新条目写入知识库时的来源标识，与事实抽取学到的知识区分开。
KNOWLEDGE_SOURCE = 'release_notes'
# 知识正文里的项目名。群里的问法是「yuelibot 0.1.5 更新了什么」，每条带上项目名与
# 版本号，词面检索才能把它们与其他知识区分开，也不会和别的版本的条目混淆。
_PROJECT_NAME = 'YueLiBot'
# 更新日志小节标题的前缀，例如 ``### 聊天``。
_SUBSECTION_PREFIX = '###'


def parse_version(text: str) -> Tuple[int, ...] | None:
    """把版本号解析成可比较的整数序列。

    :param text: 形如 ``1.2.3`` 的版本号；允许前后空白。
    :return: 整数元组；无法解析时返回 ``None``。不按字符串比较：``"0.10.0"`` 在
        字典序里小于 ``"0.9.0"``，那会让新版本永远公告不出去。
    """
    parts = text.strip().split('.')
    if not parts or any(not part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def is_newer(candidate: str, current: str) -> bool:
    """判断 ``candidate`` 是否比 ``current`` 新。

    :param candidate: 待判断的版本号。
    :param current: 基准版本号。
    :return: ``candidate`` 更高时为真；任一无法解析、或二者相等时均为假。
        解析失败按"不是更新"处理：宁可漏一次公告，也不能因为一个畸形版本号
        在群里刷出莫名其妙的公告。
    """
    left = parse_version(candidate)
    right = parse_version(current)
    if left is None or right is None:
        return False
    return left > right


def _higher(left: str, right: str) -> str:
    """返回两个版本号里较高的那个。

    :param left: 第一个版本号。
    :param right: 第二个版本号。
    :return: 较高的版本号；任一畸形（无法比较）时返回 ``right``。
    """
    return left if is_newer(left, right) else right


def announcement_text(version: str, lines: List[str]) -> str:
    """拼出群公告正文。

    :param version: 要公告的版本号。
    :param lines: 该版本的更新条目行，直接来自 ``CHANGELOG.md``。
    :return: 以版本标题开头、其后为空行与全部条目的单段文本。正文里的小节标题
        用 ``###`` 前缀，在 QQ 里就是普通文本，不做任何转换——公告与更新日志逐字
        一致，比在群里另造一套排版更容易核对。
    """
    return '\n'.join([_HEADING.format(version=version), '', *lines])


def release_note_knowledge(version: str, date: str, lines: List[str]) -> List[str]:
    """把某个版本的更新条目拆成逐条知识正文。

    逐条而不是整节写入：``consult`` 一次只返回少量条目、每条还会截断，整节塞进一行
    只能被看到开头几十个字。每条自带项目名、版本号、发布日期与所属小节，单独被检索
    出来也读得懂，且日期写死，不会被说成「两年前的事」。

    :param version: 版本号。
    :param date: 发布日期，取自更新日志小节标题；为空时省略。
    :param lines: 该版本小节下的原始行，含 ``###`` 小节标题、空行与 ``-`` 条目。
    :return: 知识正文列表，顺序与更新日志一致；小节标题与空行不单独成条。
    副作用：无。
    """
    released = f'（{date} 发布）' if date else ''
    section = ''
    entries: List[str] = []
    for raw in lines:
        text = raw.strip()
        if not text:
            continue
        if text.startswith(_SUBSECTION_PREFIX):
            section = text.lstrip('#').strip()
            continue
        item = text[1:].strip() if text.startswith('-') else text
        if not item:
            continue
        topic = f'的{section}更新' if section else '的更新'
        entries.append(f'{_PROJECT_NAME} {version} 版本{released}{topic}：{item}')
    return entries


class UpdateAnnounceService:
    """定时检查广播端点并在群公告新版本的后台服务。"""

    def __init__(
        self,
        data_dir: Path,
        config: UpdateAnnounceConfig,
        *,
        project_root: Path,
        registry: Any,
        broker: Any,
        register_stream: Callable[[StreamRef], None],
        store: MemoryStore,
        db: sqlite3.Connection,
        embed_knowledge: EmbedKnowledgeFn | None,
        endpoint: str,
        interval_s: float,
        previous_version: str | None = None,
    ) -> None:
        """保存运行参数，不做任何网络动作。

        :param data_dir: 运行时数据目录，已公告的版本号记在这里。
        :param config: ``[update_announce]`` 配置段；``enabled`` 为假时整条链路惰性。
        :param project_root: 仓库根目录，更新日志在其下。
        :param registry: ``StreamRegistry``，用于把群号解析成出站 stream。
        :param broker: ``PlatformBroker``，出站投递的唯一接缝。
        :param register_stream: 为该 stream 注册平台驱动的回调；群会话从未收到过消息时
            注册表里只有 stream 而没有驱动，缺这一步投递会直接失败。
        :param store: 记忆存储，公告原文作为她的发言写进该群的消息历史。
        :param db: 记忆库连接，更新条目写进知识库。
        :param embed_knowledge: 知识向量写入回调；向量服务未配置时为 ``None``，
            此时知识仍可经词面检索找到。
        :param endpoint: 广播端点根地址，不带尾斜杠。
        :param interval_s: 两次检查之间的间隔，单位秒，必须为正。
        :param previous_version: **启动那一刻**从状态文件读到的上次运行版本。必须由启动
            流程注入，不能在本服务里现读：启动期的 ``update_notes`` 会把同一个字段改写
            成当前版本，本服务再读就只剩「没有升级」这一种结论，升级带来的公告会被永久
            吞掉——部署紧接发版时必然如此，那不是偶发竞态而是固定顺序。
        :raises ValueError: ``interval_s`` 不为正。
        副作用：只保存引用，不建立连接也不读文件。
        """
        if interval_s <= 0:
            raise ValueError('公告检查间隔必须为正数')
        self._data_dir = data_dir
        self._config = config
        self._project_root = project_root
        self._registry = registry
        self._broker = broker
        self._register_stream = register_stream
        self._store = store
        self._db = db
        self._embed_knowledge = embed_knowledge
        self._endpoint = endpoint.strip().rstrip('/')
        self._interval_s = interval_s
        self._previous_version = previous_version
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def active(self) -> bool:
        """判断本轮是否会真的检查与公告。

        :return: 配置开关打开、群号已填且端点非空时为真。
        """
        return self._config.enabled and bool(self._config.group.strip()) and bool(self._endpoint)

    async def startup(self) -> None:
        """启动检查任务。

        必须立即返回：把循环本身注册成启动钩子会让生命周期永久 await。

        :return: ``None``。
        副作用：``active`` 为真时创建名为 ``update-announce`` 的后台任务。
        """
        if not self.active:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name='update-announce')

    async def shutdown(self) -> None:
        """停止检查任务并等待它退出。

        :return: ``None``。
        副作用：置停止信号并取消后台任务。
        """
        self._stop.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: B014 - 收尾不因公告失败而中断
            pass

    async def _loop(self) -> None:
        """先立即检查一次，随后按固定间隔检查，直到收到停止信号。

        开机先查一次是刻意的：机器人重启往往紧跟一次升级，先查能把公告的延迟压到
        重启那一刻，而不必等满一个间隔。
        """
        while not self._stop.is_set():
            try:
                await self.check_once()
            except Exception as exc:  # noqa: BLE001 - 公告不得把异常带进任何功能路径
                logger.warning('update_announce_failed', kind=type(exc).__name__, error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_s)
            except asyncio.TimeoutError:
                continue

    async def check_once(self) -> bool:
        """检查一次并在此需要时公告。

        :return: 本次是否真的发出了公告。
        :raises httpx.HTTPError: 广播端点不可达或返回非成功状态码。
        :raises ValueError: 端点返回的结构不符合约定。
        :raises OSError: 更新日志不可读。
        :raises RuntimeError: 出站 broker 未装配。
        :raises sqlite3.Error: 公告已发出，但写进记忆时失败；已公告的版本号此前已记下，
            下一轮不会重发。
        """
        if not self.active:
            return False
        version = await self.fetch_latest()
        # 门槛一律取「启动那一刻的上次运行版本」，由启动流程注入本对象。
        #
        # 不在这里现读状态文件：同一个字段会被启动期的 update_notes 改写成当前版本，
        # 而本服务的循环是服务启动之后才跑的，现读只会得到「没有升级」这一种结论。
        # 部署紧接发版时必然如此，所以这不是偶发竞态，是固定顺序。
        previous = self._previous_version
        # 门槛就是「这次启动之前的那个版本」，一个字都不改：
        # - 远端比它新 → 差异来自这次发版，该公告（本机是否已经升上去都不影响）；
        # - 远端不比它新 → 要么已公告过、要么远端广播落后于本机，都不该发。
        #
        # 不要在这里按「本机是否升过头」再抬门槛：本机版本高于过去版本正是升级本身，
        # 抬上去就把这一版的公告判成「不是更新」而吞掉；而远端真落后时上面第二条
        # 已经拦住了，不需要额外判断。
        floor = previous if previous is not None else APP_VERSION
        if not is_newer(version, floor):
            return False
        # 这条不能省：门槛固定在启动时的旧版本上，少了它，同一次升级会在每轮检查里
        # 重复公告，直到下一次重启把门槛抬上去为止。
        if read_state(self._data_dir).get(ANNOUNCED_FIELD) == version:
            return False
        notes = read_release_notes(self._project_root, version)
        if notes is None:
            # 广播端点说发了新版，更新日志里却没有这一节：不公告，也不记状态，
            # 等你补上更新日志后下一轮会自动补发。
            logger.warning('update_announce_notes_missing', version=version)
            return False
        date, lines = notes
        text = announcement_text(version, lines)
        stream = await self.post_to_group(text)
        if stream is None:
            return False
        # 先记状态再写记忆：写记忆失败时异常经检查循环记成 warning，若此时状态还没记下，
        # 下一轮会把同一份公告再往群里发一遍——记忆缺一份可以补，群里重复刷屏收不回来。
        self._remember(version)
        message_id, knowledge_ids = await self._record_in_memory(stream, text, version, date, lines)
        logger.info(
            'update_announced_to_group',
            version=version,
            group=self._config.group,
            messageId=message_id,
            knowledge=len(knowledge_ids),
        )
        return True

    async def fetch_latest(self) -> str:
        """读取广播端点上的最新版本号。

        :return: 版本号字符串；端点尚未广播过任何版本时为空串——那是正常空态，
            不是错误，调用方按"没有更新"处理。
        :raises httpx.HTTPError: 请求失败、超时或返回非成功状态码。
        :raises ValueError: 响应不是 JSON 对象，或缺少 ``version`` 字段、字段不是字符串。
            空串是合法的：端点刚部署、还没发过版时就是这个值，把它当错误会让每次
            检查都在日志里留一条没人能处理的警告。
        副作用：一次 HTTPS GET。
        """
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            response = await client.get(f'{self._endpoint}{LATEST_VERSION_PATH}')
            response.raise_for_status()
            document = response.json()
        if not isinstance(document, dict):
            raise ValueError(f'{LATEST_VERSION_PATH} 返回的顶层不是对象')
        version = document.get('version')
        if not isinstance(version, str):
            raise ValueError(f'{LATEST_VERSION_PATH} 的 version 必须是字符串')
        return version.strip()

    async def post_to_group(self, text: str) -> StreamRef | None:
        """把公告正文发到配置指定的群。

        :param text: 公告正文。
        :return: 投递成功时返回该群的 stream，供随后写消息历史；失败只记 warning 并返回 ``None``。
        :raises RuntimeError: 出站 broker 未装配（装配错误必须暴露，不当成投递失败）。
        """
        if self._broker is None:
            raise RuntimeError('公告出站缺少平台 broker')
        stream = self._registry.get_or_create_stream(
            ANNOUNCE_PLATFORM, 'group', self._config.group.strip(),
        )
        self._register_stream(stream)
        try:
            await self._broker.dispatch(OutboundMessage(stream=stream, segments=[text]))
        except Exception as exc:  # noqa: BLE001 - 投递失败不能让后台任务退出
            logger.warning(
                'update_announce_delivery_failed',
                group=self._config.group,
                kind=type(exc).__name__,
                error=str(exc),
            )
            return None
        return stream

    async def _record_in_memory(
        self,
        stream: StreamRef,
        text: str,
        version: str,
        date: str,
        lines: List[str],
    ) -> Tuple[int, List[int]]:
        """把已经发出的公告写进她自己的记忆。

        原文以她的身份落进该群的消息历史，与开发者命令回复同一做法：该群后续的对话、
        摘要都能看到她说过这段话。更新条目另外逐条写进知识库，因为消息历史和由它生成的
        情节都不跨会话，别的群和私聊只能经 ``consult`` 从知识库里查到。

        :param stream: 公告所在群的 stream。
        :param text: 已发出的公告原文。
        :param version: 公告的版本号。
        :param date: 该版本的发布日期，可为空串。
        :param lines: 更新日志里该版本的原始行。
        :return: ``(消息 ID, 知识 ID 列表)``；同一条目已在库里时返回既有知识 ID。
        :raises sqlite3.Error: 写入失败。
        副作用：写 ``messages``、``knowledge`` 与 ``knowledge_fts`` 并提交；配置了向量服务时
            为新条目生成向量。
        """
        message_id = self._store.append_message(
            stream_id=stream.id,
            sender_person_id=None,
            role='assistant',
            content=text,
            now=current_time(),
        )
        knowledge_ids = await persist_knowledge(
            self._db,
            release_note_knowledge(version, date, lines),
            current_time(),
            embed_knowledge=self._embed_knowledge,
            source=KNOWLEDGE_SOURCE,
        )
        return message_id, knowledge_ids

    def _remember(self, version: str) -> None:
        """记下已公告的版本号，写不进去只记日志。

        :param version: 已公告的版本号。
        """
        try:
            write_state(self._data_dir, {ANNOUNCED_FIELD: version})
        except OSError as exc:
            logger.warning(
                'update_announce_state_unwritable',
                path=str(self._data_dir),
                error=str(exc),
            )


__all__ = [
    'ANNOUNCED_FIELD',
    'KNOWLEDGE_SOURCE',
    'LATEST_VERSION_PATH',
    'UpdateAnnounceService',
    'announcement_text',
    'is_newer',
    'parse_version',
    'release_note_knowledge',
]
