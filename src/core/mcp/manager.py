"""在后台连接服务，把部署者授权的只读工具登记到真实工具注册表。

统一模型与插件调用的日计数、断线恢复和运行状态；不做定时重连或配置热重载。
"""

from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple
import asyncio
import re

from src.core.logging.logger import get_logger
from src.core.tooling.registry import ToolRegistry
from src.core.tooling.spec import ToolContext, ToolExecutionResult, ToolInvocation, ToolSpec

from .compat import McpConnection, McpFailure, McpTool, McpToolResult, McpUnavailableError, check_sdk_version, connect
from .config import McpConfig, McpConfigError, McpServerConfig, load_mcp_config, load_provider_keys
from .render import render_observation
from .schema import McpSchemaError, narrow_schema
from .snapshot import McpSnapshotChange, save_snapshot

logger = get_logger(__name__)


@dataclass(frozen=True)
class McpToolStatus:
    """保存工具原文、授权设置、实际开放判定与今日使用情况。"""

    name: str
    title: str
    description: str
    input_schema: Dict[str, Any]
    read_only: Optional[bool]
    listed: bool
    expose: bool
    readonly: bool
    alias: str
    override_description: str
    daily_limit: int
    used_today: int
    exposed: bool
    reason: str


@dataclass(frozen=True)
class McpServerStatus:
    """管理页可读的运行信息，不携带请求头、环境变量或凭证。"""

    name: str
    state: str
    protocol_version: str = ''
    server_name: str = ''
    server_version: str = ''
    last_error: str = ''
    changes: Tuple[McpSnapshotChange, ...] = ()
    tools: Tuple[McpToolStatus, ...] = ()


class _McpExecutor:
    """在工具边界收口五类外部失败，观察与 metadata 沿用既有事件协议。"""

    def __init__(self, manager: 'McpManager', server: str, tool: str) -> None:
        self.manager, self.server, self.tool = manager, server, tool

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolExecutionResult:
        """调用统一计数入口，失败返回 success=False，不抛到会话回合层。"""
        metadata = {'mcp_server': self.server, 'mcp_tool': self.tool, 'protocol_version': ''}
        connection = self.manager._connections.get(self.server)
        if connection is not None and connection.info is not None:
            metadata['protocol_version'] = connection.info.protocol_version
        try:
            result = await self.manager.call_tool(self.server, self.tool, invocation.arguments)
            connection = self.manager._connections[self.server]
            if connection.info is not None:
                metadata['protocol_version'] = connection.info.protocol_version
            if result.is_error:
                raise McpFailure('工具报错', result.text or '服务器返回工具内部错误')
            return ToolExecutionResult(invocation.tool_name, True,
                observation=render_observation(invocation.tool_name, result), metadata=metadata)
        except McpUnavailableError as exc:
            return ToolExecutionResult(invocation.tool_name, False,
                error_message='MCP 工具不可用：' + str(exc), metadata=metadata)
        except McpFailure as exc:
            return ToolExecutionResult(invocation.tool_name, False, error_message=str(exc), metadata=metadata)


