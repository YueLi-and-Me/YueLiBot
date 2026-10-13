"""补充 SDK 的可选方法、分页、实际错误码和最小子进程环境验证。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
import json
import tempfile

import pytest

from src.core.mcp.compat import sdk
from src.core.mcp.compat.types import McpFailure
from src.core.mcp.render import render_observation
from pytests.mcp.test_mcp import manager_for, server_config


async def test_optional_tool_listing_and_pagination(tmp_path, monkeypatch) -> None:
    """空能力与方法不存在不破坏连接，分页必须完整聚合。"""
    from mcp import MCPError
    from mcp_types import METHOD_NOT_FOUND, ListToolsResult, Tool
    manager, _registry = await manager_for(tmp_path, monkeypatch)
    connection = manager._connections['demo']
    client = SimpleNamespace(server_capabilities=SimpleNamespace(tools=None), list_tools=AsyncMock())
    assert await connection._list_tools(client) == ()
    client.list_tools.assert_not_called()
    client.server_capabilities.tools = True
    client.list_tools.side_effect = MCPError(METHOD_NOT_FOUND, '未实现可选方法')
    assert await connection._list_tools(client) == ()
    client.list_tools.side_effect = [ListToolsResult(tools=[Tool(name='first', input_schema={'type': 'object'})], next_cursor='next'),
                                     ListToolsResult(tools=[Tool(name='second', input_schema={'type': 'object'})])]
    assert [t.name for t in await connection._list_tools(client)] == ['first', 'second']
    assert client.list_tools.call_args.kwargs['cursor'] == 'next'
    await manager.shutdown()


@pytest.mark.parametrize('code,category', [(-32603, '服务器错误'), (-32001, '超时')])
async def test_sdk_error_codes_and_key_redaction(tmp_path, monkeypatch, code, category) -> None:
    """JSON-RPC 与请求超时按实际 SDK 错误码分账，异常中的凭证必须隐藏。"""
    from mcp import MCPError
    from mcp_types import REQUEST_TIMEOUT
    manager, _registry = await manager_for(tmp_path, monkeypatch)
    connection = manager._connections['demo']
    connection.secrets = ('credential-fixture',)
    code = REQUEST_TIMEOUT if category == '超时' else code
    monkeypatch.setattr(connection._client, 'call_tool', AsyncMock(side_effect=MCPError(code, 'credential-fixture 错误')))
    with pytest.raises(McpFailure) as error:
        await connection.call_tool('echo', {'query': '资料'})
    assert error.value.category == category
    assert 'credential-fixture' not in str(error.value)
    await manager.shutdown()


async def test_sdk_call_timeout_is_bounded(tmp_path, monkeypatch) -> None:
    """即使第三方调用不自行返回，兼容层的外层超时也必须结束请求。"""
    manager, _registry = await manager_for(tmp_path, monkeypatch)
    connection = manager._connections['demo']
    connection.config.call_timeout_seconds = 1
    async def slow(*_args, **_kwargs):
        await asyncio.sleep(10)
    monkeypatch.setattr(connection._client, 'call_tool', slow)
    with pytest.raises(McpFailure, match='超时'):
        await connection.call_tool('echo', {'query': '资料'})
    await manager.shutdown()


def test_result_blocks_and_structured_only_rendering() -> None:
    """官方内容对象逐字段转换，资源文本、未知块与结构化正文不会被丢掉。"""
    from mcp_types import AudioContent, CallToolResult, EmbeddedResource, ImageContent, ResourceLink, TextContent, TextResourceContents
    result = sdk.convert_result(CallToolResult(content=[TextContent(text='资料'),
        ImageContent(data='AA==', mime_type='image/png'), AudioContent(data='AA==', mime_type='audio/wav'),
        ResourceLink(name='链接', title='标题', uri='https://example.invalid/resource'),
        EmbeddedResource(resource=TextResourceContents(uri='https://example.invalid/text', text='资源正文'))]))
    rendered = render_observation('demo_echo', result)
    assert all(s in rendered for s in ('资料', '[图片]', '[音频]', '[链接] 标题', '资源正文'))
    structured = sdk.convert_result(CallToolResult(content=[], structured_content={'中文': ['值']}))
    assert render_observation('demo_echo', structured).endswith('{"中文":["值"]}')


async def test_stdio_environment_is_minimal(monkeypatch) -> None:
    """宿主未列入配置的凭证不会传给子进程，显式配置的环境值会传入。"""
    monkeypatch.setenv('YUELI_FORBIDDEN_CREDENTIAL', 'forbidden-fixture')
    config = server_config(env={'MCP_PROBE_VALUE': 'configured', 'TEMP': tempfile.gettempdir(), 'TMP': tempfile.gettempdir()})
    connection = await sdk.connect(config)
    try:
        data = json.loads((await connection.call_tool('environment', {})).text)
        assert data == {'danger': None, 'custom': '[已隐藏]'}
    finally:
        await connection.close()
