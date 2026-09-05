"""实现开发者命令 ``/inst``：读遥测服务端的聚合数据，画折线图发到聊天里。

本模块只负责取数、出图与措辞；命令匹配、owner 鉴权与出站投递属于
``src.core.commands`` 与 ``src.core.api.http``。两者由
:func:`register_install_stats_command` 对接，它在启动期把命令注册进通道注册表。

数据来自遥测服务端的 ``GET /stats``（实现见 ``telemetry-server/src/index.js``），
访问令牌从环境变量 :data:`STATS_TOKEN_ENV` 读取——那份令牌不进 ``features.toml``，
因为该文件会被 WebUI 整体重写并展示。端点或令牌任一为空时命令**不注册**：
没有服务端就没有这条命令，而不是「有但一问就报错」。

绘图依赖 matplotlib，装在可选 extra ``chart`` 里。依赖缺失、绘图抛异常、
历史不足两天，三种情形都退回纯文字，不让 ``/inst`` 因为少装一个包而失败。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import asyncio
import os

import httpx

from src.core.commands import CommandContext, CommandReply, register_command
from src.core.logging.logger import get_logger
from src.core.runtime.telemetry import REQUEST_TIMEOUT_S, TELEMETRY_ENDPOINT

logger = get_logger(__name__)

# ``GET /stats`` 的访问令牌来源。不放配置文件：那份文件会被 WebUI 整体重写并展示。
STATS_TOKEN_ENV = 'YUELI_STATS_TOKEN'

# 图默认覆盖的天数；服务端按同名参数裁剪，不足这么多天时有几天画几天。
CHART_DAYS = 30
# 少于两个数据点画不出线，此时只发文字。
MIN_CHART_POINTS = 2
# 图尺寸：800×400 像素，由 figsize 英寸乘 dpi 得到。发到群里要一眼能读，不做成大图。
CHART_DPI = 100
CHART_FIGSIZE_INCHES = (8.0, 4.0)
# 版本折线最多画几条，其余合并成一条「其它」；线太多时图例与颜色都分辨不出来。
MAX_VERSION_LINES = 5
# 横轴最多标几个日期，避免 30 个日期挤成一片黑。
MAX_X_TICKS = 6
# 图片落盘目录名，位于运行时数据目录下。同名覆盖，不按次累积。
CHARTS_DIRNAME = 'charts'

# 子命令与它在文件名、日志里的标识一致；缺省等同于 'a'。
MODE_INSTALLS = 'd'
MODE_ONLINE = 'n'
MODE_VERSIONS = 'v'
MODE_ALL = 'a'
DEFAULT_MODE = MODE_ALL

# 版本长尾合并后的名字，与服务端 foldVersions 用的是同一个词。
OTHER_VERSION = '其它'


class StatsUnavailable(Exception):
    """取数失败。异常消息即直接发给 owner 的那句话，调用方不再改写。"""


@dataclass(frozen=True)
class DailyPoint:
    """一天的快照。

    :ivar day: UTC 日期，``YYYY-MM-DD``。
    :ivar installs: 当日累计装机量。
    :ivar online: 当日 24 小时内有心跳的实例数。
    :ivar versions: 存活实例的版本构成，版本号到实例数。
    """

    day: str
    installs: int
    online: int
    versions: Dict[str, int]


@dataclass(frozen=True)
class InstallStats:
    """``GET /stats`` 的返回值。

    :ivar installs: 当前累计装机量。
    :ivar online: 当前 24 小时内在线数。
    :ivar versions: 存活实例的版本构成，按实例数降序。
    :ivar daily: 最近若干天的快照，按日期升序；画图的唯一数据源。
    """

    installs: int
    online: int
    versions: Tuple[Tuple[str, int], ...]
    daily: Tuple[DailyPoint, ...]


def stats_token() -> str:
    """读取 ``/stats`` 的访问令牌。

    :return: 环境变量里的令牌，去除首尾空白；未设置时为空字符串。
    """
    return os.environ.get(STATS_TOKEN_ENV, '').strip()


async def fetch_stats(endpoint: str, token: str, days: int = CHART_DAYS) -> InstallStats:
    """向遥测服务端取一次聚合数据。

    :param endpoint: 服务端根地址，不带尾斜杠。
    :param token: ``Authorization: Bearer`` 用的令牌。
    :param days: 需要多少天的历史快照。
    :return: 解析后的聚合数据。
    :raises StatsUnavailable: 请求失败、超时、鉴权不通过或返回结构不可解析；
        异常消息是直接发给 owner 的完整句子。
    副作用：一次 HTTPS GET，超时 :data:`REQUEST_TIMEOUT_S` 秒；不写任何文件。
    """
    url = f'{endpoint.rstrip("/")}/stats'
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            response = await client.get(
                url,
                params={'days': days},
                headers={'Authorization': f'Bearer {token}'},
            )
    except httpx.HTTPError as exc:
        logger.warning('inst_stats_unreachable', error=str(exc))
        raise StatsUnavailable('连不上遥测服务端，稍后再试。') from exc
    if response.status_code == 401:
        raise StatsUnavailable('STATS_TOKEN 不对，检查配置。')
    if response.status_code != 200:
        logger.warning('inst_stats_bad_status', status=response.status_code)
        raise StatsUnavailable('连不上遥测服务端，稍后再试。')
    try:
        return parse_stats(response.json())
    except (ValueError, TypeError) as exc:
        logger.warning('inst_stats_unparsable', error=str(exc))
        raise StatsUnavailable('连不上遥测服务端，稍后再试。') from exc


def parse_stats(document: Any) -> InstallStats:
    """把 ``GET /stats`` 的 JSON 转成 :class:`InstallStats`。

    对结构不做宽容处理：字段缺失或类型不对直接抛，让问题暴露在取数这一层，
    而不是留到画图时变成一张空图。

    :param document: 已解析的 JSON。
    :return: 聚合数据。
    :raises ValueError: 顶层不是对象，或必需字段缺失、类型不符。
    """
    if not isinstance(document, dict):
        raise ValueError('/stats 返回的顶层不是对象')
    versions: List[Tuple[str, int]] = []
    for item in _required_list(document, 'versions'):
        if not isinstance(item, dict):
            raise ValueError('/stats 的 versions 元素不是对象')
        versions.append((_required_str(item, 'version'), _required_int(item, 'count')))
    daily: List[DailyPoint] = []
    for item in _required_list(document, 'daily'):
        if not isinstance(item, dict):
            raise ValueError('/stats 的 daily 元素不是对象')
        daily.append(DailyPoint(
            day=_required_str(item, 'day'),
            installs=_required_int(item, 'installs'),
            online=_required_int(item, 'online'),
            versions=_version_map(item.get('versions')),
        ))
    return InstallStats(
        installs=_required_int(document, 'installs'),
        online=_required_int(document, 'online'),
        versions=tuple(versions),
        daily=tuple(daily),
    )


def summary_text(mode: str, stats: InstallStats) -> str:
    """给出与图配套的那行文字。

    这行在正常出图时也要发，所以它不是「降级文案」而是常规输出的一半：
    图看趋势，文字给当前的准确数字。

    :param mode: 子命令标识，见 :data:`MODE_INSTALLS` 等四个常量。
    :param stats: 聚合数据。
    :return: 单行（``a`` 为两行）中文文本。
    """
    installs = f'装机量 {stats.installs}'
    online = f'24 小时内在线 {stats.online}'
    if mode == MODE_INSTALLS:
        return installs
    if mode == MODE_ONLINE:
        return online
    if mode == MODE_VERSIONS:
        return _versions_line(stats.versions)
    return f'{installs}；{online}\n{_versions_line(stats.versions)}'


def _versions_line(versions: Sequence[Tuple[str, int]]) -> str:
    """把版本构成写成一行：``版本分布：0.1.0 占 63%（812）、…``。

    百分比以存活实例总数为分母。没有任何存活实例时给出明确的空态描述，
    不返回一个只有冒号的半句话。
    """
    total = sum(count for _version, count in versions)
    if total <= 0:
        return '版本分布：暂无存活实例'
    parts = [
        f'{version} 占 {round(count * 100 / total)}%（{count}）'
        for version, count in versions
    ]
    return '版本分布：' + '、'.join(parts)


def chart_modes(mode: str) -> Tuple[str, ...]:
    """把子命令展开成要画的图。

    :param mode: 子命令标识。
    :return: ``a`` 展开为三张图，其余各为一张。
    """
    if mode == MODE_ALL:
        return (MODE_INSTALLS, MODE_ONLINE, MODE_VERSIONS)
    return (mode,)


def render_charts(mode: str, stats: InstallStats, charts_dir: Path) -> Tuple[str, ...]:
    """画图并落盘，返回图片路径。

    三种情形返回空元组而不是抛异常，由调用方退回纯文字：可选依赖没装、
    历史不足两天、绘图过程出错。**少装一个可选依赖不该让命令失败**。

    :param mode: 子命令标识。
    :param stats: 聚合数据。
    :param charts_dir: 图片目录；不存在时递归创建。
    :return: 按发送顺序排列的 PNG 绝对路径；不出图时为空元组。
    副作用：写入 ``<charts_dir>/inst-<模式>.png``，同名覆盖。
    """
    if len(stats.daily) < MIN_CHART_POINTS:
        return ()
    try:
        pyplot = _load_pyplot()
    except ImportError:
        logger.info('inst_chart_skipped', reason='matplotlib 未安装')
        return ()
    try:
        charts_dir.mkdir(parents=True, exist_ok=True)
        paths: List[str] = []
        for single in chart_modes(mode):
            paths.append(str(_draw_one(pyplot, single, stats, charts_dir)))
        return tuple(paths)
    except Exception as exc:  # noqa: BLE001 - 出图失败退回文字，不影响命令本身
        logger.warning('inst_chart_failed', mode=mode, error=str(exc))
        return ()


def _load_pyplot() -> Any:
    """载入 matplotlib 的 pyplot，并锁定无界面后端。

    :return: ``matplotlib.pyplot`` 模块。
    :raises ImportError: 可选依赖 ``chart`` 未安装。

    必须在导入 pyplot 之前调用 ``use('Agg')``：
    - 现象：默认后端会尝试连接显示服务，在无头进程里抛 ``ImportError`` 或直接卡住。
    - 原因：后端一旦随 pyplot 导入确定就不能再切换。
    - 后果：顺序写反时，出图会在服务器与 Windows 服务两种环境下表现不同，难以复现。
    """
    import matplotlib

    matplotlib.use('Agg')
    from matplotlib import pyplot

    _apply_chinese_font(matplotlib)
    return pyplot


def _apply_chinese_font(matplotlib: Any) -> None:
    """把 rcParams 的无衬线字体换成系统上可用的中文字体。

    - 现象：不设置时图上的中文渲染成空心方框。
    - 原因：matplotlib 默认字体族不含中文字形。
    - 后果：图里的「其它」一项无法辨认；纯 ASCII 的版本号不受影响，
      所以找不到中文字体时只记一条日志，不阻止出图。
    """
    from matplotlib import font_manager

    available = {font.name for font in font_manager.fontManager.ttflist}
    for candidate in ('Microsoft YaHei', 'SimHei', 'Noto Sans CJK SC', 'Source Han Sans SC', 'PingFang SC'):
        if candidate in available:
            matplotlib.rcParams['font.sans-serif'] = [candidate]
            # 中文字体多半不含 U+2212，负号会掉字形；退回 ASCII 连字符。
            matplotlib.rcParams['axes.unicode_minus'] = False
            return
    logger.info('inst_chart_no_cjk_font')


def _draw_one(pyplot: Any, mode: str, stats: InstallStats, charts_dir: Path) -> Path:
    """画一张图并保存。

    :param pyplot: 已确定后端的 pyplot 模块。
    :param mode: 单张图的模式，只接受 ``d`` / ``n`` / ``v``。
    :param stats: 聚合数据。
    :param charts_dir: 图片目录，调用方保证已存在。
    :return: 落盘后的 PNG 路径。

    图上不写标题、不加图例框，每条线在末端直接标名字——发到群里的图要一眼能读，
    图例框会把本就不大的画布再切掉一角。
    """
    days = [point.day for point in stats.daily]
    if mode == MODE_INSTALLS:
        series: List[Tuple[str, List[int]]] = [('装机量', [p.installs for p in stats.daily])]
    elif mode == MODE_ONLINE:
        series = [('在线', [p.online for p in stats.daily])]
    else:
        series = version_series(stats.daily)

    figure, axes = pyplot.subplots(figsize=CHART_FIGSIZE_INCHES, dpi=CHART_DPI)
    try:
        positions = list(range(len(days)))
        for name, values in series:
            axes.plot(positions, values, marker='', linewidth=1.8)
            # 末端标名字代替图例：线本身已经区分了颜色，名字贴在它旁边最省视线。
            axes.annotate(
                name,
                xy=(positions[-1], values[-1]),
                xytext=(4, 0),
                textcoords='offset points',
                va='center',
                fontsize=9,
            )
        axes.set_xticks(_tick_positions(len(days)))
        axes.set_xticklabels([days[index] for index in _tick_positions(len(days))], fontsize=8)
        axes.set_ylim(bottom=0)
        axes.grid(True, axis='y', linewidth=0.4, alpha=0.4)
        axes.spines['top'].set_visible(False)
        axes.spines['right'].set_visible(False)
        # 右侧留白给末端的线名，否则名字会被画布边缘截断。
        figure.subplots_adjust(right=0.86)
        path = charts_dir / f'inst-{mode}.png'
        figure.savefig(path, format='png')
    finally:
        pyplot.close(figure)
    return path


def version_series(daily: Sequence[DailyPoint]) -> List[Tuple[str, List[int]]]:
    """把逐日的版本构成整理成若干条折线。

    只画**最新一天存活数大于零**的版本：已经没人在用的版本，它的线只会贴着零轴
    走到头，除了占位置什么也没说。存活版本超过 :data:`MAX_VERSION_LINES` 条时，
    按最新一天的存活数取前几名，其余合并成一条「其它」。

    :param daily: 按日期升序的快照序列，至少一项。
    :return: ``[(版本名, 逐日数值), ...]``；没有任何存活版本时为空列表。
    """
    if not daily:
        return []
    latest = daily[-1].versions
    alive = sorted(
        (version for version, count in latest.items() if count > 0),
        key=lambda version: (-latest[version], version),
    )
    if not alive:
        return []
    drawn = alive[:MAX_VERSION_LINES]
    folded = alive[MAX_VERSION_LINES:]
    series = [
        (version, [point.versions.get(version, 0) for point in daily])
        for version in drawn
    ]
    if folded:
        series.append((
            OTHER_VERSION,
            [sum(point.versions.get(version, 0) for version in folded) for point in daily],
        ))
    return series


def _tick_positions(count: int) -> List[int]:
    """在 ``count`` 个数据点上挑不超过 :data:`MAX_X_TICKS` 个刻度位置。

    :param count: 数据点个数，至少为 1。
    :return: 升序的下标列表，首尾两天一定在内。
    """
    if count <= MAX_X_TICKS:
        return list(range(count))
    step = (count - 1) / (MAX_X_TICKS - 1)
    return sorted({round(index * step) for index in range(MAX_X_TICKS)})


async def handle_inst(
    mode: str,
    *,
    endpoint: str,
    token: str,
    charts_dir: Path,
) -> CommandReply:
    """处理一次 ``/inst``：取数、出图、组织回复。

    :param mode: 子命令标识；``None`` 由调用方替换为 :data:`DEFAULT_MODE`。
    :param endpoint: 遥测服务端根地址。
    :param token: ``/stats`` 的访问令牌。
    :param charts_dir: 图片落盘目录。
    :return: 文字必有，图片按情况附带；取数失败时只返回那句失败说明。
    副作用：一次 HTTPS GET；成功且能出图时写入 PNG 文件。
    """
    try:
        stats = await fetch_stats(endpoint, token)
    except StatsUnavailable as exc:
        return CommandReply(text=str(exc))
    # 绘图是纯 CPU 的阻塞调用，放线程里跑，避免三张图把事件循环占住。
    images = await asyncio.to_thread(render_charts, mode, stats, charts_dir)
    return CommandReply(text=summary_text(mode, stats), image_refs=images)


def register_install_stats_command(
    data_dir: Path,
    *,
    endpoint: str = TELEMETRY_ENDPOINT,
    token: str | None = None,
) -> bool:
    """在服务端可用时把 ``/inst`` 注册进开发者命令通道。

    端点或令牌任一为空就**不注册**：没有服务端就没有这条命令，比「注册了但一问
    就报错」诚实，也不会在 ``/help`` 里挂一条注定失败的条目。

    :param data_dir: 运行时数据目录，图片落在其下的 ``charts/``。
    :param endpoint: 遥测服务端根地址；缺省取 :data:`TELEMETRY_ENDPOINT`。
    :param token: ``/stats`` 令牌；缺省从环境变量读取。
    :return: 是否完成注册。
    :raises ValueError: 命令名重复注册（同一进程内重复调用本函数）。
    副作用：向命令通道的进程内注册表追加一条命令。
    """
    resolved_token = stats_token() if token is None else token.strip()
    resolved_endpoint = endpoint.strip().rstrip('/')
    if not resolved_endpoint or not resolved_token:
        return False
    charts_dir = data_dir / CHARTS_DIRNAME

    @register_command(
        name='/inst',
        pattern=r'/inst(?:\s+(?P<mode>[dnva]))?',
        # 参数写别的（如 /inst x）不匹配，按通道既有行为落回普通聊天，
        # 不返回「参数错误」——那等于向群里宣告存在一套隐藏命令。
        description='匿名安装统计：d 装机数、n 在线数、v 版本分布、a 三者合一（默认）',
    )
    async def _inst(context: CommandContext) -> CommandReply:
        """把捕获的子命令交给处理函数；未给参数时走默认模式。"""
        return await handle_inst(
            context.match.group('mode') or DEFAULT_MODE,
            endpoint=resolved_endpoint,
            token=resolved_token,
            charts_dir=charts_dir,
        )

    return True


def _required_list(document: Mapping[str, Any], key: str) -> List[Any]:
    """取出必须是数组的字段。

    :raises ValueError: 字段缺失或不是数组。
    """
    value = document.get(key)
    if not isinstance(value, list):
        raise ValueError(f'/stats 的 {key} 必须是数组')
    return value


def _required_str(document: Mapping[str, Any], key: str) -> str:
    """取出必须是非空字符串的字段。

    :raises ValueError: 字段缺失、类型不符或为空串。
    """
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f'/stats 的 {key} 必须是非空字符串')
    return value


def _required_int(document: Mapping[str, Any], key: str) -> int:
    """取出必须是整数的字段。

    布尔值被排除：``bool`` 是 ``int`` 的子类，不挡住会让 ``true`` 静默变成 1。

    :raises ValueError: 字段缺失或类型不符。
    """
    value = document.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f'/stats 的 {key} 必须是整数')
    return value


def _version_map(value: Any) -> Dict[str, int]:
    """把一天的版本构成转成 ``{版本: 实例数}``。

    :raises ValueError: 不是对象，或值不是整数。
    """
    if not isinstance(value, dict):
        raise ValueError('/stats 的 daily.versions 必须是对象')
    result: Dict[str, int] = {}
    for version, count in value.items():
        if not isinstance(count, int) or isinstance(count, bool):
            raise ValueError('/stats 的 daily.versions 值必须是整数')
        result[str(version)] = count
    return result


__all__ = [
    'CHART_DAYS',
    'DailyPoint',
    'InstallStats',
    'MODE_ALL',
    'MODE_INSTALLS',
    'MODE_ONLINE',
    'MODE_VERSIONS',
    'STATS_TOKEN_ENV',
    'StatsUnavailable',
    'chart_modes',
    'fetch_stats',
    'handle_inst',
    'parse_stats',
    'register_install_stats_command',
    'render_charts',
    'stats_token',
    'summary_text',
    'version_series',
]
