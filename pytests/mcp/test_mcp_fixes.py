"""验证凭证脱敏、断线后的模型调用、后台故障隔离与 stderr 管道。

使用真实注册表、官方进程内服务与本地 stdio 服务，不访问真实模型或外部服务。
"""

from pathlib import Path
from structlog.testing import capture_logs
from typing import Any, Tuple
import asyncio
import pytest
import tempfile
import threading

from src.core.mcp.compat import McpFailure, sdk
from src.core.mcp.config import McpConfig, McpServerConfig, McpToolConfig
from src.core.mcp.manager import McpManager
from src.core.tooling.registry import ToolRegistry, build_builtin_action_registry
from src.core.tooling.spec import ToolContext, ToolExecutionResult
from pytests.mcp.test_mcp import frame_for, memory_server, server_config


async def memory_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **settings: Any) -> Tuple[McpManager, ToolRegistry]:
    """装配真实管理器；仅将 SDK 传输指向进程内服务，配置和快照均在临时目录。"""
    from mcp import Client
    server = memory_server()
    monkeypatch.setattr(sdk, '_client_factory', lambda _transport, **kwargs: Client(server, **kwargs))
    config = McpConfig(servers=[server_config(tools={'echo': McpToolConfig(daily_limit=1)}, **settings)])
    registry = build_builtin_action_registry()
    manager = McpManager(config, data_dir=tmp_path, providers={})
    manager.bind_registry(registry)
    await manager.connect_all()
    return manager, registry


async def execute_echo(manager: McpManager, registry: ToolRegistry, query: str) -> ToolExecutionResult:
    """按当前能力经过真实注册表解析并执行模型调用，保留工具边界的失败结果。"""
    frame = frame_for(manager)
    invocation = registry.parse_invocation('demo_echo', {'query': query}, frame)
    resolved = registry.resolve(invocation.tool_name)
    assert resolved is not None and resolved.executor is not None
    context = ToolContext(stream_id=9, stream_kind='group', frame=frame, turn_id=7, snapshot_id='snap-7')
    return await resolved.executor.execute(invocation, context)


@pytest.mark.parametrize('env,headers,api_key,query,expected', [
    ({'DEBUG': '1'}, {}, '', '协议 2024-11-05，结果 1 条', '协议 2024-11-05，结果 1 条'),
    ({}, {'Authorization': 'Bearer {api_key}'}, 'test-key-12345', '原文 Bearer {api_key}', '原文 Bearer {api_key}'),
    ({'TOKEN': 'abcdefgh12345'}, {}, '', '结果 abcdefgh12345', '结果 [已隐藏]'),
])
async def test_star23_observation_preserves_short_values_and_masks_credentials(tmp_path, monkeypatch, env, headers, api_key, query, expected):
    """短环境值与凭证模板不能损坏观察，长凭证仍需在 SDK 结果边界抹除。"""
    manager, registry = await memory_manager(tmp_path, monkeypatch, env=env, headers=headers, api_key=api_key)
    try:
        result = await execute_echo(manager, registry, query)
        assert result.success
        assert result.observation.splitlines()[-1] == expected
    finally:
        await manager.shutdown()


@pytest.mark.parametrize('restart_fails', [False, True])
async def test_star24_disconnected_tool_remains_available_through_registry(tmp_path, restart_fails):
    """本机程序退出后模型仍能选择工具；真实注册表执行时重连或回灌连接失败。"""
    server = server_config(tools={'echo': McpToolConfig()})
    server.args += ['--marker', str(tmp_path / 'once')]
    if restart_fails:
        server.args += ['--exit-on-restart']
    manager = McpManager(McpConfig(servers=[server]), data_dir=tmp_path, providers={})
    registry = build_builtin_action_registry()
    manager.bind_registry(registry)
    try:
        await manager.connect_all()
        with pytest.raises(McpFailure):
            await manager.call_tool('demo', 'echo', {'query': 'die'})
        assert not manager._connections['demo'].connected
        names = {definition['function']['name'] for definition in registry.build_tool_definitions(frame_for(manager))}
        assert 'demo_echo' in names, '子进程退出后模型工具被撤销，无法触发调用时重连'
        status = manager.statuses()[0].tools[0]
        assert not status.exposed and status.reason == '连接已断开，下一次调用时重连'
        result = await execute_echo(manager, registry, '重连资料')
        if restart_fails:
            assert not result.success
            assert result.error_message.startswith('MCP 工具失败（连接）：')
        else:
            assert result.success and result.observation.endswith('重连资料')
    finally:
        await manager.shutdown()


