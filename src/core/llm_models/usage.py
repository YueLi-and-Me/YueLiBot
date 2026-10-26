"""模型调用的 token 用量：标准化、计费与按小时落账。

服务商返回的用量形状各异，本模块只认协议层面的标准形状，统一成五个字段
（:class:`TokenUsage`）；形状都不认识就当作「没返回」，不估算。每个候选尝试由路由层
开一笔 :class:`UsageAttempt`，收尾时按尝试开始时刻生效的单价算好费用，累加进
``llm_usage_hourly`` 小时表（表结构见 ``src.core.db.schema.USAGE_DDL``）。

对外接口：

- :class:`TokenUsage` / :func:`normalize_usage`：用量的统一形状与标准化；
- :func:`report_usage` / :func:`collect_usage`：非流式请求函数上报用量，路由层为每个
  候选尝试设置收集器；
- :class:`PriceSet` / :func:`prices_at` / :func:`compute_cost`：按时段选单价与算费；
- :class:`UsageAttempt` / :func:`strip_usage`：路由层的尝试记账与流式用量块剥离；
- :class:`UsageLedger` 与模块级单例 :data:`usage_ledger`：独立连接写库与只读查询。

依赖观测上下文（会话）与库表结构；单价随模型候选传入，插件归属由路由层读取后
传入，本模块运行期不导入配置包与插件系统（原因见导入处的注释）。
被 ``router``、``embeddings``、``rerank``、``openai`` 与用量统计调用。账本由 ``src.main``
在迁移之后配置。
"""

from __future__ import annotations

from contextlib import aclosing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Callable,
    Dict,
    Iterator,
    Literal,
    Mapping,
    Set,
    Tuple,
    TypeVar,
)

import sqlite3
import threading
import time

from src.core.db.schema import USAGE_DDL
from src.core.logging.logger import get_logger
from src.core.observe.events import current_stream_id
from src.core.runtime.clock import now as current_time

# 两个类型只用于注解。运行期导入会成环：配置包的初始化导入 loader，loader 导入
# openai，而 openai 依赖本模块；插件包的初始化同样会反向导入 openai。
if TYPE_CHECKING:
    from src.core.config.schema import ModelCandidate
    from src.plugin_system.attribution import PluginOrigin

logger = get_logger(__name__)

# 一小时的毫秒数；小时表按 UTC 整点对齐分桶。
HOUR_MS = 3_600_000
# 单价的计量单位：元／百万 token。
TOKENS_PER_PRICE_UNIT = 1_000_000

# 一次候选尝试的结局：正常结束、以错误结束（含首字超时与格式不合格）、被打断
# （用户中断或调用方提前关流）。
AttemptOutcome = Literal['ok', 'failed', 'interrupted']

_T = TypeVar('_T')


@dataclass(frozen=True)
class TokenUsage:
    """一次模型调用的用量，单位 token。

    :ivar prompt_tokens: 输入 token，含缓存命中部分。
    :ivar completion_tokens: 输出 token，含思考。
    :ivar total_tokens: 服务商给的总数；没给时为前两项之和。
    :ivar cached_tokens: 输入中缓存命中的部分。
    :ivar reasoning_tokens: 输出中思考的部分。
    """

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int = 0
    reasoning_tokens: int = 0

    def __add__(self, other: 'TokenUsage') -> 'TokenUsage':
        """逐字段相加，用于一次尝试内分多次请求的协议（如方舟多模态向量逐条请求）。

        :param other: 另一份用量。
        :return: 各字段之和。
        副作用：无。
        """
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )

    def as_dict(self) -> Dict[str, int]:
        """转成调用记录与接口使用的驼峰字段字典。

        :return: 含五个字段的字典。
        副作用：无。
        """
        return {
            'promptTokens': self.prompt_tokens,
            'completionTokens': self.completion_tokens,
            'totalTokens': self.total_tokens,
            'cachedTokens': self.cached_tokens,
            'reasoningTokens': self.reasoning_tokens,
        }


