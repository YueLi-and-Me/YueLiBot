"""匿名安装量统计的客户端。

只回答一个问题：全球有多少个安装、分别是什么版本、跑在什么系统上。
载荷固定为三个字段，不含聊天内容、不含任何身份信息，也不含模型别名。

**端点候选全为空时整条链路保持惰性**——不注册、不心跳、不建任何网络对象。
发行版里这条路径是活的但服务端可能尚未立起，惰性保证用户看到的是「什么都
没发生」，而不是每十分钟一次注定失败的请求。

身份由服务端下发：首次 ``POST /register`` 取回 UUID 并落在数据目录，之后每次
``POST /heartbeat`` 带 ``Client-UUID`` 头。客户端不自己生成 ID，换来的是统计
口径能防刷、能去重。

服务端有主备两个地址，按 :data:`TELEMETRY_ENDPOINTS` 的顺序逐个尝试。之所以
不能只留一个：

- 现象：``*.workers.dev`` 在中国大陆被 DNS 污染，解析即失败，注册请求到不了边缘
  节点；服务端侧连一条访问记录都不会留下，统计出来的装机量只包含挂了代理的用户。
- 原因：被污染的是这个公共后缀本身，不是某个 IP，换解析地址或写 hosts 都无效。
- 后果：自建域名（:data:`TELEMETRY_ENDPOINT`）才是不受影响的首选入口；旧地址必须
  留在候选里，因为 0.1.0–0.1.2 的存量装机只认它，删掉等于那些装机的心跳全部落空。

**遥测绝不能影响正常使用**：全部网络调用带超时，失败只记 warning，不抛、不重试
到阻塞、不进任何功能路径；启动失败不影响其余服务装配。

被 ``src.main`` 在生命周期里注册，与其它后台服务同一形态。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple, Union

import asyncio
import json
import platform

import httpx

from src.core.app_meta import APP_VERSION
from src.core.logging.logger import get_logger

logger = get_logger(__name__)

# 服务端首选地址，不带尾斜杠。自建域名走 zone 自己的权威 DNS，不受 workers.dev
# 那层污染影响，所以新装机打的是它。
#
# 这个值一旦发布就等于定死：已经发出去的版本只认自己写死的那一个地址，改它不会
# 让老装机跟过来，只能等用户升级（见下方 TELEMETRY_ENDPOINT_LEGACY）。服务端
# 实现见 telemetry-server/。
TELEMETRY_ENDPOINT = 'https://telemetry.yuelibot.org'

# 历史地址。0.1.0–0.1.2 的装机只认它，因此它必须一直可解析、一直保留在候选里，
# 否则已发布的客户端心跳全部落空，历史装机量不再增长。
TELEMETRY_ENDPOINT_LEGACY = 'https://yueli-telemetry.yuelibot.workers.dev'

# 端点候选，按尝试顺序排列：首选在前，历史地址兜底。
TELEMETRY_ENDPOINTS: Tuple[str, ...] = (TELEMETRY_ENDPOINT, TELEMETRY_ENDPOINT_LEGACY)

# 身份文件名，落在数据目录下。
IDENTITY_FILENAME = 'telemetry.json'
# 心跳间隔（秒）。十分钟一次，与「在线」窗口的口径配套。
HEARTBEAT_INTERVAL_S = 600.0
# 单次请求超时（秒）。遥测不值得为它多等，超时即放弃本轮。
REQUEST_TIMEOUT_S = 10.0
# 一轮上报全部失败后的重试间隔（秒）。600 秒的常规节拍在首次注册就失败时太慢：
# 只开机跑几分钟的安装，整段使用期都不会出现在统计里。
RETRY_INTERVAL_S = 30.0
# 连续失败时重试间隔的放大倍数与上限（秒）。退避是为了不把网络不通变成每 30 秒
# 一次的固定打点，上限则保证网络恢复后最多等两分钟就能上报。
RETRY_BACKOFF_FACTOR = 2.0
MAX_RETRY_INTERVAL_S = 120.0


def _os_type() -> str:
    """把 ``platform.system()`` 归一成三档，未知系统原样返回。

    :return: ``Windows`` / ``Linux`` / ``macOS`` 之一，或原始系统名。
    """
    system = platform.system()
    return 'macOS' if system == 'Darwin' else system


def payload() -> Dict[str, str]:
    """组装上报载荷。

    固定三个字段，这是 2026-09-04 拍板的范围。**加字段容易，减字段要重新征求
    同意**——要动这里之前先确认协议正文与配置注释是否同步。

    :return: 应用版本、系统类型与 Python 版本。
    """
    return {
        'app_version': APP_VERSION,
        'os_type': _os_type(),
        'python_version': platform.python_version(),
    }


def identity_path(data_dir: Path) -> Path:
    """给出身份文件路径。

    :param data_dir: 运行时数据目录。
    :return: ``<data_dir>/telemetry.json``；文件是否存在不在此校验。
    """
    return data_dir / IDENTITY_FILENAME


def read_identity(data_dir: Path) -> str | None:
    """读取已注册的 UUID。

    :param data_dir: 运行时数据目录。
    :return: UUID 字符串；未注册、文件损坏或字段缺失时返回 ``None``。
        损坏按未注册处理——重注册的代价只是统计上多一个安装，
        而报错会把遥测的失败带进启动路径。
    """
    path = identity_path(data_dir)
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(document, dict):
        return None
    uuid = document.get('uuid')
    return uuid if isinstance(uuid, str) and uuid.strip() else None


def write_identity(data_dir: Path, uuid: str) -> None:
    """落盘服务端下发的 UUID。

    :param data_dir: 运行时数据目录；不存在时递归创建。
    :param uuid: 服务端返回的标识。
    :raises OSError: 目录或文件无法写入。
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    identity_path(data_dir).write_text(
        json.dumps({'uuid': uuid}, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )


def next_retry_interval(wait_s: float) -> float:
    """给出一轮全部失败后的等待时长。

    常规节拍与退避节拍是两套刻度：首次失败必须立刻从 :data:`HEARTBEAT_INTERVAL_S`
    落回 :data:`RETRY_INTERVAL_S`（先把间隔除下来会被上限接住，变成一直停在
    :data:`MAX_RETRY_INTERVAL_S`，等于没缩短）；之后按
    :data:`RETRY_BACKOFF_FACTOR` 放大，并在 :data:`MAX_RETRY_INTERVAL_S` 处封顶。

    :param wait_s: 上一轮的等待时长，单位秒，初始为 :data:`HEARTBEAT_INTERVAL_S`。
    :return: 下一轮等待时长，单位秒，落在
        [:data:`RETRY_INTERVAL_S`, :data:`MAX_RETRY_INTERVAL_S`] 区间内。
    """
    if wait_s > MAX_RETRY_INTERVAL_S:
        return RETRY_INTERVAL_S
    return min(wait_s * RETRY_BACKOFF_FACTOR, MAX_RETRY_INTERVAL_S)


class TelemetryService:
    """按固定间隔上报三个字段的后台服务。"""

    def __init__(
        self,
        data_dir: Path,
        *,
        enabled: bool,
        endpoint: Union[str, None] = None,
    ) -> None:
        """保存运行参数，不做任何网络动作。

        :param data_dir: 运行时数据目录，身份文件落在这里。
        :param enabled: 配置开关；关闭时整条链路不启动。
        :param endpoint: 显式指定单个服务端根地址，测试与自建部署从这一口注入；``''``
            表示服务端尚未立起，保持惰性。缺省取 :data:`TELEMETRY_ENDPOINTS` 的
            主备候选顺序，此时任一地址可用即可上报。
        副作用：只保存引用，不建立连接也不读文件。
        """
        self._data_dir = data_dir
        self._enabled = enabled
        self._endpoints = resolve_endpoints(endpoint)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def active(self) -> bool:
        """判断本轮是否真的会上报。

        :return: 开关打开且至少有一个非空端点时为真。两者任一不成立都保持惰性。
        """
        return self._enabled and bool(self._endpoints)

    async def startup(self) -> None:
        """启动心跳任务。

        必须立即返回：把循环本身注册成启动钩子会让生命周期永久 await，
        其后的服务全部起不来。

        :return: ``None``。
        副作用：``active`` 为真时创建名为 ``telemetry-heartbeat`` 的后台任务。
        """
        if not self.active:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name='telemetry-heartbeat')

    async def shutdown(self) -> None:
        """停止心跳任务并等待它退出。

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
        except (asyncio.CancelledError, Exception):  # noqa: B014 - 收尾不因遥测失败而中断
            pass

    async def _loop(self) -> None:
        """按间隔上报，直到收到停止信号。

        失败后间隔先压到 :data:`RETRY_INTERVAL_S`，再按 :data:`RETRY_BACKOFF_FACTOR`
        逐轮放大到 :data:`MAX_RETRY_INTERVAL_S`（30 → 60 → 120 → 120…）；任一成功即
        退回 :data:`HEARTBEAT_INTERVAL_S`。重试次数不受限制，只放大等待，网络恢复后
        最迟两分钟就能接回来。

        首次上报失败后必须先缩短间隔：常规的 600 秒节拍意味着只开机跑几分钟的安装，
        整段使用期都不会出现在统计里，而这正是「新装机统计不到」的主要形态。
        """
        wait_s = HEARTBEAT_INTERVAL_S
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait_s)
                continue
            except asyncio.TimeoutError:
                pass
            wait_s = HEARTBEAT_INTERVAL_S if await self._beat_once() else next_retry_interval(wait_s)

    async def _beat_once(self) -> bool:
        """走一轮「必要时注册 + 心跳」，按候选顺序逐个端点尝试。

        每个端点各自完成「注册 + 心跳」，任一成功即结束本轮，失败才轮到下一个。
        全部失败只记 warning，不抛异常。

        :return: 至少一个端点成功时为真；开关关闭、无端点或全部失败时为假。
        """
        if not self.active:
            return False
        failures: List[str] = []
        rejected = False
        for endpoint in self._endpoints:
            try:
                ok, identity_rejected = await self._attempt(endpoint)
            except Exception as exc:  # noqa: BLE001 - 遥测不得把异常带进任何功能路径
                # 记异常类型：连接类失败（DNS 污染、断网）的 str(exc) 常常是空串，
                # 只记 error 会让远程用户报「统计不到」时无据可查。
                failures.append(endpoint)
                logger.warning(
                    'telemetry_failed',
                    kind=type(exc).__name__,
                    endpoint=endpoint,
                    error=str(exc),
                )
                continue
            if ok:
                return True
            rejected = rejected or identity_rejected
        if not failures:
            # 端点没抛异常却也没成功，只有两种可能：服务端不认这个 UUID（403），
            # 或者注册回来了一个空 UUID。前者删本地身份，下一轮重新注册——清库或
            # 重建数据库后靠这条路径把老客户端拉回统计范围；后者只记日志。
            if rejected:
                identity_path(self._data_dir).unlink(missing_ok=True)
                logger.warning('telemetry_identity_rejected', endpoints=list(self._endpoints))
            else:
                logger.warning('telemetry_register_no_uuid')
        return False

    async def _attempt(self, endpoint: str) -> Tuple[bool, bool]:
        """在单个端点上完成一次注册与心跳。

        :param endpoint: 服务端根地址，不带尾斜杠。
        :return: ``(是否成功, 是否被判为未知身份)``。身份被拒时调用方会在所有端点都
            走完之后才删本地文件：另一个端点连的可能是另一套库，只有全都认不出这个
            UUID 才算真的失效。
        :raises Exception: 网络与解析错误原样向上抛，由 :meth:`_beat_once` 记日志。
        """
        uuid = read_identity(self._data_dir) or await self._register(endpoint)
        if uuid is None:
            return False, False
        return await self._heartbeat(endpoint, uuid), False

    async def _register(self, endpoint: str) -> str | None:
        """向服务端申请一个 UUID 并落盘。

        首次启动没网时这一步会失败，返回 ``None``；:meth:`_loop` 会按退避后的间隔
        在下几轮继续尝试，不做阻塞重试，也不影响任何功能路径。

        :param endpoint: 服务端根地址，不带尾斜杠。
        :return: 新的 UUID；注册失败时为 ``None``。
        """
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            response = await client.post(f'{endpoint}/register', json={})
            response.raise_for_status()
            uuid = response.json().get('uuid')
        if not isinstance(uuid, str) or not uuid.strip():
            logger.warning('telemetry_register_no_uuid', endpoint=endpoint)
            return None
        write_identity(self._data_dir, uuid)
        # 首次注册成功要留痕：排查「新装机没进统计」时，这行日志与「有没有
        # telemetry.json」合起来就能把问题定位到客户端还是服务端。
        logger.info('telemetry_registered', endpoint=endpoint)
        return uuid

    async def _heartbeat(self, endpoint: str, uuid: str) -> bool:
        """发一次心跳。

        :param endpoint: 服务端根地址，不带尾斜杠。
        :param uuid: 当前安装的标识。
        :return: 服务端接受时为真；服务端认不出这个 UUID（403）时为假，由调用方决定
            是否丢弃本地身份。

        :raises httpx.HTTPError: 网络失败与非 204 响应原样向上抛。
        """
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            response = await client.post(
                f'{endpoint}/heartbeat',
                headers={'Client-UUID': uuid},
                json=payload(),
            )
        if response.status_code == 403:
            return False
        response.raise_for_status()
        return True


def resolve_endpoints(endpoint: Union[str, Sequence[str], None]) -> Tuple[str, ...]:
    """把「单地址 / 多地址 / 未指定」归一成候选端点序列。

    :param endpoint: 显式端点。``None`` 表示按 :data:`TELEMETRY_ENDPOINTS` 的主备
        顺序；字符串按单个端点处理，空串解析为空序列（整条链路保持惰性）；序列按
        给定的顺序逐个尝试。
    :return: 去空白、去空项后的端点元组；调用方据其是否为空判断链路是否惰性。
    """
    if endpoint is None:
        candidates: Sequence[str] = TELEMETRY_ENDPOINTS
    elif isinstance(endpoint, str):
        candidates = (endpoint,)
    else:
        candidates = endpoint
    return tuple(item.strip().rstrip('/') for item in candidates if item.strip())


def describe_for_console(
    enabled: bool,
    endpoint: Union[str, Sequence[str], None] = None,
) -> list[str]:
    """给出首次启动时在控制台明说的那几行。

    默认开启意味着必须当面讲清楚：传什么、怎么关。埋在文档里不算说过。

    :param enabled: 配置开关的当前值。
    :param endpoint: 实际使用的端点，语义见 :func:`resolve_endpoints`；解析为空表示
        服务端尚未启用。
    :return: 供信息框呈现的文本行。
    """
    if not enabled:
        return ['匿名统计：已关闭。']
    endpoints = resolve_endpoints(endpoint)
    fields = '、'.join(f'{key}={value}' for key, value in payload().items())
    rows = [
        '匿名统计：默认开启，用于统计全球有多少个安装、分别是什么版本。',
        f'  上报内容仅三项：{fields}',
        '  不含聊天内容、不含任何身份信息，也不采集 IP。',
        '  关闭方式：把 config/features.toml 的 [telemetry] enabled 改成 false。',
    ]
    if not endpoints:
        rows.append('  当前服务端尚未启用，本版本实际不会发出任何请求。')
    else:
        rows.append(f'  上报地址：{endpoints[0]}')
    return rows


__all__ = [
    'HEARTBEAT_INTERVAL_S',
    'MAX_RETRY_INTERVAL_S',
    'REQUEST_TIMEOUT_S',
    'RETRY_BACKOFF_FACTOR',
    'RETRY_INTERVAL_S',
    'TELEMETRY_ENDPOINT',
    'TELEMETRY_ENDPOINT_LEGACY',
    'TELEMETRY_ENDPOINTS',
    'TelemetryService',
    'describe_for_console',
    'identity_path',
    'next_retry_interval',
    'payload',
    'read_identity',
    'resolve_endpoints',
    'write_identity',
]
