"""示例插件 hello-yueli 的守卫：它必须真的能加载、能执行，且默认关着。

示例的价值全在「照抄就能跑」。插件 API 一旦改动而示例没跟上，它会变成一份把人
带偏的错误教材，而这种腐烂没有任何其他信号——没人在生产里跑它。本包因此把示例
当成真实插件来验：走真实的清单解析与加载器，不用替身。

依赖 ``src.plugin_system`` 与 ``src.core.tooling.spec``。
"""

from __future__ import annotations

from pathlib import Path

import shutil

import pytest

from src.core.agent.action_protocol import (
    DecisionFrame,
    PlatformCapabilities,
    available_actions,
)
from src.core.tooling.spec import ToolContext, ToolInvocation
from src.plugin_system import PluginRegistry, load_manifest
from src.plugin_system.loader import load_tool_plugin
from src.plugin_system.switch import plugin_enabled

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_DIR = PROJECT_ROOT / 'src' / 'plugins' / 'built_in' / 'hello-yueli'
EXAMPLE_ID = 'example.hello-yueli'


def _plugin():
    """走真实加载器构造示例插件实例。"""
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


def test_示例插件自带的开关是关闭的() -> None:
    """全新安装不该被一份教学素材占用模型的工具声明预算。"""
    assert plugin_enabled(EXAMPLE_DIR) is False


def test_开关确实挡住了示例插件() -> None:
    """把真实的内置根交给真实注册表，示例不应出现在已加载列表里。"""
    registry = PluginRegistry()

    registry.discover([EXAMPLE_DIR.parent])

    assert EXAMPLE_ID not in [
        plugin.manifest.plugin_id for plugin in registry.tool_plugins()
    ]


def test_打开开关后示例能被加载(tmp_path: Path) -> None:
    """把示例整份复制出去、开关改成 true，它必须真的能被发现并加载。

    这一条守的是「照抄就能跑」——示例的全部价值所在。
    """
    root = tmp_path / 'plugins'
    destination = root / 'hello-yueli'
    shutil.copytree(EXAMPLE_DIR, destination, ignore=shutil.ignore_patterns('__pycache__'))
    (destination / 'config.toml').write_text('[plugin]\nenabled = true\n', encoding='utf-8')
    registry = PluginRegistry()

    registry.discover([root])

    assert EXAMPLE_ID in [
        plugin.manifest.plugin_id for plugin in registry.tool_plugins()
    ]
