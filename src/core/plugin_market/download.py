"""按索引镜像顺序下载插件包，逐次核对大小和摘要并清除失败的暂存文件。

operations 在共享安装锁内调用本模块；地址已由 index 校验，网络使用宿主代理。
"""

from pathlib import Path
from typing import Optional
from uuid import uuid4

import hashlib

import httpx

from .index import VersionEntry

# 压缩包允许的最大字节数；超过即停止接收，不等待完整响应。
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
# 单次下载网络等待上限，单位秒，正整数。
DOWNLOAD_TIMEOUT_SECONDS = 60


def download_archive(version: VersionEntry, staging: Path, *, proxy: str = '', transport: Optional[httpx.BaseTransport] = None) -> Path:
    """流式下载一个版本，失败时按顺序换源，每次都重新验证完整性。

    :param version: 已验证的索引版本，提供有序下载地址、字节数和 sha256。
    :param staging: 暂存目录，下载文件名为随机 UUID。
    :param proxy: 宿主代理；空值继承 HTTP 客户端环境。
    :param transport: 离线测试的内存传输，生产不传。
    :return: 验证成功的 zip 路径；调用方负责操作结束后的清理。
    :raises ValueError: 所有地址均失败，消息逐项列出地址和原因。
    """
    staging.mkdir(parents=True, exist_ok=True)
    errors = []
    for url in version.downloads:
        path = staging / f'{uuid4()}.zip'
        try:
            digest, size = hashlib.sha256(), 0
            with httpx.Client(proxy=proxy or None, transport=transport, timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True) as client:
                with client.stream('GET', url) as response:
                    if response.status_code != 200:
                        raise ValueError(f'HTTP {response.status_code}')
                    with path.open('wb') as output:
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > MAX_DOWNLOAD_BYTES:
                                raise ValueError(f'超过 {MAX_DOWNLOAD_BYTES} 字节上限')
                            digest.update(chunk)
                            output.write(chunk)
            if size != version.size:
                raise ValueError(f'字节数不符，索引 {version.size}，实际 {size}')
            if digest.hexdigest() != version.sha256.lower():
                raise ValueError('sha256 校验不符')
            return path
        except (OSError, ValueError, httpx.HTTPError) as exc:
            path.unlink(missing_ok=True)
            errors.append(f'{url}：{exc}')
    raise ValueError('插件下载全部失败：\n' + '\n'.join(errors))
