"""两个模型配置写入器写出的价格段必须逐字一致，读回后与写入前等价。

WebUI 模型工作台（``src/core/config/model_webui.py``）与 Electron 设置窗口
（``electron/main/config.ts``）都会整份重写 models.toml。单价进入用量统计计费之后，
两边对同一份配置写出不同的价格文本，就会出现「换个入口保存一次，费用口径就变了」
却没有任何报错。用例用 ``scripts/check/config_models_io.ts`` 驱动 Electron 写入器与读取器。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import json
import shutil
import subprocess
import tomllib

import pytest

from src.core.config.model_webui import (
    _dump_models,
    _normalize_generation,
    _normalize_models,
    _normalize_tasks,
)
from src.core.config.schema import CONFIG_VERSION, ModelCatalog, ModelDefinitionConfig
from src.core.config.settings_webui import _write_documented_toml, file_schema

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = 'scripts/check/config_models_io.ts'

# 覆盖：基础单价、缓存命中价（非零与 0）、留空缓存价、跨午夜时段、Python 与 JavaScript
# 在指数写法阈值上分歧的极小单价，以及不含两个新字段的老写法。
_BASE: Dict[str, Any] = {
    'model_identifier': 'fixture', 'api_provider': 'dashscope', 'extra_body': {},
    'api_format': 'openai', 'reasoning_parse_mode': 'field', 'visual': False, 'omni': False,
    'temperature': None, 'max_tokens': None, 'embedding_dim': 0,
}
MODELS: List[Dict[str, Any]] = [
    {
        **_BASE, 'name': '峰谷模型', 'price_in': 2.0, 'price_out': 8.0, 'cache_price_in': 0.5,
        'price_periods': [
            {'start': '00:30', 'end': '08:30', 'price_in': 1.0, 'price_out': 4.0, 'cache_price_in': 0.25},
            {'start': '23:00', 'end': '00:30', 'price_in': 0.00005, 'price_out': 1.5, 'cache_price_in': None},
        ],
    },
    {**_BASE, 'name': '缓存免费', 'price_in': 3.0, 'price_out': 12.0, 'cache_price_in': 0.0, 'price_periods': []},
    {**_BASE, 'name': '未填单价', 'price_in': 0.0, 'price_out': 0.0, 'cache_price_in': None, 'price_periods': []},
]


def _npx() -> str:
    npx = shutil.which('npx')
    if npx is None:
        pytest.fail('未找到 npx，无法运行 Electron 写入器；先在仓库根目录执行 npm install')
    return npx


def _run_ts(*args: str) -> str:
    result = subprocess.run(
        [_npx(), 'tsx', SCRIPT, *args],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace',
    )
    assert result.returncode == 0, f'{result.stdout}\n{result.stderr}'
    return result.stdout


def _price_section(text: str, model_name: str) -> str:
    """取出某个模型条目里从 price_in 起到条目结束的价格段。"""
    lines = text.replace('\r\n', '\n').split('\n')
    start = next(i for i, line in enumerate(lines) if line.startswith(f'name = "{model_name}"'))
    first = next(i for i in range(start, len(lines)) if lines[i].startswith('price_in = '))
    end = first
    while end < len(lines) and lines[end].strip() and not lines[end].startswith('[[models]]'):
        end += 1
    return '\n'.join(lines[first:end])


def _price_fields(model: ModelDefinitionConfig) -> Dict[str, Any]:
    return model.model_dump(include={'price_in', 'price_out', 'cache_price_in', 'price_periods'})


def _expected(name: str) -> Dict[str, Any]:
    source = next(model for model in MODELS if model['name'] == name)
    return ModelDefinitionConfig.model_validate(source).model_dump(
        include={'price_in', 'price_out', 'cache_price_in', 'price_periods'},
    )


@pytest.fixture(scope='module')
def electron_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """用 Electron 写入器写出一份含测试模型的完整配置目录。"""
    root = tmp_path_factory.mktemp('electron-writer')
    models_json = root / 'models.json'
    models_json.write_text(json.dumps(MODELS, ensure_ascii=False), encoding='utf-8')
    directory = root / 'config'
    _run_ts('write', str(directory), str(models_json))
    return directory


def test_python_and_electron_write_identical_price_sections(electron_dir: Path) -> None:
    electron_text = (electron_dir / 'models.toml').read_text(encoding='utf-8')
    python_text = _dump_models(
        _normalize_tasks({}), _normalize_generation({}), _normalize_models(MODELS),
    )
    for model in MODELS:
        python_section = _price_section(python_text, model['name'])
        assert python_section == _price_section(electron_text, model['name'])
        assert python_section.startswith('price_in = ')
    peak = _price_section(python_text, '峰谷模型')
    assert peak.count('[[models.price_periods]]') == 2
    # 极小单价两边都写成定点小数，不出现指数写法。
    assert 'price_in = 0.00005 #' in peak
    assert 'cache_price_in' not in _price_section(python_text, '未填单价')


def test_both_outputs_read_back_to_the_written_prices(electron_dir: Path, tmp_path: Path) -> None:
    electron_raw = tomllib.loads((electron_dir / 'models.toml').read_text(encoding='utf-8'))
    # Python 读 Electron 写出的文件。
    catalog = ModelCatalog.model_validate(electron_raw)
    by_name = {model.name: model for model in catalog.models}
    for model in MODELS:
        assert _price_fields(by_name[model['name']]) == _expected(model['name'])

    # Python 把同一份内容重写一遍，再交给 Electron 读取器读回。
    python_dir = tmp_path / 'config'
    shutil.copytree(electron_dir, python_dir)
    (python_dir / 'models.toml').write_text(
        _dump_models(
            _normalize_tasks(electron_raw['model_tasks']),
            _normalize_generation(electron_raw['generation']),
            _normalize_models(electron_raw['models']),
        ),
        encoding='utf-8',
    )
    read_back = {model['name']: model for model in json.loads(_run_ts('read', str(python_dir)))}
    for model in MODELS:
        electron_model = ModelDefinitionConfig.model_validate(read_back[model['name']])
        assert _price_fields(electron_model) == _expected(model['name'])


def test_settings_writer_emits_price_period_tables(tmp_path: Path) -> None:
    """设置页写入器同样把分时段单价写成子表数组，读回与写入前等价。"""
    models = _normalize_models(MODELS)
    document = {
        'inner': {'version': CONFIG_VERSION},
        'model_tasks': _normalize_tasks({}),
        'generation': _normalize_generation({}),
        'models': models,
    }
    path = tmp_path / 'models.toml'
    _write_documented_toml(path, file_schema('models.toml'), document, CONFIG_VERSION)
    text = path.read_text(encoding='utf-8')
    assert text.count('[[models.price_periods]]') == 2
    catalog = ModelCatalog.model_validate(tomllib.loads(text))
    for model in catalog.models:
        assert _price_fields(model) == _expected(model.name)


def test_legacy_models_without_new_fields_still_load() -> None:
    """不含 cache_price_in 与 price_periods 的老配置不需要改写就能加载。"""
    raw = tomllib.loads(
        f'[inner]\nversion = "{CONFIG_VERSION}"\n\n[model_tasks.chat]\nmodel_list = []\n\n'
        '[[models]]\nname = "old"\napi_provider = "p"\nprice_in = 1.5\nprice_out = 3.0\n'
    )
    model = ModelCatalog.model_validate(raw).models[0]
    assert model.cache_price_in is None
    assert model.price_periods == []
    assert (model.price_in, model.price_out) == (1.5, 3.0)