def _count(raw: Mapping[str, Any], *keys: str) -> int | None:
    """按顺序取第一个存在且为非负整数的计数字段。

    :param raw: 用量映射。
    :param keys: 候选字段名，按优先级排列。
    :return: 计数；全部缺失或类型不符时为 ``None``。
    副作用：无。
    """
    for key in keys:
        value = raw.get(key)
        # bool 是 int 的子类，``true`` 不能被当成 1 个 token。
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _detail_count(raw: Mapping[str, Any], pairs: Tuple[Tuple[str, str], ...]) -> int:
    """从 ``*_details`` 子对象里取明细计数。

    :param raw: 用量映射。
    :param pairs: ``(子对象名, 字段名)`` 候选，按优先级排列。
    :return: 第一个存在的计数；都没有时为 0（服务商不细分时明细就是 0）。
    副作用：无。
    """
    for container, key in pairs:
        details = raw.get(container)
        if isinstance(details, Mapping):
            value = _count(details, key)
            if value is not None:
                return value
    return 0


def normalize_usage(raw: Any, *, input_only: bool = False) -> TokenUsage | None:
    """把服务商返回的用量对象统一成 :class:`TokenUsage`。

    只认两套协议层面的标准形状：Chat Completions 的 ``prompt_tokens`` /
    ``completion_tokens`` 及其 ``*_tokens_details``，与 Responses 的 ``input_tokens`` /
    ``output_tokens`` 及其 ``*_tokens_details``。不按服务商枚举私有字段。

    :param raw: 响应里的 ``usage`` 字段原值，可为 ``None``。
    :param input_only: 该协议没有输出（向量、重排）。为真时只给 ``total_tokens``
        的用量按全部是输入记，默认 ``False``。
    :return: 统一后的用量。输入与输出计数一个都不认识时返回 ``None``，按「服务商
        没返回」处理；只给 ``total_tokens`` 的对话类用量同样返回 ``None``，不推测拆分。
    副作用：无。
    """
    if not isinstance(raw, Mapping):
        return None
    prompt = _count(raw, 'prompt_tokens', 'input_tokens')
    completion = _count(raw, 'completion_tokens', 'output_tokens')
    total = _count(raw, 'total_tokens')
    if prompt is None and completion is None:
        # 部分重排接口的用量只有 total_tokens。向量与重排没有输出，总数就是输入，
        # 按输入记是精确值而不是估算；对话类分不清输入与输出，仍按没返回处理。
        if input_only and total is not None:
            return TokenUsage(prompt_tokens=total, completion_tokens=0, total_tokens=total)
        return None
    prompt = prompt or 0
    completion = completion or 0
    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total if total is not None else prompt + completion,
        cached_tokens=_detail_count(raw, (
            ('prompt_tokens_details', 'cached_tokens'),
            ('input_tokens_details', 'cached_tokens'),
        )),
        reasoning_tokens=_detail_count(raw, (
            ('completion_tokens_details', 'reasoning_tokens'),
            ('output_tokens_details', 'reasoning_tokens'),
        )),
    )


@dataclass
class UsageCollector:
    """一个非流式候选尝试内上报的用量。

    :ivar metered: 请求函数调用过 :func:`report_usage`，即该协议按 token 计量。
        没调用过（如语音合成）表示不按 token 计量，不算「服务商没返回」。
    :ivar usage: 已识别用量之和；一次都没识别出来时为 ``None``。
    """

    metered: bool = False
    usage: TokenUsage | None = None


# 当前候选尝试的用量收集器，由 ModelRouter.run 为每个候选设置。可变对象放进
# ContextVar 是有意的：请求函数内 gather 出去的子任务复制的是同一个引用，各自上报
# 的用量累加到同一个收集器上。
_collector: ContextVar[UsageCollector | None] = ContextVar('llm_usage_collector', default=None)