class McpManager:
    """管理每服务一个连接、动态登记与部署者白名单，不接触 SDK 字段。"""

    def __init__(self, config: McpConfig, *, data_dir: Path, providers: Dict[str, str],
                 proxy: str = '', force: bool = False) -> None:
        """保存配置与依赖；不建立连接、不启动进程、不阻塞宿主构造。"""
        self.config, self.data_dir, self.providers = config, data_dir, providers
        self.proxy, self.force = proxy, force
        self.enabled = config.enabled
        self._registry: Optional[ToolRegistry] = None
        self._connections: Dict[str, McpConnection] = {}
        self._ever_connected: Set[str] = set()
        self._states = {s.name: McpServerStatus(s.name, '未启动' if config.enabled and s.enabled else '已停用') for s in config.servers}
        self._exposed: Dict[Tuple[str, str], str] = {}
        self._registered: Dict[Tuple[str, str], str] = {}
        self._counts: Dict[Tuple[str, str], Tuple[date, int]] = {}
        self._locks: Dict[str, asyncio.Lock] = {s.name: asyncio.Lock() for s in config.servers}
        self._task: Optional[asyncio.Task[None]] = None

    @classmethod
    def from_directory(cls, directory: Path, *, data_dir: Path, proxy: str = '', force: bool = False) -> 'McpManager':
        """加载独立配置；整份验证失败逐条报错并整体停用，不运行半套服务。"""
        providers = load_provider_keys(directory)
        try:
            config = load_mcp_config(directory, data_dir=data_dir, providers=providers)
        except McpConfigError as exc:
            for error in exc.errors:
                logger.error(f'MCP 配置错误，整体不启用：{error}')
            config = McpConfig(enabled=False)
        return cls(config, data_dir=data_dir, providers=providers, proxy=proxy, force=force)

    def bind_registry(self, registry: ToolRegistry) -> None:
        """在启动前绑定聊天服务真实注册表，后续连上即追加工具。"""
        self._registry = registry

    async def startup(self) -> None:
        """只创建后台连接任务并立即返回，远端协商不能阻塞 YUELI_READY。"""
        if self._task is None:
            logger.info('MCP 工具在服务连上之后才出现')
            self._task = asyncio.create_task(self.connect_all(), name='mcp-connect-all')

    async def shutdown(self) -> None:
        """取消尚未完成的建连并关闭全部常驻任务，重复调用安全。"""
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await asyncio.gather(*(c.close() for c in self._connections.values()))

    async def connect_all(self) -> None:
        """并行建连且逐服务登记；批量取消必须关闭已经连上的连接后再上抛。"""
        if not self.enabled:
            return
        try:
            check_sdk_version()
        except McpFailure as exc:
            self.enabled = False
            logger.error(str(exc))
            return
        tasks = [asyncio.create_task(self._connect_server(s)) for s in self.config.servers if s.enabled]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(*(c.close() for c in self._connections.values()))
            raise

    async def _connect_server(self, server: McpServerConfig, *, reconnect: bool = False) -> None:
        """按服务串行化建连并独立收口错误；快照故障只影响诊断，不关闭成功连接。

        server 为已校验服务设置；reconnect 区分首次连接与调用时重连的诊断。
        取消原样传播并结束连接中状态，意外异常带服务名与堆栈立即报告。
        """
        async with self._locks[server.name]:
            try:
                old = self._connections.get(server.name)
                if old is not None and old.connected:
                    return
                self._states[server.name] = replace(self._states[server.name], state='连接中', last_error='')
                if old is not None:
                    await old.close()
                key = server.api_key or self.providers.get(server.api_key_provider, '')
                connection = await connect(server, api_key=key, proxy=self.proxy, force=self.force)
                self._connections[server.name] = connection
                info = connection.info
                if info is None:
                    raise RuntimeError('MCP 连接就绪但缺少服务器信息')
                self._register_tools(server, connection.tools)
                self._ever_connected.add(server.name)
                self._states[server.name] = replace(self._states[server.name], state='已连接',
                    protocol_version=info.protocol_version, server_name=info.name, server_version=info.version,
                    last_error='', changes=())
                # 快照属于本地诊断。读写失败必须立即报错，但不能抹掉已经成功的协商和工具登记。
                try:
                    changes = save_snapshot(self.data_dir, server.name, info, connection.tools,
                        exposed={tool for name, tool in self._exposed if name == server.name},
                        overrides={tool for tool, settings in server.tools.items() if settings.description})
                    self._states[server.name] = replace(self._states[server.name], changes=changes)
                except Exception as exc:
                    logger.exception(f'MCP 服务 {server.name} 快照失败，连接仍可用：{exc}')
                    self._states[server.name] = replace(self._states[server.name], last_error=f'快照失败：{exc}')
                state = self._states[server.name]
                opened = sum(t.exposed for t in state.tools)
                plugin_only = sum(t.listed and not t.exposed and (t.read_only is True or t.readonly) for t in state.tools)
                logger.info(f'MCP 服务 {server.name} ' + ('重连成功' if reconnect else '已连接') +
                    f'：协议 {info.protocol_version}，服务器 {info.name} {info.version}，发现 {len(connection.tools)} 个，开放 {opened} 个，仅供插件调用 {plugin_only} 个')
            except McpFailure as exc:
                self._states[server.name] = replace(self._states[server.name], state='失败', last_error=str(exc))
                log = logger.error if exc.category == '协议版本' else logger.warning
                log(f'MCP 服务 {server.name} ' + ('重连失败：' if reconnect else '连接失败：') + str(exc))
            except Exception as exc:
                logger.exception(f'MCP 服务 {server.name} 连接意外错误：{exc}')
                self._states[server.name] = replace(self._states[server.name], state='失败', last_error=f'连接意外错误：{exc}')
                connection = self._connections.get(server.name)
                if connection is not None:
                    await connection.close()
            finally:
                if self._states[server.name].state == '连接中':
                    self._states[server.name] = replace(self._states[server.name], state='失败', last_error='连接过程已取消')

    def _register_tools(self, server: McpServerConfig, tools: Tuple[McpTool, ...]) -> None:
        """独立判定每个工具的白名单、只读、名字与参数结构，冲突只跳过后来者。"""
        if self._registry is None:
            raise RuntimeError('MCP 管理器尚未绑定聊天工具注册表')
        self._exposed = {key: value for key, value in self._exposed.items() if key[0] != server.name}
        statuses = []
        for tool in tools:
            settings = server.tools.get(tool.name)
            reason, exposed = '', False
            public_name = ''
            if settings is None:
                reason = '未列入配置'
            elif tool.read_only is not True and not settings.readonly:
                reason = '未标注只读，需在配置里确认 readonly = true'
            elif not settings.expose:
                reason = '仅供插件调用'
            else:
                public_name = re.sub(r'[^A-Za-z0-9_-]', '_', settings.alias or f'{server.name}_{tool.name}')[:64]
                try:
                    parameters = narrow_schema(tool.input_schema)
                except McpSchemaError as exc:
                    reason = '参数结构不支持：' + str(exc)
                else:
                    binding = (server.name, tool.name)
                    previous = self._registered.get(binding)
                    if previous == public_name:
                        exposed = True
                    elif self._registry.resolve(public_name) is not None:
                        reason = f'工具名冲突：{public_name}'
                        logger.warning(f'MCP 服务 {server.name} 工具 {tool.name} {reason}，后到的跳过')
                    else:
                        description = (settings.description or tool.description or tool.title or f'{server.name} 提供的工具 {tool.name}')[:300]
                        spec = ToolSpec(name=public_name, description=description, parameters=parameters,
                            kind='external', side_effect='readonly', capabilities=frozenset({f'mcp.{server.name}.{tool.name}'}),
                            timeout_ms=(server.call_timeout_seconds + 2) * 1000)
                        self._registry.register_tool(spec, _McpExecutor(self, server.name, tool.name))
                        self._registered[binding] = public_name
                        exposed = True
                    if exposed:
                        self._exposed[binding] = public_name
            statuses.append(McpToolStatus(tool.name, tool.title or '', tool.description or '', tool.input_schema,
                tool.read_only, settings is not None, settings.expose if settings else False,
                settings.readonly if settings else False, settings.alias if settings else '',
                settings.description if settings else '', settings.daily_limit if settings else 50,
                self._used(server.name, tool.name), exposed, reason))
            if reason:
                logger.info(f'MCP 服务 {server.name} 工具 {tool.name} 未开放：{reason}')
        self._states[server.name] = replace(self._states[server.name], tools=tuple(statuses))

    def _today(self) -> date:
        """按服务器本地日期分日，测试可替换日期而不修改计数算法。"""
        return date.today()

    def _used(self, server: str, tool: str) -> int:
        """返回今天的次数，首次跨日读取即清零。"""
        key, today = (server, tool), self._today()
        stamp, count = self._counts.get(key, (today, 0))
        if stamp != today:
            count = 0
        self._counts[key] = (today, count)
        return count

    def stream_capabilities(self, stream_id: int) -> FrozenSet[str]:
        """所有会话共用曾连上、最近清单开放且未到上限的能力；断线后留给调用时重连。"""
        if not self.enabled:
            return frozenset()
        limits = {s.name: s.tools for s in self.config.servers}
        return frozenset(f'mcp.{server}.{tool}' for server, tool in self._exposed
            if server in self._ever_connected and self._used(server, tool) < limits[server][tool].daily_limit)

    def statuses(self) -> List[McpServerStatus]:
        """取得即时断线状态与日计数，不含连接配置中的敏感值。"""
        result = []
        for state in self._states.values():
            connection = self._connections.get(state.name)
            if state.state == '已连接' and connection is not None and not connection.connected:
                state = replace(state, state='失败', last_error=connection.last_error)
            tools = []
            for tool in state.tools:
                used = self._used(state.name, tool.name)
                exposed, reason = tool.exposed, tool.reason
                if exposed and state.state != '已连接':
                    exposed = False
                    reason = '连接已断开，下一次调用时重连' if state.name in self._ever_connected else '服务未连上，工具暂不可用'
                elif exposed and used >= tool.daily_limit:
                    exposed, reason = False, f'已达到今日上限 {tool.daily_limit} 次'
                tools.append(replace(tool, used_today=used, exposed=exposed, reason=reason))
            result.append(replace(state, tools=tuple(tools)))
        return result

    async def call_tool(self, server: str, tool: str, arguments: Dict[str, Any]) -> McpToolResult:
        """模型和插件共用授权与额度；已连接过的断线服务在下一次调用重连一次。

        :raises McpUnavailableError: 未授权、未确认只读、首次未连上或今日到上限。
        :raises McpFailure: 协议、连接、超时或服务器错误；工具内部错误仍是结果。
        """
        config = next((s for s in self.config.servers if s.name == server), None)
        if not self.enabled or config is None or not config.enabled:
            raise McpUnavailableError(f'MCP 服务 {server} 未启用')
        settings = config.tools.get(tool)
        if settings is None:
            raise McpUnavailableError(f'MCP 工具 {server}.{tool} 未列入配置')
        connection = self._connections.get(server)
        if connection is None:
            raise McpUnavailableError(f'MCP 服务 {server} 未连上')
        found = next((t for t in connection.tools if t.name == tool), None)
        if found is None:
            raise McpUnavailableError(f'MCP 工具 {server}.{tool} 未在服务工具清单中发现')
        if found.read_only is not True and not settings.readonly:
            raise McpUnavailableError('未标注只读，需在配置里确认 readonly = true')
        if self._used(server, tool) >= settings.daily_limit:
            raise McpUnavailableError(f'MCP 工具 {server}.{tool} 已达到今日上限 {settings.daily_limit} 次')
        reconnecting = not connection.connected
        try:
            async with asyncio.timeout(config.call_timeout_seconds + 1):
                if reconnecting:
                    await self._connect_server(config, reconnect=True)
                    connection = self._connections[server]
                    if not connection.connected:
                        raise McpFailure('连接', self._states[server].last_error)
                # 现象：并发请求可能同时读到尚有额度。原因：请求期间会让出事件循环。
                # 后果：必须在首次 await 前预占次数，模型和插件才能严格共用同一上限。
                used = self._used(server, tool)
                if used >= settings.daily_limit:
                    raise McpUnavailableError(f'MCP 工具 {server}.{tool} 已达到今日上限 {settings.daily_limit} 次')
                self._counts[(server, tool)] = (self._today(), used + 1)
                if used + 1 == settings.daily_limit:
                    logger.info(f'MCP 工具 {server}.{tool} 已达到今日上限 {settings.daily_limit} 次')
                return await connection.call_tool(tool, arguments)
        except TimeoutError as exc:
            raise McpFailure('连接' if reconnecting else '超时', '重连超时' if reconnecting else '请求超过配置的超时限制') from exc
