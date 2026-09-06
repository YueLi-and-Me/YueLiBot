"""示例插件 hello-yueli 的守卫：它必须真的能加载、能执行，且默认关着。

示例的价值全在「照抄就能跑」。插件 API 一旦改动而示例没跟上，它会变成一份把人
带偏的错误教材，而这种腐烂没有任何其他信号——没人在生产里跑它。本包因此把示例
当成真实插件来验：走真实的清单解析、配置生成与加载器，不用替身。

用例一律在 ``tmp_path`` 里操作副本，不碰仓库里的那份目录——发现流程会往插件目录
写生成的 ``config.toml``，让测试污染工作区不可接受。

依赖 ``src.plugin_system`` 与 ``src.core.tooling.spec``。
"""

from __future__ import annotations

from pathlib import Path

import shutil
import tomllib

import pytest

from src.core.agent.action_protocol import (
    DecisionFrame,
    PlatformCapabilities,
    available_actions,
)
from src.core.tooling.spec import ToolContext, ToolInvocation
from src.plugin_system import PluginRegistry, load_manifest
from src.plugin_system.config import plugin_config_path
from src.plugin_system.loader import load_tool_plugin

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_DIR = PROJECT_ROOT / 'src' / 'plugins' / 'built_in' / 'hello-yueli'
EXAMPLE_ID = 'example.hello-yueli'


def _copy_example(tmp_path: Path) -> Path:
    """把示例目录复制到临时位置，连同可能已生成的配置一并剔除。

    :param tmp_path: pytest 提供的临时目录。
    :return: 副本所在的插件根目录（``<tmp>/plugins``）。
    """
    root = tmp_path / 'plugins'
    destination = root / 'hello-yueli'
    shutil.copytree(
        EXAMPLE_DIR,
        destination,
        ignore=shutil.ignore_patterns('__pycache__', 'config.toml'),
    )
    return root


def _plugin():
    """走真实加载器构造示例插件实例（不触发配置生成）。"""
    manifest = load_manifest(EXAMPLE_DIR / '_manifest.json')
    return load_tool_plugin(EXAMPLE_DIR, manifest)


def _context(stream_kind: str = 'direct') -> ToolContext:
    """造一个最小可用的工具上下文。"""
    capabilities = PlatformCapabilities()
    frame = DecisionFrame(
        turn_id=1,
        snapshot_id='snapshot-1',
        stream_kind=stream_kind,
        disposition='deliberate',
        selectable_message_ids=(101,),
        message_watermark=101,
        available_actions=available_actions(stream_kind, 'deliberate', capabilities),
        capabilities=capabilities,
    )
    return ToolContext(
        stream_id=7,
        stream_kind=stream_kind,
        frame=frame,
        turn_id=1,
        snapshot_id='snapshot-1',
    )


def test_示例插件能被真实加载器加载() -> None:
    """清单与入口都合法，且模块内只有一个 ToolPlugin 子类。"""
    plugin = _plugin()

    assert plugin.manifest.plugin_id == EXAMPLE_ID
    assert plugin.manifest.plugin_type == 'tool'


def test_示例插件只声明一个工具() -> None:
    """示例的用途是看清骨架，多一个工具就多一份噪声。"""
    plugin = _plugin()

    tools = plugin.tools()

    assert [spec.name for spec, _executor in tools] == ['hello_yueli']
    spec = tools[0][0]
    assert spec.kind == 'external'
    assert spec.side_effect == 'readonly'


async def test_工具执行返回可回灌的观察() -> None:
    """成功路径：observation 非空，且按会话类型区分措辞。"""
    plugin = _plugin()
    await plugin.on_load()
    _spec, executor = plugin.tools()[0]

    result = await executor.execute(
        ToolInvocation(tool_name='hello_yueli', arguments={'name': '月璃'}),
        _context('group'),
    )

    assert result.success is True
    assert result.observation.startswith('月璃好')
    assert '群聊' in result.observation
    assert result.metadata['greeted'] == 1


