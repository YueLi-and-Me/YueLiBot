"""开发者命令 /inst 的验收：取数、措辞、出图与注册门。

覆盖三条线：
1. ``GET /stats`` 的解析与失败分类——两种失败各有各的话，不笼统报错。
2. 折线数据的口径——只画最新一天还有存活实例的版本，长尾并成一条。
3. 三种退回文字的情形——依赖没装、历史不足两天、绘图抛异常，都不让命令失败。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest

from src.core.commands import CommandReply
from src.core.services.dev import install_stats
from src.core.services.dev.install_stats import (
    DailyPoint,
    InstallStats,
    MODE_ALL,
    MODE_INSTALLS,
    MODE_ONLINE,
    MODE_VERSIONS,
    StatsUnavailable,
    chart_modes,
    fetch_stats,
    handle_inst,
    parse_stats,
    register_install_stats_command,
    render_charts,
    summary_text,
    version_series,
)


def _document(daily: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    """构造一份形如服务端返回的 /stats 文档。"""
    return {
        'installs': 1284,
        'online': 137,
        'versions': [
            {'version': '0.1.0', 'count': 812},
            {'version': '0.0.9', 'count': 341},
            {'version': '其它', 'count': 131},
        ],
        'daily': [] if daily is None else daily,
    }


def _days(count: int) -> List[Dict[str, Any]]:
    """造 count 天的快照，装机量与在线数逐日递增。"""
    return [
        {
            'day': f'2026-09-{index + 1:02d}',
            'installs': 1000 + index * 10,
            'online': 100 + index,
            'versions': {'0.1.0': 60 + index, '0.0.9': 40 - index},
        }
        for index in range(count)
    ]


def _stats(daily_count: int = 0) -> InstallStats:
    """解析出一份 :class:`InstallStats`，daily_count 决定历史长度。"""
    return parse_stats(_document(_days(daily_count)))


def test_解析保留服务端给出的版本顺序与逐日构成() -> None:
    """versions 按服务端的降序原样保留，daily 的 versions 转成整数映射。"""
    stats = _stats(daily_count=3)

    assert stats.installs == 1284
    assert stats.online == 137
    assert stats.versions[0] == ('0.1.0', 812)
    assert [point.day for point in stats.daily] == ['2026-09-01', '2026-09-02', '2026-09-03']
    assert stats.daily[2].versions == {'0.1.0': 62, '0.0.9': 38}


@pytest.mark.parametrize('mutate', [
    lambda doc: doc.pop('installs'),
    lambda doc: doc.update(installs=True),
    lambda doc: doc.update(installs='1284'),
    lambda doc: doc.update(versions={}),
    lambda doc: doc.update(daily=[{'day': '2026-09-01'}]),
    lambda doc: doc.update(versions=[{'version': '0.1.0'}]),
])
def test_结构不符直接抛而不是画一张空图(mutate: Any) -> None:
    """字段缺失或类型不对止步于解析层，不留到画图时变成静默的空图。

    布尔值单独一条：``bool`` 是 ``int`` 的子类，不挡住会让 ``true`` 变成 1。
    """
    document = _document(_days(2))
    mutate(document)

    with pytest.raises(ValueError):
        parse_stats(document)


def test_四种子命令各说各的话() -> None:
    """d/n/v 各一行，a 是「两数合一行 + 版本分布」两行。"""
    stats = _stats()

    assert summary_text(MODE_INSTALLS, stats) == '装机量 1284'
    assert summary_text(MODE_ONLINE, stats) == '24 小时内在线 137'
    assert summary_text(MODE_VERSIONS, stats) == (
        '版本分布：0.1.0 占 63%（812）、0.0.9 占 27%（341）、其它 占 10%（131）'
    )
    assert summary_text(MODE_ALL, stats) == (
        '装机量 1284；24 小时内在线 137\n'
        '版本分布：0.1.0 占 63%（812）、0.0.9 占 27%（341）、其它 占 10%（131）'
    )


def test_没有存活实例时版本分布说得出口() -> None:
    """分母为零不能除，也不能返回一个只剩冒号的半句话。"""
    stats = parse_stats({'installs': 3, 'online': 0, 'versions': [], 'daily': []})

    assert summary_text(MODE_VERSIONS, stats) == '版本分布：暂无存活实例'


def test_a_展开成三张图其余各一张() -> None:
    """默认形态发三张，不合成一张。"""
    assert chart_modes(MODE_ALL) == (MODE_INSTALLS, MODE_ONLINE, MODE_VERSIONS)
    assert chart_modes(MODE_VERSIONS) == (MODE_VERSIONS,)


def test_版本折线只画最新一天还有人用的版本() -> None:
    """已经归零的版本不画：它的线只会贴着零轴走到头，除了占位置什么也没说。"""
    daily = [
        DailyPoint('2026-09-01', 10, 8, {'0.0.9': 8, '0.1.0': 0}),
        DailyPoint('2026-09-02', 12, 9, {'0.0.9': 0, '0.1.0': 9}),
    ]

    series = version_series(daily)

    assert [name for name, _values in series] == ['0.1.0']
    assert series[0][1] == [0, 9]


def test_存活版本超过五个时长尾并成一条其它() -> None:
    """前五按最新一天的存活数取，其余逐日求和成一条线。"""
    latest = {f'0.{index}.0': index + 1 for index in range(7)}
    daily = [
        DailyPoint('2026-09-01', 10, 8, {key: 1 for key in latest}),
        DailyPoint('2026-09-02', 12, 9, latest),
    ]

    series = version_series(daily)
    names = [name for name, _values in series]

    assert names == ['0.6.0', '0.5.0', '0.4.0', '0.3.0', '0.2.0', install_stats.OTHER_VERSION]
    # 落到「其它」的是 0.1.0 与 0.0.0，最新一天各 2 与 1。
    assert series[-1][1] == [2, 3]


def test_全部版本归零时不画版本图() -> None:
    """没有任何存活实例就没有线可画，交由调用方退回文字。"""
    daily = [
        DailyPoint('2026-09-01', 10, 0, {'0.0.9': 0}),
        DailyPoint('2026-09-02', 10, 0, {'0.0.9': 0}),
    ]

    assert version_series(daily) == []


def test_横轴刻度最多六个且首尾必在() -> None:
    """30 天全标会挤成一片黑；首尾两天是读图的锚点，必须保留。"""
    assert install_stats._tick_positions(4) == [0, 1, 2, 3]
    positions = install_stats._tick_positions(30)
    assert len(positions) <= install_stats.MAX_X_TICKS
    assert positions[0] == 0
    assert positions[-1] == 29


async def test_取数带上令牌与天数(monkeypatch: pytest.MonkeyPatch) -> None:
    """请求必须带 Bearer 令牌与 days 参数，否则服务端会 401 或只给默认窗口。"""
    seen: Dict[str, Any] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        seen['url'] = str(request.url)
        seen['auth'] = request.headers.get('Authorization')
        return httpx.Response(200, json=_document(_days(2)))

    _patch_transport(monkeypatch, _handler)

    stats = await fetch_stats('https://example.invalid/', 'token-1', days=7)

    assert seen['auth'] == 'Bearer token-1'
    assert seen['url'] == 'https://example.invalid/stats?days=7'
    assert stats.installs == 1284


@pytest.mark.parametrize(('status', 'expected'), [
    (401, 'STATS_TOKEN 不对，检查配置。'),
    (500, '连不上遥测服务端，稍后再试。'),
])
async def test_失败各有各的话(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    expected: str,
) -> None:
    """401 是令牌问题、其余是连通性问题，两者的处置动作不同，不能合并成一句。"""

    async def _handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(status, json={})

    _patch_transport(monkeypatch, _handler)

    with pytest.raises(StatsUnavailable) as excinfo:
        await fetch_stats('https://example.invalid', 'token-1')
    assert str(excinfo.value) == expected


async def test_连不上时不抛原始异常(monkeypatch: pytest.MonkeyPatch) -> None:
    """网络层异常要换成给人看的句子，不能把 httpx 的英文报错发进群里。"""

    async def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError('nope', request=request)

    _patch_transport(monkeypatch, _handler)

    with pytest.raises(StatsUnavailable) as excinfo:
        await fetch_stats('https://example.invalid', 'token-1')
    assert str(excinfo.value) == '连不上遥测服务端，稍后再试。'


def test_历史不足两天不画图(tmp_path: Path) -> None:
    """一个点连不成线，直接不出图，由调用方只发文字。"""
    assert render_charts(MODE_ALL, _stats(daily_count=1), tmp_path / 'charts') == ()
    assert not (tmp_path / 'charts').exists()


def test_缺可选依赖时退回文字而不是报错(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chart extra 没装是常态（主依赖里没有），不该让命令失败。"""

    def _missing() -> Any:
        raise ImportError('matplotlib 未安装')

    monkeypatch.setattr(install_stats, '_load_pyplot', _missing)

    assert render_charts(MODE_ALL, _stats(daily_count=5), tmp_path / 'charts') == ()


