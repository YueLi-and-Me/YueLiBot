"""每 15 分钟重写一次用量报告文件，并在有新调用时向控制台打印一个用量汇总框。

报告文件 ``data/reports/usage.html`` 让服务器部署（WebUI 只能走隧道）不开 WebUI 也能
看统计：单文件自带样式与 SVG 柱状图，不引任何外部资源，断网可打开，深浅色都可读；
五个时间范围都在一个文件里，用页内切换（纯 CSS，不依赖脚本）。写入先落临时文件再
改名替换，读的人不会看到写了一半的文件。文件里有群名，只存在本机。

控制台汇总框内容是本小时与今天两栏的总览，加今天按来源、按模型的前几名；距上次
打印没有新调用就不打印，避免空框刷屏。

两者的数字都来自 ``usage_stats.summarize_usage``，与 WebUI 用量统计页同一口径。

对外接口：:class:`UsageReportService`（生命周期由 ``src.main`` 注册），以及纯函数
:func:`render_usage_html`、:func:`render_console_rows`、:func:`write_report`。
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

import asyncio
import os

from src.core.llm_models.usage import usage_ledger
from src.core.llm_models.usage_stats import (
    RANGE_KEYS,
    RANGE_LABELS,
    local_tz_offset_minutes,
    model_prices,
    range_bounds,
    summarize_usage,
)
from src.core.logging.console_layout import display_width, print_box
from src.core.logging.logger import get_logger
from src.core.runtime.clock import now as current_time

logger = get_logger(__name__)

# 报告与控制台汇总的周期，单位秒：15 分钟。报告每个周期都重写（时间范围随时间推移），
# 控制台只在周期内有新调用时打印。
REPORT_INTERVAL_SECONDS = 15 * 60
# 报告文件相对数据目录的位置。
REPORT_RELATIVE_PATH = Path('reports') / 'usage.html'
# 控制台汇总里按来源、按模型各列出的前几名。
CONSOLE_TOP_N = 3
# 控制台汇总框的宽度，单位终端列。
CONSOLE_BOX_WIDTH = 100


def format_tokens(value: int) -> str:
    """token 数加千分位，如 ``12,345``。"""
    return f'{value:,}'


def format_cost(value: float) -> str:
    """把费用（元）格式化成展示文本，规则与 WebUI 的 ``formatCost`` 相同。

    0 写成 ``¥0``；不足 0.0001 元写成 ``<¥0.0001``，避免四舍五入成 0 让人以为免费；
    不足 1 元保留四位小数，其余保留两位并加千分位。

    :param value: 费用，单位元。
    :return: 带 ``¥`` 的文本。
    副作用：无。
    """
    if value == 0:
        return '¥0'
    if value < 0.0001:
        return '<¥0.0001'
    if value < 1:
        return f'¥{value:.4f}'
    return f'¥{value:,.2f}'


def format_duration(average: float | None, deviation: float | None) -> str:
    """平均耗时与波动，如 ``2.31 s ±0.42``；没有调用时为 ``—``。"""
    if average is None or deviation is None:
        return '—'
    return f'{average / 1000:.2f} s ±{deviation / 1000:.2f}'


def _pad(text: str, width: int) -> str:
    """按终端显示宽度右侧补空格。"""
    return text + ' ' * max(width - display_width(text), 0)


def _top(groups: Sequence[Mapping[str, Any]], label_key: str) -> str:
    """把分组的前几名拼成一行：名称与费用，按费用从高到低。"""
    if not groups:
        return '暂无调用'
    return ' · '.join(
        f"{group[label_key]} {format_cost(group['cost'])}" for group in groups[:CONSOLE_TOP_N]
    )


def render_console_rows(hour: Mapping[str, Any], today: Mapping[str, Any]) -> List[str]:
    """组装控制台汇总框的行：两栏总览加今天的前几名。

    :param hour: 本小时的汇总。
    :param today: 今天的汇总。
    :return: 纯文本行，交给 ``render_box`` 排版。
    副作用：无。
    """
    columns = ((RANGE_LABELS['hour'], hour), (RANGE_LABELS['today'], today))

    def cell(summary: Mapping[str, Any], field: str) -> str:
        overview = summary['overview']
        per_reply = summary['perReply']
        if field == 'calls':
            return f"{overview['calls']:,}（失败 {overview['failedCalls']} · 中断 {overview['interruptedCalls']}）"
        if field == 'input':
            return f"{format_tokens(overview['promptTokens'])}（缓存 {format_tokens(overview['cachedTokens'])}）"
        if field == 'output':
            return f"{format_tokens(overview['completionTokens'])}（思考 {format_tokens(overview['reasoningTokens'])}）"
        if field == 'cost':
            return format_cost(overview['cost'])
        if field == 'unreported':
            return f"{overview['unreportedCalls']} 次"
        if field == 'replies':
            return f"{overview['replies']:,}"
        return '—' if per_reply['cost'] is None else format_cost(per_reply['cost'])

    label_width = 14
    value_width = 40
    rows = [_pad('', label_width) + ''.join(_pad(label, value_width) for label, _ in columns)]
    for label, field in (
        ('调用次数', 'calls'),
        ('输入 token', 'input'),
        ('输出 token', 'output'),
        ('费用', 'cost'),
        ('未返回用量', 'unreported'),
        ('回复条数', 'replies'),
        ('每条回复费用', 'per_reply'),
    ):
        rows.append(_pad(label, label_width) + ''.join(
            _pad(cell(summary, field), value_width) for _, summary in columns
        ))
    rows.append('')
    rows.append(f"今天按来源：{_top(today['groups']['source'], 'label')}")
    rows.append(f"今天按模型：{_top(today['groups']['model'], 'model')}")
    return rows


_STYLE = '''
:root { color-scheme: light dark; --bg: #f7f7f8; --card: #ffffff; --text: #1d1d22; --muted: #6b6b76;
  --line: #e3e3e8; --accent: #4f6bed; --accent-soft: #e7ebfd; --warn: #b4690e; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #16161a; --card: #1f1f25; --text: #ececf1; --muted: #9d9daa; --line: #33333d;
    --accent: #8ea2ff; --accent-soft: #2a2f4d; --warn: #f0b35a; }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 24px 16px; background: var(--bg); color: var(--text);
  font: 14px/1.6 -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
main { max-width: 1120px; margin: 0 auto; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 24px 0 8px; }
.muted { color: var(--muted); font-size: 12px; }
.tabs > input { position: absolute; opacity: 0; pointer-events: none; }
.tabs > label { display: inline-block; padding: 6px 14px; margin: 16px 4px 0 0; border: 1px solid var(--line);
  border-radius: 999px; cursor: pointer; background: var(--card); }
.panel { display: none; }
''' + ''.join(
    f'#range-{key}:checked ~ label[for="range-{key}"] {{ background: var(--accent-soft); border-color: var(--accent); }}\n'
    f'#range-{key}:checked ~ #panel-{key} {{ display: block; }}\n'
    for key in RANGE_KEYS
) + '''
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 8px; margin-top: 12px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; }
.card b { display: block; font-size: 18px; font-variant-numeric: tabular-nums; }
.note { margin-top: 8px; padding: 8px 12px; border-radius: 8px; background: var(--card); border: 1px solid var(--line); }
.warn { color: var(--warn); }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; background: var(--card); border: 1px solid var(--line);
  border-radius: 10px; font-variant-numeric: tabular-nums; }
th, td { padding: 6px 10px; border-bottom: 1px solid var(--line); text-align: right; white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
th { color: var(--muted); font-weight: 500; font-size: 12px; }
tr:last-child td { border-bottom: none; }
.charts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
@media (max-width: 720px) { .charts { grid-template-columns: 1fr; } }
.chart { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; }
.chart svg { width: 100%; height: 120px; display: block; }
.chart rect { fill: var(--accent); }
.chart .axis { stroke: var(--line); }
'''


def _cards(summary: Mapping[str, Any]) -> str:
    """总览卡片与每条回复平均值。"""
    overview = summary['overview']
    per_reply = summary['perReply']
    cards = [
        ('调用次数', f"{overview['calls']:,}", f"失败 {overview['failedCalls']} · 中断 {overview['interruptedCalls']}"),
        ('输入 token', format_tokens(overview['promptTokens']), f"缓存命中 {format_tokens(overview['cachedTokens'])}"),
        ('输出 token', format_tokens(overview['completionTokens']), f"其中思考 {format_tokens(overview['reasoningTokens'])}"),
        ('费用', format_cost(overview['cost']), '按调用时生效的单价累计'),
        ('回复条数', f"{overview['replies']:,}", '她发出的消息条数'),
        (
            '每条回复',
            '—' if per_reply['cost'] is None else format_cost(per_reply['cost']),
            '—' if per_reply['tokens'] is None
            else f"{per_reply['tokens']:,.0f} token · {per_reply['calls']:.1f} 次调用",
        ),
    ]
    return '<div class="cards">' + ''.join(
        f'<div class="card"><span class="muted">{escape(title)}</span><b>{escape(value)}</b>'
        f'<span class="muted">{escape(detail)}</span></div>'
        for title, value, detail in cards
    ) + '</div>'


def _unreported_note(summary: Mapping[str, Any]) -> str:
    """成功调用中没返回用量的次数与对应模型。"""
    overview = summary['overview']
    if not overview['unreportedCalls']:
        return ''
    detail = '、'.join(
        f"{item['model']}（{item['provider']}）{item['calls']} 次" for item in summary['unreported']
    )
    return (
        f'<p class="note warn">成功调用中有 {overview["unreportedCalls"]} 次服务商没返回用量，'
        f'这部分 token 与费用不在统计里：{escape(detail)}</p>'
    )


def _group_table(title: str, groups: Sequence[Mapping[str, Any]], first_column: str, *, with_price: bool = False) -> str:
    """一张分组表：次数、token、费用、平均耗时与波动，按模型分组时附当前单价。"""
    if not groups:
        return f'<h2>{escape(title)}</h2><p class="muted">这个时间范围内没有调用。</p>'
    headers = [first_column, '调用', '失败', '中断', '输入', '输出', '缓存命中', '费用', '平均耗时']
    if with_price:
        headers.append('当前单价（入 / 出 / 缓存）')
    rows = []
    for group in groups:
        name = group['label'] if 'label' in group else f"{group['model']}（{group['provider']}）"
        cells = [
            escape(name),
            f"{group['calls']:,}",
            f"{group['failedCalls']:,}",
            f"{group['interruptedCalls']:,}",
            format_tokens(group['promptTokens']),
            format_tokens(group['completionTokens']),
            format_tokens(group['cachedTokens']),
            format_cost(group['cost']),
            format_duration(group['avgDurationMs'], group['durationStdMs']),
        ]
        if with_price:
            cells.append(_price_text(group['price']))
        rows.append('<tr>' + ''.join(f'<td>{cell}</td>' for cell in cells) + '</tr>')
    head = ''.join(f'<th>{escape(header)}</th>' for header in headers)
    return (
        f'<h2>{escape(title)}</h2><div class="table-wrap"><table><thead><tr>{head}</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )


def _price_text(price: Mapping[str, Any] | None) -> str:
    """按模型表里的当前单价；单价为 0 的标注出来，让人分清没填还是真免费。"""
    if price is None:
        return '<span class="muted">已不在当前配置中</span>'
    cache = '同输入' if price['cachePriceIn'] is None else f"{price['cachePriceIn']:g}"
    text = f"¥{price['priceIn']:g} / ¥{price['priceOut']:g} / {cache}"
    if price['periods']:
        text += f"（另有 {price['periods']} 个时段价）"
    if price['unpriced']:
        return f'<span class="warn">{escape(text)} · 单价为 0：未填写或免费</span>'
    return escape(text)


def _bar_chart(title: str, points: Sequence[Mapping[str, Any]], field: str, label_format: str) -> str:
    """手写的 SVG 柱状图：每个点一根柱，悬停显示时刻与数值。"""
    width = 480
    height = 120
    peak = max((point[field] for point in points), default=0) or 1
    step = width / max(len(points), 1)
    bars = []
    for index, point in enumerate(points):
        value = point[field]
        bar_height = value / peak * (height - 10)
        moment = datetime.fromtimestamp(point['start'] / 1000).strftime(label_format)
        shown = format_cost(value) if field == 'cost' else f'{format_tokens(value)} token'
        bars.append(
            f'<rect x="{index * step + 1:.1f}" y="{height - bar_height:.1f}" '
            f'width="{max(step - 2, 1):.1f}" height="{bar_height:.1f}">'
            f'<title>{escape(moment)}：{escape(shown)}</title></rect>'
        )
    first = datetime.fromtimestamp(points[0]['start'] / 1000).strftime(label_format) if points else ''
    last = datetime.fromtimestamp(points[-1]['start'] / 1000).strftime(label_format) if points else ''
    return (
        f'<div class="chart"><span class="muted">{escape(title)}</span>'
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img" aria-label="{escape(title)}">'
        f'<line class="axis" x1="0" y1="{height}" x2="{width}" y2="{height}"></line>{"".join(bars)}</svg>'
        f'<span class="muted">{escape(first)} → {escape(last)}</span></div>'
    )


def render_usage_html(summaries: Mapping[str, Mapping[str, Any]], generated_at: int) -> str:
    """把五个时间范围的汇总渲染成一个自包含的 HTML 文件。

    :param summaries: 时间范围名到汇总的映射，须覆盖 :data:`RANGE_KEYS`。
    :param generated_at: 生成时刻，Unix 毫秒，按本机时区显示。
    :return: 完整的 HTML 文本：内联样式与 SVG，不引用任何外部资源，不含脚本。
    副作用：读取本机时区设置。
    """
    generated = datetime.fromtimestamp(generated_at / 1000).strftime('%Y-%m-%d %H:%M')
    tabs: List[str] = []
    for key in RANGE_KEYS:
        checked = ' checked' if key == 'today' else ''
        tabs.append(f'<input type="radio" name="range" id="range-{key}"{checked}>')
    for key in RANGE_KEYS:
        tabs.append(f'<label for="range-{key}">{escape(RANGE_LABELS[key])}</label>')
    for key in RANGE_KEYS:
        summary = summaries[key]
        groups = summary['groups']
        start = datetime.fromtimestamp(summary['range']['start'] / 1000).strftime('%Y-%m-%d %H:%M')
        tabs.append(
            f'<section class="panel" id="panel-{key}">'
            f'<p class="muted">统计范围：{escape(start)} 起（{escape(RANGE_LABELS[key])}）</p>'
            + _cards(summary)
            + _unreported_note(summary)
            + _group_table('按来源', groups['source'], '来源')
            + _group_table('按任务', groups['task'], '任务')
            + _group_table('按模型', groups['model'], '模型（厂商）', with_price=True)
            + _group_table('按会话', groups['stream'], '会话')
            + '</section>'
        )
    trend = summaries['today']['trend']
    charts = ''.join([
        _bar_chart('近 48 小时 · token', trend['hourly'], 'tokens', '%m-%d %H:00'),
        _bar_chart('近 48 小时 · 费用', trend['hourly'], 'cost', '%m-%d %H:00'),
        _bar_chart('近 30 天 · token', trend['daily'], 'tokens', '%m-%d'),
        _bar_chart('近 30 天 · 费用', trend['daily'], 'cost', '%m-%d'),
    ])
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>月璃用量报告</title><style>{_STYLE}</style></head><body><main>'
        '<h1>模型用量报告</h1>'
        f'<p class="muted">生成于 {escape(generated)}，每 15 分钟更新一次。本文件含群名与私聊对象名，'
        '只保存在本机，不会上传。费用按调用时生效的单价计算，填写单价之前的调用费用为 0。</p>'
        f'<div class="tabs">{"".join(tabs)}</div>'
        f'<h2>趋势</h2><div class="charts">{charts}</div>'
        '</main></body></html>\n'
    )


def write_report(path: Path, html: str) -> None:
    """先写同目录临时文件再改名替换，读的人不会看到写了一半的报告。

    :param path: 报告文件路径。
    :param html: 完整 HTML 文本。
    :return: ``None``。
    :raises OSError: 目录创建、写入或替换失败。
    副作用：创建报告目录，写入并原子替换报告文件。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'{path.name}.tmp')
    temporary.write_text(html, encoding='utf-8')
    os.replace(temporary, path)


