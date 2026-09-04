"""运行画像：本地采集一台机器自己的安装 ID、启动次数与累计运行时长。

采集侧无条件运行——它是每台机器都在记的本地数据，必须进发行版，不挂在
``[developer]`` 开关下；读取侧的 ``/stat`` 才挂在开关下，而开关与拦截由 D1 的
命令通道统一执行，本模块不自带门控。

三条硬约束的实现位置：

- 零出网：本模块只使用 sqlite3 / secrets / time / asyncio，没有任何网络调用，
  也没有指向外部地址的配置项（约束检查见 pytests 的 C-5 用例）。
- 安装 ID 随机生成：``secrets.token_hex`` 产出，不由 QQ 号、机器名、MAC、路径
  或任何可关联到人的东西派生，与身份体系完全隔离（约束检查见 C-2 用例）。
- 不采集可关联到人的信息：画像表只有计数与时间戳；库规模只记行数，且行数是
  读取时现算的，不落第二份会过期的副本。

画像是可丢的派生数据：整表删掉只丢历史统计，不影响任何功能；下次启动由迁移
链尾 DDL 的幂等建表重建，采集从零重新开始（安装 ID 只存在这张表里，也随之
重新生成）。
"""

from __future__ import annotations

import asyncio
import secrets
import sqlite3
import time
from typing import Dict, List, Optional

from src.core.app_meta import APP_VERSION
from src.core.commands import CommandContext, register_command
from src.core.common.clock import now as current_time
from src.core.common.logger import get_logger

logger = get_logger(__name__)

# 心跳落库间隔（秒）。取 5 分钟的理由：它界定了进程被强杀（断电、任务管理器
# 结束、崩溃）时累计运行时长的最大损失；每次落库只是一行 UPDATE，代价可忽略，
# 而 5 分钟的损失对「这台机器大概跑了多久」这个量级的问题没有影响。
HEARTBEAT_SECONDS = 300

# 库规模统计的表清单。只数行数、不读内容；表按固定字面量列出，不接受外部输入。
LIBRARY_TABLES = (
    'messages',
    'episodes',
    'facts',
    'knowledge',
    'jargon',
    'expressions',
    'emoji',
    'persons',
    'streams',
)

_ENSURE_DDL = '''
CREATE TABLE IF NOT EXISTS runtime_profile (
  id               INTEGER PRIMARY KEY CHECK (id = 1),
  install_id       TEXT    NOT NULL DEFAULT '',
  app_version      TEXT    NOT NULL DEFAULT '',
  launch_count     INTEGER NOT NULL DEFAULT 0,
  first_launch_at  INTEGER,
  last_launch_at   INTEGER,
  total_runtime_ms INTEGER NOT NULL DEFAULT 0,
  updated_at       INTEGER
);
'''


def read_app_version() -> str:
    """返回应用版本号。

    只从 :mod:`src.core.app_meta` 这一处读——版本号各写一份正是那个模块要消灭的
    现状，本模块不解析 pyproject.toml / package.json，也不留兜底分支：那一处不在了
    就该在导入期直接报错，而不是把画像里的版本悄悄记成空串。

    :return: 单一来源声明的应用版本号。
    """

    return APP_VERSION


