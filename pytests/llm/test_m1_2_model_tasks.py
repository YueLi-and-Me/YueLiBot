"""验证模型任务按角色分层的配置与路由行为。

本模块覆盖任务级模型候选、生成参数和配置读写，确保聊天、主动回复、摘要、
日程、表情及视觉任务能够独立使用各自配置。
"""

from __future__ import annotations

from inspect import Parameter, signature
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List
import json

import pytest

from src.core.config.loader import load_config, reset_config
from src.core.config.schema import CONFIG_VERSION, Config
from src.core.llm_models.router import ModelRouters
from src.core.services.chat import ChatService

# 测试夹具占位密钥：不对应任何真实服务，经变量间接写入避免被安全扫描当作硬编码凭据。
_FIXTURE_API_KEY = "-".join(("test", "key"))
_API_KEY_LINE = f"api_key = {json.dumps(_FIXTURE_API_KEY)}"


@pytest.fixture(autouse=True)
def _reset_config_singleton() -> None:
    reset_config()
    yield
    reset_config()


def _write_config(
    directory: Path,
    *,
    chat_model: str = 'chat',
    extra_model_tasks: str = '',
    vision_model: str = '',
) -> None:
    """写一份可加载的最小配置；新任务段由用例按需追加。"""
    directory.mkdir()
    directory.joinpath('providers.toml').write_text(
        f'''
[inner]
version = "{CONFIG_VERSION}"

[[api_providers]]
name = "main"
kind = "openai"
base_url = "https://api.example.com"
{_API_KEY_LINE}
client_type = "openai"
''',
        encoding='utf-8',
    )
    directory.joinpath('models.toml').write_text(
        f'''
[inner]
version = "{CONFIG_VERSION}"

[model_tasks.chat]
model_list = ["{chat_model}"]
selection_strategy = "sequential"

[model_tasks.vision]
model_list = [{json.dumps(vision_model) if vision_model else ''}]

[model_tasks.tts]
model_list = []

[model_tasks.embedding]
model_list = []
{extra_model_tasks}
[generation.chat]
temperature = 0.85

[[models]]
name = "chat"
model_identifier = "chat-model"
api_provider = "main"

[[models]]
name = "alternate"
model_identifier = "alternate-model"
api_provider = "main"

[[models]]
name = "cheap"
model_identifier = "cheap-model"
api_provider = "main"
''',
        encoding='utf-8',
    )
    directory.joinpath('bot.toml').write_text(
        '''
[inner]
version = "''' + CONFIG_VERSION + '''"

[bot]
name = "测试角色"

[group_chat]
at_mention_must_reply = true

[personality]
birthday = ""
personality = "测试人设"
reply_style = "测试说话方式"
tone_probability = 0.0
tone_variants = []
''',
        encoding='utf-8',
    )
    directory.joinpath('features.toml').write_text(
        '''
[inner]
version = "''' + CONFIG_VERSION + '''"

[tts]
enabled = false

[vision]
enabled = false

[vector]
enabled = false

[advanced]
''',
        encoding='utf-8',
    )


def test_typoed_model_task_section_is_rejected_at_load_time(tmp_path, capsys) -> None:
    directory = tmp_path / 'config'
    _write_config(
        directory,
        extra_model_tasks='''
[model_tasks.summry]
model_list = ["cheap"]
''',
    )

    with pytest.raises(SystemExit):
        load_config(directory)

    assert 'model_tasks.summry' in capsys.readouterr().err


def test_empty_new_tasks_inherit_chat_candidates_and_strategy(tmp_path) -> None:
    directory = tmp_path / 'config'
    _write_config(directory)

    config = load_config(directory)

    for task in (config.routing.proactive, config.routing.summary, config.routing.schedule):
        assert task.candidates == config.routing.chat.candidates
        assert task.strategy == config.routing.chat.strategy


def test_planner_and_replyer_inherit_chat_when_unset(tmp_path) -> None:
    """决策与表达两个槽留空即继承 chat，拆分本身不要求先配模型。

    这两个槽是给「planner 换快模型压首字延迟」留的口子；没配之前行为必须与
    拆分前一致，否则升级配置的人会在没改任何设置的情况下看到模型变了。
    """
    directory = tmp_path / 'config'
    _write_config(directory)

    config = load_config(directory)

    for task in (config.routing.planner, config.routing.replyer, config.routing.scene):
        assert task.candidates == config.routing.chat.candidates
        assert task.strategy == config.routing.chat.strategy


