"""加载、校验、哈希并归档运行时提示词模板。

本模块从内置目录读取声明式 Markdown 模板，可按配置目录加载用户覆盖版本，
校验模板 ID 与占位符集合，并在内容变化时写入有限历史。调用方通过全局目录
快照取得模板，``src.core.services.chat``、调度和摘要服务只负责传入已声明参数。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Tuple
import re

from src.core.common.logger import get_logger

logger = get_logger(__name__)

BUILTIN_PROMPT_DIR = Path(__file__).parent
MAX_TEMPLATE_HISTORY = 50
FIXED_TEMPLATE_IDS = frozenset({'chat.discipline', 'chat.boundaries'})
CHAT_PROTOCOL_TEMPLATE_ID = 'chat.protocol'
CHAT_SYSTEM_COMPONENTS: Dict[str, str] = {
    'chat.boundaries': 'boundaries',
    'chat.discipline': 'discipline',
    CHAT_PROTOCOL_TEMPLATE_ID: 'protocol',
}
CHAT_SYSTEM_VARIANT_COMPONENTS: Dict[str, str] = {
    'chat.length.brief': 'length',
    'chat.length.long': 'length',
}
REPLY_LENGTH_TEMPLATE_IDS: Dict[str, str] = {
    'brief': 'chat.length.brief',
    'long': 'chat.length.long',
}
CHAT_SYSTEM_TEMPLATE_IDS = (
    *CHAT_SYSTEM_COMPONENTS,
    'chat.system',
)
CHAT_PROACTIVE_TEMPLATE_IDS = (*CHAT_SYSTEM_TEMPLATE_IDS, 'chat.proactive')
# Conversation Agent 的系统提示词用「先动作头、后正文」协议替换既有直接发言协议，
# 避免同一段提示词同时给出两条冲突的输出指令。
CHAT_CONVERSATION_TEMPLATE_IDS = (
    'chat.discipline',
    'chat.boundaries',
    'chat.system',
    'chat.action.protocol',
)
# 决策与表达分离时回复生成那一次调用的模板组合：同一份人格与历史，协议换成
# 「动作已定，只把话说出来」。指纹与决策那一次分开算，两级各自的提示词变更
# 才能在观察面板上区分开。
CHAT_REPLYER_TEMPLATE_IDS = (
    'chat.discipline',
    'chat.boundaries',
    'chat.system',
    'chat.replyer',
)
TEMPLATE_IDS = (
    'chat.protocol',
    'chat.discipline',
    'chat.boundaries',
    *CHAT_SYSTEM_VARIANT_COMPONENTS,
    'chat.system',
    'chat.proactive',
    'summary',
    'schedule',
    'expression.select',
    'vision.glance',
    # 追加在末尾：既有验收断言 TEMPLATE_IDS[3:5] 固定为篇幅变体。
    'chat.action.protocol',
    'image.description',
    'emoji.description',
    'scene.observe',
    'chat.replyer',
)
TEMPLATE_PLACEHOLDERS: Dict[str, FrozenSet[str]] = {
    'chat.protocol': frozenset({'emotions', 'gestures', 'emoji_rule'}),
    'chat.discipline': frozenset(),
    'chat.boundaries': frozenset(),
    'chat.length.brief': frozenset(),
    'chat.length.long': frozenset(),
    'chat.system': frozenset({
        'name',
        'identity',
        'relationship',
        'time_context',
        'birthday_note',
        'resumption',
        'persona',
        'activity',
        'scene',
        'facts',
        'episodes',
        'reply_style',
        'tone',
        'expression_habits',
        'length',
        'discipline',
        'boundaries',
        'protocol',
    }),
    'chat.proactive': frozenset({'situation'}),
    'summary': frozenset({'character_name', 'character_personality'}),
    'schedule': frozenset({
        'character_name',
        'date',
        'weekday',
        'occasion',
        'character_personality',
        'persona',
        'yesterday_theme',
        'yesterday_bedtime',
        'yesterday_wake',
        'yesterday_carry_over',
        'yesterday_avoided',
        'density',
        'min_slots',
        'max_slots',
        'sleep_rule',
    }),
    'expression.select': frozenset({'history', 'user_text', 'options', 'limit'}),
    'vision.glance': frozenset({'app_hint'}),
    'scene.observe': frozenset({'history', 'atmospheres', 'topic_limit'}),
    'chat.replyer': frozenset({
        'reference',
        'length_rule',
        'emotions',
        'gestures',
        'emoji_rule',
    }),
    'chat.action.protocol': frozenset({
        'available_actions',
        'selectable_messages',
        'turn_scope',
        'quote_rule',
        'emotions',
        'gestures',
        'reply_example',
        'silent_example',
        'emoji_rule',
        'cognition_rule',
        'react_rule',
        'poke_rule',
        'wait_rule',
        'speak_rule',
    }),
    'image.description': frozenset(),
    'emoji.description': frozenset(),
}

_PLACEHOLDER_PATTERN = re.compile(r'\{\{([a-z][a-z0-9_]*)\}\}')


@dataclass(frozen=True)
class PromptTemplate:
    """一次启动中固定不变的生效模板及其内容指纹。"""

    id: str
    text: str
    source: Path
    placeholders: FrozenSet[str]
    sha256: str

    def render(self, **values: str) -> str:
        """使用完整占位符集合渲染模板文本。

        :param **values: 占位符名称到替换文本的映射。键集合必须与模板声明完全相同，
                值按字符串处理，不会再次解释其中的模板语法。

        :return: 仅替换 ``{{name}}`` 形式占位符后的模板文本。

        :raises ValueError: 提供的键缺失或多于模板声明。
        """

        provided = frozenset(values)
        if provided != self.placeholders:
            raise _placeholder_error(self.id, self.placeholders, provided, '渲染参数')
        return _PLACEHOLDER_PATTERN.sub(
            lambda match: values[match.group(1)],
            self.text,
        )


class PromptCatalog:
    """全量提示词模板的不可变启动快照。"""

    def __init__(self, templates: Dict[str, PromptTemplate]) -> None:
        """创建模板目录副本。

        :param templates: 模板 ID 到模板对象的映射；构造后目录不会引用调用方的
                可变字典。
        """

        self._templates = dict(templates)

    def get(self, template_id: str) -> PromptTemplate:
        """按声明的模板 ID 取得模板。

        :param template_id: ``TEMPLATE_IDS`` 中的模板标识。

        :return: 对应的不可变模板对象。

        :raises KeyError: 模板 ID 未加载或未在注册表中声明。
        """

        try:
            return self._templates[template_id]
        except KeyError as exc:
            raise KeyError(f'未声明的提示词模板：{template_id}') from exc

    def combined_hash(self, template_ids: Iterable[str]) -> str:
        """计算一组模板内容的稳定八位调用指纹。

        :param template_ids: 要参与计算的模板 ID 可迭代对象；顺序不影响结果。

        :return: 按模板 ID 排序后拼接各模板 SHA-256，再计算得到的十六进制前八位。

        :raises KeyError: 集合中包含未加载模板。
        """

        hashes = ''.join(self.get(template_id).sha256 for template_id in sorted(template_ids))
        return sha256(hashes.encode('ascii')).hexdigest()[:8]


def _placeholder_error(
    template_id: str,
    declared: FrozenSet[str],
    actual: FrozenSet[str],
    source: str,
) -> ValueError:
    """构造占位符声明与实际输入不一致的错误。

    :param template_id: 出错模板 ID。
    :param declared: 模板声明的占位符集合。
    :param actual: 调用方或文件实际提供的占位符集合。
    :param source: 错误来源标签，例如 ``渲染参数`` 或 ``占位符``。

    :return: 包含缺失项和多余项的 ``ValueError`` 实例。
    """

    missing = sorted(declared - actual)
    extra = sorted(actual - declared)
    details = []
    if missing:
        details.append(f'缺少 {", ".join(missing)}')
    if extra:
        details.append(f'多出 {", ".join(extra)}')
    return ValueError(
        f'提示词模板 {template_id} 的{source}与声明不一致：{"；".join(details)}'
    )


def validate_prompt_text(template_id: str, text: str) -> FrozenSet[str]:
    """校验一份候选模板的 ID 和占位符集合。

    :param template_id: 已声明的模板 ID。
    :param text: 待校验的完整 Markdown 文本。

    :return: 与声明一致的占位符集合。

    :raises KeyError: 模板 ID 未声明。
    :raises ValueError: 文本为空，或占位符集合缺失或多出字段。
    """

    try:
        declared = TEMPLATE_PLACEHOLDERS[template_id]
    except KeyError as exc:
        raise KeyError(f'未声明的提示词模板：{template_id}') from exc
    if not text.strip():
        raise ValueError(f'提示词模板 {template_id} 不能为空')
    actual = frozenset(_PLACEHOLDER_PATTERN.findall(text))
    if actual != declared:
        raise _placeholder_error(template_id, declared, actual, '占位符')
    return actual


def load_prompt_catalog(
    data_dir: Path | None,
    *,
    builtin_dir: Path = BUILTIN_PROMPT_DIR,
) -> PromptCatalog:
    """加载并校验全部提示词模板。

    :param data_dir: 可选的运行时数据目录；存在时从其 ``prompts`` 子目录读取用户
            覆盖并归档生效内容。
    :param builtin_dir: 内置模板目录，默认指向当前模块所在目录。

    :return: 已完成占位符校验的模板目录快照。

    :raises FileNotFoundError: 必需的内置模板缺失。
    :raises ValueError: 模板内容的占位符集合与声明不一致。
    :raises OSError: 模板读取或归档失败。

    副作用：
        当 ``data_dir`` 非空时，内容变化会写入模板历史并删除超出保留数量的旧版本。
    """

    override_dir = data_dir / 'prompts' if data_dir is not None else None
    templates: Dict[str, PromptTemplate] = {}
    for template_id in TEMPLATE_IDS:
        builtin_path = builtin_dir / f'{template_id}.md'
        override_path = override_dir / f'{template_id}.md' if override_dir is not None else None
        # 固定纪律模板禁止覆盖，其余模板按“用户文件存在则优先”选择来源。
        if override_path is not None and override_path.exists():
            if template_id in FIXED_TEMPLATE_IDS:
                logger.warning(
                    'prompt_override_ignored',
                    promptId=template_id,
                    reason='固定事实纪律与边界不允许覆盖',
                )
                source = builtin_path
            else:
                source = override_path
        else:
            source = builtin_path
        text = source.read_text(encoding='utf-8')
        # 在构造目录快照前校验占位符集合，避免错误模板进入运行时全局状态。
        declared = validate_prompt_text(template_id, text)
        templates[template_id] = PromptTemplate(
            id=template_id,
            text=text,
            source=source,
            placeholders=declared,
            sha256=sha256(text.encode('utf-8')).hexdigest(),
        )
    catalog = PromptCatalog(templates)
    if data_dir is not None:
        _archive_templates(catalog, data_dir)
    return catalog


def _archive_templates(catalog: PromptCatalog, data_dir: Path) -> None:
    """归档发生变化的模板，并限制每个模板的历史文件数量。

    :param catalog: 已加载并校验的模板目录。
    :param data_dir: 运行时数据目录，历史写入其 ``prompts/history`` 子目录。

    :raises OSError: 历史目录创建、读取、写入或清理失败。
    """

    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    history_root = data_dir / 'prompts' / 'history'
    for template_id in TEMPLATE_IDS:
        template = catalog.get(template_id)
        history_dir = history_root / template_id
        archived = list(history_dir.glob('*.md')) if history_dir.exists() else []
        if archived:
            # 文件系统不保证目录枚举顺序，使用修改时间和文件名共同确定最近归档。
            latest = max(archived, key=lambda path: (path.stat().st_mtime_ns, path.name))
            latest_text = latest.read_text(encoding='utf-8')
            latest_hash = sha256(latest_text.encode('utf-8')).hexdigest()
            # 内容未变化时不新增副本，避免启动流程重复堆积相同模板。
            if latest_hash == template.sha256:
                continue
        history_dir.mkdir(parents=True, exist_ok=True)
        archive_path = history_dir / f'{timestamp}-{template.sha256[:8]}.md'
        archive_path.write_text(template.text, encoding='utf-8')
        archived = sorted(
            history_dir.glob('*.md'),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
        )
        # 按同一排序规则保留最新的固定数量，删除更早版本以限制磁盘增长。
        for expired in archived[:-MAX_TEMPLATE_HISTORY]:
            expired.unlink()


_catalog = load_prompt_catalog(None)
_data_dir: Path | None = None


def configure_prompts(data_dir: Path) -> PromptCatalog:
    """在启动期加载指定数据目录中的用户提示词覆盖。

    :param data_dir: 运行时数据目录。

    :return: 新的全局提示词目录快照。

    :raises FileNotFoundError, ValueError, OSError: 加载或校验模板失败时直接传播。
    """

    global _catalog, _data_dir
    _catalog = load_prompt_catalog(data_dir)
    _data_dir = data_dir
    return _catalog


def get_prompt(template_id: str) -> PromptTemplate:
    """从当前全局目录取得指定提示词模板。

    :param template_id: 已声明的模板标识。

    :return: 当前生效的模板对象。

    :raises KeyError: 模板标识不存在。
    """

    return _catalog.get(template_id)


def render_chat_system(
    system_values: Dict[str, str],
    component_values: Dict[str, Dict[str, str]],
) -> Tuple[str, Dict[str, str]]:
    """用当前聊天子模板补全并渲染系统提示词。

    :param system_values: 不含聊天子模板正文的 ``chat.system`` 渲染参数。
    :param component_values: 固定聊天组件与至多一个同槽位变体的渲染参数。
    :return: 完整系统提示词，以及包含当前子模板正文的完整渲染参数副本。
    :raises KeyError: 缺少已声明子模板的渲染参数或模板未加载。
    :raises ValueError: 子模板或系统模板的渲染参数不符合占位符声明。
    副作用：不修改输入字典。
    """
    fixed_template_ids = set(CHAT_SYSTEM_COMPONENTS)
    missing_fixed = fixed_template_ids - component_values.keys()
    if missing_fixed:
        raise KeyError(f'系统提示词缺少固定组件：{", ".join(sorted(missing_fixed))}')
    known_template_ids = fixed_template_ids | set(CHAT_SYSTEM_VARIANT_COMPONENTS)
    unknown = component_values.keys() - known_template_ids
    if unknown:
        raise ValueError(f'系统提示词包含未知组件：{", ".join(sorted(unknown))}')

    resolved_values = dict(system_values)
    resolved_values.update({
        placeholder: get_prompt(template_id).render(
            **component_values[template_id]
        ).rstrip()
        for template_id, placeholder in CHAT_SYSTEM_COMPONENTS.items()
    })
    selected_variants: Dict[str, str] = {}
    for template_id, placeholder in CHAT_SYSTEM_VARIANT_COMPONENTS.items():
        if template_id not in component_values:
            continue
        previous = selected_variants.get(placeholder)
        if previous is not None:
            raise ValueError(
                f'系统提示词变体占位符 {placeholder} 同时选择了 '
                f'{previous} 与 {template_id}'
            )
        selected_variants[placeholder] = template_id
    for placeholder in set(CHAT_SYSTEM_VARIANT_COMPONENTS.values()):
        template_id = selected_variants.get(placeholder)
        resolved_values[placeholder] = (
            f'\n\n{get_prompt(template_id).render(**component_values[template_id]).rstrip()}'
            if template_id is not None
            else ''
        )
    return get_prompt('chat.system').render(**resolved_values), resolved_values


def prompt_metadata(prompt_id: str, template_ids: Iterable[str]) -> Dict[str, str]:
    """生成调用日志使用的提示词 ID 与内容指纹。

    :param prompt_id: 当前调用场景的逻辑标识。
    :param template_ids: 参与当前调用的模板 ID 可迭代对象。

    :return: 包含 ``promptId`` 和八位 ``promptHash`` 的字典。

    :raises KeyError: ``template_ids`` 包含未知模板。
    """

    return {
        'promptId': prompt_id,
        'promptHash': _catalog.combined_hash(template_ids),
    }


def list_prompts() -> List[Dict[str, Any]]:
    """列出全部模板的生效来源、哈希与占位符声明。"""

    return [
        {
            'id': template_id,
            'source': (
                'builtin'
                if _catalog.get(template_id).source == BUILTIN_PROMPT_DIR / f'{template_id}.md'
                else 'override'
            ),
            'promptHash': _catalog.get(template_id).sha256[:8],
            'placeholders': sorted(_catalog.get(template_id).placeholders),
            'fixed': template_id in FIXED_TEMPLATE_IDS,
        }
        for template_id in TEMPLATE_IDS
    ]


def prompt_detail(template_id: str) -> Dict[str, Any]:
    """读取指定模板的生效文本与内置文本。"""

    template = _catalog.get(template_id)
    builtin = (BUILTIN_PROMPT_DIR / f'{template_id}.md').read_text(encoding='utf-8')
    return {
        **next(item for item in list_prompts() if item['id'] == template_id),
        'content': template.text,
        'builtinContent': builtin,
    }


def update_prompt(template_id: str, text: str) -> Dict[str, Any]:
    """校验并原子写入一份用户模板覆盖，随后立即热重载。"""

    if template_id in FIXED_TEMPLATE_IDS:
        raise PermissionError(f'固定提示词模板不允许修改：{template_id}')
    validate_prompt_text(template_id, text)
    if _data_dir is None:
        raise RuntimeError('提示词数据目录尚未配置')
    override_dir = _data_dir / 'prompts'
    override_dir.mkdir(parents=True, exist_ok=True)
    target = override_dir / f'{template_id}.md'
    temporary = override_dir / f'.{template_id}.md.tmp'
    temporary.write_text(text, encoding='utf-8')
    temporary.replace(target)
    configure_prompts(_data_dir)
    return prompt_detail(template_id)


def delete_prompt_override(template_id: str) -> Dict[str, Any]:
    """删除用户模板覆盖并立即回到内置版本。"""

    if template_id in FIXED_TEMPLATE_IDS:
        raise PermissionError(f'固定提示词模板不允许删除：{template_id}')
    if template_id not in TEMPLATE_PLACEHOLDERS:
        raise KeyError(f'未声明的提示词模板：{template_id}')
    if _data_dir is None:
        raise RuntimeError('提示词数据目录尚未配置')
    target = _data_dir / 'prompts' / f'{template_id}.md'
    if target.exists():
        target.unlink()
    configure_prompts(_data_dir)
    return prompt_detail(template_id)


def prompt_history(template_id: str) -> List[Dict[str, Any]]:
    """按时间倒序列出指定模板的归档文件。"""

    if template_id not in TEMPLATE_PLACEHOLDERS:
        raise KeyError(f'未声明的提示词模板：{template_id}')
    if _data_dir is None:
        return []
    history_dir = _data_dir / 'prompts' / 'history' / template_id
    if not history_dir.exists():
        return []
    return [
        {
            'name': path.name,
            'content': path.read_text(encoding='utf-8'),
            'updatedAt': int(path.stat().st_mtime * 1_000),
        }
        for path in sorted(
            history_dir.glob('*.md'),
            key=lambda item: (item.stat().st_mtime_ns, item.name),
            reverse=True,
        )
    ]


def reset_prompts_for_tests() -> None:
    """将全局目录恢复为仅包含内置模板的快照。

    该函数只供测试隔离使用；调用会替换进程内当前目录，不写入用户覆盖目录。
    """

    global _catalog, _data_dir
    _catalog = load_prompt_catalog(None)
    _data_dir = None
