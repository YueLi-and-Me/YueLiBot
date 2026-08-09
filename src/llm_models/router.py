"""
任务级 API 轮询。一个厂商挂了就换下一条连接，而不是让她整轮说不出话。

配置是三层关系：厂商连接 → 具体模型 → 任务引用一串候选。
每个任务拿到一个 ModelRouter，它按 selection_strategy 决定先试谁，失败了
再往后顺延；配置里排第一的就是主力。

和单条连接自己的重试是两码事：
  · OpenAiChatProvider 的 max_retries 管的是「同一个厂商再试一次」——
    网络抖动、限流这种等一会儿就好的问题。
  · 这里管的是「这个厂商靠不住了，换一家」——重试用尽仍失败才轮到它。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Sequence, TypeVar
import asyncio
import random
import time

from src.common.clock import now as current_time
from src.common.logger import get_logger
from src.config.schema import ModelCandidate
from src.llm_models.openai import LlmError, OpenAiChatProvider, resolve_base_url
from src.llm_models.snapshot import record_attempt, record_internal_request, select_candidate
from src.observe.events import current_stage_id, current_stream_id, current_turn_id, emit

logger = get_logger(__name__)

T = TypeVar('T')

# 刚失败过的厂商在这段时间内排到候选队尾。
# ★ 只有一个常量是刻意的：没有它，sequential 策略每一轮都要先撞一次已经挂掉的
#   主力、白等一个超时，用户感受到的就是「每句话都要卡半分钟」。有了它，第一次
#   失败之后的一分钟内直接从备用开始。冷却过了自动回到主力，不需要恢复探测。
PROVIDER_COOLDOWN_MS = 60_000


class ProviderHealth:
    """记住哪条连接刚失败过。多个任务共用一份——主力厂商挂了，视觉不用再撞一次。"""

    def __init__(self, cooldown_ms: int = PROVIDER_COOLDOWN_MS) -> None:
        self._cooldown_ms = cooldown_ms
        self._penalized_at: Dict[str, int] = {}

    def penalize(self, provider: str) -> None:
        self._penalized_at[provider] = current_time()

    def recover(self, provider: str) -> None:
        self._penalized_at.pop(provider, None)

    def available(self, provider: str) -> bool:
        penalized_at = self._penalized_at.get(provider)
        if penalized_at is None:
            return True
        if current_time() - penalized_at >= self._cooldown_ms:
            del self._penalized_at[provider]
            return True
        return False

    def snapshot(self) -> Dict[str, int]:
        """观察面板用：冷却中的厂商 → 还要冷却多少毫秒。"""
        now = current_time()
        return {
            provider: max(0, self._cooldown_ms - (now - at))
            for provider, at in self._penalized_at.items()
            if now - at < self._cooldown_ms
        }


class ModelRouter:
    """一个任务的候选序列。对外的 stream() 与 OpenAiChatProvider 同形，可直接替换。"""

    def __init__(self, task: str, candidates: Sequence[ModelCandidate],
                 strategy: str = 'sequential',
                 health: ProviderHealth | None = None,
                 first_token_timeout_ms: int = 30_000,
                 slow_threshold_ms: int = 8_000) -> None:
        self.task = task
        self._candidates = list(candidates)
        self._strategy = strategy
        self._health = health or ProviderHealth()
        self._first_token_timeout_ms = first_token_timeout_ms
        self._slow_threshold_ms = slow_threshold_ms
        self._clients: Dict[str, OpenAiChatProvider] = {}

    @property
    def ready(self) -> bool:
        return bool(self._candidates)

    @property
    def candidates(self) -> List[ModelCandidate]:
        return list(self._candidates)

    @property
    def model(self) -> str:
        """主力模型 ID。只用于日志和观察面板，实际用哪个看当轮轮询结果。"""
        return self._candidates[0].identifier if self._candidates else ''

    def order(self) -> List[ModelCandidate]:
        """本轮的尝试顺序。

        冷却中的厂商排到最后而不是被丢弃——全都在冷却时还得有人顶上，
        否则一次全网抖动会让她彻底哑掉，直到冷却自然过期。
        """
        if not self._candidates:
            return []
        ordered = list(self._candidates)
        if self._strategy == 'random':
            ordered = random.sample(ordered, len(ordered))
        ready = [c for c in ordered if self._health.available(c.provider)]
        cooling = [c for c in ordered if not self._health.available(c.provider)]
        return ready + cooling

    def client(self, candidate: ModelCandidate) -> OpenAiChatProvider:
        """按候选取 OpenAI 兼容客户端，建好就缓存。"""
        client = self._clients.get(candidate.name)
        if client is None:
            client = OpenAiChatProvider(
                base_url=resolve_base_url(candidate.kind, candidate.base_url),
                api_key=candidate.api_key,
                model=candidate.identifier,
                auth_type=candidate.auth_type,
                auth_name=candidate.auth_name,
                extra_body=candidate.extra_body,
                timeout_ms=candidate.timeout_ms,
                max_retries=candidate.max_retries,
                retry_interval_ms=candidate.retry_interval_ms,
            )
            self._clients[candidate.name] = client
        return client

    def _no_candidate_error(self) -> LlmError:
        return LlmError(
            'model',
            f'{self.task} 任务没有可用模型',
            f'在 models.toml 的 model_tasks.{self.task}.model_list 里填至少一个模型',
        )

    async def stream(self, messages: List[dict], temperature: float = 0.85,
                     max_tokens: int | None = None,
                     signal: asyncio.Event | None = None,
                     response_format: Dict[str, str] | None = None,
                     ) -> AsyncIterator[dict]:
        """依次尝试候选模型，直到有一个开始出字。

        ★ 一旦 yield 过内容就不能再换模型：换了等于把同一句话重新说一遍，
          用户看到的是半句话接着另外半句。这条约束和单连接内部的重试一致。
        """
        record_internal_request(
            task=self.task,
            stage=current_stage_id(),
            turn_id=current_turn_id(),
            stream_id=current_stream_id(),
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
        )
        order = self.order()
        if not order:
            raise self._no_candidate_error()

        last_error: LlmError | None = None
        for index, candidate in enumerate(order):
            yielded = False
            try:
                client = self.client(candidate)
                select_candidate(
                    model=candidate.name,
                    provider=candidate.provider,
                    kind=candidate.kind,
                )
                options: Dict[str, Any] = {}
                if response_format is not None:
                    options['response_format'] = response_format
                chunks = client.stream(
                    messages,
                    temperature,
                    max_tokens,
                    signal,
                    **options,
                )
                iterator = chunks.__aiter__()
                started = time.monotonic()
                try:
                    async with asyncio.timeout(self._first_token_timeout_ms / 1_000):
                        first_chunk = await anext(iterator)
                except StopAsyncIteration:
                    self._health.recover(candidate.provider)
                    return
                except TimeoutError as exc:
                    raise LlmError(
                        'network',
                        f'等待首字超过 {self._first_token_timeout_ms} 毫秒',
                    ) from exc

                elapsed_ms = int((time.monotonic() - started) * 1_000)
                if self._slow_threshold_ms and elapsed_ms >= self._slow_threshold_ms:
                    emit(
                        'llm_slow',
                        task=self.task,
                        model=candidate.name,
                        provider=candidate.provider,
                        elapsedMs=elapsed_ms,
                    )
                yielded = True
                yield first_chunk
                async for chunk in iterator:
                    yielded = True
                    yield chunk
                self._health.recover(candidate.provider)
                return
            except LlmError as exc:
                # 用户主动打断不是厂商的问题，不能记账也不该换人重来。
                if exc.kind != 'aborted':
                    record_attempt(
                        model=candidate.name,
                        provider=candidate.provider,
                        error_kind=exc.kind,
                        message=str(exc),
                    )
                if yielded or exc.kind == 'aborted':
                    raise
                last_error = exc
                self._health.penalize(candidate.provider)
                logger.warning(
                    'model_switch',
                    task=self.task,
                    failed_model=candidate.name,
                    failed_provider=candidate.provider,
                    reason=str(exc),
                    remaining=len(order) - index - 1,
                )

        logger.error('model_all_failed', task=self.task, tried=len(order))
        raise last_error if last_error else self._no_candidate_error()

    async def run(self, call: Callable[[ModelCandidate], Awaitable[T]]) -> T:
        """非流式任务（TTS、embedding）的轮询：第一个成功的候选说了算。"""
        record_internal_request(
            task=self.task,
            stage=current_stage_id(),
            turn_id=current_turn_id(),
            stream_id=current_stream_id(),
            messages=[],
            temperature=None,
            max_tokens=None,
            response_format=None,
        )
        order = self.order()
        if not order:
            raise self._no_candidate_error()

        last_error: Exception | None = None
        for index, candidate in enumerate(order):
            try:
                select_candidate(
                    model=candidate.name,
                    provider=candidate.provider,
                    kind=candidate.kind,
                )
                result = await call(candidate)
                self._health.recover(candidate.provider)
                return result
            except Exception as exc:
                last_error = exc
                record_attempt(
                    model=candidate.name,
                    provider=candidate.provider,
                    error_kind=exc.kind if isinstance(exc, LlmError) else type(exc).__name__,
                    message=str(exc),
                )
                self._health.penalize(candidate.provider)
                logger.warning(
                    'model_switch',
                    task=self.task,
                    failed_model=candidate.name,
                    failed_provider=candidate.provider,
                    reason=str(exc),
                    remaining=len(order) - index - 1,
                )

        logger.error('model_all_failed', task=self.task, tried=len(order))
        assert last_error is not None   # order 非空，循环至少跑过一次
        raise last_error

    def inspect(self) -> Dict[str, Any]:
        """观察面板用的只读快照，不含密钥。"""
        return {
            'task': self.task,
            'strategy': self._strategy,
            'candidates': [
                {'model': c.name, 'provider': c.provider, 'identifier': c.identifier}
                for c in self._candidates
            ],
            'coolingDown': self._health.snapshot(),
        }


class ModelRouters:
    """八个任务的路由器。共享一份 ProviderHealth，好让熔断结论跨任务复用。"""

    def __init__(self, config: Any) -> None:
        self.health = ProviderHealth()
        routing = config.routing
        self.chat = self._build('chat', routing.chat)
        self.proactive = self._build('proactive', routing.proactive)
        self.summary = self._build('summary', routing.summary)
        self.schedule = self._build('schedule', routing.schedule)
        self.vision = self._build('vision', routing.vision)
        self.expression = self._build('expression', routing.expression)
        self.tts = self._build('tts', routing.tts)
        self.embedding = self._build('embedding', routing.embedding)

    def _build(self, task: str, routing: Any) -> ModelRouter:
        return ModelRouter(
            task,
            routing.candidates,
            routing.strategy,
            self.health,
            routing.first_token_timeout_ms,
            routing.slow_threshold_ms,
        )

    def inspect(self) -> Dict[str, Any]:
        return {
            task: getattr(self, task).inspect()
            for task in ('chat', 'proactive', 'summary', 'schedule', 'vision', 'expression', 'tts', 'embedding')
        }


def create_routers(config: Any) -> ModelRouters:
    """从 pydantic Config 构造八个任务的路由器。"""
    return ModelRouters(config)
