"""新装预填：一条临时说话方式与一条正则规则。

对应任务书 ★9。渲染出的 ``config.example/`` 与种子逐字一致由
``test_config_example.py`` 把关；这里钉住预填的具体内容、能通过校验，并能对
「男人是这样的」命中、替换出以「男人是这样的，」开头的文本。
"""

from __future__ import annotations

from pathlib import Path

import random
import tomllib

import pytest

from pytests.conftest import render_loadable_config
from src.core.agent.regex_rule import match_regex_rules
from src.core.config.bootstrap import render_example_configs
from src.core.config.loader import load_config, reset_config

REPO_ROOT = Path(__file__).resolve().parents[2]
# 新装预填的那一条规则；内容由用户 10-28 过目定稿，改动须同步任务书。
_SEED_PATTERN = r'^(?P<n>\S{1,20})是这样的[。！!]*$'
_SEED_REACTION = (
    '{n}是这样的，〈某类人〉只要〈做一件简单的事〉就可以，'
    '可是{n}要考虑的事情就很多了，比如什么时候〈…〉，什么时候〈…〉'
)
_SEED_TONE = '这次回复里夹一句繁体文言，像随口引一句古书。'


@pytest.fixture(autouse=True)
def _reset_config_singleton():
    reset_config()
    yield
    reset_config()


def test_rendered_example_carries_seed(tmp_path: Path) -> None:
    render_example_configs(tmp_path)
    personality = tomllib.loads((tmp_path / 'bot.toml').read_text(encoding='utf-8'))['personality']

    # 临时说话方式只在现有两条后面追加一条，前两条不动。
    assert personality['tone_variants'] == [
        '这次只用一两个字回。',
        '这次语气平淡一点，带一句吐槽。',
        _SEED_TONE,
    ]
    assert personality['regex_rules'] == [
        {'pattern': _SEED_PATTERN, 'reaction': _SEED_REACTION, 'streams': []},
    ]


def test_committed_example_bot_toml_carries_seed() -> None:
    raw = tomllib.loads((REPO_ROOT / 'config.example' / 'bot.toml').read_text(encoding='utf-8'))

    assert raw['personality']['regex_rules'] == [
        {'pattern': _SEED_PATTERN, 'reaction': _SEED_REACTION, 'streams': []},
    ]


def test_seed_rule_validates_and_matches(tmp_path: Path) -> None:
    config = load_config(render_loadable_config(tmp_path / 'config'))

    result = match_regex_rules(
        config.personality.regex_rules,
        '男人是这样的',
        stream_external_id='10001',
        stream_id=1,
        now=1_800_000_000_000,
        cooldowns={},
        rng=random.Random(1),
    )

    assert result.reaction is not None
    assert result.reaction.startswith('男人是这样的，')
    assert '可是男人要考虑的事情就很多了' in result.reaction
