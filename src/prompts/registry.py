"""内置提示词与用户覆盖文件的加载入口。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet
import re

from src.common.logger import get_logger

logger = get_logger(__name__)

BUILTIN_PROMPT_DIR = Path(__file__).parent
FIXED_TEMPLATE_IDS = frozenset({'chat.discipline', 'chat.boundaries'})
CHAT_SYSTEM_TEMPLATE_IDS = (
    'chat.boundaries',
    'chat.discipline',
    'chat.protocol',
)
TEMPLATE_IDS = (
    'chat.protocol',
    'chat.discipline',
    'chat.boundaries',
    'chat.proactive',
    'summary',
    'schedule',
    'expression.select',
    'vision.glance',
)
TEMPLATE_PLACEHOLDERS: Dict[str, FrozenSet[str]] = {
    'chat.protocol': frozenset({'emotions', 'gestures'}),
    'chat.discipline': frozenset(),
    'chat.boundaries': frozenset(),
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
}

_PLACEHOLDER_PATTERN = re.compile(r'\{\{([a-z][a-z0-9_]*)\}\}')


@dataclass(frozen=True)
class PromptTemplate:
    """一次启动中固定不变的生效模板。"""

    id: str
    text: str
    source: Path
    placeholders: FrozenSet[str]

    def render(self, **values: str) -> str:
        """只替换声明式双花括号，不解释条件、循环或值中的模板文本。"""

        provided = frozenset(values)
        if provided != self.placeholders:
            raise _placeholder_error(self.id, self.placeholders, provided, '渲染参数')
        return _PLACEHOLDER_PATTERN.sub(
            lambda match: values[match.group(1)],
            self.text,
        )


class PromptCatalog:
    """全量模板的不可变启动快照。"""

    def __init__(self, templates: Dict[str, PromptTemplate]) -> None:
        self._templates = dict(templates)

    def get(self, template_id: str) -> PromptTemplate:
        try:
            return self._templates[template_id]
        except KeyError as exc:
            raise KeyError(f'未声明的提示词模板：{template_id}') from exc


def _placeholder_error(
    template_id: str,
    declared: FrozenSet[str],
    actual: FrozenSet[str],
    source: str,
) -> ValueError:
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


def load_prompt_catalog(
    data_dir: Path | None,
    *,
    builtin_dir: Path = BUILTIN_PROMPT_DIR,
) -> PromptCatalog:
    """优先读取用户覆盖；固定纪律始终使用内置版本。"""

    override_dir = data_dir / 'prompts' if data_dir is not None else None
    templates: Dict[str, PromptTemplate] = {}
    for template_id in TEMPLATE_IDS:
        builtin_path = builtin_dir / f'{template_id}.md'
        override_path = override_dir / f'{template_id}.md' if override_dir is not None else None
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
        declared = TEMPLATE_PLACEHOLDERS[template_id]
        actual = frozenset(_PLACEHOLDER_PATTERN.findall(text))
        if actual != declared:
            raise _placeholder_error(template_id, declared, actual, '占位符')
        templates[template_id] = PromptTemplate(
            id=template_id,
            text=text,
            source=source,
            placeholders=declared,
        )
    return PromptCatalog(templates)


_catalog = load_prompt_catalog(None)


def configure_prompts(data_dir: Path) -> PromptCatalog:
    """在启动期一次性装入用户覆盖。"""

    global _catalog
    _catalog = load_prompt_catalog(data_dir)
    return _catalog


def get_prompt(template_id: str) -> PromptTemplate:
    return _catalog.get(template_id)


def reset_prompts_for_tests() -> None:
    """恢复仅使用内置模板的注册表。"""

    global _catalog
    _catalog = load_prompt_catalog(None)
