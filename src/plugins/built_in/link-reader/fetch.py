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


# 只声明我们确实能解码的压缩方式；br 需要额外依赖，不声明就不该收到。
ACCEPT_ENCODING = 'gzip, deflate'
# httpx 能在流式读取中逐块解码的内容编码；identity 与空值表示未压缩。
DECODABLE_ENCODINGS = frozenset({'gzip', 'x-gzip', 'deflate', 'identity'})


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
        if _is_transparent_proxy_placeholder(address):
            continue
        if (address.is_loopback or address.is_private or address.is_link_local
                or address.is_reserved or address.is_multicast or address.is_unspecified
                or not address.is_global):
            raise FetchRejected('拒绝访问内网或非公网地址')
    # DNS 校验通过后，实际连接仍会再次解析，两个时刻之间存在时间窗。
    # 攻击者切换 DNS 记录仍可能绕过检查；纯 Python 侧 httpx 无法闭合该窗口，
    # 代理也可能使用不同的解析结果，因此这里不能宣称防住了 DNS 重绑定。


# TUN 模式代理客户端为被代理域名分配的 fake-IP 占位网段。
#
# - 现象：开着 TUN 代理时，公网域名解析成 198.18.x.x，地址校验按「私有地址」整段拒绝，
#   于是一条外链都读不了。
# - 原因：这一段是 RFC 2544 的基准测试保留段，``ipaddress`` 判定为 is_private 且
#   非 is_global；而代理客户端正是挑这种「不会出现在真实网络里」的段做占位，
#   应用连上去之后由 TUN 驱动劫持并按域名走代理出网。
# - 后果：把它算进内网会让本机的可用性归零；不算进内网也不产生新风险——这一段
#   按 RFC 不得出现在公网，也不是任何常规局域网的取值，没有代理时根本不会有域名
#   解析到这里。守卫要挡的是「读本机的 WebUI、协议端端口或局域网设备」，那些落在
#   127/8、10/8、172.16/12、192.168/16 与 link-local，逐条仍然拒绝。
#
# 再遇到别的客户端用其它占位段（例如 Class E 240/4）时按同样判据往这里加，
# 不要改上面那串通用判定。
_PROXY_PLACEHOLDER_NETWORKS = (
    ipaddress.ip_network('198.18.0.0/15'),
)


def _is_transparent_proxy_placeholder(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """判断地址是否落在透明代理的 fake-IP 占位段。

    :param address: 已解析出的目标地址。
    :return: 命中占位段返回 ``True``，此时跳过内网判定；其余一律返回 ``False``。
    """
    return any(address in network for network in _PROXY_PLACEHOLDER_NETWORKS)


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
                    headers={'User-Agent': USER_AGENT, 'Accept-Encoding': ACCEPT_ENCODING},
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
                        # 压缩炸弹的闸门是「解压后累计字节」，不是「拒绝压缩」。
                        #
                        # - 现象：早先只接受 identity，站点无视协商照常返回 gzip 就报错。
                        #   真机上 B 站正是如此，一类最常被分享的链接整类读不了。
                        # - 原因：Accept-Encoding 是协商不是强制，服务器有权忽略；把
                        #   合规响应判成错误，判据本身就站不住。
                        # - 后果：下面的累计上限作用在 aiter_bytes 产出的**解压后**字节上，
                        #   达到上限即停止读取，炸弹不会被完整展开——这比拒绝压缩更严格，
                        #   因为它与压缩比无关。残留风险：单个解码块可能瞬时超过上限，
                        #   量级为原始块大小乘以压缩比，无法在纯 httpx 侧消除。
                        encoding = response.headers.get('content-encoding', '').strip().lower()
                        if encoding and encoding not in DECODABLE_ENCODINGS:
                            raise FetchError(f'网页返回了无法解码的内容编码：{encoding}')
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
