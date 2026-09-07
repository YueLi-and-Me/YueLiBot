"""验证协议端能力驱动动作集，以及适配器插件的加载约定。

本轮要消灭的故障形态：协议端发包能力失效后 `group_poke` 恒定失败，而主体只看
配置开关就把 poke 放进动作集，她反复选中一个执行不了的终局动作，对方收到彻底的
沉默。判据因此改为「配置开关 AND 协议端实测能力」，且未收到上报时按不可用处理。

依赖 ``src.core.services.chat`` 的能力登记入口与 ``src.plugin_system.loader``。
"""

from __future__ import annotations

from pathlib import Path

import asyncio
import contextlib
import json

import pytest

from src.plugin_system import SUPPORTED_MANIFEST_VERSION, PluginLoadError, load_adapter_plugin


_ADAPTERS = Path('adapters')


def _write_adapter(root: Path, name: str, entry_source: str) -> Path:
    """在临时目录里造一个适配器目录，用于加载约定的反例。"""
    directory = root / name
    directory.mkdir(parents=True)
    (directory / '_manifest.json').write_text(json.dumps({
        'manifest_version': SUPPORTED_MANIFEST_VERSION,
        'id': f'test.{name}',
        'plugin_type': 'adapter',
        'name': name,
        'version': '0.0.1',
        'description': '测试用适配器',
        'protocol': 'onebot11',
        'config_section': name,
        'capabilities': {'static': ['send_message'], 'probed': []},
    }, ensure_ascii=False), encoding='utf-8')
    (directory / 'plugin.py').write_text(entry_source, encoding='utf-8')
    return directory


_ONE_CLASS = '''
from src.plugin_system import AdapterPlugin


class OnlyPlugin(AdapterPlugin):
    async def on_load(self) -> None:
        pass

    async def probe_capabilities(self):
        return frozenset()

    async def on_start(self) -> None:
        pass

    async def on_stop(self) -> None:
        pass
'''

_TWO_CLASSES = _ONE_CLASS + '''

class SecondPlugin(AdapterPlugin):
    async def on_load(self) -> None:
        pass

    async def probe_capabilities(self):
        return frozenset()

    async def on_start(self) -> None:
        pass

    async def on_stop(self) -> None:
        pass
'''

_NO_CLASS = '''
VALUE = 1
'''


def test_both_shipped_adapters_load() -> None:
    """两个随仓库交付的适配器都必须能按加载约定实例化。"""
    for name in ('yueli-napcat-adapter', 'yueli-snowluma-adapter'):
        plugin = load_adapter_plugin(_ADAPTERS / name)
        assert plugin.manifest.config_section
        assert 'send_message' in plugin.manifest.declared_capabilities


def test_two_adapters_do_not_share_config_section() -> None:
    """两个适配器读不同配置段，否则无法区分各自的连接参数。"""
    napcat = load_adapter_plugin(_ADAPTERS / 'yueli-napcat-adapter')
    snowluma = load_adapter_plugin(_ADAPTERS / 'yueli-snowluma-adapter')

    assert napcat.manifest.config_section != snowluma.manifest.config_section
    assert napcat.manifest.plugin_id != snowluma.manifest.plugin_id


def test_poke_is_probed_on_napcat_and_static_on_snowluma() -> None:
    """NapCat 的戳一戳依赖发包组件必须探测，SnowLuma 不依赖故静态声明。"""
    napcat = load_adapter_plugin(_ADAPTERS / 'yueli-napcat-adapter')
    snowluma = load_adapter_plugin(_ADAPTERS / 'yueli-snowluma-adapter')

    assert 'poke' in napcat.manifest.probed_capabilities
    assert 'poke' not in napcat.manifest.static_capabilities
    assert 'poke' in snowluma.manifest.static_capabilities
    assert snowluma.manifest.probed_capabilities == frozenset()


def test_missing_directory_is_rejected(tmp_path: Path) -> None:
    """目录不存在时报出明确错误，而不是留到运行期才失败。"""
    with pytest.raises(PluginLoadError, match='适配器目录不存在'):
        load_adapter_plugin(tmp_path / 'nope')


def test_entry_without_plugin_class_is_rejected(tmp_path: Path) -> None:
    """入口没有实现时当场报错：零个候选说明忘了写。"""
    directory = _write_adapter(tmp_path, 'empty-adapter', _NO_CLASS)

    with pytest.raises(PluginLoadError, match='没有定义 AdapterPlugin 子类'):
        load_adapter_plugin(directory)


def test_entry_with_two_plugin_classes_is_rejected(tmp_path: Path) -> None:
    """入口有多个实现时不猜该用哪个，直接报歧义。"""
    directory = _write_adapter(tmp_path, 'ambiguous-adapter', _TWO_CLASSES)

    with pytest.raises(PluginLoadError, match='入口有歧义'):
        load_adapter_plugin(directory)


@pytest.mark.asyncio
async def test_host_does_not_exit_while_run_loop_is_alive() -> None:
    """宿主必须等到收发结束才返回。

    - 现象：``on_start`` 若起个任务就返回，宿主会以为跑完了，随即取消刚建的
      任务，进程零错误退出（``code=0``）且日志为空。
    - 原因：宿主用 ``on_start`` 的返回判断「收发结束」，那是它唯一能等的信号。
    - 后果：真机上表现为适配器每次启动立刻退出，看起来像没启动。
    """
    from src.platforms.onebot11.__main__ import _serve
    from src.plugin_system import AdapterPlugin, manifest_from_payload

    started = asyncio.Event()

    class _BlockingPlugin(AdapterPlugin):
        async def on_load(self) -> None:
            pass

        async def probe_capabilities(self):
            return frozenset()

        async def on_start(self) -> None:
            started.set()
            await asyncio.sleep(3600)

        async def on_stop(self) -> None:
            pass

    manifest = manifest_from_payload({
        'manifest_version': SUPPORTED_MANIFEST_VERSION,
        'id': 'test.blocking-adapter',
        'plugin_type': 'adapter',
        'name': 'blocking',
        'version': '0.0.1',
        'description': '测试用',
        'protocol': 'onebot11',
        'config_section': 'blocking',
        'capabilities': {'static': ['send_message'], 'probed': []},
    })

    task = asyncio.create_task(_serve(_BlockingPlugin(manifest)))
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.sleep(0)

    assert not task.done(), '收发循环还活着时宿主就返回了'

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
