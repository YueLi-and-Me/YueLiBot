"""模型轮询回归：某个厂商挂了要能自动换下一条连接。

覆盖三件事：
  · 失败切换：主力抛错就换备用，用户这一轮照样有回复；
  · 熔断降序：刚失败过的厂商在冷却期内排到最后，不用每轮都先撞一次；
  · 不可重放：已经吐字之后不许换模型，否则同一句话会被说两遍。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, List

import asyncio
import json
import pytest

from src.core.config.schema import Config, ModelCandidate
from src.core.llm_models.openai import LlmError
from src.core.llm_models.router import ModelRouter, ModelRouters, ProviderHealth


def _candidate(name: str, provider: str) -> ModelCandidate:
    return ModelCandidate(
        name=name, provider=provider, kind='openai',
        base_url=f'https://{provider}.example.com/v1', api_key=f'sk-{provider}',
        identifier=f'{name}-model',
    )


class _FakeClient:
    """替掉真实 HTTP 客户端：要么按脚本吐字，要么按脚本抛错。"""

    def __init__(self, chunks: List[str] | None = None, error: LlmError | None = None) -> None:
        self.model = 'fake'
        self._chunks = chunks or []
        self._error = error
        self.calls = 0

    async def stream(self, messages, temperature=0.85, max_tokens=None,
                     signal=None, **_options: Any) -> AsyncIterator[dict]:
        self.calls += 1
        for chunk in self._chunks:
            yield {'text': chunk}
        if self._error:
            raise self._error


class _ClosingClient(_FakeClient):
    """首片后持续等待，并记录上层是否立即关闭了当前候选流。"""

    def __init__(self) -> None:
        super().__init__()
        self.closed = asyncio.Event()

    async def stream(
        self,
        messages,
        temperature=0.85,
        max_tokens=None,
        signal=None,
    ) -> AsyncIterator[dict]:
        self.calls += 1
        try:
            yield {'text': '第一片'}
            await asyncio.Event().wait()
        finally:
            self.closed.set()


class _ReasoningClient(_FakeClient):
    """先返回推理，可选地再返回正文，用于视觉正文门槛回归。"""

    def __init__(self, reasoning: str, text: str = '') -> None:
        super().__init__()
        self._reasoning = reasoning
        self._text = text

    async def stream(
        self,
        messages,
        temperature=0.85,
        max_tokens=None,
        signal=None,
    ) -> AsyncIterator[dict]:
        self.calls += 1
        yield {'reasoning': self._reasoning}
        if self._text:
            yield {'text': self._text}


def _router_with(clients: dict[str, _FakeClient], strategy: str = 'sequential',
                 health: ProviderHealth | None = None) -> ModelRouter:
    """构造一个把 client() 换成假客户端的路由器。"""
    candidates = [_candidate(name, f'厂商{name}') for name in clients]
    router = ModelRouter('chat', candidates, strategy, health)
    router.client = lambda candidate: clients[candidate.name]   # type: ignore[method-assign]
    return router


async def _collect(router: ModelRouter, *, require_text: bool = False) -> str:
    text = ''
    async for chunk in router.stream(
        [{'role': 'user', 'content': '在吗'}],
        require_text=require_text,
    ):
        text += chunk.get('text', '')
    return text


async def test_switches_to_backup_when_primary_fails() -> None:
    clients = {
        '主力': _FakeClient(error=LlmError('quota', '额度用完了')),
        '备用': _FakeClient(chunks=['我在']),
    }
    router = _router_with(clients)

    assert await _collect(router) == '我在'
    assert clients['主力'].calls == 1
    assert clients['备用'].calls == 1


async def test_blocked_family_does_not_skip_another_provider() -> None:
    """同族模型由不同厂商提供时，后者仍是有效的内容策略兜底。"""
    primary = _candidate('主力', '厂商甲')
    primary.identifier = 'gemini-3.7-flash'
    backup = _candidate('备用', '厂商乙')
    backup.identifier = 'gemini-3.6-flash'
    clients = {
        '主力': _FakeClient(error=LlmError('blocked', '内容策略拒绝')),
        '备用': _FakeClient(chunks=['另一厂商可用']),
    }
    router = ModelRouter('chat', [primary, backup], 'sequential')
    router.client = lambda candidate: clients[candidate.name]  # type: ignore[method-assign]

    assert await _collect(router) == '另一厂商可用'
    assert clients['主力'].calls == 1
    assert clients['备用'].calls == 1


async def test_structured_output_switches_when_primary_returns_invalid_json() -> None:
    """JSON mode 的坏正文不得抢占成功位置，备用模型应接管本次调用。"""

    clients = {
        '主力': _FakeClient(chunks=['{"meaning":"群里，"出"表示出征"}']),
        '备用': _FakeClient(chunks=['{"meaning":"群里，“出”表示出征"}']),
    }
    router = _router_with(clients)
    text = ''

    async for chunk in router.stream(
        [{'role': 'user', 'content': '解释“出”'}],
        response_format={'type': 'json_object'},
    ):
        text += chunk.get('text', '')

    assert json.loads(text) == {'meaning': '群里，“出”表示出征'}
    assert clients['主力'].calls == 1
    assert clients['备用'].calls == 1


async def test_structured_output_switches_when_primary_returns_empty_stream() -> None:
    """普通任务可接受的空流不能被结构化任务当成合法 JSON。"""

    clients = {
        '主力': _FakeClient(),
        '备用': _FakeClient(chunks=['{"meaning":"备用结果"}']),
    }
    router = _router_with(clients)
    text = ''

    async for chunk in router.stream(
        [{'role': 'user', 'content': '解释黑话'}],
        response_format={'type': 'json_object'},
    ):
        text += chunk.get('text', '')

    assert json.loads(text) == {'meaning': '备用结果'}
    assert clients['主力'].calls == 1
    assert clients['备用'].calls == 1


async def test_structured_output_switches_when_primary_breaks_business_schema() -> None:
    """语法合法但字段互斥失败时，也必须在正文交付前切到备用候选。"""

    def validate_meaning(raw: str) -> None:
        payload = json.loads(raw)
        if set(payload) != {'meaning'}:
            raise ValueError('结果必须且只能包含 meaning 字段')

    clients = {
        '主力': _FakeClient(chunks=['{"meaning":"信息不足","insufficient":true}']),
        '备用': _FakeClient(chunks=['{"meaning":"群内含义"}']),
    }
    router = _router_with(clients)
    text = ''

    async for chunk in router.stream(
        [{'role': 'user', 'content': '解释黑话'}],
        response_format={'type': 'json_object'},
        response_validator=validate_meaning,
    ):
        text += chunk.get('text', '')

    assert json.loads(text) == {'meaning': '群内含义'}
    assert clients['主力'].calls == 1
    assert clients['备用'].calls == 1


async def test_required_text_switches_when_primary_returns_no_chunks() -> None:
    """视觉任务要求正文时，零增量响应必须继续尝试备用候选。"""
    clients = {
        '主力': _FakeClient(),
        '备用': _FakeClient(chunks=['我在']),
    }
    router = _router_with(clients)

    assert await _collect(router, require_text=True) == '我在'
    assert clients['主力'].calls == 1
    assert clients['备用'].calls == 1


async def test_required_text_switches_after_reasoning_only_candidate() -> None:
    """图片描述不能把只有思考、没有最终正文的响应当作成功。"""
    clients = {
        '主力': _ReasoningClient('还在分析图片'),
        '备用': _FakeClient(chunks=['一只白猫']),
    }
    router = _router_with(clients)

    assert await _collect(router, require_text=True) == '一只白猫'
    assert clients['主力'].calls == 1
    assert clients['备用'].calls == 1


async def test_required_text_rejects_unsupported_image_reasoning() -> None:
    """模型明确报告 Unsupported Image 时，不能缓存它编出的模糊占位描述。"""
    clients = {
        '主力': _ReasoningClient('Unsupported Image：我是纯文本模型', '内容不清晰。'),
        '备用': _FakeClient(chunks=['一只白猫']),
    }
    router = _router_with(clients)

    assert await _collect(router, require_text=True) == '一只白猫'
    assert clients['主力'].calls == 1
    assert clients['备用'].calls == 1


async def test_all_candidates_failing_raises_the_last_error() -> None:
    clients = {
        '主力': _FakeClient(error=LlmError('quota', '额度用完了')),
        '备用': _FakeClient(error=LlmError('auth', 'Key 不对')),
    }
    router = _router_with(clients)

    with pytest.raises(LlmError) as excinfo:
        await _collect(router)

    assert 'Key 不对' in str(excinfo.value)


async def test_no_candidate_reports_where_to_fix_it() -> None:
    router = ModelRouter('chat', [], 'sequential')

    with pytest.raises(LlmError) as excinfo:
        await _collect(router)

    assert 'model_tasks.chat' in excinfo.value.detail


async def test_content_already_sent_is_never_replayed() -> None:
    """已经输出部分内容后不得切换模型继续拼接同一回复。"""
    clients = {
        '主力': _FakeClient(chunks=['你好'], error=LlmError('network', '中途断了')),
        '备用': _FakeClient(chunks=['我在']),
    }
    router = _router_with(clients)

    with pytest.raises(LlmError):
        await _collect(router)

    assert clients['备用'].calls == 0, '已经开口了就不能换人重说一遍'


async def test_closing_router_stream_closes_selected_client_immediately() -> None:
    """上层提前结束消费时，路由器必须同步关闭仍挂着的候选 HTTP 流。"""
    client = _ClosingClient()
    router = _router_with({'主力': client})
    stream = router.stream([{'role': 'user', 'content': '在吗'}])

    assert await anext(stream) == {'text': '第一片'}
    await stream.aclose()

    assert client.closed.is_set()


async def test_user_interrupt_is_not_blamed_on_the_provider() -> None:
    clients = {
        '主力': _FakeClient(error=LlmError('aborted', '生成已中断')),
        '备用': _FakeClient(chunks=['我在']),
    }
    health = ProviderHealth()
    router = _router_with(clients, health=health)

    with pytest.raises(LlmError):
        await _collect(router)

    assert clients['备用'].calls == 0, '用户打断不该触发换模型'
    assert health.available('厂商主力') is True, '用户打断不该记到厂商头上'


async def test_failed_provider_goes_last_until_cooldown_expires() -> None:
    """第一次失败之后，下一轮直接从备用开始，不用再白等一次超时。"""
    clients = {
        '主力': _FakeClient(error=LlmError('network', '连不上')),
        '备用': _FakeClient(chunks=['我在']),
    }
    router = _router_with(clients)

    await _collect(router)
    assert clients['主力'].calls == 1

    await _collect(router)
    assert clients['主力'].calls == 1, '冷却期内不该再撞挂掉的主力'
    assert clients['备用'].calls == 2


def test_cooldown_expiry_returns_the_provider_to_the_front() -> None:
    health = ProviderHealth(cooldown_ms=0)
    health.penalize('厂商主力')

    # 冷却时长为 0，下一次查询就该恢复——不需要额外的探测请求
    assert health.available('厂商主力') is True
    assert health.snapshot() == {}


def test_everything_cooling_down_still_gets_tried() -> None:
    """所有模型请求失败时必须记录完整错误并进入冷却，不能静默等待冷却结束。"""
    health = ProviderHealth()
    candidates = [_candidate('主力', '厂商A'), _candidate('备用', '厂商B')]
    router = ModelRouter('chat', candidates, 'sequential', health)
    health.penalize('厂商A')
    health.penalize('厂商B')

    assert len(router.order()) == 2


def test_random_strategy_keeps_every_candidate() -> None:
    """随机只改顺序，不能漏掉任何一条候选——漏了就等于少一层保险。"""
    candidates = [_candidate(f'模型{i}', f'厂商{i}') for i in range(4)]
    router = ModelRouter('chat', candidates, 'random')

    for _ in range(20):
        assert {c.name for c in router.order()} == {c.name for c in candidates}


def test_balance_strategy_rotates_healthy_candidates() -> None:
    """负载均衡应逐轮更换首选模型，同时保留完整故障切换顺序。"""
    candidates = [_candidate(f'模型{i}', f'厂商{i}') for i in range(3)]
    router = ModelRouter('chat', candidates, 'balance')

    assert [[candidate.name for candidate in router.order()] for _ in range(4)] == [
        ['模型0', '模型1', '模型2'],
        ['模型1', '模型2', '模型0'],
        ['模型2', '模型0', '模型1'],
        ['模型0', '模型1', '模型2'],
    ]


def test_balance_strategy_skips_cooling_candidate_until_failover() -> None:
    """冷却模型不能占用首选轮次，但仍要留在末尾供最终故障切换。"""
    health = ProviderHealth()
    candidates = [_candidate(f'模型{i}', f'厂商{i}') for i in range(3)]
    router = ModelRouter('chat', candidates, 'balance', health)
    health.penalize('厂商1')

    assert [[candidate.name for candidate in router.order()] for _ in range(2)] == [
        ['模型0', '模型2', '模型1'],
        ['模型2', '模型0', '模型1'],
    ]


def test_health_is_shared_so_one_task_warns_the_others() -> None:
    """对话发现某厂商挂了，视觉不用再撞一次同一堵墙。"""
    health = ProviderHealth()
    chat = ModelRouter('chat', [_candidate('chat', '共用厂商')], 'sequential', health)
    vision = ModelRouter('vision', [_candidate('vision', '共用厂商')], 'sequential', health)

    health.penalize('共用厂商')

    assert health.available('共用厂商') is False
    # 比较冷却中的厂商集合而不是剩余毫秒数：snapshot() 每次调用都现取时钟算余量，
    # 两次 inspect() 之间只要走过 1 毫秒，余量就差 1，全等比较会在机器繁忙时偶发失败。
    # 本用例要证明的是「两个任务看到同一个厂商在冷却」，与余量的精确值无关。
    assert set(chat.inspect()['coolingDown']) == set(vision.inspect()['coolingDown'])
    assert '共用厂商' in chat.inspect()['coolingDown']


def test_model_routers_exposes_all_declared_tasks() -> None:
    routers = ModelRouters(Config())
    expected = {
        'chat', 'planner', 'replyer', 'scene', 'memory', 'proactive', 'summary', 'schedule',
        'vision', 'expression', 'tts', 'embedding',
    }

    assert set(routers.inspect()) == expected
    for task in expected:
        assert routers.for_task(task).task == task


def test_model_routers_rejects_unknown_task_explicitly() -> None:
    routers = ModelRouters(Config())

    with pytest.raises(ValueError, match='未知模型任务：unknown'):
        routers.for_task('unknown')


async def test_run_rotates_for_non_streaming_tasks() -> None:
    """TTS / embedding 走的是 run()，同样要能换厂商。"""
    tried: list[str] = []

    async def call(candidate: ModelCandidate) -> str:
        tried.append(candidate.name)
        if candidate.name == '主力':
            raise RuntimeError('厂商 500')
        return '合成好了'

    router = ModelRouter('tts', [_candidate('主力', '厂商A'), _candidate('备用', '厂商B')])

    assert await router.run(call) == '合成好了'
    assert tried == ['主力', '备用']


def test_inspect_never_leaks_keys() -> None:
    router = ModelRouter('chat', [_candidate('chat', '厂商A')], 'sequential')

    assert 'sk-厂商A' not in repr(router.inspect())


async def test_background_stream_prints_complete_model_call(monkeypatch) -> None:
    """没有用户回合承载时，路由收尾应把正文与推理交给独立控制台出口。"""
    import src.core.llm_models.router as router_module
    from src.core.services.console.turn_panel import take_calls

    take_calls()
    rendered = []
    monkeypatch.setattr(router_module, 'render_model_call', rendered.append)
    router = _router_with({'主力': _ReasoningClient('完整推理', '完整正文')})

    assert await _collect(router, require_text=True) == '完整正文'
    assert len(rendered) == 1
    assert rendered[0].reasoning == '完整推理'
    assert rendered[0].text == '完整正文'


async def test_turn_stream_is_not_printed_twice(monkeypatch) -> None:
    """用户回合已接住调用时不走独立出口，正文只在轮末面板展示一次。"""
    import src.core.llm_models.router as router_module
    from src.core.services.console.turn_panel import begin_turn, take_calls

    rendered = []
    monkeypatch.setattr(router_module, 'render_model_call', rendered.append)
    begin_turn()
    router = _router_with({'主力': _ReasoningClient('回合推理', '回合正文')})

    assert await _collect(router, require_text=True) == '回合正文'
    calls = take_calls()
    assert rendered == []
    assert len(calls) == 1
    assert calls[0].reasoning == '回合推理'
    assert calls[0].text == '回合正文'


async def test_prompt_record_written_per_call(tmp_path) -> None:
    """每次模型调用都落一份分阶段记录，成功也落。

    多级 Agent 下同一回合有多次调用，控制台只看得到最后一条可见产物；记录按
    任务分目录，是「哪一级收到什么、答了什么」的唯一可回溯来源。
    """
    from src.core.llm_models import snapshot

    snapshot.configure_exchanges(tmp_path, 5)
    try:
        clients = {'主力': _FakeClient(chunks=['我', '在'])}
        router = _router_with(clients)

        assert await _collect(router) == '我在'

        files = list((tmp_path / 'chat').glob('*.json'))
        assert len(files) == 1
        record = json.loads(files[0].read_text(encoding='utf-8'))
        assert record['task'] == 'chat'
        assert record['response']['text'] == '我在'
        assert record['response']['chunks'] == 2
        assert record['request']['messages'] == [{'role': 'user', 'content': '在吗'}]
        assert record['error'] is None
        assert record['timing']['totalMs'] >= 0
    finally:
        snapshot.configure_exchanges(None)


async def test_prompt_record_written_on_failure(tmp_path) -> None:
    """全部候选失败时同样落记录，并带上失败类型——否则最该查的那次反而没有。"""
    from src.core.llm_models import snapshot

    snapshot.configure_exchanges(tmp_path, 5)
    try:
        clients = {'主力': _FakeClient(error=LlmError('quota', '额度用完了'))}
        router = _router_with(clients)

        with pytest.raises(LlmError):
            await _collect(router)

        files = list((tmp_path / 'chat').glob('*.json'))
        assert len(files) == 1
        record = json.loads(files[0].read_text(encoding='utf-8'))
        assert record['error'] == {'type': 'quota', 'message': '额度用完了'}
        assert record['response']['text'] == ''
    finally:
        snapshot.configure_exchanges(None)


async def test_prompt_records_pruned_per_task(tmp_path) -> None:
    """保留份数按任务目录各自计数，超出的从最旧删起。"""
    from src.core.llm_models import snapshot

    snapshot.configure_exchanges(tmp_path, 2)
    try:
        for _ in range(4):
            router = _router_with({'主力': _FakeClient(chunks=['嗯'])})
            assert await _collect(router) == '嗯'

        assert len(list((tmp_path / 'chat').glob('*.json'))) == 2
    finally:
        snapshot.configure_exchanges(None)
