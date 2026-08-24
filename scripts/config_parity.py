"""配置写入器对齐校验：Python schema 与 Electron 模板必须字段级一致。

起因见 docs/config-writer-parity.md：两个运行时对同一份配置文件的所有权分裂
（Electron 负责写、Python 负责校验），schema 加了字段而 TS 模板没跟上的漂移
曾让整份重写静默毁掉约 30 个字段。本脚本让漂移当场红灯：

1. 用 Electron 写入器把「全默认值配置目录」写进临时目录（scripts/config_defaults.ts）；
2. 用 tomllib 解析四个文件，抽取每个配置表的字段集与字典表的键集；
3. 与 src/core/config/schema.py 各文档模型的字段集做差集；
4. 任一方向的差集非空即失败，逐字段报告。

排除口径与 config/upgrade.py 的 _SKIP_SECTIONS 一致：`inner` 是版本标记不是
设置项；`api_providers` / `models` 的条目是用户数据，但**条目内的字段名**仍要
对齐。`models[].temperature` / `max_tokens` 默认 None——TOML 表达不了 null，
模板只在有值时写出，因此这两个字段允许缺席。

用法（在仓库根目录）：
    python scripts/config_parity.py     # 校验，漂移时退出码 1
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import get_args, get_origin, get_type_hints

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    # 直接以 python scripts/config_parity.py 运行时仓库根不在导入路径上。
    sys.path.insert(0, str(REPO_ROOT))

from pydantic import BaseModel

from src.core.config.schema import (
    BotDocument,
    FeatureDocument,
    GenerationConfig,
    GenerationTaskConfig,
    ModelCatalog,
    ModelTaskConfig,
    ProviderCatalog,
    ProactiveGenerationTaskConfig,
    TaskRoutingConfig,
)

# 版本标记不是设置项，与 upgrade.py 的 _SKIP_SECTIONS 同口径。
SKIP_TOP_SECTIONS = frozenset({'inner'})

# 默认 None 的可写字段：TOML 没有 null，模板只在有值时写键，允许缺席。
NULLABLE_TABLE_FIELDS: dict[str, frozenset[str]] = {
    'models': frozenset({'temperature', 'max_tokens'}),
}

# 文件 → 文档模型。schema 是真相源；这里的映射只描述「哪个文件长什么样」。
DOCUMENTS: dict[str, type[BaseModel]] = {
    'providers.toml': ProviderCatalog,
    'models.toml': ModelCatalog,
    'bot.toml': BotDocument,
    'features.toml': FeatureDocument,
}

# TOML 里形如「字典表」的模型字段：模型自身的字段名是键集（任务名），
# 值模型给出每个子表的字段集。GenerationConfig 的 proactive 档带 enabled，
# 因此并集同时覆盖两种任务模型。
DICT_TABLE_VALUE_MODELS: dict[type, tuple[type, ...]] = {
    ModelTaskConfig: (TaskRoutingConfig,),
    GenerationConfig: (GenerationTaskConfig, ProactiveGenerationTaskConfig),
}


def _entry_model(annotation: object) -> type[BaseModel] | None:
    """若注解是 ``list[某模型]``，返回条目模型；否则返回 None。"""
    if get_origin(annotation) is not list:
        return None
    args = get_args(annotation)
    if args and isinstance(args[0], type) and issubclass(args[0], BaseModel):
        return args[0]
    return None


def schema_tables(document: type[BaseModel]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """把文档模型展开成「表名 → 字段集」与「字典表名 → 键集」。

    - 数组表（api_providers / models）：条目模型的字段就是该表的字段；
    - 字典表（model_tasks / generation）：模型字段名是键集（任务槽，少写一个
      槽必须红灯——当年正是这样丢了 planner/replyer/scene/memory），值模型的
      字段并入表字段；
    - 嵌套模型（typing.follow_up / typing.nudge）：父表不含该键，子表单独列出。
    """
    tables: dict[str, set[str]] = {}
    dict_keys: dict[str, set[str]] = {}
    hints = get_type_hints(document)
    for name, annotation in hints.items():
        if name in SKIP_TOP_SECTIONS:
            continue
        if isinstance(annotation, type) and annotation in DICT_TABLE_VALUE_MODELS:
            value_models = DICT_TABLE_VALUE_MODELS[annotation]
            tables[name] = set().union(*(set(m.model_fields) for m in value_models))
            dict_keys[name] = set(annotation.model_fields)
            continue
        item_model = _entry_model(annotation)
        if item_model is not None:
            tables[name] = set(item_model.model_fields)
            continue
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            nested: set[str] = set()
            for child_name, child_annotation in get_type_hints(annotation).items():
                if isinstance(child_annotation, type) and issubclass(child_annotation, BaseModel):
                    tables[f'{name}.{child_name}'] = set(child_annotation.model_fields)
                else:
                    nested.add(child_name)
            tables[name] = nested
            continue
        tables.setdefault('__top__', set()).add(name)
    return tables, dict_keys


def template_tables(
    data: dict,
    dict_table_names: set[str],
    subtables: dict[str, set[str]],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """从模板写出的 TOML 文档抽取「表名 → 字段集」与「字典表名 → 键集」。

    TOML 解析后「配置子表」（typing.follow_up）与「数据映射字段」
    （log.library_levels 的库名）都是 dict，纯结构区分不了；因此以 schema
    侧的展开结果为准——只有 schema 声明过的子表名才按子表处理，其余
    dict 值一律算作父表的字段键。

    :param data: tomllib 解析出的文档。
    :param dict_table_names: schema 侧的字典表名（model_tasks / generation）。
    :param subtables: schema 侧的子表名映射（顶层表名 → 子表名集合）。
    :return: ``(表名 → 字段集, 字典表名 → 键集)``。
    """
    tables: dict[str, set[str]] = {}
    dict_keys: dict[str, set[str]] = {}
    for key, value in data.items():
        if key in SKIP_TOP_SECTIONS:
            continue
        if isinstance(value, list):
            tables[key] = set(value[0]) if value else set()
        elif isinstance(value, dict):
            known = subtables.get(key, set())
            if key in dict_table_names:
                # 字典表：每个子表是一个任务槽，字段集取所有子表的并集。
                tables[key] = set().union(*(set(v) for v in value.values() if isinstance(v, dict)))
                dict_keys[key] = set(value)
            else:
                fields: set[str] = set()
                for child_name, child_value in value.items():
                    if isinstance(child_value, dict) and child_name in known:
                        tables[f'{key}.{child_name}'] = set(child_value)
                    else:
                        # 普通字段；映射型字段的键是用户数据，不算配置字段名。
                        fields.add(child_name)
                tables[key] = fields
        else:
            tables.setdefault('__top__', set()).add(key)
    return tables, dict_keys


def collect_problems() -> list[str]:
    """跑完整校验并收集全部差异行。

    :return: 差异描述列表；为空表示 schema 与模板完全对齐。
    """
    npx = shutil.which('npx')
    if npx is None:
        return ['未找到 npx，无法运行 Electron 写入器；先在仓库根目录执行 npm install']
    with tempfile.TemporaryDirectory(prefix='config-parity-') as temp:
        result = subprocess.run(
            [npx, 'tsx', 'scripts/config_defaults.ts', temp],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return [f'生成默认配置目录失败：\n{result.stdout}\n{result.stderr}']
        problems: list[str] = []
        for file_name, document in DOCUMENTS.items():
            raw = tomllib.loads((Path(temp) / file_name).read_text(encoding='utf-8'))
            expected, expected_keys = schema_tables(document)
            subtables: dict[str, set[str]] = {}
            for table in expected:
                parent, _, child = table.partition('.')
                if child:
                    subtables.setdefault(parent, set()).add(child)
            actual, actual_keys = template_tables(raw, set(expected_keys), subtables)
            for table in sorted(expected.keys() | actual.keys()):
                want = expected.get(table, set()) - NULLABLE_TABLE_FIELDS.get(table, frozenset())
                have = actual.get(table, set())
                for field in sorted(want - have):
                    problems.append(f'{file_name} 的 [{table}] 缺字段：{field}（schema 有，模板没有）')
                for field in sorted(have - want):
                    problems.append(f'{file_name} 的 [{table}] 多字段：{field}（模板有，schema 没有）')
            for table in sorted(expected_keys.keys() | actual_keys.keys()):
                want = expected_keys.get(table, set())
                have = actual_keys.get(table, set())
                for key in sorted(want - have):
                    problems.append(f'{file_name} 的 [{table}] 缺键：{key}（schema 有该槽，模板没写）')
                for key in sorted(have - want):
                    problems.append(f'{file_name} 的 [{table}] 多键：{key}（模板写了，schema 没有该槽）')
    return problems


def main() -> int:
    """执行校验并打印报告。

    :return: 对齐时 0，发现漂移时 1。
    """
    problems = collect_problems()
    if not problems:
        print('配置写入器与 schema 对齐：四个文件的字段集与任务槽完全一致。')
        return 0
    print('配置写入器与 schema 漂移（schema 是真相源，请补齐 electron/main/config.ts）：')
    for line in problems:
        print(f'  - {line}')
    return 1


if __name__ == '__main__':
    sys.exit(main())
