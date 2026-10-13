"""唯一接触 MCP SDK 的兼容实现。

每个连接由常驻任务持有全部异步作用域；传输、分页、错误与内容块在此转为自有类型。
包版本检查先于 SDK 导入，错误大版本不会通过导入失败拖垮宿主启动。
"""

from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from importlib.metadata import version
from threading import Thread
from typing import Any, AsyncIterator, Dict, Optional, Tuple
import asyncio
import logging
import os
import re
import traceback

from src.core.app_meta import APP_VERSION
from src.core.logging.logger import get_logger

from .types import (
    McpContentItem, McpFailure, McpServerInfo, McpTool, McpToolResult,
    PROTOCOL_VERSION_MAX, PROTOCOL_VERSION_MIN, SDK_VERSION_RANGE, connection_secrets, redact,
)

logger = get_logger(__name__)
# 连接协商与列工具的总超时，单位秒；与工具调用的可配置超时分开。
CONNECT_TIMEOUT_SECONDS = 15
# 子进程 stderr 单次日志上限，单位字符，避免启动输出占满控制台。
STDERR_MAX_CHARS = 500


class _SecretLogFilter(logging.Filter):
    """遮去 SDK 标准日志中回显的连接凭证，保留完整异常堆栈文本。"""

    def __init__(self, secrets: Tuple[str, ...]) -> None:
        super().__init__()
        self.secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        """在日志格式化前遮密钥，异常堆栈也包含可能回显的协议正文。"""
        text = record.getMessage()
        if record.exc_info:
            text += '\n' + ''.join(traceback.format_exception(*record.exc_info))
            record.exc_info = None
            record.exc_text = None
        record.msg = redact(text, self.secrets)
        record.args = ()
        return True

def check_sdk_version() -> None:
    """校验已安装 SDK 左闭右开的包版本区间，不受强制兼容开关影响。"""
    installed = version('mcp')
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)', installed)
    if match is None or not SDK_VERSION_RANGE[0] <= tuple(map(int, match.groups())) < SDK_VERSION_RANGE[1]:
        raise McpFailure('连接', f'已安装 MCP SDK {installed}，要求 >=2.3.0,<3.0.0，MCP 整体不启用')


def _client_factory(transport: Any, **kwargs: Any) -> Any:
    """版本已核对后才导入 Client；测试可包住真实作用域核对所属任务。"""
    from mcp import Client
    return Client(transport, **kwargs)


def convert_tool(tool: Any) -> McpTool:
    """逐字段转换工具，缺失属性直接抛 AttributeError，不以默认值掩盖换版错误。"""
    return McpTool(tool.name, tool.title, tool.description, dict(tool.input_schema),
                   None if tool.annotations is None else tool.annotations.read_only_hint)


def convert_result(result: Any) -> McpToolResult:
    """转换官方结果与内容块；未知块只记录类名，不猜测字段。

    :param result: SDK CallToolResult，字段必须为 v2 的下划线命名。
    :return: 自有不可变结果，is_error 原样保留。
    :raises AttributeError: SDK 形状与兼容层不符，错误携带缺失字段名。
    """
    is_error = result.is_error
    import mcp_types
    items = []
    for content in result.content:
        if isinstance(content, mcp_types.TextContent):
            item = McpContentItem('text', text=content.text)
        elif isinstance(content, mcp_types.ImageContent):
            item = McpContentItem('image', mime_type=content.mime_type)
        elif isinstance(content, mcp_types.AudioContent):
            item = McpContentItem('audio', mime_type=content.mime_type)
        elif isinstance(content, mcp_types.ResourceLink):
            item = McpContentItem('resource_link', uri=str(content.uri), title=content.title or content.name,
                                  mime_type=content.mime_type or '')
        elif isinstance(content, mcp_types.EmbeddedResource):
            resource = content.resource
            text = resource.text if isinstance(resource, mcp_types.TextResourceContents) else ''
            item = McpContentItem('resource', text=text, uri=str(resource.uri), mime_type=resource.mime_type or '')
        else:
            item = McpContentItem('unknown', text=type(content).__name__)
            logger.warning(f'MCP 工具返回未知内容类型 {type(content).__name__}')
        items.append(item)
    return McpToolResult(is_error, tuple(items), result.structured_content)


