"""视觉模型配置和多模态消息构造回归测试。"""

from __future__ import annotations

from typing import Any, AsyncIterator

from yueli.config.schema import Config
from yueli.llm.openai import LlmError, create_vision_provider
from yueli.services.vision import VisionService


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


def test_vision_provider_uses_all_explicit_overrides() -> None:
    cfg = Config.model_validate({
        'llm': {
            'provider': 'deepseek',
            'model': 'deepseek-v4-flash',
            'base_url': 'https://api.deepseek.com',
            'api_key': 'chat-key',
        },
        'vision': {
            'enabled': True,
            'model': 'multimodal-model',
            'base_url': 'https://vision.example.com/v1',
            'api_key': 'vision-key',
        },
    })

    provider = create_vision_provider(cfg)

    assert provider.model == 'multimodal-model'
    assert provider.base_url == 'https://vision.example.com/v1'
    assert provider.api_key == 'vision-key'


async def test_vision_service_sends_image_to_injected_provider() -> None:
    provider = RecordingVisionProvider()
    service = VisionService(Config(), _noop_push, provider)

    result = await service._call_vision_model(b'jpeg bytes', 'gameplay')

    assert result == '正在打开装备菜单'
    content = provider.messages[0]['content']
    assert content[0]['type'] == 'text'
    assert content[1]['type'] == 'image_url'
    assert content[1]['image_url']['url'].startswith('data:image/jpeg;base64,')


async def test_text_only_protocol_error_stops_repeated_calls() -> None:
    provider = TextOnlyProvider()
    service = VisionService(Config(), _noop_push, provider)

    first = await service._call_vision_model(b'first frame', 'gameplay')
    second = await service._call_vision_model(b'second frame', 'gameplay')

    assert first is None
    assert second is None
    assert provider.calls == 1
    assert service.stats()['available'] is False
    assert 'image_url' in service.stats()['error']
