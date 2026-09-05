"""multiagent-2-prompts 的 V1-V15 验收用例。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

import asyncio

import pytest

from src.core.agent.prompt import build_system_prompt
from src.core.config.loader import load_config, reset_config
from src.core.config.schema import CONFIG_VERSION, Config
from src.core.observe import events
from src.core.observe.store import event_store
from src.core.services.chat import ChatService, InboundMessage


OLD_DEFAULT_PROMPT_CHARS = 2418
# 默认系统提示词的字符预算。它是活的上限不是历史数字：撞线说明该回头精简，
# 而不是说明「这条规则不许加」。
DEFAULT_PROMPT_CHAR_BUDGET = 1600
DEFAULT_BIRTHDAY = '2006-09-12'
DEFAULT_PERSONALITY = (
    '银发猫系的文学系大二女生，在 B 站做虚拟主播。表面乖巧内里腹黑，毒舌但心软，'
    '被夸会脸红嘴硬。爱看番、打游戏、熬夜。'
)
DEFAULT_REPLY_STYLE = """像朋友私聊，不是问答页面。默认一两句，说到够用就停。
不要像 AI 那样列举信息，不用标题、条列、总结，也不用在结尾抛个问题把话续上。
他明显难受时先回应那份具体的感受，除非他在求办法，别马上端出解决方案。
他话里有特别扎眼或好笑的细节，可以先被它勾走一下再回到正题，一轮最多一次。"""


class _FakeProvider:
    model = 'fake-model'

    async def stream(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.85,
        max_tokens: int | None = None,
        signal: asyncio.Event | None = None,
    ) -> AsyncIterator[dict[str, str]]:
        yield {'text': '<say emotion="normal">知道啦。</say>'}


async def _noop_push(
    channel: str,
    payload: dict[str, Any],
    stream_id: int = 1,
) -> None:
    return None


def _write_config(directory: Path, personality: str) -> Path:
    """写一份只用于加载器验收的最小四文件配置。"""

    directory.mkdir()
    directory.joinpath('providers.toml').write_text(
        '''
[inner]
version = "''' + CONFIG_VERSION + '''"

[[api_providers]]
name = "测试厂商"
kind = "openai"
base_url = "https://api.example.com"
api_key = "sk-test"
client_type = "openai"
''',
        encoding='utf-8',
    )
    directory.joinpath('models.toml').write_text(
        '''
[inner]
version = "''' + CONFIG_VERSION + '''"

[model_tasks.chat]
model_list = ["chat"]

[model_tasks.vision]
model_list = []

[model_tasks.tts]
model_list = []

[model_tasks.embedding]
model_list = []

[[models]]
name = "chat"
model_identifier = "test-chat"
api_provider = "测试厂商"
''',
        encoding='utf-8',
    )
    directory.joinpath('bot.toml').write_text(
        f'''
[inner]
version = "{CONFIG_VERSION}"

[bot]
name = "月璃"

[group_chat]
at_mention_must_reply = true

[personality]
{personality}
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
log_level = "INFO"
https_proxy = ""
''',
        encoding='utf-8',
    )
    return directory


def _prompt_api():
    from src.core.prompts import registry

    return {
        'builtin_dir': registry.BUILTIN_PROMPT_DIR,
        'chat_ids': registry.CHAT_SYSTEM_TEMPLATE_IDS,
        'configure': registry.configure_prompts,
        'get': registry.get_prompt,
        'load': registry.load_prompt_catalog,
        'metadata': getattr(registry, 'prompt_metadata', None),
        'reset': registry.reset_prompts_for_tests,
    }


def test_v1_empty_personality_keeps_fixed_rules() -> None:
    prompt = build_system_prompt(
        name='月璃',
        now=datetime(2026, 8, 11, 12, 0),
        schedule='',
        birthday='',
        personality='',
        reply_style='',
    )

    assert '具体的名称（游戏名、软件名、文件名、数字）' in prompt
    assert '不要自称助手、模型或 AI' in prompt


@pytest.mark.parametrize(
    ('now', 'age'),
    [
        (datetime(2026, 8, 11, 12, 0), 19),
        (datetime(2027, 8, 11, 12, 0), 20),
    ],
)
def test_v2_birthday_calculates_age(now: datetime, age: int) -> None:
    prompt = build_system_prompt(
        name='月璃',
        now=now,
        schedule='',
        birthday='2006-09-12',
        personality='',
        reply_style='',
    )
    assert '你的生日是 2006年9月12日。' in prompt
    assert f'{age} 岁' in prompt


def test_v3_birthday_note_only_on_month_and_day() -> None:
    birthday = '2006-09-12'
    matching = build_system_prompt(
        name='月璃',
        now=datetime(2026, 9, 12, 12, 0),
        schedule='',
        birthday=birthday,
        personality='',
        reply_style='',
    )
    previous_day = build_system_prompt(
        name='月璃',
        now=datetime(2026, 9, 11, 12, 0),
        schedule='',
        birthday=birthday,
        personality='',
        reply_style='',
    )

    assert '今天是你的生日' in matching
    assert '今天是你的生日' not in previous_day


def test_v4_old_identity_key_fails_with_migration_message(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    directory = _write_config(
        tmp_path / 'config',
        'identity = "旧身份"\nreply_style = "旧说法"',
    )
    reset_config()
    try:
        with pytest.raises(SystemExit):
            load_config(directory)
        assert 'personality.personality' in capsys.readouterr().err
    finally:
        reset_config()


def test_v5_summary_override_is_used(tmp_path: Path) -> None:
    api = _prompt_api()
    override_dir = tmp_path / 'prompts'
    override_dir.mkdir()
    override_dir.joinpath('summary.md').write_text(
        '摘要覆写：{{character_name}} / {{character_personality}}',
        encoding='utf-8',
    )
    api['configure'](tmp_path)
    try:
        from src.core.agent.summarize import _system_prompt

        assert '摘要覆写：巡星 / 数据生命' == _system_prompt('巡星', '数据生命')
    finally:
        api['reset']()


def test_v6_summary_override_missing_placeholder_fails(tmp_path: Path) -> None:
    api = _prompt_api()
    override_dir = tmp_path / 'prompts'
    override_dir.mkdir()
    override_dir.joinpath('summary.md').write_text(
        '只剩 {{character_name}}',
        encoding='utf-8',
    )

    with pytest.raises(ValueError) as exc_info:
        api['configure'](tmp_path)
    message = str(exc_info.value)
    assert 'summary' in message
    assert 'character_personality' in message
    api['reset']()


@pytest.mark.parametrize(
    ('replacement', 'placeholder'),
    [
        ('只剩 {{gestures}}', 'emotions'),
        ('{{emotions}} {{gestures}} {{nonexistent}}', 'nonexistent'),
    ],
)
def test_v7_v8_builtin_protocol_placeholder_mismatch_fails(
    tmp_path: Path,
    replacement: str,
    placeholder: str,
) -> None:
    from shutil import copytree

    api = _prompt_api()
    builtin_dir = tmp_path / 'builtin'
    copytree(api['builtin_dir'], builtin_dir)
    builtin_dir.joinpath('chat.protocol.md').write_text(replacement, encoding='utf-8')

    with pytest.raises(ValueError) as exc_info:
        api['load'](tmp_path / 'data', builtin_dir=builtin_dir)
    message = str(exc_info.value)
    assert 'chat.protocol' in message
    assert placeholder in message


def test_v9_fixed_boundary_override_is_ignored_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core.prompts import registry

    api = _prompt_api()
    warnings: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    class _WarningRecorder:
        """接管模块级 logger 的告警探针。

        get_logger 返回的 _DynamicLogger 以 __slots__ 锁死实例属性并靠
        __getattr__ 动态转发，直接 monkeypatch 其 warning 会抛 AttributeError；
        改为整体替换 registry.logger，命中 registry 在调用点查全局的写法。
        """

        def warning(self, *args: Any, **kwargs: Any) -> None:
            warnings.append((args, kwargs))

    monkeypatch.setattr(registry, 'logger', _WarningRecorder())
    override_dir = tmp_path / 'prompts'
    override_dir.mkdir()
    marker = '不该生效的边界覆写'
    override_dir.joinpath('chat.boundaries.md').write_text(marker, encoding='utf-8')
    api['configure'](tmp_path)
    try:
        assert marker not in api['get']('chat.boundaries').text
        assert warnings
    finally:
        api['reset']()


async def test_v10_llm_request_is_stamped_and_hash_changes(
    db: Any,
    tmp_path: Path,
) -> None:
    api = _prompt_api()
    api['configure'](tmp_path)
    try:
        event_store.clear()
        events.reset_for_tests()
        provider = _FakeProvider()
        chat = ChatService(db, provider, provider, provider, _noop_push, cfg=Config())
        context = chat.desktop_context
        await chat.send(InboundMessage(text='你好', context=context))
        await chat._tick()
        inflight = chat._inflight.get(context.stream.id)
        if inflight is not None:
            await asyncio.wait_for(inflight.task, timeout=5)
        first = next(
            event for event in event_store.since(0).events
            if event['kind'] == 'llm_request'
        )
        assert first['promptId'] == 'chat.system'
        selected_chat_ids = (*api['chat_ids'], 'chat.length.brief')
        assert first['promptHash'] == api['metadata'](
            'chat.system',
            selected_chat_ids,
        )['promptHash']

        builtin = api['get']('chat.protocol').text
        override_dir = tmp_path / 'prompts'
        override_dir.mkdir(exist_ok=True)
        override_dir.joinpath('chat.protocol.md').write_text(
            builtin + '。',
            encoding='utf-8',
        )
        api['configure'](tmp_path)
        changed = api['metadata']('chat.system', selected_chat_ids)
        assert changed['promptHash'] != first['promptHash']
    finally:
        api['reset']()


def test_v11_deleting_override_returns_to_builtin(tmp_path: Path) -> None:
    api = _prompt_api()
    override_dir = tmp_path / 'prompts'
    override_dir.mkdir()
    override_path = override_dir / 'summary.md'
    override_path.write_text(
        '覆盖版 {{character_name}} / {{character_personality}}',
        encoding='utf-8',
    )
    api['configure'](tmp_path)
    assert api['get']('summary').source == override_path

    override_path.unlink()
    api['configure'](tmp_path)
    try:
        assert api['get']('summary').source == api['builtin_dir'] / 'summary.md'
    finally:
        api['reset']()


def test_v12_changed_override_creates_history_with_exact_content(tmp_path: Path) -> None:
    api = _prompt_api()
    override_dir = tmp_path / 'prompts'
    override_dir.mkdir()
    override_path = override_dir / 'summary.md'
    first = '第一版 {{character_name}} / {{character_personality}}'
    second = '第二版 {{character_name}} / {{character_personality}}'
    override_path.write_text(first, encoding='utf-8')
    api['configure'](tmp_path)
    history_dir = override_dir / 'history' / 'summary'
    before = set(history_dir.glob('*.md'))

    override_path.write_text(second, encoding='utf-8')
    api['configure'](tmp_path)
    try:
        created = set(history_dir.glob('*.md')) - before
        assert len(created) == 1
        assert created.pop().read_text(encoding='utf-8') == second
    finally:
        api['reset']()


def test_v13_unchanged_restarts_do_not_duplicate_history(tmp_path: Path) -> None:
    api = _prompt_api()
    override_dir = tmp_path / 'prompts'
    override_dir.mkdir()
    override_dir.joinpath('summary.md').write_text(
        '稳定版 {{character_name}} / {{character_personality}}',
        encoding='utf-8',
    )
    api['configure'](tmp_path)
    history_dir = override_dir / 'history' / 'summary'
    count = len(list(history_dir.glob('*.md')))

    api['configure'](tmp_path)
    api['configure'](tmp_path)
    api['configure'](tmp_path)
    try:
        assert len(list(history_dir.glob('*.md'))) == count
    finally:
        api['reset']()


def test_v14_render_rejects_undeclared_key() -> None:
    api = _prompt_api()
    with pytest.raises(ValueError) as exc_info:
        api['get']('summary').render(
            character_name='月璃',
            character_personality='测试人设',
            extra='不该出现',
        )
    message = str(exc_info.value)
    assert 'summary' in message
    assert 'extra' in message


def test_v15_default_prompt_stays_far_below_the_old_bloated_one() -> None:
    """默认系统提示词不得回涨到瘦身之前的量级。

    原断言写的是「比旧版短 1000 字以上」，那是 M8 第 2 步瘦身当时的**成绩**，
    到 2026-08-20 只剩 2 个字符余量——任何一条新规则都会撞线，它已经变成
    「不许再加任何字」而不是「不许回胖」。

    改成绝对上限：守的是同一件事（别退回旧版那种长度），但留出了可用预算，
    撞线时是真的该回头精简，而不是被历史数字卡住。
    """
    prompt = build_system_prompt(
        name='月璃',
        birthday=DEFAULT_BIRTHDAY,
        personality=DEFAULT_PERSONALITY,
        reply_style=DEFAULT_REPLY_STYLE,
        now=datetime(2026, 8, 11, 12, 0),
        schedule='',
    )
    assert len(prompt) < DEFAULT_PROMPT_CHAR_BUDGET
    # 同时保住原来那条「显著短于旧版」的性质。
    assert OLD_DEFAULT_PROMPT_CHARS - len(prompt) > 800
