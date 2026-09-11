"""验证群聊三态门控的决策表。

本模块覆盖 stream 类型、睡眠状态、称呼命中、协议 @ 与频率计数的组合，
确保门控只产出 DROP / FORCE / DELIBERATE 三态，且原因码封闭可审计。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from src.core.agent.conversation_gate import GateRequest, GateResult, decide_disposition, mentions_bot_name
from src.core.config.schema import BotDocument
from src.core.config.schema import CONFIG_VERSION
from src.core.config.toml_io import read_versioned_toml


def _load_bot_document(
    tmp_path: Path,
    *,
    at_mention_must_reply: bool = True,
) -> BotDocument:
    """从临时 bot.toml 加载测试 Bot，避免门控测试依赖固定角色名。"""
    path = tmp_path / "bot.toml"
    path.write_text(
        f'''[inner]
 version = "{CONFIG_VERSION}"

 [bot]
 name = "Bot#7"
 aliases = ["星", "NOVA!"]

 [group_chat]
 at_mention_must_reply = {str(at_mention_must_reply).lower()}

 [personality]
 birthday = ""
 personality = "测试人设"
 reply_style = "测试表达"
 tone_probability = 0.0
 tone_variants = []
''',
        encoding="utf-8",
    )
    return BotDocument.model_validate(
        read_versioned_toml(path, CONFIG_VERSION, '测试配置必须使用当前版本')
    )


def _bot_names(document: BotDocument) -> tuple[str, ...]:
    """返回配置文件声明的主名称和别名。"""
    return (document.bot.name, *document.bot.aliases)


def _gate(
    document: BotDocument,
    *,
    stream_kind: str = 'group',
    asleep: bool = False,
    mentioned_me: bool = False,
    text: str = "这句没有任何已登记称呼",
    reply_count: int = 0,
) -> GateResult:
    """按文档配置与给定事实构造并执行一次三态门控。"""
    return decide_disposition(GateRequest(
        stream_kind=stream_kind,  # type: ignore[arg-type]
        mentioned_me=mentioned_me,
        name_mentioned=mentions_bot_name(text, _bot_names(document)),
        sleep_level='light' if asleep else 'awake',
        at_mention_must_reply=document.group_chat.at_mention_must_reply,
        replies_in_window=reply_count,
        max_replies_in_window=3,
    ))


@pytest.mark.parametrize(
    ('stream_kind', 'asleep', 'mentioned_me', 'text_kind', 'reply_count', 'expected'),
    [
        ('desktop', True, False, 'plain', 99, ('force', ('direct_conversation',))),
        ('direct', True, False, 'plain', 99, ('force', ('direct_conversation',))),
        ('group', True, False, 'name', 0, ('drop', ('light_sleep',))),
        # 频率硬上限只压「没人点名的自发参与」：撞上限时点名仍然放行进 DELIBERATE，
        # 没有任何称呼的普通消息才 DROP。
        ('group', False, False, 'name', 3, ('deliberate', ('name_mention',))),
        ('group', False, False, 'plain', 3, ('drop', ('rate_limited',))),
        ('group', True, True, 'at', 3, ('force', ('at_mention_must_reply',))),
        ('group', False, False, 'plain', 0, ('drop', ('attention_filtered',))),
        ('group', False, False, 'alias', 0, ('deliberate', ('name_mention',))),
        ('group', False, True, 'at', 0, ('force', ('at_mention_must_reply',))),
    ],
)
def test_gate_follows_the_fixed_decision_order(
    tmp_path: Path,
    stream_kind: str,
    asleep: bool,
    mentioned_me: bool,
    text_kind: str,
    reply_count: int,
    expected: tuple[str, tuple[str, ...]],
) -> None:
    document = _load_bot_document(tmp_path)
    texts = {
        'plain': '这句没有任何已登记称呼',
        'name': f'{document.bot.name}在吗',
        'alias': f'{document.bot.aliases[0]}，在吗',
        'at': '@协议提及',
    }
    result = _gate(
        document,
        stream_kind=stream_kind,
        asleep=asleep,
        mentioned_me=mentioned_me,
        text=texts[text_kind],
        reply_count=reply_count,
    )

    assert (result.disposition, result.reason_codes) == expected


def test_disabled_at_must_reply_defers_to_deliberate(tmp_path: Path) -> None:
    """真实 @ 但 @必回未开启时只进入意识，回不回由后续决策点决定。"""
    document = _load_bot_document(tmp_path, at_mention_must_reply=False)
    result = _gate(document, mentioned_me=True, text="@协议提及")

    assert result.disposition == 'deliberate'
    assert 'direct_mention' in result.reason_codes


@pytest.mark.parametrize(
    ('text_factory', 'expected'),
    [
        (lambda document: f'x{document.bot.name.casefold()}y', True),
        (lambda document: f'关于{document.bot.aliases[0]}河的故事', True),
        (lambda document: f'x{document.bot.aliases[1]}y', True),
        (lambda _document: '平台登录昵称，在吗', False),
    ],
)
def test_name_matching_uses_every_configured_name_without_character_assumptions(
    tmp_path: Path,
    text_factory: Callable[[BotDocument], str],
    expected: bool,
) -> None:
    document = _load_bot_document(tmp_path)
    text = text_factory(document)

    assert mentions_bot_name(text, _bot_names(document)) is expected


def test_configured_alias_enters_deliberate_with_name_signal(tmp_path: Path) -> None:
    document = _load_bot_document(tmp_path)
    result = _gate(
        document,
        text=f'{document.bot.aliases[1]}请回应',
    )

    assert result.disposition == 'deliberate'
    assert result.reason_codes == ('name_mention',)
