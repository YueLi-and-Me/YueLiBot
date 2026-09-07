"""验证插件组件模型的新增部分：入站改写、入站观察、命令与宿主入口注入。

锁死的边界语义：改写发生在落库之前且落库的就是改写后的正文（查库断言）；
改写顺序 ``(order, 插件 id)`` 确定且串行接力；单个组件失败只影响自身；
观察拿到的 ``message_id`` 与落库行一致；命令组件继承命令通道的 owner 与
``[developer] enabled`` 两道门控；已关闭插件的命令不进目录；组件重名当场报错。

依赖 ``src.plugin_system``、``src.core.commands`` 与聊天服务的两个入站调用点。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List

import json
import shutil

import pytest
from structlog.testing import capture_logs

from src.core.commands import registered_commands
from src.core.commands.registry import dispatch_developer_command
from src.core.config.schema import Config
from src.core.platform_io.types import (
    ConversationContext,
    InboundMessage,
    PersonRef,
    StreamRef,
)
from src.core.services.chat import ChatService
from src.plugin_system import SUPPORTED_MANIFEST_VERSION, PluginRegistry


# ------------------------------------------------------------ 插件目录素材

def _write_plugin(
    root: Path,
    dirname: str,
    plugin_id: str,
    source: str,
    *,
    enabled: bool = True,
) -> Path:
    """在临时根目录里造一个已启用（或已关闭）的插件目录。"""
    directory = root / dirname
    directory.mkdir(parents=True)
    (directory / '_manifest.json').write_text(
        json.dumps({
            'manifest_version': SUPPORTED_MANIFEST_VERSION,
            'id': plugin_id,
            'plugin_type': 'tool',
            'name': dirname,
            'version': '0.0.1',
            'description': '测试用工具插件',
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    (directory / 'plugin.py').write_text(source, encoding='utf-8')
    (directory / 'config.toml').write_text(
        f'[plugin]\nenabled = {str(enabled).lower()}\n',
        encoding='utf-8',
    )
    return directory


def _rewriter_source(order: int, marker: str) -> str:
    """生成一个把标记追加到正文末尾的改写器插件。"""
    return f'''
from src.plugin_system import ToolPlugin, inbound_rewrite


class RewriterPlugin(ToolPlugin):
    """把固定标记追加到正文末尾。"""

    def __init__(self, manifest):
        super().__init__(manifest)
        self.seen_texts = []

    @inbound_rewrite(order={order})
    async def rewrite(self, inbound):
        self.seen_texts.append(inbound.text)
        return inbound.text + {marker!r}
'''


def _write_rewriter(
    root: Path,
    dirname: str,
    plugin_id: str,
    order: int,
    marker: str,
    *,
    enabled: bool = True,
) -> Path:
    """写一个改写器插件目录。"""
    return _write_plugin(
        root, dirname, plugin_id, _rewriter_source(order, marker),
        enabled=enabled,
    )


_OBSERVER_SOURCE = '''
from src.plugin_system import ToolPlugin, inbound_observe


class ObserverPlugin(ToolPlugin):
    """记录观察到的编号与正文。"""

    def __init__(self, manifest):
        super().__init__(manifest)
        self.seen = []

    @inbound_observe()
    def observe(self, stream_id, message_id, inbound):
        self.seen.append((stream_id, message_id, inbound.text))
'''


_COMMAND_SOURCE = '''
from src.plugin_system import ToolPlugin, command


class CommandPlugin(ToolPlugin):
    """贡献一条开发者命令。"""

    def __init__(self, manifest):
        super().__init__(manifest)
        self.calls = 0

    @command(name='{name}', pattern=r'{name}', description='{description}')
    async def probe(self, ctx):
        self.calls += 1
        return '{reply}'
'''


def _command_source(name: str, description: str, reply: str) -> str:
    """生成一个贡献单条命令的插件。"""
    return _COMMAND_SOURCE.format(name=name, description=description, reply=reply)


def _inbound(text: str, context: ConversationContext) -> InboundMessage:
    """构造一条最小入站消息。"""
    return InboundMessage(text=text, context=context)


def _conversation(person_id: int) -> ConversationContext:
    """构造一个私聊归属上下文。"""
    return ConversationContext(
        stream=StreamRef(
            id=5,
            platform='qq',
            kind='direct',
            external_id='direct-5',
        ),
        person=PersonRef(id=person_id, kind='contact', first_seen_at=0),
    )


class _OwnerRegistry:
    """命令通道鉴权所需的最小注册表替身：owner 恒为 1 号人物。"""

    def owner_person(self) -> PersonRef:
        """返回系统唯一 owner。"""
        return PersonRef(id=1, kind='contact', first_seen_at=0)


# ------------------------------------------------------------ 装饰器契约

def test_inbound_rewrite_rejects_sync_handler_at_declaration() -> None:
    """改写体必须 async：分发侧会 await，同步实现的返回值会被当成正文。"""
    from src.plugin_system import ToolPlugin, inbound_rewrite

    with pytest.raises(ValueError, match='必须是 async 方法'):

        class _Sync(ToolPlugin):
            @inbound_rewrite()
            def rewrite(self, inbound):  # type: ignore[no-untyped-def]
                """占位。"""


def test_inbound_observe_rejects_async_handler_at_declaration() -> None:
    """观察体必须同步：入站主链路上无人 await 它，协程对象会静默丢失。"""
    from src.plugin_system import ToolPlugin, inbound_observe

    with pytest.raises(ValueError, match='必须是同步方法'):

        class _Async(ToolPlugin):
            @inbound_observe()
            async def observe(self, stream_id, message_id, inbound):  # type: ignore[no-untyped-def]
                """占位。"""


# ------------------------------------------------------------ 改写分发语义

def _registry_with(root: Path) -> PluginRegistry:
    """发现指定根目录并返回注册表。"""
    registry = PluginRegistry()
    registry.discover([root])
    return registry


def _plugin_by_id(registry: PluginRegistry, plugin_id: str) -> Any:
    """按 id 取出插件实例。"""
    return next(
        plugin for plugin in registry.tool_plugins()
        if plugin.manifest.plugin_id == plugin_id
    )


async def test_rewrite_passes_seed_and_chains_results(tmp_path: Path) -> None:
    """改写器按 order 升序接力：后一个看到的是前一个改写后的正文。"""
    root = tmp_path / 'plugins'
    _write_rewriter(root, 'zulu', 'test.zulu', order=1, marker='+Z')
    _write_rewriter(root, 'alpha', 'test.alpha', order=0, marker='+A')
    registry = _registry_with(root)

    final = await registry.rewrite_inbound(_inbound('原文', _conversation(1)), '原文')

    assert final == '原文+A+Z', 'order=0 必须先于 order=1 执行'
    assert _plugin_by_id(registry, 'test.alpha').seen_texts == ['原文']
    assert _plugin_by_id(registry, 'test.zulu').seen_texts == ['原文+A']


async def test_rewrite_same_order_sorts_by_plugin_id(tmp_path: Path) -> None:
    """同 order 时按插件 id 排序，与目录扫描顺序无关。"""
    root = tmp_path / 'plugins'
    _write_rewriter(root, 'zulu', 'test.zulu', order=0, marker='+Z')
    _write_rewriter(root, 'alpha', 'test.alpha', order=0, marker='+A')
    registry = _registry_with(root)

    final = await registry.rewrite_inbound(_inbound('原文', _conversation(1)), '原文')

    assert final == '原文+A+Z', '同 order 时 id 字母序在前者先执行'


async def test_rewrite_none_keeps_text(tmp_path: Path) -> None:
    """返回 None 表示不改：正文原样穿过。"""
    root = tmp_path / 'plugins'
    directory = _write_plugin(
        root, 'passthrough', 'test.passthrough', '''
from src.plugin_system import ToolPlugin, inbound_rewrite


class PassthroughPlugin(ToolPlugin):
    """声明了改写器但不改任何东西。"""

    @inbound_rewrite()
    async def rewrite(self, inbound):
        return None
''',
    )
    assert directory is not None
    registry = _registry_with(root)

    final = await registry.rewrite_inbound(_inbound('原文', _conversation(1)), '原文')

    assert final == '原文'


async def test_rewrite_blank_result_is_rejected_with_error(
    tmp_path: Path,
) -> None:
    """★3 改写器返回空串或纯空白时保留上一步正文并记 error。"""
    root = tmp_path / 'plugins'
    _write_plugin(
        root, 'blanker', 'test.blanker', '''
from src.plugin_system import ToolPlugin, inbound_rewrite


class BlankerPlugin(ToolPlugin):
    """把正文改成纯空白。"""

    @inbound_rewrite()
    async def rewrite(self, inbound):
        return '   '
''',
    )
    _write_rewriter(root, 'appender', 'test.appender', order=1, marker='+尾部')
    registry = _registry_with(root)

    with capture_logs() as logs:
        final = await registry.rewrite_inbound(
            _inbound('原文', _conversation(1)), '原文',
        )

    assert final == '原文+尾部', '空白改写必须被丢弃，后续改写器照常接力'
    assert any(
        entry.get('log_level') == 'error'
        and entry.get('plugin') == 'test.blanker'
        and '空白' in str(entry.get('event', ''))
        for entry in logs
    ), '丢弃必须记 error，否则「正文怎么没被改」无从排障'


async def test_rewrite_non_string_result_is_rejected_with_error(
    tmp_path: Path,
) -> None:
    """返回非字符串结果按插件缺陷处理：记 error、保留上一步正文。"""
    root = tmp_path / 'plugins'
    _write_plugin(
        root, 'typer', 'test.typer', '''
from src.plugin_system import ToolPlugin, inbound_rewrite


class TyperPlugin(ToolPlugin):
    """返回非字符串结果。"""

    @inbound_rewrite()
    async def rewrite(self, inbound):
        return 123
''',
    )
    registry = _registry_with(root)

    with capture_logs() as logs:
        final = await registry.rewrite_inbound(
            _inbound('原文', _conversation(1)), '原文',
        )

    assert final == '原文'
    assert any(
        entry.get('log_level') == 'error'
        and entry.get('plugin') == 'test.typer'
        for entry in logs
    )


async def test_rewrite_failure_isolates_component(tmp_path: Path) -> None:
    """改写器抛异常时记 error、保留上一步正文、继续下一个。"""
    root = tmp_path / 'plugins'
    _write_plugin(
        root, 'raiser', 'test.raiser', '''
from src.plugin_system import ToolPlugin, inbound_rewrite


class RaiserPlugin(ToolPlugin):
    """改写时恒定抛异常。"""

    @inbound_rewrite()
    async def rewrite(self, inbound):
        raise RuntimeError('改写器内部错误')
''',
    )
    _write_rewriter(root, 'appender', 'test.appender', order=1, marker='+尾部')
    registry = _registry_with(root)

    with capture_logs() as logs:
        final = await registry.rewrite_inbound(
            _inbound('原文', _conversation(1)), '原文',
        )

    assert final == '原文+尾部', '坏组件之前的正文被保留，后续组件照常执行'
    assert any(
        entry.get('log_level') == 'error'
        and entry.get('plugin') == 'test.raiser'
        and '改写器内部错误' in str(entry.get('error', ''))
        for entry in logs
    )


async def test_observe_failure_isolates_per_component(tmp_path: Path) -> None:
    """同一插件内一个观察器抛异常，同插件的另一个观察器仍被调用到。"""
    root = tmp_path / 'plugins'
    _write_plugin(
        root, 'twin', 'test.twin', '''
from src.plugin_system import ToolPlugin, inbound_observe


class TwinObserverPlugin(ToolPlugin):
    """先抛异常的观察器与正常观察器各一个。"""

    def __init__(self, manifest):
        super().__init__(manifest)
        self.seen = []

    @inbound_observe()
    def broken(self, stream_id, message_id, inbound):
        raise RuntimeError('观察器内部错误')

    @inbound_observe()
    def healthy(self, stream_id, message_id, inbound):
        self.seen.append((stream_id, message_id))
''',
    )
    registry = _registry_with(root)

    with capture_logs() as logs:
        registry.observe_inbound(3, 9, object())

    assert _plugin_by_id(registry, 'test.twin').seen == [(3, 9)]
    assert any(
        entry.get('log_level') == 'error'
        and entry.get('plugin') == 'test.twin'
        for entry in logs
    )


# ------------------------------------------------------------ 聊天服务接线

def _agent_config() -> Config:
    """启用对话代理的最小配置；本模块不驱动回合，provider 可缺省。"""
    config = Config()
    config.bot.name = '月璃'
    config.conversation_agent.mode = 'enabled'
    return config


async def _noop(_channel: str, _payload: Any, _stream_id: int) -> None:
    """占位事件回调。"""
    return None


def _chat_with_plugins(db: Any, monkeypatch: pytest.MonkeyPatch, root: Path) -> ChatService:
    """把插件根目录指到临时目录后构造聊天服务。"""
    import src.core.services.chat.service as service_module

    monkeypatch.setattr(service_module, 'PLUGIN_ROOTS', (root,))
    return ChatService(db, None, None, None, _noop, cfg=_agent_config())


def _stored_user_texts(db: Any, stream_id: int) -> List[str]:
    """读出该会话全部已落库用户消息正文。"""
    rows = db.execute(
        "SELECT content FROM messages WHERE stream_id = ? AND role = 'user' ORDER BY id",
        (stream_id,),
    ).fetchall()
    return [str(row[0]) for row in rows]


async def test_send_stores_rewritten_text(
    db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★2 send 路径：落库的是改写后的正文（查库断言，不是返回值）。"""
    root = tmp_path / 'plugins'
    _write_rewriter(root, 'rewriter', 'test.rewriter', order=0, marker='（已改写）')
    chat = _chat_with_plugins(db, monkeypatch, root)
    context = chat._registry.resolve_inbound(
        platform='qq',
        stream_kind='direct',
        stream_external_id='97531',
        sender_external_id='97531',
        sender_nickname='账号昵称',
        sender_group_card='',
        first_seen_at=1_000_000,
    )

    await chat.send(_inbound('原始正文', context))

    stream_id = context.stream.id
    assert _stored_user_texts(db, stream_id) == ['原始正文（已改写）']