class RuntimeProfileService:
    """启动时记一次启动，此后按心跳把运行时长落库，退出时收尾再落一次。

    采集不在回合关键路径上：启动一次写、心跳周期写、退出一次写，消息处理链路
    不触碰本服务。
    """

    def __init__(
        self,
        db: sqlite3.Connection,
        *,
        app_version: Optional[str] = None,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
    ) -> None:
        """保存数据库连接与采集参数。

        :param db: 进程级 SQLite 连接（与各服务同一来源）。
        :param app_version: 应用版本；为 ``None`` 时保留库中已有值（D2 来源未落地）。
        :param heartbeat_seconds: 心跳落库间隔；测试可注入小值。
        副作用：只保存引用，不启动任务、不写库。
        """
        self._db = db
        self._app_version = app_version
        self._heartbeat_seconds = heartbeat_seconds
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._last_persist_mono: Optional[float] = None

    async def startup(self) -> None:
        """建表兜底、登记本次启动，并启动心跳落库任务。

        :return: ``None``。
        副作用：写入一行启动记录（必要时生成随机安装 ID），随后创建名为
            ``runtime-profile-heartbeat`` 的后台任务。
        """
        self._stop.clear()
        self.record_launch()
        self._last_persist_mono = time.monotonic()
        self._task = asyncio.create_task(
            self._heartbeat_loop(), name='runtime-profile-heartbeat')

    async def shutdown(self) -> None:
        """停止心跳并把整段会话时长收尾落库。

        :return: ``None``。
        副作用：设置停止事件、等待心跳任务退出，随后执行最后一次时长落库。
        """
        self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.flush_runtime()

    def record_launch(self) -> None:
        """登记一次启动：必要时生成安装 ID，启动次数 +1，记下启动时刻与版本。

        表不存在时先按当前形态补齐——画像表允许被整表删除（可丢的派生数据），
        删除后的下一次启动必须能重新开始采集。
        """
        now_ms = current_time()
        with self._db:
            self._db.executescript(_ENSURE_DDL)
            self._db.execute(
                'INSERT OR IGNORE INTO runtime_profile (id, updated_at) VALUES (1, ?)',
                (now_ms,),
            )
            row = self._db.execute(
                'SELECT install_id FROM runtime_profile WHERE id = 1'
            ).fetchone()
            install_id = str(row['install_id']) if row is not None else ''
            if not install_id:
                # 随机安装 ID：32 位十六进制字符（128 bit），由 CSPRNG 生成，
                # 不读取任何身份、机器或路径信息，设计上不可反查到人。
                install_id = secrets.token_hex(16)
                self._db.execute(
                    'UPDATE runtime_profile SET install_id = ?, first_launch_at = ? '
                    'WHERE id = 1',
                    (install_id, now_ms),
                )
                logger.info(
                    'runtime_profile_install_id_created',
                    length=len(install_id),
                    charset='hex',
                )
            if self._app_version is not None:
                self._db.execute(
                    'UPDATE runtime_profile SET app_version = ? WHERE id = 1',
                    (self._app_version,),
                )
            self._db.execute(
                'UPDATE runtime_profile '
                'SET launch_count = launch_count + 1, last_launch_at = ?, '
                '    updated_at = ? WHERE id = 1',
                (now_ms, now_ms),
            )
        logger.info(
            'runtime_profile_launch_recorded',
            launch_count=self._db.execute(
                'SELECT launch_count FROM runtime_profile WHERE id = 1'
            ).fetchone()['launch_count'],
            app_version=self._app_version if self._app_version is not None else '保留原值',
        )

    def flush_runtime(self) -> None:
        """把自上次落库以来的会话时长追加进累计运行时长。

        心跳与退出收尾共用这一个写点；用单调时钟计量会话时长，避免系统时间
        回拨把累计值改小。
        """
        if self._last_persist_mono is None:
            return
        now_mono = time.monotonic()
        delta_ms = int((now_mono - self._last_persist_mono) * 1000)
        self._last_persist_mono = now_mono
        if delta_ms <= 0:
            return
        now_ms = current_time()
        with self._db:
            self._db.execute(
                'UPDATE runtime_profile '
                'SET total_runtime_ms = total_runtime_ms + ?, updated_at = ? '
                'WHERE id = 1',
                (delta_ms, now_ms),
            )
        logger.debug('runtime_profile_runtime_flushed', delta_ms=delta_ms)

    async def _heartbeat_loop(self) -> None:
        """按心跳间隔周期落库运行时长，直到收到停止信号。

        落库失败只记日志、不终止循环：画像是派生数据，一次失败等下一个间隔即可。
        """
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._heartbeat_seconds)
                break
            except asyncio.TimeoutError:
                pass
            try:
                self.flush_runtime()
            except sqlite3.Error as exc:
                logger.error('runtime_profile_heartbeat_failed', error=str(exc))


