"""平台入站并发基线：测量固定注入速率下的首个文本事件延迟。

脚本使用内存 SQLite、模拟模型和模拟嵌入客户端，隔离网络与磁盘因素，重点观察
``ChatService`` 在多 stream 并发入站时的事件延迟与召回路径。
"""

from __future__ import annotations

from argparse import ArgumentParser
from dataclasses import dataclass
from math import ceil
from struct import pack
from time import perf_counter
from typing import AsyncIterator, Dict, List

import asyncio
import json
import sqlite3

from src.core.logging.logger import initialize_logging
from src.core.config.schema import Config, LogConfig
from src.core.memory.store import EpisodeInput, FactInput, MemoryStore
from src.core.observe.store import event_store
from src.core.platform_io.registry import StreamRegistry
from src.core.platform_io.types import ConversationContext, InboundMessage
from src.core.services.chat import ChatService
from src.core.services.maintenance.vector import VectorService


_FACT_COUNT = 24
_EPISODE_COUNT = 12
_QUERY_EMBEDDING = pack('<64f', *([0.5] * 64))


class _BenchmarkProvider:
    """只模拟模型首字可用，不把真实网络抖动混入接入层基线。"""

    async def stream(self, **_kwargs: object) -> AsyncIterator[Dict[str, str]]:
        """延迟固定时间后产生一段最小合法的流式回复。

        Args:
            **_kwargs: 兼容模型 provider 接口的请求参数；基准实现不读取其值。

        Yields:
            包含 ``<say>`` 正文的单个文本增量。

        Side Effects:
            挂起约 2 毫秒以模拟首字延迟；不访问网络或写入外部数据。
        """

        await asyncio.sleep(0.002)
        yield {'text': '<say>收到压测消息。</say>'}


class _BenchmarkEmbeddingClient:
    """让事实召回走过包含余弦计算的路径。"""

    async def embed_one(self, _query: str) -> bytes:
        """返回固定维度的测试向量，确保基准经过余弦计算路径。

        Args:
            _query: 待向量化的查询文本；基准实现不读取其内容。

        Returns:
            由 64 个 float32 分量组成的小端 packed 字节串。

        Side Effects:
            不访问模型或修改存储。
        """

        return _QUERY_EMBEDDING


@dataclass(frozen=True)
class BenchmarkResult:
    """一次固定注入速率的首字延迟汇总。"""

    rate_per_second: int
    duration_seconds: int
    sample_count: int
    p50_ms: float
    p95_ms: float
    max_ms: float

    def as_dict(self) -> Dict[str, int | float]:
        """将基准结果转换为前端和 JSON 序列化使用的字段字典。

        Returns:
            使用 camelCase 键名表示速率、时长、样本数和延迟分位数的字典。

        Side Effects:
            不修改基准结果对象。
        """

        return {
            'ratePerSecond': self.rate_per_second,
            'durationSeconds': self.duration_seconds,
            'sampleCount': self.sample_count,
            'p50Ms': self.p50_ms,
            'p95Ms': self.p95_ms,
            'maxMs': self.max_ms,
        }


def _parse_args() -> tuple[int, int]:
    """解析并校验压测速率和持续时间参数。

    Returns:
        ``(rate_per_second, duration_seconds)``。

    Raises:
        SystemExit: 参数格式错误或不是正整数时由 ``argparse`` 结束进程。
    """

    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--rate', type=int, default=20, help='每秒注入消息数，默认 20')
    parser.add_argument('--seconds', type=int, default=5, help='持续秒数，默认 5')
    args = parser.parse_args()
    if args.rate <= 0 or args.seconds <= 0:
        parser.error('--rate 和 --seconds 必须为正整数')
    return args.rate, args.seconds


def _seed_recall_data(store: MemoryStore, contexts: List[ConversationContext]) -> None:
    """向基准数据库写入固定规模的事实、向量和 episode 数据。

    Args:
        store: 基准使用的记忆存储。
        contexts: 需要写入历史 episode 的会话上下文列表。

    Side Effects:
        修改内存 SQLite 中的事实、事实向量和 episode 记录。
    """

    # 固定召回规模，避免数据量差异掩盖并发调度造成的延迟变化。
    for index in range(_FACT_COUNT):
        fact_write = store.add_fact(
            1,
            FactInput(content=f'压测事实 {index}：用户在意并发、首字延迟与数据库召回。'),
            now=index + 1,
        )
        store.store_embedding(fact_write.fact_id, _QUERY_EMBEDDING)
    for context in contexts:
        for index in range(_EPISODE_COUNT):
            store.add_episode(
                context.stream.id,
                EpisodeInput(
                    summary=f'压测情节 {index}：讨论并发消息的响应速度。',
                    cues=['压测', '并发消息', '首字延迟'],
                    started_at=index + 1,
                    ended_at=index + 1,
                    message_ids=[],
                ),
                now=index + 1,
            )


