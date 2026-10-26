"""模型用量按插件归属：插件的五类执行入口里发起的调用记在插件名下。

两个测试插件（一个内置、一个第三方）在生命周期钩子、入站改写、入站观察、命令、
工具执行体里各发起一次模型调用，工具执行体与入站观察另经 ``create_task`` 派生后台
任务再调用一次。断言账本里每次调用的 ``plugin_id`` 与 ``plugin_third_party``；主程序
自己的调用归主程序；执行体结束后归属已还原。另钉住组件类型清单：新增组件类型而不
经注册表包装时用例变红。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List, Set, Tuple

import asyncio
import json
import sys

import pytest

import src.plugin_system.components as components_module
from src.core.app_meta import APP_VERSION
from src.core.commands.registry import dispatch_developer_command
from src.core.config.schema import ModelCandidate
from src.core.llm_models.router import ModelRouter
from src.core.llm_models.usage import TokenUsage
from src.core.platform_io.types import ConversationContext, InboundMessage, PersonRef, StreamRef
from src.core.tooling.spec import ToolInvocation
from src.plugin_system import SUPPORTED_MANIFEST_VERSION, PluginRegistry
from src.plugin_system.attribution import current_plugin_origin

# 每个插件在各执行入口里调用的路由任务名；任务名就是入口名，便于按行断言。
ENTRY_KINDS = ('on_load', 'rewrite', 'observe', 'command', 'tool', 'tool_background', 'on_unload')

PLUGIN_SOURCE = '''
import usage_probe

from src.plugin_system import ToolPlugin, command, inbound_observe, inbound_rewrite, tool


class ProbePlugin(ToolPlugin):
    """在每类执行入口里各发起一次模型调用。"""

    async def on_load(self):
        await usage_probe.call('on_load')

    async def on_unload(self):
        await usage_probe.call('on_unload')

    @inbound_rewrite()
    async def rewrite(self, inbound):
        await usage_probe.call('rewrite')
        return None

    @inbound_observe()
    def observe(self, stream_id, message_id, inbound):
        # 观察器是同步的，只能派生任务；派生任务继承创建时的归属。
        usage_probe.spawn('observe')

    @command(name='/{slug}', pattern='/{slug}', description='用量归属探针')
    async def probe_command(self, context):
        await usage_probe.call('command')
        return '好'

    @tool(name='{slug}_tool', description='用量归属探针')
    async def probe_tool(self, invocation, context):
        await usage_probe.call('tool')
        usage_probe.spawn('tool_background')
        return 'ok'
'''


class _UsageClient:
    """只吐一个字并在流末尾交出固定用量的假 provider。"""

    async def stream(self, messages, temperature=0.85, max_tokens=None, signal=None, **_options: Any) -> AsyncIterator[dict]:
        yield {'text': '好'}
        yield {'usage': TokenUsage(prompt_tokens=10, completion_tokens=2, total_tokens=12)}


def _router(task: str) -> ModelRouter:
    router = ModelRouter(task, [ModelCandidate(name='探针模型', provider='探针厂商', identifier='probe')])
    client = _UsageClient()
    router.client = lambda candidate: client  # type: ignore[method-assign]
    return router


class _Probe:
    """插件代码经 ``import usage_probe`` 拿到的探针：发起调用并收集派生任务。"""

    def __init__(self) -> None:
        self.routers: Dict[str, ModelRouter] = {}
        self.tasks: List[asyncio.Task[None]] = []

    async def call(self, kind: str) -> None:
        router = self.routers.setdefault(kind, _router(kind))
        async for _chunk in router.stream([{'role': 'user', 'content': kind}]):
            pass

    def spawn(self, kind: str) -> None:
        self.tasks.append(asyncio.get_running_loop().create_task(self.call(kind)))


def _write_plugin(root: Path, plugin_id: str, slug: str, *, third_party: bool) -> None:
    directory = root / slug
    directory.mkdir(parents=True)
    manifest: Dict[str, Any] = {
        'manifest_version': SUPPORTED_MANIFEST_VERSION,
        'id': plugin_id,
        'plugin_type': 'tool',
        'name': slug,
        'version': '0.0.1',
        'description': '用量归属探针插件',
    }
    if third_party:
        manifest['host_application'] = {'min_version': APP_VERSION, 'max_version': APP_VERSION}
    (directory / '_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    (directory / 'plugin.py').write_text(PLUGIN_SOURCE.replace('{slug}', slug), encoding='utf-8')
    (directory / 'config.toml').write_text('[plugin]\nenabled = true\n', encoding='utf-8')


def _conversation() -> ConversationContext:
    return ConversationContext(
        stream=StreamRef(id=5, platform='qq', kind='direct', external_id='direct-5'),
        person=PersonRef(id=1, kind='contact', first_seen_at=0),
    )


class _OwnerRegistry:
    """命令通道鉴权替身：owner 恒为 1 号人物。"""

    def owner_person(self) -> PersonRef:
        return PersonRef(id=1, kind='contact', first_seen_at=0)


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> _Probe:
    import src.core.commands.registry as command_registry

    # 命令目录是进程级的，换成本用例私有的空目录。
    monkeypatch.setattr(command_registry, '_commands', [])
    monkeypatch.setattr(command_registry, '_command_names', set())
    instance = _Probe()
    monkeypatch.setitem(sys.modules, 'usage_probe', SimpleNamespace(call=instance.call, spawn=instance.spawn))
    return instance


async def test_every_plugin_entry_point_attributes_model_calls(tmp_path: Path, probe: _Probe, usage_rows) -> None:
    builtin_root = tmp_path / 'built_in'
    third_root = tmp_path / 'third_party'
    _write_plugin(builtin_root, 'test.builtin-probe', 'builtinprobe', third_party=False)
    _write_plugin(third_root, 'test.third-probe', 'thirdprobe', third_party=True)
    registry = PluginRegistry()
    registry.discover([(builtin_root, False), (third_root, True)])
    assert sorted(plugin.manifest.plugin_id for plugin in registry.tool_plugins()) == [
        'test.builtin-probe', 'test.third-probe',
    ]

    await registry.load_all()
    inbound = InboundMessage(text='原文', context=_conversation())
    assert await registry.rewrite_inbound(inbound, '原文') == '原文'
    registry.observe_inbound(5, 1, inbound)
    registry.register_commands()
    for slug in ('builtinprobe', 'thirdprobe'):
        dispatch = await dispatch_developer_command(
            enabled=True, text=f'/{slug}', context=_conversation(), registry=_OwnerRegistry(),
        )
        assert dispatch is not None and dispatch.text == '好'
    for _spec, executor in registry.tools():
        assert await executor.execute(ToolInvocation(tool_name=_spec.name), None) == 'ok'
    await registry.unload_all()
    await asyncio.gather(*probe.tasks)
    # 主程序自己发起的调用。
    await probe.call('main')

    # 执行体结束后归属已还原，主程序上下文里读到的仍是主程序。
    assert current_plugin_origin() is None
    seen: Set[Tuple[str, str, int]] = {
        (row['task'], row['plugin_id'], row['plugin_third_party']) for row in usage_rows()
    }
    expected = {('main', '', 0)}
    for kind in ENTRY_KINDS:
        expected.add((kind, 'test.builtin-probe', 0))
        expected.add((kind, 'test.third-probe', 1))
    assert seen == expected
    assert all(row['calls'] == 1 and row['prompt_tokens'] == 10 for row in usage_rows())


async def test_origin_is_restored_after_each_entry_point(tmp_path: Path, probe: _Probe, usage_rows) -> None:
    """包装只在执行期间设置归属，执行体抛错时同样还原。"""
    from src.plugin_system.attribution import PluginOrigin, attributed_async, attributed_callable

    origin = PluginOrigin('test.restore', third_party=True)

    async def failing() -> None:
        assert current_plugin_origin() == origin
        raise RuntimeError('插件出错')

    with pytest.raises(RuntimeError):
        await attributed_async(origin, failing)()
    assert current_plugin_origin() is None

    observed: List[Any] = []
    attributed_callable(origin, lambda: observed.append(current_plugin_origin()))()
    assert observed == [origin]
    assert current_plugin_origin() is None


def test_component_kinds_are_all_wrapped_by_the_registry() -> None:
    """钉住组件类型清单：新增组件类型必须经注册表的归属包装，并在这里登记。

    注册表包装的入口：生命周期（on_load / on_unload）、入站改写、入站观察、命令、
    工具执行体。新增一类组件（包括将来开放给插件的模型调用接口）时，先在
    ``PluginRegistry`` 的收集处用 ``attribution`` 包装，再把它加进下面的清单。
    """
    declared = {
        name for name in vars(components_module)
        if name.startswith('_') and name.endswith('_ATTR')
    }
    assert declared == {
        '_TOOL_SPEC_ATTR',
        '_INBOUND_REWRITE_ATTR',
        '_INBOUND_OBSERVE_ATTR',
        '_COMMAND_ATTR',
    }
    source = Path(sys.modules[PluginRegistry.__module__].__file__ or '').read_text(encoding='utf-8')
    for wrapped in (
        'attributed_async(self.origin_of(plugin), plugin.on_load)',
        'attributed_async(self.origin_of(plugin), plugin.on_unload)',
        'attributed_async(origin, handler)',
        'attributed_callable(origin, handler)(stream_id, message_id, inbound)',
        ')(attributed_callable(origin, handler))',
        'attributed_tool(origin, executor)',
    ):
        assert wrapped in source, f'注册表缺少归属包装：{wrapped}'
