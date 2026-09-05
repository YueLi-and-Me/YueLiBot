"""视觉模型配置和多模态消息构造回归测试。"""

from __future__ import annotations

from typing import Any, AsyncIterator

from src.core.config.schema import Config, ModelCandidate, TaskRouting
from src.core.llm_models.openai import LlmError
from src.core.llm_models.router import ModelRouter
from src.desktop.vision import VisionService


class RecordingVisionProvider:
    """记录视觉请求，不调用真实模型接口。"""

    model = 'vision-test'

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def stream(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.85,
        max_tokens: int | None = None,
        signal: Any = None,
    ) -> AsyncIterator[dict[str, Any]]:
        self.messages = messages
        yield {'text': '正在打开装备菜单'}


class TextOnlyProvider:
    """模拟只接受字符串 content 的模型接口。"""

    model = 'text-only'

    def __init__(self) -> None:
        self.calls = 0

    async def stream(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.85,
        max_tokens: int | None = None,
        signal: Any = None,
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls += 1
        raise LlmError(
            'unknown',
            '模型接口返回 HTTP 400：unknown variant `image_url`, expected `text`',
        )
        if False:
            yield {}


async def _noop_push(channel: str, payload: dict[str, Any]) -> None:
    return None


def test_vision_router_builds_client_from_its_own_candidate() -> None:
    """视觉候选自带地址和密钥，不再从对话配置里东拼西凑。"""
    routing = TaskRouting(task='vision', candidates=[ModelCandidate(
        name='vision', provider='视觉', kind='openai',
        base_url='https://vision.example.com/v1', api_key='vision-key',
        identifier='multimodal-model',
    )])
    router = ModelRouter('vision', routing.candidates, routing.strategy)

    provider = router.client(routing.candidates[0])

    assert router.model == 'multimodal-model'
    assert provider.model == 'multimodal-model'
    assert provider.base_url == 'https://vision.example.com/v1'
    assert provider.api_key == 'vision-key'


async def test_vision_service_sends_image_to_injected_provider() -> None:
    provider = RecordingVisionProvider()
    service = VisionService(Config(), _noop_push, provider)

    result = await service._call_vision_model(b'jpeg bytes')

    assert result.description == '正在打开装备菜单'
    assert result.failure is None
    content = provider.messages[0]['content']
    assert content[0]['type'] == 'text'
    assert content[1]['type'] == 'image_url'
    assert content[1]['image_url']['url'].startswith('data:image/jpeg;base64,')


async def test_text_only_protocol_error_stops_repeated_calls() -> None:
    provider = TextOnlyProvider()
    service = VisionService(Config(), _noop_push, provider)

    first = await service._call_vision_model(b'first frame')
    second = await service._call_vision_model(b'second frame')

    assert first.description is None
    assert second.description is None
    assert provider.calls == 1
    assert service.stats()['available'] is False
    assert 'image_url' in service.stats()['error']


async def test_empty_glance_trace_contains_failure_diagnostics(monkeypatch) -> None:
    """视觉失败必须可从 trace 定位，不能只依赖一闪而过的终端 logger。"""
    from src.desktop import vision as vision_module

    class FailingProvider:
        model = 'vision-test'

        async def stream(self, messages, temperature=0.85, max_tokens=None, signal=None):
            raise LlmError(
                'auth',
                '模型接口返回 HTTP 401：无效凭证',
                '{"error":{"message":"invalid api key"}}',
            )
            yield {}  # pragma: no cover - 保持为异步生成器

    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        vision_module.trace,
        'emit',
        lambda kind, **fields: events.append({'kind': kind, **fields}),
    )
    service = VisionService(Config(vision={'enabled': True}), _noop_push, FailingProvider())

    assert await service.glance(b'jpeg bytes') is None
    event = next(event for event in events if event['kind'] == 'vision_glance')
    assert event == {
        'kind': 'vision_glance',
        'result': 'empty',
        'app': '',
        'errorType': 'LlmError',
        'errorKind': 'auth',
        'statusCode': 401,
        'responseExcerpt': '{"error":{"message":"invalid api key"}}',
    }
