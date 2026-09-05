"""匿名安装量统计的客户端。

只回答一个问题：全球有多少个安装、分别是什么版本、跑在什么系统上。
载荷固定为三个字段，不含聊天内容、不含任何身份信息，也不含模型别名。

**端点常量为空时整条链路保持惰性**——不注册、不心跳、不建任何网络对象。
发行版里这条路径是活的但服务端可能尚未立起，惰性保证用户看到的是「什么都
没发生」，而不是每十分钟一次注定失败的请求。

身份由服务端下发：首次 ``POST /register`` 取回 UUID 并落在数据目录，之后每次
``POST /heartbeat`` 带 ``Client-UUID`` 头。客户端不自己生成 ID，换来的是统计
口径能防刷、能去重。

**遥测绝不能影响正常使用**：全部网络调用带超时，失败只记 warning，不抛、不重试
到阻塞、不进任何功能路径；启动失败不影响其余服务装配。

被 ``src.main`` 在生命周期里注册，与其它后台服务同一形态。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import asyncio
import json
import platform

import httpx

from src.core.app_meta import APP_VERSION
from src.core.logging.logger import get_logger

logger = get_logger(__name__)

# 服务端地址。空字符串表示服务端尚未立起，此时整条链路保持惰性。
# 立起来之后只需在这里填上根地址（不带尾斜杠），其余代码不动。
TELEMETRY_ENDPOINT = ''

# 身份文件名，落在数据目录下。
IDENTITY_FILENAME = 'telemetry.json'
# 心跳间隔（秒）。十分钟一次，与「在线」窗口的口径配套。
HEARTBEAT_INTERVAL_S = 600.0
# 单次请求超时（秒）。遥测不值得为它多等，超时即放弃本轮。
REQUEST_TIMEOUT_S = 10.0


def _os_type() -> str:
    """把 ``platform.system()`` 归一成三档，未知系统原样返回。

    :return: ``Windows`` / ``Linux`` / ``macOS`` 之一，或原始系统名。
    """
    system = platform.system()
    return 'macOS' if system == 'Darwin' else system


def payload() -> Dict[str, str]:
    """组装上报载荷。

    固定三个字段，这是 2026-09-04 拍板的范围。**加字段容易，减字段要重新征求
    同意**——要动这里之前先确认协议正文与配置注释是否同步。

    :return: 应用版本、系统类型与 Python 版本。
    """
    return {
        'app_version': APP_VERSION,
        'os_type': _os_type(),
        'python_version': platform.python_version(),
    }


def identity_path(data_dir: Path) -> Path:
    """给出身份文件路径。

    :param data_dir: 运行时数据目录。
    :return: ``<data_dir>/telemetry.json``；文件是否存在不在此校验。
    """
    return data_dir / IDENTITY_FILENAME


def read_identity(data_dir: Path) -> str | None:
    """读取已注册的 UUID。

    :param data_dir: 运行时数据目录。
    :return: UUID 字符串；未注册、文件损坏或字段缺失时返回 ``None``。
        损坏按未注册处理——重注册的代价只是统计上多一个安装，
        而报错会把遥测的失败带进启动路径。
    """
    path = identity_path(data_dir)
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(document, dict):
        return None
    uuid = document.get('uuid')
    return uuid if isinstance(uuid, str) and uuid.strip() else None


def write_identity(data_dir: Path, uuid: str) -> None:
    """落盘服务端下发的 UUID。

    :param data_dir: 运行时数据目录；不存在时递归创建。
    :param uuid: 服务端返回的标识。
    :raises OSError: 目录或文件无法写入。
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    identity_path(data_dir).write_text(
        json.dumps({'uuid': uuid}, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )


class TelemetryService:
    """按固定间隔上报三个字段的后台服务。"""

    def __init__(self, data_dir: Path, *, enabled: bool, endpoint: str = TELEMETRY_ENDPOINT) -> None:
        """保存运行参数，不做任何网络动作。

        :param data_dir: 运行时数据目录，身份文件落在这里。
        :param enabled: 配置开关；关闭时整条链路不启动。
        :param endpoint: 服务端根地址；为空表示服务端尚未立起，保持惰性。
        副作用：只保存引用，不建立连接也不读文件。
        """
        self._data_dir = data_dir
        self._enabled = enabled
        self._endpoint = endpoint.rstrip('/')
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def active(self) -> bool:
        """判断本轮是否真的会上报。

        :return: 开关打开且端点非空时为真。两者任一不成立都保持惰性。
        """
        return self._enabled and bool(self._endpoint)

    async def startup(self) -> None:
        """启动心跳任务。

        必须立即返回：把循环本身注册成启动钩子会让生命周期永久 await，
        其后的服务全部起不来。

        :return: ``None``。
        副作用：``active`` 为真时创建名为 ``telemetry-heartbeat`` 的后台任务。
        """
        if not self.active:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name='telemetry-heartbeat')

    async def shutdown(self) -> None:
        """停止心跳任务并等待它退出。

        :return: ``None``。
        副作用：置停止信号并取消后台任务。
        """
        self._stop.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: B014 - 收尾不因遥测失败而中断
            pass

    async def _loop(self) -> None:
        """按间隔上报，直到收到停止信号。"""
        while not self._stop.is_set():
            await self._beat_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=HEARTBEAT_INTERVAL_S)
            except asyncio.TimeoutError:
                continue

    async def _beat_once(self) -> None:
        """走一轮「必要时注册 + 心跳」。任何失败都只记 warning。"""
        try:
            uuid = read_identity(self._data_dir) or await self._register()
            if uuid is None:
                return
            await self._heartbeat(uuid)
        except Exception as exc:  # noqa: BLE001 - 遥测不得把异常带进任何功能路径
            logger.warning('telemetry_failed', error=str(exc))

    async def _register(self) -> str | None:
        """向服务端申请一个 UUID 并落盘。

        首次启动没网时这一步会失败，返回 ``None``；下一轮心跳会再试一次，
        不做退避空转，也不阻塞任何东西。

        :return: 新的 UUID；注册失败时为 ``None``。
        """
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            response = await client.post(f'{self._endpoint}/register', json={})
            response.raise_for_status()
            uuid = response.json().get('uuid')
        if not isinstance(uuid, str) or not uuid.strip():
            logger.warning('telemetry_register_no_uuid')
            return None
        write_identity(self._data_dir, uuid)
        return uuid

    async def _heartbeat(self, uuid: str) -> None:
        """发一次心跳。

        服务端认不出这个 UUID（403）时删掉本地身份，下一轮重新注册。

        :param uuid: 当前安装的标识。
        """
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            response = await client.post(
                f'{self._endpoint}/heartbeat',
                headers={'Client-UUID': uuid},
                json=payload(),
            )
        if response.status_code == 403:
            identity_path(self._data_dir).unlink(missing_ok=True)
            logger.warning('telemetry_identity_rejected')
            return
        response.raise_for_status()


def describe_for_console(enabled: bool, endpoint: str = TELEMETRY_ENDPOINT) -> list[str]:
    """给出首次启动时在控制台明说的那几行。

    默认开启意味着必须当面讲清楚：传什么、怎么关。埋在文档里不算说过。

    :param enabled: 配置开关的当前值。
    :param endpoint: 服务端根地址，为空表示尚未启用。
    :return: 供信息框呈现的文本行。
    """
    if not enabled:
        return ['匿名统计：已关闭。']
    fields = '、'.join(f'{key}={value}' for key, value in payload().items())
    rows = [
        '匿名统计：默认开启，用于统计全球有多少个安装、分别是什么版本。',
        f'  上报内容仅三项：{fields}',
        '  不含聊天内容、不含任何身份信息，也不采集 IP。',
        '  关闭方式：把 config/features.toml 的 [telemetry] enabled 改成 false。',
    ]
    if not endpoint:
        rows.append('  当前服务端尚未启用，本版本实际不会发出任何请求。')
    return rows


__all__ = [
    'HEARTBEAT_INTERVAL_S',
    'TELEMETRY_ENDPOINT',
    'TelemetryService',
    'describe_for_console',
    'identity_path',
    'payload',
    'read_identity',
    'write_identity',
]
