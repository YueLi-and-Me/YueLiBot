"""正则规则的配置模型与版本升级补键。

对应任务书 ★1（校验）与 ★10（升级器补键与 Electron 写入器）。
校验失败的信息必须指明第几条与原因：WebUI 保存失败时页面显示的就是这段文字。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import shutil
import subprocess
import tomllib

from pydantic import ValidationError
import pytest

from pytests.conftest import render_loadable_config
from src.core.config.loader import load_config, read_config, reset_config
from src.core.config.schema import (
    CONFIG_VERSION,
    BotDocument,
    FeatureDocument,
    ModelCatalog,
    PersonalityConfig,
    ProviderCatalog,
)
from src.core.config.upgrade import upgrade_config_directory

REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCUMENTS = {
    'providers.toml': ProviderCatalog,
    'models.toml': ModelCatalog,
    'bot.toml': BotDocument,
    'features.toml': FeatureDocument,
}


@pytest.fixture(autouse=True)
def _reset_config_singleton():
    reset_config()
    yield
    reset_config()


def _personality(**overrides: Any) -> Dict[str, Any]:
    """给出一份合法的人设段原始映射，按需覆盖字段。"""

    raw: Dict[str, Any] = {
        'birthday': '',
        'personality': '虚构人设',
        'reply_style': '',
        'tone_probability': 0.0,
        'tone_variants': [],
    }
    raw.update(overrides)
    return raw


def _rule(pattern: str = '晚安', reaction: str = '回一句晚安', **fields: Any) -> Dict[str, Any]:
    return {'pattern': pattern, 'reaction': reaction, **fields}


class TestValidation:
    """★1：合法规则加载成功；三类错误都报出第几条与原因。"""

    def test_valid_rules_load(self) -> None:
        config = PersonalityConfig.model_validate(_personality(regex_rules=[
            _rule(),
            _rule(r'(?P<who>\S+)来了', '{who}来了就打个招呼', streams=['10001']),
        ]))

        assert [rule.pattern for rule in config.regex_rules] == ['晚安', r'(?P<who>\S+)来了']
        assert config.regex_rules[0].streams == []
        assert config.regex_rules[1].streams == ['10001']

    def test_missing_regex_rules_loads_as_empty(self) -> None:
        assert PersonalityConfig.model_validate(_personality()).regex_rules == []

    def test_uncompilable_pattern_names_rule_and_reason(self) -> None:
        with pytest.raises(ValidationError) as caught:
            PersonalityConfig.model_validate(_personality(regex_rules=[
                _rule(), _rule('(?P<n>未闭合', '{n}'),
            ]))

        message = str(caught.value)
        assert 'regex_rules 第 2 条' in message
        assert '无法编译' in message

    def test_overlong_pattern_names_rule_and_reason(self) -> None:
        with pytest.raises(ValidationError) as caught:
            PersonalityConfig.model_validate(_personality(regex_rules=[_rule('喵' * 201)]))

        message = str(caught.value)
        assert 'regex_rules 第 1 条' in message
        assert '200' in message

    def test_pattern_at_length_limit_is_accepted(self) -> None:
        config = PersonalityConfig.model_validate(_personality(regex_rules=[_rule('喵' * 200)]))

        assert len(config.regex_rules[0].pattern) == 200

    def test_reaction_referencing_unknown_group_names_rule_and_group(self) -> None:
        with pytest.raises(ValidationError) as caught:
            PersonalityConfig.model_validate(_personality(regex_rules=[
                _rule(r'(?P<who>\S+)来了', '{who}来了，{what}也来了'),
            ]))

        message = str(caught.value)
        assert 'regex_rules 第 1 条' in message
        assert 'what' in message

    def test_empty_pattern_and_reaction_are_rejected(self) -> None:
        with pytest.raises(ValidationError) as empty_pattern:
            PersonalityConfig.model_validate(_personality(regex_rules=[_rule(' ', '回一句')]))
        with pytest.raises(ValidationError) as empty_reaction:
            PersonalityConfig.model_validate(_personality(regex_rules=[_rule('晚安', '  ')]))

        assert 'regex_rules 第 1 条的 pattern 不能为空' in str(empty_pattern.value)
        assert 'regex_rules 第 1 条的 reaction 不能为空' in str(empty_reaction.value)

    def test_overlong_reaction_is_rejected(self) -> None:
        with pytest.raises(ValidationError) as caught:
            PersonalityConfig.model_validate(_personality(regex_rules=[_rule('晚安', '嗯' * 501)]))

        message = str(caught.value)
        assert 'regex_rules 第 1 条' in message
        assert '500' in message

    def test_config_load_reports_the_same_reason(self, tmp_path: Path) -> None:
        directory = render_loadable_config(tmp_path / 'config')
        bot = directory / 'bot.toml'
        bot.write_text(
            _strip_regex_rules(bot.read_text(encoding='utf-8'))
            + '\n[[personality.regex_rules]]\npattern = "(未闭合"\nreaction = "回一句"\n',
            encoding='utf-8',
        )

        # 走启动与热重载共用的读取入口；load_config 在失败时直接退出进程。
        with pytest.raises(ValidationError) as caught:
            read_config(directory)

        assert 'regex_rules 第 1 条' in str(caught.value)
        assert '无法编译' in str(caught.value)


def _strip_regex_rules(text: str) -> str:
    """把 bot.toml 里的 regex_rules（内联或子表数组）整段去掉，模拟 1.6.0 的文件。"""

    kept = []
    dropping = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('[[personality.regex_rules]]'):
            dropping = True
            continue
        if dropping and (stripped.startswith('[') or not stripped):
            dropping = False
        if dropping or stripped.startswith('regex_rules ='):
            continue
        kept.append(line)
    return '\n'.join(kept) + '\n'


class TestUpgrade:
    """★10：升级器给 1.6.0 的老配置补上 regex_rules，已有内容不变。"""

    def test_legacy_bot_toml_gets_empty_regex_rules(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        directory = render_loadable_config(tmp_path / 'config')
        for name in _DOCUMENTS:
            path = directory / name
            text = path.read_text(encoding='utf-8')
            path.write_text(
                text.replace(f'version = "{CONFIG_VERSION}"', 'version = "1.6.0"', 1),
                encoding='utf-8',
            )
        bot = directory / 'bot.toml'
        bot.write_text(_strip_regex_rules(bot.read_text(encoding='utf-8')), encoding='utf-8')
        before = tomllib.loads(bot.read_text(encoding='utf-8'))
        assert 'regex_rules' not in before['personality']
        data_dir = tmp_path / 'data'
        data_dir.mkdir()

        diffs = upgrade_config_directory(directory, _DOCUMENTS, data_dir)

        after = tomllib.loads(bot.read_text(encoding='utf-8'))
        assert after['inner']['version'] == CONFIG_VERSION
        assert after['personality']['regex_rules'] == []
        # 除补上的键与版本号外，原有内容逐项不变。
        after['personality'].pop('regex_rules')
        after['inner'] = before['inner']
        assert after == before
        bot_diff = next(diff for diff in diffs if diff.name == 'bot.toml')
        assert [item.path for item in bot_diff.added] == ['personality.regex_rules']
        assert bot_diff.version_from == '1.6.0'
        assert '新增 personality.regex_rules = []' in capsys.readouterr().out
        assert load_config(directory).personality.regex_rules == []


def test_electron_writer_emits_regex_rules(tmp_path: Path) -> None:
    """★10：Electron 写入器的默认配置带 personality.regex_rules，与 Python 侧对账。"""

    npx = shutil.which('npx')
    assert npx is not None, '未找到 npx，先在仓库根目录执行 npm install'
    result = subprocess.run(
        [npx, 'tsx', 'scripts/check/config_defaults.ts', str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
    )
    assert result.returncode == 0, result.stdout + result.stderr

    raw = tomllib.loads((tmp_path / 'bot.toml').read_text(encoding='utf-8'))
    assert raw['inner']['version'] == CONFIG_VERSION
    assert raw['personality']['regex_rules'] == []