@asynccontextmanager
async def _stdio_transport(parameters: Any, name: str, secrets: Tuple[str, ...]) -> AsyncIterator[Tuple[Any, Any]]:
    """将子进程 stderr 写入管道，由守护线程按行转成截断、脱敏的宿主日志。

    现象：文本日志代理缺少 fileno，临时文件轮询又会让长期连接持续占盘。
    原因：SDK 的 errlog 直接交给操作系统，必须提供真实文件描述符。
    后果：使用管道避免落盘；SDK 关闭子进程后关闭父写端，读线程收到 EOF 才退出。
    参数 parameters 为 SDK 子进程参数，name 为服务名，secrets 为已过滤凭证；
    返回 SDK 的读写流，退出时等待日志线程处理完剩余输出。
    """
    from mcp.client.stdio import stdio_client
    stdio_logger = logging.getLogger('mcp.client.stdio')
    log_filter = _SecretLogFilter(secrets)
    stdio_logger.addFilter(log_filter)
    try:
        read_descriptor, write_descriptor = os.pipe()
        with os.fdopen(write_descriptor, 'wb') as error_pipe, os.fdopen(read_descriptor, 'rb') as reader:
            def forward() -> None:
                """阻塞读管道不占事件循环；每行只保留控制台允许的最大字符数。"""
                for raw in reader:
                    line = raw.decode('utf-8', errors='backslashreplace').strip()
                    if line:
                        logger.info(f'MCP 子进程 {name} 输出：{redact(line, secrets)[:STDERR_MAX_CHARS]}')
            thread = Thread(target=forward, name=f'mcp-stderr-{name}', daemon=True)
            thread.start()
            try:
                async with stdio_client(parameters, errlog=error_pipe) as streams:
                    yield streams
            finally:
                error_pipe.close()
                await asyncio.to_thread(thread.join)
    finally:
        stdio_logger.removeFilter(log_filter)


class _ObservedRead:
    """观察公开读流的 EOF，让服务退出立即标记断线，不进行后台轮询。"""

    def __init__(self, stream: Any, ended: asyncio.Event) -> None:
        self.stream, self.ended = stream, ended

    async def receive(self) -> Any:
        """收到流关闭即通知常驻任务；取消仍原样传播。"""
        try:
            return await self.stream.receive()
        except Exception:
            self.ended.set()
            raise

    def __aiter__(self) -> '_ObservedRead':
        return self

    async def __anext__(self) -> Any:
        import anyio
        try:
            return await self.receive()
        except anyio.EndOfStream:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        await self.stream.aclose()
        self.ended.set()

    async def __aenter__(self) -> '_ObservedRead':
        await self.stream.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> Any:
        return await self.stream.__aexit__(*exc)


@asynccontextmanager
async def _observe_transport(transport: Any, ended: asyncio.Event):
    """只包公开传输协议的读端；进入与退出仍由连接所属任务驱动。"""
    async with transport as (read, write):
        yield _ObservedRead(read, ended), write


def _failure(exc: BaseException, secrets: Tuple[str, ...], *, connecting: bool = False) -> McpFailure:
    """保留最内层原因并脱敏；connecting 区分固定建连期限与可配置调用期限。"""
    if isinstance(exc, BaseExceptionGroup):
        exc = exc.exceptions[0]
        return _failure(exc, secrets, connecting=connecting)
    if isinstance(exc, McpFailure):
        return McpFailure(exc.category, redact(exc.message, secrets))
    if isinstance(exc, AttributeError):
        return McpFailure('连接', 'SDK 对象形状与兼容层不符（' + redact(str(exc), secrets) + '）')
    if isinstance(exc, TimeoutError):
        if connecting:
            return McpFailure('连接', '连接与列工具超过 15 秒')
        return McpFailure('超时', '请求超过配置的超时限制')
    return McpFailure('连接', redact(str(exc), secrets))