async def test_record_silent_inbound_stores_rewritten_text(
    db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★2 静默观察路径：门控拒绝的消息同样落改写后的正文。"""
    root = tmp_path / 'plugins'
    _write_rewriter(root, 'rewriter', 'test.rewriter', order=0, marker='（已改写）')
    chat = _chat_with_plugins(db, monkeypatch, root)
    context = chat._registry.resolve_inbound(
        platform='qq',
        stream_kind='direct',
        stream_external_id='97531',
        sender_external_id='97531',
        sender_nickname='账号昵称',
        sender_group_card='',
        first_seen_at=1_000_000,
    )

    message_id = await chat.record_silent_inbound(
        _inbound('原始正文', context), 'attention_filtered',
    )

    assert message_id > 0
    assert _stored_user_texts(db, context.stream.id) == ['原始正文（已改写）']


async def test_blank_rewrite_still_lands_original_text(
    db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★3 改写为空白时：消息照常落库，库存的是改写前的正文。"""
    root = tmp_path / 'plugins'
    _write_plugin(
        root, 'blanker', 'test.blanker', '''
from src.plugin_system import ToolPlugin, inbound_rewrite


class BlankerPlugin(ToolPlugin):
    """把正文改成空串。"""

    @inbound_rewrite()
    async def rewrite(self, inbound):
        return ''
''',
    )
    chat = _chat_with_plugins(db, monkeypatch, root)
    context = chat._registry.resolve_inbound(
        platform='qq',
        stream_kind='direct',
        stream_external_id='97531',
        sender_external_id='97531',
        sender_nickname='账号昵称',
        sender_group_card='',
        first_seen_at=1_000_000,
    )

    with capture_logs() as logs:
        await chat.send(_inbound('绝不能丢的正文', context))

    assert _stored_user_texts(db, context.stream.id) == ['绝不能丢的正文']
    assert any(
        entry.get('log_level') == 'error' and entry.get('plugin') == 'test.blanker'
        for entry in logs
    )


async def test_observer_message_id_matches_stored_row(
    db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★7 观察组件拿到的 message_id 与落库行一致。"""
    root = tmp_path / 'plugins'
    _write_plugin(root, 'observer', 'test.observer', _OBSERVER_SOURCE)
    chat = _chat_with_plugins(db, monkeypatch, root)
    context = chat._registry.resolve_inbound(
        platform='qq',
        stream_kind='direct',
        stream_external_id='97531',
        sender_external_id='97531',
        sender_nickname='账号昵称',
        sender_group_card='',
        first_seen_at=1_000_000,
    )

    await chat.send(_inbound('要被观察的正文', context))

    stream_id = context.stream.id
    row = db.execute(
        "SELECT id, content FROM messages WHERE stream_id = ? AND role = 'user'",
        (stream_id,),
    ).fetchone()
    observer = _plugin_by_id(chat._plugins, 'test.observer')
    assert observer.seen == [(stream_id, row[0], '要被观察的正文')], (
        '观察必须在落库之后，且编号指向同一行'
    )


async def test_frame_carries_plugin_capabilities_union(
    db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★4 能力泛化后，回合帧携带插件能力并集，不再挑名字。"""
    root = tmp_path / 'plugins'
    _write_plugin(
        root, 'capable', 'test.capable', '''
from src.plugin_system import ToolPlugin


class CapablePlugin(ToolPlugin):
    """贡献一个自定义能力名。"""

    def stream_capabilities(self, stream_id):
        return frozenset({'forward_message', 'custom_probe'})
''',
    )
    chat = _chat_with_plugins(db, monkeypatch, root)
    context = chat._registry.resolve_inbound(
        platform='qq',
        stream_kind='direct',
        stream_external_id='97531',
        sender_external_id='97531',
        sender_nickname='账号昵称',
        sender_group_card='',
        first_seen_at=1_000_000,
    )
    await chat.send(_inbound('要进回合的正文', context))
    batch = chat._buffers[context.stream.id]

    frame = chat._agent_frame(context, batch, turn=1, disposition='force')

    assert frame.capabilities.plugin_capabilities == frozenset(
        {'forward_message', 'custom_probe'}
    ), '泛化后的能力通道必须原样透传插件并集，自定义名不再被滤掉'
    assert frame.capabilities.tool_capabilities() == frozenset(
        {'forward_message', 'custom_probe'}
    )


# ------------------------------------------------------------ 命令组件

def _fresh_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """把进程内命令目录换成本用例私有的空目录，隔离全局注册状态。"""
    import src.core.commands.registry as registry_module

    monkeypatch.setattr(registry_module, '_commands', [])
    monkeypatch.setattr(registry_module, '_command_names', set())


async def test_command_component_is_dispatched_for_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★8 插件命令能被 dispatch_developer_command 命中。"""
    _fresh_catalog(monkeypatch)
    root = tmp_path / 'plugins'
    _write_plugin(
        root, 'commander', 'test.commander',
        _command_source('/probe8', '探针命令', '探针回复'),
    )
    registry = _registry_with(root)
    registry.register_commands()

    dispatch = await dispatch_developer_command(
        enabled=True,
        text='/probe8',
        context=_conversation(person_id=1),
        registry=_OwnerRegistry(),
    )

    assert dispatch is not None
    assert dispatch.command == '/probe8'
    assert dispatch.text == '探针回复'
    assert dispatch.succeeded is True


async def test_command_component_ignores_non_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★8 非 owner 不触发：按普通聊天继续，也不泄露命令存在。"""
    _fresh_catalog(monkeypatch)
    root = tmp_path / 'plugins'
    _write_plugin(
        root, 'commander', 'test.commander',
        _command_source('/probe8b', '探针命令', '探针回复'),
    )
    registry = _registry_with(root)
    registry.register_commands()

    dispatch = await dispatch_developer_command(
        enabled=True,
        text='/probe8b',
        context=_conversation(person_id=2),
        registry=_OwnerRegistry(),
    )

    assert dispatch is None
    assert _plugin_by_id(registry, 'test.commander').calls == 0


async def test_command_component_respects_developer_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★8 [developer] enabled = false 时 owner 也不触发。"""
    _fresh_catalog(monkeypatch)
    root = tmp_path / 'plugins'
    _write_plugin(
        root, 'commander', 'test.commander',
        _command_source('/probe8c', '探针命令', '探针回复'),
    )
    registry = _registry_with(root)
    registry.register_commands()

    dispatch = await dispatch_developer_command(
        enabled=False,
        text='/probe8c',
        context=_conversation(person_id=1),
        registry=_OwnerRegistry(),
    )

    assert dispatch is None


def test_disabled_plugin_command_stays_out_of_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★10 插件关闭时其命令不出现在命令目录里。"""
    _fresh_catalog(monkeypatch)
    root = tmp_path / 'plugins'
    _write_plugin(
        root, 'commander', 'test.commander',
        _command_source('/probe10', '探针命令', '探针回复'),
        enabled=False,
    )
    registry = _registry_with(root)

    registry.register_commands()

    assert [item.name for item in registered_commands()] == []


def test_enabled_plugin_command_enters_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """插件开启时其命令进入目录，说明关闭态的空目录不是装配假象。"""
    _fresh_catalog(monkeypatch)
    root = tmp_path / 'plugins'
    _write_plugin(
        root, 'commander', 'test.commander',
        _command_source('/probe10b', '探针命令', '探针回复'),
    )
    registry = _registry_with(root)

    registry.register_commands()

    assert [item.name for item in registered_commands()] == ['/probe10b']


def test_hello_yueli_command_declared_and_gated_by_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★10 示例插件声明 /hello；目录里是否出现完全由插件开关决定。"""
    _fresh_catalog(monkeypatch)
    example = Path(__file__).resolve().parents[2] / 'src' / 'plugins' / 'built_in' / 'hello-yueli'
    off_root = tmp_path / 'off' / 'plugins'
    on_root = tmp_path / 'on' / 'plugins'
    shutil.copytree(
        example, off_root / 'hello-yueli',
        ignore=shutil.ignore_patterns('__pycache__', 'config.toml'),
    )
    shutil.copytree(
        example, on_root / 'hello-yueli',
        ignore=shutil.ignore_patterns('__pycache__', 'config.toml'),
    )
    (off_root / 'hello-yueli' / 'config.toml').write_text(
        '[plugin]\nenabled = false\n', encoding='utf-8',
    )
    (on_root / 'hello-yueli' / 'config.toml').write_text(
        '[plugin]\nenabled = true\n', encoding='utf-8',
    )

    off_registry = _registry_with(off_root)
    off_registry.register_commands()
    assert '/hello' not in [item.name for item in registered_commands()]

    on_registry = _registry_with(on_root)
    on_registry.register_commands()
    assert '/hello' in [item.name for item in registered_commands()]


# ------------------------------------------------------------ 重名与入口注入

def test_duplicate_command_name_within_plugin_is_rejected(tmp_path: Path) -> None:
    """★9 同一插件内两条同名命令在收集期报错。"""
    root = tmp_path / 'plugins'
    directory = _write_plugin(
        root, 'duplicator', 'test.duplicator', '''
from src.plugin_system import ToolPlugin, command


class DuplicatorPlugin(ToolPlugin):
    """同一命令名声明两次。"""

    @command(name='/dup', pattern=r'/dup', description='一号')
    async def first(self, ctx):
        return '一'

    @command(name='/dup', pattern=r'/dup', description='二号')
    async def second(self, ctx):
        return '二'
''',
    )
    assert directory is not None

    from src.plugin_system.loader import load_manifest, load_tool_plugin

    plugin = load_tool_plugin(directory, load_manifest(directory / '_manifest.json'))
    with pytest.raises(ValueError, match='重复声明了命令 /dup'):
        plugin.commands()


def test_cross_plugin_command_conflict_fails_registration(tmp_path: Path) -> None:
    """★9 跨插件同名命令在注册期报错，静默丢命令无从排障。"""
    root = tmp_path / 'plugins'
    _write_plugin(
        root, 'first', 'test.first',
        _command_source('/dup9', '一号', '一'),
    )
    _write_plugin(
        root, 'second', 'test.second',
        _command_source('/dup9', '二号', '二'),
    )
    registry = _registry_with(root)

    with pytest.raises(ValueError, match='重复注册'):
        registry.register_commands()


class _StubContext:
    """宿主入口桩件：只记录自己被哪个插件拿过。"""

    def __init__(self, plugin_id: str) -> None:
        """保存绑定 id。"""
        self.plugin_id = plugin_id


async def test_context_factory_binds_before_load(tmp_path: Path) -> None:
    """ctx 在 bind_config 之后、on_load 之前注入，on_load 里可访问。"""
    root = tmp_path / 'plugins'
    _write_plugin(
        root, 'contextual', 'test.contextual', '''
from src.plugin_system import ToolPlugin


class ContextualPlugin(ToolPlugin):
    """在 on_load 里读取宿主入口。"""

    def __init__(self, manifest):
        super().__init__(manifest)
        self.bound_id = None

    async def on_load(self):
        self.bound_id = self.ctx.plugin_id
''',
    )
    stubs: List[_StubContext] = []

    def factory(plugin_id: str, plugin_dir: Path) -> _StubContext:
        """构造桩件并登记；工厂签名带插件目录，与生产工厂一致。"""
        stub = _StubContext(plugin_id)
        stub.plugin_dir = plugin_dir
        stubs.append(stub)
        return stub

    registry = PluginRegistry(context_factory=factory)
    registry.discover([root])

    await registry.load_all()

    assert [stub.plugin_id for stub in stubs] == ['test.contextual']
    assert _plugin_by_id(registry, 'test.contextual').bound_id == 'test.contextual'


async def test_ctx_access_before_binding_raises(tmp_path: Path) -> None:
    """未注入就访问 ctx 当场报错，而不是返回空值让错误在远处炸掉。"""
    root = tmp_path / 'plugins'
    _write_plugin(
        root, 'eager', 'test.eager', '''
from src.plugin_system import ToolPlugin


class EagerPlugin(ToolPlugin):
    """在 on_load 里读取宿主入口。"""

    def __init__(self, manifest):
        super().__init__(manifest)
        self.error = None

    async def on_load(self):
        try:
            _ = self.ctx
        except RuntimeError as exc:
            self.error = str(exc)
''',
    )
    registry = _registry_with(root)

    await registry.load_all()

    error = _plugin_by_id(registry, 'test.eager').error
    assert error is not None and '尚未注入' in error