async def test_star25_corrupt_snapshot_is_logged_without_losing_connections(tmp_path, monkeypatch):
    """非法快照必须带服务名与堆栈报错；两个成功连接及其工具继续可用。"""
    from mcp import Client
    server = memory_server()
    monkeypatch.setattr(sdk, '_client_factory', lambda _transport, **kwargs: Client(server, **kwargs))
    configs = [server_config(tools={'echo': McpToolConfig()}).model_copy(update={'name': name})
        for name in ('broken', 'healthy')]
    (tmp_path / 'mcp').mkdir()
    (tmp_path / 'mcp/broken.json').write_text('{invalid', encoding='utf-8')
    manager = McpManager(McpConfig(servers=configs), data_dir=tmp_path, providers={})
    registry = build_builtin_action_registry()
    manager.bind_registry(registry)
    try:
        with capture_logs() as logs:
            await manager.startup()
            outcomes = await asyncio.gather(manager._task, return_exceptions=True)
        errors = [event for event in logs if event['log_level'] == 'error' and 'broken' in event['event']]
        assert errors, '快照异常未及时输出带服务名的 error'
        assert any(event.get('exc_info') for event in errors), '快照错误缺少异常堆栈'
        assert outcomes == [None], outcomes
        states = {state.name: state for state in manager.statuses()}
        assert states['broken'].state == '已连接' and '快照失败' in states['broken'].last_error
        assert states['healthy'].state == '已连接' and not states['healthy'].last_error
        names = {definition['function']['name'] for definition in registry.build_tool_definitions(frame_for(manager))}
        assert {'broken_echo', 'healthy_echo'} <= names
        assert (await manager.call_tool('broken', 'echo', {'query': '资料'})).text == '资料'
    finally:
        await manager.shutdown()


async def test_unexpected_connection_exception_is_isolated_per_server(tmp_path, monkeypatch):
    """SDK 收口以外的意外错误不应终止整批建连，失败服务需及时暴露原因。"""
    from src.core.mcp import manager as manager_module
    manager, _registry = await memory_manager(tmp_path, monkeypatch)
    await manager.shutdown()
    broken = manager.config.servers[0].model_copy(update={'name': 'broken'})
    manager = McpManager(McpConfig(servers=[broken, manager.config.servers[0]]), data_dir=tmp_path, providers={})
    manager.bind_registry(build_builtin_action_registry())
    original = manager_module.connect
    async def fail_one(config: McpServerConfig, **kwargs: Any):
        if config.name == 'broken':
            raise RuntimeError('测试意外错误')
        return await original(config, **kwargs)
    monkeypatch.setattr(manager_module, 'connect', fail_one)
    try:
        with capture_logs() as logs:
            outcomes = await asyncio.gather(manager.connect_all(), return_exceptions=True)
        assert any(event['log_level'] == 'error' and 'broken' in event['event'] and event.get('exc_info') for event in logs)
        assert outcomes == [None]
        states = {state.name: state for state in manager.statuses()}
        assert states['broken'].state == '失败' and '测试意外错误' in states['broken'].last_error
        assert states['demo'].state == '已连接'
    finally:
        await manager.shutdown()