def _contexts(registry: StreamRegistry, count: int) -> List[ConversationContext]:
    """为每条压测消息创建独立 direct stream 上下文。

    Args:
        registry: 负责创建稳定 stream 的注册表。
        count: 需要创建的上下文数量。

    Returns:
        使用同一 owner、不同 stream 的上下文列表。
    """

    contexts: List[ConversationContext] = []
    owner = registry.owner_person()
    for index in range(count):
        # chat.event 解析事件只对 desktop 平台推送，平台名必须是 desktop 才能采到首字事件。
        stream = registry.get_or_create_stream('desktop', 'direct', f'stream-{index}')
        contexts.append(ConversationContext(stream=stream, person=owner))
    return contexts


async def run(rate_per_second: int, duration_seconds: int) -> BenchmarkResult:
    """以均匀间隔向独立 stream 注入消息并汇总首个文本事件延迟。

    Args:
        rate_per_second: 每秒注入消息数，必须为正整数。
        duration_seconds: 注入持续秒数，必须为正整数。

    Returns:
        包含 p50、p95 和最大延迟的 ``BenchmarkResult``。

    Side Effects:
        创建并关闭内存 SQLite，运行模拟模型和聊天任务；不访问真实运行时数据。

    Raises:
        RuntimeError: 回合未进入运行态或未收集到全部首字事件。
    """

    # 每条消息使用独立 stream，避免 ChatService 的同 stream interrupt 机制改变样本。
    db = sqlite3.connect(':memory:', check_same_thread=False)
    db.row_factory = sqlite3.Row
    # 回合链路会写模块级事件账本；基准用独立的内存库，不碰运行期数据。
    event_store.configure(':memory:')
    store = MemoryStore(db)
    registry = StreamRegistry(db)
    message_count = rate_per_second * duration_seconds
    contexts = _contexts(registry, message_count)
    _seed_recall_data(store, contexts)
    starts: Dict[int, float] = {}
    first_text_at: Dict[int, float] = {}

    async def push_event(channel: str, payload: object, stream_id: int) -> None:
        """记录每个 stream 首个可见文本解析事件的时间戳。

        Args:
            channel: 推送通道名称。
            payload: 通道负载；仅处理字典型聊天事件，``text`` 与 ``say`` 都算可见文本。
            stream_id: 事件所属 stream ID；基准按 stream 统计首字延迟。

        Side Effects:
            首次收到指定 stream 的文本事件时更新内存延迟采样表。
        """

        if channel != 'chat.event' or not isinstance(payload, dict):
            return
        event = payload.get('event')
        if not isinstance(event, dict) or event.get('type') not in ('text', 'say'):
            return
        if stream_id not in first_text_at:
            first_text_at[stream_id] = perf_counter()

    provider = _BenchmarkProvider()
    chat = ChatService(
        db=db,
        chat_provider=provider,
        proactive_provider=None,
        summary_provider=None,
        push_event=push_event,
        cfg=Config(),
        vector=VectorService(store, _BenchmarkEmbeddingClient()),
    )
    tasks: List[asyncio.Task[None]] = []
    interval = 1 / rate_per_second
    benchmark_start = perf_counter()
    for index, context in enumerate(contexts):
        # 使用绝对到期时间控制注入间隔，避免每次处理耗时累积到后续样本。
        due_at = benchmark_start + index * interval
        remaining = due_at - perf_counter()
        if remaining > 0:
            await asyncio.sleep(remaining)
        started_at = perf_counter()
        # send 只入缓冲、不创建回合；显式 tick 让该 stream 的回合当场进入运行态。
        await chat.send(InboundMessage(text='压测：并发消息的首字延迟。', context=context))
        starts[context.stream.id] = started_at
        await chat._tick()
        inflight = chat._inflight.get(context.stream.id)
        if inflight is None:
            raise RuntimeError(f'压测 stream {context.stream.id} 未进入运行态')
        tasks.append(inflight.task)
    await asyncio.gather(*tasks)
    db.close()
    event_store.close()

    # 只有每个回合都产生首个文本事件时，分位数才具有完整样本语义。
    delays = sorted((first_text_at[stream_id] - started_at) * 1000 for stream_id, started_at in starts.items())
    if len(delays) != message_count:
        raise RuntimeError(f'只收集到 {len(delays)}/{message_count} 条首字延迟')
    return BenchmarkResult(
        rate_per_second=rate_per_second,
        duration_seconds=duration_seconds,
        sample_count=len(delays),
        p50_ms=round(delays[(len(delays) - 1) // 2], 2),
        p95_ms=round(delays[ceil(len(delays) * 0.95) - 1], 2),
        max_ms=round(delays[-1], 2),
    )


async def main() -> None:
    """解析基准参数、运行模拟负载并输出 JSON 结果。

    Raises:
        SystemExit: 参数格式错误时由 argparse 触发。
        RuntimeError: 基准回合或首字延迟样本不完整。

    Side Effects:
        初始化进程日志，创建隔离内存数据库并向标准输出写入一行 JSON 结果。
    """

    initialize_logging(LogConfig(level='WARNING', to_file=False))
    rate, seconds = _parse_args()
    result = await run(rate, seconds)
    print(json.dumps(result.as_dict(), ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main())