def test_绘图抛异常也只是没有图(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """出图是锦上添花，任何失败都不该冒泡成「命令执行失败」。"""
    monkeypatch.setattr(install_stats, '_load_pyplot', lambda: object())

    assert render_charts(MODE_VERSIONS, _stats(daily_count=5), tmp_path / 'charts') == ()


def test_出图落到约定路径且同名覆盖(tmp_path: Path) -> None:
    """a 画三张，文件名与各自的子命令一致，重复执行不累积文件。"""
    pytest.importorskip('matplotlib', reason='chart extra 未安装')
    charts_dir = tmp_path / 'charts'
    stats = _stats(daily_count=5)

    first = render_charts(MODE_ALL, stats, charts_dir)
    second = render_charts(MODE_ALL, stats, charts_dir)

    assert [Path(path).name for path in first] == ['inst-d.png', 'inst-n.png', 'inst-v.png']
    assert first == second
    assert sorted(item.name for item in charts_dir.iterdir()) == [
        'inst-d.png', 'inst-n.png', 'inst-v.png',
    ]
    for path in first:
        assert Path(path).stat().st_size > 0


def test_图的尺寸是约定的八百乘四百(tmp_path: Path) -> None:
    """发到群里的图要一眼能读，尺寸是契约的一部分。"""
    pytest.importorskip('matplotlib', reason='chart extra 未安装')
    from PIL import Image

    paths = render_charts(MODE_INSTALLS, _stats(daily_count=5), tmp_path / 'charts')

    assert len(paths) == 1
    with Image.open(paths[0]) as image:
        assert image.size == (800, 400)


async def test_取数失败时只回一句话不带图(monkeypatch: pytest.MonkeyPatch) -> None:
    """连不上就说连不上，不再附一张上一次的旧图。"""

    async def _handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(503, json={})

    _patch_transport(monkeypatch, _handler)

    reply = await handle_inst(
        MODE_ALL,
        endpoint='https://example.invalid',
        token='token-1',
        charts_dir=Path('unused'),
    )

    assert isinstance(reply, CommandReply)
    assert reply.text == '连不上遥测服务端，稍后再试。'
    assert reply.image_refs == ()


async def test_成功时文字与图一起返回(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """文字不是降级文案：它给准确数字，图给趋势，两者都要有。"""
    pytest.importorskip('matplotlib', reason='chart extra 未安装')

    async def _handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=_document(_days(5)))

    _patch_transport(monkeypatch, _handler)

    reply = await handle_inst(
        MODE_ALL,
        endpoint='https://example.invalid',
        token='token-1',
        charts_dir=tmp_path / 'charts',
    )

    assert reply.text.startswith('装机量 1284；24 小时内在线 137')
    assert len(reply.image_refs) == 3


@pytest.mark.parametrize(('endpoint', 'token'), [
    ('', 'token-1'),
    ('https://example.invalid', ''),
    ('', ''),
])
def test_服务端或令牌缺一就不注册(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    token: str,
) -> None:
    """没有服务端就没有这条命令，而不是「有但一问就报错」。"""
    _isolate_registry(monkeypatch)

    assert register_install_stats_command(tmp_path, endpoint=endpoint, token=token) is False
    from src.core.commands import registered_commands

    assert [item.name for item in registered_commands()] == []


def test_两者齐备时注册出可匹配的四个子命令(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/inst 及三个子命令都要命中；写别的参数不匹配，落回普通聊天。"""
    _isolate_registry(monkeypatch)

    assert register_install_stats_command(
        tmp_path, endpoint='https://example.invalid/', token=' token-1 ',
    ) is True

    import src.core.commands.registry as registry_module

    spec = registry_module._commands[0]
    assert spec.name == '/inst'
    for text in ('/inst', '/inst d', '/inst n', '/inst v', '/inst a'):
        assert spec.pattern.fullmatch(text) is not None
    for text in ('/inst x', '/inst dd', '/instd', '/inst 7'):
        assert spec.pattern.fullmatch(text) is None


def test_令牌从环境变量读而不是配置文件(monkeypatch: pytest.MonkeyPatch) -> None:
    """features.toml 会被 WebUI 整体重写并展示，放特权令牌不合适。"""
    monkeypatch.setenv(install_stats.STATS_TOKEN_ENV, '  secret  ')

    assert install_stats.stats_token() == 'secret'

    monkeypatch.delenv(install_stats.STATS_TOKEN_ENV)
    assert install_stats.stats_token() == ''


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    """把 httpx.AsyncClient 换成走 MockTransport 的版本。

    只改传输层而不是整个 client，保证被测代码里的 URL 拼接、参数与请求头
    仍然真实地经过 httpx 组装。
    """
    original = httpx.AsyncClient

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs['transport'] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(install_stats.httpx, 'AsyncClient', _factory)


def _isolate_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """把命令注册表换成空的进程内副本。

    注册表是模块级全局状态，注册在用例之间会串味；用例结束后由 monkeypatch
    还原，不影响同一进程里其它包已经注册的命令。
    """
    import src.core.commands.registry as registry_module

    monkeypatch.setattr(registry_module, '_commands', [])
    monkeypatch.setattr(registry_module, '_command_names', set())
