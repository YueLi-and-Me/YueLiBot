"""验证无头首次启动的配置生成：内容、幂等、待填项判定与种子表的完整性。

配置文件此前只有 Electron 会写，无头部署因此起不来。本文件覆盖
``src.core.config.bootstrap`` 的四件事：

- 空目录能生成一份结构完整、可被 tomllib 解析的初始配置；
- 已存在的文件一律不覆盖（每次启动都会跑，覆盖等于冲掉用户填好的密钥）；
- 「还差什么」的判定认得 ``auth_type = none`` 这类不需要密钥的本地连接；
- schema 新增必填字段却没在种子表里给初值时，测试先红，而不是让新用户在第一次
  启动时撞上。

适配器根目录一律指向临时目录：工作区里那两份 ``adapters/*/config.toml`` 带着真实
QQ 号与协议端令牌，测试不得触碰。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import shutil
import tomllib

import pytest

from src.core.config import adapter_selection
from src.core.config.adapter_selection import DEFAULT_ADAPTER_PLUGIN
from src.core.config.bootstrap import (
    MAIN_CONFIG_FILES,
    _default_document,
    bootstrap_config_directory,
    missing_startup_requirements,
)
from src.core.config.schema import (
    CONFIG_VERSION,
    BotDocument,
    FeatureDocument,
    ModelCatalog,
    ProviderCatalog,
)
from src.platforms.onebot11.config import AdapterDocument

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def adapters_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """把适配器根目录指向只含清单的临时副本。

    :param tmp_path: pytest 提供的临时目录。
    :param monkeypatch: 用于替换模块级根目录常量。
    :yields: 临时适配器根目录。
    副作用：创建临时插件目录并复制清单；不读写工作区里的真实连接配置。
    """
    root = tmp_path / 'adapters'
    plugin = root / DEFAULT_ADAPTER_PLUGIN
    plugin.mkdir(parents=True)
    shutil.copy(
        _PROJECT_ROOT / 'adapters' / DEFAULT_ADAPTER_PLUGIN / '_manifest.json',
        plugin / '_manifest.json',
    )
    monkeypatch.setattr(adapter_selection, 'ADAPTERS_ROOT', root)
    yield root


def _fill(config_dir: Path) -> None:
    """把生成的初始配置填成一份可启动的最小配置。

    :param config_dir: 主体配置目录。
    副作用：改写 bot.toml、providers.toml 与 models.toml。
    """
    bot = config_dir / 'bot.toml'
    bot.write_text(
        bot.read_text(encoding='utf-8').replace('name = "" #', 'name = "测试" #', 1),
        encoding='utf-8',
    )
    providers = config_dir / 'providers.toml'
    providers.write_text(
        providers.read_text(encoding='utf-8')
        .replace('base_url = ""', 'base_url = "http://127.0.0.1:1/v1"', 1)
        .replace('api_key = ""', 'api_key = "sk-test"', 1),
        encoding='utf-8',
    )
    models = config_dir / 'models.toml'
    models.write_text(
        models.read_text(encoding='utf-8')
        .replace('model_identifier = ""', 'model_identifier = "dummy"', 1),
        encoding='utf-8',
    )


def test_空目录生成整套配置(tmp_path: Path, adapters_root: Path) -> None:
    """全新签出里 config/ 根本不存在，入口要能自己造出来。"""
    config_dir = tmp_path / 'config'

    created = bootstrap_config_directory(config_dir)

    names = {path.name for path in created}
    assert names >= set(MAIN_CONFIG_FILES)
    assert 'adapter.toml' in names
    assert (adapters_root / DEFAULT_ADAPTER_PLUGIN / 'config.toml').is_file()
    for name in MAIN_CONFIG_FILES:
        document = tomllib.loads((config_dir / name).read_text(encoding='utf-8'))
        assert document['inner']['version']


def test_已有文件不被覆盖(tmp_path: Path, adapters_root: Path) -> None:
    """本函数每次启动都会跑，覆盖已有文件等于冲掉用户填好的密钥。"""
    config_dir = tmp_path / 'config'
    bootstrap_config_directory(config_dir)
    _fill(config_dir)
    before = {name: (config_dir / name).read_bytes() for name in MAIN_CONFIG_FILES}

    created = bootstrap_config_directory(config_dir)

    assert created == []
    for name in MAIN_CONFIG_FILES:
        assert (config_dir / name).read_bytes() == before[name]


def test_刚生成时只报出密钥一项(tmp_path: Path, adapters_root: Path) -> None:
    """产出是故意不完整的，但只差密钥一项。

    她的名字、厂商地址与模型 ID 都由种子预填，唯独 API Key 别人替不了。
    这条断言盯的是「待填项恰好只有一条」——多出任何一条都意味着某个本该有
    默认值的字段又空着了，那是首次安装体验的退化。
    """
    config_dir = tmp_path / 'config'
    bootstrap_config_directory(config_dir)

    missing = missing_startup_requirements(config_dir)

    assert len(missing) == 1, missing
    assert 'api_key' in missing[0]


def test_填好之后不再报缺项(tmp_path: Path, adapters_root: Path) -> None:
    """填完两处必填项就该放行，不能一直拦着。"""
    config_dir = tmp_path / 'config'
    bootstrap_config_directory(config_dir)
    _fill(config_dir)

    assert missing_startup_requirements(config_dir) == []


def test_本地连接无需密钥也算填好(tmp_path: Path, adapters_root: Path) -> None:
    """``auth_type = none`` 的连接必须留空 api_key，拿密钥非空当判据会误报。"""
    config_dir = tmp_path / 'config'
    bootstrap_config_directory(config_dir)
    _fill(config_dir)
    providers = config_dir / 'providers.toml'
    providers.write_text(
        providers.read_text(encoding='utf-8')
        .replace('auth_type = "bearer"', 'auth_type = "none"', 1)
        .replace('api_key = "sk-test"', 'api_key = ""', 1),
        encoding='utf-8',
    )

    assert missing_startup_requirements(config_dir) == []


# 这条守的是维护期而不是运行期：新增一个必填且无默认值的字段却忘了给初值，
# 后果是所有新用户在第一次启动时撞上 KeyError，而开发机上有配置文件，跑不出来。
@pytest.mark.parametrize('model', [
    BotDocument, FeatureDocument, ProviderCatalog, ModelCatalog, AdapterDocument,
])
def test_每个必填字段都有初值(model: type) -> None:
    """种子表必须覆盖全部必填且无默认值的字段。"""
    from src.core.config import bootstrap

    builders = {
        BotDocument: bootstrap._bot_document,
        FeatureDocument: bootstrap._feature_document,
        ProviderCatalog: bootstrap._provider_document,
        ModelCatalog: bootstrap._model_document,
        AdapterDocument: bootstrap._adapter_document,
    }
    document = builders[model]()

    expected = set(model.model_fields)
    if model is FeatureDocument:
        # 开发者命令段故意不进入首次安装种子；schema 默认值仍保证缺段可加载。
        expected.remove('developer')
    assert set(document) == expected


def test_全新安装不生成开发者命令段(tmp_path: Path, adapters_root: Path) -> None:
    """A-6：首次安装必须让用户配置里根本看不到隐藏命令通道。"""
    config_dir = tmp_path / 'config'

    bootstrap_config_directory(config_dir)

    features_text = (config_dir / 'features.toml').read_text(encoding='utf-8')
    features = tomllib.loads(features_text)
    assert '[developer]' not in features_text
    assert 'developer' not in features


def test_默认适配器与外壳侧常量一致() -> None:
    """两侧各写一份字面量，改了一处就会出现「桌宠与主体读不同适配器」。"""
    source = (_PROJECT_ROOT / 'electron' / 'main' / 'config.ts').read_text(encoding='utf-8')

    assert f"DEFAULT_ADAPTER_PLUGIN = '{DEFAULT_ADAPTER_PLUGIN}'" in source


def test_种子展开覆盖嵌套子模型() -> None:
    """子模型没有默认值时要递归展开，而不是留下一个缺失的段。"""
    document = _default_document(FeatureDocument, {'inner': {'version': CONFIG_VERSION}})

    assert isinstance(document['tts'], dict)
    assert 'enabled' in document['tts']
