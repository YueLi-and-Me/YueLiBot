"""实现任务级模型候选路由、失败切换和提供者健康状态管理。

配置将提供者连接、具体模型和任务候选列表分层关联；``ModelRouter`` 按选择策略
执行候选调用，提供者连续失败时进入冷却并降低后续请求中的优先级。提供者内部
重试仅处理同一候选的瞬时网络错误，候选路由负责在内部重试耗尽后切换到下一项。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Sequence, TypeVar
import asyncio
import random
import time

from src.core.common.clock import now as current_time
from src.core.common.logger import get_logger
from src.core.config.schema import ModelCandidate
from src.core.llm_models.openai import LlmError, OpenAiChatProvider, resolve_base_url
from src.core.llm_models.snapshot import (
    current_render_params,
    dump_exchange,
    record_attempt,
    record_internal_request,
    select_candidate,
)
from src.core.observe.events import current_stage_id, current_stream_id, current_turn_id, emit
from src.core.services.turn_panel import ModelCall, note_model_call

logger = get_logger(__name__)

T = TypeVar('T')


class _ExchangeRecord:
    """聚合一次模型调用的产出，并在调用收尾时落一份分阶段记录。

    路由层是所有模型任务的唯一必经出口，聚合放在这里意味着新增一级 Agent
    （planner / replyer 拆分之后会有更多级）不需要各自记得补观测。

    :ivar task: 模型任务名，决定记录写进哪个子目录。
    """

    def __init__(self, task: str) -> None:
        self.task = task
        self._started = time.monotonic()
        self._text: list[str] = []
        self._reasoning: list[str] = []
        self._tool_calls: list[dict] = []
        self._chunks = 0
        self._first_token_ms: int | None = None
        self._error_type = ''
        self._error = ''

    def select(self, model: str, provider: str) -> None:
        """记录本次实际选中的候选；候选切换时以最后一次为准。"""
        self._model = model
        self._provider = provider

    def first_token(self, elapsed_ms: int) -> None:
        """记录首字耗时；候选切换后以真正产出的那一次为准。"""
        self._first_token_ms = elapsed_ms

    def observe(self, chunk: dict) -> None:
        """累计一条流式增量的可见文本与推理文本。"""
        self._chunks += 1
        text = chunk.get('text')
        if text:
            self._text.append(text)
        reasoning = chunk.get('reasoning')
        if reasoning:
            self._reasoning.append(reasoning)
        tool_calls = chunk.get('tool_calls')
        if tool_calls:
            self._tool_calls.extend(tool_calls)

    def fail(self, error_type: str, message: str) -> None:
        """记录本次调用最终失败的类型与消息。"""
        self._error_type = error_type
        self._error = message

    def write(self) -> None:
        """落盘本次调用记录。

        观测失败不能影响对话：这里捕获写盘异常并只记一行警告。落盘属于旁路
        设施，磁盘满或权限不足时让正在进行的回复整个失败是更坏的结果；异常
        本身仍然完整暴露在日志里，不做静默吞掉。
        """
        try:
            path = dump_exchange(
                task=self.task,
                model=getattr(self, '_model', ''),
                provider=getattr(self, '_provider', ''),
                output_text=''.join(self._text),
                reasoning_text=''.join(self._reasoning),
                tool_calls=self._tool_calls or None,
                chunk_count=self._chunks,
                first_token_ms=self._first_token_ms,
                total_ms=int((time.monotonic() - self._started) * 1_000),
                error_type=self._error_type,
                error=self._error,
            )
        except OSError as exc:
            # 落盘失败不能连带丢掉面板：写不进磁盘时，终端上那份现场就是唯一
            # 还能看到「这一级做了什么」的地方。
            logger.warning('prompt_record_failed', task=self.task, error=str(exc))
            path = None
        if path is not None:
            emit('prompt_record', task=self.task, path=str(path))
        # 同一份事实再交给回合面板。事件账本是给事后查的，面板是给当场看的；
        # 路由层不知道自己属于哪个回合，收集靠 ContextVar 在 Task 内传递。
        note_model_call(ModelCall(
            task=self.task,
            model=getattr(self, '_model', ''),
            provider=getattr(self, '_provider', ''),
            first_token_ms=self._first_token_ms,
            total_ms=int((time.monotonic() - self._started) * 1_000),
            reasoning=''.join(self._reasoning),
            text=''.join(self._text),
            tool_calls=list(self._tool_calls),
            record_path=str(path) if path is not None else '',
            error=f'{self._error_type}：{self._error}' if self._error_type else '',
        ))


# 刚失败过的厂商在这段时间内排到候选队尾。
# 冷却期避免 sequential 策略在每轮请求中重复等待已失败的厂商；到期后自动恢复候选。
PROVIDER_COOLDOWN_MS = 60_000


class ProviderHealth:
    """记录厂商最近一次失败时间，并在冷却期内降低其候选优先级。

    多个任务路由器共享同一实例，使一个厂商的失败状态在任务之间生效。
    """

    def __init__(self, cooldown_ms: int = PROVIDER_COOLDOWN_MS) -> None:
        """创建厂商健康状态表。

        :param cooldown_ms: 单次失败后的冷却时长，单位为毫秒，默认值为 60000。
        副作用：初始化空的失败时间映射，不访问网络。
        """
        self._cooldown_ms = cooldown_ms
        self._penalized_at: Dict[str, int] = {}

    def penalize(self, provider: str) -> None:
        """记录厂商当前时间的失败。

        :param provider: 厂商名称。
        副作用：覆盖该厂商最近失败时间。
        """
        self._penalized_at[provider] = current_time()

    def recover(self, provider: str) -> None:
        """清除厂商的失败惩罚。

        :param provider: 厂商名称。
        副作用：从失败时间映射移除该厂商；不存在时无操作。
        """
        self._penalized_at.pop(provider, None)

    def available(self, provider: str) -> bool:
        """判断厂商是否已过冷却期。

        :param provider: 厂商名称。
        :return: 未被惩罚或冷却已结束时返回 `True`。
        副作用：冷却结束时从映射中删除过期惩罚。
        """
        penalized_at = self._penalized_at.get(provider)
        if penalized_at is None:
            return True
        if current_time() - penalized_at >= self._cooldown_ms:
            del self._penalized_at[provider]
            return True
        return False

    def snapshot(self) -> Dict[str, int]:
        """生成厂商冷却状态的只读快照。

        :return: 冷却中厂商到剩余冷却毫秒数的映射；已恢复的厂商不会出现在结果中。

        副作用：
            读取当前系统时钟，不清理过期映射；状态清理由 ``available`` 负责。
        """
        now = current_time()
        return {
            provider: max(0, self._cooldown_ms - (now - at))
            for provider, at in self._penalized_at.items()
            if now - at < self._cooldown_ms
        }


class ModelRouter:
    """管理一个任务的模型候选序列和流式/非流式切换。

    对外提供与 provider 兼容的 `stream` 方法；候选配置顺序决定主力和备用，
    厂商健康状态决定当前轮次的实际尝试顺序。
    """

    def __init__(self, task: str, candidates: Sequence[ModelCandidate],
                 strategy: str = 'sequential',
                 health: ProviderHealth | None = None,
                 first_token_timeout_ms: int = 30_000,
                 slow_threshold_ms: int = 8_000) -> None:
        """创建一个任务级模型路由器。

        :param task: 任务名称。
        :param candidates: 按优先级排列的模型候选序列。
        :param strategy: `sequential` 或 `random`，默认值为 `sequential`。
        :param health: 可选共享厂商健康状态；为空时创建新实例。
        :param first_token_timeout_ms: 首个增量的任务级超时，默认值为 30000。
        :param slow_threshold_ms: 慢响应记录阈值，默认值为 8000；0 表示禁用。
        副作用：复制候选序列并初始化客户端缓存，不建立模型连接。
        """
        self.task = task
        self._candidates = list(candidates)
        self._strategy = strategy
        self._health = health or ProviderHealth()
        self._first_token_timeout_ms = first_token_timeout_ms
        self._slow_threshold_ms = slow_threshold_ms
        self._clients: Dict[str, OpenAiChatProvider] = {}

    @property
    def ready(self) -> bool:
        """判断当前任务是否至少配置一个模型候选。

        :return: 候选序列非空时返回 `True`。
        副作用：不修改路由状态。
        """
        return bool(self._candidates)

    @property
    def candidates(self) -> List[ModelCandidate]:
        """返回模型候选的浅复制列表。

        :return: 当前候选对象列表的副本。
        副作用：不修改内部候选序列。
        """
        return list(self._candidates)

    @property
    def model(self) -> str:
        """返回候选序列首项的模型标识，供日志和观察面板展示。

        :return: 首个候选的 ``identifier``；没有候选时返回空字符串。该值不代表当前轮次
            一定实际调用的模型，实际调用顺序由 ``order`` 决定。

        副作用：
            仅读取候选序列，不创建客户端或发起模型请求。
        """
        return self._candidates[0].identifier if self._candidates else ''

    def order(self) -> List[ModelCandidate]:
        """本轮的尝试顺序。

        冷却中的服务商排到最后而不是被丢弃；当所有候选都处于冷却期时仍保留完整候选
        集合，以便本轮可以继续尝试并记录实际故障。

        :return: 按配置策略排序、可用候选在前且冷却候选在后的新列表；没有候选时返回空列表。

        副作用：
            读取并可能清理已过期的厂商冷却记录；不修改候选配置。
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
        """获取候选对应的 OpenAI 兼容客户端，并按候选名称缓存实例。

        :param candidate: 已校验的模型候选配置。

        :return: 与候选连接参数和模型标识绑定的 ``OpenAiChatProvider``。

        :raises ValueError: 候选 URL、鉴权方式或超时重试配置不合法时由 provider 构造函数抛出。

        副作用：
            首次访问某候选时创建并缓存客户端；不在此方法中建立网络连接。
        """
        client = self._clients.get(candidate.name)
        if client is None:
            client = OpenAiChatProvider(
                base_url=resolve_base_url(candidate.kind, candidate.base_url),
                api_key=candidate.api_key,
                model=candidate.identifier,
                auth_type=candidate.auth_type,
                auth_name=candidate.auth_name,
                extra_body=candidate.extra_body,
                reasoning_parse_mode=candidate.reasoning_parse_mode,
                headers=candidate.default_headers,
                query=candidate.default_query,
                timeout_ms=candidate.timeout_ms,
                max_retries=candidate.max_retries,
                retry_interval_ms=candidate.retry_interval_ms,
            )
            self._clients[candidate.name] = client
        return client

    def _no_candidate_error(self) -> LlmError:
        """构造当前任务没有可用模型时的标准错误。

        :return: 包含任务名称和配置修复位置的 :class:`LlmError`。
        副作用：不修改路由状态。
        """
        return LlmError(
            'model',
            f'{self.task} 任务没有可用模型',
            f'在 models.toml 的 model_tasks.{self.task}.model_list 里填至少一个模型',
        )

    async def stream(self, messages: List[dict], temperature: float = 0.85,
                     max_tokens: int | None = None,
                     signal: asyncio.Event | None = None,
                     response_format: Dict[str, str] | None = None,
                     tools: List[dict] | None = None,
                     ) -> AsyncIterator[dict]:
        """依次尝试候选模型，直到一个候选产生首个输出增量。

        一旦向调用方产生内容就不再切换候选，避免同一请求重复输出；首 token 超时
        包含下层 provider 的内部重试时间。

        :param messages: OpenAI 兼容消息列表。
        :param temperature: 采样温度，默认 ``0.85``。
        :param max_tokens: 可选最大输出 token 数。
        :param signal: 可选取消事件，传递给底层 provider。
        :param response_format: 可选响应格式配置。
        :param tools: 可选的工具声明列表，透传给候选 provider。

        :yield: 底层 provider 返回的增量字典，顺序与实际模型流一致。

        :raises LlmError: 所有候选均失败、首 token 超时、模型响应异常或调用被中断。
        :raises asyncio.CancelledError: 调用方取消异步生成器时传播。

        副作用：
            记录内部请求、候选选择、失败尝试和慢响应观测；更新共享厂商健康状态，
            可能为候选创建缓存客户端并发起网络请求。

        性能：
            候选排序和健康判断与候选数量线性相关；模型请求耗时占主要成本。
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
            render_params=current_render_params(),
        )
        # 分阶段调用记录的聚合状态。写在这里而不是各调用点，是因为路由层是所有
        # 模型任务的唯一必经出口：planner、replyer、表达选择、情景分析共用它，
        # 在这里落盘才能保证新增一级 Agent 时不必再记得补一次观测。
        exchange = _ExchangeRecord(task=self.task)
        try:
            async for chunk in self._stream_candidates(
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                signal=signal,
                response_format=response_format,
                tools=tools,
                exchange=exchange,
            ):
                exchange.observe(chunk)
                yield chunk
        except LlmError as exc:
            exchange.fail(exc.kind, str(exc))
            raise
        finally:
            exchange.write()

    async def _stream_candidates(self,
                                 *,
                                 messages: List[dict],
                                 temperature: float,
                                 max_tokens: int | None,
                                 signal: asyncio.Event | None,
                                 response_format: Dict[str, str] | None,
                                 tools: List[dict] | None,
                                 exchange: '_ExchangeRecord',
                                 ) -> AsyncIterator[dict]:
        """按候选顺序实际发起请求，产生增量并在切换候选时更新记录状态。

        与 ``stream`` 拆开只为让收尾落盘有一个唯一出口：候选循环里有多处
        ``return`` 与 ``raise``，把 ``finally`` 放在这一层会随候选切换重复触发。

        :param exchange: 本次调用的记录聚合器；候选选定时回填模型与服务商。
        :yield: 底层 provider 返回的增量字典。
        :raises LlmError: 与 ``stream`` 相同。
        """
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
                exchange.select(candidate.name, candidate.provider)
                options: Dict[str, Any] = {}
                if response_format is not None:
                    options['response_format'] = response_format
                if tools:
                    options['tools'] = tools
                effective_temperature = (
                    candidate.temperature
                    if candidate.temperature is not None
                    else temperature
                )
                effective_max_tokens = (
                    candidate.max_tokens
                    if candidate.max_tokens is not None
                    else max_tokens
                )
                chunks = client.stream(
                    messages,
                    effective_temperature,
                    effective_max_tokens,
                    signal,
                    **options,
                )
                iterator = chunks.__aiter__()
                started = time.monotonic()
                try:
                    # 任务级首字窗口包含下层内部重试，窗口耗尽会在重试跑满前切换候选。
                    async with asyncio.timeout(self._first_token_timeout_ms / 1_000):
                        first_chunk = await anext(iterator)
                except StopAsyncIteration:
                    self._health.recover(candidate.provider)
                    return
                except TimeoutError as exc:
                    raise LlmError(
                        'timeout',
                        f'等待首字超过 {self._first_token_timeout_ms} 毫秒',
                    ) from exc

                elapsed_ms = int((time.monotonic() - started) * 1_000)
                exchange.first_token(elapsed_ms)
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
                # 用户主动打断不属于服务商故障，不记录为失败，也不切换到备用服务商。
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
        """执行非流式任务，并按候选顺序返回第一个成功结果。

        :param call: 接收一个模型候选并异步返回任务结果的回调；回调异常触发下一候选。

        :return: 第一个成功候选产生的任务结果，类型由回调返回值决定。

        :raises LlmError: 未配置候选时抛出标准模型错误。
        :raises Exception: 所有候选均失败时重新抛出最后一次异常。

        副作用：
            记录内部请求和每次候选尝试，更新共享健康状态，并可能发起多次模型请求。
        """
        record_internal_request(
            task=self.task,
            stage=current_stage_id(),
            turn_id=current_turn_id(),
            stream_id=current_stream_id(),
            messages=[],
            temperature=None,
            max_tokens=None,
            response_format=None,
            render_params=None,
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
        """生成不包含密钥的任务路由只读快照。

        :return: 包含任务名、选择策略、候选模型摘要和冷却状态的可序列化字典。

        副作用：
            仅读取路由和健康状态，不修改候选、客户端或认证信息。
        """
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
    """为各类模型任务构造路由器，并共享一份厂商健康状态。"""

    def __init__(self, config: Any) -> None:
        """按任务构造全部模型路由，决策与表达各占一个独立槽。

        :param config: 含 `routing` 属性的配置对象。
        副作用：创建八个 `ModelRouter` 和一个共享 `ProviderHealth`，不发起模型请求。
        :raises AttributeError: 配置缺少路由字段时传播属性错误。
        """
        self.health = ProviderHealth()
        routing = config.routing
        self.chat = self._build('chat', routing.chat)
        self.proactive = self._build('proactive', routing.proactive)
        self.summary = self._build('summary', routing.summary)
        self.schedule = self._build('schedule', routing.schedule)
        self.vision = self._build('vision', routing.vision)
        self.expression = self._build('expression', routing.expression)
        self.planner = self._build('planner', routing.planner)
        self.replyer = self._build('replyer', routing.replyer)
        self.tts = self._build('tts', routing.tts)
        self.embedding = self._build('embedding', routing.embedding)
        self._routers: Dict[str, ModelRouter] = {
            'chat': self.chat,
            'proactive': self.proactive,
            'summary': self.summary,
            'schedule': self.schedule,
            'vision': self.vision,
            'expression': self.expression,
            'planner': self.planner,
            'replyer': self.replyer,
            'tts': self.tts,
            'embedding': self.embedding,
        }

    def _build(self, task: str, routing: Any) -> ModelRouter:
        """把单个配置路由转换为 `ModelRouter`。

        :param task: 任务名称。
        :param routing: 含 candidates、strategy 和超时字段的配置对象。
        :return: 使用共享健康状态的新路由器。
        副作用：不建立模型连接。
        """
        return ModelRouter(
            task,
            routing.candidates,
            routing.strategy,
            self.health,
            routing.first_token_timeout_ms,
            routing.slow_threshold_ms,
        )

    def inspect(self) -> Dict[str, Any]:
        """生成所有任务路由的只读观测快照。

        :return: 任务名称到各路由 `inspect()` 结果的字典。
        副作用：只读取路由状态，不修改候选或健康记录。
        """
        return {
            task: self.for_task(task).inspect()
            for task in self._routers
        }

    def for_task(self, task: str) -> ModelRouter:
        """按封闭任务名取得对应模型路由。

        :param task: chat、proactive、summary、schedule、vision、expression、tts
                或 embedding。
        :return: 对应的任务级模型路由。
        :raises ValueError: 任务名不在已声明的八类任务中。
        副作用：只读取路由映射，不创建客户端或发起模型请求。
        """
        try:
            return self._routers[task]
        except KeyError as exc:
            raise ValueError(f'未知模型任务：{task}') from exc


def create_routers(config: Any) -> ModelRouters:
    """从完整应用配置构造各模型任务路由器。

    :param config: 提供 ``routing`` 及各任务候选配置的配置对象。

    :return: 共享厂商健康状态的 ``ModelRouters`` 实例。

    :raises AttributeError: 配置缺少路由字段。
    :raises ValueError: 任一任务候选或路由参数校验失败。
    """
    return ModelRouters(config)