def report_usage(raw: Any, *, input_only: bool = False) -> None:
    """非流式请求函数拿到响应后上报其中的 ``usage`` 字段。

    :param raw: 响应体里的 ``usage`` 原值；响应没有该字段时传 ``None``，表示该协议
        按 token 计量但这次服务商没返回。
    :param input_only: 该协议没有输出（向量、重排），语义见 :func:`normalize_usage`，
        默认 ``False``。
    :return: ``None``。
    副作用：把用量累加进当前尝试的收集器；不在路由尝试内（脚本或用例直接调用
        请求函数）时没有账要记，直接返回。
    """
    collector = _collector.get()
    if collector is None:
        return
    collector.metered = True
    usage = normalize_usage(raw, input_only=input_only)
    if usage is not None:
        collector.usage = usage if collector.usage is None else collector.usage + usage


@contextmanager
def collect_usage() -> Iterator[UsageCollector]:
    """为一次非流式候选尝试设置独立的用量收集器。

    :return: 上下文管理器，产出本次尝试的收集器；退出时还原上一个收集器。
    副作用：在当前上下文里临时改写收集器 ContextVar。
    """
    collector = UsageCollector()
    token = _collector.set(collector)
    try:
        yield collector
    finally:
        _collector.reset(token)


@dataclass(frozen=True)
class PriceSet:
    """一组生效的单价，单位元／百万 token。

    :ivar price_in: 输入单价。
    :ivar price_out: 输出单价（含思考）。
    :ivar cache_price_in: 缓存命中部分的输入单价；``None`` 表示与 ``price_in`` 相同。
    """

    price_in: float
    price_out: float
    cache_price_in: float | None


def prices_at(candidate: ModelCandidate, at_ms: int) -> PriceSet:
    """选出某一时刻对该候选生效的单价组。

    时刻按进程所在时区的本地时间解释，与作息、日程一致。

    :param candidate: 带单价与分时段单价的模型候选。
    :param at_ms: Unix 毫秒时刻，取候选尝试的开始时刻。
    :return: 落在某个时段内时为该时段的单价组，否则为模型的基础单价。
    副作用：读取本机时区设置。
    """
    moment = datetime.fromtimestamp(at_ms / 1000)
    minute = moment.hour * 60 + moment.minute
    for period in candidate.price_periods:
        if period.covers(minute):
            return PriceSet(period.price_in, period.price_out, period.cache_price_in)
    return PriceSet(candidate.price_in, candidate.price_out, candidate.cache_price_in)


def compute_cost(usage: TokenUsage, prices: PriceSet) -> float:
    """按单价组算一次调用的费用。

    :param usage: 本次调用的用量。
    :param prices: 生效的单价组。
    :return: 费用，单位元：未命中缓存的输入按 ``price_in``，命中部分按
        ``cache_price_in``（留空按 ``price_in``），输出按 ``price_out``。
    副作用：无。
    """
    cache_price = prices.price_in if prices.cache_price_in is None else prices.cache_price_in
    return (
        (usage.prompt_tokens - usage.cached_tokens) * prices.price_in
        + usage.cached_tokens * cache_price
        + usage.completion_tokens * prices.price_out
    ) / TOKENS_PER_PRICE_UNIT