class UsageReportService:
    """周期性重写用量报告并打印控制台汇总。"""

    def __init__(self, data_dir: Path, routers: Any) -> None:
        """保存报告位置与模型路由引用。

        :param data_dir: 运行时数据目录；报告写到其下的 ``reports/usage.html``。
        :param routers: ``ModelRouters``，取当前单价用。
        副作用：只保存引用，不启动任务。
        """
        self._path = data_dir / REPORT_RELATIVE_PATH
        self._routers = routers
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        # 上次打印控制台汇总时账本的累计落账数；相同即表示期间没有新调用。
        self._printed_at_count = usage_ledger.recorded

    @property
    def path(self) -> Path:
        """报告文件路径。"""
        return self._path

    async def startup(self) -> None:
        """先生成一次报告，再启动周期任务。

        :return: ``None``。
        副作用：写报告文件；创建名为 ``usage-report`` 的后台任务。
        """
        self._stop.clear()
        await self.run_once()
        self._task = asyncio.create_task(self._loop(), name='usage-report')

    async def shutdown(self) -> None:
        """请求停止并等待周期任务退出。

        :return: ``None``。
        副作用：设置停止事件。
        """
        self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        """按周期重复生成，直到收到停止信号。"""
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=REPORT_INTERVAL_SECONDS)
                break
            except asyncio.TimeoutError:
                pass
            await self.run_once()

    async def run_once(self) -> None:
        """生成一次报告，有新调用时打印控制台汇总。

        报告与汇总都是旁路观测：失败只记一条带完整异常的 warning，不终止周期任务。

        :return: ``None``。
        副作用：重写报告文件；可能向控制台打印一个信息框。
        """
        recorded = usage_ledger.recorded
        try:
            summaries = await asyncio.to_thread(self._summaries, current_time())
            write_report(self._path, render_usage_html(summaries, summaries['today']['generatedAt']))
        except Exception:
            logger.warning('usage_report_failed', path=str(self._path), exc_info=True)
            return
        if recorded == self._printed_at_count:
            return
        self._printed_at_count = recorded
        print_usage_box(summaries['hour'], summaries['today'])

    def _summaries(self, now: int) -> Dict[str, Dict[str, Any]]:
        """在工作线程里算出五个时间范围的汇总。"""
        offset = local_tz_offset_minutes(now)
        prices = model_prices(self._routers.all_candidates())
        summaries: Dict[str, Dict[str, Any]] = {}
        for key in RANGE_KEYS:
            start, end = range_bounds(key, now, offset)
            summaries[key] = usage_ledger.read(lambda connection, start=start, end=end: summarize_usage(
                connection, start_ms=start, end_ms=end, tz_offset_minutes=offset, now_ms=now, prices=prices,
            ))
        return summaries


def print_usage_box(hour: Mapping[str, Any], today: Mapping[str, Any]) -> None:
    """把本小时与今天的汇总打印成一个控制台信息框。

    :param hour: 本小时的汇总。
    :param today: 今天的汇总。
    :return: ``None``。
    副作用：向标准输出与 WebUI 日志面板写一个信息框。
    """
    print_box('模型用量', render_console_rows(hour, today), width=CONSOLE_BOX_WIDTH, source=__name__)


__all__ = [
    'CONSOLE_BOX_WIDTH',
    'REPORT_INTERVAL_SECONDS',
    'REPORT_RELATIVE_PATH',
    'UsageReportService',
    'format_cost',
    'format_duration',
    'format_tokens',
    'print_usage_box',
    'render_console_rows',
    'render_usage_html',
    'write_report',
]
