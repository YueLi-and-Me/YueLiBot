"""提供兼容层生命周期用例的本地 stdio 服务。

由官方服务端实现协议；横幅、中途退出与启动延迟只在显式参数启用时发生。
"""

from pathlib import Path
import argparse
import asyncio
import json
import os
import sys
import time

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

parser = argparse.ArgumentParser()
parser.add_argument('--banner', action='store_true')
parser.add_argument('--delay', type=float, default=0)
parser.add_argument('--marker', type=Path)
parser.add_argument('--exit-on-restart', action='store_true')
parser.add_argument('--stderr', action='store_true')
args = parser.parse_args()
if args.exit_on_restart and args.marker is not None and args.marker.exists():
    os._exit(3)
if args.stderr:
    sys.stderr.reconfigure(encoding='utf-8')
    print('stderr 第一行 ' + os.environ['TOKEN'], file=sys.stderr, flush=True)
    print('stderr 第二行 ' + '输出' * 400, file=sys.stderr, flush=True)
if args.banner:
    print('本地测试服务启动横幅', flush=True)
time.sleep(args.delay)
server = MCPServer('本地测试服务', version='1.0.0')


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
async def echo(query: str) -> str:
    """返回测试资料；退出分支用于验证断线与下一次调用恢复。"""
    if query == 'die' and args.marker is not None and not args.marker.exists():
        args.marker.write_text('exited', encoding='utf-8')
        os._exit(3)
    if query == 'slow':
        await asyncio.sleep(5)
    if query == 'error':
        raise ValueError('测试工具报错')
    return query


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def environment() -> str:
    """只返回测试指定的两个环境键，不泄露宿主或操作系统环境。"""
    return json.dumps({'danger': os.environ.get('YUELI_FORBIDDEN_CREDENTIAL'),
                       'custom': os.environ.get('MCP_PROBE_VALUE')})


if __name__ == '__main__':
    server.run()