@dataclass(frozen=True)
class UsageRecord:
    """一次候选尝试的落账内容。

    :ivar started_at: 尝试开始的 Unix 毫秒时刻，决定小时桶与单价组。
    :ivar task: 模型任务名。
    :ivar model: models.toml 里的模型名。
    :ivar provider: providers.toml 里的厂商名。
    :ivar stream_id: 所属会话；0 表示不属于任何会话。
    :ivar plugin_id: 发起调用的插件 id；空串表示主程序。
    :ivar plugin_third_party: 发起调用的是否第三方插件。
    :ivar outcome: 尝试结局。
    :ivar metered: 该协议是否按 token 计量。
    :ivar usage: 服务商返回的用量；没返回或不计量时为 ``None``。
    :ivar cost: 费用，单位元。
    :ivar duration_ms: 从发起请求到尝试收尾的毫秒数。
    """

    started_at: int
    task: str
    model: str
    provider: str
    stream_id: int
    plugin_id: str
    plugin_third_party: bool
    outcome: AttemptOutcome
    metered: bool
    usage: TokenUsage | None
    cost: float
    duration_ms: int

    @property
    def unreported(self) -> bool:
        """正常结束、按 token 计量、但服务商没返回用量。"""
        return self.outcome == 'ok' and self.metered and self.usage is None


_UPSERT_SQL = """
INSERT INTO llm_usage_hourly (
  hour_start, task, model, provider, stream_id, plugin_id, plugin_third_party,
  calls, failed_calls, interrupted_calls, unreported_calls,
  prompt_tokens, completion_tokens, total_tokens, cached_tokens, reasoning_tokens,
  cost, duration_ms_sum, duration_ms_sq_sum
) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (hour_start, task, model, provider, stream_id, plugin_id) DO UPDATE SET
  plugin_third_party = excluded.plugin_third_party,
  calls              = calls + excluded.calls,
  failed_calls       = failed_calls + excluded.failed_calls,
  interrupted_calls  = interrupted_calls + excluded.interrupted_calls,
  unreported_calls   = unreported_calls + excluded.unreported_calls,
  prompt_tokens      = prompt_tokens + excluded.prompt_tokens,
  completion_tokens  = completion_tokens + excluded.completion_tokens,
  total_tokens       = total_tokens + excluded.total_tokens,
  cached_tokens      = cached_tokens + excluded.cached_tokens,
  reasoning_tokens   = reasoning_tokens + excluded.reasoning_tokens,
  cost               = cost + excluded.cost,
  duration_ms_sum    = duration_ms_sum + excluded.duration_ms_sum,
  duration_ms_sq_sum = duration_ms_sq_sum + excluded.duration_ms_sq_sum
"""


