"""验证真实聊天工厂的 MCP 接缝与插件配置保存的整份拒绝边界。"""

from dataclasses import replace
from pathlib import Path
from typing import Any, Dict
from pydantic import Field
import json

import pytest

from src.core.config.schema import Config
from src.core.mcp.config import McpConfig, McpToolConfig
from src.core.mcp.manager import McpManager
from src.core.services.chat import ChatService
from src.plugin_system import PluginConfig
from pytests.agent.test_plugin_compatibility import write_plugin
from pytests.mcp.test_mcp import memory_server, server_config


@pytest.mark.parametrize('case', ['success', 'unlisted', 'unconfirmed', 'limit', 'unconnected', 'tool_error'])
async def test_star17_real_chat_factory_injects_mcp(db, tmp_path, monkeypatch, case) -> None:
    """工厂保持生产实现，只替换服务传输；ctx 的访问限制均由真实管理器执行。"""
    from mcp import Client
    from src.plugin_system import McpUnavailableError
    from src.core.mcp.compat import sdk
    import src.core.services.chat.service as service_module
    root = tmp_path / 'plugins'
    write_plugin(root, 'access')
    monkeypatch.setattr(service_module, 'PLUGIN_ROOTS', ((root, False),))
    server = memory_server()
    monkeypatch.setattr(sdk, '_client_factory', lambda _transport, **kw: Client(server, **kw))
    if case == 'unconfirmed':
        original = sdk.convert_tool
        monkeypatch.setattr(sdk, 'convert_tool', lambda tool: replace(original(tool), read_only=None))
    settings = McpToolConfig(expose=False, daily_limit=1)
    config = McpConfig(servers=[server_config(tools={} if case == 'unlisted' else {'echo': settings})])
    manager = McpManager(config, data_dir=tmp_path, providers={})
    chat = ChatService(db, None, None, None, lambda *_args: None, cfg=Config(), data_dir=tmp_path, mcp=manager)
    access = chat._plugins.tool_plugins()[0].ctx.mcp
    assert {name for name in dir(access) if not name.startswith('_')} == {'call_tool'}
    if case != 'unconnected':
        await manager.connect_all()
    if case == 'limit':
        await access.call_tool('demo', 'echo', {'query': '第一次'})
    if case in ('success', 'tool_error'):
        result = await access.call_tool('demo', 'echo', {'query': 'error' if case == 'tool_error' else '插件资料'})
        assert result.is_error is (case == 'tool_error')
        if case == 'success':
            assert result.text == '插件资料'
    else:
        reasons = {'unlisted': '未列入配置', 'unconfirmed': '未标注只读', 'limit': '今日上限', 'unconnected': '未连上'}
        with pytest.raises(McpUnavailableError, match=reasons[case]):
            await access.call_tool('demo', 'echo', {'query': '资料'})
    await manager.shutdown()


class EditableConfig(PluginConfig):
    """覆盖密钥、数值约束与内联值转义的测试模型。"""

    api_key: str = Field('', json_schema_extra={'secret': True})
    limit: int = Field(2, ge=1, le=3)
    text: str = ''
    headers: Dict[str, Any] = Field(default_factory=dict)


def test_star18_save_rejects_whole_document_and_preserves_secret(tmp_path: Path) -> None:
    """非法字段不能触碰磁盘；空密钥沿用原值，写回文件由同一读取器原样读取。"""
    from src.plugin_system.config import ensure_plugin_config, save_plugin_config, secret_fields
    assert secret_fields(EditableConfig) == frozenset({'api_key'})
    first = save_plugin_config(tmp_path, EditableConfig, {'api_key': 'test-only-key', 'limit': 2})
    assert first['ok']
    path = tmp_path / 'config.toml'
    before = path.read_bytes()
    invalid = save_plugin_config(tmp_path, EditableConfig, {'api_key': '', 'limit': 10, 'text': '不能写入'})
    assert not invalid['ok'] and 'limit' in invalid['errors']
    assert path.read_bytes() == before
    values = {'api_key': '', 'limit': 3, 'text': '第一行\n第二行\t"引号"', 'headers': {'X custom.key': '值\n下一行'}}
    assert save_plugin_config(tmp_path, EditableConfig, values)['ok']
    loaded = ensure_plugin_config(tmp_path, EditableConfig)
    assert loaded.api_key == 'test-only-key'
    assert loaded.limit == 3 and loaded.text == values['text'] and loaded.headers == values['headers']
    assert '# ' in path.read_text(encoding='utf-8')
