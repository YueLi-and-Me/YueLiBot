"""维护只读开发者命令目录，并在唯一入口执行 owner 鉴权。"""

from __future__ import annotations

from dataclasses import dataclass
from inspect import isawaitable
from typing import Awaitable, Callable, List, Match, Pattern, Protocol, Set, Tuple

import re

from src.core.common.logger import get_logger
from src.core.platform_io.types import ConversationContext, PersonRef

logger = get_logger(__name__)


class OwnerRegistry(Protocol):
    """声明命令通道鉴权所需的最小注册表接口。"""

    def owner_person(self) -> PersonRef:
        """返回系统唯一 owner 的稳定人物引用。"""


@dataclass(frozen=True)
class CommandContext:
    """传给命令处理器的只读入站上下文。"""

    raw_text: str
    match: Match[str]
    conversation: ConversationContext


CommandHandler = Callable[[CommandContext], str | Awaitable[str]]


@dataclass(frozen=True)
class CommandSpec:
    """一条已注册命令的匹配规则、说明与处理器。"""

    name: str
    pattern: Pattern[str]
    description: str
    handler: CommandHandler


@dataclass(frozen=True)
class CommandCatalogItem:
    """提供给只读 WebUI 的命令目录项。"""

    name: str
    pattern: str
    description: str
    owner_required: bool = True


@dataclass(frozen=True)
class CommandDispatch:
    """命令通道已经消费入站消息后的出站结果。"""

    command: str
    text: str
    succeeded: bool


_commands: List[CommandSpec] = []
_command_names: Set[str] = set()


def register_command(
    name: str,
    pattern: str,
    description: str,
) -> Callable[[CommandHandler], CommandHandler]:
    """注册一条只读 owner 命令。

    :param name: 以 `/` 开头、用于帮助页展示的命令名。
    :param pattern: 对完整去首尾空白文本执行匹配的正则表达式。
    :param description: 在帮助页与只读 WebUI 中展示的简体中文说明。
    :return: 保留原处理器的注册装饰器。
    :raises ValueError: 名称、说明或正则不合法，或命令名已经注册。
    副作用：把处理器追加到进程内只读命令目录；不接收写权限声明。
    """
    normalized_name = name.strip()
    normalized_description = description.strip()
    if not normalized_name.startswith('/') or any(char.isspace() for char in normalized_name):
        raise ValueError('开发者命令名必须以 / 开头且不能包含空白')
    if not normalized_description:
        raise ValueError(f'开发者命令 {normalized_name} 缺少说明')
    if normalized_name in _command_names:
        raise ValueError(f'开发者命令重复注册：{normalized_name}')
    compiled = re.compile(pattern)

    def decorator(handler: CommandHandler) -> CommandHandler:
        _commands.append(CommandSpec(
            name=normalized_name,
            pattern=compiled,
            description=normalized_description,
            handler=handler,
        ))
        _command_names.add(normalized_name)
        return handler

    return decorator


def registered_commands() -> Tuple[CommandCatalogItem, ...]:
    """返回不可变的命令目录快照，不暴露处理器或可变注册表。"""
    return tuple(
        CommandCatalogItem(
            name=spec.name,
            pattern=spec.pattern.pattern,
            description=spec.description,
        )
        for spec in _commands
    )


async def dispatch_developer_command(
    *,
    enabled: bool,
    text: str,
    context: ConversationContext,
    registry: OwnerRegistry,
) -> CommandDispatch | None:
    """在唯一入口完成开关、owner、匹配与执行判定。

    返回 `None` 表示必须继续走普通聊天路径。只有开关已开、消息来自 owner
    且完整命中注册规则时才消费消息。非 owner 与关闭态都走普通聊天，
    不返回任何权限提示——那等于向所有人宣告存在一套隐藏命令。

    会话面不设限：私聊与群聊都可触发（2026-09-05 由私聊限制放开）。
    代价是**回复会被整群看到**，`/stat` 会广播安装 ID 与库规模；
    这是权衡后接受的——能触发的只有 owner 一人，群本身又是白名单群。
    真要收回来只需在这里加回 `context.stream.kind != 'direct'` 一个条件。

    :param enabled: `[developer].enabled` 的当前运行值。
    :param text: 平台入站原始正文。
    :param context: `resolve_inbound` 已确定的会话与人物引用。
    :param registry: 提供系统唯一 owner 的归属注册表。
    :return: 命中时返回待投递文本，否则返回 `None`。
    副作用：处理器失败时记录结构化错误；不写消息、记忆、召回或管线事件。
    """
    if not enabled:
        return None
    owner = registry.owner_person()
    if context.person.id != owner.id:
        return None

    normalized_text = text.strip()
    for spec in _commands:
        matched = spec.pattern.fullmatch(normalized_text)
        if matched is None:
            continue
        command_context = CommandContext(
            raw_text=text,
            match=matched,
            conversation=context,
        )
        try:
            result = spec.handler(command_context)
            rendered = await result if isawaitable(result) else result
            if not isinstance(rendered, str) or not rendered.strip():
                raise ValueError('命令处理器必须返回非空字符串')
        except Exception:
            logger.exception(
                'developer_command_failed',
                command=spec.name,
                stream_id=context.stream.id,
            )
            return CommandDispatch(
                command=spec.name,
                text=f'开发者命令 {spec.name} 执行失败，请查看主体日志。',
                succeeded=False,
            )
        return CommandDispatch(command=spec.name, text=rendered.strip(), succeeded=True)
    return None


@register_command(
    name='/help',
    pattern=r'/help',
    description='列出当前已注册的开发者命令',
)
def _help_command(context: CommandContext) -> str:
    """列出命令目录；目录增长后无需修改本处理器。"""
    del context
    lines = ['开发者命令：']
    lines.extend(f'{item.name} — {item.description}' for item in registered_commands())
    return '\n'.join(lines)