class UsageLedger:
    """模型用量小时表的独立连接写入与只读查询。

    用独立连接而不是事件循环线程上那条共享连接：
    - 现象：用量写入的提交把别的代码尚未完成的半个事务一起提交，或随对方回滚被
      一并撤销，表现为小时表偶发少记、业务表出现不该落地的中间状态。
    - 原因：sqlite3 连接上的事务是连接级的，共享连接上可能有别的代码开着事务；
      在同一连接上执行写入与提交会被卷进对方的事务边界。
    - 后果：用量写入必须走本类自己的连接，并与管线事件账本一样用锁串行化。
    """

    def __init__(self) -> None:
        """创建未配置的账本。

        副作用：初始化连接引用、线程锁与写入计数，不打开文件。
        """
        self._connection: sqlite3.Connection | None = None
        self._path: Path | None = None
        self._lock = threading.Lock()
        # 本进程累计落账的尝试数，供控制台汇总判断「距上次打印有没有新调用」。
        self._recorded = 0

    @property
    def configured(self) -> bool:
        """账本是否已打开连接。"""
        return self._connection is not None

    @property
    def recorded(self) -> int:
        """本进程累计成功落账的候选尝试数。"""
        return self._recorded

    def configure(self, db_path: str | Path) -> None:
        """打开或替换账本连接并确保小时表存在。

        :param db_path: SQLite 数据库路径，与主库同一个文件。
        :raises (OSError, sqlite3.Error): 数据库打开或建表失败。
        副作用：关闭旧连接，打开新连接并执行幂等建表。
        """
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            connection = sqlite3.connect(str(db_path), check_same_thread=False)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute('PRAGMA synchronous = NORMAL')
                connection.execute('PRAGMA busy_timeout = 3000')
                connection.executescript(USAGE_DDL)
                connection.commit()
            except Exception:
                connection.close()
                raise
            self._connection = connection
            self._path = Path(db_path)

    def close(self) -> None:
        """关闭账本连接；未打开时安全返回。

        副作用：关闭连接并清空引用。
        """
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            self._path = None

    def record(self, record: UsageRecord) -> None:
        """把一次尝试累加进它所在小时的汇总行。

        写库失败只记一条带完整异常的 warning：用量是旁路观测，磁盘满或库被锁时
        让正在进行的回复失败是更坏的结果，异常本身仍完整进入日志。

        :param record: 一次尝试的落账内容。
        :return: ``None``。
        副作用：未配置时直接返回；否则执行一次 UPSERT 并提交。
        """
        if self._connection is None:
            return
        usage = record.usage or TokenUsage(0, 0, 0)
        params = (
            record.started_at - record.started_at % HOUR_MS,
            record.task,
            record.model,
            record.provider,
            record.stream_id,
            record.plugin_id,
            int(record.plugin_third_party),
            int(record.outcome == 'failed'),
            int(record.outcome == 'interrupted'),
            int(record.unreported),
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.total_tokens,
            usage.cached_tokens,
            usage.reasoning_tokens,
            record.cost,
            record.duration_ms,
            record.duration_ms * record.duration_ms,
        )
        try:
            with self._lock:
                connection = self._connection
                if connection is None:
                    return
                connection.execute(_UPSERT_SQL, params)
                connection.commit()
                self._recorded += 1
        except sqlite3.Error:
            logger.warning(
                'llm_usage_write_failed',
                task=record.task,
                model=record.model,
                provider=record.provider,
                exc_info=True,
            )

    def read(self, query: Callable[[sqlite3.Connection], _T]) -> _T:
        """在一条临时只读连接上执行查询。

        读不复用写连接：统计查询在工作线程里可能跑上百毫秒，持写锁会让事件循环上
        的落账同步等待；WAL 模式下读连接与写连接互不阻塞。

        :param query: 接收连接（``row_factory`` 为 ``sqlite3.Row``）、返回查询结果的
            函数；不得写库。
        :return: ``query`` 的返回值。
        :raises RuntimeError: 账本尚未配置。
        :raises sqlite3.Error: 查询失败。
        副作用：打开并关闭一条连接；可在工作线程里调用。
        """
        path = self._path
        if path is None:
            raise RuntimeError('用量账本尚未配置')
        connection = sqlite3.connect(str(path))
        try:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA busy_timeout = 3000')
            return query(connection)
        finally:
            connection.close()


usage_ledger = UsageLedger()

# 本进程里已经告警过「正常结束但没返回用量」的 (厂商, 模型)，每对只告警一次。
_unreported_warned: Set[Tuple[str, str]] = set()


