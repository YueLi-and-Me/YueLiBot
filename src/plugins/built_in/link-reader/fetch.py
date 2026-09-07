"""链接插件的 HTTP 抓取边界。

fetch_url 逐跳检查协议与解析地址，在总截止时间内流式读取有限正文。
仅依赖标准库和 httpx；plugin.py 负责将这里的拒绝与网络异常转成工具结果。
"""

from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urljoin, urlsplit
import asyncio
import ipaddress
import socket

import httpx


# 总链路限时 6 秒，给宿主的 8 秒工具限时留下解码、抽取和渲染时间。
FETCH_TIMEOUT_SECONDS = 6.0
# 每次抓取最多保存 2 MiB 正文；重定向上限为 3 跳。
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
# 插件每次消费最多 64 KiB；累计正文不超过 MAX_BODY_BYTES，当前消费块不超过此值。
# 两者的有效数据量上界为 MAX_BODY_BYTES + READ_CHUNK_BYTES。httpx 的 transport
# 内部缓冲与最终 bytes 封装分配不在此公式内，不能把它声称为整个进程的峰值内存。
READ_CHUNK_BYTES = 64 * 1024
# 空 UA 或库默认 UA 会被部分站点直接拒绝为 403，因此使用浏览器标识。
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36'
READABLE_TYPES = frozenset({
    'text/html', 'application/xhtml+xml', 'text/plain', 'text/markdown', 'application/json',
})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class FetchRejected(ValueError):
    """URL 或重定向违反出站安全边界；尚未向被拒地址发送 HTTP 请求。"""


class FetchError(RuntimeError):
    """站点状态、响应协议或总截止时间使本次抓取无法完成。"""


@dataclass(frozen=True)
class FetchResult:
    """抓取的有限结果；size_bytes 是站点声明的大小，未知时为 None。

    content_type 保留 charset 参数供解码使用；不可抽取类型的 body 恒为空。
    truncated 表示达到本地上限而停止读取，不能据此声称已经拿到完整页面。
    """

    final_url: str
    content_type: str
    body: bytes
    size_bytes: Optional[int]
    truncated: bool


async def resolve_addresses(host: str, port: int) -> List[str]:
    """在线程池解析 host 的所有 TCP 地址；port 是已校验的目标端口。

    返回地址字符串列表；DNS 失败原样抛出，由工具执行体转成失败观察。
    独立入口允许离线用例替换解析过程，避免测试产生真实 DNS 请求。
    """
    rows = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [row[4][0] for row in rows]


async def validate_url(url: str) -> None:
    """在请求之前校验绝对 HTTP(S) URL 及其所有解析地址。

    URL 含凭据、非法端口、空主机或非公网地址时抛 FetchRejected；DNS 失败上抛。
    检查结果不持久缓存，重定向每跳必须重新检查。
    """
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in {'http', 'https'} or not parts.hostname:
            raise FetchRejected('链接只接受带主机名的 http 或 https 地址')
        if parts.username is not None or parts.password is not None:
            raise FetchRejected('链接不能包含认证凭据')
        port = parts.port
        if port is not None and port <= 0:
            raise FetchRejected('链接端口必须大于 0')
    except ValueError as exc:
        raise FetchRejected(f'链接地址无效：{exc}') from exc
    host = parts.hostname
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        addresses = await resolve_addresses(host, port if port is not None else (443 if parts.scheme == 'https' else 80))
    else:
        addresses = [str(address)]
    if not addresses:
        raise FetchRejected('链接主机未解析出可用地址')
    for raw in addresses:
        address = ipaddress.ip_address(raw)
        if (address.is_loopback or address.is_private or address.is_link_local
                or address.is_reserved or address.is_multicast or address.is_unspecified
                or not address.is_global):
            raise FetchRejected('拒绝访问内网或非公网地址')
    # DNS 校验通过后，实际连接仍会再次解析，两个时刻之间存在时间窗。
    # 攻击者切换 DNS 记录仍可能绕过检查；纯 Python 侧 httpx 无法闭合该窗口，
    # 代理也可能使用不同的解析结果，因此这里不能宣称防住了 DNS 重绑定。


async def fetch_url(
    url: str, *, proxy: str = '', transport: Optional[httpx.AsyncBaseTransport] = None,
) -> FetchResult:
    """在总截止时间内抓取一个链接，返回有限正文或文件类型信息。

    :param url: 待校验的绝对地址。
    :param proxy: 宿主的全局 HTTP(S) 代理，空字符串表示直连。
    :param transport: 离线测试用 MockTransport；生产环境省略。
    :return: 最后一跳的类型、大小、正文和截断状态。
    :raises FetchRejected: 地址或重定向违反安全约束。
    :raises FetchError: HTTP 失败、无效重定向或总超时。
    :raises Exception: 其它网络与解析错误，由调用它的工具执行体统一收口。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + FETCH_TIMEOUT_SECONDS
    try:
        # 每跳超时会让多次慢重定向累加，最终被宿主砍断而丢失中文诊断。
        # 外层总截止时间覆盖 DNS、响应头和慢速分块；每跳 httpx 同时使用剩余预算。
        async with asyncio.timeout_at(deadline):
            current = url
            for hop in range(MAX_REDIRECTS + 1):
                await validate_url(current)
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError
                # 每跳独立客户端，不会把前一跳 Set-Cookie 保存后带往下一跳。
                # 禁止读取环境代理与 netrc；代理只能来自宿主注入的参数。
                async with httpx.AsyncClient(
                    proxy=proxy if proxy else None, transport=transport,
                    follow_redirects=False, trust_env=False, timeout=remaining,
                    headers={'User-Agent': USER_AGENT, 'Accept-Encoding': 'identity'},
                ) as client:
                    async with client.stream('GET', current) as response:
                        if response.status_code in REDIRECT_STATUSES:
                            if hop == MAX_REDIRECTS:
                                raise FetchRejected('链接重定向超过 3 跳')
                            location = response.headers.get('location', '')
                            if not location:
                                raise FetchError('链接重定向缺少 Location')
                            current = urljoin(str(response.url), location)
                            continue
                        if not 200 <= response.status_code < 300:
                            raise FetchError(f'网页返回 HTTP {response.status_code}')
                        content_type = response.headers.get('content-type', '')
                        raw_size = response.headers.get('content-length', '')
                        size = int(raw_size) if raw_size.isascii() and raw_size.isdigit() else None
                        if content_type.partition(';')[0].strip().lower() not in READABLE_TYPES:
                            return FetchResult(str(response.url), content_type, b'', size, False)
                        # identity 避免压缩炸弹在 httpx 自动解压时先膨胀再触发大小闸门。
                        # 站点若无视协商继续压缩，明确报错，不能把压缩字节当文本回灌。
                        encoding = response.headers.get('content-encoding', 'identity').strip().lower()
                        if encoding != 'identity':
                            raise FetchError(f'网页忽略未压缩传输要求，返回了不支持的内容编码：{encoding}')
                        body = bytearray()
                        truncated = False
                        async for chunk in response.aiter_bytes(chunk_size=READ_CHUNK_BYTES):
                            body.extend(chunk[:MAX_BODY_BYTES - len(body)])
                            if len(body) >= MAX_BODY_BYTES:
                                truncated = True
                                break
                        # 触限后不再消费下一块，退出响应上下文立刻关闭流。
                        return FetchResult(str(response.url), content_type, bytes(body), size, truncated)
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise FetchError(f'链接读取超过总时限 {FETCH_TIMEOUT_SECONDS:g} 秒') from exc
    raise FetchError('链接未返回可处理的响应')