class McpConnection:
    """持有一个服务连接；调用可跨任务，SDK 作用域只能由常驻任务进出。"""

    def __init__(self, config: Any, *, api_key: str, proxy: str, force: bool) -> None:
        """保存已校验参数并创建连接所属任务；禁止在构造调用者中进入 SDK。"""
        self.config = config
        self.api_key, self.proxy, self.force = api_key, proxy, force
        self.secrets = connection_secrets(api_key, headers=config.headers, env=config.env)
        self.connected = False
        self.info: Optional[McpServerInfo] = None
        self.tools: Tuple[McpTool, ...] = ()
        self.last_error = ''
        self._client: Any = None
        self._close = asyncio.Event()
        self._ended = asyncio.Event()
        self._ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._run(), name=f'mcp-{config.name}')

    async def _run(self) -> None:
        """在常驻任务内协商、列工具与等待退出，部分建立失败也由本任务清理。

        现象：跨任务退出 SDK 的取消作用域会触发运行时错误或停机卡住。
        原因：传输与会话持有按任务嵌套的异步作用域。
        后果：外部 close 只能发信号，不能替常驻任务调用 __aexit__。
        """
        from mcp import StdioServerParameters
        from mcp.client.stdio import get_default_environment
        from mcp.client.streamable_http import streamable_http_client
        import httpx2
        import mcp_types
        waiters = []
        try:
            async with AsyncExitStack() as stack:
                async with asyncio.timeout(CONNECT_TIMEOUT_SECONDS):
                    if self.config.transport == 'http':
                        headers = {k: v.replace('{api_key}', self.api_key) for k, v in self.config.headers.items()}
                        options: Dict[str, Any] = {'headers': headers, 'timeout': CONNECT_TIMEOUT_SECONDS,
                                                  'follow_redirects': False, 'trust_env': False}
                        if self.config.use_proxy and self.proxy:
                            options['proxy'] = self.proxy
                        http = await stack.enter_async_context(httpx2.AsyncClient(**options))
                        transport = streamable_http_client(self.config.url, http_client=http)
                    else:
                        parameters = StdioServerParameters(command=self.config.command, args=self.config.args,
                            env=get_default_environment() | self.config.env, cwd=self.config.cwd or None)
                        transport = _stdio_transport(parameters, self.config.name, self.secrets)
                    client = await stack.enter_async_context(_client_factory(_observe_transport(transport, self._ended),
                        read_timeout_seconds=CONNECT_TIMEOUT_SECONDS,
                        client_info=mcp_types.Implementation(name='YueLiBot', version=APP_VERSION)))
                    self._client = client
                    protocol = client.protocol_version
                    if not PROTOCOL_VERSION_MIN <= protocol <= PROTOCOL_VERSION_MAX:
                        reason = f'MCP 服务 {self.config.name} 使用协议 {protocol}，超出支持范围 {PROTOCOL_VERSION_MIN}～{PROTOCOL_VERSION_MAX}'
                        if not self.force:
                            raise McpFailure('协议版本', reason + '，已跳过')
                        logger.warning('已开启强制兼容：' + reason + '，仍然连接')
                    server = client.server_info
                    self.info = McpServerInfo('' if server is None else server.name,
                                              '' if server is None else server.version, protocol)
                    self.tools = await self._list_tools(client)
                self.connected = True
                self._ready.set_result(None)
                waiters = [asyncio.create_task(self._close.wait()), asyncio.create_task(self._ended.wait())]
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                if self._ended.is_set() and not self._close.is_set():
                    self.last_error = '连接已断开'
        except BaseException as exc:
            failure = _failure(exc, self.secrets, connecting=not self._ready.done())
            self.last_error = str(failure)
            if not self._ready.done():
                self._ready.set_exception(failure)
            elif not self._close.is_set():
                logger.warning(f'MCP 服务 {self.config.name} 连接结束：{failure}')
        finally:
            self.connected = False
            self._client = None
            for waiter in waiters:
                waiter.cancel()
            if waiters:
                await asyncio.gather(*waiters, return_exceptions=True)

    async def _list_tools(self, client: Any) -> Tuple[McpTool, ...]:
        """按 cursor 翻页；无 tools 能力或方法不存在时按空列表保留连接。"""
        from mcp import MCPError
        import mcp_types
        if client.server_capabilities.tools is None:
            logger.warning(f'MCP 服务 {self.config.name} 未声明 tools 能力，按零个工具处理')
            return ()
        tools, seen = [], set()
        cursor = None
        while True:
            try:
                page = await client.list_tools(cursor=cursor)
            except MCPError as exc:
                if exc.code == mcp_types.METHOD_NOT_FOUND:
                    logger.warning(f'MCP 服务 {self.config.name} 未实现 tools/list，按零个工具处理')
                    return ()
                raise
            tools.extend(convert_tool(tool) for tool in page.tools)
            cursor = page.next_cursor
            if not cursor:
                return tuple(tools)
            if cursor in seen:
                raise McpFailure('服务器错误', 'tools/list 返回重复的分页游标')
            seen.add(cursor)

    async def wait_ready(self) -> None:
        """等待协商完成；调用者取消时仍须让常驻任务清理已经建立的部分。"""
        try:
            await asyncio.shield(self._ready)
        except BaseException:
            await self.close()
            if self._ready.done() and not self._ready.cancelled():
                self._ready.exception()
            raise

    async def close(self) -> None:
        """请求所属任务退出并等待清理；重复关闭为空操作，跨任务关闭安全。"""
        self._close.set()
        if not self._ready.done() and not self._task.done():
            self._task.cancel()
        await asyncio.shield(self._task)

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> McpToolResult:
        """请求可跨任务执行，双层超时限制调用，所有 SDK 错误转换为自有失败。"""
        from mcp import MCPError
        import mcp_types
        if not self.connected:
            raise McpFailure('连接', self.last_error or '服务未连接')
        timeout = self.config.call_timeout_seconds
        try:
            raw = await asyncio.wait_for(self._client.call_tool(name, arguments, read_timeout_seconds=timeout), timeout + 1)
            result = convert_result(raw)
        except MCPError as exc:
            if exc.code == mcp_types.CONNECTION_CLOSED:
                self.connected = False
                self._close.set()
                raise McpFailure('连接', redact(str(exc), self.secrets)) from exc
            if exc.code == mcp_types.REQUEST_TIMEOUT:
                raise McpFailure('超时', '请求超过配置的超时限制') from exc
            raise McpFailure('服务器错误', redact(str(exc), self.secrets)) from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _failure(exc, self.secrets) from exc
        def clean(value: Any) -> Any:
            if isinstance(value, str):
                return redact(value, self.secrets)
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items()}
            if isinstance(value, list):
                return [clean(item) for item in value]
            return value
        return replace(result, items=tuple(replace(item, text=redact(item.text, self.secrets),
            uri=redact(item.uri, self.secrets), title=redact(item.title, self.secrets)) for item in result.items), structured=clean(result.structured))


async def connect(config: Any, *, api_key: str = '', proxy: str = '', force: bool = False) -> McpConnection:
    """核对 SDK 后建立并返回自有连接，取消和失败均等待所属任务清理。"""
    check_sdk_version()
    connection = McpConnection(config, api_key=api_key, proxy=proxy, force=force)
    await connection.wait_ready()
    return connection