class UsageAttempt:
    """路由层一次候选尝试的用量账。

    在发起请求前创建：此刻记下开始时刻、会话与插件归属；收尾时按开始时刻生效的
    单价算费并落账。归属由路由层在尝试开始时读一次传入，尝试中途上下文变化不影响记账。
    """

    def __init__(self, task: str, candidate: ModelCandidate, origin: PluginOrigin | None) -> None:
        """记下尝试的开始时刻与归属。

        :param task: 模型任务名。
        :param candidate: 本次尝试的候选，提供模型名、厂商与单价。
        :param origin: 发起调用的插件；``None`` 表示主程序。
        副作用：读取时钟与会话上下文。
        """
        self.task = task
        self.candidate = candidate
        self.started_at = current_time()
        self._started = time.monotonic()
        self.stream_id = current_stream_id() or 0
        self.plugin_id = origin.plugin_id if origin is not None else ''
        self.plugin_third_party = origin.third_party if origin is not None else False
        self.usage: TokenUsage | None = None
        self._settled = False

    def observe(self, usage: TokenUsage) -> None:
        """记下流式响应末尾交来的用量；同一流出现多次时以最后一次为准。

        :param usage: 协议层已标准化的用量。
        副作用：覆盖本次尝试的用量。
        """
        self.usage = usage

    def settle(self, outcome: AttemptOutcome, *, metered: bool = True) -> UsageRecord:
        """收尾：算费、落账，并对「没返回用量」做一次性告警。

        :param outcome: 尝试结局。
        :param metered: 该协议是否按 token 计量；流式对话恒为真。
        :return: 已落账的记录。
        :raises RuntimeError: 同一次尝试重复收尾，说明调用方的收尾出口不唯一。
        副作用：写入用量账本；同一 (厂商, 模型) 首次出现没返回用量时记一条 warning。
        """
        if self._settled:
            raise RuntimeError(f'{self.task} 任务的同一次候选尝试重复落账')
        self._settled = True
        cost = (
            compute_cost(self.usage, prices_at(self.candidate, self.started_at))
            if self.usage is not None
            else 0.0
        )
        record = UsageRecord(
            started_at=self.started_at,
            task=self.task,
            model=self.candidate.name,
            provider=self.candidate.provider,
            stream_id=self.stream_id,
            plugin_id=self.plugin_id,
            plugin_third_party=self.plugin_third_party,
            outcome=outcome,
            metered=metered,
            usage=self.usage,
            cost=cost,
            duration_ms=int((time.monotonic() - self._started) * 1_000),
        )
        if record.unreported:
            scope = (record.provider, record.model)
            if scope not in _unreported_warned:
                _unreported_warned.add(scope)
                logger.warning(
                    'llm_usage_unreported',
                    task=record.task,
                    model=record.model,
                    provider=record.provider,
                    hint='该服务商这次正常返回但没有带用量，统计里计入「未返回用量」；同一模型本进程内只提示一次',
                )
        usage_ledger.record(record)
        return record


async def strip_usage(chunks: AsyncIterator[Dict[str, Any]], attempt: UsageAttempt) -> AsyncIterator[Dict[str, Any]]:
    """从 provider 流里收走用量增量，其余增量原样转交。

    必须套在首字判定、``require_text`` 与结构化校验缓冲之前：

    - 现象：只有用量、没有正文的流被当成「已经出了首字」；``require_text`` 的循环
      把用量块当成一个有效增量；结构化校验缓冲把用量块拼进待校验的正文。
    - 原因：用量块在流的最后一块，是 provider 额外产出的协议增量，不是模型输出。
    - 后果：剥离放在 ``client.stream(...)`` 返回之后立刻进行，调用方任何路径都看不到
      用量块；不产出用量块的 provider（含测试替身）行为不变。

    :param chunks: provider 的增量流。
    :param attempt: 本次尝试的用量账，收到用量块时更新。
    :yield: 不含 ``usage`` 字段的增量。
    :raises TypeError: 用量块不是 :class:`TokenUsage`，说明 provider 没有先标准化。
    副作用：关闭本生成器时同步关闭内层 provider 流。
    """
    async with aclosing(chunks) as stream:
        async for chunk in stream:
            if 'usage' in chunk:
                usage = chunk['usage']
                if not isinstance(usage, TokenUsage):
                    raise TypeError(f'用量增量必须是 TokenUsage，实际为 {type(usage).__name__}')
                attempt.observe(usage)
                rest = {key: value for key, value in chunk.items() if key != 'usage'}
                if not rest:
                    continue
                chunk = rest
            yield chunk


__all__ = [
    'HOUR_MS',
    'AttemptOutcome',
    'PriceSet',
    'TokenUsage',
    'UsageAttempt',
    'UsageCollector',
    'UsageLedger',
    'UsageRecord',
    'collect_usage',
    'compute_cost',
    'normalize_usage',
    'prices_at',
    'report_usage',
    'strip_usage',
    'usage_ledger',
]