@pytest.mark.parametrize(('arguments', 'expected'), [
    ({'name': 123}, 'name 必须是字符串'),
    ({'name': '啊' * 33}, 'name 最长 32 字'),
])
async def test_非法参数返回显式失败(arguments: dict, expected: str) -> None:
    """失败必须带原因——ToolExecutionResult 自己会拒绝没有原因的失败。"""
    plugin = _plugin()
    _spec, executor = plugin.tools()[0]

    result = await executor.execute(
        ToolInvocation(tool_name='hello_yueli', arguments=arguments),
        _context(),
    )

    assert result.success is False
    assert expected in result.error_message


async def test_生命周期钩子幂等() -> None:
    """on_unload 在停机与重载两条路径上都会被调用，重复调用不得抛异常。"""
    plugin = _plugin()

    await plugin.on_load()
    await plugin.on_unload()
    await plugin.on_unload()


def test_首次发现会生成一份带注释的配置(tmp_path: Path) -> None:
    """目录里不该有手写的 TOML：声明只有 config_model 一份，文件由宿主生成。"""
    root = _copy_example(tmp_path)
    destination = root / 'hello-yueli'

    PluginRegistry().discover([root])

    text = plugin_config_path(destination).read_text(encoding='utf-8')
    document = tomllib.loads(text)
    assert document['plugin']['enabled'] is False
    assert 'greeting_suffix' in document['plugin']
    # 字段的 description 必须落到注释里，那是用户唯一能看到的解释。
    assert '# 是否启用示例插件' in text


def test_示例默认关闭且确实不被加载(tmp_path: Path) -> None:
    """全新安装不该被一份教学素材占用模型的工具声明预算。"""
    root = _copy_example(tmp_path)
    registry = PluginRegistry()

    registry.discover([root])

    assert EXAMPLE_ID not in [
        plugin.manifest.plugin_id for plugin in registry.tool_plugins()
    ]


def test_打开开关后示例能被加载并读到配置(tmp_path: Path) -> None:
    """把示例复制出去、开关改成 true，它必须真的能被发现、加载并读到自己的配置。

    这一条守的是「照抄就能跑」——示例的全部价值所在。
    """
    root = _copy_example(tmp_path)
    destination = root / 'hello-yueli'
    plugin_config_path(destination).write_text(
        '[plugin]\nenabled = true\ngreeting_suffix = "早上好"\n',
        encoding='utf-8',
    )
    registry = PluginRegistry()

    registry.discover([root])

    loaded = [
        plugin for plugin in registry.tool_plugins()
        if plugin.manifest.plugin_id == EXAMPLE_ID
    ]
    assert loaded, '开关为 true 时示例必须被加载'
    assert loaded[0].config.greeting_suffix == '早上好'


async def test_配置里的问候词进入了观察正文(tmp_path: Path) -> None:
    """配置不是摆设：改了它，模型看到的观察就该跟着变。"""
    root = _copy_example(tmp_path)
    destination = root / 'hello-yueli'
    plugin_config_path(destination).write_text(
        '[plugin]\nenabled = true\ngreeting_suffix = "晚安"\n',
        encoding='utf-8',
    )
    registry = PluginRegistry()
    registry.discover([root])
    plugin = next(
        item for item in registry.tool_plugins()
        if item.manifest.plugin_id == EXAMPLE_ID
    )
    await plugin.on_load()
    _spec, executor = plugin.tools()[0]

    result = await executor.execute(
        ToolInvocation(tool_name='hello_yueli', arguments={'name': '月璃'}),
        _context(),
    )

    assert result.observation.startswith('月璃晚安')


def test_配置模型的默认值就是关闭() -> None:
    """默认关闭写在代码里，不依赖磁盘上那份生成物。"""
    plugin = _plugin()
    defaults = type(plugin).config_model()

    assert defaults.enabled is False
