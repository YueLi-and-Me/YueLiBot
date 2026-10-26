"""模型用量的统计口径：时间范围、分组汇总与趋势，WebUI 接口、报告文件与控制台共用。

只有一个汇总入口 :func:`summarize_usage`，输入时间范围与时区偏移，输出总览、每条回复
的平均值、四种分组与两条趋势；三个展示出口都调它，不各算一套。

对外接口：

- :data:`RANGE_KEYS` / :data:`RANGE_LABELS` / :func:`range_bounds`：五个时间范围的起止；
- :class:`ModelPrice` / :func:`model_prices`：按模型名取当前配置里的单价，供按模型表展示；
- :func:`task_labels`：任务中文名，取自 ``settings_schema.json`` 的模型任务条目，与 WebUI
  模型页的 ``TASK_LABELS`` 同一份文字；
- :func:`local_tz_offset_minutes`：本进程所在时区相对 UTC 的分钟偏移；
- :func:`summarize_usage`：汇总函数。

时区口径：小时桶按 UTC 整点存储；归「天」时按调用方给的时区偏移切日界。半小时时区
（如 +5:30）的日界落在两个小时桶中间，按天汇总会差 30 分钟。

依赖用量账本的表结构（``usage``）与配置模型；被 ``src.core.api.http`` 与
``src.core.services.maintenance.usage_report`` 调用。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Tuple

import math
import sqlite3

from .usage import HOUR_MS

from src.core.config.schema import ModelCandidate
from src.core.config.settings_webui import file_schema

# 一天的毫秒数。
DAY_MS = 24 * HOUR_MS
# 小时趋势覆盖的小时数（含当前小时）与天趋势覆盖的天数（含今天）。
HOURLY_TREND_HOURS = 48
DAILY_TREND_DAYS = 30

# 页面、报告与控制台共用的时间范围，顺序即展示顺序。
RANGE_KEYS: Tuple[str, ...] = ('hour', 'today', '7d', '30d', 'all')
RANGE_LABELS: Dict[str, str] = {
    'hour': '本小时',
    'today': '今天',
    '7d': '近 7 天',
    '30d': '近 30 天',
    'all': '全部',
}

# 0 号会话不属于任何会话：日程、摘要、记忆抽取等后台任务发起的调用。
BACKGROUND_STREAM_LABEL = '后台任务'
MAIN_SOURCE_LABEL = '主程序'

_SUMS = '''
  COALESCE(SUM(calls), 0) AS calls,
  COALESCE(SUM(failed_calls), 0) AS failed_calls,
  COALESCE(SUM(interrupted_calls), 0) AS interrupted_calls,
  COALESCE(SUM(unreported_calls), 0) AS unreported_calls,
  COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
  COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
  COALESCE(SUM(total_tokens), 0) AS total_tokens,
  COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
  COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
  COALESCE(SUM(cost), 0) AS cost,
  COALESCE(SUM(duration_ms_sum), 0) AS duration_ms_sum,
  COALESCE(SUM(duration_ms_sq_sum), 0) AS duration_ms_sq_sum
'''
_RANGE_FILTER = 'hour_start >= ? AND hour_start < ?'


@dataclass(frozen=True)
class ModelPrice:
    """一个模型在当前配置里的单价，单位元／百万 token。

    :ivar provider: 模型引用的厂商名。
    :ivar price_in: 输入单价。
    :ivar price_out: 输出单价（含思考）。
    :ivar cache_price_in: 缓存命中价；``None`` 表示与输入单价相同。
    :ivar periods: 分时段单价的段数。
    """

    provider: str
    price_in: float
    price_out: float
    cache_price_in: float | None
    periods: int

    @property
    def unpriced(self) -> bool:
        """基础单价全为 0 且没有分时段单价：多半是没填，而不是真的免费。"""
        return self.price_in == 0 and self.price_out == 0 and self.periods == 0


def model_prices(candidates: Iterable[ModelCandidate]) -> Dict[str, ModelPrice]:
    """从当前配置的模型候选里取出按模型名索引的单价。

    :param candidates: 全部任务路由里的候选。
    :return: 模型名到单价的映射；同名模型取第一次出现的那条。
    副作用：无。
    """
    prices: Dict[str, ModelPrice] = {}
    for candidate in candidates:
        prices.setdefault(candidate.name, ModelPrice(
            provider=candidate.provider,
            price_in=candidate.price_in,
            price_out=candidate.price_out,
            cache_price_in=candidate.cache_price_in,
            periods=len(candidate.price_periods),
        ))
    return prices


def task_labels() -> Dict[str, str]:
    """返回模型任务名到中文名的映射。

    :return: 取自 ``settings_schema.json`` 的 ``model_tasks`` 条目；该清单在加载时已与
        配置模型的任务槽逐一核对。
    :raises ValueError: schema 缺少 models.toml 或 model_tasks 段。
    副作用：首次调用时读取并缓存 schema 文件。
    """
    for section in file_schema('models.toml')['sections']:
        if section['key'] == 'model_tasks':
            return {entry['key']: entry['label'] for entry in section['entries']}
    raise ValueError('settings_schema.json 的 models.toml 缺少 model_tasks 段')


def local_tz_offset_minutes(now_ms: int) -> int:
    """返回本进程所在时区在某一时刻相对 UTC 的偏移。

    :param now_ms: Unix 毫秒时刻；夏令时地区偏移随时刻变化。
    :return: 东区为正的分钟数，如东八区为 480。
    副作用：读取本机时区设置。
    """
    offset = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).astimezone().utcoffset()
    assert offset is not None  # astimezone() 产出的对象一定带时区
    return int(offset.total_seconds() // 60)


def _day_start(at_ms: int, tz_offset_minutes: int) -> int:
    """取某一时刻所在本地日的零点。

    :param at_ms: Unix 毫秒时刻。
    :param tz_offset_minutes: 东区为正的时区偏移分钟数。
    :return: 该本地日零点对应的 Unix 毫秒时刻。
    副作用：无。
    """
    offset = tz_offset_minutes * 60_000
    return (at_ms + offset) // DAY_MS * DAY_MS - offset


def range_bounds(key: str, now_ms: int, tz_offset_minutes: int) -> Tuple[int, int]:
    """把时间范围名换算成半开区间 ``[起, 止)``。

    止点统一是当前小时桶的结束，因此当前小时总在范围内。「近 7 天」「近 30 天」按
    本地日计，含今天。

    :param key: :data:`RANGE_KEYS` 之一。
    :param now_ms: 当前 Unix 毫秒时刻。
    :param tz_offset_minutes: 东区为正的时区偏移分钟数。
    :return: ``(起点, 止点)`` Unix 毫秒。
    :raises ValueError: 未知的范围名。
    副作用：无。
    """
    hour_start = now_ms - now_ms % HOUR_MS
    end = hour_start + HOUR_MS
    if key == 'hour':
        return hour_start, end
    today = _day_start(now_ms, tz_offset_minutes)
    if key == 'today':
        return today, end
    if key == '7d':
        return today - 6 * DAY_MS, end
    if key == '30d':
        return today - 29 * DAY_MS, end
    if key == 'all':
        return 0, end
    raise ValueError(f'未知的时间范围：{key}')


def _totals(row: Mapping[str, Any]) -> Dict[str, Any]:
    """把一行累加值转成展示用的合计，补上平均耗时与波动。

    :param row: 含 :data:`_SUMS` 各列的查询行。
    :return: 驼峰字段的合计；没有调用时平均耗时与波动为 ``None``。
    副作用：无。
    """
    calls = int(row['calls'])
    average: float | None = None
    deviation: float | None = None
    if calls:
        average = row['duration_ms_sum'] / calls
        # 总体标准差：E[x²] − E[x]²；浮点误差可能让方差略小于 0。
        deviation = math.sqrt(max(row['duration_ms_sq_sum'] / calls - average * average, 0.0))
    return {
        'calls': calls,
        'failedCalls': int(row['failed_calls']),
        'interruptedCalls': int(row['interrupted_calls']),
        'unreportedCalls': int(row['unreported_calls']),
        'promptTokens': int(row['prompt_tokens']),
        'completionTokens': int(row['completion_tokens']),
        'totalTokens': int(row['total_tokens']),
        'cachedTokens': int(row['cached_tokens']),
        'reasoningTokens': int(row['reasoning_tokens']),
        'cost': float(row['cost']),
        'avgDurationMs': average,
        'durationStdMs': deviation,
    }


def _sort_key(group: Mapping[str, Any]) -> Tuple[float, int, int]:
    """分组按费用、token、次数从高到低排列。"""
    return (-group['cost'], -group['totalTokens'], -group['calls'])


def _stream_labels(connection: sqlite3.Connection, stream_ids: List[int]) -> Dict[int, str]:
    """给会话编号配上可读名称：群聊取群名，私聊取对方昵称。

    口径与 ``StreamRegistry.stream_readable_name`` 一致；名称未知时退回外部标识。

    :param connection: 主库连接。
    :param stream_ids: 待命名的会话编号，不含 0。
    :return: 会话编号到名称的映射；库里已不存在的会话不在其中。
    :raises sqlite3.Error: 查询失败。
    副作用：只读。
    """
    if not stream_ids:
        return {}
    placeholders = ', '.join('?' for _ in stream_ids)
    rows = connection.execute(
        f'''SELECT s.id, s.kind, s.external_id, s.display_name,
                   (SELECT i.display_name FROM identities i
                     WHERE i.platform = s.platform AND i.external_id = s.external_id) AS peer
              FROM streams s WHERE s.id IN ({placeholders})''',
        stream_ids,
    ).fetchall()
    labels: Dict[int, str] = {}
    for stream_id, kind, external_id, display_name, peer in rows:
        if kind == 'desktop':
            labels[stream_id] = '桌面'
        elif kind == 'direct':
            labels[stream_id] = f'私聊 · {peer or external_id}'
        else:
            labels[stream_id] = f'群聊 · {display_name or external_id}'
    return labels


def _grouped(
    connection: sqlite3.Connection,
    columns: str,
    start_ms: int,
    end_ms: int,
) -> List[sqlite3.Row]:
    """按给定列分组累加范围内的小时行。

    :param connection: 账本连接，``row_factory`` 须为 ``sqlite3.Row``。
    :param columns: GROUP BY 的列，逗号分隔，来自本模块的固定字面量。
    :param start_ms: 范围起点。
    :param end_ms: 范围止点（不含）。
    :return: 每组一行，含分组列与 :data:`_SUMS` 各列。
    :raises sqlite3.Error: 查询失败。
    副作用：只读。
    """
    return connection.execute(
        f'SELECT {columns}, {_SUMS} FROM llm_usage_hourly WHERE {_RANGE_FILTER} GROUP BY {columns}',
        (start_ms, end_ms),
    ).fetchall()


def summarize_usage(
    connection: sqlite3.Connection,
    *,
    start_ms: int,
    end_ms: int,
    tz_offset_minutes: int,
    now_ms: int,
    prices: Mapping[str, ModelPrice],
) -> Dict[str, Any]:
    """汇总一个时间范围内的模型用量。

    :param connection: 主库连接，``row_factory`` 须为 ``sqlite3.Row``；同时读小时表、
        消息表与会话表。
    :param start_ms: 范围起点 Unix 毫秒；只统计小时起点不早于它的桶。
    :param end_ms: 范围止点 Unix 毫秒，不含。
    :param tz_offset_minutes: 东区为正的时区偏移分钟数，用于按天的趋势切日界。
    :param now_ms: 当前 Unix 毫秒时刻，决定两条趋势的窗口。
    :param prices: 模型名到当前单价的映射，见 :func:`model_prices`。
    :return: 可 JSON 序列化的字典：``range``、``overview``（含回复条数）、``perReply``
        （回复数为 0 时三项均为 ``None``）、``unreported``（没返回用量的模型与厂商）、
        ``groups``（``source`` / ``task`` / ``model`` / ``stream`` 四种分组，各组合计之和
        等于总览）与 ``trend``（``hourly`` 48 小时、``daily`` 30 天）。
    :raises ValueError: 止点不晚于起点。
    :raises sqlite3.Error: 查询失败。
    副作用：只读。
    """
    if end_ms <= start_ms:
        raise ValueError('统计范围的止点必须晚于起点')
    overview = _totals(connection.execute(
        f'SELECT {_SUMS} FROM llm_usage_hourly WHERE {_RANGE_FILTER}', (start_ms, end_ms),
    ).fetchone())
    # 回复条数只数账本开始记账之后的。消息表的历史远早于账本，「近 30 天」「全部」若照数
    # 全部回复，每条回复的平均会被没有用量记录的那些天摊薄（上线首日实测被摊到约十分之一）。
    # 起点取首个小时桶的起点，首小时内记账前的几分钟会多数几条，量级可忽略。
    first_hour = connection.execute('SELECT MIN(hour_start) FROM llm_usage_hourly').fetchone()[0]
    replies_start = start_ms if first_hour is None else max(start_ms, int(first_hour))
    replies = int(connection.execute(
        "SELECT COUNT(*) FROM messages WHERE role = 'assistant' AND created_at >= ? AND created_at < ?",
        (replies_start, end_ms),
    ).fetchone()[0])
    overview['replies'] = replies
    per_reply: Dict[str, float | None] = {
        'cost': overview['cost'] / replies if replies else None,
        'tokens': overview['totalTokens'] / replies if replies else None,
        'calls': overview['calls'] / replies if replies else None,
    }

    sources = []
    for row in _grouped(connection, 'plugin_id, plugin_third_party', start_ms, end_ms):
        plugin_id = row['plugin_id']
        sources.append({
            'key': plugin_id or 'main',
            'label': plugin_id or MAIN_SOURCE_LABEL,
            'kind': 'main' if not plugin_id else ('third_party' if row['plugin_third_party'] else 'builtin'),
            **_totals(row),
        })

    labels = task_labels()
    tasks = [
        {'key': row['task'], 'label': labels.get(row['task'], row['task']), **_totals(row)}
        for row in _grouped(connection, 'task', start_ms, end_ms)
    ]

    models = []
    for row in _grouped(connection, 'model, provider', start_ms, end_ms):
        price = prices.get(row['model'])
        # 同名模型改指向了别的厂商时，当前单价不是这一行的单价，不展示。
        current = price if price is not None and price.provider == row['provider'] else None
        models.append({
            'key': f"{row['model']}@{row['provider']}",
            'model': row['model'],
            'provider': row['provider'],
            'price': None if current is None else {
                'priceIn': current.price_in,
                'priceOut': current.price_out,
                'cachePriceIn': current.cache_price_in,
                'periods': current.periods,
                'unpriced': current.unpriced,
            },
            **_totals(row),
        })

    stream_rows = _grouped(connection, 'stream_id', start_ms, end_ms)
    names = _stream_labels(connection, [row['stream_id'] for row in stream_rows if row['stream_id']])
    streams = [
        {
            'key': str(row['stream_id']),
            'streamId': row['stream_id'],
            'label': (
                BACKGROUND_STREAM_LABEL if row['stream_id'] == 0
                else names.get(row['stream_id'], f"会话 #{row['stream_id']}")
            ),
            **_totals(row),
        }
        for row in stream_rows
    ]

    for groups in (sources, tasks, models, streams):
        groups.sort(key=_sort_key)

    return {
        'range': {'start': start_ms, 'end': end_ms, 'tzOffsetMinutes': tz_offset_minutes},
        'generatedAt': now_ms,
        'overview': overview,
        'perReply': per_reply,
        'unreported': [
            {'model': group['model'], 'provider': group['provider'], 'calls': group['unreportedCalls']}
            for group in models if group['unreportedCalls']
        ],
        'groups': {'source': sources, 'task': tasks, 'model': models, 'stream': streams},
        'trend': _trend(connection, now_ms, tz_offset_minutes),
    }


def _trend(connection: sqlite3.Connection, now_ms: int, tz_offset_minutes: int) -> Dict[str, Any]:
    """近 48 小时按小时、近 30 天按天的 token 与费用序列，缺的桶补 0。

    :param connection: 账本连接。
    :param now_ms: 当前 Unix 毫秒时刻。
    :param tz_offset_minutes: 东区为正的时区偏移分钟数。
    :return: ``hourly`` 与 ``daily`` 两个列表，每项含 ``start``、``tokens``、``cost``，
        按时间升序。
    :raises sqlite3.Error: 查询失败。
    副作用：只读。
    """
    current_hour = now_ms - now_ms % HOUR_MS
    first_hour = current_hour - (HOURLY_TREND_HOURS - 1) * HOUR_MS
    hourly_rows = {
        row[0]: (int(row[1]), float(row[2]))
        for row in connection.execute(
            '''SELECT hour_start, SUM(total_tokens), SUM(cost) FROM llm_usage_hourly
                WHERE hour_start >= ? AND hour_start <= ? GROUP BY hour_start''',
            (first_hour, current_hour),
        )
    }
    hourly = []
    for index in range(HOURLY_TREND_HOURS):
        start = first_hour + index * HOUR_MS
        tokens, cost = hourly_rows.get(start, (0, 0.0))
        hourly.append({'start': start, 'tokens': tokens, 'cost': cost})

    offset = tz_offset_minutes * 60_000
    today = _day_start(now_ms, tz_offset_minutes)
    first_day = today - (DAILY_TREND_DAYS - 1) * DAY_MS
    daily_rows = {
        int(row[0]): (int(row[1]), float(row[2]))
        for row in connection.execute(
            '''SELECT (hour_start + ?) / ? AS day, SUM(total_tokens), SUM(cost) FROM llm_usage_hourly
                WHERE hour_start >= ? AND hour_start < ? GROUP BY day''',
            (offset, DAY_MS, first_day, today + DAY_MS),
        )
    }
    daily = []
    for index in range(DAILY_TREND_DAYS):
        start = first_day + index * DAY_MS
        tokens, cost = daily_rows.get((start + offset) // DAY_MS, (0, 0.0))
        daily.append({'start': start, 'tokens': tokens, 'cost': cost})
    return {'hourly': hourly, 'daily': daily}


__all__ = [
    'BACKGROUND_STREAM_LABEL',
    'DAY_MS',
    'MAIN_SOURCE_LABEL',
    'ModelPrice',
    'RANGE_KEYS',
    'RANGE_LABELS',
    'local_tz_offset_minutes',
    'model_prices',
    'range_bounds',
    'summarize_usage',
    'task_labels',
]