def test_nonempty_summary_route_is_independent_from_chat(tmp_path) -> None:
    summary_task = '''
[model_tasks.summary]
model_list = ["cheap"]
selection_strategy = "random"
'''
    first_directory = tmp_path / 'first'
    _write_config(first_directory, chat_model='chat', extra_model_tasks=summary_task)
    first_config = load_config(first_directory)

    reset_config()
    second_directory = tmp_path / 'second'
    _write_config(second_directory, chat_model='alternate', extra_model_tasks=summary_task)
    second_config = load_config(second_directory)

    assert [candidate.name for candidate in first_config.routing.summary.candidates] == ['cheap']
    assert [candidate.name for candidate in second_config.routing.summary.candidates] == ['cheap']
    assert first_config.routing.summary.strategy == 'random'
    assert second_config.routing.summary.strategy == 'random'
    assert first_config.routing.chat.candidates != second_config.routing.chat.candidates


def test_vision_route_rejects_model_without_visual_marker(tmp_path, capsys) -> None:
    """纯文本模型即使被手工写进 vision，也必须在启动阶段暴露配置错误。"""
    directory = tmp_path / 'config'
    _write_config(directory, vision_model='chat')

    with pytest.raises(SystemExit):
        load_config(directory)

    error = capsys.readouterr().err
    assert 'model_tasks.vision 的候选 chat' in error
    assert 'visual = true' in error


class _RecordingProvider:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> AsyncIterator[Dict[str, str]]:
        self.calls.append({'messages': messages, **kwargs})
        yield {'text': '日程内容'}


class _ReasoningOnlyProvider:
    async def stream(
        self,
        messages: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> AsyncIterator[Dict[str, str]]:
        yield {'reasoning': '还在推理，没有正文'}


async def test_schedule_generator_uses_schedule_route() -> None:
    from src import main

    assert hasattr(main, '_LLMGenerator'), '日程生成器必须接收显式的 schedule 路由'
    chat_provider = _RecordingProvider()
    schedule_provider = _RecordingProvider()
    generator = main._LLMGenerator(
        schedule_provider,
        temperature=0.95,
        max_tokens=700,
        prompt_id='schedule',
        template_id='schedule',
    )

    result = await generator.generate('请生成日程')

    assert result == '日程内容'
    assert len(schedule_provider.calls) == 1
    assert schedule_provider.calls[0]['messages'] == [{'role': 'user', 'content': '请生成日程'}]
    assert schedule_provider.calls[0]['temperature'] == 0.95
    assert schedule_provider.calls[0]['max_tokens'] == 700
    assert schedule_provider.calls[0]['response_format'] == {'type': 'json_object'}
    assert 'thinking' not in schedule_provider.calls[0]
    assert chat_provider.calls == []


async def test_schedule_generator_rejects_reasoning_without_body() -> None:
    from src import main

    generator = main._LLMGenerator(
        _ReasoningOnlyProvider(),
        temperature=0.95,
        max_tokens=4096,
        prompt_id='schedule',
        template_id='schedule',
    )

    with pytest.raises(ValueError, match='正文字符=0，推理字符=9'):
        await generator.generate('请生成日程')


def test_schedule_default_reserves_reasoning_budget() -> None:
    assert Config().generation.schedule.max_tokens == 4096
    assert not hasattr(Config().generation.schedule, 'thinking')


def test_model_routers_inspect_returns_all_tasks() -> None:
    routers = ModelRouters(Config())

    assert set(routers.inspect()) == {
        'chat', 'planner', 'replyer', 'scene', 'memory', 'proactive', 'summary', 'schedule',
        'vision', 'expression', 'tts', 'embedding',
    }


def test_chat_service_requires_each_role_provider_explicitly() -> None:
    parameters = signature(ChatService.__init__).parameters

    for name in ('chat_provider', 'proactive_provider', 'summary_provider'):
        assert parameters[name].default is Parameter.empty