async def test_stdio_stderr_uses_pipe_and_is_fully_drained(tmp_path, monkeypatch):
    """真实子进程的两行 stderr 及时转为脱敏日志，连接期间及关闭后均不落临时文件。"""
    stderr_dir = tmp_path / 'stderr'
    stderr_dir.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(stderr_dir))
    config = server_config(env={'TOKEN': 'abcdefgh12345'})
    config.args += ['--stderr']
    connection = None
    try:
        with capture_logs() as logs:
            connection = await sdk.connect(config)
            async with asyncio.timeout(3):
                while not any('stderr 第二行' in event['event'] for event in logs):
                    await asyncio.sleep(.01)
            assert not list(stderr_dir.iterdir()), 'stdio stderr 在连接存续期间仍写临时文件'
            await connection.close()
        lines = [event['event'] for event in logs if 'MCP 子进程 demo 输出' in event['event']]
        assert len(lines) == 2
        assert any('stderr 第一行 [已隐藏]' in line for line in lines)
        assert any('stderr 第二行' in line for line in lines)
        assert all(len(line.split('输出：', 1)[1]) <= sdk.STDERR_MAX_CHARS for line in lines)
        assert not list(stderr_dir.iterdir())
        assert not any(thread.name == 'mcp-stderr-demo' for thread in threading.enumerate())
    finally:
        if connection is not None:
            await connection.close()


async def test_connect_timeout_is_a_fixed_connection_failure(monkeypatch):
    """缩短实际等待，建连超时的对外文案仍说明固定的十五秒期限。"""
    monkeypatch.setattr(sdk, 'CONNECT_TIMEOUT_SECONDS', .02)
    with pytest.raises(McpFailure) as failure:
        await sdk.connect(server_config())
    assert failure.value.category == '连接'
    assert failure.value.message == '连接与列工具超过 15 秒'


async def test_call_timeout_keeps_configured_request_message(tmp_path, monkeypatch):
    """调用阶段的超时继续使用已有超时类别与配置期限文案。"""
    manager, _registry = await memory_manager(tmp_path, monkeypatch)
    async def timed_out(*args: Any, **kwargs: Any):
        raise TimeoutError()
    monkeypatch.setattr(manager._connections['demo']._client, 'call_tool', timed_out)
    try:
        with pytest.raises(McpFailure) as failure:
            await manager.call_tool('demo', 'echo', {'query': '资料'})
        assert failure.value.category == '超时'
        assert failure.value.message == '请求超过配置的超时限制'
    finally:
        await manager.shutdown()


@pytest.mark.parametrize('unavailable', ['limit', 'disabled'])
async def test_executor_reports_unavailable_without_connection_category(tmp_path, monkeypatch, unavailable):
    """模型选择后到执行前，额度或启用状态改变时回灌不可用原因。"""
    manager, registry = await memory_manager(tmp_path, monkeypatch)
    frame = frame_for(manager)
    invocation = registry.parse_invocation('demo_echo', {'query': '资料'}, frame)
    context = ToolContext(stream_id=9, stream_kind='group', frame=frame, turn_id=7, snapshot_id='snap-7')
    try:
        if unavailable == 'limit':
            await manager.call_tool('demo', 'echo', {'query': '先占额度'})
        else:
            manager.config.servers[0].enabled = False
        result = await registry.resolve('demo_echo').executor.execute(invocation, context)
        assert not result.success and result.error_message.startswith('MCP 工具不可用：')
    finally:
        await manager.shutdown()


async def test_call_timeout_finishes_reconnecting_state(tmp_path, monkeypatch):
    """真实调用期限取消重连时，管理页不能永久停在连接中。"""
    from src.core.mcp import manager as manager_module
    manager, _registry = await memory_manager(tmp_path, monkeypatch)
    await manager._connections['demo'].close()
    manager.config.servers[0].call_timeout_seconds = 1
    async def blocked(*args: Any, **kwargs: Any):
        await asyncio.Future()
    monkeypatch.setattr(manager_module, 'connect', blocked)
    try:
        with pytest.raises(McpFailure, match='重连超时'):
            await manager.call_tool('demo', 'echo', {'query': '资料'})
        state = manager.statuses()[0]
        assert state.state == '失败' and '取消' in state.last_error
    finally:
        await manager.shutdown()
