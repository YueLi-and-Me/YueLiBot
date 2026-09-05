"""汇总可在线执行的只读运行时健康检查。

命令行入口与启动路径都调用 :func:`run_self_check`。检查只读取代码结构、配置文件
和 SQLite 快照；不会迁移数据库、补算向量、清理积压或改写配置。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Dict, List, Mapping, Optional, Set, Tuple

import inspect
import json
import sqlite3

from src.core.logging.console_layout import print_box
from src.core.db.migrations.bootstrap import (
    TS_FINAL_SCHEMA_VERSION,
    write_user_version,
)
from src.core.db.migrations.manager import (
    CURRENT_VERSION,
    load_migration_registry,
    migration_chain_errors,
)
from src.core.db.migrations.registry import MigrationFn
from src.core.logging.logger import get_logger
from src.core.config.adapter_selection import read_active_adapter
from src.core.config.loader import read_config
from src.core.config.schema import CONFIG_VERSION, Config
from src.core.memory import FACT_EXTRACT_CURSOR_KEY
from src.core.memory.vector_health import (
    VectorHealth,
    VectorTableHealth,
    inspect_vector_health,
    pending_embedding_counts,
)
from src.platforms.onebot11.config import read_section_config
from src.plugin_system import AdapterManifest, load_manifest

logger = get_logger(__name__)

PASS = '通过'
FAIL = '失败'
NOTICE = '提示'
CATEGORY_ORDER = (
    '迁移链完整性',
    '库结构与版本',
    '向量健康',
    '配置一致性',
    '功能开关与实际状态',
    '后台链路待办',
)
EXPECTED_ADAPTER_DIRECTORIES = (
    'yueli-napcat-adapter',
    'yueli-snowluma-adapter',
)


@dataclass(frozen=True)
class CheckItem:
    """一条可独立定位的自检结论。"""

    category: str
    name: str
    status: str
    detail: str

    def render(self) -> str:
        """返回适合命令行与启动信息框共用的中文单行文本。"""
        return f'[{self.status}] {self.category} / {self.name}：{self.detail}'


@dataclass(frozen=True)
class BacklogCounts:
    """后台链路三项待办的实际数量。"""

    knowledge_embedding: Optional[int] = None
    profile_dirty: Optional[int] = None
    extraction_lag: Optional[int] = None


@dataclass(frozen=True)
class SelfCheckReport:
    """一次运行时自检的完整只读报告。"""

    database_path: Path
    items: Tuple[CheckItem, ...]
    backlog: BacklogCounts
    elapsed_seconds: float

    @property
    def failed(self) -> bool:
        """任一检查失败时返回 ``True``。"""
        return any(item.status == FAIL for item in self.items)

    @property
    def exit_code(self) -> int:
        """返回可直接用于进程退出的健康结论。"""
        return 1 if self.failed else 0

    @property
    def findings(self) -> Tuple[CheckItem, ...]:
        """返回需要在启动信息框中展示的失败与非零待办。"""
        return tuple(item for item in self.items if item.status != PASS)


def open_readonly_database(path: Path) -> sqlite3.Connection:
    """以 SQLite URI 只读模式打开一个可与运行中 Bot 并存的连接。

    :param path: 已存在的 SQLite 数据库文件。
    :return: ``mode=ro`` 且 ``query_only`` 已开启的新连接。
    :raises OSError: 文件不存在或路径无法解析。
    :raises sqlite3.Error: SQLite 无法打开该文件。
    副作用：只创建连接并读取文件，不创建数据库、不写 WAL、不修改任何表。
    """
    resolved = path.resolve(strict=True)
    db = sqlite3.connect(
        f'{resolved.as_uri()}?mode=ro',
        uri=True,
        timeout=5.0,
        check_same_thread=False,
    )
    db.execute('PRAGMA query_only = ON')
    db.execute('PRAGMA busy_timeout = 5000')
    return db


def run_self_check(
    database_path: Path,
    config_dir: Path,
    adapters_root: Path,
    *,
    validated_config: Config | None = None,
    migration_registry: Mapping[int, MigrationFn] | None = None,
) -> SelfCheckReport:
    """执行六类只读检查并返回逐项报告。

    :param database_path: 真机或测试 SQLite 文件路径。
    :param config_dir: 四份主体 TOML 与 ``adapter.toml`` 所在目录。
    :param adapters_root: 适配器插件目录的父目录。
    :param validated_config: 启动路径已经由同一加载器校验过的配置对象；命令省略时
        本函数自行只读加载。
    :param migration_registry: 测试可注入的注册表快照；生产省略时从迁移管理器装载。
    :return: 包含退出码、耗时、逐项结论和三项积压数字的报告。
    副作用：读取代码、配置和数据库；bootstrap 版本域在独立内存库中执行写入探针，
        不触碰目标数据库或任何持久化文件。
    """
    started_at = perf_counter()
    categories: Dict[str, List[CheckItem]] = {
        category: [] for category in CATEGORY_ORDER
    }

    categories['迁移链完整性'].extend(
        check_migration_integrity(migration_registry)
    )

    config = validated_config
    if config is None:
        try:
            config = read_config(config_dir)
        except Exception as exc:
            categories['配置一致性'].append(CheckItem(
                '配置一致性',
                '主体配置',
                FAIL,
                f'{config_dir} 未通过现有配置加载器：{exc}',
            ))
    if config is not None:
        categories['配置一致性'].append(CheckItem(
            '配置一致性',
            '主体配置',
            PASS,
            f'4 份 TOML 的版本均为 {CONFIG_VERSION}，字段、引用与路由校验通过',
        ))
    categories['配置一致性'].extend(
        check_adapter_configs(config_dir, adapters_root)
    )

    vector_health: VectorHealth | None = None
    backlog = BacklogCounts()
    db: sqlite3.Connection | None = None
    try:
        db = open_readonly_database(database_path)
        # 一轮检查固定在同一个 WAL 读快照，避免后台刚好提交时各项数字来自不同时刻。
        db.execute('BEGIN')
    except (OSError, sqlite3.Error) as exc:
        if db is not None:
            db.close()
        detail = f'无法以只读方式检查 {database_path}：{exc}'
        categories['库结构与版本'].append(CheckItem(
            '库结构与版本', '只读连接', FAIL, detail,
        ))
        categories['向量健康'].append(CheckItem(
            '向量健康', '覆盖与维度', FAIL, detail,
        ))
        categories['功能开关与实际状态'].append(CheckItem(
            '功能开关与实际状态', '通道产出', FAIL, detail,
        ))
        categories['后台链路待办'].append(CheckItem(
            '后台链路待办', '积压计数', FAIL, detail,
        ))
    else:
        try:
            categories['库结构与版本'].extend(check_database_integrity(db))
            try:
                vector_health = inspect_vector_health(db)
                categories['向量健康'].extend(
                    check_vector_health(vector_health, config)
                )
            except (sqlite3.Error, TypeError, ValueError) as exc:
                categories['向量健康'].append(CheckItem(
                    '向量健康',
                    '覆盖与维度',
                    FAIL,
                    f'无法读取 facts / knowledge 向量列：{exc}',
                ))
            categories['功能开关与实际状态'].extend(
                check_feature_state(config, vector_health)
            )
            backlog, backlog_items = check_background_backlog(db)
            categories['后台链路待办'].extend(backlog_items)
        finally:
            db.close()

    items = tuple(
        item
        for category in CATEGORY_ORDER
        for item in categories[category]
    )
    return SelfCheckReport(
        database_path=database_path.resolve(),
        items=items,
        backlog=backlog,
        elapsed_seconds=perf_counter() - started_at,
    )


def check_migration_integrity(
    registry: Mapping[int, MigrationFn] | None = None,
) -> List[CheckItem]:
    """检查注册表连续性、链头、bootstrap 版本域和迁移文件归属。"""
    items: List[CheckItem] = []
    try:
        active_registry = (
            dict(registry) if registry is not None else load_migration_registry()
        )
    except Exception as exc:
        return [CheckItem(
            '迁移链完整性', '注册表装载', FAIL, f'迁移模块导入失败：{exc}',
        )]

    chain_errors = migration_chain_errors(active_registry)
    if chain_errors:
        items.extend(
            CheckItem('迁移链完整性', '连续性', FAIL, error)
            for error in chain_errors
        )
    else:
        items.append(CheckItem(
            '迁移链完整性',
            '连续性',
            PASS,
            f'注册表从 v{TS_FINAL_SCHEMA_VERSION}→v{CURRENT_VERSION} 连续，链头与 '
            f'CURRENT_VERSION={CURRENT_VERSION} 一致',
        ))

    file_errors: List[str] = []
    for version, migration in sorted(active_registry.items()):
        expected_name = f'v{version}_to_v{version + 1}.py'
        source = inspect.getsourcefile(migration)
        if source is None:
            file_errors.append(f'@register({version}) 无法定位源文件，应为 {expected_name}')
            continue
        source_path = Path(source)
        if source_path.name != expected_name or not source_path.is_file():
            file_errors.append(
                f'@register({version}) 来自 {source_path.name}，应来自 {expected_name}'
            )
    if file_errors:
        items.extend(
            CheckItem('迁移链完整性', '迁移文件', FAIL, error)
            for error in file_errors
        )
    else:
        items.append(CheckItem(
            '迁移链完整性',
            '迁移文件',
            PASS,
            f'{len(active_registry)} 个注册号均有同名 vN_to_vN+1.py 文件',
        ))

    bootstrap_error = _probe_bootstrap_version_domain()
    items.append(CheckItem(
        '迁移链完整性',
        'bootstrap 版本域',
        FAIL if bootstrap_error else PASS,
        bootstrap_error or f'write_user_version 覆盖 v1～v{CURRENT_VERSION}',
    ))
    return items


def _probe_bootstrap_version_domain() -> str:
    """在独立内存库中逐个调用真实版本写入口，捕获漏登记分支。"""
    db = sqlite3.connect(':memory:')
    try:
        for version in range(1, CURRENT_VERSION + 1):
            try:
                write_user_version(db, version)
            except (sqlite3.Error, ValueError) as exc:
                return f'bootstrap.py 未覆盖版本 v{version}：{exc}'
            actual = int(db.execute('PRAGMA user_version').fetchone()[0])
            if actual != version:
                return (
                    f'bootstrap.py 写入 v{version} 后读回 v{actual}，版本分支不一致'
                )
    finally:
        db.close()
    return ''


def check_database_integrity(db: sqlite3.Connection) -> List[CheckItem]:
    """检查 SQLite 自身完整性、版本关系与外键。"""
    items: List[CheckItem] = []
    try:
        quick_rows = [str(row[0]) for row in db.execute('PRAGMA quick_check').fetchall()]
        quick_ok = quick_rows == ['ok']
        items.append(CheckItem(
            '库结构与版本',
            'quick_check',
            PASS if quick_ok else FAIL,
            'ok' if quick_ok else '；'.join(quick_rows),
        ))
    except sqlite3.Error as exc:
        items.append(CheckItem(
            '库结构与版本', 'quick_check', FAIL, f'执行失败：{exc}',
        ))

    try:
        user_version = int(db.execute('PRAGMA user_version').fetchone()[0])
        if user_version < CURRENT_VERSION:
            status = FAIL
            detail = (
                f'user_version={user_version}，代码需要 {CURRENT_VERSION}；'
                '需要重启 Bot 完成迁移'
            )
        elif user_version > CURRENT_VERSION:
            status = FAIL
            detail = (
                f'user_version={user_version} 超前于代码的 CURRENT_VERSION='
                f'{CURRENT_VERSION}，这是危险信号，请核对代码与数据库来源'
            )
        else:
            status = PASS
            detail = f'user_version={user_version}，与 CURRENT_VERSION 一致'
        items.append(CheckItem('库结构与版本', 'user_version', status, detail))
    except (sqlite3.Error, TypeError, ValueError) as exc:
        items.append(CheckItem(
            '库结构与版本', 'user_version', FAIL, f'读取失败：{exc}',
        ))

    try:
        foreign_keys = db.execute('PRAGMA foreign_key_check').fetchall()
        if foreign_keys:
            samples = [
                f'{row[0]}(rowid={row[1]})→{row[2]}(fk={row[3]})'
                for row in foreign_keys[:5]
            ]
            items.append(CheckItem(
                '库结构与版本',
                'foreign_key_check',
                FAIL,
                f'发现 {len(foreign_keys)} 条外键错误：' + '、'.join(samples),
            ))
        else:
            items.append(CheckItem(
                '库结构与版本', 'foreign_key_check', PASS, '0 条外键错误',
            ))
    except sqlite3.Error as exc:
        items.append(CheckItem(
            '库结构与版本', 'foreign_key_check', FAIL, f'执行失败：{exc}',
        ))
    return items


def check_vector_health(
    health: VectorHealth,
    config: Config | None,
) -> List[CheckItem]:
    """把向量快照转为覆盖率、逐行一致性与维度结论。"""
    items = [
        _vector_table_item(health.facts),
        _vector_table_item(health.knowledge),
    ]
    dimensions: Set[int] = set(
        health.facts.embedding_dimensions
        + health.facts.quantized_dimensions
        + health.knowledge.embedding_dimensions
        + health.knowledge.quantized_dimensions
    )
    problems: List[str] = []
    if len(dimensions) > 1:
        problems.append(f'库内同时存在多个向量维度：{sorted(dimensions)}')
    configured_dimension = _configured_embedding_dimension(config)
    if configured_dimension is not None:
        if configured_dimension <= 0 and config is not None and config.vector.enabled:
            problems.append(
                f'向量开关已启用，但模型配置 embedding_dim={configured_dimension}'
            )
        elif dimensions and configured_dimension not in dimensions:
            problems.append(
                f'模型配置维度 {configured_dimension} 与库内维度 '
                f'{sorted(dimensions)} 不一致'
            )
    items.append(CheckItem(
        '向量健康',
        '维度自洽',
        FAIL if problems else PASS,
        '；'.join(problems)
        if problems
        else (
            f'库内维度={sorted(dimensions) if dimensions else "暂无向量"}；'
            f'配置维度={configured_dimension if configured_dimension is not None else "未配置"}'
        ),
    ))
    return items


def _vector_table_item(table: VectorTableHealth) -> CheckItem:
    """生成一张向量表的覆盖与逐行一致性结论。"""
    hard_errors = (
        table.embedding_without_quantized
        + table.quantized_without_embedding
        + table.invalid_embedding_count
        + table.invalid_quantized_count
        + table.dimension_mismatch_count
    )
    dimension_errors = (
        len(table.embedding_dimensions) > 1
        or len(table.quantized_dimensions) > 1
    )
    if hard_errors > 0 or dimension_errors:
        status = FAIL
    elif table.missing_embedding_count > 0 or table.missing_quantized_count > 0:
        status = NOTICE
    else:
        status = PASS
    embedding_rate = _coverage_rate(table.embedding_count, table.total)
    quantized_rate = _coverage_rate(table.quantized_count, table.total)
    detail = (
        f'总数={table.total}，embedding={table.embedding_count}/{table.total}'
        f'（{embedding_rate}），SQ8={table.quantized_count}/{table.total}'
        f'（{quantized_rate}）；缺原始={table.missing_embedding_count}，'
        f'缺SQ8={table.missing_quantized_count}，'
        f'原始有/SQ8空={table.embedding_without_quantized}，'
        f'原始空/SQ8有={table.quantized_without_embedding}，'
        f'非法原始={table.invalid_embedding_count}，非法SQ8={table.invalid_quantized_count}，'
        f'逐行维度不一致={table.dimension_mismatch_count}'
    )
    if table.problem_samples:
        detail += '；样例：' + '、'.join(table.problem_samples)
    return CheckItem('向量健康', table.table, status, detail)


def _coverage_rate(count: int, total: int) -> str:
    """把覆盖数量格式化为一位小数百分比。"""
    if total == 0:
        return '100.0%'
    return f'{count * 100 / total:.1f}%'


def _configured_embedding_dimension(config: Config | None) -> int | None:
    """返回已通过加载器一致性校验的首个 embedding 候选维度。"""
    if config is None or not config.routing.embedding.candidates:
        return None
    return config.routing.embedding.candidates[0].embedding_dim


def check_adapter_configs(config_dir: Path, adapters_root: Path) -> List[CheckItem]:
    """用插件清单和现有适配器配置读取器核对目录、段名与版本。"""
    items: List[CheckItem] = []
    try:
        active = read_active_adapter(config_dir)
        if not (adapters_root / active).is_dir():
            items.append(CheckItem(
                '配置一致性',
                '当前适配器',
                FAIL,
                f'{config_dir / "adapter.toml"} 指向不存在的目录 {active}',
            ))
        else:
            items.append(CheckItem(
                '配置一致性',
                '当前适配器',
                PASS,
                f'adapter.toml 指向 {active}',
            ))
    except Exception as exc:
        items.append(CheckItem(
            '配置一致性', '当前适配器', FAIL, f'读取 adapter.toml 失败：{exc}',
        ))

    manifest_paths = {
        adapters_root / directory / '_manifest.json'
        for directory in EXPECTED_ADAPTER_DIRECTORIES
    }
    manifest_paths.update(adapters_root.glob('*/_manifest.json'))
    adapter_count = 0
    for manifest_path in sorted(manifest_paths):
        try:
            manifest = load_manifest(manifest_path)
        except Exception as exc:
            items.append(CheckItem(
                '配置一致性',
                manifest_path.parent.name,
                FAIL,
                f'清单读取失败：{exc}',
            ))
            continue
        if not isinstance(manifest, AdapterManifest):
            continue
        adapter_count += 1
        directory = manifest_path.parent
        expected_directory = f'yueli-{manifest.config_section}-adapter'
        if directory.name != expected_directory:
            items.append(CheckItem(
                '配置一致性',
                directory.name,
                FAIL,
                f'清单声明 [{manifest.config_section}]，按约定目录应为 '
                f'{expected_directory}',
            ))
            continue
        config_path = directory / 'config.toml'
        try:
            read_section_config(config_path, manifest.config_section)
        except Exception as exc:
            items.append(CheckItem(
                '配置一致性',
                directory.name,
                FAIL,
                f'{config_path} 未通过 [{manifest.config_section}] 段与版本校验：{exc}',
            ))
            continue
        items.append(CheckItem(
            '配置一致性',
            directory.name,
            PASS,
            f'目录、清单与 config.toml 的 [{manifest.config_section}] 段一致',
        ))
    if adapter_count == 0:
        items.append(CheckItem(
            '配置一致性',
            '适配器配置',
            FAIL,
            f'{adapters_root} 下没有可检查的适配器清单',
        ))
    return items


def check_feature_state(
    config: Config | None,
    health: VectorHealth | None,
) -> List[CheckItem]:
    """检查功能声明与已有数据产出是否互相矛盾。"""
    items: List[CheckItem] = []
    if config is None:
        return [CheckItem(
            '功能开关与实际状态',
            '通道产出',
            FAIL,
            '主体配置未通过校验，无法判断功能开关',
        )]

    if config.vector.enabled and health is not None:
        zero_tables = [
            table.table
            for table in (health.facts, health.knowledge)
            if table.total > 0 and table.embedding_count == 0
        ]
        if zero_tables:
            items.append(CheckItem(
                '功能开关与实际状态',
                '向量通道产出',
                FAIL,
                'vector.enabled=true，但已有数据的 embedding 覆盖率为 0：'
                + '、'.join(
                    f'{table.table}={table.total} 条'
                    for table in (health.facts, health.knowledge)
                    if table.table in zero_tables
                ),
            ))
        else:
            sample_total = health.facts.total + health.knowledge.total
            items.append(CheckItem(
                '功能开关与实际状态',
                '向量通道产出',
                PASS,
                'vector.enabled=true，'
                + (
                    '已有样本至少产出过一条向量'
                    if sample_total > 0
                    else '当前没有 facts / knowledge 样本，暂无反证'
                ),
            ))
    elif config.vector.enabled:
        items.append(CheckItem(
            '功能开关与实际状态',
            '向量通道产出',
            FAIL,
            'vector.enabled=true，但向量表状态无法读取',
        ))
    else:
        items.append(CheckItem(
            '功能开关与实际状态',
            '向量通道产出',
            PASS,
            'vector.enabled=false，不要求向量通道产出',
        ))

    memory_route = config.routing.memory
    items.append(CheckItem(
        '功能开关与实际状态',
        '事实抽取装配',
        PASS if memory_route.ready else NOTICE,
        (
            f'memory 路由有 {len(memory_route.candidates)} 个候选，事实抽取已装配'
            if memory_route.ready
            else 'memory 模型路由不可用，事实抽取关闭（与启动期告警同一判据）'
        ),
    ))
    return items


def check_background_backlog(
    db: sqlite3.Connection,
) -> Tuple[BacklogCounts, List[CheckItem]]:
    """读取知识补算、画像脏位与事实抽取游标积压；非零不置失败。"""
    items: List[CheckItem] = []
    knowledge_count: int | None = None
    profile_count: int | None = None
    extraction_count: int | None = None

    try:
        knowledge_count = pending_embedding_counts(db)['knowledge']
        items.append(CheckItem(
            '后台链路待办',
            '知识待补算',
            NOTICE if knowledge_count > 0 else PASS,
            f'{knowledge_count} 条',
        ))
    except (sqlite3.Error, TypeError, ValueError) as exc:
        items.append(CheckItem(
            '后台链路待办',
            '知识待补算',
            FAIL,
            f'统计失败：{exc}',
        ))

    try:
        profile_count = int(db.execute(
            'SELECT COUNT(*) FROM person_profile WHERE dirty = 1'
        ).fetchone()[0])
        items.append(CheckItem(
            '后台链路待办',
            '画像脏位',
            NOTICE if profile_count > 0 else PASS,
            f'{profile_count} 人',
        ))
    except (sqlite3.Error, TypeError, ValueError) as exc:
        items.append(CheckItem(
            '后台链路待办', '画像脏位', FAIL, f'统计失败：{exc}',
        ))

    try:
        extraction_count, lagging_streams = _fact_extraction_backlog(db)
        detail = f'{extraction_count} 条消息'
        if lagging_streams:
            detail += '；' + '、'.join(
                f'stream {stream_id}={count}'
                for stream_id, count in lagging_streams[:5]
            )
        items.append(CheckItem(
            '后台链路待办',
            '抽取游标落后',
            NOTICE if extraction_count > 0 else PASS,
            detail,
        ))
    except (json.JSONDecodeError, sqlite3.Error, TypeError, ValueError) as exc:
        items.append(CheckItem(
            '后台链路待办', '抽取游标落后', FAIL, f'统计失败：{exc}',
        ))

    return BacklogCounts(
        knowledge_embedding=knowledge_count,
        profile_dirty=profile_count,
        extraction_lag=extraction_count,
    ), items


def _fact_extraction_backlog(
    db: sqlite3.Connection,
) -> Tuple[int, List[Tuple[int, int]]]:
    """按事实抽取使用的游标键和消息 ID 口径统计各 stream 落后量。"""
    row = db.execute(
        'SELECT value FROM meta WHERE key = ?',
        (FACT_EXTRACT_CURSOR_KEY,),
    ).fetchone()
    cursor_document: object = {} if row is None else json.loads(str(row[0]))
    if not isinstance(cursor_document, dict):
        raise ValueError(f'{FACT_EXTRACT_CURSOR_KEY} 不是 JSON 对象')

    lagging: List[Tuple[int, int]] = []
    total = 0
    streams = db.execute('SELECT id FROM streams ORDER BY id').fetchall()
    for stream_row in streams:
        stream_id = int(stream_row[0])
        raw_cursor = cursor_document.get(str(stream_id), 0)
        if not isinstance(raw_cursor, int) or raw_cursor < 0:
            raise ValueError(
                f'{FACT_EXTRACT_CURSOR_KEY}[{stream_id}]={raw_cursor!r} 不是非负整数'
            )
        count = int(db.execute(
            'SELECT COUNT(*) FROM messages WHERE stream_id = ? AND id > ?',
            (stream_id, raw_cursor),
        ).fetchone()[0])
        total += count
        if count > 0:
            lagging.append((stream_id, count))
    lagging.sort(key=lambda item: (-item[1], item[0]))
    return total, lagging


def render_report(report: SelfCheckReport) -> str:
    """把完整报告渲染为命令行文本。"""
    conclusion = '通过' if not report.failed else '发现问题'
    lines = [
        '月璃运行时自检',
        f'数据库：{report.database_path}',
        *[item.render() for item in report.items],
        (
            f'待办汇总：知识待补算={_count_text(report.backlog.knowledge_embedding)} · '
            f'画像脏位={_count_text(report.backlog.profile_dirty)} · '
            f'抽取游标落后={_count_text(report.backlog.extraction_lag)}'
        ),
        f'结论：{conclusion}（退出码 {report.exit_code}）',
        f'耗时：{report.elapsed_seconds:.3f} 秒',
        '数据变更：0（目标库以 mode=ro + query_only 打开）',
    ]
    return '\n'.join(lines)


def _count_text(value: int | None) -> str:
    """将未能读取的积压数字明确显示为不可用。"""
    return str(value) if value is not None else '不可用'


def announce_startup_self_check(
    database_path: Path,
    config_dir: Path,
    adapters_root: Path,
    config: Config,
) -> SelfCheckReport:
    """在启动期调用同一套自检，并只为失败或积压输出信息框。"""
    report = run_self_check(
        database_path,
        config_dir,
        adapters_root,
        validated_config=config,
    )
    if report.findings:
        print_box(
            '运行时自检发现问题' if report.failed else '运行时自检待办',
            [item.render() for item in report.findings],
            width=112,
            source=__name__,
        )
    if report.failed:
        logger.error(
            'runtime_self_check_failed',
            failedChecks=sum(item.status == FAIL for item in report.items),
            elapsedMs=round(report.elapsed_seconds * 1000),
        )
    else:
        logger.info(
            'runtime_self_check_passed',
            noticeChecks=sum(item.status == NOTICE for item in report.items),
            elapsedMs=round(report.elapsed_seconds * 1000),
        )
    return report
