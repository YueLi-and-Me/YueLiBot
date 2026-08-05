"""M2 平台入站并发基线：20 条/秒下的首个文本事件延迟。"""

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

from src.common.logger import initialize_logging
from src.memory.store import EpisodeInput, FactInput, MemoryStore
from src.platform_io.registry import StreamRegistry
from src.platform_io.types import ConversationContext, InboundMessage
from src.services.chat import ChatService
from src.services.vector import VectorService


_FACT_COUNT = 24
_EPISODE_COUNT = 12
_QUERY_EMBEDDING = pack('<64f', *([0.5] * 64))


class _BenchmarkProvider:
    """只模拟模型首字可用，不把真实网络抖动混入接入层基线。"""

    async def stream(self, **_kwargs: object) -> AsyncIterator[Dict[str, str]]:
        await asyncio.sleep(0.002)
        yield {'text': '<say>收到压测消息。</say>'}


class _BenchmarkEmbeddingClient:
    """让事实召回走过包含余弦计算的路径。"""

    async def embed_one(self, _query: str) -> bytes:
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
        return {
            'ratePerSecond': self.rate_per_second,
            'durationSeconds': self.duration_seconds,
            'sampleCount': self.sample_count,
            'p50Ms': self.p50_ms,
            'p95Ms': self.p95_ms,
            'maxMs': self.max_ms,
        }


def _parse_args() -> tuple[int, int]:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--rate', type=int, default=20, help='每秒注入消息数，默认 20')
    parser.add_argument('--seconds', type=int, default=5, help='持续秒数，默认 5')
    args = parser.parse_args()
    if args.rate <= 0 or args.seconds <= 0:
        parser.error('--rate 和 --seconds 必须为正整数')
    return args.rate, args.seconds


def _seed_recall_data(store: MemoryStore, contexts: List[ConversationContext]) -> None:
    for index in range(_FACT_COUNT):
        fact_id = store.add_fact(
            1,
            FactInput(content=f'压测事实 {index}：用户在意并发、首字延迟与数据库召回。'),
            now=index + 1,
        )
        store.store_embedding(fact_id, _QUERY_EMBEDDING)
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
    contexts: List[ConversationContext] = []
    owner = registry.owner_person()
    for index in range(count):
        stream = registry.get_or_create_stream('benchmark', 'direct', f'stream-{index}')
        contexts.append(ConversationContext(stream=stream, person=owner))
    return contexts


async def run(rate_per_second: int, duration_seconds: int) -> BenchmarkResult:
    """以均匀间隔注入独立 stream，避免同 stream 的正常打断干扰数据。"""
    db = sqlite3.connect(':memory:', check_same_thread=False)
    db.row_factory = sqlite3.Row
    store = MemoryStore(db)
    registry = StreamRegistry(db)
    message_count = rate_per_second * duration_seconds
    contexts = _contexts(registry, message_count)
    _seed_recall_data(store, contexts)
    starts: Dict[int, float] = {}
    first_text_at: Dict[int, float] = {}

    async def push_event(channel: str, payload: object, _stream_id: int) -> None:
        if channel != 'chat.event' or not isinstance(payload, dict):
            return
        event = payload.get('event')
        if not isinstance(event, dict) or event.get('type') != 'text':
            return
        turn_id = payload.get('turnId')
        if isinstance(turn_id, int) and turn_id not in first_text_at:
            first_text_at[turn_id] = perf_counter()

    provider = _BenchmarkProvider()
    chat = ChatService(
        db=db,
        chat_provider=provider,
        proactive_provider=None,
        summary_provider=None,
        push_event=push_event,
        vector=VectorService(store, _BenchmarkEmbeddingClient()),
    )
    tasks: List[asyncio.Task[None]] = []
    interval = 1 / rate_per_second
    benchmark_start = perf_counter()
    for index, context in enumerate(contexts):
        due_at = benchmark_start + index * interval
        remaining = due_at - perf_counter()
        if remaining > 0:
            await asyncio.sleep(remaining)
        started_at = perf_counter()
        turn_id = await chat.send(InboundMessage(text='压测：并发消息的首字延迟。', context=context))
        starts[turn_id] = started_at
        task = chat._inflight.get(context.stream.id)
        if task is None:
            raise RuntimeError(f'压测 turn {turn_id} 未进入运行态')
        tasks.append(task.task)
    await asyncio.gather(*tasks)
    db.close()

    delays = sorted((first_text_at[turn_id] - started_at) * 1000 for turn_id, started_at in starts.items())
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
    initialize_logging('WARNING')
    rate, seconds = _parse_args()
    result = await run(rate, seconds)
    print(json.dumps(result.as_dict(), ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main())