def collect_library_scale(db: sqlite3.Connection) -> Dict[str, int]:
    """统计各主要表的行数；只数行数，不读任何行内容。

    行数在读取时现算，不落第二份会过期的副本；``/stat`` 展示的数字因此与直接
    ``SELECT COUNT(*)`` 永远一致。清单里不存在的表（历史形态差异）跳过。
    """

    existing = {
        str(row[0])
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    scale: Dict[str, int] = {}
    for table in LIBRARY_TABLES:
        if table not in existing:
            continue
        scale[table] = int(
            db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        )
    return scale


def read_profile(db: sqlite3.Connection) -> Optional[Dict[str, object]]:
    """读取画像行；表缺失或尚未写入时返回 ``None``。"""

    try:
        row = db.execute(
            'SELECT install_id, app_version, launch_count, first_launch_at, '
            'last_launch_at, total_runtime_ms FROM runtime_profile WHERE id = 1'
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return dict(row)


def _format_duration(total_ms: int) -> str:
    """把毫秒时长格式化为「x 天 x 小时 x 分钟」的人读文本。"""

    minutes = max(total_ms, 0) // 60_000
    days, rem = divmod(minutes, 60 * 24)
    hours, mins = divmod(rem, 60)
    parts: List[str] = []
    if days:
        parts.append(f'{days} 天')
    if hours:
        parts.append(f'{hours} 小时')
    parts.append(f'{mins} 分钟')
    return ' '.join(parts)


def _format_timestamp(ms: Optional[int]) -> str:
    """把 Unix 毫秒时间戳格式化为本地时间文本；空值显示「未知」。"""

    if not ms:
        return '未知'
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ms / 1000))


def build_stat_message(db: sqlite3.Connection) -> str:
    """把运行画像组织成给人看的一条消息（``/stat`` 的回复正文）。

    只读画像表与各表行数，不输出任何可关联到人的信息：安装 ID 是本机随机值，
    库规模只有行数。
    """

    profile = read_profile(db)
    if profile is None:
        return '运行画像还没有数据：画像表缺失或尚未启动过采集。'
    scale = collect_library_scale(db)
    lines = [
        '运行画像（本机）',
        f"安装 ID：{profile['install_id'] or '尚未生成'}",
        f"应用版本：{profile['app_version'] or '未知'}",
        f"启动次数：{profile['launch_count']}",
        f"首次启动：{_format_timestamp(profile['first_launch_at'])}",
        f"最近启动：{_format_timestamp(profile['last_launch_at'])}",
        f"累计运行：{_format_duration(int(profile['total_runtime_ms']))}",
    ]
    if scale:
        lines.append('库规模（行数）：')
        lines.extend(f'  {table}：{count}' for table, count in scale.items())
    return '\n'.join(lines)


def register_stat_command(db: sqlite3.Connection) -> None:
    """把 ``/stat`` 注册进命令通道的注册表。

    读取侧的 ``[developer]`` 门控由通道的拦截与鉴权统一执行，本函数不自带开关；
    采集侧则无条件运行，两者的边界就在这里——注册失败不影响采集，但也不再吞掉：
    重复注册是编码错误，应当在启动期直接暴露。

    :param db: 进程级 SQLite 连接，``/stat`` 处理函数读取画像与库规模用。
    :return: ``None``。
    :raises ValueError: 命令名重复注册（同一进程内重复调用本函数）。
    副作用：向命令通道的进程内注册表追加一条只读命令。
    """

    @register_command(
        name='/stat',
        pattern=r'/stat',
        description='本机运行画像：安装 ID、启动次数、累计运行时长与库规模',
    )
    def _stat(context: CommandContext) -> str:
        """画像命令不取参数，上下文只用于满足处理器签名。"""
        del context
        return build_stat_message(db)
